"""The one background tick (§10): scheduled re-evaluation, alerts and expiry sweeps.

Launchpad had exactly one periodic thread (the model-price refresher); everything else
only happened when a request asked for it, which is why alert rules were evaluated by
whoever opened the page and a model could be updated under an agent unnoticed.

This adds a single 60-second tick that:

* runs `watch_configs` that are due (respecting their cost ceiling);
* evaluates alert rules and notifies only on a transition;
* sweeps calibration and waiver expiry into inbox-visible state, and expires
  cross-project shares past their date (projects §11.2);
* checks every hosted MCP server's Gateway target hourly (`mcp.health`, design
  projects §15.8): re-sync, tool-list drift against the approved list, health stored,
  and the same drift check for hand-made targets brought under review (§15.15).

Single-writer safety: every task claims a `scheduler_claims` row with a conditional
UPDATE, so a second backend process cannot double-run it; a claim older than ten minutes
is reclaimable (a crashed process must not wedge the schedule). Nothing here raises: a
failing task records its error and the tick continues.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models.dlc import SchedulerClaim

logger = logging.getLogger(__name__)

TICK_SECONDS = 60
CLAIM_STALE_AFTER = timedelta(minutes=10)
ALERT_EVERY = timedelta(minutes=5)
SWEEP_EVERY = timedelta(hours=1)
MCP_HEALTH_EVERY = timedelta(hours=1)
_STOP = threading.Event()
_THREAD: threading.Thread | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def claim(db: Session, task: str, *, every: timedelta, worker: str) -> bool:
    """True when this process may run `task` now (and nobody else will)."""
    now = _now()
    row = db.get(SchedulerClaim, task)
    if row is None:
        db.add(SchedulerClaim(task=task, claimed_by=worker, claimed_at=now))
        try:
            db.commit()
            return True
        except Exception:  # another process inserted first
            db.rollback()
            return False
    last_done = _aware(row.last_done_at)
    claimed_at = _aware(row.claimed_at)
    if last_done is not None and now - last_done < every:
        return False
    if claimed_at is not None and now - claimed_at < CLAIM_STALE_AFTER and row.claimed_by:
        return False  # someone is on it
    taken = db.execute(
        update(SchedulerClaim)
        .where(SchedulerClaim.task == task, SchedulerClaim.claimed_at == row.claimed_at)
        .values(claimed_by=worker, claimed_at=now)
    ).rowcount
    db.commit()
    return taken == 1


def release(db: Session, task: str) -> None:
    db.execute(
        update(SchedulerClaim).where(SchedulerClaim.task == task)
        .values(claimed_by="", claimed_at=None, last_done_at=_now())
    )
    db.commit()


# ── tasks ──────────────────────────────────────────────────────────────────────


def run_due_watches(db: Session) -> dict[str, Any]:
    from app.core.errors import AppError
    from app.core.project_scope import project_context
    from app.dlc import watch as watch_svc
    from app.models.ledger import Agent
    from app.models.projects import Project
    from app.services.workspace import context_for_workspace

    started, skipped = [], []
    # the scan is unscoped (every workspace, every project); each run is not
    for config in watch_svc.due_configs(db):
        # the agent is the subject: its project decides (a config row stamped before
        # the agent moved projects must not run — or write — as the old one)
        agent = db.get(Agent, config.agent_id)
        project_id = (agent.project_id if agent is not None else None) or config.project_id
        project = db.get(Project, project_id) if project_id else None
        if project is not None and project.status != "active":
            # an archived project serves reads only (design §5.12): nothing is queued
            config.last_status = "skipped"
            config.last_detail = "project archived"
            config.last_checked_at = _now()
            config.next_due_at = watch_svc.next_due(config)
            skipped.append({"agent_id": config.agent_id, "reason": "project.archived"})
            db.commit()
            continue
        try:
            with project_context(project_id):
                run_id = watch_svc.run_now(
                    db, config, workspace_ctx=context_for_workspace(config.workspace_id),
                    actor="scheduler",
                )
            started.append({"agent_id": config.agent_id, "run_id": run_id})
        except AppError as exc:
            config.last_status = "skipped"
            config.last_detail = f"{exc.code}: {exc.message}"[:500]
            config.last_checked_at = _now()
            config.next_due_at = watch_svc.next_due(config)
            skipped.append({"agent_id": config.agent_id, "reason": exc.code})
        except Exception as exc:  # noqa: BLE001
            config.last_status = "failed"
            config.last_detail = f"{type(exc).__name__}: {exc}"[:500]
            config.last_checked_at = _now()
            config.next_due_at = watch_svc.next_due(config)
            skipped.append({"agent_id": config.agent_id, "reason": type(exc).__name__})
        db.commit()
    return {"started": started, "skipped": skipped}


def evaluate_alerts(db: Session) -> dict[str, Any]:
    from app.models.ledger import Workspace
    from app.services import alerts as alert_service
    from app.services.workspace import context_for_workspace

    out: list[dict[str, Any]] = []
    for workspace in db.query(Workspace).all():
        try:
            result = alert_service.evaluate_rules(
                db, context_for_workspace(workspace.id), notify_transitions=True
            )
            out.append({"workspace_id": workspace.id, "firing": result.get("firing")})
        except Exception as exc:  # noqa: BLE001 - one workspace must not stop the rest
            logger.warning("alert evaluation failed for %s: %s", workspace.id, exc)
            out.append({"workspace_id": workspace.id, "error": type(exc).__name__})
    return {"workspaces": out}


def sweep_expiries(db: Session) -> dict[str, Any]:
    """Waivers past their date and calibrations past their period become visible."""
    from app.dlc import calibration as cal
    from app.models.dlc import CalibrationRecord, Waiver
    from app.models.ledger import Workspace

    now = _now()
    expired = 0
    for waiver in db.query(Waiver).filter(Waiver.status == "approved").all():
        if (_aware(waiver.expires_on) or now) <= now:
            waiver.status = "expired"
            expired += 1
    due: list[dict[str, Any]] = []
    policies = {
        w.id: cal.policy_of(w.release_policy) for w in db.query(Workspace).all()
    }
    seen: set[tuple[str, str, str]] = set()
    for record in db.query(CalibrationRecord).filter(
        CalibrationRecord.verdict == "aligned"
    ).order_by(CalibrationRecord.decided_at.desc()).all():
        key = (record.workspace_id or "", record.agent_id or "", record.criterion_key)
        if key in seen:
            continue
        seen.add(key)
        policy = policies.get(record.workspace_id) or cal.policy_of(None)
        status = cal.status_of(record, policy, now=now)
        if not status.get("calibrated") and status.get("reason") == "expired":
            due.append({"workspace_id": record.workspace_id, "agent_id": record.agent_id,
                        "criterion_key": record.criterion_key})
    db.commit()
    return {"waivers_expired": expired, "calibrations_due": due}


class _Gone(Exception):
    """The AWS resource is already gone — stop retrying this row."""


def _resource_exists(control: Any, agent: Any) -> bool:
    try:
        if agent.method == "harness":
            from app.services.agentcore import harness as hc

            hc.get_harness(control, agent.resource_id)
        else:
            from app.services.agentcore import runtime as rt

            rt.get_runtime(control, agent.resource_id)
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ in ("ResourceNotFoundException", "NotFoundException"):
            return False
        raise
    return True


def _other_endpoints(control: Any, agent: Any) -> list[dict[str, Any]]:
    """The resource's named endpoints besides DEFAULT and the release pair the gated
    path already handles — a Runtime Canary's, typically — as {name, status}."""
    from app.dlc import releases as release_svc

    skip = {"DEFAULT", release_svc.CANDIDATE, release_svc.LIVE}
    if agent.method == "harness":
        from app.services.agentcore import harness as hc

        rows = [{"name": e.get("endpointName"), "status": e.get("status")}
                for e in hc.list_harness_endpoints(control, agent.resource_id)]
    else:
        from app.services.agentcore import runtime as rt

        rows = [{"name": e.get("name"), "status": e.get("status")}
                for e in rt.list_runtime_endpoints(control, agent.resource_id)]
    return [r for r in rows if r["name"] and r["name"] not in skip]


