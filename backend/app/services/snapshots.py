"""Ledger spec snapshots per publish, their field-level diff, and the rollback checks.

A snapshot is written by `deployer.pipeline.create_deployment` — the one place every
create / redeploy / rollback / promotion / architect approval / preset release passes
through — so no publish path can forget it. The two canary paths that change
`Agent.spec` without a deployment (Runtime-canary promote, Harness-canary rollback)
record their own, and `backfill_baselines` gives an agent that predates the table a
`baseline` at startup.

The AWS version is only known once the deploy stage ran, so `_finish` fills it in
through `stamp_deployed`, which also replaces the stored spec with the spec **as
deployed**: a deploy stage may rewrite it mid-job (the architect's harness stage pins
`spec.skills` to immutable copies), and a rollback must re-deploy what actually ran.

Rollback never reverts an AWS version in place: it re-publishes a stored spec through
the ordinary redeploy path, which writes a NEW snapshot.
"""

from __future__ import annotations

import copy
import json
import logging
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, defer

from app.core.errors import NotFoundError, is_not_found
from app.models.ledger import Agent, Deployment, SpecSnapshot
from app.services import creators
from app.services.creators import Creator
from app.services.workspace import WorkspaceContext

logger = logging.getLogger(__name__)

# Top-level spec field -> the group the console labels it with.
_GROUPS = {
    "system_prompt": "prompt",
    "model_id": "model",
    "model_source": "model",
    "max_tokens": "model",
    "reasoning_effort": "model",
    "tools": "tools",
    "toolkits": "tools",
    "native_tools": "tools",
    "allowed_tools": "tools",
    "tool_description_overrides": "tools",
    "skills": "skills",
    "knowledge_bases": "knowledge_bases",
    "memory": "memory",
    "guardrail": "guardrail",
}
GROUP_ORDER = ("prompt", "model", "tools", "skills", "knowledge_bases", "memory", "guardrail")
# Fields compared whole: recursing into a document / code blob would only produce noise.
_ATOMIC = {"code", "code_bundle", "studio_flow", "system_prompt"}
# An atomic field's before/after body larger than this is replaced by `{"length": n}`
# (a `code_bundle` may be 1 MB); the full body is one `GET /snapshots/{seq}` away.
BODY_LIMIT = 2048

BASELINE_NOTE = "baseline"


# ── write ─────────────────────────────────────────────────────────────────────


def _agent_creator(agent: Agent) -> Creator | None:
    if agent.created_by is None and agent.created_by_name is None:
        return None
    return Creator(user_id=agent.created_by, name=agent.created_by_name)


def record_snapshot(
    db: Session,
    agent: Agent,
    *,
    deployment_id: str | None = None,
    actor: Creator | None = None,
    note: str | None = None,
) -> SpecSnapshot:
    """Append the agent's current spec as its next snapshot (`seq = max + 1`).

    `actor` is who published; without one the agent's own creator is recorded.
    Flushes, never commits: the row shares the caller's transaction (including the
    `commit=False` callers of `create_deployment`).

    The caller's pending writes are flushed *before* `max(seq)` is read: the session
    does not autoflush, and on SQLite the first write takes the database write lock, so
    a concurrent publish of the same agent (a canary action thread next to a redeploy)
    reads the other's committed row instead of minting the same `seq` and failing the
    `UNIQUE(agent_id, seq)` index.
    """
    db.flush()
    last = db.scalar(select(func.max(SpecSnapshot.seq)).where(SpecSnapshot.agent_id == agent.id))
    snap = SpecSnapshot(
        workspace_id=agent.workspace_id,
        agent_id=agent.id,
        seq=(last or 0) + 1,
        spec=copy.deepcopy(agent.spec or {}),
        deployment_id=deployment_id,
        note=note,
    )
    creators.stamp(snap, actor or _agent_creator(agent))
    db.add(snap)
    db.flush()
    return snap


