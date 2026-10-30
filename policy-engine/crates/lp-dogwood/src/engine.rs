//! One shard of the engine service: every workspace's installed set, the store epoch, and
//! the I4 / I5 operations (contract docs/cn/interfaces/policy-principal-contracts.md).

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use serde_json::{Value as Json, json};

use crate::clock::SharedClock;
use crate::events::{action_id, request_event, response_event};
use crate::wire::{
    DecideRequest, DecideResponse, Determining, EngineError, Install, RespondRequest,
    RespondResponse,
};
use crate::workspace::{InstalledMeta, StoredPolicy, Workspace};

#[derive(Debug, Clone)]
pub struct ShardConfig {
    pub state_dir: PathBuf,
    pub shard: u32,
    pub shards: u32,
    pub utc_offset_s: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Epoch {
    id: String,
    started_at_s: i64,
}

/// A workspace whose store could not be recovered (lead review defect 3): moved to
/// `shard-<n>/quarantine/`, outside the scan root, and denied (fail closed) until the hub
/// installs it again. The shard epoch is NOT bumped — other workspaces keep their history.
#[derive(Debug, Clone, Serialize, Deserialize)]
struct Tombstone {
    ws: String,
    quarantined_at_s: i64,
    reason: String,
}

pub struct Shard {
    cfg: ShardConfig,
    clock: SharedClock,
    epoch: Epoch,
    workspaces: BTreeMap<String, Workspace>,
    quarantined: BTreeMap<String, Tombstone>,
    fault: Option<InstallFault>,
}

/// Test hook: stop an install right after a step, as a crash there would (defect 4).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InstallFault {
    /// crash right after the pending write
    AfterPending,
    /// crash right after the store sync
    AfterSync,
    /// a step after the store sync fails (the error path, not a crash)
    ErrorAfterSync,
    /// the commit rename fails
    RenameFails,
    /// the commit rename fails and so does removing the pending file
    RenameAndRemoveFail,
    /// the commit rename succeeds, the directory fsync after it fails
    DirFsyncFails,
}

/// `errors[0]` of a decision denied because a policy failed to evaluate (review nit a); the
/// interceptor reports it as the deny reason.
pub const POLICY_EVALUATION_ERROR: &str = "policy_evaluation_error";

pub type Reply<T> = Result<T, (u16, EngineError)>;

const INSTALLED: &str = "installed.json";
const PENDING: &str = "installed.pending.json";
const QUARANTINE: &str = "quarantine";

fn err(status: u16, code: &'static str, message: impl Into<String>) -> (u16, EngineError) {
    (status, EngineError::new(code, message))
}

/// `w-<hex of the workspace id>` — always hex, so two ids can never share a directory
/// (review nit g) and nothing in an id reaches the filesystem unescaped.
fn ws_dir_name(ws: &str) -> String {
    format!("w-{}", ws.bytes().map(|b| format!("{b:02x}")).collect::<String>())
}

/// Crash-safe replace: temp file in the same directory, fsync, rename, fsync the directory
/// (review defect 4). A torn write leaves only the `.tmp`, which is never read.
pub fn write_atomic(path: &Path, bytes: &[u8]) -> Result<(), String> {
    use std::io::Write;
    let dir = path.parent().ok_or("no parent directory")?;
    let tmp = path.with_extension("tmp");
    {
        let mut f = std::fs::File::create(&tmp).map_err(|e| e.to_string())?;
        f.write_all(bytes).map_err(|e| e.to_string())?;
        f.sync_all().map_err(|e| e.to_string())?;
    }
    std::fs::rename(&tmp, path).map_err(|e| e.to_string())?;
    std::fs::File::open(dir).and_then(|d| d.sync_all()).map_err(|e| e.to_string())
}

fn read_json<T: serde::de::DeserializeOwned>(path: &Path) -> Option<Result<T, String>> {
    let bytes = std::fs::read(path).ok()?;
    Some(serde_json::from_slice(&bytes).map_err(|e| e.to_string()))
}

