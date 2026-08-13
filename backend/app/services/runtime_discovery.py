"""Read-only Runtime/Harness discovery and explicit externally-owned ledger imports.

Two resource kinds are discovered and imported through the same ledger shape: an
`Agent` row with ``method = DISCOVERED_METHOD`` whose ``spec.discovery.
resource_type`` says which AWS resource it points at (absent ⇒ ``"runtime"``).
An imported harness stores the HARNESS arn/id, so invoke goes through
InvokeHarness and the harness's backing runtime resolves its owner by the same
join a launchpad-created harness agent uses.
"""

import re
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.ledger import Agent
from app.services.agentcore import harness as harness_api
from app.services.agentcore import runtime as runtime_api

DISCOVERED_METHOD = "discovered_runtime"
HARNESS_RESOURCE_TYPE = "harness"
AGENT_PROTOCOLS = {"http", "a2a"}
FAILURE_STATUSES = {"CREATE_FAILED", "UPDATE_FAILED", "DELETING", "DELETE_FAILED"}

# The managed Harness service materializes each harness as a backing Runtime it
# owns: named ``harness_<harnessName>`` and running the service's public image
# ``public.ecr.aws/<alias>/harness-<region>``. Such runtimes reject
# InvokeAgentRuntime (only InvokeHarness works), so they must never be imported
# or invoked as plain runtimes.
_HARNESS_RUNTIME_PREFIX = "harness_"
_HARNESS_IMAGE = re.compile(r"^public\.ecr\.aws/[^/]+/harness-[a-z0-9-]+([:@]|$)")
_HARNESS_MANAGED_CAPABILITY = {
    "eligible": False,
    "reason_code": "harness-managed",
    "reason": (
        "This runtime is the backing runtime of a managed Harness; it cannot be "
        "imported or invoked directly."
    ),
}


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


def _harness_index(control: Any) -> dict[str, dict[str, Any]]:
    """Backing-runtime name → owning harness summary (fail-soft to {}).

    On ListHarnesses failure the image heuristic in ``_backing_harness`` still
    flags harness-managed runtimes, just without the owner linkage.
    """
    try:
        summaries = harness_api.list_harnesses(control)
    except Exception:
        return {}
    return {
        f"{_HARNESS_RUNTIME_PREFIX}{summary['harnessName']}": summary
        for summary in summaries
        if summary.get("harnessName")
    }


def _backing_harness(
    detail: dict[str, Any],
    summary: dict[str, Any],
    harness_index: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """The harness owning this runtime; {} when harness-managed but unmatched."""
    name = str(detail.get("agentRuntimeName") or summary.get("agentRuntimeName") or "")
    owner = harness_index.get(name)
    if owner is not None:
        return owner
    artifact = detail.get("agentRuntimeArtifact") or {}
    image = str((artifact.get("containerConfiguration") or {}).get("containerUri") or "")
    if _HARNESS_IMAGE.match(image):
        return {}
    return None


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
    protocol: str, aws_status: str, authorizer_type: str, artifact_type: str = "unknown"
) -> dict[str, Any]:
    if artifact_type == "harness":
        return dict(_HARNESS_MANAGED_CAPABILITY)
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


def _harness_status_capability(aws_status: str) -> dict[str, Any]:
    if aws_status in FAILURE_STATUSES or aws_status.endswith("_FAILED"):
        return {
            "eligible": False,
            "reason_code": "harness-not-ready",
            "reason": f"Harness status is {aws_status or 'UNKNOWN'}, not READY.",
        }
    return {"eligible": True, "reason_code": None, "reason": None}


def _harness_invoke_capability(aws_status: str, authorizer_type: str) -> dict[str, Any]:
    """An imported harness is invokable only through InvokeHarness with SigV4.

    Deliberately NOT ``_runtime_invoke_capability``: its ``harness`` artifact arm
    means "backing runtime of a harness", which is the opposite verdict.
    """
    if authorizer_type != "none":
        return {
            "eligible": False,
            "reason_code": "external-authorizer",
            "reason": "This Harness uses an external custom JWT authorizer.",
        }
    if aws_status != "READY":
        return {
            "eligible": False,
            "reason_code": "harness-not-ready",
            "reason": f"Harness status is {aws_status or 'UNKNOWN'}, not READY.",
        }
    return {"eligible": True, "reason_code": None, "reason": None}


