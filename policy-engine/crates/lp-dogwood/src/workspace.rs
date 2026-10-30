//! One workspace's installed policy set on one shard: Stage A (stateless, the agent),
//! Stage BT (the token principal: stateless part in memory, the full set durable), each
//! with an optional LOG_ONLY shadow (contract §I5, design §13.3 / §12.11).

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use dogwood_language::{Event, ParsedPolicySet, ServiceSchema};
use dogwood_local_engine::{DurableConfig, DurableTemporalEngine, Outcome, Submitted, Verb};
use serde::{Deserialize, Serialize};

use crate::clock::SharedClock;
use crate::events::ActionTypes;
use crate::schema::EVENT_SCHEMA;
use crate::stateless::{StatelessSet, lower};
use crate::wire::{Determining, InstallPolicy};

/// What a verdict came from: the effective (ACTIVE-only) set and, when the workspace has
/// LOG_ONLY policies, the shadow (ACTIVE + LOG_ONLY) set.
#[derive(Debug, Clone)]
pub struct StageVerdict {
    pub allowed: bool,
    pub determining: Vec<Determining>,
    pub shadow_allowed: Option<bool>,
    pub errors: Vec<String>,
    pub submitted: Option<(u64, i64)>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InstalledMeta {
    pub ws: String,
    pub policy_set_version: String,
    pub temporal_set_version: u64,
    pub temporal_count: usize,
    pub schema: String,
    pub policies: Vec<StoredPolicy>,
    /// set when this workspace was re-installed after its store was quarantined: recorded
    /// calls of sessions that started earlier answer 409 `history_lost`
    #[serde(default)]
    pub history_lost_at_s: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StoredPolicy {
    pub id: String,
    pub version: u32,
    pub mode: String,
    pub stage: String,
    pub kind: String,
    pub text: String,
}

impl From<&InstallPolicy> for StoredPolicy {
    fn from(p: &InstallPolicy) -> Self {
        StoredPolicy {
            id: p.id.clone(),
            version: p.version,
            mode: p.mode.clone(),
            stage: p.stage.clone(),
            kind: p.kind.clone(),
            text: p.text.clone(),
        }
    }
}

pub struct Workspace {
    pub meta: InstalledMeta,
    pub types: ActionTypes,
    /// action ids a temporal policy covers or references — those calls must record
    pub temporal_actions: BTreeSet<String>,
    stage_a: StatelessSet,
    stage_a_shadow: Option<StatelessSet>,
    bt_stateless: StatelessSet,
    bt_stateless_shadow: Option<StatelessSet>,
    bt_store: Option<DurableTemporalEngine>,
    bt_shadow_store: Option<DurableTemporalEngine>,
}

fn annotated(p: &StoredPolicy) -> String {
    format!("@id(\"{}:v{}\")\n{}", p.id, p.version, p.text.trim())
}

fn texts<'a>(policies: impl Iterator<Item = &'a StoredPolicy>) -> String {
    policies.map(annotated).collect::<Vec<_>>().join("\n\n")
}

pub fn service_schema() -> ServiceSchema {
    ServiceSchema::builder()
        .event_schema_str(EVENT_SCHEMA)
        .build()
        .expect("the bundled event schema is valid")
}

/// Every policy must be exactly one statement without its own `@id` (the engine stamps
/// `@id("<policy_id>:v<n>")`, which is what decisions report back).
pub fn check_policies(policies: &[StoredPolicy], service: &ServiceSchema) -> Vec<String> {
    let mut errors = Vec::new();
    for p in policies {
        if !matches!(p.mode.as_str(), "active" | "log_only")
            || !matches!(p.stage.as_str(), "a" | "bt")
            || !matches!(p.kind.as_str(), "stateless" | "temporal")
        {
            errors.push(format!("{}: bad mode / stage / kind", p.id));
            continue;
        }
        // a statement with its own @id fails to parse once the engine's @id is prepended
        // (duplicate annotation) — no text heuristics here
        match ParsedPolicySet::parse(&annotated(p), service) {
            Ok(set) if set.policy_count() == 1 => {
                let temporal = set.policies().any(|q| q.uses_temporal());
                if temporal != (p.kind == "temporal") {
                    errors.push(format!("{}: kind {} does not match the statement", p.id, p.kind));
                }
                if temporal && p.stage == "a" {
                    errors.push(format!("{}: temporal policies belong to stage bt", p.id));
                }
            }
            Ok(set) => errors.push(format!("{}: {} statements; one per policy", p.id, set.policy_count())),
            Err(e) => errors.push(format!("{}: {e}", p.id)),
        }
    }
    errors
}

fn open_store(path: &Path, clock: &SharedClock) -> Result<DurableTemporalEngine, String> {
    std::fs::create_dir_all(path.parent().unwrap_or(Path::new("."))).map_err(|e| e.to_string())?;
    DurableTemporalEngine::open_with_config(path, DurableConfig::new(1000).with_clock(clock.boxed()))
        .map_err(|e| format!("open {}: {e}", path.display()))
}

impl Workspace {
    /// Everything `build` checks, without touching a durable store.
    pub fn validate(meta: &InstalledMeta) -> Result<(), Vec<String>> {
        let service = service_schema();
        let errors = check_policies(&meta.policies, &service);
        if !errors.is_empty() {
            return Err(errors);
        }
        for stage in ["a", "bt"] {
            for shadow in [false, true] {
                let text = texts(meta.policies.iter().filter(|p| {
                    p.stage == stage && (shadow || p.mode == "active")
                }));
                if text.trim().is_empty() {
                    continue;
                }
                lower(&text, &meta.schema, &service).map_err(|e| vec![format!("stage {stage}: {e}")])?;
            }
        }
        Ok(())
    }

