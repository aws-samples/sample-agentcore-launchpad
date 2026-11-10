"""Local Agent Registry backend (N4): ``RegistryBackend`` on the SQLite ledger.

The CN partition has no Agent Registry service (spec C5), so records live in the
``registry_records`` table and search runs on the ``registry_records_fts`` FTS5 side
table (``app.core.db._migrate_registry_search_index`` creates it and its sync triggers
at startup; when this SQLite build lacks FTS5 the table is absent and search degrades to
LIKE). ``registry_id`` is ``local-<workspace_id>``; every query is workspace-scoped.

Approval state machine (ruling R4 of the N4 plan):

    DRAFT ──submit──▶ PENDING_APPROVAL ──approve──▶ APPROVED ──disable──▶ DEPRECATED
      ▲                     │  reject                    ▲                 (terminal)
      │                     ▼                            │
      └─(content update)  REJECTED ──approve / submit────┘

Any other transition raises ``RegistryConflictError`` (409 ``registry.invalid_transition``).
``upsert_record`` never overwrites (R1); ``update_record`` with new content resets the
record to DRAFT, a description-only edit keeps status and version (R2).
"""

import json
import re
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import inspect, or_, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.db import engine as default_engine
from app.core.errors import AppError, NotFoundError
from app.models.ledger import RegistryRecord
from app.services import creators, registry_backend
from app.services.registry_backend import (
    INITIAL_VERSION_BY_TYPE,
    platform_record_type,
    stored_a2a_card,
)
from app.services.workspace import WorkspaceContext

STATUSES: tuple[str, ...] = (
    "DRAFT", "PENDING_APPROVAL", "APPROVED", "REJECTED", "DEPRECATED",
)
# target status → the statuses it may be entered from
TRANSITIONS: dict[str, frozenset[str]] = {
    "PENDING_APPROVAL": frozenset({"DRAFT", "REJECTED"}),
    "APPROVED": frozenset({"PENDING_APPROVAL", "REJECTED"}),
    "REJECTED": frozenset({"PENDING_APPROVAL"}),
    "DEPRECATED": frozenset({"APPROVED"}),
}
# GA descriptor kind names the console shows as `descriptor_types` chips
DESCRIPTOR_KINDS: dict[str, str] = {
    "A2A": "a2aAgentCard",
    "MCP": "mcpServer",
    "AGENT_SKILLS": "agentSkillsDefinition",
    "CUSTOM": "custom",
}
FTS_TABLE = "registry_records_fts"
_SEARCH_TEXT_MAX = 20_000
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


class RegistryConflictError(AppError):
    """A write the record's state refuses (invalid transition, terminal record, duplicate
    version). Routers surface it as 409."""

    def __init__(self, code: str, message: str, **detail: Any):
        super().__init__(code, message, detail or None, status_code=409)


def registry_id_for(workspace: WorkspaceContext) -> str:
    return f"local-{workspace.id}"


def fts_index_present(bind: Engine) -> bool:
    """Whether the startup migration managed to create the FTS5 side table."""
    return FTS_TABLE in set(inspect(bind).get_table_names())


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _inline(container: Any, *path: str) -> str:
    """``inlineContent`` string at ``path`` inside a descriptor tree, or ``""``."""
    node: Any = container
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    if isinstance(node, dict):
        node = node.get("inlineContent")
    return node if isinstance(node, str) else ""


def _loads(value: str) -> Any:
    try:
        return json.loads(value) if value else None
    except ValueError:
        return None


