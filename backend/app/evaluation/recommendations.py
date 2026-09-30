"""Evaluation-task recommendations — StartRecommendation scoped to one run's sessions.

A completed evaluation run already names the exact sessions it judged, so a
recommendation started from it reads those traces — never a rolling window — and
optimizes toward an evaluator the operator just looked at. The system-prompt job
pins the run's batch evaluation; the tool-description job refuses that source
(live-verified), so it gets the same sessions' spans inline, as the CLI's
``--session-id`` does.

Inputs (the current system prompt / tool descriptions the job revises):

- **Managed Harness** — read live from ``GetHarness``: the system prompt, every
  ``inline_function`` description and the tool schemas of each attached
  ``agentcore_gateway`` (control-plane target definitions), narrowed by the
  Harness's own ``allowedTools``. Gateway tools are named ``<target>___<tool>``,
  the name a Harness span records for them.
- **any other agent** — the Launchpad spec when it carries a prompt / discoverable
  tools, else nothing: the console then requires the operator to type them.

The job is asynchronous on AWS; nothing here polls in the background. Every read
of a non-terminal row refreshes it with one ``GetRecommendation`` (AWS is the
source of truth, the row is the pointer + last-seen result), so a restart loses
nothing.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.evaluation import agentcore_eval as ac
from app.evaluation.models import EvalRecommendation, EvalRun
from app.models.ledger import Agent
from app.services.agentcore import harness as hc
from app.services.agentcore import policy as policy_api
from app.services.agentcore.client import control_client, data_client
from app.services.workspace import WorkspaceContext

KINDS = ("system_prompt", "tool_descriptions")
DEFAULT_EVALUATOR = "Builtin.GoalSuccessRate"
# Always offered as optimization targets: the two the devguide recommends for
# task-completion and open-ended agents respectively.
BASELINE_EVALUATORS = (DEFAULT_EVALUATOR, "Builtin.Helpfulness")
SYSTEM_PROMPT_MAX = 20000  # SystemPromptText max (service model)
TOOL_DESCRIPTION_MAX = 20000  # ToolDescriptionText max
TOOL_NAME_MAX = 256  # RecommendationToolName max
_BUILTIN_PREFIX = "Builtin."
SPANS_MAX = 20000  # Spans list max (service model)
# Logs Insights allows 30 concurrent queries per account; leave room for the console
SPAN_QUERY_BATCH = 10
SPAN_LOOKBACK_MARGIN_H = 48  # a run's sessions ran shortly before its row was written
SPAN_LOOKBACK_MAX_H = 24 * 90


def _is_harness(agent: Agent) -> bool:
    from app.services.runtime_discovery import is_discovered_harness

    return agent.method == "harness" or is_discovered_harness(agent)


# ─── input resolution ───────────────────────────────────────────────────────
def _selected(allowed: list[str] | None, alias: str, tool: str | None) -> bool:
    """Does the Harness ``allowedTools`` let the model call ``alias`` (/ ``tool``)?

    Same selector grammar the deployer writes (``harness_tool_access``): ``*`` is
    everything, ``@<alias>`` a whole configured group, ``@<alias>/<pattern>`` some
    of its tools. A plain name selects builtins only, never a configured group.
    An absent / empty list is the service default — every configured tool.
    """
    if not allowed or "*" in allowed:
        return True
    for pattern in allowed:
        group, slash, suffix = pattern.partition("/")
        if not group.startswith("@") or not fnmatchcase(alias, group[1:]):
            continue
        if not slash or tool is None or fnmatchcase(tool, suffix):
            return True
    return False


def _harness_inputs(agent: Agent, workspace: WorkspaceContext) -> dict[str, Any]:
    from app.services.governance import discover_actions

    control = control_client(workspace)
    try:
        detail = hc.get_harness(control, str(agent.resource_id))
    except Exception as exc:
        # The run outlives its Harness (deleted / converted since — measured on prod
        # 2026-09-30, ResourceNotFoundException), or the read is denied. The run's
        # traces are still there, so fall back like any unreadable agent: the spec
        # if it carries inputs, else operator input — never an error card.
        inputs = _spec_inputs(agent)
        inputs["notes"] = [{"code": "harness_unreadable", "tool": str(agent.resource_id),
                            "detail": f"{type(exc).__name__}: {exc}"[:300]}]
        return inputs
    prompt = "\n".join(
        str(part["text"]) for part in detail.get("systemPrompt") or [] if part.get("text")
    )
    allowed = detail.get("allowedTools")
    tools: list[dict[str, str]] = []
    notes: list[dict[str, str]] = []
    for tool in detail.get("tools") or []:
        kind, alias = tool.get("type"), str(tool.get("name") or "")
        config = tool.get("config") or {}
        if kind == "inline_function":
            if _selected(allowed, alias, None):
                description = (config.get("inlineFunction") or {}).get("description") or ""
                tools.append({"name": alias, "description": description,
                              "origin": "inline_function"})
        elif kind == "agentcore_gateway":
            gateway_arn = str((config.get("agentCoreGateway") or {}).get("gatewayArn") or "")
            gateway_id = gateway_arn.rsplit("/", 1)[-1]
            try:
                targets = policy_api.list_gateway_target_details(control, gateway_id)
            except Exception as exc:
                notes.append({"code": "gateway_unreadable", "tool": alias,
                              "detail": f"{type(exc).__name__}: {exc}"[:300]})
                continue
            for action in discover_actions(targets):
                if _selected(allowed, alias, action["name"]):
                    tools.append({"name": action["name"],
                                  "description": action["description"],
                                  "origin": "gateway"})
        elif kind == "remote_mcp":
            # the server's tool list only exists at runtime; a Harness names them
            # `<server>_<tool>` — the operator adds the ones worth optimizing
            notes.append({"code": "remote_mcp_runtime_only", "tool": alias, "detail": ""})
    deduped = list({t["name"]: t for t in tools}.values())
    return {"source": "harness", "system_prompt": prompt, "tools": deduped, "notes": notes}


def _spec_inputs(agent: Agent) -> dict[str, Any]:
    from app.optimization.service import discover_agent_tools

    spec = agent.spec or {}
    prompt = spec.get("system_prompt") if isinstance(spec.get("system_prompt"), str) else ""
    tools = [
        {"name": name, "description": description, "origin": "spec"}
        for name, description in discover_agent_tools(spec).items()
    ]
    source = "spec" if (prompt or tools) else "manual"
    return {"source": source, "system_prompt": prompt or "", "tools": tools, "notes": []}


def evaluator_options(run: EvalRun) -> list[str]:
    """Evaluators a system-prompt recommendation may optimize toward.

    The run's own evaluators first (what the operator just read scores for), then
    the devguide's two defaults. Lower-is-better evaluators are dropped: the job
    pushes the prompt toward whatever scores HIGH, which for Harmfulness /
    Refusal is the opposite of an improvement.
    """
    own = run.evaluators if run.mode == "evaluators" else []
    return [
        e for e in dict.fromkeys([*own, *BASELINE_EVALUATORS])
        if ac.evaluator_polarity(e) > 0
    ]


def resolve_inputs(
    db: Session, run: EvalRun, workspace: WorkspaceContext
) -> dict[str, Any]:
    agent = db.get(Agent, run.agent_id) if run.agent_id else None
    if agent is not None and agent.workspace_id == run.workspace_id and _is_harness(agent) \
            and agent.resource_id:
        inputs = _harness_inputs(agent, workspace)
    elif agent is not None and agent.workspace_id == run.workspace_id:
        inputs = _spec_inputs(agent)
    else:
        # a CloudWatch-sourced run, or its agent is gone: nothing to read from
        inputs = {"source": "manual", "system_prompt": "", "tools": [], "notes": []}
    return {
        **inputs,
        "agent_method": agent.method if agent is not None else None,
        "evaluators": evaluator_options(run),
        "default_evaluator": DEFAULT_EVALUATOR,
        **eligibility(run),
    }


def eligibility(run: EvalRun) -> dict[str, Any]:
    if run.status != "completed":
        return {"eligible": False, "reason_code": "run_not_completed",
                "tools_eligible": False}
    if not run.batch_eval_id:
        return {"eligible": False, "reason_code": "run_no_batch", "tools_eligible": False}
    # tool jobs read the sessions' spans inline, so they need the session list; a
    # time-window run evaluated traffic it never recorded session by session
    return {"eligible": True, "reason_code": None, "tools_eligible": bool(run.session_ids)}


# ─── start ──────────────────────────────────────────────────────────────────
def _evaluator_arn(evaluator: str, workspace: WorkspaceContext) -> str:
    """An evaluator id → the ARN the job takes, refusing a non-numeric scale.

    Built-ins have a region-less ARN. Anything else is read back: GetEvaluator both
    proves it exists here and exposes its rating scale — a categorical judge gives
    the optimizer no numeric signal and fails server-side far less legibly.
    """
    if evaluator.startswith("arn:"):
        return evaluator
    if evaluator.startswith(_BUILTIN_PREFIX):
        return f"arn:aws:bedrock-agentcore:::evaluator/{evaluator}"
    try:
        detail = ac.get_evaluator(control_client(workspace), evaluator_id=evaluator)
    except Exception as exc:
        raise AppError(
            "recommendation.evaluator_unreadable",
            f"evaluator {evaluator} could not be read from AWS",
            {"aws_error": f"{type(exc).__name__}: {exc}"},
            status_code=400,
        ) from exc
    scale = ((detail.get("evaluatorConfig") or {}).get("llmAsAJudge") or {}).get(
        "ratingScale"
    ) or {}
    if scale.get("categorical"):
        raise AppError(
            "recommendation.evaluator_categorical",
            "a system-prompt recommendation needs an evaluator with a numerical rating "
            "scale; this judge uses a categorical one",
            {"evaluator": evaluator},
            status_code=422,
        )
    arn = detail.get("evaluatorArn")
    if not arn:
        raise AppError("recommendation.evaluator_unreadable",
                       f"evaluator {evaluator} reported no ARN", status_code=400)
    return str(arn)


def _batch_arn(run: EvalRun, workspace: WorkspaceContext) -> str:
    try:
        detail = ac.get_batch_evaluation(data_client(workspace), batch_id=str(run.batch_eval_id))
    except Exception as exc:
        raise AppError(
            "recommendation.batch_unreadable",
            "the run's batch evaluation could not be read from AWS",
            {"batch_eval_id": run.batch_eval_id,
             "aws_error": f"{type(exc).__name__}: {exc}"},
            status_code=502,
        ) from exc
    arn = detail.get("batchEvaluationArn")
    if not arn:
        raise AppError("recommendation.batch_unreadable",
                       "the run's batch evaluation reported no ARN",
                       {"batch_eval_id": run.batch_eval_id}, status_code=502)
    return str(arn)


def run_spans(run: EvalRun, workspace: WorkspaceContext) -> list[dict[str, Any]]:
    """The raw span documents of the run's sessions (the on-demand evaluation query)."""
    from app.services.observability import q_session_spans, run_insights_queries

    sessions = list(dict.fromkeys(run.session_ids or []))
    if not sessions:
        raise AppError(
            "recommendation.run_no_sessions",
            "this run recorded no session ids, so there are no spans to read for a "
            "tool-description recommendation",
            status_code=409,
        )
    created = run.created_at or datetime.now(UTC)
    if created.tzinfo is None:  # SQLite hands timestamps back naive
        created = created.replace(tzinfo=UTC)
    age_h = math.ceil((datetime.now(UTC) - created).total_seconds() / 3600)
    hours = min(max(age_h, 0) + SPAN_LOOKBACK_MARGIN_H, SPAN_LOOKBACK_MAX_H)
    spans: list[dict[str, Any]] = []
    for start in range(0, len(sessions), SPAN_QUERY_BATCH):
        chunk = sessions[start:start + SPAN_QUERY_BATCH]
        rows = run_insights_queries(
            {sid: q_session_spans(sid) for sid in chunk}, hours, workspace=workspace
        )
        for sid in chunk:
            for row in rows.get(sid) or []:
                try:
                    doc = json.loads(row.get("@message") or "")
                except (TypeError, ValueError):
                    continue
                if isinstance(doc, dict):
                    spans.append(doc)
    if not spans:
        raise AppError(
            "recommendation.no_spans",
            "no spans were found for this run's sessions — their telemetry may have "
            "expired from CloudWatch Logs",
            {"sessions": len(sessions), "lookback_hours": hours},
            status_code=409,
        )
    return spans[:SPANS_MAX]