    /// Build (or rebuild after restart) from installed metadata. Durable stores live at
    /// `<dir>/bt.redb` and `<dir>/bt-shadow.redb`; reopening recovers their history.
    pub fn build(meta: InstalledMeta, dir: &Path, clock: &SharedClock) -> Result<Self, Vec<String>> {
        let service = service_schema();
        let errors = check_policies(&meta.policies, &service);
        if !errors.is_empty() {
            return Err(errors);
        }
        let pick = |stage: &str, kind: Option<&str>, shadow: bool| {
            texts(meta.policies.iter().filter(|p| {
                p.stage == stage
                    && kind.is_none_or(|k| p.kind == k)
                    && (shadow || p.mode == "active")
            }))
        };
        let has_log_only = |stage: &str| meta.policies.iter().any(|p| p.stage == stage && p.mode == "log_only");
        let mk = |text: String| StatelessSet::new(&text, &meta.schema, &service);
        let errs = |e: String| vec![e];
        let stage_a = mk(pick("a", None, false)).map_err(errs)?;
        let stage_a_shadow = if has_log_only("a") { Some(mk(pick("a", None, true)).map_err(errs)?) } else { None };
        let bt_stateless = mk(pick("bt", Some("stateless"), false)).map_err(errs)?;
        let bt_stateless_shadow = if has_log_only("bt") {
            Some(mk(pick("bt", Some("stateless"), true)).map_err(errs)?)
        } else {
            None
        };
        // types come from lowering the full BT set (all actions are in the schema)
        let full_bt = pick("bt", None, true);
        let types = if full_bt.trim().is_empty() {
            ActionTypes::default()
        } else {
            ActionTypes::from_lowered(&lower(&full_bt, &meta.schema, &service).map_err(errs)?)
        };
        let install = |path: PathBuf, stage_policies: Vec<String>| -> Result<Option<DurableTemporalEngine>, Vec<String>> {
            if stage_policies.is_empty() {
                return Ok(None);
            }
            let mut store = open_store(&path, clock).map_err(errs)?;
            sync_store(&mut store, &stage_policies, &meta.schema)
                .map_err(|e| vec![format!("install {}: {e}", path.display())])?;
            Ok(Some(store))
        };
        let annotated_bt = |shadow: bool| -> Vec<String> {
            meta.policies
                .iter()
                .filter(|p| p.stage == "bt" && (shadow || p.mode == "active"))
                .map(annotated)
                .collect()
        };
        let bt_store = install(dir.join("bt.redb"), annotated_bt(false))?;
        let bt_shadow_store =
            if has_log_only("bt") { install(dir.join("bt-shadow.redb"), annotated_bt(true))? } else { None };
        // the SAME coverage the compiler reports as temporal_referenced_actions (defect 2)
        let all_tools = crate::coverage::tool_actions(&meta.schema);
        let temporal_actions = crate::coverage::temporal_referenced(
            meta.policies.iter().filter(|p| p.kind == "temporal").map(|p| p.text.as_str()),
            &all_tools,
            &service,
        );
        Ok(Workspace {
            meta,
            types,
            temporal_actions,
            stage_a,
            stage_a_shadow,
            bt_stateless,
            bt_stateless_shadow,
            bt_store,
            bt_shadow_store,
        })
    }