def is_discovered_harness(agent: Agent) -> bool:
    """True for an imported (externally owned) managed Harness."""
    if agent.method != DISCOVERED_METHOD:
        return False
    discovery = (agent.spec or {}).get("discovery") or {}
    return discovery.get("resource_type") == HARNESS_RESOURCE_TYPE


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
    if discovery.get("resource_type") == HARNESS_RESOURCE_TYPE:
        return _harness_invoke_capability(
            str(discovery.get("aws_status") or "UNKNOWN").upper(),
            str(discovery.get("authorizer_type") or "none").lower(),
        )
    return _runtime_invoke_capability(
        str(spec.get("protocol") or "unknown").lower(),
        str(discovery.get("aws_status") or "UNKNOWN").upper(),
        str(discovery.get("authorizer_type") or "unknown").lower(),
        str(discovery.get("artifact_type") or "unknown").lower(),
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


def _managed_match(
    db: Session,
    workspace_id: str,
    runtime_arn: str | None,
    runtime_id: str,
    harness: dict[str, Any] | None = None,
) -> Agent | None:
    """Ledger row owning this runtime — directly, or via its owning harness.

    Prefers a launchpad-managed row over a stale discovered import of the same
    resource, so a harness backing runtime surfaces its real harness agent.

    Scoped to the workspace the scan ran against: the same resource name/id can
    exist in two environments, and matching ledger-wide would report an agent
    from a region this scan never looked at.
    """
    arns = {value for value in (runtime_arn, (harness or {}).get("arn")) if value}
    ids = {value for value in (runtime_id, (harness or {}).get("harnessId")) if value}
    rows = (
        db.query(Agent)
        .filter(Agent.workspace_id == workspace_id, Agent.status != "deleted")
        .all()
    )
    matches = [row for row in rows if row.arn in arns] or [
        row for row in rows if row.resource_id in ids
    ]
    if not matches:
        return None
    return min(matches, key=lambda row: row.method == DISCOVERED_METHOD)


def _candidate(
    detail: dict[str, Any],
    db: Session,
    *,
    workspace_id: str,
    summary: dict[str, Any] | None = None,
    harness_index: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    summary = summary or {}
    runtime_id = str(detail.get("agentRuntimeId") or summary.get("agentRuntimeId") or "")
    runtime_arn = str(detail.get("agentRuntimeArn") or summary.get("agentRuntimeArn") or "")
    protocol = _protocol(detail)
    aws_status = str(detail.get("status") or summary.get("status") or "UNKNOWN").upper()
    authorizer_type = _authorizer_type(detail)
    harness = _backing_harness(detail, summary, harness_index or {})
    artifact_type = "harness" if harness is not None else _artifact_type(detail)
    import_capability = (
        dict(_HARNESS_MANAGED_CAPABILITY)
        if harness is not None
        else _import_capability(protocol)
    )
    managed = _managed_match(db, workspace_id, runtime_arn or None, runtime_id, harness=harness)
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
        "artifact_type": artifact_type,
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
            protocol, aws_status, authorizer_type, artifact_type
        ),
    }


def _inspection_failure(
    summary: dict[str, Any], db: Session, exc: Exception, *, workspace_id: str
) -> dict[str, Any]:
    runtime_id = str(summary.get("agentRuntimeId") or "")
    runtime_arn = str(summary.get("agentRuntimeArn") or "")
    managed = _managed_match(db, workspace_id, runtime_arn or None, runtime_id)
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


def scan_runtimes(control: Any, db: Session, *, workspace_id: str) -> list[dict[str, Any]]:
    try:
        summaries = runtime_api.list_runtimes(control)
    except Exception as exc:
        raise AppError(
            "runtime.discovery_failed",
            f"AgentCore Runtime discovery failed ({_aws_error(exc)}).",
            status_code=502,
        ) from exc

    harness_index = _harness_index(control)
    candidates: list[dict[str, Any]] = []
    for summary in summaries:
        try:
            detail = runtime_api.get_runtime(control, str(summary["agentRuntimeId"]))
            candidates.append(
                _candidate(
                    detail,
                    db,
                    workspace_id=workspace_id,
                    summary=summary,
                    harness_index=harness_index,
                )
            )
        except Exception as exc:
            candidates.append(
                _inspection_failure(summary, db, exc, workspace_id=workspace_id)
            )
    return candidates


