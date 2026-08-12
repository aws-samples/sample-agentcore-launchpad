"""AgentCore client factories — the only place these clients are named.

Preview API drift is contained here and in the sibling wrapper modules;
everything else passes clients explicitly so tests can inject stubs. Construction
goes through ``services.aws_clients`` via the workspace context, so a client
belongs to a workspace's account/region rather than to a process-wide region.

Every factory takes the workspace it targets, and takes it as a required
argument: a request's ``ws.context``, or ``context_for_workspace(row.
workspace_id)`` in a background worker. There is deliberately no default — a
factory that could fall back to the hub would silently send a second
workspace's work to the wrong account.
"""

from typing import Any

from botocore.config import Config

from app.core.config import get_settings
from app.services.workspace import WorkspaceContext


def control_client(ctx: WorkspaceContext) -> Any:
    return ctx.client("bedrock-agentcore-control")


def data_client(ctx: WorkspaceContext) -> Any:
    # Called on every invoke/chat turn — cache_token keeps it on the factory's
    # cached path despite the per-settings Config.
    timeout = get_settings().agentcore_read_timeout_s
    return ctx.client(
        "bedrock-agentcore",
        cache_token=f"read_timeout={timeout}",
        config=Config(read_timeout=timeout),
    )


def registry_control_client(ctx: WorkspaceContext) -> Any:
    return ctx.client("agent-registry-control")


def registry_data_client(ctx: WorkspaceContext) -> Any:
    return ctx.client("agent-registry")


def agent_client(ctx: WorkspaceContext) -> Any:
    return ctx.client("bedrock-agent")


def agent_runtime_client(ctx: WorkspaceContext) -> Any:
    return ctx.client("bedrock-agent-runtime")


def iam_client(ctx: WorkspaceContext) -> Any:
    return ctx.client("iam")