impl Shard {
    /// Open the shard: load (or create) its epoch, load the quarantine tombstones, and
    /// rebuild every installed workspace — the durable stores recover their own history. An
    /// interrupted install (a pending file) is rolled FORWARD to the set it was installing.
    /// A workspace that cannot be rebuilt is quarantined; the rest of the shard is untouched.
    pub fn open(cfg: ShardConfig, clock: SharedClock) -> Result<Self, String> {
        let root = cfg.state_dir.join(format!("shard-{}", cfg.shard));
        let quarantine = root.join(QUARANTINE);
        std::fs::create_dir_all(&quarantine).map_err(|e| e.to_string())?;
        let epoch_path = root.join("epoch.json");
        let epoch = match read_json::<Epoch>(&epoch_path) {
            Some(Ok(e)) => e,
            _ => {
                let e = new_epoch(&clock);
                write_atomic(&epoch_path, &serde_json::to_vec(&e).unwrap())?;
                e
            }
        };
        let mut quarantined = BTreeMap::new();
        for entry in std::fs::read_dir(&quarantine).map_err(|e| e.to_string())?.flatten() {
            if entry.path().extension().is_some_and(|x| x == "lost") {
                if let Some(Ok(t)) = read_json::<Tombstone>(&entry.path()) {
                    quarantined.insert(t.ws.clone(), t);
                }
            }
        }
        let mut shard = Shard { cfg, clock, epoch, workspaces: BTreeMap::new(), quarantined, fault: None };
        for entry in std::fs::read_dir(&root).map_err(|e| e.to_string())?.flatten() {
            let dir = entry.path();
            if !dir.is_dir() || entry.file_name() == QUARANTINE {
                continue;
            }
            // a pending install is the newest intent: roll it forward
            let (meta, pending) = match read_json::<InstalledMeta>(&dir.join(PENDING)) {
                Some(Ok(m)) => (Some(Ok(m)), true),
                _ => (read_json::<InstalledMeta>(&dir.join(INSTALLED)), false),
            };
            let Some(meta) = meta else { continue };
            let meta = match meta {
                Ok(m) => m,
                Err(e) => {
                    shard.quarantine(&dir, None, &format!("unreadable metadata: {e}"));
                    continue;
                }
            };
            // a directory must be the one its workspace maps to, so no workspace is ever
            // loaded twice (re-review item 5). A stray beside a correctly named twin is set
            // aside; a stray that is the ONLY copy of its workspace's state is quarantined
            // with a tombstone, so the re-install marks older sessions history_lost instead
            // of silently starting them from an empty store (follow-up item 1).
            if entry.file_name().to_string_lossy() != ws_dir_name(&meta.ws) {
                let twin = root.join(ws_dir_name(&meta.ws));
                let reason = format!("directory does not belong to workspace {}", meta.ws);
                if twin.join(INSTALLED).is_file() || twin.join(PENDING).is_file() {
                    shard.set_aside(&dir, &reason);
                } else {
                    shard.quarantine(&dir, Some(&meta.ws), &reason);
                }
                continue;
            }
            match Workspace::build(meta.clone(), &dir, &shard.clock) {
                Ok(ws) => {
                    if pending {
                        let _ = std::fs::rename(dir.join(PENDING), dir.join(INSTALLED));
                        let _ = std::fs::File::open(&dir).and_then(|d| d.sync_all());
                    }
                    shard.workspaces.insert(meta.ws.clone(), ws);
                }
                Err(errors) => shard.quarantine(&dir, Some(&meta.ws), &errors.join("; ")),
            }
        }
        Ok(shard)
    }

    /// Move a workspace directory into `quarantine/` and leave a tombstone, so it is never
    /// rescanned (defect 3) and its calls fail closed until it is installed again.
    fn quarantine(&mut self, dir: &Path, ws: Option<&str>, reason: &str) {
        let root = self.cfg.state_dir.join(format!("shard-{}", self.cfg.shard));
        let now = self.clock.now_s();
        let name = dir.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
        let ws = ws.map(String::from).or_else(|| decode_ws_dir(&name)).unwrap_or(name.clone());
        let _ = std::fs::rename(dir, root.join(QUARANTINE).join(format!("{name}-{now}")));
        let tomb = Tombstone { ws: ws.clone(), quarantined_at_s: now, reason: reason.chars().take(500).collect() };
        let _ = write_atomic(
            &root.join(QUARANTINE).join(format!("{}.lost", ws_dir_name(&ws))),
            &serde_json::to_vec(&tomb).unwrap(),
        );
        eprintln!("launchpad-policy-engine: workspace {ws} quarantined: {}", tomb.reason);
        self.quarantined.insert(ws, tomb);
    }

