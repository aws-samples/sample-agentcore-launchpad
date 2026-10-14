"""Environment comparison and drift detection, both answered from evidence.

* **compare** — for one agent *name*, what does each workspace the caller can see run?
  Ledger only; no AWS call. The baseline is the caller's current workspace; every
  other row is `same`, `differs` (naming the spec groups that differ) or `absent`.
  Agents are matched by name, so two unrelated agents that share a name in two
  workspaces compare as one. The fingerprint is a canonical-JSON sha256 of
  `{method, spec}` without deploy-time stamps; the image digest (方式A only) is
  reported beside it, not folded in.
* **drift** — for the current workspace, does AWS still run what the ledger believes?
  One `GetAgentRuntime` / `GetHarness` per visible live agent (≤ 100, 8 at a time),
  on request only. It compares versions and status, **not** spec contents. The rule
  that matters is *fail soft, never fail green*: an answer that could not be read, or
  cannot be judged (a resource mid-update, a canary that legitimately moved the
  version), is `unknown`, never `in_sync`.

Both apply workspace visibility: a member never sees an agent hidden from them under
`isolated`, in any workspace, and drift reads AWS only for the agents they can see.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ledger import Agent, Deployment, Workspace
from app.services import agent_access, snapshots
from app.services.agent_versions import resource_kind
from app.services.agentcore import harness as harness_api
from app.services.agentcore import runtime as runtime_api
from app.services.visibility import Viewer

logger = logging.getLogger(__name__)

# compare states
BASELINE = "baseline"
SAME = "same"
DIFFERS = "differs"
ABSENT = "absent"
NO_BASELINE = "no_baseline"  # present here, but the current workspace has no such agent

# drift states
IN_SYNC = "in_sync"
DRIFT = "drift"
UNKNOWN = "unknown"

DRIFT_AGENT_LIMIT = 100
_READ_WORKERS = 8
_HEALTHY = {"READY"}
_TRANSIENT = {"CREATING", "UPDATING"}

# Spec paths the deploy pipeline stamps (server-owned, environment specific): left out
# of the fingerprint and of the compared groups so they never make two specs differ.
_DEPLOY_STAMPS: tuple[tuple[str, ...], ...] = (("byoc", "provenance"),)


# ── compare ─────────────────────────────────────────────────────────────────────


def comparable_spec(spec: dict[str, Any] | None) -> dict[str, Any]:
    """The spec without deploy-time stamps (`byoc.provenance`)."""
    out = copy.deepcopy(spec or {})
    for path in _DEPLOY_STAMPS:
        parent: Any = out
        for key in path[:-1]:
            parent = parent.get(key) if isinstance(parent, dict) else None
        if isinstance(parent, dict):
            parent.pop(path[-1], None)
    return out


def spec_digest(method: str, spec: dict[str, Any] | None) -> str:
    """sha256 of canonical JSON of `{method, spec}` (deploy-time stamps stripped)."""
    canonical = json.dumps(
        {"method": method, "spec": comparable_spec(spec)},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _iso(value: datetime | None) -> str | None:
    """SQLite drops the zone; every stored time is UTC, so say so on the wire."""
    if value is None:
        return None
    return value.replace(tzinfo=value.tzinfo or UTC).isoformat()


def _last_deploy(db: Session, agent: Agent) -> dict[str, Any] | None:
    deployment = db.scalars(
        select(Deployment)
        .where(Deployment.agent_id == agent.id)
        .order_by(Deployment.started_at.desc())
    ).first()
    if deployment is None:
        return None
    return {
        "status": deployment.status,
        "started_at": _iso(deployment.started_at),
        "ended_at": _iso(deployment.ended_at),
        "image_digest": deployment.image_digest,
    }


def _workspace_out(row: Workspace) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "account_id": row.account_id,
        "region": row.region,
        "status": row.bootstrap_status,
    }


def _visible_agent(db: Session, workspace_id: str, name: str, viewer: Viewer) -> Agent | None:
    """The newest non-deleted agent of that name the viewer may see in the workspace.

    Live names are unique per workspace, so this is the one agent holding the name; a
    hidden one reads as absent."""
    return db.scalars(
        select(Agent)
        .where(
            Agent.workspace_id == workspace_id,
            Agent.name == name,
            Agent.status != "deleted",
            agent_access.agent_clause(viewer),
        )
        .order_by(Agent.created_at.desc())
    ).first()


def compare_agent(
    db: Session,
    *,
    name: str,
    workspaces: list[Workspace],
    baseline_id: str,
    viewer_for: Callable[[Workspace], Viewer],
) -> dict[str, Any]:
    """One row per workspace for agent `name`, judged against the baseline workspace.

    `viewer_for` gives the caller's viewer in each workspace (each has its own
    visibility mode)."""
    ordered = sorted(
        workspaces, key=lambda w: (w.id != baseline_id, (w.name or "").lower(), w.id)
    )
    rows: list[dict[str, Any]] = []
    specs: dict[str, dict[str, Any]] = {}
    for workspace in ordered:
        agent = _visible_agent(db, workspace.id, name, viewer_for(workspace))
        row: dict[str, Any] = {
            "workspace": _workspace_out(workspace),
            "baseline": workspace.id == baseline_id,
            "present": agent is not None,
            "state": ABSENT,
            "changed_groups": [],
            "agent": None,
        }
        if agent is not None:
            last = _last_deploy(db, agent)
            row["agent"] = {
                "id": agent.id,
                "name": agent.name,
                "status": agent.status,
                "method": agent.method,
                "version": agent.version,
                "spec_digest": spec_digest(agent.method, agent.spec),
                "image_digest": (last or {}).get("image_digest"),
                "last_deploy": last,
                "updated_at": _iso(agent.updated_at),
            }
            specs[workspace.id] = agent.spec or {}
        rows.append(row)

    reference = next((r for r in rows if r["baseline"] and r["present"]), None)
    for row in rows:
        if not row["present"]:
            continue
        if row["baseline"]:
            row["state"] = BASELINE
            continue
        if reference is None:
            row["state"] = NO_BASELINE
            continue
        ours, theirs = row["agent"], reference["agent"]
        groups: list[str] = []
        if ours["spec_digest"] != theirs["spec_digest"]:
            changes = snapshots.diff_specs(
                comparable_spec(specs[reference["workspace"]["id"]]),
                comparable_spec(specs[row["workspace"]["id"]]),
            )
            groups = snapshots.changed_groups(changes) or ["other"]
            if ours["method"] != theirs["method"] and "other" not in groups:
                groups.append("other")
        image_differs = (
            ours["image_digest"] is not None
            and theirs["image_digest"] is not None
            and ours["image_digest"] != theirs["image_digest"]
        )
        if image_differs:
            groups.append("image")
        row["state"] = DIFFERS if groups else SAME
        row["changed_groups"] = groups

    present = [r for r in rows if r["present"]]
    counts = {state: 0 for state in (BASELINE, SAME, DIFFERS, ABSENT, NO_BASELINE)}
    for row in rows:
        counts[row["state"]] += 1
    return {
        "agent": name,
        "baseline_workspace": baseline_id,
        "environments": rows,
        "summary": {
            "workspaces": len(rows),
            "present": len(present),
            "same": counts[SAME],
            "differs": counts[DIFFERS],
            "absent": counts[ABSENT],
            "distinct_spec_digests": len({r["agent"]["spec_digest"] for r in present}),
            "aligned": reference is not None
            and all(r["state"] in (BASELINE, SAME) for r in present),
        },
    }


# ── drift ───────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DriftTarget:
    """What a worker thread needs of one agent: plain values, no ORM row (the request's
    session is never shared across threads)."""

    agent_id: str
    name: str
    method: str
    kind: str  # "runtime" | "harness"
    resource_id: str
    version: str | None
    canary: bool


def _is_not_found(exc: Exception) -> bool:
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code") in (
            "ResourceNotFoundException",
            "NotFoundException",
        )
    return type(exc).__name__ == "ResourceNotFoundException"


def _text(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def _observe(control: Any, target: DriftTarget) -> tuple[str | None, str | None]:
    """(status, version) as AWS reports them right now."""
    if target.kind == "harness":
        raw = harness_api.get_harness(control, target.resource_id)
        return raw.get("status"), _text(raw.get("harnessVersion"))
    raw = runtime_api.get_runtime(control, target.resource_id)
    return raw.get("status"), _text(raw.get("agentRuntimeVersion"))


def check_agent(control: Any, target: DriftTarget) -> dict[str, Any]:
    """One agent's ledger-vs-AWS verdict. Never raises."""
    result: dict[str, Any] = {
        "agent_id": target.agent_id,
        "name": target.name,
        "method": target.method,
        "kind": target.kind,
        "state": UNKNOWN,
        "findings": [],
        "reason": None,
        "ledger_version": target.version,
        "aws_version": None,
        "aws_status": None,
    }
    try:
        status, version = _observe(control, target)
    except Exception as exc:
        if _is_not_found(exc):
            result["state"] = DRIFT
            result["findings"].append(
                {"code": "resource_missing", "expected": target.resource_id, "observed": None}
            )
            return result
        logger.warning("drift read failed for agent %s: %s", target.agent_id, type(exc).__name__)
        result["reason"] = "unreadable"
        return result
    result["aws_status"], result["aws_version"] = status, version
    if not status:
        result["reason"] = "no_status_reported"  # an empty answer is not a healthy one
        return result
    if status in _TRANSIENT:
        result["reason"] = "transitioning"
        return result
    findings: list[dict[str, Any]] = result["findings"]
    if status not in _HEALTHY:
        findings.append({"code": "unhealthy", "expected": "READY", "observed": status})
    expected = _text(target.version)
    if expected is None or version is None:
        if not findings:
            result["reason"] = "version_not_comparable"
            return result
    elif expected != version:
        if target.canary:
            # a running canary mints a candidate version on purpose; the ledger's
            # version is the champion's, so a difference cannot be called drift
            if not findings:
                result["reason"] = "canary_active"
                return result
        else:
            findings.append({"code": "version_changed", "expected": expected, "observed": version})
    result["state"] = DRIFT if findings else IN_SYNC
    return result


