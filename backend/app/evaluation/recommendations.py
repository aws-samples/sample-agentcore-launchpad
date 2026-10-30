"""Evaluation-task recommendations — a provider job scoped to one run's sessions (N12 E2-4).

A completed evaluation run already names the exact sessions it judged and holds their
scores in ``eval_results``, so a recommendation started from it reflects on those
judgements — never a rolling window — and optimizes toward an evaluator the operator
just looked at.

Inputs (the current system prompt / tool descriptions the provider revises) come from the
Launchpad spec when it carries a prompt / discoverable tools, else from the operator
(the console then requires them to be typed).

Providers (``optimization/providers``): ``gepa_lite`` (default — one reflective round) and
``harness_optimizer`` (Strands contrastive reflection, disabled by default). Both run on
the evaluation worker: :func:`start` collects the evidence on a background thread, builds
the provider's job spec and submits it through ``executor.select_executor`` (CA-8); the
row keeps the job id as its ``recommendation_id`` (``provider-<id>-<hex12>``) and every
read of a non-terminal row polls the executor (``status.json`` / ``done.json``), so a
restart loses nothing a running worker has written.

"""

from __future__ import annotations

import re
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError, NotFoundError
from app.evaluation import jobs
from app.evaluation.executor import assert_executor_ready, select_executor
from app.evaluation.jobs import JobDone, JobSpec
from app.evaluation.judge_budget import judge_budget, judge_budget_cap
from app.evaluation.models import (
    EvalDataset,
    EvalEvaluator,
    EvalRecommendation,
    EvalResult,
    EvalRun,
)
from app.evaluation.online_evaluators import (
    evaluator_polarity,
    ground_truth_placeholders,
    is_categorical_scale,
)
from app.evaluation.online_scheduler import evaluator_defs
from app.evaluation.scenarios import SPLIT_HELD_OUT, SPLIT_TUNING, select_split, split_counts
from app.models.ledger import Agent
from app.optimization import bundles
from app.optimization import providers as rec_providers
from app.optimization.models import ConfigBundle
from app.optimization.providers import evidence as rec_evidence
from app.optimization.providers.base import (
    EvidenceStats,
    OptimizeRequest,
    PromptOptimizationProvider,
    SessionEvidence,
)
from app.schemas import model_catalog
from app.schemas.agent import (
    ModelRefusal,
    allowed_model_ids,
    assert_model_provider_configured,
    assert_model_visible,
    hub_model_refusal_code,
)
from app.services.strands_eval.evaluators import BUILTIN_LEVELS, LEVELS, TRAJECTORY_LEVELS
from app.services.workspace import WorkspaceContext
from app.templates.toolkits import toolkit_tool_descriptions

KINDS = ("system_prompt", "tool_descriptions")
PROVIDER_JOB_PREFIX = "provider-"  # recommendation_id (== worker job id) of every row
REC_TERMINAL = {"COMPLETED", "FAILED"}
# provider jobs submitted by THIS process; a non-terminal row that is neither here nor
# known to the executor was interrupted (the server restarted before the worker started)
_live_provider_jobs: set[str] = set()
_live_provider_lock = threading.Lock()
DEFAULT_EVALUATOR = "Builtin.GoalSuccessRate"
# The devguide's two recommended optimization targets ("Choosing an evaluator"):
# GoalSuccessRate for an agent with a clear task, Helpfulness for an open-ended one.
RECOMMENDED_EVALUATORS = (DEFAULT_EVALUATOR, "Builtin.Helpfulness")
SYSTEM_PROMPT_MAX = 20000
TOOL_DESCRIPTION_MAX = 20000
TOOL_NAME_MAX = 256
PROMPT_MAX_CHARS = 8000  # the revised prompt budget the reflection is held to
_BUILTIN_PREFIX = "Builtin."
POLL_SECONDS = 5.0

Progress = Callable[[str], None]


def _now() -> datetime:
    return datetime.now(UTC)


# ─── input resolution ───────────────────────────────────────────────────────
# Methods whose deployed runtime takes its system prompt from the Launchpad spec, so a
# re-publish with a revised prompt (``republish_agent`` → update deploy) actually changes
# what the agent runs: the Harness template (A1 ``system_prompt=DEFAULT_SYSTEM_PROMPT``) and
# the Strands zip template (``__LAUNCHPAD_SYSTEM_PROMPT__``). A ``studio`` artifact and a
# ``byoc`` image carry their own prompt in member code; a discovered runtime is external.
PROMPT_REPUBLISHABLE_METHODS: frozenset[str] = frozenset({"harness", "zip_runtime"})


def prompt_accept_ineligibility(agent: Agent | None) -> str | None:
    """Why accepting a system-prompt recommendation cannot re-publish ``agent``, or None.

    N12-obs44: the ONE predicate behind both the backend accept route and the console's
    ACCEPT button — it mirrors the refusals ``republish_agent`` would raise after the
    method check (system preset, converted/baked code) so the button is never shown for
    an accept that the redeploy guards would then reject.

    ``agent_missing`` · ``method`` (studio / byoc / discovered) · ``system_preset`` ·
    ``converted_locked`` (a converted agent's prompt is baked into its exported code).
    """
    if agent is None:
        return "agent_missing"
    if agent.method not in PROMPT_REPUBLISHABLE_METHODS:
        return "method"
    if getattr(agent, "system_key", None):
        return "system_preset"
    if (agent.spec or {}).get("code_bundle"):
        return "converted_locked"
    return None


