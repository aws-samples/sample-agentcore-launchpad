"""Creation-time validation of a spec against its method's declared domain (round 16).

The ONE request-boundary call every creation entry point makes before its first write —
``POST /api/agents``, the re-publish routes (``routers.agents.republish_agent``) and the
architect approval (``assistant.service.approve_proposal``): the method's
``MethodCapabilities`` (declared at ``register_method``) decide what its generate stage can
render, the workspace's resource map decides what the deploy can reach. Design:
plans/CAPABILITY-MATRIX-DESIGN.md §1.2; proof: ``tests/test_creation_matrix.py`` and
``tests/test_fail_early_guard.py``.
"""

import json
import re
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.deployer.pipeline import method_capabilities
from app.models.ledger import Agent, KnowledgeBase, RegistryRecord
from app.schemas.agent import (
    GATEWAY_TOOL_UNBOUND_CODE,
    METHOD_UNSUPPORTED_CODE,
    RESOURCE_MISSING_CODE,
    AgentSpec,
    GatewayToolUnbound,
    MethodUnsupported,
    WorkspaceResourceMissing,
    validate_against_method,
)

REFERENCE_MISSING_CODE = "agent.reference_missing"
LEGACY_BUILTIN_CODE = "agent.builtin_unsupported"


def require_method_support(spec: AgentSpec, workspace: Any, db: Session | None = None) -> None:
    """422 ``agent.method_unsupported`` ``{method, field, value, supported}`` for an option
    the method cannot render; 409 ``workspace.resource_missing`` ``{resource, how_to_fix}``
    for a workspace resource the deploy needs. Reads no ledger row and makes no AWS call —
    except, with ``db``, the one ledger lookup of ``require_known_references``."""
    try:
        validate_against_method(spec, method_capabilities(spec.method), workspace)
    except MethodUnsupported as exc:
        detail = exc.detail()
        if exc.field == "tools.builtin":
            # N15-obs16's code, kept one round as an alias on the wire (docs/api.md)
            detail["legacy_code"] = LEGACY_BUILTIN_CODE
        raise AppError(METHOD_UNSUPPORTED_CODE, str(exc), detail, status_code=422) from exc
    except WorkspaceResourceMissing as exc:
        raise AppError(RESOURCE_MISSING_CODE, str(exc), exc.detail(), status_code=409) from exc
    except GatewayToolUnbound as exc:
        raise AppError(GATEWAY_TOOL_UNBOUND_CODE, str(exc), exc.detail(), status_code=422) from exc
    if db is not None:
        require_known_references(db, workspace, spec)
    else:
        # no ledger at hand (the architect approval): a record-only Gateway ref cannot be
        # resolved, so it is refused rather than deployed without an InvokeGateway grant
        _require_gateway_ids_resolved(spec, workspace)


def require_known_references(db: Session, workspace: Any, spec: AgentSpec) -> None:
    """N16-obs73: every ``knowledge_bases[].kb_id`` names a knowledge base of THIS workspace
    (a ledger row — the CN KB is self-hosted); a bogus id used to deploy ACTIVE and fail at
    the first search. 422 ``agent.reference_missing {field, value, kind, how_to_fix}``.
    Visibility (isolated mode) is ``agent_access.check_pins``' job and answers 404 first."""
    _resolve_gateway_records(db, workspace, spec)
    _require_gateway_ids_resolved(spec, workspace)
    _require_a2a_peers(db, workspace, spec)
    wanted = [ref.kb_id for ref in spec.knowledge_bases]
    if not wanted:
        return
    known = {
        row[0] for row in db.query(KnowledgeBase.kb_id)
        .filter(KnowledgeBase.workspace_id == workspace.id, KnowledgeBase.kb_id.in_(wanted))
        .all()
    }
    for kb_id in wanted:
        if kb_id not in known:
            raise AppError(
                REFERENCE_MISSING_CODE,
                f"knowledge_bases: {kb_id!r} is not a knowledge base of this workspace",
                {"field": "knowledge_bases", "value": kb_id, "kind": "knowledge_base",
                 "how_to_fix": "pick a knowledge base from GET /api/knowledge-bases/attachable, "
                               "or create it first"},
                status_code=422,
            )


