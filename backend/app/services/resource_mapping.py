"""Logical resource mapping and spec resolution (roadmap T23).

A bundle's spec carries ids that only exist in the environment it was built in. This
module finds those references, names each one logically, and — given a target workspace —
either rewrites the spec to the target's ids or reports exactly which references have no
mapping there.

**What counts as an environment-specific reference** (kept to what `AgentSpec` actually
carries; a reference the platform does not model cannot be found here):

* `knowledge_bases[].kb_id`         -> kind `kb`
* `memory.memory_id`                -> kind `memory`
* `tools[type=gateway].config.gateway_id` -> kind `gateway`
* `tools[type=gateway].config.record_id`  -> kind `mcp_record` (the approved Registry record)
* `skills[]` entries that are `s3://` prefixes -> kind `skill` (the bucket is per account;
  filesystem-path skills are environment-neutral and left alone)
* `network.profile_id`              -> kind `network_profile` (design projects §12: a VPC
  profile is workspace infrastructure; the target must name its own). It has no implicit
  default — an unmapped profile refuses the release with `network_profile.unmapped`
  (`assert_network_mapped`), and a mapping must name a live profile the project may use.
* `filesystem.mounts[].storage_id`  -> kind `storage` (design storage-mounts §6). The
  catalog id is the logical name, so DEV's `datasets` maps to the target's own
  `datasets` without a mapping row; a row maps it elsewhere. Either way the target entry
  must be live and allowed for the project, or the release is refused with
  `storage.unmapped` (`assert_storage_mapped`); the resolved spec's mounts are re-stamped
  with the target entry's access point (or bucket fields); an entry of another kind than
  the source's reads as unmapped. Inline access points (`filesystem.s3_files` /
  `efs`) carry the source account in their ARN and are always refused
  (`storage.inline_not_promotable`).

Free-form `env` values and the code inside a BYOC artifact are NOT scanned: the platform
cannot tell an id from any other string there. That is a stated limit, not an oversight.

**Naming a reference.** The bundle stores the SOURCE id. To look it up in the target we
need a logical name, found in this order: (1) a mapping in the SOURCE workspace whose
`resource_id` equals it — the team named it on purpose; (2) a name derived from what the
spec itself says (the KB's denormalized name, the tool name, the skill directory);
(3) the id itself. Then the target is asked for `<kind>:<name>`.

**Implicit defaults.** A workspace's own shared gateway and shared memory differ per
workspace by construction, and every agent that uses "the default" references them.
Requiring a hand-written mapping for that would be noise, so the source's
`resources.gateway_id` / `resources.memory_id` resolve to the target's own.
"""

import copy
import dataclasses
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.core.errors import AppError, NotFoundError
from app.core.project_scope import current_project
from app.models.resource_mapping import ResourceMapping

KIND_KB = "kb"
KIND_MEMORY = "memory"
KIND_GATEWAY = "gateway"
KIND_MCP_RECORD = "mcp_record"
KIND_SKILL = "skill"
KIND_NETWORK_PROFILE = "network_profile"
KIND_STORAGE = "storage"
# an inline S3 Files / EFS access point: listed so the gate names it, never mappable
KIND_INLINE_MOUNT = "inline_mount"
KINDS = (KIND_KB, KIND_MEMORY, KIND_GATEWAY, KIND_MCP_RECORD, KIND_SKILL, KIND_NETWORK_PROFILE,
         KIND_STORAGE)

# Who a mapping row is for (design §5.6): a project's own override, or the workspace
# default (project_id NULL) every project of the workspace falls back to.
SCOPE_PROJECT = "project"
SCOPE_WORKSPACE = "workspace"
SCOPES = (SCOPE_PROJECT, SCOPE_WORKSPACE)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
# The same shapes the AgentSpec fields enforce, so a mapping can never produce a spec the
# schema would then refuse.
_KB_ID_RE = re.compile(r"^[A-Za-z0-9]{1,32}$")
_MEMORY_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,127}$")
_TOKEN_RE = re.compile(r"^\S{1,512}$")
# the shape `schemas.agent.NETWORK_PROFILE_ID_RE` accepts
_NETWORK_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")

