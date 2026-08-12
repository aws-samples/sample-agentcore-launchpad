"""KB gateway bootstrap — the dedicated MCP gateway that fronts Managed KB
retrieval as gateway tools.

Mirrors gateway_bootstrap: ensure_* functions are create-if-missing by name
(idempotent). One shared gateway ``launchpad-kb-gw`` carries a per-KB ``Retrieve``
target (globally visible) plus a per-agent ``AgenticRetrieveStream`` target whose
retrievers are bound to that agent's selected KBs.
"""

import re
import time
from typing import Any

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID
from app.services.bootstrap import write_config
from app.services.gateway_bootstrap import (
    _list_targets,
    _wait_gateway_ready,
    _wait_target_ready,
    ensure_gateway_allows_client,
)
from app.services.workspace import WorkspaceContext, merge_workspace_resources

KB_GATEWAY_NAME = "launchpad-kb-gw"
_CONNECTOR_ID = "bedrock-knowledge-bases"


def _sanitize(text: str, max_len: int) -> str:
    """lowercase → [a-z0-9-] → collapse dashes → trim → truncate."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:max_len].rstrip("-")


def retrieve_target_name(kb_id: str, kb_name: str) -> str:
    """Readable + stable per-KB target name, e.g. ``product-docs-k5yakymmu6``."""
    slug = _sanitize(kb_name, 30) or "kb"
    return f"{slug}-{kb_id.lower()}"


def agentic_target_name(agent_name: str) -> str:
    """Per-agent agentic-retrieval target name, e.g. ``agentic-support-bot``."""
    return "agentic-" + (_sanitize(agent_name, 60) or "agent")


def ensure_kb_gateway(control: Any, resources: dict[str, Any], region: str) -> dict[str, str]:
    """Create (or reuse) the KB gateway with Cognito-JWT inbound auth. Existing
    gateways get the console + M2M clients appended to allowedClients."""
    client_id = resources["user_pool_client_id"]
    m2m_client_id = resources.get("m2m_client_id", "")
    allowed = [client_id] + ([m2m_client_id] if m2m_client_id else [])

    items: list[dict[str, Any]] = []
    token = None
    while True:
        kwargs = {"maxResults": 100} | ({"nextToken": token} if token else {})
        page = control.list_gateways(**kwargs)
        items.extend(page.get("items", []))
        token = page.get("nextToken")
        if not token:
            break
    for gw in items:
        if gw.get("name") == KB_GATEWAY_NAME:
            gateway_id = gw["gatewayId"]
            for cid in allowed:
                ensure_gateway_allows_client(control, gateway_id, cid)
            detail = control.get_gateway(gatewayIdentifier=gateway_id)
            return {
                "id": detail["gatewayId"],
                "arn": detail["gatewayArn"],
                "url": detail["gatewayUrl"],
            }

    discovery = (
        f"https://cognito-idp.{region}.amazonaws.com/{resources['user_pool_id']}"
        "/.well-known/openid-configuration"
    )
    created = control.create_gateway(
        name=KB_GATEWAY_NAME,
        description="AgentCore Launchpad managed-KB retrieval gateway",
        roleArn=resources["gateway_role_arn"],
        protocolType="MCP",
        authorizerType="CUSTOM_JWT",
        authorizerConfiguration={
            "customJWTAuthorizer": {
                "discoveryUrl": discovery,
                "allowedClients": allowed,
            }
        },
        exceptionLevel="DEBUG",
    )
    gateway_id = created["gatewayId"]
    _wait_gateway_ready(control, gateway_id)
    detail = control.get_gateway(gatewayIdentifier=gateway_id)
    return {"id": gateway_id, "arn": detail["gatewayArn"], "url": detail["gatewayUrl"]}


def _find_target_by_name(control: Any, gateway_id: str, name: str) -> dict[str, Any] | None:
    return _list_targets(control, gateway_id).get(name)


def _find_retrieve_target(control: Any, gateway_id: str, kb_id: str) -> dict[str, Any] | None:
    suffix = f"-{kb_id.lower()}"
    for tname, target in _list_targets(control, gateway_id).items():
        if tname.endswith(suffix):
            return target
    return None


def ensure_retrieve_target(
    control: Any,
    gateway_id: str,
    kb_id: str,
    kb_name: str,
    kb_description: str,
) -> str:
    """Create-if-missing the per-KB ``Retrieve`` connector target; wait READY."""
    existing = _find_retrieve_target(control, gateway_id, kb_id)
    if existing:
        return existing["targetId"]
    try:
        created = control.create_gateway_target(
            gatewayIdentifier=gateway_id,
            name=retrieve_target_name(kb_id, kb_name),
            description=f"Managed KB {kb_name} · Retrieve",
            targetConfiguration={
                "mcp": {
                    "connector": {
                        "source": {"connectorId": _CONNECTOR_ID},
                        "configurations": [
                            {
                                "name": "Retrieve",
                                "description": (kb_description or kb_name)[:200],
                                "parameterValues": {"knowledgeBaseId": kb_id},
                            }
                        ],
                    }
                }
            },
            credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
        )
    except control.exceptions.ConflictException:
        # a concurrent publish of the same KB won the create race — adopt its
        # target rather than failing this publish
        existing = _find_retrieve_target(control, gateway_id, kb_id)
        if not existing:
            raise
        _wait_target_ready(control, gateway_id, existing["targetId"])
        return existing["targetId"]
    target_id = created["targetId"]
    _wait_target_ready(control, gateway_id, target_id)
    return target_id


def _agentic_target_configuration(retrievers: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "mcp": {
            "connector": {
                "source": {"connectorId": _CONNECTOR_ID},
                "configurations": [
                    {
                        "name": "AgenticRetrieveStream",
                        "parameterValues": {
                            "retrievers": [
                                {
                                    "description": (r["description"] or r["kb_id"])[:200],
                                    "configuration": {
                                        "knowledgeBase": {"knowledgeBaseId": r["kb_id"]}
                                    },
                                }
                                for r in retrievers
                            ],
                            "agenticRetrieveConfiguration": {
                                "foundationModelType": "MANAGED",
                                "rerankingModelType": "MANAGED",
                            },
                        },
                    }
                ],
            }
        }
    }


def sync_agentic_target(
    control: Any,
    gateway_id: str,
    agent_name: str,
    retrievers: list[dict[str, Any]],
) -> str | None:
    """Create/update/delete the per-agent ``AgenticRetrieveStream`` target so its
    retrievers match ``retrievers`` ({"kb_id","description"}). Empty retrievers
    removes the target. Returns the targetId, or None when there is no target."""
    name = agentic_target_name(agent_name)
    existing = _find_target_by_name(control, gateway_id, name)
    if existing and existing.get("status") == "DELETING":
        # a just-deleted target still lists while draining — updating it fails
        # with ValidationException, so wait for it to vanish and create fresh
        existing = _wait_target_gone(control, gateway_id, name)

    if existing and not retrievers:
        _delete_target(control, gateway_id, existing["targetId"])
        return None
    if not existing and not retrievers:
        return None

    target_config = _agentic_target_configuration(retrievers)
    creds = [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]
    description = f"Managed KB agentic retrieval for agent {agent_name}"

    if existing:
        target_id = existing["targetId"]
        control.update_gateway_target(
            gatewayIdentifier=gateway_id,
            targetId=target_id,
            name=name,
            description=description,
            targetConfiguration=target_config,
            credentialProviderConfigurations=creds,
        )
    else:
        created = control.create_gateway_target(
            gatewayIdentifier=gateway_id,
            name=name,
            description=description,
            targetConfiguration=target_config,
            credentialProviderConfigurations=creds,
        )
        target_id = created["targetId"]
    _wait_target_ready(control, gateway_id, target_id)
    return target_id


def _wait_target_gone(
    control: Any, gateway_id: str, name: str, timeout_s: int = 60
) -> dict[str, Any] | None:
    """Wait for a DELETING target to drop out of the listing. Returns the target
    if it is still there (non-DELETING) after the timeout, else None."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        target = _find_target_by_name(control, gateway_id, name)
        if target is None:
            return None
        if target.get("status") != "DELETING":
            return target
        # Deliberate polling interval; the surrounding loop owns the retry bound.
        time.sleep(3)  # nosemgrep: arbitrary-sleep
    return _find_target_by_name(control, gateway_id, name)


