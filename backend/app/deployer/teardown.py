"""Agent teardown residue — what an agent leaves in AWS besides its Runtime.

N10-bug7 / N10-bug8 (live cn-northwest-1): ``DELETE /api/agents/{id}`` removed the
AgentCore Runtime and nothing else. After four deletes the account still carried the
per-agent execution roles (``launchpad-agent-<name>-<id8>``), the runtime log groups
(``/aws/bedrock-agentcore/runtimes/<runtime_id>-DEFAULT``), the S3 artifact prefix
(``agents/<name>/``), the self-hosted memory snapshots of the agent's scoped actors
(``<memory_prefix>/<store>/<agent_id>__<human>/…``) and the auto-created A2A registry
record pointing at a runtime that no longer existed.

``cleanup_residue`` runs the residue steps **best-effort**: each one is isolated, logged
and reported; a failing step never stops the next one and never fails the delete the
member asked for. The log-group step runs **last** (N10v4-obs9): ``DeleteAgentRuntime``
returns while the runtime is still ``DELETING`` and an A2A runtime's trailing OTel flush
re-created the just-deleted group ~1 s later, empty — so the step first waits for the
runtime to be gone, deletes, then **settles** in the background (N-SCN-obs8): live, the
trailing flush landed 2 s and 18 s after the delete had answered, past the old 5 s inline
re-check. ``settle_log_groups`` re-lists the prefix at +10 s, +30 s and +60 s after the
first delete, deletes whatever came back, stops after two consecutive empty checks, and
records its outcome on a ``log_group_settle`` job row — the delete response never waits
for it, and nothing in the settle can fail the purge. A backend restart that interrupts a
settle leaves that row ``queued`` / ``running``; ``start_settle_resume`` (registered in
``pipeline.resume_pending_jobs``) re-runs the recorded schedule against the recorded prefix
on startup and closes the row — list + delete is idempotent, so repeating a settle is safe.
The method-specific Runtime delete stays in the router (it is the one hard step — a
Runtime that could not be deleted keeps the ledger row so the member can retry).
Every AWS client comes from ``WorkspaceContext.client`` (the funnel).

Why the role survived live although ``agent_iam.delete_role`` existed: the hub's EC2
ops role grants ``iam:DeleteRole``/``iam:DeleteRolePolicy`` but not
``iam:ListRolePolicies``, so the helper's listing was denied, ``DeleteRole`` then hit
``DeleteConflict`` (inline policies still attached) and the failure was only logged.
``_delete_known_inline_policies`` removes the two policy names the platform itself
attaches *before* the helper runs, so the role delete succeeds without the listing.
"""

import json
import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError

from app.core.db import SessionLocal
from app.deployer import build_dirs
from app.models.ledger import Agent, Job
from app.services import agent_iam, memory_backend, memory_selfhosted, registry_backend, s3_sweep
from app.services import opensearch_http as _opensearch_core
from app.services.agentcore import runtime as rt
from app.services.agentcore.client import control_client
from app.services.observability import RUNTIME_LOG_GROUP_PREFIX
from app.services.workspace import WorkspaceContext, context_for_workspace

logger = logging.getLogger(__name__)

ARTIFACT_PREFIX_TMPL = "agents/{name}/"
_S3_DELETE_BATCH = 1000
_DETAIL_MAX = 300

STEP_RUNTIME = "runtime"
STEP_ROLE = "execution_role"
STEP_LOGS = "log_groups"
STEP_ARTIFACTS = "artifacts"
STEP_SNAPSHOTS = "memory_snapshots"
STEP_RECORDS = "memory_records"  # N11-obs19 (r3-C): long-term records in OpenSearch
STEP_REGISTRY = "registry_record"
STEP_BUILD_DIR = "build_dir"  # N17-obs151: the local data/builds/<agent> scratch
# Log groups go LAST (N10v4-obs9): only once the runtime is gone can nothing re-create them.
RESIDUE_STEPS: tuple[str, ...] = (
    STEP_ROLE, STEP_ARTIFACTS, STEP_SNAPSHOTS, STEP_RECORDS, STEP_REGISTRY, STEP_BUILD_DIR,
    STEP_LOGS,
)
# Defaults when the settings object lacks the knobs (tests pass a bare namespace).
DEFAULT_RUNTIME_GONE_TIMEOUT_S = 120
# N-SCN-obs8: seconds after the first delete at which the settle re-lists the prefix. The
# live re-creations landed at +2 s and +18 s; two consecutive empty checks end it early
# (10 → 30 s in the common case), the full schedule is ≤ 60 s + three API round-trips.
DEFAULT_LOG_GROUP_SETTLE_S: tuple[float, ...] = (10.0, 30.0, 60.0)
SETTLE_EMPTY_STREAK = 2  # consecutive empty checks that end the settle early
SETTLE_JOB_TYPE = "log_group_settle"
_RUNTIME_GONE_INTERVAL_S = 3
_SLEEP = time.sleep  # the one sleeper of this module; tests replace it


