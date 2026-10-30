"""Minting the principal token at the hub's invoke entrances (stream A2; contract I1 / I2 in
docs/cn/interfaces/policy-principal-contracts.md, design §13.1 D2 / D4 / D6, §14.2 A2).

``mint_for_turn`` is called once per turn by the one invoke chain (``services/invoke.py``),
after the invoke gate and after the Runtime session id is resolved, for every entrance that
goes through it (Chat, the console ``POST /api/agents/{id}/invoke``, ``/v1``). It returns the
token the runtime carries to the Gateway (``payload["launchpad"]["principal"]`` /
the A2A message metadata), or ``None`` — and then nothing about the request changes:

* the global switch or the workspace's ``policy_settings.gateway_interceptor`` is off
  (``policy_enforcement.interceptor_enabled``) — byte-identical requests;
* the agent is an exempt system preset (``policy_enforcement.is_exempt``);
* the caller is an agent, which acts for nobody (stage BT then sees the agent itself);
* the token cannot be minted (no key, KMS down): the turn still runs, the Gateway decides
  and denies where principal rules apply (D2). One WARN names the code — never the token.

The principal is the request's own (D6: the SENDER of a chat turn, the API key of a ``/v1``
call); a call without one is ``System::"hub"``.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import threading
import time
from collections.abc import Iterator
from typing import Any

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.core.db import SessionLocal
from app.core.errors import AppError
from app.models.ledger import Agent
from app.services import policy_enforcement, principal_token
from app.services.policy_entities import Principal

logger = logging.getLogger("launchpad.policy")

_SIGNER: principal_token.KmsSigner | None = None
_SIGNER_LOCK = threading.Lock()
# a failed key resolution (no key, DescribeKey refused / timed out) is remembered this long:
# the turns in the window mint no token without another KMS call (each one would cost the
# client's whole timeout budget on the turn's critical path)
KEY_FAILURE_TTL_S = 60.0
_KEY_FAILED_UNTIL = 0.0
_KEY_FAILURE_CODE = ""
# N17-obs493 (re-review F4): a KMS *Sign* failure is remembered per (caller path, workspace) —
# the eval / canary bulk minter's window never makes an interactive turn token-less, one
# member's failing replay never touches another workspace, and the interactive path keeps its
# own 60 s window per workspace. Key RESOLUTION (`_signer`) stays hub-global: there is one key.
PATH_TURN = "turn"  # chat / console / `/v1` / A2A turns and the hub's own Gateway calls
PATH_EVAL = "eval"  # `eval_tokens`: eval runs and canaries
_SIGN_FAILED: dict[tuple[str, str], tuple[float, str]] = {}  # (path, ws) → (until, code)

_A2A = "a2a"


def _signer() -> principal_token.KmsSigner:
    global _SIGNER, _KEY_FAILED_UNTIL, _KEY_FAILURE_CODE
    with _SIGNER_LOCK:
        if _SIGNER is not None:
            return _SIGNER
        if time.monotonic() < _KEY_FAILED_UNTIL:
            raise principal_token.PrincipalTokenUnavailable(
                _KEY_FAILURE_CODE, "the principal-token key failed to resolve recently")
        try:
            signer = principal_token.hub_signer()
            signer.kid  # noqa: B018 — resolve the key now, inside the failure window
        except principal_token.PrincipalTokenUnavailable as exc:
            _KEY_FAILED_UNTIL = time.monotonic() + KEY_FAILURE_TTL_S
            _KEY_FAILURE_CODE = exc.code
            raise
        _SIGNER = signer
        return _SIGNER


def _circuit_check(path: str = PATH_TURN, workspace_id: str = "") -> None:
    """The 60 s failure circuits (review D1 + re-review defect 1, scoped by N17-obs493): open
    after a ``PrincipalTokenUnavailable`` from resolving the key (hub-global — one key) OR from
    KMS Sign on THIS ``(path, workspace)`` — the mints in the window make no KMS call. Another
    path's or another workspace's Sign failure is never seen here."""
    now = time.monotonic()
    with _SIGNER_LOCK:
        if now < _KEY_FAILED_UNTIL:
            raise principal_token.PrincipalTokenUnavailable(
                _KEY_FAILURE_CODE, "the principal-token key failed to resolve recently")
        until, code = _SIGN_FAILED.get((path, workspace_id), (0.0, ""))
        if now < until:
            raise principal_token.PrincipalTokenUnavailable(
                code, f"the principal-token signer failed recently ({path} path, workspace "
                      f"{workspace_id or '?'})")


def _trip(code: str, *, path: str = PATH_TURN, workspace_id: str = "") -> None:
    """Open the Sign-failure circuit of ``(path, workspace)`` for ``KEY_FAILURE_TTL_S``."""
    with _SIGNER_LOCK:
        _SIGN_FAILED[(path, workspace_id)] = (time.monotonic() + KEY_FAILURE_TTL_S, code)


def circuit_open(path: str = PATH_TURN, workspace_id: str = "") -> bool:
    """Whether a mint on ``(path, workspace)`` would be refused right now (observability /
    tests; the mint paths call ``_circuit_check``)."""
    try:
        _circuit_check(path, workspace_id)
    except principal_token.PrincipalTokenUnavailable:
        return True
    return False


def _sign(claims: dict[str, Any], *, path: str = PATH_TURN) -> str:
    """``mint`` behind the failure circuits of ``(path, claims["ws"])``: checked BEFORE the
    signer is touched; a Sign failure trips that circuit — and only that one."""
    workspace_id = str(claims.get("ws") or "")
    _circuit_check(path, workspace_id)
    signer = _signer()
    try:
        return principal_token.mint(claims, signer)
    except principal_token.PrincipalTokenUnavailable as exc:
        _trip(exc.code, path=path, workspace_id=workspace_id)
        raise


def reset_signer() -> None:
    """Test seam / key rotation: the next mint resolves the configured key again; every
    failure window is closed."""
    global _SIGNER, _KEY_FAILED_UNTIL
    with _SIGNER_LOCK:
        _SIGNER = None
        _KEY_FAILED_UNTIL = 0.0
        _SIGN_FAILED.clear()


# the token's `aud` holds the agent + at most this many peers (I1 `aud` maxItems 32)
MAX_PEERS = 31
_PEER_CAP_WARNED: set[str] = set()


def _peer_ids(db: Any, agent: Agent) -> list[str]:
    """The agent's declared A2A peers (the token audience beyond the agent itself): per a2a
    ref, the ACTIVE agent of the same workspace whose Runtime ARN is ``config.runtime_arn`` —
    or, only when no agent has that ARN, the one whose id is ``config.agent_id`` (re-review
    nit a: the ARN wins when the two disagree) — never by name, which a rename or a deleted
    and re-created agent would move to another runtime. Deduped and capped at
    ``MAX_PEERS`` (one WARN per agent; the token is minted without the extras)."""
    refs: list[tuple[str, str]] = []
    for ref in (agent.spec or {}).get("tools") or []:
        if not isinstance(ref, dict) or ref.get("type") != _A2A:
            continue
        config = ref.get("config") if isinstance(ref.get("config"), dict) else {}
        arn = config.get("runtime_arn") if isinstance(config.get("runtime_arn"), str) else ""
        aid = config.get("agent_id") if isinstance(config.get("agent_id"), str) else ""
        if arn or aid:
            refs.append((arn, aid))
    if not refs:
        return []
    rows = db.query(Agent.id, Agent.arn).filter(
        Agent.workspace_id == agent.workspace_id, Agent.status == "active").all()
    by_arn = {r[1]: r[0] for r in rows if r[1]}
    ids = {r[0] for r in rows}
    peers: list[str] = []
    for arn, aid in refs:
        peer = by_arn.get(arn) if arn else None
        if peer is None and aid in ids:
            peer = aid
        if peer and peer != agent.id and peer not in peers:
            peers.append(peer)
    if len(peers) > MAX_PEERS:
        if agent.id not in _PEER_CAP_WARNED:
            _PEER_CAP_WARNED.add(agent.id)
            logger.warning("policy: agent %s declares %d a2a peers — the principal token's "
                           "audience keeps the first %d", agent.id, len(peers), MAX_PEERS)
        peers = peers[:MAX_PEERS]
    return peers


def mint_for_turn(
    agent: Agent, principal: Principal | None, *, session_id: str, channel: str,
) -> str | None:
    """The principal token of this turn, or ``None`` (see the module docstring). Never
    raises: any failure is a token-less turn and one WARN naming the code."""
    try:
        return _mint_for_turn(agent, principal, session_id=session_id, channel=channel)
    except principal_token.PrincipalTokenUnavailable as exc:
        code = exc.code
    except Exception as exc:  # noqa: BLE001 — the turn never fails on the token
        code = type(exc).__name__
    logger.warning("policy: no principal token for agent %s (%s) — the gateway decides",
                   getattr(agent, "id", "?"), code)
    return None


def _mint_for_turn(
    agent: Agent, principal: Principal | None, *, session_id: str, channel: str,
) -> str | None:
    from app.core.config import get_settings

    if not get_settings().policy_gateway_interceptor or not agent.workspace_id:
        return None
    if policy_enforcement.is_exempt(agent):
        return None
    who = principal or Principal.system()
    token_principal = principal_token.TokenPrincipal.from_policy_principal(who)
    if token_principal is None:
        return None
    db = SessionLocal()
    try:
        if not policy_enforcement.interceptor_enabled(db, agent.workspace_id):
            return None
        audience = _peer_ids(db, agent)
        tsv = temporal_set_version(db, agent.workspace_id)
        versions = engine_versions(db, agent.workspace_id, agent_ids=[agent.id, *audience])
    finally:
        db.close()
    claims = principal_token.build_claims(
        workspace_id=agent.workspace_id, agent_id=agent.id, session_id=session_id,
        tsv=tsv, channel=channel, principal=token_principal, method=agent.method,
        audience=audience, engine_versions=versions)
    return _sign(claims)


# ── re-review defect 2: one runtime session per (caller session, principal) ──────────
_IN_FLIGHT: set[tuple[str, str]] = set()
_IN_FLIGHT_LOCK = threading.Lock()
RUNTIME_SESSION_PREFIX = "lpp-"


def _principal_key(principal: Principal | None) -> str:
    who = principal or Principal.system()
    if who.kind == "user":
        return f"user:{who.attrs.get('user_id') or who.id}"
    return f"{who.kind}:{who.id}"


def _carrier_on(agent: Agent) -> bool:
    from app.core.config import get_settings

    if not get_settings().policy_gateway_interceptor or not agent.workspace_id:
        return False
    if policy_enforcement.is_exempt(agent):
        return False
    db = SessionLocal()
    try:
        return policy_enforcement.interceptor_enabled(db, agent.workspace_id)
    finally:
        db.close()


def carrier_on(agent: Agent) -> bool:
    """Whether the hub mints principal tokens for ``agent``'s turns: both switches on for its
    workspace and not an exempt preset (D5's eval / canary tokens follow the same rule)."""
    return _carrier_on(agent)


def derive_runtime_session_id(agent: Agent, principal: Principal | None,
                              session_id: str) -> str:
    """``lpp-`` + 60 hex of sha256(workspace, agent, caller session, principal): stable for
    one principal in one conversation, different for another principal (64 chars, inside
    the Runtime's 33-256)."""
    raw = "\0".join((agent.workspace_id or "", agent.id, session_id, _principal_key(principal)))
    return RUNTIME_SESSION_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:60]


