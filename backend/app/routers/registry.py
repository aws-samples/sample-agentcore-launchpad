"""Registry console API — records per type, register, lifecycle actions,
attachables catalog, search, defaults sync, multi-source skill ingestion.

Backed by the ``RegistryBackend`` Protocol (``app.services.registry_backend``).
Routes that map 1:1 onto the Protocol call it; the console-only surfaces that the
AWS-era service module provided (register/edit/reimport, skill import, the
attachables catalog, defaults sync) are implemented in ``app.services.registry_console``
on top of the Protocol.
"""

import secrets
import time
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any, Literal

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile

from app.assistant.principal import principal_of
from app.core.errors import AppError, NotFoundError, aws_error_message, mapped_aws_error
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import creators, registry_backend
from app.services import registry_console as console
from app.services import skill_ingest as si
from app.services.skill_ingest import (
    SKILL_BUNDLE_MAX_BYTES,
    SkillBundle,
    bundle_errors,
    bundle_from_zip,
)
from app.services.visibility import Viewer, require_viewer

router = APIRouter(prefix="/api/registry", tags=["registry"])


def _hidden_record_ids(ws: WorkspaceScope, viewer: Viewer) -> frozenset[str]:
    """Registry record ids of agents hidden from ``viewer`` by workspace isolation
    (an A2A record is the agent's: its deploy ``register`` stage created it). Every
    other record kind stays shared. No ledger read for a viewer who does not filter."""
    if not viewer.filters:
        return frozenset()
    from app.core.db import SessionLocal
    from app.services.agent_access import HiddenAgents

    db = SessionLocal()
    try:
        return HiddenAgents(db, viewer, ws.id).record_ids
    finally:
        db.close()


def _require_record_visible(ws: WorkspaceScope, viewer: Viewer, record_id: str) -> None:
    """A hidden agent's A2A record answers like a missing record — before the backend."""
    if record_id in _hidden_record_ids(ws, viewer):
        raise NotFoundError("registry.record_not_found", "registry record not found")


