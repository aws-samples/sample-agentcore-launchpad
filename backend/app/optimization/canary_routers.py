"""Runtime Canary API — independent target-routing experiment lifecycle."""

from functools import partial
from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError, NotFoundError
from app.evaluation.models import EvalDataset
from app.evaluation.online_evaluators import normalize_online_evaluators
from app.models.ledger import Agent
from app.optimization import canary_harness, canary_service, service
from app.optimization.models import RUNTIME_CANARY_STAGES, Experiment, RuntimeCanary
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.schemas.agent import AgentSpec
from app.services.agentcore.client import control_client
from app.services.harness_convert import graft_config_bundle
from app.system_agents import service as system_agents

router = APIRouter(prefix="/api/runtime-canaries", tags=["runtime-canaries"])


def _out(row: RuntimeCanary) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "champion_agent_id": row.champion_agent_id,
        "champion_agent_name": row.champion_agent_name,
        "challenger_agent_id": row.challenger_agent_id,
        "challenger_agent_name": row.challenger_agent_name,
        "source_experiment_id": row.source_experiment_id,
        "status": row.status,
        "stage": row.stage,
        "stages": RUNTIME_CANARY_STAGES,
        "artifacts": row.artifacts,
        "running_action": row.running_action,
        "progress": row.progress,
        "error": row.error,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("")