def runtime_session_id(agent: Agent, principal: Principal | None, session_id: str) -> str:
    """The AgentCore Runtime session this principal's turn in ``session_id`` runs in. With the
    carrier off (or an exempt preset): ``session_id`` itself — byte-identical to before. On:
    the derived id, remembered under the caller's session (``policy_runtime_sessions``) so
    stop / trace can find it."""
    if not session_id:
        return session_id
    try:
        if not _carrier_on(agent):
            return session_id
        derived = derive_runtime_session_id(agent, principal, session_id)
        _remember(agent.id, session_id, _principal_key(principal), derived)
    except SQLAlchemyError:
        # fail closed (re-review nit): never fall back to the shared caller id — two
        # principals could then share one runtime process and its token
        raise AppError("policy.session_unavailable",
                       "the policy session store is unavailable — try again shortly",
                       {"agent_id": agent.id}, status_code=503) from None
    return derived


def _remember(agent_id: str, session_id: str, key: str, derived: str) -> None:
    from app.models.policy import PolicyRuntimeSession

    db = SessionLocal()
    try:
        if db.get(PolicyRuntimeSession, (agent_id, session_id, key)) is not None:
            return
        db.add(PolicyRuntimeSession(agent_id=agent_id, session_id=session_id, principal=key,
                                    runtime_session_id=derived))
        try:
            db.commit()
        except IntegrityError:  # a concurrent first turn recorded the same derived id
            db.rollback()
    finally:
        db.close()


