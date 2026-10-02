"""as_user (3LO) consent sessions, grants and revocations.

**The four-leg contract (live-measured 2026-09-20, us-west-2; re-proved by the
P2 e2e).** ``GetResourceOauth2Token(oauth2Flow="USER_FEDERATION")`` answers
``{authorizationUrl, sessionUri}``; the user consents at the IdP and AgentCore
Identity redirects the browser to the allow-listed return URL with
``?session_id=<sessionUri>``; the token is NOT bound until the platform calls

    CompleteResourceTokenAuth(userIdentifier={"userId": <user>}, sessionUri=…)

after which the ordinary call returns ``{accessToken}``.

**Who says which user.** The ``sessionUri`` travels through the browser — it is
a one-time unguessable capability, not an identity. The user id comes from the
``oauth_pending_sessions`` row written when the platform forwarded the
``auth_required`` ask to that caller, and ``complete`` additionally requires the
signed-in caller to BE that user: a leaked return link cannot bind somebody
else's consent into the victim's session, nor the other way round.

**Revocation.** The service models no revoke operation, so revoke is
"force re-auth on the next call": a ``user_token_revocations`` row makes every
invoke for that (user, Connection) send ``forceAuthentication=true``, which
restarts consent and replaces the vault binding. The token vault is keyed per
workload identity, i.e. per agent, so the revocation stays in force for an
agent until THAT agent's grant is re-authorized after the revocation — a turn
that never called the tool, or a re-consent on another agent, cannot silently
cancel it.

**Grants** (``user_grants``) are derived progress only: the vault exposes no
"is a token bound" read, so the row records what the platform observed on its
own legs (pending → authorized → revoked).
"""

import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.models.ledger import OauthPendingSession, UserGrant, UserTokenRevocation

logger = logging.getLogger(__name__)

# How long a recorded consent session may still be redeemed. The IdP request
# behind it is itself short-lived; this bounds the ledger and the replay window.
SESSION_TTL_S = 900

GRANT_STATUSES = ("pending", "authorized", "revoked")

# Who the Runtime saw as the caller of the turn that asked for consent (P3).
# IAM runtimes key the vault user on runtimeUserId (the Launchpad username);
# JWT-inbound runtimes key it on the inbound JWT (iss + sub), so the binding
# leg must present ``userToken`` — a fresh JWT of the same subject.
CALLER_IAM = "iam"
CALLER_USER_JWT = "user_jwt"
CALLER_M2M = "m2m"
CALLER_KINDS = (CALLER_IAM, CALLER_USER_JWT, CALLER_M2M)

# An as_user ask raised on a JWT-inbound agent by a call that presented the
# workspace M2M token. The vault would key that consent on the ONE machine
# subject every M2M caller shares (Chat with "invoke as me" off, every /v1 key
# holder, direct invoke, evaluation runs), so completing it would hand one
# member's downstream token to all of them. Such an ask is never recorded and
# such a session is never completed.
AS_USER_REQUIRES_USER_JWT = "identity.as_user_requires_user_jwt"


def as_user_requires_user_jwt(
    *, provider: str = "", tool: str = "", agent_id: str = "", console: bool = False
) -> AppError:
    """The named refusal for an as_user consent over the shared M2M subject."""
    if console:
        message = (
            "this tool acts as you, and this turn called the agent with the "
            "workspace machine token — turn on \"invoke as me\" and retry"
        )
    else:
        message = (
            "as_user tools on a JWT-inbound agent need a user JWT — this call "
            "presented the workspace machine (M2M) token, whose subject every "
            "M2M caller shares, so no consent is started for it"
        )
    return AppError(
        AS_USER_REQUIRES_USER_JWT,
        message,
        {"provider": provider, "tool": tool, "agent_id": agent_id, "caller": CALLER_M2M},
        status_code=409,
    )


