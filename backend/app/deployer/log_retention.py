"""Runtime log-group retention (N11-obs56).

AgentCore Runtime writes a runtime's logs to ``/aws/bedrock-agentcore/runtimes/<runtime_id>-
DEFAULT`` and creates that group itself on the first line — with no retention, so every
log line is kept forever. Nothing in the platform created or configured these groups (the
teardown only deletes them), and the stacks' Lambda log groups had the same gap (fixed in
CDK: ``infra/stacks/base_stack.py``). The deploy stage now ends with
``apply_runtime_log_retention``: create the group if it does not exist yet (the service
then logs into the existing group), set ``Settings.log_group_retention_days`` on it.
Idempotent (``ResourceAlreadyExistsException`` is the expected answer on a redeploy) and
fail-soft — a denied or failed call is a WARN line in the deploy log, never a failed deploy.

Called through the module attribute (``log_retention.apply_runtime_log_retention``) so the
hermetic suite can replace it: a stage test that stubs only the control client must never
reach a real ``logs`` client (``tests/conftest.py::hermetic_log_retention``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import get_settings
from app.deployer.teardown import runtime_log_group_prefix

RUNTIME_LOG_GROUP_SUFFIX = "DEFAULT"


def runtime_log_group_name(runtime_id: str) -> str:
    """``/aws/bedrock-agentcore/runtimes/<runtime_id>-DEFAULT`` — the group the Runtime
    writes to (the same prefix the teardown sweeps)."""
    return f"{runtime_log_group_prefix(runtime_id)}{RUNTIME_LOG_GROUP_SUFFIX}"


def _error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code") or type(exc).__name__)


def ensure_log_group_retention(
    logs: Any, name: str, *, days: int, log: Callable[[str], None] = lambda _m: None
) -> str:
    """Create ``name`` if missing, then put ``days`` as its retention. Returns the one-line
    outcome for the deploy log; raises the botocore error for the caller to degrade on."""
    try:
        logs.create_log_group(logGroupName=name)
        created = True
    except ClientError as exc:
        if _error_code(exc) != "ResourceAlreadyExistsException":
            raise
        created = False
    logs.put_retention_policy(logGroupName=name, retentionInDays=days)
    state = "created" if created else "exists"
    log(f"log group {name} {state}")
    return f"log group {name} {state} · retention {days} d"


def apply_runtime_log_retention(ctx: Any, runtime_id: str) -> str:
    """The deploy stage's last step: retention on the runtime's log group, through the
    workspace's funnel ``logs`` client. Never raises — the deploy is READY by now."""
    days = int(get_settings().log_group_retention_days or 0)
    name = runtime_log_group_name(runtime_id)
    if days <= 0:
        return f"log-group retention: disabled (log_group_retention_days=0) — {name} unbounded"
    try:
        logs = ctx.workspace.client("logs")
        return ensure_log_group_retention(logs, name, days=days, log=ctx.log)
    except ClientError as exc:
        reason = _error_code(exc)
    except BotoCoreError as exc:
        reason = type(exc).__name__
    except Exception as exc:  # noqa: BLE001 — "never raises": the deploy is READY already
        reason = f"unexpected {type(exc).__name__}"
    return (
        f"log-group retention WARN: {name} keeps the account default (never expires) — "
        f"{reason}; the deploy role needs logs:CreateLogGroup + logs:PutRetentionPolicy "
        f"on log-group:/aws/bedrock-agentcore/runtimes/* (N11-obs56)"
    )
