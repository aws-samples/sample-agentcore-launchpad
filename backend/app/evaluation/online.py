"""Per-agent online evaluation: continuous, sampled scoring of live sessions (N12 E2).

The ``OnlineEvalConfig`` row **is** the config (plan CA-3, §B.3): evaluators, sampling,
session timeout, filters, mode, execution status and health are columns; nothing is read
back from a control plane. The scheduler (``online_scheduler.py``) scans each ENABLED
config's log group for idle sessions, samples them into ``online_samples`` and dispatches
``online_eval`` jobs to the evaluation worker; the worker's records land in
``eval_results`` (``source="online"``, ``config_id``), which every read surface here
aggregates — the per-config results page, the Observability session detail and the
Overview quality tile.

Owners (plan §B.9 — ``external`` cannot occur any more):

- ``agent`` — created on this page for an agent: full control.
- ``experiment`` — ``exp_*`` / ``can_*`` arms created by the experiment and canary flows
  through :func:`create_for_owner` (their cleanup deletes them): read-only here.

Insights-mode configs sample sessions only; their reports are on-demand insights runs
over the sampled sessions (:func:`start_report`), listed from the run ledger.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Iterable, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.errors import AppError, NotFoundError
from app.evaluation import service as eval_service
from app.evaluation.models import EvalResult, EvalRun, OnlineEvalConfig, OnlineSample
from app.evaluation.online_evaluators import evaluator_polarity, normalize_online_evaluators
from app.evaluation.service import resolve_telemetry
from app.models.ledger import Agent
from app.services import creators, ownership, visibility
from app.services.observability import RANGE_HOURS
from app.services.strands_eval import extraction_check
from app.services.visibility import Viewer
from app.services.workspace import WorkspaceContext

if TYPE_CHECKING:
    from app.routers.auth import Identity

EXPERIMENT_PREFIXES = ("exp_", "can_")
NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,47}$")
FILTER_KEY_RE = re.compile(r"^[a-zA-Z0-9._-]{1,256}$")
FILTER_OPERATORS = (
    "Equals", "NotEquals", "GreaterThan", "LessThan",
    "GreaterThanOrEqual", "LessThanOrEqual", "Contains", "NotContains",
)
MAX_FILTERS = 5
RECENT_LIMIT = 50
# Insights mode: a config carries `insights` (+ optional clustering frequencies) INSTEAD
# of `evaluators`. The ids are the Global contract (the UI renders them by id).
INSIGHT_IDS = (
    "Builtin.Insight.FailureAnalysis",
    "Builtin.Insight.UserIntent",
    "Builtin.Insight.ExecutionSummary",
)
FREQUENCIES = ("DAILY", "WEEKLY", "MONTHLY")
Mode = str  # "scores" | "insights"
REPORT_RANGES = {"1h": 1, "6h": 6, "24h": 24, "7d": 168}
# The optional EMF mirror writes each record here too (Settings.eval_results_log_group_prefix);
# SQLite stays authoritative. The UI shows the name in delete toasts, as it did for AWS.
RESULTS_LOG_GROUP_PREFIX = "/launchpad/evaluations/results/"
BUCKET_SECONDS = {"1h": 300, "6h": 900, "24h": 3600, "7d": 6 * 3600}
QUALITY_RANGE = "24h"
SESSION_SCORES_LIMIT = 200
RESULTS_SOURCE = "online"

Owner = str  # "agent" | "experiment"
RunQueries = Callable[..., Any]  # legacy keyword on the cross-surface reads; ignored


# ─── helpers ────────────────────────────────────────────────────────────────


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return _aware(value).isoformat()
    return str(value) if value else None


def _aware(value: datetime) -> datetime:
    """SQLite hands timestamps back naive; every column here is written in UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _ts(value: datetime) -> str:
    """Logs-Insights-shaped timestamp (``YYYY-MM-DD HH:MM:SS.mmm``) the UI already parses."""
    value = _aware(value)
    return value.strftime("%Y-%m-%d %H:%M:%S.") + f"{value.microsecond // 1000:03d}"


def _ws_id(ws: Any) -> str:
    return str(ws.id)


def _ctx(ws: Any) -> WorkspaceContext:
    return ws.context if hasattr(ws, "context") else ws


def _logs_client(ctx: WorkspaceContext) -> Any:
    return ctx.client("logs")


@contextmanager
def _session(db: Session | None):
    """The caller's session, or a short-lived one for the E3-facing helpers."""
    if db is not None:
        yield db
        return
    own = SessionLocal()
    try:
        yield own
    finally:
        own.close()


def owner_of(name: str, *_legacy: Any) -> Owner:
    """``experiment`` for the arm configs the experiment / canary flows create, else ``agent``."""
    return "experiment" if str(name or "").startswith(EXPERIMENT_PREFIXES) else "agent"


def results_log_group(config_id: str) -> str:
    return f"{RESULTS_LOG_GROUP_PREFIX}{config_id}"


def mode_of(row: OnlineEvalConfig) -> Mode:
    return "insights" if row.mode == "insights" or row.insights else "scores"


def validate_insights(
    insights: Sequence[str] | None, frequencies: Sequence[str] | None
) -> tuple[list[str], list[str]]:
    """Insights-mode payload: 1..3 distinct known insight ids, 0..3 distinct frequencies."""
    ids = list(dict.fromkeys(insights or []))
    bad = [i for i in ids if i not in INSIGHT_IDS]
    if not ids or bad:
        raise AppError(
            "online_eval.mode_conflict",
            f"insights must be 1..3 of {list(INSIGHT_IDS)}",
            {"invalid": bad},
            status_code=422,
        )
    freqs = list(dict.fromkeys(frequencies or []))
    bad_f = [f for f in freqs if f not in FREQUENCIES]
    if bad_f:
        raise AppError(
            "online_eval.mode_conflict",
            f"clustering_frequencies must be a subset of {list(FREQUENCIES)}",
            {"invalid": bad_f},
            status_code=422,
        )
    return ids, freqs


