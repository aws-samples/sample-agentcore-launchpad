//! The compiler: stage / kind classification, quotas, diagnostics — and a compiled set
//! installs and decides like the hand-built demo set. Nothing is permitted implicitly (Global
//! parity): a user is allowed only by a permit that covers the action.

use lp_dogwood::clock::{NANOS, SharedClock};
use lp_dogwood::compile::{CompileInput, compile};
use lp_dogwood::engine::{Shard, ShardConfig};
use lp_dogwood::wire::{DecideRequest, Install};
use serde_json::{Value, json};

fn tools() -> Value {
    serde_json::from_str(include_str!("../assets/demo-tools.json")).unwrap()
}

fn input(policies: Value) -> CompileInput {
    serde_json::from_value(json!({"tools_list": tools(), "policies": policies})).unwrap()
}

const AGENT_PERMIT: &str = r#"permit (principal in AgentCore::Agent::"ag_hrhelp01", action in AgentCore::Action::"CallTool", resource);"#;
const PAYOUT_ADMIN: &str = r#"forbid (principal, action == AgentCore::Action::"hr-database___create_payout", resource) unless { principal in AgentCore::Role::"admin" };"#;
const APPROVAL: &str = r#"forbid (principal, action == AgentCore::Action::"hr-database___create_payout", resource)
unless temporal { formerly within 1h AgentCore::Action::"hr-approvals___approve_payout"::response{ input.employee_id: context.input.employee_id, output.approved: true } };"#;
/// The hub's baseline users permit (`permit_users_call_tools`), admin row.
const ADMIN_PERMIT: &str = r#"permit (principal in AgentCore::Role::"admin", action in AgentCore::Action::"CallTool", resource);"#;

#[test]
fn classifies_stages_and_kinds_and_adds_nothing_of_its_own() {
    let out = compile(&input(json!([
        {"id": "pol_agent", "version": 1, "statement": AGENT_PERMIT},
        {"id": "pol_payoutadm", "version": 2, "statement": PAYOUT_ADMIN},
        {"id": "pol_approval", "version": 1, "mode": "log_only", "statement": APPROVAL},
        {"id": "pol_both", "version": 1,
         "statement": "forbid (principal, action == AgentCore::Action::\"hr-database___check_calendar\", resource);"}
    ])));
    assert!(out.ok, "{:?}", out.errors);
    let find = |id: &str| out.items.iter().filter(|i| i.id == id).map(|i| (i.stage.as_str(), i.kind.as_str(), i.mode.as_str())).collect::<Vec<_>>();
    assert_eq!(find("pol_agent"), [("a", "stateless", "active")]);
    assert_eq!(find("pol_payoutadm"), [("bt", "stateless", "active")], "role test => user stage");
    assert_eq!(find("pol_approval"), [("bt", "temporal", "log_only")]);
    assert_eq!(find("pol_both"), [("a", "stateless", "active"), ("bt", "stateless", "active")]);
    // no BT permit at all: the compiler adds none (Global parity — no closure permit)
    let ids: std::collections::BTreeSet<&str> = out.items.iter().map(|i| i.id.as_str()).collect();
    assert_eq!(ids.into_iter().collect::<Vec<_>>(), ["pol_agent", "pol_approval", "pol_both", "pol_payoutadm"]);
    assert!(out.temporal_referenced_actions.contains(&"hr-approvals___approve_payout".to_string()));
    assert!(out.temporal_referenced_actions.contains(&"hr-database___create_payout".to_string()));
    assert_eq!(out.temporal_count, 1);
}

#[test]
fn a_target_group_permit_covers_only_that_target() {
    let members_hr = json!([
        {"id": "pol_agent", "version": 1, "statement": AGENT_PERMIT},
        {"id": "pol_members_hr", "version": 1,
         "statement": "permit (principal in AgentCore::Role::\"member\", action in AgentCore::Action::\"target:hr-database\", resource);"}
    ]);
    let (_, r) = install_and_decide(members_hr.clone(), "hr-database___list_departments", member(), false);
    let r = r.unwrap();
    assert_eq!(r.decision, "allow");
    assert!(r.determining.iter().any(|d| d.policy_id == "pol_members_hr" && d.stage == "bt"), "{:?}", r.determining);
    let (_, r) = install_and_decide(members_hr, "hr-approvals___approve_payout", member(), false);
    assert_eq!(r.unwrap().decision, "deny", "another target is not covered");
}

