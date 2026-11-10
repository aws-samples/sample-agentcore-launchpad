"""The hub's principal token (stream A1; contract I1 in
docs/cn/interfaces/policy-principal-contracts.md; design §13.1 D1 / D4 / D5 / D6, §14.2).

On every turn of a governed agent the hub mints a short-lived compact JWS ES256 naming WHO
the agent acts for; the runtime carries it to the Gateway in ``params._meta`` (I2) and the
interceptor verifies it offline against the public key in its bundle (I3). Signing is KMS
``Sign`` over the SHA-256 digest (``ECC_NIST_P256`` / ``ECDSA_SHA_256``), so the private key
never leaves KMS and only the hub role may sign; the wire framing is the shared reference
``app.policy.contracts.codec``.

* ``build_claims`` builds AND validates the claim set against
  ``principal_token_claims.schema.json`` before anything is signed.
* ``KmsSigner`` resolves the configured key (alias or id) once and refuses one that is not an
  enabled P-256 SIGN_VERIFY key; ``public_spki_b64`` is the bundle's ``pubkeys`` value.

A token is a bearer credential for the length of its TTL: it is never logged, never put into
model input, memory or a span — failures name a stable code, never the token or the digest.
Nothing here runs unless ``Settings.policy_gateway_interceptor`` and the workspace flag are on.
"""

from __future__ import annotations

import base64
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import jsonschema

from app.core.naming import current_env_name
from app.policy import contracts
from app.policy.contracts import codec

# D4: the platform's own templates (Harness, zip_runtime, Studio) run at most one synchronous
# InvokeAgentRuntime (15 min); code the platform did not write (BYOC, discovered runtimes)
# gets the shorter 5 min.
PLATFORM_METHODS = frozenset({"harness", "zip_runtime", "studio"})
SYSTEM_PRINCIPALS = frozenset({"hub", "eval", "canary"})
USER_ROLES = frozenset({"admin", "member"})