def _spawn_thread(target: Callable[[], None], name: str) -> threading.Thread:
    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    return thread


_SPAWN = _spawn_thread  # the one thread starter of this module; tests run the target inline

DELETED = "deleted"
SKIPPED = "skipped"
FAILED = "failed"


@dataclass
class TeardownStep:
    step: str
    status: str  # deleted | skipped | failed
    detail: str = ""
    # the background follow-up of this step, when it has one (``log_groups``: the
    # ``log_group_settle`` job — ``GET /api/jobs/{job_id}``)
    job_id: str | None = None
    # N-SCN-obs9: for a ``failed`` step, the one-line reason `failed_detail` carries —
    # exception type + the code/status that tells a transient from a leak, never a body
    reason: str = ""


@dataclass
class TeardownReport:
    """What one agent delete removed. ``aws_resource_deleted`` keeps the historical
    meaning of the API field: the method-specific Runtime was torn down (``False`` for a
    discovered/imported agent, which the workspace does not own)."""

    aws_resource_deleted: bool
    steps: list[TeardownStep] = field(default_factory=list)

    @property
    def removed(self) -> list[str]:
        return [s.step for s in self.steps if s.status == DELETED]

    @property
    def skipped(self) -> list[str]:
        """N16-obs76: steps with nothing to remove (the resource never existed, or the
        step does not apply to this agent) — ``steps[].detail`` carries the reason."""
        return [s.step for s in self.steps if s.status == SKIPPED]

    @property
    def failed(self) -> list[str]:
        return [s.step for s in self.steps if s.status == FAILED]

    @property
    def failed_detail(self) -> dict[str, str]:
        """N-SCN-obs9: ``{step: reason}`` for every failed step, so the operator can tell a
        transient (`OpenSearchError: HTTP 503`, `ClientError: ThrottlingException on
        DeleteLogGroup`) from a real leak without the backend log. Additive to `failed`."""
        return {s.step: (s.reason or s.detail) for s in self.steps if s.status == FAILED}

    def as_dict(self) -> dict[str, Any]:
        return {
            "aws_resource_deleted": self.aws_resource_deleted,
            "removed": self.removed,
            "skipped": self.skipped,
            "failed": self.failed,
            "failed_detail": self.failed_detail,
            "steps": [asdict(s) for s in self.steps],
        }


def step_dict(step: TeardownStep) -> dict[str, Any]:
    """One step as the JSON the reports / job payloads carry."""
    return asdict(step)


