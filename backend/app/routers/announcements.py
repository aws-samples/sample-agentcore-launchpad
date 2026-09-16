"""Global announcement reads and admin publication, authorized in route_policy."""

import re
from typing import Any
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.routers.auth import Identity, require_identity
from app.services import announcements

router = APIRouter(prefix="/api/announcements", tags=["announcements"])


def _author(request: Request) -> Identity:
    # require_identity also accepts Settings for internal callers; exposing it
    # directly as a dependency would accidentally add a second request body.
    return require_identity(request)


class AnnouncementContent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=1, max_length=6000)
    link_url: str | None = Field(default=None, max_length=2048)
    link_label: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("link_url")
    @classmethod
    def safe_link(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # Match the console's decodeURIComponent guard: malformed escapes and
        # encoded controls/backslashes must not become saved but unrenderable links.
        if re.search(r"%(?![0-9a-fA-F]{2})", value):
            raise ValueError("link contains an invalid percent escape")
        decoded = unquote(value, errors="strict")
        if (
            not value or "\\" in decoded or any(ch.isspace() for ch in value)
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in decoded)
        ):
            raise ValueError("link must be an app-relative path or an HTTPS URL")
        parsed = urlsplit(value)
        if value.startswith("/") and not decoded.startswith("//") and not parsed.netloc:
            return value
        if (
            not value.lower().startswith("https://")
            or parsed.scheme != "https" or not parsed.hostname
            or parsed.username or parsed.password
        ):
            raise ValueError(
                "link must be an app-relative path or an HTTPS URL without credentials"
            )
        # urlsplit alone accepts hosts that a browser's URL parser rejects.
        # Validate host/port semantics without replacing the author's URL with
        # the normalized representation (for example, adding a trailing slash).
        HttpUrl(value)
        return value

    @model_validator(mode="after")
    def link_has_label(self) -> "AnnouncementContent":
        if (self.link_url is None) != (self.link_label is None):
            raise ValueError("link_url and link_label must be provided together")
        return self


class RevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1, strict=True)


class AnnouncementEdit(AnnouncementContent):
    expected_revision: int = Field(ge=1, strict=True)


@router.get("")
def published(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return announcements.list_announcements(db, admin=False, limit=limit, offset=offset)


@router.get("/manage")
def managed(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return announcements.list_announcements(db, admin=True, limit=limit, offset=offset)


@router.get("/{announcement_id}")
def detail(announcement_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    return announcements.serialize(announcements.get_announcement(db, announcement_id))


@router.post("", status_code=201)
def create(
    content: AnnouncementContent,
    db: Session = Depends(get_db),
    author: Identity = Depends(_author),
) -> dict[str, Any]:
    return announcements.create(db, content.model_dump(), author.username)


@router.put("/{announcement_id}")
def edit(
    announcement_id: str,
    content: AnnouncementEdit,
    db: Session = Depends(get_db),
    author: Identity = Depends(_author),
) -> dict[str, Any]:
    return announcements.change(
        db, announcement_id, content.expected_revision, author.username,
        action="save", content=content.model_dump(exclude={"expected_revision"}),
    )


@router.post("/{announcement_id}/publish")
def publish(
    announcement_id: str,
    revision: RevisionRequest,
    db: Session = Depends(get_db),
    author: Identity = Depends(_author),
) -> dict[str, Any]:
    return announcements.change(
        db, announcement_id, revision.expected_revision, author.username, action="publish",
    )


@router.post("/{announcement_id}/unpublish")
def unpublish(
    announcement_id: str,
    revision: RevisionRequest,
    db: Session = Depends(get_db),
    author: Identity = Depends(_author),
) -> dict[str, Any]:
    return announcements.change(
        db, announcement_id, revision.expected_revision, author.username, action="unpublish",
    )


@router.delete("/{announcement_id}")
def delete(
    announcement_id: str,
    expected_revision: int = Query(ge=1),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    return announcements.delete(db, announcement_id, expected_revision)
