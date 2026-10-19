"""The low-level action call into the coding runtime.

Every request is one short InvokeAgentRuntime with ``{action, …}`` and every answer
the container's envelope ``{ok, action, boot_id, result | error}`` (always HTTP 200
from the container — AgentCore turns a container 5xx into an opaque
``RuntimeClientError``). ``call`` returns ``result`` or raises
``CodingRuntimeError(code, message)`` with the container's stable error code
(``turn_running``, ``conflict``, ``path_invalid``, …) or one of this module's own
codes for a call that never got a container answer. Project lookup, ownership
and restore-URL folding are the caller's (the backend layer above).
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from app.core.errors import aws_error_code, aws_error_message
from app.services.agentcore import runtime as rt
from app.services.agentcore.harness import new_session_id
from app.services.workspace import WorkspaceContext

# Error codes the container answers with (image/src/util.mjs ERROR_CODES).
CONTAINER_ERROR_CODES = frozenset({
    "turn_running", "no_turn", "path_invalid", "file_too_large", "conflict", "not_found",
    "restore_failed", "appserver_failed", "upload_failed", "bad_request", "internal",
})
# Codes this client raises when there is no container answer to report.
RUNTIME_UNAVAILABLE = "runtime_unavailable"  # the AWS call itself failed
RUNTIME_PROTOCOL = "runtime_protocol"  # an answer that is not the envelope

# AgentCore requires runtime session ids of at least 33 characters.
MIN_SESSION_ID_LEN = 33

__all__ = ["CodingRuntimeError", "call", "new_session_id"]


class CodingRuntimeError(RuntimeError):
    """A coding-runtime action failed. ``detail`` carries the container's extra
    error fields (e.g. ``sha256`` on a ``conflict``); ``aws_code`` the AWS error
    code when the invoke itself failed."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        detail: dict[str, Any] | None = None,
        aws_code: str | None = None,
        boot_id: str | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = detail or {}
        self.aws_code = aws_code
        self.boot_id = boot_id


def call(
    ctx: WorkspaceContext,
    runtime_arn: str,
    session_id: str,
    action: str,
    *,
    read_timeout: float | None = None,
    client: Any = None,
    **payload: Any,
) -> Any:
    """Run one action on the session's microVM and return its ``result``.

    ``payload`` goes to the container verbatim (``None`` values included — e.g.
    ``expected_sha256=None`` means "the file must not exist yet")."""
    if not runtime_arn:
        raise CodingRuntimeError(RUNTIME_UNAVAILABLE, "the coding runtime is not installed")
    if not session_id or len(session_id) < MIN_SESSION_ID_LEN:
        raise ValueError(f"runtime session ids need at least {MIN_SESSION_ID_LEN} characters")
    body = {**payload, "action": action}
    try:
        answer = rt.invoke_runtime_json(
            ctx, runtime_arn, session_id, body, read_timeout=read_timeout, client=client
        )
    except rt.RuntimeJsonError as exc:
        raise CodingRuntimeError(RUNTIME_PROTOCOL, str(exc)) from exc
    except ClientError as exc:
        raise CodingRuntimeError(
            RUNTIME_UNAVAILABLE, aws_error_message(exc), aws_code=aws_error_code(exc)
        ) from exc
    except BotoCoreError as exc:  # timeouts, connection errors
        raise CodingRuntimeError(RUNTIME_UNAVAILABLE, str(exc)) from exc
    if not isinstance(answer, dict) or not isinstance(answer.get("ok"), bool):
        raise CodingRuntimeError(RUNTIME_PROTOCOL, f"unexpected answer to {action!r}")
    if answer["ok"]:
        return answer.get("result")
    error = answer.get("error") if isinstance(answer.get("error"), dict) else {}
    code = str(error.get("code") or "internal")
    raise CodingRuntimeError(
        code,
        str(error.get("message") or f"{action} failed"),
        detail={k: v for k, v in error.items() if k not in ("code", "message")},
        boot_id=answer.get("boot_id"),
    )
