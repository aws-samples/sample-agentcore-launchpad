"""Read-only Runtime discovery and explicit externally-owned ledger imports."""

from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.ledger import Agent
from app.services.agentcore import runtime as runtime_api

DISCOVERED_METHOD = "discovered_runtime"
AGENT_PROTOCOLS = {"http", "a2a"}
FAILURE_STATUSES = {"CREATE_FAILED", "UPDATE_FAILED", "DELETING", "DELETE_FAILED"}


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _aws_error(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code") or "ClientError")
    if isinstance(exc, BotoCoreError):
        return type(exc).__name__
    return type(exc).__name__


def _protocol(detail: dict[str, Any]) -> str:
    value = detail.get("protocolConfiguration", {}).get("serverProtocol", "HTTP")
    return str(value).lower()


def _artifact_type(detail: dict[str, Any]) -> str:
    artifact = detail.get("agentRuntimeArtifact") or {}
    if "containerConfiguration" in artifact:
        return "container"
    if "codeConfiguration" in artifact:
        return "code"
    return "unknown"


def _authorizer_type(detail: dict[str, Any]) -> str:
    authorizer = detail.get("authorizerConfiguration")
    if authorizer is None or authorizer == {}:
        return "none"
    if isinstance(authorizer, dict) and "customJWTAuthorizer" in authorizer:
        return "custom_jwt"
    return "unknown"


def _import_capability(protocol: str) -> dict[str, Any]:
    if protocol == "mcp":
        return {
            "eligible": False,
            "reason_code": "not-agent-protocol",
            "reason": "MCP runtimes are tool servers, not agents.",
        }
    if protocol not in AGENT_PROTOCOLS:
        return {
            "eligible": False,
            "reason_code": "unsupported-protocol",
            "reason": f"Runtime protocol '{protocol}' is not supported.",
        }
    return {"eligible": True, "reason_code": None, "reason": None}


def _runtime_invoke_capability(
    protocol: str, aws_status: str, authorizer_type: str
) -> dict[str, Any]:
    if protocol == "mcp":
        return {
            "eligible": False,
            "reason_code": "not-agent-protocol",
            "reason": "MCP runtimes are not invokable as agents.",
        }
    if protocol not in AGENT_PROTOCOLS:
        return {
            "eligible": False,
            "reason_code": "unsupported-protocol",
            "reason": f"Runtime protocol '{protocol}' is not supported.",
        }
    if authorizer_type != "none":
        return {
            "eligible": False,
            "reason_code": "external-authorizer",
            "reason": "This Runtime uses an external custom JWT authorizer.",
        }
    if aws_status != "READY":
        return {
            "eligible": False,
            "reason_code": "runtime-not-ready",
            "reason": f"Runtime status is {aws_status or 'UNKNOWN'}, not READY.",
        }
    return {"eligible": True, "reason_code": None, "reason": None}


def invoke_capability(agent: Agent) -> dict[str, Any]:
    """Project the one invoke contract consumed by API and frontend surfaces."""
    if agent.status != "active" or not agent.arn:
        return {
            "eligible": False,
            "reason_code": "not-active",
            "reason": "The agent is not active.",
        }
    if agent.method != DISCOVERED_METHOD:
        return {"eligible": True, "reason_code": None, "reason": None}

    spec = agent.spec or {}
    discovery = spec.get("discovery") or {}
    return _runtime_invoke_capability(
        str(spec.get("protocol") or "unknown").lower(),
        str(discovery.get("aws_status") or "UNKNOWN").upper(),
        str(discovery.get("authorizer_type") or "unknown").lower(),
    )


def require_invoke_capability(agent: Agent) -> None:
    capability = invoke_capability(agent)
    if capability["eligible"]:
        return
    if agent.method != DISCOVERED_METHOD and capability["reason_code"] == "not-active":
        raise AppError("agent.not_active", "agent is not active", status_code=409)
    raise AppError(
        "agent.invoke_not_supported",
        capability["reason"],
        {"reason_code": capability["reason_code"]},
        status_code=409,
    )


def _managed_match(db: Session, runtime_arn: str | None, runtime_id: str) -> Agent | None:
    rows = db.query(Agent).filter(Agent.status != "deleted").all()
    if runtime_arn:
        match = next((row for row in rows if row.arn == runtime_arn), None)
        if match is not None:
            return match
    return next((row for row in rows if row.resource_id == runtime_id), None)


def _candidate(
    detail: dict[str, Any],
    db: Session,
    *,
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary = summary or {}
    runtime_id = str(detail.get("agentRuntimeId") or summary.get("agentRuntimeId") or "")
    runtime_arn = str(detail.get("agentRuntimeArn") or summary.get("agentRuntimeArn") or "")
    protocol = _protocol(detail)
    aws_status = str(detail.get("status") or summary.get("status") or "UNKNOWN").upper()
    authorizer_type = _authorizer_type(detail)
    import_capability = _import_capability(protocol)
    managed = _managed_match(db, runtime_arn or None, runtime_id)
    return {
        "runtime_id": runtime_id,
        "runtime_arn": runtime_arn,
        "name": str(
            detail.get("agentRuntimeName") or summary.get("agentRuntimeName") or runtime_id
        ),
        "description": str(detail.get("description") or summary.get("description") or ""),
        "version": str(
            detail.get("agentRuntimeVersion") or summary.get("agentRuntimeVersion") or ""
        ),
        "aws_status": aws_status,
        "protocol": protocol.upper(),
        "artifact_type": _artifact_type(detail),
        "authorizer_type": authorizer_type,
        "last_updated_at": _iso(
            detail.get("lastUpdatedAt") or summary.get("lastUpdatedAt")
        ),
        "managed_agent_id": managed.id if managed else None,
        "managed_agent_name": managed.name if managed else None,
        "managed_agent_method": managed.method if managed else None,
        "importable": import_capability["eligible"],
        "reason_code": import_capability["reason_code"],
        "reason": import_capability["reason"],
        "invoke_capability": _runtime_invoke_capability(
            protocol, aws_status, authorizer_type
        ),
    }


def _inspection_failure(
    summary: dict[str, Any], db: Session, exc: Exception
) -> dict[str, Any]:
    runtime_id = str(summary.get("agentRuntimeId") or "")
    runtime_arn = str(summary.get("agentRuntimeArn") or "")
    managed = _managed_match(db, runtime_arn or None, runtime_id)
    reason = f"Runtime detail inspection failed ({_aws_error(exc)})."
    return {
        "runtime_id": runtime_id,
        "runtime_arn": runtime_arn,
        "name": str(summary.get("agentRuntimeName") or runtime_id),
        "description": str(summary.get("description") or ""),
        "version": str(summary.get("agentRuntimeVersion") or ""),
        "aws_status": str(summary.get("status") or "UNKNOWN").upper(),
        "protocol": "UNKNOWN",
        "artifact_type": "unknown",
        "authorizer_type": "unknown",
        "last_updated_at": _iso(summary.get("lastUpdatedAt")),
        "managed_agent_id": managed.id if managed else None,
        "managed_agent_name": managed.name if managed else None,
        "managed_agent_method": managed.method if managed else None,
        "importable": False,
        "reason_code": "inspection-failed",
        "reason": reason,
        "invoke_capability": {
            "eligible": False,
            "reason_code": "inspection-failed",
            "reason": reason,
        },
    }


def scan_runtimes(control: Any, db: Session) -> list[dict[str, Any]]:
    try:
        summaries = runtime_api.list_runtimes(control)
    except Exception as exc:
        raise AppError(
            "runtime.discovery_failed",
            f"AgentCore Runtime discovery failed ({_aws_error(exc)}).",
            status_code=502,
        ) from exc

    candidates: list[dict[str, Any]] = []
    for summary in summaries:
        try:
            detail = runtime_api.get_runtime(control, str(summary["agentRuntimeId"]))
            candidates.append(_candidate(detail, db, summary=summary))
        except Exception as exc:
            candidates.append(_inspection_failure(summary, db, exc))
    return candidates


def _ledger_status(aws_status: str) -> str:
    if aws_status == "READY":
        return "active"
    if aws_status in FAILURE_STATUSES or aws_status.endswith("_FAILED"):
        return "failed"
    return "deploying"


def _display_name(
    db: Session, runtime_name: str, runtime_id: str, existing: Agent | None
) -> str:
    conflict = (
        db.query(Agent)
        .filter(Agent.name == runtime_name, Agent.status != "deleted")
        .first()
    )
    if conflict is None or (existing is not None and conflict.id == existing.id):
        return runtime_name[:64]
    suffix = f"-{runtime_id[-10:]}"
    return f"{runtime_name[: 64 - len(suffix)]}{suffix}"


def _discovery_spec(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol": candidate["protocol"].lower(),
        "discovery": {
            "runtime_name": candidate["name"],
            "description": candidate["description"],
            "artifact_type": candidate["artifact_type"],
            "authorizer_type": candidate["authorizer_type"],
            "aws_status": candidate["aws_status"],
            "last_updated_at": candidate["last_updated_at"],
        },
    }


def import_runtimes(
    control: Any, db: Session, runtime_ids: list[str]
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {
        "imported": [],
        "updated": [],
        "already_managed": [],
        "failed": [],
    }
    for runtime_id in runtime_ids:
        try:
            detail = runtime_api.get_runtime(control, runtime_id)
            candidate = _candidate(detail, db)
        except Exception as exc:
            result["failed"].append(
                {
                    "runtime_id": runtime_id,
                    "reason_code": "inspection-failed",
                    "reason": f"Runtime detail inspection failed ({_aws_error(exc)}).",
                }
            )
            continue

        if not candidate["importable"]:
            result["failed"].append(
                {
                    "runtime_id": runtime_id,
                    "reason_code": candidate["reason_code"],
                    "reason": candidate["reason"],
                }
            )
            continue

        existing = _managed_match(db, candidate["runtime_arn"], runtime_id)
        if existing is not None and existing.method != DISCOVERED_METHOD:
            result["already_managed"].append(
                {
                    "runtime_id": runtime_id,
                    "agent_id": existing.id,
                    "agent_name": existing.name,
                }
            )
            continue

        now = datetime.now(UTC)
        created = existing is None
        display_name = _display_name(db, candidate["name"], runtime_id, existing)
        if existing is None:
            existing = Agent(method=DISCOVERED_METHOD, owner="aws-discovery")
            db.add(existing)
        existing.name = display_name
        existing.status = _ledger_status(candidate["aws_status"])
        existing.resource_id = runtime_id
        existing.arn = candidate["runtime_arn"]
        existing.version = candidate["version"] or None
        existing.spec = _discovery_spec(candidate)
        existing.error = None
        existing.updated_at = now
        db.flush()
        result["imported" if created else "updated"].append(
            {
                "runtime_id": runtime_id,
                "agent_id": existing.id,
                "agent_name": existing.name,
            }
        )
    return result