def _delete_endpoint(control: Any, agent: Any, name: str) -> None:
    if agent.method == "harness":
        from app.services.agentcore import harness as hc

        hc.delete_harness_endpoint(control, agent.resource_id, name)
    else:
        from app.services.agentcore import runtime as rt

        rt.delete_runtime_endpoint(control, runtime_id=agent.resource_id, endpoint_name=name)


def _finish_deferred_cleanup(agent: Any, workspace: Any) -> None:
    """What the deferred delete skipped after the resource: the agent's own execution
    role, its A2A registry record and its S3 artifacts. All three helpers are
    best-effort and never raise."""
    from app.routers import agents as agents_router
    from app.services import agent_artifacts

    agents_router.delete_agent_role(agent, workspace)
    agents_router.delete_agent_registry_record(agent, workspace)
    agent_artifacts.delete_agent_artifacts(agent, workspace)


def sweep_deleted_resources(db: Session) -> dict[str, Any]:
    """Finish an AWS teardown the delete request could not wait out.

    A gated agent's endpoints — or a Runtime Canary's on any agent — can stay DELETING
    for minutes, and AgentCore refuses to delete a harness/runtime that still has
    them. Rather than hold an HTTP delete open (or leave the operator retrying by hand
    against an asynchronous state machine), the route marks the ledger row deleted
    with `aws_resource_deleted: false`, stamps `teardown_pending_at`, and this task
    retries until AWS has let go.

    Two markers select a row: `teardown_pending_at` (every deferred delete, whatever
    the endpoint_mode) and, for rows deferred before that column existed,
    `endpoint_mode == "live"`. Done clears both — `endpoint_mode` back to `default`
    (semantically true: no named endpoint remains) and `teardown_pending_at` to NULL —
    and keeps `resource_id`, which every other deleted row retains as a historical
    pointer. An ordinary deleted agent carries neither marker and is never retried.
    """
    from sqlalchemy import or_, select

    from app.dlc import releases as release_svc
    from app.models.ledger import Agent
    from app.services.agentcore.client import control_client
    from app.services.workspace import context_for_workspace

    rows = db.scalars(
        select(Agent).where(
            Agent.status == "deleted",
            Agent.resource_id.isnot(None),
            Agent.resource_id != "",
            or_(Agent.endpoint_mode == "live", Agent.teardown_pending_at.isnot(None)),
        ).limit(20)
    ).all()
    out: list[dict[str, Any]] = []
    for agent in rows:
        deferred = agent.teardown_pending_at is not None
        workspace = None
        try:
            workspace = context_for_workspace(agent.workspace_id)
            control = control_client(workspace)
            pending = [
                name for name in (release_svc.CANDIDATE, release_svc.LIVE)
                if release_svc.endpoint_status(agent, control, name) is not None
            ]
            if pending:
                # re-issue: an endpoint whose delete never landed would wait forever
                release_svc.delete_endpoints(agent, control, timeout_s=0)
            if deferred:
                # canary (or any other named) endpoints hold the resource too
                for endpoint in _other_endpoints(control, agent):
                    pending.append(endpoint["name"])
                    if str(endpoint["status"] or "").upper() != "DELETING":
                        try:
                            _delete_endpoint(control, agent, endpoint["name"])
                        except Exception as exc:  # noqa: BLE001 - retried next tick
                            logger.info("teardown sweep: agent %s endpoint %s: %s: %s",
                                        agent.id, endpoint["name"],
                                        type(exc).__name__, exc)
            if pending:
                out.append({"agent_id": agent.id, "status": "waiting",
                            "endpoints": pending})
                continue
            # Ask whether the resource is still there before deleting it: AgentCore
            # answers `DeleteHarness` on an already-gone harness with
            # **AccessDenied**, not ResourceNotFound, so inferring "gone" from the
            # delete error would either retry a vanished resource forever or swallow
            # a real permission problem.
            if not _resource_exists(control, agent):
                raise _Gone
            if agent.method == "harness":
                from app.services.agentcore import harness as hc

                hc.delete_harness(control, agent.resource_id)
            else:
                from app.services.agentcore import runtime as rt

                rt.delete_runtime(control, agent.resource_id)
        except _Gone:
            pass
        except Exception as exc:  # noqa: BLE001 - a sweep never raises
            kind = type(exc).__name__
            if kind not in ("ResourceNotFoundException", "NotFoundException"):
                out.append({"agent_id": agent.id, "status": f"error:{kind}"})
                logger.info("teardown sweep: agent %s not yet deletable: %s: %s",
                            agent.id, kind, exc)
                continue
        # gone (or never there): stop retrying this row
        if deferred and workspace is not None:
            _finish_deferred_cleanup(agent, workspace)
        agent.endpoint_mode = "default"
        agent.teardown_pending_at = None
        db.commit()
        out.append({"agent_id": agent.id, "status": "deleted"})
    return {"checked": len(rows), "results": out}