# the shape `schemas.agent.STORAGE_ID_RE` accepts
_STORAGE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")

NETWORK_UNMAPPED = "network_profile.unmapped"
STORAGE_UNMAPPED = "storage.unmapped"
INLINE_NOT_PROMOTABLE = "storage.inline_not_promotable"

# resources-map keys whose value is the workspace's own default for a kind
_IMPLICIT_RESOURCE_KEY = {KIND_GATEWAY: "gateway_id", KIND_MEMORY: "memory_id"}

Path = tuple[str | int, ...]


def key_of(kind: str, name: str) -> str:
    return f"{kind}:{name}"


def slug(value: str) -> str:
    """A logical-name-safe form of an arbitrary label."""
    cleaned = re.sub(r"[^a-z0-9._-]+", "-", value.strip().lower()).strip("-._")
    return cleaned[:64]


def validate_mapping(kind: str, name: str, resource_id: str) -> None:
    if kind not in KINDS:
        raise AppError(
            "mapping.invalid_kind", f"kind must be one of {', '.join(KINDS)}", status_code=422
        )
    if not NAME_RE.fullmatch(name):
        raise AppError(
            "mapping.invalid_name",
            "a logical name is 1-64 lowercase letters, digits, '.', '_' or '-'",
            status_code=422,
        )
    shape = {
        KIND_KB: _KB_ID_RE,
        KIND_MEMORY: _MEMORY_ID_RE,
        KIND_GATEWAY: _TOKEN_RE,
        KIND_MCP_RECORD: _TOKEN_RE,
        KIND_SKILL: re.compile(r"^s3://\S{3,500}$"),
        KIND_NETWORK_PROFILE: _NETWORK_PROFILE_RE,
        KIND_STORAGE: _STORAGE_RE,
    }[kind]
    if not shape.fullmatch(resource_id):
        raise AppError(
            "mapping.invalid_resource_id",
            f"'{resource_id}' is not a valid {kind} id"
            + (" (an s3:// prefix is expected)" if kind == KIND_SKILL else ""),
            status_code=422,
        )


# ── references ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Reference:
    kind: str
    source_id: str
    path: Path
    # what the spec itself calls it, used to derive a logical name
    hint: str = ""

    @property
    def path_text(self) -> str:
        out = ""
        for part in self.path:
            out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else part)
        return out


def extract_references(spec: dict[str, Any]) -> list[Reference]:
    """Every environment-specific id in `spec`, in a stable order."""
    refs: list[Reference] = []
    for i, kb in enumerate(spec.get("knowledge_bases") or []):
        if isinstance(kb, dict) and kb.get("kb_id"):
            refs.append(
                Reference(
                    KIND_KB, str(kb["kb_id"]), ("knowledge_bases", i, "kb_id"),
                    str(kb.get("name") or ""),
                )
            )
    memory = spec.get("memory") or {}
    if isinstance(memory, dict) and memory.get("memory_id"):
        refs.append(Reference(KIND_MEMORY, str(memory["memory_id"]), ("memory", "memory_id")))
    for i, tool in enumerate(spec.get("tools") or []):
        if not isinstance(tool, dict) or tool.get("type") != "gateway":
            continue
        config = tool.get("config") or {}
        hint = str(tool.get("name") or "")
        if config.get("gateway_id"):
            refs.append(
                Reference(
                    KIND_GATEWAY, str(config["gateway_id"]),
                    ("tools", i, "config", "gateway_id"), hint,
                )
            )
        if config.get("record_id"):
            refs.append(
                Reference(
                    KIND_MCP_RECORD, str(config["record_id"]),
                    ("tools", i, "config", "record_id"), hint,
                )
            )
    for i, skill in enumerate(spec.get("skills") or []):
        if isinstance(skill, str) and skill.startswith("s3://"):
            tail = skill.removesuffix("SKILL.md").rstrip("/").rsplit("/", 1)[-1]
            refs.append(Reference(KIND_SKILL, skill, ("skills", i), tail))
    network = spec.get("network")
    if isinstance(network, dict) and isinstance(network.get("profile_id"), str):
        refs.append(Reference(KIND_NETWORK_PROFILE, network["profile_id"],
                              ("network", "profile_id")))
    fs = spec.get("filesystem") or {}
    if isinstance(fs, dict):
        for i, mount in enumerate(fs.get("mounts") or []):
            if isinstance(mount, dict) and isinstance(mount.get("storage_id"), str):
                refs.append(Reference(KIND_STORAGE, mount["storage_id"],
                                      ("filesystem", "mounts", i, "storage_id"),
                                      mount["storage_id"]))
        for key in ("s3_files", "efs"):
            for i, mount in enumerate(fs.get(key) or []):
                if isinstance(mount, dict) and mount.get("access_point_arn"):
                    refs.append(Reference(KIND_INLINE_MOUNT, str(mount["access_point_arn"]),
                                          ("filesystem", key, i, "access_point_arn"),
                                          str(mount.get("mount_path") or "")))
    return refs