def _harness_candidate(
    record: dict[str, Any], db: Session, *, workspace_id: str
) -> dict[str, Any]:
    """Projection of one ListHarnesses summary / GetHarness detail.

    Harness summaries carry no authorizer or artifact detail, so the scan makes
    no invoke claim — import eligibility is the status verdict only, and the
    invoke verdict is projected from the stored spec after import.
    """
    harness_id = str(record.get("harnessId") or "")
    harness_arn = str(record.get("arn") or "")
    aws_status = str(record.get("status") or "UNKNOWN").upper()
    managed = _managed_match(db, workspace_id, harness_arn or None, harness_id)
    capability = _harness_status_capability(aws_status)
    return {
        "harness_id": harness_id,
        "harness_arn": harness_arn,
        "name": str(record.get("harnessName") or harness_id),
        "description": str(record.get("description") or ""),
        "version": str(record.get("harnessVersion") or ""),
        "aws_status": aws_status,
        "last_updated_at": _iso(record.get("updatedAt") or record.get("createdAt")),
        "managed_agent_id": managed.id if managed else None,
        "managed_agent_name": managed.name if managed else None,
        "managed_agent_method": managed.method if managed else None,
        "importable": capability["eligible"],
        "reason_code": capability["reason_code"],
        "reason": capability["reason"],
    }


def scan_harnesses(
    control: Any, db: Session, *, workspace_id: str
) -> tuple[list[dict[str, Any]], str | None]:
    """Harness candidates plus a fail-soft error string.

    Unlike the Runtime scan a ListHarnesses failure is not fatal: the Runtime
    half of the discovery response still renders, with the error reported next
    to it (same posture as ``_harness_index``).
    """
    try:
        summaries = harness_api.list_harnesses(control)
    except Exception as exc:
        return [], f"AgentCore Harness discovery failed ({_aws_error(exc)})."
    return (
        [
            _harness_candidate(summary, db, workspace_id=workspace_id)
            for summary in summaries
        ],
        None,
    )


def _ledger_status(aws_status: str) -> str:
    if aws_status == "READY":
        return "active"
    if aws_status in FAILURE_STATUSES or aws_status.endswith("_FAILED"):
        return "failed"
    return "deploying"


def _display_name(
    db: Session,
    resource_name: str,
    resource_id: str,
    existing: Agent | None,
    *,
    workspace_id: str,
) -> str:
    conflict = (
        db.query(Agent)
        .filter(
            Agent.workspace_id == workspace_id,
            Agent.name == resource_name,
            Agent.status != "deleted",
        )
        .first()
    )
    if conflict is None or (existing is not None and conflict.id == existing.id):
        return resource_name[:64]
    suffix = f"-{resource_id[-10:]}"
    return f"{resource_name[: 64 - len(suffix)]}{suffix}"


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


def _harness_discovery_spec(
    candidate: dict[str, Any], authorizer_type: str
) -> dict[str, Any]:
    return {
        "protocol": HARNESS_RESOURCE_TYPE,
        "discovery": {
            "resource_type": HARNESS_RESOURCE_TYPE,
            "harness_name": candidate["name"],
            "description": candidate["description"],
            "authorizer_type": authorizer_type,
            "aws_status": candidate["aws_status"],
            "last_updated_at": candidate["last_updated_at"],
        },
    }


def _empty_import_result() -> dict[str, list[dict[str, Any]]]:
    return {"imported": [], "updated": [], "already_managed": [], "failed": []}


