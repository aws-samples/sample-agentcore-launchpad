"""Target-based canary infrastructure helpers (pure, injected-client style).

Phase 2a of the production canary: the AWS-touching building blocks the canary
state machine (Phase 2b, ``canary_service``) composes — minting a candidate
runtime version, standing up a dedicated per-canary Gateway, and pinning the
stable/treatment named endpoints. Every function takes its AWS client(s) as
arguments (no ``control_client()`` factory here) so unit tests inject stubs and
never touch pip / S3 / AWS.

Topology (Model 1): one runtime, two immutable versions. ``stable`` (named
endpoint) is pinned to the live ``v_current``; ``treatment`` to the minted
``v_candidate``. A dedicated Gateway fronts a target-based A/B test that splits
live traffic between the two endpoints while the canary runs. Because
``UpdateAgentRuntime`` auto-rolls DEFAULT to the new version, the stable
endpoint — not DEFAULT — is what keeps production on ``v_current``.
"""

import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.deployer.environment import runtime_environment
from app.deployer.zip_runtime import (
    _generate_code,
    _method_requirements,
    build_zip,
    bundle_skills,
    write_bundle_files,
)
from app.schemas.agent import AgentSpec
from app.services import agent_iam
from app.services.agentcore import runtime as rt
from app.services.workspace import default_workspace_context

_sleep = time.sleep  # injectable
_NOT_FOUND = {"ResourceNotFoundException", "NotFoundException"}


def _noop(_msg: str) -> None:
    pass


Log = Callable[[str], None]


def _is_conflict(exc: Exception) -> bool:
    return type(exc).__name__ == "ConflictException"


# ─── telemetry naming ────────────────────────────────────────────────────────
# A named endpoint gets its own content-log group at endpoint-create time
# (verified live 2026-07-14): /aws/bedrock-agentcore/runtimes/{resource_id}-<ENDPOINT>.
# service.name is assumed to mirror the DEFAULT form ({runtime_name}.DEFAULT,
# see service.stage_gateway) as {runtime_name}.<ENDPOINT>. Per-variant online-eval
# points control→stable, treatment→treatment using these.


def endpoint_log_group(resource_id: str, endpoint_name: str) -> str:
    """Content-log group for a named runtime endpoint."""
    return f"/aws/bedrock-agentcore/runtimes/{resource_id}-{endpoint_name}"


def endpoint_service_name(runtime_name: str, endpoint_name: str) -> str:
    """span service.name for a named runtime endpoint."""
    return f"{runtime_name}.{endpoint_name}"


# ─── runtime versions ────────────────────────────────────────────────────────
def current_version(control_client: Any, runtime_id: str) -> str:
    """The runtime's current (live) version as a string."""
    return str(rt.get_runtime(control_client, runtime_id)["agentRuntimeVersion"])


def _default_uploader(local_path: str, bucket: str, key: str) -> None:
    default_workspace_context().client("s3").upload_file(local_path, bucket, key)


def candidate_s3_key(agent_name: str, canary_id: str, role: str) -> str:
    """Deterministic key for a canary's zip: one object per canary + role.

    A uuid per mint orphaned a ~37MB object on every attempt (ISSUE-011); keyed
    this way a retried mint overwrites its own object, and cleanup knows exactly
    which keys the record owns.
    """
    return f"agents/{agent_name}/canary/{canary_id}-{role}.zip"


def delete_object_quiet(key: str, *, bucket: str | None = None,
                        s3_client: Any = None) -> None:
    """Delete one artifacts-bucket object. Raises — the caller decides whether a
    failure is fatal (cleanup records it as skipped)."""
    settings = get_settings()
    target = bucket or settings.resources.get("artifacts_bucket")
    if not target:
        raise RuntimeError("artifacts_bucket missing from config")
    client = s3_client or default_workspace_context().client("s3")
    client.delete_object(Bucket=target, Key=key)