def _set_path(spec: dict[str, Any], path: Path, value: str) -> None:
    node: Any = spec
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = value


# ── mappings ─────────────────────────────────────────────────────────────────────


def mapping_out(row: ResourceMapping) -> dict[str, Any]:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "kind": row.kind,
        "name": row.name,
        "key": key_of(row.kind, row.name),
        "resource_id": row.resource_id,
        "scope": SCOPE_WORKSPACE if row.project_id is None else SCOPE_PROJECT,
        "project_id": row.project_id,
        "note": row.note,
        "updated_by": row.updated_by,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def list_mappings(db: Session, workspace_id: str) -> list[ResourceMapping]:
    return list(
        db.scalars(
            select(ResourceMapping)
            .where(ResourceMapping.workspace_id == workspace_id)
            .order_by(ResourceMapping.kind, ResourceMapping.name)
        ).all()
    )


def _find(
    db: Session, workspace_id: str, kind: str, name: str, scope: str
) -> ResourceMapping | None:
    """The one row a write addresses. Inside a project the read criteria show its own
    rows plus the workspace defaults, so the scope must pick one explicitly — never an
    arbitrary `.first()` that would let any project rewrite the default. Unscoped (no
    current project) the old single lookup applies."""
    query = select(ResourceMapping).where(
        ResourceMapping.workspace_id == workspace_id,
        ResourceMapping.kind == kind,
        ResourceMapping.name == name,
    )
    pid = current_project()
    if scope == SCOPE_WORKSPACE:
        query = query.where(ResourceMapping.project_id.is_(None))
    elif pid is not None:
        query = query.where(ResourceMapping.project_id == pid)
    return db.scalars(query).first()


def upsert_mapping(
    db: Session,
    *,
    workspace_id: str,
    kind: str,
    name: str,
    resource_id: str,
    note: str | None,
    updated_by: str | None,
    scope: str = SCOPE_PROJECT,
) -> ResourceMapping:
    resource_id = resource_id.strip()
    validate_mapping(kind, name, resource_id)
    if kind == KIND_NETWORK_PROFILE:
        _assert_mappable_profile(db, workspace_id, resource_id,
                                 None if scope == SCOPE_WORKSPACE else current_project())
    if kind == KIND_STORAGE:
        _assert_mappable_storage(db, workspace_id, resource_id,
                                 None if scope == SCOPE_WORKSPACE else current_project())
    row = _find(db, workspace_id, kind, name, scope)
    created = row is None
    if row is None:
        row = ResourceMapping(workspace_id=workspace_id, kind=kind, name=name, resource_id="")
        db.add(row)
    row.resource_id = resource_id
    row.note = (note or "").strip() or None
    row.updated_by = updated_by
    db.flush()
    if created and scope == SCOPE_WORKSPACE and row.project_id is not None:
        # the flush stamped the new row with the current (or sole) project; a workspace
        # default is project-less by definition
        db.execute(text("UPDATE resource_mappings SET project_id = NULL WHERE id = :id"),
                   {"id": row.id})
        set_committed_value(row, "project_id", None)
    return row


