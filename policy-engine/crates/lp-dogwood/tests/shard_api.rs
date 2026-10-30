//! The shard API (I4 decide / respond, I5 install / authorize_stateless) end to end over
//! the contract's JSON shapes, with a manual clock.

use lp_dogwood::clock::{NANOS, SharedClock};
use lp_dogwood::engine::{Shard, ShardConfig};
use lp_dogwood::wire::{DecideRequest, Install, RespondRequest};
use serde_json::{Value, json};

const GW: &str = r#"AgentCore::Gateway::"arn:aws-cn:bedrock-agentcore:cn-northwest-1:123456789012:gateway/launchpad-gw-exampleid0""#;
const ROLE: &str = r#"AgentCore::IamEntity::"arn:aws-cn:iam::123456789012:role/launchpad-agent-hr-helpdesk-ab12cd34""#;

fn policies(extra: Value) -> Value {
    let mut list = json!([
        {"id": "pol_agent", "version": 1, "mode": "active", "stage": "a", "kind": "stateless",
         "text": "permit (principal in AgentCore::Agent::\"ag_hrhelp01\", action in AgentCore::Action::\"CallTool\", resource);"},
        {"id": "pol_payoutadm", "version": 2, "mode": "active", "stage": "bt", "kind": "stateless",
         "text": "forbid (principal, action == AgentCore::Action::\"hr-database___create_payout\", resource) unless { principal in AgentCore::Role::\"admin\" };"},
        {"id": "pol_verify", "version": 1, "mode": "active", "stage": "bt", "kind": "temporal",
         "text": "forbid (principal, action == AgentCore::Action::\"hr-database___get_employee\", resource) unless temporal { formerly within 8h AgentCore::Action::\"hr-approvals___verify_identity\"::response{ output.employee_id: context.input.employee_id, output.verified: true } };"},
        {"id": "pol_approval", "version": 1, "mode": "active", "stage": "bt", "kind": "temporal",
         "text": "forbid (principal, action == AgentCore::Action::\"hr-database___create_payout\", resource) unless temporal { formerly within 1h AgentCore::Action::\"hr-approvals___approve_payout\"::response{ input.employee_id: context.input.employee_id, output.approved: true } };"},
        {"id": "pol_users", "version": 1, "mode": "active", "stage": "bt", "kind": "stateless",
         "text": "permit (principal, action in AgentCore::Action::\"CallTool\", resource);"}
    ]);
    if let Value::Array(more) = extra {
        list.as_array_mut().unwrap().extend(more);
    }
    list
}

fn install(shard: &mut Shard, version: &str, tsv: u64, extra: Value) {
    let schema = lp_dogwood::schema::action_schema(include_str!("../assets/demo-tools.json")).unwrap();
    let msg: Install = serde_json::from_value(json!({
        "op": "install", "ws": "default", "policy_set_version": version,
        "temporal_set_version": tsv, "policies": policies(extra), "schema": schema,
        "event_schema": ""
    }))
    .unwrap();
    shard.install(msg).expect("install");
}

fn user(name: &str, role: &str) -> Value {
    json!({"entity": format!("AgentCore::User::\"{name}\""),
           "parents": [format!("AgentCore::Role::\"{role}\"")],
           "attrs": {"username": name, "role": role}})
}

fn session(tag: &str, who: &str) -> String {
    // any 64-hex works; derive deterministically and keep it on shard 0 of 1
    format!("{:0>64}", format!("{:x}", tag.len() * 7919 + who.len() * 104729))
}

fn decide(shard: &mut Shard, tool: &str, who: &Value, sess: &str, input: Value, record: bool) -> Value {
    let req: DecideRequest = serde_json::from_value(json!({
        "ws": "default", "session": sess, "record": record,
        "action": format!("AgentCore::Action::\"{tool}\""), "resource": GW,
        "agent": {"entity": ROLE, "agent_id": "ag_hrhelp01", "method": "zip_runtime", "system": false},
        "principal": who, "context": {"input": input, "agent_id": "ag_hrhelp01", "channel": "chat"},
        "tsv": 3
    }))
    .unwrap();
    match shard.decide(&req) {
        Ok(r) => serde_json::to_value(r).unwrap(),
        Err((status, e)) => json!({"status": status, "code": e.code, "message": e.message}),
    }
}

fn respond(shard: &mut Shard, tool: &str, who: &Value, sess: &str, input: Value, output: Value) {
    let req: RespondRequest = serde_json::from_value(json!({
        "ws": "default", "session": sess, "action": format!("AgentCore::Action::\"{tool}\""),
        "kind": "response", "input": input, "output": output, "principal": who,
        "agent": {"entity": ROLE, "agent_id": "ag_hrhelp01", "method": "zip_runtime", "system": false},
        "resource": GW
    }))
    .unwrap();
    shard.respond(&req).expect("respond");
}

fn open(dir: &std::path::Path, clock: &SharedClock) -> Shard {
    Shard::open(
        ShardConfig { state_dir: dir.into(), shard: 0, shards: 1, utc_offset_s: 8 * 3600 },
        clock.clone(),
    )
    .unwrap()
}

