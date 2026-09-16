"""Draft/publication lifecycle with revision-checked writes and separate projections."""

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.core.errors import AppError
from app.models.announcement import Announcement


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=value.tzinfo or UTC).isoformat()


def serialize(row: Announcement, *, admin: bool = True) -> dict[str, Any]:
    if not admin:
        return {
            "id": row.id, **(row.published_content or {}),
            "published_at": _iso(row.published_at),
        }
    return {
        "id": row.id,
        "content": row.content,
        "published_content": row.published_content,
        "status": "published" if row.published_content is not None else "draft",
        "has_unpublished_changes": row.content != row.published_content,
        "revision": row.revision,
        "created_by": row.created_by,
        "updated_by": row.updated_by,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "published_at": _iso(row.published_at),
    }


def list_announcements(
    db: Session, *, admin: bool, limit: int, offset: int,
) -> dict[str, Any]:
    query = db.query(Announcement)
    if not admin:
        query = query.filter(Announcement.published_at.is_not(None))
    total = query.count()
    ordering = Announcement.updated_at if admin else Announcement.published_at
    rows = query.order_by(ordering.desc(), Announcement.id.desc()).offset(offset).limit(limit).all()
    return {"announcements": [serialize(row, admin=admin) for row in rows], "total": total}


def get_announcement(db: Session, announcement_id: str) -> Announcement:
    row = db.get(Announcement, announcement_id)
    if row is None:
        raise AppError("announcements.not_found", "Announcement not found", status_code=404)
    return row


def _conflict() -> AppError:
    return AppError(
        "announcements.conflict",
        "This announcement changed. Reload it before saving or publishing.",
        status_code=409,
    )


def check_revision(row: Announcement, expected_revision: int) -> None:
    if row.revision != expected_revision:
        raise _conflict()


def _commit(db: Session) -> None:
    try:
        db.commit()
    except StaleDataError as exc:
        db.rollback()
        raise _conflict() from exc


def create(db: Session, content: dict[str, Any], author: str) -> dict[str, Any]:
    row = Announcement(content=content, created_by=author, updated_by=author)
    db.add(row)
    _commit(db)
    return serialize(row)


def change(
    db: Session, announcement_id: str, expected_revision: int, author: str,
    *, action: str, content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = get_announcement(db, announcement_id)
    check_revision(row, expected_revision)
    now = datetime.now(UTC)
    if action == "save":
        if content is None:
            raise ValueError("Saving an announcement requires content")
        row.content = content
    elif action == "publish":
        row.published_content = deepcopy(row.content)
        row.published_at = now
    elif action == "unpublish":
        row.published_content = None
        row.published_at = None
    else:
        raise ValueError(f"Unknown announcement action: {action}")
    row.updated_by = author
    row.updated_at = now
    row.revision += 1
    _commit(db)
    return serialize(row)


def delete(db: Session, announcement_id: str, expected_revision: int) -> dict[str, bool]:
    row = get_announcement(db, announcement_id)
    check_revision(row, expected_revision)
    db.delete(row)
    _commit(db)
    return {"deleted": True}
