"""Gateway tool targets besides hosted MCP servers (design projects §15.12, §15.15).

Console-created Lambda targets (``POST /api/gateway-tools/lambda``):

    validate   the function is in this workspace's account and region and exists;
               the tool schema is a list of MCP tool definitions
    review     the schema is the tool list — it is recorded as a tool review (§15.6)
               before anything exists on the Gateway, so the creator cannot wave it
               through: a member holding ``mcp.approve`` who is not the creator decides
    publish    on approval (background): the Gateway role gets ``lambda:InvokeFunction``
               on the function (`workspace_iam.sync_lambda_tools_policy`), then the
               ``mcp.lambda`` target is created with the approved schema inline
    delete     the target, then the function leaves the role's policy, then the row

A Lambda can run in a VPC, so this is also the way to reach a private backend that
does not depend on the Gateway reaching private endpoints (§15.12, §15.17).

Hand-made MCP-server targets (made through Connections before §15, §15.11) are listed
as *externally hosted*; adopting one records what it lists today as the approved
baseline (so agents already calling it are not cut off) and the hourly ``mcp.health``
task compares later lists with it. A drift opens a review — the target itself is the
customer's and is never withheld or re-pointed here.

Every row is project-owned (the creating / adopting project); another project's id reads
as missing.
"""

import logging
import re
import threading
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.errors import AppError, is_not_found
from app.core.project_scope import current_project, project_context
from app.models.lambda_tools import ExternalMcpTarget, LambdaToolTarget
from app.models.ledger import Agent
from app.models.mcp_review import McpToolReview
from app.services import (
    creators,
    gateway_bootstrap,
    mcp_client,
    mcp_reviews,
    project_gateway,
    workspace_iam,
)
from app.services.agentcore import policy as policy_api
from app.services.agentcore.client import control_client
from app.services.audit import record_audit_event
from app.services.workspace import WorkspaceContext

logger = logging.getLogger(__name__)

LAMBDA_ARN_RE = re.compile(
    r"^arn:(aws[a-z-]*):lambda:([a-z0-9-]+):(\d{12}):function:([A-Za-z0-9_-]{1,64})"
    r"(?::[A-Za-z0-9_$-]{1,128})?$"
)
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_TOOLS = 100
MAX_DESCRIPTION = 4000
SYSTEM_DRIFT = "system:drift"
TARGET_TIMEOUT_S = 300


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _gateway_id(workspace: WorkspaceContext) -> str:
    gateway_id = str(workspace.resources.get("gateway_id") or "")
    if not gateway_id:
        raise AppError(
            "gateway.not_bootstrapped",
            "gateway_id missing from this workspace's resource map — run its bootstrap",
            status_code=503,
        )
    return gateway_id


# ── validation ─────────────────────────────────────────────────────────────────


def validate_tool_schema(raw: Any) -> list[dict[str, Any]]:
    """MCP tool definitions as the Gateway takes them inline:
    ``[{name, description, inputSchema: {type: "object", …}}]`` (422 otherwise)."""

    def bad(message: str, **extra: Any) -> AppError:
        return AppError("lambda_tool.invalid_schema", message, extra, status_code=422)

    if not isinstance(raw, list) or not raw:
        raise bad("tool_schema must be a non-empty list of tool definitions")
    if len(raw) > MAX_TOOLS:
        raise bad(f"at most {MAX_TOOLS} tools per target", count=len(raw))
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, tool in enumerate(raw):
        if not isinstance(tool, dict):
            raise bad("every tool must be an object", index=index)
        name = tool.get("name")
        if not isinstance(name, str) or not TOOL_NAME_RE.match(name):
            raise bad("a tool name is 1-64 letters, digits, '_' or '-'", index=index)
        if name in seen:
            raise bad(f"tool {name!r} is listed twice", index=index)
        seen.add(name)
        description = tool.get("description", "")
        if not isinstance(description, str) or len(description) > MAX_DESCRIPTION:
            raise bad(f"tool {name!r}: description must be text of at most "
                      f"{MAX_DESCRIPTION} characters", index=index)
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise bad(f"tool {name!r}: inputSchema must be a JSON schema of type 'object'",
                      index=index)
        extra = set(tool) - {"name", "description", "inputSchema", "outputSchema"}
        if extra:
            raise bad(f"tool {name!r}: unknown keys {sorted(extra)}", index=index)
        cleaned = {"name": name, "description": description, "inputSchema": schema}
        if isinstance(tool.get("outputSchema"), dict):
            cleaned["outputSchema"] = tool["outputSchema"]
        out.append(cleaned)
    return out


