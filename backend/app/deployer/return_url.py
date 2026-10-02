"""as_user (3LO) return-URL allow-list on the agent's workload identity.

AgentCore Identity only redirects the browser to ``resourceOauth2ReturnUrl``
after IdP consent if that URL is allow-listed on the workload identity
(``allowedResourceOauth2ReturnUrls``). Every AgentCore Runtime auto-creates a
workload identity whose **name equals the runtime id**, and that identity IS
updatable through ``UpdateWorkloadIdentity`` (live-verified 2026-09-19 in
us-west-2), so no shadow identity is needed.

Called from the zip_runtime and byoc deploy stages — the methods that honour
tool-level auth — on create and redeploy alike: a no-op when the URL is already
listed, and it adds the URL again when the console's public base URL changed.
"""

from collections.abc import Callable
from typing import Any

from botocore.exceptions import ClientError

from app.core.config import get_settings
from app.core.errors import aws_error_code
from app.schemas.agent import AgentSpec
from app.templates.identity_support import uses_as_user


def ensure_return_url_allowed(
    control: Any,
    runtime_id: str,
    return_url: str,
    log: Callable[[str], None],
) -> bool:
    """Idempotently add ``return_url`` to the runtime's workload identity.

    Returns True when an update was issued. Existing entries are preserved:
    another console (or an operator) may have registered its own URL.
    """
    identity = control.get_workload_identity(name=runtime_id)
    current = list(identity.get("allowedResourceOauth2ReturnUrls") or [])
    if return_url in current:
        log(f"return URL already allow-listed on workload identity {runtime_id}")
        return False
    control.update_workload_identity(
        name=runtime_id,
        allowedResourceOauth2ReturnUrls=sorted({*current, return_url}),
    )
    log(
        f"return URL registered on workload identity {runtime_id} "
        f"({len(current)} → {len(current) + 1} entries)"
    )
    return True


def register_return_url_stage(
    control: Any,
    spec: AgentSpec,
    runtime_id: str,
    log: Callable[[str], None],
) -> None:
    """Deploy-stage hook: reconcile the allow-list for specs with an as_user tool.

    A failure fails the stage visibly: without the URL on the allow-list every
    consent redirect dead-ends at the provider callback page.
    """
    if not uses_as_user(spec):
        return
    return_url = get_settings().resolved_oauth_return_url()
    try:
        ensure_return_url_allowed(control, runtime_id, return_url, log)
    except ClientError as exc:
        log(
            f"return-URL registration failed on workload identity {runtime_id}: "
            f"{aws_error_code(exc)} — as_user consent redirects cannot complete "
            "until the URL is allow-listed"
        )
        raise
