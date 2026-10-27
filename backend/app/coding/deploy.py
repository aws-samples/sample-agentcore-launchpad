"""Deploy handoff: a coding repo becomes an ordinary governed Agent (R6, §7.6).

The coding agent never deploys anything itself. The backend snapshots the
repo, has the runtime zip its files (tracked + untracked-not-ignored, no
``.git``, no symlinks) to a presigned PUT, downloads that zip into a private temp
file and stages it with ``byoc_uploads.stage_upload`` — the very checks a
member's own upload passes. Then it creates a ``byoc`` Agent stamped with the
caller as creator (``routers.agents.start_create``, the code behind
``POST /api/agents``) or, when the repo already deployed one the caller can
still see, re-publishes that Agent with the new upload
(``routers.agents.republish_agent``, the code behind redeploy). Same validation,
IAM, registry and observability as an uploaded zip.

``artifact_kind`` defaults to ``container_source`` when the export has a root
``Dockerfile`` and to ``code_zip`` (entrypoint ``main.py``) otherwise. Settings
the repo cannot deploy with are ``422 coding.deploy_failed`` with
``detail.reason`` (``entrypoint_missing`` + ``entrypoint`` / ``candidates``,
``dockerfile_missing``, ``invalid_spec`` + ``errors``) — the console's copy.

The export comes from a microVM that runs arbitrary code, so its size is checked
(``HeadObject``) against the BYOC zip cap before the backend downloads it — the
cap ``stage_upload`` expects its caller to have enforced.
"""

from __future__ import annotations

import hashlib
import logging
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.routers.auth import Identity
from app.routers.workspaces import WorkspaceScope
from app.schemas.agent import AgentSpec
from app.services import agent_access, agent_names, byoc_uploads
from app.services.creators import Creator
from app.services.visibility import Viewer

from . import runtime_client, s3urls, snapshots
from .models import REPO_RUNNING, CodingRepo

logger = logging.getLogger("launchpad.coding")

AGENT_NAME_PATTERN = r"^[a-z][a-z0-9-]{2,47}$"
DEFAULT_ENTRYPOINT = "main.py"
DEFAULT_PYTHON = "PYTHON_3_13"


def agent_name_from(repo_name: str) -> str:
    """A valid Agent name (``^[a-z][a-z0-9-]{2,47}$``) from a repo name."""
    slug = re.sub(r"[^a-z0-9]+", "-", (repo_name or "").lower()).strip("-")
    if not slug or not slug[0].isalpha():
        slug = f"coding-{slug}".strip("-")
    slug = slug[:48].rstrip("-")
    if len(slug) < 3:
        slug = f"{slug}-agent"[:48]
    return slug


def _invalid(reason: str, message: str, **detail: Any) -> AppError:
    return AppError("coding.deploy_failed", message, {"reason": reason, **detail},
                    status_code=422)


def _refuse_oversized(size: int) -> None:
    if size > byoc_uploads.MAX_ZIP_BYTES:
        raise AppError("byoc.upload_too_large",
                       "the repo export exceeds the 250 MiB zip limit",
                       {"size_bytes": size}, status_code=413)


def _stage_export(ws: WorkspaceScope, repo: CodingRepo, identity: Identity,
                  python_version: str) -> dict[str, Any]:
    """Export the repo through S3 and stage it as a BYOC upload; the manifest."""
    ctx = ws.context
    key = s3urls.export_key(ws.id, repo.id)
    runtime_client.run(ctx, repo, "export", put_url=s3urls.presign_put(ctx, key))
    try:
        _refuse_oversized(s3urls.object_size(ctx, key))
        with tempfile.TemporaryDirectory(prefix="coding-export-") as tmp:
            path = Path(tmp) / "source.zip"
            s3urls.download(ctx, key, str(path))
            size = path.stat().st_size
            _refuse_oversized(size)  # re-PUT between the HEAD and the GET
            digest = hashlib.sha256()
            with path.open("rb") as fh:
                while chunk := fh.read(1024 * 1024):
                    digest.update(chunk)
            filename = f"{agent_name_from(repo.name)}.zip"
            manifest = byoc_uploads.stage_upload(
                ctx,
                filename=filename,
                tmp_zip=path,
                sha256=digest.hexdigest(),
                size_bytes=size,
                uploaded_by=identity.username,
                uploaded_at=datetime.now(UTC).isoformat(timespec="seconds"),
                python_version=python_version,
            )
    finally:
        try:
            s3urls.delete_key(ctx, key)  # the staged BYOC copy is the durable one
        except Exception as exc:  # noqa: BLE001 — a leftover export is harmless
            logger.info("coding repo %s: export cleanup failed: %s", repo.id, exc)
    return manifest