def _canary_agent_ids(db: Session, workspace_id: str) -> set[str]:
    from app.optimization.models import RuntimeCanary

    rows = db.execute(
        select(RuntimeCanary.champion_agent_id, RuntimeCanary.challenger_agent_id).where(
            RuntimeCanary.workspace_id == workspace_id, RuntimeCanary.status == "running"
        )
    )
    return {agent_id for pair in rows for agent_id in pair if agent_id}


def detect_drift(
    db: Session,
    *,
    workspace_id: str,
    control: Any,
    viewer: Viewer = Viewer.ALL,
    agent_name: str | None = None,
    limit: int = DRIFT_AGENT_LIMIT,
    reader: Callable[[Any, DriftTarget], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Ledger expectation vs what AWS reports, for the workspace's live agents the
    viewer can see (filtered in SQL, before the cap). Read-only."""
    started = time.monotonic()
    query = select(Agent).where(
        Agent.workspace_id == workspace_id,
        Agent.status == "active",
        Agent.resource_id.is_not(None),
        agent_access.agent_clause(viewer),
    )
    if agent_name:
        query = query.where(Agent.name == agent_name)
    agents = [a for a in db.scalars(query.order_by(Agent.name, Agent.id)) if resource_kind(a)]
    truncated = len(agents) > limit
    agents = agents[:limit]
    canaries = _canary_agent_ids(db, workspace_id) if agents else set()
    targets = [
        DriftTarget(
            agent_id=a.id,
            name=a.name,
            method=a.method,
            kind=resource_kind(a) or "runtime",
            resource_id=a.resource_id or "",
            version=_text(a.version),
            canary=a.id in canaries,
        )
        for a in agents
    ]
    check = reader or check_agent
    if targets:
        with ThreadPoolExecutor(max_workers=_READ_WORKERS) as pool:
            results = list(pool.map(lambda t: check(control, t), targets))
    else:
        results = []
    counts = {IN_SYNC: 0, DRIFT: 0, UNKNOWN: 0}
    for item in results:
        counts[item["state"]] += 1
    overall = DRIFT if counts[DRIFT] else UNKNOWN if counts[UNKNOWN] else IN_SYNC
    return {
        "workspace_id": workspace_id,
        "state": overall,
        "checked": len(results),
        "counts": counts,
        "truncated": truncated,
        "limit": limit,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "agents": results,
    }
