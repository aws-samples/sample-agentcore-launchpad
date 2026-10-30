"""Managed Knowledge Base API — KB CRUD, data sources, ingestion monitoring,
and the retrieval Playground. Thin router over the ``KnowledgeBackend`` Protocol
(``app.services.knowledge_backend``; 503 ``feature.unavailable`` until N6 registers
one); error handling mirrors routers/registry.py (domain AppErrors surface as
envelopes).

Workspace isolation (`services/visibility.py`): a KB another member created is not
listed and every by-id route answers the backend's own 404 ``kb.not_found`` — decided on
the ``knowledge_bases`` ledger row before the backend (and any OpenSearch / S3 call) is
consulted. The names of agents the caller cannot see are dropped from ``attached_agents``
while ``attached_agent_count`` keeps the true number, so "in use" stays truthful.

Mutations are the creator's or an administrator's (`services/ownership.py`, users-and-ownership.md
§8; N17-obs530): a member who can SEE a KB another principal created — an administrator's, or a
NULL-creator preset / legacy row — may read it, run the incremental ``sync`` and ``query`` it, but
``PATCH`` / ``DELETE`` the KB, add / delete a data source, upload files or start a full rebuild
answer ``403 auth.forbidden`` (``_require_owner``), after the visibility 404 and before any
stage / in-use check. The console mirrors the rule (`frontend/src/v2/ownership.ts`).
"""

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from app.core.db import get_db
from app.core.errors import (
    OPENSEARCH_ERROR_MESSAGES,
    AppError,
    NotFoundError,
    mapped_aws_error,
    mapped_opensearch_error,
)
from app.models.ledger import KnowledgeBase
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.schemas.attachments import MAX_FILE_BYTES, MAX_FILES, MAX_TOTAL_BYTES
from app.services import agent_access, knowledge_backend, ownership
from app.services.creators import Creator
from app.services.opensearch_http import OpenSearchError
from app.services.visibility import Viewer, require_viewer

router = APIRouter(prefix="/api/knowledge-bases", tags=["knowledge-bases"])


def _guard(fn, *args, **kwargs):
    """Map OpenSearch/AWS failures reaching the knowledge-base backend onto a
    translatable ``kb.*`` envelope (N6 Task 3 fix round 1).

    Domain errors the backend already raises (``kb.not_found``,
    ``feature.unavailable``, …) pass through untouched — mirrors
    ``routers/memory.py``'s ``_guard``. Unlike that one, a mapped
    ``OpenSearchError`` is translated to an ``AppError`` *here*, not re-raised for
    the shared global handler (`app.core.errors.opensearch_error_handler`, which
    defaults to `feature="memory"`) — otherwise every knowledge-base 404/503 would
    read `memory.*` regardless of which feature's request actually failed. An AWS
    `ClientError` the console can translate is still left to the matching global
    handler (those codes carry no feature prefix either way)."""
    try:
        return fn(*args, **kwargs)
    except AppError:
        raise
    except OpenSearchError as exc:
        mapped = mapped_opensearch_error(exc, feature="kb")
        if mapped is not None:
            status, code = mapped
            raise AppError(code, OPENSEARCH_ERROR_MESSAGES[code], status_code=status) from exc
        raise AppError(
            "kb.unavailable", f"knowledge base lookup failed: {exc}", status_code=502
        ) from exc
    except Exception as exc:  # botocore ClientError, ...
        if mapped_aws_error(exc):
            raise
        raise AppError(
            "kb.unavailable", f"knowledge base lookup failed: {exc}", status_code=502
        ) from exc


# ── workspace isolation ──────────────────────────────────────────────────────


