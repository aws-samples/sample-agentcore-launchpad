//! C0 spike: the §15.6 demo policies on the durable engine with an injected clock —
//! Stage BT (the token principal, recorded history) with an explicit users permit.

use std::sync::Arc;
use std::sync::atomic::{AtomicI64, Ordering};

use dogwood_language::{Event, EventBuilder, Value};
use dogwood_local_engine::{Clock, DurableConfig, DurableTemporalEngine, Outcome};

const NS: i64 = 1_000_000_000;
const GW: &str = r#"AgentCore::Gateway::"arn:aws-cn:bedrock-agentcore:cn-northwest-1:123456789012:gateway/launchpad-gw-exampleid0""#;

const POLICIES: &str = r#"
@id("pol_payoutadm:v1")
forbid (principal, action == AgentCore::Action::"hr-database___create_payout", resource)
unless { principal in AgentCore::Role::"admin" };

@id("pol_verify:v1")
forbid (principal, action == AgentCore::Action::"hr-database___get_employee", resource)
unless temporal {
    formerly within 8h AgentCore::Action::"hr-approvals___verify_identity"::response{
        output.employee_id: context.input.employee_id,
        output.verified: true
    }
};

@id("pol_approval:v1")
forbid (principal, action == AgentCore::Action::"hr-database___create_payout", resource)
unless temporal {
    formerly within 1h AgentCore::Action::"hr-approvals___approve_payout"::response{
        input.employee_id: context.input.employee_id,
        output.approved: true
    }
};

@id("pol_users:v1")
permit (principal, action in AgentCore::Action::"CallTool", resource);
"#;

#[derive(Clone)]
struct TestClock(Arc<AtomicI64>);
impl Clock for TestClock {
    fn now_nanos(&self) -> i64 {
        self.0.load(Ordering::SeqCst)
    }
}

fn user(name: &str, role: &str) -> (String, String, String, String) {
    (
        format!("AgentCore::User::\"{name}\""),
        format!("AgentCore::Role::\"{role}\""),
        name.to_string(),
        role.to_string(),
    )
}

fn ev(action: &str, kind: &str, who: &(String, String, String, String), session: &str, input: &[(&str, Value)],
      output: &[(&str, Value)]) -> EventBuilder {
    let mut b = Event::builder_for(&["AgentCore", "Action"], action, kind)
        .principal(&who.0)
        .resource(GW)
        .entity_parents(&who.0, [who.1.as_str()])
        .entity(&who.0, [("username", Value::String(who.2.clone())), ("role", Value::String(who.3.clone()))])
        .request_context("launchpad", "sessionId", Value::String(session.into()))
        .request_context("launchpad", "agent_id", Value::String("ag_hrhelp01".into()))
        .request_context("launchpad", "channel", Value::String("chat".into()))
        .request_context("system", "now_epoch_s", Value::Int(0))
        .request_context("system", "local_hour", Value::Int(10))
        .request_context("system", "local_weekday", Value::Int(1))
        .logged_field("sessionId", Value::String(session.into()))
        .logged_field("requestId", Value::String("r".into()));
    for (k, v) in input {
        b = b.field("input", k, v.clone()).request_context("input", k, v.clone());
    }
    for (k, v) in output {
        b = b.field("output", k, v.clone());
    }
    b
}

fn verdict(engine: &mut DurableTemporalEngine, b: EventBuilder) -> (bool, Vec<String>) {
    match engine.submit(b).expect("submit").outcome {
        Outcome::Decision(d) => (
            d.allowed(),
            d.diagnostics()
                .reason()
                .filter_map(|r| r.annotation_id().map(String::from))
                .chain(d.diagnostics().errors().map(|e| format!("ERR {e}")))
                .collect(),
        ),
        Outcome::Recorded => panic!("request must decide"),
    }
}

fn s(v: &str) -> Value {
    Value::String(v.into())
}