def _mode_conflict(msg: str) -> AppError:
    return AppError("online_eval.mode_conflict", msg, status_code=422)


def generate_name(agent_name: str, suffix: str | None = None) -> str:
    """``oe_<agent-slug>_<6hex>`` — always matches :data:`NAME_RE`."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", agent_name or "agent").strip("_")[:24] or "agent"
    return f"oe_{slug}_{suffix or uuid.uuid4().hex[:6]}"


def new_config_id(name: str) -> str:
    """``<name>-<10hex>`` — the Global ids were ``<name>-<10 chars>``; the UI treats them
    opaquely."""
    return f"{name}-{uuid.uuid4().hex[:10]}"


def validate_filters(filters: Sequence[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Shape-check filters (key regex, operator enum, exactly one typed value)."""
    out: list[dict[str, Any]] = []
    for raw in filters or ():
        key = str(raw.get("key", "")).strip()
        operator = raw.get("operator")
        value = raw.get("value") or {}
        typed = {k: v for k, v in value.items() if v is not None}
        if not FILTER_KEY_RE.match(key):
            raise AppError("online_eval.invalid_filter", f"invalid filter key {key!r}",
                           {"key": key}, status_code=422)
        if operator not in FILTER_OPERATORS:
            raise AppError("online_eval.invalid_filter", f"invalid filter operator {operator!r}",
                           {"operator": operator}, status_code=422)
        if len(typed) != 1 or not set(typed) <= {"stringValue", "doubleValue", "booleanValue"}:
            raise AppError("online_eval.invalid_filter",
                           "filter value needs exactly one of stringValue / doubleValue / "
                           "booleanValue", {"key": key}, status_code=422)
        out.append({"key": key, "operator": operator, "value": typed})
    if len(out) > MAX_FILTERS:
        raise AppError("online_eval.invalid_filter",
                       f"at most {MAX_FILTERS} filters", {"count": len(out)}, status_code=422)
    return out


def _normalize(config: OnlineEvalConfig | dict[str, Any], **_legacy: Any) -> dict[str, Any]:
    """The console row shape (frozen key-set, plan §A.1) from a ledger row.

    A plain dict in the row's column names is accepted too (the contract key-set test
    feeds one); unknown keys simply read as ``None``.
    """
    if isinstance(config, dict):
        def get(key: str) -> Any:
            return config.get(key)
    else:
        def get(key: str) -> Any:
            return getattr(config, key, None)

    config_id = get("config_id")
    name = get("name") or ""
    filters = list(get("filters") or [])
    log_group = get("log_group")
    log_groups = list(get("log_groups") or ([log_group] if log_group else []))
    return {
        "config_id": config_id,
        "arn": get("config_arn"),
        "name": name or None,
        "description": get("description") or "",
        "owner": owner_of(name),
        "status": get("status"),
        "execution_status": get("execution_status"),
        "failure_reason": get("failure_reason"),
        "agent_id": get("agent_id") or None,  # a log-source config has no agent
        "agent_name": get("agent_name"),
        "matched_agent": None,
        "detailed": True,
        "evaluators": list(get("evaluators") or []),
        "sampling_percentage": get("sampling_percentage"),
        "session_timeout_minutes": get("session_timeout_minutes"),
        "filter_count": len(filters),
        "filters": filters,
        "data_source": {
            "log_groups": log_groups,
            "service_name": get("service_name"),
        },
        "mode": get("mode") or ("insights" if get("insights") else "scores"),
        "insights": list(get("insights") or []),
        "clustering_frequencies": list(get("clustering_frequencies") or []),
        # P3.2 (additive): the judge snapshotted at create; null on older rows (those read
        # the `judge` model role at each dispatch)
        "judge_model_id": get("judge_model_id"),
        "judge_inference": get("judge_inference"),
        "execution_role_arn": None,
        "results_log_group": results_log_group(config_id) if config_id else None,
        "duplicate_enabled": False,
        "created_at": _iso(get("created_at")),
        "updated_at": _iso(get("updated_at")),
        # console-created configs only: experiment / canary-owned ones record no creator
        "created_by": get("created_by"),
        "created_by_name": get("created_by_name"),
        # N17-obs445 (additive): an experiment-owned config's owning canary / experiment
        # (filled by the reads that hold a session — ``_out``); null on agent-owned configs
        "owner_run": None,
    }


def _judge_snapshot() -> dict[str, Any]:
    """P3.2 (N17-obs350): the `judge` model role NOW and its inference config, pinned on a
    new config so every dispatch of the series uses the same judge."""
    from app.evaluation.judge_budget import judge_inference

    judge = judge_inference()
    return {"judge_model_id": judge["model_id"], "judge_inference": judge}


def _rows(db: Session, workspace_id: str) -> list[OnlineEvalConfig]:
    return list(db.scalars(
        select(OnlineEvalConfig).where(OnlineEvalConfig.workspace_id == workspace_id)
    ).all())


def _row(db: Session, workspace_id: str, config_id: str) -> OnlineEvalConfig:
    row = db.scalars(
        select(OnlineEvalConfig).where(
            OnlineEvalConfig.workspace_id == workspace_id,
            OnlineEvalConfig.config_id == config_id,
        )
    ).first()
    if row is None:
        raise NotFoundError(
            "online_eval.not_found", f"online evaluation config {config_id} not found"
        )
    return row


def _not_found(config_id: str) -> NotFoundError:
    return NotFoundError(
        "online_eval.not_found", f"online evaluation config {config_id} not found"
    )