_TOOL_DEF_RE = re.compile(
    r"@tool\s*\ndef\s+(\w+)\s*\([^)]*\)[^:]*:\s*\n\s+(?:\"\"\"|''')([\s\S]*?)(?:\"\"\"|''')"
)


def discover_agent_tools(spec: dict[str, Any]) -> dict[str, str]:
    """toolName → current description, from the agent's own spec.

    Sources: registry tool attachments (spec.tools), platform toolkits
    (spec.toolkits) and `@tool` docstrings in the agent's code / code bundle.
    Gateway-served tools (KB targets, MCP) only exist at runtime and can't be
    discovered here — the recommend UI lets the user add those by hand.

    Toolkits are read from the registry, NOT from the emitted source: a template
    agent's `spec.code`/`code_bundle` are `None` by design, so the docstring regex
    below can never see them. The registry derives the same names + descriptions
    the template renders from, so the two cannot drift.

    A gateway/mcp ToolRef names a **server, not a tool**, and is skipped for the
    same reason. Its tools arrive namespaced (`hr-database___get_employee`) and only
    at runtime, so the bare server name can never appear in observed telemetry
    (measured live on an agent carrying both a toolkit and a gateway attachment).
    Builtin ToolRefs stay: their names ARE what the model calls.
    """
    tools: dict[str, str] = {}
    for entry in spec.get("tools") or []:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        if entry.get("type") in ("gateway", "mcp"):
            continue
        tools[str(entry["name"])] = str(
            entry.get("description") or entry.get("desc") or ""
        )
    # update, not setdefault: a toolkit owns its description more authoritatively
    # than a same-named registry attachment would.
    tools.update(toolkit_tool_descriptions(list(spec.get("toolkits") or [])))
    sources = [spec.get("code")] if isinstance(spec.get("code"), str) else []
    bundle = spec.get("code_bundle")
    if isinstance(bundle, dict):
        sources += [s for s in bundle.values() if isinstance(s, str)]
    for src in sources:
        for name, doc in _TOOL_DEF_RE.findall(src or ""):
            # docstring summary only — Args/Returns sections are signature
            # docs, not part of the description contract
            summary = re.split(r"\n\s*\n|\n\s*(?:Args|Returns|Raises):", doc)[0]
            tools.setdefault(name, " ".join(summary.split())[:500])
    tools.update({
        str(name): str(description)
        for name, description in (spec.get("tool_description_overrides") or {}).items()
    })
    return tools


def _spec_inputs(agent: Agent) -> dict[str, Any]:

    spec = agent.spec or {}
    prompt = spec.get("system_prompt") if isinstance(spec.get("system_prompt"), str) else ""
    tools = [
        {"name": name, "description": description, "origin": "spec"}
        for name, description in discover_agent_tools(spec).items()
    ]
    source = "spec" if (prompt or tools) else "manual"
    return {"source": source, "system_prompt": prompt or "", "tools": tools, "notes": []}


def _custom_evaluators(db: Session, workspace_id: str | None) -> dict[str, EvalEvaluator]:
    """ACTIVE custom evaluators of the workspace (``eval_evaluators`` rows)."""
    rows = db.scalars(select(EvalEvaluator).where(
        EvalEvaluator.workspace_id == workspace_id, EvalEvaluator.status == "ACTIVE"))
    return {row.id: row for row in rows}


def _evaluator_problem(evaluator: str, row: EvalEvaluator | None) -> str | None:
    """Why an evaluator cannot be a recommendation's optimization signal, or None.

    The provider pushes the prompt toward whatever the evaluator scores HIGH and needs a
    numeric score from traces alone (no dataset ground truth reaches it):

    - ``lower_is_better`` — Refusal: optimizing toward a high score would make the
      agent worse;
    - ``ground_truth`` — trajectory matchers, judges whose instructions read
      ``{expected_response}`` & friends;
    - ``categorical`` — a judge whose rating scale carries no numeric values gives no
      signal.
    """
    if evaluator_polarity(evaluator) < 0:
        return "lower_is_better"
    if evaluator in TRAJECTORY_LEVELS:
        return "ground_truth"
    if row is not None and row.definition == "judge":
        if ground_truth_placeholders(row.instructions):
            return "ground_truth"
        if is_categorical_scale(row.rating_scale):
            return "categorical"
    return None


