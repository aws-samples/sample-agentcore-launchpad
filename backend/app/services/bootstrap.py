"""Idempotent hub bootstrap (CN build).

Reads the CDK stack outputs, writes the resource map into ``config/launchpad.yaml``,
ensures the Global Bedrock model credential (AgentCore Identity API-key provider,
spec C4), then ensures the shared AgentCore Gateway (+ its API-key provider and demo
targets), X-Ray Transaction Search and the self-hosted Memory/KB substrate (this
environment's ``launchpad-memory[-env]`` stack on the shared ``launchpad-memory-domain``
OpenSearch domain — D17; N6 ruling R2/R21 — absent stack degrades instead of failing
bootstrap) exist. The Registry is local (N4) and only records its id.

Every ``ensure_*`` function is single-purpose and create-if-missing by name so
later phases can add shared resources without touching existing behaviour.
"""

import os
import re
import time
import traceback
from collections.abc import Callable, Mapping
from typing import Any

import yaml
from botocore.exceptions import BotoCoreError, ClientError

from app.core import naming
from app.core.config import CONFIG_FILE, get_settings
from app.core.naming import resource_name
from app.services import aws_clients
from app.services import opensearch_http as _opensearch_core
from app.services.model_credential import (
    credential_record,
    credential_summary,
    ensure_provider_credentials,
    ensure_registry_credentials,
    provider_credential_resources,
)
from app.services.workspace import WorkspaceContext

# Base name; `base_stack_name()` applies `Settings.env_name` (N11) — `launchpad-base`
# by default, `launchpad-base-t1` for a parallel environment. Every stack lookup goes
# through the function, never the constant.
STACK_NAME = "launchpad-base"
CDK_CONTEXT_ENV_VAR = "LAUNCHPAD_CDK_CONTEXT"


def base_stack_name() -> str:
    return resource_name(STACK_NAME)


def cdk_context_args(environ: Mapping[str, str] | None = None) -> list[str]:
    """Parse `LAUNCHPAD_CDK_CONTEXT` ("k=v,k2=v2") into repeated `cdk --context k=v` args,
    and keep the CDK app's `env_name` context consistent with `Settings.env_name`.

    N9 introduced it to pass the OpenSearch overrides (R13b: `opensearch_version`,
    `opensearch_instance_type`) without editing the scripts; the former
    `memory_master_arn` (R13c) is gone with D17 — the shared domain's FGAC master is
    the domain stack's own admin role — and the CDK app ignores it if still present.
    N11: when the backend runs with a non-empty env name, `--context env_name=<it>` is
    appended so `cdk deploy` / `cdk destroy` address the suffixed stacks; a value already
    present in the variable (the EC2 template writes `env_name=<EnvName>`) must agree
    with the setting — a mismatch would deploy one environment's stacks under another's
    name, so it is refused outright. Empty/unset → no extra args (today's behavior).
    """
    source = os.environ if environ is None else environ
    raw = source.get(CDK_CONTEXT_ENV_VAR, "").strip()
    pairs = [pair.strip() for pair in raw.split(",") if pair.strip()] if raw else []
    env_name = naming.current_env_name()
    explicit = [pair for pair in pairs if pair.startswith("env_name=")]
    if explicit:
        value = explicit[-1].split("=", 1)[1]
        if value != env_name:
            raise RuntimeError(
                f"{CDK_CONTEXT_ENV_VAR} carries env_name={value!r} but Settings.env_name "
                f"(LAUNCHPAD_ENV_NAME) is {env_name!r} — the CDK stacks and the backend "
                "would name two different environments; make the two agree"
            )
    elif env_name:
        pairs.append(f"env_name={env_name}")
    mode = get_settings().memory_domain_mode
    explicit_modes = [p.split("=", 1)[1] for p in pairs if p.startswith("memory_domain_mode=")]
    if any(value != mode for value in explicit_modes):
        raise RuntimeError("CDK memory_domain_mode differs from Settings.memory_domain_mode")
    if not explicit_modes and mode != "shared":
        pairs.append(f"memory_domain_mode={mode}")
    naming.memory_domain_env()  # Reject dedicated mode without a named environment.
    args: list[str] = []
    for pair in pairs:
        args += ["--context", pair]
    return args


def _client(service: str, region: str):
    # Hub bootstrap targets an explicit region because it *writes* the resource
    # map a workspace context later reads — it cannot resolve one from settings.
    # Account is unknown here, hence "" — ambient credentials decide it.
    return WorkspaceContext(account_id="", region=region).client(service)


def get_stack_outputs(region: str, stack_name: str | None = None) -> dict[str, str]:
    """CDK CfnOutputs as a flat dict; raises if the stack is absent.
    `stack_name=None` means this environment's base stack (`base_stack_name()`)."""
    cfn = _client("cloudformation", region)
    stacks = cfn.describe_stacks(StackName=stack_name or base_stack_name())["Stacks"]
    return {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}


