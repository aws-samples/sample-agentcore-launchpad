"""Attach-without-registering skill sources.

The create wizard's custom skill flow: the user inspects a zip / git / url
source through the registry's ``/api/registry/skills/inspect`` (same staging,
validation caps and error matrix), then imports the selected bundles HERE —
files land under a non-registry S3 prefix (``agent-skills/{uid8}/{name}/``)
and the returned ``path`` plugs straight into ``spec.skills``. No registry
record is created; the registry catalog is untouched.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.errors import AppError
from app.routers.registry import (
    ImportSelection,
    _drop_staging,
    _match_bundle,
    _staging,
    _sweep_staging,
)
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services.registry_console import _delete_keys, upload_bundle_files
from app.services.skill_ingest import (
    SKILL_NAME_RE,
    SkillBundle,
    SkillValidationError,
    validate_bundle,
)

router = APIRouter(prefix="/api/agent-skills", tags=["agent-skills"])


class AttachRequest(BaseModel):
    staging_id: str
    selections: list[ImportSelection]


@router.post("/import")
def import_for_agent(
    req: AttachRequest,
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Upload each selected staged bundle to a per-request S3 prefix and return
    attachable ``{name, path}`` entries. Mirrors the registry import semantics:
    per-item failures never abort the batch; staging survives any failure so
    the user can retry without re-uploading."""
    _sweep_staging()
    entry = _staging.get(req.staging_id)
    if entry is None:
        raise AppError(
            "registry.staging_expired",
            "staging session expired or unknown — re-inspect the source",
            status_code=410,
        )
    bucket = ws.context.resources.get("artifacts_bucket")
    if not bucket:
        raise RuntimeError(
            "artifacts_bucket missing from this workspace's resource map — run its bootstrap"
        )
    s3 = ws.context.client("s3")

    bundles: list[SkillBundle] = entry["bundles"]
    uid = uuid.uuid4().hex[:8]
    skills: list[dict[str, Any]] = []
    taken: set[str] = set()
    for sel in req.selections:
        label = sel.name_override or sel.name or f"#{sel.index}"
        bundle = _match_bundle(bundles, sel)
        if bundle is None:
            skills.append({"name": label, "ok": False,
                           "error": "no staged skill matches this selection",
                           "error_code": "registry.skill_not_staged"})
            continue
        name = (sel.name_override or bundle.name or "").strip()
        try:
            if not SKILL_NAME_RE.match(name):
                raise SkillValidationError(
                    f"skill name '{name}' must match ^[a-z][a-z0-9-]{{2,63}}$"
                )
            if name in taken:
                raise SkillValidationError(f"skill '{name}' selected twice")
            validate_bundle(bundle)
            prefix = f"agent-skills/{uid}/{name}/"
            uploaded: list[str] = []
            try:
                upload_bundle_files(bundle, bucket, prefix, s3, uploaded=uploaded)
            except Exception:
                _delete_keys(s3, bucket, uploaded)  # no half-uploaded prefixes
                raise
            taken.add(name)
            skills.append({
                "name": name,
                "ok": True,
                "path": f"s3://{bucket}/{prefix}",
                "description": (sel.description_override or bundle.description or "").strip(),
            })
        except SkillValidationError as exc:
            skills.append({"name": label, "ok": False,
                           "error": str(exc), "error_code": "registry.skill_invalid"})
        except Exception as exc:  # never let one bad skill abort the batch
            skills.append({"name": label, "ok": False,
                           "error": str(exc), "error_code": "agents.skill_attach_failed"})
    if skills and all(s["ok"] for s in skills):
        _drop_staging(req.staging_id)
    return {"skills": skills}