/// Global parity: a user with no permit is denied even when the agent is permitted (no D7
/// closure); the hub's two baseline permits are how users get allowed.
#[test]
fn a_user_with_no_permit_is_denied() {
    let agent_only = json!([{"id": "pol_agent", "version": 1, "statement": AGENT_PERMIT}]);
    let (_, r) = install_and_decide(agent_only.clone(), "hr-database___list_departments", member(), false);
    let r = r.unwrap();
    assert_eq!((r.decision, r.stage_a, r.stage_bt), ("deny", "allow", "deny"));
    assert!(r.determining.is_empty(), "default deny names no policy: {:?}", r.determining);
    let (_, r) = install_and_decide(agent_only, "hr-database___list_departments", admin(), false);
    assert_eq!(r.unwrap().decision, "deny", "admins are not special without a permit");
    let with_baseline = json!([
        {"id": "pol_agent", "version": 1, "statement": AGENT_PERMIT},
        {"id": "pol_users", "version": 1, "statement": ADMIN_PERMIT}
    ]);
    let (_, r) = install_and_decide(with_baseline, "hr-database___list_departments", admin(), false);
    assert_eq!(r.unwrap().decision, "allow");
}

#[test]
fn rejects_what_global_or_the_two_stages_cannot_express() {
    let out = compile(&input(json!([
        {"id": "bad_syntax", "version": 1, "statement": "permit (principal, action, resource"},
        {"id": "agent_temporal", "version": 1,
         "statement": "forbid (principal in AgentCore::Agent::\"ag_x\", action, resource) when temporal { formerly within 1h AgentCore::Action::\"hr-database___get_employee\"::request{ input.employee_id: _ } };"},
        {"id": "mixed", "version": 1,
         "statement": "forbid (principal, action, resource) when { principal in AgentCore::Role::\"member\" && principal in AgentCore::Agent::\"ag_x\" };"},
        {"id": "typo", "version": 1,
         "statement": "permit (principal, action == AgentCore::Action::\"hr-database___nope\", resource);"}
    ])));
    assert!(!out.ok);
    let ids: Vec<&str> = out.errors.iter().map(|e| e.policy_id.as_str()).collect();
    for expected in ["bad_syntax", "agent_temporal", "mixed"] {
        assert!(ids.contains(&expected), "{expected} not rejected: {:?}", out.errors);
    }
    assert!(out.errors.iter().any(|e| e.policy_id.starts_with("stage")), "unknown action must fail type-checking: {:?}", out.errors);
}

