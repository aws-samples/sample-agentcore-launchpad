"""spec.filesystem → the AgentCore ``filesystemConfigurations`` union list.

One mapping for every method that mounts storage: the container runtime, the
Strands zip runtime and the managed Harness (inside its
``environment.agentCoreRuntimeEnvironment``). BYO mounts (S3 Files / EFS) are
container-only — ``AgentSpec`` refuses them elsewhere — so for the other two the
list is at most the managed session storage entry.

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
    return out
