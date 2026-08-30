"""Shared runtime environment derivation for AgentCore deployers."""

from collections.abc import Mapping
from typing import Any

from app.schemas.agent import AgentSpec
from app.services.gateway_bootstrap import GATEWAY_SCOPE
from app.templates.gateway_support import (
    ENV_PROVIDER,
    ENV_SCOPE,
    ENV_URL,
    ENV_WORKLOAD,
    provider_name,
    uses_gateway,
)


def runtime_environment(
    spec: AgentSpec, resources: Mapping[str, Any], workload_name: str = ""
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
        if workload_name:
            environment[ENV_WORKLOAD] = workload_name
    return environment