    /// Move a stray directory into `quarantine/` without a tombstone: the workspace it claims
    /// keeps its own (correctly named) directory.
    fn set_aside(&self, dir: &Path, reason: &str) {
        let root = self.cfg.state_dir.join(format!("shard-{}", self.cfg.shard));
        let name = dir.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
        let _ = std::fs::rename(dir, root.join(QUARANTINE).join(format!("{name}-stray-{}", self.clock.now_s())));
        eprintln!("launchpad-policy-engine: {} set aside: {reason}", dir.display());
    }

    fn ws_dir(&self, ws: &str) -> PathBuf {
        self.cfg.state_dir.join(format!("shard-{}", self.cfg.shard)).join(ws_dir_name(ws))
    }

    pub fn inject_install_fault(&mut self, fault: Option<InstallFault>) {
        self.fault = fault;
    }

    pub fn store_epoch(&self) -> &str {
        &self.epoch.id
    }

    pub fn sets(&self) -> BTreeMap<String, String> {
        self.workspaces.iter().map(|(k, w)| (k.clone(), w.meta.policy_set_version.clone())).collect()
    }

    pub fn is_quarantined(&self, ws: &str) -> bool {
        self.quarantined.contains_key(ws)
    }

    /// The actions the engine records for `ws` (what `must_record` answers) — for the
    /// compiler cross-check and diagnostics.
    pub fn temporal_actions(&self, ws: &str) -> Option<Vec<String>> {
        self.workspaces.get(ws).map(|w| w.temporal_actions.iter().cloned().collect())
    }

    pub fn healthz(&self) -> Json {
        json!({"ready": true, "shard": self.cfg.shard, "store_epoch": self.epoch.id, "sets": self.sets()})
    }

    // ── I5 install ─────────────────────────────────────────────────────

