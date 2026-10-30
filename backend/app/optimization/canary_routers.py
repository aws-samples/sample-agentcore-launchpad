"""Runtime Canary API — the target-routing canary lifecycle (the only optimization flow
since N13-obs2; the configuration-bundle experiment routes are retired)."""

from functools import partial
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.assistant import purge as assistant_purge
from app.core.db import get_db
from app.core.errors import AppError, NotFoundError
from app.evaluation import scenarios
from app.evaluation.models import EvalDataset
from app.evaluation.online_evaluators import (
    normalize_online_evaluators,
    refuse_hidden_evaluators,
)
from app.models.ledger import Agent
from app.optimization import canary_service, stats, traffic
from app.optimization.capability import canary_capability
from app.optimization.models import CANARY_BLOCKING_STATUSES, RUNTIME_CANARY_STAGES, RuntimeCanary
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.schemas.agent import AgentSpec
from app.services import agent_access, creators, ownership, visibility
from app.services.creators import Creator
from app.services.harness_convert import graft_config_bundle
from app.services.visibility import Viewer, require_viewer
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
        "status": row.status,
        "stage": row.stage,
        "stages": RUNTIME_CANARY_STAGES,
        "artifacts": row.artifacts,
        "running_action": row.running_action,
        "progress": row.progress,
        # N17-obs447 (additive): the running line's stable code + params; null for plain text
        "progress_code": (row.progress_detail or {}).get("code"),
        "progress_params": (row.progress_detail or {}).get("params"),
        "error": row.error,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        **creators.fields(row),
    }