def _err(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= _DETAIL_MAX else text[: _DETAIL_MAX - 1] + "…"


_REASON_MAX = 160


def short_reason(exc: BaseException) -> str:
    """N-SCN-obs9: the `failed_detail` text for one exception — type plus the one
    code/status that classifies it; never a response body (`OpenSearchError.__str__`
    carries the first 500 bytes of the body, a botocore message may echo request
    content) and never more than the first line, capped at ``_REASON_MAX``."""
    name = type(exc).__name__
    if isinstance(exc, _opensearch_core.OpenSearchError):
        return f"{name}: HTTP {exc.status}"
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code") or "Unknown")
        return f"{name}: {code} on {exc.operation_name or 'unknown operation'}"
    first = str(exc).strip().splitlines()[0].strip() if str(exc).strip() else ""
    text = f"{name}: {first}" if first else name
    return text if len(text) <= _REASON_MAX else text[: _REASON_MAX - 1] + "…"


def _is_not_found(exc: Exception) -> bool:
    return any(
        marker in f"{type(exc).__name__}{exc}"
        for marker in ("ResourceNotFound", "NoSuchEntity", "NoSuchBucket", "NotFound")
    )


# ── the steps ──────────────────────────────────────────────────────────────────


def _delete_known_inline_policies(iam: Any, agent: Agent, log: Callable[[str], None]) -> None:
    """Drop the inline policies the platform attaches by name, ignoring absence.

    ``agent_iam.delete_role`` lists them first; an ops role without
    ``iam:ListRolePolicies`` is denied that listing and the role delete then fails on
    ``DeleteConflict``. The names are deterministic, so they need no listing."""
    role = agent_iam.role_name_for(agent.name, agent.id)
    for policy in (
        agent_iam.capability_policy_name(agent.name),
        agent_iam.fs_policy_name(agent.name),
    ):
        try:
            iam.delete_role_policy(RoleName=role, PolicyName=policy)
            log(f"deleted inline policy {policy} on {role}")
        except Exception as exc:  # noqa: BLE001 — absence is the common case
            if not _is_not_found(exc):
                log(f"could not delete inline policy {policy} on {role}: {_err(exc)}")


def _step_execution_role(
    agent: Agent, workspace: WorkspaceContext, settings: Any, log: Callable[[str], None]
) -> TeardownStep:
    if not getattr(settings, "per_agent_execution_roles", True):
        return TeardownStep(STEP_ROLE, SKIPPED, "shared execution role is not per-agent")
    role = agent_iam.role_name_for(agent.name, agent.id)
    iam = workspace.client("iam")
    _delete_known_inline_policies(iam, agent, log)
    outcome = agent_iam.delete_execution_role_outcome(agent, settings, workspace, log, iam=iam)
    if outcome == agent_iam.ROLE_DELETED:
        return TeardownStep(STEP_ROLE, DELETED, role)
    if outcome == agent_iam.ROLE_ABSENT:
        # N16-obs76: a role that never existed (deploy failed before provision) or was
        # already swept is not a removal — the report must not claim one
        return TeardownStep(STEP_ROLE, SKIPPED, f"{role} did not exist")
    return TeardownStep(
        STEP_ROLE, FAILED, f"{role} could not be deleted — see the backend log; "
        "scripts/teardown.py sweeps tagged orphans",
    )


def runtime_log_group_prefix(runtime_id: str) -> str:
    """``/aws/bedrock-agentcore/runtimes/<runtime_id>-`` — the trailing dash keeps the
    prefix from matching another runtime whose id merely starts with this one."""
    return f"{RUNTIME_LOG_GROUP_PREFIX}{runtime_id}-"


def _list_log_groups(logs: Any, prefix: str) -> list[str]:
    names: list[str] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"logGroupNamePrefix": prefix}
        if token:
            kwargs["nextToken"] = token
        page = logs.describe_log_groups(**kwargs)
        names.extend(g["logGroupName"] for g in page.get("logGroups", []))
        token = page.get("nextToken")
        if not token:
            return names


def _delete_log_groups(
    logs: Any, names: list[str], log: Callable[[str], None], *, note: str = ""
) -> tuple[list[str], list[str]]:
    """Delete each named group, idempotent on NotFound; returns (deleted, errors)."""
    deleted: list[str] = []
    errors: list[str] = []
    for name in names:
        try:
            logs.delete_log_group(logGroupName=name)
            deleted.append(name)
            log(f"deleted log group {name}{note}")
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                deleted.append(name)
            else:
                errors.append(f"{name}: {_err(exc)}")
    return deleted, errors


def delete_runtime_log_groups(
    logs: Any, runtime_id: str, log: Callable[[str], None]
) -> tuple[list[str], list[str]]:
    """List and delete every group under the runtime's prefix; (deleted, errors). Shared by
    the agent-delete step and ``scripts/teardown.py``."""
    prefix = runtime_log_group_prefix(runtime_id)
    return _delete_log_groups(logs, _list_log_groups(logs, prefix), log)