    /// Replace a workspace's set. Validation happens before anything is written; then the
    /// commit protocol is `installed.pending.json` (atomic) → sync the durable stores →
    /// rename pending → `installed.json` (the commit point). A crash anywhere is rolled
    /// forward on open; a set that does not compile changes nothing.
    pub fn install(&mut self, msg: Install) -> Result<(), Vec<String>> {
        let mut meta = InstalledMeta {
            ws: msg.ws.clone(),
            policy_set_version: msg.policy_set_version,
            temporal_set_version: msg.temporal_set_version,
            temporal_count: msg.policies.iter().filter(|p| p.kind == "temporal").count(),
            schema: msg.schema,
            policies: msg.policies.iter().map(StoredPolicy::from).collect(),
            history_lost_at_s: self.workspaces.get(&msg.ws).and_then(|w| w.meta.history_lost_at_s),
        };
        Workspace::validate(&meta)?;
        // N17-obs492 (re-review F3): version CAS — a set that is not newer than the installed
        // one is refused unless the hub says `force` (its rollback of a partial install, or a
        // re-sync of shards that disagree). Without it a publish that one shard refused left
        // the other on the new version with nothing able to move it.
        if !msg.force {
            if let Some(current) = self.workspaces.get(&msg.ws) {
                if !version_is_newer(&current.meta.policy_set_version, &meta.policy_set_version) {
                    return Err(vec![format!(
                        "policy_set_version {} is not newer than the installed {} (force: true to re-install or roll back)",
                        meta.policy_set_version, current.meta.policy_set_version
                    )]);
                }
            }
        }
        // a re-install after quarantine starts from an empty store: older sessions lost history
        if let Some(tomb) = self.quarantined.get(&msg.ws) {
            meta.history_lost_at_s = Some(tomb.quarantined_at_s);
        }
        let dir = self.ws_dir(&msg.ws);
        std::fs::create_dir_all(&dir).map_err(|e| vec![e.to_string()])?;
        write_atomic(&dir.join(PENDING), &serde_json::to_vec(&meta).unwrap()).map_err(|e| vec![e])?;
        if self.fault == Some(InstallFault::AfterPending) {
            self.workspaces.remove(&msg.ws);
            return Err(vec!["injected crash after pending".into()]);
        }
        // drop the old handles first: the durable stores hold a file lock
        let previous = self.workspaces.remove(&msg.ws);
        let previous_meta = previous.as_ref().map(|w| w.meta.clone());
        drop(previous);
        let built = Workspace::build(meta.clone(), &dir, &self.clock);
        if self.fault == Some(InstallFault::AfterSync) {
            return Err(vec!["injected crash after sync".into()]);
        }
        let committed = built.and_then(|ws| {
            if self.fault == Some(InstallFault::ErrorAfterSync) {
                return Err(vec!["injected error after sync".into()]);
            }
            let renamed = if matches!(self.fault, Some(InstallFault::RenameFails | InstallFault::RenameAndRemoveFail)) {
                Err(std::io::Error::other("injected rename failure"))
            } else {
                std::fs::rename(dir.join(PENDING), dir.join(INSTALLED))
            };
            // the rename IS the commit (follow-up item 4): once it happened, a failed
            // directory fsync must not report a failure — a crash now either keeps the
            // rename (committed) or loses it, and then the pending file rolls forward to
            // the same set on open. Either way the new set is the one in force.
            if renamed.is_ok() {
                let synced = if self.fault == Some(InstallFault::DirFsyncFails) {
                    Err(std::io::Error::other("injected directory fsync failure"))
                } else {
                    std::fs::File::open(&dir).and_then(|d| d.sync_all())
                };
                if let Err(e) = synced {
                    eprintln!("launchpad-policy-engine: {} committed, directory fsync failed: {e}", msg.ws);
                }
            }
            renamed.map(|_| ws).map_err(|e| vec![format!("commit {INSTALLED}: {e}")])
        });
        match committed {
            Ok(ws) => {
                self.workspaces.insert(msg.ws.clone(), ws);
                if self.quarantined.remove(&msg.ws).is_some() {
                    let root = self.cfg.state_dir.join(format!("shard-{}", self.cfg.shard));
                    let _ = std::fs::remove_file(root.join(QUARANTINE).join(format!("{}.lost", ws_dir_name(&msg.ws))));
                }
                Ok(())
            }
            Err(mut errors) => {
                // re-review item 6: never leave a half-applied install. Drop the intent, then
                // re-sync the store to the committed set (by diff, history kept). If either
                // step fails the workspace is quarantined: it fails closed now, and its state
                // is rebuilt from a fresh install instead of a guess at what was synced.
                let removed = if self.fault == Some(InstallFault::RenameAndRemoveFail) {
                    Err(std::io::Error::other("injected remove failure"))
                } else {
                    match std::fs::remove_file(dir.join(PENDING)) {
                        Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(e),
                        _ => std::fs::File::open(&dir).and_then(|d| d.sync_all()),
                    }
                };
                let restored = match (&removed, previous_meta) {
                    (Err(_), _) => None,
                    (Ok(_), None) => Some(Ok(None)),
                    (Ok(_), Some(old)) => Some(Workspace::build(old, &dir, &self.clock).map(Some)),
                };
                match restored {
                    Some(Ok(Some(ws))) => {
                        self.workspaces.insert(msg.ws.clone(), ws);
                    }
                    Some(Ok(None)) => {} // a first install: nothing was committed before
                    _ => {
                        if let Err(e) = removed {
                            errors.push(format!("remove {PENDING}: {e}"));
                        }
                        errors.push(format!("workspace {} quarantined: the previous set could not be restored", msg.ws));
                        self.quarantine(&dir, Some(&msg.ws), &errors.join("; "));
                    }
                }
                Err(errors)
            }
        }
    }

    // ── I4 decide ──────────────────────────────────────────────────────