def _aware(value: datetime | None) -> datetime | None:
    """SQLite drops tzinfo on the way back; every stored time is UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _now() -> datetime:
    return datetime.now(UTC)


def _grant(
    db: Session, workspace_id: str, user_id: str, provider: str, agent_id: str
) -> UserGrant | None:
    return (
        db.query(UserGrant)
        .filter(
            UserGrant.workspace_id == workspace_id,
            UserGrant.user_id == user_id,
            UserGrant.provider == provider,
            UserGrant.agent_id == agent_id,
        )
        .first()
    )


def _revocation(
    db: Session, workspace_id: str, user_id: str, provider: str
) -> UserTokenRevocation | None:
    return (
        db.query(UserTokenRevocation)
        .filter(
            UserTokenRevocation.workspace_id == workspace_id,
            UserTokenRevocation.user_id == user_id,
            UserTokenRevocation.provider == provider,
        )
        .first()
    )


def _revocation_in_force(revocation: UserTokenRevocation | None, grant: UserGrant | None) -> bool:
    """A revocation binds an agent until that agent re-authorizes after it."""
    if revocation is None:
        return False
    authorized = _aware(grant.authorized_at) if grant is not None else None
    requested = _aware(revocation.requested_at)
    return authorized is None or requested is None or authorized < requested


def pending_revocations(
    db: Session,
    workspace_id: str,
    *,
    user_id: str,
    agent_id: str,
    providers: Iterable[str],
) -> list[str]:
    """Connections whose next exchange on ``agent_id`` must force re-auth."""
    if not user_id:
        return []
    names = sorted({str(p) for p in providers if p})
    if not names:
        return []
    revocations = {
        row.provider: row
        for row in db.query(UserTokenRevocation).filter(
            UserTokenRevocation.workspace_id == workspace_id,
            UserTokenRevocation.user_id == user_id,
            UserTokenRevocation.provider.in_(names),
        )
    }
    return [
        name
        for name in names
        if _revocation_in_force(
            revocations.get(name), _grant(db, workspace_id, user_id, name, agent_id)
        )
    ]


def record_pending(
    db: Session,
    workspace_id: str,
    *,
    session_uri: str,
    provider: str,
    user_id: str,
    agent_id: str = "",
    tool: str = "",
    scopes: Iterable[str] = (),
    caller_kind: str = CALLER_IAM,
) -> None:
    """Remember which user a forwarded ``auth_required`` ask belongs to, and
    mark that user's grant pending.

    Best-effort on purpose: a failure here must not break the turn (the user
    still sees the Authorize card, and a retry mints a new session).

    An ask over the shared M2M subject is never recorded (see
    ``AS_USER_REQUIRES_USER_JWT``); the invoke layer refuses it before this.
    """
    if not (session_uri and provider and user_id) or caller_kind == CALLER_M2M:
        return
    try:
        existing = (
            db.query(OauthPendingSession)
            .filter(
                OauthPendingSession.workspace_id == workspace_id,
                OauthPendingSession.session_uri == session_uri,
            )
            .first()
        )
        if existing is None:
            db.add(
                OauthPendingSession(
                    workspace_id=workspace_id,
                    session_uri=session_uri,
                    provider=provider,
                    user_id=user_id,
                    agent_id=agent_id,
                    tool=tool,
                    caller_kind=caller_kind if caller_kind in CALLER_KINDS else CALLER_IAM,
                )
            )
        grant = _grant(db, workspace_id, user_id, provider, agent_id)
        if grant is None:
            grant = UserGrant(
                workspace_id=workspace_id,
                user_id=user_id,
                provider=provider,
                agent_id=agent_id,
            )
            db.add(grant)
        grant.status = "pending"
        grant.tool = tool or grant.tool or ""
        grant.scopes = [str(s) for s in scopes] or list(grant.scopes or [])
        grant.updated_at = _now()
        db.commit()
    except Exception:  # noqa: BLE001 — never fail a chat turn over bookkeeping
        db.rollback()
        logger.warning("could not record the 3LO consent session", exc_info=True)


def _expired(row: OauthPendingSession) -> bool:
    created = _aware(row.created_at)
    if created is None:
        return False
    return _now() - created > timedelta(seconds=SESSION_TTL_S)


def _user_identifier(
    row: OauthPendingSession, user_token: Callable[[str], str | None] | None
) -> dict[str, str]:
    """``CompleteResourceTokenAuth.userIdentifier`` for one recorded session.

    The service model's union is ``{userToken | userId}``. A session the
    Runtime asked under a JWT authorizer belongs to the JWT subject, so it is
    completed with a fresh token of that subject — never the bare username,
    which would bind the consent to an identity the runtime never uses.
    """
    kind = getattr(row, "caller_kind", None) or CALLER_IAM
    if kind == CALLER_IAM:
        return {"userId": row.user_id}
    if kind != CALLER_USER_JWT:
        # checked by ``complete`` before this; never present the M2M token
        raise as_user_requires_user_jwt(provider=row.provider, tool=row.tool or "")
    token = user_token(kind) if user_token is not None else None
    if not token:
        raise AppError(
            "identity.session_token_unavailable",
            "this authorization was started over a JWT-inbound agent, and no "
            "token of the same caller could be minted to complete it — sign in "
            "to the console with its user pool, or retry the request",
            {"provider": row.provider, "caller_kind": kind},
            status_code=409,
        )
    return {"userToken": token}


def complete(
    db: Session,
    workspace_id: str,
    session_uri: str,
    *,
    caller: str,
    data_client: Any,
    user_token: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """Bind the consented token to its recorded user (leg 3), then mark the
    grant authorized and drop the session row.

    ``caller`` is the signed-in console user; it must equal the recorded user.
    ``data_client`` is the workspace's ``bedrock-agentcore`` data-plane client
    (injected so tests need no AWS). ``user_token`` mints the JWT a session
    asked over a JWT-inbound runtime must be completed with (called with the
    recorded caller kind); an IAM-asked session completes with ``userId``.
    A session asked over the shared M2M subject (a row written before that ask
    was refused) is dropped with 409 and never reaches the identity service.
    """
    row = (
        db.query(OauthPendingSession)
        .filter(
            OauthPendingSession.workspace_id == workspace_id,
            OauthPendingSession.session_uri == session_uri,
        )
        .first()
    )
    if row is None:
        raise NotFoundError(
            "identity.session_unknown",
            "this authorization session is unknown or has already been "
            "completed — go back to Chat and retry the request",
        )
    if row.user_id != caller:
        # Deliberately no detail: neither user id is echoed.
        raise AppError(
            "identity.session_user_mismatch",
            "this authorization was started by a different user — sign in as "
            "that user, or retry the request from your own Chat",
            status_code=403,
        )
    if _expired(row):
        db.delete(row)
        db.commit()
        raise AppError(
            "identity.session_expired",
            "this authorization session has expired — go back to Chat and "
            "retry the request to start a new one",
            {"provider": row.provider},
            status_code=409,
        )
    provider, agent_id, tool = row.provider, row.agent_id, row.tool
    if row.caller_kind == CALLER_M2M:
        db.delete(row)
        db.commit()
        raise as_user_requires_user_jwt(
            provider=provider, tool=tool or "", agent_id=agent_id or "", console=True
        )
    identifier = _user_identifier(row, user_token)
    try:
        data_client.complete_resource_token_auth(
            userIdentifier=identifier, sessionUri=session_uri
        )
    except Exception as exc:  # noqa: BLE001 — the reason is for the operator
        logger.warning(
            "CompleteResourceTokenAuth failed for provider %s: %s",
            provider,
            type(exc).__name__,
        )
        raise AppError(
            "identity.session_completion_failed",
            "the identity service could not complete this authorization "
            f"({type(exc).__name__}) — go back to Chat and retry the request",
            {"provider": provider},
            status_code=502,
        ) from exc
    now = _now()
    grant = _grant(db, workspace_id, row.user_id, provider, agent_id)
    if grant is None:
        grant = UserGrant(
            workspace_id=workspace_id,
            user_id=row.user_id,
            provider=provider,
            agent_id=agent_id,
            tool=tool,
            scopes=[],
        )
        db.add(grant)
    grant.status = "authorized"
    grant.authorized_at = now
    grant.updated_at = now
    db.delete(row)
    db.commit()
    return {"completed": True, "provider": provider, "agent_id": agent_id, "tool": tool}


def revoke(db: Session, workspace_id: str, *, user_id: str, provider: str) -> dict[str, Any]:
    """Force re-auth on the next call for every agent holding this grant."""
    now = _now()
    revocation = _revocation(db, workspace_id, user_id, provider)
    if revocation is None:
        db.add(
            UserTokenRevocation(
                workspace_id=workspace_id, provider=provider, user_id=user_id, requested_at=now
            )
        )
    else:
        revocation.requested_at = now
    grants = (
        db.query(UserGrant)
        .filter(
            UserGrant.workspace_id == workspace_id,
            UserGrant.user_id == user_id,
            UserGrant.provider == provider,
        )
        .all()
    )
    for grant in grants:
        grant.status = "revoked"
        grant.revoked_at = now
        grant.updated_at = now
    # in-flight consents for the revoked Connection must not bind after the fact
    db.query(OauthPendingSession).filter(
        OauthPendingSession.workspace_id == workspace_id,
        OauthPendingSession.user_id == user_id,
        OauthPendingSession.provider == provider,
    ).delete(synchronize_session=False)
    db.commit()
    return {"revoked": True, "provider": provider, "agents": len(grants)}


def _grant_view(grant: UserGrant, revocation: UserTokenRevocation | None) -> dict[str, Any]:
    def stamp(value: datetime | None) -> str | None:
        aware = _aware(value)
        return aware.isoformat() if aware else None

    return {
        "connection": grant.provider,
        "agent_id": grant.agent_id,
        "tool": grant.tool,
        "scopes": list(grant.scopes or []),
        "status": grant.status,
        "force_reauth": _revocation_in_force(revocation, grant),
        "created_at": stamp(grant.created_at),
        "updated_at": stamp(grant.updated_at),
        "authorized_at": stamp(grant.authorized_at),
        "revoked_at": stamp(grant.revoked_at),
    }


def list_grants(db: Session, workspace_id: str, *, user_id: str) -> list[dict[str, Any]]:
    """The caller's own grants, newest first. Never another user's."""
    rows = (
        db.query(UserGrant)
        .filter(UserGrant.workspace_id == workspace_id, UserGrant.user_id == user_id)
        .order_by(UserGrant.updated_at.desc())
        .all()
    )
    revocations = {
        row.provider: row
        for row in db.query(UserTokenRevocation).filter(
            UserTokenRevocation.workspace_id == workspace_id,
            UserTokenRevocation.user_id == user_id,
        )
    }
    return [_grant_view(row, revocations.get(row.provider)) for row in rows]


def grant_status(
    db: Session, workspace_id: str, *, user_id: str, provider: str, agent_id: str
) -> dict[str, Any]:
    """What the Chat auth card polls: ``none`` until an ask was recorded."""
    grant = _grant(db, workspace_id, user_id, provider, agent_id)
    if grant is None:
        return {
            "connection": provider,
            "agent_id": agent_id,
            "status": "none",
            "force_reauth": _revocation_in_force(
                _revocation(db, workspace_id, user_id, provider), None
            ),
            "authorized_at": None,
        }
    view = _grant_view(grant, _revocation(db, workspace_id, user_id, provider))
    keys = ("connection", "agent_id", "status", "force_reauth", "authorized_at")
    return {key: view[key] for key in keys}
