"""Backend ↔ evaluation-worker job protocol (N12 E0-5; plan §B.0).

Pure stdlib — this module is inlined into the worker zip together with
``services/strands_eval``, so it must not import ``app.*``. The backend's
``evaluation/executor.py`` turns :class:`KindUnsupportedError` into the HTTP error.

Layout under the workspace artifacts bucket::

    evaluations/<job_id>/a<attempt>/status.json        JobStatus (≤ every 10 s)
    evaluations/<job_id>/a<attempt>/invocations.jsonl  one line per replayed session
    evaluations/<job_id>/a<attempt>/results.jsonl      one result record per line
    evaluations/<job_id>/a<attempt>/<artifact>.json    insights | recommendation | optimize
    evaluations/<job_id>/a<attempt>/done.json          JobDone (written last — R-18)
    evaluations/<job_id>/cancel                        marker written by the backend
    evaluations/<run_id>/shards.json                   shard list of a sharded run
"""

from __future__ import annotations

import hashlib
import math
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

JOB_TYPES: tuple[str, ...] = ("batch_eval", "online_eval", "insights", "recommend", "optimize")
# Reserved for a candidate N13 "Skill Lab on Strands" (lead ruling 4): accepted by the
# schema, refused by both the backend and the worker dispatcher until implemented.
RESERVED_JOB_TYPES: tuple[str, ...] = ("skill_taskgen", "skill_train", "skill_eval")
PHASES: tuple[str, ...] = ("invoking", "waiting", "evaluating")
RUNNING = "RUNNING"
TERMINAL_STATES: tuple[str, ...] = ("COMPLETED", "FAILED", "STOPPED")
DEFAULT_DEADLINE_S = 25200  # 7 h; the Runtime's async budget is 8 h
KIND_UNSUPPORTED_CODE = "evaluation.kind_unsupported"


class KindUnsupportedError(ValueError):
    code = KIND_UNSUPPORTED_CODE

    def __init__(self, job_type: str) -> None:
        self.job_type = job_type
        if job_type in RESERVED_JOB_TYPES:
            text = (f"job_type {job_type!r} is reserved for Skill Lab (N13) and not "
                    "implemented by this worker")
        else:
            text = f"unknown job_type {job_type!r} (implemented: {', '.join(JOB_TYPES)})"
        super().__init__(text)


def validate_job_type(job_type: str) -> str:
    if job_type not in JOB_TYPES:
        raise KindUnsupportedError(job_type)
    return job_type


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ── payloads ────────────────────────────────────────────────────────────────
@dataclass
class JobSpec:
    """The ``InvokeAgentRuntime`` payload (``spec`` is job_type-specific, §B.0)."""

    job_id: str
    job_type: str
    workspace: dict[str, Any]  # {account_id, region}
    sink: dict[str, Any]  # {bucket, prefix}
    spec: dict[str, Any]
    attempt: int = 1
    deadline_s: int = DEFAULT_DEADLINE_S
    # D21 (M1-d, additive — CA-3 precedent): ``{model_id: descriptor}`` for every catalog
    # model the spec names (judge / actor / optimizer …), resolved by the hub at dispatch
    # (`evaluation/job_descriptors.py`). The worker registers them before its inlined
    # provider table; an old payload without the field falls back to that table.
    models: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> JobSpec:
        job_type = validate_job_type(str(payload.get("job_type") or ""))
        return cls(
            job_id=str(payload["job_id"]), job_type=job_type,
            workspace=dict(payload.get("workspace") or {}), sink=dict(payload.get("sink") or {}),
            spec=dict(payload.get("spec") or {}), attempt=int(payload.get("attempt") or 1),
            deadline_s=int(payload.get("deadline_s") or DEFAULT_DEADLINE_S),
            models=dict(payload.get("models") or {}),
        )


