//! `launchpad-policy-engine` — one shard of the Launchpad policy engine (design §13.5 H1:
//! `launchpad-policy-engine@0` / `@1` on the hub host).
//!
//! * data plane (I4): HTTP/1.1 JSON on `--listen` (or the systemd socket-activation fd
//!   3 when `LISTEN_FDS=1`), HMAC-authenticated except `GET /healthz`;
//! * control plane (I5): JSON lines on the Unix socket `--control` (mode 0660, group
//!   `lp-policy`, of which only the hub user is a member);
//! * audit (I7 engine line): one JSON line per recorded event appended to `--audit`.
//!
//! Configuration is flags or `LP_ENGINE_*` environment variables; the HMAC key is read
//! from `--key-file` (never logged).

mod auth;
mod http;
mod server;

use std::path::PathBuf;

fn arg(flag: &str, env: &str, default: Option<&str>) -> Option<String> {
    let args: Vec<String> = std::env::args().collect();
    args.iter()
        .position(|a| a == flag)
        .and_then(|i| args.get(i + 1).cloned())
        .or_else(|| std::env::var(env).ok())
        .or_else(|| default.map(String::from))
}

/// Offline subcommands for the hub (JSON on stdin, JSON on stdout, exit 0 even when the
/// document reports `ok: false`; a non-zero exit means the input itself was unreadable).
fn offline(command: &str) -> Option<i32> {
    use std::io::Read;
    let run = |f: &dyn Fn(&str) -> Result<serde_json::Value, String>| -> i32 {
        let mut stdin = String::new();
        if std::io::stdin().read_to_string(&mut stdin).is_err() {
            return 2;
        }
        match f(&stdin) {
            Ok(out) => {
                println!("{out}");
                0
            }
            Err(e) => {
                eprintln!("{e}");
                2
            }
        }
    };
    Some(match command {
        "compile" => run(&|s| {
            let input: lp_dogwood::compile::CompileInput = serde_json::from_str(s).map_err(|e| e.to_string())?;
            serde_json::to_value(lp_dogwood::compile::compile(&input)).map_err(|e| e.to_string())
        }),
        "replay" => run(&|s| {
            let input: lp_dogwood::simulate::SimInput = serde_json::from_str(s).map_err(|e| e.to_string())?;
            Ok(lp_dogwood::simulate::replay(&input))
        }),
        "bench" => {
            let iterations: usize = arg("--iterations", "LP_BENCH_ITERATIONS", Some("1000")).unwrap().parse().unwrap_or(1000);
            let shards: u32 = arg("--shards", "LP_BENCH_SHARDS", Some("2")).unwrap().parse().unwrap_or(2);
            let dir = PathBuf::from(arg("--dir", "LP_BENCH_DIR", Some(&std::env::temp_dir().display().to_string())).unwrap());
            match lp_dogwood::bench::bench(iterations, shards, &dir) {
                Ok(report) => {
                    println!("{}", serde_json::to_string_pretty(&report).unwrap());
                    if report["meets_budget"].as_bool() == Some(true) { 0 } else { 1 }
                }
                Err(e) => {
                    eprintln!("bench: {e}");
                    2
                }
            }
        }
        "version" => {
            println!("{}", serde_json::json!({"engine": env!("CARGO_PKG_VERSION"), "dogwood_local_engine": "1.1.1", "dogwood_language": "1.1.0"}));
            0
        }
        _ => return None,
    })
}

fn main() {
    if let Some(command) = std::env::args().nth(1).filter(|a| !a.starts_with("--")) {
        match offline(&command) {
            Some(code) => std::process::exit(code),
            None => {
                eprintln!("unknown command {command:?} (compile | replay | bench | version, or flags to serve)");
                std::process::exit(2);
            }
        }
    }
    let shard: u32 = arg("--shard", "LP_ENGINE_SHARD", Some("0")).unwrap().parse().expect("--shard");
    let cfg = server::Config {
        state_dir: PathBuf::from(arg("--state-dir", "LP_ENGINE_STATE_DIR", Some("/var/lib/launchpad-policy-engine")).unwrap()),
        shard,
        shards: arg("--shards", "LP_ENGINE_SHARDS", Some("2")).unwrap().parse().expect("--shards"),
        listen: arg("--listen", "LP_ENGINE_LISTEN", None),
        control: PathBuf::from(arg(
            "--control",
            "LP_ENGINE_CONTROL",
            Some(&format!("/run/launchpad-policy-engine/control-{shard}.sock")),
        )
        .unwrap()),
        key_file: PathBuf::from(arg("--key-file", "LP_ENGINE_KEY_FILE", Some("/etc/launchpad-policy-engine/engine-key")).unwrap()),
        audit: PathBuf::from(arg(
            "--audit",
            "LP_ENGINE_AUDIT",
            Some(&format!("/var/lib/launchpad-policy-engine/audit/shard-{shard}.jsonl")),
        )
        .unwrap()),
        utc_offset_s: arg("--utc-offset-s", "LP_ENGINE_UTC_OFFSET_S", Some("28800")).unwrap().parse().expect("offset"),
        workers: arg("--workers", "LP_ENGINE_WORKERS", Some("4")).unwrap().parse().expect("workers"),
        limits: {
            let d = http::Limits::default();
            let n = |flag, env, v: usize| arg(flag, env, Some(&v.to_string())).unwrap().parse().expect(flag);
            http::Limits {
                max_connections: n("--max-connections", "LP_ENGINE_MAX_CONNECTIONS", d.max_connections),
                max_per_peer: n("--max-connections-per-peer", "LP_ENGINE_MAX_CONNECTIONS_PER_PEER", d.max_per_peer),
                max_inflight_body_bytes: n("--max-inflight-body-bytes", "LP_ENGINE_MAX_INFLIGHT_BODY_BYTES", d.max_inflight_body_bytes),
            }
        },
    };
    if let Err(e) = server::run(cfg) {
        eprintln!("launchpad-policy-engine: {e}");
        std::process::exit(1);
    }
}