#[test]
fn demo_policies_decide_as_specified() {
    let dir = std::env::temp_dir().join(format!("lp-spike-{}", std::process::id()));
    let _ = std::fs::remove_file(&dir);
    let now = Arc::new(AtomicI64::new(1_793_022_000 * NS));
    let mut config = DurableConfig::new(0);
    config.clock = Box::new(TestClock(now.clone()));
    let mut engine = DurableTemporalEngine::open_with_config(&dir, config).expect("open");
    let schema = lp_dogwood::schema::action_schema(include_str!("../assets/demo-tools.json")).unwrap();
    engine
        .install(POLICIES, &schema, Some(lp_dogwood::schema::EVENT_SCHEMA), None)
        .expect("install");

    let intern = user("hr-intern", "member");
    let admin = user("demo-admin", "admin");
    let payout = [("employee_id", s("EMP-1024")), ("amount", Value::Decimal("420.0".into()))];

    // 1. member: payout denied by the admin-only rule
    let (ok, why) = verdict(&mut engine, ev("hr-database___create_payout", "request", &intern, "s1", &payout, &[]));
    assert!(!ok);
    assert!(why.iter().any(|w| w == "pol_payoutadm:v1"), "{why:?}");

    // 2. admin, no approval yet: denied by the approval rule
    let (ok, why) = verdict(&mut engine, ev("hr-database___create_payout", "request", &admin, "s2", &payout, &[]));
    assert!(!ok, "{why:?}");

    // 3. admin approves (request + response), then payout is allowed
    let approve_in = [("employee_id", s("EMP-1024")), ("amount", Value::Decimal("420.0".into()))];
    let (ok, _) = verdict(&mut engine, ev("hr-approvals___approve_payout", "request", &admin, "s2", &approve_in, &[]));
    assert!(ok);
    now.fetch_add(NS, Ordering::SeqCst);
    engine.submit(ev("hr-approvals___approve_payout", "response", &admin, "s2", &approve_in,
        &[("approved", Value::Bool(true)), ("approval_id", s("apr-1"))])).unwrap();
    now.fetch_add(20 * 60 * NS, Ordering::SeqCst);
    let (ok, why) = verdict(&mut engine, ev("hr-database___create_payout", "request", &admin, "s2", &payout, &[]));
    assert!(ok, "{why:?}");

    // 4. the approval does not leak into another session (session pin) ...
    let (ok, _) = verdict(&mut engine, ev("hr-database___create_payout", "request", &admin, "s3", &payout, &[]));
    assert!(!ok);
    // ... and expires after 1 h
    now.fetch_add(2 * 3600 * NS, Ordering::SeqCst);
    let (ok, _) = verdict(&mut engine, ev("hr-database___create_payout", "request", &admin, "s2", &payout, &[]));
    assert!(!ok);

    // 5. get_employee: only the employee id verified earlier in the session
    let verify_in = [("employee_id", s("EMP-1024")), ("code", s("x"))];
    verdict(&mut engine, ev("hr-approvals___verify_identity", "request", &intern, "s4", &verify_in, &[]));
    engine.submit(ev("hr-approvals___verify_identity", "response", &intern, "s4", &verify_in,
        &[("employee_id", s("EMP-1024")), ("verified", Value::Bool(true))])).unwrap();
    now.fetch_add(NS, Ordering::SeqCst);
    let (ok, _) = verdict(&mut engine, ev("hr-database___get_employee", "request", &intern, "s4", &[("employee_id", s("EMP-2048"))], &[]));
    assert!(!ok);
    let (ok, _) = verdict(&mut engine, ev("hr-database___get_employee", "request", &intern, "s4", &[("employee_id", s("EMP-1024"))], &[]));
    assert!(ok);
    // another principal in the same session id does not inherit the verification
    let (ok, _) = verdict(&mut engine, ev("hr-database___get_employee", "request", &admin, "s4", &[("employee_id", s("EMP-1024"))], &[]));
    assert!(!ok);
    drop(engine);
    let _ = std::fs::remove_file(&dir);
}