def stamp_deployed(db: Session, deployment_id: str, agent: Agent) -> None:
    """On a successful deploy: the AWS version it left live and the spec as deployed.

    A deployment with no snapshot (a job that predates the table) is left alone; the
    next startup's baseline backfill covers its agent. Does not commit.
    """
    snap = db.scalar(select(SpecSnapshot).where(SpecSnapshot.deployment_id == deployment_id))
    if snap is None:
        return
    snap.spec = copy.deepcopy(agent.spec or {})
    if agent.version:
        snap.aws_version = str(agent.version)


def backfill_baselines(db: Session | None = None) -> int:
    """Snapshot #1 (`note="baseline"`) for every live agent that has none. Idempotent.

    Covers active agents with an AWS resource that are not discovered runtimes; each
    gets its current spec (the last one that deployed successfully), stamped with
    `agent.version` and its creator. Ledger-only — no AWS call. Run once per startup,
    so today's configuration is a rollback target right after the first edit. Never
    raises: a ledger failure (two processes racing the unique index) is logged, rolled
    back as a whole and left for the next startup — a missing baseline only means one
    fewer rollback target, never a reason to refuse to boot.
    """
    from app.core.db import SessionLocal
    from app.services.runtime_discovery import DISCOVERED_METHOD

    own = db is None
    session = SessionLocal() if own else db
    try:
        snapshotted = select(SpecSnapshot.agent_id)
        agents = session.scalars(
            select(Agent).where(
                Agent.status == "active",
                Agent.resource_id.is_not(None),
                Agent.method != DISCOVERED_METHOD,
                Agent.id.not_in(snapshotted),
            )
        ).all()
        for agent in agents:
            snap = record_snapshot(session, agent, note=BASELINE_NOTE)
            snap.aws_version = str(agent.version) if agent.version else None
        session.commit()
        return len(agents)
    except SQLAlchemyError as exc:
        session.rollback()
        logger.warning(
            "spec snapshot baseline backfill failed (%s); retried next startup",
            type(exc).__name__,
        )
        return 0
    finally:
        if own:
            session.close()


# ── read ──────────────────────────────────────────────────────────────────────


def _iso(value: datetime | None) -> str | None:
    """SQLite drops the zone; every stored time is UTC, so say so on the wire."""
    if value is None:
        return None
    return value.replace(tzinfo=value.tzinfo or UTC).isoformat()


def _state(snap: SpecSnapshot, deployment_status: str | None) -> str:
    """deployed | deploying | failed | not_deployed — the card's state column."""
    if snap.aws_version:
        return "deployed"
    if deployment_status == "failed":
        return "failed"
    if deployment_status == "running":
        return "deploying"
    return "not_deployed"


def snapshot_out(
    snap: SpecSnapshot, *, deployment_status: str | None = None, with_spec: bool = False
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "seq": snap.seq,
        "agent_id": snap.agent_id,
        "aws_version": snap.aws_version,
        "deployed": snap.aws_version is not None,
        "state": _state(snap, deployment_status),
        "deployment_id": snap.deployment_id,
        "note": snap.note,
        **creators.fields(snap),
        "created_at": _iso(snap.created_at),
    }
    if with_spec:
        out["spec"] = snap.spec or {}
    return out


def deployment_statuses(db: Session, snaps: list[SpecSnapshot]) -> dict[str, str]:
    """`deployment_id → Deployment.status` for the snapshots' deployments (one query)."""
    ids = {s.deployment_id for s in snaps if s.deployment_id}
    if not ids:
        return {}
    rows = db.execute(select(Deployment.id, Deployment.status).where(Deployment.id.in_(ids)))
    return dict(rows.tuples().all())


def list_snapshots(db: Session, agent_id: str) -> list[SpecSnapshot]:
    """Newest first, **without** loading `spec` (a code agent's spec can be ~1 MB)."""
    return list(
        db.scalars(
            select(SpecSnapshot)
            .options(defer(SpecSnapshot.spec))
            .where(SpecSnapshot.agent_id == agent_id)
            .order_by(SpecSnapshot.seq.desc())
        )
    )