A2A_PEER_KIND = "a2a_peer"
GATEWAY_RECORD_KIND = "gateway_record"


def _gateway_url_id(url: str) -> str:
    """The gateway id a Gateway MCP endpoint URL names (``https://<id>.gateway.…/mcp``)."""
    match = re.match(r"^https://([0-9a-z-]+)\.gateway\.bedrock-agentcore\.", url or "")
    return match.group(1) if match else ""


def _reference_missing(field: str, value: str, kind: str, message: str,
                       how_to_fix: str) -> AppError:
    return AppError(REFERENCE_MISSING_CODE, message,
                    {"field": field, "value": value, "kind": kind, "how_to_fix": how_to_fix},
                    status_code=422)


def _require_gateway_ids_resolved(spec: AgentSpec, workspace: Any) -> None:
    """Resolve every Gateway ToolRef to an account gateway and its authoritative URL."""
    gateway_refs = [t for t in spec.tools if t.type == "gateway"]
    if not gateway_refs:
        return
    resources = getattr(workspace, "resources", None) or {}
    workspace_gateway_id = str(resources.get("gateway_id") or "")
    for tool in gateway_refs:
        gateway_id = str((tool.config or {}).get("gateway_id") or "")
        if not gateway_id:
            exc = GatewayToolUnbound(tool.name, "absent", gateway_id or None, workspace_gateway_id)
            raise AppError(GATEWAY_TOOL_UNBOUND_CODE, str(exc), exc.detail(), status_code=422)
        if gateway_id == workspace_gateway_id:
            tool.config.pop("gateway_url", None)
            if resources.get("gateway_url"):
                tool.config["gateway_url"] = str(resources["gateway_url"])
        else:
            from app.services.agentcore.client import control_client
            from app.services.agentcore.gateway import get_gateway

            try:
                detail = get_gateway(control_client(workspace), gateway_id)
                arn = str(detail.get("gatewayArn") or "").split(":")
                if len(arn) < 6 or arn[3] != workspace.region or arn[4] != workspace.account_id:
                    raise ValueError("gateway account or region differs")
            except Exception as cause:
                exc = GatewayToolUnbound(tool.name, "foreign", gateway_id, workspace_gateway_id)
                raise AppError(GATEWAY_TOOL_UNBOUND_CODE, str(exc), exc.detail(),
                               status_code=422) from cause
            tool.config["gateway_url"] = detail["gatewayUrl"]


def _resolve_gateway_records(db: Session, workspace: Any, spec: AgentSpec) -> None:
    """N17-obs487 (V1, record-only refs): ``config.record_id`` must be an MCP registry record of
    THIS workspace whose server URL is the workspace Gateway's endpoint; the stored spec is then
    normalised with ``config.gateway_id`` so ``agent_iam`` and the renderer agree."""
    resources = getattr(workspace, "resources", None) or {}
    workspace_gateway_id = str(resources.get("gateway_id") or "")
    pending = [t for t in spec.tools
               if t.type == "gateway" and not (t.config or {}).get("gateway_id")]
    if not pending:
        return
    wanted = [str(t.config["record_id"]) for t in pending]
    rows = {row.id: row for row in db.query(RegistryRecord)
            .filter(RegistryRecord.workspace_id == workspace.id, RegistryRecord.id.in_(wanted))
            .all()}
    for tool in pending:
        record_id = str(tool.config["record_id"])
        row = rows.get(record_id)
        if row is None:
            raise _reference_missing(
                "tools.gateway.record_id", record_id, GATEWAY_RECORD_KIND,
                f"gateway tool {tool.name!r}: record {record_id!r} is not a registry record of "
                "this workspace",
                "pick the server from GET /api/registry/attachables "
                "(mcp_servers[].record_id)")
        url = ""
        try:
            mcp = (row.descriptors or {}).get("mcp") or {}
            server = json.loads(mcp["server"]["inlineContent"])
            url = str((server.get("remotes") or [{}])[0].get("url") or "")
        except (KeyError, TypeError, ValueError, IndexError):
            url = ""
        gateway_id = _gateway_url_id(url)
        if not gateway_id:
            exc = GatewayToolUnbound(tool.name, "foreign", _gateway_url_id(url) or url or None,
                                     workspace_gateway_id)
            raise AppError(GATEWAY_TOOL_UNBOUND_CODE, str(exc), exc.detail(), status_code=422)
        tool.config["gateway_id"] = gateway_id  # checked against AWS before any write


