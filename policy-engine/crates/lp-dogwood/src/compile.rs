//! The policy compiler (stream C3): a workspace's stored policies + the Gateway's
//! `tools/list` → the generated schema and the engine's install items (one statement each,
//! classified into Stage A / Stage BT, stateless / temporal), Global's quotas (T8) and
//! validation diagnostics. Like Global, nothing is permitted implicitly: a principal with no
//! permit that covers the action is denied (Cedar default-deny; the compiler adds no permit of
//! its own). Exposed to the hub as the `launchpad-policy-engine compile` subcommand (JSON in /
//! out).

use std::collections::BTreeSet;

use dogwood_language::{ParsedPolicySet, PrincipalConstraint, Validator};
use serde::{Deserialize, Serialize};

use crate::schema::action_schema;
use crate::stateless::lower;
use crate::workspace::service_schema;

/// Global's Policy-in-AgentCore quotas (bedrock-agentcore-limits.html, T8).
pub const MAX_POLICIES: usize = 1000;
pub const MAX_TEMPORAL_POLICIES: usize = 20;
pub const MAX_TEMPORAL_OPERATORS: usize = 3;
pub const MAX_POLICY_BYTES: usize = 10 * 1024;
pub const MAX_TOTAL_BYTES: usize = 200 * 1024;
pub const MAX_SCHEMA_BYTES: usize = 400 * 1024;

#[derive(Debug, Clone, Deserialize)]
pub struct SourcePolicy {
    pub id: String,
    pub version: u32,
    #[serde(default = "active")]
    pub mode: String,
    pub statement: String,
}

fn active() -> String {
    "active".into()
}

#[derive(Debug, Clone, Deserialize)]
pub struct CompileInput {
    pub tools_list: serde_json::Value,
    pub policies: Vec<SourcePolicy>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct Item {
    pub id: String,
    pub version: u32,
    pub mode: String,
    pub stage: String,
    pub kind: String,
    pub text: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct Diagnostic {
    pub policy_id: String,
    pub message: String,
}

#[derive(Debug, Clone, Serialize, Default)]
pub struct CompileOutput {
    pub ok: bool,
    pub schema: String,
    pub items: Vec<Item>,
    pub errors: Vec<Diagnostic>,
    pub warnings: Vec<Diagnostic>,
    pub actions: Vec<String>,
    pub temporal_count: usize,
    pub temporal_referenced_actions: Vec<String>,
}

fn diag(id: &str, message: impl Into<String>) -> Diagnostic {
    Diagnostic { policy_id: id.to_string(), message: message.into() }
}

/// Stage of a statement from its principal scope (design §4.1 / §13.3): agent-scoped →
/// `a`; human-scoped → `bt`; unscoped → both.
fn stages(scope: &PrincipalConstraint) -> &'static [&'static str] {
    let type_of = |uid: &dogwood_language::cedar::EntityUid| uid.type_name().to_string();
    let agentish = |ty: &str| matches!(ty, "AgentCore::IamEntity" | "AgentCore::Agent" | "AgentCore::Workspace");
    match scope {
        PrincipalConstraint::Any => &["a", "bt"],
        PrincipalConstraint::Is(ty) | PrincipalConstraint::IsIn(ty, _) => {
            if agentish(&ty.to_string()) { &["a"] } else { &["bt"] }
        }
        PrincipalConstraint::In(Some(uid)) | PrincipalConstraint::Eq(Some(uid)) => {
            if agentish(&type_of(uid)) { &["a"] } else { &["bt"] }
        }
        _ => &["a", "bt"],
    }
}

const HUMAN_MARKERS: [&str; 6] = [
    "AgentCore::Role::", "AgentCore::User::", "AgentCore::ApiKey", "AgentCore::System::",
    "principal.username", "principal.role",
];
const AGENT_MARKERS: [&str; 4] =
    ["AgentCore::Agent::", "AgentCore::IamEntity", "AgentCore::Workspace::", "principal.agent_id"];

fn mentions_both(text: &str) -> bool {
    HUMAN_MARKERS.iter().any(|m| text.contains(m)) && AGENT_MARKERS.iter().any(|m| text.contains(m))
}

/// For an unscoped head: `bt` when the body only talks about users / roles / keys,
/// `a` when it only talks about the agent, `None` when neither (applies to both stages) or
/// both (ambiguous — rejected by the caller).
fn body_stage(text: &str) -> Option<&'static str> {
    let human = HUMAN_MARKERS.iter().any(|m| text.contains(m));
    let agent = AGENT_MARKERS.iter().any(|m| text.contains(m));
    match (human, agent) {
        (true, false) => Some("bt"),
        (false, true) => Some("a"),
        _ => None,
    }
}

/// Whether the statement carries its own `@id` (from the lowered AST, not the text).
fn has_id_annotation(text: &str, schema: &str, service: &dogwood_language::ServiceSchema) -> bool {
    lower(text, schema, service)
        .map(|l| l.as_cedar().policies().any(|p| p.annotation("id").is_some()))
        .unwrap_or(false)
}

