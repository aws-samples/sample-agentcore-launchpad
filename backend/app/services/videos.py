"""Hub-global video catalog: fixed V2 sections, editable drafts, published snapshots."""

import ipaddress
import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.core.errors import AppError
from app.models.video import Video, VideoCatalogSeed

_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
_TAXONOMY_PATH = _DATA_DIR / "video_sections.json"
_LEGACY_PATH = _DATA_DIR / "videos.initial.json"
_V2_MEDIA_REVISION = re.compile(r"/media/[^/]+/[0-9]{8}-v2(?:-[^/]*)?/")

ConsoleVersion = Literal["v2", "classic"]

# One-time migration of the bundled directory into its matching V2 navigation section.
_LEGACY_SECTIONS = {
    "agent-development-harness": "agents",
    "architect-assistant": "assistant",
    "architect-assistant-runtime-ab": "assistant",
    "agent-management": "agents",
    "agent-management-byoc": "agents",
    "registry": "registry",
    "memory": "memory",
    "observability": "observability",
    "evaluation-datasets": "eval-data",
    "evaluation-runs": "eval-tasks",
    "evaluation-ab": "eval-experiments",
    "evaluation-online": "eval-online",
    "evaluation-evaluators": "eval-evaluators",
    "skill-tasksets": "skill-lab",
    "skill-evaluation": "skill-lab",
    "skill-optimization": "skill-lab",
}


@lru_cache
def taxonomy() -> dict[str, Any]:
    data = json.loads(_TAXONOMY_PATH.read_text(encoding="utf-8"))
    groups = {item["id"] for item in data["categories"]}
    ids: set[str] = set()
    paths: set[str] = set()
    for item in data["sections"]:
        if item["id"] in ids or item["path"] in paths or item["categoryId"] not in groups:
            raise RuntimeError("Video section taxonomy is not aligned with V2 navigation")
        ids.add(item["id"])
        paths.add(item["path"])
    return data


def _section(section_id: str) -> dict[str, Any] | None:
    return next((item for item in taxonomy()["sections"] if item["id"] == section_id), None)


