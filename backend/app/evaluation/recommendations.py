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

A system-prompt recommendation may instead come from a registered 3rd-party
provider (``gepa_lite``): the same reflective pipeline the experiment RECOMMEND
stage runs (the run's own batch-evaluation results joined with each session's
transcript → one Bedrock reflection), executed here on a background thread. Its
row carries a ``gepa-`` pointer instead of an AWS recommendation id; a row left
non-terminal by a restart reads as interrupted.
"""

from __future__ import annotations

import json
import math
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.evaluation import agentcore_eval as ac
from app.evaluation.models import EvalDataset, EvalRecommendation, EvalRun
from app.models.ledger import Agent
from app.services.agentcore import harness as hc
from app.services.agentcore import policy as policy_api
from app.services.agentcore.client import control_client, data_client
from app.services.workspace import WorkspaceContext

KINDS = ("system_prompt", "tool_descriptions")
AGENTCORE_PROVIDER = "agentcore"
PROVIDER_JOB_PREFIX = "provider-"  # recommendation_id of a 3rd-party provider row
# provider jobs running in THIS process; a non-terminal provider row not listed here
# was interrupted (the server restarted while it ran)
_live_provider_jobs: set[str] = set()
_live_provider_lock = threading.Lock()
DEFAULT_EVALUATOR = "Builtin.GoalSuccessRate"
# The devguide's two recommended optimization targets ("Choosing an evaluator"):
# GoalSuccessRate for an agent with a clear task, Helpfulness for an open-ended one.
RECOMMENDED_EVALUATORS = (DEFAULT_EVALUATOR, "Builtin.Helpfulness")
EVALUATOR_READ_WORKERS = 8  # GetEvaluator fan-out when screening the account's judges
GATEWAY_READ_WORKERS = 8  # GetGatewayTarget fan-out (a shared KB gateway has 15+ targets)
# Gateway tool catalogs and the account's evaluator screen barely change, yet reading
# them cost ~5 s on prod (one GetGatewayTarget per target, one GetEvaluator per judge)
# on every card load. The Harness itself is never cached: a prompt edit must show at
# once. Failures are not cached, so a denied read is retried on the next load.
CATALOG_TTL_S = 60.0
_catalog_cache: dict[tuple[str, ...], tuple[float, Any]] = {}
_catalog_lock = threading.Lock()
SYSTEM_PROMPT_MAX = 20000  # SystemPromptText max (service model)
TOOL_DESCRIPTION_MAX = 20000  # ToolDescriptionText max
TOOL_NAME_MAX = 256  # RecommendationToolName max
_BUILTIN_PREFIX = "Builtin."
SPANS_MAX = 20000  # Spans list max (service model)
# Logs Insights allows 30 concurrent queries per account; leave room for the console
SPAN_QUERY_BATCH = 10
SPAN_LOOKBACK_MARGIN_H = 48  # a run's sessions ran shortly before its row was written
SPAN_LOOKBACK_MAX_H = 24 * 90


def _cached(key: tuple[str, ...], load: Any) -> Any:
    """``load()`` memoized for ``CATALOG_TTL_S``; an exception is never stored."""
    with _catalog_lock:
        hit = _catalog_cache.get(key)
        if hit and time.monotonic() - hit[0] < CATALOG_TTL_S:
            return hit[1]
    value = load()
    with _catalog_lock:
        _catalog_cache[key] = (time.monotonic(), value)
    return value


def clear_catalog_cache() -> None:
    with _catalog_lock:
        _catalog_cache.clear()


def _workspace_key(workspace: WorkspaceContext) -> tuple[str, ...]:
    return (str(workspace.id), str(workspace.account_id), str(workspace.region))


def _gateway_actions(
    control: Any, workspace: WorkspaceContext, gateway_id: str
) -> list[dict[str, Any]]:
    """The Gateway's tools (``<target>___<tool>`` + description), targets read in
    parallel and the catalog cached per workspace + gateway."""
    from app.services.governance import discover_actions

    def load() -> list[dict[str, Any]]:
        summaries = policy_api.list_gateway_targets(control, gateway_id)
        with ThreadPoolExecutor(max_workers=GATEWAY_READ_WORKERS) as pool:
            details = list(pool.map(
                lambda t: policy_api.get_gateway_target(control, gateway_id, t["targetId"]),
                summaries,
            ))
        return discover_actions(details)

    return _cached(("gateway", *_workspace_key(workspace), gateway_id), load)


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
                actions = _gateway_actions(control, workspace, gateway_id)
            except Exception as exc:
                notes.append({"code": "gateway_unreadable", "tool": alias,
                              "detail": f"{type(exc).__name__}: {exc}"[:300]})
                continue
            for action in actions:
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


def _evaluator_problem(
    evaluator: str, detail: dict[str, Any] | None, gap: dict[str, list[str]],
) -> str | None:
    """Why an evaluator cannot be a recommendation's optimization signal, or None.

    The job pushes the prompt toward whatever the evaluator scores HIGH and needs a
    numeric score from traces alone (no dataset ground truth reaches it):

    - ``lower_is_better`` — Harmfulness / Refusal / Bias …: optimizing toward a high
      score would make the agent worse;
    - ``ground_truth`` — trajectory matchers, judges whose instructions read
      ``{expected_response}`` & friends, managed code evaluators reading references;
    - ``categorical`` — a judge with a categorical rating scale gives no numeric
      signal (the devguide requires ``ratingScale.numerical``).
    """
    if ac.evaluator_polarity(evaluator) < 0:
        return "lower_is_better"
    if evaluator in ac.TRAJECTORY_EVALUATORS or evaluator in gap:
        return "ground_truth"
    if detail:
        if ac.ground_truth_placeholders(ac.judge_instructions(detail)):
            return "ground_truth"
        scale = ((detail.get("evaluatorConfig") or {}).get("llmAsAJudge") or {}).get(
            "ratingScale") or {}
        if scale.get("categorical"):
            return "categorical"
    return None


def _account_evaluators(workspace: WorkspaceContext) -> list[dict[str, Any]]:
    """ACTIVE non-built-in evaluators, each custom one read back with GetEvaluator
    (ListEvaluators carries no config), cached per workspace. Unreadable judge →
    listed without detail: the start-time check still has the last word."""
    try:
        return _cached(("evaluators", *_workspace_key(workspace)),
                       lambda: _read_account_evaluators(workspace))
    except Exception:
        return []  # listing unavailable — built-ins still render (and nothing is cached)


def _read_account_evaluators(workspace: WorkspaceContext) -> list[dict[str, Any]]:
    control = control_client(workspace)
    listed = [
        e for e in ac.list_evaluators(control)
        if not str(e.get("evaluatorId", "")).startswith(_BUILTIN_PREFIX)
        and e.get("status", "ACTIVE") == "ACTIVE"
    ]

    def read(entry: dict[str, Any]) -> dict[str, Any]:
        evaluator_id = str(entry["evaluatorId"])
        detail = None
        if entry.get("evaluatorType") != "ThirdParty":  # managed: no config to screen
            try:
                detail = ac.get_evaluator(control, evaluator_id=evaluator_id)
            except Exception:
                detail = None
        return {**entry, "detail": detail}

    with ThreadPoolExecutor(max_workers=EVALUATOR_READ_WORKERS) as pool:
        return list(pool.map(read, listed))


def evaluator_options(
    db: Session, run: EvalRun, workspace: WorkspaceContext
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """``(options, excluded)`` for a system-prompt recommendation's target evaluator.

    Every evaluator the job can actually optimize toward, grouped: the run's own
    first (what the operator just read scores for), then AWS built-ins, then
    third-party managed evaluators, then this account's custom ones. The devguide's
    two recommended targets are flagged. ``excluded`` names what was left out and
    why, so the console can say so instead of silently shortening the list.
    """
    from app.assistant.evaluation_assets import managed_reference_gap

    account = _account_evaluators(workspace)
    custom_ids = [str(e["evaluatorId"]) for e in account if e.get("evaluatorType") != "ThirdParty"]
    gap = managed_reference_gap(db, run.workspace_id, custom_ids, set()) if custom_ids else {}
    candidates: dict[str, dict[str, Any]] = {}
    for evaluator, level in {**ac.ALL_BUILTIN_EVALUATORS, **ac.TRAJECTORY_EVALUATORS}.items():
        candidates[evaluator] = {"id": evaluator, "name": evaluator, "level": level,
                                 "group": "builtin", "detail": None}
    for entry in account:
        evaluator = str(entry["evaluatorId"])
        candidates[evaluator] = {
            "id": evaluator, "name": entry.get("evaluatorName") or evaluator,
            "level": entry.get("level"), "detail": entry.get("detail"),
            "group": "third_party" if entry.get("evaluatorType") == "ThirdParty" else "custom",
        }
    own = run.evaluators if run.mode == "evaluators" else []
    options: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    for evaluator in own:
        if evaluator not in candidates:  # deleted since the run, or another region's
            excluded.append({"id": evaluator, "reason": "unavailable"})
    ordered = [
        *[e for e in own if e in candidates],
        *[e for e in RECOMMENDED_EVALUATORS if e not in own],
        *[e for e in candidates if e not in own and e not in RECOMMENDED_EVALUATORS],
    ]
    for evaluator in dict.fromkeys(ordered):
        item = candidates[evaluator]
        problem = _evaluator_problem(evaluator, item["detail"], gap)
        if problem:
            excluded.append({"id": evaluator, "reason": problem})
            continue
        options.append({
            "id": evaluator, "name": item["name"], "level": item["level"],
            "group": "run" if evaluator in own else item["group"],
            "recommended": evaluator in RECOMMENDED_EVALUATORS,
        })
    return options, excluded


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
    options, excluded = evaluator_options(db, run, workspace)
    return {
        **inputs,
        "agent_method": agent.method if agent is not None else None,
        "evaluators": options,
        "excluded_evaluators": excluded,
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
_PROBLEM_TEXT = {
    "lower_is_better": "scores a penalty (lower is better), so optimizing toward a high "
                       "score would make the agent worse",
    "ground_truth": "needs dataset ground truth, which a recommendation's traces do not "
                    "carry",
    "categorical": "uses a categorical rating scale; a recommendation needs a numerical "
                   "score as its optimization signal",
}


def _evaluator_arn(db: Session, evaluator: str, workspace: WorkspaceContext) -> str:
    """An evaluator id → the ARN the job takes, refusing any the options would hide.

    Built-ins have a region-less ARN. Anything else is read back: GetEvaluator both
    proves it exists here and exposes its config for the same screening the option
    list applies, so the API cannot start a job the console would not offer.
    """
    from app.assistant.evaluation_assets import managed_reference_gap

    if evaluator.startswith("arn:"):
        return evaluator
    detail = None
    gap: dict[str, list[str]] = {}
    if not evaluator.startswith(_BUILTIN_PREFIX):
        try:
            detail = ac.get_evaluator(control_client(workspace), evaluator_id=evaluator)
        except Exception as exc:
            raise AppError(
                "recommendation.evaluator_unreadable",
                f"evaluator {evaluator} could not be read from AWS",
                {"aws_error": f"{type(exc).__name__}: {exc}"},
                status_code=400,
            ) from exc
        gap = managed_reference_gap(db, None, [evaluator], set())
    problem = _evaluator_problem(evaluator, detail, gap)
    if problem:
        raise AppError(
            f"recommendation.evaluator_{problem}",
            f"{evaluator} cannot be a recommendation's optimization target: it "
            f"{_PROBLEM_TEXT[problem]}",
            {"evaluator": evaluator, "reason": problem},
            status_code=422,
        )
    if evaluator.startswith(_BUILTIN_PREFIX):
        return f"arn:aws:bedrock-agentcore:::evaluator/{evaluator}"
    arn = detail.get("evaluatorArn") if detail else None
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


def adversarial_sessions(db: Session, run: EvalRun) -> list[dict[str, str]]:
    """The run's sessions whose scenario is a reviewed adversarial golden test.

    AgentCore Recommendations refuses traces that contain prompt-injection content
    (``ValidationException`` … "flagged by our safety filters as a potential prompt
    attack"), and a red-team scenario puts exactly that content in its trace. Sessions
    pair with the local Dataset's scenarios by position (``execute_run``); when the
    Dataset no longer has one scenario per session, nothing can be re-paired and
    nothing is excluded.
    """
    from app.evaluation.scenarios import normalize_scenarios

    sessions = list(run.session_ids or [])
    if not run.dataset_id or not sessions:
        return []
    dataset = db.get(EvalDataset, run.dataset_id)
    if dataset is None or (run.workspace_id and dataset.workspace_id != run.workspace_id):
        return []
    scenarios = normalize_scenarios(list(dataset.items or []))
    if len(scenarios) != len(sessions):
        return []
    out: list[dict[str, str]] = []
    for scenario, session_id in zip(scenarios, sessions, strict=True):
        assets = (scenario.get("metadata") or {}).get("launchpad_assets") or {}
        if (assets.get("golden_test") or {}).get("adversarial") is True:
            out.append({"session_id": str(session_id),
                        "scenario_id": str(scenario.get("scenario_id") or "")})
    return out


def run_spans(
    run: EvalRun, workspace: WorkspaceContext, *, exclude: set[str] | None = None,
) -> list[dict[str, Any]]:
    """The raw span documents of the run's sessions (the on-demand evaluation query),
    minus the ``exclude``d session ids."""
    from app.services.observability import q_session_spans, run_insights_queries

    skip = exclude or set()
    sessions = [sid for sid in dict.fromkeys(run.session_ids or []) if sid not in skip]
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
    provider: str | None = None,
    model_id: str | None = None,
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
    third_party = bool(provider) and provider != AGENTCORE_PROVIDER
    if third_party and "system_prompt" in kinds:
        provider_source = _provider_preflight(run, workspace, provider)
    # every AWS read that can refuse happens before the first Start, so a refusal
    # never leaves one kind running and the other unstarted
    evaluator_id = evaluator or DEFAULT_EVALUATOR
    evaluator_arn = batch_arn = ""
    excluded: list[dict[str, str]] = []
    prompt_spans: list[dict[str, Any]] = []
    if "system_prompt" in kinds and not third_party:
        evaluator_arn = _evaluator_arn(db, evaluator_id, workspace)
        excluded = adversarial_sessions(db, run)
        if excluded and len(excluded) == len(set(run.session_ids or [])):
            raise AppError(
                "recommendation.only_adversarial_sessions",
                "every session of this run is an adversarial test — AgentCore "
                "Recommendations refuses prompt-injection traces; pick a run with "
                "ordinary scenarios",
                {"excluded": excluded}, status_code=409,
            )
        if excluded:
            # the run's other sessions, inline: a batch reference cannot leave one out
            prompt_spans = run_spans(run, workspace,
                                     exclude={e["session_id"] for e in excluded})
        else:
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
        if kind == "system_prompt" and third_party:
            created.append(_start_provider_job(
                db, run, workspace, provider=str(provider), model_id=model_id,
                prompt=prompt, input_source=input_source, source=provider_source,
            ))
            continue
        if kind == "system_prompt":
            name = _job_name(run.id, kind)
            started = ac.start_system_prompt_recommendation(
                data, name=name, system_prompt=prompt,
                batch_evaluation_arn=batch_arn or None,
                session_spans=prompt_spans or None, evaluator_arn=evaluator_arn,
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
            result=({"excluded_sessions": excluded}
                    if kind == "system_prompt" and excluded else {}),
        )
        db.add(row)
        db.commit()
        created.append(row)
    return created


def _provider_preflight(run: EvalRun, workspace: WorkspaceContext, provider: str) -> dict[str, Any]:
    """Validate a 3rd-party provider request before anything starts; returns the
    run's pinned trace source (its batch's results stream — the provider's evidence)."""
    from app.optimization import providers as rec_providers
    from app.optimization import service as opt_service

    if provider not in rec_providers.PROVIDER_IDS:
        raise AppError("recommendation.provider_unknown", f"unknown provider {provider!r}",
                       {"providers": list(rec_providers.PROVIDER_IDS)}, status_code=422)
    if not run.agent_id:
        # the evidence joins each session's transcript through the agent's telemetry
        raise AppError(
            "recommendation.provider_needs_agent",
            "a 3rd-party provider reflects on a platform agent's sessions — this run has "
            "no agent", status_code=422,
        )
    source = opt_service.resolve_recommend_source(run.agent_id, run.id, workspace)
    if not (source.get("results_log_group") and source.get("results_log_stream")):
        raise AppError(
            "recommendation.provider_no_results",
            "the run's batch evaluation has no results log stream for the provider to read",
            status_code=409,
        )
    return source


def _spawn(fn: Any) -> None:
    """Run ``fn`` on a daemon thread (tests replace this to run inline)."""
    threading.Thread(target=fn, name="run-rec-provider", daemon=True).start()


def _start_provider_job(
    db: Session, run: EvalRun, workspace: WorkspaceContext, *, provider: str,
    model_id: str | None, prompt: str, input_source: str, source: dict[str, Any],
) -> EvalRecommendation:
    from app.optimization import providers as rec_providers

    prov = rec_providers.get_provider(provider)
    model = (model_id or "").strip() or prov.default_model_id() or ""
    row = EvalRecommendation(
        workspace_id=run.workspace_id, run_id=run.id, kind="system_prompt",
        recommendation_id=f"{PROVIDER_JOB_PREFIX}{prov.id}-{uuid.uuid4().hex[:12]}",
        name=f"{prov.id} · {model}", status="IN_PROGRESS", input_source=input_source,
        system_prompt=prompt, evaluator=None, tools={}, skipped_tools=[],
        result={"provider": prov.id, "provider_model_id": model},
    )
    db.add(row)
    db.commit()
    row_id, agent_id = row.id, str(run.agent_id)
    with _live_provider_lock:
        _live_provider_jobs.add(row_id)
    _spawn(lambda: _run_provider_job(row_id, agent_id, workspace, prov.id, model, prompt, source))
    return row


def _run_provider_job(
    row_id: str, agent_id: str, workspace: WorkspaceContext, provider: str, model: str,
    prompt: str, source: dict[str, Any],
) -> None:
    """The provider's reflection, start to finish; its outcome lands on the row."""
    from app.core.db import SessionLocal
    from app.optimization import service as opt_service

    def note(message: str) -> None:
        db = SessionLocal()
        try:
            row = db.get(EvalRecommendation, row_id)
            if row is not None and row.status not in ac.REC_TERMINAL:
                row.result = {**(row.result or {}), "progress": message[:300]}
                db.commit()
        finally:
            db.close()

    try:
        db = SessionLocal()
        try:
            agent = db.get(Agent, agent_id)
            meta = {"id": agent_id, "name": getattr(agent, "name", ""),
                    "method": getattr(agent, "method", ""), "system_prompt": prompt}
        finally:
            db.close()
        out_keys = opt_service._third_party_prompt_recommendation(
            "", meta, workspace, note, provider_id=provider, model_id=model, source=source,
        )
        status = str(out_keys.get("system_prompt_status") or "FAILED")
        text = str(out_keys.get("recommended_prompt") or "")
        result = {"provider": provider, "provider_model_id": model,
                  "provider_meta": out_keys.get("provider_meta") or {}}
        error = None
        if status == "COMPLETED" and text:
            result.update(recommended_prompt=text, explanation=out_keys.get("explanation") or "")
        else:
            status = "FAILED"
            error = str(out_keys.get("system_prompt_error") or "the provider produced no prompt")
    except Exception as exc:  # the row must always reach a terminal state
        status, error = "FAILED", f"{type(exc).__name__}: {exc}"[:1000]
        result = {"provider": provider, "provider_model_id": model}
    db = SessionLocal()
    try:
        row = db.get(EvalRecommendation, row_id)
        if row is not None:
            row.status, row.result, row.error = status, result, error
            db.commit()
    finally:
        db.close()
        # only once the outcome is durable — a read in between must not see an orphan
        with _live_provider_lock:
            _live_provider_jobs.discard(row_id)


# ─── refresh / read ─────────────────────────────────────────────────────────
def _apply(row: EvalRecommendation, detail: dict[str, Any]) -> None:
    status = str(detail.get("status") or row.status)
    result = detail.get("recommendationResult") or {}
    row.error = None  # a stale-read note from an earlier refresh no longer applies
    if row.kind == "system_prompt":
        payload = result.get("systemPromptRecommendationResult") or {}
        text = payload.get("recommendedSystemPrompt") or ""
        if status == "COMPLETED" and text:
            # which sessions were left out is part of the record, not AWS output
            kept = {k: v for k, v in (row.result or {}).items() if k == "excluded_sessions"}
            row.result = {**kept, "recommended_prompt": text,
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
    if row.recommendation_id.startswith(PROVIDER_JOB_PREFIX):
        with _live_provider_lock:
            live = row.id in _live_provider_jobs
        if not live:  # its thread died with a previous server process
            row.status = "FAILED"
            row.error = "the recommendation was interrupted (the server restarted while it ran)"
            db.commit()
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


def acceptance_target(
    db: Session, run: EvalRun, rec_id: str
) -> tuple[EvalRecommendation, Agent, str]:
    """Validate accepting a recommendation into a new Harness version.

    Only a COMPLETED system-prompt recommendation of a run whose agent is a live,
    platform-deployed Harness qualifies, and only once: a tool-description
    recommendation revises Gateway tool schemas the Harness version does not own.
    Returns ``(row, agent, recommended_prompt)``; the caller re-publishes."""
    row = db.get(EvalRecommendation, rec_id)
    if row is None or row.run_id != run.id or row.workspace_id != run.workspace_id:
        raise NotFoundError("recommendation.not_found", "recommendation not found")
    if row.kind != "system_prompt":
        raise AppError(
            "recommendation.accept_kind",
            "only a system-prompt recommendation can be accepted into a Harness version",
            status_code=400,
        )
    prompt = str((row.result or {}).get("recommended_prompt") or "").strip()
    if row.status != "COMPLETED" or not prompt:
        raise AppError(
            "recommendation.not_completed",
            "the recommendation has not completed with a recommended prompt",
            {"status": row.status}, status_code=409,
        )
    if row.accepted:
        raise AppError(
            "recommendation.already_accepted",
            "this recommendation was already accepted",
            {"accepted": row.accepted}, status_code=409,
        )
    agent = db.get(Agent, run.agent_id) if run.agent_id else None
    if agent is None or agent.workspace_id != run.workspace_id or agent.status == "deleted":
        raise NotFoundError("agent.not_found", "the run's agent no longer exists")
    if agent.method != "harness":
        raise AppError(
            "recommendation.accept_not_harness",
            "accepting a recommendation publishes a new Harness version — "
            "this run's agent is not a platform-deployed Harness",
            {"method": agent.method}, status_code=400,
        )
    if agent.status != "active":
        raise AppError(
            "recommendation.agent_not_active",
            "the agent must be active before a new version can be published",
            {"status": agent.status}, status_code=409,
        )
    if len(prompt) > SYSTEM_PROMPT_MAX:
        raise AppError(
            "recommendation.prompt_too_long",
            f"the recommended prompt exceeds {SYSTEM_PROMPT_MAX} characters",
            status_code=400,
        )
    return row, agent, prompt


def record_acceptance(
    db: Session, row: EvalRecommendation, *, by: str, agent_id: str,
    previous_version: str | None, job_id: str, deployment_id: str, edited: bool = False,
) -> None:
    row.accepted = {
        "edited": edited,
        "by": by,
        "at": datetime.now(UTC).isoformat(),
        "agent_id": agent_id,
        "previous_version": previous_version,
        "job_id": job_id,
        "deployment_id": deployment_id,
    }
    db.commit()


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
        "accepted": row.accepted,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
