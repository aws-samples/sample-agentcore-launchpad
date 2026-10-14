"""Console thumbs feedback: rate a chat answer, list the feedback of a range.

`GET /api/feedback` returns `down_session_ids`, which the existing "add to dataset"
flow (`POST /api/eval/datasets/from-sessions`) takes as-is.
"""

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.routers.auth import enabled as auth_enabled
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import feedback as feedback_service
from app.services.visibility import Viewer, require_viewer

router = APIRouter(prefix="/api", tags=["feedback"])


class FeedbackRequest(BaseModel):
    message_id: int = Field(ge=1)
    # required, so a body that forgets it is a 422 rather than a silent withdraw;
    # an explicit null withdraws the caller's verdict
    verdict: Literal["up", "down"] | None


@router.post("/feedback")
def rate_answer(
    req: FeedbackRequest,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    identity = require_identity(request)
    return feedback_service.rate(
        db,
        workspace_id=ws.id,
        message_id=req.message_id,
        verdict=req.verdict,
        # the same actor the chat route records for the ledger session
        actor=identity.username if auth_enabled() else "river",
        viewer=viewer,
    )


@router.get("/feedback")
def list_feedback(
    range: Literal["1h", "6h", "24h", "7d"] = "24h",
    verdict: Literal["up", "down"] | None = None,
    agent_id: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    return feedback_service.list_feedback(
        db, ws.id, range_key=range, verdict=verdict, agent_id=agent_id, limit=limit,
        viewer=viewer,
    )