# r8-nits (rev-r8 on N11-obs44c): the memory stage degrades on ANY error, which also
# swallows a programming error in the stage itself (a TypeError from a changed signature, a
# KeyError on a renamed output). Those are not operational shapes — a healthy probe would
# clear them as "step skipped" and nobody would ever see the traceback. They still never
# abort, but the traceback is printed and the summary warning is marked `unexpected error`.
UNEXPECTED_ERROR_TYPES: tuple[type[BaseException], ...] = (
    TypeError, KeyError, AttributeError, NameError, AssertionError, IndexError,
)


def is_unexpected_error(exc: BaseException) -> bool:
    """A programming error (never a ClientError / OpenSearch / transport error)."""
    if isinstance(exc, (ClientError, _opensearch_core.OpenSearchError)):
        return False
    return isinstance(exc, UNEXPECTED_ERROR_TYPES)


def _memory_substrate_error(exc: Exception) -> str:
    """A short, safe diagnostic for a failed OpenSearch FGAC call — the exception
    type and, where one exists, a status/error code, never the response body
    (which could echo stack traces or other response content) or any secret."""
    if isinstance(exc, _opensearch_core.OpenSearchError):
        return f"OpenSearchError {exc.status}"
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        return f"ClientError {code}" if code else "ClientError"
    return type(exc).__name__


# ── N11-obs11: IAM propagation retry ─────────────────────────────────────────
# Right after an IAM change (put-role-policy on the ops role, a new execution role) a
# call the previous run was allowed can be denied once and pass seconds later — IAM is
# eventually consistent and can regress briefly. Both bootstrap runners re-run a stage on
# AccessDenied only (≤3 attempts, fixed back-off inside the 10–20 s band); every other
# error propagates at once, and the stages are idempotent so a re-run is safe.
ACCESS_DENIED_CODES = frozenset({"AccessDeniedException", "AccessDenied", "AccessDeniedError"})
ACCESS_DENIED_ATTEMPTS = 3
ACCESS_DENIED_DELAY_S = 15.0


def is_access_denied(exc: BaseException) -> bool:
    return (
        isinstance(exc, ClientError)
        and exc.response.get("Error", {}).get("Code", "") in ACCESS_DENIED_CODES
    )