@dataclass
class SettleOutcome:
    """What one settle pass found: ``checks`` listings made, the groups it deleted again,
    the errors it swallowed, and whether two empty checks ended it before the schedule."""

    checks: int = 0
    re_deleted: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stopped_early: bool = False

    def summary(self) -> str:
        text = f"{self.checks} check(s) · {len(self.re_deleted)} re-created and deleted again"
        if self.stopped_early:
            text += " · stopped early (two consecutive empty checks)"
        if self.errors:
            text += f" · {len(self.errors)} error(s) · {self.errors[0]}"
        return text


def format_schedule(schedule: Sequence[float]) -> str:
    return "+" + "/".join(f"{s:g}" for s in schedule) + " s"


def settle_log_groups(
    logs: Any,
    prefix: str,
    *,
    schedule: Sequence[float],
    log: Callable[[str], None],
    sleeper: Callable[[float], None] | None = None,
) -> SettleOutcome:
    """Re-list ``prefix`` at each offset of ``schedule`` (seconds after the first delete —
    the caller starts the clock), delete whatever a trailing flush re-created, and stop
    early once ``SETTLE_EMPTY_STREAK`` consecutive checks find nothing. Never raises: a
    failing listing is recorded and the next check still runs."""
    sleep = sleeper or _SLEEP
    outcome = SettleOutcome()
    elapsed = 0.0
    empty_streak = 0
    for offset in schedule:
        if offset > elapsed:
            sleep(offset - elapsed)
            elapsed = offset
        outcome.checks += 1
        try:
            found = _list_log_groups(logs, prefix)
        except Exception as exc:  # noqa: BLE001 — best-effort by contract
            outcome.errors.append(f"check {format_schedule([offset])}: {_err(exc)}")
            log(f"settle check {format_schedule([offset])} failed: {_err(exc)}")
            continue
        if found:
            empty_streak = 0
            deleted, errors = _delete_log_groups(
                logs, found, log, note=f" (re-created, settle check {format_schedule([offset])})"
            )
            outcome.re_deleted.extend(deleted)
            outcome.errors.extend(errors)
            log(f"settle check {format_schedule([offset])}: {len(found)} re-created under "
                f"{prefix}, deleted again")
        else:
            empty_streak += 1
            log(f"settle check {format_schedule([offset])}: nothing under {prefix}")
            if empty_streak >= SETTLE_EMPTY_STREAK:
                break
    outcome.stopped_early = outcome.checks < len(schedule)
    return outcome


def settle_schedule(settings: Any) -> tuple[float, ...]:
    """The configured offsets, ascending and positive; empty disables the settle."""
    raw = getattr(settings, "teardown_log_group_settle_s", DEFAULT_LOG_GROUP_SETTLE_S)
    return tuple(sorted({float(v) for v in (raw or ()) if float(v) > 0}))


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _settle_job_create(agent: Agent, prefix: str, schedule: Sequence[float]) -> str | None:
    """The ledger row the background settle reports on; ``None`` (logged) when the ledger
    refuses — the settle then still runs, logging only."""
    try:
        db = SessionLocal()
        try:
            job = Job(
                workspace_id=agent.workspace_id,
                type=SETTLE_JOB_TYPE,
                status="queued",
                payload={
                    "agent_id": agent.id,
                    "runtime_id": agent.resource_id,
                    "log_group_prefix": prefix,
                    "schedule_s": [float(v) for v in schedule],
                },
            )
            db.add(job)
            db.commit()
            return job.id
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 — the settle must not depend on the ledger
        logger.warning("agent %s: could not file the log-group settle job: %s",
                       agent.id, _err(exc))
        return None


def _settle_job_update(
    job_id: str | None,
    *,
    status: str | None = None,
    message: str | None = None,
    level: str = "info",
    error: str | None = None,
) -> None:
    """Append one JSONL event (the deploy job's shape) and/or move the status."""
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
                    {"ts": _now_iso(), "stage": STEP_LOGS, "level": level, "msg": message},
                    ensure_ascii=False,
                )
                job.log = (job.log + "\n" + line) if job.log else line
            if status is not None:
                job.status = status
            if error is not None:
                job.error = error
            db.commit()
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("log-group settle job %s: ledger update failed: %s", job_id, _err(exc))


