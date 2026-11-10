//! The engine service loop: HTTP data plane + Unix control socket around one shard.

use std::fs::OpenOptions;
use std::io::{BufRead, BufReader, Write};
use std::net::TcpListener;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixListener;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use lp_dogwood::clock::SharedClock;
use lp_dogwood::engine::{Shard, ShardConfig};
use lp_dogwood::wire::{DecideRequest, Install, RespondRequest};
use serde_json::{Value as Json, json};

use crate::auth::Verifier;
use crate::http;

/// Bound on `/v1/list-filter` `actions[]` (review nit d): one request cannot pin the shard
/// lock for an unbounded number of evaluations.
pub const MAX_LIST_FILTER_ACTIONS: usize = 1024;

pub struct Config {
    pub state_dir: PathBuf,
    pub shard: u32,
    pub shards: u32,
    pub listen: Option<String>,
    pub control: PathBuf,
    pub key_file: PathBuf,
    pub audit: PathBuf,
    pub utc_offset_s: i64,
    pub workers: usize,
    pub limits: http::Limits,
}

pub struct State {
    pub shard: Mutex<Shard>,
    pub verifier: Verifier,
    pub audit: Mutex<std::fs::File>,
    pub clock: SharedClock,
}

pub fn run(cfg: Config) -> Result<(), String> {
    let key = std::fs::read(&cfg.key_file).map_err(|e| format!("key file {}: {e}", cfg.key_file.display()))?;
    // contract amendment 3: exactly 32 raw bytes, used as-is (no hex, no trimming)
    if key.len() != 32 {
        return Err(format!("engine key must be exactly 32 bytes (got {})", key.len()));
    }
    let clock = SharedClock::wall();
    let shard = Shard::open(
        ShardConfig { state_dir: cfg.state_dir.clone(), shard: cfg.shard, shards: cfg.shards, utc_offset_s: cfg.utc_offset_s },
        clock.clone(),
    )?;
    if let Some(parent) = cfg.audit.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let audit = OpenOptions::new().create(true).append(true).open(&cfg.audit).map_err(|e| e.to_string())?;
    let state = Arc::new(State { shard: Mutex::new(shard), verifier: Verifier::new(key, clock.now_s()), audit: Mutex::new(audit), clock });

    let listener = listen(&cfg)?;
    let control = state.clone();
    let control_path = cfg.control.clone();
    std::thread::spawn(move || {
        if let Err(e) = control_loop(&control_path, control) {
            eprintln!("launchpad-policy-engine: control socket stopped: {e}");
        }
    });
    eprintln!("launchpad-policy-engine: shard {} ready", cfg.shard);
    let handler: Arc<http::Handler> = Arc::new(move |req: &http::Request| {
        let out = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| handle(&state, req)));
        // a panic while the shard was locked may have left it half-updated: never serve from
        // it — exit so systemd restarts the shard from its durable state (re-review item 2)
        if state.shard.is_poisoned() {
            poisoned_exit();
        }
        match out {
            Ok((status, body)) => (status, body.to_string()),
            Err(panic) => std::panic::resume_unwind(panic), // answered 500 by http::call
        }
    });
    http::run(listener, cfg.workers, cfg.limits, handler).map_err(|e| e.to_string())
}

fn listen(cfg: &Config) -> Result<TcpListener, String> {
    // systemd socket activation: LISTEN_FDS=1 hands us fd 3 (the unit's .socket), and
    // LISTEN_PID names the process it is meant for (a stale env inherited by a child must
    // not make it adopt fd 3)
    if std::env::var("LISTEN_FDS").ok().as_deref() == Some("1") {
        let pid = std::env::var("LISTEN_PID").unwrap_or_default();
        if pid != std::process::id().to_string() {
            return Err(format!("LISTEN_PID {pid:?} is not this process"));
        }
        use std::os::fd::FromRawFd;
        return Ok(unsafe { TcpListener::from_raw_fd(3) });
    }
    let addr = cfg.listen.clone().ok_or("--listen or LISTEN_FDS is required")?;
    TcpListener::bind(&addr).map_err(|e| format!("listen {addr}: {e}"))
}

