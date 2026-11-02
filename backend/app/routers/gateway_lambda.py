"""Gateway tool targets the console makes or adopts (design projects §15.12, §15.13,
§15.15).

* ``/api/gateway-tools/lambda`` — console-created Lambda targets. Creating and deleting
  one changes the workspace Gateway every project shares, so both are
  ``infra+perm:agents.deploy`` (the project's ``agents.deploy`` while the workspace holds
  one project, administrators only once it holds several). A new target's tool schema
  waits for review; the target is created on approval.
* ``/api/gateway-tools/unmanaged`` — the Gateway's other targets (hand-made Lambda,
  bootstrap, OpenAPI); a hand-made inline-schema Lambda one can be adopted as a
  console Lambda target (approved baseline, ``mcp.approve``, like the external adopt).
* ``/api/gateway-tools/external`` — hand-made MCP-server targets, listed as externally
  hosted; adopting one under review (approved baseline) is ``mcp.approve`` and, with
  several projects in the workspace, an administrator's (``require_infra``).
* ``/api/gateway-tools/{tool_id}/reviews…`` — the tool reviews of either kind; reads are
  participation, decisions ``perm:mcp.approve`` and never the creator's (409).

A row of another project answers 404 like a missing one.
"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.actor import user_id_for
from app.core.db import get_db
from app.core.errors import NotFoundError
from app.models.lambda_tools import LambdaToolTarget
from app.models.mcp_review import McpToolReview
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.schemas.agent import MCP_TARGET_NAME_RE
from app.services import gateway_tools, mcp_reviews

router = APIRouter(prefix="/api/gateway-tools", tags=["gateway-tools"])


class LambdaTargetIn(BaseModel):
    model_config = {"extra": "forbid"}

    lambda_arn: str = Field(min_length=20, max_length=512)
    target_name: str = Field(min_length=1, max_length=100, pattern=MCP_TARGET_NAME_RE)
    # MCP tool definitions, inline: [{name, description, inputSchema}]
    tool_schema: list[dict[str, Any]] = Field(min_length=1, max_length=gateway_tools.MAX_TOOLS)
    description: str = Field(default="", max_length=500)


class DecisionIn(BaseModel):
    model_config = {"extra": "forbid"}

    note: str = Field(default="", max_length=mcp_reviews.MAX_NOTE)


@router.get("/lambda")
def list_lambda_targets(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return {"targets": gateway_tools.list_lambda(db, ws.id)}


@router.post("/lambda", status_code=201)
def create_lambda_target(
    body: LambdaTargetIn,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Record the target and open its schema's review (no Gateway change yet)."""
    actor = require_identity(request).username
    row, review = gateway_tools.create_lambda(
        db, ws.context, lambda_arn=body.lambda_arn, target_name=body.target_name,
        tool_schema=body.tool_schema, description=body.description, actor=actor,
        actor_user_id=user_id_for(actor),
    )
    db.commit()
    return {**gateway_tools.lambda_out(db, row, full=True),
            "review_id": review.id}