def evaluator_options(
    db: Session, run: EvalRun, workspace: WorkspaceContext
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """``(options, excluded)`` for a system-prompt recommendation's target evaluator.

    Every evaluator the provider can actually optimize toward, grouped: the run's own
    first (what the operator just read scores for), then the built-ins, then this
    workspace's custom ones. The devguide's two recommended targets are flagged.
    ``excluded`` names what was left out and why, so the console can say so instead
    of silently shortening the list.
    """
    custom = _custom_evaluators(db, run.workspace_id)
    candidates: dict[str, dict[str, Any]] = {}
    for evaluator, level in LEVELS.items():
        candidates[evaluator] = {"id": evaluator, "name": evaluator, "level": level,
                                 "group": "builtin", "row": None}
    for evaluator, row in custom.items():
        candidates[evaluator] = {"id": evaluator, "name": row.name or evaluator,
                                 "level": row.level, "group": "custom", "row": row}
    own = run.evaluators if run.mode == "evaluators" else []
    options: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    for evaluator in own:
        if evaluator not in candidates:  # deleted since the run
            excluded.append({"id": evaluator, "reason": "unavailable"})
    ordered = [
        *[e for e in own if e in candidates],
        *[e for e in RECOMMENDED_EVALUATORS if e not in own],
        *[e for e in candidates if e not in own and e not in RECOMMENDED_EVALUATORS],
    ]
    for evaluator in dict.fromkeys(ordered):
        item = candidates[evaluator]
        problem = _evaluator_problem(evaluator, item["row"])
        if problem:
            excluded.append({"id": evaluator, "reason": problem})
            continue
        options.append({
            "id": evaluator, "name": item["name"], "level": item["level"],
            "group": "run" if evaluator in own else item["group"],
            "recommended": evaluator in RECOMMENDED_EVALUATORS,
        })
    return options, excluded


def _result_count(db: Session, run: EvalRun) -> int:
    return len(db.scalars(select(EvalResult.id).where(
        EvalResult.source == "run", EvalResult.run_id == run.id)).all())


def eligibility(db: Session, run: EvalRun) -> dict[str, Any]:
    """A completed run with at least one ``eval_results`` row can seed a recommendation.
    ``run_no_batch`` keeps the baseline reason code the console translates ("no results
    to analyze")."""
    if run.status != "completed":
        return {"eligible": False, "reason_code": "run_not_completed",
                "tools_eligible": False}
    if _result_count(db, run) == 0:
        return {"eligible": False, "reason_code": "run_no_batch", "tools_eligible": False}
    # tool evidence is read per session, so it needs the session list; a time-window run
    # evaluated traffic it never recorded session by session
    return {"eligible": True, "reason_code": None, "tools_eligible": bool(run.session_ids)}


def resolve_inputs(
    db: Session, run: EvalRun, workspace: WorkspaceContext, *, agent_visible: bool = True
) -> dict[str, Any]:
    """``agent_visible=False`` (the run's agent is hidden from the caller by workspace
    isolation) reads like an agent that is gone: nothing of its prompt or tools is
    disclosed, the operator enters them."""
    agent = db.get(Agent, run.agent_id) if run.agent_id and agent_visible else None
    if agent is not None and agent.workspace_id == run.workspace_id:
        inputs = _spec_inputs(agent)
    else:
        # a CloudWatch-sourced run, or its agent is gone: nothing to read from
        inputs = {"source": "manual", "system_prompt": "", "tools": [], "notes": []}
    options, excluded = evaluator_options(db, run, workspace)
    # N12-obs44 (CN additive): whether ACCEPT may re-publish this run's agent, decided by
    # the same predicate ``acceptance_target`` enforces — the console gates the button on it
    scoped_agent = agent if agent is not None and agent.workspace_id == run.workspace_id else None
    ineligible = prompt_accept_ineligibility(scoped_agent)
    return {
        **inputs,
        "agent_method": agent.method if agent is not None else None,
        "accept_eligible": ineligible is None,
        "accept_ineligible_reason": ineligible,
        "evaluators": options,
        "excluded_evaluators": excluded,
        "default_evaluator": DEFAULT_EVALUATOR,
        **eligibility(db, run),
    }


# ─── start ──────────────────────────────────────────────────────────────────
_PROBLEM_TEXT = {
    "lower_is_better": "scores a penalty (lower is better), so optimizing toward a high "
                       "score would make the agent worse",
    "ground_truth": "needs dataset ground truth, which a recommendation's traces do not "
                    "carry",
    "categorical": "uses a categorical rating scale; a recommendation needs a numerical "
                   "score as its optimization signal",
}


def _check_evaluator(db: Session, run: EvalRun, evaluator: str) -> str:
    """An evaluator id the job may optimize toward, refusing any the options would hide."""
    row = None
    if not evaluator.startswith(_BUILTIN_PREFIX):
        row = _custom_evaluators(db, run.workspace_id).get(evaluator)
        if row is None:
            raise AppError(
                "recommendation.evaluator_unknown",
                f"evaluator {evaluator} is not a custom evaluator of this workspace",
                {"evaluator": evaluator}, status_code=400,
            )
    elif evaluator not in BUILTIN_LEVELS and evaluator not in TRAJECTORY_LEVELS:
        raise AppError("recommendation.evaluator_unknown",
                       f"unknown built-in evaluator {evaluator}",
                       {"evaluator": evaluator}, status_code=400)
    problem = _evaluator_problem(evaluator, row)
    if problem:
        raise AppError(
            f"recommendation.evaluator_{problem}",
            f"{evaluator} cannot be a recommendation's optimization target: it "
            f"{_PROBLEM_TEXT[problem]}",
            {"evaluator": evaluator, "reason": problem},
            status_code=422,
        )
    return evaluator


def _provider(provider_id: str | None) -> PromptOptimizationProvider:
    try:
        prov = rec_providers.get_provider(provider_id)
    except ValueError:
        raise AppError("recommendation.provider_unknown", f"unknown provider {provider_id!r}",
                       {"providers": list(rec_providers.PROVIDER_IDS)},
                       status_code=422) from None
    if not rec_providers.provider_enabled(prov):
        raise AppError(
            "recommendation.provider_disabled",
            f"the {prov.id} provider is disabled in this workspace "
            "(Settings.prompt_opt_harness_optimizer_enabled)",
            {"provider": prov.id}, status_code=422,
        )
    return prov


def _model_for(prov: PromptOptimizationProvider, model_id: str | None,
               resources: dict[str, Any] | None = None,
               principal: model_catalog.Principal | None = None) -> str:
    model = (model_id or "").strip() or prov.default_model_id() or ""
    allowed = allowed_model_ids()  # D21: the registry snapshot, read per call
    if model not in allowed:
        raise AppError(
            "model.not_allowed",
            f"model_id {model!r} is not available in this build — allowed: "
            f"{', '.join(allowed)}",
            {"model_id": model}, status_code=422,
        )
    # N17-obs227: another member's personal provider's model is refused first (422
    # `models.model_not_visible`) — the reflection would spend that member's key; then
    # N-R6-obs1: the optimizer runs on the hub / worker with the provider's key — refuse a
    # provider without one here, not after the job was queued
    try:
        assert_model_visible(model, principal, "model_id")
        assert_model_provider_configured(model, "model_id", resources)
    except ModelRefusal as exc:
        raise AppError(hub_model_refusal_code(exc), exc.reason, {"model_id": model},
                       status_code=422) from exc
    return model


def adversarial_sessions(db: Session, run: EvalRun) -> list[dict[str, str]]:
    """The run's sessions whose scenario is a reviewed red-team (``adversarial``) test.

    Prompt-injection sessions teach the optimizer the wrong lesson (and the Global job
    refused them outright), so a scenario whose golden test is marked ``adversarial``
    stays evaluated but its session is left out of the evidence. Sessions pair with the
    local Dataset's scenarios by the scenario id the run stamped (``scenario_map``), or — an
    older row without it — by position (``execute_run``); when such a Dataset no longer has
    one scenario per session, nothing can be re-paired and nothing is excluded.
    """
    from app.evaluation.scenarios import normalize_scenarios

    sessions = list(run.session_ids or [])
    if not run.dataset_id or not sessions:
        return []
    dataset = db.get(EvalDataset, run.dataset_id)
    if dataset is None or (run.workspace_id and dataset.workspace_id != run.workspace_id):
        return []
    scenarios = normalize_scenarios(list(dataset.items or []))
    mapping = dict(run.scenario_map or {})
    if mapping:
        # D3-d: a split run replayed a SUBSET, so sessions pair by the scenario id the run
        # stamped, not by position
        by_id = {str(s.get("scenario_id") or ""): s for s in scenarios}
        pairs = [(by_id.get(str((mapping.get(sid) or {}).get("scenario_id") or "")), sid)
                 for sid in sessions]
        pairs = [(scenario, sid) for scenario, sid in pairs if scenario is not None]
    elif len(scenarios) == len(sessions):
        pairs = list(zip(scenarios, sessions, strict=True))
    else:
        return []
    out: list[dict[str, str]] = []
    for scenario, session_id in pairs:
        assets = (scenario.get("metadata") or {}).get("launchpad_assets") or {}
        if (assets.get("golden_test") or {}).get("adversarial") is True:
            out.append({"session_id": str(session_id),
                        "scenario_id": str(scenario.get("scenario_id") or "")})
    return out


def held_out_sessions(run: EvalRun) -> list[dict[str, str]]:
    """D3-d / R3-c: the run's sessions that replayed a HELD-OUT scenario. Tuning must never
    read them — a recommendation learnt from the acceptance split would make the acceptance
    verdict measure what was tuned on — so they are left out of the evidence like the
    adversarial ones. The split is the one ``plan_run`` stamped into ``scenario_map`` (a
    later dataset edit does not move a session); older rows carry none and exclude none."""
    mapping = dict(run.scenario_map or {})
    return [{"session_id": str(sid), "scenario_id": str(entry.get("scenario_id") or "")}
            for sid in run.session_ids or []
            if (entry := mapping.get(sid) or {}).get("split") == SPLIT_HELD_OUT]


def held_out_notice(db: Session, run: EvalRun, held_out: list[dict[str, str]]) -> dict | None:
    """D3-d: what the tuning paths left out, stated (never silent) — ``{sessions: the run's
    held-out sessions dropped from the evidence, scenarios: the dataset's held-out scenarios
    the prompt optimizer will not replay, message}``; None when nothing was held out."""
    dataset = db.get(EvalDataset, run.dataset_id) if run.dataset_id else None
    scenarios = (split_counts(list(dataset.items or []))[SPLIT_HELD_OUT]
                 if dataset is not None and dataset.workspace_id == run.workspace_id else 0)
    n = max(len(held_out), scenarios)
    if not n:
        return None
    return {"sessions": len(held_out), "scenarios": scenarios,
            "message": f"{n} held-out scenario{'s' if n != 1 else ''} excluded — the held-out "
                       "split is kept for acceptance runs and never used for tuning"}


def _new_job_id(provider_id: str) -> str:
    return f"{PROVIDER_JOB_PREFIX}{provider_id}-{uuid.uuid4().hex[:12]}"


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
    principal: model_catalog.Principal | None = None,
) -> list[EvalRecommendation]:
    gate = eligibility(db, run)
    if not gate["eligible"]:
        raise AppError(
            f"recommendation.{gate['reason_code']}",
            "only a completed evaluation run with evaluation results can seed a "
            "recommendation",
            {"status": run.status}, status_code=409,
        )
    # validate every requested kind before anything starts — a half-started pair
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
    prov = _provider(provider)
    unsupported = [k for k in kinds if k not in prov.supports]
    if unsupported:
        raise AppError(
            "recommendation.kind_unsupported_by_provider",
            f"the {prov.id} provider does not generate {', '.join(unsupported)}",
            {"provider": prov.id, "kinds": unsupported, "supports": list(prov.supports)},
            status_code=422,
        )
    model = _model_for(prov, model_id, workspace.resources, principal)
    # N17-obs453: only a provider that optimises toward a target takes (and stores) one; a
    # provider that reflects on every evaluator of the run ignores a sent evaluator
    evaluator_id = (evaluator or DEFAULT_EVALUATOR) if rec_providers.takes_target(prov) else None
    if "system_prompt" in kinds and evaluator_id is not None:
        _check_evaluator(db, run, evaluator_id)
    if not run.agent_id:
        # the evidence joins each session's transcript through the agent's telemetry
        raise AppError(
            "recommendation.provider_needs_agent",
            "a recommendation reflects on a platform agent's sessions — this run has no agent",
            status_code=422,
        )
    excluded = adversarial_sessions(db, run)
    if excluded and len(excluded) == len(set(run.session_ids or [])):
        raise AppError(
            "recommendation.only_adversarial_sessions",
            "every session of this run is an adversarial test — prompt-injection traces are "
            "left out of recommendation evidence; pick a run with ordinary scenarios",
            {"excluded": excluded}, status_code=409,
        )
    held_out = [s for s in held_out_sessions(run)
                if s["session_id"] not in {e["session_id"] for e in excluded}]
    if held_out and len(excluded) + len(held_out) == len(set(run.session_ids or [])):
        raise AppError(
            "recommendation.only_held_out_sessions",
            "every usable session of this run replayed a held-out scenario — the held-out "
            "split is for acceptance only and never seeds a recommendation; pick a run on the "
            "tuning split",
            {"held_out": held_out, "excluded": excluded}, status_code=409,
        )
    notice = held_out_notice(db, run, held_out)
    # CA-8: the executor is chosen (and refused: 503 worker_missing / 409 in prod) before
    # any row exists, so the operator sees the refusal at once; N12-bug6: the worker is
    # probed from the preset row now, not from a cached ARN
    executor = select_executor(get_settings(), job_type=prov.job_type)
    assert_executor_ready(executor, workspace)

    created: list[EvalRecommendation] = []
    for kind in KINDS:
        if kind not in kinds:
            continue
        row = EvalRecommendation(
            workspace_id=run.workspace_id, run_id=run.id, kind=kind,
            recommendation_id=_new_job_id(prov.id), name=f"{prov.id} · {model}",
            status="IN_PROGRESS", input_source=input_source,
            system_prompt=prompt if kind == "system_prompt" else None,
            evaluator=evaluator_id if kind == "system_prompt" else None,
            tools=clean_tools if kind == "tool_descriptions" else {}, skipped_tools=[],
            result={"provider": prov.id, "provider_model_id": model,
                    **({"excluded_sessions": excluded} if excluded else {}),
                    **({"held_out_sessions": held_out} if held_out else {}),
                    **({"held_out_excluded": notice} if notice else {})},
        )
        db.add(row)
        db.commit()
        with _live_provider_lock:
            _live_provider_jobs.add(row.id)
        _spawn(lambda row_id=row.id, kind=kind: _run_provider_job(
            row_id, run.id, workspace, prov.id, model, kind, prompt, clean_tools,
            {e["session_id"] for e in excluded + held_out}, executor,
            evaluator_id or DEFAULT_EVALUATOR,
        ))
        created.append(row)
    return created


