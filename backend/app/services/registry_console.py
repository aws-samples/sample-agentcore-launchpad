"""Registry console orchestration on top of the ``RegistryBackend`` Protocol (N4).

The ledger is the registry (``services/registry_local.py``); this module is the
console-only layer the AWS-era service provided — skill bundle ingestion to S3 and the
AGENT_SKILLS record that points at it, record edits/reimports with a minor version bump,
the defaults sync (gateway targets as MCP records + the sample skill), the attachables
catalog the create wizard mounts from, and the live A2A card read. It never names a boto3
registry client; S3 and the AgentCore control/data planes come from the workspace.
"""

import json
import logging
import posixpath
import re
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

from app.core.config import REPO_ROOT
from app.core.errors import AppError, redacted_error
from app.models.ledger import Agent
from app.services import governance, mcp_client, ownership, registry_backend, s3_sweep
from app.services.agentcore import gateway as gateway_api
from app.services.agentcore import runtime as rt
from app.services.agentcore.client import control_client, data_client
from app.services.registry_backend import RegistryBackend, bump_minor_version
from app.services.skill_ingest import (
    SKILL_MD_MAX_BYTES,
    SKILL_NAME_RE,
    SkillBundle,
    SkillValidationError,
    _delete_keys,
    bundle_from_inline,
    bundle_from_source,
    parse_frontmatter,
    upload_bundle_files,
    validate_bundle,
)
from app.services.workspace import WorkspaceContext

if TYPE_CHECKING:
    from app.routers.auth import Identity

SKILLS_DIR = REPO_ROOT / "samples" / "skills"
SKILL_NAME = "expense-report-writer"
_bump_minor = bump_minor_version  # name the restored tests use


def _backend() -> RegistryBackend:
    return registry_backend.get_backend()  # 503 feature.unavailable when none is registered


def _artifacts_bucket(workspace: WorkspaceContext) -> str:
    bucket = workspace.resources.get("artifacts_bucket")
    if not bucket:
        raise RuntimeError(
            "artifacts_bucket missing from this workspace's resource map — run its bootstrap"
        )
    return str(bucket)


def _utcnow_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _require_new_name(backend: RegistryBackend, workspace: WorkspaceContext, name: str,
                      kind: str) -> None:
    if backend.find_record(workspace, name, kind):
        raise AppError(
            "registry.name_exists",
            f"a {kind} record named '{name}' already exists",
            status_code=409,
        )


def require_record_mutable(
    record: dict[str, Any], identity: "Identity | None", record_id: str, action: str
) -> None:
    """Creator-or-admin on a registry record's content (N15-obs7, users-and-ownership.md
    §8): 403 ``registry.forbidden {created_by, created_by_name, record_id, action}`` unless
    ``identity`` is an administrator or the member who created ``record``; a NULL creator
    (legacy, imported, platform default, Skill Lab sample) makes it an administrator's.
    The registry console routes (`PUT` / `reimport` / `DELETE`) and Skill Lab publish
    (N17-obs263 — publish rewrites the SOURCE record) share this one rule."""
    ownership.require_creator_or_admin(
        SimpleNamespace(
            created_by=record.get("createdBy"), created_by_name=record.get("createdByName")
        ),
        identity,
        "registry.forbidden",
        message="only the record's creator or an administrator may edit, reimport, publish or "
                "delete it",
        detail={"record_id": record_id, "action": action},
    )


def refuse_sample_skill_name(name: str) -> None:
    """N17-obs265: the platform sample skill's name (``SKILL_NAME``) is reserved for the
    defaults sync (``_ensure_sample_skill``) — an ordinary register / import /
    import-with-rename under it is refused (409 ``registry.sample_name_reserved``) before
    the name lookup and any S3 write, so a member's bytes never land in the platform
    sample's ``skills/<name>/`` prefix. A member record that already holds the name (from
    before this rule) is left alone (``name_held``)."""
    if name == SKILL_NAME:
        raise AppError(
            "registry.sample_name_reserved",
            f"'{name}' is the platform sample skill's name; it is registered by the "
            "defaults sync, not as an ordinary Skill — choose another name",
            {"name": name},
            status_code=409,
        )


def _mcp_record_url(record: dict[str, Any]) -> str:
    try:
        server = json.loads(record["descriptors"]["mcp"]["server"]["inlineContent"])
        return str((server.get("remotes") or [{}])[0].get("url") or "")
    except (KeyError, TypeError, ValueError, IndexError):
        return ""


# ---------- staging ownership (fix round 1) ----------


def take_staging(
    staging_id: str, *, workspace: WorkspaceContext, principal: str
) -> dict[str, Any]:
    """The staged skill-import session for ``staging_id`` — but only when it was
    minted for THIS exact (workspace, principal) pair.

    ``inspect_skill`` stamps both onto the entry it creates; nobody but the caller
    who inspected the source could have produced this 128-bit token, so an unknown
    id and an ownership mismatch must be indistinguishable to the caller — both
    answer the same 404 ``registry.staging_not_found`` (never a 403, which would
    confirm a staging record exists in another tenant). Every consumer of the
    in-process staging cache (the registry console's inspect→import and
    edit-with-bundle-replace routes, the assistant's preparation import) calls
    this one function so the ownership check cannot be forgotten by a new one.
    """
    from app.routers import registry as registry_router  # local: the staging
    # cache is an in-process dict owned by the router module (not ledger state);
    # importing it at module level here would create a routers<->services cycle.

    registry_router._sweep_staging()
    entry = registry_router._staging.get(staging_id)
    if (
        entry is None
        or entry.get("workspace_id") != workspace.id
        or entry.get("owner_principal") != principal
    ):
        raise AppError(
            "registry.staging_not_found",
            "staging session not found — re-inspect the source",
            status_code=404,
        )
    return entry


# ---------- sample skill + defaults ----------


def _prune_prefix(s3: Any, bucket: str, prefix: str, *, keep: set[str]) -> None:
    """Delete every key under ``prefix`` that is not in ``keep`` (paginated listing, the
    shared reporting deleter); a refused key raises ``S3SweepResidue``."""
    if not prefix:
        raise ValueError("refusing to prune an empty S3 prefix")
    stale: list[str] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        stale.extend(o["Key"] for o in page.get("Contents") or [] if o["Key"] not in keep)
    if stale:
        s3_sweep.delete_keys(s3, bucket, stale).raise_for_residue()


def upload_skill_bundle(
    workspace: WorkspaceContext, skill_name: str = SKILL_NAME
) -> dict[str, Any]:
    """Upload the sample skill bundle to S3; return definition metadata.

    The upload is a COMPLETE bundle write (N17-obs264): every consumer downloads the whole
    ``skills/<name>/`` prefix (zip_runtime, ``_reupload_and_update``), so once this build's
    files are up every other key under the prefix — a file an older release shipped, or
    one a stray write left — is deleted. Pruning AFTER the upload (not clearing first)
    keeps the prefix complete for a deploy reading it mid-refresh; a refused delete raises
    ``S3SweepResidue`` (counts and S3 codes only) instead of shipping the stale file."""
    bucket = _artifacts_bucket(workspace)
    skill_dir = SKILLS_DIR / skill_name
    prefix = f"skills/{skill_name}/"
    s3 = workspace.client("s3")
    files: list[str] = []
    for path in sorted(skill_dir.rglob("*")):
        if path.is_file():
            rel = path.relative_to(skill_dir).as_posix()
            s3.upload_file(str(path), bucket, f"{prefix}{rel}")
            files.append(rel)
    _prune_prefix(s3, bucket, prefix, keep={f"{prefix}{rel}" for rel in files})
    skill_md = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    meta = parse_frontmatter(skill_md)
    return {
        "skill_md": skill_md,
        "definition": {
            "name": skill_name,
            "description": meta.get("description", ""),
            "version": meta.get("version", "1.0.0"),
            "path": f"s3://{bucket}/skills/{skill_name}/",
            "files": files,
            "source": {"kind": "sample", "imported_at": _utcnow_iso()},
        },
    }


# The platform's own gateway targets are system-owned, like system presets: the record
# the import creates for one is approved on the spot, stamped with this actor/reason, so
# the Create-Agent wizard offers them right after bootstrap (N10-bug1). Member-registered
# MCP servers and skills keep the DRAFT → submit → approve flow.
PLATFORM_TARGET_ACTOR = "system"
PLATFORM_TARGET_REASON = (
    "platform gateway target · system-owned · auto-approved by bootstrap/sync-defaults"
)
# N17-obs257 / obs261: a member's sync / import creates a platform-target record and only
# SUBMITS it; this reason marks that submission (older ones read the backend's default
# "submitted via launchpad console" — `platform_target_origin` covers both)
MEMBER_TRIGGERED_REASON = (
    "platform gateway target · submitted by a member's sync / import · awaiting administrator "
    "approval"
)


def _promote_to_approved(
    backend: RegistryBackend, workspace: WorkspaceContext, record_id: str,
    status: str, reason: str,
) -> dict[str, Any]:
    """Walk a record in ``status`` to APPROVED along the lifecycle the backend enforces:
    DRAFT → (submit) PENDING_APPROVAL → (approve) APPROVED; PENDING_APPROVAL / REJECTED →
    APPROVED directly. Anything else is left to the backend's own 409
    ``registry.invalid_transition`` — never guessed here."""
    if status == "DRAFT":
        backend.submit_record(workspace, record_id)
    return backend.set_record_status(workspace, record_id, "APPROVED", reason)