def _url(value: str | None, extension: str, *, optional: bool = False) -> str | None:
    if value is None or value == "":
        if optional:
            return None
        raise ValueError("A CDN video URL is required")
    if re.search(r"%(?![0-9a-fA-F]{2})", value) or any(ch.isspace() for ch in value):
        raise ValueError("CDN URLs must not contain whitespace or malformed escapes")
    decoded = unquote(value, errors="strict")
    if "\\" in decoded or any(ord(ch) < 32 or ord(ch) == 127 for ch in decoded):
        raise ValueError("CDN URLs cannot contain controls or backslashes")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
        or parsed.query or parsed.fragment
    ):
        raise ValueError("CDN URLs must be permanent credential-free HTTPS links")
    HttpUrl(value)
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("CDN URL must use a public host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("CDN URL must use a public host")
    if not parsed.path.lower().endswith(extension):
        raise ValueError(f"CDN URL must end with {extension}")
    return value


def _console_version(content: dict[str, Any]) -> ConsoleVersion:
    """Classify pre-field ledger snapshots once at the API boundary.

    The revision segment identifies the already-published V2 recordings. New
    writes persist an explicit value; reads never rewrite old snapshots.
    """
    explicit = content.get("console_version")
    if explicit in ("v2", "classic"):
        return explicit
    media_path = urlsplit(str(content.get("cdn_url") or "")).path
    return "v2" if _V2_MEDIA_REVISION.search(media_path) else "classic"


def _present_content(content: dict[str, Any] | None) -> dict[str, Any] | None:
    return {**content, "console_version": _console_version(content)} if content else None


class LocalizedText(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, populate_by_name=True)

    en: str = Field(default="", max_length=2000)
    zh_cn: str = Field(alias="zh-CN", min_length=1, max_length=2000)

    @model_validator(mode="after")
    def fallback_english(self) -> "LocalizedText":
        if not self.en:
            self.en = self.zh_cn
        return self


class Chapter(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    start_seconds: float = Field(alias="startSeconds", ge=0)
    title: LocalizedText


class VideoContent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    category_id: str
    section_id: str
    console_version: ConsoleVersion
    title: LocalizedText
    description: LocalizedText
    cdn_url: str = Field(min_length=1, max_length=2048)
    webm_url: str | None = Field(default=None, max_length=2048)
    poster_url: str | None = Field(default=None, max_length=2048)
    caption_url: str | None = Field(default=None, max_length=2048)
    duration_seconds: float = Field(default=0, ge=0, le=86400)
    chapters: list[Chapter] = Field(default_factory=list, max_length=100)
    sort_order: int = Field(default=1000, ge=0, le=1_000_000)

    @model_validator(mode="before")
    @classmethod
    def classify_existing_content(cls, value: Any) -> Any:
        if isinstance(value, dict) and "console_version" not in value:
            return {**value, "console_version": _console_version(value)}
        return value

    @model_validator(mode="after")
    def valid_directory_and_media(self) -> "VideoContent":
        section = _section(self.section_id)
        if section is None or section["categoryId"] != self.category_id:
            raise ValueError("Second-level video category must belong to the selected V2 area")
        for text in (self.title, self.description):
            if not text.zh_cn or not text.en:
                raise ValueError("Video title and introduction are required")
        if len(self.title.zh_cn) > 160 or len(self.title.en) > 160:
            raise ValueError("Video title cannot exceed 160 characters")
        primary = _url(self.cdn_url, ".mp4" if self.cdn_url.lower().endswith(".mp4") else ".webm")
        if self.webm_url is not None:
            _url(self.webm_url, ".webm")
            if primary and primary.lower().endswith(".webm"):
                raise ValueError("A second WebM URL requires an MP4 primary URL")
        _url(self.poster_url, ".jpg", optional=True)
        _url(self.caption_url, ".vtt", optional=True)
        previous = -1.0
        for chapter in self.chapters:
            if chapter.start_seconds <= previous or chapter.start_seconds >= self.duration_seconds:
                raise ValueError("Chapter offsets must increase within the video duration")
            previous = chapter.start_seconds
        return self


def _dump(content: VideoContent) -> dict[str, Any]:
    return content.model_dump(by_alias=True)


def _iso(value: datetime | None) -> str | None:
    return value.replace(tzinfo=value.tzinfo or UTC).isoformat() if value else None


def _legacy_content(video: dict[str, Any], section_id: str, order: int) -> dict[str, Any]:
    section = _section(section_id)
    if section is None:
        raise RuntimeError(f"Unknown V2 section for legacy video {video['id']}")
    sources = {source["type"]: source["url"] for source in video["sources"]}
    primary = sources.get("video/mp4") or sources.get("video/webm")
    if primary is None:
        raise RuntimeError(f"Legacy video {video['id']} has no playable source")
    return _dump(VideoContent(
        category_id=section["categoryId"],
        section_id=section_id,
        title=video["title"],
        description=video["description"],
        cdn_url=primary,
        webm_url=sources.get("video/webm") if sources.get("video/mp4") else None,
        poster_url=video.get("posterUrl"),
        caption_url=next((item["url"] for item in video.get("captions", []) if
                          item.get("language") == "zh-CN"), None),
        duration_seconds=video["durationSeconds"],
        chapters=video.get("chapters", []),
        sort_order=order,
    ))


def seed_legacy_catalog(bind) -> None:
    """Import the existing bundled catalog once; later restarts never overwrite admin edits."""
    with Session(bind=bind) as db:
        if db.get(VideoCatalogSeed, 1) is not None:
            return
        if not db.query(Video).first():
            legacy = json.loads(_LEGACY_PATH.read_text(encoding="utf-8"))
            videos = legacy["videos"]
            missing = {video["id"] for video in videos} - _LEGACY_SECTIONS.keys()
            if missing:
                raise RuntimeError(f"Legacy video section mapping missing: {sorted(missing)}")
            playlist_order = {
                video_id: index * 10
                for index, video_id in enumerate(
                    video_id for collection in legacy["collections"]
                    for video_id in collection["videoIds"]
                )
            }
            for video in videos:
                content = _legacy_content(
                    video, _LEGACY_SECTIONS[video["id"]], playlist_order[video["id"]],
                )
                db.add(Video(
                    id=video["id"], content=content, published_content=deepcopy(content),
                    published_at=datetime.fromisoformat(video["publishedAt"]).replace(tzinfo=UTC),
                    created_by="catalog-import", updated_by="catalog-import",
                ))
        db.add(VideoCatalogSeed(id=1))
        db.commit()


def serialize_managed(row: Video) -> dict[str, Any]:
    return {
        "id": row.id,
        "content": _present_content(row.content),
        "published_content": _present_content(row.published_content),
        "status": "published" if row.published_content is not None else "draft",
        "has_unpublished_changes": row.content != row.published_content,
        "revision": row.revision,
        "created_by": row.created_by,
        "updated_by": row.updated_by,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "published_at": _iso(row.published_at),
    }


def _public(row: Video) -> dict[str, Any]:
    content = row.published_content or {}
    primary = content["cdn_url"]
    sources = [{"url": primary, "type": "video/mp4" if primary.lower().endswith(".mp4")
                else "video/webm"}]
    if content.get("webm_url"):
        sources.append({"url": content["webm_url"], "type": "video/webm"})
    captions = [{"url": content["caption_url"], "language": "zh-CN", "label": "简体中文"}] \
        if content.get("caption_url") else []
    return {
        "id": row.id,
        "consoleVersion": _console_version(content),
        "title": content["title"],
        "description": content["description"],
        "publishedAt": _iso(row.published_at),
        "durationSeconds": content["duration_seconds"],
        "language": "zh-CN",
        "posterUrl": content.get("poster_url") or "",
        "sources": sources,
        "captions": captions,
        "chapters": content.get("chapters") or [],
    }


def _ordered(rows: list[Video], *, published: bool) -> list[Video]:
    taxonomy_data = taxonomy()
    group_order = {item["id"]: i for i, item in enumerate(taxonomy_data["categories"])}
    section_order = {item["id"]: i for i, item in enumerate(taxonomy_data["sections"])}
    return sorted(rows, key=lambda row: (
        group_order.get((row.published_content if published else row.content)["category_id"], 999),
        section_order.get((row.published_content if published else row.content)["section_id"], 999),
        (row.published_content if published else row.content)["sort_order"],
        row.created_at, row.id,
    ))


def published_catalog(db: Session) -> dict[str, Any]:
    rows = _ordered(db.query(Video).filter(Video.published_at.is_not(None)).all(),
                    published=True)
    by_section: dict[str, list[Video]] = {}
    for row in rows:
        by_section.setdefault(row.published_content["section_id"], []).append(row)
    sections = [item for item in taxonomy()["sections"] if item["id"] in by_section]
    category_ids = {item["categoryId"] for item in sections}
    return {
        "schemaVersion": 3,
        "categories": [item for item in taxonomy()["categories"] if item["id"] in category_ids],
        "collections": [{
            "id": item["id"],
            "categoryId": item["categoryId"],
            "title": item["title"],
            "description": by_section[item["id"]][0].published_content["description"],
            "videoIds": [row.id for row in by_section[item["id"]]],
        } for item in sections],
        "videos": [_public(row) for row in rows],
    }


def managed_catalog(db: Session) -> dict[str, Any]:
    rows = _ordered(db.query(Video).all(), published=False)
    return {"taxonomy": taxonomy(), "videos": [serialize_managed(row) for row in rows]}


def get_video(db: Session, video_id: str) -> Video:
    row = db.get(Video, video_id)
    if row is None:
        raise AppError("videos.not_found", "Video not found", status_code=404)
    return row


def _conflict() -> AppError:
    return AppError(
        "videos.conflict", "This video changed. Reload it before saving or publishing.",
        status_code=409,
    )


def _check_revision(row: Video, expected_revision: int) -> None:
    if row.revision != expected_revision:
        raise _conflict()


def _commit(db: Session) -> None:
    try:
        db.commit()
    except StaleDataError as exc:
        db.rollback()
        raise _conflict() from exc


def create(db: Session, content: VideoContent, author: str) -> dict[str, Any]:
    row = Video(content=_dump(content), created_by=author, updated_by=author)
    db.add(row)
    _commit(db)
    return serialize_managed(row)


def change(
    db: Session, video_id: str, expected_revision: int, author: str,
    *, action: str, content: VideoContent | None = None,
) -> dict[str, Any]:
    row = get_video(db, video_id)
    _check_revision(row, expected_revision)
    now = datetime.now(UTC)
    if action == "save":
        if content is None:
            raise ValueError("Saving a video requires content")
        row.content = _dump(content)
    elif action == "publish":
        row.content = _dump(VideoContent.model_validate(row.content))
        row.published_content = deepcopy(row.content)
        row.published_at = now
    elif action == "unpublish":
        row.published_content = None
        row.published_at = None
    else:
        raise ValueError(f"Unknown video action: {action}")
    row.updated_by = author
    row.updated_at = now
    row.revision += 1
    _commit(db)
    return serialize_managed(row)


def delete(db: Session, video_id: str, expected_revision: int) -> dict[str, bool]:
    row = get_video(db, video_id)
    _check_revision(row, expected_revision)
    db.delete(row)
    _commit(db)
    return {"deleted": True}
