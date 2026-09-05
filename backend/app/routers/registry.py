"""Registry console API — records per type, register, lifecycle actions,
attachables catalog, search, defaults sync, multi-source skill ingestion."""

import secrets
import time
from dataclasses import asdict
from typing import Any, Literal

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile

from app.core.errors import AppError, aws_error_message, mapped_aws_error
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import registry_console as console
from app.services import skill_ingest as si
from app.services.skill_ingest import (
    SKILL_BUNDLE_MAX_BYTES,
    SkillBundle,
    bundle_errors,
    bundle_from_zip,
)

router = APIRouter(prefix="/api/registry", tags=["registry"])


class A2ADemoRequest(BaseModel):
    """Front-desk demo ask: invoke a routing agent and pass its trace through."""

    agent_id: str
    question: str = Field(min_length=1, max_length=4000)


@router.post("/a2a-demo")
def a2a_demo(
    req: A2ADemoRequest, ws: WorkspaceScope = Depends(require_workspace)
) -> dict[str, Any]:
    """Invoke a front-desk-style agent and return {answer, trace}.

    Deliberately bypasses invoke_agent_text: the demo needs the agent's extra
    `a2a_trace` payload field (DISCOVER/INVOKE stage records), which the
    text-only dispatch drops.
    """
    import json as _json

    from app.core.db import SessionLocal
    from app.models.ledger import Agent
    from app.services.agentcore.client import data_client
    from app.services.agentcore.harness import new_session_id

    db = SessionLocal()
    try:
        agent = db.get(Agent, req.agent_id)
        # An agent from another workspace is as good as absent: this route is the
        # second invoke entrance, so it owes the same boundary as /agents/{id}/invoke.
        if agent is not None and agent.workspace_id != ws.id:
            agent = None
        if agent is None or agent.status != "active":
            raise AppError("registry.a2a_demo_agent", "agent not found or not active",
                           status_code=404)
        if agent.method not in ("zip_runtime", "studio"):
            raise AppError("registry.a2a_demo_unsupported",
                           "the demo drives runtime agents with a {prompt} contract",
                           status_code=400)
        arn = agent.arn
    finally:
        db.close()

    started = time.monotonic()
    session_id = new_session_id()
    response = data_client(ws.context).invoke_agent_runtime(
        agentRuntimeArn=arn,
        runtimeSessionId=session_id,
        payload=_json.dumps({"prompt": req.question, "actor_id": "a2a-demo"}).encode(),
    )
    body = _json.loads(response["response"].read())
    if isinstance(body, dict) and body.get("error"):
        raise AppError("registry.a2a_demo_failed", str(body["error"])[:300],
                       status_code=502)
    return {
        "answer": str(body.get("result", "")) if isinstance(body, dict) else str(body),
        "trace": (body.get("a2a_trace") or []) if isinstance(body, dict) else [],
        "session_id": session_id,
        "latency_ms": int((time.monotonic() - started) * 1000),
    }

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


def _record_out(record: dict[str, Any]) -> dict[str, Any]:
    return {
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
    }