def review_tools(schema: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The schema as a review snapshot (the same shape `mcp_reviews.normalize_tools`
    gives a hosted server's list)."""
    return sorted(
        ({"name": t["name"], "description": t.get("description") or "",
          "input_schema": t.get("inputSchema") or {}} for t in schema),
        key=lambda t: t["name"],
    )


def validate_lambda(workspace: WorkspaceContext, lambda_arn: str) -> None:
    """The function is in this workspace's account and region, and exists."""
    found = LAMBDA_ARN_RE.match(lambda_arn)
    if not found:
        raise AppError("lambda_tool.invalid_arn", "not a Lambda function ARN",
                       {"lambda_arn": lambda_arn}, status_code=422)
    _, region, account, _name = found.groups()[:4]
    if region != workspace.region or (workspace.account_id and account != workspace.account_id):
        raise AppError(
            "lambda_tool.wrong_environment",
            "the function must be in this workspace's account and region "
            f"({workspace.account_id} / {workspace.region}), where the Gateway's role "
            "can be granted to invoke it",
            {"lambda_arn": lambda_arn, "account_id": workspace.account_id,
             "region": workspace.region},
            status_code=422,
        )
    try:
        workspace.client("lambda").get_function(FunctionName=lambda_arn)
    except ClientError as exc:
        if is_not_found(exc):
            raise AppError("lambda_tool.function_missing",
                           "no such Lambda function in this workspace's account",
                           {"lambda_arn": lambda_arn}, status_code=422) from exc
        raise


# ── review subjects ────────────────────────────────────────────────────────────


def lambda_subject(row: LambdaToolTarget) -> mcp_reviews.Subject:
    return mcp_reviews.Subject(
        id=row.id, workspace_id=row.workspace_id, project_id=row.project_id,
        target_name=row.target_name, requested_by=row.created_by,
        requested_by_user_id=row.created_by_user_id,
    )


def external_subject(row: ExternalMcpTarget) -> mcp_reviews.Subject:
    # a drift is nobody's deploy: any member holding mcp.approve may decide it
    return mcp_reviews.Subject(
        id=row.id, workspace_id=row.workspace_id, project_id=row.project_id,
        target_name=row.target_name, requested_by=SYSTEM_DRIFT,
    )


def find_tool(db: Session, workspace_id: str, tool_id: str) -> LambdaToolTarget | ExternalMcpTarget:
    """A Lambda or adopted row of this workspace in the current project (404 else).
    A Lambda row is also found by its Gateway target id."""
    for model in (LambdaToolTarget, ExternalMcpTarget):
        row = db.get(model, tool_id)
        if row is not None and row.workspace_id == workspace_id:
            return row
    row = db.scalars(select(LambdaToolTarget).where(
        LambdaToolTarget.workspace_id == workspace_id, LambdaToolTarget.target_id == tool_id,
    )).first()
    if row is None:
        raise AppError("gateway_tool.not_found", "tool target not found", status_code=404)
    return row


# ── Lambda targets ─────────────────────────────────────────────────────────────


def _names_in_use(db: Session, workspace: WorkspaceContext, gateway_id: str) -> set[str]:
    names = {
        str(t.get("name") or "")
        for t in policy_api.list_gateway_targets(control_client(workspace), gateway_id)
    }
    with project_context(None):  # a name another project holds is taken too
        names |= set(db.scalars(select(LambdaToolTarget.target_name).where(
            LambdaToolTarget.workspace_id == workspace.id,
            LambdaToolTarget.gateway_id == gateway_id,
        )))
    return names


def create_lambda(
    db: Session,
    workspace: WorkspaceContext,
    *,
    lambda_arn: str,
    target_name: str,
    tool_schema: Any,
    description: str,
    actor: str,
    actor_user_id: str | None,
) -> tuple[LambdaToolTarget, McpToolReview]:
    """Validate and record the target with its schema's review. Nothing is created on
    the Gateway until the review is approved. The caller commits."""
    schema = validate_tool_schema(tool_schema)
    validate_lambda(workspace, lambda_arn)
    gateway_id = _gateway_id(workspace)
    if target_name in _names_in_use(db, workspace, gateway_id):
        raise AppError("lambda_tool.target_name_taken",
                       f"the Gateway already has a target named {target_name!r}",
                       {"target_name": target_name}, status_code=409)
    row = LambdaToolTarget(
        workspace_id=workspace.id, gateway_id=gateway_id, target_name=target_name,
        lambda_arn=lambda_arn, description=description, tool_schema=schema,
        status="reviewing", created_by=actor[:64], created_by_user_id=actor_user_id,
    )
    db.add(row)
    db.flush()
    review = mcp_reviews.record_tool_list(db, lambda_subject(row), review_tools(schema),
                                          source="deploy")
    record_audit_event(workspace_id=workspace.id, actor=actor, action="lambda_tool.create",
                       target=row.id, db=db, actor_user_id=actor_user_id)
    return row, review


def _gateway_role_name(control: Any, gateway_id: str) -> str:
    role_arn = str(policy_api.get_gateway(control, gateway_id).get("roleArn") or "")
    if not role_arn:
        raise RuntimeError(f"gateway {gateway_id} reports no service role")
    return role_arn.rsplit("/", 1)[-1]


def _sync_invoke_policy(
    db: Session, workspace: WorkspaceContext, control: Any, gateway_id: str,
) -> None:
    """The Gateway role may invoke exactly the functions of this gateway's published
    (or publishing) Lambda targets, across projects."""
    with project_context(None):
        arns = list(db.scalars(select(LambdaToolTarget.lambda_arn).where(
            LambdaToolTarget.workspace_id == workspace.id,
            LambdaToolTarget.gateway_id == gateway_id,
            LambdaToolTarget.status.in_(("publishing", "live")),
        )))
    workspace_iam.sync_lambda_tools_policy(
        workspace.client("iam"), _gateway_role_name(control, gateway_id), arns
    )


def publish_lambda(workspace: WorkspaceContext, row_id: str) -> None:
    """After an approval: grant the Gateway role, create the target with the approved
    schema, stamp its id. A failure is recorded on the row (``failed``)."""
    db = SessionLocal()
    try:
        row = db.get(LambdaToolTarget, row_id)
        if row is None:
            return
        control = control_client(workspace)
        try:
            approved = mcp_reviews.latest_approved(db, row.id)
            if approved is None or approved.tools_hash != mcp_reviews.tools_hash(
                review_tools(row.tool_schema or [])
            ):
                raise RuntimeError("the schema on the row is not the approved one")
            row.status = "publishing"
            db.commit()
            _sync_invoke_policy(db, workspace, control, row.gateway_id)
            row.target_id = gateway_bootstrap.create_lambda_target(
                control, row.gateway_id, name=row.target_name, lambda_arn=row.lambda_arn,
                tools=row.tool_schema or [],
                description=row.description or f"Launchpad Lambda tools {row.target_name}",
            )
            row.status, row.error = "live", None
            approved.publish_status, approved.publish_error = "published", None
        except (AppError, ClientError, BotoCoreError, RuntimeError, TimeoutError) as exc:
            error = f"{type(exc).__name__}: {getattr(exc, 'message', None) or exc}"[:2000]
            row.status, row.error = "failed", error
            db.commit()
            try:  # a target that never came up keeps no invoke grant
                _sync_invoke_policy(db, workspace, control, row.gateway_id)
            except (ClientError, BotoCoreError, RuntimeError) as sync_exc:
                logger.warning("lambda tool %s: invoke grant not withdrawn: %s", row.id, sync_exc)
            approved = mcp_reviews.latest_approved(db, row.id)
            if approved is not None:
                approved.publish_status, approved.publish_error = "failed", error
        db.commit()
        if row.status == "live" and row.target_id:
            _owned(row.workspace_id, row.target_id,
                   creators.Creator(user_id=row.created_by_user_id, name=row.created_by))
    finally:
        db.close()


def record_ownership(row: LambdaToolTarget) -> None:
    """An adopted target becomes the current project's, as a published one does."""
    if row.target_id:
        _owned(row.workspace_id, row.target_id,
               creators.Creator(user_id=row.created_by_user_id, name=row.created_by))


def _owned(workspace_id: str, target_id: str, creator: creators.Creator | None) -> None:
    """Record the published target as the current project's (its Gateway ownership,
    projects design §5.5) and, in an isolated project, have its Cedar rule written
    once the target is READY (§10.6) — as `identity_gateway_targets` does."""
    db = SessionLocal()
    try:
        creators.record(db, workspace_id, project_gateway.TARGET_KIND, target_id, creator)
    finally:
        db.close()
    project_id = current_project()
    if project_id:
        project_gateway.schedule_reconcile(project_id, workspace_id, wait_target=target_id)


def _disowned(target_id: str) -> None:
    """After a delete: drop the ownership row and the project's rule for the target."""
    db = SessionLocal()
    try:
        owner = project_gateway.target_owner(db, target_id)
        if owner is None:
            return
        workspace_id, project_id = owner
        creators.forget(db, workspace_id, project_gateway.TARGET_KIND, target_id)
    finally:
        db.close()
    if project_id:
        project_gateway.schedule_reconcile(project_id, workspace_id)


def start_publish_lambda(workspace: WorkspaceContext, row: LambdaToolTarget) -> None:
    project_id, row_id = row.project_id, row.id

    def run() -> None:
        with project_context(project_id):
            try:
                publish_lambda(workspace, row_id)
            except Exception:  # a crashed publish must not kill the process
                logger.exception("lambda tool %s: publishing failed", row_id)

    threading.Thread(target=run, daemon=True, name=f"lambda-tool-{row_id[:8]}").start()


def _delete_target_and_wait(control: Any, gateway_id: str, target_id: str) -> None:
    try:
        control.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
    except ClientError as exc:
        if is_not_found(exc):
            return
        raise
    deadline = time.monotonic() + TARGET_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            policy_api.get_gateway_target(control, gateway_id, target_id)
        except ClientError as exc:
            if is_not_found(exc):
                return
            raise
        time.sleep(3)  # nosemgrep: arbitrary-sleep
    raise RuntimeError(f"gateway target {target_id} still present after {TARGET_TIMEOUT_S}s")


def delete_lambda(
    db: Session, workspace: WorkspaceContext, row: LambdaToolTarget, *, actor: str,
) -> None:
    """Target → the role's invoke grant → the row (reviews stay, as history)."""
    if row.status == "publishing":
        raise AppError("lambda_tool.publishing",
                       "the target is being published — delete it once that finishes",
                       status_code=409)
    control = control_client(workspace)
    if row.target_id:
        _delete_target_and_wait(control, row.gateway_id, row.target_id)
        _disowned(row.target_id)
    was_granted = row.status == "live"
    row_id, gateway_id = row.id, row.gateway_id
    db.delete(row)
    db.flush()
    if was_granted:
        _sync_invoke_policy(db, workspace, control, gateway_id)
    record_audit_event(workspace_id=workspace.id, actor=actor, action="lambda_tool.delete",
                       target=row_id, db=db)


def lambda_out(db: Session, row: LambdaToolTarget, *, full: bool = False) -> dict[str, Any]:
    out = {
        "id": row.id, "kind": "lambda", "gateway_id": row.gateway_id,
        "target_id": row.target_id, "target_name": row.target_name,
        "lambda_arn": row.lambda_arn, "description": row.description,
        "tool_names": [t.get("name") for t in row.tool_schema or []],
        "status": row.status, "error": row.error, "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "review": mcp_reviews.state(db, row.id),
    }
    if full:
        out["tool_schema"] = row.tool_schema or []
    return out


def list_lambda(db: Session, workspace_id: str) -> list[dict[str, Any]]:
    rows = db.scalars(select(LambdaToolTarget).where(
        LambdaToolTarget.workspace_id == workspace_id
    ).order_by(LambdaToolTarget.created_at.desc())).all()
    return _label_shared(db, [lambda_out(db, row) for row in rows], "id")


def _label_shared(db: Session, items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Add `shared_from` ({project_id, name, access}) to the targets another project
    lent this one (projects §11.2) — read like its own, changed only by its owner."""
    from app.services import resource_shares

    ids = {item[key] for item in items if item.get(key)}
    lent = resource_shares.shared_from(db, resource_shares.KIND_GATEWAY_TARGET, ids)
    for item in items:
        if item.get(key) in lent:
            item["shared_from"] = lent[item[key]]
    return items


# ── externally hosted MCP targets (§15.15) ─────────────────────────────────────


def _mcp_endpoint(target: dict[str, Any]) -> str | None:
    server = (((target.get("targetConfiguration") or {}).get("mcp") or {}).get("mcpServer"))
    return str(server.get("endpoint") or "") if isinstance(server, dict) else None


def _hosted_target_ids(db: Session, workspace_id: str) -> tuple[set[str], set[str]]:
    """Target ids and names the hosted MCP servers of this workspace own (any project)."""
    ids: set[str] = set()
    names: set[str] = set()
    with project_context(None):
        for agent in db.scalars(select(Agent).where(
            Agent.workspace_id == workspace_id, Agent.status != "deleted",
        )):
            spec = agent.spec or {}
            if spec.get("protocol") != "mcp":
                continue
            mcp = spec.get("mcp") or {}
            if mcp.get("target_id"):
                ids.add(str(mcp["target_id"]))
            if mcp.get("target_name"):
                names.add(str(mcp["target_name"]))
    return ids, names


def list_external(db: Session, workspace: WorkspaceContext) -> list[dict[str, Any]]:
    """MCP-server targets on the workspace Gateway that no hosted server owns."""
    gateway_id = _gateway_id(workspace)
    hosted_ids, hosted_names = _hosted_target_ids(db, workspace.id)
    with project_context(None):
        adopted_anywhere = {
            r.target_id: r.project_id for r in db.scalars(select(ExternalMcpTarget).where(
                ExternalMcpTarget.workspace_id == workspace.id))
        }
    mine = {
        r.target_id: r for r in db.scalars(select(ExternalMcpTarget).where(
            ExternalMcpTarget.workspace_id == workspace.id))
    }
    out: list[dict[str, Any]] = []
    for target in policy_api.list_gateway_target_details(control_client(workspace), gateway_id):
        endpoint = _mcp_endpoint(target)
        target_id = str(target.get("targetId") or "")
        name = str(target.get("name") or "")
        if endpoint is None or target_id in hosted_ids or name in hosted_names:
            continue
        row = mine.get(target_id)
        out.append({
            "target_id": target_id, "target_name": name, "endpoint": endpoint,
            "status": target.get("status"), "hosting": "external",
            "tool_id": row.id if row is not None else None,
            "adopted_elsewhere": row is None and target_id in adopted_anywhere,
            "review": mcp_reviews.state(db, row.id) if row is not None else None,
            "health": (row.health or {}) if row is not None else None,
        })
    return _label_shared(db, sorted(out, key=lambda item: item["target_name"]), "tool_id")


def adopt_external(
    db: Session, workspace: WorkspaceContext, target_id: str, *, actor: str,
    actor_user_id: str | None,
) -> ExternalMcpTarget:
    """Bring a hand-made MCP target under tool review: its current list becomes the
    approved baseline (agents calling it are not cut off). The caller commits."""
    gateway_id = _gateway_id(workspace)
    control = control_client(workspace)
    try:
        target = policy_api.get_gateway_target(control, gateway_id, target_id)
    except ClientError as exc:
        if is_not_found(exc):
            raise AppError("gateway_tool.not_found", "gateway target not found",
                           status_code=404) from exc
        raise
    endpoint = _mcp_endpoint(target)
    hosted_ids, hosted_names = _hosted_target_ids(db, workspace.id)
    name = str(target.get("name") or "")
    if endpoint is None or target_id in hosted_ids or name in hosted_names:
        raise AppError("gateway_tool.not_external",
                       "only a hand-made MCP-server target can be brought under review",
                       status_code=409)
    with project_context(None):
        taken = db.scalars(select(ExternalMcpTarget.id).where(
            ExternalMcpTarget.workspace_id == workspace.id,
            ExternalMcpTarget.target_id == target_id,
        )).first()
    if taken is not None:
        raise AppError("gateway_tool.already_adopted",
                       "this target is already under tool review", status_code=409)
    tools = mcp_reviews.normalize_tools(mcp_client.tools_list(workspace), name)
    row = ExternalMcpTarget(
        workspace_id=workspace.id, gateway_id=gateway_id, target_id=target_id,
        target_name=name, endpoint=endpoint, adopted_by=actor[:64],
        adopted_by_user_id=actor_user_id,
        health={"status": "healthy", "checked_at": _now_iso(), "tool_count": len(tools)},
    )
    db.add(row)
    db.flush()
    mcp_reviews.record_baseline(
        db, external_subject(row), tools,
        note=f"recorded as approved: hand-made target brought under review by {actor}",
    )
    record_audit_event(workspace_id=workspace.id, actor=actor, action="gateway_tool.adopt",
                       target=row.id, db=db, actor_user_id=actor_user_id)
    return row


# ── other targets nothing here manages yet (hand-made Lambda, OpenAPI, …) ────────


def _target_kind(target: dict[str, Any]) -> str:
    """The `mcp` union member a target uses: lambda, mcpServer, openApiSchema, …"""
    mcp = (target.get("targetConfiguration") or {}).get("mcp") or {}
    return next(iter(mcp), "unknown") if isinstance(mcp, dict) else "unknown"


def _inline_lambda(target: dict[str, Any]) -> tuple[str, list[dict[str, Any]] | None]:
    """(function ARN, inline tool schema) of a Lambda target; the schema is None when
    it lives in S3 (no list to record as the approved baseline)."""
    lam = ((target.get("targetConfiguration") or {}).get("mcp") or {}).get("lambda") or {}
    payload = (lam.get("toolSchema") or {}).get("inlinePayload")
    return str(lam.get("lambdaArn") or ""), payload if isinstance(payload, list) else None


def _unmanaged_reason(
    workspace: WorkspaceContext, target: dict[str, Any], kind: str,
) -> str | None:
    """Why a target cannot be adopted here (None = it can): a bootstrap target, one
    bound to a Connection (managed under Connections), a kind other than an inline-
    schema Lambda in this workspace's account and region."""
    from app.services import identity_gateway_targets

    if str(target.get("name") or "") in identity_gateway_targets.SYSTEM_TARGET_NAMES:
        return "system"
    if identity_gateway_targets.target_binding(target).get("connection"):
        return "connection"
    if kind != "lambda":
        return "kind"
    arn, payload = _inline_lambda(target)
    if payload is None:
        return "schema"
    found = LAMBDA_ARN_RE.match(arn)
    if not found or found.group(2) != workspace.region or (
        workspace.account_id and found.group(3) != workspace.account_id
    ):
        return "environment"
    try:
        validate_tool_schema(payload)
    except AppError:
        return "schema"
    return None


def list_unmanaged(db: Session, workspace: WorkspaceContext) -> list[dict[str, Any]]:
    """Targets on the workspace Gateway that neither tab above lists: not a console
    Lambda target, not a hosted MCP server, not an MCP-server target (those are the
    externally hosted list) — typically a Lambda target made by hand or a bootstrap
    one. Each says whether it can be brought under management here and why not."""
    from app.services import identity_gateway_targets

    gateway_id = _gateway_id(workspace)
    hosted_ids, hosted_names = _hosted_target_ids(db, workspace.id)
    mine = set(db.scalars(select(LambdaToolTarget.target_id).where(
        LambdaToolTarget.workspace_id == workspace.id)))
    with project_context(None):
        anywhere = set(db.scalars(select(LambdaToolTarget.target_id).where(
            LambdaToolTarget.workspace_id == workspace.id)))
    out: list[dict[str, Any]] = []
    for target in policy_api.list_gateway_target_details(control_client(workspace), gateway_id):
        target_id = str(target.get("targetId") or "")
        name = str(target.get("name") or "")
        kind = _target_kind(target)
        if (kind == "mcpServer" or target_id in mine or target_id in hosted_ids
                or name in hosted_names):
            continue
        arn, payload = _inline_lambda(target) if kind == "lambda" else ("", None)
        elsewhere = target_id in anywhere
        out.append({
            "target_id": target_id, "target_name": name, "kind": kind,
            "status": target.get("status"), "description": target.get("description") or "",
            "lambda_arn": arn or None,
            "tool_names": [str(t.get("name")) for t in payload or [] if isinstance(t, dict)],
            "system": name in identity_gateway_targets.SYSTEM_TARGET_NAMES,
            "connection": identity_gateway_targets.target_binding(target).get("connection"),
            "adopted_elsewhere": elsewhere,
            "reason": "elsewhere" if elsewhere else _unmanaged_reason(workspace, target, kind),
        })
    return sorted(out, key=lambda item: item["target_name"])


def adopt_lambda(
    db: Session, workspace: WorkspaceContext, target_id: str, *, actor: str,
    actor_user_id: str | None,
) -> LambdaToolTarget:
    """Bring a hand-made Lambda target under management: a ``live`` console row for
    it, its inline schema recorded as the approved baseline (agents calling it are not
    cut off), the function added to the Gateway role's invoke grant the console keeps,
    and the target owned by the current project. Nothing on the target changes; from
    here it is listed, reviewed and deleted like a console-created one. The caller
    commits."""
    gateway_id = _gateway_id(workspace)
    control = control_client(workspace)
    try:
        target = policy_api.get_gateway_target(control, gateway_id, target_id)
    except ClientError as exc:
        if is_not_found(exc):
            raise AppError("gateway_tool.not_found", "gateway target not found",
                           status_code=404) from exc
        raise
    with project_context(None):
        taken = db.scalars(select(LambdaToolTarget.id).where(
            LambdaToolTarget.workspace_id == workspace.id,
            LambdaToolTarget.target_id == target_id,
        )).first()
    if taken is not None:
        raise AppError("gateway_tool.already_adopted",
                       "this target is already managed here", status_code=409)
    hosted_ids, hosted_names = _hosted_target_ids(db, workspace.id)
    name = str(target.get("name") or "")
    kind = _target_kind(target)
    reason = (
        "hosted" if target_id in hosted_ids or name in hosted_names
        else _unmanaged_reason(workspace, target, kind)
    )
    if reason is not None:
        raise AppError(
            "gateway_tool.not_adoptable",
            "only a hand-made Lambda target with an inline tool schema in this "
            "workspace's account and region can be brought under management",
            {"target_id": target_id, "reason": reason}, status_code=409,
        )
    arn, payload = _inline_lambda(target)
    schema = validate_tool_schema(payload)
    row = LambdaToolTarget(
        workspace_id=workspace.id, gateway_id=gateway_id, target_id=target_id,
        target_name=name, lambda_arn=arn,
        description=str(target.get("description") or "")[:500], tool_schema=schema,
        status="live", created_by=actor[:64], created_by_user_id=actor_user_id,
    )
    db.add(row)
    db.flush()
    mcp_reviews.record_baseline(
        db, lambda_subject(row), review_tools(schema),
        note=f"recorded as approved: hand-made Lambda target brought under management by {actor}",
    )
    _sync_invoke_policy(db, workspace, control, gateway_id)
    record_audit_event(workspace_id=workspace.id, actor=actor, action="lambda_tool.adopt",
                       target=row.id, db=db, actor_user_id=actor_user_id)
    return row


def check_external(workspace: WorkspaceContext, row_id: str) -> dict[str, Any]:
    """One drift check of an adopted target: a list other than the approved one opens
    a review (the target is left alone). Recorded on the row."""
    db = SessionLocal()
    try:
        row = db.get(ExternalMcpTarget, row_id)
        if row is None:
            return {}
        previous = dict(row.health or {})
        health: dict[str, Any] = {"checked_at": _now_iso(), "consecutive_failures": 0,
                                  "error": None}
        try:
            tools = mcp_reviews.normalize_tools(mcp_client.tools_list(workspace),
                                                row.target_name)
            review = mcp_reviews.record_tool_list(db, external_subject(row), tools,
                                                  source="drift")
            health.update(status="healthy" if review.status == "approved" else "drift",
                          tool_count=len(tools), review_id=review.id)
        except (AppError, httpx.HTTPError) as exc:
            health.update(status="failing", error=f"{type(exc).__name__}: {exc}"[:500],
                          consecutive_failures=int(previous.get("consecutive_failures") or 0)
                          + 1)
        row.health = health
        db.commit()
        return health
    finally:
        db.close()
