"""Inbound-auth service: AWS_IAM-only in this build (GRAPH-LOOP-SPEC N3 /
decision D9).

The pure model lives in ``app.schemas.inbound_auth``; this module is the
agent-facing read side — the deploy-time snapshot on the Agent row and the
console's display mode. The ledger ``settings`` blob no longer carries an
inbound-auth default (there is only one mode to default to).

``fetch_discovery_document`` is unrelated to inbound Runtime auth — it is
shared infra for the OIDC discovery probe used by *outbound* Connection
checks (``identity_providers.check_obo_idp``), kept here because it is
network-adjacent, non-pure code.
"""

import logging
from typing import Any

from app.core.errors import AppError
from app.models.ledger import Agent
from app.schemas.inbound_auth import IAM_INBOUND, InboundAuth
from app.services.public_fetch import REASON_LABELS, PublicFetchError, fetch_public_json

__all__ = [
    "InboundAuth",
    "authorizer_configuration",
    "deployed_inbound_auth",
    "display_mode",
    "fetch_discovery_document",
    "record_deployed_auth",
    "resolve_for_agent",
]

logger = logging.getLogger("launchpad.inbound_auth")

DISCOVERY_PROBE_TIMEOUT_S = 5.0


def _default_probe(url: str) -> dict[str, Any]:
    """The production probe: https-only, public addresses only, no redirects
    (``public_fetch``) — the URL is whatever a console user typed."""
    return fetch_public_json(url, timeout=DISCOVERY_PROBE_TIMEOUT_S)


def fetch_discovery_document(url: str, probe: Any = None) -> dict[str, Any]:
    """GET a discovery document, failing as a generic 422 by category.

    Shared by every save-time read of a user-supplied discovery URL — today
    only the OBO Connection check in ``identity_providers``. The message
    carries the URL the caller typed and a short reason only — never the
    exception text, which for an internal target would describe the network
    behind the backend; the detail is logged server-side.
    """
    try:
        return (probe or _default_probe)(url)
    except AppError:
        raise
    except PublicFetchError as exc:
        reason, label = exc.reason, exc.label
    except Exception as exc:  # an injected probe, or an unexpected failure
        logger.info("discovery fetch of %s failed: %s: %s", url, type(exc).__name__, exc)
        reason, label = "unreachable", REASON_LABELS["unreachable"]
    raise AppError(
        "identity.discovery_unreachable",
        f"could not fetch the OIDC discovery document at {url} ({label})",
        {"reason": reason},
        status_code=422,
    )


def authorizer_configuration(auth: InboundAuth) -> dict[str, Any] | None:
    """The CreateAgentRuntime/UpdateAgentRuntime authorizerConfiguration param.

    Always None in this IAM-only build — the field is omitted, which is the
    SigV4 default. ``UpdateAgentRuntime`` resets an omitted authorizer, so an
    IAM redeploy must keep omitting it rather than ever send an empty block.
    """
    return None


def record_deployed_auth(agent: Agent, resolved: InboundAuth) -> None:
    """Snapshot the resolved choice onto the row (caller commits).

    Written by the deploy stage once the runtime reports READY (in the same
    commit as the READY ARN/version), never at Create/Update acceptance: an
    update that is accepted and then fails must not leave the ledger claiming
    a mode the live runtime does not serve.
    """
    agent.inbound_auth_mode = "iam"
    agent.inbound_auth_config = None


def deployed_inbound_auth(agent: Agent) -> InboundAuth:
    """The inbound auth the LIVE Runtime carries — always IAM in this build."""
    return InboundAuth(mode="iam")


def display_mode(agent: Agent) -> str:
    """This build is AWS_IAM-only; kept as a function so callers do not
    special-case the agent row shape, and so a future external-IdP node has
    one place to extend."""
    return "iam"


def resolve_for_agent(agent: Agent, db: Any = None) -> InboundAuth:
    """The effective inbound auth this agent's next deploy will apply.

    Always IAM in this build. Kept with the ``(agent, db)`` signature its
    callers (the zip_runtime/byoc deploy stages, the execution-role policy
    builder) already use, so none of them need to change.
    """
    return IAM_INBOUND