    pub fn decide(&mut self, req: &DecideRequest) -> Reply<DecideResponse> {
        let action = action_id(&req.action)
            .ok_or_else(|| err(400, "bad_request", "action must be AgentCore::Action::\"<tool>\""))?
            .to_string();
        let now_s = self.clock.now_s();
        let local = self.clock.local(self.cfg.utc_offset_s);
        let owner = crate::shard_for(&req.session, self.cfg.shards);
        let epoch_id = self.epoch.id.clone();
        if self.quarantined.contains_key(&req.ws) && !self.workspaces.contains_key(&req.ws) {
            return Err(err(503, "unavailable", format!("workspace {} store quarantined; republish", req.ws)));
        }
        let ws = self
            .workspaces
            .get_mut(&req.ws)
            .ok_or_else(|| err(409, "set_mismatch", format!("workspace {} not installed", req.ws)))?;
        if let Some(expect) = &req.context.expect {
            if expect.policy_set_version != ws.meta.policy_set_version
                || expect.temporal_set_version != ws.meta.temporal_set_version
            {
                let mut e = EngineError::new("set_mismatch", format!(
                    "caller expects {} / v{}", expect.policy_set_version, expect.temporal_set_version
                ));
                e.have = Some(ws.meta.policy_set_version.clone());
                return Err((409, e));
            }
        }
        if ws.meta.temporal_count > 0 {
            if let Some(tsv) = req.tsv {
                if tsv < ws.meta.temporal_set_version {
                    return Err(err(409, "session_invalidated", format!(
                        "temporal set v{tsv} superseded by v{}", ws.meta.temporal_set_version
                    )));
                }
            }
        }
        let record = ws.must_record(&action);
        if record && !req.record {
            return Err(err(400, "bad_request", format!("{action} is temporal-governed: record must be true")));
        }
        if req.record && owner != self.cfg.shard {
            return Err(err(400, "bad_request", format!("session belongs to shard {owner}")));
        }
        if req.record {
            if let (Some(started), Some(lost_at)) = (req.session_started_at, ws.meta.history_lost_at_s) {
                if started < lost_at {
                    return Err(err(409, "history_lost", format!("store epoch {epoch_id} began after the session")));
                }
            }
        }
        let types = ws.types.clone();
        let a_event = request_event(req, &action, &types, true, now_s, local)
            .timestamp(now_s * crate::clock::NANOS)
            .build();
        let a = ws.stage_a(&a_event);
        let bt = if req.record {
            ws.stage_bt_record(request_event(req, &action, &types, false, now_s, local))
                .map_err(|e| err(503, "unavailable", e))?
        } else {
            let event = request_event(req, &action, &types, false, now_s, local).timestamp(now_s * crate::clock::NANOS).build();
            ws.stage_bt_stateless(&event)
        };
        // review nit a: Cedar skips a policy whose evaluation errors, so an erroring forbid
        // could let a call through — any evaluation error is a deny (Global fails closed)
        let evaluation_error = !a.errors.is_empty() || !bt.errors.is_empty();
        // a stage whose evaluation errored is a deny of that stage, so `stage_a` alone is a
        // safe input for list-filter (re-review item 3)
        let a_allowed = a.allowed && a.errors.is_empty();
        let bt_allowed = bt.allowed && bt.errors.is_empty();
        let decision = a_allowed && bt_allowed;
        // the shadow never reports allow for an evaluation that errored (re-review item 4)
        let shadow = |s: &crate::workspace::StageVerdict| s.errors.is_empty() && s.shadow_allowed.unwrap_or(s.allowed);
        let would = shadow(&a) && shadow(&bt);
        let mut determining: Vec<Determining> = Vec::new();
        // a denial names what denied it; an allow names both stages' permits
        if decision {
            determining.extend(a.determining.clone());
            determining.extend(bt.determining.clone());
        } else {
            if !a_allowed {
                determining.extend(a.determining.clone());
            }
            if !bt_allowed {
                determining.extend(bt.determining.clone());
            }
        }
        // Cedar reasons are a set: report them in a stable order (stage, id, version)
        determining.sort_by(|x, y| (&x.stage, &x.policy_id, x.version).cmp(&(&y.stage, &y.policy_id, y.version)));
        determining.dedup();
        let mut errors = Vec::new();
        if evaluation_error {
            errors.push(POLICY_EVALUATION_ERROR.to_string());
        }
        errors.extend(a.errors.clone());
        errors.extend(bt.errors.clone());
        let (event_seq, ts_ns) = bt.submitted.map(|(o, t)| (Some(o), Some(t))).unwrap_or((None, None));
        Ok(DecideResponse {
            decision: if decision { "allow" } else { "deny" },
            stage_a: if a_allowed { "allow" } else { "deny" },
            stage_bt: if bt_allowed { "allow" } else { "deny" },
            determining,
            log_only_would: (would != decision).then_some(if would { "allow" } else { "deny" }),
            event_seq,
            ts_ns,
            store_epoch: epoch_id,
            policy_set_version: ws.meta.policy_set_version.clone(),
            errors,
        })
    }