def build_search_text(descriptor_type: str, descriptors: dict[str, Any] | None) -> str:
    """Flatten the searchable words of a descriptor tree (what FTS/LIKE scan besides
    ``name``/``description``): A2A card description + skill names/descriptions/tags,
    MCP server URL + tool names/descriptions, SKILL.md body + definition description,
    CUSTOM inline content. Capped so a huge SKILL.md cannot bloat the index."""
    descriptors = descriptors or {}
    parts: list[str] = []
    if descriptor_type == "A2A":
        card = stored_a2a_card({"descriptors": descriptors})
        parts.append(str(card.get("description") or ""))
        for skill in card.get("skills") or []:
            if isinstance(skill, dict):
                parts.append(str(skill.get("name") or ""))
                parts.append(str(skill.get("description") or ""))
                parts.extend(str(tag) for tag in skill.get("tags") or [])
    elif descriptor_type == "MCP":
        server = _loads(_inline(descriptors, "mcp", "server")) or {}
        if isinstance(server, dict):
            parts.append(str(server.get("description") or ""))
            for remote in server.get("remotes") or []:
                if isinstance(remote, dict):
                    parts.append(str(remote.get("url") or ""))
        tools = _loads(_inline(descriptors, "mcp", "tools")) or {}
        for tool in (tools.get("tools") if isinstance(tools, dict) else None) or []:
            if isinstance(tool, dict):
                parts.append(str(tool.get("name") or ""))
                parts.append(str(tool.get("description") or ""))
    elif descriptor_type == "AGENT_SKILLS":
        definition = _loads(_inline(descriptors, "agentSkills", "skillDefinition")) or {}
        if isinstance(definition, dict):
            parts.append(str(definition.get("description") or ""))
            parts.append(str(definition.get("version") or ""))
        parts.append(_inline(descriptors, "agentSkills", "skillMd"))
    elif descriptor_type == "CUSTOM":
        parts.append(_inline(descriptors, "custom"))
    return " ".join(part for part in parts if part)[:_SEARCH_TEXT_MAX]