pub fn compile(input: &CompileInput) -> CompileOutput {
    let mut out = CompileOutput::default();
    let schema = match action_schema(&input.tools_list.to_string()) {
        Ok(s) => s,
        Err(e) => {
            out.errors.push(diag("", e.to_string()));
            return out;
        }
    };
    if schema.len() > MAX_SCHEMA_BYTES {
        out.errors.push(diag("", format!("generated schema is {} bytes (> {MAX_SCHEMA_BYTES})", schema.len())));
    }
    let actions = crate::coverage::tool_actions(&schema);
    let service = service_schema();
    if input.policies.len() > MAX_POLICIES {
        out.errors.push(diag("", format!("{} policies (> {MAX_POLICIES})", input.policies.len())));
    }
    let total: usize = input.policies.iter().map(|p| p.statement.len()).sum();
    if total > MAX_TOTAL_BYTES {
        out.errors.push(diag("", format!("policies total {total} bytes (> {MAX_TOTAL_BYTES})")));
    }
    let mut temporal_refs: BTreeSet<String> = BTreeSet::new();
    let mut temporal_policies = 0usize;
    for p in &input.policies {
        if p.statement.len() > MAX_POLICY_BYTES {
            out.errors.push(diag(&p.id, format!("{} bytes (> {MAX_POLICY_BYTES})", p.statement.len())));
            continue;
        }
        if !matches!(p.mode.as_str(), "active" | "log_only") {
            out.errors.push(diag(&p.id, format!("mode {:?} is not active | log_only", p.mode)));
            continue;
        }
        let parsed = match ParsedPolicySet::parse(&p.statement, &service) {
            Ok(set) => set,
            Err(e) => {
                out.errors.push(diag(&p.id, e.to_string()));
                continue;
            }
        };
        let mut any_temporal = false;
        for (k, q) in parsed.policies().enumerate() {
            let id = if parsed.policy_count() > 1 { format!("{}#{k}", p.id) } else { p.id.clone() };
            let text = q.expanded_source();
            if has_id_annotation(&text, &schema, &service) {
                out.errors.push(diag(&p.id, "remove the @id annotation; ids are assigned on publish"));
                continue;
            }
            let temporal = q.uses_temporal();
            any_temporal |= temporal;
            if temporal && q.temporal_count() > MAX_TEMPORAL_OPERATORS {
                out.errors.push(diag(&p.id, format!(
                    "{} temporal conditions (> {MAX_TEMPORAL_OPERATORS} per policy)", q.temporal_count()
                )));
            }
            let mut targets = stages(&q.scope().principal()).to_vec();
            if targets.len() == 2 {
                // an unscoped head: the body decides which principal it talks about
                match body_stage(&text) {
                    Some(stage) => targets = vec![stage],
                    None if mentions_both(&text) => {
                        out.errors.push(diag(&p.id, "the statement tests both the agent and the user; split it into an agent rule and a user rule"));
                        continue;
                    }
                    None => {}
                }
            }
            if temporal {
                if targets == ["a"] {
                    out.errors.push(diag(&p.id, "temporal policies cannot be scoped to the agent itself; scope them to users or leave the principal unscoped"));
                    continue;
                }
                targets = vec!["bt"];
                // the actions to record: the SAME function the engine's must_record uses
                temporal_refs.extend(crate::coverage::temporal_referenced([text.as_str()], &actions, &service));
            }
            // a statement that does not lower is reported against ITS id (the stage
            // type-check below would only name the stage)
            if lower(&text, &schema, &service).is_err() {
                out.errors.push(diag(&p.id, "the statement does not lower against the schema"));
                continue;
            }
            for stage in targets {
                out.items.push(Item {
                    id: id.clone(),
                    version: p.version,
                    mode: p.mode.clone(),
                    stage: stage.to_string(),
                    kind: if temporal { "temporal" } else { "stateless" }.to_string(),
                    text: text.clone(),
                });
            }
        }
        if any_temporal {
            temporal_policies += 1;
        }
    }
    if temporal_policies > MAX_TEMPORAL_POLICIES {
        out.errors.push(diag("", format!("{temporal_policies} temporal policies (> {MAX_TEMPORAL_POLICIES})")));
    }
    // type-check each stage's whole set against the schema
    for stage in ["a", "bt"] {
        let text: String = out
            .items
            .iter()
            .filter(|i| i.stage == stage)
            .map(|i| format!("@id(\"{}:v{}\")\n{}", i.id, i.version, i.text))
            .collect::<Vec<_>>()
            .join("\n\n");
        if text.is_empty() {
            continue;
        }
        match lower(&text, &schema, &service) {
            Ok(lowered) => {
                let result = Validator::new().validate(&lowered);
                for e in result.validation_errors() {
                    out.errors.push(diag(&format!("stage {stage}"), e.to_string()));
                }
                for w in result.validation_warnings() {
                    out.warnings.push(diag(&format!("stage {stage}"), w.to_string()));
                }
            }
            Err(e) => out.errors.push(diag(&format!("stage {stage}"), e)),
        }
    }
    out.ok = out.errors.is_empty();
    out.schema = schema;
    out.actions = actions;
    out.temporal_count = temporal_policies;
    out.temporal_referenced_actions = temporal_refs.into_iter().collect();
    out
}
