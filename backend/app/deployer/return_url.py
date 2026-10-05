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
A failure is retried, then reported as a warning — never a failed stage (see
``register_return_url_stage``).
"""

import logging
import time
from collections.abc import Callable
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import get_settings
from app.core.errors import aws_error_code
from app.schemas.agent import AgentSpec
from app.templates.identity_support import uses_as_user

logger = logging.getLogger("launchpad.deployer.return_url")


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


# Attempts at the GetWorkloadIdentity/UpdateWorkloadIdentity pair, and the
# backoff between them: a freshly created runtime's identity (and a fresh role
# grant) can lag the READY status by seconds.
RETURN_URL_ATTEMPTS = 3
RETURN_URL_BACKOFF_S = 2.0

RETURN_URL_WARNING = "return URL not allow-listed (see job log)"


def register_return_url_stage(
    control: Any,
    spec: AgentSpec,
    runtime_id: str,
    log: Callable[[str], None],
    sleeper: Callable[[float], None] = time.sleep,
) -> bool:
    """Deploy-stage hook: reconcile the allow-list for specs with an as_user tool.

    Returns False when the URL could not be allow-listed. NON-FATAL by design:
    it runs after the runtime is READY on its new version, and failing the
    deploy stage there would mark a working agent failed — and the redeploy an
    operator then starts issues another UpdateAgentRuntime (a new version) just
    to retry this one call. So it retries with backoff, then leaves a warning
    in the job log (and the stage detail) instead; until the URL is listed, a
    consent redirect for this agent ends at the provider callback page, and
    the next deploy of the agent reconciles it again.
    """
    if not uses_as_user(spec):
        return True
    return_url = get_settings().resolved_oauth_return_url()
    for attempt in range(1, RETURN_URL_ATTEMPTS + 1):
        try:
            ensure_return_url_allowed(control, runtime_id, return_url, log)
            return True
        except (ClientError, BotoCoreError) as exc:
            code = aws_error_code(exc) if isinstance(exc, ClientError) else type(exc).__name__
            if attempt < RETURN_URL_ATTEMPTS:
                log(
                    f"return-URL registration on workload identity {runtime_id} "
                    f"failed ({code}); retrying ({attempt}/{RETURN_URL_ATTEMPTS})"
                )
                sleeper(RETURN_URL_BACKOFF_S * attempt)
                continue
            logger.warning(
                "return-URL registration failed on workload identity %s: %s",
                runtime_id, code,
            )
            log(
                f"WARNING: return-URL registration failed on workload identity "
                f"{runtime_id}: {code} — the agent is deployed, but as_user consent "
                "redirects cannot return to the console until the URL is "
                "allow-listed; redeploy the agent to retry"
            )
    return False