def _arn_account(arn: str) -> tuple[str, str]:
    parts = arn.split(":")
    return (parts[1], parts[4]) if len(parts) >= 6 else ("", "")


def _require_a2a_peers(db: Session, workspace: Any, spec: AgentSpec) -> None:
    """N17-obs496 (V2): an ``a2a`` ToolRef is bound to an ACTIVE A2A server agent of THIS
    workspace — ``config.record_id`` is an APPROVED A2A registry record of the workspace, the
    agent owning that record (``Agent.registry_record_id``, the attachables chain) is active with
    ``protocol: a2a``, and ``config.runtime_arn`` is exactly that agent's Runtime ARN in this
    account and partition. Otherwise the consumer's role would be granted ``InvokeAgentRuntime``
    on an arbitrary runtime (any 12-digit account) while the hub's token audience
    (``principal_mint._peer_ids``) names something else."""
    refs = [t for t in spec.tools if t.type == "a2a"]
    if not refs:
        return
    account = str(getattr(workspace, "account_id", "") or "")
    region = str(getattr(workspace, "region", "") or "")
    partition = "aws-cn" if region.startswith("cn-") else "aws"
    record_ids = [str((t.config or {}).get("record_id") or "") for t in refs]
    records = {row.id: row for row in db.query(RegistryRecord)
               .filter(RegistryRecord.workspace_id == workspace.id,
                       RegistryRecord.id.in_(record_ids)).all()}
    owners = {}
    for row in (db.query(Agent)
                .filter(Agent.workspace_id == workspace.id,
                        Agent.registry_record_id.in_(record_ids), Agent.status != "deleted")
                .order_by(Agent.updated_at.desc()).all()):
        owners.setdefault(row.registry_record_id or "", row)
    how = "pick the peer from GET /api/registry/attachables (agents[] with attachable: true)"
    for tool in refs:
        record_id = str((tool.config or {}).get("record_id") or "")
        arn = str((tool.config or {}).get("runtime_arn") or "")
        field = "tools.a2a"
        record = records.get(record_id)
        if record is None or str(record.status).upper() != "APPROVED":
            raise _reference_missing(field, record_id, A2A_PEER_KIND,
                                     f"a2a peer {tool.name!r}: record {record_id!r} is not an "
                                     "APPROVED A2A record of this workspace", how)
        agent = owners.get(record_id)
        if agent is None or agent.status != "active" or not agent.arn \
                or (agent.spec or {}).get("protocol") != "a2a":
            raise _reference_missing(field, record_id, A2A_PEER_KIND,
                                     f"a2a peer {tool.name!r}: no active A2A server agent of "
                                     "this workspace owns record {record_id!r}".replace(
                                         "{record_id!r}", repr(record_id)), how)
        arn_partition, arn_account = _arn_account(arn)
        if arn != agent.arn or arn_account != account or arn_partition != partition:
            raise _reference_missing(field, arn, A2A_PEER_KIND,
                                     f"a2a peer {tool.name!r}: config.runtime_arn is not the "
                                     f"peer's Runtime ARN in this workspace ({agent.arn})", how)
