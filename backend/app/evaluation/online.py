"""Per-agent online evaluation: continuous, sampled scoring of live sessions.

One AWS ``OnlineEvaluationConfig`` per (agent, evaluator set). The ledger keeps
identifiers only (:class:`OnlineEvalConfig`); every status/rule/evaluator field
is read back from the control plane. The list surface shows **every** config in
the workspace account, classified by owner:

- ``agent`` — created here for an agent (ledger row): full control.
- ``experiment`` — ``exp_*`` / ``can_*`` configs owned by the experiment and canary
  flows (their cleanup deletes them): read-only; ``parent`` names the owning record
  (resolved from the ledger by the name's id prefix) and the arm it scores.
- ``external`` — anything else (AWS console, CLI, quick start): status and results
  visible, pause/resume/delete allowed, no edit (the owning agent is unknown).

Live-verified AWS semantics this module leans on (2026-09-02):

- ``Update`` keeps omitted top-level fields but replaces ``rule`` wholesale —
  :func:`patch_config` always sends the merged, complete rule.
- ``Create`` validates that the data-source log groups exist — a never-invoked
  agent is refused with ``online_eval.no_telemetry``.
- ``Delete`` works while ENABLED; the results log group survives deletion.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.core.project_scope import current_project, project_context
from app.evaluation import agentcore_eval as ac
from app.evaluation import service as eval_service
from app.evaluation.models import EvalRun, OnlineEvalConfig
from app.evaluation.online_evaluators import (
    normalize_online_evaluators,
    refuse_hidden_evaluators,
)
from app.evaluation.service import EVAL_SUPPORTED_METHODS, resolve_telemetry
from app.models.ledger import Agent
from app.optimization.models import Experiment, RuntimeCanary
from app.optimization.service import experiment_status
from app.routers.workspaces import WorkspaceScope
from app.services import creators, visibility
from app.services.agentcore.client import control_client, data_client
from app.services.creators import Creator
from app.services.observability import BIN_BY_RANGE, RANGE_HOURS, run_insights_queries
from app.services.visibility import Viewer
from app.services.workspace import WorkspaceContext

# The naming rule of experiment-owned configs — the only place it is parsed here.
# `exp_<id8>_…` belongs to a configuration A/B experiment, `can_<id8>_…` to a runtime
# canary, id8 = the first 8 chars of the owning record's 12-char id. A canary scores
# each arm with its own config (`_oec` control, `_oet` treatment); an experiment's
# `_oe1` scores both arms through the shared gateway (legacy rows also have `_oe2`).
PARENT_KINDS = {"exp_": ("experiment", Experiment), "can_": ("canary", RuntimeCanary)}
# the arm a config scores, by its name suffix; unlisted (an experiment's `_oe1`) = both.
# `exp_<id8>_oe2` is pre-split (ff7756c9): it judged the experiment's built-in canary challenger
PARENT_ARMS = {
    "canary": {"oec": "control", "oet": "treatment"},
    "experiment": {"oe2": "treatment"},
}
EXPERIMENT_PREFIXES = tuple(PARENT_KINDS)
NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,47}$")
FILTER_KEY_RE = re.compile(r"^[a-zA-Z0-9._-]{1,256}$")
FILTER_OPERATORS = (
    "Equals", "NotEquals", "GreaterThan", "LessThan",
    "GreaterThanOrEqual", "LessThanOrEqual", "Contains", "NotContains",
)
MAX_FILTERS = 5
RECENT_LIMIT = 50
# Historical configs retain their insight types and schedule on read.
INSIGHT_IDS = tuple(ac.INSIGHT_TYPES)
FREQUENCIES = ("DAILY", "WEEKLY", "MONTHLY")
Mode = str  # "scores" | "insights"
REPORT_SCAN_LIMIT = 300
REPORT_RANGES = {"1h": 1, "6h": 6, "24h": 24, "7d": 168}
RUNTIME_LOG_GROUP_PREFIX = "/aws/bedrock-agentcore/runtimes/"

Owner = str  # "agent" | "experiment" | "external"


# ─── helpers ────────────────────────────────────────────────────────────────


def _exc_name(exc: Exception) -> str:
    return type(exc).__name__


def _is_not_found(exc: Exception) -> bool:
    return _exc_name(exc) in {"ResourceNotFoundException", "NotFoundException"}


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else None


def owner_of(name: str, config_id: str, ledger_ids: set[str]) -> Owner:
    if config_id in ledger_ids:
        return "agent"
    if str(name or "").startswith(EXPERIMENT_PREFIXES):
        return "experiment"
    return "external"


ParentRecord = Experiment | RuntimeCanary
ParentIndex = dict[tuple[str, str], list[ParentRecord]]  # (kind, id8) → newest first


def parent_ref(name: str) -> tuple[str, str, str | None] | None:
    """``(kind, id8, arm)`` of an experiment/canary-owned config name, else None."""
    name = str(name or "")
    for prefix, (kind, _model) in PARENT_KINDS.items():
        if name.startswith(prefix):
            id8, _, suffix = name[len(prefix):].partition("_")
            return kind, id8, PARENT_ARMS[kind].get(suffix)
    return None


def parent_index(db: Session, ws: WorkspaceScope, names: Sequence[str]) -> ParentIndex:
    """The experiment / canary records behind ``names`` — one query per model at
    most, none when no name has a parent, so a list never queries per row."""
    wanted: dict[str, set[str]] = {}
    for name in names:
        ref = parent_ref(name)
        if ref is not None and ref[1]:
            wanted.setdefault(ref[0], set()).add(ref[1])
    models = dict(PARENT_KINDS.values())
    index: ParentIndex = {}
    for kind, id8s in wanted.items():
        model = models[kind]
        records = db.scalars(
            select(model)
            .where(model.workspace_id == ws.id, func.substr(model.id, 1, 8).in_(sorted(id8s)))
            .order_by(model.created_at.desc())
        )
        for record in records:
            index.setdefault((kind, record.id[:8]), []).append(record)
    return index


def _parent_records(index: ParentIndex, name: str) -> list[ParentRecord]:
    ref = parent_ref(name)
    return index.get((ref[0], ref[1]), []) if ref is not None else []


def parent_of(
    db: Session, viewer: Viewer, index: ParentIndex, name: str
) -> dict[str, Any] | None:
    """The ``parent`` field of an experiment-owned config: the owning record (the
    newest one ``viewer`` sees when an 8-char prefix is shared) and the arm it
    scores. A record gone from the ledger keeps ``kind`` + ``arm`` (both read off the
    name); ``id`` / ``name`` / ``status`` / ``agent_name`` are then None."""
    ref = parent_ref(name)
    if ref is None:
        return None
    kind, _id8, arm = ref
    seen = [r for r in _parent_records(index, name) if visibility.sees(viewer, db, r.created_by)]
    record = seen[0] if seen else None
    if record is None:
        agent_name = None
    elif isinstance(record, Experiment):
        # a pre-split `_oe2` judged the built-in canary's challenger, another agent
        agent_name = (
            ((record.artifacts or {}).get("canary") or {}).get("challenger_agent")
            if arm == "treatment" else record.agent_name
        )
    else:
        # a canary's two arms are versions of one agent
        agent_name = record.champion_agent_name
    return {
        "kind": kind,
        "id": record.id if record else None,
        "name": record.name if record else None,
        # the status the A/B test page shows (an unfinished legacy promotion is `ready`)
        "status": (
            None if record is None
            else experiment_status(record) if isinstance(record, Experiment)
            else record.status
        ),
        "agent_name": agent_name,
        "arm": arm,
    }


def config_visible(
    db: Session, ws: WorkspaceScope, viewer: Viewer, *, name: str,
    row: OnlineEvalConfig | None, parents: ParentIndex | None = None,
) -> bool:
    """Workspace isolation for one config: a ledger row → its creator; an
    experiment/canary-owned config follows its owning record (shared when that
    record is gone); an external config has no known creator → shared.
    ``parents`` is a preloaded :func:`parent_index` (the list path)."""
    if not viewer.filters:
        return True
    if row is not None:
        return visibility.sees(viewer, db, row.created_by)
    index = parents if parents is not None else parent_index(db, ws, [str(name or "")])
    records = _parent_records(index, str(name or ""))
    return not records or any(visibility.sees(viewer, db, r.created_by) for r in records)


class _ProjectConfigs:
    """Which online configs the current project owns (design §5.5), for a workspace
    holding several projects. ListOnlineEvaluationConfigs is account-wide and the
    ledger is project-filtered, so another project's config would otherwise read as
    `external` (or as an experiment arm whose owner is gone) and be shared.

    A config belongs to the project of its ledger row, else of the experiment /
    canary its name embeds; an external one (no owner at all) only to a project whose
    agent its log groups match. Built across every project; None — no fence — with no
    current project or a single-project workspace."""

    def __init__(self, db: Session, ws: WorkspaceScope, project: str) -> None:
        self._db, self._ws, self.project = db, ws, project
        with project_context(None):
            self.ledger: dict[str, str | None] = dict(db.execute(
                select(OnlineEvalConfig.config_id, OnlineEvalConfig.project_id)
                .where(OnlineEvalConfig.workspace_id == ws.id)
            ).all())

    @classmethod
    def load(cls, db: Session, ws: WorkspaceScope) -> _ProjectConfigs | None:
        project = current_project()
        if project is None or not eval_service.several_projects(db, ws.id):
            return None
        return cls(db, ws, project)

    def foreign_row(self, config_id: str) -> bool:
        return config_id in self.ledger and self.ledger[config_id] != self.project

    def decide(self, config_id: str, name: str) -> bool | None:
        """True / False, or None: visible only if its log groups match this project's agent."""
        if config_id in self.ledger:
            return self.ledger[config_id] == self.project
        model, id8 = _parent_model(str(name or ""))
        if model is None:
            return None
        with project_context(None):
            parents = set(self._db.scalars(
                select(model.project_id).where(
                    model.workspace_id == self._ws.id, model.id.startswith(id8, autoescape=True)
                )
            ))
        return self.project in parents  # an arm whose owner is gone is no one's here


