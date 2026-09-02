"""Per-agent online evaluation: continuous, sampled scoring of live sessions.

One AWS ``OnlineEvaluationConfig`` per (agent, evaluator set). The ledger keeps
identifiers only (:class:`OnlineEvalConfig`); every status/rule/evaluator field
is read back from the control plane. The list surface shows **every** config in
the workspace account, classified by owner:

- ``agent`` — created here for an agent (ledger row): full control.
- ``experiment`` — ``exp_*`` / ``can_*`` arms owned by the experiment and canary
  flows (their cleanup deletes them): read-only.
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

import re
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.evaluation import agentcore_eval as ac
from app.evaluation.models import OnlineEvalConfig
from app.evaluation.online_evaluators import normalize_online_evaluators
from app.evaluation.service import EVAL_SUPPORTED_METHODS, resolve_telemetry
from app.models.ledger import Agent
from app.routers.workspaces import WorkspaceScope
from app.services.agentcore.client import control_client
from app.services.observability import BIN_BY_RANGE, RANGE_HOURS, run_insights_queries

EXPERIMENT_PREFIXES = ("exp_", "can_")
NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,47}$")
FILTER_KEY_RE = re.compile(r"^[a-zA-Z0-9._-]{1,256}$")
FILTER_OPERATORS = (
    "Equals", "NotEquals", "GreaterThan", "LessThan",
    "GreaterThanOrEqual", "LessThanOrEqual", "Contains", "NotContains",
)
MAX_FILTERS = 5
RECENT_LIMIT = 50
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


def _agent_log_group_matchers(db: Session, ws: WorkspaceScope) -> list[tuple[Agent, str, bool]]:
    """(agent, log-group needle, is_prefix) for every deployed agent in the workspace.

    Derived offline: runtime methods log to ``runtimes/<resource_id>-DEFAULT``; a
    harness's backing runtime logs under ``runtimes/harness_<name>-*`` (see
    evaluation-agent-eligibility.md). Used only for the best-effort
    ``matched_agent`` hint on external configs.
    """
    rows = db.scalars(
        select(Agent).where(Agent.workspace_id == ws.id, Agent.status == "active")
    ).all()
    out: list[tuple[Agent, str, bool]] = []
    for agent in rows:
        if not agent.resource_id or agent.method not in EVAL_SUPPORTED_METHODS:
            continue
        if agent.method == "harness":
            base = agent.resource_id.rsplit("-", 1)[0]
            out.append((agent, f"{RUNTIME_LOG_GROUP_PREFIX}harness_{base}-", True))
        else:
            out.append((agent, f"{RUNTIME_LOG_GROUP_PREFIX}{agent.resource_id}-DEFAULT", False))
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
) -> dict[str, Any]:
    """One row shape for list summaries and Get details (``detailed`` marks which)."""
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
        },
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


def list_configs(db: Session, ws: WorkspaceScope, control: Any = None) -> list[dict[str, Any]]:
    """Every config in the account, newest-updated first, classified + enriched.

    Summaries lack evaluators / rule, so agent- and external-owned rows get one
    ``Get`` each (experiment rows stay summary-only — their page is elsewhere).
    A failing ``Get`` degrades that row to its summary rather than failing the list.
    """
    control = control or control_client(ws.context)
    ledger = _ledger_rows(db, ws)
    matchers: list[tuple[Agent, str, bool]] | None = None
    out: list[dict[str, Any]] = []
    for summary in ac.list_online_eval_configs(control):
        config_id = summary["onlineEvaluationConfigId"]
        owner = owner_of(summary.get("onlineEvaluationConfigName", ""), config_id, set(ledger))
        row = ledger.get(config_id)
        aws, detailed = summary, False
        if owner != "experiment":
            try:
                aws, detailed = ac.get_online_eval_config(control, config_id=config_id), True
            except Exception:  # noqa: BLE001 — degrade to the summary
                pass
        matched = None
        if owner == "external" and detailed:
            if matchers is None:
                matchers = _agent_log_group_matchers(db, ws)
            ds = ((aws.get("dataSourceConfig") or {}).get("cloudWatchLogs")) or {}
            matched = _match_agent(ds.get("logGroupNames") or [], matchers)
        out.append(_normalize(aws, owner=owner, row=row, matched_agent=matched,
                              detailed=detailed))
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
    db: Session, ws: WorkspaceScope, config_id: str, control: Any = None
) -> dict[str, Any]:
    control = control or control_client(ws.context)
    ledger = _ledger_rows(db, ws)
    try:
        aws = ac.get_online_eval_config(control, config_id=config_id)
    except Exception as exc:
        if _is_not_found(exc):
            raise NotFoundError(
                "online_eval.not_found", f"online evaluation config {config_id} not found"
            ) from exc
        raise
    owner = owner_of(aws.get("onlineEvaluationConfigName", ""), config_id, set(ledger))
    matched = None
    if owner == "external":
        ds = ((aws.get("dataSourceConfig") or {}).get("cloudWatchLogs")) or {}
        matched = _match_agent(ds.get("logGroupNames") or [], _agent_log_group_matchers(db, ws))
    return _normalize(aws, owner=owner, row=ledger.get(config_id), matched_agent=matched,
                      detailed=True)


# ─── write side ─────────────────────────────────────────────────────────────


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
    control: Any = None,
    logs: Any = None,
) -> dict[str, Any]:
    control = control or control_client(ws.context)
    service_name, log_group = resolve_telemetry(agent, ws.context, logs)
    chosen = normalize_online_evaluators(evaluators, control, code_prefix="online_eval")
    rule = build_rule(sampling_percentage, session_timeout_minutes, validate_filters(filters))
    role_arn = ws.context.resources.get("execution_role_arn")
    if not role_arn:
        raise AppError(
            "online_eval.workspace_not_bootstrapped",
            "this workspace has no execution role yet — run bootstrap first",
            status_code=400,
        )
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
        "evaluationExecutionRoleArn": role_arn,
    }
    return _normalize(aws, owner="agent", row=row, detailed=True)


def patch_config(
    db: Session, ws: WorkspaceScope, config_id: str, patch: dict[str, Any], control: Any = None
) -> dict[str, Any]:
    """Update description / evaluators / sampling / timeout / filters on an agent config.

    AWS replaces ``rule`` as a unit, so any rule field in the patch triggers a
    Get → merge → send-complete-rule sequence; the other fields go through
    unchanged because omitted top-level fields are kept.
    """
    control = control or control_client(ws.context)
    current = get_config(db, ws, config_id, control)
    _require_owner(current, ("agent",), "edit")
    fields: dict[str, Any] = {}
    if patch.get("description") is not None:
        # AWS requires 1..200 chars (min 1): a cleared description falls back to
        # the same auto text Create uses instead of a botocore ParamValidationError.
        text = str(patch["description"]).strip()[:200]
        fields["description"] = text or (
            f"Launchpad online evaluation · {current['agent_name'] or current['name']}"
        )[:200]
    if patch.get("evaluators") is not None:
        chosen = normalize_online_evaluators(
            patch["evaluators"], control, code_prefix="online_eval"
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
    db: Session, ws: WorkspaceScope, config_id: str, *, enabled: bool, control: Any = None
) -> dict[str, Any]:
    control = control or control_client(ws.context)
    current = get_config(db, ws, config_id, control)
    _require_owner(current, ("agent", "external"), "resume" if enabled else "pause")
    ac.update_online_eval_config(
        control, config_id=config_id, executionStatus="ENABLED" if enabled else "DISABLED"
    )
    return get_config(db, ws, config_id, control)


def delete_config(
    db: Session, ws: WorkspaceScope, config_id: str, control: Any = None
) -> dict[str, Any]:
    """Delete on AWS, drop the ledger row; the results log group is left in place."""
    control = control or control_client(ws.context)
    ledger = _ledger_rows(db, ws)
    row = ledger.get(config_id)
    try:
        current = get_config(db, ws, config_id, control)
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