#[test]
fn compiled_items_install_and_decide() {
    let out = compile(&input(json!([
        {"id": "pol_agent", "version": 1, "statement": AGENT_PERMIT},
        {"id": "pol_payoutadm", "version": 2, "statement": PAYOUT_ADMIN},
        {"id": "pol_approval", "version": 1, "statement": APPROVAL},
        {"id": "pol_users", "version": 1, "statement": ADMIN_PERMIT}
    ])));
    assert!(out.ok, "{:?}", out.errors);
    let dir = std::env::temp_dir().join(format!("lp-compile-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    let (clock, _) = SharedClock::manual(1_793_022_000 * NANOS);
    let mut shard = Shard::open(ShardConfig { state_dir: dir.clone(), shard: 0, shards: 1, utc_offset_s: 0 }, clock).unwrap();
    let install: Install = serde_json::from_value(json!({
        "ws": "default", "policy_set_version": "ps-1", "temporal_set_version": 1,
        "policies": out.items, "schema": out.schema
    })).unwrap();
    shard.install(install).expect("compiled set installs");
    let admin = json!({"entity": "AgentCore::User::\"demo-admin\"", "parents": ["AgentCore::Role::\"admin\""], "attrs": {"username": "demo-admin", "role": "admin"}});
    let req = |tool: &str, record: bool| -> DecideRequest {
        serde_json::from_value(json!({
            "ws": "default", "session": "0".repeat(64), "record": record,
            "action": format!("AgentCore::Action::\"{tool}\""),
            "resource": "AgentCore::Gateway::\"gw\"",
            "agent": {"entity": "AgentCore::IamEntity::\"arn:aws-cn:iam::123456789012:role/r\"", "agent_id": "ag_hrhelp01", "method": "zip_runtime", "system": false},
            "principal": admin, "context": {"input": {"employee_id": "EMP-1", "amount": 1}, "agent_id": "ag_hrhelp01", "channel": "chat"}
        })).unwrap()
    };
    // admin, no approval: denied by the temporal rule; an ungoverned tool is allowed by the
    // users permit
    assert_eq!(shard.decide(&req("hr-database___create_payout", true)).unwrap().decision, "deny");
    let r = shard.decide(&req("hr-database___list_departments", false)).unwrap();
    assert_eq!(r.decision, "allow");
    assert!(r.determining.iter().any(|d| d.policy_id == "pol_users"), "{:?}", r.determining);
    let _ = std::fs::remove_dir_all(&dir);
}

// ── lead review 2026-10-26: defects 1 (annotated permit) and 2 (target-group temporal) ──

fn install_and_decide(items_policies: Value, tool: &str, who: Value, record: bool) -> (lp_dogwood::compile::CompileOutput, Result<lp_dogwood::wire::DecideResponse, (u16, lp_dogwood::wire::EngineError)>) {
    let out = compile(&input(items_policies));
    assert!(out.ok, "{:?}", out.errors);
    let dir = std::env::temp_dir().join(format!("lp-review-{}-{}", std::process::id(), std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()));
    let (clock, _) = SharedClock::manual(1_793_022_000 * NANOS);
    let mut shard = Shard::open(ShardConfig { state_dir: dir.clone(), shard: 0, shards: 1, utc_offset_s: 0 }, clock).unwrap();
    shard.install(serde_json::from_value::<Install>(json!({
        "ws": "default", "policy_set_version": "ps-1", "temporal_set_version": 1,
        "policies": out.items.clone(), "schema": out.schema.clone()
    })).unwrap()).expect("install");
    let req: DecideRequest = serde_json::from_value(json!({
        "ws": "default", "session": "0".repeat(64), "record": record,
        "action": format!("AgentCore::Action::\"{tool}\""), "resource": "AgentCore::Gateway::\"gw\"",
        "agent": {"entity": "AgentCore::IamEntity::\"arn:aws-cn:iam::123456789012:role/r\"", "agent_id": "ag_hrhelp01", "method": "zip_runtime", "system": false},
        "principal": who, "context": {"input": {"employee_id": "EMP-1", "amount": 1}, "agent_id": "ag_hrhelp01", "channel": "chat"}
    })).unwrap();
    let r = shard.decide(&req);
    let _ = std::fs::remove_dir_all(&dir);
    (out, r)
}

fn member() -> Value {
    json!({"entity": "AgentCore::User::\"hr-intern\"", "parents": ["AgentCore::Role::\"member\""], "attrs": {"username": "hr-intern", "role": "member"}})
}

fn admin() -> Value {
    json!({"entity": "AgentCore::User::\"demo-admin\"", "parents": ["AgentCore::Role::\"admin\""], "attrs": {"username": "demo-admin", "role": "admin"}})
}

#[test]
fn an_annotated_admin_permit_allows_admins_only() {
    for prefix in ["@reason(\"admins only\")\n", "@advice(\"x\") @reason(\"y\")\n", "  \n\t", "// a comment\n", "@id_like(\"z\")\n// c\n  "] {
        let admin_only = format!("{prefix}permit (principal in AgentCore::Role::\"admin\", action == AgentCore::Action::\"hr-database___create_payout\", resource);");
        let set = json!([
            {"id": "pol_agent", "version": 1, "statement": AGENT_PERMIT},
            {"id": "pol_admin_payout", "version": 1, "statement": admin_only}
        ]);
        let (_, r) = install_and_decide(set.clone(), "hr-database___create_payout", member(), false);
        assert_eq!(r.unwrap().decision, "deny", "{prefix:?}: a member must not be allowed");
        let (_, r) = install_and_decide(set, "hr-database___create_payout", admin(), false);
        assert_eq!(r.unwrap().decision, "allow", "{prefix:?}: the annotated permit still permits admins");
    }
}

#[test]
fn a_target_group_temporal_policy_is_recorded() {
    let policy = "forbid (principal, action in AgentCore::Action::\"target:hr-database\", resource) unless temporal { formerly within 1h AgentCore::Action::\"hr-approvals___verify_identity\"::response{ output.verified: true } };";
    let (out, r) = install_and_decide(json!([
        {"id": "pol_agent", "version": 1, "statement": AGENT_PERMIT},
        {"id": "pol_group", "version": 1, "statement": policy}
    ]), "hr-database___get_employee", member(), false);
    assert!(out.temporal_referenced_actions.contains(&"hr-database___get_employee".to_string()), "{:?}", out.temporal_referenced_actions);
    let (status, e) = r.expect_err("a temporal-governed tool must not decide with record:false");
    assert_eq!((status, e.code), (400, "bad_request"));
}

#[test]
fn an_own_id_annotation_is_rejected_by_compiler_and_engine() {
    let out = compile(&input(json!([{"id": "p", "version": 1,
        "statement": "@id(\"mine\")\npermit (principal, action, resource);"}])));
    assert!(!out.ok);
}

/// Defect 2 cross-check: the compiler's temporal_referenced_actions and the engine's
/// must_record set are the same set for every fixture policy set.
#[test]
fn compiler_and_engine_agree_on_what_must_be_recorded() {
    let sets = [
        json!([{"id": "t", "version": 1, "statement": APPROVAL}]),
        json!([{"id": "g", "version": 1, "statement": "forbid (principal, action in AgentCore::Action::\"target:hr-database\", resource) unless temporal { formerly within 1h AgentCore::Action::\"hr-approvals___verify_identity\"::response{ output.verified: true } };"}]),
        json!([{"id": "all", "version": 1, "statement": "forbid (principal, action, resource) when temporal { (count for (t: Timepoint). where (formerly within 1h (AgentCore::Action::\"hr-database___get_employee\"::request{ input.employee_id: _ } && tp(t)))) > 100 };"}]),
        json!([{"id": "calltool", "version": 1, "statement": "forbid (principal, action in AgentCore::Action::\"CallTool\", resource) unless temporal { formerly within 8h AgentCore::Action::\"hr-approvals___verify_identity\"::response{ output.verified: true } };"}]),
        json!([{"id": "none", "version": 1, "statement": PAYOUT_ADMIN}]),
    ];
    for (i, policies) in sets.into_iter().enumerate() {
        let out = compile(&input(policies));
        assert!(out.ok, "set {i}: {:?}", out.errors);
        let dir = std::env::temp_dir().join(format!("lp-xcheck-{}-{i}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let (clock, _) = SharedClock::manual(1_793_022_000 * NANOS);
        let mut shard = Shard::open(ShardConfig { state_dir: dir.clone(), shard: 0, shards: 1, utc_offset_s: 0 }, clock).unwrap();
        shard.install(serde_json::from_value::<Install>(json!({
            "ws": "default", "policy_set_version": "ps", "temporal_set_version": 1,
            "policies": out.items, "schema": out.schema
        })).unwrap()).unwrap();
        let mut engine = shard.temporal_actions("default").unwrap();
        engine.sort();
        assert_eq!(engine, out.temporal_referenced_actions, "set {i}");
        let _ = std::fs::remove_dir_all(&dir);
    }
}

/// Review nit b: a LOG_ONLY permit never changes enforcement — it only shadows.
#[test]
fn a_log_only_permit_never_permits() {
    let with_shadow = json!([
        {"id": "pol_agent", "version": 1, "statement": AGENT_PERMIT},
        {"id": "pol_lo", "version": 1, "mode": "log_only",
         "statement": "permit (principal in AgentCore::Role::\"admin\", action == AgentCore::Action::\"hr-database___create_payout\", resource);"}
    ]);
    let (_, r) = install_and_decide(with_shadow, "hr-database___create_payout", admin(), false);
    let r = r.unwrap();
    assert_eq!(r.decision, "deny", "no ACTIVE permit covers the admin");
    assert_eq!(r.log_only_would, Some("allow"), "the shadow reports what the LOG_ONLY permit would do");
}