class PrincipalTokenUnavailable(RuntimeError):
    """No token can be minted. ``code``: ``policy.principal_unavailable`` (no key / KMS
    failure) or ``policy.principal_invalid`` (the claim set violates I1)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TokenPrincipal:
    """The ``p`` claim — on whose behalf the agent acts (§13.3 principals)."""

    kind: str
    id: str
    label: str
    role: str | None = None
    user_id: str | None = None

    @classmethod
    def user(cls, username: str, *, role: str, user_id: str | None = None) -> TokenPrincipal:
        if role not in USER_ROLES or not username:
            raise ValueError(f"a user principal needs a username and admin|member, got {role!r}")
        return cls("user", username, username, role, user_id)

    @classmethod
    def api_key(cls, key_id: str, *, name: str) -> TokenPrincipal:
        # D1 (revised): its own principal type, no role and no parent
        if not key_id:
            raise ValueError("an api-key principal needs the key id")
        return cls("api_key", key_id, name or key_id)

    @classmethod
    def system(cls, name: str) -> TokenPrincipal:
        # D5: online eval / canary act as System::"eval" / "canary"
        if name not in SYSTEM_PRINCIPALS:
            raise ValueError(f"unknown system principal {name!r}")
        return cls("system", name, f"system:{name}")

    @classmethod
    def from_policy_principal(cls, principal: Any) -> TokenPrincipal | None:
        """From the D20 ``policy_entities.Principal``; ``None`` for an agent principal — an
        agent acting for nobody has no on-behalf-of principal (stage BT sees the agent)."""
        if principal.kind == "user":
            return cls.user(principal.id, role=principal.role or principal.attrs.get("role"),
                            user_id=principal.attrs.get("user_id"))
        if principal.kind == "api_key":
            return cls.api_key(principal.id, name=principal.label or principal.id)
        if principal.kind == "system":
            return cls.system(principal.id)
        return None

    def claim(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "id": self.id, "label": self.label}
        if self.role is not None:
            out["role"] = self.role
        if self.user_id:
            out["user_id"] = self.user_id
        return out


def principal_for_turn(*, sender: Any, session_owner: Any | None = None) -> TokenPrincipal:
    """D6: the principal of a turn is its SENDER — an administrator continuing a member's
    session acts as the administrator, never as the session owner (``session_owner`` is
    accepted only so call sites state both and cannot swap them)."""
    del session_owner
    return TokenPrincipal.user(sender.username, role=sender.role,
                               user_id=getattr(sender, "user_id", None))


def ttl_for(method: str | None) -> int:
    return contracts.TOKEN_TTL_S if method in PLATFORM_METHODS else contracts.TOKEN_TTL_BYOC_S


def _claims_validator() -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(contracts.load_schema("principal_token_claims"))


def build_claims(
    *,
    workspace_id: str,
    agent_id: str,
    session_id: str,
    tsv: int,
    channel: str,
    principal: TokenPrincipal,
    method: str | None,
    generation: int = 0,
    audience: Iterable[str] = (),
    engine_versions: dict[str, int] | None = None,
    now: float | None = None,
    jti: str | None = None,
) -> dict[str, Any]:
    """The I1 claim set of one turn. ``audience`` adds the agent's declared A2A peers (the
    invoked agent always comes first); ``psid`` = ``codec.psid_for(ws, sid, tsv, gen)``."""
    iat = int(time.time() if now is None else now)
    aud = [agent_id] + [a for a in dict.fromkeys(audience) if a and a != agent_id]
    claims = {
        "iss": contracts.TOKEN_ISSUER, "v": codec.TOKEN_VERSION,
        "ws": workspace_id, "env": current_env_name(),
        "agt": agent_id, "aud": aud,
        "sid": session_id,
        "psid": codec.psid_for(workspace_id, session_id, tsv, generation),
        "tsv": int(tsv),
        "jti": jti or uuid.uuid4().hex,
        "iat": iat, "exp": iat + ttl_for(method),
        "ch": channel,
        "p": principal.claim(),
    }
    if engine_versions is not None:
        claims["engine_versions"] = {eid: int(version) for eid, version in engine_versions.items()}
        claims["generation"] = generation
    errors = sorted(_claims_validator().iter_errors(claims), key=lambda e: e.path)
    if errors:
        # the claim NAMES are safe to report; values may identify a user
        fields = sorted({".".join(str(p) for p in e.path) or e.validator for e in errors})
        raise PrincipalTokenUnavailable(
            "policy.principal_invalid", f"principal token claims violate I1: {fields}")
    return claims


class KmsSigner:
    """``codec.Signer`` over KMS. ``key`` is the configured key id / ARN / alias
    (``Settings.policy_principal_kms_key_id``); the resolved KeyId is the JWS ``kid``."""

    def __init__(self, kms: Any, key: str) -> None:
        if not key:
            raise PrincipalTokenUnavailable(
                "policy.principal_unavailable", "no principal-token KMS key is configured")
        self._kms = kms
        self._key = key
        self._kid: str | None = None
        self._spki: str | None = None

    @property
    def kid(self) -> str:
        if self._kid is None:
            try:
                meta = self._kms.describe_key(KeyId=self._key)["KeyMetadata"]
            except Exception as exc:  # noqa: BLE001 — a stable code, never the cause's payload
                raise PrincipalTokenUnavailable(
                    "policy.principal_unavailable",
                    f"principal-token key not readable ({type(exc).__name__})") from None
            if (meta.get("KeySpec") != "ECC_NIST_P256" or meta.get("KeyUsage") != "SIGN_VERIFY"
                    or not meta.get("Enabled", False)):
                raise PrincipalTokenUnavailable(
                    "policy.principal_unavailable",
                    "the principal-token key must be an enabled ECC_NIST_P256 SIGN_VERIFY key")
            self._kid = str(meta["KeyId"])
        return self._kid

    def sign_digest(self, digest: bytes) -> bytes:
        try:
            out = self._kms.sign(KeyId=self.kid, Message=digest, MessageType="DIGEST",
                                 SigningAlgorithm="ECDSA_SHA_256")
        except PrincipalTokenUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PrincipalTokenUnavailable(
                "policy.principal_unavailable",
                f"KMS Sign failed ({type(exc).__name__})") from None
        return bytes(out["Signature"])

    def public_spki_b64(self) -> str:
        """Base64 DER SubjectPublicKeyInfo — the bundle's ``pubkeys[kid]`` (I3)."""
        if self._spki is None:
            der = self._kms.get_public_key(KeyId=self.kid)["PublicKey"]
            spki = base64.b64encode(bytes(der)).decode("ascii")
            codec.load_public_key(spki)  # refuses anything but P-256
            self._spki = spki
        return self._spki


def mint(claims: dict[str, Any], signer: KmsSigner) -> str:
    """The compact JWS of an already-validated claim set (``build_claims``)."""
    token = codec.encode_token(claims, kid=signer.kid, signer=signer.sign_digest)
    if len(token) > codec.MAX_TOKEN_CHARS:
        raise PrincipalTokenUnavailable("policy.principal_invalid",
                                        "principal token exceeds the wire size limit")
    return token


def hub_signer() -> KmsSigner:
    """The hub's signer: the configured key in the hub's own account / region (the default
    workspace context — the client is built by ``aws_clients`` like every other, with the
    bounded ``PRINCIPAL_KMS_CONFIG``).

    The key is hub-global by design (one issuer for every workspace's tokens; the Gateway
    interceptor trusts one ``pubkeys`` set): this is the ONLY ``default_workspace_context``
    use in this module, and ``tests/test_workspace_funnel.py`` pins it to this function."""
    from app.core.config import get_settings
    from app.services import aws_clients
    from app.services.workspace import default_workspace_context

    key = get_settings().policy_principal_kms_key_id
    if not key:
        raise PrincipalTokenUnavailable(
            "policy.principal_unavailable", "no principal-token KMS key is configured")
    return KmsSigner(aws_clients.principal_kms_client(default_workspace_context()), key)
