//! Engine requests → Dogwood events (contract "Vocabulary and event facts").

use std::collections::BTreeMap;

use dogwood_language::{Event, EventBuilder, LoweredPolicySet, Value};
use serde_json::Value as Json;

use crate::values::{FieldTypes, to_value};
use crate::wire::{AgentRef, DecideRequest, PrincipalRef, RespondRequest};

pub const ACTION_NS: [&str; 2] = ["AgentCore", "Action"];
const ACTION_PREFIX: &str = "AgentCore::Action::\"";

/// `AgentCore::Action::"hr-database___get_employee"` → `hr-database___get_employee`.
pub fn action_id(action: &str) -> Option<&str> {
    action.strip_prefix(ACTION_PREFIX)?.strip_suffix('"')
}

/// Declared field types per action (from the lowered set's event signatures): one map
/// with `input.*` and `output.*` paths, used to coerce JSON numbers.
#[derive(Debug, Default, Clone)]
pub struct ActionTypes(BTreeMap<String, FieldTypes>);

impl ActionTypes {
    pub fn from_lowered(lowered: &LoweredPolicySet) -> Self {
        let mut out: BTreeMap<String, FieldTypes> = BTreeMap::new();
        for sig in lowered.event_signatures() {
            let entry = out.entry(sig.action().to_string()).or_default();
            for field in sig.fields() {
                if let dogwood_language::EventFieldType::Cedar(ty) = field.field_type() {
                    entry.insert(field.path().to_vec(), ty.clone());
                }
            }
        }
        ActionTypes(out)
    }

    fn of(&self, action: &str) -> FieldTypes {
        self.0.get(action).cloned().unwrap_or_default()
    }
}

fn group(json: &serde_json::Map<String, Json>, group: &str, types: &FieldTypes) -> BTreeMap<String, Value> {
    let mut out = BTreeMap::new();
    let mut path = vec![group.to_string()];
    for (key, value) in json {
        path.push(key.clone());
        if let Some(v) = to_value(value, &mut path, types) {
            out.insert(key.clone(), v);
        }
        path.pop();
    }
    out
}

fn attrs(json: &serde_json::Map<String, Json>) -> Vec<(String, Value)> {
    let none = FieldTypes::new();
    json.iter()
        .filter_map(|(k, v)| to_value(v, &mut vec![k.clone()], &none).map(|v| (k.clone(), v)))
        .collect()
}

fn agent_uid(agent_id: &str) -> String {
    format!("AgentCore::Agent::\"{agent_id}\"")
}

/// The scope + entity store + Launchpad context every event of this call carries.
fn base(
    action: &str,
    kind: &str,
    principal: &PrincipalRef,
    agent: &AgentRef,
    ws: &str,
    resource: &str,
    session: &str,
    channel: &str,
    now_s: i64,
    local: (i64, i64),
) -> EventBuilder {
    let ws = agent.workspace_id.as_deref().unwrap_or(ws);
    let principal_attrs = attrs(&principal.attrs);
    let b = Event::builder_for(&ACTION_NS, action, kind)
        .principal(&principal.entity)
        .resource(resource)
        .entity(&principal.entity, principal_attrs.iter().map(|(k, v)| (k.as_str(), v.clone())))
        .entity_parents(&principal.entity, principal.parents.iter().map(String::as_str))
        // the agent chain: IamEntity -> Agent -> Workspace (Stage A rules and `in Agent`)
        .entity(&agent.entity, [("agent_id", Value::String(agent.agent_id.clone()))])
        .entity_parents(&agent.entity, [agent_uid(&agent.agent_id).as_str()])
        .entity(
            &agent_uid(&agent.agent_id),
            [("method", Value::String(agent.method.clone())), ("system", Value::Bool(agent.system))],
        )
        .entity_parents(
            &agent_uid(&agent.agent_id),
            [format!("AgentCore::Workspace::\"{ws}\"").as_str()],
        )
        .request_context("launchpad", "sessionId", Value::String(session.into()))
        .request_context("launchpad", "agent_id", Value::String(agent.agent_id.clone()))
        .request_context("launchpad", "channel", Value::String(channel.into()))
        .request_context("system", "now_epoch_s", Value::Int(now_s))
        .request_context("system", "local_hour", Value::Int(local.0))
        .request_context("system", "local_weekday", Value::Int(local.1))
        .logged_field("sessionId", Value::String(session.into()));
    b.logged_field("requestId", Value::String(String::new()))
}

/// The request event for one decision. `as_agent` builds the Stage-A form (principal =
/// the calling agent's IamEntity); otherwise the principal is the token's.
pub fn request_event(
    req: &DecideRequest,
    action: &str,
    types: &ActionTypes,
    as_agent: bool,
    now_s: i64,
    local: (i64, i64),
) -> EventBuilder {
    let agent_principal = PrincipalRef {
        entity: req.agent.entity.clone(),
        parents: vec![agent_uid(&req.agent.agent_id)],
        attrs: serde_json::Map::from_iter([(
            "agent_id".to_string(),
            Json::String(req.agent.agent_id.clone()),
        )]),
    };
    let principal = if as_agent { &agent_principal } else { &req.principal };
    let field_types = types.of(action);
    let input = group(&req.context.input, "input", &field_types);
    let mut b = base(
        action, "request", principal, &req.agent, &req.ws, &req.resource, &req.session,
        &req.context.channel, now_s, local,
    );
    for (k, v) in input {
        b = b.field("input", &k, v.clone()).request_context("input", &k, v);
    }
    b
}

/// The response / error event recorded after the call.
pub fn response_event(
    req: &RespondRequest,
    action: &str,
    types: &ActionTypes,
    now_s: i64,
    local: (i64, i64),
) -> EventBuilder {
    let field_types = types.of(action);
    let mut b = base(
        action, &req.kind, &req.principal, &req.agent, &req.ws, &req.resource, &req.session,
        "", now_s, local,
    );
    for (k, v) in group(&req.input, "input", &field_types) {
        b = b.field("input", &k, v);
    }
    if req.kind == "response" {
        for (k, v) in group(&req.output, "output", &field_types) {
            b = b.field("output", &k, v);
        }
    }
    b
}