def list_runtime_canaries(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    rows = (
        db.query(RuntimeCanary)
        .filter(RuntimeCanary.workspace_id == ws.id)
        .order_by(RuntimeCanary.created_at.desc())
        .limit(20)
        .all()
    )
    return {"canaries": [_out(row) for row in rows]}


def _canary_in(db: Session, ws: WorkspaceScope, canary_id: str) -> RuntimeCanary:
    """The canary, or 404 — another workspace's row is not visible here."""
    row = db.get(RuntimeCanary, canary_id)
    if row is None or row.workspace_id != ws.id:
        raise NotFoundError("canary.not_found", "runtime canary not found")
    return row


@router.get("/{canary_id}")
def get_runtime_canary(
    canary_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return _out(_canary_in(db, ws, canary_id))


class CandidateEdit(BaseModel):
    """The edit that mints the canary's candidate version of the one agent."""

    system_prompt: str | None = None
    tool_description_overrides: dict[str, str] = Field(default_factory=dict)
    code: str | None = None  # studio only


class HarnessVersions(BaseModel):
    """A Harness canary A/Bs two EXISTING versions: control (any earlier version)
    against treatment (the latest version, which DEFAULT already serves)."""

    control: str = Field(pattern=r"^[0-9]{1,9}$")
    treatment: str = Field(pattern=r"^[0-9]{1,9}$")


class RuntimeCanaryCreate(BaseModel):
    agent_id: str
    # runtime canaries mint a candidate from this edit; harness canaries pick versions
    candidate: CandidateEdit | None = None
    harness_versions: HarnessVersions | None = None
    # the ramp stage setup opens at: 0 = 90/10, 1 = 50/50 (skips 90/10; Harness only)
    start_stage: int = Field(default=0, ge=0, le=canary_service.EARLY_COMPLETE_STAGE)
    source_experiment_id: str | None = None
    # the evaluators BOTH arms' online evaluations score with; absent/empty → the
    # default pair. More than ONLINE_EVAL_MAX (after dedup) is a 400, not a 422.
    online_evaluators: list[str] | None = Field(default=None, max_length=50)
    # when set, the verdict's winner / significance / sample size come from this one
    # evaluator (it must be among the selected); absent → the legacy aggregate verdict
    primary_evaluator: str | None = None


def _validate_evaluators(
    req: RuntimeCanaryCreate, ws: WorkspaceScope,
) -> tuple[list[str], str | None]:
    """The canary's evaluator set + primary, refused before any row is written.

    Same rules as every online evaluation config (no trajectory matchers, no
    unknown built-ins, no ground-truth judges, ≤ 10), plus a numeric rating scale:
    the verdict compares the two versions' mean scores."""
    ids = [str(e).strip() for e in req.online_evaluators or ()]
    # only a custom id is read back (GetEvaluator); a built-in-only set needs no client
    custom = any(e and not e.startswith("Builtin.") for e in ids)
    chosen = normalize_online_evaluators(
        ids, control_client(ws.context) if custom else None,
        code_prefix="canary", require_numeric=True,
    )
    primary = (req.primary_evaluator or "").strip() or None
    if primary is not None and primary not in chosen:
        raise AppError(
            "canary.primary_not_selected",
            f"primary evaluator {primary} is not one of the canary's evaluators",
            {"primary_evaluator": primary, "online_evaluators": chosen},
            status_code=400,
        )
    return chosen, primary


def _validate_harness_versions(agent: Agent, versions: HarnessVersions, ws: WorkspaceScope) -> None:
    if versions.control == versions.treatment:
        raise AppError(
            "canary.versions_identical",
            "control and treatment must be different Harness versions",
            status_code=400,
        )
    known = canary_harness.version_numbers(control_client(ws.context), agent.resource_id)
    missing = [v for v in (versions.control, versions.treatment) if v not in known]
    if missing:
        raise AppError(
            "canary.version_not_found",
            f"Harness version(s) {', '.join(missing)} do not exist",
            {"versions": known},
            status_code=400,
        )
    if versions.treatment != known[-1]:
        # DEFAULT always serves the latest version; pinning treatment there keeps
        # promotion a ledger-only step (nothing on AWS has to be re-pointed)
        raise AppError(
            "canary.treatment_not_latest",
            f"treatment must be the latest Harness version ({known[-1]})",
            {"latest": known[-1]},
            status_code=400,
        )


def _eligible_agent(db: Session, ws: WorkspaceScope, agent_id: str) -> Agent:
    agent = db.get(Agent, agent_id)
    if agent is not None and agent.workspace_id != ws.id:
        agent = None
    if agent is None or agent.status != "active":
        raise AppError(
            "canary.agent_not_active",
            "canary agent must be active",
            {"agent_id": agent_id},
            status_code=400,
        )
    capability = service.canary_capability(agent)
    if not capability["eligible"]:
        raise AppError(
            "canary.agent_unsupported",
            capability["reason"],
            {"agent_id": agent_id, "canary_capability": capability},
            status_code=400,
        )
    # One running canary per agent: a second would mint another candidate version
    # and stand up a competing gateway route for the same agent's live traffic.
    existing = (
        db.query(RuntimeCanary)
        .filter(
            RuntimeCanary.workspace_id == ws.id,
            RuntimeCanary.champion_agent_id == agent_id,
            RuntimeCanary.status == "running",
        )
        .first()
    )
    if existing is not None:
        raise AppError(
            "canary.already_running",
            "a canary is already running for this agent",
            {"agent_id": agent_id, "canary_id": existing.id},
            status_code=409,
        )
    return agent


def _resolve_edited_spec(agent: Agent, candidate: CandidateEdit) -> AgentSpec:
    """Apply the candidate edit onto the agent's current spec (mirrors
    ``service.act_promote``) and return the AgentSpec that setup will mint."""
    spec_data = dict(agent.spec or {})
    prompt = str(
        candidate.system_prompt
        if candidate.system_prompt is not None
        else spec_data.get("system_prompt") or ""
    ).strip()
    overrides = dict(spec_data.get("tool_description_overrides") or {})
    overrides.update(
        {str(k): str(v) for k, v in (candidate.tool_description_overrides or {}).items()}
    )
    spec_data.update({
        "name": agent.name,
        "method": agent.method,
        "system_prompt": prompt,
        "tool_description_overrides": overrides,
    })
    if agent.method == "studio" and candidate.code is not None:
        spec_data["code"] = candidate.code
    spec = AgentSpec(**spec_data)
    if spec.source_harness:
        bundle = dict(spec.code_bundle or {})
        if "main.py" not in bundle:
            raise AppError(
                "canary.candidate_invalid",
                "converted runtime bundle has no main.py",
                status_code=400,
            )
        bundle["main.py"] = graft_config_bundle(
            bundle["main.py"],
            default_system_prompt=prompt,
            tool_description_overrides=overrides,
        )
        spec = spec.model_copy(update={"code_bundle": bundle})
    return spec


@router.post("", status_code=201)
def create_runtime_canary(
    req: RuntimeCanaryCreate,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    agent = _eligible_agent(db, ws, req.agent_id)
    if agent.method == "harness":
        if req.harness_versions is None:
            raise AppError(
                "canary.versions_required",
                "a Harness canary needs harness_versions {control, treatment}",
                status_code=422,
            )
        _validate_harness_versions(agent, req.harness_versions, ws)
        evaluators, primary = _validate_evaluators(req, ws)
        row = canary_service.start_harness_canary(
            agent,
            control_version=req.harness_versions.control,
            treatment_version=req.harness_versions.treatment,
            workspace=ws.context,
            start_stage=req.start_stage,
            online_evaluators=evaluators,
            primary_evaluator=primary,
        )
        return _out(row)
    if req.start_stage:
        # a Runtime canary's candidate is not production yet: 90/10 is its blast-radius cap
        raise AppError(
            "canary.start_stage_harness_only",
            "only a Harness canary may skip the 90/10 stage",
            status_code=400,
        )
    candidate = req.candidate
    if candidate is None:
        raise AppError(
            "canary.candidate_empty",
            "candidate must change something",
            status_code=400,
        )
    if not (
        (candidate.system_prompt or "").strip()
        or candidate.tool_description_overrides
        or (candidate.code or "").strip()
    ):
        raise AppError(
            "canary.candidate_empty",
            "candidate must change something",
            status_code=400,
        )
    if req.source_experiment_id:
        source = db.get(Experiment, req.source_experiment_id)
        if source is not None and source.workspace_id != ws.id:
            source = None
        if source is None or not service.promotion_complete(source.artifacts):
            raise AppError(
                "canary.source_experiment_invalid",
                "source experiment must have a completed production promotion",
                {"source_experiment_id": req.source_experiment_id},
                status_code=400,
            )
        if source.agent_id != agent.id:
            raise AppError(
                "canary.source_champion_mismatch",
                "source experiment agent must be the canary agent",
                {"source_experiment_id": source.id},
                status_code=400,
            )
    edited_spec = _resolve_edited_spec(agent, candidate)
    evaluators, primary = _validate_evaluators(req, ws)
    row = canary_service.start_canary(
        agent, edited_spec, ws.context, req.source_experiment_id,
        online_evaluators=evaluators, primary_evaluator=primary,
    )
    return _out(row)


class RuntimeCanaryAction(BaseModel):
    action: str = Field(
        pattern="^(setup|traffic|verdict|advance|complete|rollback|cleanup)$"
    )
    dataset_id: str | None = None
    allow_non_significant: bool = False


@router.post("/{canary_id}/action")
def runtime_canary_action(
    canary_id: str,
    req: RuntimeCanaryAction,
    response: Response,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    row = _canary_in(db, ws, canary_id)
    system_agents.assert_not_system_agent(db, row.champion_agent_id, "canary")
    system_agents.assert_not_system_agent(db, row.challenger_agent_id, "canary")
    if row.running_action:
        raise AppError(
            "canary.action_in_flight",
            f"{row.running_action} is still running — wait for it to finish",
            status_code=409,
        )
    reason = canary_service.stage_not_ready_reason(row, req.action)
    if reason:
        raise AppError("canary.stage_not_ready", reason, status_code=409)

    if req.action == "setup":
        canary_service.assert_setup_available(row.id)
    elif req.action == "traffic":
        if not req.dataset_id:
            raise AppError(
                "canary.dataset_required",
                "traffic requires a replay dataset",
                status_code=422,
            )
        dataset = db.get(EvalDataset, req.dataset_id)
        if dataset is None or dataset.workspace_id != ws.id:
            raise NotFoundError("dataset.not_found", "dataset not found")
        try:
            items = service.resolve_traffic_items(dataset)
        except ValueError as exc:
            raise AppError(
                "canary.dataset_unsupported", str(exc), status_code=422
            ) from exc
        prompts = [prompt for _label, prompt in items]
        scenario_ids = [label for label, _prompt in items]
        dataset_info = {
            "dataset_id": dataset.id,
            "dataset_name": dataset.name,
        }
    elif req.action in {"advance", "complete"}:
        canary_service.assert_verdict_allows(
            row, allow_non_significant=req.allow_non_significant
        )

    action = req.action
    if action == "setup":
        fn = partial(canary_service.act_setup, canary_id)
    elif action == "traffic":
        fn = partial(
            canary_service.act_traffic, canary_id, prompts, dataset_info,
            scenario_ids=scenario_ids,
        )
    elif action == "verdict":
        fn = partial(canary_service.act_verdict, canary_id)
    elif action == "advance":
        fn = partial(
            canary_service.act_advance,
            canary_id,
            allow_non_significant=req.allow_non_significant,
        )
    elif action == "complete":
        fn = partial(
            canary_service.act_complete,
            canary_id,
            allow_non_significant=req.allow_non_significant,
        )
    elif action == "rollback":
        fn = partial(canary_service.act_rollback, canary_id)
    else:
        fn = partial(canary_service.act_cleanup, canary_id)

    canary_service.run_action(canary_id, action, fn)
    response.status_code = 202
    db.expire_all()
    row = db.get(RuntimeCanary, canary_id)
    return {"canary": _out(row)}
