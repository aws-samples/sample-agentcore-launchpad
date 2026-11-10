"""Runtime canary infrastructure helpers (pure, injected-client style).

The AWS-touching building blocks the canary state machine (``canary_service``)
composes — minting a candidate runtime version and pinning the stable/treatment
named endpoints. Every function takes its AWS client(s) as arguments (no
``control_client()`` factory here) so unit tests inject stubs and never touch pip /
S3 / AWS.

Topology (Model 1): one runtime, two immutable versions. ``stable`` (named endpoint)
is pinned to the live ``v_current``; ``treatment`` to the minted ``v_candidate``.
CN (N12 W-E3): the per-canary Gateway + target-routing A/B test are gone (not in
``aws-cn``); Launchpad's own invoke chain splits live traffic between the two
endpoints (``optimization.routing`` → ``qualifier``). Because ``UpdateAgentRuntime``
auto-rolls DEFAULT to the new version, the stable endpoint — not DEFAULT — is what
keeps production on ``v_current``.
"""

import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.deployer.environment import runtime_environment
from app.deployer.filesystem import filesystem_configurations
from app.deployer.pipeline import StageContext, method_capabilities
from app.deployer.zip_runtime import (
    _generate_code,
    _method_requirements,
    build_zip,
    bundle_skills,
    write_bundle_files,
)
from app.optimization.progress_steps import step
from app.schemas.agent import AgentSpec
from app.services import agent_iam, policy_enforcement
from app.services import inbound_auth as inbound_auth_service
from app.services.agentcore import runtime as rt
from app.services.workspace import WorkspaceContext

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


def _uploader_for(workspace: WorkspaceContext) -> Callable[[str, str, str], None]:
    def upload(local_path: str, bucket: str, key: str) -> None:
        workspace.client("s3").upload_file(local_path, bucket, key)

    return upload


def candidate_s3_key(agent_name: str, canary_id: str, role: str) -> str:
    """Deterministic key for a canary's zip: one object per canary + role.

    A uuid per mint orphaned a ~37MB object on every attempt (ISSUE-011); keyed
    this way a retried mint overwrites its own object, and cleanup knows exactly
    which keys the record owns.
    """
    return f"agents/{agent_name}/canary/{canary_id}-{role}.zip"


def delete_object_quiet(key: str, workspace: WorkspaceContext, *,
                        bucket: str | None = None,
                        s3_client: Any = None) -> None:
    """Delete one artifacts-bucket object. Raises — the caller decides whether a
    failure is fatal (cleanup records it as skipped)."""
    target = bucket or workspace.resources.get("artifacts_bucket")
    if not target:
        raise RuntimeError("artifacts_bucket missing from this workspace's resource map")
    client = s3_client or workspace.client("s3")
    client.delete_object(Bucket=target, Key=key)


# The two purposes a mint serves (the ``role`` argument, also the zip's key suffix):
# ``candidate`` = the canary's treatment version built from the edited spec; ``restore`` =
# the rollback's re-publish of production's CURRENT spec (roll-forward off a rejected
# candidate). The policy-guard gate below treats them differently.
CANDIDATE_ROLE = "candidate"
RESTORE_ROLE = "restore"


