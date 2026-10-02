"""Shared runtime environment derivation for AgentCore deployers."""

from collections.abc import Mapping
from typing import Any

from app.core.config import get_settings
from app.schemas.agent import AgentSpec
from app.services.gateway_bootstrap import GATEWAY_SCOPE
from app.templates.gateway_support import (
    ENV_PROVIDER,
    ENV_SCOPE,
    ENV_TOOLS,
    ENV_URL,
    ENV_WORKLOAD,
    gateway_tool_patterns,
    provider_name,
    uses_gateway,
)
from app.templates.identity_support import outbound_auth_env, uses_as_user

# Tool-level outbound auth declarations ({tool, type, connection, kind, mode,
# scopes[, url]} JSON). BYOC code reads this to perform its own Identity SDK
# exchange (the platform generates no code for it); generated agents carry it
# for observability only — their exchange config is baked into the source.
ENV_OUTBOUND_AUTH = "LAUNCHPAD_OUTBOUND_AUTH"
# as_user (3LO): where AgentCore Identity sends the browser after IdP consent,
# and the agent the round-tripped customState names. The deployer allow-lists
# the same URL on the workload identity (deployer/return_url.py).
ENV_OAUTH_RETURN_URL = "LAUNCHPAD_OAUTH_RETURN_URL"
ENV_AGENT_ID = "LAUNCHPAD_AGENT_ID"


def runtime_environment(
    spec: AgentSpec,
    resources: Mapping[str, Any],
    workload_name: str = "",
    agent_id: str = "",
) -> dict[str, str]:
    """Merge user environment with platform-owned runtime values.

    ``workload_name`` is the runtime's auto-created workload identity name, which
    does not exist until the runtime does — so it is empty on create and supplied
    on later updates. The rendered Gateway client treats a missing value as "no
    self-minted token", not as an error.
    """
    environment = dict(spec.env)
    # a spec-pinned memory overrides the workspace's shared bootstrap memory
    memory_id = spec.memory.memory_id or resources.get("memory_id")
    if (spec.memory.short_term or spec.memory.long_term) and memory_id:
        environment["LAUNCHPAD_MEMORY_ID"] = str(memory_id)
    if uses_gateway(spec):
        # Only set what is actually resolved: the generated client treats any
        # missing piece as "no gateway tools" and continues, whereas an empty
        # string would look configured and produce a confusing auth failure.
        gateway_url = str(resources.get("gateway_url") or "")
        provider = provider_name(resources)
        if gateway_url and provider:
            environment[ENV_URL] = gateway_url
            environment[ENV_PROVIDER] = provider
            environment[ENV_SCOPE] = GATEWAY_SCOPE
            # the client loads only the selected records' tools (target-scoped)
            environment[ENV_TOOLS] = ",".join(gateway_tool_patterns(spec))
        if workload_name:
            environment[ENV_WORKLOAD] = workload_name
    auth_declarations = outbound_auth_env(spec)
    if auth_declarations:
        environment[ENV_OUTBOUND_AUTH] = auth_declarations
        if workload_name:
            environment.setdefault(ENV_WORKLOAD, workload_name)
    if uses_as_user(spec):
        environment[ENV_OAUTH_RETURN_URL] = get_settings().resolved_oauth_return_url()
        if agent_id:
            environment[ENV_AGENT_ID] = agent_id
    return environment
