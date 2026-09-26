"""Published video directory and administrator-only draft/publication API."""

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.routers.auth import Identity, require_identity
from app.services import videos

router = APIRouter(prefix="/api/videos", tags=["videos"])


def _author(request: Request) -> Identity:
    return require_identity(request)


class RevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1, strict=True)


class VideoEdit(videos.VideoContent):
    expected_revision: int = Field(ge=1, strict=True)


@router.get("")
def published(db: Session = Depends(get_db)) -> dict[str, Any]:
    return videos.published_catalog(db)


@router.get("/manage")
def managed(db: Session = Depends(get_db)) -> dict[str, Any]:
    return videos.managed_catalog(db)


@router.get("/manage/{video_id}")
def detail(video_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    return videos.serialize_managed(videos.get_video(db, video_id))


@router.post("/manage", status_code=201)
def create(
    content: videos.VideoContent,
    db: Session = Depends(get_db),
    author: Identity = Depends(_author),
) -> dict[str, Any]:
    return videos.create(db, content, author.username)


@router.put("/manage/{video_id}")
def edit(
    video_id: str,
    content: VideoEdit,
    db: Session = Depends(get_db),
    author: Identity = Depends(_author),
) -> dict[str, Any]:
    return videos.change(
        db, video_id, content.expected_revision, author.username, action="save",
        content=videos.VideoContent.model_validate(content.model_dump(exclude={"expected_revision"})),
    )


@router.post("/manage/{video_id}/publish")
def publish(
    video_id: str,
    revision: RevisionRequest,
    db: Session = Depends(get_db),
    author: Identity = Depends(_author),
) -> dict[str, Any]:
    return videos.change(
        db, video_id, revision.expected_revision, author.username, action="publish",
    )


@router.post("/manage/{video_id}/unpublish")
def unpublish(
    video_id: str,
    revision: RevisionRequest,
    db: Session = Depends(get_db),
    author: Identity = Depends(_author),
) -> dict[str, Any]:
    return videos.change(
        db, video_id, revision.expected_revision, author.username, action="unpublish",
    )


@router.delete("/manage/{video_id}")
def delete(
    video_id: str,
    expected_revision: int = Query(ge=1),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    return videos.delete(db, video_id, expected_revision)