def visible_kbs(
    db: Session, workspace_id: str, viewer: Viewer, items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """``items`` (any list carrying ``kb_id``) minus the KBs workspace isolation hides
    from ``viewer`` — the list route and the architect catalog share this one filter.
    No query for a viewer who does not filter."""
    hidden = agent_access.hidden_kb_ids(db, viewer, workspace_id)
    if not hidden:
        return items
    return [i for i in items if (i.get("kb_id") or "") not in hidden]


def _require_visible(db: Session, ws: WorkspaceScope, viewer: Viewer, kb_id: str) -> None:
    """A KB hidden by workspace isolation answers like a missing one — before any
    backend call."""
    if kb_id in agent_access.hidden_kb_ids(db, viewer, ws.id):
        raise NotFoundError(
            "kb.not_found", f"knowledge base {kb_id!r} not found", {"kb_id": kb_id}
        )


def _require_owner(
    db: Session, ws: WorkspaceScope, request: Request, kb_id: str, action: str
) -> None:
    """Creator-or-admin on a KB mutation (N17-obs530): the ``knowledge_bases`` ledger row's
    ``created_by`` against the caller; a row with no creator (preset-installed, legacy, the
    config admin's) — or no ledger row at all — is an administrator's. Runs AFTER
    ``_require_visible`` (a hidden KB stays an indistinguishable 404) and before any
    attached / status check, so a forbidden caller learns nothing about the KB's state."""
    row = db.execute(
        select(KnowledgeBase).where(
            KnowledgeBase.workspace_id == ws.id, KnowledgeBase.kb_id == kb_id
        )
    ).scalar_one_or_none()
    ownership.require_creator_or_admin(
        row,
        require_identity(request),
        "auth.forbidden",
        message=(
            "only the knowledge base's creator or an administrator may change it "
            f"({action}); other members may query it and run the incremental sync"
        ),
        detail={"kb_id": kb_id, "action": action},
    )


def _mask_attached(item: dict[str, Any], hidden_names: set[str]) -> dict[str, Any]:
    """Drop the names of agents workspace isolation hides from the caller; the count
    of every mounting agent stays, so "in use" remains truthful."""
    names = list(item.get("attached_agents") or [])
    item["attached_agent_count"] = len(names)
    if hidden_names:
        item["attached_agents"] = [n for n in names if n not in hidden_names]
    return item


class SourceSpec(BaseModel):
    """S3 data source: ``upload`` targets the artifacts bucket, ``existing`` a
    caller-provided bucket (+ optional prefix)."""

    mode: Literal["upload", "existing"] = "upload"
    bucket: str | None = None
    prefix: str | None = None


class CreateKBRequest(BaseModel):
    # the creator is server-derived: a payload naming `created_by` is refused (422)
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100, pattern=r"^[0-9a-zA-Z][0-9a-zA-Z_-]{0,99}$")
    description: str = Field(default="", max_length=1000)
    source: SourceSpec = SourceSpec()


class PatchKBRequest(BaseModel):
    description: str = Field(default="", max_length=1000)


class QueryRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    number_of_results: int = Field(default=8, ge=1, le=100)