def delete_mapping(
    db: Session, *, workspace_id: str, kind: str, name: str, scope: str = SCOPE_PROJECT
) -> bool:
    row = _find(db, workspace_id, kind, name, scope)
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True


def _assert_mappable_profile(
    db: Session, workspace_id: str, profile_id: str, project_id: str | None,
) -> None:
    """A network-profile mapping names a live profile of the workspace it lives in —
    and, for a project's own mapping, one in the project's allowed set (any other reads
    as missing, design §12.3)."""
    from app.models.network import NetworkProfile, ProjectNetworkProfile

    row = db.get(NetworkProfile, profile_id)
    hidden = row is None or row.workspace_id != workspace_id or row.archived_at is not None
    if hidden or (
        project_id is not None and db.get(ProjectNetworkProfile, (project_id, profile_id)) is None
    ):
        raise NotFoundError("network_profile.not_found",
                            f"network profile '{profile_id}' not found",
                            {"profile_id": profile_id})


def _assert_mappable_storage(
    db: Session, workspace_id: str, storage_id: str, project_id: str | None,
) -> None:
    """A storage mapping names a live catalog entry of its workspace — for a project's
    own mapping, one in the project's allowed set (any other reads as missing)."""
    from app.services import storage_mounts

    storage_mounts.usable_entry(db, workspace_id, project_id, storage_id)


def _storage_target(
    db: Session, workspace_id: str, storage_id: str, project_id: str | None,
) -> bool:
    try:
        _assert_mappable_storage(db, workspace_id, storage_id, project_id)
    except AppError:
        return False
    return True


def _profile_names(db: Session, workspace_id: str | None, ids: set[str]) -> dict[str, str]:
    """Source profile id → its name: the logical name a reference falls back to."""
    if not workspace_id or not ids:
        return {}
    from app.models.network import NetworkProfile

    rows = db.scalars(select(NetworkProfile).where(NetworkProfile.workspace_id == workspace_id,
                                                   NetworkProfile.id.in_(sorted(ids))))
    return {row.id: row.name for row in rows}


# ── resolution ───────────────────────────────────────────────────────────────────


@dataclass
class Resolution:
    spec: dict[str, Any]
    resolved: list[dict[str, Any]]
    unmapped: list[dict[str, Any]]

    @property
    def complete(self) -> bool:
        return not self.unmapped


def _logical_name(ref: Reference, source_by_id: dict[tuple[str, str], str]) -> str:
    named = source_by_id.get((ref.kind, ref.source_id))
    if named:
        return named
    derived = slug(ref.hint) or slug(ref.source_id)
    return derived or "unnamed"


def _defaults_first(rows: list[ResourceMapping]) -> list[ResourceMapping]:
    return sorted(rows, key=lambda row: row.project_id is not None)


