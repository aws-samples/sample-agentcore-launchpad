"""The low-level action call into the coding runtime.

Every request is one short InvokeAgentRuntime with ``{action, …}`` and every answer
the container's envelope ``{ok, action, boot_id, result | error}`` (always HTTP 200
from the container — AgentCore turns a container 5xx into an opaque
``RuntimeClientError``). ``call`` returns ``result`` or raises
``CodingRuntimeError(code, message)`` with the container's stable error code
(``turn_running``, ``conflict``, ``path_invalid``, …) or one of this module's own
codes for a call that never got a container answer.

``project_call`` is the project-level entry every backend path uses: it refuses
before any AWS call when the workspace has no runtime (``409
coding.runtime_not_installed``) and folds ``restore_url`` — a fresh presigned GET
of the project's newest snapshot — into every action, so a microVM whose session
storage came back empty (runtime version update, 14-day idle expiry) restores the
project before it handles anything. Ownership is the caller's (``projects``).
``api_error`` maps a ``CodingRuntimeError`` onto the console's ``coding.*``
error envelope.
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from app.core.errors import AppError, aws_error_code, aws_error_message
from app.services.agentcore import runtime as rt
from app.services.agentcore.harness import new_session_id
from app.services.workspace import WorkspaceContext

from . import infra, s3urls

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

__all__ = [
    "CodingRuntimeError", "api_error", "call", "installed_runtime_arn", "new_session_id",
    "project_call", "run",
]


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


# ── the project-level entry ─────────────────────────────────────────────────

# container code → (console error code, HTTP status); anything else is a 502
_API_ERRORS: dict[str, tuple[str, int]] = {
    "turn_running": ("coding.turn_running", 409),
    "no_turn": ("coding.no_turn", 409),
    "path_invalid": ("coding.path_invalid", 422),
    "file_too_large": ("coding.file_too_large", 413),
    "conflict": ("coding.file_conflict", 409),
    "restore_failed": ("coding.restore_failed", 502),
}
RUNTIME_ERROR = "coding.runtime_error"


def installed_runtime_arn(ctx: WorkspaceContext) -> str | None:
    """The workspace's coding runtime ARN, or None when it is not installed."""
    return str((ctx.resources or {}).get(infra.KEY_RUNTIME_ARN) or "") or None


def not_installed() -> AppError:
    return AppError(
        "coding.runtime_not_installed",
        "the coding runtime is not installed in this workspace — an administrator "
        "installs it from the coding console",
        status_code=409,
    )


def require_runtime_arn(ctx: WorkspaceContext) -> str:
    arn = installed_runtime_arn(ctx)
    if not arn:
        raise not_installed()
    return arn


def project_call(
    ctx: WorkspaceContext,
    project: Any,
    action: str,
    *,
    read_timeout: float | None = None,
    client: Any = None,
    **payload: Any,
) -> Any:
    """Run ``action`` on the project's session, restore URL folded in.

    Raises ``AppError(coding.runtime_not_installed)`` before any AWS call when the
    workspace has no runtime, and ``CodingRuntimeError`` when the action fails."""
    arn = require_runtime_arn(ctx)
    if getattr(project, "snapshot_key", None):
        try:
            payload["restore_url"] = s3urls.presign_get(ctx, project.snapshot_key)
        except Exception as exc:  # noqa: BLE001 — a signing failure is a call failure
            raise CodingRuntimeError(
                RUNTIME_UNAVAILABLE, f"could not sign the snapshot restore URL: {exc}"
            ) from exc
    return call(
        ctx, arn, project.session_id, action, read_timeout=read_timeout, client=client,
        **payload,
    )


def api_error(exc: CodingRuntimeError) -> AppError:
    """The console envelope for a failed action: the container's stable codes map
    onto ``coding.*`` (a conflict carries the file's current ``sha256``); a call
    that never got a container answer, or an unexpected one, is a 502."""
    code, status = _API_ERRORS.get(exc.code, (RUNTIME_ERROR, 502))
    if exc.code == "conflict":
        detail: dict[str, Any] = {"sha256": exc.detail.get("sha256")}
    else:
        detail = {"runtime_code": exc.code, **exc.detail}
        if exc.aws_code:
            detail["aws_error_code"] = exc.aws_code
    return AppError(code, exc.message, detail, status_code=status)


def run(ctx: WorkspaceContext, project: Any, action: str, **payload: Any) -> Any:
    """``project_call`` for a request handler: a failure is the console error."""
    try:
        return project_call(ctx, project, action, **payload)
    except CodingRuntimeError as exc:
        raise api_error(exc) from exc