def forget_agent_sessions(db: Any, agent_id: str) -> int:
    """An agent's runtime is gone (delete / purge): its derived-session records go too, in
    the caller's transaction."""
    from app.models.policy import PolicyRuntimeSession

    return db.query(PolicyRuntimeSession).filter_by(agent_id=agent_id).delete(
        synchronize_session=False)


def runtime_sessions_for(agent_id: str, session_id: str) -> list[str]:
    """Every derived runtime session recorded under the caller's ``session_id`` (empty when
    the carrier never ran a turn in it)."""
    from app.models.policy import PolicyRuntimeSession

    db = SessionLocal()
    try:
        rows = db.query(PolicyRuntimeSession.runtime_session_id).filter_by(
            agent_id=agent_id, session_id=session_id).order_by(
            PolicyRuntimeSession.created_at).all()
    finally:
        db.close()
    return [r[0] for r in rows]


def derived_session_ids(db: Any, *, agent_ids: Any = None,
                        session_ids: Any = None) -> set[str]:
    """The derived runtime sessions recorded for ``agent_ids`` and / or under the callers'
    ``session_ids`` (both given: either matches) — what the visibility rules must hide
    together with the conversation / agent they belong to. One query on the caller's
    session; empty input → empty, without a query."""
    from app.models.policy import PolicyRuntimeSession

    agent_ids = [a for a in (agent_ids or []) if a]
    session_ids = [s for s in (session_ids or []) if s]
    if not agent_ids and not session_ids:
        return set()
    clauses = []
    if agent_ids:
        clauses.append(PolicyRuntimeSession.agent_id.in_(agent_ids))
    if session_ids:
        clauses.append(PolicyRuntimeSession.session_id.in_(session_ids))
    from sqlalchemy import or_

    rows = db.query(PolicyRuntimeSession.runtime_session_id).filter(or_(*clauses)).all()
    return {r[0] for r in rows if r[0]}


