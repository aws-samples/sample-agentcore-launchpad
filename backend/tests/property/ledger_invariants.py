"""Persistence-invariant scan (round 16, channel A — the fifth layer of the six-layer stack).

Reusable by every r16 branch: run it at the end of a state machine / property test to prove
the ledger (and, for memory, the OpenSearch document set) is still in a state the contracts
allow — no orphan rows, no dangling references-by-convention, no illegal status
combination. Every rule names the contract clause it comes from; nothing here reads a
threshold off the implementation.

    from tests.property.ledger_invariants import assert_ledger_invariants
    assert_ledger_invariants(SessionLocal)              # SQLite ledger (+ control plane, r16-e)
    assert_memory_index_invariants(fake_os.indices[idx]) # memory documents

`scan_ledger` / `scan_memory_index` return the violation list for callers that want to
assert a *specific* violation (a shrunk Hypothesis example is a finding, not a failure).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from typing import Any

from sqlalchemy import select

from app.assistant import purge
from app.evaluation.models import OnlineEvalConfig
from app.models.assistant import (
    AssistantConversation,
    AssistantEvaluationPlan,
    AssistantMessage,
    AssistantProposal,
    AssistantTurnEvent,
)
from app.models.ledger import (
    Agent,
    IdentityProvider,
    Job,
    KnowledgeBase,
    MemoryStore,
    RegistryRecord,
    Workspace,
)
from app.optimization.models import ExperimentAssignment, RuntimeCanary
from app.skill_lab.models import SkillLabJob, SkillLabTaskset

# ── contract constants (cited, not imported from the implementation) ─────────────────

# registry-backend.md "State machine (ruling R4)": the five statuses of the table.
REGISTRY_STATUSES = ("DRAFT", "PENDING_APPROVAL", "APPROVED", "REJECTED", "DEPRECATED")
# registry-backend.md: "`decided_at`/`decided_by` are stamped" on a decision; a submit
# stamps `submitted_at` (same section, `set_record_status` semantics).
REGISTRY_DECIDED = ("APPROVED", "REJECTED", "DEPRECATED")
# knowledge-backend.md "Delete contract (WB-008)": the ledger delete is committed first, in
# the same request — a row still `DELETING` at rest is a half-finished delete.
KB_STATUSES_AT_REST = ("CREATING", "ACTIVE", "FAILED")
# knowledge-backend.md contract notes (R18): a data source's prefix must satisfy
# `prefix.startswith(f"kb/{kb_id}/")`; mode is `upload` | `existing`.
KB_SOURCE_MODES = ("upload", "existing")
# memory-backend.md "Strategy catalog (fixed in v1, ruling R11)".
MEMORY_STRATEGIES = ("semantic", "user_preference", "summarization")
# memory-backend.md "Per-store settings" table: knob → (lower, upper) bound.
MEMORY_MERGE_BOUNDS = {
    "recall_half_life_days": (1, 3650),
    "max_facts_per_actor": (10, 10000),
    "max_preferences_per_actor": (10, 10000),
    "long_term_ttl_days": (7, 3650),
    "actor_summary_interval": (1, 100),
}
# api.md `GET /api/identity/connections`: `kind: oauth2|api_key`.
IDENTITY_KINDS = ("oauth2", "api_key")
# memory-backend.md "Merge strategy (v2)": "nearest active record at cosine ≥ 0.92 →
# duplicate, nothing written, whatever the op".
MEMORY_DEDUPE_COSINE = 0.92
# memory-backend.md "Merge strategy (v2)": extraction with operation intent applies to
# `/facts/{actorId}` and `/preferences/{actorId}` (summaries are one doc per namespace).
MEMORY_DEDUPED_NAMESPACE_PREFIXES = ("/facts/", "/preferences/")


# ── SQLite ledger ───────────────────────────────────────────────────────────────────


def _pins(spec: dict[str, Any] | None, key: str, field: str) -> list[str]:
    out: list[str] = []
    for item in (spec or {}).get(key) or []:
        if isinstance(item, dict) and isinstance(item.get(field), str) and item[field]:
            out.append(item[field])
    return out


def scan_ledger(session_factory: Callable[[], Any]) -> list[str]:
    """Every contract violation in the ledger, as one line each (empty = clean)."""
    db = session_factory()
    try:
        violations: list[str] = []
        workspaces = {w.id: w for w in db.query(Workspace).all()}
        kbs = db.query(KnowledgeBase).all()
        kb_ids = {(k.workspace_id, k.kb_id) for k in kbs}
        records = db.query(RegistryRecord).all()
        record_ids = {(r.workspace_id, r.id) for r in records}
        stores = db.query(MemoryStore).all()
        store_ids = {(s.workspace_id, s.memory_id) for s in stores}

        # agents: references by convention (spec JSON) — users-and-ownership.md §1 keeps
        # the ledger as identifiers only, so a dangling id is an orphan reference.
        for agent in db.query(Agent).all():
            if agent.status == "deleted":
                continue
            where = f"agent {agent.id} ({agent.name!r}, status {agent.status})"
            spec = agent.spec or {}
            # knowledge-backend.md: `delete_kb` 409s while agents mount the KB; the router
            # states "no agent may keep a dangling KB id".
            for kb_id in _pins(spec, "knowledge_bases", "kb_id"):
                if (agent.workspace_id, kb_id) not in kb_ids:
                    violations.append(
                        f"{where} pins knowledge base {kb_id!r} that no longer exists"
                    )
            # memory-backend.md / CLAUDE.md "Per-agent memory scoping": a spec may pin a
            # `memory.memory_id` only if the workspace manages it (default store or a
            # `memory_stores` row); routers/memory_resources 409s `memory.in_use` on delete.
            memory = spec.get("memory") if isinstance(spec.get("memory"), dict) else {}
            memory_id = memory.get("memory_id") if isinstance(memory, dict) else None
            if isinstance(memory_id, str) and memory_id:
                ws = workspaces.get(agent.workspace_id)
                default_id = ((ws.resources or {}) if ws is not None else {}).get("memory_id")
                if memory_id != default_id and (agent.workspace_id, memory_id) not in store_ids:
                    violations.append(
                        f"{where} pins memory store {memory_id!r} that no longer exists"
                    )
            # registry-backend.md "Agent delete removes the A2A record": a live agent's
            # `registry_record_id` must resolve to a record of its workspace.
            if (
                agent.registry_record_id
                and (agent.workspace_id, agent.registry_record_id) not in record_ids
            ):
                violations.append(
                    f"{where} points at registry record {agent.registry_record_id!r} "
                    "that no longer exists"
                )

        # registry records — registry-backend.md state machine + unique index.
        seen_versions: set[tuple[str, str, str]] = set()
        for r in records:
            where = f"registry record {r.id} ({r.name!r})"
            if r.status not in REGISTRY_STATUSES:
                violations.append(f"{where} has status {r.status!r} outside the state machine")
            key = (r.workspace_id, r.name, r.record_version)
            if key in seen_versions:
                violations.append(f"{where} duplicates (workspace, name, record_version) {key}")
            seen_versions.add(key)
            if r.status == "PENDING_APPROVAL" and r.submitted_at is None:
                violations.append(f"{where} is PENDING_APPROVAL without submitted_at")
            if r.status in REGISTRY_DECIDED and r.decided_at is None:
                violations.append(f"{where} is {r.status} without decided_at")
            if r.status == "DRAFT" and (r.decided_at is not None or r.decided_by):
                violations.append(f"{where} is DRAFT but carries a decision stamp")
            if r.created_by and not r.created_by_name:
                violations.append(f"{where} has created_by without created_by_name (§1)")

        # knowledge bases — knowledge-backend.md delete contract + R18 prefix rule.
        seen_kb_names: set[tuple[str, str]] = set()
        for kb in kbs:
            where = f"knowledge base {kb.kb_id} ({kb.name!r})"
            if kb.status not in KB_STATUSES_AT_REST:
                violations.append(f"{where} rests in status {kb.status!r}")
            name_key = (kb.workspace_id, kb.name)
            if name_key in seen_kb_names:
                violations.append(f"{where} duplicates a KB name in its workspace (kb.name_exists)")
            seen_kb_names.add(name_key)
            seen_ds: set[str] = set()
            for ds in kb.data_sources or []:
                ds_id = ds.get("ds_id")
                if not ds_id or ds_id in seen_ds:
                    violations.append(f"{where} has a data source without a unique ds_id: {ds}")
                seen_ds.add(ds_id or "")
                if ds.get("mode") not in KB_SOURCE_MODES:
                    violations.append(f"{where} data source {ds_id} has mode {ds.get('mode')!r}")
                prefix = ds.get("prefix") or ""
                if not prefix.startswith(f"kb/{kb.kb_id}/"):
                    violations.append(
                        f"{where} data source {ds_id} prefix {prefix!r} leaves kb/{kb.kb_id}/ (R18)"
                    )
            if kb.created_by and not kb.created_by_name:
                violations.append(f"{where} has created_by without created_by_name (§1)")

        # memory stores — memory-backend.md strategy catalog + per-store settings bounds.
        seen_store_names: set[tuple[str, str]] = set()
        for s in stores:
            where = f"memory store {s.memory_id} ({s.name!r})"
            name_key = (s.workspace_id, s.name)
            if name_key in seen_store_names:
                violations.append(f"{where} duplicates a store name (memory.name_exists)")
            seen_store_names.add(name_key)
            for strategy in s.strategies or []:
                if strategy not in MEMORY_STRATEGIES:
                    violations.append(f"{where} lists strategy {strategy!r} outside the catalog")
            for knob, value in (s.merge_settings or {}).items():
                bounds = MEMORY_MERGE_BOUNDS.get(knob)
                if bounds is None:
                    violations.append(f"{where} stores unknown merge knob {knob!r}")
                elif value is not None and not (bounds[0] <= int(value) <= bounds[1]):
                    violations.append(f"{where} merge knob {knob}={value!r} outside {bounds}")

        # identity providers — api.md connections shape; one row per (workspace, kind, name).
        seen_idp: set[tuple[str | None, str, str]] = set()
        for p in db.query(IdentityProvider).all():
            where = f"identity provider {p.name!r} ({p.kind})"
            if p.kind not in IDENTITY_KINDS:
                violations.append(f"{where} has kind outside oauth2|api_key")
            key = (p.workspace_id, p.kind, p.name)
            if key in seen_idp:
                violations.append(f"{where} is recorded twice in workspace {p.workspace_id}")
            seen_idp.add(key)
        violations.extend(scan_control_plane(db))
        return violations
    finally:
        db.close()


def assert_ledger_invariants(session_factory: Callable[[], Any]) -> None:
    violations = scan_ledger(session_factory)
    assert not violations, "ledger invariants violated:\n  " + "\n  ".join(violations)


# ── control plane: architect conversations, canaries, Skill Lab, purge jobs (r16-e) ──────
# Contract: assistant-turns.md §10 (the ledger step removes "operations, plans, proposals,
# messages, turn events, conversation"; "one live job per conversation" claimed through
# ``purge_job_id``), evaluation-engine.md §routing (``experiment_assignments.owner_id``, the
# ``can_<id8>_`` owner prefix of a canary's per-arm online configs), Skill Lab
# ``taskset_in_use`` (a job keeps its task set), and the status vocabularies the contracts name.

AGENT_STATUSES = {"active", "deploying", "failed", "deleted", "inactive", "pending", "stopped"}
CANARY_STATUSES = {"running", "completed", "rolled_back", "cleaned", "failed"}
# evaluation-engine.md §canary: the seven actions (`canary_service.ASYNC_ACTIONS`)
CANARY_ACTIONS = {"setup", "traffic", "verdict", "advance", "complete", "rollback", "cleanup"}
PROPOSAL_STATUSES = {"draft", "invalid", "superseded", "rejected", "approved"}
JOB_STATUSES = {"queued", "running", "succeeded", "failed", "cancelled", "interrupted"}
SKILL_LAB_STATUSES = {"queued", "running", "succeeded", "failed", "cancelled", "interrupted"}


def scan_control_plane(db: Any) -> list[str]:
    out: list[str] = []
    conversations = set(db.scalars(select(AssistantConversation.id)).all())
    for model, label in ((AssistantProposal, "proposal"), (AssistantMessage, "message"),
                         (AssistantTurnEvent, "turn event"),
                         (AssistantEvaluationPlan, "evaluation plan")):
        for cid in set(db.scalars(select(model.conversation_id)).all()) - conversations:
            out.append(f"orphan assistant {label} rows of conversation {cid}")
    agents = {a.id: a for a in db.scalars(select(Agent)).all()}
    for agent in agents.values():
        if agent.status not in AGENT_STATUSES:
            out.append(f"agent {agent.id} has unknown status {agent.status!r}")
    for p in db.scalars(select(AssistantProposal)).all():
        if p.status not in PROPOSAL_STATUSES:
            out.append(f"proposal {p.id} has unknown status {p.status!r}")
        if p.agent_id and p.agent_id not in agents:
            out.append(f"proposal {p.id} names missing agent {p.agent_id}")
        if p.status == "approved" and not p.agent_id:
            out.append(f"approved proposal {p.id} names no agent")
    canaries = {c.id: c for c in db.scalars(select(RuntimeCanary)).all()}
    for c in canaries.values():
        if c.status not in CANARY_STATUSES:
            out.append(f"canary {c.id} has unknown status {c.status!r}")
        for aid in (c.champion_agent_id, c.challenger_agent_id):
            if aid and aid not in agents:
                out.append(f"canary {c.id} names missing agent {aid}")
        if c.running_action is not None and c.running_action not in CANARY_ACTIONS:
            out.append(f"canary {c.id} holds unknown running_action {c.running_action!r}")
    for owner_id in set(db.scalars(select(ExperimentAssignment.owner_id)).all()):
        if owner_id not in canaries:
            out.append(f"assignment rows of a missing owner {owner_id}")
    tags = {cid[:8] for cid in canaries}
    for row in db.scalars(select(OnlineEvalConfig)).all():
        name = str(row.name)
        if name.startswith("can_") and name[4:12] not in tags:
            out.append(f"online config {row.config_id} ({name}) names a missing canary")
    tasksets = set(db.scalars(select(SkillLabTaskset.id)).all())
    for job in db.scalars(select(SkillLabJob)).all():
        if job.status not in SKILL_LAB_STATUSES:
            out.append(f"skill lab job {job.id} has unknown status {job.status!r}")
        if job.taskset_id not in tasksets:
            out.append(f"skill lab job {job.id} names missing task set {job.taskset_id}")
    for job in db.scalars(select(Job)).all():
        if job.status not in JOB_STATUSES:
            out.append(f"job {job.id} has unknown status {job.status!r}")
        if job.type == purge.JOB_TYPE and job.status in purge.LIVE_JOB_STATUSES:
            cid = (job.payload or {}).get("conversation_id")
            row = db.get(AssistantConversation, cid) if cid else None
            if row is None:
                out.append(f"live purge job {job.id} names missing conversation {cid}")
            elif row.purge_job_id != job.id:
                out.append(f"live purge job {job.id} is not the claim of conversation {cid}")
    return out




# ── memory documents (OpenSearch index contents) ────────────────────────────────────


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return 0.0 if na == 0.0 or nb == 0.0 else dot / (na * nb)


def record_is_active(doc: dict[str, Any]) -> bool:
    """memory-backend.md "Recall": active = not superseded, deleted or expired; a document
    without the v2 fields is ACTIVE."""
    return not (doc.get("superseded_by") or doc.get("deleted_at") or doc.get("expired_at"))


def scan_memory_index(
    docs: dict[str, dict[str, Any]],
    *,
    dedupe_cosine: float = MEMORY_DEDUPE_COSINE,
) -> list[str]:
    """Violations of the merge-strategy (v2) contract over one memory index's documents."""
    violations: list[str] = []
    # superseded chains: `superseded_by` must resolve, be acyclic and end at a record that
    # is itself not superseded (memory-backend.md: "target gets superseded_by"; the record
    # that replaced it is the live one — or was in turn replaced further down the chain).
    for rid, doc in docs.items():
        if not doc.get("superseded_by"):
            continue
        seen = [rid]
        cur = doc
        while cur.get("superseded_by"):
            nxt = cur["superseded_by"]
            if nxt in seen:
                violations.append(f"superseded chain cycles: {' → '.join([*seen, nxt])}")
                break
            if nxt not in docs:
                violations.append(f"record {seen[-1]} superseded_by {nxt!r} which does not exist")
                break
            seen.append(nxt)
            cur = docs[nxt]
    # `supersedes` consistency: a record never supersedes itself, and every member it names
    # exists and carries `superseded_by` (it is no longer active).
    for rid, doc in docs.items():
        for member in doc.get("supersedes") or []:
            if member == rid:
                violations.append(f"record {rid} supersedes itself")
                continue
            target = docs.get(member)
            if target is None:
                violations.append(f"record {rid} supersedes {member!r} which does not exist")
            elif not target.get("superseded_by"):
                violations.append(
                    f"record {rid} supersedes {member} but {member} carries no superseded_by "
                    f"(it is {'active' if record_is_active(target) else 'inactive'})"
                )
    # confidence domain (memory-backend.md: `confidence` float; recall uses max(conf, 0.5)).
    for rid, doc in docs.items():
        value = doc.get("confidence")
        if value is not None and not (0.0 <= float(value) <= 1.0):
            violations.append(f"record {rid} confidence {value!r} outside [0, 1]")
    # no two ACTIVE records of one (store, namespace) at cosine ≥ the dedupe threshold.
    by_ns: dict[tuple[str, str], list[tuple[str, list[float]]]] = {}
    for rid, doc in docs.items():
        ns = str(doc.get("namespace") or "")
        if not ns.startswith(MEMORY_DEDUPED_NAMESPACE_PREFIXES) or not record_is_active(doc):
            continue
        if doc.get("embedding"):
            by_ns.setdefault((str(doc.get("store_id")), ns), []).append((rid, doc["embedding"]))
    for (_store, ns), items in by_ns.items():
        for i, (a_id, a_vec) in enumerate(items):
            for b_id, b_vec in items[i + 1 :]:
                sim = _cosine(a_vec, b_vec)
                if sim >= dedupe_cosine:
                    violations.append(
                        f"two ACTIVE records in {ns} at cosine {sim:.3f} ≥ {dedupe_cosine}: "
                        f"{a_id} / {b_id}"
                    )
    return violations


def assert_memory_index_invariants(docs: dict[str, dict[str, Any]], **kwargs: Any) -> None:
    violations = scan_memory_index(docs, **kwargs)
    assert not violations, "memory index invariants violated:\n  " + "\n  ".join(violations)


def active_ids(docs: dict[str, dict[str, Any]]) -> set[str]:
    return {rid for rid, doc in docs.items() if record_is_active(doc)}


def first_violation(violations: Iterable[str], needle: str) -> str | None:
    for line in violations:
        if needle in line:
            return line
    return None