def _parent_creators(db: Session, workspace_id: str, name: str) -> list[str | None]:
    """The creators of the canary that owns an arm config — its name carries the
    owner's id prefix (``can_<id8>_…``; legacy ``exp_<id8>_…`` arms of the retired
    configuration experiment resolve to nobody, N13-obs2); ``[]`` for an agent-owned
    config or an owner row that is gone."""
    if owner_of(name) != "experiment":
        return []
    from app.optimization.models import RuntimeCanary

    tag = ""
    for prefix in EXPERIMENT_PREFIXES:
        if str(name).startswith(prefix):
            tag = str(name)[len(prefix):].split("_", 1)[0]
            break
    if not tag:
        return []
    return [
        row.created_by
        for row in db.scalars(
            select(RuntimeCanary).where(
                RuntimeCanary.workspace_id == workspace_id, RuntimeCanary.id.like(f"{tag}%")
            )
        )
    ]


def config_visible(db: Session, viewer: Viewer, row: OnlineEvalConfig) -> bool:
    """Workspace isolation: an agent-owned config follows its own creator; an
    experiment / canary-owned arm follows its OWNER's creator (the arm records none)."""
    if not viewer.filters:
        return True
    parents = _parent_creators(db, row.workspace_id, row.name)
    if parents:
        return all(visibility.sees(viewer, db, created_by) for created_by in parents)
    return visibility.sees(viewer, db, row.created_by)


def _visible_row(db: Session, ws: Any, config_id: str, viewer: Viewer) -> OnlineEvalConfig:
    """``_row`` + the isolation check: a hidden config reads exactly like a missing one."""
    row = _row(db, _ws_id(ws), config_id)
    if not config_visible(db, viewer, row):
        raise _not_found(config_id)
    return row


def _require_creator(row: OnlineEvalConfig, identity: Identity | None, action: str) -> None:
    """N17-obs426: creator-or-admin on every config mutation (``ownership``,
    users-and-ownership §8) — a member may not pause / resume / edit / delete another
    member's or an administrator's config (a NULL creator's is an administrator's). Runs
    after the visibility lookup (a hidden config stays a 404) and before any change.
    ``identity=None`` is an in-process caller (tests, the scheduler), never a route."""
    if identity is None:
        return
    ownership.require_creator_or_admin(
        row, identity, "online_config.forbidden",
        message=f"only the config's creator or an administrator may {action} it",
        detail={"config_id": row.config_id, "action": action},
    )


def _require_run_creator(row: OnlineEvalConfig, run: Any, identity: Identity | None,
                         action: str) -> None:
    """N17-obs445: creator-or-admin on an arm config, against its owning run's creator."""
    if identity is None:
        return
    ownership.require_creator_or_admin(
        run, identity, "online_config.forbidden",
        message=f"only the canary's creator or an administrator may {action} its arm",
        detail={"config_id": row.config_id, "action": action, "owner_run": run.id},
    )


def _require_owner(row: OnlineEvalConfig, allowed: Sequence[Owner], action: str) -> None:
    owner = owner_of(row.name)
    if owner not in allowed:
        raise AppError(
            "online_eval.read_only",
            f"{action} is not available for {owner}-owned configs",
            {"owner": owner, "action": action},
            status_code=403,
        )


# N17-obs445: the owning run's statuses after which its arm configs may be paused / resumed
TERMINAL_OWNER_STATUSES = {
    "canary": frozenset({"completed", "rolled_back", "cleaned"}),
    # the retired configuration experiment's own terminal states (models.Experiment)
    "experiment": frozenset({"promoted", "cleaned", "failed"}),
}


def _owning_run(db: Session, row: OnlineEvalConfig) -> tuple[str, Any] | None:
    """``(kind, row)`` of the canary / experiment an arm config belongs to (its name's
    ``can_<id8>_`` / ``exp_<id8>_`` tag); ``None`` for an agent-owned config, or when no
    single run of the workspace carries the tag."""
    from app.optimization.models import Experiment, RuntimeCanary

    name = str(row.name or "")
    if owner_of(name) != "experiment":
        return None
    kind, model = ("canary", RuntimeCanary) if name.startswith("can_") else (
        "experiment", Experiment)
    tag = name.split("_", 2)[1] if name.count("_") >= 2 else ""
    if not tag:
        return None
    runs = list(db.scalars(select(model).where(
        model.workspace_id == row.workspace_id, model.id.startswith(tag))).all())
    return (kind, runs[0]) if len(runs) == 1 else None


def _owner_run_out(db: Session, row: OnlineEvalConfig) -> dict[str, Any] | None:
    owning = _owning_run(db, row)
    if owning is None:
        return None
    kind, run = owning
    return {"kind": kind, "id": run.id, "status": run.status,
            "terminal": run.status in TERMINAL_OWNER_STATUSES[kind],
            "created_by": run.created_by, "created_by_name": run.created_by_name}


def _out(db: Session, row: OnlineEvalConfig) -> dict[str, Any]:
    out = _normalize(row)
    if out["owner"] == "experiment":
        out["owner_run"] = _owner_run_out(db, row)
    return out


def _auto_description(agent_name: str | None, name: str) -> str:
    return (f"Launchpad online evaluation · {agent_name or name}")[:200]


def _delete_samples(db: Session, config_ids: Iterable[str]) -> None:
    ids = list(config_ids)
    if not ids:
        return
    for sample in db.scalars(select(OnlineSample).where(OnlineSample.config_id.in_(ids))):
        db.delete(sample)


# ─── read side ──────────────────────────────────────────────────────────────


def _watched(config: dict[str, Any]) -> str:
    """What a config judges: its agent, or (a log-source config) its service."""
    return config["agent_id"] or f"service:{config['data_source']['service_name']}"


def list_configs(db: Session, ws: Any, *, viewer: Viewer = Viewer.ALL) -> list[dict[str, Any]]:
    """Every config of the workspace ``viewer`` may see, newest-updated first, with the
    duplicate flag: two ENABLED agent-owned configs on one agent judge the same
    sessions twice."""
    out = [_out(db, row) for row in _rows(db, _ws_id(ws)) if config_visible(db, viewer, row)]
    enabled_by_agent: dict[str, int] = {}
    for c in out:
        if c["owner"] == "agent" and c["execution_status"] == "ENABLED":
            enabled_by_agent[_watched(c)] = enabled_by_agent.get(_watched(c), 0) + 1
    for c in out:
        c["duplicate_enabled"] = (
            c["owner"] == "agent"
            and c["execution_status"] == "ENABLED"
            and enabled_by_agent.get(_watched(c), 0) > 1
        )
    out.sort(key=lambda c: c.get("updated_at") or c.get("created_at") or "", reverse=True)
    return out