def run_mcp_health(db: Session) -> dict[str, Any]:
    """One health check per active hosted MCP server and adopted hand-made target,
    each inside its row's project; an archived project's are skipped."""
    from app.core.project_scope import project_context
    from app.models.lambda_tools import ExternalMcpTarget
    from app.models.projects import Project
    from app.services import gateway_tools, mcp_servers
    from app.services.workspace import context_for_workspace

    def archived(project_id: str | None) -> bool:
        project = db.get(Project, project_id) if project_id else None
        return project is not None and project.status != "active"

    servers: list[dict[str, Any]] = []
    for agent in mcp_servers.health_candidates(db):
        if archived(agent.project_id):
            servers.append({"agent_id": agent.id, "status": "skipped",
                            "reason": "project.archived"})
            continue
        try:
            with project_context(agent.project_id):
                health = mcp_servers.check_health(
                    context_for_workspace(agent.workspace_id), agent
                )
            servers.append({"agent_id": agent.id, "status": health.get("status")})
        except Exception as exc:  # noqa: BLE001 - one server must not stop the rest
            logger.warning("MCP health check of %s failed: %s", agent.id, exc)
            servers.append({"agent_id": agent.id, "status": f"error:{type(exc).__name__}"})
    external: list[dict[str, Any]] = []
    for row in db.query(ExternalMcpTarget).all():
        if archived(row.project_id):
            continue
        try:
            with project_context(row.project_id):
                health = gateway_tools.check_external(
                    context_for_workspace(row.workspace_id), row.id
                )
            external.append({"tool_id": row.id, "status": health.get("status")})
        except Exception as exc:  # noqa: BLE001
            logger.warning("drift check of hand-made target %s failed: %s", row.id, exc)
            external.append({"tool_id": row.id, "status": f"error:{type(exc).__name__}"})
    return {"servers": servers, "external": external}


