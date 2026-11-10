"""System Skill registration (SE-043): a preset's published Skill bundle as its own
Agent Registry record, owned by the platform.

The preset agent already has an A2A record (``Agent.registry_record_id``); this module
registers the *Skill* — the versioned, content-addressed release the Harness loads —
as an ``AGENT_SKILLS`` record that points at the **existing immutable** S3 release
(``system-skills/<name>/<version>-<digest12>/``). Nothing is copied to the mutable
``skills/<name>/`` prefix, nothing is uploaded and nothing is deleted here: the only
S3 traffic is a read-back that proves the published release is exactly the snapshot
the descriptor describes.

Ownership is a server-owned ledger row (``SystemSkillRecord``, one per workspace and
preset), never a descriptor field or tag a client could send — descriptor metadata is
copyable and proves nothing. The only proof that a record is ours is that **our own
create produced it**: the create intent (name, version, content digest, ``imported_at``)
is persisted before the write; a crash between the write and the ledger commit is
recovered on the next run by finding the same-name record and matching its content
against that intent — a same-name record the intent cannot account for is refused,
never bound, never overwritten. An id AWS accepted but
whose read-back has not verified yet is ``accepted``/``pending_record_id`` (protected,
not registered); only a verified read-back makes it ``registered``. Re-registering
identical content is a no-op that keeps the record's approval; a new release commits
its intent (a high-water mark) before ``UpdateRegistryRecord`` and re-reads the remote
release first, so a stale worker can never downgrade an accepted newer release; the
update resets the record to DRAFT and it goes through review again. Approval itself is
always an explicit administrator action in the Registry console — nothing here approves.

Single-host topology (one process tree, one SQLite ledger): concurrent registrations
serialize on an advisory ``fcntl`` lock per (workspace, preset), like the uninstall
worker; the unique constraint on the row arbitrates the claim across processes.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import DATA_DIR
from app.core.db import SessionLocal
from app.core.errors import AppError, NotFoundError
from app.models.ledger import Agent, SystemSkillRecord
from app.services import registry_backend
from app.services.registry_backend import RegistryBackend, bump_minor_version
from app.services.skill_ingest import parse_frontmatter
from app.services.workspace import WorkspaceContext
from app.system_agents import presets as catalogue
from app.system_agents.presets import SystemPreset
from app.system_agents.service import (
    MANIFEST_KEY,
    _get_bytes,
    _list_keys,
    _manifest_matches,
    _parse_manifest,
)

SOURCE_KIND = "system"
DESCRIPTOR_TYPE = "AGENT_SKILLS"
LOCK_DIR = DATA_DIR / "locks" / "system-agents"
STATUS_CREATING = "creating"
STATUS_ACCEPTED = "accepted"  # our create returned an id; read-back not verified yet
STATUS_REGISTERED = "registered"
INITIAL_RECORD_VERSION = "1.0.0-skill"  # type-qualified, like every platform Skill record
# Content mutations nobody performs through the Registry console — the release is
# published by the preset pipeline only.
PROTECTED_ACTIONS: tuple[str, ...] = ("edit", "replace", "reimport", "delete")
# Lifecycle actions an administrator (and only an administrator) may still run.
ADMIN_LIFECYCLE_ACTIONS: tuple[str, ...] = ("submit", "approve", "publish", "reject", "disable")


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


def _error(code: str, message: str, status: int = 409, **detail: Any) -> AppError:
    return AppError(f"system_skill.{code}", message, detail or None, status_code=status)


def protection_error(row: SystemSkillRecord, action: str) -> AppError:
    preset = catalogue.get_preset(row.preset_key)
    label = preset.label if preset else row.preset_key
    return AppError(
        "registry.system_skill_protected",
        f"'{row.skill_name}' is the system-managed Skill of the {label} preset; {action} is "
        "not available on it. Its content is published by the preset pipeline "
        "(System presets panel → REPAIR/UPDATE) and re-registered from there; only the "
        "approval lifecycle is operated in the Registry, by an administrator.",
        {
            "record_id": row.record_id,
            "preset_key": row.preset_key,
            "action": action,
            "maintenance_route": f"/api/system-agents/{row.preset_key}/skill-registration",
        },
        status_code=403,
    )


# ---------------------------------------------------------------------------
# ledger lookups (no AWS)
# ---------------------------------------------------------------------------


def find_mapping(db: Session, workspace_id: str, preset_key: str) -> SystemSkillRecord | None:
    return (
        db.query(SystemSkillRecord)
        .filter(
            SystemSkillRecord.workspace_id == workspace_id,
            SystemSkillRecord.preset_key == preset_key,
        )
        .first()
    )


def _registry_of(workspace: WorkspaceContext) -> str:
    """The registry identity mapping rows are scoped to: the resource map's
    ``registry_id`` when bootstrap wrote one, else the local backend's own
    ``local-<workspace_id>`` (N4)."""
    from app.services.registry_local import registry_id_for

    return str((workspace.resources or {}).get("registry_id") or "") or registry_id_for(workspace)


def protected_record(
    db: Session, workspace_id: str, registry_id: str | None, record_id: str
) -> SystemSkillRecord | None:
    """The mapping row that makes ``record_id`` system-protected in this workspace's
    CURRENT registry, if any. Scoped to (workspace, registry identity, record id): the
    same id in another workspace, or in a replacement registry this workspace switched
    to, is an ordinary record. Both the verified id and an accepted-but-unverified id
    count — the latter is ours too (our create returned it), it just is not registered."""
    if not record_id or not registry_id:
        return None
    return (
        db.query(SystemSkillRecord)
        .filter(
            SystemSkillRecord.workspace_id == workspace_id,
            SystemSkillRecord.registry_id == registry_id,
            (SystemSkillRecord.record_id == record_id)
            | (SystemSkillRecord.pending_record_id == record_id),
        )
        .first()
    )


def record_projection(row: SystemSkillRecord) -> dict[str, Any]:
    """The server-owned ``system`` member of a registry record's API projection."""
    preset = catalogue.get_preset(row.preset_key)
    return {
        "managed": True,
        "preset_key": row.preset_key,
        "label": preset.label if preset else row.preset_key,
        "skill_version": row.release_version,
        "release_digest": (row.release_digest or "")[:12] or None,
        "path": row.s3_uri,
        "protected_actions": list(PROTECTED_ACTIONS),
        "admin_actions": list(ADMIN_LIFECYCLE_ACTIONS),
    }