@router.get("")
def list_knowledge_bases(
    status: str | None = None,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    items = _guard(knowledge_backend.get_backend().list_kbs, ws.context)
    if status:  # e.g. the Create-Agent picker requests ?status=ACTIVE
        items = [i for i in items if i.get("status") == status]
    items = visible_kbs(db, ws.id, viewer, items)
    hidden_names = agent_access.hidden_agent_names(db, viewer, ws.id)
    for item in items:
        _mask_attached(item, hidden_names)
    return {"items": items}


# 202: the KB is returned while still CREATING — the data source is finished by
# a backend thread and the client polls GET /{kb_id} (see KnowledgeBackend.create_kb).
@router.post("", status_code=202)
def create_knowledge_base(
    req: CreateKBRequest,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return _guard(
        knowledge_backend.get_backend().create_kb,
        ws.context, req.name, req.description, req.source.model_dump(),
        creator=Creator.of_request(request),
    )


@router.get("/{kb_id}")
def get_knowledge_base(
    kb_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _require_visible(db, ws, viewer, kb_id)
    detail = _guard(knowledge_backend.get_backend().get_kb_detail, ws.context, kb_id)
    return _mask_attached(detail, agent_access.hidden_agent_names(db, viewer, ws.id))


@router.patch("/{kb_id}")
def patch_knowledge_base(
    kb_id: str,
    req: PatchKBRequest,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _require_visible(db, ws, viewer, kb_id)
    _require_owner(db, ws, request, kb_id, "edit")
    return _guard(
        knowledge_backend.get_backend().update_description, ws.context, kb_id, req.description,
    )


@router.delete("/{kb_id}")
def delete_knowledge_base(
    kb_id: str,
    request: Request,
    force: bool = False,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _require_visible(db, ws, viewer, kb_id)
    # N17-obs530: ownership first (users-and-ownership.md §8 order) — a member who did not
    # create the KB is refused before the force / attached checks can say anything about it.
    _require_owner(db, ws, request, kb_id, "delete")
    identity = require_identity(request)
    # N17-obs4: `force` strips OTHER members' agents' specs — a cross-member mutation — so the
    # flag is an administrator's override exactly as on datasets / evaluators (evaluation-
    # engine.md §22): a member's force is 403 auth.forbidden whether or not anything mounts the
    # KB; a member's plain delete keeps the route policy (409 kb.has_attached_agents if mounted).
    if force and not identity.is_admin:
        raise AppError(
            "auth.forbidden",
            "force-deleting a knowledge base requires an administrator",
            {"kb_id": kb_id},
            status_code=403,
        )
    # Ledger guards first (N1 contract note): a preset mount is never deletable, and an
    # ordinary mount 409s unless forced — both before the backend is even consulted.
    # Pure ledger reads (no AWS/OpenSearch call), so they stay outside `_guard`.
    knowledge_backend.refuse_if_attached_to_system_agent(db, ws.context, kb_id)
    attached = knowledge_backend.attached_agent_names(db, ws.context, kb_id)
    if attached and not force:
        # the guard counts every mounting agent; the 409 names only the visible ones
        hidden_names = agent_access.hidden_agent_names(db, viewer, ws.id)
        error = knowledge_backend.KBAttachedError(kb_id, attached)
        if hidden_names:
            error.detail = {"agents": [n for n in attached if n not in hidden_names]}
        raise error
    # N17-obs31 (= N16-obs34): force strips the KB from EVERY mounting agent, hidden ones
    # included (integrity: no agent may keep a dangling KB id) — in the shared layer, before
    # the backend is consulted, committed first (a storage failure afterwards leaves at
    # worst a KB with no mounts, never a mount on a gone KB); a mounting agent mid-deploy
    # refuses the force with 409 agent.deploy_in_progress and nothing is written.
    stripped: list[dict[str, Any]] = []
    if attached:
        stripped = knowledge_backend.strip_kb_from_agents(
            db, ws.context, kb_id, by=identity.username,
        )
    out = _guard(knowledge_backend.get_backend().delete_kb, ws.context, kb_id, force=force)
    return {**out, "stripped_agents": stripped}


@router.post("/{kb_id}/files")
async def upload_knowledge_base_files(
    kb_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _require_visible(db, ws, viewer, kb_id)
    _require_owner(db, ws, request, kb_id, "upload")
    form = await request.form()
    uploads = [
        f
        for f in (form.getlist("files") + form.getlist("file"))
        if isinstance(f, UploadFile)
    ]
    if not uploads:
        raise AppError(
            "kb.no_files", "expected one or more files under 'files'", status_code=400
        )
    # Count/size caps BEFORE reading any bytes or touching S3 (ASGI-level
    # `AttachmentBodyCap` also bounds the whole request body for this path; these
    # are the member-facing, per-file/total checks that produce a distinct code).
    if len(uploads) > MAX_FILES:
        raise AppError(
            "kb.too_many_files", f"At most {MAX_FILES} files are allowed per upload.",
            {"max_files": MAX_FILES}, status_code=422,
        )
    files: list[tuple[str, bytes]] = []
    total = 0
    for u in uploads:
        data = await u.read()
        if len(data) > MAX_FILE_BYTES:
            raise AppError(
                "kb.upload_too_large",
                f"{u.filename or 'file'!r} exceeds the per-file size limit.",
                {"max_bytes": MAX_FILE_BYTES}, status_code=413,
            )
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise AppError(
                "kb.upload_too_large", "Upload exceeds the total size limit.",
                {"max_bytes": MAX_TOTAL_BYTES}, status_code=413,
            )
        files.append((u.filename or "file", data))
    # This route is `async def` only for the multipart reads above; the S3 puts (and the
    # PDF text extraction) are blocking and must leave the event loop — on the loop they
    # froze every other request for the upload's duration (N-SCN-obs13). Same pattern as
    # `routers/agents.py::upload_byoc_artifact`.
    # `{keys, job}` — the sync the upload started (or coalesced onto), N17-obs204.
    return await run_in_threadpool(
        _guard, knowledge_backend.get_backend().upload_files, ws.context, kb_id, files,
    )


@router.post("/{kb_id}/data-sources", status_code=201)
def add_data_source(
    kb_id: str,
    req: SourceSpec,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """201 with the new source (`created: true`); mode `upload` on a KB that already has an
    upload-mode source answers 200 with THAT source and `created: false` — the call is
    idempotent per KB, so a repeated 补建 click cannot pile up empty sources (N17-obs243)."""
    _require_visible(db, ws, viewer, kb_id)
    _require_owner(db, ws, request, kb_id, "add_data_source")
    out = _guard(
        knowledge_backend.get_backend().add_data_source, ws.context, kb_id, req.model_dump(),
    )
    if out.get("created") is False:
        response.status_code = 200
    return out


@router.delete("/{kb_id}/data-sources/{ds_id}")
def delete_data_source(
    kb_id: str,
    ds_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _require_visible(db, ws, viewer, kb_id)
    _require_owner(db, ws, request, kb_id, "delete_data_source")
    return _guard(knowledge_backend.get_backend().delete_data_source, ws.context, kb_id, ds_id)


class SyncRequest(BaseModel):
    """Optional body of ``POST …/sync`` (N17-obs272). ``full: true`` forces a complete
    rebuild (every document re-chunked and re-embedded); omitted / false = the incremental
    sync (N17-obs246: unchanged documents are skipped)."""

    model_config = ConfigDict(extra="forbid")

    full: bool = False


@router.post("/{kb_id}/data-sources/{ds_id}/sync")
def sync_data_source(
    kb_id: str,
    ds_id: str,
    request: Request,
    req: SyncRequest | None = None,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Start an ingestion sync. The incremental sync is every viewer's (a member syncs an
    administrator's KB — the workshop's service desk refreshes a personal KB the trainer
    created); the full rebuild (`full: true`, N17-obs272) re-chunks and re-embeds every
    document, so it is the creator's or an administrator's (N17-obs530)."""
    _require_visible(db, ws, viewer, kb_id)
    full = bool(req and req.full)
    if full:
        _require_owner(db, ws, request, kb_id, "full_rebuild")
    return _guard(
        knowledge_backend.get_backend().start_sync, ws.context, kb_id, ds_id, full=full,
    )


@router.get("/{kb_id}/data-sources/{ds_id}/ingestion-jobs")
def list_ingestion_jobs(
    kb_id: str,
    ds_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _require_visible(db, ws, viewer, kb_id)
    items = _guard(
        knowledge_backend.get_backend().list_ingestion_jobs, ws.context, kb_id, ds_id,
    )
    return {"items": items}


@router.get("/{kb_id}/data-sources/{ds_id}/documents")
def list_documents(
    kb_id: str,
    ds_id: str,
    page_size: int = Query(default=50, ge=1, le=100),
    token: str | None = None,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _require_visible(db, ws, viewer, kb_id)
    return _guard(
        knowledge_backend.get_backend().list_documents,
        ws.context, kb_id, ds_id, page_size=page_size, token=token,
    )


@router.post("/{kb_id}/query")
def query_knowledge_base(
    kb_id: str,
    req: QueryRequest,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _require_visible(db, ws, viewer, kb_id)
    backend = knowledge_backend.get_backend()
    results = _guard(backend.query, ws.context, kb_id, req.text, req.number_of_results)
    return {"results": results}
