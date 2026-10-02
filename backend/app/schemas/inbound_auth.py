"""Inbound authentication of an agent's Runtime: IAM (SigV4) or JWT bearer.

One Runtime supports exactly one inbound mode at a time (service constraint, see
docs/identity.md). The workspace carries a default (`Workspace.settings
["inbound_auth_default"]`), every `AgentSpec` may override it, and
`resolve_inbound_auth` is the single place the two are folded together — the
deployer, the invoke path and the UI all consume its result rather than
re-deriving precedence.

Pure models + pure functions only. The discovery-document probe (network) and
the ledger reads live in `app.services.inbound_auth`.
"""

import re
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, model_validator

# The service model's CustomJWTAuthorizerConfiguration.discoveryUrl pattern
# (bedrock-agentcore-control 1.43.x), plus a scheme requirement so a pasted
# hostname fails at validation rather than at the first invoke.
DISCOVERY_URL_RE = re.compile(r"^https?://.+/\.well-known/openid-configuration$")

# customClaims patterns from the same service model: inboundTokenClaimName
# `[A-Za-z0-9_.-:]+` and matchValueString `[A-Za-z0-9_.-]+` (1.43.103). A
# value outside them fails CreateAgentRuntime with a ValidationException deep
# in the deploy job — checked here so the request fails with the field name.
CLAIM_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-:]+$")
CLAIM_VALUE_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

DISCOVERY_SUFFIX = "/.well-known/openid-configuration"
# Same shape as `schemas.agent.CONNECTION_NAME_RE` (not imported: agent imports us).
SOURCE_CONNECTION_RE = r"^[a-zA-Z0-9\-_]{1,128}$"

InboundMode = Literal["iam", "jwt"]

# Methods whose agents are AgentCore *Runtimes* and can therefore carry a JWT
# authorizer. The managed Harness is invoked via InvokeHarness (SigV4 only in
# Launchpad) and a discovered resource is externally owned — both resolve to IAM
# regardless of the workspace default.
JWT_CAPABLE_METHODS = frozenset({"zip_runtime", "studio", "container", "byoc"})


class CustomClaim(BaseModel):
    """One required-claim rule of the JWT authorizer.

    Maps onto the service model's customClaims entry: {inboundTokenClaimName,
    inboundTokenClaimValueType, authorizingClaimMatchValue {claimMatchValue,
    claimMatchOperator}}.
    """

    name: str = Field(min_length=1, max_length=256)
    value_type: Literal["STRING", "STRING_ARRAY"] = "STRING"
    match_operator: Literal["EQUALS", "CONTAINS", "CONTAINS_ANY"] = "EQUALS"
    match_values: list[str] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _values_shape(self) -> "CustomClaim":
        if not CLAIM_NAME_RE.match(self.name):
            raise ValueError(
                "claim name may use only letters, digits and _ . - : characters"
            )
        for value in self.match_values:
            if not value or len(value) > 512:
                raise ValueError("each claim match value must be 1-512 characters")
            if not CLAIM_VALUE_RE.match(value):
                raise ValueError(
                    "claim match values may use only letters, digits and _ . - characters"
                )
        if self.match_operator == "EQUALS" and len(self.match_values) != 1:
            raise ValueError("match_operator=EQUALS takes exactly one match value")
        return self