def resolve_spec(
    db: Session,
    spec: dict[str, Any],
    *,
    source_workspace_id: str | None,
    target_workspace_id: str,
    source_resources: dict[str, Any] | None = None,
    target_resources: dict[str, Any] | None = None,
    project_id: str | None = None,
) -> Resolution:
    """Rewrite `spec` for the target workspace, or say what is missing.

    Never mutates `spec`. `Resolution.spec` is a deep copy with every resolvable
    reference replaced; unresolvable ones keep their SOURCE value there and are listed in
    `unmapped` (callers must not deploy a spec whose `complete` is False).
    """
    refs = extract_references(spec)
    out = copy.deepcopy(spec)
    if not refs:
        return Resolution(out, [], [])
    names = _profile_names(db, source_workspace_id,
                           {r.source_id for r in refs if r.kind == KIND_NETWORK_PROFILE})
    refs = [dataclasses.replace(r, hint=names.get(r.source_id, "")) if
            r.kind == KIND_NETWORK_PROFILE else r for r in refs]
    # a project's own mapping overrides the workspace default (§5.6): defaults first,
    # so the project's row is the one left in each dict
    source_by_id: dict[tuple[str, str], str] = {}
    if source_workspace_id:
        for row in _defaults_first(list_mappings(db, source_workspace_id)):
            source_by_id[(row.kind, row.resource_id)] = row.name
    target_by_key = {
        (row.kind, row.name): row
        for row in _defaults_first(list_mappings(db, target_workspace_id))
    }
    src_res, tgt_res = source_resources or {}, target_resources or {}
    resolved: list[dict[str, Any]] = []
    unmapped: list[dict[str, Any]] = []
    for ref in refs:
        name = _logical_name(ref, source_by_id)
        entry = {
            "kind": ref.kind,
            "logical": key_of(ref.kind, name),
            "path": ref.path_text,
            "source_id": ref.source_id,
        }
        row = target_by_key.get((ref.kind, name))
        if row is not None:
            _set_path(out, ref.path, row.resource_id)
            resolved.append({**entry, "target_id": row.resource_id, "via": "mapping"})
            continue
        implicit_key = _IMPLICIT_RESOURCE_KEY.get(ref.kind)
        if (
            implicit_key
            and src_res.get(implicit_key) == ref.source_id
            and tgt_res.get(implicit_key)
        ):
            _set_path(out, ref.path, str(tgt_res[implicit_key]))
            resolved.append(
                {**entry, "target_id": str(tgt_res[implicit_key]), "via": "workspace-default"}
            )
            continue
        if ref.kind == KIND_STORAGE and _storage_target(
            db, target_workspace_id, name, project_id
        ):
            # the catalog id is the logical name: the target's own entry of that id
            _set_path(out, ref.path, name)
            resolved.append({**entry, "target_id": name, "via": "same-id"})
            continue
        if ref.kind == KIND_INLINE_MOUNT:
            unmapped.append({
                **entry, "code": INLINE_NOT_PROMOTABLE,
                "reason": f"the inline access point at {ref.hint or ref.path_text} names the "
                          "source account — register it in the storage catalog and mount it "
                          "by id",
            })
            continue
        if ref.kind == KIND_NETWORK_PROFILE:
            entry["code"] = NETWORK_UNMAPPED
        if ref.kind == KIND_STORAGE:
            entry["code"] = STORAGE_UNMAPPED
        unmapped.append(
            {
                **entry,
                "reason": (
                    f"workspace '{target_workspace_id}' has no mapping for "
                    f"{key_of(ref.kind, name)} (dev id {ref.source_id})"
                ),
            }
        )
    if any(r.kind == KIND_STORAGE for r in refs):
        # the mapped ids name the target's entries: their access points replace the source's
        from app.services import storage_mounts

        mismatched: list[str] = []
        storage_mounts.restamp_spec(db, target_workspace_id, out, project_id,
                                    mismatched=mismatched)
        # the target's entry of that id is another kind of storage (an S3 bucket where the
        # source had an access point, or the reverse): its mount shape differs, so it is
        # not the same storage — refused like an id with no entry
        for entry in [e for e in resolved if e["kind"] == KIND_STORAGE
                      and e.get("target_id") in mismatched]:
            resolved.remove(entry)
            unmapped.append({
                **{k: entry[k] for k in ("kind", "logical", "path", "source_id")},
                "code": STORAGE_UNMAPPED,
                "reason": (f"workspace '{target_workspace_id}' has a storage entry "
                           f"'{entry['target_id']}' of another kind than the source's "
                           f"(dev id {entry['source_id']}) — map it to an entry of the same "
                           "kind, or register one"),
            })
    return Resolution(out, resolved, unmapped)