def _untraced(tools: dict[str, str], spans: list[dict[str, Any]]) -> set[str]:
    """Tools whose name appears nowhere in the spans.

    The job refuses the WHOLE list when one listed tool is absent from the traces,
    so they are dropped up front. Deliberately loose (any mention counts): a tool
    kept here that the job still rejects is caught by the one retry in ``refresh``.
    """
    blob = json.dumps(spans, ensure_ascii=False)
    return {name for name in tools if name not in blob}


RETRY_SUFFIX = "_r"  # names the one untraced-tools retry of a tool job


def _job_name(run_id: str, kind: str, *, retry: bool = False) -> str:
    # [a-zA-Z][a-zA-Z0-9_-]{0,47}; a per-start tag so a re-run never collides
    tag = f"{'sp' if kind == 'system_prompt' else 'td'}_{uuid.uuid4().hex[:6]}"
    return f"evrec_{run_id[:12]}_{tag}{RETRY_SUFFIX if retry else ''}"


def _start_tools_job(
    data: Any, run_id: str, tools: dict[str, str], spans: list[dict[str, Any]],
    *, retry: bool = False,
) -> tuple[str, str]:
    name = _job_name(run_id, "tool_descriptions", retry=retry)
    started = ac.start_tool_description_recommendation(
        data, name=name,
        tools=[{"toolName": k, "description": v} for k, v in tools.items()],
        session_spans=spans,
    )
    return str(started["recommendationId"]), name