def _run_settle(
    job_id: str | None,
    logs: Any,
    prefix: str,
    schedule: Sequence[float],
    log: Callable[[str], None],
) -> SettleOutcome | None:
    """Body of the background settle. Never raises — the delete has already answered."""
    _settle_job_update(
        job_id, status="running",
        message=f"settle re-check of {prefix}* at {format_schedule(schedule)} after the delete",
    )

    def emit(msg: str) -> None:
        log(msg)
        _settle_job_update(job_id, message=msg)

    try:
        outcome = settle_log_groups(logs, prefix, schedule=schedule, log=emit)
    except Exception as exc:  # noqa: BLE001
        log(f"log-group settle of {prefix}* failed: {_err(exc)}")
        _settle_job_update(job_id, status="failed", error=_err(exc), level="error",
                           message=f"settle failed: {_err(exc)}")
        return None
    status = "failed" if outcome.errors else "succeeded"
    emit(f"settle done: {outcome.summary()}")
    _settle_job_update(job_id, status=status,
                       error=outcome.errors[0] if outcome.errors else None)
    return outcome


def _start_settle(
    agent: Agent, logs: Any, prefix: str, schedule: Sequence[float], log: Callable[[str], None]
) -> str | None:
    """File the settle job and start its thread; returns the job id (``None`` when the
    ledger refused — the thread still runs). Spawning cannot fail the step."""
    job_id = _settle_job_create(agent, prefix, schedule)
    try:
        _SPAWN(lambda: _run_settle(job_id, logs, prefix, schedule, log),
               f"log-group-settle-{agent.id}")
    except Exception as exc:  # noqa: BLE001
        log(f"could not start the log-group settle thread: {_err(exc)}")
        _settle_job_update(job_id, status="failed", error=_err(exc), level="error",
                           message=f"settle thread did not start: {_err(exc)}")
        return job_id
    return job_id


def _schedule_from_payload(raw: Any) -> tuple[float, ...]:
    """The ``schedule_s`` a settle job recorded, cleaned like ``settle_schedule``; the
    default schedule when the payload carries none (or junk) — a job exists only because a
    schedule was configured, so an empty one here is a damaged payload, not a disabled settle."""
    try:
        schedule = tuple(sorted({float(v) for v in (raw or ()) if float(v) > 0}))
    except (TypeError, ValueError):
        schedule = ()
    return schedule or DEFAULT_LOG_GROUP_SETTLE_S


def _resume_settle(job_id: str) -> SettleOutcome | None:
    """Body of the startup resume of a ``log_group_settle`` job a backend restart left
    ``queued`` / ``running``. The clock the interrupted settle was keeping is lost, so the
    whole recorded schedule runs again from now against the recorded prefix (bounded like a
    fresh settle: ≤ 60 s + three round-trips by default). Never raises: a job whose payload
    or workspace cannot be resolved closes ``failed`` with the reason — the agent row itself
    is not needed and may already be gone."""
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None or job.type != SETTLE_JOB_TYPE or job.status not in ("queued", "running"):
            return None  # terminal or not ours — inert
        payload = dict(job.payload or {})
        workspace_id = job.workspace_id
    finally:
        db.close()
    agent_id = str(payload.get("agent_id") or "?")
    prefix = str(payload.get("log_group_prefix") or "")
    schedule = _schedule_from_payload(payload.get("schedule_s"))

    def log(msg: str) -> None:
        logger.info("agent %s: %s", agent_id, msg)

    def abandon(reason: str) -> None:
        note = f"interrupted by backend restart; not re-run: {reason}"
        log(f"log-group settle job {job_id}: {note}")
        _settle_job_update(job_id, status="failed", error=note, level="error", message=note)

    # Only ever list/delete under the runtime log-group tree — a damaged payload must not
    # turn the resume into a delete of arbitrary groups.
    if not prefix.startswith(RUNTIME_LOG_GROUP_PREFIX):
        abandon(f"payload carries no runtime log-group prefix ({prefix!r})")
        return None
    try:
        logs = context_for_workspace(workspace_id).client("logs")
    except Exception as exc:  # noqa: BLE001 — the workspace may have been deleted meanwhile
        abandon(f"workspace {workspace_id!r} could not be rebuilt: {_err(exc)}")
        return None
    _settle_job_update(
        job_id,
        message=f"interrupted by backend restart — re-running the settle of {prefix}* at "
                f"{format_schedule(schedule)} from now",
    )
    return _run_settle(job_id, logs, prefix, schedule, log)


