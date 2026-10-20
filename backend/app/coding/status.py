"""``GET /api/coding/runtime`` — the coding runtime's state, read, never installed.

State comes from three places: the latest install/uninstall ``Job`` (in flight →
``installing`` / ``uninstalling``; a failed install with nothing installed →
``failed``), the workspace resource keys (``installed``), and the runtime's own
``GetAgentRuntime`` status (one call per workspace per 10 s, cached here) —
AWS is the source of truth for whether it is READY.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import is_not_found
from app.models.ledger import Job
from app.services.agentcore import runtime as rt
from app.services.agentcore.client import control_client
from app.services.workspace import WorkspaceContext

from . import image_build, infra, install

logger = logging.getLogger("launchpad.coding")

STATUS_TTL_S = 10.0
LOG_TAIL_LINES = 30
# admin-only `status_error` when GetAgentRuntime cannot be read (detail in the log)
STATUS_UNAVAILABLE = "aws_status_unavailable"
# AgentCore runtime status → console state
_AWS_STATE = {
    "READY": "ready",
    "CREATING": "installing",
    "UPDATING": "installing",
    "CREATE_FAILED": "failed",
    "UPDATE_FAILED": "failed",
    "DELETING": "uninstalling",
    "DELETE_FAILED": "failed",
}

_CACHE: dict[tuple[str, str], tuple[float, str | None]] = {}
_CACHE_LOCK = threading.Lock()
_clock = time.monotonic  # injectable in tests


def invalidate(workspace_id: str) -> None:
    with _CACHE_LOCK:
        for key in [k for k in _CACHE if k[0] == workspace_id]:
            del _CACHE[key]


install._on_finish.append(invalidate)


def _aws_status(ctx: WorkspaceContext, workspace_id: str, runtime_id: str) -> str | None:
    """The runtime's AWS status; ``None`` = it does not exist (deleted out of band).
    Raises on any other AWS error (the caller degrades)."""
    key = (workspace_id, runtime_id)
    now = _clock()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < STATUS_TTL_S:
            return hit[1]
    try:
        detail = rt.get_runtime(control_client(ctx), runtime_id)
        status: str | None = str(detail.get("status") or "")
    except Exception as exc:  # noqa: BLE001 — typed not-found is client-specific
        if not is_not_found(exc):
            raise
        status = None
    with _CACHE_LOCK:
        _CACHE[key] = (now, status)
    return status


def log_tail(raw: str, limit: int = LOG_TAIL_LINES) -> list[str]:
    """The job's JSONL log as readable lines (``HH:MM:SS [stage] message``)."""
    lines: list[str] = []
    for line in (raw or "").splitlines()[-limit:]:
        try:
            event = json.loads(line)
        except ValueError:
            lines.append(line)
            continue
        ts = str(event.get("ts") or "")
        clock = ts[11:19] if len(ts) >= 19 else ts
        lines.append(f"{clock} [{event.get('stage', '')}] {event.get('msg', '')}".strip())
    return lines


def job_view(job: Job | None, *, is_admin: bool) -> dict[str, Any] | None:
    if job is None:
        return None
    view: dict[str, Any] = {
        "id": job.id,
        "type": job.type,
        "status": job.status,
        # IAM / CodeBuild failure text is operator detail
        "error": job.error if is_admin else None,
    }
    if is_admin:
        view["log_tail"] = log_tail(job.log)
    return view


def runtime_status(
    db: Session, workspace_id: str, ctx: WorkspaceContext, *, is_admin: bool
) -> dict[str, Any]:
    settings = get_settings()
    resources = ctx.resources or {}
    runtime_arn = str(resources.get(infra.KEY_RUNTIME_ARN) or "") or None
    runtime_id = str(resources.get(infra.KEY_RUNTIME_ID) or "") or None
    image_tag = str(resources.get(infra.KEY_IMAGE_TAG) or "") or None
    job = install.latest_job(db, workspace_id)
    live = job is not None and job.status in install.LIVE_STATUSES
    installed = bool(runtime_arn and runtime_id)
    status_error: str | None = None

    if live:
        state = "installing" if job.type == install.INSTALL_JOB_TYPE else "uninstalling"
    elif installed:
        try:
            aws = _aws_status(ctx, workspace_id, runtime_id or "")
        except Exception as exc:  # noqa: BLE001 — a read must not fail the page
            # The detail stays in the backend log; the response carries only a stable
            # code (exception text must not reach an HTTP client).
            logger.warning("coding runtime status read failed: %s", exc)
            aws = ""
            status_error = STATUS_UNAVAILABLE
        if aws is None:
            installed = False  # deleted out of band: show it as not installed
            state = "not_installed"
        elif aws == "":
            # unknown right now: trust the last install's outcome
            ok = job is None or (job.type == install.INSTALL_JOB_TYPE and job.status == "succeeded")
            state = "ready" if ok else "failed"
        else:
            state = _AWS_STATE.get(aws, "failed")
    elif job is not None and job.type == install.INSTALL_JOB_TYPE and job.status == "failed":
        state = "failed"
    else:
        state = "not_installed"

    models = list(settings.coding_models)
    efforts = list(settings.coding_efforts)
    installed_models = resources.get(infra.KEY_MODELS)
    repair_recommended = bool(
        installed
        and state == "ready"
        and (
            image_tag != _current_image_tag()
            or (isinstance(installed_models, list) and installed_models != models)
        )
    )
    out: dict[str, Any] = {
        "installed": installed,
        "status": state,
        "runtime_arn": runtime_arn if (is_admin and installed) else None,
        "image_tag": image_tag if installed else None,
        "job": job_view(job, is_admin=is_admin),
        "models": models,
        "efforts": efforts,
        "default_model": (settings.coding_default_model
                          if settings.coding_default_model in models else (models or [""])[0]),
        "default_effort": (settings.coding_default_effort
                           if settings.coding_default_effort in efforts else (efforts or [""])[0]),
        # the image sources or the model list changed since the install: a
        # repair rolls them out (admins see the button either way)
        "repair_recommended": repair_recommended,
    }
    if is_admin and status_error:
        out["status_error"] = status_error
    return out


def _current_image_tag() -> str | None:
    try:
        return image_build.image_tag()
    except FileNotFoundError:
        return None
