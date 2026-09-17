"""Owner-bound resource preparation and bounded, advisory intake requirements.

Selections and imported source metadata are conversation state, never additions to
the proposal schema/hash. Only explicit member actions upload validated Skill bundles.
"""

import json
import re
import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import update
from sqlalchemy.orm import Session, object_session

from app.assistant import proposal
from app.assistant.principal import principal_of
from app.core.errors import AppError
from app.models.assistant import AssistantConversation, AssistantProposal
from app.routers.auth import Identity
from app.services.workspace import WorkspaceContext

FENCE = "launchpad-preparation"
MAX_BYTES = 24_000
REJECTED_NAME = "preparation_rejected"
_OPEN = re.compile(r"```" + FENCE + r"(?=[ \t\r\n]|$)")
_BLOCK = re.compile(r"```" + FENCE + r"[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.DOTALL)


class Requirement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Annotated[str, Field(min_length=1, max_length=64)]
    kind: Literal["knowledge_base", "skill", "tool", "clarification"]
    title: Annotated[str, Field(min_length=1, max_length=200)]
    reason: Annotated[str, Field(min_length=1, max_length=1000)]
    materials: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(
        default_factory=list, max_length=10
    )
    required: bool = Field(default=False, strict=True)


class Requirements(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirements: list[Requirement] = Field(max_length=20)

    @model_validator(mode="after")
    def unique_ids(self) -> "Requirements":
        if len({item.id for item in self.requirements}) != len(self.requirements):
            raise ValueError("requirement ids must be unique")
        return self


class SelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0, strict=True)
    knowledge_bases: list[proposal.Key] = Field(max_length=10)
    skills: list[proposal.Key] = Field(max_length=10)
    # Older clients only edit KB/Skill selections. Omission must not detach MCPs.
    tools: list[proposal.Key] | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def unique_keys(self) -> "SelectionRequest":
        for field in ("knowledge_bases", "skills", "tools"):
            keys = getattr(self, field) or []
            if len(keys) != len(set(keys)):
                raise ValueError(f"{field} must not repeat an entry")
        return self


class ImportSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int = Field(ge=0, strict=True)


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0, strict=True)
    staging_id: str = Field(min_length=1, max_length=200)
    selections: list[ImportSelection] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def unique_indexes(self) -> "ImportRequest":
        if len({s.index for s in self.selections}) != len(self.selections):
            raise ValueError("selections must not repeat an index")
        return self


def extract(text: str) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """Absent → no change; malformed → advisory failure, independent of proposals."""
    starts, blocks = _OPEN.findall(text), _BLOCK.findall(text)
    if not starts:
        return None, []
    if len(starts) != 1 or len(blocks) != 1:
        return None, ["preparation reply must contain exactly one complete fenced block"]
    if len(blocks[0].encode("utf-8")) > MAX_BYTES:
        return None, [f"preparation block exceeds {MAX_BYTES} bytes"]
    try:
        parsed = Requirements.model_validate_json(blocks[0])
        return [item.model_dump() for item in parsed.requirements], []
    except ValidationError as exc:
        return None, [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()][:20]


def view(db: Session, row: AssistantConversation) -> dict[str, Any]:
    state = row.preparation or {}
    kbs, skills = state.get("knowledge_bases", []), state.get("skills", [])
    tools = state.get("tools", [])
    if not state.get("selection_set") or not state.get("tools_selection_set"):
        latest = (
            db.query(AssistantProposal)
            .filter(AssistantProposal.conversation_id == row.id,
                    AssistantProposal.validation_errors == [],
                    AssistantProposal.bindings.isnot(None))
            .order_by(AssistantProposal.revision.desc())
            .first()
        )
        if latest is not None and not latest.validation_errors:
            if not state.get("selection_set"):
                kbs = latest.content.get("knowledge_bases") or []
                skills = latest.content.get("skills") or []
            if not state.get("tools_selection_set"):
                tools = latest.content.get("tools") or []
    return {"revision": state.get("revision", 0), "knowledge_bases": list(kbs),
            "skills": list(skills), "tools": list(tools),
            "requirements": state.get("requirements") or []}


def require_idle(row: AssistantConversation, *, token: str | None = None) -> None:
    if row.active_turn is not None:
        raise AppError("assistant.turn_in_progress", "wait for the current turn to finish",
                       {"active_turn": row.active_turn}, status_code=409)
    require_no_import(row, token=token)


def require_no_import(row: AssistantConversation, *, token: str | None = None) -> None:
    if row.preparation_token and row.preparation_token != token:
        raise AppError("assistant.preparation_in_progress",
                       "resource preparation is in progress; wait for it to finish",
                       status_code=409)


def check_revision(row: AssistantConversation, expected: int) -> None:
    current = (row.preparation or {}).get("revision", 0)
    if current != expected:
        raise AppError("assistant.preparation_stale",
                       "resource preparation changed; reload the conversation",
                       {"current_revision": current}, status_code=409)