@dataclass
class JobStatus:
    phase: str = "invoking"
    state: str = RUNNING
    progress: dict[str, Any] = field(default_factory=lambda: {"done": 0, "total": 0, "message": ""})
    updated_at: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        if self.phase not in PHASES:
            raise ValueError(f"unknown phase {self.phase!r} (expected one of {PHASES})")
        if self.state != RUNNING and self.state not in TERMINAL_STATES:
            raise ValueError(f"unknown state {self.state!r}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobStatus:
        return cls(phase=str(data.get("phase") or "invoking"),
                   state=str(data.get("state") or RUNNING),
                   progress=dict(data.get("progress") or {}),
                   updated_at=str(data.get("updated_at") or _now_iso()))


@dataclass
class JobDone:
    state: str
    error: str | None = None
    result_count: int = 0
    summary: dict[str, Any] = field(default_factory=dict)  # {scores[], budget_stops[], …}
    finished_at: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        if self.state not in TERMINAL_STATES:
            raise ValueError(
                f"done.json state must be one of {TERMINAL_STATES}, not {self.state!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobDone:
        return cls(state=str(data["state"]), error=data.get("error"),
                   result_count=int(data.get("result_count") or 0),
                   summary=dict(data.get("summary") or {}),
                   finished_at=str(data.get("finished_at") or _now_iso()))


# ── keys ────────────────────────────────────────────────────────────────────
def job_prefix(job_id: str, attempt: int) -> str:
    return f"evaluations/{job_id}/a{int(attempt)}/"


def status_key(job_id: str, attempt: int) -> str:
    return job_prefix(job_id, attempt) + "status.json"


def invocations_key(job_id: str, attempt: int) -> str:
    return job_prefix(job_id, attempt) + "invocations.jsonl"


def results_key(job_id: str, attempt: int) -> str:
    return job_prefix(job_id, attempt) + "results.jsonl"


def done_key(job_id: str, attempt: int) -> str:
    return job_prefix(job_id, attempt) + "done.json"


def artifact_key(job_id: str, attempt: int, name: str) -> str:
    return job_prefix(job_id, attempt) + name


def cancel_key(job_id: str) -> str:
    return f"evaluations/{job_id}/cancel"


def shards_key(run_id: str) -> str:
    return f"evaluations/{run_id}/shards.json"


def shard_job_id(run_id: str, index: int) -> str:
    return f"{run_id}-s{int(index)}"


# ── identities ──────────────────────────────────────────────────────────────
def record_id(job_id: str, session_id: str, evaluator_id: str, ordinal: int) -> str:
    """Deterministic result-record id → idempotent ingest across attempts (sha1 hex)."""
    raw = f"{job_id}\x1f{session_id}\x1f{evaluator_id}\x1f{int(ordinal)}".encode()
    return hashlib.sha1(raw).hexdigest()  # noqa: S324 — identity, not security


def runtime_session_id(job_id: str, attempt: int) -> str:
    """Fresh Runtime session per job attempt (the service requires ≥ 33 characters)."""
    return f"evaljob-{job_id}-a{int(attempt)}-{uuid.uuid4().hex}"


# ── sharding / health ───────────────────────────────────────────────────────
def estimate_seconds(
    session_count: int, evaluator_count: int, *, judge_seconds_est: float = 20.0,
    judge_concurrency: int = 4,
) -> float:
    calls = max(0, session_count) * max(1, evaluator_count)
    return calls * float(judge_seconds_est) / max(1, int(judge_concurrency))


def plan_shards(
    session_count: int, evaluator_count: int, *, judge_seconds_est: float = 20.0,
    judge_concurrency: int = 4, budget_s: int = DEFAULT_DEADLINE_S, max_sessions: int = 150,
) -> list[tuple[int, int]]:
    """``[start, end)`` session-index ranges, each within the time budget and session cap."""
    if session_count <= 0:
        return []
    per_session = estimate_seconds(1, evaluator_count, judge_seconds_est=judge_seconds_est,
                                   judge_concurrency=judge_concurrency)
    fit = max(1, int(budget_s // per_session)) if per_session > 0 else session_count
    size = max(1, min(int(max_sessions), fit))
    return [(start, min(start + size, session_count)) for start in range(0, session_count, size)]


def is_stale(status: JobStatus | None, *, now: datetime, stale_s: int) -> bool:
    """A job whose ``status.json`` has not moved for ``stale_s`` (or has none) is lost."""
    if status is None:
        return True
    try:
        updated = datetime.fromisoformat(status.updated_at)
    except ValueError:
        return True
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=UTC)
    return (now - updated).total_seconds() > stale_s


def shard_count_for(session_count: int, **kwargs: Any) -> int:
    return math.ceil(len(plan_shards(session_count, **kwargs)) or 0)
