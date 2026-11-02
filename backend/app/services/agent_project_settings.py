"""The agent-project rule's one switch: may a new agent be filed under a `default` project?

``Settings.agent_allow_default_project`` (env ``LAUNCHPAD_AGENT_ALLOW_DEFAULT_PROJECT``,
default ``False``). Off, every creation path files a new agent under a department's
business project (``services/agent_project``); on, a workspace ``default`` project is
eligible too — without a department — for whoever holds the creation key there.

Hub-global and administrator-only. An administrator overrides it on the console: one
``platform_settings`` row (``agent_allow_default_project`` = ``true`` / ``false``);
precedence is console > env > launchpad.yaml > default, like the model settings
(``services/model_settings``). The row is applied onto the process's cached
``Settings`` at startup and on every write, so ``allowed()`` — read by the rule, the
eligible-project list and the ORM stamp guard (``core/project_scope``) — is an
attribute read, never a query. The backend runs as one uvicorn process.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings, load_yaml_config
from app.core.db import DEFAULT_WORKSPACE_ID
from app.models.platform_settings import PlatformSetting
from app.services.audit import record_audit_event

FIELD = "agent_allow_default_project"
SETTING = FIELD  # the platform_settings key is the Settings field, as for model roles
ENV = f"LAUNCHPAD_{FIELD.upper()}"
AUDIT_ACTION = "platform.agent_default_project"


def allowed() -> bool:
    """The effective switch (console > env > yaml > default) — no DB read."""
    return bool(getattr(get_settings(), FIELD, False))


def _parse(raw: str | None) -> bool | None:
    value = (raw or "").strip().lower()
    if value == "true":
        return True
    if value == "false":
        return False
    return None  # an unreadable row is no override


def _baseline() -> bool:
    """The switch as env / launchpad.yaml / the default say — no console layer."""
    return bool(getattr(Settings(), FIELD, False))


def apply_override(db: Session) -> None:
    """Put the console row (if any) onto the cached Settings; called at startup."""
    row = db.get(PlatformSetting, SETTING)
    value = _parse(row.value if row is not None else None)
    if value is not None:
        setattr(get_settings(), FIELD, value)


def _env_value() -> str | None:
    for key, value in os.environ.items():
        if key.upper() == ENV:
            return value
    return None


def _source(row: PlatformSetting | None) -> str:
    if row is not None and _parse(row.value) is not None:
        return "console"
    if _env_value() is not None:
        return "env"
    from app.core import config

    if FIELD in load_yaml_config(config.CONFIG_FILE):
        return "config"
    return "default"


def view(db: Session) -> dict[str, Any]:
    row = db.get(PlatformSetting, SETTING)
    console = _parse(row.value) if row is not None else None
    return {
        "allow_default_project": allowed(),
        "source": _source(row),
        "console_value": console,
        "env": ENV,
        "env_value": _env_value(),
        "default": bool(Settings.model_fields[FIELD].default),
        "updated_by": row.updated_by if row is not None and console is not None else None,
        "updated_at": (
            row.updated_at.isoformat()
            if row is not None and console is not None and row.updated_at else None
        ),
    }


def set_allowed(db: Session, value: bool | None, actor: Any) -> dict[str, Any]:
    """Set the console override (``None`` clears it: back to env / yaml / default),
    journal the change, commit, and apply it to the cached Settings."""
    before = allowed()
    row = db.get(PlatformSetting, SETTING)
    previous = _parse(row.value) if row is not None else None
    if value is None:
        if row is not None:
            db.delete(row)
    else:
        if row is None:
            row = PlatformSetting(key=SETTING)
            db.add(row)
        row.value = "true" if value else "false"
        row.updated_by = actor.username[:64]
        row.updated_at = datetime.now(UTC)
    effective = _baseline() if value is None else value
    if previous != value or before != effective:
        record_audit_event(
            workspace_id=DEFAULT_WORKSPACE_ID, actor=actor.username, action=AUDIT_ACTION,
            target=(
                f"{FIELD}={'unset' if value is None else str(value).lower()}"
                f" effective={str(before).lower()}->{str(effective).lower()}"
            ),
            db=db, actor_user_id=getattr(actor, "user_id", None), project_id=None,
        )
    db.commit()
    setattr(get_settings(), FIELD, effective)
    return view(db)