def live_catalog(
    workspace: WorkspaceContext, sources: list[dict[str, Any]]
) -> dict[str, Any]:
    """Merge only server-owned sources and re-read their real bytes on EVERY call."""
    from app.assistant import service

    catalog = service.fetch_catalog(workspace)
    catalog = {**catalog, "skills": list(catalog.get("skills") or []),
               "warnings": list(catalog.get("warnings") or [])}
    for source in sources:
        entry = {k: source[k] for k in ("key", "name", "path", "description")}
        entry["record_id"] = None
        snapshot = None
        try:
            snapshot = service.skill_content_snapshot(workspace, source["path"])
        except Exception as exc:
            catalog["warnings"].append(
                f"imported skill '{source['name']}' content unreadable: {service._short(exc)}"
            )
        entry.update(snapshot or {})
        if not snapshot:
            catalog["warnings"].append(f"imported skill '{source['name']}' has no readable bundle")
        catalog["skills"].append(entry)
    return catalog


def revise_latest(db: Session, row: AssistantConversation, created_by: str) -> None:
    """A changed selection/binding yields a fresh review; approvals remain untouched."""
    from app.assistant import service

    latest = service.latest_proposal(db, row.id)
    if latest is None or latest.status not in ("draft", "approved", "invalid"):
        return
    # Check shape without rejecting old capability conflicts: applying the new
    # selections below can repair them (for example, removing an MCP from a
    # zero-call proposal). The merged content still receives full validation.
    try:
        proposal.ProposalContent.model_validate(latest.content)
    except ValidationError:
        return
    selected = view(db, row)
    raw = {**latest.content, "knowledge_bases": selected["knowledge_bases"],
           "skills": selected["skills"], "tools": selected["tools"]}
    content, display, _errors = proposal.validate(raw, row.catalog or {})
    bindings = proposal.resource_bindings(content, row.catalog or {}) if content else None
    if proposal.revision_hash(display, bindings) == latest.content_hash:
        return
    # record_proposal also preserves failures as an explicitly invalid new revision.
    tools_selection_set = bool((row.preparation or {}).get("tools_selection_set"))
    service.record_proposal(db, row.id, row.catalog or {}, row.workspace_id, raw,
                            source="member", created_by=created_by)
    # Refreshes and old-client KB/Skill saves create member revisions, but are not
    # explicit MCP edits. Preserve inheritance until tools are actually selected.
    row.preparation = {**(row.preparation or {}), "tools_selection_set": tools_selection_set}


def save_selection(
    db: Session, row: AssistantConversation, workspace: WorkspaceContext,
    identity: Identity, request: SelectionRequest,
) -> AssistantConversation:
    from app.assistant import service

    require_idle(row)
    check_revision(row, request.expected_revision)
    cid, principal = row.id, principal_of(identity)
    sources = list(row.preparation_sources or [])
    selected_tools = request.tools if request.tools is not None else view(db, row)["tools"]
    catalog = live_catalog(workspace, sources)  # no write lock across AWS reads
    # Reuse the exact Harness mount contract; no invented paths or gateway provisioning.
    candidate = proposal.ProposalContent(
        name="prepared-agent", system_prompt="Resource preparation",
        knowledge_bases=request.knowledge_bases, skills=request.skills, tools=selected_tools,
    )
    errors = proposal.reference_errors(candidate, catalog)
    if errors:
        raise AppError("assistant.preparation_invalid", "; ".join(errors),
                       {"errors": errors}, status_code=409)
    service._lock_conversation(db, cid)
    try:
        row = service.owned_conversation(db, workspace.id, principal, cid)
        require_idle(row)
        check_revision(row, request.expected_revision)
        state = view(db, row)
        row.preparation = {**(row.preparation or {}), **state, "selection_set": True,
                           "tools_selection_set": request.tools is not None
                           or bool((row.preparation or {}).get("tools_selection_set")),
                           "revision": state["revision"] + 1,
                           "knowledge_bases": request.knowledge_bases, "skills": request.skills,
                           "tools": selected_tools}
        row.catalog = catalog
        revise_latest(db, row, identity.username)
        db.commit()
        return row
    except Exception:
        db.rollback()
        raise