@contextlib.contextmanager
def session_turn(agent: Agent, runtime_session: str) -> Iterator[None]:
    """At most one in-flight turn per (agent, RUNTIME session) while the carrier is on (→ 409
    ``policy.session_busy``): the runtime holds the token per process, so two overlapping
    turns of one principal in one session could swap tokens. A no-op otherwise."""
    if not runtime_session or not _carrier_on(agent):
        yield
        return
    slot = (agent.id, runtime_session)
    with _IN_FLIGHT_LOCK:
        if slot in _IN_FLIGHT:
            raise AppError(
                "policy.session_busy",
                "a turn is already running in this session — wait for it to finish",
                {"agent_id": agent.id}, status_code=409)
        _IN_FLIGHT.add(slot)
    try:
        yield
    finally:
        with _IN_FLIGHT_LOCK:
            _IN_FLIGHT.discard(slot)


# the hub's own Gateway client (console tool list / test call, registry reads) is no agent:
# B's interceptor takes `ws` / `agt` from the token for the hub role (`_hub_caller`)
HUB_AGENT_ID = "hub"


def mint_for_hub_call(workspace_id: str, principal: Principal | None, *,
                      gateway_id: str | None = None) -> str | None:
    """The token of one hub-side Gateway call (``services/mcp_client``): the console user
    who asked (``ch: console``), or ``System::"hub"`` for the hub's own reads (``ch:
    system``); ``None`` exactly when ``mint_for_turn`` would be (a switch off, mint failure).
    Never raises, like ``mint_for_turn``."""
    try:
        return _mint_for_hub_call(workspace_id, principal, gateway_id=gateway_id)
    except principal_token.PrincipalTokenUnavailable as exc:
        code = exc.code
    except Exception as exc:  # noqa: BLE001
        code = type(exc).__name__
    logger.warning("policy: no principal token for a hub gateway call (%s)", code)
    return None