def _delete_target(control: Any, gateway_id: str, target_id: str) -> None:
    try:
        control.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
    except Exception:  # already gone (or racing delete) — nothing to clean
        pass


def delete_retrieve_target(control: Any, gateway_id: str, kb_id: str) -> None:
    target = _find_retrieve_target(control, gateway_id, kb_id)
    if target:
        _delete_target(control, gateway_id, target["targetId"])


def delete_agentic_target(control: Any, gateway_id: str, agent_name: str) -> None:
    target = _find_target_by_name(control, gateway_id, agentic_target_name(agent_name))
    if target:
        _delete_target(control, gateway_id, target["targetId"])


def ensure_kb_gateway_persisted(
    control: Any, workspace: WorkspaceContext
) -> dict[str, str]:
    """Lazily ensure the KB gateway and persist its id/arn/url onto the workspace.

    The workspace row is the resource map of record, so the ids land there (and on
    the in-memory context, so the rest of *this* request sees them). For the
    `default` workspace the same keys are mirrored into `launchpad.yaml`, which
    `make bootstrap` and the settings-seeded default row still read — without the
    mirror the next startup's settings mirror would overwrite the row's copy.
    """
    resources = workspace.resources
    if resources.get("kb_gateway_id") and resources.get("kb_gateway_url"):
        return {
            "id": resources["kb_gateway_id"],
            "arn": resources.get("kb_gateway_arn", ""),
            "url": resources["kb_gateway_url"],
        }
    gateway = ensure_kb_gateway(control, resources, workspace.region)
    provisioned = {
        "kb_gateway_id": gateway["id"],
        "kb_gateway_arn": gateway["arn"],
        "kb_gateway_url": gateway["url"],
    }
    merge_workspace_resources(workspace, provisioned)
    if workspace.id == DEFAULT_WORKSPACE_ID:
        write_config({"resources": provisioned})
        get_settings.cache_clear()
    return gateway