def import_runtimes(
    control: Any, db: Session, runtime_ids: list[str], *, workspace_id: str
) -> dict[str, list[dict[str, Any]]]:
    """Import selected Runtimes as externally-owned ledger rows (idempotent).

    `workspace_id` is the environment the scan ran against: it stamps the rows
    this call creates and scopes every ownership/name-collision query, so a
    resource that another workspace already imported is invisible here rather
    than refreshed in place.
    """
    result = _empty_import_result()
    harness_index = _harness_index(control)
    for runtime_id in runtime_ids:
        try:
            detail = runtime_api.get_runtime(control, runtime_id)
            candidate = _candidate(
                detail, db, workspace_id=workspace_id, harness_index=harness_index
            )
        except Exception as exc:
            result["failed"].append(
                {
                    "runtime_id": runtime_id,
                    "reason_code": "inspection-failed",
                    "reason": f"Runtime detail inspection failed ({_aws_error(exc)}).",
                }
            )
            continue

        # Ownership first: a launchpad-managed resource (including the harness
        # owning a backing runtime) must never be duplicated as a new import.
        if (
            candidate["managed_agent_id"] is not None
            and candidate["managed_agent_method"] != DISCOVERED_METHOD
        ):
            result["already_managed"].append(
                {
                    "runtime_id": runtime_id,
                    "agent_id": candidate["managed_agent_id"],
                    "agent_name": candidate["managed_agent_name"],
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

        existing = (
            db.get(Agent, candidate["managed_agent_id"])
            if candidate["managed_agent_id"] is not None
            else None
        )

        now = datetime.now(UTC)
        created = existing is None
        display_name = _display_name(
            db, candidate["name"], runtime_id, existing, workspace_id=workspace_id
        )
        if existing is None:
            existing = Agent(
                workspace_id=workspace_id, method=DISCOVERED_METHOD, owner="aws-discovery"
            )
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


def import_harnesses(
    control: Any, db: Session, harness_ids: list[str], *, workspace_id: str
) -> dict[str, list[dict[str, Any]]]:
    """Import managed Harnesses as externally-owned ledger rows (idempotent).

    The row carries the harness ARN/id, never the backing runtime's, so delete
    stays ledger-only and invoke dispatches to InvokeHarness. `workspace_id`
    stamps the rows this call creates and scopes the ownership/name queries.
    """
    result = _empty_import_result()
    for harness_id in harness_ids:
        try:
            detail = harness_api.get_harness(control, harness_id)
        except Exception as exc:
            result["failed"].append(
                {
                    "harness_id": harness_id,
                    "reason_code": "inspection-failed",
                    "reason": f"Harness detail inspection failed ({_aws_error(exc)}).",
                }
            )
            continue
        candidate = _harness_candidate(detail, db, workspace_id=workspace_id)

        # Ownership first: a harness deployed by launchpad (method="harness")
        # must never be duplicated as an externally-owned import.
        if (
            candidate["managed_agent_id"] is not None
            and candidate["managed_agent_method"] != DISCOVERED_METHOD
        ):
            result["already_managed"].append(
                {
                    "harness_id": harness_id,
                    "agent_id": candidate["managed_agent_id"],
                    "agent_name": candidate["managed_agent_name"],
                }
            )
            continue

        existing = (
            db.get(Agent, candidate["managed_agent_id"])
            if candidate["managed_agent_id"] is not None
            else None
        )
        # Status gates the FIRST import only. Refreshing an existing import is how
        # the ledger learns the external harness broke — refusing would leave a
        # stale "active" row whose invoke attempts keep reaching a dead harness.
        if existing is None and not candidate["importable"]:
            result["failed"].append(
                {
                    "harness_id": harness_id,
                    "reason_code": candidate["reason_code"],
                    "reason": candidate["reason"],
                }
            )
            continue

        created = existing is None
        display_name = _display_name(
            db, candidate["name"], harness_id, existing, workspace_id=workspace_id
        )
        if existing is None:
            existing = Agent(
                workspace_id=workspace_id, method=DISCOVERED_METHOD, owner="aws-discovery"
            )
            db.add(existing)
        existing.name = display_name
        existing.status = _ledger_status(candidate["aws_status"])
        existing.resource_id = harness_id
        existing.arn = candidate["harness_arn"]
        existing.version = candidate["version"] or None
        existing.spec = _harness_discovery_spec(candidate, _authorizer_type(detail))
        existing.error = None
        existing.updated_at = datetime.now(UTC)
        db.flush()
        result["imported" if created else "updated"].append(
            {
                "harness_id": harness_id,
                "agent_id": existing.id,
                "agent_name": existing.name,
            }
        )
    return result
