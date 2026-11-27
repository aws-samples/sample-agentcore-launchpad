"""What an agent is told about its BYO mounts (design storage-mounts §3.2).

AgentCore mounts the S3 Files / EFS access points at their paths, but the agent's
code and model do not know they are there. Every method hands the same manifest on:

* code methods (zip / studio / container / BYOC) get ``LAUNCHPAD_MOUNTS`` — JSON
  ``[{name, path, kind, mode}]`` — in the runtime environment; a template-generated
  agent (Strands zip, Claude SDK container) also gets the "available directories"
  paragraph at the end of its system prompt, rendered into the code;
* the harness gets the same paragraph appended to its system prompt (as it does for
  mounted knowledge bases), and the variable in its environment variables.

Managed session storage is not listed: it is on by default for every agent, and
adding it would change every existing agent's environment and prompt. ``mode`` is
``rw`` for every mount in S1 — the execution role is always granted ``ClientWrite``
(`agent_iam.fs_policy_document`); a read-only mode arrives with the storage
catalog (§4.3), and the paragraph says "read-only" for such a mount so the model
does not keep retrying writes.
"""

import json
from typing import Any

from app.schemas.agent import AgentSpec
from app.services.agentcore.filesystem_kinds import OUTPUTS_RETENTION_DAYS

ENV_MOUNTS = "LAUNCHPAD_MOUNTS"

_KIND_LABEL = {"s3files": "S3 Files", "efs": "EFS", "s3bucket": "S3 bucket"}
# Mountpoint for S3 (probe P7, 2026-11-23): sequential writes of whole files only — an
# append fails EPERM, an in-place edit EBADF, a rename ENOSYS
_WHOLE_FILES = ("whole-file writes only — no append, rename or in-place edit; write a new "
                "file or replace it")


def mount_manifest(spec: AgentSpec) -> list[dict[str, str]]:
    """``[{name, path, kind, mode}]`` for the spec's BYO mounts (``[]`` when none). An S3
    bucket mount also carries its ``scope`` (``per_session`` | ``shared``)."""
    out: list[dict[str, str]] = []
    for kind, mount in [*spec.filesystem.byo_mounts(), *spec.filesystem.catalog_mounts()]:
        path = mount.mount_path.rstrip("/")
        entry = {
            "name": path.rsplit("/", 1)[-1],
            "path": path,
            "kind": kind,
            "mode": getattr(mount, "mode", None) or "rw",
        }
        if kind == "s3bucket":
            entry["scope"] = getattr(mount, "scope", None) or "shared"
        out.append(entry)
    outputs = spec.filesystem.outputs
    if outputs is not None:
        path = outputs.mount_path.rstrip("/")
        out.append({"name": path.rsplit("/", 1)[-1], "path": path, "kind": "s3bucket",
                    "mode": "rw", "scope": "per_session", "purpose": "outputs"})
    return out


def mounts_env(spec: AgentSpec) -> dict[str, str]:
    """``{LAUNCHPAD_MOUNTS: <json>}``, or ``{}`` for an agent without BYO mounts."""
    manifest = mount_manifest(spec)
    return {ENV_MOUNTS: json.dumps(manifest, separators=(",", ":"))} if manifest else {}


def with_mounts_env(environment: dict[str, str], spec: AgentSpec) -> dict[str, str]:
    """``environment`` plus the manifest — platform-owned, so it wins over spec.env."""
    environment.update(mounts_env(spec))
    return environment


def prompt_note(spec: AgentSpec) -> str:
    """The "available directories" paragraph for the system prompt ('' when none)."""
    manifest = mount_manifest(spec)
    if not manifest:
        return ""
    lines = []
    for entry in manifest:
        access = "read-only — do not write here" if entry["mode"] == "ro" else "read-write"
        notes = ""
        if entry["kind"] == "s3bucket":
            if entry["scope"] == "per_session":
                notes += ", this session only"
            if entry["mode"] != "ro":
                notes += f"; {_WHOLE_FILES}"
        if entry.get("purpose") == "outputs":
            line = (f"- {entry['path']} (session output folder, read-write; this "
                    "session only): save the files the user should receive here — "
                    "the user can download them from the chat for "
                    f"{OUTPUTS_RETENTION_DAYS} days; {_WHOLE_FILES}")
            if spec.filesystem.session_storage is None:
                # the manifest does not list managed session storage, so say nothing
                # about it while it is on; only claim its absence when it is off
                line += (". This agent has no managed session storage: files you write "
                         "outside this folder (including /tmp) are not kept for later "
                         "sessions")
            lines.append(line)
            continue
        lines.append(f"- {entry['path']} ({_KIND_LABEL.get(entry['kind'], entry['kind'])}, "
                     f"{access}{notes})")
    return (
        "\n\n## Shared directories\n"
        "These directories are mounted into your environment. Unless a line says "
        "otherwise they are shared across sessions (and possibly with other "
        "agents):\n" + "\n".join(lines)
    )


def with_prompt_note(spec: AgentSpec) -> AgentSpec:
    """``spec`` with the paragraph appended to its system prompt, for the renderers of
    generated code; the stored spec is never changed."""
    note = prompt_note(spec)
    if not note:
        return spec
    return spec.model_copy(update={"system_prompt": spec.system_prompt + note})


def describe(spec: AgentSpec) -> str:
    """One log-line summary of the mounts ('' when none)."""
    manifest = mount_manifest(spec)
    if not manifest:
        return ""
    return "mounts: " + ", ".join(f"{m['path']} ({m['kind']}, {m['mode']})" for m in manifest)


def manifest_paths(spec: dict[str, Any] | None) -> set[str]:
    """The BYO mount paths of a stored spec dict (best effort, no validation)."""
    fs = (spec or {}).get("filesystem") or {}
    paths: set[str] = set()
    for key in ("s3_files", "efs"):
        for mount in fs.get(key) or []:
            path = str((mount or {}).get("mount_path") or "").rstrip("/")
            if path:
                paths.add(path)
    if fs.get("outputs") is not None:  # the session output folder (default path when unset)
        paths.add(str((fs["outputs"] or {}).get("mount_path") or "/mnt/outputs").rstrip("/"))
    for mount in fs.get("mounts") or []:  # catalog references
        sid = str((mount or {}).get("storage_id") or "")
        path = str((mount or {}).get("mount_path") or (f"/mnt/{sid}" if sid else "")).rstrip("/")
        if path:
            paths.add(path)
    return paths


def published_mount_paths(db: Any, agent_id: str) -> set[str]:
    """The BYO mount paths any publish of this agent set (its spec snapshots): the
    entries the platform owns on the live Runtime / Harness even after the spec
    dropped them, so a re-publish detaches a removed mount (storage-mounts §3.2) while
    keeping one added outside the platform."""
    from app.models.ledger import SpecSnapshot

    paths: set[str] = set()
    for (spec,) in db.query(SpecSnapshot.spec).filter(SpecSnapshot.agent_id == agent_id):
        paths |= manifest_paths(spec)
    return paths