def _spawn(fn: Any) -> None:
    """Run ``fn`` on a daemon thread (tests replace this to run inline)."""
    threading.Thread(target=fn, name="run-rec-provider", daemon=True).start()


# ─── evidence → job ─────────────────────────────────────────────────────────
def collect_run_evidence(
    db: Session,
    run: EvalRun,
    workspace: WorkspaceContext,
    agent: Agent | None,
    *,
    want_tools: bool,
    exclude: set[str] | None = None,
    progress: Progress | None = None,
    transcript: rec_evidence.TranscriptFn | None = None,
    tool_turns: rec_evidence.ToolTurnsFn | None = None,
    tool_spans: rec_evidence.ToolSpansFn | None = None,
) -> tuple[list[SessionEvidence], EvidenceStats, list[str]]:
    """The run's ``eval_results`` (minus ``exclude``d sessions) joined with transcripts
    (+ tool turns / spans when ``want_tools``), plus the insight feedback lines."""
    settings = get_settings()
    skip = exclude or set()
    records = [r for r in rec_evidence.read_run_records(db, run.id, run.workspace_id)
               if r["session_id"] not in skip]
    extra_feedback = (rec_evidence.insight_feedback(run.insights)
                      if run.mode == "insights" else [])
    if progress:
        progress("collecting evidence from the evaluation run…")
    started_at = run.created_at
    if want_tools:
        tool_turns = tool_turns or rec_evidence.default_tool_turns(workspace, agent, started_at)
        tool_spans = tool_spans or rec_evidence.default_tool_spans(workspace, started_at)
    else:
        tool_turns = tool_spans = None
    evidence, stats = rec_evidence.collect_evidence(
        records=records,
        transcript=transcript or rec_evidence.default_transcript(db, workspace, agent),
        max_sessions=settings.prompt_opt_max_sessions,
        progress=progress, tool_turns=tool_turns, tool_spans=tool_spans,
    )
    return evidence, stats, extra_feedback