def mint_candidate_version(
    *,
    agent: Any,
    edited_spec: AgentSpec,
    control_client: Any,
    canary_id: str,
    role: str = "candidate",
    log: Log = _noop,
    pip_runner: Callable[..., Any] = subprocess.run,
    uploader: Callable[[str, str, str], None] | None = None,
    build_root: Path | None = None,
) -> tuple[str, str, str]:
    """Publish a candidate immutable version of ``agent``'s runtime from an
    edited spec, returning ``(v_current, v_candidate, s3_key)``.

    ``canary_id``/``role`` key the uploaded zip (see ``candidate_s3_key``); the
    caller records the returned key on the canary artifacts so cleanup can
    delete it.

    The candidate zip is built with the same blocks as the deploy pipeline
    (``_generate_code`` / ``_method_requirements`` / ``build_zip``), uploaded to
    a canary-scoped S3 key, then published in place via ``UpdateAgentRuntime``
    (same ARN — a new version). ``UpdateAgentRuntime`` auto-rolls DEFAULT to the
    candidate; the caller (Phase 2b) pins a stable endpoint to ``v_current`` so
    production keeps serving the current behavior.

    CRITICAL: this NEVER mutates a ledger ``Agent`` row (no session, no version/
    spec/status write). Versions are recorded only in the canary's artifacts by
    the caller. ``pip_runner`` / ``uploader`` / ``build_root`` are test seams so
    unit tests run no real pip, S3, or AWS.
    """
    if edited_spec.method not in {"zip_runtime", "studio"}:
        # FOLLOW-UP: container candidates need a CodeBuild image push (not a
        # zip); Phase 2b's canary_capability gates container out until then.
        raise NotImplementedError(
            "container candidate minting via CodeBuild is a follow-up"
        )

    settings = get_settings()
    bucket = settings.resources.get("artifacts_bucket")
    if not bucket:
        raise RuntimeError(
            "artifacts_bucket missing from config — run scripts/bootstrap.py"
        )
    # A candidate is a new *version of the same runtime*, so it keeps whatever role
    # production is already on. Swapping in the shared role would measure the
    # candidate with permissions production does not have, and a promotion would
    # inherit them. Read from the live resource rather than derived from the agent
    # name, so an agent deployed before per-agent roles existed still works.
    live = rt.get_runtime(control_client, agent.resource_id)
    role_arn = agent_iam.live_runtime_role_arn(live, settings)
    if not role_arn:
        raise RuntimeError(
            "the runtime reports no execution role and execution_role_arn is missing "
            "from config — run scripts/bootstrap.py"
        )

    v_current = str(live["agentRuntimeVersion"])
    log(f"current production version {v_current}")

    code, source = _generate_code(edited_spec)
    requirements = _method_requirements(edited_spec)
    root = build_root or Path(tempfile.gettempdir())
    build_dir = root / f"launchpad_canary_{agent.name}"

    def _on_pkg_ready(pkg_dir: Path) -> None:
        if edited_spec.code_bundle:
            write_bundle_files(edited_spec, pkg_dir)
        bundle_skills(edited_spec, code, pkg_dir, log)

    zip_path = build_zip(
        code, requirements, build_dir, pip_runner=pip_runner,
        on_pkg_ready=_on_pkg_ready,
    )
    s3_key = candidate_s3_key(agent.name, canary_id, role)
    (uploader or _default_uploader)(str(zip_path), bucket, s3_key)
    log(f"candidate artifact → s3://{bucket}/{s3_key} · {source}")

    environment = runtime_environment(edited_spec, settings.resources)

    resp = rt.update_code_runtime(
        control_client,
        runtime_id=agent.resource_id,
        s3_bucket=bucket,
        s3_key=s3_key,
        role_arn=role_arn,
        environment=environment,
        protocol=edited_spec.protocol,
    )
    v_candidate = str(resp["agentRuntimeVersion"])
    log(f"UpdateAgentRuntime published candidate version {v_candidate}")
    rt.wait_runtime_ready(
        control_client, agent.resource_id,
        on_status=lambda s: log(f"candidate runtime status: {s}"),
    )
    return v_current, v_candidate, s3_key


# ─── dedicated per-canary gateway ────────────────────────────────────────────
def create_canary_gateway(
    *,
    control_client: Any,
    canary_id: str,
    log: Log = _noop,
) -> dict[str, Any]:
    """Create a DEDICATED Gateway for this canary and wait until READY.

    Unlike ``service.ensure_experiment_gateway`` (a single shared gateway with
    conflict-adopt), each canary gets its own uniquely named gateway so canaries
    can run concurrently across agents and the gateway is the front door only
    for this canary's lifetime. AWS_IAM authorizer + the shared gateway role.
    """
    settings = get_settings()
    name = f"lp-canary-{canary_id}"
    log(f"creating dedicated canary gateway {name}…")
    try:
        gateway = control_client.create_gateway(
            name=name,
            description=f"Launchpad canary gateway ({canary_id})",
            authorizerType="AWS_IAM",
            roleArn=settings.resources["gateway_role_arn"],
            clientToken=str(uuid.uuid4()),
        )
        gateway_id = gateway["gatewayId"]
    except Exception as exc:
        # A prior setup attempt already created this canary's uniquely named
        # gateway — adopt it (mirror service.ensure_experiment_gateway's
        # conflict-adopt) instead of failing the retry.
        if not _is_conflict(exc):
            raise
        log(f"canary gateway {name} already exists — adopting…")
        summary = next(
            (
                item
                for item in control_client.list_gateways(maxResults=100).get(
                    "items", []
                )
                if item.get("name") == name
            ),
            None,
        )
        if summary is None:
            raise
        gateway_id = summary["gatewayId"]
    log("waiting for canary gateway READY…")
    for _ in range(30):
        detail = control_client.get_gateway(gatewayIdentifier=gateway_id)
        if detail.get("status") == "READY":
            return {
                "gateway_id": gateway_id,
                "gateway_arn": detail["gatewayArn"],
                "gateway_url": detail["gatewayUrl"],
            }
        _sleep(5)
    raise TimeoutError(f"canary gateway {gateway_id} not READY")