class JwtInboundConfig(BaseModel):
    """The JWT authorizer half of an inbound-auth choice."""

    discovery_url: str = Field(min_length=1, max_length=1024)
    allowed_clients: list[str] = Field(default_factory=list, max_length=16)
    allowed_audience: list[str] = Field(default_factory=list, max_length=16)
    allowed_scopes: list[str] = Field(default_factory=list, max_length=16)
    custom_claims: list[CustomClaim] = Field(default_factory=list, max_length=8)
    # Display only: the Connection the console filled `discovery_url` from. It
    # never reaches `authorizer_configuration`, so it cannot change what the
    # Runtime accepts.
    source_connection: str | None = Field(default=None, pattern=SOURCE_CONNECTION_RE)

    @model_validator(mode="after")
    def _shape(self) -> "JwtInboundConfig":
        if not DISCOVERY_URL_RE.match(self.discovery_url):
            raise ValueError(
                "discovery_url must be an OpenID Connect discovery URL ending in "
                "/.well-known/openid-configuration"
            )
        for field_name in ("allowed_clients", "allowed_audience", "allowed_scopes"):
            for entry in getattr(self, field_name):
                if not entry or len(entry) > 512:
                    raise ValueError(f"each {field_name} entry must be 1-512 characters")
        if not (
            self.allowed_clients
            or self.allowed_audience
            or self.allowed_scopes
            or self.custom_claims
        ):
            # A discovery URL alone admits EVERY token the issuer ever minted;
            # requiring at least one restriction makes "open to the whole IdP"
            # impossible to configure by accident.
            raise ValueError(
                "a JWT inbound config must restrict callers: set at least one of "
                "allowed_clients, allowed_audience, allowed_scopes or custom_claims"
            )
        return self


class InboundAuth(BaseModel):
    """One inbound-auth choice: how callers must authenticate to this Runtime."""

    mode: InboundMode = "iam"
    jwt: JwtInboundConfig | None = None

    @model_validator(mode="after")
    def _jwt_required(self) -> "InboundAuth":
        if self.mode == "jwt" and self.jwt is None:
            raise ValueError("mode='jwt' requires the jwt config block")
        if self.mode == "iam" and self.jwt is not None:
            raise ValueError("the jwt config block applies to mode='jwt' only")
        return self


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

    Lenient on purpose for the read path: a settings blob written by a future
    release (or hand-edited) must not make every agent list 500 — an unreadable
    default simply behaves like no default.
    """
    if not isinstance(value, dict):
        return None
    try:
        return InboundAuth(**value)
    except ValueError:
        return None


def resolve_inbound_auth(
    spec_inbound: InboundAuth | None,
    workspace_default: InboundAuth | None,
    *,
    method: str = "",
    protocol: str = "http",
) -> InboundAuth:
    """The effective inbound auth for one agent: spec > workspace default > IAM.

    Methods that are not Runtime-backed (harness, discovered imports) and A2A
    runtimes resolve to IAM even under a JWT workspace default — they have no
    bearer invoke path in Launchpad, and inheriting a mode they cannot serve
    would deploy an agent nothing can call. An *explicit* spec-level JWT request
    for those is refused earlier, at AgentSpec validation.
    """
    if method not in JWT_CAPABLE_METHODS or protocol == "a2a":
        return IAM_INBOUND
    if spec_inbound is not None:
        return spec_inbound
    if workspace_default is not None:
        return workspace_default
    return IAM_INBOUND


def _claim_match_value(claim: CustomClaim) -> dict[str, Any]:
    if claim.match_operator == "EQUALS":
        return {"matchValueString": claim.match_values[0]}
    return {"matchValueStringList": list(claim.match_values)}


def authorizer_configuration(auth: InboundAuth) -> dict[str, Any] | None:
    """The CreateAgentRuntime/UpdateAgentRuntime authorizerConfiguration param,
    or None for IAM (the field is omitted, which is the SigV4 default)."""
    if auth.mode != "jwt" or auth.jwt is None:
        return None
    jwt = auth.jwt
    config: dict[str, Any] = {"discoveryUrl": jwt.discovery_url}
    if jwt.allowed_clients:
        config["allowedClients"] = list(jwt.allowed_clients)
    if jwt.allowed_audience:
        config["allowedAudience"] = list(jwt.allowed_audience)
    if jwt.allowed_scopes:
        config["allowedScopes"] = list(jwt.allowed_scopes)
    if jwt.custom_claims:
        config["customClaims"] = [
            {
                "inboundTokenClaimName": claim.name,
                "inboundTokenClaimValueType": claim.value_type,
                "authorizingClaimMatchValue": {
                    "claimMatchValue": _claim_match_value(claim),
                    "claimMatchOperator": claim.match_operator,
                },
            }
            for claim in jwt.custom_claims
        ]
    return {"customJWTAuthorizer": config}