def resolve_gate(
    db: Session, bundle: Any, target: Any, source: Any | None
) -> dict[str, Any]:
    """The promotion gate entry: `{key, ok, detail}` plus the reference lists."""
    resolution = resolve_spec(
        db,
        bundle.spec or {},
        source_workspace_id=bundle.workspace_id,
        target_workspace_id=target.id,
        source_resources=getattr(source, "resources", None),
        target_resources=getattr(target, "resources", None),
        project_id=getattr(bundle, "project_id", None),
    )
    total = len(resolution.resolved) + len(resolution.unmapped)
    if resolution.complete:
        detail = (
            f"all {total} environment-specific reference(s) map into {target.id}"
            if total
            else "no environment-specific references in the spec"
        )
    else:
        shown = ", ".join(f"{u['logical']} ({u['source_id']})" for u in resolution.unmapped[:3])
        more = len(resolution.unmapped) - 3
        detail = (
            f"{len(resolution.unmapped)} unmapped in {target.id}: {shown}"
            + (f" and {more} more" if more > 0 else "")
        )
    return {
        "key": "resource_mapping",
        "ok": resolution.complete,
        "detail": detail,
        "resolved": resolution.resolved,
        "unmapped": resolution.unmapped,
    }


def assert_network_mapped(
    db: Session,
    spec: dict[str, Any],
    *,
    source_workspace_id: str | None,
    target_workspace_id: str,
    project_id: str | None,
) -> str | None:
    """The target's profile for a promoted spec's network (None: the spec runs PUBLIC
    or names an inline legacy network): its mapping must exist and name a live profile
    the releasing project may use there. Otherwise a 409 `network_profile.unmapped` that
    says which mapping to create — a profile id never means anything in another account.

    Every caller (the execute route, the job admission, the resolve stage) also needs
    the spec's storage to land, so the storage refusals (`assert_storage_mapped`) are
    raised from here too, after the network's."""
    target_profile = _assert_network_profile_mapped(
        db, spec, source_workspace_id=source_workspace_id,
        target_workspace_id=target_workspace_id, project_id=project_id,
    )
    assert_storage_mapped(db, spec, source_workspace_id=source_workspace_id,
                          target_workspace_id=target_workspace_id, project_id=project_id)
    return target_profile


def _assert_network_profile_mapped(
    db: Session,
    spec: dict[str, Any],
    *,
    source_workspace_id: str | None,
    target_workspace_id: str,
    project_id: str | None,
) -> str | None:
    ref = next((r for r in extract_references(spec) if r.kind == KIND_NETWORK_PROFILE), None)
    if ref is None:
        return None
    names = _profile_names(db, source_workspace_id, {ref.source_id})
    ref = dataclasses.replace(ref, hint=names.get(ref.source_id, ""))
    source_by_id: dict[tuple[str, str], str] = {}
    if source_workspace_id:
        for row in _defaults_first(list_mappings(db, source_workspace_id)):
            source_by_id[(row.kind, row.resource_id)] = row.name
    name = _logical_name(ref, source_by_id)
    target = {
        (row.kind, row.name): row
        for row in _defaults_first(list_mappings(db, target_workspace_id))
    }.get((KIND_NETWORK_PROFILE, name))
    detail = {"logical": key_of(KIND_NETWORK_PROFILE, name), "source_id": ref.source_id,
              "target_workspace_id": target_workspace_id}
    if target is None:
        raise AppError(
            NETWORK_UNMAPPED,
            f"the agent runs in network profile '{ref.hint or ref.source_id}', which has no "
            f"mapping in workspace '{target_workspace_id}' — map "
            f"{key_of(KIND_NETWORK_PROFILE, name)} to one of its network profiles first",
            detail, status_code=409,
        )
    try:
        _assert_mappable_profile(db, target_workspace_id, target.resource_id, project_id)
    except AppError as exc:
        raise AppError(
            NETWORK_UNMAPPED,
            f"{key_of(KIND_NETWORK_PROFILE, name)} maps to '{target.resource_id}' in workspace "
            f"'{target_workspace_id}', which is archived or not allowed for this project",
            {**detail, "target_id": target.resource_id}, status_code=409,
        ) from exc
    return target.resource_id