    pub fn must_record(&self, action: &str) -> bool {
        self.temporal_actions.contains(action)
    }

    fn mode_of(&self, stamp: &str) -> (String, u32, String) {
        let (id, version) = stamp.rsplit_once(":v").unwrap_or((stamp, "0"));
        let mode = self
            .meta
            .policies
            .iter()
            .find(|p| p.id == id)
            .map(|p| p.mode.clone())
            .unwrap_or_else(|| "active".into());
        (id.to_string(), version.parse().unwrap_or(0), mode)
    }

    fn reasons(&self, ids: impl Iterator<Item = String>, stage: &str) -> Vec<Determining> {
        ids.map(|stamp| {
            let (policy_id, version, mode) = self.mode_of(&stamp);
            Determining { policy_id, version, mode, stage: stage.into() }
        })
        .collect()
    }

    pub fn stage_a(&mut self, event: &Event) -> StageVerdict {
        let active = self.stage_a.decide(event);
        let shadow = self.stage_a_shadow.as_mut().map(|s| s.decide(event));
        self.stateless_verdict(active, shadow, "a")
    }

    /// Stage BT without recording (the action is not temporal-governed).
    pub fn stage_bt_stateless(&mut self, event: &Event) -> StageVerdict {
        let active = self.bt_stateless.decide(event);
        let shadow = self.bt_stateless_shadow.as_mut().map(|s| s.decide(event));
        self.stateless_verdict(active, shadow, "bt")
    }

    fn stateless_verdict(
        &self,
        active: Option<dogwood_language::Response>,
        shadow: Option<Option<dogwood_language::Response>>,
        stage: &str,
    ) -> StageVerdict {
        let allowed = active.as_ref().is_some_and(|r| r.allowed());
        let (mut determining, errors) = match &active {
            Some(r) => (
                self.reasons(rule_stamps(r, &self.meta.policies, stage), stage),
                r.diagnostics().errors().map(String::from).collect(),
            ),
            None => (vec![], vec![]),
        };
        // an erroring shadow evaluation would deny (re-review item 4), like the active one
        let shadow_allowed =
            shadow.map(|s| s.as_ref().is_some_and(|r| r.allowed() && r.diagnostics().errors().next().is_none()));
        determining.dedup();
        StageVerdict { allowed, determining, shadow_allowed, errors, submitted: None }
    }

    /// Stage BT recorded in the durable store(s): the request event becomes history.
    pub fn stage_bt_record(&mut self, event: dogwood_language::EventBuilder) -> Result<StageVerdict, String> {
        let shadow = match self.bt_shadow_store.as_mut() {
            Some(store) => Some(submit(store, event.clone())?),
            None => None,
        };
        let store = self.bt_store.as_mut().ok_or("no stage-bt policies installed")?;
        let sub = submit(store, event)?;
        let (allowed, ids, errors) = verdict_parts(&sub);
        let shadow_allowed = shadow.as_ref().map(|s| {
            let (allowed, _, errors) = verdict_parts(s);
            allowed && errors.is_empty()
        });
        Ok(StageVerdict {
            allowed,
            determining: self.reasons(ids.into_iter(), "bt"),
            shadow_allowed,
            errors,
            submitted: Some((sub.offset, sub.ts)),
        })
    }