def get_config(
    db: Session, ws: Any, config_id: str, *, viewer: Viewer = Viewer.ALL
) -> dict[str, Any]:
    return _out(db, _visible_row(db, ws, config_id, viewer))


# ─── write side ─────────────────────────────────────────────────────────────


def _log_group_exists(ctx: WorkspaceContext, log_group: str, logs: Any = None) -> bool:
    """A never-invoked agent has no runtime log group yet — the scheduler would scan
    nothing. Unknown (permission / transient error) reads as present: the config is
    then validated by the first scan instead."""
    try:
        resp = (logs or _logs_client(ctx)).describe_log_groups(
            logGroupNamePrefix=log_group, limit=5
        )
    except Exception:  # noqa: BLE001 — cannot tell; let the scheduler find out
        return True
    return any(g.get("logGroupName") == log_group for g in resp.get("logGroups", []))


def create_config(
    db: Session,
    ws: Any,
    agent: Agent | None,
    *,
    log_source: dict[str, Any] | None = None,
    evaluators: Sequence[str] | None,
    sampling_percentage: float,
    session_timeout_minutes: int,
    filters: Sequence[dict[str, Any]] | None,
    description: str | None,
    enable_on_create: bool,
    mode: Mode = "scores",
    insights: Sequence[str] | None = None,
    clustering_frequencies: Sequence[str] | None = None,
    logs: Any = None,
    creator: creators.Creator | None = None,
) -> dict[str, Any]:
    ctx = _ctx(ws)
    log_groups: list[str] | None = None
    if agent is None:
        # N17-obs337: a CloudWatch log source (BYOC / external agent) — no platform agent
        if not log_source:
            raise AppError("online_eval.target_required",
                           "exactly one target required: agent_id or log_source",
                           status_code=422)
        service_name = str(log_source["service_name"])
        log_groups = list(dict.fromkeys(str(g) for g in log_source["log_group_names"]))
        missing = [g for g in log_groups if not _log_group_exists(ctx, g, logs)]
        if missing:
            raise AppError(
                "online_eval.no_telemetry",
                "these log groups do not exist — check the names and the workspace region",
                {"log_groups": missing},
                status_code=400,
            )
        log_group = log_groups[0]
    else:
        service_name, log_group = resolve_telemetry(agent, ctx)
        if not _log_group_exists(ctx, log_group, logs):
            raise AppError(
                "online_eval.no_telemetry",
                "this agent has no telemetry log group yet — run at least one "
                "chat/invoke session first, then create the online evaluation",
                {"log_group": log_group},
                status_code=400,
            )
    agent_id = agent.id if agent is not None else ""
    agent_name = agent.name if agent is not None else service_name
    chosen: list[str] = []
    chosen_insights: list[str] = []
    freqs: list[str] = []
    if mode == "insights":
        if evaluators:
            raise _mode_conflict("insights mode takes no evaluators")
        chosen_insights, freqs = validate_insights(insights, clustering_frequencies)
    else:
        if insights or clustering_frequencies:
            raise _mode_conflict("scores mode takes no insights / clustering_frequencies")
        chosen = normalize_online_evaluators(
            evaluators, db, code_prefix="online_eval", workspace_id=_ws_id(ws)
        )
    clean_filters = validate_filters(filters)
    name = generate_name(agent_name)
    config_id = new_config_id(name)
    row = OnlineEvalConfig(
        workspace_id=_ws_id(ws),
        agent_id=agent_id,
        agent_name=agent_name[:64],
        config_id=config_id,
        config_arn=ctx.arn("launchpad", f"online-evaluation-config/{config_id}"),
        name=name,
        service_name=service_name,
        log_group=log_group,
        log_groups=log_groups,
        description=(description or "").strip()[:200] or _auto_description(agent_name, name),
        mode=mode,
        evaluators=chosen,
        insights=chosen_insights,
        clustering_frequencies=freqs,
        sampling_percentage=float(sampling_percentage),
        session_timeout_minutes=int(session_timeout_minutes),
        filters=clean_filters,
        execution_status="ENABLED" if enable_on_create else "DISABLED",
        status="ACTIVE",
        **_judge_snapshot(),
    )
    creators.stamp(row, creator)
    db.add(row)
    db.commit()
    db.refresh(row)
    return _normalize(row)


def patch_config(
    db: Session, ws: Any, config_id: str, patch: dict[str, Any], *, viewer: Viewer = Viewer.ALL,
    identity: Identity | None = None,
) -> dict[str, Any]:
    """Update description / evaluators / sampling / timeout / filters on an agent config.
    Mode is immutable: each mode accepts only its own analysis fields."""
    row = _visible_row(db, ws, config_id, viewer)
    _require_owner(row, ("agent",), "edit")
    _require_creator(row, identity, "edit")
    changed = False
    if patch.get("description") is not None:
        text = str(patch["description"]).strip()[:200]
        row.description = text or _auto_description(row.agent_name, row.name)
        changed = True
    if mode_of(row) == "insights":
        if patch.get("evaluators") is not None:
            raise _mode_conflict("this config runs insights — evaluators cannot be set")
        if patch.get("insights") is not None or patch.get("clustering_frequencies") is not None:
            ids, freqs = validate_insights(
                row.insights if patch.get("insights") is None else patch["insights"],
                row.clustering_frequencies
                if patch.get("clustering_frequencies") is None
                else patch["clustering_frequencies"],
            )
            row.insights, row.clustering_frequencies = ids, freqs
            changed = True
    else:
        if patch.get("insights") is not None or patch.get("clustering_frequencies") is not None:
            raise _mode_conflict("this config runs evaluators — insights cannot be set")
        if patch.get("evaluators") is not None:
            row.evaluators = normalize_online_evaluators(
                patch["evaluators"], db, code_prefix="online_eval", workspace_id=_ws_id(ws)
            )
            changed = True
    if patch.get("sampling_percentage") is not None:
        row.sampling_percentage = float(patch["sampling_percentage"])
        changed = True
    if patch.get("session_timeout_minutes") is not None:
        row.session_timeout_minutes = int(patch["session_timeout_minutes"])
        changed = True
    if patch.get("filters") is not None:
        row.filters = validate_filters(patch["filters"])
        changed = True
    if changed:
        row.updated_at = datetime.now(UTC)
        db.commit()
        db.refresh(row)
    return _normalize(row)