def start_settle_resume(job_id: str) -> threading.Thread:
    """Startup resume starter (``pipeline.resume_pending_jobs``): re-run the settle of a
    ``log_group_settle`` job a restart interrupted, on the module's background thread."""
    return _SPAWN(lambda: _resume_settle(job_id), f"log-group-settle-resume-{job_id[:8]}")


def _step_log_groups(
    agent: Agent, workspace: WorkspaceContext, settings: Any, log: Callable[[str], None]
) -> TeardownStep:
    """Runs last. Wait for the Runtime to be gone, delete the groups, then hand the prefix
    to the background settle (``teardown_log_group_settle_s``, N-SCN-obs8) that deletes
    whatever the trailing flush re-creates. The request thread never sleeps for it."""
    if not agent.resource_id:
        return TeardownStep(STEP_LOGS, SKIPPED, "agent has no runtime id")
    runtime_id = agent.resource_id
    wait_s = float(getattr(settings, "teardown_runtime_gone_timeout_s",
                           DEFAULT_RUNTIME_GONE_TIMEOUT_S))
    gone = rt.wait_runtime_gone(
        control_client(workspace), runtime_id,
        timeout_s=wait_s, interval_s=_RUNTIME_GONE_INTERVAL_S, sleeper=_SLEEP,
    )
    if gone:
        log(f"runtime {runtime_id} is gone — deleting its log groups")
    else:
        log(f"runtime {runtime_id} still present after {wait_s:g}s — deleting its log groups "
            "anyway; a trailing flush may re-create one (the settle re-check catches it)")
    logs = workspace.client("logs")
    prefix = runtime_log_group_prefix(runtime_id)
    deleted, errors = delete_runtime_log_groups(logs, runtime_id, log)
    # The settle: the runtime's OTel exporter flushed AFTER our delete live (+1 s, +2 s,
    # +18 s), re-creating an empty group. Three more looks, off the request thread.
    schedule = settle_schedule(settings)
    job_id = _start_settle(agent, logs, prefix, schedule, log) if schedule else None
    settle_note = ""
    if schedule:
        settle_note = f" · settle re-check at {format_schedule(schedule)} in the background"
        if job_id:
            settle_note += f" (job {job_id})"
    if errors:
        return TeardownStep(
            STEP_LOGS, FAILED,
            f"{len(deleted)} deleted · {len(errors)} failed · {errors[0]}{settle_note}",
            job_id=job_id,
            reason=f"{len(errors)} of {len(deleted) + len(errors)} log group delete(s) "
                   f"failed · {errors[0].split(': ', 1)[-1].splitlines()[0][:100]}",
        )
    if not deleted:
        return TeardownStep(STEP_LOGS, SKIPPED, f"no log group under {prefix}{settle_note}",
                            job_id=job_id)
    detail = ", ".join(deleted)
    if not gone:
        detail += f" · runtime still present after {wait_s:g}s"
    return TeardownStep(STEP_LOGS, DELETED, detail + settle_note, job_id=job_id)


def delete_s3_prefix(s3: Any, bucket: str, prefix: str) -> s3_sweep.SweepResult:
    """Delete every object under ``prefix`` through the shared reporting deleter
    (`s3_sweep.delete_keys`, N17-obs190 — a key S3 refuses inside a 200 `DeleteObjects`
    answer lands in ``failed`` with its code instead of being counted as deleted). Paginated
    and batched to the 1000-key ``DeleteObjects`` ceiling. An empty prefix is refused, never
    expanded to the whole bucket."""
    if not prefix:
        raise ValueError("refusing to delete an empty S3 prefix")
    result = s3_sweep.SweepResult()
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": _S3_DELETE_BATCH}
        if token:
            kwargs["ContinuationToken"] = token
        page = s3.list_objects_v2(**kwargs)
        keys = [c["Key"] for c in page.get("Contents", [])]
        if keys:
            swept = s3_sweep.delete_keys(s3, bucket, keys)
            result.deleted.extend(swept.deleted)
            result.failed.update(swept.failed)
        if not page.get("IsTruncated"):
            return result
        token = page.get("NextContinuationToken")


