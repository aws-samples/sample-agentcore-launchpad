"""Experiments API — create, inspect, and drive one stage action at a time."""

import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError, NotFoundError
from app.evaluation.models import EvalDataset
from app.models.ledger import Agent
from app.optimization import providers as rec_providers
from app.optimization import readiness, service
from app.optimization.models import STAGES, Experiment
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services.agentcore.client import control_client

router = APIRouter(prefix="/api/experiments", tags=["experiments"])


def _out(exp: Experiment) -> dict[str, Any]:
    status = "ready" if service.legacy_promotion(exp.artifacts) else exp.status
    return {
        "id": exp.id,
        "name": exp.name,
        "agent_id": exp.agent_id,
        "agent_name": exp.agent_name,
        "status": status,
        "stage": exp.stage,
        "stages": STAGES,
        "artifacts": exp.artifacts,
        "running_action": exp.running_action,
        "progress": exp.progress,
        "error": exp.error,
        "created_at": exp.created_at.isoformat() if exp.created_at else None,
    }


@router.get("")
def list_experiments(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    rows = (
        db.query(Experiment)
        .filter(Experiment.workspace_id == ws.id)
        .order_by(Experiment.created_at.desc())
        .limit(20)
        .all()
    )
    return {"experiments": [_out(e) for e in rows]}


def _experiment_in(db: Session, ws: WorkspaceScope, exp_id: str) -> Experiment:
    """The experiment, or 404 — another workspace's row is not visible here."""
    exp = db.get(Experiment, exp_id)
    if exp is None or exp.workspace_id != ws.id:
        raise NotFoundError("experiment.not_found", "experiment not found")
    return exp


def _eligible_agent(agent_id: str, db: Session, ws: WorkspaceScope) -> Agent:
    agent = db.get(Agent, agent_id)
    if agent is not None and agent.workspace_id != ws.id:
        agent = None
    if agent is None or agent.status != "active":
        raise AppError("agent.not_active", "agent must be active", status_code=400)
    capability = service.experiment_capability(agent)
    if not capability["eligible"]:
        code = "experiment.agent_unsupported"
        if agent.method == "harness":
            code = "experiment.method_unsupported"
        elif (agent.spec or {}).get("protocol") == "a2a":
            code = "experiment.protocol_unsupported"
        raise AppError(
            code,
            capability["reason"],
            {"experiment_capability": capability},
            status_code=400,
        )
    return agent


@router.get("/readiness")
def experiment_readiness(
    agent_id: str = Query(min_length=1, max_length=32),
    lookback_hours: int = Query(
        default=readiness.DEFAULT_LOOKBACK_HOURS,
        ge=readiness.MIN_LOOKBACK_HOURS,
        le=readiness.MAX_LOOKBACK_HOURS,
    ),
    force: bool = False,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    agent = _eligible_agent(agent_id, db, ws)
    return readiness.project_readiness(
        agent,
        db,
        ws.context,
        lookback_hours=lookback_hours,
        force=force,
    )


@router.get("/providers")
def recommend_providers(
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """The recommend-stage generators the console can offer (no AWS call)."""
    return {"providers": rec_providers.describe_providers()}


@router.get("/{exp_id}")
def get_experiment(
    exp_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return _out(_experiment_in(db, ws, exp_id))


class ExperimentCreate(BaseModel):
    agent_id: str
    lookback_hours: int = Field(
        default=readiness.DEFAULT_LOOKBACK_HOURS,
        ge=readiness.MIN_LOOKBACK_HOURS,
        le=readiness.MAX_LOOKBACK_HOURS,
    )


@router.post("", status_code=201)
def create_experiment(
    req: ExperimentCreate,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    # experiments share one gateway (EXP_GATEWAY_NAME) and the AB service
    # allows a single active test per gateway — a concurrent loop would fail
    # at the abtest stage, so reject up front.
    running = (
        db.query(Experiment)
        .filter(Experiment.workspace_id == ws.id, Experiment.status == "running")
        .first()
    )
    if running is not None:
        raise AppError(
            "experiment.already_running",
            f"experiment {running.name} is still running — "
            "wait for its verdict or clean it up first",
            status_code=409,
        )
    agent = _eligible_agent(req.agent_id, db, ws)
    trace_readiness = readiness.project_readiness(
        agent,
        db,
        ws.context,
        lookback_hours=req.lookback_hours,
    )
    if trace_readiness["state"] == "missing":
        raise AppError(
            "experiment.trace_required",
            "CloudWatch traces are required within the selected lookback window",
            {"readiness": trace_readiness},
            status_code=409,
        )
    exp = service.start_experiment(agent, ws.context)
    return _out(exp)


class ActionRequest(BaseModel):
    action: str = Field(
        pattern="^(recommend|accept|bundles|gateway|abtest|traffic|verdict"
                "|promote|canary|ramp|cleanup)$"
    )
    # recommend — which generators to run (default: both) and, for the
    # tool-description one, the toolName → current-description set to analyze
    # (default: tools discovered from the agent's spec)
    recommend_types: list[Literal["system_prompt", "tool_descriptions"]] | None = (
        Field(default=None, min_length=1)
    )
    recommend_tools: dict[str, str] | None = None
    # recommend — pin the trace source to one completed evaluation run (typically an
    # Insights job) instead of the default rolling window, so the recommendation is
    # generated from exactly the sessions that run analysed. Omitted ⇒ unchanged.
    recommend_source_run_id: str | None = Field(default=None, min_length=1, max_length=16)
    # recommend — which system-prompt generator runs. Absent ⇒ the AgentCore
    # recommendation job (unchanged path). A 3rd-party provider reflects on the
    # pinned run's scored sessions with the given Bedrock model (or its default);
    # tool descriptions always come from AgentCore. The Literal mirrors
    # providers.PROVIDER_IDS (pinned by a test) so an unknown id is a 422.
    recommend_provider: Literal["agentcore", "gepa_lite"] | None = None
    recommend_model_id: str | None = Field(default=None, min_length=3, max_length=128)
    accepted_prompt: str | None = None                        # accept
    accepted_tool_descriptions: dict[str, str] | None = None  # accept
    dataset_id: str | None = None                             # traffic
    # gateway — evaluators the online evaluation config scores both arms with
    # (default: service.ONLINE_EVAL_DEFAULT). AWS caps the list at 10.
    online_evaluators: list[str] | None = Field(
        default=None, min_length=1, max_length=service.ONLINE_EVAL_MAX
    )
    challenger_agent_id: str | None = None                    # legacy canary


_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")


def _validate_recommend_provider(req: ActionRequest) -> None:
    """Provider-side preconditions, checked before any thread or AWS call.

    A provider that reflects on scored evidence needs a pinned evaluation run —
    the rolling CloudWatch window carries traces but no judge scores. Only
    enforced when the system-prompt generator is actually selected: a
    tool-descriptions-only run ignores the provider (AgentCore generates those).
    """
    if req.recommend_model_id is not None and not _MODEL_ID.match(req.recommend_model_id):
        raise AppError(
            "experiment.model_id_invalid",
            "recommend_model_id must be a Bedrock model / inference-profile id",
            {"model_id": req.recommend_model_id},
            status_code=422,
        )
    provider = rec_providers.get_provider(req.recommend_provider)
    wants_prompt = req.recommend_types is None or "system_prompt" in req.recommend_types
    if wants_prompt and provider.requires_source and not req.recommend_source_run_id:
        raise AppError(
            "experiment.provider_requires_source",
            f"the {provider.id} provider reflects on a completed evaluation run's "
            "scores — pin one with recommend_source_run_id",
            {"provider": provider.id},
            status_code=422,
        )


@router.post("/{exp_id}/action")
def experiment_action(
    exp_id: str, req: ActionRequest, response: Response,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    exp = _experiment_in(db, ws, exp_id)
    if exp.running_action:
        raise AppError(
            "experiment.action_in_flight",
            f"{exp.running_action} is still running — wait for it to finish",
            status_code=409,
        )
    if req.action in {"canary", "ramp"}:
        raise AppError(
            "experiment.action_moved",
            "Runtime canaries now use /api/runtime-canaries",
            {"runtime_canaries_path": "/api/runtime-canaries"},
            status_code=410,
        )
    reason = service.stage_not_ready_reason(exp, req.action)
    if reason:
        raise AppError("experiment.stage_not_ready", reason, status_code=409)

    # sync actions answer inline; async ones 202 into a background thread and
    # the client watches running_action/progress on the experiment itself
    if req.action == "accept":
        rec = exp.artifacts.get("recommend") or {}
        # tool-description-only recommendations have no recommended_prompt —
        # the treatment keeps the current production prompt
        meta = exp.artifacts.get("agent_meta") or {}
        control = (meta.get("system_prompt") or "").strip()
        if service.system_prompt_rec_failed(rec):
            # a failed system-prompt job produced nothing to accept — the only
            # ways forward are a successful re-generation or an operator-authored
            # treatment prompt that actually differs from the control. Any stored
            # recommended_prompt is ignored: rows written before this guard hold
            # the old generic fallback text, which no optimizer produced.
            prompt = (req.accepted_prompt or "").strip()
            if not prompt or prompt == control:
                raise AppError(
                    "experiment.accept_rec_failed",
                    "the system-prompt recommendation failed ("
                    f"{rec.get('system_prompt_error') or rec.get('system_prompt_status')}"
                    ") — re-generate it or supply an edited treatment prompt",
                    status_code=409,
                )
        else:
            prompt = (req.accepted_prompt or rec.get("recommended_prompt")
                      or control).strip()
        if not prompt:
            raise AppError("experiment.accept_invalid",
                           "accepted prompt is empty", status_code=400)
        service.action_accept(exp, prompt, req.accepted_tool_descriptions)
    elif req.action == "bundles":
        service.action_bundles(exp)
    elif req.action == "promote":
        service.run_action(
            exp.id,
            "promote",
            lambda progress: service.act_promote(exp_id, progress),
        )
    elif req.action == "traffic":
        if not req.dataset_id:
            raise AppError(
                "experiment.dataset_required",
                "traffic requires a replay dataset",
                status_code=422,
            )
        dataset = db.get(EvalDataset, req.dataset_id)
        if dataset is None or dataset.workspace_id != ws.id:
            raise NotFoundError("dataset.not_found", "dataset not found")
        try:
            prompts = service.resolve_traffic_prompts(dataset)
        except ValueError as exc:
            raise AppError("experiment.dataset_unsupported", str(exc),
                           status_code=422) from exc
        dataset_info = {"dataset_id": dataset.id, "dataset_name": dataset.name}
        service.run_action(
            exp.id, "traffic",
            lambda progress: service.act_traffic(exp_id, prompts, dataset_info,
                                                 progress),
        )
    elif req.action == "recommend":
        _validate_recommend_provider(req)
        # resolved here, not inside the thread: a run that is missing, unfinished, or
        # another agent's should be an immediate API error, not a failed background
        # job the user has to go read a stage error to understand
        source = service.resolve_recommend_source(
            exp.agent_id, req.recommend_source_run_id, ws.context
        )
        service.run_action(
            exp.id, "recommend",
            lambda progress: service.act_recommend(
                exp_id, progress,
                types=req.recommend_types, tools=req.recommend_tools,
                source=source, provider=req.recommend_provider,
                model_id=req.recommend_model_id),
        )
    elif req.action == "gateway":
        service.assert_shared_gateway_available(ws.context)
        # validated here, not inside the thread: a bad evaluator id shouldn't
        # cost a gateway + runtime-target round-trip before it is caught
        evaluators = service.normalize_online_evaluators(
            req.online_evaluators, control_client(ws.context)
        )
        service.run_action(
            exp.id, "gateway",
            lambda progress: service.act_gateway(exp_id, progress, evaluators),
        )
    else:  # abtest | verdict | cleanup
        if req.action == "abtest":
            service.assert_shared_gateway_available(
                ws.context, own_test_name=f"exp_{exp.id[:8]}_bundle"
            )
        fn = {
            "abtest": service.act_abtest,
            "verdict": service.act_verdict,
            "cleanup": service.act_cleanup,
        }[req.action]
        service.run_action(exp.id, req.action,
                           lambda progress: fn(exp_id, progress))

    if req.action in service.ASYNC_ACTIONS:
        response.status_code = 202
    db.expire_all()  # the action thread/service wrote via its own session
    exp = db.get(Experiment, exp_id)
    return {"experiment": _out(exp)}
