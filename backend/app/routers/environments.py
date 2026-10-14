"""Environment comparison and drift (`app.services.environments`).

Both routes are workspace-scoped: the current workspace is the comparison's baseline
and the one drift is measured in. `compare` also reads the *other* workspaces' ledger
rows, filtered by the caller's grants exactly as `GET /api/workspaces` is (an
administrator sees every workspace, a member only the granted ones), and inside each
one by that workspace's visibility mode.
"""

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.models.ledger import Workspace
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import environments, visibility
from app.services.agentcore.client import control_client
from app.services.visibility import Viewer, require_viewer
from app.services.workspace import granted_workspace_ids

router = APIRouter(prefix="/api/environments", tags=["environments"])


@router.get("/compare")
def compare(
    request: Request,
    agent: str = Query(min_length=1, max_length=64),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """One agent name across every workspace the caller can see. Ledger only."""
    identity = require_identity(request)
    rows = list(db.scalars(select(Workspace).order_by(Workspace.id)))
    if not identity.is_admin:
        granted = set(granted_workspace_ids(db, identity.user_id or ""))
        rows = [row for row in rows if row.id in granted]

    def viewer_for(row: Workspace) -> Viewer:
        # each workspace has its own visibility mode; an admin always sees everything
        return Viewer(
            user_id=identity.user_id,
            filters=visibility.mode(row) == visibility.ISOLATED and not identity.is_admin,
        )

    return environments.compare_agent(
        db, name=agent, workspaces=rows, baseline_id=ws.id, viewer_for=viewer_for
    )


@router.get("/drift")
def drift(
    agent: str | None = Query(default=None, max_length=64),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Ledger expectation vs what AWS reports, for the current workspace's agents the
    caller can see. Read-only; one control-plane read per agent, on request only."""
    return environments.detect_drift(
        db,
        workspace_id=ws.id,
        control=control_client(ws.context),
        viewer=viewer,
        agent_name=agent,
    )
