//! The decide-latency benchmark (design §13.5 budget; M1 exit criterion): per shard,
//! stateless vs recorded decisions on the engine's own code path, on a real directory so
//! the recorded figure includes the durable append's fsync. Exposed as
//! `launchpad-policy-engine bench [--iterations N] [--shards S] [--dir PATH]`.

use std::path::Path;
use std::time::Instant;

use serde_json::{Value, json};

use crate::clock::SharedClock;
use crate::compile::{CompileInput, compile};
use crate::engine::{Shard, ShardConfig};
use crate::wire::{DecideRequest, Install};

const TOOLS: &str = include_str!("../assets/demo-tools.json");

fn percentile(sorted: &[f64], p: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    let idx = ((sorted.len() as f64 - 1.0) * p).round() as usize;
    sorted[idx.min(sorted.len() - 1)]
}

fn summary(mut ms: Vec<f64>) -> Value {
    ms.sort_by(|a, b| a.partial_cmp(b).unwrap());
    json!({
        "n": ms.len(),
        "p50_ms": (percentile(&ms, 0.50) * 1000.0).round() / 1000.0,
        "p95_ms": (percentile(&ms, 0.95) * 1000.0).round() / 1000.0,
        "p99_ms": (percentile(&ms, 0.99) * 1000.0).round() / 1000.0,
        "max_ms": (ms.last().copied().unwrap_or(0.0) * 1000.0).round() / 1000.0,
    })
}

fn request(tool: &str, record: bool, session: &str) -> DecideRequest {
    serde_json::from_value(json!({
        "ws": "bench", "session": session, "record": record,
        "action": format!("AgentCore::Action::\"{tool}\""), "resource": "AgentCore::Gateway::\"bench\"",
        "agent": {"entity": "AgentCore::IamEntity::\"bench-role\"", "agent_id": "ag_bench", "method": "zip_runtime", "system": false},
        "principal": {"entity": "AgentCore::User::\"bench\"", "parents": ["AgentCore::Role::\"member\""],
                      "attrs": {"username": "bench", "role": "member"}},
        "context": {"input": {"employee_id": "EMP-1024", "amount": 1}, "agent_id": "ag_bench", "channel": "bench"}
    }))
    .expect("bench request")
}

/// Run the benchmark under `dir` (created, then removed). Budgets (§13.5): decide p99 ≤ 10 ms.
pub fn bench(iterations: usize, shards: u32, dir: &Path) -> Result<Value, String> {
    let compiled = compile(&CompileInput {
        tools_list: serde_json::from_str(TOOLS).map_err(|e| e.to_string())?,
        policies: serde_json::from_value(json!([
            {"id": "a", "version": 1, "statement": "permit (principal in AgentCore::Agent::\"ag_bench\", action in AgentCore::Action::\"CallTool\", resource);"},
            {"id": "adm", "version": 1, "statement": "forbid (principal, action == AgentCore::Action::\"hr-database___create_payout\", resource) unless { principal in AgentCore::Role::\"admin\" };"},
            {"id": "t", "version": 1, "statement": "forbid (principal, action == AgentCore::Action::\"hr-database___create_payout\", resource) unless temporal { formerly within 1h AgentCore::Action::\"hr-approvals___approve_payout\"::response{ input.employee_id: context.input.employee_id, output.approved: true } };"}
        ]))
        .map_err(|e| e.to_string())?,
    });
    if !compiled.ok {
        return Err(format!("{:?}", compiled.errors));
    }
    let mut per_shard = Vec::new();
    for shard_no in 0..shards.max(1) {
        let root = dir.join(format!("bench-{}-{shard_no}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let mut shard = Shard::open(
            ShardConfig { state_dir: root.clone(), shard: 0, shards: 1, utc_offset_s: 0 },
            SharedClock::wall(),
        )?;
        let install: Install = serde_json::from_value(json!({
            "ws": "bench", "policy_set_version": "bench", "temporal_set_version": 1,
            "policies": compiled.items, "schema": compiled.schema
        }))
        .map_err(|e| e.to_string())?;
        shard.install(install).map_err(|e| e.join("; "))?;
        let mut run = |tool: &str, record: bool| -> Result<Vec<f64>, String> {
            let mut out = Vec::with_capacity(iterations);
            for i in 0..iterations + iterations / 10 {
                // a spread of sessions, like real traffic
                let session = format!("{:064x}", (i % 64) as u64 * 0x9e37_79b9 + u64::from(shard_no));
                let req = request(tool, record, &session);
                let started = Instant::now();
                shard.decide(&req).map_err(|(_, e)| format!("{}: {:?}", e.code, e.message))?;
                if i >= iterations / 10 {
                    out.push(started.elapsed().as_secs_f64() * 1000.0); // warm-up excluded
                }
            }
            Ok(out)
        };
        let stateless = run("hr-database___list_departments", false)?;
        let recorded = run("hr-database___create_payout", true)?;
        drop(shard);
        let _ = std::fs::remove_dir_all(&root);
        per_shard.push(json!({"shard": shard_no, "stateless": summary(stateless), "recorded": summary(recorded)}));
    }
    let worst_p99 = per_shard
        .iter()
        .flat_map(|s| [s["stateless"]["p99_ms"].as_f64(), s["recorded"]["p99_ms"].as_f64()])
        .flatten()
        .fold(0.0_f64, f64::max);
    Ok(json!({
        "iterations": iterations, "dir": dir.display().to_string(), "shards": per_shard,
        "budget": {"decide_p99_ms": 10.0}, "meets_budget": worst_p99 <= 10.0,
    }))
}