def _byoc_block(manifest: dict[str, Any], *, artifact_kind: str | None,
                entrypoint: str | None, python_version: str,
                stored: dict[str, Any] | None = None) -> dict[str, Any]:
    detected = manifest.get("detected") or {}
    has_dockerfile = bool(detected.get("has_dockerfile"))
    kind = artifact_kind or ("container_source" if has_dockerfile else "code_zip")
    if kind == "container_source" and not has_dockerfile:
        raise _invalid("dockerfile_missing",
                       "a container_source deploy needs a Dockerfile at the repo root")
    block: dict[str, Any] = {
        **{k: v for k, v in (stored or {}).items()
           if k not in ("artifact_kind", "upload_id", "image_uri", "provenance")},
        "artifact_kind": kind,
        "upload_id": manifest["upload_id"],
        "python_version": python_version,
    }
    if kind == "code_zip":
        entry = (entrypoint or (stored or {}).get("entrypoint") or DEFAULT_ENTRYPOINT).strip()
        candidates = detected.get("entrypoint_candidates") or []
        if "/" not in entry and len(candidates) < 50 and entry not in candidates:
            raise _invalid(
                "entrypoint_missing",
                f"the repo has no {entry} at its root — create it, or pick the "
                "entrypoint the agent serves from",
                entrypoint=entry, candidates=candidates,
            )
        block["entrypoint"] = entry
    return block


def deploy_repo(
    db: Session,
    ws: WorkspaceScope,
    repo: CodingRepo,
    identity: Identity,
    *,
    request: Request,
    viewer: Viewer,
    agent_name: str | None = None,
    description: str | None = None,
    artifact_kind: str | None = None,
    entrypoint: str | None = None,
    python_version: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Create or re-publish the repo's Agent; ``{agent_id, job_id, created}``.
    ``project_id`` names a new agent's business project (a re-publish keeps its own)."""
    from app.routers import agents as agents_router  # local import: router ↔ service cycle

    if repo.status == REPO_RUNNING:
        raise AppError("coding.turn_running", "a turn is running in this repo — "
                       "deploy it when the turn has ended", status_code=409)
    runtime_client.require_runtime_arn(ws.context)

    existing = agent_access.agent_in(db, ws.id, repo.agent_id, viewer=viewer) \
        if repo.agent_id else None
    if existing is not None and existing.method != "byoc":
        existing = None
    stored_byoc = dict((existing.spec or {}).get("byoc") or {}) if existing else None
    name = agent_name or agent_name_from(repo.name)
    if existing is not None:
        # refused before any export: a busy or protected Agent cannot take an update
        agents_router._refuse_redeploy(existing)
    elif (holder := agent_names.live_holder(db, ws.id, name)) is not None:
        # the same 409 start_create would answer, before the export and staging work
        raise agent_names.name_exists_error(
            name, holder.id if agent_access.agent_visible(db, viewer, holder) else None
        )
    version = python_version or (stored_byoc or {}).get("python_version") or DEFAULT_PYTHON

    snapshots.take_quietly(repo.id, ctx=ws.context, reason="deploy")
    db.refresh(repo)  # the snapshot moved snapshot_key: restore from the newest
    manifest = _stage_export(ws, repo, identity, version)
    block = _byoc_block(manifest, artifact_kind=artifact_kind, entrypoint=entrypoint,
                        python_version=version, stored=stored_byoc)
    creator = Creator.of(identity)
    try:
        if existing is not None:
            data = dict(existing.spec or {})
            data["byoc"] = block
            if description is not None:
                data["system_prompt"] = description
            spec = AgentSpec(**data)
            result = agents_router.republish_agent(
                db, existing, spec, ws, actor=identity.username,
                note=f"coding repo {repo.name}", viewer=viewer,
            )
            created = False
        else:
            spec = AgentSpec(
                name=name,
                method="byoc",
                system_prompt=(description if description is not None
                               else repo.description or ""),
                byoc=block,
                project_id=project_id,
            )
            # request: V3's project resolution (body project_id / X-Project), quota
            # and project budget gates run exactly as for POST /api/agents
            result = agents_router.start_create(db, ws, spec, request=request,
                                                creator=creator, viewer=viewer)
            created = True
    except ValidationError as exc:
        raise _invalid("invalid_spec",
                       f"the deploy settings are not a valid agent: {exc.errors()[0]['msg']}",
                       errors=[e.get("msg") for e in exc.errors()][:5]) from exc
    agent_id = result["agent"]["id"]
    repo.agent_id = agent_id
    repo.updated_at = datetime.now(UTC)
    db.commit()
    logger.info("coding repo %s: %s agent %s (job %s)", repo.id,
                "created" if created else "re-published", agent_id, result["job_id"])
    return {"agent_id": agent_id, "job_id": result["job_id"], "created": created}