def _approve_platform_target(
    backend: RegistryBackend, workspace: WorkspaceContext, record_id: str
) -> dict[str, Any]:
    """DRAFT → PENDING_APPROVAL → APPROVED for a freshly created gateway-target record,
    decided by ``system`` (the lifecycle only admits APPROVED from PENDING_APPROVAL)."""
    token = registry_backend.current_actor.set(PLATFORM_TARGET_ACTOR)
    try:
        return _promote_to_approved(
            backend, workspace, record_id, "DRAFT", PLATFORM_TARGET_REASON
        )
    finally:
        registry_backend.current_actor.reset(token)


# The two statuses a platform-owned target record can be stuck in on an UPGRADED
# environment (imported before N11 r2-B auto-approved new ones — N11-r3-bug1). Anything
# else is a decision someone took (APPROVED, REJECTED, DEPRECATED) and stays.
_RECONCILABLE_STATUSES = frozenset({"DRAFT", "PENDING_APPROVAL"})


def _has_no_creator(record: dict[str, Any]) -> bool:
    """No account registered this record: the platform paths (bootstrap, sync-defaults,
    gateway import) create rows outside any ``current_creator`` stamp, so both creator
    fields are empty. A console registration always carries at least the caller's name
    (the config admin is ``(None, <name>)``)."""
    return not record.get("createdBy") and not record.get("createdByName")


def is_platform_target_record(record: dict[str, Any], gateway_url: str) -> bool:
    """Whether an existing MCP record is the import's own record for a shared-gateway
    target: its server URL is this workspace's gateway AND no account created it. A
    member-registered MCP server is never one, even under a target's name — neither with
    an external URL nor with the gateway's own URL typed in (N17-obs256: the ``system``
    reconcile must not adopt, and so approve, a record a member authored)."""
    return (
        bool(gateway_url)
        and _mcp_record_url(record) == gateway_url
        and _has_no_creator(record)
    )


def _normalised_url(url: str) -> str:
    """The endpoint identity two spellings of one URL share (N17-obs256, hardened by
    N17-obs257 / rev-r23 R2-2): http and https alike; the percent-unquoted, lower-cased
    hostname with a trailing dot stripped; userinfo dropped; the default port (80/443)
    dropped; the path percent-unquoted, ``;params`` dropped, repeated slashes collapsed and
    ``posixpath.normpath``-ed (``/.``, ``..``), no trailing slash; query and fragment
    dropped. Returned without a scheme: ``host[:port]/path``."""
    raw = url.strip()
    parts = urlsplit(raw)
    if not parts.netloc:
        return raw.rstrip("/").lower()
    host = unquote(parts.hostname or "").lower().rstrip(".")
    try:
        port = parts.port
    except ValueError:  # a non-numeric port: keep whatever follows the host verbatim
        port = None
        host = f"{host}:{parts.netloc.rsplit(':', 1)[-1].lower()}"
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    segments = [seg.split(";", 1)[0] for seg in parts.path.split("/")]
    path = unquote("/".join(segments))
    path = re.sub(r"/{2,}", "/", path)
    path = posixpath.normpath(path) if path else ""
    if path in (".", "/"):
        path = ""
    return f"{netloc}{path.rstrip('/')}"


PLATFORM_URL_RESERVED_CODE = "registry.platform_gateway_url_reserved"


def refuse_platform_gateway_url(workspace: WorkspaceContext, url: str | None) -> None:
    """N17-obs256: the workspace's own gateway URL is the platform's to register — its
    records are created by the import / sync-defaults path, never through the console
    register or edit routes. 422 for every caller."""
    platform_url = str(workspace.resources.get("gateway_url") or "")
    if url and platform_url and _normalised_url(url) == _normalised_url(platform_url):
        raise AppError(
            PLATFORM_URL_RESERVED_CODE,
            "this URL is the workspace's platform gateway — its targets are imported as "
            "MCP records by Registry → Import Gateway targets, not registered by hand",
            status_code=422,
        )


def _settle_new_platform_target(
    backend: RegistryBackend, workspace: WorkspaceContext, record_id: str, *,
    auto_approve: bool,
) -> dict[str, Any]:
    """A freshly created platform-target record (N17-obs257, user ruling 2026-10-22):
    APPROVED by ``system`` when the trigger may approve (host bootstrap, an
    administrator's request); otherwise only SUBMITTED (PENDING_APPROVAL) for an
    administrator — a member can create a target on the platform gateway (Identity →
    gateway targets), so a member-run sync / import must not publish it. The record keeps
    its null creator, so a later admin-triggered sync / import or bootstrap reconciles
    it."""
    if auto_approve:
        return _approve_platform_target(backend, workspace, record_id)
    token = registry_backend.current_actor.set(PLATFORM_TARGET_ACTOR)
    try:
        # N17-obs261: the reason says WHO put it in the queue (a member's trigger), so the
        # record detail and an administrator's import preview can tell it from a platform row
        return backend.set_record_status(
            workspace, record_id, "PENDING_APPROVAL", MEMBER_TRIGGERED_REASON
        )
    finally:
        registry_backend.current_actor.reset(token)


def _reconcile_platform_target(
    backend: RegistryBackend, workspace: WorkspaceContext, record: dict[str, Any],
    gateway_url: str, *, auto_approve: bool,
) -> tuple[dict[str, Any], bool]:
    """Promote an existing platform-owned target record that is still DRAFT or
    PENDING_APPROVAL to APPROVED, decided by ``system`` (N11-r3-bug1) — only when the
    trigger may approve (N17-obs257: never on a member's request). Returns the
    (possibly updated) record and whether a transition happened; a record in any other
    status, or one that is not platform-owned, is returned untouched."""
    status = str(record.get("status") or "")
    if (
        not auto_approve
        or status not in _RECONCILABLE_STATUSES
        or not is_platform_target_record(record, gateway_url)
    ):
        return record, False
    token = registry_backend.current_actor.set(PLATFORM_TARGET_ACTOR)
    try:
        return _promote_to_approved(
            backend, workspace, record["recordId"], status, PLATFORM_TARGET_REASON
        ), True
    finally:
        registry_backend.current_actor.reset(token)


def ensure_default_records(
    workspace: WorkspaceContext, *, log: Callable[[str], None] | None = None,
    auto_approve: bool = False,
) -> list[dict[str, Any]]:
    """Register the shared gateway's targets (one MCP record each, built from the live
    ``tools/list``) + the sample skill bundle (AGENT_SKILLS). Idempotent: existing
    records are left alone, content included (ruling R6: this is the local build's
    gateway → MCP record import; retirement is the ``disable`` action).

    ``auto_approve`` (N17-obs257, user ruling 2026-10-22) is the trigger's authority: True
    for host bootstrap (``make bootstrap``, the workspace bootstrap job) and an
    administrator's ``POST /api/registry/sync-defaults``; False (the default) for a
    member's. With it, new gateway-target records are **APPROVED** immediately
    (platform-owned, N10-bug1) and an existing platform-owned target record still in
    DRAFT or PENDING_APPROVAL (an upgraded environment, or one a member's sync
    submitted) is **reconciled** to APPROVED, decided by ``system`` (N11-r3-bug1).
    Without it, new target records are only **submitted** (PENDING_APPROVAL) for an
    administrator and nothing is reconciled. The new sample-skill record is submitted
    for approval either way; an APPROVED platform sample skill whose bundle version moved
    is re-approved by ``system`` on every trigger (N17-obs262 — platform-shipped content).
    APPROVED / REJECTED / DEPRECATED records and member-registered MCP servers (or a
    member's skill under the sample's name) are never touched. A workspace whose
    resource map carries no ``gateway_url`` (a spoke before its gateway stage, or a build
    without the gateway) imports no targets and only the skill.
    Each result carries ``name``, ``type``, ``record_id``, ``created``, ``reconciled``
    and ``status``.
    """
    backend = _backend()
    emit = log or (lambda _msg: None)
    results: list[dict[str, Any]] = []

    gateway_url = str(workspace.resources.get("gateway_url") or "")
    by_target: dict[str, list[dict[str, Any]]] = {}
    if gateway_url:
        for tool in mcp_client.tools_list(workspace):
            if "___" in tool["name"]:
                target = tool["name"].split("___", 1)[0]
                by_target.setdefault(target, []).append({
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "inputSchema": tool.get("inputSchema", {}),
                })
    else:
        emit("no gateway_url in the resource map — no gateway targets to import")
    for target, target_tools in sorted(by_target.items()):
        description = f"Gateway target {target} · {len(target_tools)} MCP tool(s)"
        record, created = backend.upsert_record(
            workspace,
            name=target,
            description=description,
            descriptor_type="MCP",
            descriptors=registry_backend.build_mcp_descriptors(
                target=target, description=description, gateway_url=gateway_url,
                tools=target_tools,
            ),
        )
        reconciled = False
        if created:
            record = _settle_new_platform_target(
                backend, workspace, record["recordId"], auto_approve=auto_approve
            )
            emit(f"registry: gateway target {target} → MCP record {record['recordId']} "
                 + ("created · APPROVED (system-owned)" if auto_approve
                    else "created · submitted for administrator approval"))
        else:
            before = record.get("status")
            record, reconciled = _reconcile_platform_target(
                backend, workspace, record, gateway_url, auto_approve=auto_approve
            )
            if reconciled:
                emit(f"registry: gateway target {target} already imported "
                     f"({record['recordId']} · {before}) → APPROVED (system-owned, reconciled)")
            elif not is_platform_target_record(record, gateway_url):
                emit(f"registry: gateway target {target} name is held by a record that is "
                     f"not the platform's ({record['recordId']} · {before}) — left alone")
            else:
                emit(f"registry: gateway target {target} already imported "
                     f"({record['recordId']} · {before})")
        results.append({"name": target, "type": "MCP", "record_id": record["recordId"],
                        "created": created, "reconciled": reconciled,
                        "status": record.get("status")})

    results.append(_ensure_sample_skill(
        backend, workspace, lambda: upload_skill_bundle(workspace), emit
    ))
    return results