fn handle(state: &State, request: &http::Request) -> (u16, Json) {
    let method = request.method.as_str();
    let path = request.path.as_str();
    if method == "GET" && path == "/healthz" {
        return (200, lock(&state.shard).healthz());
    }
    let ok = state.verifier.check(
        method,
        path,
        request.header("X-LP-Ts"),
        request.header("X-LP-Nonce"),
        &request.body,
        request.header("X-LP-Sig"),
        state.clock.now_s(),
    );
    if !ok {
        return (401, json!({"code": "unauthorized"}));
    }
    if method != "POST" {
        return bad("method");
    }
    dispatch(state, path, &request.body)
}

/// Exit code of a shard that found its state lock poisoned (systemd `Restart=always`).
pub const POISONED_EXIT: i32 = 70;

fn poisoned_exit() -> ! {
    eprintln!("launchpad-policy-engine: shard state lock poisoned by a panic; exiting for a restart");
    std::process::exit(POISONED_EXIT)
}

/// Lock shared state; a poisoned lock ends the process instead of serving possibly torn state.
pub fn lock<T>(m: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|_| poisoned_exit())
}

fn bad(message: impl ToString) -> (u16, Json) {
    (400, json!({"code": "bad_request", "message": message.to_string()}))
}

pub fn dispatch(state: &State, path: &str, body: &[u8]) -> (u16, Json) {
    match path {
        "/v1/decide" => {
            let req: DecideRequest = match serde_json::from_slice(body) {
                Ok(r) => r,
                Err(e) => return bad(e),
            };
            let result = lock(&state.shard).decide(&req);
            match result {
                Ok(r) => {
                    if let Some(seq) = r.event_seq {
                        audit(state, json!({"ws": req.ws, "event_seq": seq, "ts_ns": r.ts_ns, "session": req.session,
                            "action": req.action, "kind": "request", "verdict": r.decision,
                            "determining": r.determining.iter().map(|d| format!("{}:v{}", d.policy_id, d.version)).collect::<Vec<_>>(),
                            "store_epoch": r.store_epoch, "policy_set_version": r.policy_set_version}));
                    }
                    (200, serde_json::to_value(r).unwrap())
                }
                Err((status, e)) => (status, serde_json::to_value(e).unwrap()),
            }
        }
        "/v1/respond" => {
            let req: RespondRequest = match serde_json::from_slice(body) {
                Ok(r) => r,
                Err(e) => return bad(e),
            };
            let mut shard = lock(&state.shard);
            match shard.respond(&req) {
                Ok(r) => {
                    if r.event_seq > 0 {
                        audit(state, json!({"ws": req.ws, "event_seq": r.event_seq, "ts_ns": r.ts_ns, "session": req.session,
                            "action": req.action, "kind": req.kind, "verdict": null, "determining": [],
                            "store_epoch": shard.store_epoch(), "policy_set_version":
                            shard.sets().get(&req.ws).cloned().unwrap_or_default()}));
                    }
                    (200, serde_json::to_value(r).unwrap())
                }
                Err((status, e)) => (status, serde_json::to_value(e).unwrap()),
            }
        }
        "/v1/list-filter" => list_filter(state, body),
        _ => bad("unknown route"),
    }
}

/// `tools/list` filtering, read-only and conservative: a tool is hidden only when the
/// calling agent itself is denied it (Stage A, input-independent); per-user and input /
/// history-dependent rules are decided at call time.
fn list_filter(state: &State, body: &[u8]) -> (u16, Json) {
    let Ok(req) = serde_json::from_slice::<Json>(body) else { return bad("not JSON") };
    let actions = req["actions"].as_array().cloned().unwrap_or_default();
    if actions.len() > MAX_LIST_FILTER_ACTIONS {
        return bad(format!("at most {MAX_LIST_FILTER_ACTIONS} actions"));
    }
    let mut visible = Vec::new();
    let mut shard = lock(&state.shard);
    for action in actions {
        let Some(action) = action.as_str() else { continue };
        let probe = json!({"ws": req["ws"], "session": "0".repeat(64), "record": false,
            "action": action, "resource": req["resource"].as_str().unwrap_or("AgentCore::Gateway::\"\""),
            "agent": req["agent"], "principal": req["principal"],
            "context": {"input": {}, "agent_id": req["agent"]["agent_id"], "channel": "list"}});
        let Ok(decide) = serde_json::from_value::<DecideRequest>(probe) else { return bad("bad agent / principal") };
        if let Ok(r) = shard.authorize_stateless(&decide) {
            if r.stage_a == "allow" {
                visible.push(action.to_string());
            }
        }
    }
    (200, json!({"visible": visible}))
}