    /// I5 `authorize_stateless`: a decision that never records (simulator, narrowing). A temporal-governed action answers from the stateless policies only and
    /// says so in `errors`.
    pub fn authorize_stateless(&mut self, req: &DecideRequest) -> Reply<DecideResponse> {
        let mut req = req.clone();
        req.record = false;
        let action = action_id(&req.action).unwrap_or_default().to_string();
        let governed = self.workspaces.get(&req.ws).is_some_and(|w| w.must_record(&action));
        if governed {
            // evaluate the stateless part as if ungoverned (no history is touched)
            let ws = self.workspaces.get_mut(&req.ws).unwrap();
            let saved = std::mem::take(&mut ws.temporal_actions);
            let out = self.decide(&req);
            let ws = self.workspaces.get_mut(&req.ws).unwrap();
            ws.temporal_actions = saved;
            return out.map(|mut r| {
                r.errors.push("temporal policies not evaluated (stateless authorize)".into());
                r
            });
        }
        self.decide(&req)
    }

    // ── I4 respond ─────────────────────────────────────────────────────

    pub fn respond(&mut self, req: &RespondRequest) -> Reply<RespondResponse> {
        if !matches!(req.kind.as_str(), "response" | "error") {
            return Err(err(400, "bad_request", "kind must be response or error"));
        }
        let action = action_id(&req.action)
            .ok_or_else(|| err(400, "bad_request", "action must be AgentCore::Action::\"<tool>\""))?
            .to_string();
        // the response event belongs to the session's owner shard, like its request (defect 5)
        let owner = crate::shard_for(&req.session, self.cfg.shards);
        if owner != self.cfg.shard {
            return Err(err(400, "bad_request", format!("session belongs to shard {owner}")));
        }
        if self.quarantined.contains_key(&req.ws) && !self.workspaces.contains_key(&req.ws) {
            return Err(err(503, "unavailable", format!("workspace {} store quarantined; republish", req.ws)));
        }
        let now_s = self.clock.now_s();
        let local = self.clock.local(self.cfg.utc_offset_s);
        let ws = self
            .workspaces
            .get_mut(&req.ws)
            .ok_or_else(|| err(409, "set_mismatch", format!("workspace {} not installed", req.ws)))?;
        if !ws.must_record(&action) {
            // nothing can read this event: acknowledge without writing (unobservable)
            return Ok(RespondResponse { event_seq: 0, ts_ns: now_s * crate::clock::NANOS });
        }
        let types = ws.types.clone();
        let (seq, ts) = ws
            .record_response(response_event(req, &action, &types, now_s, local))
            .map_err(|e| err(503, "unavailable", e))?;
        Ok(RespondResponse { event_seq: seq, ts_ns: ts })
    }

    pub fn state_dir(&self) -> &Path {
        &self.cfg.state_dir
    }
}

fn decode_ws_dir(name: &str) -> Option<String> {
    let hex = name.strip_prefix("w-")?;
    let bytes: Option<Vec<u8>> = (0..hex.len())
        .step_by(2)
        .map(|i| hex.get(i..i + 2).and_then(|h| u8::from_str_radix(h, 16).ok()))
        .collect();
    String::from_utf8(bytes?).ok()
}

fn new_epoch(clock: &SharedClock) -> Epoch {
    let mut bytes = [0u8; 6];
    let seed = clock.now_nanos_u128();
    for (i, b) in bytes.iter_mut().enumerate() {
        *b = (seed >> (i * 8)) as u8 ^ (std::process::id() as u8).wrapping_mul(i as u8 + 1);
    }
    Epoch {
        id: format!("e{}", bytes.iter().map(|b| format!("{b:02x}")).collect::<String>()),
        started_at_s: clock.now_s(),
    }
}

/// `<prefix>-<n>` ordering of set versions (`ps-7` < `ps-8`): newer when the prefixes match
/// and `n` grows; a different prefix (a hand-installed `ps-m1-1` replaced by the hub's `ps-1`)
/// is always newer; two versions that do not both parse are newer only when they differ.
pub fn version_is_newer(installed: &str, candidate: &str) -> bool {
    fn split(v: &str) -> Option<(&str, u64)> {
        let (prefix, n) = v.rsplit_once('-')?;
        Some((prefix, n.parse().ok()?))
    }
    match (split(installed), split(candidate)) {
        (Some((p1, n1)), Some((p2, n2))) if p1 == p2 => n2 > n1,
        _ => installed != candidate,
    }
}