def import_skills(
    db: Session, row: AssistantConversation, workspace: WorkspaceContext,
    identity: Identity, request: ImportRequest, *, recheck=None,
) -> tuple[AssistantConversation, list[dict[str, Any]]]:
    """Claim → validated staged upload → persist private sources → live read → review.

    Successful source entries remember staging/index for retries after partial failure.
    No network call holds the SQLite write lock. The claim blocks competing mutations.
    """
    from app.assistant import service
    from app.routers import agent_skills, registry

    if not identity.can(service.PERMISSION_DEPLOY):
        raise AppError("auth.permission_required", "Skill import requires agents.deploy",
                       {"permission": service.PERMISSION_DEPLOY}, status_code=403)
    cid, principal, token = row.id, principal_of(identity), uuid.uuid4().hex
    service._lock_conversation(db, cid)
    try:
        row = service.owned_conversation(db, workspace.id, principal, cid)
        require_idle(row)
        check_revision(row, request.expected_revision)
        state = view(db, row)
        sources = list(row.preparation_sources or [])
        known = {s["index"]: s for s in sources if s.get("staging_id") == request.staging_id}
        new_count = sum(s.index not in known for s in request.selections)
        additions = sum(s.index not in known or known[s.index]["key"] not in state["skills"]
                        for s in request.selections)
        if len(state["skills"]) + additions > 10 or len(sources) + new_count > 30:
            raise AppError("assistant.preparation_full",
                           "select at most 10 Skills and import at most 30 per conversation",
                           status_code=409)
        row.preparation_token = token
        db.commit()
    except Exception:
        db.rollback()
        raise
    results: list[dict[str, Any]] = []
    try:
        registry._sweep_staging()
        entry = registry._staging.get(request.staging_id)
        if new_count and (
            entry is None or entry.get("owner_principal") != principal
            or entry.get("workspace_id") != workspace.id
        ):
            raise AppError("registry.staging_expired", "staging session expired or unknown",
                           status_code=410)
        for selected in request.selections:
            source = known.get(selected.index)
            if source is None:
                # Re-resolve permission/grant before each explicit upload.
                if recheck is not None:
                    caller, fresh_ws = recheck(db)
                    if principal_of(caller) != principal or fresh_ws.id != workspace.id:
                        raise AppError("assistant.conversation_not_found",
                                       "conversation not found", status_code=404)
                imported = agent_skills.import_staged_skills(
                    request.staging_id, [registry.ImportSelection(index=selected.index)],
                    workspace, consume=False,
                )["skills"][0]
                if not imported["ok"]:
                    results.append({k: imported[k] for k in ("name", "ok", "error")})
                    continue
                source = {
                    **{k: imported[k] for k in ("name", "path", "description")},
                    "key": f"imported:{uuid.uuid4().hex}",
                    "staging_id": request.staging_id, "index": selected.index,
                }
                sources.append(source)
                # Keep successful uploads attributable even if a later catalog read fails.
                service._lock_conversation(db, cid)
                row = service.owned_conversation(db, workspace.id, principal, cid)
                if row.preparation_token != token:
                    raise AppError("assistant.preparation_stale", "import claim lost",
                                   status_code=409)
                row.preparation_sources = list(sources)
                db.commit()
            results.append({"name": source["name"], "ok": True, "key": source["key"]})
        catalog = live_catalog(workspace, sources)
        service._lock_conversation(db, cid)
        row = service.owned_conversation(db, workspace.id, principal, cid)
        require_idle(row, token=token)
        check_revision(row, request.expected_revision)
        if row.preparation_token != token:
            raise AppError("assistant.preparation_stale", "import claim lost", status_code=409)
        # Only readable bundles can become selected resources.
        readable = {s["key"] for s in catalog["skills"] if s.get("content_digest")}
        for result in results:
            if result["ok"] and result["key"] not in readable:
                result.update(
                    ok=False,
                    error="Skill files were uploaded but could not be verified. "
                    "Refresh or retry this import before adding it to the plan.",
                )
        added = [r["key"] for r in results if r["ok"] and r["key"] in readable]
        state["skills"] = list(dict.fromkeys(state["skills"] + added))
        row.preparation = {**(row.preparation or {}), **state,
                           "selection_set": True, "revision": state["revision"] + 1}
        row.catalog = catalog
        revise_latest(db, row, identity.username)
        row.preparation_token = None
        db.commit()
        if results and all(r["ok"] for r in results):
            registry._drop_staging(request.staging_id)
        return row, results
    finally:
        db.rollback()
        db.execute(update(AssistantConversation)
                   .where(AssistantConversation.id == cid,
                          AssistantConversation.preparation_token == token)
                   .values(preparation_token=None))
        db.commit()


def context(row: AssistantConversation) -> str:
    state = row.preparation or {}
    db = object_session(row)
    selected = view(db, row) if db is not None else state
    values = {k: selected.get(k, []) for k in ("knowledge_bases", "skills", "tools")}
    if not state.get("selection_set"):
        if not any(values.values()):
            return ""
        return (
            "\n\n## Resource selections of the latest valid proposal\n"
            "Carry this baseline forward unless the member asks to change it.\n"
            + json.dumps(values, ensure_ascii=False)
        )
    return (
        "\n\n## Member-selected preparation resources\n"
        "These are the member's current resource selections. Preserve the explicit lists "
        "in every proposal. Discuss changes and ask the member to update the preparation "
        "panel; never silently drop or replace a selected resource.\n"
        + ("MCP tools are explicitly selected, including an empty list.\n"
           if state.get("tools_selection_set")
           else "MCP tools inherit the latest valid proposal; change them only on request.\n")
        + json.dumps(values, ensure_ascii=False)
    )