def _agent_meta(agent: Agent | None, agent_id: str, prompt: str) -> dict[str, Any]:
    return {"id": agent_id, "name": getattr(agent, "name", ""),
            "method": getattr(agent, "method", ""), "system_prompt": prompt}


def optimize_extras(
    db: Session, run: EvalRun, agent: Agent | None, workspace: WorkspaceContext, job_id: str,
    evaluator_id: str,
) -> dict[str, Any]:
    """What an ``optimize`` job needs beyond the evidence (E2-5): the run's dataset prompts
    to replay, the target runtime, the temporary bundle location and the reward evaluator."""
    from app.evaluation.scenarios import normalize_scenarios

    prompts: list[dict[str, Any]] = []
    dataset = db.get(EvalDataset, run.dataset_id) if run.dataset_id else None
    # D3-d / R3-c: the optimizer replays (and is rewarded on) the TUNING split only — a
    # held-out scenario never reaches it, whatever split the seeding run replayed
    tuning = select_split(list(dataset.items or []), SPLIT_TUNING) if dataset else []
    for scenario in normalize_scenarios(tuning):
        turns = scenario.get("turns") or []
        text = (turns[0].get("input") if turns and isinstance(turns[0], dict)
                else scenario.get("prompt")) or ""
        if str(text).strip():
            prompts.append({"scenario_id": scenario.get("scenario_id"), "prompt": str(text)})
    resource_id = getattr(agent, "resource_id", None) or ""
    settings = get_settings()
    # P3.2: the reward judge is the one that scored the baseline run (an older run without
    # one: the hub setting) — candidates are compared against those scores
    judge = run.judge_model_id or settings.eval_judge_model_id
    return {
        "dataset_prompts": prompts,
        "target": {"runtime_arn": getattr(agent, "arn", None), "agent_id": str(run.agent_id),
                   "resource_id": resource_id},
        "bundle": {"bucket": workspace.resources.get("artifacts_bucket", ""),
                   "agent_id": str(run.agent_id), "bundle_id": f"opt_{job_id.rsplit('-', 1)[-1]}"},
        "evaluator_def": evaluator_defs(db, run.workspace_id, [evaluator_id])[0],
        "log_groups": [f"/aws/bedrock-agentcore/runtimes/{resource_id}-DEFAULT", "aws/spans"],
        "judge_model_id": judge,
        # N-SCN-bug2: model-aware start budget + the cap (CN additive) for the reward judge
        "judge_max_tokens": judge_budget(judge, settings),
        "judge_max_tokens_cap": judge_budget_cap(judge, settings),
    }


