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

# The ``s3bucket`` catalog kind's layout (`models/storage.StorageMount.scope`):
# ``per_session`` mounts ``<entry prefix><agent id>/{runtimeSessionId}/`` (private to one
# session of one agent, platform-forced), ``shared`` mounts the entry prefix as it is.
SCOPE_PER_SESSION = "per_session"
SCOPE_SHARED = "shared"
# What the role policy writes where the runtime substitutes the session id
# (``aws:AgentCoreRuntimeSourceSid`` is the invoking session's id, probed live).
SOURCE_SID_VAR = "${aws:AgentCoreRuntimeSourceSid}"

# The session output folder (`filesystem.outputs`, task 11-23-s3mount-p2): one platform
# bucket per workspace, one prefix per (project, agent, session). One formula for the
# mount, the role policy, the files API and the delete hooks.
OUTPUTS_BUCKET_PREFIX = "launchpad-agent-files-"
OUTPUTS_DEFAULT_PATH = "/mnt/outputs"
OUTPUTS_SHARED_KEY = "shared"  # the project segment of an agent filed under no project
OUTPUTS_RETENTION_DAYS = 30


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


def mounted_prefix(entry_prefix: str | None, scope: str | None, agent_id: str | None) -> str:
    """The ``prefix`` an ``s3BucketMount`` of a catalog entry carries: the entry prefix
    (``''`` or ``x/y/``) for a shared entry, ``<entry prefix><agent id>/{runtimeSessionId}/``
    for a per-session one. A per-session mount cannot be rendered without its agent."""
    base = entry_prefix or ""
    if scope != SCOPE_PER_SESSION:
        return base
    if not agent_id:
        raise ValueError("a per-session S3 bucket mount needs its agent id")
    return f"{base}{agent_id}/{SESSION_TOKEN}/"


def iam_prefix(prefix: str) -> str:
    """``prefix`` as an IAM policy writes it: the session token becomes the
    ``aws:AgentCoreRuntimeSourceSid`` variable."""
    return prefix.replace(SESSION_TOKEN, SOURCE_SID_VAR)


def outputs_bucket_name(account_id: str, region: str) -> str:
    """The workspace's agent-files bucket (names are global; this one carries account
    and Region, as the artifacts bucket does)."""
    return f"{OUTPUTS_BUCKET_PREFIX}{account_id}-{region}"


def outputs_agent_prefix(project_key: str | None, agent_id: str) -> str:
    """``projects/<project|shared>/agents/<agent_id>/`` — everything an agent ever wrote
    to its output folder (what an agent delete removes)."""
    if not agent_id:
        raise ValueError("the session output folder needs its agent id")
    return f"projects/{project_key or OUTPUTS_SHARED_KEY}/agents/{agent_id}/"


def outputs_prefix(project_key: str | None, agent_id: str,
                   session: str = SESSION_TOKEN) -> str:
    """The prefix of one session's output folder. With the default ``session`` it is
    the ``s3BucketMount`` prefix (AgentCore substitutes the session id for the token)."""
    return f"{outputs_agent_prefix(project_key, agent_id)}sessions/{session}/"
