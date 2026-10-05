"""SSRF-guarded GET of a small public JSON document.

The one place the backend fetches a URL a console user typed — today the OIDC
discovery document (inbound JWT authorizer, OBO Connection IdP check). The URL
is attacker-controlled, so the fetch:

- requires ``https`` (a discovery document over plain HTTP is not trustworthy
  anyway, and the Runtime authorizer itself needs https);
- resolves the host and refuses it when ANY resolved address is non-public
  (private, loopback, link-local incl. 169.254.169.254, reserved, multicast,
  unspecified, CGNAT; IPv4-mapped IPv6 judged as its IPv4 address);
- connects to the address it validated (pinned: the URL host is swapped for
  the IP, with the original Host header and TLS SNI/certificate name), so a
  DNS answer that changes between check and connect cannot reach inside;
- never follows a redirect — a 3xx is a failure, since a redirect is exactly
  how a public URL would bounce the request to an internal one;
- caps the body size.

Failures raise ``PublicFetchError`` with a short ``reason`` category the caller
may show; the underlying exception text (resolver/TLS/connection detail that
could map the internal network) is logged here, never returned.
"""

import ipaddress
import json
import logging
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger("launchpad.public_fetch")

MAX_DOCUMENT_BYTES = 1_000_000

# reason → the short label a 422 may carry
REASON_LABELS = {
    "not_https": "not https",
    "unresolvable": "host does not resolve",
    "non_public_address": "non-public address",
    "redirect_refused": "redirect refused",
    "http_error": "HTTP error",
    "unreachable": "unreachable",
    "too_large": "response too large",
    "not_json_object": "not a JSON object",
}

Resolver = Callable[..., list[Any]]


class PublicFetchError(Exception):
    """A refused or failed public fetch, by category."""

    def __init__(self, reason: str, status_code: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code

    @property
    def label(self) -> str:
        if self.reason == "http_error" and self.status_code:
            return f"HTTP {self.status_code}"
        return REASON_LABELS.get(self.reason, self.reason)


IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def is_public_address(ip: IpAddress) -> bool:
    """Whether one resolved address is safe to connect to from the backend."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def resolve_public(host: str, port: int, resolver: Resolver = socket.getaddrinfo) -> list[str]:
    """Every address ``host`` resolves to, or PublicFetchError when there is none
    or when ANY of them is non-public (one bad answer is enough to refuse)."""
    try:
        infos = resolver(host, port, proto=socket.IPPROTO_TCP)
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    except (OSError, ValueError, UnicodeError) as exc:
        logger.info("public fetch: %s does not resolve: %s", host, exc)
        raise PublicFetchError("unresolvable") from exc
    if not addresses:
        raise PublicFetchError("unresolvable")
    blocked = [str(ip) for ip in addresses if not is_public_address(ip)]
    if blocked:
        logger.warning("public fetch: refused %s, resolves to non-public %s", host, blocked)
        raise PublicFetchError("non_public_address")
    return [str(ip) for ip in addresses]


def fetch_public_json(
    url: str,
    *,
    timeout: float,
    resolver: Resolver = socket.getaddrinfo,
    transport: httpx.BaseTransport | None = None,
    max_bytes: int = MAX_DOCUMENT_BYTES,
) -> dict[str, Any]:
    """GET ``url`` under the guards above and return its JSON object body.

    ``resolver`` / ``transport`` are test seams (``socket.getaddrinfo`` and
    httpx's default transport in production)."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port or 443
    except ValueError as exc:
        raise PublicFetchError("not_https") from exc
    if parts.scheme.lower() != "https" or not host:
        raise PublicFetchError("not_https")
    address = resolve_public(host, port, resolver)[0]
    pinned_host = f"[{address}]" if ":" in address else address
    target = parts._replace(netloc=f"{pinned_host}:{port}").geturl()
    host_header = host if parts.port in (None, 443) else f"{host}:{port}"
    body = bytearray()
    try:
        with httpx.Client(
            timeout=timeout, follow_redirects=False, transport=transport
        ) as client, client.stream(
            "GET",
            target,
            headers={"Host": host_header, "Accept": "application/json"},
            # TLS SNI + certificate verification against the real host name
            extensions={"sni_hostname": host},
        ) as response:
            if 300 <= response.status_code < 400:
                logger.warning(
                    "public fetch: refused a %s redirect from %s", response.status_code, host
                )
                raise PublicFetchError("redirect_refused", response.status_code)
            if response.status_code >= 400:
                raise PublicFetchError("http_error", response.status_code)
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise PublicFetchError("too_large")
    except PublicFetchError:
        raise
    except (httpx.HTTPError, OSError) as exc:
        logger.info("public fetch: %s unreachable: %s: %s", host, type(exc).__name__, exc)
        raise PublicFetchError("unreachable") from exc
    try:
        document = json.loads(bytes(body))
    except ValueError as exc:
        raise PublicFetchError("not_json_object") from exc
    if not isinstance(document, dict):
        raise PublicFetchError("not_json_object")
    return document
