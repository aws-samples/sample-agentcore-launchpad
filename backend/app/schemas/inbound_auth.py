"""Inbound authentication of an agent's Runtime: AWS_IAM (SigV4) only in this
build (GRAPH-LOOP-SPEC N3 / decision D9).

The CUSTOM_JWT authorizer this model used to also carry is not offered: the
China Gateway/Runtime disallow the managed-user-pool JWT authorizer this build
previously relied on, and it mints no bearer token of its own to present to
any other IdP's authorizer either — so there is nothing a JWT mode here could
serve. See
``docs/cn/interfaces/gateway-iam.md`` for the shared-gateway shape this
pairs with.

The OIDC issuer helpers below are unrelated to inbound Runtime auth — they
back the *outbound* Connection → discovery derivation (identity_providers,
identity_gateway_targets) and stay for that.
"""

from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel

DISCOVERY_SUFFIX = "/.well-known/openid-configuration"


class InboundAuth(BaseModel):
    """One inbound-auth choice: how callers must authenticate to this Runtime.

    AWS_IAM (SigV4) is the only mode this build offers."""

    mode: Literal["iam"] = "iam"


IAM_INBOUND = InboundAuth(mode="iam")


def issuer_from_discovery_url(url: str | None) -> str | None:
    """The issuer an OIDC discovery URL names: the URL minus its well-known suffix.

    OIDC Discovery 1.0 §4 builds the discovery URL by appending the suffix to
    the issuer, so this is the issuer without a network read. Scheme and host
    are lowercased (case-insensitive); the path is kept as-is.
    """
    url = (url or "").strip()
    if not url.endswith(DISCOVERY_SUFFIX):
        return None
    return normalize_issuer(url[: -len(DISCOVERY_SUFFIX)])


def normalize_issuer(issuer: str | None) -> str | None:
    """An issuer in comparable form, or None when it is not an http(s) URL.

    A templated issuer (multi-tenant metadata such as ``.../{tenantid}/v2.0``)
    names no single IdP, so it reads as None too.
    """
    raw = (issuer or "").strip()
    parts = urlsplit(raw)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    if "{" in raw or "}" in raw:
        return None
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip('/')}"


def discovery_url_for_issuer(issuer: str | None) -> str | None:
    """The discovery URL of an issuer (OIDC Discovery 1.0 §4), or None."""
    normalized = normalize_issuer(issuer)
    return f"{normalized}{DISCOVERY_SUFFIX}" if normalized else None


def parse_inbound_auth(value: Any) -> InboundAuth | None:
    """A stored/blob value → model, or None for absent/invalid.

    Lenient on purpose for the read path: a settings blob written by an older
    release (e.g. a pre-N3 ``{"mode": "jwt", "jwt": {...}}`` snapshot) must not
    make every agent list 500 — an unreadable value simply behaves like no
    value (the caller treats that as IAM).
    """
    if not isinstance(value, dict):
        return None
    try:
        return InboundAuth(**value)
    except ValueError:
        return None
