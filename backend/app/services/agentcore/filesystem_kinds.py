"""Which kind a ``filesystemConfigurations`` union member is, and what it needs.

A leaf module (no app imports) so the Runtime wrapper (``agentcore/runtime``), the
deployer's mapping (``deployer/filesystem``, which re-exports it), the storage
catalog and the invoke chain can share one classification without an import cycle.

Only the two NFS-backed members (S3 Files, EFS) are reached through the runtime's
ENIs on port 2049 and therefore need ``networkMode: VPC``. ``s3BucketMount``
(Mountpoint for S3) works on PUBLIC and VPC; ``sessionStorage`` is managed and needs
nothing (devguide ``runtime-filesystem-configurations``, botocore >= 1.44.6).
"""

from typing import Any

SESSION_KIND = "sessionStorage"
S3_FILES_KIND = "s3FilesAccessPoint"
EFS_KIND = "efsAccessPoint"
S3_BUCKET_KIND = "s3BucketMount"

# Members that mount over NFS and so need VPC mode.
NFS_KINDS = frozenset({S3_FILES_KIND, EFS_KIND})
# Customer-owned storage (everything but the managed session storage).
BYO_KINDS = NFS_KINDS | {S3_BUCKET_KIND}

# AgentCore substitutes the invoking session's id for this token in an
# ``s3BucketMount`` prefix.
SESSION_TOKEN = "{runtimeSessionId}"

# The live-view label of each member (`storage_mounts.live_mounts`).
LIVE_KIND = {S3_FILES_KIND: "s3files", EFS_KIND: "efs", S3_BUCKET_KIND: "s3bucket"}


def member_kind(entry: dict[str, Any]) -> str:
    """The union key of one filesystemConfigurations entry ('' if it has none)."""
    for key, member in (entry or {}).items():
        if isinstance(member, dict):
            return str(key)
    return ""


def needs_vpc(entry: dict[str, Any]) -> bool:
    """True for an S3 Files / EFS access-point mount (NFS, port 2049)."""
    return member_kind(entry) in NFS_KINDS


def per_session_prefix(entry: dict[str, Any]) -> bool:
    """True for an ``s3BucketMount`` whose prefix carries ``{runtimeSessionId}``."""
    if member_kind(entry) != S3_BUCKET_KIND:
        return False
    return SESSION_TOKEN in str((entry.get(S3_BUCKET_KIND) or {}).get("prefix") or "")
