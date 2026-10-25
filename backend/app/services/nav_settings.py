"""The console entries an administrator hid from the V3 rail and Command center.

Hub-global, one ``platform_settings`` row (``nav_hidden_modules``) holding a sorted
JSON list of rail entry paths as the V3 rail names them before its V3 twin mapping
(``/v2/eval/experiments``, ``/v3/costs?view=project``…), the pinned entries
(``/v3``, ``/v3/gate``) and the Command center sections (``/v3?section=launch``,
``/v3?section=turn``). The backend does not know the rail's catalog — that lives in
the frontend (``v3/nav.ts``) — so it checks only the shape of an entry; an unknown
path simply never matches a rail entry.

A **view filter, not authorization**, like the business/expert nav mode: the rail,
the ⌘K palette and the Command center drop a hidden entry for every account,
administrators included. Administrator-only entries are never hidden (so an
administrator always keeps Menu settings to show an entry again), and every route
stays reachable by URL with the route policy untouched.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.db import DEFAULT_WORKSPACE_ID
from app.core.errors import AppError
from app.models.platform_settings import PlatformSetting
from app.services.audit import record_audit_event

HIDDEN_MODULES_SETTING = "nav_hidden_modules"
MAX_ENTRIES = 100
_PATH = re.compile(r"^/v[23](/[A-Za-z0-9_\-/]*)?(\?[A-Za-z0-9_\-=&]*)?$")
_MAX_PATH = 200


def hidden_modules(db: Session) -> list[str]:
    """The stored list; an absent or unreadable row reads as nothing hidden.

    The console reads an *absent* row (``view()['updated_at'] is None``) as its default
    Normal preset (``effectiveHidden`` in ``frontend/src/v3/nav.ts``); the backend keeps
    no preset of its own, so the Normal list lives in one place."""
    row = db.get(PlatformSetting, HIDDEN_MODULES_SETTING)
    if row is None or not row.value:
        return []
    try:
        doc = json.loads(row.value)
    except ValueError:
        return []
    if not isinstance(doc, list):
        return []
    return sorted({p for p in doc if isinstance(p, str) and _valid(p)})


def _valid(path: str) -> bool:
    return len(path) <= _MAX_PATH and bool(_PATH.match(path))


def view(db: Session) -> dict[str, Any]:
    row = db.get(PlatformSetting, HIDDEN_MODULES_SETTING)
    return {
        "hidden": hidden_modules(db),
        "updated_by": row.updated_by if row is not None else "",
        "updated_at": row.updated_at.isoformat() if row is not None and row.updated_at else None,
    }


def set_hidden_modules(db: Session, paths: list[str], actor: Any) -> dict[str, Any]:
    bad = [p for p in paths if not _valid(p)]
    if bad:
        raise AppError("nav.bad_path", "not a console rail path", {"paths": bad[:5]},
                       status_code=422)
    wanted = sorted(set(paths))
    if len(wanted) > MAX_ENTRIES:
        raise AppError("nav.too_many", f"at most {MAX_ENTRIES} hidden entries",
                       {"max": MAX_ENTRIES}, status_code=422)
    before = hidden_modules(db)
    row = db.get(PlatformSetting, HIDDEN_MODULES_SETTING)
    if row is None:
        row = PlatformSetting(key=HIDDEN_MODULES_SETTING)
        db.add(row)
    row.value = json.dumps(wanted)
    row.updated_by = actor.username[:64]
    row.updated_at = datetime.now(UTC)
    db.flush()
    if wanted != before:
        added = sorted(set(wanted) - set(before))
        removed = sorted(set(before) - set(wanted))
        record_audit_event(
            workspace_id=DEFAULT_WORKSPACE_ID, actor=actor.username,
            action="console.nav_hidden",
            target=f"hidden+{','.join(added) or '-'} shown+{','.join(removed) or '-'}"[:256],
            db=db, actor_user_id=getattr(actor, "user_id", None), project_id=None,
        )
    return view(db)