fn audit(state: &State, line: Json) {
    let record = json!({"launchpad.policy.engine": line});
    let mut file = lock(&state.audit);
    let _ = writeln!(file, "{record}");
}

fn control_loop(path: &PathBuf, state: Arc<State>) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let _ = std::fs::remove_file(path);
    let listener = UnixListener::bind(path).map_err(|e| format!("bind {}: {e}", path.display()))?;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o660)).map_err(|e| e.to_string())?;
    for stream in listener.incoming() {
        let Ok(stream) = stream else { continue };
        let state = state.clone();
        std::thread::spawn(move || {
            let mut writer = match stream.try_clone() {
                Ok(w) => w,
                Err(_) => return,
            };
            for line in BufReader::new(stream).lines() {
                let Ok(line) = line else { break };
                let out = control(&state, &line);
                if writeln!(writer, "{out}").is_err() {
                    break;
                }
            }
        });
    }
    Ok(())
}

pub fn control(state: &State, line: &str) -> Json {
    let Ok(msg) = serde_json::from_str::<Json>(line) else {
        return json!({"ok": false, "errors": ["not JSON"]});
    };
    match msg["op"].as_str() {
        Some("install") => match serde_json::from_value::<Install>(msg) {
            Ok(install) => match lock(&state.shard).install(install) {
                Ok(()) => json!({"ok": true, "errors": []}),
                Err(errors) => json!({"ok": false, "errors": errors}),
            },
            Err(e) => json!({"ok": false, "errors": [e.to_string()]}),
        },
        Some("authorize_stateless") => {
            let mut shard = lock(&state.shard);
            let mut decisions = Vec::new();
            for item in msg["requests"].as_array().cloned().unwrap_or_default() {
                let mut item = item;
                if item.get("ws").is_none() {
                    item["ws"] = msg["ws"].clone();
                }
                decisions.push(match serde_json::from_value::<DecideRequest>(item) {
                    Ok(req) => match shard.authorize_stateless(&req) {
                        Ok(r) => serde_json::to_value(r).unwrap(),
                        Err((status, e)) => json!({"status": status, "code": e.code, "message": e.message}),
                    },
                    Err(e) => json!({"status": 400, "code": "bad_request", "message": e.to_string()}),
                });
            }
            json!({"ok": true, "decisions": decisions})
        }
        Some("status") => {
            let shard = lock(&state.shard);
            json!({"ok": true, "shards": [shard.healthz()]})
        }
        _ => json!({"ok": false, "errors": ["unknown op"]}),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_poisoned_shard_lock_exits_the_process_for_a_restart() {
        // the exit is observed from a child process running this same test
        if std::env::var("LP_POISON_CHILD").is_ok() {
            let m = Arc::new(Mutex::new(0u8));
            let m2 = m.clone();
            let _ = std::thread::spawn(move || {
                let _g = m2.lock().unwrap();
                panic!("poison");
            })
            .join();
            let guard = lock(&m);
            drop(guard);
            unreachable!("lock() returned on a poisoned mutex");
        }
        let out = std::process::Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "server::tests::a_poisoned_shard_lock_exits_the_process_for_a_restart", "--nocapture"])
            .env("LP_POISON_CHILD", "1")
            .output()
            .unwrap();
        assert_eq!(out.status.code(), Some(POISONED_EXIT), "{}", String::from_utf8_lossy(&out.stderr));
    }
}
