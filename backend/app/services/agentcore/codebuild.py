"""CodeBuild wrapper for the container image path (explicit client, injectable sleep)."""

import time
from collections.abc import Callable
from typing import Any

TERMINAL = {"SUCCEEDED", "FAILED", "FAULT", "TIMED_OUT", "STOPPED"}


def start_image_build(
    client: Any,
    *,
    project: str,
    s3_bucket: str,
    s3_key: str,
    region: str,
    ecr_registry: str,
    ecr_repo: str,
    image_tag: str,
    extra_env: dict[str, str] | None = None,
) -> str:
    """Start a docker build from a source zip on S3. Returns the build id. ``extra_env``
    (N17-obs465) adds the buildspec's optional switches (LAUNCHPAD_CN_TRACE_LAYER, …)."""
    build = client.start_build(
        projectName=project,
        sourceTypeOverride="S3",
        sourceLocationOverride=f"{s3_bucket}/{s3_key}",
        environmentVariablesOverride=[
            {"name": "AWS_REGION", "value": region, "type": "PLAINTEXT"},
            {"name": "ECR_REGISTRY", "value": ecr_registry, "type": "PLAINTEXT"},
            {"name": "ECR_REPO", "value": ecr_repo, "type": "PLAINTEXT"},
            {"name": "IMAGE_TAG", "value": image_tag, "type": "PLAINTEXT"},
            *({"name": k, "value": v, "type": "PLAINTEXT"}
              for k, v in sorted((extra_env or {}).items())),
        ],
    )["build"]
    return build["id"]


def wait_build(
    client: Any,
    build_id: str,
    timeout_s: int = 1800,
    interval_s: int = 10,
    sleeper: Callable[[float], None] = time.sleep,
    on_phase: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Poll the build, streaming phase transitions; raise on any terminal failure."""
    deadline = time.monotonic() + timeout_s
    last_phase = None
    while True:
        build = client.batch_get_builds(ids=[build_id])["builds"][0]
        phase = build.get("currentPhase", "UNKNOWN")
        if phase != last_phase and on_phase:
            on_phase(phase)
        last_phase = phase
        status = build.get("buildStatus")
        if status in TERMINAL:
            if status != "SUCCEEDED":
                failed = [
                    f"{p.get('phaseType')}: {ctx.get('message', '')}"
                    for p in build.get("phases", [])
                    if p.get("phaseStatus") == "FAILED"
                    for ctx in p.get("contexts", [{}])
                ]
                raise RuntimeError(
                    f"codebuild {build_id} ended {status} — {'; '.join(failed) or 'see logs'}"
                )
            return build
        if time.monotonic() > deadline:
            raise TimeoutError(f"codebuild {build_id} still {phase} after {timeout_s}s")
        sleeper(interval_s)