def _parent_model(name: str) -> tuple[Any, str]:
    """(Experiment | RuntimeCanary | None, id8) for an `exp_<id8>_…` / `can_<id8>_…` name."""
    model = (
        Experiment if name.startswith("exp_")
        else RuntimeCanary if name.startswith("can_")
        else None
    )
    id8 = name[4:].split("_", 1)[0] if model is not None else ""
    return (model, id8) if id8 else (None, "")


def _not_found(config_id: str) -> NotFoundError:
    return NotFoundError(
        "online_eval.not_found", f"online evaluation config {config_id} not found"
    )


def mode_of(aws: dict[str, Any]) -> Mode:
    return "insights" if aws.get("insights") else "scores"


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
    """``oe_<agent-slug>_<6hex>`` — always matches the AWS name regex."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", agent_name or "agent").strip("_")[:24] or "agent"
    return f"oe_{slug}_{suffix or uuid.uuid4().hex[:6]}"


def build_rule(
    sampling_percentage: float, session_timeout_minutes: int, filters: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """The complete ``rule`` — Create and Update both take it as one value."""
    rule: dict[str, Any] = {
        "samplingConfig": {"samplingPercentage": float(sampling_percentage)},
        "sessionConfig": {"sessionTimeoutMinutes": int(session_timeout_minutes)},
    }
    if filters:
        rule["filters"] = [dict(f) for f in filters]
    return rule


def validate_filters(filters: Sequence[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Shape-check filters the way AWS will (key regex, operator enum, one typed value)."""
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


def _agent_log_group_matchers(
    db: Session, ws: WorkspaceScope, viewer: Viewer = Viewer.ALL
) -> list[tuple[Agent, str, bool]]:
    """(agent, log-group needle, is_prefix) for every deployed agent in the workspace
    that ``viewer`` may see (a hidden agent is never named as the match).

    Derived offline: runtime methods log to ``runtimes/<resource_id>-<endpoint>``; a
    harness's backing runtime logs under ``runtimes/harness_<name>-*`` (see
    evaluation-agent-eligibility.md). Used only for the best-effort
    ``matched_agent`` hint on external configs.
    """
    rows = db.scalars(
        select(Agent).where(
            Agent.workspace_id == ws.id,
            Agent.status == "active",
            visibility.clause(viewer, Agent.created_by),
        )
    ).all()
    out: list[tuple[Agent, str, bool]] = []
    for agent in rows:
        if not agent.resource_id or agent.method not in EVAL_SUPPORTED_METHODS:
            continue
        if agent.method == "harness":
            base = agent.resource_id.rsplit("-", 1)[0]
            out.append((agent, f"{RUNTIME_LOG_GROUP_PREFIX}harness_{base}-", True))
        else:
            # the runtime's whole family of endpoint groups (-DEFAULT, -live, -candidate)
            out.append((agent, f"{RUNTIME_LOG_GROUP_PREFIX}{agent.resource_id}-", True))
    return out


def _match_agent(
    log_groups: Sequence[str], matchers: Sequence[tuple[Agent, str, bool]]
) -> dict[str, str] | None:
    for lg in log_groups:
        for agent, needle, is_prefix in matchers:
            if (lg.startswith(needle) if is_prefix else lg == needle):
                return {"id": agent.id, "name": agent.name}
    return None