def _build_job(
    prov: PromptOptimizationProvider, job_id: str, workspace: WorkspaceContext,
    req: OptimizeRequest, extras: dict[str, Any] | None = None,
) -> JobSpec:
    settings = get_settings()
    return JobSpec(
        job_id=job_id, job_type=prov.job_type,
        workspace={"account_id": workspace.account_id, "region": workspace.region},
        sink={"bucket": workspace.resources.get("artifacts_bucket", ""),
              "prefix": jobs.job_prefix(job_id, 1)},
        spec={**prov.build_spec(req), **(extras or {})},
        deadline_s=min(jobs.DEFAULT_DEADLINE_S, int(settings.eval_job_budget_s)),
    )


def _progress_writer(row_id: str) -> Progress:
    def note(message: str) -> None:
        db = SessionLocal()
        try:
            row = db.get(EvalRecommendation, row_id)
            if row is not None and row.status not in REC_TERMINAL:
                row.result = {**(row.result or {}), "progress": message[:300]}
                db.commit()
        finally:
            db.close()

    return note


def _run_provider_job(
    row_id: str, run_id: str, workspace: WorkspaceContext, provider_id: str, model: str,
    kind: str, prompt: str, tools: dict[str, str], exclude: set[str], executor: Any,
    evaluator_id: str = DEFAULT_EVALUATOR,
) -> None:
    """Collect the evidence and submit the provider job; a failure lands on the row."""
    note = _progress_writer(row_id)
    db = SessionLocal()
    try:
        run = db.get(EvalRun, run_id)
        row = db.get(EvalRecommendation, row_id)
        if run is None or row is None:
            return
        agent = db.get(Agent, run.agent_id) if run.agent_id else None
        prov = rec_providers.get_provider(provider_id)
        evidence, stats, extra = collect_run_evidence(
            db, run, workspace, agent, want_tools=(kind == "tool_descriptions"),
            exclude=exclude, progress=note,
        )
        req = OptimizeRequest(
            current_prompt=prompt, agent=_agent_meta(agent, str(run.agent_id), prompt),
            trace_source={"kind": "eval_results", "run_id": run.id},
            evidence=evidence, stats=stats, model_id=model, workspace=workspace,
            max_chars=PROMPT_MAX_CHARS, extra_feedback=extra,
            max_tokens=get_settings().prompt_opt_max_tokens, components=(kind,),
            tools=dict(tools) if kind == "tool_descriptions" else {},
            max_tokens_cap=judge_budget_cap(model, get_settings()),
        )
        extras = (optimize_extras(db, run, agent, workspace, row.recommendation_id, evaluator_id)
                  if prov.job_type == "optimize" else None)
        job = _build_job(prov, row.recommendation_id, workspace, req, extras)
        executor.submit(workspace, job)
        note("submitted to the evaluation worker")
    except Exception as exc:  # the row must always reach a terminal state
        db.rollback()
        row = db.get(EvalRecommendation, row_id)
        if row is not None:
            row.status, row.error = "FAILED", f"{type(exc).__name__}: {exc}"[:1000]
            db.commit()
        with _live_provider_lock:
            _live_provider_jobs.discard(row_id)
    finally:
        db.close()


