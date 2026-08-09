"""Managed Knowledge Base API — KB CRUD, S3 data sources, ingestion monitoring,
and the retrieval Playground. Thin router over app.services.knowledge; error
handling mirrors routers/registry.py (domain AppErrors surface as envelopes)."""

from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile

from app.core.errors import AppError
from app.services import kb_gateway, knowledge
from app.services.agentcore.client import control_client

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


@router.get("")
def list_knowledge_bases(status: str | None = None) -> dict[str, Any]:
    items = knowledge.list_kbs()
    if status:  # e.g. the Create-Agent picker requests ?status=ACTIVE
        items = [i for i in items if i.get("status") == status]
    return {"items": items}


# 202: the KB is returned while still CREATING — the data source is finished by
# a backend thread and the client polls GET /{kb_id} (see knowledge.create_kb).
@router.post("", status_code=202)
def create_knowledge_base(req: CreateKBRequest) -> dict[str, Any]:
    return knowledge.create_kb(req.name, req.description, req.source.model_dump())


@router.get("/{kb_id}")
def get_knowledge_base(kb_id: str) -> dict[str, Any]:
    return knowledge.get_kb_detail(kb_id)


@router.patch("/{kb_id}")
def patch_knowledge_base(kb_id: str, req: PatchKBRequest) -> dict[str, Any]:
    return knowledge.update_description(kb_id, req.description)


@router.delete("/{kb_id}")
def delete_knowledge_base(kb_id: str, force: bool = False) -> dict[str, Any]:
    return knowledge.delete_kb(kb_id, force=force)


@router.post("/{kb_id}/files")
async def upload_knowledge_base_files(kb_id: str, request: Request) -> dict[str, Any]:
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
    return {"keys": knowledge.upload_files(kb_id, files)}


@router.post("/{kb_id}/data-sources", status_code=201)
def add_data_source(kb_id: str, req: SourceSpec) -> dict[str, Any]:
    return knowledge.add_data_source(kb_id, req.model_dump())


@router.delete("/{kb_id}/data-sources/{ds_id}")
def delete_data_source(kb_id: str, ds_id: str) -> dict[str, Any]:
    return knowledge.delete_data_source(kb_id, ds_id)


@router.post("/{kb_id}/data-sources/{ds_id}/sync")
def sync_data_source(kb_id: str, ds_id: str) -> dict[str, Any]:
    return knowledge.start_sync(kb_id, ds_id)


@router.get("/{kb_id}/data-sources/{ds_id}/ingestion-jobs")
def list_ingestion_jobs(kb_id: str, ds_id: str) -> dict[str, Any]:
    return {"items": knowledge.list_ingestion_jobs(kb_id, ds_id)}


@router.get("/{kb_id}/data-sources/{ds_id}/documents")
def list_documents(
    kb_id: str,
    ds_id: str,
    page_size: int = Query(default=50, ge=1, le=100),
    token: str | None = None,
) -> dict[str, Any]:
    return knowledge.list_documents(kb_id, ds_id, page_size=page_size, token=token)


@router.post("/{kb_id}/query")
def query_knowledge_base(kb_id: str, req: QueryRequest) -> dict[str, Any]:
    return {"results": knowledge.query(kb_id, req.text, req.number_of_results)}


@router.post("/ensure-gateway")
def ensure_gateway() -> dict[str, Any]:
    return kb_gateway.ensure_kb_gateway_persisted(control_client())