def _is_skill_kind(record: dict[str, Any]) -> bool:
    """A skill record under either spelling of the kind (platform ``AGENT_SKILLS`` or GA
    ``SKILL``/``skill``) — a legacy row must still count as the sample skill."""
    try:
        return registry_backend.platform_record_type(
            str(record.get("descriptorType") or "")) == "AGENT_SKILLS"
    except ValueError:
        return False


def sample_skill_records(
    backend: RegistryBackend, workspace: WorkspaceContext
) -> list[dict[str, Any]]:
    """Every record of this workspace that IS the sample skill — same name, skill kind —
    oldest first. The key is name + kind, never the record id or version."""
    rows = [r for r in backend.list_records(workspace)
            if r.get("name") == SKILL_NAME and _is_skill_kind(r)]
    return sorted(rows, key=lambda r: (str(r.get("createdAt") or ""), r["recordId"]))


def _skill_definition(record: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(
            record["descriptors"]["agentSkills"]["skillDefinition"]["inlineContent"])
    except (KeyError, TypeError, ValueError):
        return {}


def _ensure_sample_skill(
    backend: RegistryBackend, workspace: WorkspaceContext,
    load_bundle: Callable[[], dict[str, Any]], emit: Callable[[str], None],
) -> dict[str, Any]:
    """Seed the sample skill record idempotently (N11-obs31: a fresh PENDING_APPROVAL
    duplicate appeared on every bootstrap after a release change).

    Lookup is by name + kind over the workspace's records (``sample_skill_records``), and
    only a **platform** row counts — one no account created (``_has_no_creator``; the
    sync / bootstrap path writes it outside any creator stamp). A member-authored skill
    under the sample's name is never adopted, refreshed, deduplicated or approved
    (N17-obs256 provenance): with no platform row beside it, nothing is uploaded or created
    and the result reports ``name_held: true``. Absent → one record, submitted for approval
    (an administrator approves it, whoever triggered — N17-obs257); present → reused. Exact
    platform duplicates are removed, keeping the oldest APPROVED one, else the oldest. When
    the bundle this build ships carries a different ``version`` than the kept record
    describes, the record's content is refreshed (``update_record``); a record that WAS
    approved is re-approved through the platform path (``_approve_platform_target``) on
    EVERY trigger — member, administrator or bootstrap (N17-obs262, user ruling
    2026-10-22): its bytes are the repo's own ``samples/skills/expense-report-writer``
    files (``upload_skill_bundle``), never member input. One still in review is
    re-submitted (R2); a DEPRECATED record is a decision and is left as is. ``load_bundle``
    (the S3 upload) runs only once a platform row may be written. The result carries
    ``updated``, ``removed_duplicates`` and ``name_held`` beside the usual members.
    """
    all_rows = sample_skill_records(backend, workspace)
    rows = [r for r in all_rows if _has_no_creator(r)]
    if all_rows and not rows:
        held = all_rows[0]
        emit(f"registry: sample skill {SKILL_NAME} name is held by a record that is not "
             f"the platform's ({held['recordId']} · {held.get('status')}) — left alone")
        return {"name": SKILL_NAME, "type": "AGENT_SKILLS", "record_id": held["recordId"],
                "created": False, "reconciled": False, "updated": False,
                "removed_duplicates": [], "name_held": True, "status": held.get("status")}
    bundle = load_bundle()
    definition = bundle["definition"]
    descriptors = registry_backend.build_skills_descriptors(
        skill_md=bundle["skill_md"], definition=definition)
    if not rows:
        record, _created = backend.upsert_record(
            workspace, name=SKILL_NAME, description=definition["description"][:200],
            descriptor_type="AGENT_SKILLS", descriptors=descriptors,
        )
        record = backend.submit_record(workspace, record["recordId"])
        emit(f"registry: sample skill {SKILL_NAME} → record {record['recordId']} "
             f"created · submitted for approval")
        return {"name": SKILL_NAME, "type": "AGENT_SKILLS", "record_id": record["recordId"],
                "created": True, "reconciled": False, "updated": False,
                "removed_duplicates": [], "name_held": False, "status": record.get("status")}
    approved = [r for r in rows if r.get("status") == "APPROVED"]
    keep = approved[0] if approved else rows[0]
    removed: list[str] = []
    for dup in rows:
        if dup["recordId"] == keep["recordId"]:
            continue
        backend.delete_record(workspace, dup["recordId"])
        removed.append(dup["recordId"])
        emit(f"registry: sample skill {SKILL_NAME} duplicate {dup['recordId']} "
             f"({dup.get('status')} · v{dup.get('recordVersion')}) removed — keeping "
             f"{keep['recordId']} ({keep.get('status')})")
    updated = False
    stored_version = _skill_definition(keep).get("version")
    if stored_version != definition.get("version") and keep.get("status") != "DEPRECATED":
        was_approved = keep.get("status") == "APPROVED"
        keep = backend.update_record(
            workspace, keep["recordId"], description=definition["description"][:200],
            descriptors=descriptors,
        )
        if was_approved:
            # Lead ruling (round-4 review), user ruling N17-obs262 (2026-10-22): the sample
            # skill is platform-shipped content — whoever triggers the refresh, an
            # APPROVED record must not drop out of every zip_runtime / Studio deploy and
            # the wizard until an admin re-approves. ``keep`` is a platform row (null
            # creator, see above), so no member-authored content is approved here. A
            # record that was still in review stays in review.
            keep = _approve_platform_target(backend, workspace, keep["recordId"])
            verdict = "re-approved (platform-shipped content)"
        else:
            keep = backend.submit_record(workspace, keep["recordId"])
            verdict = "submitted for approval"
        updated = True
        emit(f"registry: sample skill {SKILL_NAME} record {keep['recordId']} refreshed "
             f"(bundle v{stored_version} → v{definition.get('version')}) · {verdict}")
    else:
        emit(f"registry: sample skill {SKILL_NAME} already registered "
             f"({keep['recordId']} · {keep.get('status')})")
    return {"name": SKILL_NAME, "type": "AGENT_SKILLS", "record_id": keep["recordId"],
            "created": False, "reconciled": False, "updated": updated,
            "removed_duplicates": removed, "name_held": False, "status": keep.get("status")}


# ---------- registration (MCP server, skill bundle, inline skill) ----------


def register_mcp_server(
    workspace: WorkspaceContext, name: str, description: str, url: str
) -> dict[str, Any]:
    """Register an external remote MCP server (streamable-http URL) as an MCP record.
    Starts in DRAFT — submit → approve gates when it becomes attachable."""
    backend = _backend()
    _require_new_name(backend, workspace, name, "MCP")
    record, _ = backend.upsert_record(
        workspace,
        name=name,
        description=description or f"remote MCP server · {url}",
        descriptor_type="MCP",
        descriptors=registry_backend.build_mcp_descriptors(
            target=name, description=description, gateway_url=url, tools=None
        ),
    )
    return backend.wait_record_settled(workspace, record["recordId"])


def _definition_or_422(definition: dict[str, Any]) -> None:
    # The serialized definition and SKILL.md share the 102,400-byte descriptor cap a
    # large file list could overflow. Fail at the Launchpad layer BEFORE any S3 write.
    definition_bytes = len(json.dumps(definition).encode("utf-8"))
    if definition_bytes > SKILL_MD_MAX_BYTES:
        raise SkillValidationError(
            f"skill descriptor is {definition_bytes} bytes — exceeds the "
            f"{SKILL_MD_MAX_BYTES} byte limit (too many files or paths too long)"
        )


def register_skill_bundle(
    bundle: SkillBundle,
    workspace: WorkspaceContext,
    *,
    name_override: str | None = None,
    description_override: str | None = None,
) -> dict[str, Any]:
    """Register any acquired skill bundle: validate → refuse reserved names → claim the
    name by creating the AGENT_SKILLS record (DRAFT, real file list + provenance) → upload
    every file under ``bundle.root`` to ``skills/{name}/{rel}``.

    N17-obs265: the record is created BEFORE any S3 write. ``upsert_record`` is
    create-if-absent — when a same-name record appeared after ``_require_new_name`` (a
    concurrent register, a defaults sync) it answers ``created=False``; that is a 409
    ``registry.name_exists`` with NOTHING uploaded, so another record's prefix is never
    overwritten and the caller never gets someone else's record back. If the upload
    fails after the claim, the uploaded objects AND the claimed record are deleted (no
    orphan prefix, no record pointing at missing files). The sample skill's name is
    reserved for the defaults sync (``refuse_sample_skill_name``).

    N17-obs269: after the upload every other key under the prefix is deleted (keep = the
    bundle's files, ``_prune_prefix``), so a residue of a deleted same-name record never
    rides along; a refused delete is a failed registration (rolled back like an upload
    failure, ``S3SweepResidue`` raised — counts and S3 codes only)."""
    name = (name_override or bundle.name or "").strip()
    if not SKILL_NAME_RE.match(name):
        raise SkillValidationError(
            f"skill name '{name}' must match ^[a-z][a-z0-9-]{{2,63}}$ "
            "(set it in SKILL.md frontmatter or provide an override)"
        )
    # A system preset's Skill name is platform-reserved (SE-043) — refused before the
    # name lookup, the upload and the record create.
    from app.system_agents.skill_registry import refuse_reserved_skill_name

    refuse_reserved_skill_name(name)
    refuse_sample_skill_name(name)
    validate_bundle(bundle)
    backend = _backend()
    _require_new_name(backend, workspace, name, "AGENT_SKILLS")
    bucket = _artifacts_bucket(workspace)
    prefix = f"skills/{name}/"
    description = (description_override or bundle.description or "").strip()
    source = replace(bundle.source, imported_at=_utcnow_iso())
    definition = {
        "name": name,
        "description": description,
        "version": bundle.version,
        "path": f"s3://{bucket}/{prefix}",
        "files": bundle.files,
        "source": asdict(source),
    }
    _definition_or_422(definition)
    record, created = backend.upsert_record(
        workspace,
        name=name,
        description=(description or name)[:200],
        descriptor_type="AGENT_SKILLS",
        descriptors=registry_backend.build_skills_descriptors(
            skill_md=bundle.skill_md, definition=definition
        ),
    )
    if not created:
        # lost the race to a record created after the name check: not ours, untouched
        raise AppError(
            "registry.name_exists",
            f"a AGENT_SKILLS record named '{name}' already exists",
            status_code=409,
        )
    s3 = workspace.client("s3")
    uploaded: list[str] = []
    try:
        upload_bundle_files(bundle, bucket, prefix, s3, uploaded=uploaded)
        # N17-obs269: a NEW record never inherits residue — keys a deleted same-name record
        # left under `skills/<name>/` (a refused delete, a stray write) would ship with every
        # deploy that downloads the whole prefix. Pruned AFTER the upload (the obs264 rule);
        # a refused delete raises `S3SweepResidue` into the rollback below.
        _prune_prefix(s3, bucket, prefix, keep=set(uploaded))
    except Exception:
        _delete_keys(s3, bucket, uploaded)  # best-effort orphan cleanup
        try:
            backend.delete_record(workspace, record["recordId"])
        except Exception:  # noqa: BLE001 — the upload failure is the one to report
            _logger.warning("registry: could not drop claimed skill record %s after a "
                            "failed upload", record["recordId"])
        raise
    return backend.wait_record_settled(workspace, record["recordId"])


def register_skill(
    workspace: WorkspaceContext, name: str, description: str, skill_md: str
) -> dict[str, Any]:
    """Register a skill from raw SKILL.md content (the console paste path)."""
    bundle = bundle_from_inline(skill_md)
    try:
        return register_skill_bundle(
            bundle, workspace, name_override=name, description_override=description or None
        )
    finally:
        bundle.close()


# ---------- reimport / update ----------


def _delete_prefix(s3: Any, bucket: str, prefix: str) -> s3_sweep.SweepResult:
    """Delete every object under ``prefix`` (paginated) through the shared reporting deleter
    (N17-obs118): the result names the keys S3 refused (`DeleteObjects` `Errors[]`) instead of
    swallowing them; a raised listing / delete propagates to the caller's guard."""
    return s3_sweep.delete_prefix(s3, bucket, prefix)


_logger = logging.getLogger("launchpad.registry")
SKILLS_PREFIX = "skills/"


def skill_prefix(record: dict[str, Any], bucket: str | None = None) -> str | None:
    """The artifacts-bucket prefix an AGENT_SKILLS record's registration uploaded
    (``skills/<name>/``), read from its definition's ``path`` (``s3://<bucket>/skills/<name>/``)
    with the name as the fallback. ``None`` for any other record type and for anything outside
    ``skills/`` — the versioned ``system-skills/<name>/<version>/`` prefixes of the platform
    presets are disjoint by design and are never a delete target here."""
    if record.get("descriptorType") != "AGENT_SKILLS":
        return None
    definition = _skill_definition(record) or {}
    path = str(definition.get("path") or "")
    prefix = None
    if path.startswith("s3://"):
        rest = path[len("s3://"):]
        prefix = rest.split("/", 1)[1] if "/" in rest else ""
    if not prefix:
        name = str(record.get("name") or "")
        prefix = f"{SKILLS_PREFIX}{name}/" if name else ""
    if not prefix.startswith(SKILLS_PREFIX) or prefix == SKILLS_PREFIX:
        return None
    return prefix if prefix.endswith("/") else prefix + "/"


def _skill_key(uri: str) -> str:
    """One shape for a skill reference however the spec spells it: the deployer accepts
    ``s3://<bucket>/skills/a``, ``s3://<bucket>/skills/a/`` and the bare ``skills/a[/]``
    (``zip_runtime._parse_s3_uri`` + rsplit; ``AgentSpec.skills`` has no validator) — strip an
    ``s3://<bucket>/`` head and normalise to exactly one trailing slash (rev-r17 on cn/r17-j: the
    no-slash URI used to miss the holder match and the files would have been swept)."""
    key = uri.strip()
    if key.startswith("s3://"):
        rest = key[len("s3://"):]
        key = rest.split("/", 1)[1] if "/" in rest else ""
    return key.rstrip("/") + "/" if key else ""


def skill_prefix_holders(
    workspace: WorkspaceContext, prefix: str, *, except_record_id: str | None = None,
) -> list[dict[str, str]]:
    """Who still points at ``prefix``: other live AGENT_SKILLS records whose definition path
    names it, and non-deleted agents of the workspace whose ``spec.skills`` list it (as the
    bare prefix or as an ``s3://…/`` URI). The sweep runs only when this is empty."""
    from app.core.db import SessionLocal

    holders: list[dict[str, str]] = []
    backend = registry_backend.get_backend()
    for other in backend.list_records(workspace, descriptor_type="AGENT_SKILLS"):
        if other.get("recordId") == except_record_id:
            continue
        if skill_prefix(other) == prefix:
            holders.append({"kind": "record", "id": str(other.get("recordId")),
                            "name": str(other.get("name") or "")})
    db = SessionLocal()
    try:
        rows = (
            db.query(Agent)
            .filter(Agent.workspace_id == workspace.id, Agent.status != "deleted")
            .all()
        )
        for agent in rows:
            skills = (agent.spec or {}).get("skills") or []
            if any(isinstance(uri, str) and _skill_key(uri) == prefix for uri in skills):
                holders.append({"kind": "agent", "id": agent.id, "name": agent.name})
    finally:
        db.close()
    return holders


def delete_skill_objects(workspace: WorkspaceContext, record: dict[str, Any]) -> dict[str, Any]:
    """N16-obs35 (fixed in cn/r17-j): after an AGENT_SKILLS record's row is deleted, delete the
    current objects under its ``skills/<name>/`` prefix — unless another record or a live agent
    still references the prefix (then the objects stay and ``kept_for`` names the holders). On a
    versioned bucket this deletes the CURRENT versions (delete markers); noncurrent versions
    follow the bucket's lifecycle rules. Best-effort: an S3 failure is reported in the
    response (``objects_deleted: false, error``), never a failed delete — the row is gone and
    the next registration of the name replaces the prefix anyway. N17-obs118 (cn/r17-t): a
    REFUSED object — a raised ``DeleteObjects`` or a key named in its ``Errors[]`` — is such a
    failure too (``error`` = `redacted_error` / the sweep summary: type, code, operation —
    never an S3 message); ``objects_deleted: true`` means every listed object is gone. Never
    touches ``system-skills/`` (``skill_prefix`` returns ``None`` for it)."""
    prefix = skill_prefix(record)
    if prefix is None:
        return {"objects_deleted": False, "prefix": None}
    holders = skill_prefix_holders(workspace, prefix, except_record_id=record.get("recordId"))
    if holders:
        return {"objects_deleted": False, "prefix": prefix, "kept_for": holders}
    bucket = _artifacts_bucket(workspace)
    try:
        result = _delete_prefix(workspace.client("s3"), bucket, prefix)
    except Exception as exc:  # noqa: BLE001 — best-effort sweep, reported not raised
        return {"objects_deleted": False, "prefix": prefix, "error": redacted_error(exc)}
    if not result.complete:
        _logger.warning("registry: skill sweep of s3://%s/%s left residue: %s",
                        bucket, prefix, result.summary())
        return {"objects_deleted": False, "prefix": prefix, "error": result.summary()}
    return {"objects_deleted": True, "prefix": prefix}


def _skill_definition(record: dict[str, Any]) -> dict[str, Any] | None:
    try:
        old = json.loads(record["descriptors"]["agentSkills"]["skillDefinition"]["inlineContent"])
    except (KeyError, ValueError, TypeError):
        return None
    return old if isinstance(old, dict) else None


def reimport_skill(workspace: WorkspaceContext, record_id: str) -> dict[str, Any]:
    """Re-run the ingestion pipeline for a git/url-sourced skill record: re-acquire from
    the stored provenance, replace the whole S3 prefix, update the record with a bumped
    ``recordVersion`` (→ DRAFT) and a refreshed ``imported_at``. inline/zip records and
    DEPRECATED records are 400 ``registry.not_reimportable``."""
    from app.system_agents.skill_registry import refuse_protected_mutation

    refuse_protected_mutation(workspace, record_id, "reimport")  # before any AWS client
    backend = _backend()
    record = backend.get_record(workspace, record_id)
    if record.get("status") == "DEPRECATED":
        raise AppError("registry.not_reimportable",
                       "a DEPRECATED record is terminal and cannot be reimported",
                       status_code=400)
    name = (record.get("name") or "").strip()
    old_definition = _skill_definition(record)
    if old_definition is None:
        raise AppError("registry.not_reimportable",
                       "record has no readable skill descriptor to reimport", status_code=400)
    source = old_definition.get("source") or {}
    kind = source.get("kind")
    if kind not in ("git", "url"):
        raise AppError(
            "registry.not_reimportable",
            f"only git/url-sourced skills can be reimported (source kind: {kind or 'none'})",
            status_code=400,
        )
    bundle = bundle_from_source(source)
    try:
        return _reupload_and_update(backend, record, name, bundle, workspace)
    finally:
        bundle.close()


def _reupload_and_update(
    backend: RegistryBackend,
    record: dict[str, Any],
    name: str,
    bundle: SkillBundle,
    workspace: WorkspaceContext,
    *,
    description: str | None = None,
) -> dict[str, Any]:
    """Validate the re-acquired bundle, clear the old S3 prefix, upload the fresh files,
    update the record (bumped recordVersion, refreshed provenance). The upload is NOT
    rolled back if the record update fails: the record already points at this prefix, and
    the consumer downloads the whole prefix — fresh files are the right thing to leave."""
    if not SKILL_NAME_RE.match(name):
        raise SkillValidationError(f"record name '{name}' is not a valid skill name")
    validate_bundle(bundle)
    bucket = _artifacts_bucket(workspace)
    prefix = f"skills/{name}/"
    if description is None:
        description = record.get("description") or bundle.description
    description = (description or "").strip()
    source = replace(bundle.source, imported_at=_utcnow_iso())
    definition = {
        "name": name,
        "description": description,
        "version": bundle.version,
        "path": f"s3://{bucket}/{prefix}",
        "files": bundle.files,
        "source": asdict(source),
    }
    _definition_or_422(definition)
    s3 = workspace.client("s3")
    swept = _delete_prefix(s3, bucket, prefix)
    if not swept.complete:
        # the re-upload overwrites what the new bundle still ships; a refused key the bundle
        # dropped is residue the operator hears about (N17-obs118) — the reimport goes on
        _logger.warning("registry: reimport sweep of s3://%s/%s left residue: %s",
                        bucket, prefix, swept.summary())
    for rel in bundle.files:
        s3.upload_file(str(bundle.root / rel), bucket, f"{prefix}{rel}")
    return backend.update_record(
        workspace,
        record["recordId"],
        description=(description or name)[:200],
        descriptors=registry_backend.build_skills_descriptors(
            skill_md=bundle.skill_md, definition=definition
        ),
        record_version=bump_minor_version(record.get("recordVersion") or "1.0.0"),
    )


def update_record(
    record_id: str,
    workspace: WorkspaceContext,
    *,
    description: str | None = None,
    url: str | None = None,
    skill_md: str | None = None,
    bundle: SkillBundle | None = None,
) -> dict[str, Any]:
    """Edit an existing MCP/AGENT_SKILLS record from the console edit sub-page.

    Description-only: the backend keeps status and version. Content edits rebuild the
    descriptors, bump the minor version and (backend) reset the record to DRAFT:
    ``url`` (MCP) · ``skill_md`` (AGENT_SKILLS, overwrites only ``skills/{name}/SKILL.md``)
    · ``bundle`` (AGENT_SKILLS, whole-bundle replace through the reimport path). The name
    is never editable; DEPRECATED and A2A records refuse editing (400 ``registry.not_editable``).
    """
    from app.system_agents.skill_registry import refuse_protected_mutation

    refuse_protected_mutation(workspace, record_id, "replace" if bundle else "edit")
    backend = _backend()
    record = backend.get_record(workspace, record_id)
    if record.get("status") == "DEPRECATED":
        raise AppError("registry.not_editable",
                       "a DEPRECATED record is terminal and cannot be edited", status_code=400)
    rtype = record.get("descriptorType")
    if rtype == "A2A":
        raise AppError("registry.not_editable",
                       "A2A records are managed by agent deploys and cannot be edited here",
                       status_code=400)
    name = (record.get("name") or "").strip()
    new_desc = description if description is not None else (record.get("description") or "")
    if url is not None:
        return backend.update_record(
            workspace, record_id,
            description=(new_desc or name)[:200],
            descriptors=registry_backend.build_mcp_descriptors(
                target=name, description=new_desc, gateway_url=url, tools=None
            ),
            record_version=bump_minor_version(record.get("recordVersion") or "1.0.0"),
        )
    if skill_md is not None:
        return _update_skill_md(backend, record, name, skill_md, new_desc, workspace)
    if bundle is not None:
        return _reupload_and_update(backend, record, name, bundle, workspace,
                                    description=description)
    return backend.update_record(workspace, record_id, description=(new_desc or name)[:200])


def _update_skill_md(
    backend: RegistryBackend,
    record: dict[str, Any],
    name: str,
    skill_md: str,
    description: str,
    workspace: WorkspaceContext,
) -> dict[str, Any]:
    """Overwrite ONLY ``skills/{name}/SKILL.md`` and refresh the descriptor, leaving every
    supporting file untouched. ``files``/``source`` are kept, ``version`` follows the new
    frontmatter (falling back to the old value), ``imported_at`` refreshes. Minor bump."""
    md_bytes = len(skill_md.encode("utf-8"))
    if md_bytes > SKILL_MD_MAX_BYTES:
        raise SkillValidationError(
            f"SKILL.md is {md_bytes} bytes — exceeds the {SKILL_MD_MAX_BYTES} byte limit"
        )
    bucket = _artifacts_bucket(workspace)
    old = _skill_definition(record) or {}
    prefix = f"skills/{name}/"
    meta = parse_frontmatter(skill_md)
    source = old.get("source")
    source = dict(source) if isinstance(source, dict) else {"kind": "inline"}
    source["imported_at"] = _utcnow_iso()
    definition = {
        "name": name,
        "description": (description or old.get("description") or name),
        "version": str(meta.get("version") or old.get("version") or "1.0.0").strip(),
        "path": f"s3://{bucket}/{prefix}",
        "files": old.get("files") or ["SKILL.md"],
        "source": source,
    }
    _definition_or_422(definition)
    s3 = workspace.client("s3")
    s3.put_object(Bucket=bucket, Key=f"{prefix}SKILL.md", Body=skill_md.encode("utf-8"))
    return backend.update_record(
        workspace,
        record["recordId"],
        description=(definition["description"] or name)[:200],
        descriptors=registry_backend.build_skills_descriptors(skill_md=skill_md,
                                                              definition=definition),
        record_version=bump_minor_version(record.get("recordVersion") or "1.0.0"),
    )


# ---------- live agent card ----------


def console_get(workspace: WorkspaceContext, record_id: str) -> dict[str, Any]:
    """One record, any status (Skill Lab evaluates DRAFT skills too)."""
    return _backend().get_record(workspace, record_id)


REAPPROVE_REASON = "re-approved after a Skill Lab publish bumped the record version"


def reapprove_record(
    workspace: WorkspaceContext, record_id: str, *, is_admin: bool = False
) -> dict[str, Any]:
    """Bring a record that an update settled into DRAFT back toward APPROVED, **through**
    the lifecycle (N10v4-bug3). Idempotent — an already-APPROVED record is returned
    untouched (no transition, no new decision stamp).

    * ``is_admin`` — an administrator's request approves: DRAFT → submit → approve,
      PENDING_APPROVAL / REJECTED → approve, decided by the current actor with
      ``REAPPROVE_REASON`` as the status reason.
    * otherwise (a member, the default) — N17-obs248: the approval is an administrator's
      decision, so the record is only **submitted** (DRAFT / REJECTED → PENDING_APPROVAL)
      and stops there; a record already PENDING_APPROVAL is returned untouched.

    A DEPRECATED record surfaces the backend's own 409 ``registry.invalid_transition``."""
    backend = _backend()
    record = backend.get_record(workspace, record_id)
    status = str(record.get("status") or "")
    if status == "APPROVED":
        return record
    if not is_admin:
        if status == "PENDING_APPROVAL":
            return record
        return backend.submit_record(workspace, record_id)
    return _promote_to_approved(backend, workspace, record_id, status, REAPPROVE_REASON)


def console_live_agent_card(
    workspace: WorkspaceContext, record_id: str, agent: Agent
) -> dict[str, Any]:
    """The card the runtime behind an A2A record serves *now* (Runtime data-plane
    ``GetAgentCard``), diffed against the card the record stores. ``agent`` is the
    ledger row whose ``registry_record_id`` is ``record_id`` — the caller vetted it, so
    the ARN handed to the data plane is always the ledger's. Nothing is persisted.

    China availability caveat: ``GetAgentCard`` is part of the Runtime A2A protocol
    surface; if the CN endpoint does not serve it, the data plane answers a ``ClientError``
    the router maps to the standard 4xx/502 envelope — the button fails honestly.
    """
    record = _backend().get_record(workspace, record_id)
    live = rt.get_agent_card(data_client(workspace), runtime_arn=agent.arn or "")
    return {
        "agent_id": agent.id,
        "runtime_arn": agent.arn,
        "status_code": live["status_code"],
        "card": live["card"],
        "diff": registry_backend.agent_card_diff(
            registry_backend.stored_a2a_card(record), live["card"]
        ),
    }


# ---------- attachables catalog ----------


def gateway_endpoint(url: str, workspace: WorkspaceContext) -> str | None:
    """The gateway id when ``url`` is an AgentCore Gateway MCP endpoint of this
    workspace's region and partition (``https://<id>.gateway.bedrock-agentcore.<region>.
    <dns_suffix>/…``), else ``None``. Built from the context — never a literal suffix (C2)."""
    pattern = re.compile(
        r"^https://(?P<gateway_id>[0-9a-z-]+)\.gateway\.bedrock-agentcore\."
        + re.escape(workspace.region) + r"\." + re.escape(workspace.dns_suffix) + r"(?:/|$)"
    )
    match = pattern.match(url or "")
    return match["gateway_id"] if match else None


def _list_live_mcp_gateways(workspace: WorkspaceContext) -> list[dict[str, Any]]:
    client = control_client(workspace)
    return [
        gateway_api.get_gateway(client, summary["gatewayId"])
        for summary in gateway_api.list_gateways(client)
        if summary.get("protocolType") == "MCP"
    ]


def _gateway_auth(gateway: dict[str, Any]) -> tuple[str | None, bool, str | None]:
    """``(auth_type, attachable, reason)``. The CN gateway is AWS_IAM (N3); NONE is
    attachable too; any other authorizer has no Launchpad-managed credential."""
    authorizer = gateway.get("authorizerType")
    if authorizer == "AWS_IAM":
        return "aws_iam", True, None
    if authorizer == "NONE":
        return "none", True, None
    return (
        "oauth" if authorizer == "CUSTOM_JWT" else None,
        False,
        "Gateway outbound auth is not mapped to a Launchpad-managed credential provider",
    )


def _declared_tools(record: dict[str, Any]) -> list[str] | None:
    raw = (record.get("descriptors", {}).get("mcp", {}).get("tools", {}) or {}).get(
        "inlineContent"
    )
    if not raw:
        return None
    try:
        entries = json.loads(raw).get("tools")
    except (ValueError, AttributeError):
        return None
    if isinstance(entries, list) and all(
        isinstance(t, dict) and isinstance(t.get("name"), str) and t["name"].strip()
        for t in entries
    ):
        return sorted({t["name"] for t in entries})
    return None


def _agents_by_record(workspace: WorkspaceContext, record_ids: list[str]) -> dict[str, Agent]:
    """The live ledger agent behind each A2A record id of THIS workspace (newest row wins;
    deleted rows never match) — the same record → agent chain the live-card route walks."""
    from app.core.db import SessionLocal

    if not record_ids:
        return {}
    db = SessionLocal()
    try:
        rows = (
            db.query(Agent)
            .filter(
                Agent.workspace_id == workspace.id,
                Agent.registry_record_id.in_(record_ids),
                Agent.status != "deleted",
            )
            .order_by(Agent.updated_at.desc())
            .all()
        )
        out: dict[str, Agent] = {}
        for row in rows:
            out.setdefault(row.registry_record_id or "", row)
        return out
    finally:
        db.close()


def _a2a_attachables(
    workspace: WorkspaceContext, records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """``agents`` entries of the attachables catalog (N11 r3-C, N10v2-bug5): every APPROVED
    A2A record, joined to the ledger agent that owns it. Attachable only when that agent
    is an active A2A server deployed from this workspace — the consumer's role is granted
    exactly that Runtime ARN, so an orphan card (registered by hand, or whose agent is
    gone/failed) is listed with the reason, never offered."""
    agents = _agents_by_record(workspace, [r["recordId"] for r in records])
    out: list[dict[str, Any]] = []
    for record in records:
        card = registry_backend.stored_a2a_card(record)
        agent = agents.get(record["recordId"])
        skills = [
            {"id": str(s.get("id") or ""), "name": str(s.get("name") or ""),
             "description": str(s.get("description") or "")}
            for s in (card.get("skills") or []) if isinstance(s, dict)
        ]
        reason: str | None = None
        if agent is None:
            reason = ("no Launchpad agent of this workspace owns this record — only agents "
                      "deployed here can be invoked")
        elif (agent.spec or {}).get("protocol") != "a2a":
            reason = "the agent behind this record is not an A2A server"
        elif agent.status != "active" or not agent.arn:
            reason = f"the agent behind this record is not active ({agent.status})"
        out.append({
            "name": record["name"],
            "description": record.get("description", ""),
            "record_id": record["recordId"],
            "agent_id": agent.id if agent is not None else None,
            "runtime_arn": agent.arn if agent is not None and reason is None else None,
            "url": card.get("url"),
            "skills": skills,
            "attachable": reason is None,
            "attachability_reason": reason,
        })
    return out


def attachable_records(
    workspace: WorkspaceContext, *, gateways: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Catalog entries an agent can mount, sourced ONLY from APPROVED records — the
    registry lifecycle is the availability gate. MCP records whose URL matches a live
    AgentCore Gateway of the workspace region expose server-derived attachability; a
    Gateway endpoint that resolves to no live Gateway is listed as an unattachable
    Gateway, never as an unauthenticated server (ruling R8). Other MCP records are
    unauthenticated remote_mcp entries. Malformed descriptors are skipped."""
    backend = _backend()
    gateways = gateways if gateways is not None else _list_live_mcp_gateways(workspace)
    gateways_by_url: dict[str, list[dict[str, Any]]] = {}
    for gateway in gateways:
        if gateway.get("protocolType") == "MCP" and gateway.get("gatewayUrl"):
            gateways_by_url.setdefault(gateway["gatewayUrl"], []).append(gateway)
    mcp_servers: list[dict[str, Any]] = []
    skills: list[dict[str, Any]] = []
    a2a_records: list[dict[str, Any]] = []  # joined to the ledger after the walk (r3-C)
    for record in backend.list_records(workspace, None, "APPROVED"):
        kind = record.get("descriptorType")
        try:
            if kind == "A2A":
                a2a_records.append(record)
            elif kind == "MCP":
                url = _mcp_record_url(record)
                if not url:
                    continue
                matches = gateways_by_url.get(url, [])
                endpoint_like = gateway_endpoint(url, workspace) is not None
                gateway = matches[0] if len(matches) == 1 else None
                entry: dict[str, Any] = {
                    "name": record["name"],
                    "description": record.get("description", ""),
                    "url": url,
                    "gateway": gateway is not None or bool(matches) or endpoint_like,
                    "record_id": record["recordId"],
                }
                if gateway is not None:
                    auth_type, attachable, reason = _gateway_auth(gateway)
                    entry["runtime_tools"] = _declared_tools(record)
                    entry.update({
                        "gateway_id": gateway.get("gatewayId"),
                        "gateway_arn": gateway.get("gatewayArn"),
                        "gateway_name": gateway.get("name"),
                        "attachable": attachable,
                        "attachability_reason": reason,
                        "auth_type": auth_type,
                    })
                else:
                    unresolved = not matches and endpoint_like
                    entry.update({
                        "gateway_id": None,
                        "gateway_arn": None,
                        "attachable": not matches and not unresolved,
                        "attachability_reason": (
                            "the AgentCore Gateway behind this endpoint could not be "
                            "resolved in this account" if unresolved
                            else "multiple live Gateways expose the same endpoint" if matches
                            else None
                        ),
                        "auth_type": "none" if not matches and not unresolved else None,
                    })
                mcp_servers.append(entry)
            elif kind == "AGENT_SKILLS":
                definition = json.loads(
                    record["descriptors"]["agentSkills"]["skillDefinition"]["inlineContent"]
                )
                path = definition.get("path") or ""
                if not path:
                    continue
                skills.append({
                    "name": record["name"],
                    "description": definition.get("description", ""),
                    "path": path,
                    "record_id": record["recordId"],
                })
        except (KeyError, ValueError, TypeError):
            continue  # malformed descriptor — skip, never break the catalog
    return {
        "mcp_servers": mcp_servers,
        "skills": skills,
        "agents": _a2a_attachables(workspace, a2a_records),
    }


# ---------- gateway → MCP record import (WB-005) ----------
#
# Registry → "Import Gateway targets": the console picks an onboarded AgentCore Gateway
# (the governance inventory — every MCP Gateway of the account/region) and ticks its
# targets; each becomes one MCP record, exactly as ``ensure_default_records`` creates
# them for the platform gateway. The platform gateway (this workspace's ``gateway_url``)
# keeps that function's semantics to the letter: live ``tools/list``, APPROVED on the spot
# by ``system``, DRAFT/PENDING rows reconciled. Any other Gateway is a member import:
# the record is created and **submitted** (PENDING_APPROVAL) so an admin approves it,
# like ``register_mcp_server`` + the sample skill — the AWS account is not the workspace
# boundary, so nothing a member points the console at is auto-approved.

GATEWAY_AUTH_NOTE = (
    "Gateway outbound auth is not mapped to a Launchpad-managed credential provider — "
    "agents cannot bind this target until it is"
)
TARGET_NOT_FOUND_CODE = "registry.gateway_target_not_found"
NAME_HELD_CODE = "registry.name_held"


def gateway_auth_label(gateway: dict[str, Any]) -> str:
    """``aws_iam`` | ``none`` | ``oauth`` | ``unmapped`` for the record descriptor and the
    import results — the same classification ``attachable_records`` applies."""
    auth_type, _attachable, _reason = _gateway_auth(gateway)
    return auth_type or "unmapped"


def _auth_warning(gateway: dict[str, Any]) -> str | None:
    _auth_type, attachable, reason = _gateway_auth(gateway)
    return None if attachable else (reason or GATEWAY_AUTH_NOTE)


def is_platform_gateway(gateway: dict[str, Any], workspace: WorkspaceContext) -> bool:
    """Whether ``gateway`` is this workspace's own (bootstrap) gateway: same URL as the
    resource map's ``gateway_url`` (or the same id as ``gateway_id``)."""
    url = str(workspace.resources.get("gateway_url") or "")
    gateway_id = str(workspace.resources.get("gateway_id") or "")
    return (bool(url) and gateway.get("gatewayUrl") == url) or (
        bool(gateway_id) and gateway.get("gatewayId") == gateway_id
    )


def _platform_gateway_id(workspace: WorkspaceContext) -> str:
    gateway_id = str(workspace.resources.get("gateway_id") or "")
    if not gateway_id:
        gateway_id = gateway_endpoint(str(workspace.resources.get("gateway_url") or ""),
                                      workspace) or ""
    if not gateway_id:
        raise AppError(
            "gateway.not_bootstrapped",
            "no gateway_id/gateway_url in this workspace's resource map — pass gateway_id "
            "or run its bootstrap",
            status_code=503,
        )
    return gateway_id


def _gateway_arn(gateway: dict[str, Any], workspace: WorkspaceContext) -> str:
    """AWS's own ``gatewayArn`` (already in the right partition); built from the context
    when a stubbed/partial detail lacks it — never a literal partition (C2)."""
    return str(gateway.get("gatewayArn") or workspace.arn(
        "bedrock-agentcore", f"gateway/{gateway.get('gatewayId', '')}"
    ))


def _tool_entry(name: str, description: Any, input_schema: Any) -> dict[str, Any]:
    return {
        "name": name,
        "description": str(description or ""),
        "inputSchema": input_schema if isinstance(input_schema, dict) else {},
    }


def _live_tools_by_target(
    workspace: WorkspaceContext,
) -> tuple[dict[str, list[dict[str, Any]]], str | None]:
    """``tools/list`` of the platform gateway grouped by target name (the ``target___``
    prefix) — ``ensure_default_records``' source. Fail-soft: an unreachable gateway
    returns ``({}, "<ExcType>: …")`` and the import falls back to the control-plane
    schema, reporting ``live_error`` instead of failing the whole dialog."""
    try:
        tools = mcp_client.tools_list(workspace)
    except Exception as exc:  # the gateway may be mid-rollout — degrade, never 500
        return {}, f"{type(exc).__name__}: {exc}"
    by_target: dict[str, list[dict[str, Any]]] = {}
    for tool in tools:
        name = str(tool.get("name") or "")
        if "___" in name:
            target = name.split("___", 1)[0]
            by_target.setdefault(target, []).append(
                _tool_entry(name, tool.get("description"), tool.get("inputSchema"))
            )
    return by_target, None


def _schema_tools_by_target(targets: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Tools declared by each target's control-plane schema (Lambda/OpenAPI/static MCP/
    Smithy/connector) — Governance's ``discover_actions``; a dynamic MCP-server target
    without a static schema declares none."""
    by_target: dict[str, list[dict[str, Any]]] = {}
    for action in governance.discover_actions(targets):
        by_target.setdefault(action["target_name"], []).append(
            _tool_entry(action["name"], action["description"], action["input_schema"])
        )
    return by_target


def _target_tools(
    name: str, *, live: dict[str, list[dict[str, Any]]], schema: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]] | None, str]:
    """``(tools, source)`` — live ``tools/list`` first (platform gateway), else the
    control-plane schema, else ``(None, "none")`` (tool list unknown; the descriptor
    omits the listing like a member-registered server)."""
    if live.get(name):
        return live[name], "live"
    if schema.get(name):
        return schema[name], "schema"
    return None, "none"


def _target_description(name: str, tools: list[dict[str, Any]] | None, auth: str) -> str:
    parts = [f"Gateway target {name}"]
    parts.append(f"{len(tools)} MCP tool(s)" if tools is not None else "tool list unknown")
    if auth not in ("aws_iam", "none"):
        parts.append(f"outbound auth {auth} · {GATEWAY_AUTH_NOTE}")
    return " · ".join(parts)[:500]


def _list_import_gateways(control: Any) -> list[dict[str, Any]]:
    """The governance inventory: every MCP Gateway of the account/region (``GetGateway``
    detail each) — the same set ``GET /api/governance/gateways`` lists."""
    return governance._all_mcp_gateway_details(control)


def _resolve_import_gateway(
    control: Any, workspace: WorkspaceContext, gateway_id: str | None
) -> dict[str, Any]:
    return governance.require_gateway(control, gateway_id or _platform_gateway_id(workspace))


def _held_by(
    record: dict[str, Any], gateways_by_url: dict[str, tuple[str, str]]
) -> dict[str, Any]:
    """Who holds a target's name (N17-obs250), read-only: the holding record and its
    origin — ``gateway`` when its server URL is the URL of a known gateway
    (``gateways_by_url``: url → (gateway_id, gateway_name), built from what the caller
    already listed — no extra AWS call) and no account created it, else ``member`` (an
    MCP server a member registered — an external URL, or a legacy row carrying a gateway
    URL, N17-obs256)."""
    out: dict[str, Any] = {
        "record_id": record.get("recordId"),
        "name": record.get("name"),
        "status": record.get("status"),
        "origin": "member",
    }
    url = _mcp_record_url(record)
    known = gateways_by_url.get(url) if url and _has_no_creator(record) else None
    if known is not None:
        out.update({"origin": "gateway", "gateway_id": known[0], "gateway_name": known[1]})
    return out


def _held_by_gateways(
    workspace: WorkspaceContext, gateway: dict[str, Any]
) -> dict[str, tuple[str, str]]:
    """url → (gateway_id, gateway_name) for the gateways an import already knows without
    listing the inventory: the resolved one and the workspace's platform gateway (whose
    name the resource map does not carry — its id stands in)."""
    known: dict[str, tuple[str, str]] = {}
    platform_url = str(workspace.resources.get("gateway_url") or "")
    if platform_url:
        platform_id = str(workspace.resources.get("gateway_id") or "")
        known[platform_url] = (platform_id, platform_id or platform_url)
    if gateway.get("gatewayUrl"):
        known[str(gateway["gatewayUrl"])] = (
            str(gateway["gatewayId"]), str(gateway.get("name") or gateway["gatewayId"])
        )
    return known


def platform_target_origin(record: dict[str, Any]) -> str:
    """N17-obs261: where a platform-target record's current state came from — ``member``
    when it waits in PENDING_APPROVAL (the platform's own path never leaves its record there:
    an approving trigger goes straight to APPROVED; only a member's sync / import since
    N17-obs257 — ``MEMBER_TRIGGERED_REASON`` — or a console submit on an older environment
    queues one), else ``platform`` (created by bootstrap / an approving import, or a DRAFT an
    older import left)."""
    return "member" if record.get("status") == "PENDING_APPROVAL" else "platform"


def _target_endpoint(target: dict[str, Any]) -> tuple[str | None, str | None]:
    """``(endpoint, kind)`` of a gateway target from the detail the inventory already
    listed (no extra AWS call, N17-obs261): an MCP-server target's URL, a Lambda target's
    function ARN, an API Gateway target's ``restApiId/stage``; ``kind`` = the
    ``targetConfiguration`` union member (``governance.target_kind``) — schema-only targets
    (OpenAPI / Smithy / connector) have no endpoint of their own."""
    kind = governance.target_kind(target)
    protocol = (target.get("targetConfiguration") or {}).get(kind["protocol"]) or {}
    member = protocol.get(kind["variant"]) if kind["variant"] else None
    endpoint: str | None = None
    if isinstance(member, dict):
        if kind["variant"] == "mcpServer":
            endpoint = member.get("endpoint")
        elif kind["variant"] == "lambda":
            endpoint = member.get("lambdaArn")
        elif kind["variant"] == "apiGateway" and member.get("restApiId"):
            endpoint = "/".join(str(p) for p in (member.get("restApiId"), member.get("stage"))
                                if p)
    return (str(endpoint) if endpoint else None), kind["variant"]


def _preview_targets(
    backend: RegistryBackend, workspace: WorkspaceContext, gateway: dict[str, Any],
    targets: list[dict[str, Any]], *, live: dict[str, list[dict[str, Any]]],
    gateways_by_url: dict[str, tuple[str, str]] | None = None,
    approves: bool = False,
) -> list[dict[str, Any]]:
    """``approves`` (N17-obs261): this preview is for a caller whose import of THIS gateway
    approves — an administrator on the platform gateway; each of its records still DRAFT /
    PENDING_APPROVAL is then marked ``will_approve`` (the import's reconcile)."""
    gateway_url = str(gateway.get("gatewayUrl") or "")
    schema = _schema_tools_by_target(targets)
    out: list[dict[str, Any]] = []
    for target in sorted(targets, key=lambda t: str(t.get("name") or "")):
        name = str(target.get("name") or "")
        tools, source = _target_tools(name, live=live, schema=schema)
        record = backend.find_record(workspace, name, "MCP") if name else None
        ours = record is not None and is_platform_target_record(record, gateway_url)
        held = record is not None and not ours
        endpoint, endpoint_kind = _target_endpoint(target)
        out.append({
            "target_id": target.get("targetId"),
            "target": name,
            "target_status": target.get("status"),
            "tools": len(tools or []),
            "tool_source": source,
            "record_id": record["recordId"] if record is not None else None,
            "status": record.get("status") if record is not None else None,
            "imported": ours,
            "held": held,
            "held_by": _held_by(record, gateways_by_url or {}) if held and record else None,
            "endpoint": endpoint,
            "endpoint_kind": endpoint_kind,
            "origin": platform_target_origin(record) if ours and record else None,
            "will_approve": bool(
                approves and ours and record
                and record.get("status") in _RECONCILABLE_STATUSES
            ),
        })
    return out


def gateway_import_preview(
    workspace: WorkspaceContext, *, control: Any | None = None, auto_approve: bool = False,
) -> dict[str, Any]:
    """What ``import_gateway_targets`` would do, without writing: every MCP Gateway of
    the inventory with its targets, each target's tool count/source and the registry
    record (if any) already holding its name — ``imported`` when that record is this
    gateway's, ``held`` when a different server owns the name (the import will refuse
    it). The platform gateway is listed first.

    N17-obs261: every target carries its ``endpoint`` / ``endpoint_kind`` and, for an
    imported platform record, its ``origin`` (``member`` | ``platform``); with
    ``auto_approve`` (the caller is an administrator, whose import approves) each platform
    record still DRAFT / PENDING_APPROVAL is ``will_approve`` and the platform gateway counts
    them in ``will_approve`` / ``member_submissions_to_approve``."""
    backend = _backend()
    control = control if control is not None else control_client(workspace)
    gateways: list[dict[str, Any]] = []
    inventory = _list_import_gateways(control)
    # N17-obs250: a held name is attributed against this same inventory (no extra call)
    gateways_by_url = {
        str(g["gatewayUrl"]): (str(g["gatewayId"]), str(g.get("name") or g["gatewayId"]))
        for g in inventory if g.get("gatewayUrl")
    }
    for gateway in inventory:
        platform = is_platform_gateway(gateway, workspace)
        live, live_error = _live_tools_by_target(workspace) if platform else ({}, None)
        targets = gateway_api.list_gateway_target_details(control, gateway["gatewayId"])
        rows = _preview_targets(
            backend, workspace, gateway, targets, live=live, gateways_by_url=gateways_by_url,
            approves=platform and auto_approve,
        )
        gateways.append({
            "gateway_id": gateway["gatewayId"],
            "name": gateway.get("name") or gateway["gatewayId"],
            "gateway_arn": _gateway_arn(gateway, workspace),
            "gateway_url": gateway.get("gatewayUrl"),
            "auth": gateway_auth_label(gateway),
            "auth_warning": _auth_warning(gateway),
            "platform": platform,
            "live_error": live_error,
            "targets": rows,
            # N17-obs261: what this caller's import of every target would promote
            "will_approve": sum(1 for r in rows if r["will_approve"]),
            "member_submissions_to_approve": sum(
                1 for r in rows if r["will_approve"] and r["origin"] == "member"),
        })
    gateways.sort(key=lambda g: (not g["platform"], g["name"], g["gateway_id"]))
    return {"gateways": gateways}


def import_gateway_targets(
    workspace: WorkspaceContext, *, gateway_id: str | None = None,
    target_ids: list[str] | None = None, control: Any | None = None,
    log: Callable[[str], None] | None = None, auto_approve: bool = False,
) -> dict[str, Any]:
    """Import the chosen targets of one Gateway as MCP records (WB-005).

    ``gateway_id`` defaults to the platform gateway; ``target_ids=None`` means every target, an
    unknown id is a 404 ``registry.gateway_target_not_found`` before any write. Per target: one
    record named after the target (unique by name in the workspace), descriptors from
    ``build_mcp_descriptors`` + the ``gateway`` block (``gateway_id``, ``gateway_arn``,
    ``auth``, ``runtime_tools``). Platform gateway → ``ensure_default_records`` semantics,
    ``auto_approve`` included (N17-obs257): an administrator's import → APPROVED by ``system``
    when created, DRAFT/PENDING reconciled; a member's → created and submitted
    (PENDING_APPROVAL) as ``system``, nothing reconciled. ``auto_approved`` in the response says
    which. Any other Gateway → created and submitted (PENDING_APPROVAL) for an admin to approve;
    never reconciled. A name already held by a record that is **not** this gateway's (a
    member-registered server, another gateway's target) is a failure item (``error_code:
    registry.name_held``), never a duplicate or an overwrite. Idempotent: a re-run answers
    ``created: false`` and leaves content alone. A non-IAM/non-NONE authorizer still imports but
    every item carries ``auth_warning`` and the record description notes the unmapped auth.
    """
    backend = _backend()
    emit = log or (lambda _msg: None)
    control = control if control is not None else control_client(workspace)
    gateway = _resolve_import_gateway(control, workspace, gateway_id)
    gateway_id = gateway["gatewayId"]
    gateway_url = str(gateway.get("gatewayUrl") or "")
    gateway_arn = _gateway_arn(gateway, workspace)
    platform = is_platform_gateway(gateway, workspace)
    auth = gateway_auth_label(gateway)
    auth_warning = _auth_warning(gateway)

    targets = gateway_api.list_gateway_target_details(control, gateway_id)
    if target_ids is not None:
        wanted = {str(t) for t in target_ids}
        known = {str(t.get("targetId") or "") for t in targets}
        missing = sorted(wanted - known)
        if missing:
            raise AppError(
                TARGET_NOT_FOUND_CODE,
                f"gateway {gateway_id} has no target(s) {', '.join(missing)}",
                status_code=404,
                detail={"gateway_id": gateway_id, "target_ids": missing},
            )
        targets = [t for t in targets if str(t.get("targetId") or "") in wanted]

    live, live_error = _live_tools_by_target(workspace) if platform else ({}, None)
    schema = _schema_tools_by_target(targets)
    # N17-obs250: a held name names its holder; only the gateways already at hand are
    # known here (this one and the platform gateway by URL) — no inventory listing
    held_by_gateways = _held_by_gateways(workspace, gateway)
    results: list[dict[str, Any]] = []
    for target in sorted(targets, key=lambda t: str(t.get("name") or "")):
        name = str(target.get("name") or "")
        tools, source = _target_tools(name, live=live, schema=schema)
        item: dict[str, Any] = {
            "name": name, "target": name, "target_id": target.get("targetId"),
            "gateway_id": gateway_id, "record_id": None, "created": False,
            "reconciled": False, "status": None, "tool_source": source,
        }
        if auth_warning:
            item["auth_warning"] = auth_warning
        existing = backend.find_record(workspace, name, "MCP") if name else None
        if existing is not None and not is_platform_target_record(existing, gateway_url):
            item.update({
                "record_id": existing["recordId"], "status": existing.get("status"),
                "held_by": _held_by(existing, held_by_gateways),
                "error_code": NAME_HELD_CODE,
                "error": (f"name '{name}' is held by a record that is not this gateway's "
                          f"({existing['recordId']} · {existing.get('status')}) — rename or "
                          f"retire it first"),
            })
            emit(f"registry: gateway {gateway_id} target {name} — {item['error']}")
            results.append(item)
            continue
        description = _target_description(name, tools, auth)
        record, created = backend.upsert_record(
            workspace,
            name=name,
            description=description,
            descriptor_type="MCP",
            descriptors=registry_backend.build_mcp_descriptors(
                target=name, description=description, gateway_url=gateway_url, tools=tools,
                gateway={
                    "gateway_id": gateway_id,
                    "gateway_arn": gateway_arn,
                    "auth": auth,
                    "runtime_tools": [t["name"] for t in (tools or [])],
                    **({"auth_note": auth_warning} if auth_warning else {}),
                },
            ),
        )
        reconciled = False
        if created and platform:
            record = _settle_new_platform_target(
                backend, workspace, record["recordId"], auto_approve=auto_approve
            )
            emit(f"registry: gateway target {name} → MCP record {record['recordId']} "
                 + ("created · APPROVED (system-owned)" if auto_approve
                    else "created · submitted for administrator approval"))
        elif created:
            record = backend.submit_record(workspace, record["recordId"])
            emit(f"registry: gateway {gateway_id} target {name} → MCP record "
                 f"{record['recordId']} created · submitted for approval")
        elif platform:
            record, reconciled = _reconcile_platform_target(
                backend, workspace, record, gateway_url, auto_approve=auto_approve
            )
            emit(f"registry: gateway target {name} already imported ({record['recordId']} · "
                 f"{record.get('status')}){' · reconciled' if reconciled else ''}")
        else:
            emit(f"registry: gateway {gateway_id} target {name} already imported "
                 f"({record['recordId']} · {record.get('status')})")
        item.update({"record_id": record["recordId"], "created": created,
                     "reconciled": reconciled, "status": record.get("status")})
        results.append(item)
    return {
        "gateway_id": gateway_id,
        "gateway_name": gateway.get("name") or gateway_id,
        "gateway_arn": gateway_arn,
        "auth": auth,
        "platform": platform,
        # N17-obs257: whether this trigger approved platform records (admin) or only
        # submitted them for an administrator (member)
        "auto_approved": platform and auto_approve,
        "live_error": live_error,
        "results": results,
    }