def assert_storage_mapped(
    db: Session,
    spec: dict[str, Any],
    *,
    source_workspace_id: str | None,
    target_workspace_id: str,
    project_id: str | None,
) -> list[str]:
    """The target's catalog ids for a promoted spec's mounts (design storage-mounts §6):
    each mapped (a row, or the target's entry of the same id) to a live entry the
    releasing project may use. An inline access point is refused outright — its ARN
    names the source account. 409 `storage.unmapped` / `storage.inline_not_promotable`
    say what to register or map."""
    refs = [r for r in extract_references(spec) if r.kind in (KIND_STORAGE, KIND_INLINE_MOUNT)]
    inline = [r for r in refs if r.kind == KIND_INLINE_MOUNT]
    if inline:
        paths = ", ".join(r.hint or r.path_text for r in inline)
        raise AppError(
            INLINE_NOT_PROMOTABLE,
            f"the agent mounts inline access point(s) at {paths}, which name the source "
            "account — register the storage in the catalog of both workspaces and mount it "
            "by id before promoting",
            {"paths": [r.path_text for r in inline], "target_workspace_id": target_workspace_id},
            status_code=409,
        )
    if not refs:
        return []
    source_by_id: dict[tuple[str, str], str] = {}
    if source_workspace_id:
        for row in _defaults_first(list_mappings(db, source_workspace_id)):
            source_by_id[(row.kind, row.resource_id)] = row.name
    targets = {
        (row.kind, row.name): row
        for row in _defaults_first(list_mappings(db, target_workspace_id))
    }
    out: list[str] = []
    for ref in refs:
        name = _logical_name(ref, source_by_id)
        mapped = targets.get((KIND_STORAGE, name))
        target_id = mapped.resource_id if mapped is not None else name
        if not _storage_target(db, target_workspace_id, target_id, project_id):
            raise AppError(
                STORAGE_UNMAPPED,
                f"the agent mounts storage '{ref.source_id}', and workspace "
                f"'{target_workspace_id}' has no usable entry for "
                f"{key_of(KIND_STORAGE, name)} — register '{target_id}' there (and allow it "
                "for this project), or map it to another entry",
                {"logical": key_of(KIND_STORAGE, name), "source_id": ref.source_id,
                 "target_id": target_id, "target_workspace_id": target_workspace_id},
                status_code=409,
            )
        if ref.kind == KIND_STORAGE:
            _assert_same_kind(db, spec, ref.source_id, target_workspace_id, target_id, name)
        out.append(target_id)
    return out


def _assert_same_kind(
    db: Session, spec: dict[str, Any], source_id: str, target_workspace_id: str,
    target_id: str, name: str,
) -> None:
    """The target's entry must be the kind of storage the source mounted: an S3 bucket
    mount and an access point mount are different things to stamp and to grant, so a
    kind mismatch is refused like a missing entry (`storage.unmapped`)."""
    from app.services import storage_mounts

    source = next((m.get("kind") for m in storage_mounts.spec_mounts(spec)
                   if m.get("storage_id") == source_id), None)
    row = storage_mounts._row(db, target_workspace_id, target_id)
    if source and row is not None and row.kind != source:
        raise AppError(
            STORAGE_UNMAPPED,
            f"the agent mounts {source} storage '{source_id}', and workspace "
            f"'{target_workspace_id}' has a {row.kind} entry '{target_id}' for "
            f"{key_of(KIND_STORAGE, name)} — register or map an entry of the same kind",
            {"logical": key_of(KIND_STORAGE, name), "source_id": source_id,
             "target_id": target_id, "target_workspace_id": target_workspace_id,
             "source_kind": source, "target_kind": row.kind},
            status_code=409,
        )