def delete_canary_gateway(
    control_client: Any,
    gateway_id: str,
    *,
    timeout_s: float = 300.0,
    interval_s: float = 6.0,
    log: Log = _noop,
) -> None:
    """Delete this canary's dedicated gateway (cleanup owns the whole gateway).

    DeleteGateway is REJECTED until the gateway's async-deleting A/B test +
    targets fully propagate away — verified live to take a few MINUTES *after*
    list_ab_tests / list_gateway_targets already report them gone, so draining
    the lists is not a reliable signal. The only reliable signal is a successful
    delete: retry until it takes (or the budget is exhausted, then raise so the
    caller records the skip)."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            control_client.delete_gateway(gatewayIdentifier=gateway_id)
            return
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ in _NOT_FOUND:
                return  # already gone == success
            if time.monotonic() >= deadline:
                raise
            log(f"gateway not deletable yet ({type(exc).__name__}); retrying…")
            _sleep(interval_s)


# ─── stable / treatment named endpoints ──────────────────────────────────────
def _ensure_endpoint(
    control_client: Any,
    *,
    runtime_id: str,
    endpoint_name: str,
    version: int | str,
    log: Log,
) -> None:
    """Create the endpoint pinned to ``version``; on retry adopt+re-point it."""
    log(f"pinning endpoint {endpoint_name} → version {version}…")
    try:
        rt.create_runtime_endpoint(
            control_client, runtime_id=runtime_id,
            endpoint_name=endpoint_name, version=version,
        )
    except Exception as exc:
        if not _is_conflict(exc):
            raise
        # already exists (idempotent retry) — re-point at the intended version
        rt.update_runtime_endpoint(
            control_client, runtime_id=runtime_id,
            endpoint_name=endpoint_name, version=version,
        )


def ensure_endpoint_ready(
    control_client: Any,
    *,
    runtime_id: str,
    endpoint_name: str,
    version: int | str,
    log: Log = _noop,
) -> dict[str, Any]:
    """Ensure ONE named endpoint is pinned to ``version`` and READY.

    Idempotent (re-points an existing endpoint on retry). Setup uses this to
    stand up the stable endpoint (→ v_current) BEFORE the candidate mint and the
    treatment endpoint (→ v_candidate) after it, so production is never routed to
    the untested candidate during the setup window.
    """
    _ensure_endpoint(
        control_client, runtime_id=runtime_id,
        endpoint_name=endpoint_name, version=version, log=log,
    )
    return rt.wait_endpoint_ready(
        control_client, runtime_id=runtime_id, endpoint_name=endpoint_name,
        on_status=lambda s: log(f"endpoint {endpoint_name} status: {s}"),
    )


def ensure_canary_endpoints(
    *,
    control_client: Any,
    runtime_id: str,
    v_current: int | str,
    v_candidate: int | str,
    stable_name: str,
    treatment_name: str,
    log: Log = _noop,
) -> dict[str, str]:
    """Ensure stable→v_current and treatment→v_candidate endpoints are READY.

    Idempotent: an existing endpoint (a retried setup) is re-pointed at the
    intended version rather than failing. Returns the two endpoint names.
    """
    ensure_endpoint_ready(
        control_client, runtime_id=runtime_id,
        endpoint_name=stable_name, version=v_current, log=log,
    )
    ensure_endpoint_ready(
        control_client, runtime_id=runtime_id,
        endpoint_name=treatment_name, version=v_candidate, log=log,
    )
    return {"stable": stable_name, "treatment": treatment_name}


def promote_stable_endpoint(
    *,
    control_client: Any,
    runtime_id: str,
    stable_name: str,
    version: int | str,
    log: Log = _noop,
) -> dict[str, Any]:
    """The promote cutover: re-point the stable endpoint at the candidate
    version and wait until READY. Production (invoked via the stable endpoint)
    then serves the candidate."""
    log(f"promoting stable endpoint {stable_name} → version {version}…")
    rt.update_runtime_endpoint(
        control_client, runtime_id=runtime_id,
        endpoint_name=stable_name, version=version,
    )
    return rt.wait_endpoint_ready(
        control_client, runtime_id=runtime_id, endpoint_name=stable_name,
        on_status=lambda s: log(f"stable endpoint status: {s}"),
    )


def delete_endpoint_quiet(
    control_client: Any,
    *,
    runtime_id: str,
    endpoint_name: str,
    log: Log = _noop,
) -> None:
    """Delete a named endpoint, swallowing a not-found (already gone) error so
    cleanup is retryable."""
    try:
        rt.delete_runtime_endpoint(
            control_client, runtime_id=runtime_id, endpoint_name=endpoint_name
        )
    except Exception as exc:
        if type(exc).__name__ not in _NOT_FOUND:
            raise
        log(f"endpoint {endpoint_name} already gone")
