"""AgentCore client factories — the only place these clients are named.

Preview API drift is contained here and in the sibling wrapper modules;
everything else passes clients explicitly so tests can inject stubs. Construction
goes through ``services.aws_clients`` via the workspace context, so a client
belongs to a workspace's account/region rather than to a process-wide region.

Every factory takes the workspace it targets. ``ctx=None`` falls back to the
default workspace and exists only so a call site that has not been threaded yet
keeps working: this module is the **single** place the funnel guard test allows
that fallback, so the fallback cannot spread by copy-paste.
"""

from typing import Any

from botocore.config import Config

from app.core.config import get_settings
from app.services.workspace import WorkspaceContext, default_workspace_context


def _ws(ctx: WorkspaceContext | None) -> WorkspaceContext:
    return ctx if ctx is not None else default_workspace_context()


def control_client(ctx: WorkspaceContext | None = None) -> Any:
    return _ws(ctx).client("bedrock-agentcore-control")


def data_client(ctx: WorkspaceContext | None = None) -> Any:
    # Called on every invoke/chat turn — cache_token keeps it on the factory's
    # cached path despite the per-settings Config.
    timeout = get_settings().agentcore_read_timeout_s
    return _ws(ctx).client(
        "bedrock-agentcore",
        cache_token=f"read_timeout={timeout}",
        config=Config(read_timeout=timeout),
    )


def registry_control_client(ctx: WorkspaceContext | None = None) -> Any:
    return _ws(ctx).client("agent-registry-control")


def registry_data_client(ctx: WorkspaceContext | None = None) -> Any:
    return _ws(ctx).client("agent-registry")


def agent_client(ctx: WorkspaceContext | None = None) -> Any:
    return _ws(ctx).client("bedrock-agent")


def agent_runtime_client(ctx: WorkspaceContext | None = None) -> Any:
    return _ws(ctx).client("bedrock-agent-runtime")


def iam_client(ctx: WorkspaceContext | None = None) -> Any:
    return _ws(ctx).client("iam")