def _sweep_eni_releases(db: Session) -> Any:
    from app.deployer import pipeline

    return pipeline.sweep_eni_releases(db)


def _start_due_purges(db: Session) -> Any:
    from app.services import project_purge

    return project_purge.start_due_purges(db)


def _expire_shares(db: Session) -> Any:
    from app.services import resource_shares

    return resource_shares.expire_due(db)


def _system_update(db: Session) -> Any:
    from app.services import system_update

    return system_update.tick(db)


TASKS = (
    ("dlc.watch", run_due_watches, TICK_SECONDS),
    ("dlc.alerts", evaluate_alerts, int(ALERT_EVERY.total_seconds())),
    ("dlc.expiry", sweep_expiries, int(SWEEP_EVERY.total_seconds())),
    # AWS teardown a delete request could not wait out (endpoints mid-deletion)
    ("dlc.teardown", sweep_deleted_resources, TICK_SECONDS),
    # hosted MCP servers: health, tool-list drift, live endpoint (projects §15.8)
    ("mcp.health", run_mcp_health, int(MCP_HEALTH_EVERY.total_seconds())),
    # a deleted VPC agent's ENIs no live waiter is polling (projects §12.9)
    ("dlc.eni_release", _sweep_eni_releases, TICK_SECONDS),
    # projects past their deletion cooling-off start their purge (§11.5)
    ("project.purge", _start_due_purges, TICK_SECONDS),
    # cross-project shares past their expiry stop counting (§11.2)
    ("project.share_expiry", _expire_shares, TICK_SECONDS),
    # online update (OU-1): a due restart writes apply.request; the updater's
    # apply.result is read back
    ("system.update", _system_update, TICK_SECONDS),
)



def tick(*, worker: str = "local") -> dict[str, Any]:
    """One pass over every scheduled task. Never raises."""
    results: dict[str, Any] = {}
    for name, task, every_seconds in TASKS:
        db = SessionLocal()
        try:
            if not claim(db, name, every=timedelta(seconds=every_seconds), worker=worker):
                continue
            try:
                results[name] = task(db)
            except Exception as exc:  # noqa: BLE001
                logger.warning("scheduled task %s failed: %s", name, exc)
                results[name] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
            finally:
                release(db, name)
        except Exception as exc:  # noqa: BLE001 - claiming itself failed
            logger.warning("scheduler could not claim %s: %s", name, exc)
        finally:
            db.close()
    return results


def _loop(worker: str) -> None:
    while not _STOP.wait(TICK_SECONDS):
        tick(worker=worker)


def start(worker: str | None = None) -> threading.Thread | None:
    """Start the tick once per process (no-op when already running)."""
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return _THREAD
    import os
    import socket

    _STOP.clear()
    name = worker or f"{socket.gethostname()}:{os.getpid()}"
    _THREAD = threading.Thread(target=_loop, args=(name,), daemon=True, name="dlc-scheduler")
    _THREAD.start()
    return _THREAD


def stop() -> None:
    _STOP.set()
