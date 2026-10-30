"""Stage-0 fakes shared by streams A (hub), B (interceptor) and C (engine) — contract:
docs/cn/interfaces/policy-principal-contracts.md.

* ``EphemeralSigner`` — an ES256 key pair generated in memory per test session; nothing is
  ever written to disk or committed. ``sign_digest`` has the KMS ``Sign(DIGEST)`` shape
  (sha256 digest in, DER signature out), so the hub's real signer swaps in unchanged.
* ``FakeEngine`` — an in-process stand-in for the engine service implementing the I4 data
  plane (decide / respond / list-filter / healthz, HMAC-checked) and the I5 control plane
  (install / authorize_stateless / status). Decisions come from a pluggable rule function,
  NOT from Dogwood — it exists so B and A can test routing, fail modes and audit shapes
  without the Rust service. Fault switches: ``down``, ``slow_s``, ``force_error``.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed

from app.policy.contracts import codec


class EphemeralSigner:
    """A throwaway P-256 key standing in for the KMS key (never persisted)."""

    def __init__(self, kid: str = "kid-test-0001") -> None:
        self.kid = kid
        self._key = ec.generate_private_key(ec.SECP256R1())

    def sign_digest(self, digest: bytes) -> bytes:
        return self._key.sign(digest, ec.ECDSA(Prehashed(hashes.SHA256())))

    @property
    def public_key(self) -> ec.EllipticCurvePublicKey:
        return self._key.public_key()

    @property
    def public_spki_b64(self) -> str:
        return codec.public_key_spki_b64(self.public_key)

    def pubkeys(self) -> dict[str, ec.EllipticCurvePublicKey]:
        return {self.kid: self.public_key}

    def token(self, claims: dict[str, Any]) -> str:
        return codec.encode_token(claims, kid=self.kid, signer=self.sign_digest)


def ephemeral_hmac_key() -> bytes:
    return secrets.token_bytes(32)


def signed_headers(key: bytes, method: str, path: str, body: bytes, *, now: float | None = None,
                   nonce: str | None = None) -> dict[str, str]:
    """The I4 authentication headers a client (the interceptor) sends."""
    ts = str(int(time.time() if now is None else now))
    nonce = nonce or uuid.uuid4().hex
    return {
        "X-LP-Ts": ts,
        "X-LP-Nonce": nonce,
        "X-LP-Sig": codec.engine_signature(key, method, path, ts, nonce, body),
    }


Rule = Callable[[dict[str, Any]], tuple[str, str]]  # decide body -> (stage_a, stage_bt)


def allow_all(_body: dict[str, Any]) -> tuple[str, str]:
    return "allow", "allow"


class FakeEngineDown(ConnectionError):
    """Raised by every FakeEngine call while ``down`` is set (the socket is unreachable)."""


@dataclass
class FakeEngine:
    key: bytes
    rule: Rule = allow_all
    shard: int = 0
    store_epoch: str = "e1"
    down: bool = False
    slow_s: float = 0.0
    force_error: dict[str, Any] | None = None  # e.g. {"status": 409, "code": "session_invalidated"}
    sets: dict[str, str] = field(default_factory=dict)  # ws -> policy_set_version
    events: list[dict[str, Any]] = field(default_factory=list)
    _seq: int = 0
    _nonces: set[str] = field(default_factory=set)

    # ── I4 data plane ───────────────────────────────────────────────────
    def handle(self, method: str, path: str, body: bytes, headers: dict[str, str],
               *, now: float | None = None) -> tuple[int, dict[str, Any]]:
        """One HTTP exchange: ``(status, json body)``."""
        if self.down:
            raise FakeEngineDown("engine unreachable")
        if self.slow_s:
            time.sleep(self.slow_s)
        if path == "/healthz" and method == "GET":
            return 200, {"ready": True, "shard": self.shard, "store_epoch": self.store_epoch,
                         "sets": dict(self.sets)}
        now = time.time() if now is None else now
        nonce = headers.get("X-LP-Nonce", "")
        if nonce in self._nonces or not codec.engine_signature_ok(
            self.key, method, path, headers.get("X-LP-Ts", ""), nonce, body,
            headers.get("X-LP-Sig", ""), now=now,
        ):
            return 401, {"code": "unauthorized"}
        self._nonces.add(nonce)
        if self.force_error:
            err = dict(self.force_error)
            return int(err.pop("status", 409)), err
        try:
            payload = json.loads(body)
        except ValueError:
            return 400, {"code": "bad_request"}
        if path == "/v1/decide":
            return 200, self._decide(payload)
        if path == "/v1/respond":
            return 200, self._record(payload["session"], payload["action"], payload["kind"])
        if path == "/v1/list-filter":
            visible = [a for a in payload["actions"]
                       if self.rule({**payload, "action": a, "context": {"input": {}}}) ==
                       ("allow", "allow")]
            return 200, {"visible": visible}
        return 400, {"code": "bad_request"}

    def _record(self, session: str, action: str, kind: str, verdict: str | None = None) -> dict:
        self._seq += 1
        ts_ns = time.time_ns()
        self.events.append({"event_seq": self._seq, "ts_ns": ts_ns, "session": session,
                            "action": action, "kind": kind, "verdict": verdict})
        return {"event_seq": self._seq, "ts_ns": ts_ns}

    def _decide(self, body: dict[str, Any]) -> dict[str, Any]:
        stage_a, stage_bt = self.rule(body)
        decision = "allow" if (stage_a, stage_bt) == ("allow", "allow") else "deny"
        seq = ts = None
        if body.get("record"):
            rec = self._record(body["session"], body["action"], "request", decision)
            seq, ts = rec["event_seq"], rec["ts_ns"]
        return {"decision": decision, "stage_a": stage_a, "stage_bt": stage_bt,
                "determining": [], "log_only_would": None, "event_seq": seq, "ts_ns": ts,
                "store_epoch": self.store_epoch,
                "policy_set_version": self.sets.get(body["ws"], "ps-0"), "errors": []}

    # ── I5 control plane ───────────────────────────────────────────────
    def control(self, message: dict[str, Any]) -> dict[str, Any]:
        if self.down:
            raise FakeEngineDown("control socket unreachable")
        op = message.get("op")
        if op == "install":
            current = self.sets.get(message["ws"])
            if current is not None and not message.get("force") and \
                    not version_is_newer(current, message["policy_set_version"]):
                return {"ok": False, "errors": [
                    f"policy_set_version {message['policy_set_version']} is not newer than the "
                    f"installed {current} (force: true to re-install or roll back)"]}
            self.sets[message["ws"]] = message["policy_set_version"]
            return {"ok": True, "errors": []}
        if op == "authorize_stateless":
            return {"ok": True, "decisions": [
                self._decide({**r, "record": False}) for r in message["requests"]
            ]}
        if op == "status":
            return {"ok": True, "shards": [{"shard": self.shard, "ready": True,
                                            "store_epoch": self.store_epoch,
                                            "sets": dict(self.sets)}]}
        return {"ok": False, "errors": [f"unknown op {op!r}"]}


def body_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def version_is_newer(installed: str, candidate: str) -> bool:
    """The engine's `<prefix>-<n>` ordering (engine.rs `version_is_newer`, N17-obs492):
    newer when the prefixes match and n grows; another prefix is newer; otherwise only when
    they differ."""
    def split(v: str):
        prefix, _, n = v.rpartition("-")
        return (prefix, int(n)) if prefix and n.isdigit() else None

    a, b = split(installed), split(candidate)
    if a and b and a[0] == b[0]:
        return b[1] > a[1]
    return installed != candidate