def _to_record(row: RegistryRecord, *, with_descriptors: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {
        "recordId": row.id,
        "registryId": f"local-{row.workspace_id}",
        "name": row.name,
        "displayName": row.name,
        "description": row.description or "",
        "descriptorType": row.record_type,
        "descriptorTypes": [DESCRIPTOR_KINDS.get(row.record_type, row.record_type)],
        "status": row.status,
        "statusReason": row.status_reason,
        "recordVersion": row.record_version,
        "tags": dict(row.tags or {}),
        "createdAt": _iso(row.created_at),
        "updatedAt": _iso(row.updated_at),
        "submittedAt": _iso(row.submitted_at),
        "decidedAt": _iso(row.decided_at),
        "decidedBy": row.decided_by,
        # N15-obs7: the creator (`app.services.creators`); NULL on legacy / platform rows
        "createdBy": row.created_by,
        "createdByName": row.created_by_name,
    }
    if with_descriptors:
        out["descriptors"] = json.loads(json.dumps(row.descriptors or {}))  # detached copy
    return out


class LocalRegistryBackend:
    """``RegistryBackend`` over the ledger. Stateless apart from the engine it binds to
    and whether the FTS side table exists (probed once at construction)."""

    def __init__(self, bind: Engine | None = None, *, fts: bool | None = None):
        self._bind = bind if bind is not None else default_engine
        self._sessions = sessionmaker(bind=self._bind, autoflush=False, expire_on_commit=False)
        self.fts_enabled = fts if fts is not None else fts_index_present(self._bind)

    # ---------- helpers ----------

    def registry_id(self, workspace: WorkspaceContext) -> str:
        return registry_id_for(workspace)

    def _session(self) -> Session:
        return self._sessions()

    @staticmethod
    def _scoped(workspace: WorkspaceContext):
        return select(RegistryRecord).where(RegistryRecord.workspace_id == workspace.id)

    @staticmethod
    def _get(db: Session, workspace: WorkspaceContext, record_id: str) -> RegistryRecord:
        row = db.get(RegistryRecord, record_id)
        if row is None or row.workspace_id != workspace.id:
            raise NotFoundError(
                "registry.record_not_found",
                f"registry record {record_id} not found",
                {"record_id": record_id},
            )
        return row

    def _by_name(
        self, db: Session, workspace: WorkspaceContext, name: str, record_type: str | None
    ) -> RegistryRecord | None:
        query = self._scoped(workspace).where(RegistryRecord.name == name)
        if record_type:
            query = query.where(RegistryRecord.record_type == record_type)
        return db.scalars(query.order_by(RegistryRecord.created_at)).first()

    # ---------- Protocol: records ----------

    def find_record(
        self, workspace: WorkspaceContext, name: str, descriptor_type: str | None = None,
    ) -> dict[str, Any] | None:
        record_type = platform_record_type(descriptor_type) if descriptor_type else None
        db = self._session()
        try:
            row = self._by_name(db, workspace, name, record_type)
            return _to_record(row) if row is not None else None
        finally:
            db.close()

    def upsert_record(
        self, workspace: WorkspaceContext, *, name: str, description: str,
        descriptor_type: str, descriptors: dict[str, Any], record_version: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Create-if-absent. A same-(name, type) record is returned untouched with
        ``created=False`` — this backend never overwrites on upsert (ruling R1); callers
        that want to refresh content call ``update_record`` explicitly."""
        record_type = platform_record_type(descriptor_type)
        version = record_version or INITIAL_VERSION_BY_TYPE[record_type]
        db = self._session()
        try:
            existing = self._by_name(db, workspace, name, record_type)
            if existing is not None:
                return _to_record(existing), False
            row = RegistryRecord(
                workspace_id=workspace.id,
                record_type=record_type,
                name=name,
                description=(description or "")[:500],
                record_version=version,
                status="DRAFT",
                status_reason=None,
                descriptors=descriptors,
                search_text=build_search_text(record_type, descriptors),
            )
            creators.stamp(row, registry_backend.current_creator.get())
            db.add(row)
            try:
                db.commit()
            except IntegrityError:
                # (workspace, name, version) already exists — a concurrent create of the
                # same name, or another type pinned to the same version. Return it.
                db.rollback()
                clash = db.scalars(
                    self._scoped(workspace)
                    .where(RegistryRecord.name == name, RegistryRecord.record_version == version)
                ).first()
                if clash is None:  # pragma: no cover — the index fired, so the row exists
                    raise
                return _to_record(clash), False
            return _to_record(row), True
        finally:
            db.close()

    def get_record(self, workspace: WorkspaceContext, record_id: str) -> dict[str, Any]:
        db = self._session()
        try:
            return _to_record(self._get(db, workspace, record_id))
        finally:
            db.close()

    def wait_record_settled(
        self, workspace: WorkspaceContext, record_id: str, timeout_s: int = 60,
        sleeper: Any = time.sleep,
    ) -> dict[str, Any]:
        # No CREATING/UPDATING state locally: a write is settled when it commits.
        return self.get_record(workspace, record_id)

    def list_records(
        self, workspace: WorkspaceContext, descriptor_type: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        query = self._scoped(workspace)
        if descriptor_type:
            query = query.where(RegistryRecord.record_type == platform_record_type(descriptor_type))
        if status:
            query = query.where(RegistryRecord.status == status.upper())
        db = self._session()
        try:
            rows = db.scalars(
                query.order_by(RegistryRecord.name, RegistryRecord.record_version)
            ).all()
            return [_to_record(row) for row in rows]
        finally:
            db.close()

    def list_discoverable_records(
        self, workspace: WorkspaceContext, descriptor_type: str | None = None,
    ) -> list[dict[str, Any]]:
        record_type = platform_record_type(descriptor_type) if descriptor_type else None
        query = self._scoped(workspace).where(RegistryRecord.status == "APPROVED")
        if record_type:
            query = query.where(RegistryRecord.record_type == record_type)
        db = self._session()
        try:
            rows = db.scalars(query.order_by(RegistryRecord.name)).all()
            return [_to_record(row, with_descriptors=False) for row in rows]
        finally:
            db.close()

    def update_record(
        self, workspace: WorkspaceContext, record_id: str, *, description: str | None = None,
        descriptors: dict[str, Any] | None = None, record_version: str | None = None,
    ) -> dict[str, Any]:
        db = self._session()
        try:
            row = self._get(db, workspace, record_id)
            if row.status == "DEPRECATED":
                raise RegistryConflictError(
                    "registry.record_deprecated",
                    f"registry record {record_id} is DEPRECATED — terminal; only delete remains",
                    record_id=record_id,
                )
            content_changed = descriptors is not None or record_version is not None
            if description is not None:
                row.description = description[:500]
            if descriptors is not None:
                row.descriptors = descriptors
                row.search_text = build_search_text(row.record_type, descriptors)
            if record_version is not None:
                row.record_version = record_version
            if content_changed:
                # UpdateRegistryRecord parity: new content re-enters review (ruling R2)
                row.status = "DRAFT"
                row.status_reason = "updated — awaiting review"
                row.submitted_at = None
                row.decided_at = None
                row.decided_by = None
            row.updated_at = _now()
            try:
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                raise RegistryConflictError(
                    "registry.version_exists",
                    f"record version {record_version!r} already exists for '{row.name}'",
                    record_id=record_id, record_version=record_version,
                ) from exc
            return _to_record(row)
        finally:
            db.close()

    # ---------- Protocol: lifecycle ----------

    def set_record_status(
        self, workspace: WorkspaceContext, record_id: str, status: str, reason: str,
    ) -> dict[str, Any]:
        target = (status or "").upper()
        if target not in STATUSES:
            raise ValueError(f"unknown registry record status: {status!r}")
        db = self._session()
        try:
            row = self._get(db, workspace, record_id)
            if row.status not in TRANSITIONS.get(target, frozenset()):
                raise RegistryConflictError(
                    "registry.invalid_transition",
                    f"cannot move record {record_id} from {row.status} to {target}",
                    record_id=record_id, from_status=row.status, to_status=target,
                )
            now = _now()
            row.status = target
            row.status_reason = reason or None
            row.updated_at = now
            if target == "PENDING_APPROVAL":
                row.submitted_at = now
                row.decided_at = None
                row.decided_by = None
            else:
                row.decided_at = now
                row.decided_by = registry_backend.current_actor.get()
            db.commit()
            return _to_record(row)
        finally:
            db.close()

    def submit_record(self, workspace: WorkspaceContext, record_id: str) -> dict[str, Any]:
        return self.set_record_status(
            workspace, record_id, "PENDING_APPROVAL", "submitted via launchpad console"
        )

    def approve_record(self, workspace: WorkspaceContext, record_id: str) -> dict[str, Any]:
        return self.set_record_status(
            workspace, record_id, "APPROVED", "approved via launchpad console"
        )

    def disable_record(self, workspace: WorkspaceContext, record_id: str) -> dict[str, Any]:
        return self.set_record_status(
            workspace, record_id, "DEPRECATED", "disabled via launchpad console"
        )

    def reject_record(self, workspace: WorkspaceContext, record_id: str) -> dict[str, Any]:
        return self.set_record_status(
            workspace, record_id, "REJECTED", "rejected via launchpad console"
        )

    def delete_record(self, workspace: WorkspaceContext, record_id: str) -> None:
        db = self._session()
        try:
            row = self._get(db, workspace, record_id)
            db.delete(row)
            db.commit()
        finally:
            db.close()

    # ---------- Protocol: search (hybrid — FTS rank first, LIKE supplements/fallback) ----------

    def search_records(
        self, workspace: WorkspaceContext, query: str, max_results: int = 20,
    ) -> list[dict[str, Any]]:
        tokens = _TOKEN_RE.findall(query or "")
        if not tokens or max_results <= 0:
            return []
        db = self._session()
        try:
            out: list[dict[str, Any]] = []
            seen: set[str] = set()
            if self.fts_enabled:
                for record_id in self._fts_ids(tokens, max_results * 4):
                    row = db.get(RegistryRecord, record_id)
                    if row is None or row.workspace_id != workspace.id or row.id in seen:
                        continue
                    seen.add(row.id)
                    out.append(_to_record(row))
                    if len(out) >= max_results:
                        return out
            like = self._scoped(workspace)
            for token in tokens:
                pattern = f"%{token}%"
                like = like.where(or_(
                    RegistryRecord.name.like(pattern),
                    RegistryRecord.description.like(pattern),
                    RegistryRecord.search_text.like(pattern),
                ))
            for row in db.scalars(like.order_by(RegistryRecord.name)).all():
                if row.id in seen:
                    continue
                seen.add(row.id)
                out.append(_to_record(row))
                if len(out) >= max_results:
                    break
            return out
        finally:
            db.close()

    def _fts_ids(self, tokens: list[str], limit: int) -> list[str]:
        # Each token is \w+ only, so quoting it is safe; the trailing * makes it a prefix
        # query; juxtaposition is an implicit AND. Ranked by FTS5's bm25-based `rank`.
        match = " ".join(f'"{token}"*' for token in tokens)
        with self._bind.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT record_id FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH :q "  # noqa: S608
                    "ORDER BY rank LIMIT :n"
                ),
                {"q": match, "n": limit},
            ).all()
        return [str(row[0]) for row in rows]