def _visible_records(
    ws: WorkspaceScope, viewer: Viewer, records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    hidden = _hidden_record_ids(ws, viewer)
    if not hidden:
        return records
    return [r for r in records if str(r.get("recordId") or "") not in hidden]


def _require_record_mutable(
    ws: WorkspaceScope, request: Request, record_id: str, action: str
) -> dict[str, Any]:
    """Creator-or-admin on a registry record's content mutations (N15-obs7, contract §8):
    reads stay shared, but `PUT` / `reimport` / `DELETE` are the creator's or an
    administrator's — 403 ``registry.forbidden {created_by, created_by_name, record_id,
    action}`` before any side effect; a NULL creator (legacy, deploy-created, imported,
    platform default) makes the record an administrator's. Runs AFTER the visibility 404
    and the system-skill refusal (both learn nothing new) and BEFORE every type / status
    check, so a forbidden caller learns nothing about the record's state. Returns the
    record so the caller does not read it twice."""
    record = registry_backend.get_backend().get_record(ws.context, record_id)
    console.require_record_mutable(record, require_identity(request), record_id, action)
    return record


@contextmanager
def _stamping_creator(request: Request):
    """Records created inside the block carry the caller as their creator
    (``registry_backend.current_creator``; the local backend stamps the new row)."""
    token = registry_backend.current_creator.set(creators.Creator.of_request(request))
    try:
        yield
    finally:
        registry_backend.current_creator.reset(token)


# Per workspace: the catalog is that environment's registry + gateways.
_attachables_cache: dict[str, dict[str, Any]] = {}

# Skill inspect→import staging. inspect() acquires + validates bundles into a
# server-side temp dir and parks them here under a random id; import() consumes
# by id. Single-process uvicorn (this project's deploy shape) → an in-process
# dict is sufficient; no Redis. TTL keeps abandoned uploads/clones from leaking.
_STAGING_TTL_S = 600  # 10 minutes
_staging: dict[str, dict[str, Any]] = {}  # id → {"bundles": [SkillBundle], "expires": float}


def _invalidate_attachables(workspace_id: str) -> None:
    _attachables_cache.pop(workspace_id, None)


def _record_out(
    record: dict[str, Any], system: dict[str, Any] | None = None
) -> dict[str, Any]:
    """API projection. ``system`` is the server-owned SE-043 member — derived from the
    workspace's ledger mapping by record id (``_with_system``), never from the
    record's descriptors or tags, so a client payload cannot claim protection."""
    return {
        "system": system,
        "record_id": record.get("recordId"),
        "name": record.get("name"),
        "description": record.get("description", ""),
        "type": record.get("descriptorType"),
        "status": record.get("status"),
        "status_reason": record.get("statusReason"),
        "version": record.get("recordVersion"),
        "descriptors": record.get("descriptors"),
        "created_at": str(record.get("createdAt", "")) or None,
        "updated_at": str(record.get("updatedAt", "")) or None,
        # N15-obs7: the creator (`services/creators`); NULL = an administrator's record
        "created_by": record.get("createdBy"),
        "created_by_name": record.get("createdByName"),
    }


def _with_system(ws: WorkspaceScope, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Scoped to the workspace AND its current registry identity: a mapping left by a
    replaced registry never classifies a same-id record in the new one."""
    from app.system_agents.skill_registry import projections_for

    owned = projections_for(ws.context, [str(r.get("recordId") or "") for r in records])
    return [_record_out(r, owned.get(str(r.get("recordId") or ""))) for r in records]


def _one_with_system(ws: WorkspaceScope, record: dict[str, Any]) -> dict[str, Any]:
    return _with_system(ws, [record])[0]


@router.get("/records")
def list_records(
    type: str | None = None,
    status: str | None = None,
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    backend = registry_backend.get_backend()
    try:
        records = backend.list_records(ws.context, type, status)
    except ValueError as exc:
        raise AppError("registry.bad_type", str(exc), status_code=422) from exc
    return {"records": _with_system(ws, _visible_records(ws, viewer, records))}


@router.get("/records/search")
def search(
    q: str,
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    backend = registry_backend.get_backend()
    records = _visible_records(ws, viewer, backend.search_records(ws.context, q))
    return {"records": _with_system(ws, records)}


def _discoverable_out(record: dict[str, Any]) -> dict[str, Any]:
    """A data-plane summary: the publisher projection plus the consumer-facing
    fields, minus ``descriptors`` (the discovery API never returns them)."""
    out = _record_out(record)
    out.pop("descriptors", None)
    out["display_name"] = record.get("displayName")
    out["descriptor_types"] = list(record.get("descriptorTypes") or [])
    return out


@router.get("/records/discoverable")
def list_discoverable(
    type: str | None = None,
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Consumer view of `launchpad-registry`: the records a consumer or agent with
    data-plane access can actually discover (`ListDiscoverableRegistryRecords`,
    paginated to completion). Control-plane records missing from this list are the
    ones approval has not exposed yet (DRAFT / PENDING_APPROVAL / REJECTED /
    DEPRECATED). Read-only; `descriptors` are never part of a summary — open a
    record (`GET /records/{record_id}`) for the payload.
    """
    backend = registry_backend.get_backend()
    try:
        records = backend.list_discoverable_records(ws.context, type)
    except ValueError as exc:  # unknown ?type= — a caller mistake, not a 500
        raise AppError("registry.bad_type", str(exc), status_code=422) from exc
    records = _visible_records(ws, viewer, records)
    return {"records": [_discoverable_out(r) for r in records], "count": len(records)}


# N17-obs267: the status buckets of `GET /records/counts`; any other status (DEPRECATED,
# a transient CREATING / UPDATING) counts only towards `total` — and `deprecated` for the one
# terminal status the console lists.
_COUNT_BUCKETS = {
    "PENDING_APPROVAL": "pending",
    "APPROVED": "approved",
    "DRAFT": "draft",
    "REJECTED": "rejected",
    "DEPRECATED": "deprecated",
}


@router.get("/records/counts")
def record_counts(
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, int]:
    """Per-status record counts of the workspace registry (N17-obs267, user request
    2026-10-22): the sidebar's pending-approval badge and the list's status tabs.

    Same visibility as ``GET /records`` — a hidden agent's A2A record is not counted for
    a member under isolation. Computed from the one ``list_records`` call the list route
    makes (the Protocol has no count primitive and the hidden-record filter is by id), but
    without the per-record system projection or the response payload: one backend read,
    one ledger read only for a filtering viewer."""
    records = _visible_records(ws, viewer, registry_backend.get_backend().list_records(ws.context))
    out = dict.fromkeys(_COUNT_BUCKETS.values(), 0)
    for record in records:
        bucket = _COUNT_BUCKETS.get(str(record.get("status") or "").upper())
        if bucket:
            out[bucket] += 1
    out["total"] = len(records)
    return out


@router.get("/records/{record_id}")
def get_record(
    record_id: str,
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _require_record_visible(ws, viewer, record_id)
    backend = registry_backend.get_backend()
    return _one_with_system(ws, backend.get_record(ws.context, record_id))


@router.get("/records/{record_id}/live-agent-card")
def live_agent_card(
    record_id: str,
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """The A2A card the runtime behind this record serves right now (GetAgentCard).

    Record → ledger agent → ``Agent.arn`` is resolved here, server-side; the
    browser only names the record. Every refusal is decided on the ledger before
    AWS is called: a record no Launchpad agent owns (404), an agent that is not an
    A2A server (409), an agent not yet deployed / no longer active (409). The
    data plane's ``ClientError`` keeps the standard 4xx envelope; a runtime-side
    failure the envelope has no mapping for is a 502, never a bare 500.

    Note: ``GetAgentCard`` is Runtime A2A data-plane; its availability in the China
    Regions is unverified — a refusal there surfaces as this envelope, never a 500.
    """
    from app.core.db import SessionLocal
    from app.models.ledger import Agent
    from app.services.agent_access import agent_clause

    db = SessionLocal()
    try:
        agent = (
            db.query(Agent)
            .filter(
                Agent.registry_record_id == record_id,
                Agent.workspace_id == ws.id,
                Agent.status != "deleted",
                # a hidden agent's record reads as one no Launchpad agent owns
                agent_clause(viewer),
            )
            .order_by(Agent.updated_at.desc())
            .first()
        )
        if agent is None:
            raise AppError(
                "registry.record_not_deployed",
                "no Launchpad agent owns this registry record",
                status_code=404,
            )
        if (agent.spec or {}).get("protocol") != "a2a":
            raise AppError(
                "registry.record_not_a2a",
                "the agent behind this record is not an A2A server",
                detail={"agent_id": agent.id, "protocol": (agent.spec or {}).get("protocol")},
                status_code=409,
            )
        if agent.status != "active" or not agent.arn:
            raise AppError(
                "registry.agent_not_ready",
                f"agent is {agent.status}, not deployed and active",
                detail={"agent_id": agent.id, "status": agent.status},
                status_code=409,
            )
        db.expunge(agent)
    finally:
        db.close()

    try:
        return console.console_live_agent_card(ws.context, record_id, agent)
    except ClientError as exc:
        if mapped_aws_error(exc) is not None:
            raise  # standard 4xx envelope (not found / access denied / throttled …)
        raise AppError(
            "registry.live_card_failed",
            aws_error_message(exc),
            detail={"aws_error_code": exc.response.get("Error", {}).get("Code")},
            status_code=502,
        ) from exc


class ActionRequest(BaseModel):
    action: str  # submit | approve | publish | reject | disable


# N17-obs248 (user ruling 2026-10-22): the review decision is an administrator's. A member
# submits (DRAFT/REJECTED → PENDING_APPROVAL); approve / publish / reject / disable answer
# 403 ``registry.approval_admin_only`` for a member. The route itself stays MEMBER in
# ROUTE_POLICY because submit is a member operation — the gate is per action, here.
ADMIN_ONLY_ACTIONS = frozenset({"approve", "publish", "reject", "disable"})


@router.post("/records/{record_id}/action")
def record_action(
    record_id: str,
    req: ActionRequest,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    identity = require_identity(request)
    # The caller's role reaches the guard explicitly: a system-managed Skill's
    # lifecycle is administrator-only (its submit too) and keeps its own, more specific
    # code. A ledger-only check — system Skills are never hidden records, so answering it
    # first reveals nothing. It runs BEFORE the backend lookup (403 before 503, like
    # memory_resources).
    from app.system_agents.skill_registry import refuse_protected_mutation

    refuse_protected_mutation(
        ws.context, record_id, req.action, is_admin=identity.is_admin, lifecycle=True
    )
    # N17-obs248: approve / publish / reject / disable are an administrator's decision.
    # Refused for a member BEFORE the visibility check (rev-r23: every member decision
    # answers 403, so it is no exists-but-hidden oracle) and before any backend call.
    if req.action in ADMIN_ONLY_ACTIONS and not identity.is_admin:
        raise AppError(
            "registry.approval_admin_only",
            f"only an administrator can {req.action} a registry record — submit it for "
            "approval instead",
            status_code=403,
            detail={"action": req.action},
        )
    _require_record_visible(ws, viewer, record_id)
    backend = registry_backend.get_backend()
    actions = {
        "submit": backend.submit_record,
        "approve": backend.approve_record,
        "publish": backend.approve_record,
        "reject": backend.reject_record,
        "disable": backend.disable_record,
    }
    operation = actions.get(req.action)
    if operation is None:
        raise AppError(
            "registry.unknown_action", f"unknown action '{req.action}'", status_code=400
        )
    token = registry_backend.current_actor.set(identity.username)
    try:
        operation(ws.context, record_id)
    finally:
        registry_backend.current_actor.reset(token)
    _invalidate_attachables(ws.id)
    return _one_with_system(ws, backend.get_record(ws.context, record_id))


class RegisterRequest(BaseModel):
    """Console-side registration: external MCP servers and skills. A2A records
    are never registered by hand — deploys create and refresh them."""

    type: Literal["MCP", "AGENT_SKILLS"]
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,63}$")
    description: str = Field(default="", max_length=500)
    url: str | None = None  # MCP: streamable-http endpoint
    skill_md: str | None = Field(default=None, max_length=102400)  # AGENT_SKILLS (AWS cap)


@router.post("/records", status_code=201)
def register_record(
    req: RegisterRequest, request: Request, ws: WorkspaceScope = Depends(require_workspace)
) -> dict[str, Any]:
    if req.type == "MCP":
        if not req.url or not req.url.startswith(("https://", "http://")):
            raise AppError(
                "registry.invalid_url",
                "MCP registration needs a http(s) streamable-http server URL",
                status_code=400,
            )
        # N17-obs256: the platform gateway's records come from the import path only
        console.refuse_platform_gateway_url(ws.context, req.url)
        with _stamping_creator(request):
            record = console.register_mcp_server(
                ws.context, req.name, req.description, req.url
            )
        _invalidate_attachables(ws.id)
        return _record_out(record)
    if not req.skill_md or not req.skill_md.strip():
        raise AppError(
            "registry.skill_md_required",
            "skill registration needs SKILL.md content",
            status_code=400,
        )
    with _stamping_creator(request):
        record = console.register_skill(ws.context, req.name, req.description, req.skill_md)
    _invalidate_attachables(ws.id)
    return _record_out(record)


def _sweep_staging() -> None:
    now = time.time()
    for sid, entry in list(_staging.items()):
        if entry["expires"] <= now:
            _staging.pop(sid, None)
            for bundle in entry["bundles"]:
                bundle.close()


def _stage(bundles: list[SkillBundle]) -> str:
    _sweep_staging()
    sid = secrets.token_urlsafe(16)
    _staging[sid] = {"bundles": bundles, "expires": time.time() + _STAGING_TTL_S}
    return sid


def _drop_staging(sid: str) -> None:
    entry = _staging.pop(sid, None)
    if entry:
        for bundle in entry["bundles"]:
            bundle.close()


def _skill_out(bundle: SkillBundle, errors: list[str], index: int) -> dict[str, Any]:
    return {
        "index": index,
        "name": bundle.name,
        "description": bundle.description,
        "version": bundle.version,
        "files": bundle.files,
        "skill_md_excerpt": bundle.skill_md[:4000],
        "source": asdict(bundle.source),
        "valid": not errors,
        "errors": errors,
    }


@router.post("/skills/inspect")
async def inspect_skill(
    request: Request, ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Acquire + validate skill bundles from a source without touching S3, park
    them in staging, and return the parsed skills for preview. Accepts either a
    multipart ``.zip`` upload or a JSON ``{"source": {...}}`` body (git for now);
    a monorepo git source yields multiple skills. The whole request is refused
    (4xx) when no skill is importable."""
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        bundles = await _acquire_zip(request)
    elif content_type.startswith("application/json"):
        try:
            body = await request.json()
        except ValueError:  # malformed JSON body → clean 400, not an unhandled 500
            raise AppError(
                "registry.invalid_upload", "malformed JSON body", status_code=400
            ) from None
        if not isinstance(body, dict):
            raise AppError(
                "registry.invalid_upload", "expected a JSON object body", status_code=400
            )
        # git clone / URL fetch: blocking network + disk I/O, off the event loop
        # (N-SCN-obs13 audit — same pattern as `routers/agents.py::upload_byoc_artifact`).
        bundles = await run_in_threadpool(_acquire_source, body.get("source") or {})
    else:
        raise AppError(
            "registry.invalid_upload",
            "expected a multipart .zip upload or a JSON source",
            status_code=400,
        )
    response = await run_in_threadpool(_stage_and_respond, bundles)
    # Assistant imports require the same immutable principal and workspace that
    # inspected the source. No client-supplied source paths cross this boundary.
    _staging[response["staging_id"]].update(
        owner_principal=principal_of(require_identity(request)), workspace_id=ws.id,
    )
    return response


async def _acquire_zip(request: Request) -> list[SkillBundle]:
    form = await request.form()
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        raise AppError("registry.invalid_upload", "expected a .zip file", status_code=400)
    if not (upload.filename or "").lower().endswith(".zip"):
        raise AppError("registry.invalid_upload", "expected a .zip file", status_code=400)
    data = await upload.read()
    if len(data) > SKILL_BUNDLE_MAX_BYTES:
        raise AppError(
            "registry.upload_too_large",
            f"upload exceeds the {SKILL_BUNDLE_MAX_BYTES} byte limit",
            status_code=413,
        )
    # Extracts to a temp dir on disk — blocking, so off the event loop (N-SCN-obs13);
    # archive-safety violations raise here (422).
    return [await run_in_threadpool(bundle_from_zip, data)]


def _acquire_source(source: dict[str, Any]) -> list[SkillBundle]:
    """Dispatch a JSON source descriptor to its acquirer. The token (git private
    repos) is used transiently here and never stored on the bundle or logged."""
    kind = source.get("kind")
    if kind == "git":
        return si.bundles_from_git(
            url=source.get("url") or "",
            ref=source.get("ref") or None,
            subdir=source.get("subdir") or None,
            token=source.get("token") or None,
        )
    if kind == "url":
        return [si.bundle_from_url(source.get("url") or "")]
    raise AppError(
        "registry.invalid_source",
        f"unsupported skill source '{kind}'",
        status_code=400,
    )


def _stage_and_respond(bundles: list[SkillBundle]) -> dict[str, Any]:
    staged = [(b, bundle_errors(b)) for b in bundles]
    if not any(not errs for _, errs in staged):
        for bundle, _ in staged:
            bundle.close()
        first_errors = staged[0][1] if staged else ["no SKILL.md found in source"]
        raise AppError(
            "registry.skill_invalid",
            "; ".join(first_errors),
            detail=first_errors,
            status_code=422,
        )
    sid = _stage([b for b, _ in staged])
    return {
        "staging_id": sid,
        "skills": [_skill_out(b, errs, i) for i, (b, errs) in enumerate(staged)],
    }


class ImportSelection(BaseModel):
    """Pick a staged skill by ``index`` (preferred, unambiguous) or by ``name``;
    ``name_override`` / ``description_override`` edit the registered record."""

    index: int | None = None
    name: str = ""
    name_override: str | None = Field(default=None, max_length=64)
    description_override: str | None = Field(default=None, max_length=500)


class ImportRequest(BaseModel):
    staging_id: str
    selections: list[ImportSelection]


def _match_bundle(bundles: list[SkillBundle], sel: ImportSelection) -> SkillBundle | None:
    if sel.index is not None:
        return bundles[sel.index] if 0 <= sel.index < len(bundles) else None
    for bundle in bundles:
        if bundle.name == sel.name:
            return bundle
    return None


@router.post("/skills/import")
def import_skills(
    req: ImportRequest, request: Request, ws: WorkspaceScope = Depends(require_workspace)
) -> dict[str, Any]:
    """Register each selected staged bundle via the shared pipeline. A per-item
    failure (name conflict, validation) is reported inline and never aborts the
    other selections. Staging is consumed once the batch completes."""
    entry = console.take_staging(
        req.staging_id, workspace=ws.context,
        principal=principal_of(require_identity(request)),
    )
    bundles: list[SkillBundle] = entry["bundles"]
    records: list[dict[str, Any]] = []
    for sel in req.selections:
        label = sel.name_override or sel.name or f"#{sel.index}"
        bundle = _match_bundle(bundles, sel)
        if bundle is None:
            records.append(
                {"name": label, "ok": False,
                 "error": "no staged skill matches this selection",
                 "error_code": "registry.skill_not_staged"}
            )
            continue
        try:
            with _stamping_creator(request):
                record = console.register_skill_bundle(
                    bundle,
                    ws.context,
                    name_override=sel.name_override,
                    description_override=sel.description_override,
                )
            records.append({"name": record.get("name", label), "ok": True,
                            "record": _record_out(record)})
        except AppError as exc:
            # error is a plain string for inline display; error_code kept for i18n.
            records.append({"name": label, "ok": False,
                            "error": exc.message, "error_code": exc.code})
        except Exception as exc:  # never let one bad skill abort the batch
            records.append({"name": label, "ok": False,
                            "error": str(exc), "error_code": "registry.import_failed"})
    # keep staging on any failure so the user can fix (e.g. rename) and retry
    # without re-uploading; the TTL sweep reclaims abandoned sessions
    if all(r["ok"] for r in records):
        _drop_staging(req.staging_id)
    if any(r["ok"] for r in records):
        _invalidate_attachables(ws.id)
    return {"records": records}


@router.get("/skills/capabilities")
def skill_capabilities() -> dict[str, Any]:
    """Report git-import capability so the frontend's git branch can warn and
    offer auto-install when the ``git`` CLI is missing."""
    return {"git": si.git_capabilities()}


@router.post("/skills/capabilities/git-install")
def install_git() -> dict[str, Any]:
    """Explicit, user-triggered best-effort install of the ``git`` CLI (changes
    server state — only called from the capabilities UI button). No-ops with a
    hint when the server lacks the privilege to install."""
    return si.install_git()


@router.post("/records/{record_id}/reimport")
def reimport_record(
    record_id: str,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Re-run the ingestion pipeline for a git/url-sourced skill: re-acquire from
    the stored source, replace the S3 prefix, and bump the record version. Returns
    the updated record (same shape as GET). inline/zip records (no retrievable
    origin) and DEPRECATED records return 400 ``registry.not_reimportable``; a
    failed re-acquire/validation returns 422 ``registry.skill_invalid``. Creator-or-admin
    (N15-obs7) after the system-skill refusal, before the type / status checks."""
    from app.system_agents.skill_registry import refuse_protected_mutation

    _require_record_visible(ws, viewer, record_id)
    refuse_protected_mutation(ws.context, record_id, "reimport")  # ledger guard before 503
    _require_record_mutable(ws, request, record_id, "reimport")
    record = console.reimport_skill(ws.context, record_id)
    _invalidate_attachables(ws.id)
    return _one_with_system(ws, record)


class UpdateRecordRequest(BaseModel):
    """Partial edit of an existing record (the edit sub-page). At least one field
    must be set. ``url`` is MCP-only; ``skill_md`` and ``staging_id`` are
    AGENT_SKILLS-only and mutually exclusive — inline SKILL.md edit vs. replacing
    the whole bundle from a staged (inspected) upload. The record's name is never
    editable (it keys the S3 prefix and record identity)."""

    description: str | None = Field(default=None, max_length=500)
    url: str | None = None  # MCP: streamable-http endpoint
    skill_md: str | None = Field(default=None, max_length=102400)  # AGENT_SKILLS (AWS cap)
    staging_id: str | None = None  # AGENT_SKILLS: bundle replace from inspect staging
    index: int = 0  # which staged bundle (single-skill upload → 0)


@router.put("/records/{record_id}")
def update_record(
    record_id: str,
    req: UpdateRecordRequest,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Edit a record's description and/or content. Description-only edits don't
    bump the version; content edits (MCP url / skill SKILL.md / whole bundle) do.
    Returns the refreshed record (same shape as GET). DEPRECATED/A2A records are
    not editable (400). A ``staging_id`` bundle is consumed only on success so a
    failed save can be retried without re-uploading (same as import). Creator-or-admin
    (N15-obs7): another member answers 403 ``registry.forbidden``."""
    _require_record_visible(ws, viewer, record_id)
    if (
        req.description is None and req.url is None
        and req.skill_md is None and req.staging_id is None
    ):
        raise AppError(
            "registry.nothing_to_update",
            "provide at least one field to update",
            status_code=400,
        )
    if req.skill_md is not None and req.staging_id is not None:
        raise AppError(
            "registry.field_conflict",
            "skill_md and staging_id are mutually exclusive — edit SKILL.md inline "
            "or replace the whole bundle, not both",
            status_code=400,
        )

    # A system-managed Skill (SE-043) refuses every edit here, before the type read.
    from app.system_agents.skill_registry import refuse_protected_mutation

    action = "replace" if req.staging_id else "edit"
    refuse_protected_mutation(ws.context, record_id, action)
    # Creator-or-admin (N15-obs7): before the type / url checks so a forbidden caller
    # learns nothing about the record.
    rtype = _require_record_mutable(ws, request, record_id, action).get("descriptorType")
    if req.url is not None and rtype != "MCP":
        raise AppError(
            "registry.field_type_mismatch",
            "url can only be set on an MCP record",
            status_code=400,
        )
    if (req.skill_md is not None or req.staging_id is not None) and rtype != "AGENT_SKILLS":
        raise AppError(
            "registry.field_type_mismatch",
            "skill_md/staging_id can only be set on an AGENT_SKILLS record",
            status_code=400,
        )
    if req.url is not None and not req.url.startswith(("https://", "http://")):
        raise AppError(
            "registry.invalid_url",
            "MCP url must be a http(s) streamable-http server URL",
            status_code=400,
        )
    # N17-obs256: a record cannot be re-pointed at the platform gateway either
    console.refuse_platform_gateway_url(ws.context, req.url)

    bundle: SkillBundle | None = None
    if req.staging_id is not None:
        entry = console.take_staging(
            req.staging_id, workspace=ws.context,
            principal=principal_of(require_identity(request)),
        )
        bundles: list[SkillBundle] = entry["bundles"]
        if not 0 <= req.index < len(bundles):
            raise AppError(
                "registry.skill_not_staged",
                f"no staged skill at index {req.index}",
                status_code=400,
            )
        bundle = bundles[req.index]

    # An AppError from update_record propagates and skips the drop below, so a
    # staged bundle survives for retry; it is dropped only once the save lands.
    result = console.update_record(
        record_id,
        ws.context,
        description=req.description,
        url=req.url,
        skill_md=req.skill_md,
        bundle=bundle,
    )
    if req.staging_id is not None:
        _drop_staging(req.staging_id)
    _invalidate_attachables(ws.id)
    return _one_with_system(ws, result)


@router.delete("/records/{record_id}")
def delete_record(
    record_id: str,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Creator-or-admin (N15-obs7): visibility 404 → system-skill refusal → 403
    ``registry.forbidden`` → delete."""
    from app.system_agents.skill_registry import refuse_protected_mutation

    _require_record_visible(ws, viewer, record_id)
    refuse_protected_mutation(ws.context, record_id, "delete")  # ledger guard before 503
    _require_record_mutable(ws, request, record_id, "delete")
    backend = registry_backend.get_backend()
    record = backend.get_record(ws.context, record_id)
    backend.delete_record(ws.context, record_id)
    _invalidate_attachables(ws.id)
    out: dict[str, Any] = {"deleted": True, "record_id": record_id}
    if record.get("descriptorType") == "AGENT_SKILLS":
        # N16-obs35: a skill record's `skills/<name>/` objects go with the row — unless another
        # record or a live agent still references the prefix (then they stay, named). Additive
        # `objects` key on skill deletes only; other record types answer as before.
        out["objects"] = console.delete_skill_objects(ws.context, record)
    return out


@router.get("/attachables")
def attachables(
    refresh: bool = False, ws: WorkspaceScope = Depends(require_workspace)
) -> dict[str, Any]:
    """APPROVED MCP servers + skills the create wizard offers for mounting.
    Cached 60s per workspace — each call walks GetRegistryRecord per record."""
    slot = _attachables_cache.get(ws.id)
    if not refresh and slot is not None and time.time() - slot["at"] < 60:
        return slot["data"]
    data = console.attachable_records(ws.context)
    _attachables_cache[ws.id] = {"data": data, "at": time.time()}
    return data


@router.post("/sync-defaults")
def sync_defaults(
    request: Request, ws: WorkspaceScope = Depends(require_workspace)
) -> dict[str, Any]:
    """Register gateway targets (MCP) + the sample skill bundle (AGENT_SKILLS).
    N17-obs257: an administrator's sync approves the platform targets (and reconciles
    stuck ones); a member's only submits new ones for an administrator."""
    auto_approve = require_identity(request).is_admin
    results = console.ensure_default_records(ws.context, auto_approve=auto_approve)
    _invalidate_attachables(ws.id)
    return {"results": results, "auto_approved": auto_approve}


class ImportGatewayRequest(BaseModel):
    """``POST /api/registry/import-gateway`` (WB-005). ``gateway_id`` defaults to the
    platform gateway; ``target_ids`` omitted/null = every target of that gateway."""

    gateway_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    target_ids: list[str] | None = Field(default=None, max_length=500)


@router.get("/import-gateway/preview")
def import_gateway_preview(
    request: Request, ws: WorkspaceScope = Depends(require_workspace)
) -> dict[str, Any]:
    """Every onboarded MCP Gateway with its targets and the registry record (if any)
    holding each target's name — what the import dialog lists. Read-only. N17-obs261: for an
    administrator (whose import approves) each platform row it would promote says so."""
    return console.gateway_import_preview(
        ws.context, auto_approve=require_identity(request).is_admin
    )


@router.post("/import-gateway")
def import_gateway(
    req: ImportGatewayRequest, request: Request, ws: WorkspaceScope = Depends(require_workspace)
) -> dict[str, Any]:
    """Import the chosen Gateway targets as MCP records — one result item per target
    (created / already imported / reconciled / failed with ``error``)."""
    out = console.import_gateway_targets(
        ws.context, gateway_id=req.gateway_id, target_ids=req.target_ids,
        auto_approve=require_identity(request).is_admin,  # N17-obs257
    )
    _invalidate_attachables(ws.id)
    return out
