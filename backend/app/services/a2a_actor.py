"""Hub-signed A2A memory-actor metadata (N16-obs90, rev-r16 FIX-FIRST on N16-obs79).

The console tells an A2A-protocol runtime which Launchpad memory actor
(``<agent>__<human>``, ``memory_backend.scoped_actor``) a turn belongs to through the A2A
``message.metadata`` — the only part of the JSON-RPC envelope the Runtime forwards to the
container. Any caller holding ``InvokeAgentRuntime`` on that runtime (a member-authored BYOC
peer with an ``a2a`` ToolRef, ``agent_iam._a2a_peer_arns``) can send metadata too, so the
value alone must never be trusted: the hub signs it with a per-agent key the runtime holds.

* ``actor_key(agent_id)`` — derived in-process, ``HMAC-SHA256(session_secret, agent_id)``;
  nothing new is stored on disk or in the ledger. It reaches the runtime as the platform env
  key ``LAUNCHPAD_A2A_ACTOR_KEY`` at deploy (``deployer/environment.py``). A redeploy
  re-derives the same key, so only a ``session_secret`` rotation invalidates it (the next
  redeploy of each A2A agent picks the new key up).
* ``sign_actor(key, context_id, actor_id)`` — ``HMAC-SHA256(key, "<contextId>\\x1f<actor>")``
  hex, sent as ``message.metadata.launchpad_actor_sig`` next to ``launchpad_actor_id``; the
  A2A template (``strands_a2a_agent/main.py.tmpl``) binds the actor only when the signature
  verifies (``hmac.compare_digest``) AND the actor carries the agent's own ``<name>__`` prefix.
  Anything else falls back to the per-context actor ``<agent>__a2a__<contextId>``.

C4: the key and the secret never appear in logs, errors or test fixtures (tests derive from
an obviously fake secret).
"""

from __future__ import annotations

import hashlib
import hmac

ENV_A2A_ACTOR_KEY = "LAUNCHPAD_A2A_ACTOR_KEY"
METADATA_ACTOR = "launchpad_actor_id"
METADATA_SIG = "launchpad_actor_sig"
_SEP = "\x1f"


def actor_key(agent_id: str, secret: str | None = None) -> str:
    """The per-agent key (hex, 64 chars). ``secret`` defaults to the console's
    ``session_secret`` (``LAUNCHPAD_SESSION_SECRET`` / ``data/session_secret``)."""
    if secret is None:
        from app.routers.auth import session_secret  # lazy: routers import services

        secret = session_secret()
    return hmac.new(secret.encode("utf-8"), agent_id.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_actor(key: str, context_id: str, actor_id: str) -> str:
    """The signature the template verifies: bound to the context AND the actor, so a
    signature captured for one session cannot be replayed for another."""
    message = f"{context_id}{_SEP}{actor_id}".encode()
    return hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_actor(key: str, context_id: str, actor_id: str, signature: str) -> bool:
    return hmac.compare_digest(sign_actor(key, context_id, actor_id), signature)
