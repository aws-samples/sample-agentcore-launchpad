"""Managed Knowledge Base API — KB CRUD, S3 data sources, ingestion monitoring,
and the retrieval Playground. Thin router over app.services.knowledge; error
handling mirrors routers/registry.py (domain AppErrors surface as envelopes)."""

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from app.core.db import get_db
from app.core.errors import AppError, NotFoundError
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import creators, kb_gateway, knowledge, visibility
from app.services.agentcore.client import control_client
from app.services.creators import Creator
from app.services.visibility import Viewer, require_viewer

router = APIRouter(prefix="/api/knowledge-bases", tags=["knowledge-bases"])


class SourceSpec(BaseModel):
    """S3 data source: ``upload`` targets the artifacts bucket, ``existing`` a
    caller-provided bucket (+ optional prefix)."""

    mode: Literal["upload", "existing"] = "upload"
    bucket: str | None = None
    prefix: str | None = None


class CreateKBRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[0-9a-zA-Z][0-9a-zA-Z_-]{0,99}$")
    description: str = Field(default="", max_length=1000)
    source: SourceSpec = SourceSpec()


class PatchKBRequest(BaseModel):
    description: str = Field(default="", max_length=1000)


class QueryRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    number_of_results: int = Field(default=8, ge=1, le=100)


def visible_kbs(
    db: Session, workspace_id: str, viewer: Viewer, items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Drop the KBs workspace isolation hides from `viewer` (list + pickers +
    the architect catalog)."""
    hidden = visibility.hidden_aws_ids(
        viewer, db, workspace_id, creators.KB, (i.get("kb_id") or "" for i in items)
    )
    return [i for i in items if (i.get("kb_id") or "") not in hidden]


def _require_visible(db: Session, ws: WorkspaceScope, viewer: Viewer, kb_id: str) -> None:
    """A KB hidden by workspace isolation answers like a missing one — before any
    AWS call."""
    if not visibility.aws_id_visible(viewer, db, ws.id, creators.KB, kb_id):
        raise NotFoundError("kb.not_found", "knowledge base not found")


@router.get("")
def list_knowledge_bases(
    status: str | None = None,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    items = knowledge.list_kbs(ws.context)
    if status:  # e.g. the Create-Agent picker requests ?status=ACTIVE
        items = [i for i in items if i.get("status") == status]
    items = visible_kbs(db, ws.id, viewer, items)
    # the creator lives in the ledger (AWS has none); a KB made outside the
    # console reads null
    known = creators.lookup(db, ws.id, creators.KB, (i.get("kb_id") or "" for i in items))
    for item in items:
        item.update(known.get(item.get("kb_id") or "") or creators.empty())
    return {"items": items}


def _with_creator(db: Session, ws: WorkspaceScope, detail: dict[str, Any]) -> dict[str, Any]:
    return {
        **detail,
        **creators.lookup_one(db, ws.id, creators.KB, detail.get("kb_id") or ""),
    }


# 202: the KB is returned while still CREATING — the data source is finished by
# a backend thread and the client polls GET /{kb_id} (see knowledge.create_kb).
@router.post("", status_code=202)
def create_knowledge_base(
    req: CreateKBRequest,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    detail = knowledge.create_kb(
        ws.context, req.name, req.description, req.source.model_dump(),
        creator=Creator.of_request(request),
    )
    return _with_creator(db, ws, detail)


@router.get("/{kb_id}")
def get_knowledge_base(
    kb_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _require_visible(db, ws, viewer, kb_id)
    return _with_creator(db, ws, knowledge.get_kb_detail(ws.context, kb_id))


@router.patch("/{kb_id}")
def patch_knowledge_base(
    kb_id: str,
    req: PatchKBRequest,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _require_visible(db, ws, viewer, kb_id)
    return _with_creator(db, ws, knowledge.update_description(ws.context, kb_id, req.description))


@router.delete("/{kb_id}")
def delete_knowledge_base(
    kb_id: str,
    force: bool = False,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _require_visible(db, ws, viewer, kb_id)
    return knowledge.delete_kb(ws.context, kb_id, force=force)


@router.post("/{kb_id}/files")
async def upload_knowledge_base_files(
    kb_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _require_visible(db, ws, viewer, kb_id)
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
    files = [((u.filename or "file"), await u.read()) for u in uploads]
    return {"keys": knowledge.upload_files(ws.context, kb_id, files)}


@router.post("/{kb_id}/data-sources", status_code=201)
def add_data_source(
    kb_id: str,
    req: SourceSpec,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _require_visible(db, ws, viewer, kb_id)
    return knowledge.add_data_source(ws.context, kb_id, req.model_dump())


@router.delete("/{kb_id}/data-sources/{ds_id}")
def delete_data_source(
    kb_id: str,
    ds_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _require_visible(db, ws, viewer, kb_id)
    return knowledge.delete_data_source(ws.context, kb_id, ds_id)


@router.post("/{kb_id}/data-sources/{ds_id}/sync")
def sync_data_source(
    kb_id: str,
    ds_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _require_visible(db, ws, viewer, kb_id)
    return knowledge.start_sync(ws.context, kb_id, ds_id)


@router.get("/{kb_id}/data-sources/{ds_id}/ingestion-jobs")
def list_ingestion_jobs(
    kb_id: str,
    ds_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _require_visible(db, ws, viewer, kb_id)
    return {"items": knowledge.list_ingestion_jobs(ws.context, kb_id, ds_id)}


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
    return knowledge.list_documents(
        ws.context, kb_id, ds_id, page_size=page_size, token=token
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
    return {
        "results": knowledge.query(ws.context, kb_id, req.text, req.number_of_results)
    }


@router.post("/ensure-gateway")
def ensure_gateway(ws: WorkspaceScope = Depends(require_workspace)) -> dict[str, Any]:
    return kb_gateway.ensure_kb_gateway_persisted(control_client(ws.context), ws.context)
