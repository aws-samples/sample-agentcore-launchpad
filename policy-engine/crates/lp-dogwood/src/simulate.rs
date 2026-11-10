//! The console simulator's backend (stream C): compile a draft / pinned set, replay a
//! hand-written history into a throwaway shard on a manual clock, then decide one request
//! — exactly the engine path a live call takes, so the simulator cannot disagree with the
//! gateway. Exposed as `launchpad-policy-engine replay` (JSON in / out).

use serde::Deserialize;
use serde_json::{Value, json};

use crate::clock::{NANOS, SharedClock};
use crate::compile::{CompileInput, SourcePolicy, compile};
use crate::engine::{Shard, ShardConfig};
use crate::wire::{DecideRequest, Install, RespondRequest};

const WS: &str = "sim";
const GW: &str = "AgentCore::Gateway::\"simulator\"";

#[derive(Debug, Clone, Deserialize)]
pub struct SimEvent {
    pub tool: String,
    #[serde(default = "request")]
    pub kind: String,
    pub principal: Value,
    #[serde(default)]
    pub input: serde_json::Map<String, Value>,
    #[serde(default)]
    pub output: serde_json::Map<String, Value>,
    pub at_s: i64,
    /// the resource entity (default: the simulator gateway)
    #[serde(default)]
    pub resource: Option<String>,
}

fn request() -> String {
    "request".into()
}

#[derive(Debug, Clone, Deserialize)]
pub struct SimInput {
    pub tools_list: Value,
    pub policies: Vec<SourcePolicy>,
    pub agent: Value,
    #[serde(default)]
    pub history: Vec<SimEvent>,
    pub request: SimEvent,
    #[serde(default = "default_channel")]
    pub channel: String,
    /// the workspace the set is simulated under (default `sim`): rules that name the
    /// workspace — `principal in AgentCore::Workspace::"<ws>"`, the agent chain's parent —
    /// decide like the live gateway only when the hub passes the real id (N17-obs495)
    #[serde(default = "default_ws")]
    pub ws: String,
}

fn default_channel() -> String {
    "chat".into()
}

fn default_ws() -> String {
    WS.into()
}

pub fn replay(input: &SimInput) -> Value {
    let compiled = compile(&CompileInput { tools_list: input.tools_list.clone(), policies: input.policies.clone() });
    if !compiled.ok {
        return json!({"ok": false, "errors": compiled.errors, "warnings": compiled.warnings});
    }
    let dir = std::env::temp_dir().join(format!(
        "lp-sim-{}-{}",
        std::process::id(),
        std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0)
    ));
    let result = run(input, &compiled, &dir);
    let _ = std::fs::remove_dir_all(&dir);
    match result {
        Ok(mut v) => {
            v["warnings"] = json!(compiled.warnings);
            v["temporal_referenced_actions"] = json!(compiled.temporal_referenced_actions);
            v
        }
        Err(e) => json!({"ok": false, "errors": [{"policy_id": "", "message": e}]}),
    }
}

fn run(input: &SimInput, compiled: &crate::compile::CompileOutput, dir: &std::path::Path) -> Result<Value, String> {
    let mut events: Vec<&SimEvent> = input.history.iter().collect();
    events.sort_by_key(|e| e.at_s);
    let start = events.first().map(|e| e.at_s).unwrap_or(input.request.at_s).min(input.request.at_s);
    let (clock, hand) = SharedClock::manual(start * NANOS);
    let mut shard = Shard::open(
        ShardConfig { state_dir: dir.into(), shard: 0, shards: 1, utc_offset_s: 8 * 3600 },
        clock.clone(),
    )?;
    let install: Install = serde_json::from_value(json!({
        "ws": input.ws, "policy_set_version": "sim", "temporal_set_version": 0,
        "policies": compiled.items, "schema": compiled.schema
    }))
    .map_err(|e| e.to_string())?;
    shard.install(install).map_err(|e| e.join("; "))?;
    let session = "0".repeat(64);
    let mut now = start;
    let mut trace = Vec::new();
    for e in events.iter().copied().chain(std::iter::once(&input.request)) {
        if e.at_s > now {
            hand.advance_s(e.at_s - now);
            now = e.at_s;
        }
        let action = format!("AgentCore::Action::\"{}\"", e.tool);
        if e.kind == "request" {
            let governed = compiled.temporal_referenced_actions.contains(&e.tool);
            let req: DecideRequest = serde_json::from_value(json!({
                "ws": input.ws, "session": session, "record": governed, "action": action,
                "resource": e.resource.as_deref().unwrap_or(GW),
                "agent": input.agent, "principal": e.principal,
                "context": {"input": e.input, "agent_id": input.agent["agent_id"], "channel": input.channel}
            }))
            .map_err(|err| format!("{}: {err}", e.tool))?;
            let out = match shard.decide(&req) {
                Ok(r) => serde_json::to_value(r).unwrap(),
                Err((status, err)) => json!({"status": status, "code": err.code, "message": err.message}),
            };
            trace.push(json!({"tool": e.tool, "kind": "request", "at_s": e.at_s, "result": out}));
        } else {
            let req: RespondRequest = serde_json::from_value(json!({
                "ws": input.ws, "session": session, "action": action, "kind": e.kind,
                "input": e.input, "output": e.output, "principal": e.principal,
                "agent": input.agent, "resource": e.resource.as_deref().unwrap_or(GW)
            }))
            .map_err(|err| format!("{}: {err}", e.tool))?;
            shard.respond(&req).map_err(|(_, err)| err.message.unwrap_or_default())?;
            trace.push(json!({"tool": e.tool, "kind": e.kind, "at_s": e.at_s}));
        }
    }
    let decision = trace.last().map(|t| t["result"].clone()).unwrap_or(Value::Null);
    Ok(json!({"ok": true, "decision": decision, "trace": trace}))
}
