"""System-managed presets API — status (ledger read), admin install/repair, uninstall.

The install route is the *only* way a preset reaches AWS, and it is an explicit,
billable operator action: nothing here runs on startup, and the status read never
touches a cloud client. Authorization comes from ``ROUTE_POLICY`` (GET member, POST /
DELETE admin) plus the same check re-asserted in the handlers, so a table edit alone
cannot open the mutation paths.
"""

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError, NotFoundError
from app.deployer.pipeline import start_deploy_async
from app.routers.agents import _agent_out
from app.routers.auth import require_admin, require_identity
from app.routers.registry import _invalidate_attachables, _record_out
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.schemas.agent import MAX_TOKENS_CEILING, KnowledgeBaseRef, ModelSource, ReasoningEffort
from app.system_agents import presets as catalogue
from app.system_agents import service, skill_registry
from app.system_agents.presets import EDITABLE_FIELDS, PresetEdit
from app.system_agents.uninstall import start_uninstall_async

router = APIRouter(prefix="/api/system-agents", tags=["system-agents"])

EditableField = Literal[
    "model_id",
    "model_source",
    "max_tokens",
    "reasoning_effort",
    "system_prompt",
    "max_iterations",
    "timeout_seconds",
    "knowledge_bases",
]
assert set(EditableField.__args__) == set(EDITABLE_FIELDS)  # type: ignore[attr-defined]
ClearableField = Literal["max_tokens", "reasoning_effort"]


class InstallRequest(BaseModel):
    """The administrator's choices — a *partial* edit.

    An empty JSON object (``{}``) installs with the preset defaults or repairs with
    exactly the stored choices; the body itself is required. Every member given here
    replaces the stored value; every member omitted (or JSON ``null``) keeps it. To
    return a member to this build's default, name it in ``reset``; to send nothing for
    ``max_tokens`` / ``reasoning_effort``, name it in ``clear`` (``null`` means
    "unchanged", never "clear").
    Unknown members (``name``, ``allowed_tools``, ``memory``, ``skills``, ``tools``,
    ``system_key``, …) are refused with 422 before anything is read or written: the
    protected part of the spec is not reachable from a request body.
    """

    model_config = ConfigDict(extra="forbid")

    model_id: str | None = Field(default=None, min_length=1, max_length=200)
    model_source: ModelSource | None = None
    # per model call — bedrockModelConfig.maxTokens, not a spend cap
    max_tokens: int | None = Field(default=None, ge=1, le=MAX_TOKENS_CEILING)
    reasoning_effort: ReasoningEffort | None = None
    system_prompt: str | None = Field(default=None, min_length=1, max_length=20000)
    max_iterations: int | None = Field(default=None, ge=1, le=100)
    timeout_seconds: int | None = Field(default=None, ge=10, le=3600)
    # Existing, already-authorized knowledge bases only — never created here.
    knowledge_bases: list[KnowledgeBaseRef] | None = Field(default=None, max_length=10)
    # Members to return to the preset's defaults (explicit, never implied).
    reset: list[EditableField] = Field(default_factory=list, max_length=len(EDITABLE_FIELDS))
    # The two optional knobs can also be *cleared* (send nothing to the model) — e.g.
    # the reasoning effort must go when the model moves to a non-OpenAI one.
    clear: list[ClearableField] = Field(default_factory=list, max_length=2)
    # Re-publish even when nothing changed (e.g. after an out-of-band AWS change).
    force: bool = False

    @model_validator(mode="after")
    def _reset_clear_and_value_are_exclusive(self) -> "InstallRequest":
        named = [*self.reset, *self.clear]
        both = [name for name in named if getattr(self, name) is not None]
        if both:
            raise ValueError(
                f"{', '.join(both)}: a member cannot be both reset/cleared and set in one request"
            )
        if len(set(named)) != len(named):
            raise ValueError("reset/clear name a member twice")
        return self

    def edit(self) -> PresetEdit | None:
        """The partial edit this body describes; ``None`` ⇔ nothing to change (repair
        with what is stored / install with the defaults). Resolution against the
        stored spec is the service's job, inside the claiming transaction."""
        values = {
            name: getattr(self, name)
            for name in EDITABLE_FIELDS
            if getattr(self, name) is not None
        }
        if not values and not self.reset and not self.clear:
            return None
        return PresetEdit(values=values, reset=tuple(self.reset), clear=tuple(self.clear))


def _preset(preset_key: str):
    preset = catalogue.get_preset(preset_key)
    if preset is None:
        raise NotFoundError("system_agent.unknown", f"no system preset named '{preset_key}'")
    return preset