def start(
    db: Session,
    run: EvalRun,
    workspace: WorkspaceContext,
    *,
    kinds: list[str],
    input_source: str,
    system_prompt: str | None,
    evaluator: str | None,
    tools: dict[str, str] | None,
) -> list[EvalRecommendation]:
    gate = eligibility(run)
    if not gate["eligible"]:
        raise AppError(
            f"recommendation.{gate['reason_code']}",
            "only a completed evaluation run with a batch evaluation can seed a "
            "recommendation",
            {"status": run.status}, status_code=409,
        )
    # validate every requested kind before any AWS call — a half-started pair
    # (prompt job running, tool job refused) is the worst outcome to explain
    prompt = (system_prompt or "").strip()
    clean_tools = {k.strip(): v.strip() for k, v in (tools or {}).items() if k.strip()}
    if "system_prompt" in kinds and not prompt:
        raise AppError("recommendation.system_prompt_required",
                       "enter the agent's current system prompt", status_code=422)
    if "tool_descriptions" in kinds:
        if not clean_tools:
            raise AppError("recommendation.tools_required",
                           "enter at least one tool name and description", status_code=422)
        empty = sorted(k for k, v in clean_tools.items() if not v)
        if empty:
            raise AppError("recommendation.tool_description_required",
                           "every tool needs its current description",
                           {"tools": empty}, status_code=422)
    # every AWS read that can refuse happens before the first Start, so a refusal
    # never leaves one kind running and the other unstarted
    evaluator_id = evaluator or DEFAULT_EVALUATOR
    evaluator_arn = batch_arn = ""
    if "system_prompt" in kinds:
        evaluator_arn = _evaluator_arn(evaluator_id, workspace)
        batch_arn = _batch_arn(run, workspace)
    spans: list[dict[str, Any]] = []
    skipped: list[str] = []
    if "tool_descriptions" in kinds:
        spans = run_spans(run, workspace)
        skipped = sorted(_untraced(clean_tools, spans))
        clean_tools = {k: v for k, v in clean_tools.items() if k not in skipped}
        if not clean_tools:
            raise AppError(
                "recommendation.tools_not_traced",
                "none of these tools appears in this run's traces — only tools the "
                "agent called can be analyzed",
                {"tools": skipped}, status_code=422,
            )
    data = data_client(workspace)

    created: list[EvalRecommendation] = []
    for kind in KINDS:
        if kind not in kinds:
            continue
        if kind == "system_prompt":
            name = _job_name(run.id, kind)
            started = ac.start_system_prompt_recommendation(
                data, name=name, system_prompt=prompt,
                batch_evaluation_arn=batch_arn, evaluator_arn=evaluator_arn,
            )
            rec_id = str(started["recommendationId"])
        else:
            rec_id, name = _start_tools_job(data, run.id, clean_tools, spans)
        row = EvalRecommendation(
            workspace_id=run.workspace_id, run_id=run.id, kind=kind,
            recommendation_id=rec_id, name=name, status="PENDING",
            input_source=input_source,
            system_prompt=prompt if kind == "system_prompt" else None,
            evaluator=evaluator_id if kind == "system_prompt" else None,
            tools=clean_tools if kind == "tool_descriptions" else {},
            skipped_tools=skipped if kind == "tool_descriptions" else [],
        )
        db.add(row)
        db.commit()
        created.append(row)
    return created


