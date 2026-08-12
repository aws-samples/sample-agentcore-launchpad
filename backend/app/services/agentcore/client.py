"""AgentCore client factories — the only place these clients are named.

Preview API drift is contained here and in the sibling wrapper modules;
everything else passes clients explicitly so tests can inject stubs. Construction
goes through ``services.aws_clients`` via the workspace context, so a client
belongs to a workspace's account/region rather than to a process-wide region.
"""

from typing import Any

from botocore.config import Config

from app.core.config import get_settings
from app.services.workspace import default_workspace_context


def control_client() -> Any:
    return default_workspace_context().client("bedrock-agentcore-control")


def data_client() -> Any:
    # Called on every invoke/chat turn — cache_token keeps it on the factory's
    # cached path despite the per-settings Config.
    timeout = get_settings().agentcore_read_timeout_s
    return default_workspace_context().client(
        "bedrock-agentcore",
        cache_token=f"read_timeout={timeout}",
        config=Config(read_timeout=timeout),
    )


def registry_control_client() -> Any:
    return default_workspace_context().client("agent-registry-control")


def registry_data_client() -> Any:
    return default_workspace_context().client("agent-registry")


def agent_client() -> Any:
    return default_workspace_context().client("bedrock-agent")


def agent_runtime_client() -> Any:
    return default_workspace_context().client("bedrock-agent-runtime")


def iam_client() -> Any:
    return default_workspace_context().client("iam")
