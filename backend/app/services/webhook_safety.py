"""SSRF-guarded outbound webhook: the URL check and the one JSON POST.

An alert rule's webhook URL is typed by a console administrator and then POSTed to by
the backend, unattended, from inside the hub's network — so it gets the same guards as
the OIDC discovery fetch (`services.public_fetch`), whose address checks it reuses:

- ``https://`` only, a host, and no credentials in the URL (``user:pass@``);
- the host must resolve, and **every** resolved address must be public (private,
  loopback, link-local incl. 169.254.169.254, reserved, multicast, unspecified,
  CGNAT; an IPv4-mapped IPv6 address is judged as its IPv4 address). One bad answer
  is enough to refuse;
- checked when the rule is saved **and** again on every send;
- the send connects to the address it just validated (pinned: the URL host is
  swapped for the IP, with the original Host header and TLS SNI / certificate
  name), so a DNS answer that changes between check and connect cannot reach
  inside. The residual is the resolver itself: a check is only as good as the
  answer DNS gives at that moment, which is accepted;
- a redirect is never followed — a 3xx is a failed delivery, since a redirect is
  exactly how a public URL would bounce the request to an internal one;
- a 5 s timeout (per connect / write / read step) and a small JSON body; the
  response body is never read, only the status line.

`check_webhook_url` raises `AppError` (400) for the router; `post_json` never raises.
"""

import json
import logging
import socket
from typing import Any
from urllib.parse import SplitResult, urlsplit

import httpx

from app.core.errors import AppError
from app.services.public_fetch import PublicFetchError, resolve_public

logger = logging.getLogger("launchpad.webhook")

TIMEOUT_SECONDS = 5.0
MAX_BODY_BYTES = 16_384
MAX_URL_LENGTH = 512

# reason → (error code, message) for a refused URL
_REFUSALS: dict[str, tuple[str, str]] = {
    "not_https": ("alert.webhook_not_https", "a webhook URL must be https://"),
    "no_host": ("alert.webhook_invalid", "a webhook URL needs a host"),
    "invalid": ("alert.webhook_invalid", "the webhook URL is not a valid URL"),
    "credentials": (
        "alert.webhook_credentials",
        "a webhook URL must not carry credentials (user:password@)",
    ),
    "unresolvable": ("alert.webhook_unresolvable", "the webhook host does not resolve"),
    "non_public_address": (
        "alert.webhook_non_public",
        "the webhook host resolves to a non-public address",
    ),
}


class WebhookRefused(Exception):
    """A URL the guard refuses, by `_REFUSALS` reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _parse(url: str) -> tuple[SplitResult, str, int]:
    if len(url) > MAX_URL_LENGTH:
        raise WebhookRefused("invalid")
    try:
        parts = urlsplit(url)
        port = parts.port or 443
    except ValueError as exc:
        raise WebhookRefused("invalid") from exc
    if parts.scheme.lower() != "https":
        raise WebhookRefused("not_https")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise WebhookRefused("credentials")
    host = parts.hostname or ""
    if not host:
        raise WebhookRefused("no_host")
    return parts, host, port


def _validate(url: str) -> tuple[SplitResult, str, int, list[str]]:
    """Parse + resolve, or WebhookRefused. Returns the public addresses."""
    parts, host, port = _parse(url)
    try:
        # Looked up at call time, so a stubbed `socket.getaddrinfo` is honoured.
        addresses = resolve_public(host, port, socket.getaddrinfo)
    except PublicFetchError as exc:
        raise WebhookRefused(exc.reason) from exc
    return parts, host, port, addresses


def check_webhook_url(url: str) -> str:
    """The cleaned URL, or `AppError` 400 naming why it is refused."""
    cleaned = url.strip()
    try:
        _validate(cleaned)
    except WebhookRefused as exc:
        code, message = _REFUSALS.get(exc.reason, ("alert.webhook_invalid", exc.reason))
        raise AppError(code, message, status_code=400) from exc
    return cleaned


def post_json(
    url: str,
    body: dict[str, Any],
    *,
    timeout: float = TIMEOUT_SECONDS,
    transport: httpx.BaseTransport | None = None,
) -> tuple[bool, str]:
    """POST ``body`` as JSON under the guards above: `(delivered, short outcome)`.

    Never raises — a broken destination must not fail the evaluation that called it.
    ``transport`` is a test seam (httpx's default transport in production).
    """
    try:
        parts, host, port, addresses = _validate(url)
    except WebhookRefused as exc:
        logger.warning("webhook refused on send: %s", exc.reason)
        return False, f"refused ({_REFUSALS.get(exc.reason, (exc.reason, exc.reason))[1]})"
    payload = json.dumps(body, default=str).encode()
    if len(payload) > MAX_BODY_BYTES:
        return False, "body too large"
    address = addresses[0]
    pinned_host = f"[{address}]" if ":" in address else address
    target = parts._replace(netloc=f"{pinned_host}:{port}").geturl()
    # `hostname` drops an IPv6 literal's brackets; the Host header needs them back
    host_part = f"[{host}]" if ":" in host else host
    host_header = host_part if parts.port in (None, 443) else f"{host_part}:{port}"
    try:
        with httpx.Client(
            timeout=timeout, follow_redirects=False, transport=transport
        ) as client, client.stream(
            "POST",
            target,
            content=payload,
            headers={"Host": host_header, "Content-Type": "application/json"},
            # TLS SNI + certificate verification against the real host name
            extensions={"sni_hostname": host},
        ) as response:
            # Only the status matters: the response body is never read, so a hostile
            # endpoint cannot make the (single) scheduler thread buffer or wait on it.
            status = response.status_code
    except (httpx.HTTPError, OSError) as exc:
        logger.info("webhook to %s failed: %s: %s", host, type(exc).__name__, exc)
        return False, type(exc).__name__
    except Exception as exc:  # noqa: BLE001 - delivery must never raise
        logger.warning("webhook to %s failed: %s", host, type(exc).__name__)
        return False, type(exc).__name__
    if 300 <= status < 400:
        logger.warning("webhook: refused a %s redirect from %s", status, host)
        return False, f"redirect refused (HTTP {status})"
    return 200 <= status < 300, f"HTTP {status}"
