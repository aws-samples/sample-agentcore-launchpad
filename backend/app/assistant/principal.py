"""The immutable principal an assistant conversation belongs to.

Usernames are display names: an account can be deleted and the same username
registered again under a new ``users.id``. Ownership therefore binds to
``user:<users.id>`` for registered accounts, to the explicit stable
``config-admin`` for the built-in (row-less) administrator, and to
``local-operator`` when the login gate is off. A stored NULL principal (legacy row)
matches nobody.

Who may *work on* a conversation is wider than who owns it: while an admin shares
it, every principal collaborates (``may_collaborate`` / ``collaborator_clause``).
"""

from typing import Any

from sqlalchemy import and_, or_

from app.models.assistant import AssistantConversation
from app.routers.auth import Identity
from app.routers.auth import enabled as auth_enabled

CONFIG_ADMIN = "config-admin"
LOCAL_OPERATOR = "local-operator"


def principal_of(identity: Identity) -> str:
    if not auth_enabled():
        return LOCAL_OPERATOR
    if identity.user_id:
        return f"user:{identity.user_id}"
    return CONFIG_ADMIN


def may_collaborate(row: AssistantConversation | None, principal: str) -> bool:
    """The owner, or anyone while an admin shares the conversation. A legacy row with
    no principal is closed to everybody, shared or not."""
    return (
        row is not None
        and row.owner_principal is not None
        and (row.owner_principal == principal or bool(row.shared))
    )


def collaborator_clause(principal: str) -> Any:
    """``may_collaborate`` as a SQL predicate on ``assistant_conversations``, for the
    conditional writes that must re-check it inside the statement itself."""
    return and_(
        AssistantConversation.owner_principal.is_not(None),
        or_(AssistantConversation.owner_principal == principal,
            AssistantConversation.shared.is_(True)),
    )