# ─── refresh / read ─────────────────────────────────────────────────────────
def _provider_id_of(row: EvalRecommendation) -> str:
    rest = row.recommendation_id[len(PROVIDER_JOB_PREFIX):]
    return rest.rsplit("-", 1)[0] if "-" in rest else rest


def register_optimizer_bundles(
    db: Session, workspace_id: str | None, agent_id: str, done: JobDone, name: str,
) -> list[str]:
    """The candidate bundle versions an ``optimize`` job wrote to S3 become ``config_bundles``
    rows (the worker has no ledger) — so a promoted prompt's bundle history is complete."""
    registered: list[str] = []
    for ref, components in (done.summary.get("bundles") or {}).items():
        try:
            bundle_id, version = bundles.parse_ref(ref)
        except ValueError:
            continue
        if db.get(ConfigBundle, (bundle_id, version)) is not None:
            continue
        db.add(ConfigBundle(id=bundle_id, version=version, workspace_id=workspace_id,
                            agent_id=agent_id, name=name[:64], components=dict(components),
                            s3_key=bundles.bundle_key(agent_id, bundle_id, version)))
        registered.append(ref)
    return registered


def _apply_done(
    row: EvalRecommendation, done: JobDone, *, db: Session | None = None,
    agent_id: str | None = None,
) -> None:
    """``done.json`` → the row (the worker's ``recommendation`` / ``optimize`` summary)."""
    keep = {k: v for k, v in (row.result or {}).items()
            if k in ("provider", "provider_model_id", "excluded_sessions",
                     # D3-d: what the held-out split kept out stays on the finished row
                     "held_out_sessions", "held_out_excluded")}
    if db is not None and agent_id and done.summary.get("bundles"):
        register_optimizer_bundles(db, row.workspace_id, agent_id, done,
                                   f"opt_{row.recommendation_id.rsplit('-', 1)[-1]}")
    if done.state != "COMPLETED":
        row.status = "FAILED"
        row.result = keep
        row.error = done.error or f"the worker job ended {done.state}"
        return
    payload = dict(done.summary.get("recommendation") or done.summary.get("optimize") or {})
    meta = dict(payload.get("meta") or {})
    if row.kind == "system_prompt":
        text = str(payload.get("recommended_prompt") or "").strip()
        if payload.get("status") == "COMPLETED" and text:
            row.status, row.error = "COMPLETED", None
            row.result = {**keep, "recommended_prompt": text,
                          "explanation": payload.get("explanation") or "",
                          "provider_meta": meta}
        else:
            # a job that produced no prompt has no recommendation — never show one
            row.status = "FAILED"
            row.result = {**keep, "provider_meta": meta}
            row.error = payload.get("error") or "the provider produced no prompt"
        return
    descriptions = payload.get("tool_descriptions")
    if payload.get("tool_status") == "COMPLETED" and isinstance(descriptions, dict):
        explanation = payload.get("tool_explanation") or ""
        row.status, row.error = "COMPLETED", None
        row.result = {**keep, "provider_meta": meta, "tools": {
            str(name): {"description": str(text), "explanation": explanation}
            for name, text in descriptions.items()
        }}
    else:
        row.status = "FAILED"
        row.result = {**keep, "provider_meta": meta}
        row.error = (payload.get("tool_error") or payload.get("error")
                     or "the provider produced no tool descriptions")


