"""Background settle for the storage a deleted knowledge base leaves behind (WB-008).

`SelfHostedKnowledgeBackend.delete_kb` commits the ledger delete first and only then
purges the KB's S3 prefix, drops its OpenSearch index and deletes its rows from the two
fixed indices. Those three steps are best-effort by contract — the row is gone, so a
failure there must never turn into a 5xx on the delete the member asked for (a retried
``DELETE`` would only 404). Until this module only `OpenSearchError` was caught: a
urllib3 read timeout re-raised by `opensearch_retry.RetryingHttp`, or a botocore error
from the S3 purge, propagated after the commit and surfaced as a 502.

Now every step is wrapped (`run_steps` catches ``Exception``), the delete answers
``200 {deleted: true, cleanup: {s3|index|docs: ok|pending}, cleanup_warnings: [...]}``,
and the pending steps are handed to a ``kb_cleanup`` job — the same shape as the
teardown's ``log_group_settle`` (`deployer/teardown.py`): a `Job` row the console can
watch on ``GET /api/jobs/{job_id}``, a daemon thread that retries the remaining steps on
a bounded backoff schedule (`DEFAULT_RETRY_SCHEDULE_S`: 5 s, 30 s, 120 s — three retries,
≤ 155 s plus the calls), and a startup resume starter (`start_cleanup_resume`, registered
in `pipeline.resume_pending_jobs`) that re-runs a job a backend restart left ``queued`` /
``running``. Every step is idempotent (`delete_index` tolerates 404, `delete_by_query`
and the prefix purge are no-ops on nothing), so re-running one is always safe.

Nothing here raises past the caller: the delete has already answered, and a job whose
ledger row, workspace or backend cannot be resolved closes ``failed`` with the reason.
The module constructs no AWS client — the step bodies live on the knowledge backend
(`run_cleanup_step`), which resolves them through the usual funnel.
"""

import json
import logging
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from app.core.db import SessionLocal
from app.core.errors import redacted_error
from app.models.ledger import Job
from app.services import knowledge_backend
from app.services.workspace import WorkspaceContext, context_for_workspace

logger = logging.getLogger("launchpad.knowledge")

JOB_TYPE = "kb_cleanup"
STAGE = "kb_cleanup"  # the `stage` of the job's JSONL events (the deploy job's shape)
STEP_S3 = "s3"  # purge the KB's `kb/<kb_id>/` prefix in the workspace bucket
STEP_INDEX = "index"  # drop the KB's own k-NN index
STEP_DOCS = "docs"  # delete the KB's rows from the fixed kb-docs + jobs indices
STEPS: tuple[str, ...] = (STEP_S3, STEP_INDEX, STEP_DOCS)
OK = "ok"
PENDING = "pending"
SKIPPED = "skipped"  # a step that does not apply (an `existing`-mode data source owns no objects)
# `target` of a run — what the steps act on. ``{"scope": "kb"}`` (default, the KB's own
# prefix/index/rows) or ``{"scope": "data_source", "ds_id": …, "prefix": …}`` (WB-010: one
# data source's objects and rows). It is passed to `run_cleanup_step` as keywords and stored
# in the job payload, so one job type and one resume starter serve both.
SCOPE_KB = "kb"
SCOPE_DATA_SOURCE = "data_source"
TARGET_KEYS = ("scope", "ds_id", "prefix")
# Seconds slept before retry 1, 2, 3 of the pending steps (the request thread already ran
# each step once). Bounded on purpose: a step still failing after ~2.5 min is a domain or
# bucket problem the job records as `failed` for an operator, not something to poll forever.
DEFAULT_RETRY_SCHEDULE_S: tuple[float, ...] = (5.0, 30.0, 120.0)
_SLEEP = time.sleep  # the one sleeper of this module; tests replace it


def _spawn_thread(target: Callable[[], Any], name: str) -> threading.Thread:
    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    return thread


_SPAWN = _spawn_thread  # the one thread starter of this module; tests run the target inline


