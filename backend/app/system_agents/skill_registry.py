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
CreateRegistryRecord returned its id**: the complete create request (token, name,
version, descriptors, timestamp, tags) is persisted before the call, a crash between
the call and the ledger commit is recovered by replaying exactly those bytes with the
same token (the service answers the same record), and a same-name record the replay
cannot account for is refused — never bound, never overwritten. An id AWS accepted but
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
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from botocore.exceptions import ClientError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import DATA_DIR
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.models.ledger import Agent, SystemSkillRecord
from app.services.agentcore import registry as reg
from app.services.agentcore.client import registry_control_client
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
_CONFLICT_CODES = ("ConflictException", "ResourceConflictException", "AlreadyExistsException")
_NOT_FOUND_CODES = ("ResourceNotFoundException", "NotFoundException")


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


def _registry_of(workspace: WorkspaceContext) -> str | None:
    return str((workspace.resources or {}).get("registry_id") or "") or None


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
        return reg.build_skills_descriptors(skill_md=self.skill_md, definition=self.definition)

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
    row: SystemSkillRecord
    record: dict[str, Any]  # the record as read back from AWS after the operation
    created: bool
    changed: bool  # a CreateRegistryRecord or UpdateRegistryRecord was issued
    submitted: bool  # SubmitRegistryRecordForApproval issued (new records only)
    note: str | None = None

    @property
    def record_id(self) -> str:
        return str(self.record.get("recordId") or self.row.record_id)

    @property
    def status(self) -> str:
        return str(self.record.get("status") or "")

    def summary(self) -> str:
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
    # CreateRegistryRecord clientToken: 33–256 chars, [A-Za-z0-9-]
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


def _refuse_stale(
    row: SystemSkillRecord, registration: Registration, current: Agent, agent: Agent
) -> None:
    """Three authorities, any of which refuses a registration derived from an older
    release than the one that is current — a late deploy-job stage, an old build, a
    worker holding a stale ``Agent`` object:

    1. the CURRENT installed preset row, re-read under the lock: the release the
       caller's agent object pins must be exactly (version AND digest) what is
       installed now — a stale object with the same version but another digest is
       refused too;
    2. the ledger high-water mark: the last VERIFIED release and the last INTENDED
       release (committed before the update that may have been accepted with its
       response lost);
    3. (in ``_reconcile``) the authoritative remote record's own release.
    """
    installed = catalogue.skill_release_from_spec(current.spec or {})
    own = catalogue.skill_release_from_spec(agent.spec or {})
    if installed != own:
        raise _error(
            "stale_release",
            f"this registration was derived from release {own} but the installed preset "
            f"now pins {installed}; a stale registration cannot rewrite the record",
            installed=list(installed or ()), requested=list(own or ()),
        )
    version = registration.snapshot.version
    for label, mark in (("registered", row.release_version), ("accepted", row.intent_version)):
        if _newer(mark, version):
            raise _error(
                "stale_release",
                f"release v{version} is older than the {label} v{mark}; a stale "
                "registration cannot downgrade the record",
                registered=row.release_version, intended=row.intent_version,
                requested=version,
            )


def _client_error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code") or "")


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
    client: Any, registry_id: str, record_id: str, registration: Registration, sleeper: Any
) -> dict[str, Any]:
    """The complete record as AWS holds it, matched against what we wrote. Raises the
    AWS error (a denied read stays a denied read) or ``readback_mismatch``."""
    record = reg.wait_record_settled(client, registry_id, record_id, sleeper=sleeper)
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
    return {
        "registryId": registry_id,
        "name": registration.name,
        "displayName": registration.name,
        "description": registration.description(),
        "recordType": "SKILL",
        "descriptors": reg.to_ga_descriptors(DESCRIPTOR_TYPE, registration.descriptors()),
        "recordVersion": INITIAL_RECORD_VERSION,
        "clientToken": row.client_token,
        "tags": {
            "launchpad:system-preset": registration.preset.key,
            "launchpad:workspace": workspace_id,
        },
    }


