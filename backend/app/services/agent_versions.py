"""Read-only AWS versions + endpoints view for one agent (VERSIONS & ENDPOINTS panel).

Every UpdateAgentRuntime / UpdateHarness publishes an immutable new version;
``DEFAULT`` auto-follows the latest while named endpoints (the canary's
``stable``/``treatment``) pin one. The ledger only remembers the version a deploy
minted (``Agent.version``), so this module reads the authoritative set back from
AWS and projects an **allow-listed** shape — the same rule as
``runtime_discovery``: no environment values, artifact locations, execution
roles or authorizer configuration leave the backend.

The agent's resource kind decides which pair of list operations runs:

* ``zip_runtime`` / ``studio`` / ``container`` and imported rows whose
  ``spec.discovery.resource_type`` is absent or ``runtime`` → Runtime lists;
* ``harness`` and imported rows with ``resource_type == "harness"`` → Harness lists;
* anything else (no resource yet, deleted, unknown method) → ``agent.no_resource``.
"""

from datetime import datetime
from typing import Any, Literal

from app.core.errors import AppError
from app.models.ledger import Agent
from app.services.agentcore import harness as harness_api
from app.services.agentcore import runtime as runtime_api
from app.services.runtime_discovery import DISCOVERED_METHOD, HARNESS_RESOURCE_TYPE

ResourceKind = Literal["runtime", "harness"]

RUNTIME_METHODS = {"zip_runtime", "studio", "container"}
DEFAULT_ENDPOINT = "DEFAULT"
CANARY_ENDPOINTS = ("stable", "treatment")


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _text(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def resource_kind(agent: Agent) -> ResourceKind | None:
    """Which AWS resource family backs this ledger row, or None when neither does."""
    if agent.method == "harness":
        return "harness"
    if agent.method in RUNTIME_METHODS:
        return "runtime"
    if agent.method == DISCOVERED_METHOD:
        discovery = (agent.spec or {}).get("discovery") or {}
        resource_type = discovery.get("resource_type") or "runtime"
        if resource_type == HARNESS_RESOURCE_TYPE:
            return "harness"
        if resource_type == "runtime":
            return "runtime"
    return None


def require_resource(agent: Agent) -> tuple[ResourceKind, str]:
    """(kind, resource id) or ``agent.no_resource`` (409) with a human reason."""
    if agent.status == "deleted":
        raise AppError(
            "agent.no_resource",
            "The agent has been deleted; its AWS resource is gone.",
            status_code=409,
        )
    kind = resource_kind(agent)
    if kind is None:
        raise AppError(
            "agent.no_resource",
            "Versions are only tracked for Runtime- and Harness-backed agents; "
            f"this agent's method ({agent.method}) resolves to neither.",
            {"method": agent.method},
            status_code=409,
        )
    if not agent.resource_id:
        raise AppError(
            "agent.no_resource",
            f"The agent has no AWS resource yet (status: {agent.status}) — "
            "versions appear once the deploy stage has created it.",
            {"status": agent.status},
            status_code=409,
        )
    return kind, agent.resource_id


def _version_key(version: str | None) -> tuple[int, int | str]:
    """Numeric versions sort numerically; anything else after them, lexically."""
    if version is not None and version.isdigit():
        return (0, int(version))
    return (1, version or "")


def _endpoint_key(endpoint: dict[str, Any]) -> tuple[int, str]:
    name = endpoint["name"] or ""
    return (0 if name == DEFAULT_ENDPOINT else 1, name)


def _project_runtime_version(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": _text(raw.get("agentRuntimeVersion")),
        "status": _text(raw.get("status")),
        "description": _text(raw.get("description")),
        "last_updated_at": _iso(raw.get("lastUpdatedAt")),
    }


def _project_runtime_endpoint(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": _text(raw.get("name")),
        "live_version": _text(raw.get("liveVersion")),
        "target_version": _text(raw.get("targetVersion")),
        "status": _text(raw.get("status")),
        "description": _text(raw.get("description")),
        "created_at": _iso(raw.get("createdAt")),
        "last_updated_at": _iso(raw.get("lastUpdatedAt")),
        "failure_reason": _text(raw.get("failureReason")),
    }


def _project_harness_version(raw: dict[str, Any]) -> dict[str, Any]:
    # HarnessVersionSummary: no description, ``updatedAt`` rather than lastUpdatedAt.
    return {
        "version": _text(raw.get("harnessVersion")),
        "status": _text(raw.get("status")),
        "description": None,
        "last_updated_at": _iso(raw.get("updatedAt") or raw.get("createdAt")),
    }


def _project_harness_endpoint(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": _text(raw.get("endpointName")),
        "live_version": _text(raw.get("liveVersion")),
        "target_version": _text(raw.get("targetVersion")),
        "status": _text(raw.get("status")),
        "description": _text(raw.get("description")),
        "created_at": _iso(raw.get("createdAt")),
        "last_updated_at": _iso(raw.get("updatedAt")),
        "failure_reason": _text(raw.get("failureReason")),
    }


def list_agent_versions(control: Any, agent: Agent) -> dict[str, Any]:
    """The full AWS version + endpoint set for one agent, allow-list projected.

    Follows every page of both list operations. ``latest_version`` is the highest
    version AWS reports; ``ledger_version`` is what the last deploy recorded —
    they legitimately differ after an out-of-band update or a canary candidate
    mint, so the UI shows the mismatch rather than treating it as an error.
    """
    kind, resource_id = require_resource(agent)
    if kind == "harness":
        versions = [
            _project_harness_version(v)
            for v in harness_api.list_harness_versions(control, resource_id)
        ]
        endpoints = [
            _project_harness_endpoint(e)
            for e in harness_api.list_harness_endpoints(control, resource_id)
        ]
    else:
        versions = [
            _project_runtime_version(v)
            for v in runtime_api.list_runtime_versions(control, resource_id)
        ]
        endpoints = [
            _project_runtime_endpoint(e)
            for e in runtime_api.list_runtime_endpoints(control, resource_id)
        ]
    versions.sort(key=lambda v: _version_key(v["version"]), reverse=True)
    endpoints.sort(key=_endpoint_key)
    latest = versions[0]["version"] if versions else None
    return {
        "kind": kind,
        "resource_id": resource_id,
        "versions": versions,
        "endpoints": endpoints,
        "latest_version": latest,
        "ledger_version": _text(agent.version),
        "canary_endpoints": [e["name"] for e in endpoints if e["name"] in CANARY_ENDPOINTS],
    }