def refresh(
    db: Session, row: EvalRecommendation, run: EvalRun, workspace: WorkspaceContext,
    *, executor: Any = None, now: datetime | None = None,
) -> None:
    if row.status in REC_TERMINAL:
        return
    if not row.recommendation_id.startswith(PROVIDER_JOB_PREFIX):
        row.status = "FAILED"
        row.error = ("this recommendation was started by the Global AgentCore job, which is "
                     "not available in this build")
        db.commit()
        return
    settings = get_settings()
    if executor is None:
        try:
            prov = rec_providers.get_provider(_provider_id_of(row))
            executor = select_executor(settings, job_type=prov.job_type)
        except (ValueError, AppError) as exc:
            # transient / configuration trouble: keep the last-seen state, say why
            row.error = f"{type(exc).__name__}: {exc}"[:500]
            db.commit()
            return
    done = executor.done(workspace, row.recommendation_id)
    if done is not None:
        _apply_done(row, done, db=db, agent_id=str(run.agent_id or ""))
        with _live_provider_lock:
            _live_provider_jobs.discard(row.id)
        db.commit()
        return
    status = executor.status(workspace, row.recommendation_id)
    with _live_provider_lock:
        live = row.id in _live_provider_jobs
    if status is not None:
        message = (status.progress or {}).get("message")
        if message:
            row.result = {**(row.result or {}), "progress": str(message)[:300]}
        if jobs.is_stale(status, now=now or _now(), stale_s=settings.eval_job_stale_s):
            row.status = "FAILED"
            row.error = (f"the worker job made no progress for {settings.eval_job_stale_s} s "
                         "and is considered lost")
    elif not live:  # never submitted by this process and nothing on the worker side
        row.status = "FAILED"
        row.error = "the recommendation was interrupted (the server restarted while it ran)"
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
    db: Session, run: EvalRun, rec_id: str, *, revising: bool = False
) -> tuple[EvalRecommendation, Agent, str]:
    """Validate accepting a recommendation into a new Harness version.

    Only a COMPLETED system-prompt recommendation of a run whose live, platform-deployed
    agent takes its prompt from the spec (:func:`prompt_accept_ineligibility` — harness or
    zip_runtime) qualifies, and only once: a tool-description recommendation revises tool
    schemas the deployed version does not own.
    Returns ``(row, agent, recommended_prompt)``; the caller re-publishes.

    ``revising`` (N17-obs464): a manual edit published on top of an ACCEPTED recommendation
    — the same checks, but the row must already be accepted (an unaccepted one is edited
    through accept, which records the edit too)."""
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
    if revising and not row.accepted:
        raise AppError(
            "recommendation.not_accepted",
            "this recommendation was not accepted yet — accept it (the review step takes "
            "your edits)", status_code=409,
        )
    if row.accepted and not revising:
        raise AppError(
            "recommendation.already_accepted",
            "this recommendation was already accepted",
            {"accepted": row.accepted}, status_code=409,
        )
    agent = db.get(Agent, run.agent_id) if run.agent_id else None
    if agent is None or agent.workspace_id != run.workspace_id or agent.status == "deleted":
        raise NotFoundError("agent.not_found", "the run's agent no longer exists")
    ineligible = prompt_accept_ineligibility(agent)
    if ineligible is not None:
        # N12-obs44: the same predicate the console's ACCEPT button is gated on; the code
        # keeps its historical name (the Harness was the first method that qualified)
        raise AppError(
            "recommendation.accept_not_harness",
            "accepting a recommendation re-publishes the run's agent with the recommended "
            "system prompt — this agent cannot take one: "
            + {
                "method": f"its method ({agent.method}) bakes the prompt into its own code "
                          "or is externally owned",
                "system_preset": "it is a system-managed preset",
                "converted_locked": "it was converted and its prompt is baked into the "
                                    "exported code",
            }.get(ineligible, ineligible),
            {"method": agent.method, "reason": ineligible}, status_code=400,
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
    system_prompt: str | None = None,
) -> None:
    row.accepted = {
        "edited": edited,
        "by": by,
        "at": _now().isoformat(),
        "agent_id": agent_id,
        "previous_version": previous_version,
        "job_id": job_id,
        "deployment_id": deployment_id,
        # N17-obs464: the text that went live — the editor of a later manual edit starts here
        "system_prompt": system_prompt,
        "edits": [],
    }
    db.commit()


def record_edit(
    db: Session, row: EvalRecommendation, *, by: str, version_before: str | None,
    job_id: str, deployment_id: str, system_prompt: str,
) -> None:
    """N17-obs464: a manual edit published on top of an accepted recommendation — kept
    with the acceptance (``accepted.edits[]``, newest last)."""
    accepted = dict(row.accepted or {})
    accepted["edits"] = [*list(accepted.get("edits") or []), {
        "by": by, "at": _now().isoformat(), "version_before": version_before,
        "job_id": job_id, "deployment_id": deployment_id, "system_prompt": system_prompt,
    }]
    row.accepted = accepted
    db.commit()


def final_prompt(row: EvalRecommendation) -> str | None:
    """N17-obs464: the text a manual edit starts from — the last published edit, else what
    the acceptance published (an acceptance recorded before the text was kept: its
    deployment's spec snapshot), else the AI-recommended prompt."""
    accepted = row.accepted or {}
    edits = list(accepted.get("edits") or [])
    if edits and edits[-1].get("system_prompt"):
        return str(edits[-1]["system_prompt"])
    if accepted.get("system_prompt"):
        return str(accepted["system_prompt"])
    if accepted.get("deployment_id"):
        from sqlalchemy.orm import object_session

        from app.models.ledger import SpecSnapshot

        session = object_session(row)
        snap = session.scalars(select(SpecSnapshot).where(
            SpecSnapshot.deployment_id == accepted["deployment_id"])).first() if session else None
        published = ((snap.spec or {}).get("system_prompt") if snap is not None else None)
        if published:
            return str(published)
    return (row.result or {}).get("recommended_prompt")


def optimized_toward(row: EvalRecommendation) -> str | None:
    """The row's evaluator when its provider optimises toward one; a row recorded without a
    provider (before providers) keeps its evaluator, an unknown provider names none."""
    provider_id = (row.result or {}).get("provider")
    if not provider_id:
        return row.evaluator
    try:
        provider = rec_providers.get_provider(str(provider_id))
    except ValueError:
        return None
    return row.evaluator if rec_providers.takes_target(provider) else None


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
        # N17-obs453 (additive): the evaluator the provider actually optimised toward — null
        # for a provider that takes no target (gepa_lite), whatever an older row stored
        "optimized_toward": optimized_toward(row),
        "tools": row.tools or {},
        "skipped_tools": row.skipped_tools or [],
        "result": row.result or {},
        "error": row.error,
        "accepted": row.accepted,
        # N17-obs464 (additive): the manual edits published on top of the acceptance, and
        # the text an edit starts from
        "edits": list((row.accepted or {}).get("edits") or []),
        "final_prompt": final_prompt(row),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