    /// A response / error event (history only).
    pub fn record_response(&mut self, event: dogwood_language::EventBuilder) -> Result<(u64, i64), String> {
        if let Some(store) = self.bt_shadow_store.as_mut() {
            submit(store, event.clone())?;
        }
        let store = self.bt_store.as_mut().ok_or("no stage-bt policies installed")?;
        let sub = submit(store, event)?;
        Ok((sub.offset, sub.ts))
    }
}

/// Bring a durable store to exactly `policies` (each one annotated statement) under
/// `schema` WITHOUT resetting the history of policies that did not change: unchanged
/// `@id` stamps are kept, removed ones deleted, new ones added (born with empty history),
/// and a changed action schema is re-applied in the same atomic batch. A fresh store is
/// installed from scratch (event schema set once).
fn sync_store(store: &mut DurableTemporalEngine, policies: &[String], schema: &str) -> Result<(), String> {
    if !store.has_policy() {
        store
            .install(&policies.join("\n\n"), schema, Some(EVENT_SCHEMA), None)
            .map_err(|e| e.to_string())?;
        return Ok(());
    }
    let wanted: BTreeMap<String, &String> =
        policies.iter().filter_map(|p| stamp_of(p).map(|s| (s, p))).collect();
    let mut verbs = Vec::new();
    if store.action_schema().as_deref() != Some(schema) {
        verbs.push(Verb::SetActionSchema { action_schema: schema.to_string() });
    }
    let mut present = BTreeSet::new();
    for entry in store.list() {
        match stamp_of(&entry.statement) {
            Some(stamp) if wanted.contains_key(&stamp) => {
                present.insert(stamp);
            }
            _ => verbs.push(Verb::Delete { id: entry.token.clone() }),
        }
    }
    for (stamp, text) in &wanted {
        if !present.contains(stamp) {
            verbs.push(Verb::Add { policy: (*text).clone() });
        }
    }
    if verbs.is_empty() {
        return Ok(());
    }
    store.batch(verbs).map(|_| ()).map_err(|e| e.to_string())
}

fn stamp_of(statement: &str) -> Option<String> {
    let start = statement.find("@id(\"")? + 5;
    let end = statement[start..].find('"')? + start;
    Some(statement[start..end].to_string())
}

fn submit(store: &mut DurableTemporalEngine, event: dogwood_language::EventBuilder) -> Result<Submitted, String> {
    store.submit(event).map_err(|e| e.to_string())
}

fn verdict_parts(sub: &Submitted) -> (bool, Vec<String>, Vec<String>) {
    match &sub.outcome {
        Outcome::Decision(d) => (
            d.allowed(),
            d.diagnostics().reason().filter_map(|r| r.annotation_id().map(String::from)).collect(),
            d.diagnostics().errors().map(String::from).collect(),
        ),
        Outcome::Recorded => (false, vec![], vec![]),
    }
}

/// The `<policy_id>:v<n>` stamps behind a stateless response (rule index → installed
/// policy order of that stage's active subset).
fn rule_stamps(r: &dogwood_language::Response, policies: &[StoredPolicy], stage: &str) -> impl Iterator<Item = String> {
    let subset: Vec<&StoredPolicy> = policies
        .iter()
        .filter(|p| p.stage == stage && p.mode == "active" && (stage == "a" || p.kind == "stateless"))
        .collect();
    r.diagnostics()
        .reason()
        .filter_map(|rule| subset.get(rule.rule_index).map(|p| format!("{}:v{}", p.id, p.version)))
        .collect::<Vec<_>>()
        .into_iter()
}