def _sweep_step(name: str, result: s3_sweep.SweepResult, where: str, what: str,
                log: Callable[[str], None]) -> TeardownStep:
    """One residue step over a prefix sweep: nothing listed → `skipped`; every key gone →
    `deleted`; refused keys (N17-obs190) → `failed` with the count the operator re-runs on
    (`reason` = the sweep summary — counts and S3 codes, never a key or a message)."""
    if not result.attempted:
        return TeardownStep(name, SKIPPED, f"nothing under {where}")
    if result.failed:
        log(f"{what}: {len(result.deleted)} deleted, {result.summary()} under {where}")
        return TeardownStep(
            name, FAILED, f"{len(result.deleted)} deleted, {result.summary()} · {where}",
            reason=result.summary(),
        )
    log(f"deleted {len(result.deleted)} {what}(s) under {where}")
    return TeardownStep(name, DELETED, f"{len(result.deleted)} object(s) · {where}")


def artifact_prefix(agent: Agent) -> str:
    return ARTIFACT_PREFIX_TMPL.format(name=agent.name)


def _step_artifacts(
    agent: Agent, workspace: WorkspaceContext, log: Callable[[str], None]
) -> TeardownStep:
    bucket = str((workspace.resources or {}).get("artifacts_bucket") or "")
    if not bucket:
        return TeardownStep(STEP_ARTIFACTS, SKIPPED, "workspace has no artifacts bucket")
    prefix = artifact_prefix(agent)
    result = delete_s3_prefix(workspace.client("s3"), bucket, prefix)
    return _sweep_step(STEP_ARTIFACTS, result, f"s3://{bucket}/{prefix}", "artifact object", log)


def snapshot_prefix(agent: Agent, sub: memory_selfhosted.MemorySubstrate) -> str:
    """``<memory_prefix>/<store>/<agent_id>__`` — every scoped actor of this agent in
    the store its spec pins (or the workspace default). ``scoped_actor`` folds the agent
    id in front of the human actor, so the id-plus-separator is the exact actor prefix."""
    memory_id = memory_backend.spec_memory_id(agent.spec) or sub.store_id
    return f"{sub.prefix}/{memory_id}/{agent.id}{memory_backend.SCOPE_SEP}"


def _step_memory_snapshots(
    agent: Agent, workspace: WorkspaceContext, log: Callable[[str], None]
) -> TeardownStep:
    if not memory_selfhosted.has_substrate(workspace):
        return TeardownStep(STEP_SNAPSHOTS, SKIPPED, "self-hosted memory is not provisioned")
    sub = memory_selfhosted.substrate_for(workspace)
    prefix = snapshot_prefix(agent, sub)
    result = delete_s3_prefix(workspace.client("s3"), sub.bucket, prefix)
    return _sweep_step(STEP_SNAPSHOTS, result, f"s3://{sub.bucket}/{prefix}",
                       "memory snapshot object", log)


def _step_memory_records(
    agent: Agent, workspace: WorkspaceContext, log: Callable[[str], None]
) -> TeardownStep:
    """N11-obs19 (r3-C): the long-term records of every scoped actor of this agent in the
    store its spec pins (or the workspace default) — one delete_by_query on the
    environment's memory index, implemented in ``memory_selfhosted.purge_agent_records``."""
    if not memory_selfhosted.has_substrate(workspace):
        return TeardownStep(STEP_RECORDS, SKIPPED, "self-hosted memory is not provisioned")
    memory_id = memory_backend.spec_memory_id(agent.spec)
    count = memory_selfhosted.purge_agent_records(workspace, agent.id, memory_id)
    scope = f"store {memory_id or 'default'} · actor prefix {agent.id}{memory_backend.SCOPE_SEP}"
    if not count:
        return TeardownStep(STEP_RECORDS, SKIPPED, f"no long-term records ({scope})")
    log(f"deleted {count} long-term memory record(s) ({scope})")
    return TeardownStep(STEP_RECORDS, DELETED, f"{count} record(s) · {scope}")