#[test]
fn demo_flow_two_stages_and_restart() {
    let dir = tempdir();
    let (clock, hand) = SharedClock::manual(1_793_022_000 * NANOS);
    let mut shard = open(&dir, &clock);
    install(&mut shard, "ps-1", 3, json!([]));
    let intern = user("hr-intern", "member");
    let admin = user("demo-admin", "admin");
    let payout = json!({"employee_id": "EMP-1024", "amount": 420});

    let r = decide(&mut shard, "hr-database___create_payout", &intern, &session("a", "i"), payout.clone(), true);
    assert_eq!(r["decision"], "deny", "{r}");
    assert_eq!(r["stage_a"], "allow");
    assert!(r["determining"].as_array().unwrap().iter().any(|d| d["policy_id"] == "pol_payoutadm"), "{r}");
    assert!(r["event_seq"].is_number());

    let s2 = session("bb", "admin");
    assert_eq!(decide(&mut shard, "hr-database___create_payout", &admin, &s2, payout.clone(), true)["decision"], "deny");
    decide(&mut shard, "hr-approvals___approve_payout", &admin, &s2, payout.clone(), true);
    hand.advance_s(1);
    respond(&mut shard, "hr-approvals___approve_payout", &admin, &s2, payout.clone(), json!({"approved": true, "approval_id": "apr-1"}));
    hand.advance_s(600);
    let r = decide(&mut shard, "hr-database___create_payout", &admin, &s2, payout.clone(), true);
    assert_eq!(r["decision"], "allow", "{r}");

    // stateless path refuses to skip recording for a temporal-governed tool
    let r = decide(&mut shard, "hr-database___create_payout", &admin, &s2, payout.clone(), false);
    assert_eq!(r["code"], "bad_request", "{r}");
    // an ungoverned tool decides statelessly with record=false
    let r = decide(&mut shard, "hr-database___list_departments", &intern, &s2, json!({}), false);
    assert_eq!(r["decision"], "allow", "{r}");
    assert!(r["event_seq"].is_null());

    // restart: history survives, the approval still counts
    drop(shard);
    let mut shard = open(&dir, &clock);
    let r = decide(&mut shard, "hr-database___create_payout", &admin, &s2, payout.clone(), true);
    assert_eq!(r["decision"], "allow", "after restart: {r}");

    // a newer temporal set invalidates the session (T4 / 409)
    install(&mut shard, "ps-2", 4, json!([]));
    let r = decide(&mut shard, "hr-database___create_payout", &admin, &s2, payout, true);
    assert_eq!(r["code"], "session_invalidated", "{r}");
}

#[test]
fn stage_a_denies_an_unknown_agent_and_log_only_reports_would() {
    let dir = tempdir();
    let (clock, _) = SharedClock::manual(1_793_022_000 * NANOS);
    let mut shard = open(&dir, &clock);
    install(&mut shard, "ps-1", 3, json!([
        {"id": "pol_cal_lo", "version": 1, "mode": "log_only", "stage": "bt", "kind": "stateless",
         "text": "forbid (principal, action == AgentCore::Action::\"hr-database___check_calendar\", resource);"}
    ]));
    let intern = user("hr-intern", "member");
    let s = session("c", "i");
    let r = decide(&mut shard, "hr-database___check_calendar", &intern, &s, json!({"range": "x"}), false);
    assert_eq!(r["decision"], "allow", "{r}");
    assert_eq!(r["log_only_would"], "deny", "{r}");

    // Stage A: another agent's role is not permitted by pol_agent
    let req: DecideRequest = serde_json::from_value(json!({
        "ws": "default", "session": s, "record": false,
        "action": "AgentCore::Action::\"hr-database___list_departments\"", "resource": GW,
        "agent": {"entity": "AgentCore::IamEntity::\"arn:aws-cn:iam::123456789012:role/other\"", "agent_id": "ag_other001", "method": "byoc", "system": false},
        "principal": intern, "context": {"input": {}, "agent_id": "ag_other001", "channel": "chat"}
    })).unwrap();
    let r = shard.decide(&req).unwrap();
    assert_eq!((r.decision, r.stage_a), ("deny", "deny"));
}

#[test]
fn context_expect_mismatch_is_set_mismatch() {
    let dir = tempdir();
    let (clock, _) = SharedClock::manual(1_793_022_000 * NANOS);
    let mut shard = open(&dir, &clock);
    install(&mut shard, "ps-1", 3, json!([]));
    let req = |expect: Value| -> DecideRequest {
        serde_json::from_value(json!({
            "ws": "default", "session": session("e", "i"), "record": false,
            "action": "AgentCore::Action::\"hr-database___list_departments\"", "resource": GW,
            "agent": {"entity": ROLE, "agent_id": "ag_hrhelp01", "method": "zip_runtime", "system": false},
            "principal": user("hr-intern", "member"),
            "context": {"input": {}, "agent_id": "ag_hrhelp01", "channel": "chat", "expect": expect}
        })).unwrap()
    };
    assert!(shard.decide(&req(json!({"policy_set_version": "ps-1", "temporal_set_version": 3}))).is_ok());
    let (status, e) = shard.decide(&req(json!({"policy_set_version": "ps-0", "temporal_set_version": 3}))).unwrap_err();
    assert_eq!((status, e.code, e.have.as_deref()), (409, "set_mismatch", Some("ps-1")));
}

#[test]
fn a_bad_install_keeps_the_previous_set() {
    let dir = tempdir();
    let (clock, _) = SharedClock::manual(1_793_022_000 * NANOS);
    let mut shard = open(&dir, &clock);
    install(&mut shard, "ps-1", 3, json!([]));
    let schema = lp_dogwood::schema::action_schema(include_str!("../assets/demo-tools.json")).unwrap();
    let bad: Install = serde_json::from_value(json!({
        "ws": "default", "policy_set_version": "ps-bad", "temporal_set_version": 3, "schema": schema,
        "policies": [{"id": "x", "version": 1, "mode": "active", "stage": "bt", "kind": "stateless",
                      "text": "permit (principal, action == AgentCore::Action::\"no_such_tool\", resource);"}]
    })).unwrap();
    assert!(shard.install(bad).is_err());
    assert_eq!(shard.sets()["default"], "ps-1");
}

fn tempdir() -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "lp-shard-{}-{}",
        std::process::id(),
        std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}
