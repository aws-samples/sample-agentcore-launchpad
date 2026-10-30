//! I4 / I5 message shapes (backend/app/policy/contracts/engine_*.schema.json).

use serde::{Deserialize, Serialize};
use serde_json::Value as Json;

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgentRef {
    pub entity: String,
    pub agent_id: String,
    pub method: String,
    pub system: bool,
    /// Trusted caller workspace, independent of the engine-set key in `ws`.
    /// Old workspace-keyed requests omit it.
    #[serde(default)]
    pub workspace_id: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PrincipalRef {
    pub entity: String,
    pub parents: Vec<String>,
    pub attrs: serde_json::Map<String, Json>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct DecideContext {
    pub input: serde_json::Map<String, Json>,
    pub agent_id: String,
    pub channel: String,
    /// contract amendment 3: the set versions the caller's bundle holds
    #[serde(default)]
    pub expect: Option<Expect>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Expect {
    pub policy_set_version: String,
    pub temporal_set_version: u64,
}

/// `POST /v1/decide` (and one item of I5 `authorize_stateless`, with `record: false`).
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DecideRequest {
    pub ws: String,
    pub session: String,
    pub record: bool,
    pub action: String,
    pub resource: String,
    pub agent: AgentRef,
    pub principal: PrincipalRef,
    pub context: DecideContext,
    /// contract amendment 2: the token's temporal-set version (`tsv`) …
    #[serde(default)]
    pub tsv: Option<u64>,
    /// … and its issue time (`iat`, unix s), for invalidation / history-loss checks.
    #[serde(default)]
    pub session_started_at: Option<i64>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct Determining {
    pub policy_id: String,
    pub version: u32,
    pub mode: String,
    pub stage: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct DecideResponse {
    pub decision: &'static str,
    pub stage_a: &'static str,
    pub stage_bt: &'static str,
    pub determining: Vec<Determining>,
    pub log_only_would: Option<&'static str>,
    pub event_seq: Option<u64>,
    pub ts_ns: Option<i64>,
    pub store_epoch: String,
    pub policy_set_version: String,
    pub errors: Vec<String>,
}

/// `POST /v1/respond`.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RespondRequest {
    pub ws: String,
    pub session: String,
    pub action: String,
    pub kind: String,
    pub input: serde_json::Map<String, Json>,
    pub output: serde_json::Map<String, Json>,
    /// contract amendment 2: the call's principal — the response event is pinned to the
    /// same (session, principal) as its request.
    pub principal: PrincipalRef,
    /// contract amendment 2: the calling agent (entity + attrs), as on the request.
    pub agent: AgentRef,
    pub resource: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct RespondResponse {
    pub event_seq: u64,
    pub ts_ns: i64,
}

#[derive(Debug, Clone, Serialize)]
pub struct EngineError {
    pub code: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub message: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub have: Option<String>,
}

impl EngineError {
    pub fn new(code: &'static str, message: impl Into<String>) -> Self {
        EngineError { code, message: Some(message.into()), have: None }
    }
}

/// One installed policy (I5 `install.policies[]`).
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InstallPolicy {
    pub id: String,
    pub version: u32,
    pub mode: String,
    pub stage: String,
    pub kind: String,
    pub text: String,
}

/// I5 `install`.
#[derive(Debug, Clone, Deserialize)]
pub struct Install {
    pub ws: String,
    pub policy_set_version: String,
    pub temporal_set_version: u64,
    pub policies: Vec<InstallPolicy>,
    pub schema: String,
    #[serde(default)]
    pub event_schema: String,
    /// N17-obs492 (re-review F3): an install whose `policy_set_version` is not NEWER than the
    /// installed one (same `<prefix>-<n>` family, `n` not greater, or the same string) is
    /// refused unless `force` — the hub sets it only to roll a shard back after a partial
    /// install or to re-sync shards that disagree.
    #[serde(default)]
    pub force: bool,
}