@router.get("")
def list_runtime_canaries(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    rows = (
        db.query(RuntimeCanary)
        .filter(RuntimeCanary.workspace_id == ws.id)
        .filter(visibility.clause(viewer, RuntimeCanary.created_by))  # before .limit(20)
        .order_by(RuntimeCanary.created_at.desc())
        .limit(20)
        .all()
    )
    return {"canaries": [_out(row) for row in rows]}


def _canary_in(
    db: Session, ws: WorkspaceScope, canary_id: str, *, viewer: Viewer = Viewer.ALL
) -> RuntimeCanary:
    """The canary, or 404 — another workspace's row, or one workspace isolation hides
    from ``viewer``, is not visible here."""
    row = db.get(RuntimeCanary, canary_id)
    if row is None or row.workspace_id != ws.id:
        raise NotFoundError("canary.not_found", "runtime canary not found")
    visibility.require_seen(
        viewer, db, row.created_by, NotFoundError("canary.not_found", "runtime canary not found")
    )
    return row


def _visible_dataset(
    db: Session, ws: WorkspaceScope, viewer: Viewer, dataset_id: str
) -> EvalDataset | None:
    """The replay dataset when it is this workspace's and visible to the caller."""
    row = db.get(EvalDataset, dataset_id)
    if row is not None and (
        row.workspace_id != ws.id or not visibility.sees(viewer, db, row.created_by)
    ):
        return None
    return row


@router.get("/{canary_id}")
def get_runtime_canary(
    canary_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    return _out(_canary_in(db, ws, canary_id, viewer=viewer))


class CandidateEdit(BaseModel):
    """The edit that mints the canary's candidate version of the one agent."""

    system_prompt: str | None = None
    tool_description_overrides: dict[str, str] = Field(default_factory=dict)
    code: str | None = None  # studio only


class CanaryDesign(BaseModel):
    """R3-d: a minimum detectable effect on the primary evaluator's [0, 1] score, the
    assumed spread, the false-positive rate and the power → sessions per arm."""

    model_config = ConfigDict(extra="forbid")
    mde: float = Field(gt=0, le=1)
    sd: float = Field(default=stats.DEFAULT_SD, gt=0, le=1)
    alpha: float = Field(default=0.05, gt=0, le=0.5)
    power: float = Field(default=0.8, ge=0.5, lt=1)

    def resolved(self) -> dict[str, Any]:
        return {"mde": self.mde, "sd": self.sd, "alpha": self.alpha, "power": self.power,
                "planned_n_per_arm": stats.sessions_per_arm(
                    self.mde, sd=self.sd, alpha=self.alpha, power=self.power)}


class RuntimeCanaryCreate(BaseModel):
    agent_id: str
    # a Runtime canary mints its candidate version from this edit (CA-1a: the Harness
    # canary's `harness_versions` / `start_stage` are gone with the managed Harness)
    candidate: CandidateEdit | None = None
    # N12-obs46 (upstream b5eb3dc8 parity): the evaluators BOTH arms' online evaluations and
    # the replay job score with; absent/empty → the default pair. More than ONLINE_EVAL_MAX
    # (after dedup) is a 400, not a 422.
    online_evaluators: list[str] | None = Field(default=None, max_length=50)
    # when set, the verdict's winner / significance / sample size come from this one
    # evaluator (it must be among the selected); absent → the legacy aggregate verdict
    primary_evaluator: str | None = None
    # R3-d: the experiment design — the verdict tests the primary ONCE at the planned n
    design: CanaryDesign | None = None


def _validate_evaluators(
    req: RuntimeCanaryCreate, db: Session, ws: WorkspaceScope,
) -> tuple[list[str], str | None]:
    """The canary's evaluator set + primary, refused before any row is written.

    Same rules as every online evaluation config (no trajectory matchers, no unknown
    built-ins, no ground-truth judges, ≤ 10), plus a numeric rating scale: the verdict
    compares the two versions' mean scores. Custom ids are ``eval_evaluators`` rows of
    this workspace (one ledger read; a built-in-only set reads nothing)."""
    chosen = normalize_online_evaluators(
        [str(e).strip() for e in req.online_evaluators or ()], db,
        code_prefix="canary", workspace_id=ws.id, require_numeric=True,
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


def _eligible_agent(
    db: Session, ws: WorkspaceScope, agent_id: str, viewer: Viewer = Viewer.ALL
) -> Agent:
    # a foreign, missing or isolation-hidden id answers 400 canary.agent_not_active alike
    agent = agent_access.agent_in(db, ws.id, agent_id, viewer=viewer)
    if agent is None or agent.status != "active":
        raise AppError(
            "canary.agent_not_active",
            "canary agent must be active",
            {"agent_id": agent_id},
            status_code=400,
        )
    assistant_purge.refuse_while_clearing(agent.id, "canary")
    capability = canary_capability(agent)
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
            RuntimeCanary.status.in_(CANARY_BLOCKING_STATUSES),
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
    """Apply the candidate edit onto the agent's current spec and return the AgentSpec
    that setup will mint."""
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
    elif spec.code_bundle and (candidate.system_prompt is not None
                               or candidate.tool_description_overrides):
        # N17-obs386: an uploaded code bundle (not a harness export) runs its main.py
        # verbatim (``zip_runtime._generate_code``) — a prompt / tool-description edit has
        # no anchor to land on, so the challenger would be the champion's exact copy
        raise AppError(
            "canary.edit_not_applicable",
            "this agent runs its own uploaded code bundle; a system-prompt or "
            "tool-description edit cannot be applied to it — edit the bundle and redeploy "
            "instead",
            status_code=409,
            detail={"agent_id": agent.id, "source": "code_bundle"},
        )
    return spec


def _assert_candidate_differs(agent: Agent, edited: AgentSpec) -> None:
    """Refuse a challenger whose rendered entrypoint equals the champion's (N17-obs386):
    the canary would compare two identical versions and its verdict mean nothing. Both are
    rendered with the code generator the candidate mint uses; a render that fails here is
    left to setup, which reports it as before."""
    from app.deployer.zip_runtime import _generate_code

    try:
        champion = AgentSpec(**{**(agent.spec or {}), "name": agent.name,
                                "method": agent.method})
        before, _ = _generate_code(champion)
        after, _ = _generate_code(edited)
    except Exception:  # noqa: BLE001 — setup owns render errors
        return
    if before == after:
        raise AppError(
            "canary.candidate_identical",
            "the candidate renders exactly the champion's code — the edit changes nothing "
            "the runtime would run",
            status_code=409,
            detail={"agent_id": agent.id},
        )


@router.post("", status_code=201)
def create_runtime_canary(
    req: RuntimeCanaryCreate,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    agent = _eligible_agent(db, ws, req.agent_id, viewer)
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
    edited_spec = _resolve_edited_spec(agent, candidate)
    _assert_candidate_differs(agent, edited_spec)
    # a custom evaluator hidden from the caller is refused like an unknown one
    refuse_hidden_evaluators(db, ws.id, viewer, req.online_evaluators, code_prefix="canary")
    evaluators, primary = _validate_evaluators(req, db, ws)
    design = req.design.resolved() if req.design else None
    if design and primary is None:
        primary = evaluators[0]  # R3-d: a fixed-n design decides on ONE evaluator
    row = canary_service.start_canary(
        agent, edited_spec, ws.context,
        online_evaluators=evaluators, primary_evaluator=primary, design=design,
        creator=Creator.of_request(request),
    )
    return _out(row)


def held_out_labels(dataset: Any) -> set[str]:
    """R3-c: the traffic labels (``scenario_id`` / ``item_<n>``, as
    ``traffic.resolve_traffic_items`` names them) of the dataset's held-out scenarios —
    from ``scenarios.item_splits`` (r17-bj, D3-d); empty where the split is unknown."""
    item_splits = getattr(scenarios, "item_splits", None)
    items = list(getattr(dataset, "items", None) or [])
    if item_splits is None or not items:
        return set()
    return {str(item.get("scenario_id") or f"item_{index + 1}")
            for index, (item, split) in enumerate(zip(items, item_splits(items), strict=True))
            if split == "held_out"}


class RuntimeCanaryAction(BaseModel):
    action: str = Field(
        pattern="^(setup|traffic|verdict|advance|complete|rollback|cleanup)$"
    )
    dataset_id: str | None = None
    allow_non_significant: bool = False
    # N-SCN-obs14 (traffic only): keep cycling the dataset within this round until every
    # arm holds at least this many sessions (cap 3 × dataset size). None = one pass.
    sessions_per_arm: int | None = Field(default=None, ge=1, le=500)


# Actions that never touch the champion agent: the verdict read and the teardown of
# the canary's own resources.
AGENT_FREE_ACTIONS = frozenset({"verdict", "cleanup"})

# Actions any member who can SEE the canary may run: `verdict` only scores the sessions
# both arms already collected — it changes no version, route, weight or resource. Every
# other action (setup mints a version and opens the A/B route, traffic drives both arms,
# advance / complete / rollback change the production routing, cleanup destroys the
# canary's resources) is the creator's or an administrator's (N13-obs15, E13 NC-16).
CREATOR_FREE_ACTIONS = frozenset({"verdict"})


@router.post("/{canary_id}/action")
def runtime_canary_action(
    canary_id: str,
    req: RuntimeCanaryAction,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    row = _canary_in(db, ws, canary_id, viewer=viewer)
    if req.action not in AGENT_FREE_ACTIONS:
        # setup / advance / complete / rollback re-publish the champion and traffic
        # invokes both arms: a visible canary does not make a hidden agent the caller's
        for agent_id in (row.champion_agent_id, row.challenger_agent_id):
            agent_access.require_agent_visible(
                db, viewer, db.get(Agent, agent_id) if agent_id else None,
                NotFoundError("agent.not_found", "agent not found"),
            )
    if req.action not in CREATOR_FREE_ACTIONS:
        # after the visibility 404s (a hidden row stays indistinguishable from a missing
        # one), before any stage / in-flight check (a forbidden caller learns no state)
        ownership.require_creator_or_admin(
            row, require_identity(request), "canary.forbidden",
            message="only the canary's creator or an administrator may run this action",
            detail={"canary_id": row.id, "action": req.action},
        )
    system_agents.assert_not_system_agent(db, row.champion_agent_id, "canary")
    system_agents.assert_not_system_agent(db, row.challenger_agent_id, "canary")
    # a conversation CLEAR owns this canary's cleanup while it runs
    assistant_purge.refuse_while_clearing(row.champion_agent_id, f"canary {req.action}")
    if row.running_action:
        raise AppError(
            "canary.action_in_flight",
            f"{row.running_action} is still running — wait for it to finish",
            status_code=409,
        )
    reason = canary_service.stage_not_ready_reason(row, req.action)
    if reason:
        raise AppError("canary.stage_not_ready", reason, status_code=409)
    if req.action == "cleanup":
        # N17-obs452: a running canary past setup is completed or rolled back first
        canary_service.refuse_cleanup_before_decision(row)
    # N17-obs13 (agents.md §1.2): `complete` / `rollback` publish the champion's live version
    # — refused with the shared 409 `agent.deploy_in_progress` while a deploy job owns the
    # agent, after the canary's own state gates and before the verdict gate; a plain 409
    # here, never a 202 whose thread fails. The action re-checks under its own session.
    canary_service.refuse_publish_during_deploy(row, req.action)
    # N17-obs16: the champion's version moved since setup (a finished re-publish) — the
    # candidate was built on a base that is no longer production: 409 canary.stale_base
    canary_service.refuse_stale_base(row, req.action)

    if req.action == "setup":
        canary_service.assert_setup_available(row.id)
    elif req.action == "traffic":
        if not req.dataset_id:
            raise AppError(
                "canary.dataset_required",
                "traffic requires a replay dataset",
                status_code=422,
            )
        dataset = _visible_dataset(db, ws, viewer, req.dataset_id)
        if dataset is None or dataset.workspace_id != ws.id:
            raise NotFoundError("dataset.not_found", "dataset not found")
        try:
            items = traffic.resolve_traffic_items(dataset)
        except ValueError as exc:
            raise AppError(
                "canary.dataset_unsupported", str(exc), status_code=422
            ) from exc
        prompts = [prompt for _label, prompt in items]
        scenario_ids = [label for label, _prompt in items]
        held_out = held_out_labels(dataset)
        dataset_info = {
            "dataset_id": dataset.id,
            "dataset_name": dataset.name,
        }
    elif req.action in {"advance", "complete"}:
        canary_service.assert_verdict_allows(
            row,
            allow_non_significant=req.allow_non_significant,
            terminal=req.action == "complete",
        )

    action = req.action
    if action == "setup":
        fn = partial(canary_service.act_setup, canary_id)
    elif action == "traffic":
        fn = partial(
            canary_service.act_traffic, canary_id, prompts, dataset_info,
            scenario_ids=scenario_ids, sessions_per_arm=req.sessions_per_arm,
            # R3-c: only when the dataset holds scenarios out (all are replayed either way)
            **({"held_out_scenarios": held_out} if held_out else {}),
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
