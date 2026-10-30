//! Store safety (lead review 2026-10-26): a broken workspace is quarantined without touching
//! the rest of the shard (defect 3), installs are crash-safe at every step (defect 4), a
//! response lands only on its owner shard (defect 5), an erroring forbid denies (nit a), tool
//! names outside the Gateway grammar are refused (nit c), workspace directories never collide
//! (nit g).

use lp_dogwood::clock::{NANOS, SharedClock};
use lp_dogwood::engine::{InstallFault, POLICY_EVALUATION_ERROR, Shard, ShardConfig};
use lp_dogwood::wire::{DecideRequest, Install, RespondRequest};
use serde_json::{Value, json};

const GW: &str = r#"AgentCore::Gateway::"arn:aws-cn:bedrock-agentcore:cn-northwest-1:123456789012:gateway/launchpad-gw-exampleid0""#;
const ROLE: &str = r#"AgentCore::IamEntity::"arn:aws-cn:iam::123456789012:role/launchpad-agent-hr-helpdesk-ab12cd34""#;
const T0: i64 = 1_793_022_000;
const APPROVE: &str = "hr-approvals___approve_payout";
const PAYOUT: &str = "hr-database___create_payout";

fn policies(extra: Value) -> Value {
    let mut list = json!([
        {"id": "pol_agent", "version": 1, "mode": "active", "stage": "a", "kind": "stateless",
         "text": "permit (principal in AgentCore::Agent::\"ag_hrhelp01\", action in AgentCore::Action::\"CallTool\", resource);"},
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

fn install_msg(ws: &str, version: &str, extra: Value) -> Install {
    let schema = lp_dogwood::schema::action_schema(include_str!("../assets/demo-tools.json")).unwrap();
    serde_json::from_value(json!({
        "op": "install", "ws": ws, "policy_set_version": version, "temporal_set_version": 3,
        "policies": policies(extra), "schema": schema, "event_schema": ""
    }))
    .unwrap()
}

fn install(shard: &mut Shard, ws: &str, version: &str) {
    shard.install(install_msg(ws, version, json!([]))).expect("install");
}

fn admin() -> Value {
    json!({"entity": "AgentCore::User::\"demo-admin\"", "parents": ["AgentCore::Role::\"admin\""],
           "attrs": {"username": "demo-admin", "role": "admin"}})
}

fn agent() -> Value {
    json!({"entity": ROLE, "agent_id": "ag_hrhelp01", "method": "zip_runtime", "system": false})
}

/// A session id on shard `n` (the owner is the first 8 hex digits mod the shard count).
fn session_on(n: u32, tag: u32) -> String {
    format!("{n:08x}{tag:056x}")
}

fn decide_at(shard: &mut Shard, ws: &str, tool: &str, sess: &str, started: Option<i64>) -> Value {
    let req: DecideRequest = serde_json::from_value(json!({
        "ws": ws, "session": sess, "record": true, "action": format!("AgentCore::Action::\"{tool}\""),
        "resource": GW, "agent": agent(), "principal": admin(), "tsv": 3,
        "session_started_at": started,
        "context": {"input": {"employee_id": "EMP-1024", "amount": 420}, "agent_id": "ag_hrhelp01", "channel": "chat"}
    }))
    .unwrap();
    match shard.decide(&req) {
        Ok(r) => serde_json::to_value(r).unwrap(),
        Err((status, e)) => json!({"status": status, "code": e.code, "message": e.message}),
    }
}

fn decide(shard: &mut Shard, ws: &str, tool: &str, sess: &str) -> Value {
    decide_at(shard, ws, tool, sess, None)
}

fn respond_req(ws: &str, sess: &str) -> RespondRequest {
    serde_json::from_value(json!({
        "ws": ws, "session": sess, "action": format!("AgentCore::Action::\"{APPROVE}\""),
        "kind": "response", "input": {"employee_id": "EMP-1024", "amount": 420},
        "output": {"approved": true, "approval_id": "apr-1"}, "principal": admin(),
        "agent": agent(), "resource": GW
    }))
    .unwrap()
}

/// Record an approval in `ws`, so a later payout in the same session is allowed only if the
/// history survived.
fn approve(shard: &mut Shard, ws: &str, sess: &str) {
    assert_eq!(decide(shard, ws, APPROVE, sess)["decision"], "allow");
    shard.respond(&respond_req(ws, sess)).expect("respond");
}

fn open_n(dir: &std::path::Path, clock: &SharedClock, shard: u32, shards: u32) -> Shard {
    Shard::open(ShardConfig { state_dir: dir.into(), shard, shards, utc_offset_s: 8 * 3600 }, clock.clone())
        .unwrap()
}

fn open(dir: &std::path::Path, clock: &SharedClock) -> Shard {
    open_n(dir, clock, 0, 1)
}

fn ws_dir(dir: &std::path::Path, ws: &str) -> std::path::PathBuf {
    dir.join("shard-0").join(format!("w-{}", ws.bytes().map(|b| format!("{b:02x}")).collect::<String>()))
}

fn quarantine_entries(dir: &std::path::Path) -> usize {
    std::fs::read_dir(dir.join("shard-0").join("quarantine"))
        .unwrap()
        .flatten()
        .filter(|e| e.path().is_dir())
        .count()
}

fn tempdir() -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "lp-store-{}-{}",
        std::process::id(),
        std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn corrupt_store_quarantines_only_that_workspace(corrupt: impl Fn(&std::path::Path)) {
    let dir = tempdir();
    let (clock, hand) = SharedClock::manual(T0 * NANOS);
    let mut shard = open(&dir, &clock);
    install(&mut shard, "default", "ps-1");
    install(&mut shard, "other", "ps-1");
    let sess = session_on(0, 1);
    approve(&mut shard, "other", &sess);
    approve(&mut shard, "default", &sess);
    let epoch = shard.store_epoch().to_string();
    drop(shard);

    corrupt(&ws_dir(&dir, "default"));
    hand.advance_s(60);
    let mut shard = open(&dir, &clock);
    assert!(shard.is_quarantined("default"));
    assert!(!shard.is_quarantined("other"));
    assert_eq!(shard.store_epoch(), epoch, "a quarantine must not bump the shard epoch");
    assert_eq!(decide(&mut shard, "other", PAYOUT, &sess)["decision"], "allow", "other ws kept its history");
    let r = decide(&mut shard, "default", PAYOUT, &sess);
    assert_eq!((r["status"].as_u64(), r["code"].as_str()), (Some(503), Some("unavailable")), "{r}");
    let r = shard.respond(&respond_req("default", &sess)).unwrap_err();
    assert_eq!(r.0, 503);
    assert_eq!(quarantine_entries(&dir), 1);
    drop(shard);

    // a second restart neither rescans nor re-quarantines
    let mut shard = open(&dir, &clock);
    assert!(shard.is_quarantined("default"));
    assert_eq!(shard.store_epoch(), epoch);
    assert_eq!(quarantine_entries(&dir), 1);

    // republish: the workspace is back, sessions older than the quarantine lost history
    hand.advance_s(60);
    install(&mut shard, "default", "ps-2");
    assert!(!shard.is_quarantined("default"));
    let r = decide_at(&mut shard, "default", PAYOUT, &sess, Some(T0));
    assert_eq!(r["code"], "history_lost", "{r}");
    let fresh = session_on(0, 2);
    let r = decide_at(&mut shard, "default", PAYOUT, &fresh, Some(T0 + 120));
    assert_eq!(r["decision"], "deny", "a fresh session decides normally (no approval yet): {r}");
    assert!(r["errors"].as_array().unwrap().is_empty(), "{r}");
    drop(shard);
    let shard = open(&dir, &clock);
    assert!(!shard.is_quarantined("default"), "the tombstone is cleared by the reinstall");
    assert_eq!(shard.sets()["default"], "ps-2");
}

#[test]
fn a_corrupt_store_is_quarantined_alone() {
    corrupt_store_quarantines_only_that_workspace(|ws| {
        std::fs::write(ws.join("bt.redb"), b"not a redb file at all").unwrap();
    });
}

#[test]
fn unreadable_metadata_is_quarantined_alone() {
    corrupt_store_quarantines_only_that_workspace(|ws| {
        std::fs::write(ws.join("installed.json"), b"{ torn").unwrap();
    });
}

fn crash_rolls_forward(fault: InstallFault) {
    let dir = tempdir();
    let (clock, _) = SharedClock::manual(T0 * NANOS);
    let mut shard = open(&dir, &clock);
    install(&mut shard, "default", "ps-1");
    let sess = session_on(0, 1);
    approve(&mut shard, "default", &sess);
    let epoch = shard.store_epoch().to_string();
    shard.inject_install_fault(Some(fault));
    assert!(shard.install(install_msg("default", "ps-2", json!([]))).is_err());
    drop(shard); // the crash

    // stray torn temp files are never read
    std::fs::write(ws_dir(&dir, "default").join("installed.tmp"), b"{ torn").unwrap();
    std::fs::write(dir.join("shard-0").join("epoch.tmp"), b"{ torn").unwrap();
    let mut shard = open(&dir, &clock);
    assert_eq!(shard.sets()["default"], "ps-2", "{fault:?}: the pending install is rolled forward");
    assert_eq!(shard.store_epoch(), epoch);
    assert!(!shard.is_quarantined("default"));
    assert_eq!(decide(&mut shard, "default", PAYOUT, &sess)["decision"], "allow", "{fault:?}: history kept");
    assert!(!ws_dir(&dir, "default").join("installed.pending.json").exists());
    drop(shard);
    let shard = open(&dir, &clock);
    assert_eq!(shard.sets()["default"], "ps-2");
}

#[test]
fn a_crash_after_the_pending_write_rolls_forward() {
    crash_rolls_forward(InstallFault::AfterPending);
}

#[test]
fn a_crash_after_the_store_sync_rolls_forward() {
    crash_rolls_forward(InstallFault::AfterSync);
}

#[test]
fn a_torn_pending_file_falls_back_to_the_committed_set() {
    let dir = tempdir();
    let (clock, _) = SharedClock::manual(T0 * NANOS);
    let mut shard = open(&dir, &clock);
    install(&mut shard, "default", "ps-1");
    drop(shard);
    std::fs::write(ws_dir(&dir, "default").join("installed.pending.json"), b"{ torn").unwrap();
    let shard = open(&dir, &clock);
    assert_eq!(shard.sets()["default"], "ps-1");
    assert!(!shard.is_quarantined("default"));
}

#[test]
fn a_set_that_does_not_compile_changes_nothing_on_disk() {
    let dir = tempdir();
    let (clock, _) = SharedClock::manual(T0 * NANOS);
    let mut shard = open(&dir, &clock);
    install(&mut shard, "default", "ps-1");
    let bad = json!([{"id": "broken", "version": 1, "mode": "active", "stage": "bt", "kind": "stateless",
                      "text": "forbid (principal, action, resource) when { nope };"}]);
    assert!(shard.install(install_msg("default", "ps-2", bad)).is_err());
    assert!(!ws_dir(&dir, "default").join("installed.pending.json").exists());
    assert_eq!(shard.sets()["default"], "ps-1");
    drop(shard);
    assert_eq!(open(&dir, &clock).sets()["default"], "ps-1");
}

#[test]
fn respond_is_rejected_on_a_shard_that_does_not_own_the_session() {
    let dir = tempdir();
    let (clock, _) = SharedClock::manual(T0 * NANOS);
    let mut shard = open_n(&dir, &clock, 0, 2);
    install(&mut shard, "default", "ps-1");
    let (status, e) = shard.respond(&respond_req("default", &session_on(1, 1))).unwrap_err();
    assert_eq!((status, e.code), (400, "bad_request"));
    assert!(e.message.as_deref().unwrap_or("").contains("shard 1"), "{:?}", e.message);
    // the owner accepts it
    shard.respond(&respond_req("default", &session_on(0, 1))).expect("owner shard records");
}

#[test]
fn an_erroring_forbid_denies_with_policy_evaluation_error() {
    let dir = tempdir();
    let (clock, _) = SharedClock::manual(T0 * NANOS);
    let mut shard = open(&dir, &clock);
    // Long overflow at evaluation time: Cedar skips the policy and reports an error
    let overflow = json!([{"id": "pol_overflow", "version": 1, "mode": "active", "stage": "bt", "kind": "stateless",
        "text": "forbid (principal, action == AgentCore::Action::\"hr-database___check_calendar\", resource) when { context.system.now_epoch_s * 9223372036854775807 > 0 };"}]);
    shard.install(install_msg("default", "ps-1", overflow)).expect("install");
    let req: DecideRequest = serde_json::from_value(json!({
        "ws": "default", "session": session_on(0, 1), "record": false,
        "action": "AgentCore::Action::\"hr-database___check_calendar\"", "resource": GW,
        "agent": agent(), "principal": admin(), "tsv": 3,
        "context": {"input": {"range": "this-week"}, "agent_id": "ag_hrhelp01", "channel": "chat"}
    }))
    .unwrap();
    let r = shard.decide(&req).expect("decide");
    assert_eq!(r.decision, "deny", "{r:?}");
    assert_eq!(r.errors.first().map(String::as_str), Some(POLICY_EVALUATION_ERROR), "{r:?}");
    // an unaffected tool still decides normally
    let mut ok = req.clone();
    ok.action = "AgentCore::Action::\"hr-database___list_departments\"".into();
    ok.context.input = serde_json::Map::new();
    let r = shard.decide(&ok).expect("decide");
    assert_eq!(r.decision, "allow", "{r:?}");
    assert!(r.errors.is_empty(), "{r:?}");
}

#[test]
fn tool_names_outside_the_gateway_grammar_are_refused() {
    // an empty segment (`hr-db___`, `___x`, `a______b`, `a___b___`) is refused like any other
    // character outside the grammar; a NESTED separator is not (see the next test)
    for name in ["hr\"db___x", "hr-db___x\ny", "hr db___x", "hr-db___", "___x", "a______b", "a___b___", "t___x\\"] {
        let tools = json!({"tools": [{"name": name, "inputSchema": {"type": "object", "properties": {}}}]});
        assert!(lp_dogwood::schema::action_schema(&tools.to_string()).is_err(), "{name:?} accepted");
    }
    for name in ["hr-database___get_employee", "plain_tool", "T-1___x_2", "a___b___c"] {
        let tools = json!({"tools": [{"name": name, "inputSchema": {"type": "object", "properties": {}}}]});
        assert!(lp_dogwood::schema::action_schema(&tools.to_string()).is_ok(), "{name:?} refused");
    }
}

/// An MCP-server Gateway target prefixes the remote server's own `<server>___<tool>` names with
/// its target name, so the Gateway's live `tools/list` carries NESTED separators
/// (`paipai___paipai-data___detect_anomaly` on launchpad-gw-example001, 2026-10-27). The
/// target group is the FIRST segment, the action id is the full name — exactly what the hub
/// sends (`policy_engine_decide.tool_item`) and what the D20 rewrite of
/// `resource.name like "paipai___*"` expects (`target:paipai`).
#[test]
fn nested_mcp_server_tool_names_compile_under_the_first_segment_target() {
    let tools = json!({"tools": [
        {"name": "paipai___paipai-data___detect_anomaly", "inputSchema": {"type": "object", "properties": {"station": {"type": "string"}}}},
        {"name": "paipai___paipai-forecast___forecast", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "hr-database___get_employee", "inputSchema": {"type": "object", "properties": {}}},
    ]});
    let schema = lp_dogwood::schema::action_schema(&tools.to_string()).expect("nested names compile");
    assert!(schema.contains("action \"paipai___paipai-data___detect_anomaly\" in [Action::\"CallTool\", Action::\"target:paipai\"]"), "{schema}");
    assert!(schema.contains("action \"target:paipai\" in [Action::\"CallTool\"]"), "{schema}");
    assert!(!schema.contains("target:paipai-data"), "the nested segment must not become a target group: {schema}");
    let actions = lp_dogwood::coverage::tool_actions(&schema);
    assert_eq!(
        lp_dogwood::coverage::expand("target:paipai", &actions),
        vec!["paipai___paipai-data___detect_anomaly".to_string(), "paipai___paipai-forecast___forecast".to_string()]
    );
    // a rule on the target group and one on the nested tool both type-check and classify
    let out = lp_dogwood::compile::compile(&lp_dogwood::compile::CompileInput {
        tools_list: tools.clone(),
        policies: vec![
            lp_dogwood::compile::SourcePolicy { id: "grp".into(), version: 1, mode: "active".into(),
                statement: "permit (principal in AgentCore::Agent::\"ag_energy\", action in AgentCore::Action::\"target:paipai\", resource);".into() },
            lp_dogwood::compile::SourcePolicy { id: "one".into(), version: 1, mode: "active".into(),
                statement: "forbid (principal, action == AgentCore::Action::\"paipai___paipai-data___detect_anomaly\", resource) unless { principal in AgentCore::Role::\"admin\" };".into() },
        ],
    });
    assert!(out.ok, "{:?}", out.errors);
    let stages: Vec<(String, String)> = out.items.iter().map(|i| (i.id.clone(), i.stage.clone())).collect();
    assert!(stages.contains(&("grp".into(), "a".into())) && stages.contains(&("one".into(), "bt".into())), "{stages:?}");
}

#[test]
fn workspace_directories_never_collide() {
    let dir = tempdir();
    let (clock, _) = SharedClock::manual(T0 * NANOS);
    let mut shard = open(&dir, &clock);
    // ids a lossy escaping (e.g. `/` → `_2f`, or a case-folding filesystem) would merge
    let ids = ["a/b", "a_2fb", "A/b", "w-61", "quarantine"];
    for (i, ws) in ids.iter().enumerate() {
        install(&mut shard, ws, &format!("ps-{i}"));
    }
    drop(shard);
    let shard = open(&dir, &clock);
    for (i, ws) in ids.iter().enumerate() {
        assert_eq!(shard.sets()[*ws], format!("ps-{i}"), "{ws}");
        assert!(ws_dir(&dir, ws).is_dir(), "{ws}");
    }
    let names: Vec<String> = std::fs::read_dir(dir.join("shard-0"))
        .unwrap()
        .flatten()
        .filter(|e| e.path().is_dir())
        .map(|e| e.file_name().to_string_lossy().to_string())
        .collect();
    assert!(names.iter().all(|n| n == "quarantine" || n.starts_with("w-")), "{names:?}");
}

#[test]
fn the_quarantine_directory_itself_is_never_scanned() {
    // a complete, loadable workspace layout placed directly in shard-0/quarantine/: the scan
    // must skip the directory by name, not merely because it usually lacks installed.json
    let dir = tempdir();
    let (clock, hand) = SharedClock::manual(T0 * NANOS);
    let donor = tempdir();
    let mut shard = open(&donor, &clock);
    install(&mut shard, "ghost", "ps-ghost");
    drop(shard);
    let mut shard = open(&dir, &clock);
    install(&mut shard, "default", "ps-1");
    let epoch = shard.store_epoch().to_string();
    drop(shard);
    let quarantine = dir.join("shard-0").join("quarantine");
    for entry in std::fs::read_dir(ws_dir(&donor, "ghost")).unwrap().flatten() {
        std::fs::copy(entry.path(), quarantine.join(entry.file_name())).unwrap();
    }
    let planted: Vec<_> = std::fs::read_dir(&quarantine).unwrap().flatten().map(|e| e.file_name()).collect();
    assert!(quarantine.join("installed.json").exists(), "{planted:?}");

    for _ in 0..2 {
        hand.advance_s(60);
        let shard = open(&dir, &clock);
        assert_eq!(shard.sets().keys().collect::<Vec<_>>(), vec!["default"], "quarantine/ was loaded");
        assert!(!shard.is_quarantined("ghost") && !shard.is_quarantined("quarantine"), "quarantine/ was re-quarantined");
        assert_eq!(shard.store_epoch(), epoch, "no epoch bump");
        drop(shard);
        assert!(quarantine.join("installed.json").exists(), "quarantine/ was moved or rewritten");
        let now: Vec<_> = std::fs::read_dir(&quarantine).unwrap().flatten().map(|e| e.file_name()).collect();
        assert_eq!(now.len(), planted.len(), "quarantine/ gained or lost entries: {now:?}");
    }
}

const OVERFLOW: &str = "context.system.now_epoch_s * 9223372036854775807 > 0";

fn calendar_req(record: bool) -> DecideRequest {
    serde_json::from_value(json!({
        "ws": "default", "session": session_on(0, 1), "record": record,
        "action": "AgentCore::Action::\"hr-database___check_calendar\"", "resource": GW,
        "agent": agent(), "principal": admin(), "tsv": 3,
        "context": {"input": {"range": "this-week"}, "agent_id": "ag_hrhelp01", "channel": "chat"}
    }))
    .unwrap()
}

fn forbid_calendar(id: &str, stage: &str, mode: &str) -> Value {
    json!({"id": id, "version": 1, "mode": mode, "stage": stage, "kind": "stateless",
           "text": format!("forbid (principal, action == AgentCore::Action::\"hr-database___check_calendar\", resource) when {{ {OVERFLOW} }};")})
}

#[test]
fn an_erroring_stage_a_hides_the_tool_from_list_filter() {
    let dir = tempdir();
    let (clock, _) = SharedClock::manual(T0 * NANOS);
    let mut shard = open(&dir, &clock);
    shard.install(install_msg("default", "ps-1", json!([forbid_calendar("pol_a_err", "a", "active")]))).expect("install");
    // authorize_stateless is what /v1/list-filter calls; it shows a tool only on stage_a allow
    let r = shard.authorize_stateless(&calendar_req(false)).expect("authorize");
    assert_eq!((r.stage_a, r.decision), ("deny", "deny"), "{r:?}");
    assert_eq!(r.errors.first().map(String::as_str), Some(POLICY_EVALUATION_ERROR));
}

#[test]
fn an_erroring_log_only_forbid_reports_would_deny() {
    let dir = tempdir();
    let (clock, _) = SharedClock::manual(T0 * NANOS);
    let mut shard = open(&dir, &clock);
    shard.install(install_msg("default", "ps-1", json!([forbid_calendar("pol_lo_err", "bt", "log_only")]))).expect("install");
    let r = shard.decide(&calendar_req(false)).expect("decide");
    assert_eq!(r.decision, "allow", "a log_only policy never changes the decision: {r:?}");
    assert_eq!(r.log_only_would, Some("deny"), "{r:?}");
}

#[test]
fn an_erroring_active_forbid_never_reports_would_allow() {
    let dir = tempdir();
    let (clock, _) = SharedClock::manual(T0 * NANOS);
    let mut shard = open(&dir, &clock);
    shard.install(install_msg("default", "ps-1", json!([
        forbid_calendar("pol_err", "bt", "active"),
        {"id": "pol_lo", "version": 1, "mode": "log_only", "stage": "bt", "kind": "stateless",
         "text": "forbid (principal, action == AgentCore::Action::\"hr-database___list_departments\", resource);"}
    ]))).expect("install");
    let r = shard.decide(&calendar_req(false)).expect("decide");
    assert_eq!(r.decision, "deny");
    assert_ne!(r.log_only_would, Some("allow"), "{r:?}");
}

#[test]
fn a_directory_not_named_for_its_workspace_is_set_aside() {
    let dir = tempdir();
    let (clock, _) = SharedClock::manual(T0 * NANOS);
    let mut shard = open(&dir, &clock);
    install(&mut shard, "default", "ps-1");
    let sess = session_on(0, 1);
    approve(&mut shard, "default", &sess);
    drop(shard);
    let root = dir.join("shard-0");
    for stray in ["default", "w-6f74686572"] {
        // a legacy-style name and the hex name of ANOTHER workspace ("other"), same meta
        let target = root.join(stray);
        std::fs::create_dir_all(&target).unwrap();
        for entry in std::fs::read_dir(ws_dir(&dir, "default")).unwrap().flatten() {
            std::fs::copy(entry.path(), target.join(entry.file_name())).unwrap();
        }
    }
    for _ in 0..2 {
        let mut shard = open(&dir, &clock);
        assert_eq!(shard.sets().keys().collect::<Vec<_>>(), vec!["default"]);
        assert!(!shard.is_quarantined("default") && !shard.is_quarantined("other"));
        assert_eq!(decide(&mut shard, "default", PAYOUT, &sess)["decision"], "allow", "the real dir still serves");
        drop(shard);
        assert!(!root.join("default").exists() && !root.join("w-6f74686572").exists(), "strays not set aside");
    }
}

fn failed_install_restores(fault: InstallFault) {
    let dir = tempdir();
    let (clock, _) = SharedClock::manual(T0 * NANOS);
    let mut shard = open(&dir, &clock);
    install(&mut shard, "default", "ps-1");
    let sess = session_on(0, 1);
    approve(&mut shard, "default", &sess);
    let block = json!([{"id": "pol_block", "version": 1, "mode": "active", "stage": "bt", "kind": "stateless",
                        "text": "forbid (principal, action == AgentCore::Action::\"hr-database___create_payout\", resource);"}]);
    shard.inject_install_fault(Some(fault));
    assert!(shard.install(install_msg("default", "ps-2", block)).is_err());
    shard.inject_install_fault(None);
    // the committed set is back in force — not the half-synced new one — with its history
    assert_eq!(shard.sets()["default"], "ps-1", "{fault:?}");
    assert_eq!(decide(&mut shard, "default", PAYOUT, &sess)["decision"], "allow", "{fault:?}: pol_block leaked");
    assert!(!ws_dir(&dir, "default").join("installed.pending.json").exists(), "{fault:?}: pending left behind");
    drop(shard);
    let mut shard = open(&dir, &clock);
    assert_eq!(shard.sets()["default"], "ps-1", "{fault:?}: restart disagrees with the reported failure");
    assert_eq!(decide(&mut shard, "default", PAYOUT, &sess)["decision"], "allow");
}

#[test]
fn an_error_after_the_store_sync_rolls_back_to_the_committed_set() {
    failed_install_restores(InstallFault::ErrorAfterSync);
}

#[test]
fn a_failed_commit_rename_rolls_back_and_removes_the_pending_file() {
    failed_install_restores(InstallFault::RenameFails);
}

#[test]
fn an_install_that_cannot_be_rolled_back_quarantines_the_workspace() {
    let dir = tempdir();
    let (clock, _) = SharedClock::manual(T0 * NANOS);
    let mut shard = open(&dir, &clock);
    install(&mut shard, "default", "ps-1");
    shard.inject_install_fault(Some(InstallFault::RenameAndRemoveFail));
    let errors = shard.install(install_msg("default", "ps-2", json!([]))).unwrap_err();
    assert!(errors.iter().any(|e| e.contains("quarantined")), "{errors:?}");
    shard.inject_install_fault(None);
    let sess = session_on(0, 1);
    assert_eq!(decide(&mut shard, "default", PAYOUT, &sess)["status"], 503);
    drop(shard);
    let mut shard = open(&dir, &clock);
    assert!(shard.is_quarantined("default"), "fails closed across a restart too");
    install(&mut shard, "default", "ps-3");
    assert_eq!(shard.sets()["default"], "ps-3");
}

#[test]
fn a_failed_first_install_leaves_nothing_installed() {
    let dir = tempdir();
    let (clock, _) = SharedClock::manual(T0 * NANOS);
    let mut shard = open(&dir, &clock);
    shard.inject_install_fault(Some(InstallFault::ErrorAfterSync));
    assert!(shard.install(install_msg("default", "ps-1", json!([]))).is_err());
    shard.inject_install_fault(None);
    let r = decide(&mut shard, "default", PAYOUT, &session_on(0, 1));
    assert_eq!(r["code"], "set_mismatch", "{r}");
    drop(shard);
    assert!(open(&dir, &clock).sets().is_empty());
}

#[test]
fn a_lone_mismatched_directory_is_quarantined_so_the_reinstall_reports_history_lost() {
    let dir = tempdir();
    let (clock, hand) = SharedClock::manual(T0 * NANOS);
    let mut shard = open(&dir, &clock);
    install(&mut shard, "default", "ps-1");
    let sess = session_on(0, 1);
    approve(&mut shard, "default", &sess);
    let epoch = shard.store_epoch().to_string();
    drop(shard);
    // the workspace's ONLY state sits under a wrong name (no correctly named twin)
    std::fs::rename(ws_dir(&dir, "default"), dir.join("shard-0").join("legacy-default")).unwrap();
    hand.advance_s(60);
    let mut shard = open(&dir, &clock);
    assert!(shard.is_quarantined("default"), "a lone stray must leave a tombstone");
    assert!(shard.sets().is_empty());
    assert_eq!(shard.store_epoch(), epoch);
    assert_eq!(decide(&mut shard, "default", PAYOUT, &sess)["status"], 503);
    hand.advance_s(60);
    install(&mut shard, "default", "ps-2");
    let r = decide_at(&mut shard, "default", PAYOUT, &sess, Some(T0));
    assert_eq!(r["code"], "history_lost", "the reinstall must not silently start from empty: {r}");
}

#[test]
fn a_failed_directory_fsync_after_the_commit_rename_is_a_committed_install() {
    let dir = tempdir();
    let (clock, _) = SharedClock::manual(T0 * NANOS);
    let mut shard = open(&dir, &clock);
    install(&mut shard, "default", "ps-1");
    let sess = session_on(0, 1);
    approve(&mut shard, "default", &sess);
    shard.inject_install_fault(Some(InstallFault::DirFsyncFails));
    shard.install(install_msg("default", "ps-2", json!([]))).expect("the rename is the commit");
    shard.inject_install_fault(None);
    assert_eq!(shard.sets()["default"], "ps-2");
    assert!(!ws_dir(&dir, "default").join("installed.pending.json").exists());
    assert_eq!(decide(&mut shard, "default", PAYOUT, &sess)["decision"], "allow", "history kept");
    drop(shard);
    assert_eq!(open(&dir, &clock).sets()["default"], "ps-2");
}

/// N17-obs492 (re-review F3): the install version CAS — a set that is not newer than the
/// installed one is refused unless `force` (the hub's rollback / re-sync), so a shard that
/// accepted a publish another shard refused can never be left ahead with nothing to move it.
#[test]
fn an_install_that_is_not_newer_is_refused_unless_forced() {
    let dir = tempdir();
    let (clock, _) = SharedClock::manual(1_793_022_000 * NANOS);
    let mut shard = open(&dir, &clock);
    install(&mut shard, "default", "ps-m1-1"); // a hand-installed set (another prefix family)
    install(&mut shard, "default", "ps-1"); // the hub takes over: another prefix is newer
    assert_eq!(shard.sets()["default"], "ps-1");
    let same = shard.install(install_msg("default", "ps-1", json!([]))).unwrap_err();
    assert!(same[0].contains("not newer"), "{same:?}");
    let older = shard.install(install_msg("default", "ps-0", json!([]))).unwrap_err();
    assert!(older[0].contains("not newer"), "{older:?}");
    assert_eq!(shard.sets()["default"], "ps-1", "a refused install changes nothing");
    install(&mut shard, "default", "ps-2");
    assert_eq!(shard.sets()["default"], "ps-2");
    // the hub's rollback after a partial install: an OLDER version with force
    let mut back = install_msg("default", "ps-1", json!([]));
    back.force = true;
    shard.install(back).expect("forced rollback");
    assert_eq!(shard.sets()["default"], "ps-1");
    // another workspace is independent
    install(&mut shard, "other", "ps-1");
    assert!(lp_dogwood::engine::version_is_newer("ps-7", "ps-8"));
    assert!(!lp_dogwood::engine::version_is_newer("ps-8", "ps-8"));
    assert!(!lp_dogwood::engine::version_is_newer("ps-8", "ps-7"));
    assert!(lp_dogwood::engine::version_is_newer("ps-m1-1", "ps-1"));
    assert!(lp_dogwood::engine::version_is_newer("sim", "ps-1"));
    assert!(!lp_dogwood::engine::version_is_newer("sim", "sim"));
}