def set_execution_status(
    db: Session, ws: Any, config_id: str, *, enabled: bool, viewer: Viewer = Viewer.ALL,
    identity: Identity | None = None,
) -> dict[str, Any]:
    row = _visible_row(db, ws, config_id, viewer)
    action = "resume" if enabled else "pause"
    owning = _owning_run(db, row)
    if owning is not None and owning[1].status in TERMINAL_OWNER_STATUSES[owning[0]]:
        # N17-obs445: a finished canary's / experiment's arm may be paused or resumed —
        # creator-or-admin against the owning run's creator (an arm records none)
        _require_run_creator(row, owning[1], identity, action)
    else:
        _require_owner(row, ("agent",), action)
        _require_creator(row, identity, action)
    if enabled and row.execution_status == "DISABLED":
        # N17-obs444: a resume samples from now on — nothing active while paused is back-filled
        row.sampling_since = datetime.now(UTC)
    row.execution_status = "ENABLED" if enabled else "DISABLED"
    if enabled:
        # a FAILED config (worker missing, bad filter) gets another chance — the
        # scheduler re-validates on its next tick
        row.status, row.failure_reason = "ACTIVE", None
    row.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(row)
    return _out(db, row)


def delete_config(
    db: Session, ws: Any, config_id: str, *, viewer: Viewer = Viewer.ALL,
    identity: Identity | None = None,
) -> dict[str, Any]:
    """Drop the row and its samples; the results stay (AWS left the log group too)."""
    row = _visible_row(db, ws, config_id, viewer)
    _require_owner(row, ("agent",), "delete")
    _require_creator(row, identity, "delete")
    _delete_samples(db, [config_id])
    db.delete(row)
    db.commit()
    return {
        "config_id": config_id,
        "deleted": True,
        "results_log_group": results_log_group(config_id),
    }


def mark_failed(db: Session, row: OnlineEvalConfig, reason: str) -> None:
    """The scheduler's verdict on a config it cannot serve (validation error, no worker)."""
    row.status, row.failure_reason = "FAILED", reason[:1000]
    row.updated_at = datetime.now(UTC)
    db.commit()


# ─── experiment / canary arms (frozen E3 contract, plan §C.1) ───────────────


def _owner_prefixes(owner_id: str) -> tuple[str, ...]:
    tag = str(owner_id)[:8]
    return tuple(f"{prefix}{tag}_" for prefix in EXPERIMENT_PREFIXES)