def mint_candidate_version(
    *,
    agent: Any,
    edited_spec: AgentSpec,
    control_client: Any,
    workspace: WorkspaceContext,
    canary_id: str,
    role: str = CANDIDATE_ROLE,
    log: Log = _noop,
    pip_runner: Callable[..., Any] = subprocess.run,
    uploader: Callable[[str, str, str], None] | None = None,
    build_root: Path | None = None,
    stage_ctx: StageContext | None = None,
) -> tuple[str, str, str]:
    """Publish a candidate immutable version of ``agent``'s runtime from an
    edited spec, returning ``(v_current, v_candidate, s3_key)``.

    ``canary_id``/``role`` key the uploaded zip (see ``candidate_s3_key``); the
    caller records the returned key on the canary artifacts so cleanup can
    delete it.

    The candidate is built with the deploy pipeline's own building blocks so a
    version minted here is what a deploy of the same spec would publish
    (N12-obs45): ``policy_enforcement.stage_narrowing`` (the deploy-time narrowing;
    writes the P2-a guard bundle at the agent's next immutable
    ``policy/bundles/<agent_id>/v<n>`` key when the guard arms — gated by
    ``candidate_guard_gate``), ``_generate_code`` / ``render_harness_bundle`` with
    ``policy_guard=narrowing.guard_rendered`` (the same guard splice the deploy renders),
    ``_method_requirements`` / ``build_zip``, ``runtime_environment(..., policy=narrowing)``
    (narrowed ``LAUNCHPAD_GATEWAY_TOOLS``, ``LAUNCHPAD_POLICY_MODE/_VERSION/_BUNDLE/
    _FAIL_MODE``) and the agent's resolved inbound auth
    (``inbound_auth.resolve_for_agent`` → ``authorizer_configuration``) on the
    ``UpdateAgentRuntime`` call. The candidate keeps production's execution role: the
    deploy-provisioned ``PolicyBundleRead`` Sid covers the agent's whole bundle prefix.
    With the policy module inactive every one of these is a no-op and the call is
    byte-identical to the plain mint.

    The zip is uploaded to a canary-scoped S3 key, then published in place via
    ``UpdateAgentRuntime`` (same ARN — a new version). ``UpdateAgentRuntime``
    auto-rolls DEFAULT to the candidate; the caller (Phase 2b) pins a stable endpoint
    to ``v_current`` so production keeps serving the current behavior.

    ``stage_ctx`` is the deploy-stage context the narrowing is computed in (built here
    when omitted); ``stage_narrowing`` leaves it on ``stage_ctx.scratch["policy_narrowing"]``
    so the canary can snapshot the promoted version's narrowing (``mark_applied``) at
    ``complete`` / ``rollback`` — the way the deploy stages share it.

    CRITICAL: this NEVER mutates a ledger ``Agent`` row (no version / spec / status /
    ``policy_applied`` write — ``policy_stale`` cannot flip here). Versions are recorded
    only in the canary's artifacts by the caller. ``pip_runner`` / ``uploader`` /
    ``build_root`` are test seams so unit tests run no real pip, S3, or AWS.
    """
    if not method_capabilities(edited_spec.method).candidate_mint:
        # BYOC source is the member's (an image or their own zip) — the platform cannot
        # rebuild it from an edited spec; canary_capability gates it out upstream.
        raise NotImplementedError(
            f"candidate minting is not supported for the {edited_spec.method!r} method"
        )

    bucket = workspace.resources.get("artifacts_bucket")
    if not bucket:
        raise RuntimeError(
            "artifacts_bucket missing from this workspace's resource map — run its bootstrap"
        )
    # A candidate is a new *version of the same runtime*, so it keeps whatever role
    # production is already on. Swapping in the shared role would measure the
    # candidate with permissions production does not have, and a promotion would
    # inherit them. Read from the live resource rather than derived from the agent
    # name, so an agent deployed before per-agent roles existed still works.
    live = rt.get_runtime(control_client, agent.resource_id)
    role_arn = agent_iam.live_runtime_role_arn(live, workspace)
    if not role_arn:
        raise RuntimeError(
            "the runtime reports no execution role and execution_role_arn is missing "
            "from this workspace's resource map — run its bootstrap"
        )

    v_current = str(live["agentRuntimeVersion"])
    log(step("current_version", f"current production version {v_current}",
             version=v_current))

    # the deploy-time policy context the deploy stages share (the principal carrier), computed
    # the same way and left on the same scratch key for the canary's snapshot (N12-obs45)
    if stage_ctx is None:
        stage_ctx = StageContext(
            agent_id=str(getattr(agent, "id", "") or ""), deployment_id="",
            job_id=canary_id, workspace=workspace, log=log,
        )
    policy_enforcement.stage_policy(stage_ctx, agent, edited_spec)

    root = build_root or Path(tempfile.gettempdir())
    build_dir = root / f"launchpad_canary_{agent.name}"
    if edited_spec.method == "harness":
        # N12-obs34: the self-built Harness renders its multi-file tree from the spec on
        # every build (deployer/harness_local) — same renderer, same package layout as a
        # deploy. Skills are fetched per request from S3 by the rendered entrypoint, so
        # nothing is bundled beyond the rendered files.
        from app.deployer.harness_local import render_harness_bundle

        harness = render_harness_bundle(edited_spec, workspace)
        code, source = harness.files["main.py"], "harness template"
        requirements = harness.requirements

        def _on_pkg_ready(pkg_dir: Path) -> None:
            for rel, content in harness.files.items():
                if rel == "main.py":
                    continue  # build_zip already wrote the entrypoint
                dest = pkg_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8")
    else:
        code, source = _generate_code(edited_spec)
        requirements = _method_requirements(edited_spec)

        def _on_pkg_ready(pkg_dir: Path) -> None:
            if edited_spec.code_bundle:
                write_bundle_files(edited_spec, pkg_dir)
            bundle_skills(edited_spec, code, pkg_dir, log, workspace)

    zip_path = build_zip(
        code, requirements, build_dir, pip_runner=pip_runner,
        on_pkg_ready=_on_pkg_ready,
    )
    s3_key = candidate_s3_key(agent.name, canary_id, role)
    (uploader or _uploader_for(workspace))(str(zip_path), bucket, s3_key)
    log(step("candidate_artifact", f"candidate artifact → s3://{bucket}/{s3_key} · {source}",
             bucket=bucket, key=s3_key, source=source))

    # Same platform-owned keys the deploy stage writes: the candidate is a version of THIS
    # runtime, so it must keep naming itself (LAUNCHPAD_AGENT_ID keys the config-bundle
    # reader's S3 prefix and the identity / policy-guard templates, LAUNCHPAD_WORKLOAD_NAME
    # its workload identity) — an UpdateAgentRuntime replaces the whole environment, and
    # `complete` keeps the candidate as production.
    environment = runtime_environment(
        edited_spec, workspace.resources,
        workload_name=str(getattr(agent, "resource_id", "") or ""),
        agent_id=str(getattr(agent, "id", "") or ""),
    )
    # Inbound auth exactly as the deploy stage resolves it for this agent (spec >
    # workspace default > IAM): an UpdateAgentRuntime that omits the authorizer resets a
    # JWT runtime to SigV4, so the candidate must echo the agent's configuration — read
    # through the service, never hard-coded (IAM-only in this build → None → omitted).
    db = stage_ctx.session()
    try:
        resolved_auth = inbound_auth_service.resolve_for_agent(agent, db)
    finally:
        db.close()
    authorizer = inbound_auth_service.authorizer_configuration(resolved_auth)

    resp = rt.update_code_runtime(
        control_client,
        runtime_id=agent.resource_id,
        s3_bucket=bucket,
        s3_key=s3_key,
        role_arn=role_arn,
        environment=environment,
        protocol=edited_spec.protocol,
        authorizer_configuration=authorizer,
        # an omitted list detaches the mounts — the candidate keeps production's
        filesystem_configurations=filesystem_configurations(edited_spec) or None,
    )
    v_candidate = str(resp["agentRuntimeVersion"])
    log(step("candidate_published",
             f"UpdateAgentRuntime published candidate version {v_candidate}",
             version=v_candidate))
    rt.wait_runtime_ready(
        control_client, agent.resource_id,
        on_status=lambda s: log(step("candidate_runtime_status",
                                     f"candidate runtime status: {s}", status=str(s))),
    )
    return v_current, v_candidate, s3_key


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
    log(step("pinning_endpoint", f"pinning endpoint {endpoint_name} → version {version}…",
             endpoint=endpoint_name, version=str(version)))
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
        on_status=lambda s: log(step("endpoint_status", f"endpoint {endpoint_name} status: {s}",
                                     endpoint=endpoint_name, status=str(s))),
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
    log(step("promoting_stable_endpoint",
             f"promoting stable endpoint {stable_name} → version {version}…",
             endpoint=stable_name, version=str(version)))
    rt.update_runtime_endpoint(
        control_client, runtime_id=runtime_id,
        endpoint_name=stable_name, version=version,
    )
    return rt.wait_endpoint_ready(
        control_client, runtime_id=runtime_id, endpoint_name=stable_name,
        on_status=lambda s: log(step("stable_endpoint_status", f"stable endpoint status: {s}",
                                     status=str(s))),
    )