@router.get("/records")
def list_records(
    type: str | None = None,
    status: str | None = None,
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    records = console.console_list(ws.context, type, status)
    return {"records": [_record_out(r) for r in records]}


@router.get("/records/search")
def search(q: str, ws: WorkspaceScope = Depends(require_workspace)) -> dict[str, Any]:
    return {"records": [_record_out(r) for r in console.console_search(ws.context, q)]}


@router.get("/records/{record_id}")
def get_record(
    record_id: str, ws: WorkspaceScope = Depends(require_workspace)
) -> dict[str, Any]:
    return _record_out(console.console_get(ws.context, record_id))


@router.get("/records/{record_id}/live-agent-card")
def live_agent_card(
    record_id: str, ws: WorkspaceScope = Depends(require_workspace)
) -> dict[str, Any]:
    """The A2A card the runtime behind this record serves right now (GetAgentCard).

    Record → ledger agent → ``Agent.arn`` is resolved here, server-side; the
    browser only names the record. Every refusal is decided on the ledger before
    AWS is called: a record no Launchpad agent owns (404), an agent that is not an
    A2A server (409), an agent not yet deployed / no longer active (409). The
    data plane's ``ClientError`` keeps the standard 4xx envelope; a runtime-side
    failure the envelope has no mapping for is a 502, never a bare 500.
    """
    from app.core.db import SessionLocal
    from app.models.ledger import Agent

    db = SessionLocal()
    try:
        agent = (
            db.query(Agent)
            .filter(
                Agent.registry_record_id == record_id,
                Agent.workspace_id == ws.id,
                Agent.status != "deleted",
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


@router.post("/records/{record_id}/action")
def record_action(
    record_id: str,
    req: ActionRequest,
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    try:
        console.console_action(ws.context, record_id, req.action)
    except ValueError as exc:
        raise AppError("registry.unknown_action", str(exc), status_code=400) from exc
    _invalidate_attachables(ws.id)
    return _record_out(console.console_get(ws.context, record_id))


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
    req: RegisterRequest, ws: WorkspaceScope = Depends(require_workspace)
) -> dict[str, Any]:
    if req.type == "MCP":
        if not req.url or not req.url.startswith(("https://", "http://")):
            raise AppError(
                "registry.invalid_url",
                "MCP registration needs a http(s) streamable-http server URL",
                status_code=400,
            )
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
async def inspect_skill(request: Request) -> dict[str, Any]:
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
        bundles = _acquire_source(body.get("source") or {})
    else:
        raise AppError(
            "registry.invalid_upload",
            "expected a multipart .zip upload or a JSON source",
            status_code=400,
        )
    return _stage_and_respond(bundles)


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
    return [bundle_from_zip(data)]  # archive-safety violations raise here (422)


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
    req: ImportRequest, ws: WorkspaceScope = Depends(require_workspace)
) -> dict[str, Any]:
    """Register each selected staged bundle via the shared pipeline. A per-item
    failure (name conflict, validation) is reported inline and never aborts the
    other selections. Staging is consumed once the batch completes."""
    _sweep_staging()
    entry = _staging.get(req.staging_id)
    if entry is None:
        raise AppError(
            "registry.staging_expired",
            "staging session expired or unknown — re-inspect the source",
            status_code=410,
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
    record_id: str, ws: WorkspaceScope = Depends(require_workspace)
) -> dict[str, Any]:
    """Re-run the ingestion pipeline for a git/url-sourced skill: re-acquire from
    the stored source, replace the S3 prefix, and bump the record version. Returns
    the updated record (same shape as GET). inline/zip records (no retrievable
    origin) and DEPRECATED records return 400 ``registry.not_reimportable``; a
    failed re-acquire/validation returns 422 ``registry.skill_invalid``."""
    record = console.reimport_skill(ws.context, record_id)
    _invalidate_attachables(ws.id)
    return _record_out(record)


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
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Edit a record's description and/or content. Description-only edits don't
    bump the version; content edits (MCP url / skill SKILL.md / whole bundle) do.
    Returns the refreshed record (same shape as GET). DEPRECATED/A2A records are
    not editable (400). A ``staging_id`` bundle is consumed only on success so a
    failed save can be retried without re-uploading (same as import)."""
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

    rtype = console.console_get(ws.context, record_id).get("descriptorType")
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

    bundle: SkillBundle | None = None
    if req.staging_id is not None:
        _sweep_staging()
        entry = _staging.get(req.staging_id)
        if entry is None:
            raise AppError(
                "registry.staging_expired",
                "staging session expired or unknown — re-inspect the source",
                status_code=410,
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
    return _record_out(result)


@router.delete("/records/{record_id}")
def delete_record(
    record_id: str, ws: WorkspaceScope = Depends(require_workspace)
) -> dict[str, Any]:
    console.console_delete(ws.context, record_id)
    _invalidate_attachables(ws.id)
    return {"deleted": True, "record_id": record_id}


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
def sync_defaults(ws: WorkspaceScope = Depends(require_workspace)) -> dict[str, Any]:
    """Register gateway targets (MCP) + the sample skill bundle (AGENT_SKILLS)."""
    results = console.ensure_default_records(ws.context)
    _invalidate_attachables(ws.id)
    return {"results": results}
