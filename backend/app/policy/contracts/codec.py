"""Reference wire codec for the principal token (I1), the policy session id / session key
(§13.7) and the engine request signature (I4).

SELF-CONTAINED: stdlib + ``cryptography`` only and no ``app`` import, so the interceptor
Lambda (stream B) vendors this file verbatim and the hub (stream A) imports it — both sides
then encode and check exactly the same bytes. Contract text:
docs/cn/interfaces/policy-principal-contracts.md.

Signing is injected (``Signer``: SHA-256 digest → DER ECDSA signature), so the hub plugs in
KMS ``Sign(MessageType=DIGEST, SigningAlgorithm=ECDSA_SHA_256)`` and tests plug in a local
P-256 key; verification uses only the public key (offline, no KMS call — P0 e).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)

TOKEN_TYP = "lp-principal+jws"
TOKEN_ALG = "ES256"
TOKEN_ISSUER = "launchpad-hub"
TOKEN_VERSION = 1
MAX_TTL_S = 900
CLOCK_SKEW_S = 60
MAX_TOKEN_CHARS = 4096
CHANNELS = frozenset({"chat", "console", "v1", "assistant", "eval", "canary", "system"})
PRINCIPAL_KINDS = frozenset({"user", "api_key", "system"})
PSID_RE = re.compile(r"[A-Za-z0-9-]{1,128}\Z")
_SEP = "\x1f"

Signer = Callable[[bytes], bytes]  # sha256 digest -> DER-encoded ECDSA signature


class PrincipalTokenError(ValueError):
    """A token that must not be trusted; ``code`` is the stable reason the interceptor
    puts in its deny / audit row (contract §I1 "Verifier error codes")."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True)
class VerifiedToken:
    header: dict[str, Any]
    claims: dict[str, Any]


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]*", text):
        raise PrincipalTokenError("malformed", "non-base64url segment")
    try:  # a length % 4 == 1 segment is not base64 at all (binascii.Error)
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise PrincipalTokenError("malformed", "undecodable base64url segment") from None


def _compact_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode()


def der_to_raw(signature: bytes) -> bytes:
    """DER ECDSA (what KMS returns) → the 64-byte ``r || s`` JWS ES256 form."""
    r, s = decode_dss_signature(signature)
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def raw_to_der(signature: bytes) -> bytes:
    if len(signature) != 64:
        raise PrincipalTokenError("bad_signature", "ES256 signature must be 64 bytes")
    return encode_dss_signature(
        int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
    )


def load_public_key(spki_b64: str) -> ec.EllipticCurvePublicKey:
    """A bundle ``pubkeys`` value (base64 DER SubjectPublicKeyInfo, = KMS GetPublicKey)."""
    key = serialization.load_der_public_key(base64.b64decode(spki_b64))
    if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
        raise ValueError("principal-token keys must be EC P-256")
    return key


def public_key_spki_b64(key: ec.EllipticCurvePublicKey) -> str:
    der = key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return base64.b64encode(der).decode("ascii")


def encode_token(claims: Mapping[str, Any], *, kid: str, signer: Signer) -> str:
    """Compact JWS ES256. ``claims`` must already satisfy the I1 schema — the hub's minting
    code validates before it signs (stream A); this only frames and signs."""
    header = {"alg": TOKEN_ALG, "kid": kid, "typ": TOKEN_TYP}
    signing_input = f"{b64url(_compact_json(header))}.{b64url(_compact_json(claims))}"
    digest = hashlib.sha256(signing_input.encode("ascii")).digest()
    return f"{signing_input}.{b64url(der_to_raw(signer(digest)))}"