def get_snapshot(db: Session, agent_id: str, seq: int) -> SpecSnapshot:
    snap = db.scalar(
        select(SpecSnapshot).where(SpecSnapshot.agent_id == agent_id, SpecSnapshot.seq == seq)
    )
    if snap is None:
        raise NotFoundError("snapshot.not_found", "snapshot not found", {"seq": seq})
    return snap


# ── diff ──────────────────────────────────────────────────────────────────────


def _kind(before: Any, after: Any, missing: object) -> str:
    if before is missing:
        return "added"
    if after is missing:
        return "removed"
    return "changed"


def _size(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    return len(json.dumps(value, default=str, ensure_ascii=False))


def _walk(path: str, before: Any, after: Any, out: list[dict[str, Any]], missing: object) -> None:
    if before == after:
        return
    top = path.split(".", 1)[0]
    if isinstance(before, dict) and isinstance(after, dict) and top not in _ATOMIC:
        for key in sorted(set(before) | set(after)):
            _walk(f"{path}.{key}", before.get(key, missing), after.get(key, missing), out, missing)
        return
    row: dict[str, Any] = {
        "path": path,
        "group": _GROUPS.get(top, "other"),
        "kind": _kind(before, after, missing),
        "before": None if before is missing else before,
        "after": None if after is missing else after,
    }
    if isinstance(before, list) and isinstance(after, list):
        # Order rarely matters for tools / skills / KBs: report membership changes,
        # and keep before/after for the pure re-order case.
        row["added"] = [x for x in after if x not in before]
        row["removed"] = [x for x in before if x not in after]
    if top in _ATOMIC:
        for side in ("before", "after"):
            value = row[side]
            if value is not None and _size(value) > BODY_LIMIT:
                row[side] = {"length": _size(value)}
                row["elided"] = True
    out.append(row)


def diff_specs(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Field-level differences by dotted path (`memory.long_term`), grouped, stable.

    Lists (tools, skills, knowledge bases…) carry `added` / `removed`; `code`,
    `code_bundle`, `studio_flow` and `system_prompt` are compared whole, and a body of
    theirs over `BODY_LIMIT` is replaced by `{"length": n}` (`elided: true`).
    Unchanged fields are omitted.
    """
    missing = object()
    out: list[dict[str, Any]] = []
    for key in sorted(set(before or {}) | set(after or {})):
        _walk(key, (before or {}).get(key, missing), (after or {}).get(key, missing), out, missing)
    out.sort(key=lambda r: group_rank(r["group"]))
    return out


def group_rank(group: str) -> int:
    return GROUP_ORDER.index(group) if group in GROUP_ORDER else len(GROUP_ORDER)


def changed_groups(changes: list[dict[str, Any]]) -> list[str]:
    """The distinct groups of a diff, in display order."""
    return sorted({c["group"] for c in changes}, key=group_rank)


# ── rollback pre-check ────────────────────────────────────────────────────────


def _kb_ids(spec: dict[str, Any] | None) -> set[str]:
    return {
        ref["kb_id"]
        for ref in (spec or {}).get("knowledge_bases") or []
        if isinstance(ref, dict) and ref.get("kb_id")
    }


def stale_kb_ids(
    workspace: WorkspaceContext,
    target_spec: dict[str, Any] | None,
    current_spec: dict[str, Any] | None,
) -> list[str]:
    """The knowledge bases a rollback would re-add that no longer exist in AWS.

    Only ids in the target but not in the current spec are read (usually none, so
    usually no call): a KB force-delete strips the live spec, never old snapshots.
    Any other read failure propagates (the error handler maps it).
    """
    added = sorted(_kb_ids(target_spec) - _kb_ids(current_spec))
    if not added:
        return []
    from app.services import knowledge
    from app.services.agentcore.client import agent_client

    client = agent_client(workspace)
    stale: list[str] = []
    for kb_id in added:
        try:
            knowledge._get_kb(client, kb_id)
        except NotFoundError:
            stale.append(kb_id)
        except ClientError as exc:
            if not is_not_found(exc):
                raise
            stale.append(kb_id)
    return stale