def _request_registration(
    stored: dict[str, Any], registration: Registration
) -> Registration | None:
    """The registration a persisted create request describes (same bytes), or ``None``
    when the persisted request is for another release than the one now current."""
    try:
        ga = stored["descriptors"]["agentSkillsDefinition"]
        definition = json.loads(ga["data"])
        imported_at = str(((definition.get("source") or {}).get("imported_at")) or "")
    except (KeyError, TypeError, ValueError):
        return None
    replay = build_registration(
        registration.preset, registration.snapshot, registration.bucket, registration.prefix,
        imported_at=imported_at or None,
    )
    if reg.to_ga_descriptors(DESCRIPTOR_TYPE, replay.descriptors()) != stored["descriptors"]:
        return None
    return replay


def _create(
    db: Session,
    client: Any,
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
        existing = reg.find_record(client, registry_id, registration.name, DESCRIPTOR_TYPE)
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
    try:
        created = client.create_registry_record(**request)
    except ClientError as exc:
        if _client_error_code(exc) in _CONFLICT_CODES:
            raise _error(
                "foreign_record",
                f"a Skill record named '{registration.name}' exists that this platform cannot "
                "prove it created (the persisted create request was not accepted as a "
                "replay) — inspect the Registry; nothing was bound or written",
                client_token=row.client_token,
            ) from exc
        raise
    record_id = str(created["recordArn"]).split("/")[-1]
    # accepted, not verified: protected as ours, never projected as registered
    row.pending_record_id = record_id
    row.record_arn = str(created["recordArn"])
    row.status = STATUS_ACCEPTED
    db.commit()
    record = _readback(client, registry_id, record_id, replay, sleeper)
    _record_verified(db, row, record, replay)
    # New records enter the existing review workflow (submit → an administrator
    # approves in the Registry). A failed submit leaves an honest DRAFT, never a
    # claimed approval.
    submitted, note = False, None
    try:
        reg.submit_record(client, registry_id, record_id)
        submitted = True
        record = reg.get_record(client, registry_id, record_id)
    except ClientError as exc:
        note = f"created but not submitted: {_client_error_code(exc) or exc}"
    return RegistrationOutcome(row=row, record=record, created=True, changed=True,
                               submitted=submitted, note=note)


def _verify_pending(
    db: Session,
    client: Any,
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
        record = _readback(client, registry_id, record_id, replay or registration, sleeper)
    except ClientError as exc:
        if _client_error_code(exc) not in _NOT_FOUND_CODES:
            raise
        _reset_intent(row)
        db.commit()
        outcome = _create(db, client, registry_id, row, registration, workspace, sleeper)
        outcome.note = f"accepted record {record_id} was gone; registered anew"
        return outcome
    _record_verified(db, row, record, replay or registration)
    return RegistrationOutcome(row=row, record=record, created=False, changed=False,
                               submitted=False, note="verified the accepted record")


def _reconcile(
    db: Session,
    client: Any,
    registry_id: str,
    row: SystemSkillRecord,
    registration: Registration,
    workspace: WorkspaceContext,
    sleeper: Any,
) -> RegistrationOutcome:
    record_id = str(row.record_id)
    try:
        record = reg.get_record(client, registry_id, record_id)
    except ClientError as exc:
        if _client_error_code(exc) not in _NOT_FOUND_CODES:
            raise
        # Removed outside the console (the console refuses deleting it). Honest
        # recovery: a fresh intent + create; the old id is gone for good.
        _reset_intent(row)
        db.commit()
        outcome = _create(db, client, registry_id, row, registration, workspace, sleeper)
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
    # authority 3: the remote record's own release is never regressed
    remote_version, _ = _remote_release(record)
    if _newer(remote_version, registration.snapshot.version):
        raise _error(
            "stale_release",
            f"registry record {record_id} already describes release v{remote_version}, newer "
            f"than v{registration.snapshot.version}; a stale registration cannot downgrade it",
            remote=remote_version, requested=registration.snapshot.version,
        )
    if record.get("status") == "DEPRECATED":
        raise _error(
            "record_deprecated",
            f"registry record {record_id} is DEPRECATED — the service treats that as terminal, "
            "so the new release cannot be written onto it. Recovery: delete the deprecated "
            "record in the AWS console, then register again (a new record, reviewed anew)",
            record_id=record_id,
        )
    # the update INTENT is committed first: if the update is accepted and its response
    # lost, the high-water mark already names the newer release
    row.intent_version = registration.snapshot.version
    row.intent_digest = registration.snapshot.digest
    row.intent_content_digest = registration.content_digest()
    db.commit()
    client.update_registry_record(
        registryId=registry_id,
        recordId=record_id,
        name=registration.name,
        displayName={"optionalValue": registration.name},
        description={"optionalValue": registration.description()},
        recordType="SKILL",
        descriptors=reg.wrap_descriptors_for_update(
            registration.descriptors(), descriptor_type=DESCRIPTOR_TYPE
        ),
        recordVersion=_bump_minor(str(record.get("recordVersion") or INITIAL_RECORD_VERSION)),
    )
    record = _readback(client, registry_id, record_id, registration, sleeper)
    _record_verified(db, row, record, registration)
    # UpdateRegistryRecord resets the record to DRAFT; re-entering review is the
    # administrator's explicit decision (parity with the A2A refresh path).
    return RegistrationOutcome(row=row, record=record, created=False, changed=True,
                               submitted=False)


def _bump_minor(version: str) -> str:
    from app.services.registry_console import _bump_minor as bump

    return bump(version)


def register_system_skill(
    db: Session,
    workspace: WorkspaceContext,
    preset: SystemPreset,
    agent: Agent,
    *,
    client: Any | None = None,
    s3: Any | None = None,
    sleeper: Any = time.sleep,
) -> RegistrationOutcome:
    """Register (or verify, or roll forward) the preset's published Skill release.

    ``agent`` is the installed preset row in ``workspace`` — the caller has already
    decided the row may be registered (an active preset for the admin route; the
    deploying preset inside its own deploy job). Under the lock the CURRENT installed
    row is re-read and must pin the same release, so a worker holding a stale object
    cannot rewrite newer state. Order, all before the first Registry write: registry
    available → release derived from the stored spec and proven against this build →
    the durable ledger claim → staleness → S3 read-back of the published release →
    then create / verify-pending / no-op / update as described in the module docstring.
    """
    from app.services.registry_console import _registry_id
    from app.system_agents.service import find_installed

    if not agent.system_key or agent.system_key != preset.key:
        raise _error("not_a_preset", f"agent {agent.id} is not the {preset.key} preset", 400)
    registry_id = _registry_id(workspace)  # RegistryUnavailableError (503) when disabled
    bucket = str((workspace.resources or {}).get("artifacts_bucket") or "")
    if not bucket:
        raise _error(
            "workspace_not_ready", "artifacts_bucket missing from this workspace's resource map"
        )
    snapshot, prefix = installed_release(preset, agent, bucket)
    registration = build_registration(preset, snapshot, bucket, prefix)
    with _lock(workspace.id, preset.key):
        current = find_installed(db, workspace.id, preset)
        if current is None:
            raise _error("not_installed", f"'{preset.key}' is not installed in this workspace", 404)
        row = _claim(db, workspace.id, preset, registry_id)
        _refuse_stale(row, registration, current, agent)
        verify_published_release(s3 or workspace.client("s3"), bucket, prefix, snapshot)
        client = client or registry_control_client(workspace)
        if row.record_id is not None:
            return _reconcile(db, client, registry_id, row, registration, workspace, sleeper)
        if row.pending_record_id is not None:
            return _verify_pending(db, client, registry_id, row, registration, workspace, sleeper)
        return _create(db, client, registry_id, row, registration, workspace, sleeper)
