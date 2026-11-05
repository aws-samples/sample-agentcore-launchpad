"""spec.filesystem → the AgentCore ``filesystemConfigurations`` union list.

One mapping for every method that mounts storage: the container runtime, the
Strands zip runtime, BYOC and the managed Harness (inside its
``environment.agentCoreRuntimeEnvironment``). BYO mounts (S3 Files / EFS) are
accepted on every method of ``schemas/agent.BYO_MOUNT_METHODS``, inside the agent's
network profile (storage-mounts §3) — ``AgentSpec`` refuses them on any other method
and without a VPC network — next to the managed session storage entry.

Update semantics differ per API (probed live 2026-10-04, us-west-2):
UpdateAgentRuntime *clears* an omitted ``filesystemConfigurations``, while
UpdateHarness *keeps* an omitted ``environment`` and replaces the list wholesale
when one is sent (``[]`` detaches everything).
"""

from app.schemas.agent import AgentSpec


def filesystem_configurations(spec: AgentSpec) -> list[dict]:
    """The AWS shapes for ``spec.filesystem``; ``[]`` when nothing is mounted."""
    fs = spec.filesystem
    out: list[dict] = []
    if fs.session_storage:
        out.append({"sessionStorage": {"mountPath": fs.session_storage.mount_path}})
    for mount in fs.s3_files:
        out.append({"s3FilesAccessPoint": {
            "accessPointArn": mount.access_point_arn, "mountPath": mount.mount_path,
        }})
    for mount in fs.efs:
        out.append({"efsAccessPoint": {
            "accessPointArn": mount.access_point_arn, "mountPath": mount.mount_path,
        }})
    from app.services.storage_mounts import filesystem_entries

    out.extend(filesystem_entries(spec))  # catalog mounts (storage-mounts §4.2)
    return out


def mount_path(entry: dict) -> str:
    """The ``mountPath`` of one filesystemConfigurations union member ('' if none)."""
    for member in entry.values():
        if isinstance(member, dict) and member.get("mountPath"):
            return str(member["mountPath"]).rstrip("/")
    return ""


def merge_live(live: list[dict] | None, platform_entries: list[dict], *,
               managed_paths=(), on_conflict=None) -> list[dict]:
    """The list an update sends: the platform's entries, plus every live entry made
    outside the platform (design storage-mounts §3.2).

    Both update APIs replace the list wholesale (UpdateAgentRuntime even clears an
    omitted one), so a mount added in the AWS console would be detached by the next
    re-publish unless it is read back and sent again. A live entry is the platform's —
    and dropped — when it is session storage, sits at one of the spec's mount paths,
    or at one of ``managed_paths`` (paths an earlier publish of this agent set, so a
    mount removed from the spec is detached). Any other entry is kept. A live entry at
    a spec path the platform never set that differs from the spec's is a conflict: the
    spec wins and ``on_conflict`` gets one line for the deploy log."""
    spec_paths = {mount_path(fs): fs for fs in platform_entries}
    managed = {str(p).rstrip("/") for p in managed_paths}
    kept = []
    for fs in live or []:
        if "sessionStorage" in fs:
            continue
        path = mount_path(fs)
        if path in spec_paths:
            if path not in managed and fs != spec_paths[path] and on_conflict is not None:
                on_conflict(f"mount {path}: the live entry (set outside the platform) is "
                            "replaced by the spec's")
            continue
        if path in managed:
            continue
        kept.append(fs)
    return [*platform_entries, *kept]


def external_entries(live: list[dict] | None, platform_entries: list[dict],
                     managed_paths=()) -> list[dict]:
    """The live entries ``merge_live`` keeps: those made outside the platform."""
    return merge_live(live, platform_entries, managed_paths=managed_paths)[
        len(platform_entries):]


def runtime_update_mounts(control, runtime_id: str, spec: AgentSpec, *, managed_paths,
                          vpc, log) -> list[dict] | None:
    """``filesystemConfigurations`` for an UpdateAgentRuntime: the spec's entries plus
    the mounts on the live runtime that the platform did not set. Without them the
    update would detach a mount added in the AWS console. A runtime moving to PUBLIC
    cannot keep a BYO mount (AWS refuses one outside a VPC): those are detached and
    the deploy log says so."""
    from app.services.agentcore import runtime as rt

    platform = filesystem_configurations(spec)
    live = rt.get_runtime(control, runtime_id).get("filesystemConfigurations") or []
    kept = external_entries(live, platform, managed_paths)
    if kept and vpc is None:
        for fs in kept:
            log(f"mount {mount_path(fs)}: set outside the platform, detached because the "
                "runtime now runs on the PUBLIC network")
        kept = []
    for fs in kept:
        log(f"mount {mount_path(fs)}: set outside the platform, kept on this update")
    merged = merge_live(live, platform, managed_paths=managed_paths, on_conflict=log)
    merged = [*platform, *kept] if vpc is None else merged
    return merged or None