def decode_unverified(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Header + claims WITHOUT any trust — for diagnostics only."""
    if not isinstance(token, str) or len(token) > MAX_TOKEN_CHARS:
        raise PrincipalTokenError("malformed", "not a string or too long")
    parts = token.split(".")
    if len(parts) != 3:
        raise PrincipalTokenError("malformed", "expected three segments")
    try:
        header = json.loads(b64url_decode(parts[0]))
        claims = json.loads(b64url_decode(parts[1]))
    except PrincipalTokenError:
        raise
    except Exception as exc:
        raise PrincipalTokenError(
            "malformed", f"segment is not JSON ({type(exc).__name__})"
        ) from None
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise PrincipalTokenError("malformed", "segments must be JSON objects")
    return header, claims


def verify_token(
    token: str,
    pubkeys: Mapping[str, ec.EllipticCurvePublicKey],
    *,
    now: float,
    workspace_id: str,
    env: str,
    agent_id: str,
    max_ttl_s: int = MAX_TTL_S,
) -> VerifiedToken:
    """Every check of contract §I1, in this order (the first failure's code is returned):
    malformed → bad_header → unknown_kid → bad_signature → unsupported_version / bad_claims
    → not_yet_valid → expired → ttl_too_long → wrong_workspace → wrong_env →
    agent_not_in_audience. ``agent_id`` is the CALLING agent the interceptor resolved from
    the SigV4 ARN — never a claim."""
    header, claims = decode_unverified(token)
    kid = header.get("kid")
    if not isinstance(kid, str) or header != {"alg": TOKEN_ALG, "kid": kid, "typ": TOKEN_TYP}:
        raise PrincipalTokenError("bad_header")
    key = pubkeys.get(header["kid"])
    if key is None:
        raise PrincipalTokenError("unknown_kid", header["kid"][:64])
    signing_input, _, sig = token.rpartition(".")
    try:
        key.verify(
            raw_to_der(b64url_decode(sig)), signing_input.encode("ascii"),
            ec.ECDSA(hashes.SHA256()),
        )
    except InvalidSignature:
        raise PrincipalTokenError("bad_signature") from None
    _check_claims(claims)
    iat, exp = claims["iat"], claims["exp"]
    if iat > now + CLOCK_SKEW_S:
        raise PrincipalTokenError("not_yet_valid")
    if exp <= now - CLOCK_SKEW_S:
        raise PrincipalTokenError("expired")
    if exp - iat > max_ttl_s:
        raise PrincipalTokenError("ttl_too_long")
    if claims["ws"] != workspace_id:
        raise PrincipalTokenError("wrong_workspace")
    if claims["env"] != env:
        raise PrincipalTokenError("wrong_env")
    if agent_id not in claims["aud"]:
        raise PrincipalTokenError("agent_not_in_audience")
    return VerifiedToken(header=header, claims=claims)


def _check_claims(claims: Mapping[str, Any]) -> None:
    if claims.get("v") != TOKEN_VERSION:
        raise PrincipalTokenError("unsupported_version")
    required = {
        "iss": str, "ws": str, "env": str, "agt": str, "aud": list, "sid": str, "psid": str,
        "tsv": int, "jti": str, "iat": int, "exp": int, "ch": str, "p": dict,
    }
    for name, kind in required.items():
        value = claims.get(name)
        if not isinstance(value, kind) or isinstance(value, bool):
            raise PrincipalTokenError("bad_claims", f"{name} missing or not {kind.__name__}")
    if claims["iss"] != TOKEN_ISSUER:
        raise PrincipalTokenError("bad_claims", "iss")
    if not claims["aud"] or not all(isinstance(a, str) and a for a in claims["aud"]):
        raise PrincipalTokenError("bad_claims", "aud")
    if claims["agt"] not in claims["aud"]:
        raise PrincipalTokenError("bad_claims", "agt not in aud")
    if not PSID_RE.match(claims["psid"]):
        raise PrincipalTokenError("bad_claims", "psid")
    versions = claims.get("engine_versions")
    if "engine_versions" in claims:
        generation = claims.get("generation")
        if (not isinstance(versions, dict) or not isinstance(generation, int)
                or isinstance(generation, bool) or generation < 0):
            raise PrincipalTokenError("bad_claims", "engine_versions")
        for eid, version in versions.items():
            if (not isinstance(eid, str) or not isinstance(version, int)
                    or isinstance(version, bool) or version < 0):
                raise PrincipalTokenError("bad_claims", "engine_versions")
    if claims["ch"] not in CHANNELS:
        raise PrincipalTokenError("bad_claims", "ch")
    principal = claims["p"]
    if principal.get("kind") not in PRINCIPAL_KINDS or not isinstance(principal.get("id"), str) \
            or not principal["id"]:
        raise PrincipalTokenError("bad_claims", "p")
    if principal["kind"] == "user" and principal.get("role") not in ("admin", "member"):
        raise PrincipalTokenError("bad_claims", "p.role")
    if principal["kind"] != "user" and "role" in principal:
        raise PrincipalTokenError("bad_claims", "p.role only on users")


# ── policy session id and session key (§13.7; Global header charset) ──────────


def psid_for(workspace_id: str, runtime_session_id: str, tsv: int, generation: int = 0) -> str:
    """``hex(sha256(ws ␟ runtimeSessionId))[:64]-v<tsv>-g<gen>`` — matches Global's
    ``[A-Za-z0-9-]{1,128}``; our runtime ids may carry ``_`` so the id is hashed. A new
    temporal-set version (T4) or a hub-side generation bump (store epoch, §13.5) yields a
    new policy session for the same chat session."""
    digest = hashlib.sha256(f"{workspace_id}{_SEP}{runtime_session_id}".encode()).hexdigest()
    return f"{digest}-v{int(tsv)}-g{int(generation)}"


def session_key(workspace_id: str, psid: str, principal_kind: str, principal_id: str) -> str:
    """The engine's history key (= Global's session id + caller identity binding, T3/§13.7):
    ``sha256(ws ␟ psid ␟ kind:id)`` hex. The agent is deliberately not part of it."""
    material = f"{workspace_id}{_SEP}{psid}{_SEP}{principal_kind}:{principal_id}"
    return hashlib.sha256(material.encode()).hexdigest()


def shard_for(session: str, shards: int) -> int:
    """Owner shard of a session (§13.5 H1): first 8 hex digits mod the shard count."""
    return int(session[:8], 16) % max(1, int(shards))


def decision_id(jti: str, jsonrpc_id: str | int | None, stage: str) -> str:
    """Idempotent audit row id (32 hex = ``policy_decisions.id``) — a gateway retry of the
    same call produces the same id, so the ingester dedupes."""
    return hashlib.sha256(f"{jti}{_SEP}{jsonrpc_id}{_SEP}{stage}".encode()).hexdigest()[:32]


# ── engine request signature (I4) ─────────────────────────────────────────────


def engine_signature(key: bytes, method: str, path: str, ts: str, nonce: str, body: bytes) -> str:
    """``hex(HMAC-SHA256(key, METHOD\\nPATH\\nTS\\nNONCE\\nsha256hex(body)))``."""
    message = "\n".join(
        (method.upper(), path, ts, nonce, hashlib.sha256(body).hexdigest())
    ).encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def engine_signature_ok(
    key: bytes, method: str, path: str, ts: str, nonce: str, body: bytes, signature: str, *,
    now: float, skew_s: int = 60,
) -> bool:
    """Signature + timestamp window; the nonce replay cache is the server's (5 min)."""
    try:
        if abs(now - int(ts)) > skew_s:
            return False
    except (TypeError, ValueError):
        return False
    expected = engine_signature(key, method, path, ts, nonce, body)
    return hmac.compare_digest(expected, signature or "")