def create_for_owner(
    ws: Any,
    agent: Agent,
    *,
    name: str,
    evaluators: Sequence[str] | None,
    log_group: str,
    service_name: str,
    sampling: float,
    timeout: int,
    db: Session | None = None,
) -> OnlineEvalConfig:
    """An ``experiment``-owned config for one arm — ``name`` must carry the owner prefix
    (``exp_<id8>_…`` / ``can_<id8>_…``); the same name twice returns the existing row."""
    if not str(name).startswith(EXPERIMENT_PREFIXES):
        raise ValueError(f"an owner config name must start with one of {EXPERIMENT_PREFIXES}")
    ctx = _ctx(ws)
    with _session(db) as session:
        existing = session.scalars(
            select(OnlineEvalConfig).where(
                OnlineEvalConfig.workspace_id == _ws_id(ws), OnlineEvalConfig.name == name
            )
        ).first()
        if existing is not None:
            session.refresh(existing)
            if db is None:
                session.expunge(existing)
            return existing
        chosen = normalize_online_evaluators(
            evaluators, session, code_prefix="experiment", workspace_id=_ws_id(ws)
        )
        config_id = new_config_id(name)
        row = OnlineEvalConfig(
            workspace_id=_ws_id(ws), agent_id=agent.id, agent_name=agent.name,
            config_id=config_id,
            config_arn=ctx.arn("launchpad", f"online-evaluation-config/{config_id}"),
            name=name, service_name=service_name, log_group=log_group,
            description=f"Launchpad experiment online eval · {name}"[:200],
            mode="scores", evaluators=chosen, insights=[], clustering_frequencies=[],
            sampling_percentage=float(sampling), session_timeout_minutes=int(timeout),
            filters=[], execution_status="ENABLED", status="ACTIVE",
            **_judge_snapshot(),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        if db is None:
            session.expunge(row)
        return row


def config_ids_for_owner(ws: Any, owner_id: str, *, db: Session | None = None) -> list[str]:
    """The arm configs of an experiment / canary that still exist (sorted ids) — the
    readback the architect's conversation purge trusts instead of ``act_cleanup``'s
    return value (``assistant.purge``)."""
    prefixes = _owner_prefixes(owner_id)
    with _session(db) as session:
        return sorted(row.config_id for row in _rows(session, _ws_id(ws))
                      if str(row.name).startswith(prefixes))


def disable_for_owner(ws: Any, owner_id: str, *, db: Session | None = None) -> list[str]:
    """Set every ENABLED arm config of an experiment / canary to DISABLED, keeping the rows
    (N17-obs442: a finished experiment stops judging traffic); returns the ids it disabled.
    On the caller's session it does not commit — the caller's transition does."""
    prefixes = _owner_prefixes(owner_id)
    disabled: list[str] = []
    with _session(db) as session:
        now = datetime.now(UTC)
        for row in _rows(session, _ws_id(ws)):
            if str(row.name).startswith(prefixes) and row.execution_status == "ENABLED":
                row.execution_status, row.updated_at = "DISABLED", now
                disabled.append(row.config_id)
        if db is None:
            session.commit()
    return disabled


def delete_for_owner(ws: Any, owner_id: str, *, db: Session | None = None) -> list[str]:
    """Delete every arm config of an experiment / canary (+ their samples); returns the ids."""
    prefixes = _owner_prefixes(owner_id)
    deleted: list[str] = []
    with _session(db) as session:
        for row in _rows(session, _ws_id(ws)):
            if str(row.name).startswith(prefixes):
                deleted.append(row.config_id)
                session.delete(row)
        _delete_samples(session, deleted)
        session.commit()
    return deleted


def results_for_owner(
    ws: Any, owner_id: str, since: datetime, arm_map: dict[str, str], *,
    db: Session | None = None,
) -> dict[str, Any]:
    """Scored online records of an owner's arm configs since ``since``, grouped by
    evaluator and arm (``arm_map``: session id → ``C`` | ``T1``) — the input of W-E3's
    ``routing.ab_results`` (Welch over the two lists)."""
    prefixes = _owner_prefixes(owner_id)
    arms = sorted(set(arm_map.values())) or ["C", "T1"]
    with _session(db) as session:
        configs = sorted(
            row.config_id for row in _rows(session, _ws_id(ws))
            if str(row.name).startswith(prefixes)
        )
        rows = _result_rows(session, _ws_id(ws), configs, since) if configs else []
    by_evaluator: dict[str, dict[str, list[float]]] = {}
    sessions: dict[str, set[str]] = {arm: set() for arm in arms}
    unassigned = 0
    for rec in rows:
        if rec.score is None:
            continue
        arm = arm_map.get(rec.session_id)
        if arm is None:
            unassigned += 1
            continue
        slots = by_evaluator.setdefault(rec.evaluator_id, {a: [] for a in arms})
        slots.setdefault(arm, []).append(float(rec.score))
        sessions.setdefault(arm, set()).add(rec.session_id)
    return {
        "configs": configs,
        "by_evaluator": by_evaluator,
        "sessions": {arm: len(ids) for arm, ids in sessions.items()},
        "records": sum(len(v) for slots in by_evaluator.values() for v in slots.values()),
        "unassigned": unassigned,
    }


# ─── results (aggregated over eval_results) ─────────────────────────────────


def bucket_seconds(range_key: str) -> int:
    return BUCKET_SECONDS[range_key]


def bucket_label(value: datetime, range_key: str) -> str:
    width = bucket_seconds(range_key)
    stamp = int(_aware(value).timestamp())
    return _ts(datetime.fromtimestamp(stamp - stamp % width, UTC))


def _result_rows(
    db: Session, workspace_id: str, config_ids: Sequence[str], since: datetime | None,
    *, session_id: str | None = None, scored_only: bool = False,
) -> list[EvalResult]:
    query = select(EvalResult).where(
        EvalResult.workspace_id == workspace_id,
        EvalResult.source == RESULTS_SOURCE,
    )
    if config_ids:
        query = query.where(EvalResult.config_id.in_(list(config_ids)))
    if since is not None:
        query = query.where(EvalResult.created_at >= since)
    if session_id is not None:
        query = query.where(EvalResult.session_id == session_id)
    if scored_only:
        query = query.where(EvalResult.score.is_not(None))
    return list(db.scalars(query.order_by(EvalResult.created_at.asc())).all())


def _error_text(rec: EvalResult) -> str | None:
    if not rec.error_message:
        return None
    return f"{rec.error_type or 'error'}: {rec.error_message}"


def results(
    db: Session, ws: Any, config_id: str, range_key: str, *, viewer: Viewer = Viewer.ALL
) -> dict[str, Any]:
    """The config's results in the window: per-evaluator summary (+ label histogram),
    mean series per bucket, the newest records and the error counter."""
    if range_key not in RANGE_HOURS:
        raise AppError("online_eval.bad_range", f"range must be one of {sorted(RANGE_HOURS)}",
                       status_code=422)
    _visible_row(db, ws, config_id, viewer)  # a hidden config's results read as missing
    since = datetime.now(UTC) - timedelta(hours=RANGE_HOURS[range_key])
    rows = _result_rows(db, _ws_id(ws), [config_id], since)
    summary: dict[str, dict[str, Any]] = {}
    series: dict[str, dict[str, list[float]]] = {}
    for rec in rows:
        if rec.score is None:
            continue
        acc = summary.setdefault(rec.evaluator_id, {
            "evaluator_id": rec.evaluator_id, "level": rec.level, "sum": 0.0, "count": 0,
            "sessions": set(), "labels": {},
        })
        acc["sum"] += float(rec.score)
        acc["count"] += 1
        acc["sessions"].add(rec.session_id)
        if rec.label:
            acc["labels"][rec.label] = acc["labels"].get(rec.label, 0) + 1
        bucket = bucket_label(rec.created_at, range_key)
        series.setdefault(rec.evaluator_id, {}).setdefault(bucket, []).append(float(rec.score))
    evaluators = [
        {
            "evaluator_id": acc["evaluator_id"],
            "level": acc["level"],
            "mean": round(acc["sum"] / acc["count"], 4),
            "count": acc["count"],
            "sessions": len(acc["sessions"]),
            "labels": acc["labels"],
        }
        for acc in sorted(summary.values(), key=lambda a: a["evaluator_id"])
    ]
    series_out = {
        evaluator: [
            {"bucket": bucket, "mean": round(sum(vals) / len(vals), 4), "count": len(vals)}
            for bucket, vals in sorted(buckets.items())
        ]
        for evaluator, buckets in series.items()
    }
    recent = [
        {
            "time": _ts(rec.created_at),
            "session_id": rec.session_id,
            "trace_id": rec.trace_id,
            # N17-obs317 (additive): the judged call and the turn's reconciliation
            "span_id": rec.span_id,
            "tool_name": rec.tool_name,
            "extraction_status": rec.extraction_status,
            "evaluator_id": rec.evaluator_id,
            "level": rec.level,
            "score": None if rec.score is None else float(rec.score),
            "label": rec.label,
            "explanation": rec.explanation,
            "error": _error_text(rec),
        }
        for rec in sorted(rows, key=lambda r: _aware(r.created_at), reverse=True)[:RECENT_LIMIT]
    ]
    errored = [rec for rec in rows if rec.error_message]
    in_window = {rec.session_id for rec in rows}
    checks = {
        sample.session_id: sample.extraction_check
        for sample in db.scalars(select(OnlineSample).where(
            OnlineSample.config_id == config_id, OnlineSample.extraction_check.is_not(None)))
        if sample.session_id in in_window
    }
    return {
        "range": range_key,
        "log_group": results_log_group(config_id),
        # eval accuracy item 1 (additive): the extraction reconciliation summary of the
        # sessions scored in the window (extraction_check.summarise) — null when none was
        # checked
        "extraction_check": extraction_check.summarise(checks) if checks else None,
        "evaluators": evaluators,
        "series": series_out,
        "recent": recent,
        "errors": {
            "count": len(errored),
            "first_message": errored[0].error_message if errored else None,
        },
    }


# ─── insights reports (on-demand insights runs over the sampled sessions) ───


def _console_runs(
    db: Session, workspace_id: str, config_id: str, *, viewer: Viewer = Viewer.ALL
) -> list[EvalRun]:
    """The console-started reports of one config — only those ``viewer`` may see
    (workspace isolation on the run's creator)."""
    return list(db.scalars(
        select(EvalRun).where(EvalRun.workspace_id == workspace_id,
                              EvalRun.dataset_name == f"online:{config_id}",
                              visibility.clause(viewer, EvalRun.created_by))
        .order_by(EvalRun.created_at.desc())
    ).all())


def _run_sessions(run: EvalRun) -> dict[str, int]:
    total = len(run.session_ids or [])
    completed = total if run.status == "completed" else 0
    failed = total if run.status in ("failed", "stopped") else 0
    in_progress = total - completed - failed
    return {"completed": completed, "failed": failed, "in_progress": in_progress, "total": total}


def _report_row(run: EvalRun) -> dict[str, Any]:
    return {
        "batch_id": run.batch_eval_id,
        "name": run.name or f"run_{run.id[:8]}",
        "status": run.status.upper(),
        "run_status": run.status,
        "created_at": _iso(run.created_at),
        "updated_at": _iso(run.updated_at),
        "insights": list(run.evaluators or []),
        "sessions": _run_sessions(run),
        "origin": "console",
        "run_id": run.id,
        "error": run.error,
        # console-started reports only
        **creators.fields(run),
    }


def reports(
    db: Session, ws: Any, config_id: str, *, viewer: Viewer = Viewer.ALL
) -> dict[str, Any]:
    """Every report of one config, newest first. ``aws_unavailable`` stays in the
    shape (always ``False``: the list is the ledger, nothing remote is consulted)."""
    row = _visible_row(db, ws, config_id, viewer)
    runs = _console_runs(db, _ws_id(ws), config_id, viewer=viewer)
    return {"config_id": config_id, "mode": mode_of(row),
            "reports": [_report_row(run) for run in runs], "aws_unavailable": False}


def report_detail(
    db: Session, ws: Any, config_id: str, batch_id: str, *, viewer: Viewer = Viewer.ALL
) -> dict[str, Any]:
    _visible_row(db, ws, config_id, viewer)
    run = next(
        (r for r in _console_runs(db, _ws_id(ws), config_id, viewer=viewer)
         if batch_id in (r.batch_eval_id, r.id)),
        None,
    )
    if run is None:
        raise NotFoundError("online_eval.report_not_found",
                            f"report {batch_id} is not a report of {config_id}")
    return {
        "batch_id": run.batch_eval_id or run.id,
        "name": run.name or f"run_{run.id[:8]}",
        "status": run.status.upper(),
        "created_at": _iso(run.created_at),
        "updated_at": _iso(run.updated_at),
        "time_range": {},
        "sessions": _run_sessions(run),
        "insights": dict(run.insights or {}),
        "error_details": [run.error] if run.error else [],
    }


def sampled_sessions(
    db: Session, config_id: str, since: datetime | None = None
) -> list[str]:
    """Session ids the scheduler sampled for the config (newest first), optionally since."""
    query = select(OnlineSample).where(OnlineSample.config_id == config_id)
    if since is not None:
        query = query.where(OnlineSample.sampled_at >= since)
    rows = db.scalars(query.order_by(OnlineSample.sampled_at.desc())).all()
    return list(dict.fromkeys(r.session_id for r in rows))


def start_report(
    db: Session, ws: Any, config_id: str, range_key: str, *,
    creator: creators.Creator | None = None, viewer: Viewer = Viewer.ALL,
) -> dict[str, Any]:
    """RUN REPORT NOW: an insights run over the sessions this config sampled in the
    window, through the bounded run queue (W-E1's ``submit_run``)."""
    if range_key not in REPORT_RANGES:
        raise AppError("online_eval.bad_range", f"range must be one of {sorted(REPORT_RANGES)}",
                       status_code=422)
    row = _visible_row(db, ws, config_id, viewer)
    _require_owner(row, ("agent",), "run a report for")
    if mode_of(row) != "insights":
        raise _mode_conflict("reports exist only for insights-mode configs")
    agent = db.get(Agent, row.agent_id) if row.agent_id else None
    if agent is None or agent.workspace_id != _ws_id(ws):
        raise NotFoundError("online_eval.not_found", f"agent of {config_id} is gone")
    since = datetime.now(UTC) - timedelta(hours=REPORT_RANGES[range_key])
    sessions = sampled_sessions(db, config_id, since)
    if not sessions:
        raise AppError(
            "online_eval.no_sampled_sessions",
            f"no session was sampled for this config in the last {range_key} — the report "
            "needs sessions the scheduler has picked (sampling + session timeout apply)",
            {"range": range_key}, status_code=409,
        )
    run = eval_service.submit_run(
        agent=agent, workspace=_ctx(ws), dataset_items=[], dataset_id=None,
        dataset_name=f"online:{config_id}", evaluators=list(row.insights),
        mode="insights", insights=list(row.insights), session_ids=sessions,
        creator=creator,
    )
    return {
        "run_id": run.id, "status": run.status, "queue_position": run.queue_position,
        "range": range_key, "config_id": config_id,
    }


# ─── cross-surface reads (Observability session detail + Overview tile) ──────


def _agent_rows(db: Session, workspace_id: str) -> dict[str, OnlineEvalConfig]:
    return {r.config_id: r for r in _rows(db, workspace_id)}


def session_online_scores(
    db: Session,
    workspace: WorkspaceContext,
    session_id: str,
    hours: int,
    run_queries: RunQueries | None = None,
    logs: Any = None,
    control: Any = None,
) -> dict[str, Any]:
    """Online scores for one session — an additive, fail-soft block on the Observability
    session detail. A ledger failure here must never take the traces down with it.
    ``configs_exist`` lets the UI hide the panel on workspaces that never created a
    config (and show a "not judged yet" state on those that did). The legacy query
    keywords are accepted and ignored."""
    try:
        ledger = _agent_rows(db, workspace.id)
        since = datetime.now(UTC) - timedelta(hours=hours)
        rows = _result_rows(db, workspace.id, [], since, session_id=session_id)
    except Exception:  # noqa: BLE001 — fail-soft by contract
        return {"configs": [], "total": 0, "unavailable": True, "configs_exist": False}
    grouped: dict[str, dict[str, Any]] = {}
    newest_first = sorted(rows, key=lambda r: _aware(r.created_at), reverse=True)
    for rec in newest_first[:SESSION_SCORES_LIMIT]:
        config_id = rec.config_id or ""
        if not config_id:
            continue
        block = grouped.get(config_id)
        if block is None:
            row = ledger.get(config_id)
            block = grouped[config_id] = {
                "config_id": config_id,
                "config_name": row.name if row else None,
                "owner": owner_of(row.name) if row else "agent",
                "agent": {"id": row.agent_id or None, "name": row.agent_name} if row else None,
                "records": [],
            }
        block["records"].append({
            "time": _ts(rec.created_at),
            "evaluator_id": rec.evaluator_id,
            "level": rec.level,
            "score": None if rec.score is None else float(rec.score),
            "label": rec.label,
            "explanation": rec.explanation,
            "trace_id": rec.trace_id,
        })
    order = {"agent": 0, "experiment": 1}
    # agent-owned first, live configs before a deleted config's leftover scores, then by id
    configs = sorted(
        grouped.values(),
        key=lambda b: (order.get(b["owner"], 2), b["config_id"] not in ledger, b["config_id"]),
    )
    return {
        "configs": configs,
        "total": sum(len(b["records"]) for b in configs),
        "unavailable": False,
        "configs_exist": bool(ledger),
    }


def _empty_quality(configs: int = 0) -> dict[str, Any]:
    return {"range": QUALITY_RANGE, "mean": None, "scores": 0, "sessions": 0,
            "agents": 0, "configs": configs, "evaluators": []}


def online_quality(
    db: Session,
    workspace: WorkspaceContext,
    run_queries: RunQueries | None = None,
    logs: Any = None,
    *,
    viewer: Viewer = Viewer.ALL,
) -> dict[str, Any]:
    """24 h online quality over the workspace's agent-owned configs: the polarity-
    normalised, count-weighted mean of every score — a Refusal mean of 0.1 contributes
    0.9, so the tile reads "higher is better" regardless of the evaluator mix.
    ``evaluators`` keeps the RAW per-evaluator means (what the online page shows) plus
    the polarity applied; ``configs`` counts the agent-owned configs (so "configured,
    nothing judged yet" differs from "no config at all"); ``agents`` counts agents WITH
    scores. No agent config → the empty payload without any ledger scan."""
    # the viewer's configs only (workspace isolation): a member's tile covers their view
    ledger = {
        cid: row for cid, row in _agent_rows(db, workspace.id).items()
        if owner_of(row.name) == "agent" and config_visible(db, viewer, row)
    }
    if not ledger:
        return _empty_quality()
    since = datetime.now(UTC) - timedelta(hours=RANGE_HOURS[QUALITY_RANGE])
    rows = _result_rows(db, workspace.id, sorted(ledger), since, scored_only=True)
    if not rows:
        return _empty_quality(len(ledger))
    weighted = 0.0
    per_eval: dict[str, dict[str, float]] = {}
    sessions: set[str] = set()
    agents: set[str] = set()
    for rec in rows:
        if rec.score is None or rec.config_id not in ledger:
            continue  # scored_only + the config filter already guarantee this
        polarity = evaluator_polarity(rec.evaluator_id)
        score = float(rec.score)
        weighted += 1 - score if polarity < 0 else score
        sessions.add(rec.session_id)
        config = ledger[rec.config_id]
        agents.add(config.agent_id or f"service:{config.service_name}")
        acc = per_eval.setdefault(rec.evaluator_id, {"sum": 0.0, "count": 0, "polarity": polarity})
        acc["sum"] += score
        acc["count"] += 1
    return {
        "range": QUALITY_RANGE,
        "mean": round(weighted / len(rows), 3),
        "scores": len(rows),
        "sessions": len(sessions),
        "agents": len(agents),
        "configs": len(ledger),
        "evaluators": sorted(
            (
                {"evaluator_id": e, "mean": round(a["sum"] / a["count"], 3),
                 "count": int(a["count"]), "polarity": int(a["polarity"])}
                for e, a in per_eval.items()
            ),
            key=lambda e: e["evaluator_id"],
        ),
    }