@router.delete("/lambda/{target_id}")
def delete_lambda_target(
    target_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """``target_id`` is the console's id of the Lambda target (its Gateway target id
    works too)."""
    row = gateway_tools.find_tool(db, ws.id, target_id)
    if not isinstance(row, LambdaToolTarget):
        raise NotFoundError("gateway_tool.not_found", "Lambda tool target not found")
    gateway_tools.delete_lambda(db, ws.context, row, actor=require_identity(request).username)
    db.commit()
    return {"deleted": target_id}


@router.get("/external")
def list_external_targets(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return {"targets": gateway_tools.list_external(db, ws.context)}


@router.post("/external/{target_id}/adopt", status_code=201)
def adopt_external_target(
    target_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Bring a hand-made MCP target under tool review (its list today is approved)."""
    from app.core.route_policy import require_infra

    # the target is the workspace's, not a project's: with several projects, admins only
    require_infra(request, "infra+perm:mcp.approve")
    actor = require_identity(request).username
    row = gateway_tools.adopt_external(db, ws.context, target_id, actor=actor,
                                       actor_user_id=user_id_for(actor))
    db.commit()
    return {"tool_id": row.id, "target_id": row.target_id, "target_name": row.target_name,
            "review": mcp_reviews.state(db, row.id)}


@router.get("/unmanaged")
def list_unmanaged_targets(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """The workspace Gateway's other targets — hand-made Lambda, bootstrap, OpenAPI —
    each with whether it can be brought under management here (``reason`` if not)."""
    return {"targets": gateway_tools.list_unmanaged(db, ws.context)}


@router.post("/unmanaged/{target_id}/adopt", status_code=201)
def adopt_unmanaged_target(
    target_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Manage a hand-made Lambda target: its inline schema today is the approved
    baseline; from here it is listed, reviewed and deleted like a console-created one."""
    from app.core.route_policy import require_infra

    # the target is the workspace's, not a project's: with several projects, admins only
    require_infra(request, "infra+perm:mcp.approve")
    actor = require_identity(request).username
    row = gateway_tools.adopt_lambda(db, ws.context, target_id, actor=actor,
                                     actor_user_id=user_id_for(actor))
    db.commit()
    gateway_tools.record_ownership(row)
    return gateway_tools.lambda_out(db, row, full=True)


# ── reviews of either kind ─────────────────────────────────────────────────────


def _review(db: Session, tool_id: str, review_id: str) -> McpToolReview:
    review = db.get(McpToolReview, review_id)
    if review is None or review.agent_id != tool_id:
        raise NotFoundError("mcp_review.not_found", "tool review not found")
    return review


def _detail(db: Session, review: McpToolReview) -> dict[str, Any]:
    out = mcp_reviews.review_out(review)
    base = db.get(McpToolReview, review.base_review_id) if review.base_review_id else None
    out["base_tools"] = (base.tools or []) if base is not None else None
    return out


@router.get("/{tool_id}/reviews")
def list_tool_reviews(
    tool_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    row = gateway_tools.find_tool(db, ws.id, tool_id)
    return {
        "agent_id": row.id,
        **mcp_reviews.state(db, row.id),
        "reviews": [mcp_reviews.review_out(r, full=False)
                    for r in mcp_reviews.list_reviews(db, row.id)],
    }


@router.get("/{tool_id}/reviews/{review_id}")
def get_tool_review(
    tool_id: str,
    review_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    row = gateway_tools.find_tool(db, ws.id, tool_id)
    return _detail(db, _review(db, row.id, review_id))


def _decide(
    request: Request, db: Session, ws: WorkspaceScope, tool_id: str, review_id: str,
    body: DecisionIn, *, approve: bool,
) -> Any:
    row = gateway_tools.find_tool(db, ws.id, tool_id)
    review = mcp_reviews.decide(
        db, _review(db, row.id, review_id), actor=require_identity(request).username,
        approve=approve, note=body.note,
    )
    publish = isinstance(row, LambdaToolTarget) and row.status in ("reviewing", "failed")
    if isinstance(row, LambdaToolTarget):
        if approve and publish:
            row.status = "publishing"
        elif not approve:
            row.status = "rejected"
    else:
        # an adopted target is never withheld: approving a drift just accepts it
        review.publish_status = "published" if approve else None
    db.commit()
    if approve and publish:
        gateway_tools.start_publish_lambda(ws.context, row)
    return _detail(db, review)


@router.post("/{tool_id}/reviews/{review_id}/approve")
def approve_tool_review(
    tool_id: str,
    review_id: str,
    body: DecisionIn,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> Any:
    """Approve; a Lambda target is then published in the background."""
    return _decide(request, db, ws, tool_id, review_id, body, approve=True)


@router.post("/{tool_id}/reviews/{review_id}/reject")
def reject_tool_review(
    tool_id: str,
    review_id: str,
    body: DecisionIn,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> Any:
    return _decide(request, db, ws, tool_id, review_id, body, approve=False)

