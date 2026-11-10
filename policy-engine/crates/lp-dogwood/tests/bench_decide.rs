//! Engine-internal decide latency (no HTTP): prints p50 / p99 for recorded and stateless
//! decisions. `cargo test --release --test bench_decide -- --ignored --nocapture`.

use lp_dogwood::clock::SharedClock;
use lp_dogwood::compile::{CompileInput, compile};
use lp_dogwood::engine::{Shard, ShardConfig};
use lp_dogwood::wire::{DecideRequest, Install};
use serde_json::json;

#[test]
#[ignore = "benchmark: cargo test --release --test bench_decide -- --ignored --nocapture"]
fn decide_latency() {
    let out = compile(&serde_json::from_value::<CompileInput>(json!({
        "tools_list": serde_json::from_str::<serde_json::Value>(include_str!("../assets/demo-tools.json")).unwrap(),
        "policies": [
            {"id": "a", "version": 1, "statement": "permit (principal in AgentCore::Agent::\"ag\", action in AgentCore::Action::\"CallTool\", resource);"},
            {"id": "t", "version": 1, "statement": "forbid (principal, action == AgentCore::Action::\"hr-database___create_payout\", resource) unless temporal { formerly within 1h AgentCore::Action::\"hr-approvals___approve_payout\"::response{ input.employee_id: context.input.employee_id, output.approved: true } };"}
        ]
    })).unwrap());
    assert!(out.ok, "{:?}", out.errors);
    let dir = std::env::temp_dir().join(format!("lp-bench-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    let mut shard = Shard::open(ShardConfig { state_dir: dir.clone(), shard: 0, shards: 1, utc_offset_s: 0 }, SharedClock::wall()).unwrap();
    shard.install(serde_json::from_value::<Install>(json!({"ws": "w", "policy_set_version": "1", "temporal_set_version": 1, "policies": out.items, "schema": out.schema})).unwrap()).unwrap();
    let req = |tool: &str, record: bool| -> DecideRequest {
        serde_json::from_value(json!({
            "ws": "w", "session": "0".repeat(64), "record": record,
            "action": format!("AgentCore::Action::\"{tool}\""), "resource": "AgentCore::Gateway::\"g\"",
            "agent": {"entity": "AgentCore::IamEntity::\"r\"", "agent_id": "ag", "method": "zip_runtime", "system": false},
            "principal": {"entity": "AgentCore::User::\"u\"", "parents": ["AgentCore::Role::\"member\""], "attrs": {"username": "u", "role": "member"}},
            "context": {"input": {"employee_id": "E", "amount": 1}, "agent_id": "ag", "channel": "chat"}
        })).unwrap()
    };
    for (label, tool, record) in [("recorded", "hr-database___create_payout", true), ("stateless", "hr-database___list_departments", false)] {
        let r = req(tool, record);
        let mut ms: Vec<f64> = (0..300).map(|_| {
            let t = std::time::Instant::now();
            shard.decide(&r).unwrap();
            t.elapsed().as_secs_f64() * 1000.0
        }).collect();
        ms.sort_by(|a, b| a.partial_cmp(b).unwrap());
        println!("{label}: p50={:.3}ms p99={:.3}ms", ms[150], ms[296]);
        let r2 = r.clone();
        let mut series = vec![];
        for i in 0..600 { let t = std::time::Instant::now(); shard.decide(&r2).unwrap(); if i % 100 == 0 { series.push(format!("{:.2}", t.elapsed().as_secs_f64()*1000.0)); } }
        println!("{label} growth (every 100th): {series:?}");
    }
    let _ = std::fs::remove_dir_all(&dir);
}