def _step_registry_record(
    agent: Agent, workspace: WorkspaceContext, log: Callable[[str], None]
) -> TeardownStep:
    record_id = agent.registry_record_id or ""
    if not record_id:
        return TeardownStep(STEP_REGISTRY, SKIPPED, "agent has no registry record")
    backend = registry_backend.backend_or_none()
    if backend is None:
        return TeardownStep(STEP_REGISTRY, SKIPPED, "no registry backend in this build")
    try:
        record = backend.get_record(workspace, record_id)
    except Exception as exc:  # noqa: BLE001 — a 404 is "already gone"
        if _is_not_found(exc) or getattr(exc, "status_code", None) == 404:
            return TeardownStep(STEP_REGISTRY, SKIPPED, f"{record_id} already gone")
        raise
    # Only the A2A record the register stage made for THIS agent (same name): a record
    # id that drifted onto another kind/name is left for a human.
    if record.get("descriptorType") != "A2A" or record.get("name") != agent.name:
        return TeardownStep(
            STEP_REGISTRY, SKIPPED,
            f"{record_id} is not this agent's A2A record "
            f"({record.get('descriptorType')} {record.get('name')!r})",
        )
    backend.delete_record(workspace, record_id)
    log(f"deleted registry record {record_id} ({record.get('status')})")
    return TeardownStep(STEP_REGISTRY, DELETED, f"{record_id} · was {record.get('status')}")


def _step_build_dir(agent: Agent, log: Callable[[str], None]) -> TeardownStep:
    """N17-obs151: the local ``data/builds/<agent.name>`` scratch the package stage left
    for inspection (one directory per agent name, recreated on every build). Best-effort like
    every residue step — the reason, never a traceback, when the host refuses the delete."""
    removal = build_dirs.remove_for_agent(agent.name)
    if removal.status == "deleted":
        log(f"deleted local build dir ({removal.detail})")
    return TeardownStep(STEP_BUILD_DIR, removal.status, removal.detail, reason=removal.reason)


# ── the orchestrator ───────────────────────────────────────────────────────────


def cleanup_residue(
    agent: Agent,
    workspace: WorkspaceContext,
    *,
    settings: Any,
    log: Callable[[str], None] | None = None,
) -> list[TeardownStep]:
    """Run every residue step, each isolated: a failure is recorded as a ``failed`` step
    (type + message, never a traceback to the caller) and the next step still runs.
    Call it **after** the Runtime delete — deleting the role while the runtime still
    references it can wedge the runtime's own deletion. The log-group step is last
    (``RESIDUE_STEPS``): it waits for that delete to complete, deletes, and leaves a
    background settle re-check behind (N-SCN-obs8)."""
    emit = log or (lambda msg: logger.info("agent %s: %s", agent.id, msg))
    steps: list[TeardownStep] = []
    runners: list[tuple[str, Callable[[], TeardownStep]]] = [
        (STEP_ROLE, lambda: _step_execution_role(agent, workspace, settings, emit)),
        (STEP_ARTIFACTS, lambda: _step_artifacts(agent, workspace, emit)),
        (STEP_SNAPSHOTS, lambda: _step_memory_snapshots(agent, workspace, emit)),
        (STEP_RECORDS, lambda: _step_memory_records(agent, workspace, emit)),
        (STEP_REGISTRY, lambda: _step_registry_record(agent, workspace, emit)),
        (STEP_BUILD_DIR, lambda: _step_build_dir(agent, emit)),
        (STEP_LOGS, lambda: _step_log_groups(agent, workspace, settings, emit)),
    ]
    assert [name for name, _ in runners] == list(RESIDUE_STEPS)
    for name, run in runners:
        try:
            step = run()
        except Exception as exc:  # noqa: BLE001 — best-effort by contract
            step = TeardownStep(name, FAILED, _err(exc), reason=short_reason(exc))
            emit(f"teardown step {name} failed: {step.detail}")
        else:
            emit(f"teardown step {name}: {step.status} · {step.detail}")
        steps.append(step)
    return steps