def retry_access_denied(
    fn: Callable[[], Any],
    *,
    label: str,
    attempts: int = ACCESS_DENIED_ATTEMPTS,
    delay_s: float = ACCESS_DENIED_DELAY_S,
    sleeper: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> Any:
    """Call ``fn``; on an AccessDenied ``ClientError`` wait ``delay_s`` and call it again,
    at most ``attempts`` times in total. The last denial (and any other exception) is
    raised unchanged, so the caller's own error mapping still applies."""
    attempts = max(1, attempts)
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except ClientError as exc:
            # N11-obs44a: a denied sts:AssumeRole is a missing trust/policy statement, not
            # the stage's own IAM change propagating — raise at once, no "propagation" line.
            if (
                not is_access_denied(exc)
                or aws_clients.is_assume_role_failure(exc)
                or attempt == attempts
            ):
                raise
            log(
                f"{label}: AccessDenied right after an IAM change (propagation) — "
                f"retry {attempt}/{attempts - 1} in {delay_s:.0f}s"
            )
            sleeper(delay_s)
    raise AssertionError("unreachable")  # pragma: no cover


_MISSING_COLUMN_RE = re.compile(r"no such column:\s*([\w.]+)")


def missing_column_of(exc: BaseException) -> str | None:
    """The `table.column` SQLite names in a "no such column" `OperationalError` (N17-obs233) —
    None for any other error."""
    match = _MISSING_COLUMN_RE.search(str(exc))
    return match.group(1) if match else None


def ensure_ledger_schema() -> None:
    """N17-obs233: the bootstrap CLI runs the NEW code against the ledger the OLD backend is
    still serving (host-update step 10 precedes the restart, step 13), so a stage that reads a
    ledger table may meet a column the running schema does not have yet — both hosts logged
    `model-providers stage skipped — OperationalError: no such column:
    model_providers.key_last4`. Bring the ledger up to the models FIRST, with the same
    `init_db` the API runs at startup (additive `ALTER TABLE … ADD COLUMN`s the old process
    tolerates), before any stage selects from it."""
    from app.core.db import init_db

    # rev-r22 (obs233 nit): `_seed_default_workspace` reads Settings — the yaml this very run
    # has been writing — so a Settings cached before those writes must not seed stale resources
    get_settings.cache_clear()
    init_db()


def ensure_local_registry_backend() -> None:
    """The bootstrap CLI is not the API process, so no registry backend is registered:
    bring the ledger up and install the local backend when missing. Shared by every
    stage that writes registry records (defaults import, Skill Lab sample skills), so
    none of them silently depends on another stage having run first."""
    from app.services import registry_backend

    if registry_backend.backend_or_none() is None:
        from app.core.db import init_db
        from app.services.registry_local import LocalRegistryBackend

        init_db()
        registry_backend.set_backend(LocalRegistryBackend())


def ensure_registry_defaults(
    workspace: WorkspaceContext, log: Callable[[str], None] = print
) -> dict[str, Any]:
    """Import the shared gateway's targets as APPROVED MCP records + the sample skill
    into the local Registry (N10-bug1) — the step the N4 port dropped, which left the
    Create-Agent wizard with "No approved gateway targets" after a fresh bootstrap.

    The bootstrap CLI is not the API process, so no registry backend is registered
    here: the ledger is brought up (``init_db``) and the local backend installed when
    missing. Idempotent through ``ensure_default_records`` (``created: false`` on a
    re-run, statuses untouched). A failure (gateway not reachable yet, S3 denied)
    degrades with a reason instead of failing the bootstrap: the same import is one
    ``POST /api/registry/sync-defaults`` away.
    """
    from app.services import registry_console

    if "registry" not in get_settings().features:
        reason = "registry feature disabled in Settings.features"
        log(f"registry defaults skipped — {reason}")
        return {"records": [], "created": 0, "approved": 0, "unavailable_reason": reason}
    ensure_local_registry_backend()
    try:
        # host bootstrap may approve the platform's own targets (N17-obs257)
        results = registry_console.ensure_default_records(workspace, log=log, auto_approve=True)
    except Exception as exc:  # noqa: BLE001 — degrade, never fail the bootstrap here
        reason = f"{type(exc).__name__}: {exc}"[:300]
        log(
            f"registry defaults not imported — {reason}; rerun `make bootstrap` or "
            "POST /api/registry/sync-defaults once the gateway answers"
        )
        return {"records": [], "created": 0, "approved": 0, "unavailable_reason": reason}
    created = sum(1 for r in results if r.get("created"))
    approved = sum(1 for r in results if r.get("status") == "APPROVED")
    removed = sum(len(r.get("removed_duplicates") or []) for r in results)
    log(f"registry defaults: {len(results)} record(s) · {created} new · {approved} approved"
        + (f" · {removed} duplicate(s) removed" if removed else ""))
    return {"records": results, "created": created, "approved": approved,
            "removed_duplicates": removed, "unavailable_reason": ""}


# ── N12 (plan §B.0): the evaluation worker preset ─────────────────────────────
EVAL_WORKER_RESOURCE_KEYS = (
    "eval_worker_runtime_arn", "eval_worker_agent_id", "eval_worker_runtime_id",
    "eval_worker_release", "eval_worker_unavailable_reason",
)


# ── CN policy (D20 P1-d) ───────────────────────────────────────────────────


def ensure_policy_engine() -> str:
    """Register the Cedar engine in this process when none is (the bootstrap CLI has no
    `create_app`). Returns ``cedarpy`` or ``unavailable: <reason>`` — never raises: the
    engine matters to the API process, the stage only reports it."""
    from app.services import policy_backend

    if policy_backend.backend_or_none() is not None:
        return "cedarpy"
    try:
        from app.services.policy_cedar import CedarEngine

        policy_backend.set_backend(CedarEngine())
    except Exception as exc:  # noqa: BLE001 — a missing wheel is a report line, not a crash
        return f"unavailable: {type(exc).__name__}: {exc}"[:200]
    return "cedarpy"


def _default_workspace_active_policies() -> int:
    """ACTIVE policies of the default workspace for the summary row (0 when the ledger
    has no policy tables yet — the API process creates them on start)."""
    from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
    from app.services import policy_store

    try:
        db = SessionLocal()
        try:
            return int(policy_store.active_count(db, DEFAULT_WORKSPACE_ID))
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — summary only
        return 0


def ensure_policy_decisions(region: str, *, log: Callable[[str], None] = print) -> dict[str, Any]:
    """The hub bootstrap's ``policy`` stage (plan §7.3): register the Cedar engine and
    ensure the CloudWatch decisions log group ``/launchpad/policy/decisions[-env]`` with
    ``Settings.log_group_retention_days`` through the funnel ``logs`` client. Degrades on
    any AWS error — decisions then stay in the ledger only (the sink WARNs per batch)."""
    from app.services import policy_audit

    settings = get_settings()
    summary: dict[str, Any] = {
        "engine": "", "mode": settings.policy_default_mode, "active_policies": 0,
        "log_group": policy_audit.log_group_name(), "created": False,
        "retention_days": int(settings.log_group_retention_days or 0),
        "unavailable_reason": "", "skipped": False,
    }
    if "policy" not in settings.features:
        summary.update(skipped=True, unavailable_reason="policy feature off", log_group="")
        log("policy stage skipped — the `policy` feature is off")
        return summary
    summary["engine"] = ensure_policy_engine()
    try:
        summary.update(policy_audit.ensure_decision_log_group(
            _client("logs", region), retention_days=summary["retention_days"], log=log,
        ))
    except (ClientError, BotoCoreError) as exc:
        code = (
            str(exc.response.get("Error", {}).get("Code") or "ClientError")
            if isinstance(exc, ClientError) else type(exc).__name__
        )
        summary["unavailable_reason"] = code
        log(
            f"policy stage WARN: decisions log group {summary['log_group']} not ensured "
            f"({code}) — decisions stay in the ledger; the role needs logs:CreateLogGroup + "
            "logs:PutRetentionPolicy + logs:PutLogEvents on log-group:/launchpad/policy/*"
        )
    summary["active_policies"] = _default_workspace_active_policies()
    return summary


def policy_resources(summary: dict[str, Any]) -> dict[str, Any]:
    """The resource-map entries the ``policy`` stage records."""
    return {
        "policy_log_group": summary.get("log_group", "") if not summary.get(
            "unavailable_reason") else "",
        "policy_log_group_unavailable_reason": summary.get("unavailable_reason", ""),
    }


def _policy_interceptor_stage(stage: Callable[..., Any], config: dict[str, Any], region: str,
                              account_id: str, gateway_role_arn: str) -> dict[str, Any]:
    from pathlib import Path

    from app.core.config import REPO_ROOT
    from app.services import policy_interceptor

    ctx = WorkspaceContext(account_id=account_id, region=region)
    try:
        out = stage("policy-interceptor", lambda: policy_interceptor.run_stage(
            ctx, dict(config.get("resources") or {}), gateway_role_arn=gateway_role_arn,
            build_dir=Path(REPO_ROOT) / "data" / "build" / "policy-interceptor"))
        return {**out, "policy_interceptor_unavailable_reason": ""}
    except Exception as exc:  # noqa: BLE001 — degrade, never fail the bootstrap here
        reason = f"{type(exc).__name__}: {exc}"[:300]
        print(f"policy-interceptor stage unavailable — {reason}")
        return {"policy_interceptor_unavailable_reason": reason}


def eval_worker_unavailable(reason: str) -> dict[str, Any]:
    return {"eval_worker_runtime_arn": "", "eval_worker_agent_id": "",
            "eval_worker_runtime_id": "", "eval_worker_release": "",
            "eval_worker_unavailable_reason": reason}


def register_deploy_methods() -> None:
    """Register the deploy methods by side-effect import — exactly what ``app.main`` does.

    N12-bug1 (live t1, 2026-10-10): the bootstrap CLI never imports ``app.main``, so the
    first preset install from ``make bootstrap`` died with ``no deploy method registered for
    'zip_runtime'`` before any AWS call. Every bootstrap-time install (eval worker today,
    any zip_runtime/byoc preset tomorrow) must go through here first.
    """
    import app.deployer.byoc  # noqa: F401
    import app.deployer.harness_local  # noqa: F401
    import app.deployer.zip_runtime  # noqa: F401


def ensure_eval_worker(
    config: dict[str, Any], *, account_id: str, region: str, log: Callable[[str], None] = print,
    run_job: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Install (or repair) the ``eval-worker`` system preset on the hub's default
    workspace — idempotent through ``system_agents.install_preset``: not installed → install
    job; active on the current template release → no job; failed / stale release → repair
    job. The deploy job runs **synchronously** here (zip build → role → CreateAgentRuntime →
    READY, 5–15 min) so the bootstrap can record the worker's ARN. Not one of the
    ``REQUIRED_RESOURCE_KEYS``: without it the console answers 503
    ``evaluation.worker_missing`` on job submission while datasets / evaluators / SCORE
    NOW keep working.

    The bootstrap CLI is not the API process: the ledger is brought up here and the default
    workspace row converged on the resource map THIS run wrote (the row is what the deploy
    stages read).
    """
    register_deploy_methods()
    from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal, init_db
    from app.deployer.pipeline import execute_deploy_job
    from app.models.ledger import Agent, Workspace
    from app.system_agents import service as system_agents
    from app.system_agents.presets import EVAL_WORKER

    run_job = run_job or (lambda job_id: execute_deploy_job(job_id, resume=False))
    init_db()
    db = SessionLocal()
    try:
        row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        row.account_id = account_id
        row.region = region
        row.resources = {**(row.resources or {}), **dict(config.get("resources") or {})}
        row.bootstrap_status = "ready"
        db.commit()
        missing = system_agents.workspace_requirements(row)
        if missing:
            reason = "; ".join(m["message"] for m in missing)
            log(f"evaluation worker not installed — {reason}")
            return {"installed": False, "created": False, "changed": False,
                    "unavailable_reason": reason}
        outcome = system_agents.install_preset(db, row, EVAL_WORKER, None)
        db.commit()
        agent_id = outcome.agent.id
        job_id = outcome.job.id if (outcome.job is not None and outcome.changed) else None
        created, changed = outcome.created, outcome.changed
    finally:
        db.close()
    if job_id:
        log(f"evaluation worker: {'install' if created else 'repair'} job {job_id} — deploying "
            "the launchpad-eval-worker runtime (this takes several minutes)")
        run_job(job_id)
    else:
        log("evaluation worker: already installed on the current release")
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        if agent is None or agent.status != "active":
            reason = (agent.error if agent is not None and agent.error else
                      f"worker agent is {agent.status if agent else 'missing'}")
            log(f"evaluation worker unavailable — {reason}")
            return {"installed": False, "created": created, "changed": changed,
                    "agent_id": agent_id, "unavailable_reason": reason}
        from app.system_agents.presets import template_release_from_spec

        return {
            "installed": True, "created": created, "changed": changed,
            "agent_id": agent.id, "runtime_arn": agent.arn or "",
            "runtime_id": agent.resource_id or "",
            "release": template_release_from_spec(agent.spec or {}) or "",
            "unavailable_reason": "",
        }
    finally:
        db.close()


def eval_worker_resources(summary: dict[str, Any]) -> dict[str, Any]:
    """The resource-map entries the ``evaluation`` stage records."""
    if not summary.get("installed"):
        return eval_worker_unavailable(str(summary.get("unavailable_reason") or "not installed"))
    return {
        "eval_worker_runtime_arn": summary.get("runtime_arn", ""),
        "eval_worker_agent_id": summary.get("agent_id", ""),
        "eval_worker_runtime_id": summary.get("runtime_id", ""),
        "eval_worker_release": summary.get("release", ""),
        "eval_worker_unavailable_reason": "",
    }


def load_config() -> dict[str, Any]:
    if CONFIG_FILE.is_file():
        data = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    return {}


def merge_config(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge update into base (update wins; nested dicts merged)."""
    merged = dict(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_config(merged[key], value)
        else:
            merged[key] = value
    return merged


def write_config(
    update: dict[str, Any], replace: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Deep-merge ``update`` into the config; ``replace`` keys are set wholesale.

    ``replace`` exists for maps whose stale entries must drop out (e.g. the
    ``demo_users.passwords`` map after a legacy demo user is deleted) — a deep
    merge would resurrect them from the file.
    """
    merged = merge_config(load_config(), update)
    for key, value in (replace or {}).items():
        merged[key] = value
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        "# Generated by scripts/bootstrap.py — do not commit (gitignored).\n"
        + yaml.safe_dump(merged, sort_keys=True, allow_unicode=True),
        encoding="utf-8",
    )
    return merged


def ensure_skill_lab(
    account_id: str, region: str, resources: dict[str, Any]
) -> dict[str, str]:
    """Skill Lab exec worker (role + image + runtime), the host venv and the demo samples.

    Returns the `skill_lab_worker_*` resource keys plus `python` (the venv
    interpreter — host state, not a resource key).

    N10v3-bug1: the hub path provisioned worker + venv but never seeded the vendored
    skilleval demo (`sample-logtriage*` skills, sample task sets) — only the spoke
    workspace stage did — so a `make bootstrap` hub answered `[]` on
    `GET /api/skill-lab/tasksets`. Seeding is idempotent by name (re-runs reuse) and
    degrades like the spoke stage: optional demo content must not fail the bootstrap."""
    from app.skill_lab import infra as skill_lab_infra

    def log(msg: str) -> None:
        print(f"  [skill-lab] {msg}", flush=True)

    workspace = WorkspaceContext(
        account_id=account_id, region=region, resources=resources
    )
    skill_lab_resources = skill_lab_infra.ensure_skill_lab_worker(
        workspace, workspace.id, log=log
    )
    python = skill_lab_infra.ensure_skill_lab_venv(log=log)
    # The samples register skills through the registry backend and read the worker
    # resources, so seed on a context that already carries this run's resource keys.
    seeded = WorkspaceContext(
        account_id=account_id, region=region,
        resources={**resources, **skill_lab_resources},
    )
    try:
        from app.core.db import SessionLocal
        from app.skill_lab import samples

        # the sample skills are registry records: install the local backend ourselves
        # (the registry stage may be disabled or reordered)
        ensure_local_registry_backend()
        db = SessionLocal()
        try:
            result = samples.seed_skill_lab_samples(db, seeded.id, seeded, log=log)
        finally:
            db.close()
        log(f"demo samples: {result.detail}")
        # N11-r5-bug3: three skills and zero task sets is not a partial success — every
        # sample skill pairs with a sample task set, so fewer task sets in the ledger than
        # sample skills means the task-set rows failed (the per-item degrade above kept
        # the stage alive; the lines above carry the reason, e.g. `no such table`).
        if result.available and result.tasksets_present < result.skills_expected:
            log(
                f"WARNING: {result.tasksets_present} sample task sets present vs "
                f"{result.skills_expected} sample skills — {result.tasksets_missing} task "
                "set(s) missing; see the lines above for the per-item reason"
            )
    except Exception as exc:  # noqa: BLE001 — optional demo content only
        log(f"sample seeding skipped ({type(exc).__name__}: {exc})")
    return {**skill_lab_resources, "python": python}


def run_bootstrap(
    region: str | None = None, *, sleeper: Callable[[float], None] = time.sleep
) -> dict[str, Any]:
    """Full bootstrap pass. Returns a summary of what was created vs reused.

    Each AWS-touching block runs through ``stage`` — the hub's counterpart of the
    workspace stage runner — which re-runs it on a transient AccessDenied (N11-obs11).
    ``sleeper`` is injectable so tests never wait on the back-off."""
    region = region or get_settings().region
    outputs = get_stack_outputs(region)
    control = _client("bedrock-agentcore-control", region)
    sts = _client("sts", region)

    def stage(name: str, fn: Callable[[], Any]) -> Any:
        return retry_access_denied(fn, label=f"bootstrap stage {name}", sleeper=sleeper)

    identity = sts.get_caller_identity()
    account_id = identity["Account"]
    caller_arn = str(identity.get("Arn") or "")

    config = write_config(
        {
            "account_id": account_id,
            "region": region,
            "resources": {
                "artifacts_bucket": outputs["ArtifactsBucketName"],
                "ecr_repo": outputs["EcrRepoName"],
                "ecr_repo_uri": outputs["EcrRepoUri"],
                "codebuild_project": outputs["CodeBuildProjectName"],
                "execution_role_arn": outputs["AgentExecutionRoleArn"],
                # build-tools layer (phase 6+); absent on stacks predating it
                "hr_lambda_arn": outputs.get("HrLambdaArn", ""),
                "office_facts_api_url": outputs.get("OfficeFactsApiUrl", ""),
                "office_facts_api_key_id": outputs.get("OfficeFactsApiKeyId", ""),
                "gateway_role_arn": outputs.get("GatewayRoleArn", ""),
            },
        },
    )

    # Spec C4: every model API key comes from the bootstrap shell's environment only
    # (LAUNCHPAD_GLOBAL_BEDROCK_API_KEY — required; an admin-registered provider's
    # LAUNCHPAD_<NAME>_API_KEY — optional, N-R6) and goes straight into its AgentCore Identity
    # API-key credential provider; a key
    # is never written or logged, an unconfigured optional provider is skipped with a line.
    credentials = stage(
        "model-credential", lambda: ensure_provider_credentials(control, log=print)
    )
    credential = credentials["bedrock"]
    config = write_config({"resources": provider_credential_resources(credentials)})

    # D21 (M1-b, plan §4): the registry half of the credential flow. Every provider row
    # whose key comes from SSM (`credential_source: ssm` — every admin-registered
    # OpenAI-compatible provider, e.g. SiliconFlow; none is seeded since R13) is read from its
    # `launchpad-<name>-api-key` parameter and created / rotated in Identity, exactly like
    # `POST /credential` from the console — so a parameter stored after the hosts are up is
    # picked up by the next bootstrap, no reboot. The hub record gains `source: ssm` +
    # `key_version` (names and numbers only; the value is dropped after the Identity call).
    # Degrades with a line — a missing parameter or grant is never a reason to abort.
    try:
        # N17-obs233: the ledger migration runs BEFORE the first stage that reads ledger tables
        # — `make bootstrap` on a host update meets the old schema otherwise
        ensure_ledger_schema()
        registry_credentials = stage(
            "model-providers",
            lambda: ensure_registry_credentials(control, _client("ssm", region), log=print),
        )
    except Exception as exc:  # noqa: BLE001 — degrade, never fail the bootstrap here
        column = missing_column_of(exc)
        reason = (
            # belt: a column the migration did not add yet — one readable line, no traceback
            f"ledger column {column} missing (applied at next start)" if column
            else f"{type(exc).__name__}: {exc}"[:300]
        )
        print(f"model-providers stage skipped — {reason}")
        model_providers_summary: dict[str, Any] = {
            "configured": [], "not_configured": [], "unavailable_reason": reason,
        }
    else:
        configured = {name: result for name, result in registry_credentials.items()
                      if result.get("configured")}
        if configured:
            config = write_config({"resources": {"model_credentials": {
                name: credential_record(result) for name, result in configured.items()}}})
        model_providers_summary = {
            "configured": sorted(configured),
            "not_configured": sorted(n for n in registry_credentials if n not in configured),
            "unavailable_reason": "",
        }

    # The local Registry (N4) needs no AWS resource — only its identifier, which keys the
    # ledger scoping of system-skill mappings and the Overview's service tile.
    from app.core.db import DEFAULT_WORKSPACE_ID
    from app.services.registry_local import registry_id_for

    registry_id = registry_id_for(
        WorkspaceContext(account_id=account_id, region=region, id=DEFAULT_WORKSPACE_ID)
    )
    config = write_config(
        {"resources": {"registry_id": registry_id, "registry_unavailable_reason": ""}}
    )

    # --- CN memory/KB (N6) ---
    # The self-hosted Memory/KB substrate is the launchpad-memory stack (ruling R21). Absent
    # stack ⇒ the console degrades (memory.not_provisioned) instead of failing bootstrap.
    from app.services import agent_iam
    from app.services import memory_bootstrap as mb
    from app.services.memory_selfhosted import store_id_for

    memory_summary: dict[str, Any] = {
        "endpoint": "",
        "created": [],
        "reused": [],
        "unavailable_reason": "",
    }
    try:
        memory_outputs: dict[str, str] | None = get_stack_outputs(region, mb.memory_stack_name())
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ValidationError":
            raise
        memory_outputs = None
    if memory_outputs is None:
        memory_summary["unavailable_reason"] = (
            f"{mb.memory_stack_name()} stack not deployed in {region}"
        )
        config = write_config({"resources": mb.unavailable(memory_summary["unavailable_reason"])})
    else:
        hub = WorkspaceContext(account_id=account_id, region=region, id=DEFAULT_WORKSPACE_ID)
        memory_res = mb.memory_resources(
            memory_outputs, bucket=outputs["ArtifactsBucketName"], store_id=store_id_for(hub)
        )
        admin = WorkspaceContext(
            account_id=account_id,
            region=region,
            id=DEFAULT_WORKSPACE_ID,
            role_arn=memory_res["memory_admin_role_arn"] or None,
        )
        # N-SCN-obs6: read before the stage so the WARN line can name what it clears.
        previous_reason = str(
            (load_config().get("resources") or {}).get("memory_unavailable_reason") or ""
        )
        os_http = aws_clients.opensearch_http(admin, memory_res["memory_os_endpoint"])
        try:
            substrate = stage("memory", lambda: mb.ensure_memory_substrate(
                os_http,
                exec_role_arn=outputs["AgentExecutionRoleArn"],
                extractor_role_arn=memory_res["memory_extractor_role_arn"],
                agent_role_pattern=hub.arn("iam", agent_iam.agent_role_pattern(), region=""),
                log=print,
            ))
        except Exception as exc:  # noqa: BLE001 — N11-obs44c: degrade by contract, never abort
            # OpenSearchError / ClientError / urllib3 HTTPError are the expected shapes; a
            # botocore transport error (EndpointConnectionError is not a ClientError), a
            # non-JSON body or a missing credential would otherwise end the whole run
            # here — during a D17 migration that is after the cdk deploy and before the
            # gateway/observability/registry stages the host still needs.
            if is_access_denied(exc) and aws_clients.is_assume_role_failure(exc):
                # N11-obs44a: the admin role could not be assumed at all — every
                # OpenSearch call (stage and probe alike) would fail identically, so
                # the probe is skipped and the reason names the role and the missing
                # `MemoryAdminAssume` statement instead of an OpenSearch verdict.
                # N11-obs44c: a degrade, never an abort — the stages after memory run.
                reason = mb.assume_denied_reason(
                    memory_res["memory_admin_role_arn"], caller_arn=caller_arn
                )
                print(f"memory substrate unavailable — {reason}")
                memory_summary["unavailable_reason"] = reason
                config = write_config(
                    {"resources": {**memory_res, "memory_unavailable_reason": reason}}
                )
            else:
                error = _memory_substrate_error(exc)
                unexpected = is_unexpected_error(exc)
                if unexpected:
                    # the probe cannot explain a programming error — show it once, in full
                    print(f"memory stage: unexpected error in the provisioning step ({error})")
                    print(traceback.format_exc().rstrip())
                    error = f"unexpected error: {error}"
                # N-SCN-obs6: one idempotent step failing (a read timeout on the FGAC API
                # of a t3.small domain) is not evidence the substrate is unusable. Probe
                # before degrading: the probe covers everything the stage provisions
                # (cluster health, the fixed indices, the FGAC role and its mapping), so a
                # healthy probe is the same verdict a successful stage gives — clear the
                # reason (a stale non-empty one would keep `workspace_has_shared_memory()`
                # False and gate memory=workspace deploys until the next bootstrap) and WARN.
                healthy, probe_detail = mb.probe_memory_substrate(os_http)
                if healthy:
                    warning = (
                        f"{error} — substrate healthy, provisioning step NOT applied; "
                        "fix the stage and re-run bootstrap"
                        if unexpected
                        else f"substrate healthy, provisioning step skipped: {error}"
                    )
                    cleared = (
                        f"cleared stale memory_unavailable_reason={previous_reason!r}"
                        if previous_reason
                        else "memory_unavailable_reason stays empty"
                    )
                    print(f"memory stage WARN: {cleared} ({warning}; probe: {probe_detail})")
                    memory_summary["warning"] = warning
                    memory_summary["unavailable_reason"] = ""
                    memory_summary["endpoint"] = memory_res["memory_os_endpoint"]
                    config = write_config(
                        {"resources": {**memory_res, "memory_unavailable_reason": ""}}
                    )
                else:
                    # The stack exists but the FGAC endpoint could not be reached or
                    # authorized and the probe agrees — degrade like the stack-absent
                    # branch above instead of failing bootstrap (N11-obs44c), so the
                    # evaluation/gateway/observability stages still run.
                    reason = f"OpenSearch FGAC provisioning failed ({error})"
                    print(f"memory substrate unavailable — {reason}; probe: {probe_detail}")
                    memory_summary["unavailable_reason"] = reason
                    config = write_config(
                        {"resources": {**memory_res, "memory_unavailable_reason": reason}}
                    )
        else:
            memory_summary.update(substrate)
            memory_summary["endpoint"] = memory_res["memory_os_endpoint"]
            config = write_config({"resources": memory_res})
    # --- end CN memory/KB (N6) ---

    # --- CN evaluation worker (N12, plan §B.0) ---
    # After memory (the worker needs the bucket + a dedicated role, nothing of the gateway).
    # A failed install degrades: the reason lands in the resource map and the console says
    # 503 evaluation.worker_missing on job submission until `make bootstrap` is re-run or
    # the preset is installed from the System agents page.
    try:
        evaluation_summary = stage(
            "evaluation",
            lambda: ensure_eval_worker(config, account_id=account_id, region=region, log=print),
        )
    except Exception as exc:  # noqa: BLE001 — degrade, never fail the bootstrap here
        reason = f"{type(exc).__name__}: {exc}"[:300]
        print(f"evaluation worker unavailable — {reason}")
        evaluation_summary = {"installed": False, "created": False, "changed": False,
                              "unavailable_reason": reason}
    config = write_config({"resources": eval_worker_resources(evaluation_summary)})
    # --- end CN evaluation worker (N12) ---

    gateway_summary = None
    observability_summary = None
    if outputs.get("GatewayRoleArn"):
        from app.services.gateway_bootstrap import run_gateway_bootstrap

        gateway_summary = stage(
            "gateway",
            lambda: run_gateway_bootstrap(control, _client("apigateway", region), config),
        )
        config = write_config(
            {
                "resources": {
                    "gateway_id": gateway_summary["gateway"]["id"],
                    "gateway_arn": gateway_summary["gateway"]["arn"],
                    "gateway_url": gateway_summary["gateway"]["url"],
                    "api_key_provider_arn": gateway_summary["api_key_provider"]["arn"],
                }
            }
        )
        # Transaction Search is shared observability infrastructure. Keep it
        # bootstrapped independently from operator-managed Policy resources.
        from app.services.observability_bootstrap import ensure_transaction_search
        from app.services.workspace_bootstrap import ensure_spans_resource_policy

        # N11-bug10 (live cn-northwest-1): a fresh account/region has no
        # CloudWatch Logs resource policy letting X-Ray write aws/spans, and
        # UpdateTraceSegmentDestination fails with AccessDenied until one
        # exists. The workspace stage already placed it; the hub path did not.
        hub_ws = WorkspaceContext(account_id=account_id, region=region)

        def observability() -> dict[str, Any]:
            ensure_spans_resource_policy(_client("logs", region), hub_ws)
            return ensure_transaction_search(_client("xray", region))

        observability_summary = stage("observability", observability)

    # --- CN policy (D20 P1-d) ---
    # Self-hosted Cedar Policy needs no AWS resource but the CloudWatch decisions log
    # group the audit sink mirrors `policy_decisions` into; degrades like observability.
    policy_summary = stage("policy", lambda: ensure_policy_decisions(region, log=print))
    config = write_config({"resources": policy_resources(policy_summary)})
    # Per-user policy stream B3: the gateway interceptor Lambda — created, never attached
    # (scripts/attach_policy_interceptor.py with the user's go). Inert with the kill switch
    # off; degrades (reason in the resource map) so the stages after it still run.
    interceptor_summary = None
    if get_settings().policy_gateway_interceptor and outputs.get("GatewayRoleArn"):
        interceptor_summary = _policy_interceptor_stage(stage, config, region, account_id,
                                                        outputs["GatewayRoleArn"])
        config = write_config({"resources": {
            k: v for k, v in interceptor_summary.items() if k.startswith("policy_interceptor_")}})
    # --- end CN policy ---

    # --- registry defaults (N10-bug1) ---
    # Last, so the context carries the gateway URL the MCP records point at: the shared
    # gateway's targets (hr-database, office-facts) become APPROVED MCP records and the
    # sample skill a submitted AGENT_SKILLS record. The resource map read here is the one
    # this run just wrote — not Settings, which were loaded before any of it existed.
    registry_defaults = stage(
        "registry",
        lambda: ensure_registry_defaults(
            WorkspaceContext(
                account_id=account_id,
                region=region,
                id=DEFAULT_WORKSPACE_ID,
                resources=dict(config.get("resources") or {}),
            ),
            log=print,
        ),
    )

    # Skill Lab exec worker (vendored SkillOpt): role + content-addressed image
    # + runtime, plus the dedicated host venv the vendored CLIs run in. The
    # first run includes a CodeBuild image build (~10 min); later runs are a
    # tag lookup.
    skill_lab_summary = stage(
        "skill-lab",
        lambda: ensure_skill_lab(account_id, region, dict(config.get("resources") or {})),
    )
    write_config(
        {"resources": {k: v for k, v in skill_lab_summary.items() if k != "python"}}
    )

    return {
        "skill_lab": skill_lab_summary,
        "account_id": account_id,
        "region": region,
        "registry": {"id": registry_id, "created": False, "defaults": registry_defaults},
        "model_credential": {"name": credential["name"], "created": credential["created"]},
        "model_credentials": credential_summary(credentials),
        "model_providers": model_providers_summary,  # D21: the registry (SSM) pass
        "gateway": gateway_summary,
        "observability": observability_summary,
        "policy": policy_summary,
        # only with the kill switch on: the flag-off summary stays byte-identical
        **({"policy_interceptor": interceptor_summary} if interceptor_summary is not None
           else {}),
        "memory": memory_summary,
        "evaluation": evaluation_summary,
        "stack_outputs": outputs,
    }