@router.get("")
def list_system_agents(
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    identity = require_identity(request)
    return {
        "workspace_id": ws.id,
        "presets": service.list_status(db, ws.row, is_admin=identity.is_admin),
    }


@router.post("/{preset_key}/install")
def install_system_agent(
    preset_key: str,
    req: InstallRequest,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> Any:
    from fastapi.responses import JSONResponse

    require_admin(request)  # belt and braces with ROUTE_POLICY
    preset = _preset(preset_key)
    stored = service.find_installed(db, ws.id, preset)
    edit = req.edit()
    if edit is not None:
        # preflight 422 before any row, job or AWS call; the service re-resolves and
        # re-validates against the row it actually claims
        service.validate_options(preset, edit.resolve(preset, stored.spec if stored else None))
    outcome = service.install_preset(db, ws.row, preset, edit, force=req.force)
    db.commit()
    if outcome.job is not None and outcome.changed:  # only the claim winner launches
        start_deploy_async(outcome.job.id)
    body = {
        "agent": _agent_out(outcome.agent, outcome.deployment),
        "job_id": outcome.job.id if outcome.job else None,
        "deployment_id": outcome.deployment.id if outcome.deployment else None,
        "created": outcome.created,
        "changed": outcome.changed,
        "preset": service.preset_status(db, ws.row, preset, is_admin=True),
    }
    return JSONResponse(status_code=outcome.status_code, content=body)


@router.post("/{preset_key}/skill-registration")
def register_system_skill_record(
    preset_key: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Register — or verify, or roll forward — the installed preset's published Skill
    release as its own Registry record (SE-043). No request body: the resource is
    server-selected (the release the stored spec pins, proven against this build and
    read back from S3). Nothing is uploaded, the Harness is not re-published and the
    agent's A2A record is untouched. A first registration lands in the normal review
    queue (submitted, not approved); an identical repeat is a no-op that keeps the
    record's approval; a newer release updates the descriptor and needs review again.
    """
    require_admin(request)  # belt and braces with ROUTE_POLICY
    preset = _preset(preset_key)
    agent = service.find_installed(db, ws.id, preset)
    if agent is None:
        raise NotFoundError(
            "system_agent.not_installed", f"'{preset_key}' is not installed in this workspace"
        )
    if agent.status != "active":
        raise AppError(
            "system_skill.preset_not_active",
            f"the preset is {agent.status}; the Skill is registered once the preset is active "
            "(a deploy registers it in its register stage)",
            {"agent_status": agent.status},
            status_code=409,
        )
    outcome = skill_registry.register_system_skill(db, ws.context, preset, agent)
    _invalidate_attachables(ws.id)  # a changed or new record must not serve a stale catalog
    return {
        "preset_key": preset.key,
        "record": _record_out(outcome.record, skill_registry.record_projection(outcome.row)),
        "created": outcome.created,
        "changed": outcome.changed,
        "submitted": outcome.submitted,
        "note": outcome.note,
        "skill": {
            "name": preset.name,
            "version": outcome.row.release_version,
            "digest": (outcome.row.release_digest or "")[:12],
            "path": outcome.row.s3_uri,
            "files": _files_of(outcome.record),
        },
        "preset": service.preset_status(db, ws.row, preset, is_admin=True),
    }


def _files_of(record: dict[str, Any]) -> list[str]:
    import json

    try:
        definition = json.loads(
            record["descriptors"]["agentSkills"]["skillDefinition"]["inlineContent"]
        )
        return [str(f) for f in definition.get("files") or []]
    except (KeyError, TypeError, ValueError):
        return []


@router.delete("/{preset_key}")
def uninstall_system_agent(
    preset_key: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    from fastapi.responses import JSONResponse

    require_admin(request)
    preset = _preset(preset_key)
    outcome = service.uninstall_preset(db, ws.row, preset)
    # The claim owner launches the worker. A repeated request for a job that is still
    # QUEUED launches one too: its worker may have given up waiting for a retiring
    # predecessor's lock, and a queued job must never depend on an app restart. Two
    # workers on one queued job are harmless — the queued→running CAS admits exactly
    # one, the other exits; one thread per request, never a loop.
    if outcome.started or outcome.job.status == "queued":
        start_uninstall_async(outcome.job.id)
    return JSONResponse(
        status_code=202,
        content={
            "agent": _agent_out(outcome.agent),
            "job_id": outcome.job.id,
            "operation": "uninstall",
            "attempt": outcome.attempt,
            "started": outcome.started,
            "preset": service.preset_status(db, ws.row, preset, is_admin=True),
        },
    )