def _normalize(
    aws: dict[str, Any], *, owner: Owner, row: OnlineEvalConfig | None,
    matched_agent: dict[str, str] | None = None, detailed: bool,
    parent: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One row shape for list summaries and Get details (``detailed`` marks which).
    ``parent`` (:func:`parent_of`) is set on experiment-owned rows only."""
    config_id = aws["onlineEvaluationConfigId"]
    rule = aws.get("rule") or {}
    ds = ((aws.get("dataSourceConfig") or {}).get("cloudWatchLogs")) or {}
    output = ((aws.get("outputConfig") or {}).get("cloudWatchConfig")) or {}
    filters = list(rule.get("filters") or [])
    out: dict[str, Any] = {
        "config_id": config_id,
        "arn": aws.get("onlineEvaluationConfigArn"),
        "name": aws.get("onlineEvaluationConfigName"),
        "description": aws.get("description") or "",
        "owner": owner,
        "status": aws.get("status"),
        "execution_status": aws.get("executionStatus"),
        "failure_reason": aws.get("failureReason"),
        "agent_id": row.agent_id if row else None,
        "agent_name": row.agent_name if row else None,
        "matched_agent": matched_agent,
        "parent": parent,
        "detailed": detailed,
        "evaluators": [e.get("evaluatorId") for e in aws.get("evaluators") or []],
        "sampling_percentage": (rule.get("samplingConfig") or {}).get("samplingPercentage"),
        "session_timeout_minutes": (rule.get("sessionConfig") or {}).get(
            "sessionTimeoutMinutes"
        ),
        "filter_count": len(filters),
        "filters": filters,
        "data_source": {
            "log_groups": list(ds.get("logGroupNames") or ([row.log_group] if row else [])),
            "service_name": (ds.get("serviceNames") or [row.service_name if row else None])[0],
            "service_names": ds.get("serviceNames") or ([row.service_name] if row else []),
        },
        "mode": mode_of(aws),
        "insights": [i.get("insightId") for i in aws.get("insights") or []],
        "clustering_frequencies": list(
            (aws.get("clusteringConfig") or {}).get("frequencies") or []
        ),
        "execution_role_arn": aws.get("evaluationExecutionRoleArn"),
        "results_log_group": output.get("logGroupName")
        or ac.online_eval_results_log_group(config_id),
        "duplicate_enabled": False,
        "created_at": _iso(aws.get("createdAt")),
        "updated_at": _iso(aws.get("updatedAt")),
        # console-created configs only: experiment/canary/external ones have no row
        **creators.fields(row),
    }
    return out


def _ledger_rows(db: Session, ws: WorkspaceScope) -> dict[str, OnlineEvalConfig]:
    rows = db.scalars(
        select(OnlineEvalConfig).where(OnlineEvalConfig.workspace_id == ws.id)
    ).all()
    return {r.config_id: r for r in rows}


def _require_owner(config: dict[str, Any], allowed: Sequence[Owner], action: str) -> None:
    if config["owner"] not in allowed:
        raise AppError(
            "online_eval.read_only",
            f"{action} is not available for {config['owner']}-owned configs",
            {"owner": config["owner"], "action": action},
            status_code=403,
        )


# ─── read side ──────────────────────────────────────────────────────────────


def list_configs(
    db: Session, ws: WorkspaceScope, control: Any = None, *, viewer: Viewer = Viewer.ALL
) -> list[dict[str, Any]]:
    """Every config in the account, newest-updated first, classified + enriched.

    Summaries lack evaluators / rule, so agent- and external-owned rows get one
    ``Get`` each (experiment rows stay summary-only — their page is elsewhere).
    A failing ``Get`` degrades that row to its summary rather than failing the list.
    Configs hidden from ``viewer`` (workspace isolation) are skipped before any Get.
    The owning experiments / canaries of every listed config load in one query
    per model (:func:`parent_index`), shared by the visibility check and ``parent``.
    """
    control = control or control_client(ws.context)
    ledger = _ledger_rows(db, ws)
    fence = _ProjectConfigs.load(db, ws)
    summaries = ac.list_online_eval_configs(control)
    parents = parent_index(db, ws, [
        s.get("onlineEvaluationConfigName", "") for s in summaries
        if s["onlineEvaluationConfigId"] not in ledger
    ])
    matchers: list[tuple[Agent, str, bool]] | None = None
    out: list[dict[str, Any]] = []
    for summary in summaries:
        config_id = summary["onlineEvaluationConfigId"]
        name = summary.get("onlineEvaluationConfigName", "")
        owner = owner_of(name, config_id, set(ledger))
        row = ledger.get(config_id)
        verdict = fence.decide(config_id, name) if fence is not None else True
        if verdict is False or not config_visible(
            db, ws, viewer, name=name, row=row, parents=parents
        ):
            continue
        aws, detailed = summary, False
        if owner != "experiment":
            try:
                aws, detailed = ac.get_online_eval_config(control, config_id=config_id), True
            except Exception:  # noqa: BLE001 — degrade to the summary
                pass
        matched = None
        if owner == "external" and detailed:
            if matchers is None:
                matchers = _agent_log_group_matchers(db, ws, viewer)
            ds = ((aws.get("dataSourceConfig") or {}).get("cloudWatchLogs")) or {}
            matched = _match_agent(ds.get("logGroupNames") or [], matchers)
        if verdict is None and matched is None:
            continue  # several projects: an unmatched external config is no one's here
        parent = parent_of(db, viewer, parents, name) if owner == "experiment" else None
        out.append(_normalize(aws, owner=owner, row=row, matched_agent=matched,
                              detailed=detailed, parent=parent))
    enabled_by_agent: dict[str, int] = {}
    for c in out:
        if c["owner"] == "agent" and c["execution_status"] == "ENABLED":
            enabled_by_agent[c["agent_id"]] = enabled_by_agent.get(c["agent_id"], 0) + 1
    for c in out:
        c["duplicate_enabled"] = (
            c["owner"] == "agent"
            and c["execution_status"] == "ENABLED"
            and enabled_by_agent.get(c["agent_id"], 0) > 1
        )
    out.sort(key=lambda c: c.get("updated_at") or c.get("created_at") or "", reverse=True)
    return out


def get_config(
    db: Session, ws: WorkspaceScope, config_id: str, control: Any = None, *,
    viewer: Viewer = Viewer.ALL,
) -> dict[str, Any]:
    """One config, or 404 — also when workspace isolation hides it from ``viewer``
    (a hidden ledger-backed config answers before any AWS call)."""
    control = control or control_client(ws.context)
    ledger = _ledger_rows(db, ws)
    fence = _ProjectConfigs.load(db, ws)
    row = ledger.get(config_id)
    if fence is not None and fence.foreign_row(config_id):
        raise _not_found(config_id)  # another project's config: before any AWS call
    if row is not None and not config_visible(db, ws, viewer, name=row.name, row=row):
        raise _not_found(config_id)
    try:
        aws = ac.get_online_eval_config(control, config_id=config_id)
    except Exception as exc:
        if _is_not_found(exc):
            raise _not_found(config_id) from exc
        raise
    name = aws.get("onlineEvaluationConfigName", "")
    verdict = fence.decide(config_id, name) if fence is not None else True
    if verdict is False:
        raise _not_found(config_id)
    parents = parent_index(db, ws, [name]) if row is None else {}
    if row is None and not config_visible(db, ws, viewer, name=name, row=None,
                                          parents=parents):
        raise _not_found(config_id)
    owner = owner_of(name, config_id, set(ledger))
    matched = None
    if owner == "external":
        ds = ((aws.get("dataSourceConfig") or {}).get("cloudWatchLogs")) or {}
        matched = _match_agent(
            ds.get("logGroupNames") or [], _agent_log_group_matchers(db, ws, viewer)
        )
    if verdict is None and matched is None:
        raise _not_found(config_id)
    parent = parent_of(db, viewer, parents, name) if owner == "experiment" else None
    return _normalize(aws, owner=owner, row=ledger.get(config_id), matched_agent=matched,
                      detailed=True, parent=parent)


# ─── write side ─────────────────────────────────────────────────────────────


def _refuse_duplicate_failure_analysis(
    control: Any, service_names: Sequence[str], *, exclude: str | None = None,
) -> None:
    """Check the AWS source directly, including configs invisible to this viewer."""
    for summary in ac.list_online_eval_configs(control):
        if (summary.get("executionStatus") == "DISABLED"
                or summary["onlineEvaluationConfigId"] == exclude):
            continue
        config = ac.get_online_eval_config(
            control, config_id=summary["onlineEvaluationConfigId"],
        )
        if config.get("executionStatus") == "DISABLED":
            continue
        source = (config.get("dataSourceConfig") or {}).get("cloudWatchLogs") or {}
        if set(service_names).intersection(source.get("serviceNames") or ()) and any(
            evaluator.get("evaluatorId") == ac.FAILURE_ANALYSIS
            for evaluator in config.get("evaluators") or ()
        ):
            raise AppError(
                "online_eval.failure_analysis_already_enabled",
                "Failure Analysis is already enabled for this endpoint. Use an existing "
                "evaluation or choose another evaluator.",
                status_code=409,
            )


def create_config(
    db: Session,
    ws: WorkspaceScope,
    agent: Agent,
    *,
    evaluators: Sequence[str] | None,
    sampling_percentage: float,
    session_timeout_minutes: int,
    filters: Sequence[dict[str, Any]] | None,
    description: str | None,
    enable_on_create: bool,
    mode: Mode = "scores",
    insights: Sequence[str] | None = None,
    clustering_frequencies: Sequence[str] | None = None,
    control: Any = None,
    logs: Any = None,
    creator: Creator | None = None,
) -> dict[str, Any]:
    eval_service.reject_preview_submission(
        mode == "insights" or bool(insights or clustering_frequencies)
        or any(ac.is_preview_evaluator(e) for e in evaluators or [])
    )
    control = control or control_client(ws.context)
    service_name, log_group = resolve_telemetry(agent, ws.context, logs)
    chosen = normalize_online_evaluators(
        evaluators, control, code_prefix="online_eval", allow_failure_analysis=True,
    )
    rule = build_rule(sampling_percentage, session_timeout_minutes, validate_filters(filters))
    role_arn = ws.context.resources.get("execution_role_arn")
    if not role_arn:
        raise AppError(
            "online_eval.workspace_not_bootstrapped",
            "this workspace has no execution role yet — run bootstrap first",
            status_code=400,
        )
    if enable_on_create and ac.FAILURE_ANALYSIS in chosen:
        _refuse_duplicate_failure_analysis(control, [service_name])
    text = (description or f"Launchpad online evaluation · {agent.name}")[:200]

    created: dict[str, Any] | None = None
    name = generate_name(agent.name)
    for attempt in (0, 1):
        try:
            created = ac.create_online_eval_config(
                control, name=name, description=text, log_group=log_group,
                service_name=service_name, evaluators=chosen, rule=rule,
                role_arn=role_arn, enable_on_create=enable_on_create,
            )
            break
        except Exception as exc:
            kind = _exc_name(exc)
            if kind == "ConflictException" and attempt == 0:
                name = generate_name(agent.name)  # fresh suffix, one retry
                continue
            if kind == "ConflictException":
                raise AppError("online_eval.conflict", str(exc), {"name": name},
                               status_code=409) from exc
            if kind == "ValidationException" and "log group" in str(exc).lower():
                raise AppError(
                    "online_eval.no_telemetry",
                    "this agent has no telemetry log group yet — run at least one "
                    "chat/invoke session first, then create the online evaluation",
                    {"log_group": log_group},
                    status_code=400,
                ) from exc
            raise
    assert created is not None
    row = OnlineEvalConfig(
        workspace_id=ws.id,
        agent_id=agent.id,
        agent_name=agent.name,
        config_id=created["onlineEvaluationConfigId"],
        config_arn=created["onlineEvaluationConfigArn"],
        name=name,
        service_name=service_name,
        log_group=log_group,
    )
    creators.stamp(row, creator)
    db.add(row)
    db.commit()
    # Create returns status/executionStatus/outputConfig but not the config body;
    # merge the request so the response is a complete row without a second call.
    aws = {
        **created,
        "onlineEvaluationConfigName": name,
        "description": text,
        "rule": rule,
        "dataSourceConfig": {
            "cloudWatchLogs": {"logGroupNames": [log_group], "serviceNames": [service_name]}
        },
        "evaluators": [{"evaluatorId": e} for e in chosen],
        "insights": [],
        "evaluationExecutionRoleArn": role_arn,
    }
    return _normalize(aws, owner="agent", row=row, detailed=True)


def patch_config(
    db: Session, ws: WorkspaceScope, config_id: str, patch: dict[str, Any], control: Any = None,
    *, viewer: Viewer = Viewer.ALL,
) -> dict[str, Any]:
    """Update description / evaluators / sampling / timeout / filters on an agent config.

    AWS replaces ``rule`` as a unit, so any rule field in the patch triggers a
    Get → merge → send-complete-rule sequence; the other fields go through
    unchanged because omitted top-level fields are kept.
    """
    control = control or control_client(ws.context)
    current = get_config(db, ws, config_id, control, viewer=viewer)
    _require_owner(current, ("agent",), "edit")
    fields: dict[str, Any] = {}
    if patch.get("description") is not None:
        # AWS requires 1..200 chars (min 1): a cleared description falls back to
        # the same auto text Create uses instead of a botocore ParamValidationError.
        text = str(patch["description"]).strip()[:200]
        fields["description"] = text or (
            f"Launchpad online evaluation · {current['agent_name'] or current['name']}"
        )[:200]
    eval_service.reject_preview_submission(
        current["mode"] == "insights" or patch.get("insights") is not None
        or patch.get("clustering_frequencies") is not None
        or any(ac.is_preview_evaluator(e) for e in patch.get("evaluators") or [])
    )
    if patch.get("evaluators") is not None:
        refuse_hidden_evaluators(
            db, ws.id, viewer, patch["evaluators"], code_prefix="online_eval"
        )
        chosen = normalize_online_evaluators(
            patch["evaluators"], control, code_prefix="online_eval",
            allow_failure_analysis=True,
        )
        if (current["execution_status"] == "ENABLED" and ac.FAILURE_ANALYSIS in chosen
                and ac.FAILURE_ANALYSIS not in current["evaluators"]):
            _refuse_duplicate_failure_analysis(
                control, current["data_source"]["service_names"], exclude=config_id,
            )
        fields["evaluators"] = [{"evaluatorId": e} for e in chosen]
    rule_keys = {"sampling_percentage", "session_timeout_minutes", "filters"}
    if any(patch.get(k) is not None for k in rule_keys):
        sampling = patch.get("sampling_percentage")
        timeout = patch.get("session_timeout_minutes")
        filters = patch.get("filters")
        fields["rule"] = build_rule(
            current["sampling_percentage"] if sampling is None else sampling,
            current["session_timeout_minutes"] if timeout is None else timeout,
            current["filters"] if filters is None else validate_filters(filters),
        )
    if not fields:
        return current
    ac.update_online_eval_config(control, config_id=config_id, **fields)
    return get_config(db, ws, config_id, control)


def set_execution_status(
    db: Session, ws: WorkspaceScope, config_id: str, *, enabled: bool, control: Any = None,
    viewer: Viewer = Viewer.ALL,
) -> dict[str, Any]:
    control = control or control_client(ws.context)
    current = get_config(db, ws, config_id, control, viewer=viewer)
    _require_owner(current, ("agent", "external"), "resume" if enabled else "pause")
    eval_service.reject_preview_submission(enabled and current["mode"] == "insights")
    if (enabled and current["execution_status"] != "ENABLED"
            and ac.FAILURE_ANALYSIS in current["evaluators"]):
        _refuse_duplicate_failure_analysis(
            control, current["data_source"]["service_names"], exclude=config_id,
        )
    ac.update_online_eval_config(
        control, config_id=config_id, executionStatus="ENABLED" if enabled else "DISABLED"
    )
    return get_config(db, ws, config_id, control)


def delete_config(
    db: Session, ws: WorkspaceScope, config_id: str, control: Any = None, *,
    viewer: Viewer = Viewer.ALL,
) -> dict[str, Any]:
    """Delete on AWS, drop the ledger row; the results log group is left in place."""
    control = control or control_client(ws.context)
    ledger = _ledger_rows(db, ws)
    row = ledger.get(config_id)
    if row is not None and not config_visible(db, ws, viewer, name=row.name, row=row):
        raise _not_found(config_id)
    try:
        current = get_config(db, ws, config_id, control, viewer=viewer)
    except NotFoundError:
        if row is None:
            raise
        current = None  # AWS already lost it — reconcile the ledger below
    if current is not None:
        _require_owner(current, ("agent", "external"), "delete")
        try:
            ac.delete_online_eval_config(control, config_id=config_id)
        except Exception as exc:
            if not _is_not_found(exc):
                raise
    if row is not None:
        db.delete(row)
        db.commit()
    return {
        "config_id": config_id,
        "deleted": True,
        "results_log_group": ac.online_eval_results_log_group(config_id),
    }


# ─── results (CloudWatch Logs Insights over the results log group) ───────────

_RESULT_FILTER = 'filter name = "gen_ai.evaluation.result"'
_FIELDS = (
    "fields attributes.gen_ai.evaluation.name as evaluator, "
    "attributes.aws.bedrock_agentcore.evaluation_level as level, "
    "attributes.gen_ai.evaluation.score.value as score, "
    "attributes.gen_ai.evaluation.score.label as label, "
    "attributes.session.id as session_id"
)


def results_queries(range_key: str) -> dict[str, str]:
    return {
        "summary": f"""
{_FIELDS}
| {_RESULT_FILTER} and ispresent(score)
| stats avg(score) as mean, count(*) as count, count_distinct(session_id) as sessions
  by evaluator, level
""",
        "labels": f"""
{_FIELDS}
| {_RESULT_FILTER} and ispresent(score) and ispresent(label)
| stats count(*) as count by evaluator, label
""",
        "series": f"""
{_FIELDS}
| {_RESULT_FILTER} and ispresent(score)
| stats avg(score) as mean, count(*) as count by evaluator, bin({BIN_BY_RANGE[range_key]}) as bucket
| sort bucket asc
| limit 500
""",
        "recent": f"""
{_FIELDS}, @timestamp as time, traceId as trace_id,
  attributes.gen_ai.evaluation.explanation as explanation,
  attributes.error.type as error_type, attributes.error.message as error_message
| {_RESULT_FILTER}
| sort @timestamp desc
| limit {RECENT_LIMIT}
""",
        "errors": f"""
fields attributes.error.message as message
| {_RESULT_FILTER} and ispresent(message)
| stats count(*) as count, earliest(message) as first_message
""",
    }


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int:
    n = _num(value)
    return int(n) if n is not None else 0


def parse_results(rows: dict[str, list[dict[str, str]]]) -> dict[str, Any]:
    labels: dict[str, dict[str, int]] = {}
    for r in rows.get("labels", []):
        if r.get("evaluator") and r.get("label"):
            labels.setdefault(r["evaluator"], {})[r["label"]] = _int(r.get("count"))
    evaluators = [
        {
            "evaluator_id": r["evaluator"],
            "level": r.get("level"),
            "mean": _num(r.get("mean")),
            "count": _int(r.get("count")),
            "sessions": _int(r.get("sessions")),
            "labels": labels.get(r["evaluator"], {}),
        }
        for r in rows.get("summary", [])
        if r.get("evaluator")
    ]
    evaluators.sort(key=lambda e: e["evaluator_id"])
    series: dict[str, list[dict[str, Any]]] = {}
    for r in rows.get("series", []):
        if r.get("evaluator") and r.get("bucket"):
            series.setdefault(r["evaluator"], []).append(
                {"bucket": r["bucket"], "mean": _num(r.get("mean")), "count": _int(r.get("count"))}
            )
    recent = [
        {
            "time": r.get("time"),
            "session_id": r.get("session_id"),
            "trace_id": r.get("trace_id"),
            "evaluator_id": r.get("evaluator"),
            "level": r.get("level"),
            "score": _num(r.get("score")),
            "label": r.get("label"),
            "explanation": r.get("explanation"),
            "error": (
                f"{r.get('error_type') or 'error'}: {r['error_message']}"
                if r.get("error_message") else None
            ),
        }
        for r in rows.get("recent", [])
    ]
    err = (rows.get("errors") or [{}])[0]
    return {
        "evaluators": evaluators,
        "series": series,
        "recent": recent,
        "errors": {"count": _int(err.get("count")), "first_message": err.get("first_message")},
    }


def results(
    ws: WorkspaceScope,
    config_id: str,
    range_key: str,
    run_queries: Callable[..., dict[str, list[dict[str, str]]]] | None = None,
) -> dict[str, Any]:
    """Aggregate the config's results log group. A missing group (nothing evaluated
    yet) yields empty collections — the UI explains the session-timeout delay."""
    if range_key not in RANGE_HOURS:
        raise AppError("online_eval.bad_range", f"range must be one of {sorted(RANGE_HOURS)}",
                       status_code=422)
    log_group = ac.online_eval_results_log_group(config_id)
    run = run_queries or run_insights_queries
    rows = run(results_queries(range_key), RANGE_HOURS[range_key],
               log_groups=[log_group], workspace=ws.context)
    return {"range": range_key, "log_group": log_group, **parse_results(rows)}


# ─── daily cost (eval-cost §1.3) ──────────────────────────────────────────────
#
# An online evaluation is billed as it judges: what it cost on a day is the
# judgements its results group recorded that day × tokens per judgement × the
# judge's price (cost.py's formula and basis), plus the measured cost of the
# console-started reports run that day (their `cost_actual`).

DAILY_COST_MAX_DAYS = 30


def daily_cost_query() -> str:
    return f"""
{_FIELDS}
| {_RESULT_FILTER}
| stats count(*) as count by evaluator, bin(1d) as day
| sort day asc
| limit 1000
"""


def daily_cost(
    db: Session,
    ws: WorkspaceScope,
    config_id: str,
    *,
    days: int = 7,
    control: Any = None,
    viewer: Viewer = Viewer.ALL,
    run_queries: Callable[..., dict[str, list[dict[str, str]]]] | None = None,
) -> dict[str, Any]:
    """Per day: the config's judging cost and its reports' measured cost."""
    from app.dlc import cost as cost_svc

    if not 1 <= days <= DAILY_COST_MAX_DAYS:
        raise AppError("online_eval.bad_range",
                       f"days is 1..{DAILY_COST_MAX_DAYS}", status_code=422)
    config = get_config(db, ws, config_id, control, viewer=viewer)
    run = run_queries or run_insights_queries
    rows = run({"daily": daily_cost_query()}, days * 24,
               log_groups=[ac.online_eval_results_log_group(config_id)],
               workspace=ws.context).get("daily") or []
    judges = {j["evaluator_id"]: j for j in cost_svc._judge_evaluators(
        list(config.get("evaluators") or []), [])}
    judge_model = cost_svc._default_judge_model()
    judge_price = cost_svc._price_of(judge_model)
    prices = cost_svc.eval_prices()
    tokens_of: dict[str, tuple[float, str]] = {}
    unpriced: list[str] = []
    by_day: dict[str, dict[str, Any]] = {}

    def day_row(day: str) -> dict[str, Any]:
        return by_day.setdefault(day, {"day": day, "judgements": 0, "judge_usd": 0.0,
                                       "reports_usd": 0.0, "by_evaluator": {}})

    for row in rows:
        evaluator_id, day = row.get("evaluator"), str(row.get("day") or "")[:10]
        count = _int(row.get("count"))
        if not evaluator_id or not day or not count:
            continue
        if evaluator_id not in tokens_of:
            measured = cost_svc._judge_tokens(db, evaluator_id)
            tokens_of[evaluator_id] = ((measured, "measured") if measured
                                       else (float(cost_svc.ROUGH_JUDGE_TOKENS), "rough"))
        tin, tout = cost_svc.split_judge_tokens(tokens_of[evaluator_id][0] * count)
        if cost_svc.is_managed(evaluator_id):
            usd: float | None = cost_svc.managed_usd(tin, tout, prices)
        elif judge_price:
            usd = cost_svc.judge_usd(tin, tout, judge_price)
        else:
            usd = None
            if judge_model and judge_model not in unpriced:
                unpriced.append(judge_model)
        entry = day_row(day)
        entry["judgements"] += count
        entry["judge_usd"] += usd or 0.0
        entry["by_evaluator"][evaluator_id] = {
            "judgements": count, "usd": round(usd, 6) if usd is not None else None,
            "billed_by": (judges.get(evaluator_id) or {}).get("billed_by")
            or ("agentcore_evaluations" if cost_svc.is_managed(evaluator_id) else "your_account"),
        }
    since = datetime.now(UTC) - timedelta(days=days)
    for report in _console_runs(db, ws, config_id):
        actual = report.cost_actual or {}
        created = report.created_at
        if created is not None and created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if not actual.get("final") or created is None or created < since:
            continue
        day_row(created.date().isoformat())["reports_usd"] += float(actual.get("total_usd") or 0)
    out_days = []
    for day in sorted(by_day):
        entry = by_day[day]
        entry["judge_usd"] = round(entry["judge_usd"], 6)
        entry["reports_usd"] = round(entry["reports_usd"], 6)
        entry["usd"] = round(entry["judge_usd"] + entry["reports_usd"], 6)
        out_days.append(entry)
    return {
        "config_id": config_id,
        "days": out_days,
        "range_days": days,
        "total_usd": round(sum(d["usd"] for d in out_days), 6),
        "judge_model": judge_model,
        "unpriced_models": unpriced,
        "basis": {
            "tokens_per_judgement": {k: {"tokens": v[0], "basis": v[1]}
                                     for k, v in tokens_of.items()},
            "prices": {"source": prices.get("source"), "updated_at": prices.get("updated_at")},
        },
        "estimate": True,
    }


# ─── insights reports (batch evaluations sourced from a config) ──────────────
#
# A report is a batch evaluation whose data source is the config
# (`dataSourceConfig.onlineEvaluationConfigSource`). AWS creates them on the
# clustering cadence; the console starts one on demand through the run queue.
# Live facts (research/report-attribution.md): such a batch echoes NEITHER
# `evaluators` NOR `insights` in its record, so summaries with neither key are the
# attribution candidates; the source ARN is only on Get — cached per batch id
# because a batch's data source never changes.

_ATTRIBUTION_CAP = 1000
# batch id → (source arn | None, Get detail). The detail is kept because List
# summaries of config-sourced batches carry no session counts (live-verified) —
# the row takes them from the Get. Only terminal batches are cached (their
# counts and status are final); in-flight ones are re-read on every listing.
_attribution: dict[str, tuple[str | None, dict[str, Any]]] = {}
_attribution_lock = threading.Lock()
_TERMINAL = frozenset({"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "STOPPED"})


def reset_attribution_cache() -> None:
    _attribution.clear()


def _source_info(data: Any, batch_id: str) -> tuple[str | None, dict[str, Any]]:
    with _attribution_lock:
        if batch_id in _attribution:
            return _attribution[batch_id]
    try:
        detail = ac.get_batch_evaluation(data, batch_id=batch_id)
    except Exception:
        return None, {}  # not cached: retried on the next listing
    info = (ac.report_source_arn(detail), detail)
    if detail.get("status") in _TERMINAL:
        with _attribution_lock:
            if len(_attribution) >= _ATTRIBUTION_CAP:
                _attribution.pop(next(iter(_attribution)))
            _attribution[batch_id] = info
    return info


def _sessions(res: dict[str, Any]) -> dict[str, Any]:
    """Session counters; AWS omits ``totalNumberOfSessions`` on config-sourced
    batches (live), so the total falls back to the sum of the parts."""
    completed = res.get("numberOfSessionsCompleted", 0) or 0
    failed = res.get("numberOfSessionsFailed", 0) or 0
    in_progress = res.get("numberOfSessionsInProgress", 0) or 0
    total = res.get("totalNumberOfSessions")
    return {
        "completed": completed,
        "failed": failed,
        "in_progress": in_progress,
        "total": total if total is not None else completed + failed + in_progress,
    }


def _report_row(summary: dict[str, Any], *, origin: str, run: EvalRun | None = None,
                insights: Sequence[str] | None = None,
                detail: dict[str, Any] | None = None) -> dict[str, Any]:
    res = summary.get("evaluationResults") or (detail or {}).get("evaluationResults") or {}
    return {
        "batch_id": summary.get("batchEvaluationId") or (run.batch_eval_id if run else None),
        "name": summary.get("batchEvaluationName"),
        "status": summary.get("status") or (run.status.upper() if run else None),
        "run_status": run.status if run else None,
        "created_at": _iso(summary.get("createdAt")) or (_iso(run.created_at) if run else None),
        "updated_at": _iso(summary.get("updatedAt")) or (_iso(run.updated_at) if run else None),
        "insights": list(insights or []),
        "sessions": _sessions(res),
        "origin": origin,
        "run_id": run.id if run else None,
        "error": (summary.get("errorDetails") or [None])[0] or (run.error if run else None),
        # console-started reports only; AWS-scheduled batches have no creator
        **creators.fields(run),
    }


def _console_runs(db: Session, ws: WorkspaceScope, config_id: str) -> list[EvalRun]:
    return list(db.scalars(
        select(EvalRun).where(EvalRun.workspace_id == ws.id,
                              EvalRun.dataset_name == f"online:{config_id}")
        .order_by(EvalRun.created_at.desc())
    ).all())


def reports(
    db: Session, ws: WorkspaceScope, config_id: str, *, data: Any = None, control: Any = None,
    viewer: Viewer = Viewer.ALL,
) -> dict[str, Any]:
    """Every report of one config: console-started runs (ledger) merged with
    AWS-scheduled batches attributed by their source ARN, newest first. A console
    run hidden from ``viewer`` is dropped — and so is its batch, which would
    otherwise resurface as an AWS-scheduled row."""
    config = get_config(db, ws, config_id, control, viewer=viewer)
    data = data or data_client(ws.context)
    runs = _console_runs(db, ws, config_id)
    hidden_batches = {
        run.batch_eval_id for run in runs
        if run.batch_eval_id and not visibility.sees(viewer, db, run.created_by)
    }
    runs = [run for run in runs if visibility.sees(viewer, db, run.created_by)]
    # The ledger half of the list must survive an AWS listing failure (throttle /
    # outage): console runs are still shown, flagged, and AWS-scheduled rows wait.
    aws_unavailable = False
    try:
        summaries = {b.get("batchEvaluationId"): b
                     for b in ac.list_batch_evaluations(data, limit=REPORT_SCAN_LIMIT)}
    except Exception:
        summaries = {}
        aws_unavailable = True
    rows: list[dict[str, Any]] = []
    seen: set[str] = set(hidden_batches)
    for run in runs:  # console runs first — queued ones have no batch yet
        summary = summaries.get(run.batch_eval_id or "", {})
        detail = None
        if run.batch_eval_id and summary and not summary.get("evaluationResults"):
            detail = _source_info(data, run.batch_eval_id)[1]
        rows.append(_report_row(summary, origin="console", run=run, insights=run.evaluators,
                                detail=detail))
        if run.batch_eval_id:
            seen.add(run.batch_eval_id)
    for batch_id, summary in summaries.items():
        if not batch_id or batch_id in seen:
            continue
        if summary.get("evaluators") or summary.get("insights"):
            continue  # a cloudWatchLogs-sourced batch (console run) — never a report
        arn, detail = _source_info(data, batch_id)
        if arn != config["arn"]:
            continue
        rows.append(_report_row(summary, origin="aws_scheduled", insights=config["insights"],
                                detail=detail))
    rows.sort(key=lambda r: r["created_at"] or "", reverse=True)
    return {"config_id": config_id, "mode": config["mode"], "reports": rows,
            "aws_unavailable": aws_unavailable}


def report_detail(
    db: Session, ws: WorkspaceScope, config_id: str, batch_id: str, *,
    data: Any = None, control: Any = None, viewer: Viewer = Viewer.ALL,
) -> dict[str, Any]:
    config = get_config(db, ws, config_id, control, viewer=viewer)
    if any(
        run.batch_eval_id == batch_id and not visibility.sees(viewer, db, run.created_by)
        for run in _console_runs(db, ws, config_id)
    ):
        raise NotFoundError("online_eval.report_not_found", f"report {batch_id}")
    data = data or data_client(ws.context)
    try:
        batch = ac.get_batch_evaluation(data, batch_id=batch_id)
    except Exception as exc:
        if _is_not_found(exc):
            raise NotFoundError("online_eval.report_not_found", f"report {batch_id}") from exc
        raise
    if ac.report_source_arn(batch) != config["arn"]:
        raise NotFoundError("online_eval.report_not_found",
                            f"report {batch_id} is not sourced from {config_id}")
    res = batch.get("evaluationResults") or {}
    return {
        "batch_id": batch.get("batchEvaluationId"),
        "name": batch.get("batchEvaluationName"),
        "status": batch.get("status"),
        "created_at": _iso(batch.get("createdAt")),
        "updated_at": _iso(batch.get("updatedAt")),
        "time_range": {
            k: _iso(v) for k, v in (
                ((batch.get("dataSourceConfig") or {}).get("onlineEvaluationConfigSource")
                 or {}).get("timeRange") or {}
            ).items()
        },
        "sessions": _sessions(res),
        "insights": ac.parse_insights(batch),
        "error_details": list(batch.get("errorDetails") or []),
    }


def start_report(
    db: Session, ws: WorkspaceScope, config_id: str, range_key: str, *, control: Any = None,
    creator: Creator | None = None, viewer: Viewer = Viewer.ALL,
) -> dict[str, Any]:
    """The historical report endpoints remain readable."""
    config = get_config(db, ws, config_id, control, viewer=viewer)
    _require_owner(config, ("agent",), "run a report for")
    raise eval_service.preview_retired()


# ─── cross-surface reads (Observability session detail + Overview tile) ──────
#
# Both read EVERY config's results at once through a prefix source, so neither
# has to list configs first (and neither hits the 50-group cap of logGroupNames).
# ``_start_query`` treats a ``SOURCE`` query as un-targeted, like SPANS_SOURCE.

RESULTS_SOURCE = f"SOURCE logGroups(namePrefix: ['{ac.ONLINE_EVAL_RESULTS_PREFIX}'])"
SESSION_SCORES_LIMIT = 200
QUALITY_RANGE = "24h"
_NAMES_TTL_SECONDS = 120.0
_names_cache: dict[tuple[str, str | None], tuple[float, dict[str, str]]] = {}
_names_lock = threading.Lock()


def session_scores_query(session_id: str) -> str:
    """Every online result record for one session, across all configs."""
    sid = json.dumps(session_id)
    return f"""
{RESULTS_SOURCE}
| {_RESULT_FILTER} and attributes.session.id = {sid}
| fields @timestamp as time, onlineEvaluationConfigId as config_id,
  attributes.gen_ai.evaluation.name as evaluator,
  attributes.aws.bedrock_agentcore.evaluation_level as level,
  attributes.gen_ai.evaluation.score.value as score,
  attributes.gen_ai.evaluation.score.label as label,
  attributes.gen_ai.evaluation.explanation as explanation, traceId as trace_id
