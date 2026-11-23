"""The session-id rule of a per-session S3 bucket mount.

An ``s3BucketMount`` whose ``prefix`` carries ``{runtimeSessionId}`` makes AgentCore
substitute the invoking session's id into the S3 prefix. AgentCore then answers
``ValidationException`` unless the id is 33-256 characters, starts alphanumeric and
holds only ``[A-Za-z0-9_-]``. Console ids (``harness.new_session_id``, 64 hex) always
fit; a ``/v1`` caller composing its own (``<ulid>#feishu#<chat_id>``) may not, so the
shared invoke chain refuses it up front with a named 400 instead of an AWS error.
An absent session id is minted by the chain and always fits.
"""

import re
from typing import Any

from app.core.errors import AppError
from app.services.agentcore.filesystem_kinds import per_session_prefix

SESSION_ID_MOUNT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{32,255}$")
SESSION_ID_INCOMPATIBLE = "invoke.session_id_incompatible"


def session_id_fits_mount(session_id: str | None) -> bool:
    """True when ``session_id`` is usable under a ``{runtimeSessionId}`` prefix (or is
    absent, so the chain mints a fitting one)."""
    return not session_id or SESSION_ID_MOUNT_RE.match(session_id) is not None


def has_per_session_mount(agent: Any) -> bool:
    """Whether the agent's spec mounts a bucket prefix keyed by the session id.

    Never raises: a spec that does not parse (an imported or discovered agent) has no
    platform-set mounts, and the guard must not turn that into a failed invoke."""
    stored = getattr(agent, "spec", None)
    if not isinstance(stored, dict):
        return False
    try:
        from app.deployer import filesystem
        from app.schemas.agent import AgentSpec

        entries = filesystem.filesystem_configurations(
            AgentSpec(**stored), getattr(agent, "id", None))
    except Exception:  # noqa: BLE001 — see docstring
        return False
    return any(per_session_prefix(entry) for entry in entries)


def assert_session_id_compatible(agent: Any, session_id: str | None) -> None:
    """Refuse (400 ``invoke.session_id_incompatible``) a caller-chosen session id that
    cannot sit under the agent's per-session mount, before any AWS call. The cheap id
    check runs first, so an agent without such a mount never parses its spec for an
    ordinary id."""
    if session_id_fits_mount(session_id) or not has_per_session_mount(agent):
        return
    raise AppError(
        SESSION_ID_INCOMPATIBLE,
        "this agent's storage is mounted per session, so the session id must be 33-256 "
        "characters, start with a letter or digit and contain only letters, digits, "
        "'_' and '-' (e.g. no '#') — pick another session id or omit it",
        {"session_id_pattern": SESSION_ID_MOUNT_RE.pattern},
        status_code=400,
    )