def _err(exc: BaseException) -> str:
    """N17-obs99: a `cleanup_warnings` line is type + status / error code + operation, never a
    message — the OpenSearch body (root cause, IAM sentence with the role ARN) and botocore's
    framing used to travel in the first line of `str(exc)`."""
    return redacted_error(exc)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def run_steps(
    backend: Any, ws: WorkspaceContext, kb_id: str, bucket: str, steps: Sequence[str],
    target: dict[str, Any] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Run each requested step once through ``backend.run_cleanup_step``; never raises.
    Returns ``({step: "ok"|"pending"}, warnings)`` — one warning line per failed step,
    exception type + status / error code + operation (`redacted_error`), never a message or
    a response body. `target` (see `TARGET_KEYS`) is
    passed through as keywords; ``None`` is the KB scope."""
    outcome: dict[str, str] = {}
    warnings: list[str] = []
    for step in steps:
        try:
            backend.run_cleanup_step(ws, kb_id, bucket, step, **(target or {}))
            outcome[step] = OK
        except Exception as exc:  # noqa: BLE001 — best-effort by contract, see module doc
            outcome[step] = PENDING
            warnings.append(f"{step}: {_err(exc)}")
            logger.warning(
                "knowledge_selfhosted: cleanup step %s failed for kb %r: %s",
                step, kb_id, _err(exc),
            )
    return outcome, warnings


# ── ledger ───────────────────────────────────────────────────────────────────────
def _job_create(
    ws: WorkspaceContext, kb_id: str, bucket: str, steps: Sequence[str],
    warnings: Sequence[str], schedule: Sequence[float], target: dict[str, Any] | None,
) -> str | None:
    """The row the background retries report on; ``None`` (logged) when the ledger
    refuses — the retries then still run, logging only."""
    try:
        db = SessionLocal()
        try:
            job = Job(
                workspace_id=ws.id,
                type=JOB_TYPE,
                status="queued",
                payload={
                    "kb_id": kb_id,
                    "bucket": bucket,
                    "steps": list(steps),
                    "schedule_s": [float(v) for v in schedule],
                    "warnings": list(warnings),
                    **{k: v for k, v in (target or {}).items() if k in TARGET_KEYS},
                },
            )
            db.add(job)
            db.commit()
            return job.id
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 — the cleanup must not depend on the ledger
        logger.warning("kb %s: could not file the cleanup job: %s", kb_id, _err(exc))
        return None


def _job_update(
    job_id: str | None,
    *,
    status: str | None = None,
    message: str | None = None,
    level: str = "info",
    error: str | None = None,
    steps: Sequence[str] | None = None,
    warnings: Sequence[str] | None = None,
) -> None:
    """Append one JSONL event and/or move the status / the remaining steps."""
    if not job_id:
        return
    try:
        db = SessionLocal()
        try:
            job = db.get(Job, job_id)
            if job is None:
                return
            if message is not None:
                line = json.dumps(
                    {"ts": _now_iso(), "stage": STAGE, "level": level, "msg": message},
                    ensure_ascii=False,
                )
                job.log = (job.log + "\n" + line) if job.log else line
            if status is not None:
                job.status = status
            if error is not None:
                job.error = error
            if steps is not None or warnings is not None:
                payload = dict(job.payload or {})
                if steps is not None:
                    payload["steps"] = list(steps)
                if warnings is not None:
                    payload["warnings"] = list(warnings)
                job.payload = payload  # reassign: JSON columns do not track in-place edits
            db.commit()
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("kb cleanup job %s: ledger update failed: %s", job_id, _err(exc))


# ── the retries ──────────────────────────────────────────────────────────────────
def _run(
    job_id: str | None,
    backend: Any,
    ws: WorkspaceContext,
    kb_id: str,
    bucket: str,
    steps: Sequence[str],
    schedule: Sequence[float],
    target: dict[str, Any] | None = None,
) -> list[str]:
    """Body of the background retries: sleep, re-run what is still pending, repeat while
    the schedule lasts. Never raises. Returns the steps still pending at the end."""
    remaining = [s for s in STEPS if s in steps]
    _job_update(
        job_id, status="running",
        message=f"retrying cleanup of {', '.join(remaining)} for kb {kb_id} at "
                f"{', '.join(f'+{d:g}s' for d in schedule)}",
    )
    last_warnings: list[str] = []
    for attempt, delay in enumerate(schedule, start=1):
        try:
            _SLEEP(delay)
            outcome, last_warnings = run_steps(backend, ws, kb_id, bucket, remaining, target)
        except Exception as exc:  # noqa: BLE001 — a broken sleeper/backend ends the job
            note = f"cleanup retry {attempt} aborted: {_err(exc)}"
            _job_update(job_id, status="failed", error=note, level="error", message=note)
            return remaining
        done = [s for s in remaining if outcome.get(s) == OK]
        remaining = [s for s in remaining if outcome.get(s) != OK]
        _job_update(
            job_id,
            message=f"retry {attempt}/{len(schedule)}: "
                    f"ok [{', '.join(done)}] pending [{', '.join(remaining)}]"
                    + (" — " + "; ".join(last_warnings) if last_warnings else ""),
            level="warning" if remaining else "info",
            steps=remaining,
            warnings=last_warnings,
        )
        if not remaining:
            _job_update(job_id, status="succeeded", message=f"cleanup of kb {kb_id} complete")
            return []
    error = (
        f"cleanup of kb {kb_id} still pending after {len(schedule)} retries: "
        f"{'; '.join(last_warnings) or ', '.join(remaining)}"
    )
    _job_update(job_id, status="failed", error=error, level="error", message=error)
    return remaining


def schedule_cleanup(
    backend: Any,
    ws: WorkspaceContext,
    kb_id: str,
    bucket: str,
    pending: Sequence[str],
    warnings: Sequence[str],
    schedule: Sequence[float] = DEFAULT_RETRY_SCHEDULE_S,
    target: dict[str, Any] | None = None,
) -> str | None:
    """File the ``kb_cleanup`` job for `pending` and start its thread; returns the job id
    (``None`` when the ledger refused — the thread still runs). Never raises."""
    job_id = _job_create(ws, kb_id, bucket, pending, warnings, schedule, target)
    suffix = f"-{target['ds_id']}" if target and target.get("ds_id") else ""
    try:
        _SPAWN(lambda: _run(job_id, backend, ws, kb_id, bucket, pending, schedule, target),
               f"kb-cleanup-{kb_id}{suffix}")
    except Exception as exc:  # noqa: BLE001
        note = f"cleanup thread did not start: {_err(exc)}"
        logger.warning("kb %s: %s", kb_id, note)
        _job_update(job_id, status="failed", error=note, level="error", message=note)
    return job_id


# ── startup resume ───────────────────────────────────────────────────────────────
def _schedule_from_payload(raw: Any) -> tuple[float, ...]:
    try:
        schedule = tuple(float(v) for v in (raw or ()) if float(v) > 0)
    except (TypeError, ValueError):
        schedule = ()
    return schedule or DEFAULT_RETRY_SCHEDULE_S


def _resume(job_id: str) -> list[str] | None:
    """Body of the startup resume of a ``kb_cleanup`` job a restart left open: the whole
    recorded schedule runs again from now against the recorded steps. Never raises."""
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None or job.type != JOB_TYPE or job.status not in ("queued", "running"):
            return None  # terminal or not ours — inert
        payload = dict(job.payload or {})
        workspace_id = job.workspace_id
    finally:
        db.close()
    kb_id = str(payload.get("kb_id") or "?")
    bucket = str(payload.get("bucket") or "")
    steps = [s for s in STEPS if s in (payload.get("steps") or [])]
    schedule = _schedule_from_payload(payload.get("schedule_s"))
    target = {k: payload[k] for k in TARGET_KEYS if payload.get(k)} or None

    def abandon(reason: str) -> None:
        note = f"interrupted by backend restart; not re-run: {reason}"
        logger.warning("kb cleanup job %s: %s", job_id, note)
        _job_update(job_id, status="failed", error=note, level="error", message=note)

    if not steps:
        _job_update(job_id, status="succeeded", message="nothing left to clean up")
        return []
    if STEP_S3 in steps and not bucket:
        abandon("payload carries no bucket for the s3 step")
        return None
    if target and target.get("scope") == SCOPE_DATA_SOURCE and not target.get("ds_id"):
        abandon("payload carries a data_source scope without a ds_id")
        return None
    try:
        backend = knowledge_backend.get_backend()  # raises `feature.unavailable` when unset
    except Exception:  # noqa: BLE001
        backend = None
    if backend is None or not hasattr(backend, "run_cleanup_step"):
        abandon("no knowledge backend with cleanup steps is registered")
        return None
    try:
        ws = context_for_workspace(workspace_id)
    except Exception as exc:  # noqa: BLE001 — the workspace may have been deleted meanwhile
        abandon(f"workspace {workspace_id!r} could not be rebuilt: {_err(exc)}")
        return None
    what = f"kb {kb_id}" if not target else f"kb {kb_id} data source {target.get('ds_id')}"
    _job_update(
        job_id,
        message=f"interrupted by backend restart — re-running the cleanup of "
                f"[{', '.join(steps)}] for {what} from now",
    )
    return _run(job_id, backend, ws, kb_id, bucket, steps, schedule, target)


def start_cleanup_resume(job_id: str) -> threading.Thread:
    """Startup resume starter (``pipeline.resume_pending_jobs``)."""
    return _SPAWN(lambda: _resume(job_id), f"kb-cleanup-resume-{job_id[:8]}")