def projections_for(
    workspace: WorkspaceContext, record_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """``record_id → system projection`` for the given ids in this workspace's current
    registry (ledger-only, own session). Verified and accepted ids alike."""
    wanted = [rid for rid in record_ids if rid]
    registry_id = _registry_of(workspace)
    if not wanted or not registry_id:
        return {}
    db = SessionLocal()
    try:
        rows = (
            db.query(SystemSkillRecord)
            .filter(
                SystemSkillRecord.workspace_id == workspace.id,
                SystemSkillRecord.registry_id == registry_id,
                SystemSkillRecord.record_id.in_(wanted)
                | SystemSkillRecord.pending_record_id.in_(wanted),
            )
            .all()
        )
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            for rid in (row.record_id, row.pending_record_id):
                if rid and rid in wanted:
                    out[rid] = record_projection(row)
        return out
    finally:
        db.close()


def status_projection(
    db: Session, workspace_id: str, registry_id: str | None, preset: SystemPreset
) -> dict[str, Any] | None:
    """The ``skill_registration`` member of ``GET /api/system-agents`` (ledger-only).
    ``None`` when nothing is mapped for this workspace's CURRENT registry — a mapping
    left behind by a replaced registry is not a registration in the new one."""
    row = find_mapping(db, workspace_id, preset.key)
    if row is None or not registry_id or row.registry_id != registry_id:
        return None
    return {
        "record_id": row.record_id,
        "pending_record_id": row.pending_record_id,
        "status": row.status,
        "release_version": row.release_version,
        "release_digest": (row.release_digest or "")[:12] or None,
        "path": row.s3_uri,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def refuse_protected_mutation(
    workspace: WorkspaceContext,
    record_id: str,
    action: str,
    *,
    is_admin: bool = False,
    lifecycle: bool = False,
) -> None:
    """Service-boundary guard for every registry mutator — runs BEFORE any AWS client
    is built or S3 object touched.

    Content mutations (``edit``/``replace``/``reimport``/``delete``) are refused for
    everyone; lifecycle actions are refused unless the caller is an administrator.
    ``is_admin`` defaults to False so an internal caller that forgets to pass the
    identity (a background job, say) can never approve or disable a system Skill by
    omission.
    """
    db = SessionLocal()
    try:
        row = protected_record(db, workspace.id, _registry_of(workspace), record_id)
    finally:
        db.close()
    if row is None:
        return
    if lifecycle and is_admin and action in ADMIN_LIFECYCLE_ACTIONS:
        return
    raise protection_error(row, action)


def _gone(exc: BaseException) -> bool:
    """A delete/get that found no record: the registry's own 404 or an AWS not-found."""
    return getattr(exc, "status_code", None) == 404 or any(
        marker in f"{type(exc).__name__}{exc}" for marker in ("ResourceNotFound", "NotFound")
    )


def unregister_system_skill(
    db: Session, workspace: WorkspaceContext, preset_key: str, log: Callable[[str], None]
) -> str:
    """Remove the preset's protected Skill record and its mapping row — the preset
    uninstall's registry step (N10v3-bug6 / N11-r4-bug1: the record outlived the preset
    and, being system-protected, nobody but this path could delete it).

    This IS the maintenance path that owns the protected record, so the protection guard
    does not apply; every other caller still gets 403. Idempotent: no mapping → skipped;
    a record already gone → the mapping is dropped; a mapping left by a replaced registry
    (``registry_id`` differs from the workspace's current one) names no record here and
    is dropped. A registry error other than not-found propagates so the uninstall job
    fails at this step and a later attempt retries it.
    """
    row = find_mapping(db, workspace.id, preset_key)
    if row is None:
        return "skipped: no system skill record mapped"
    current = _registry_of(workspace)
    ids = [rid for rid in (row.record_id, row.pending_record_id) if rid]
    if row.registry_id != current:
        db.delete(row)
        db.commit()
        return (f"stale mapping {', '.join(ids) or '(no record id)'} for registry "
                f"{row.registry_id} dropped (current registry is {current})")
    backend = registry_backend.backend_or_none()
    if backend is None:
        db.delete(row)
        db.commit()
        return "skipped: no registry backend in this build (mapping dropped)"
    deleted: list[str] = []
    for rid in ids:
        try:
            backend.delete_record(workspace, rid)
        except Exception as exc:  # noqa: BLE001 — not-found is "already gone"
            if _gone(exc):
                log(f"system skill record {rid} already gone")
                continue
            raise
        deleted.append(rid)
        log(f"deleted system skill record {rid}")
    db.delete(row)
    db.commit()
    return (f"skill record {', '.join(deleted)} deleted; mapping removed" if deleted
            else f"skill record {', '.join(ids) or '(none)'} already gone; mapping removed")


def refuse_reserved_skill_name(name: str) -> None:
    """The preset's Skill name is reserved for the platform: an ordinary register /
    import / import-with-rename is refused before any S3 or Registry write. Only the
    preset names are reserved — an unrelated MCP record may still use them."""
    if catalogue.is_reserved_name(name):
        raise AppError(
            "registry.name_reserved",
            f"'{name}' is the name of a system-managed Skill; register it through the "
            "System presets panel, not as an ordinary Skill",
            {"name": name},
            status_code=409,
        )


# ---------------------------------------------------------------------------
# the registration payload — derived from the installed, published release only
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Registration:
    preset: SystemPreset
    snapshot: catalogue.BundleSnapshot
    bucket: str
    prefix: str  # S3 key prefix (no bucket), trailing slash
    skill_md: str
    definition: dict[str, Any]

    @property
    def name(self) -> str:
        return self.preset.name

    @property
    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}"

    def descriptors(self) -> dict[str, Any]:
        return registry_backend.build_skills_descriptors(
            skill_md=self.skill_md, definition=self.definition
        )

    def content_digest(self) -> str:
        return content_digest(self.definition, self.skill_md)

    def description(self) -> str:
        return (str(self.definition.get("description") or self.name))[:200]


def _utcnow_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def content_digest(definition: dict[str, Any], skill_md: str) -> str:
    """Identity of a registered payload — everything except the registration
    timestamp, so re-registering the same release compares equal."""
    stable = dict(definition)
    source = stable.get("source")
    if isinstance(source, dict):
        stable["source"] = {k: v for k, v in source.items() if k != "imported_at"}
    body = json.dumps({"definition": stable, "skill_md": skill_md}, sort_keys=True)
    return sha256(body.encode("utf-8")).hexdigest()


def installed_release(
    preset: SystemPreset, agent: Agent, bucket: str
) -> tuple[catalogue.BundleSnapshot, str]:
    """The release the installed preset actually loads, proven against this build.

    The stored spec names one content-addressed directory; this build's snapshot must
    hash to exactly that digest — otherwise the checkout is not the installed release
    (an unpublished newer revision, or an older build) and the SKILL.md we would
    register is not the one the Harness serves. Refused with the repair instruction.
    """
    spec = agent.spec or {}
    release = catalogue.skill_release_from_spec(spec)
    if release is None or release[1] is None:
        raise _error(
            "release_mismatch",
            "the installed preset does not pin a content-addressed release — re-run the "
            "preset install/repair from the System presets panel first",
            skills=list(spec.get("skills") or []),
        )
    version, digest12 = release
    snapshot = catalogue.snapshot_bundle(preset)
    if snapshot.version != version or snapshot.digest[:12] != digest12:
        raise _error(
            "release_mismatch",
            f"the installed preset loads release v{version}-{digest12} but this build's bundle "
            f"is v{snapshot.version}-{snapshot.digest[:12]} — publish the build's release with "
            "REPAIR/UPDATE in the System presets panel, then register the Skill",
            installed={"version": version, "digest": digest12},
            build={"version": snapshot.version, "digest": snapshot.digest[:12]},
        )
    prefix = catalogue.skill_prefix_from_spec(spec)
    expected_prefix = preset.skill_prefix(snapshot.digest)
    if prefix != expected_prefix or list(spec.get("skills") or []) != [
        preset.skill_uri(bucket, snapshot.digest)
    ]:
        raise _error(
            "release_mismatch",
            f"the installed spec loads {spec.get('skills')} but this workspace's release is "
            f"exactly s3://{bucket}/{expected_prefix} — re-run the preset install so the spec "
            "is re-derived",
        )
    return snapshot, expected_prefix


def build_registration(
    preset: SystemPreset,
    snapshot: catalogue.BundleSnapshot,
    bucket: str,
    prefix: str,
    *,
    imported_at: str | None = None,
) -> Registration:
    """``imported_at`` defaults to now; a replay passes the persisted request's value so
    the descriptor bytes are the same on every attempt."""
    skill_md = snapshot.files["SKILL.md"].decode("utf-8")
    front = parse_frontmatter(skill_md)
    definition = {
        "name": preset.name,
        "description": str(front.get("description") or preset.description).strip(),
        "version": snapshot.version,
        "path": f"s3://{bucket}/{prefix}",
        "files": sorted(snapshot.files),
        "source": {
            "kind": SOURCE_KIND,
            "preset_key": preset.key,
            "release_version": snapshot.version,
            "release_digest": snapshot.digest,
            "manifest": f"s3://{bucket}/{prefix}{MANIFEST_KEY}",
            "imported_at": imported_at or _utcnow_iso(),
        },
    }
    return Registration(
        preset=preset,
        snapshot=snapshot,
        bucket=bucket,
        prefix=prefix,
        skill_md=skill_md,
        definition=definition,
    )


def verify_published_release(
    s3: Any, bucket: str, prefix: str, snapshot: catalogue.BundleSnapshot
) -> None:
    """Read-only proof that the published directory is exactly the snapshot: manifest
    (version, digest, per-file digests), the complete listing, every object's bytes.
    The same checks the package stage's final verification runs — with no repair
    branch, because registration never writes to S3."""
    manifest_key = f"{prefix}{MANIFEST_KEY}"
    manifest = _get_bytes(s3, bucket, manifest_key)
    if manifest is None or not _manifest_matches(_parse_manifest(manifest[0]), snapshot):
        raise _error(
            "bundle_unverified",
            f"s3://{bucket}/{manifest_key} is missing or does not describe release "
            f"{snapshot.version}-{snapshot.digest[:12]} — publish the release with "
            "REPAIR/UPDATE in the System presets panel first",
        )
    expected = {f"{prefix}{rel}" for rel in snapshot.files} | {manifest_key}
    listed = _list_keys(s3, bucket, prefix)
    if listed != expected:
        missing = sorted(expected - listed)
        foreign = sorted(listed - expected)
        raise _error(
            "bundle_unverified",
            f"s3://{bucket}/{prefix} is not exactly the published snapshot "
            f"({len(missing)} missing, {len(foreign)} foreign object(s)) — repair the "
            "release from the System presets panel; nothing was written",
            missing=[k[len(prefix):] for k in missing][:10],
            foreign=[k[len(prefix):] for k in foreign][:10],
        )
    for rel, body in snapshot.files.items():
        readback = _get_bytes(s3, bucket, f"{prefix}{rel}")
        if readback is None or sha256(readback[0]).hexdigest() != sha256(body).hexdigest():
            raise _error(
                "bundle_unverified",
                f"s3://{bucket}/{prefix}{rel} is "
                f"{'missing' if readback is None else 'different from the published snapshot'}"
                " — repair the release from the System presets panel; nothing was written",
            )


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


@dataclass
class RegistrationOutcome:
    row: SystemSkillRecord | None
    record: dict[str, Any]  # the record as read back from the backend after the operation
    created: bool
    changed: bool  # a create or update was issued against the backend
    submitted: bool  # submit_record issued (new records only)
    note: str | None = None
    # No registry backend is registered in this build: nothing was claimed or written.
    skipped: bool = False

    @property
    def record_id(self) -> str:
        return str(self.record.get("recordId") or (self.row.record_id if self.row else None))

    @property
    def status(self) -> str:
        return str(self.record.get("status") or "")

    def summary(self) -> str:
        if self.skipped:
            return f"system skill record skipped · {self.note or 'registry unavailable'}"
        verb = "created" if self.created else ("updated" if self.changed else "verified · no-op")
        tail = (
            " · submitted for approval" if self.submitted
            else (" · DRAFT — needs review" if self.changed and not self.created
                  else f" · {self.status}")
        )
        return f"system skill record {verb} · {self.record_id}{tail}"


@contextmanager
def _lock(workspace_id: str, preset_key: str):
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        LOCK_DIR / f"skill-registration-{workspace_id}-{preset_key}.lock",
        os.O_RDWR | os.O_CREAT,
        0o600,
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _new_token() -> str:
    # durable intent id of this registration attempt; opaque, never sent anywhere (N4)
    return f"lp-sysskill-{secrets.token_hex(16)}"


def _reset_intent(row: SystemSkillRecord) -> None:
    """Back to a fresh create intent: nothing accepted, nothing to replay."""
    row.record_id = None
    row.record_arn = None
    row.pending_record_id = None
    row.status = STATUS_CREATING
    row.client_token = _new_token()
    row.create_request = None
    row.content_digest = None
    row.release_version = None
    row.release_digest = None
    row.s3_uri = None
    row.intent_version = None
    row.intent_digest = None
    row.intent_content_digest = None


def _claim(
    db: Session, workspace_id: str, preset: SystemPreset, registry_id: str
) -> SystemSkillRecord:
    """The durable mapping row, inserted (and committed) before any Registry write.
    Two racing first registrations hit the unique index; the loser re-reads the
    winner's row and continues against it. A row that belongs to a registry this
    workspace no longer uses restarts as a fresh intent for the current one."""
    row = find_mapping(db, workspace_id, preset.key)
    if row is not None:
        if row.registry_id != registry_id:
            row.registry_id = registry_id
            _reset_intent(row)
            db.commit()
        return row
    row = SystemSkillRecord(
        workspace_id=workspace_id,
        preset_key=preset.key,
        skill_name=preset.name,
        registry_id=registry_id,
        client_token=_new_token(),
        status=STATUS_CREATING,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        row = find_mapping(db, workspace_id, preset.key)
        if row is None:  # pragma: no cover — the constraint fired, so the row exists
            raise
    return row


def _stored_content(record: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """(definition, skill_md) an AGENT_SKILLS record carries; (None, "") when absent."""
    try:
        container = record["descriptors"]["agentSkills"]
        definition = json.loads(container["skillDefinition"]["inlineContent"])
        skill_md = str((container.get("skillMd") or {}).get("inlineContent") or "")
    except (KeyError, TypeError, ValueError):
        return None, ""
    return (definition if isinstance(definition, dict) else None), skill_md


def _same_content(record: dict[str, Any], registration: Registration) -> bool:
    definition, skill_md = _stored_content(record)
    if definition is None:
        return False
    return content_digest(definition, skill_md) == registration.content_digest()


def _remote_release(record: dict[str, Any]) -> tuple[str | None, str | None]:
    """(version, digest) the remote record's own definition claims to describe."""
    definition, _ = _stored_content(record)
    if not definition:
        return None, None
    source = definition.get("source") if isinstance(definition.get("source"), dict) else {}
    version = definition.get("version") or source.get("release_version")
    return (str(version) if version else None), (
        str(source.get("release_digest")) if source.get("release_digest") else None
    )


def _version_tuple(version: str | None) -> tuple[int, ...] | None:
    try:
        return tuple(int(part) for part in str(version or "").split("-", 1)[0].split("."))
    except ValueError:
        return None


def _newer(candidate: str | None, than: str) -> bool:
    have, want = _version_tuple(candidate), _version_tuple(than)
    return have is not None and want is not None and have > want


Release = tuple[str, str | None]  # (version, digest12)


def _current_installed_release(
    db: Session, workspace_id: str, preset: SystemPreset
) -> Release | None:
    """The release the installed preset row pins RIGHT NOW, read as a column value.

    A scalar ``select(Agent.spec)`` goes to the database on every call; loading the
    ``Agent`` entity would hand back the caller's identity-mapped object — exactly the
    stale snapshot a paused request already holds — and compare it against itself.
    """
    spec = db.execute(
        select(Agent.spec).where(
            Agent.workspace_id == workspace_id,
            Agent.system_key == preset.key,
            Agent.status != "deleted",
        )
    ).scalars().first()
    if spec is None:
        return None
    return catalogue.skill_release_from_spec(spec or {})


def _refuse_stale(
    row: SystemSkillRecord,
    registration: Registration,
    installed: Release | None,
    requested: Release | None,
) -> None:
    """Refuse a registration derived from a release that is no longer current — a
    late deploy-job stage, an old build, a request that loaded the preset row before
    a concurrent install rolled it forward. ``requested`` is the identity the caller
    asked for, captured **before** anything is re-read; ``installed`` is what the
    database pins now. Identity is (version, digest): the same version with another
    digest is a different release, never "the same".

    1. the caller's release must be exactly the installed one;
    2. the ledger high-water mark — the last VERIFIED and the last INTENDED release
       (committed before an update whose response may have been lost) — must not name
       a newer version, nor a same-version different digest unless the installed row
       has moved to the caller's release (a legitimate same-version roll-forward);
    3. (in ``_reconcile``) the authoritative remote record's own release.
    """
    if installed is None or requested is None or installed != requested:
        raise _error(
            "stale_release",
            f"this registration was derived from release {requested} but the installed "
            f"preset now pins {installed}; a stale registration cannot rewrite the record",
            installed=list(installed or ()), requested=list(requested or ()),
        )
    version, digest = registration.snapshot.version, registration.snapshot.digest
    for label, mark, mark_digest in (
        ("registered", row.release_version, row.release_digest),
        ("accepted", row.intent_version, row.intent_digest),
    ):
        if _newer(mark, version):
            raise _error(
                "stale_release",
                f"release v{version} is older than the {label} v{mark}; a stale "
                "registration cannot downgrade the record",
                registered=row.release_version, intended=row.intent_version,
                requested=version,
            )
        if mark == version and mark_digest and mark_digest != digest and (
            installed[1] != digest[:12]
        ):  # pragma: no cover — installed == requested already guarantees this
            raise _error(
                "stale_release",
                f"release v{version}-{digest[:12]} differs from the {label} "
                f"v{mark}-{mark_digest[:12]} and is not what is installed",
            )


def _record_verified(
    db: Session,
    row: SystemSkillRecord,
    record: dict[str, Any],
    registration: Registration,
) -> None:
    """Only after a successful, complete read-back: the verified identity + release."""
    row.record_id = str(record.get("recordId") or row.pending_record_id or row.record_id)
    arn = record.get("recordArn") or record.get("arn") or row.record_arn
    row.record_arn = str(arn) if arn else None
    row.pending_record_id = None
    row.status = STATUS_REGISTERED
    row.release_version = registration.snapshot.version
    row.release_digest = registration.snapshot.digest
    row.s3_uri = registration.uri
    row.content_digest = registration.content_digest()
    row.intent_version = registration.snapshot.version
    row.intent_digest = registration.snapshot.digest
    row.intent_content_digest = registration.content_digest()
    db.commit()


def _readback(
    backend: RegistryBackend,
    workspace: WorkspaceContext,
    record_id: str,
    registration: Registration,
    sleeper: Any,
) -> dict[str, Any]:
    """The complete record as the backend holds it, matched against what we wrote.
    Raises the backend's error (a denied read stays a denied read) or
    ``readback_mismatch``."""
    record = backend.wait_record_settled(workspace, record_id, sleeper=sleeper)
    if (
        record.get("name") != registration.name
        or record.get("descriptorType") != DESCRIPTOR_TYPE
        or not _same_content(record, registration)
    ):
        raise _error(
            "readback_mismatch",
            f"registry record {record_id} does not carry the descriptor that was written; "
            "refusing to report the Skill as registered",
            record_id=record_id,
        )
    return record


def _create_request(
    row: SystemSkillRecord, registry_id: str, registration: Registration, workspace_id: str
) -> dict[str, Any]:
    """The persisted create intent: enough to replay the identical registration
    (``imported_at`` is the only non-deterministic input) and to recognise it."""
    return {
        "registryId": registry_id,
        "workspaceId": workspace_id,
        "name": registration.name,
        "description": registration.description(),
        "recordVersion": INITIAL_RECORD_VERSION,
        "imported_at": str(
            ((registration.definition.get("source") or {}).get("imported_at")) or ""
        ),
        "contentDigest": registration.content_digest(),
    }


def _request_registration(
    stored: dict[str, Any], registration: Registration
) -> Registration | None:
    """The registration a persisted intent describes (same bytes), or ``None`` when the
    intent was for another release than the one now current."""
    imported_at = str(stored.get("imported_at") or "")
    replay = build_registration(
        registration.preset, registration.snapshot, registration.bucket, registration.prefix,
        imported_at=imported_at or None,
    )
    if replay.content_digest() != str(stored.get("contentDigest") or ""):
        return None
    return replay


def _create(
    db: Session,
    backend: RegistryBackend,
    registry_id: str,
    row: SystemSkillRecord,
    registration: Registration,
    workspace: WorkspaceContext,
    sleeper: Any,
) -> RegistrationOutcome:
    """First registration, or its recovery.

    Fresh intent (no persisted request): a same-name Skill record already in the
    registry is foreign by definition — this platform never created one — and is
    refused before anything is persisted or written. Otherwise the COMPLETE create
    request is committed to the row, then sent.

    Recovery (a persisted request whose response was lost): the exact same bytes are
    replayed with the same token; the service answering with a record id is the only
    accepted proof that the record is ours. A conflict on the replay means a
    same-name record this platform cannot account for — refused, nothing bound.
    Descriptor metadata is never consulted for ownership.
    """
    stored = row.create_request if isinstance(row.create_request, dict) else None
    replay = _request_registration(stored, registration) if stored else None
    if stored is not None and replay is None:
        # the persisted request was for another release than the one installed now
        # (a rolled-forward install between attempts): the old intent is void
        stored = None
        _reset_intent(row)
        db.commit()
    if stored is None:
        existing = backend.find_record(workspace, registration.name, DESCRIPTOR_TYPE)
        if existing is not None:
            raise _error(
                "foreign_record",
                f"a Skill record named '{registration.name}' ({existing['recordId']}) already "
                "exists and was not created by this platform — rename or delete that record "
                "in the Registry first; nothing was written",
                record_id=existing["recordId"],
            )
        request = _create_request(row, registry_id, registration, workspace.id)
        row.create_request = request
        row.intent_version = registration.snapshot.version
        row.intent_digest = registration.snapshot.digest
        row.intent_content_digest = registration.content_digest()
        db.commit()
        replay = registration
    else:
        request = stored
    created, is_new = backend.upsert_record(
        workspace,
        name=str(request["name"]),
        description=str(request["description"]),
        descriptor_type=DESCRIPTOR_TYPE,
        descriptors=replay.descriptors(),
        record_version=str(request.get("recordVersion") or INITIAL_RECORD_VERSION),
    )
    if not is_new and not (stored is not None and _same_content(created, replay)):
        # A same-name record this platform cannot account for: either it appeared between
        # our lookup and the write, or it is not the record our lost create made (its
        # content differs from the persisted intent). Refused, nothing bound.
        raise _error(
            "foreign_record",
            f"a Skill record named '{registration.name}' exists that this platform cannot "
            "prove it created — inspect the Registry; nothing was bound or written",
            record_id=str(created.get("recordId") or ""),
        )
    record_id = str(created.get("recordId") or str(created.get("recordArn", "")).split("/")[-1])
    # accepted, not verified: protected as ours, never projected as registered
    row.pending_record_id = record_id
    arn = created.get("recordArn") or created.get("arn")
    row.record_arn = str(arn) if arn else None
    row.status = STATUS_ACCEPTED
    db.commit()
    record = _readback(backend, workspace, record_id, replay, sleeper)
    _record_verified(db, row, record, replay)
    # New records enter the existing review workflow (submit → an administrator
    # approves in the Registry). A failed submit leaves an honest DRAFT, never a
    # claimed approval.
    submitted, note = False, None
    try:
        backend.submit_record(workspace, record_id)
        submitted = True
        record = backend.get_record(workspace, record_id)
    except AppError as exc:
        code = exc.code
        note = f"created but not submitted: {code or exc}"
    return RegistrationOutcome(row=row, record=record, created=True, changed=True,
                               submitted=submitted, note=note)


def _verify_pending(
    db: Session,
    backend: RegistryBackend,
    registry_id: str,
    row: SystemSkillRecord,
    registration: Registration,
    workspace: WorkspaceContext,
    sleeper: Any,
) -> RegistrationOutcome:
    """Our create was accepted earlier but never read back: verify it now. The pending
    id came from our own create response, so no ownership question arises; only the
    content is checked. Gone → start over (a new create, reviewed anew)."""
    record_id = str(row.pending_record_id)
    stored = row.create_request if isinstance(row.create_request, dict) else None
    replay = _request_registration(stored, registration) if stored else None
    try:
        record = _readback(backend, workspace, record_id, replay or registration, sleeper)
    except NotFoundError:
        _reset_intent(row)
        db.commit()
        outcome = _create(db, backend, registry_id, row, registration, workspace, sleeper)
        outcome.note = f"accepted record {record_id} was gone; registered anew"
        return outcome
    _record_verified(db, row, record, replay or registration)
    return RegistrationOutcome(row=row, record=record, created=False, changed=False,
                               submitted=False, note="verified the accepted record")


def _reconcile(
    db: Session,
    backend: RegistryBackend,
    registry_id: str,
    row: SystemSkillRecord,
    registration: Registration,
    workspace: WorkspaceContext,
    sleeper: Any,
    installed: Release = ("", None),
) -> RegistrationOutcome:
    record_id = str(row.record_id)
    try:
        record = backend.get_record(workspace, record_id)
    except NotFoundError:
        # Removed outside the console (the console refuses deleting it). Honest
        # recovery: a fresh intent + create; the old id is gone for good.
        _reset_intent(row)
        db.commit()
        outcome = _create(db, backend, registry_id, row, registration, workspace, sleeper)
        outcome.note = f"record {record_id} was gone; {outcome.note or 'registered anew'}"
        return outcome
    if _same_content(record, registration):
        # identical release already registered (or an accepted update whose response
        # was lost): no UpdateRegistryRecord, so the record's approval is untouched
        if (
            row.status != STATUS_REGISTERED
            or row.content_digest != registration.content_digest()
            or row.intent_content_digest != registration.content_digest()
        ):
            _record_verified(db, row, record, registration)
        return RegistrationOutcome(row=row, record=record, created=False, changed=False,
                                   submitted=False)
    # authority 3: the remote record's own release is never regressed — a newer
    # version, or the release the ledger INTENDED (an accepted update whose response
    # was lost) when the caller is not bringing the currently installed release.
    remote_version, remote_digest = _remote_release(record)
    if _newer(remote_version, registration.snapshot.version):
        raise _error(
            "stale_release",
            f"registry record {record_id} already describes release v{remote_version}, newer "
            f"than v{registration.snapshot.version}; a stale registration cannot downgrade it",
            remote=remote_version, requested=registration.snapshot.version,
        )
    if (
        remote_digest
        and remote_digest == row.intent_digest
        and remote_digest != registration.snapshot.digest
        and installed[1] != registration.snapshot.digest[:12]
    ):  # pragma: no cover — the installed check in register_system_skill refuses first
        raise _error(
            "stale_release",
            f"registry record {record_id} carries the accepted release "
            f"v{remote_version}-{remote_digest[:12]}; a stale registration cannot overwrite it",
        )
    if record.get("status") == "DEPRECATED":
        raise _error(
            "record_deprecated",
            f"registry record {record_id} is DEPRECATED — the service treats that as terminal, "
            "so the new release cannot be written onto it. Recovery: delete the deprecated "
            "record in the Registry console, then register again (a new record, reviewed anew)",
            record_id=record_id,
        )
    # the update INTENT is committed first: if the update is accepted and its response
    # lost, the high-water mark already names the newer release
    row.intent_version = registration.snapshot.version
    row.intent_digest = registration.snapshot.digest
    row.intent_content_digest = registration.content_digest()
    db.commit()
    backend.update_record(
        workspace,
        record_id,
        description=registration.description(),
        descriptors=registration.descriptors(),
        record_version=bump_minor_version(
            str(record.get("recordVersion") or INITIAL_RECORD_VERSION)
        ),
    )
    record = _readback(backend, workspace, record_id, registration, sleeper)
    _record_verified(db, row, record, registration)
    # UpdateRegistryRecord resets the record to DRAFT; re-entering review is the
    # administrator's explicit decision (parity with the A2A refresh path).
    return RegistrationOutcome(row=row, record=record, created=False, changed=True,
                               submitted=False)


def register_system_skill(
    db: Session,
    workspace: WorkspaceContext,
    preset: SystemPreset,
    agent: Agent,
    *,
    s3: Any | None = None,
    sleeper: Any = time.sleep,
) -> RegistrationOutcome:
    """Register (or verify, or roll forward) the preset's published Skill release.

    ``agent`` is the installed preset row in ``workspace`` — the caller has already
    decided the row may be registered (an active preset for the admin route; the
    deploying preset inside its own deploy job). The release the caller asks for is
    captured from ``agent`` first; under the lock the CURRENT installed release is read
    as a column value (never through the session's identity map, which would return
    the caller's own stale object) and must be exactly that identity — version AND
    digest — so a request that loaded the row before a concurrent install rolled it
    forward cannot rewrite the newer state. Order, all before the first Registry write: registry
    available → release derived from the stored spec and proven against this build →
    the durable ledger claim → staleness → S3 read-back of the published release →
    then create / verify-pending / no-op / update as described in the module docstring.
    """
    backend = registry_backend.backend_or_none()
    if backend is None:
        return RegistrationOutcome(
            row=None, record={}, created=False, changed=False, submitted=False,
            note="registry unavailable in this build", skipped=True,
        )
    if not agent.system_key or agent.system_key != preset.key:
        raise _error("not_a_preset", f"agent {agent.id} is not the {preset.key} preset", 400)
    # the requested identity, immutable from here on — whatever is re-read later
    requested = catalogue.skill_release_from_spec(agent.spec or {})
    registry_id = _registry_of(workspace)
    bucket = str((workspace.resources or {}).get("artifacts_bucket") or "")
    if not bucket:
        raise _error(
            "workspace_not_ready", "artifacts_bucket missing from this workspace's resource map"
        )
    snapshot, prefix = installed_release(preset, agent, bucket)
    registration = build_registration(preset, snapshot, bucket, prefix)
    with _lock(workspace.id, preset.key):
        installed = _current_installed_release(db, workspace.id, preset)
        if installed is None:
            raise _error("not_installed", f"'{preset.key}' is not installed in this workspace", 404)
        row = _claim(db, workspace.id, preset, registry_id)
        _refuse_stale(row, registration, installed, requested)
        verify_published_release(s3 or workspace.client("s3"), bucket, prefix, snapshot)
        if row.record_id is not None:
            return _reconcile(
                db, backend, registry_id, row, registration, workspace, sleeper, installed
            )
        if row.pending_record_id is not None:
            return _verify_pending(
                db, backend, registry_id, row, registration, workspace, sleeper
            )
        return _create(db, backend, registry_id, row, registration, workspace, sleeper)