# ─── refresh / read ─────────────────────────────────────────────────────────
def _apply(row: EvalRecommendation, detail: dict[str, Any]) -> None:
    status = str(detail.get("status") or row.status)
    result = detail.get("recommendationResult") or {}
    row.error = None  # a stale-read note from an earlier refresh no longer applies
    if row.kind == "system_prompt":
        payload = result.get("systemPromptRecommendationResult") or {}
        text = payload.get("recommendedSystemPrompt") or ""
        if status == "COMPLETED" and text:
            row.result = {"recommended_prompt": text,
                          "explanation": payload.get("explanation") or ""}
        elif status in ac.REC_TERMINAL:
            # a job AWS did not complete has no recommendation — never show one
            status = "FAILED"
            row.error = ac.recommendation_error(payload, str(detail.get("status") or ""))
    else:
        payload = result.get("toolDescriptionRecommendationResult") or {}
        suggestions = {
            str(t["toolName"]): {
                "description": t.get("recommendedToolDescription") or "",
                "explanation": t.get("explanation") or "",
            }
            for t in payload.get("tools") or []
            if t.get("toolName") and t.get("recommendedToolDescription")
        }
        if status == "COMPLETED" and suggestions:
            row.result = {"tools": suggestions}
        elif status in ac.REC_TERMINAL:
            status = "FAILED"
            row.error = ac.recommendation_error(payload, str(detail.get("status") or ""))
    row.status = status