| sort @timestamp desc
| limit {SESSION_SCORES_LIMIT}
"""


def quality_queries(config_ids: Sequence[str]) -> dict[str, str]:
    """Per-(evaluator, config) means plus account-wide distinct counters, limited
    to the given (agent-owned) config ids."""
    ids = ", ".join(json.dumps(c) for c in config_ids)
    base = f"""
{RESULTS_SOURCE}
| {_RESULT_FILTER} and ispresent(attributes.gen_ai.evaluation.score.value)
  and onlineEvaluationConfigId in [{ids}]
"""
    return {
        "pairs": base + """| stats avg(attributes.gen_ai.evaluation.score.value) as mean,
  count(*) as count
  by attributes.gen_ai.evaluation.name as evaluator, onlineEvaluationConfigId as config_id
""",
        "totals": base + """| stats count_distinct(attributes.session.id) as sessions,
  count_distinct(onlineEvaluationConfigId) as configs
""",
    }


def config_names(
    workspace: WorkspaceContext, control: Any = None, db: Session | None = None
) -> dict[str, str]:
    """``{config_id: name}`` for every config in the account (one paginated List,
    cached 120 s per (workspace, project)). Only feeds owner classification of
    results that no ledger row explains; a failure degrades to ``{}`` (→
    ``external``). With ``db``, a workspace holding several projects drops the
    configs another project owns (its ledger rows and experiment / canary arms),
    so no name crosses projects."""
    key = (workspace.id, current_project())
    hit = _names_cache.get(key)
    if hit and time.monotonic() - hit[0] < _NAMES_TTL_SECONDS:
        return hit[1]
    with _names_lock:
        hit = _names_cache.get(key)
        if hit and time.monotonic() - hit[0] < _NAMES_TTL_SECONDS:
            return hit[1]
        try:
            names = {
                c["onlineEvaluationConfigId"]: (
                    c.get("onlineEvaluationConfigName") or c.get("name") or ""
                )
                for c in ac.list_online_eval_configs(control or control_client(workspace))
                if c.get("onlineEvaluationConfigId")
            }
        except Exception:
            names = {}
        fence = _ProjectConfigs.load(db, workspace) if db is not None else None
        if fence is not None:
            names = {cid: n for cid, n in names.items() if fence.decide(cid, n) is not False}
        _names_cache[key] = (time.monotonic(), names)
        return names


def reset_names_cache() -> None:
    _names_cache.clear()


def _agent_ledger_rows(
    db: Session, workspace_id: str, viewer: Viewer = Viewer.ALL
) -> dict[str, OnlineEvalConfig]:
    rows = db.scalars(
        select(OnlineEvalConfig).where(
            OnlineEvalConfig.workspace_id == workspace_id,
            visibility.clause(viewer, OnlineEvalConfig.created_by),
        )
    ).all()
    return {r.config_id: r for r in rows}


def parse_session_scores(
    rows: Sequence[dict[str, str]],
    ledger: dict[str, OnlineEvalConfig],
    names: dict[str, str],
) -> dict[str, Any]:
    """Group result records by config, classified by owner (agent rows first)."""
    grouped: dict[str, dict[str, Any]] = {}
    for r in rows:
        config_id = r.get("config_id") or ""
        if not config_id or not r.get("evaluator"):
            continue
        block = grouped.get(config_id)
        if block is None:
            row = ledger.get(config_id)
            block = grouped[config_id] = {
                "config_id": config_id,
                "config_name": row.name if row else (names.get(config_id) or None),
                "owner": owner_of(names.get(config_id, ""), config_id, set(ledger)),
                "agent": {"id": row.agent_id, "name": row.agent_name} if row else None,
                "records": [],
            }
        block["records"].append({
            "time": r.get("time"),
            "evaluator_id": r["evaluator"],
            "level": r.get("level"),
            "score": _num(r.get("score")),
            "label": r.get("label"),
            "explanation": r.get("explanation"),
            "trace_id": r.get("trace_id"),
        })
    order = {"agent": 0, "experiment": 1, "external": 2}
    configs = sorted(grouped.values(), key=lambda b: (order[b["owner"]], b["config_id"]))
    return {
        "configs": configs,
        "total": sum(len(b["records"]) for b in configs),
        "unavailable": False,
    }


def session_online_scores(
    db: Session,
    workspace: WorkspaceContext,
    session_id: str,
    hours: int,
    run_queries: Callable[..., dict[str, list[dict[str, str]]]] | None = None,
    logs: Any = None,
    control: Any = None,
) -> dict[str, Any]:
    """Online scores for one session — an additive, fail-soft block on the
    Observability session detail. A Logs Insights failure here must never take
    the traces/transcript down with it, hence the separate query + try/except.
    ``configs_exist`` lets the UI hide the panel on workspaces that never
    created a config (and show a "not judged yet" state on those that did)."""
    ledger = _agent_ledger_rows(db, workspace.id)
    exists = bool(ledger)
    run = run_queries or run_insights_queries
    try:
        rows = run({"scores": session_scores_query(session_id)}, hours,
                   logs=logs, workspace=workspace)["scores"]
    except Exception:
        return {"configs": [], "total": 0, "unavailable": True, "configs_exist": exists}
    names = config_names(workspace, control, db) if any(
        (r.get("config_id") or "") not in ledger for r in rows
    ) else {}
    return {**parse_session_scores(rows, ledger, names), "configs_exist": exists}


def _empty_quality(configs: int = 0) -> dict[str, Any]:
    return {"range": QUALITY_RANGE, "mean": None, "scores": 0, "sessions": 0,
            "agents": 0, "configs": configs, "evaluators": []}


def parse_quality(rows: dict[str, list[dict[str, str]]],
                  ledger: dict[str, OnlineEvalConfig]) -> dict[str, Any]:
    """Polarity-normalised, count-weighted mean over every (evaluator, config)
    pair — a Refusal mean of 0.1 contributes 0.9, so the tile reads "higher is
    better" regardless of the evaluator mix. ``evaluators`` keeps the RAW per-
    evaluator means (what the online page shows) plus the polarity applied.
    ``configs`` counts the workspace's agent-owned configs (the ledger), not
    only the ones that scored — the tile tells "configured, nothing judged yet"
    apart from "no config at all" with it; ``agents`` counts agents WITH scores."""
    weighted = 0.0
    total = 0
    per_eval: dict[str, dict[str, float]] = {}
    scored: set[str] = set()
    for r in rows.get("pairs", []):
        evaluator, mean, count = r.get("evaluator"), _num(r.get("mean")), _int(r.get("count"))
        if not evaluator or mean is None or count <= 0:
            continue
        polarity = ac.evaluator_polarity(evaluator)
        weighted += (1 - mean if polarity < 0 else mean) * count
        total += count
        scored.add(r.get("config_id") or "")
        acc = per_eval.setdefault(evaluator, {"sum": 0.0, "count": 0, "polarity": polarity})
        acc["sum"] += mean * count
        acc["count"] += count
    if total == 0:
        return _empty_quality(len(ledger))
    totals = (rows.get("totals") or [{}])[0]
    return {
        "range": QUALITY_RANGE,
        "mean": round(weighted / total, 3),
        "scores": total,
        "sessions": _int(totals.get("sessions")),
        "agents": len({ledger[c].agent_id for c in scored if c in ledger}),
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


def online_quality(
    db: Session,
    workspace: WorkspaceContext,
    run_queries: Callable[..., dict[str, list[dict[str, str]]]] | None = None,
    logs: Any = None,
    *,
    viewer: Viewer = Viewer.ALL,
) -> dict[str, Any]:
    """24 h online quality over the workspace's agent-owned configs — only those
    ``viewer`` can see under workspace isolation. No (visible) agent config → the
    empty payload WITHOUT touching Logs Insights."""
    ledger = _agent_ledger_rows(db, workspace.id, viewer)
    if not ledger:
        return _empty_quality()
    run = run_queries or run_insights_queries
    rows = run(quality_queries(sorted(ledger)), RANGE_HOURS[QUALITY_RANGE],
               logs=logs, workspace=workspace)
    return parse_quality(rows, ledger)