def wait_endpoints_gone(
    control_client: Any,
    *,
    runtime_id: str,
    endpoint_names: list[str],
    timeout_s: float,
    interval_s: float = 5.0,
    log: Log = _noop,
) -> list[str]:
    """N-SCN-obs3: poll ListAgentRuntimeEndpoints until none of ``endpoint_names`` is
    listed — `DeleteAgentRuntimeEndpoint` is accepted at once but the endpoint stays
    listed as DELETING for 1–2 min, so a consumer reading AWS right after `cleanup` saw a
    transient inconsistency. Returns the names still listed at the deadline (``[]`` when
    all are gone). Never raises: a runtime that is itself gone means nothing is listed;
    any other listing error ends the wait with every remaining name reported pending."""
    pending = list(endpoint_names)
    deadline = time.monotonic() + max(0.0, timeout_s)
    polls = 0
    while pending:
        try:
            listed = {
                str(e.get("name") or "")
                for e in rt.list_runtime_endpoints(control_client, runtime_id)
            }
        except Exception as exc:  # noqa: BLE001 — the wait reports, it never fails cleanup
            if type(exc).__name__ in _NOT_FOUND:
                log(step("runtime_gone", f"runtime {runtime_id} is gone — its endpoints with it",
                         runtime=runtime_id))
                return []
            log(step("endpoint_listing_failed",
                     f"endpoint listing failed ({type(exc).__name__}) — "
                     f"{', '.join(pending)} left as pending",
                     error=type(exc).__name__, endpoints=list(pending)))
            return pending
        polls += 1
        pending = [name for name in pending if name in listed]
        if not pending or time.monotonic() >= deadline:
            break
        log(step("endpoints_still_listed",
                 f"endpoint(s) still listed: {', '.join(pending)} — poll {polls}, "
                 f"waiting {interval_s:g}s",
                 endpoints=list(pending), poll=polls, interval_s=interval_s))
        _sleep(interval_s)
    return pending


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
        log(step("endpoint_already_gone", f"endpoint {endpoint_name} already gone",
                 endpoint=endpoint_name))