def _retry_untraced_tools(
    row: EvalRecommendation, run: EvalRun, workspace: WorkspaceContext
) -> bool:
    """Restart a tool job once without the tools its traces never showed.

    The job refuses the whole list when any tool is absent from the sampled
    traces; the tools that were called still deserve a recommendation.
    """
    missing = ac.tools_not_in_traces(row.error or "")
    remaining = {k: v for k, v in (row.tools or {}).items() if k not in missing}
    if row.kind != "tool_descriptions" or row.name.endswith(RETRY_SUFFIX) or not missing \
            or not remaining:
        return False
    rec_id, name = _start_tools_job(
        data_client(workspace), run.id, remaining, run_spans(run, workspace), retry=True
    )
    row.recommendation_id, row.name = rec_id, name
    row.skipped_tools = sorted({*(row.skipped_tools or []), *missing})
    row.tools = remaining
    row.status, row.error, row.result = "PENDING", None, {}
    return True


def refresh(
    db: Session, row: EvalRecommendation, run: EvalRun, workspace: WorkspaceContext
) -> None:
    if row.status in ac.REC_TERMINAL:
        return
    try:
        detail = ac.get_recommendation(data_client(workspace),
                                       recommendation_id=row.recommendation_id)
    except Exception as exc:
        # transient read failure: keep the last-seen state, say why it is stale
        row.error = f"{type(exc).__name__}: {exc}"[:500]
        db.commit()
        return
    _apply(row, detail)
    if row.status == "FAILED":
        try:
            _retry_untraced_tools(row, run, workspace)
        except Exception as exc:
            row.error = f"{row.error} · retry failed: {type(exc).__name__}: {exc}"[:1000]
    db.commit()


def list_for_run(
    db: Session, run: EvalRun, workspace: WorkspaceContext
) -> list[EvalRecommendation]:
    rows = list(db.scalars(
        select(EvalRecommendation)
        .where(EvalRecommendation.run_id == run.id,
               EvalRecommendation.workspace_id == run.workspace_id)
        .order_by(EvalRecommendation.created_at.desc())
    ))
    for row in rows:
        refresh(db, row, run, workspace)
    return rows


def out(row: EvalRecommendation) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "kind": row.kind,
        "recommendation_id": row.recommendation_id,
        "name": row.name,
        "status": row.status,
        "input_source": row.input_source,
        "system_prompt": row.system_prompt,
        "evaluator": row.evaluator,
        "tools": row.tools or {},
        "skipped_tools": row.skipped_tools or [],
        "result": row.result or {},
        "error": row.error,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
