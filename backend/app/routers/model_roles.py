"""Model roles for the console (r15-e, upstream #275 subset — `services/model_roles.py`).

- ``GET /api/model-defaults`` (every signed-in role): each role's effective model, what a
  reset returns to, and who overrode it — the V2 pickers (agent wizard, task wizard,
  evaluators, assistant proposal) take their pre-selection from here, so a change needs no
  frontend rebuild.
- ``PUT`` / ``DELETE /api/settings/model-roles/{role}`` (ADMIN, `core/route_policy.py`):
  set or clear an override; applied at once, persisted in ``platform_settings``, journaled
  as ``model_role.set`` / ``model_role.reset`` in ``model_catalog_changes``, no restart.

Hub-global (one ledger table, no workspace column), so none of these routes is
workspace-scoped. The probe of a role's model is the registry's own re-probe
(``POST /api/settings/model-providers/{provider_id}/models/{model_ref}/probe``) — there is
no second probe path.
"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.routers.auth import Identity, require_identity
from app.schemas.model_providers import ModelRoleOut, ModelRoleSetIn, ModelRolesOut
from app.services import model_roles

router = APIRouter(tags=["model-roles"])


@router.get("/api/model-defaults", response_model=ModelRolesOut)
def model_defaults(db: Session = Depends(get_db),
                   identity: Identity = Depends(require_identity)) -> dict[str, Any]:
    """Every role with its effective model (override > env / yaml > default).

    N17-obs251 (user ruling 2026-10-22): members read the roles too (the console's Model
    roles tab is read-only for them); the row never carried a secret, and for a member the
    overriding administrator's username is redacted — ``overridden_by`` is ``""`` when the
    role is overridden (the console says "overridden by an administrator"), ``null`` when
    not. Administrators see the username as before."""
    rows = model_roles.effective(db)
    if not identity.is_admin:
        rows = [{**row, "overridden_by": "" if row["overridden_by"] is not None else None}
                for row in rows]
    return {"roles": rows}


@router.put("/api/settings/model-roles/{role}", response_model=ModelRoleOut)
def set_model_role(role: str, body: ModelRoleSetIn, request: Request,
                   db: Session = Depends(get_db)) -> dict[str, Any]:
    """404 ``models.unknown_role``; 422 ``models.model_not_allowed {role, model_id, reason}``
    for a model outside the catalog, disabled, not a chat row, or without tool use where the
    role runs tools (``reason`` ∈ ``empty | not_in_catalog | not_chat | not_embedding |
    disabled | personal_scope | no_tool_use``; ``not_embedding`` = a chat row offered to the
    embedding role, N17-obs252)."""
    return model_roles.set_override(db, role, body.model_id, require_identity(request).username)


@router.delete("/api/settings/model-roles/{role}", response_model=ModelRoleOut)
def clear_model_role(role: str, request: Request,
                     db: Session = Depends(get_db)) -> dict[str, Any]:
    """Back to env / launchpad.yaml / the built-in default (a no-op without an override)."""
    return model_roles.clear_override(db, role, require_identity(request).username)
