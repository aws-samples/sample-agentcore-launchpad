"""The immutable principal an assistant conversation belongs to.

Usernames are display names: an account can be deleted and the same username
registered again under a new ``users.id``. Ownership therefore binds to
``user:<users.id>`` for registered accounts, to the explicit stable
``config-admin`` for the built-in (row-less) administrator, and to
``local-operator`` when the login gate is off. A stored NULL principal (legacy row)
matches nobody.
"""

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