def _mint_for_hub_call(workspace_id: str, principal: Principal | None, *,
                       gateway_id: str | None = None) -> str | None:
    from app.core.config import get_settings

    if not get_settings().policy_gateway_interceptor or not workspace_id:
        return None
    token_principal = principal_token.TokenPrincipal.from_policy_principal(
        principal or Principal.system())
    if token_principal is None:
        return None
    db = SessionLocal()
    try:
        if not policy_enforcement.interceptor_enabled(db, workspace_id):
            return None
        tsv = temporal_set_version(db, workspace_id)
        versions = engine_versions(db, workspace_id, gateway_id=gateway_id)
    finally:
        db.close()
    import uuid

    claims = principal_token.build_claims(
        workspace_id=workspace_id, agent_id=HUB_AGENT_ID, session_id=uuid.uuid4().hex,
        tsv=tsv, channel="console" if token_principal.kind == "user" else "system",
        principal=token_principal, method=None, engine_versions=versions)
    return _sign(claims)


def temporal_set_version(db: Any, workspace_id: str) -> int:
    """The temporal-set version (T4) of the engine attached to the workspace's Gateway —
    advanced by the engine install when the temporal items change (``policy_engine_sync``);
    the psid embeds it, so a temporal publish starts fresh policy sessions. 0 when the
    gateway has no engine attached."""
    from app.services import policy_engines

    engine = policy_engines.engine_for_workspace_gateway(db, workspace_id)
    return int(engine.temporal_set_version or 0) if engine is not None else 0


def engine_versions(db: Any, workspace_id: str, *, agent_ids: list[str] | None = None,
                    gateway_id: str | None = None) -> dict[str, int]:
    """Signed session epochs for the declared gateway targets, including A2A peers.

    These are session metadata, not permissions. Identity, audience, gateway IAM and policies
    still authorize each call. The attachment's trusted engine ID selects the signed epoch.
    """
    from app.models.policy import PolicyEngine, PolicyEngineAttachment
    from app.services import policy_engines

    own_gateway, _ = policy_engines.workspace_gateway(db, workspace_id)
    gateways = {gateway_id or own_gateway}
    if agent_ids:
        for agent in db.query(Agent).filter(Agent.id.in_(agent_ids),
                                           Agent.workspace_id == workspace_id).all():
            for tool in (agent.spec or {}).get("tools") or []:
                if tool.get("type") == "gateway":
                    gateways.add(str((tool.get("config") or {}).get("gateway_id") or own_gateway))
    gateways.discard("")
    return {
        engine.id: int(engine.temporal_set_version or 0)
        for engine in db.query(PolicyEngine).join(
            PolicyEngineAttachment, PolicyEngineAttachment.engine_id == PolicyEngine.id)
        .filter(PolicyEngineAttachment.gateway_id.in_(gateways)).all()
    }
