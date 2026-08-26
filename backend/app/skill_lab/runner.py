"""Subprocess mechanics for Skill Lab jobs: skill materialization, command
construction, env allowlist, spawn, and the exec-jobs S3 janitor.

The vendored CLI runs in the dedicated venv with an allowlisted environment —
never `os.environ.copy()` — so backend settings and operator secrets don't
reach the child. Rollouts execute on the AgentCore worker (the vendored
runner's own boto3 talks to S3/runtime); the judge goes to Bedrock via
bedrock_chat, so the child needs ambient AWS credentials only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any

import yaml

from app.core.config import get_settings
from app.core.errors import AppError
from app.services.local_exec import _AWS_CREDENTIAL_ENV, build_spawn_kwargs
from app.services.workspace import WorkspaceContext
from app.skill_lab import artifacts
from app.skill_lab.infra import EXEC_JOBS_PREFIX
from app.skill_lab.worker_build import VENDOR_ROOT, _codex_catalog_bytes

EVAL_SCRIPT = VENDOR_ROOT / "scripts" / "evaluate_skill.py"
TRAIN_SCRIPT = VENDOR_ROOT / "scripts" / "train.py"
TASKGEN_SCRIPT = VENDOR_ROOT / "scripts" / "generate_tasks.py"

# Studio PARAM_RANGES floor (S§2.1) — refuse impossible jobs before spending money.
PARAM_BOUNDS = {"workers": (1, 8), "timeout": (60, 3600), "limit": (0, 10000)}
# Studio's exec-backend pair; both CLIs are baked into the worker image and talk
# to Bedrock with the execution role (claude via CLAUDE_CODE_USE_BEDROCK, codex
# via its baked amazon-bedrock provider config).
TARGET_BACKENDS = ("claude_code_exec", "codex_exec")
# Upstream JUDGE_MODES: auto picks per task (chat for text-only, agentic when
# artifacts need inspection). The agentic judge runs on the HOST — its claude
# CLI + bwrap-sandboxed artifact parsers; see design.md for why not the worker.
JUDGE_MODES = ("auto", "chat", "agentic")
_BASE_ENV_KEYS = ("PATH", "HOME", "TMPDIR", "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR")
_EXEC_JOBS_TTL = timedelta(days=7)
# Isolation profile for the CLI child. `build_spawn_kwargs`' own defaults describe
# the studio's *caller-supplied code* posture, which misfires here: this child is
# platform code that must reach the backend's credentials and write into the job
# dir (a uid drop breaks both), and it spends minutes waiting on AgentCore and
# Bedrock (the 5-CPU-minute studio ceiling would SIGKILL a long run as
# "process exited -9"). The untrusted part — the skill's own generated code —
# runs in the exec worker's microVM, not here. Memory and file-size ceilings are
# kept and widened: a vendored bug must not take the control plane down, and the
# docker profile is forced off because it carries no rlimits at all.
_SPAWN_CEILINGS = {
    "studio_exec_backend": "subprocess",
    "studio_exec_user": "",
    "studio_exec_cpu_seconds": 24 * 3600,
    # ≥ the agentic judge's own sandbox cap: its prlimit sets --as=max(6GiB,
    # scratch*4) on the parser tree, and a child cannot raise a hard limit —
    # a 4GiB ceiling here EPERM'd the boundary probe (live 2026-08-18).
    "studio_exec_memory_mb": 8192,
    "studio_exec_max_file_mb": 1024,
}


def clamp_params(params: dict[str, Any] | None) -> dict[str, Any]:
    settings = get_settings()
    incoming = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    backend = str(incoming.get("target_backend") or TARGET_BACKENDS[0])
    if backend not in TARGET_BACKENDS:
        raise AppError(
            "skill_lab.bad_params",
            f"target_backend must be one of {'/'.join(TARGET_BACKENDS)}",
            status_code=422,
        )
    merged = {
        "target_backend": backend,
        # A blank target model resolves per backend: the CLIs consume different
        # id families (claude: Converse profile ids; codex: the catalog slugs
        # its baked amazon-bedrock provider resolves). Always explicit — an
        # empty --model would let train.py substitute upstream's non-Bedrock
        # per-backend default (gpt-4o for codex_exec).
        "target_model": (
            settings.skill_lab_codex_target_model_id
            if backend == "codex_exec"
            else settings.skill_lab_target_model_id
        ),
        "judge_model": settings.skill_lab_judge_model_id,
        "judge_mode": "auto",
        "workers": 2,
        "timeout": 600,
        "limit": 0,
    }
    merged.update(incoming)
    if merged["judge_mode"] not in JUDGE_MODES:
        raise AppError(
            "skill_lab.bad_params",
            f"judge_mode must be one of {'/'.join(JUDGE_MODES)}",
            status_code=422,
        )
    for key, (low, high) in PARAM_BOUNDS.items():
        try:
            value = int(merged[key])
        except (TypeError, ValueError):
            raise AppError(
                "skill_lab.bad_params", f"{key} must be an integer", status_code=422
            ) from None
        if not low <= value <= high:
            raise AppError(
                "skill_lab.bad_params",
                f"{key} must be between {low} and {high}",
                status_code=422,
            )
        merged[key] = value
    for key in ("target_model", "judge_model"):
        if not str(merged[key]).strip():
            raise AppError("skill_lab.bad_params", f"{key} must not be empty", status_code=422)
    return merged


def require_worker(workspace: WorkspaceContext) -> dict[str, str]:
    """The resource keys a job needs; 503 with the bootstrap hint when absent."""
    resources = workspace.resources
    needed = {
        "runtime_arn": str(resources.get("skill_lab_worker_runtime_arn") or ""),
        "bucket": str(resources.get("artifacts_bucket") or ""),
    }
    if not all(needed.values()):
        raise AppError(
            "skill_lab.not_provisioned",
            "the Skill Lab exec worker is not provisioned for this workspace — "
            "run `make bootstrap` (hub) or re-bootstrap the workspace",
            status_code=503,
        )
    if workspace.role_arn:
        # Frozen assumed-role credentials expire in <=1h; an eval/train job can
        # outlive them (parent design §8). Hub-account workspaces only for v1.
        raise AppError(
            "skill_lab.workspace_unsupported",
            "Skill Lab jobs run with the backend host's credentials and do not "
            "support assumed-role workspaces yet",
            status_code=400,
        )
    if not Path(get_settings().skill_lab_python).exists():
        raise AppError(
            "skill_lab.not_provisioned",
            "skill-lab interpreter missing — run `make bootstrap`",
            status_code=503,
        )
    return needed


def materialize_registry_skill(
    workspace: WorkspaceContext,
    record_id: str,
    dest_parent: Path,
    log: Callable[[str], None],
) -> tuple[Path, dict[str, Any]]:
    """Download a Registry skill record's bundle into <dest_parent>/skills/<name>/.

    Any-status records are accepted on purpose: a just-registered DRAFT skill is
    exactly what a user wants to evaluate (deliberate divergence from the
    APPROVED-only attachables path).
    """
    from app.deployer.zip_runtime import bundle_skill_paths_into
    from app.services import registry_console

    record = registry_console.console_get(workspace, record_id)
    try:
        definition = json.loads(
            record["descriptors"]["agentSkills"]["skillDefinition"]["inlineContent"]
        )
        name = str(definition["name"])
        path = str(definition["path"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AppError(
            "skill_lab.skill_unreadable",
            f"record {record_id} is not a readable skill record ({exc})",
            status_code=400,
        ) from exc
    bundle_skill_paths_into([path], dest_parent, log, workspace)
    # The downloader names the directory after the S3 prefix tail (and refuses a
    # tail that is not a valid skill name), so that — not the descriptor's `name`
    # field — is where the bundle actually landed. They agree for records this
    # platform registered; joining on the tail keeps a hand-written descriptor
    # from either missing the dir or steering the path.
    skill_dir = dest_parent / "skills" / path.rstrip("/").rsplit("/", 1)[-1]
    if not (skill_dir / "SKILL.md").is_file():
        raise AppError(
            "skill_lab.skill_unreadable",
            f"failed to materialize skill '{name}' from {path} — see the job log",
            status_code=502,
        )
    return skill_dir, {
        "kind": "registry",
        "record_id": record_id,
        "name": name,
        "version": str(record.get("recordVersion") or ""),
    }


def materialize_staged_skill(
    staging_id: str, index: int, dest_parent: Path
) -> tuple[Path, dict[str, Any]]:
    """Copy an inspect-staged bundle (ad-hoc upload) into the job dir.

    The staging entry is deliberately NOT consumed: registry semantics keep it
    until a successful *import*, and here it lets an operator score the same
    upload twice (or after a submit error) without re-uploading. The TTL sweep
    reclaims it.
    """
    from app.routers.registry import _staging, _sweep_staging
    from app.services.skill_ingest import SKILL_NAME_RE, validate_bundle

    _sweep_staging()
    entry = _staging.get(staging_id)
    if entry is None:
        raise AppError(
            "registry.staging_expired",
            "staging session expired or unknown — re-inspect the source",
            status_code=410,
        )
    bundles = entry["bundles"]
    if not 0 <= index < len(bundles):
        raise AppError(
            "skill_lab.skill_unreadable", f"no staged skill at index {index}", status_code=400
        )
    bundle = bundles[index]
    # Same gate as a registry import, for the same two reasons: an unimportable
    # bundle can't be evaluated either, and `name` comes from caller-supplied
    # SKILL.md frontmatter — unchecked it is a path segment the caller controls.
    validate_bundle(bundle)
    name = bundle.name
    if SKILL_NAME_RE.match(name) is None:
        raise AppError(
            "skill_lab.skill_unreadable",
            f"skill name '{name}' is not a valid skill name "
            "(lowercase letters, digits and hyphens, 3-64 chars)",
            status_code=422,
        )
    skill_dir = dest_parent / "skills" / name
    shutil.copytree(bundle.root, skill_dir)
    return skill_dir, {"kind": "upload", "name": name, "version": bundle.version}


# Converse cross-region inference-profile prefixes. The chat judge needs the
# profile id as-is; codex resolves the bare catalog slug through ~/.codex.
_PROFILE_PREFIX = re.compile(r"^(us|eu|apac|global)\.")


def judge_exec_route(judge_model: str) -> tuple[str, str]:
    """(exec backend, exec model) the agentic judge runs with for `judge_model`.

    One judge model still drives both judge modes, but the judge *agent* is a
    host CLI and only claude can run anthropic models: an openai-family judge
    (e.g. us.openai.gpt-5.6-sol) routes to the host codex CLI with the profile
    prefix stripped — codex does its own resolution via ~/.codex, the same
    coupling the worker image build already relies on for its model catalog.
    Anything non-openai keeps the claude CLI with the model id unchanged."""
    bare = _PROFILE_PREFIX.sub("", judge_model)
    if bare.startswith("openai."):
        return "codex_exec", bare
    return "claude_code_exec", judge_model


# The host binary each judge exec backend shells out to. Kept beside
# judge_exec_route because it is the same routing knowledge: readiness has to
# probe the CLI a run would actually use, not a fixed one.
JUDGE_CLI_BINARY = {"claude_code_exec": "claude", "codex_exec": "codex"}


def judge_cli_binary(judge_model: str) -> str:
    """Host binary the agentic judge needs for `judge_model`'s route."""
    backend, _ = judge_exec_route(judge_model)
    return JUDGE_CLI_BINARY[backend]


def _judge_exec_flags(params: dict[str, Any]) -> list[str]:
    """Agentic-judge CLI flags (eval path). `judge_model` feeds
    --optimizer_model (chat verdicts, Bedrock Converse via the instance role)
    AND — routed by family — the host-side judge agent's exec CLI.

    Passed for EVERY mode, `chat` included. Whether the agentic judge runs is
    decided by `--judge_mode` alone (vendored `evaluator.should_use_agentic`
    keys on the config's mode, never on flag presence), but a task can still
    escalate under a chat run — an explicit per-task `judge_mode` outranks the
    run-level one. Omitting the flags there left that escalation on the vendored
    default backend (`claude_code_exec`) regardless of the judge model's family,
    which is how an openai-family judge reached the claude CLI on the prod host
    and died with FileNotFoundError."""
    backend, model = judge_exec_route(str(params["judge_model"]))
    return [
        "--judge_exec_backend", backend,
        "--judge_exec_model", model,
        "--judge_exec_effort", "low",
        "--judge_sandbox_command", str(get_settings().skill_lab_judge_sandbox),
    ]


# Provider config the codex judge client starts from. The vendored judge runs
# codex with an isolated, initially-empty CODEX_HOME (fail-closed: no user
# config, rules or sessions leak in), which would pin it to codex's default
# `openai` provider — this seed keeps the isolation but swaps the provider to
# Bedrock (Mantle). Region pinned to us-east-1 like the worker image's baked
# config: us-west-2's Mantle catalog lacks openai.gpt-5.6-sol (live-verified).
_JUDGE_CODEX_CONFIG = """\
model_provider = "amazon-bedrock"
web_search = "disabled"
model_catalog_json = "{catalog}"

[model_providers.amazon-bedrock.aws]
region = "us-east-1"

[features.multi_agent_v2]
enabled = false
"""


def ensure_judge_codex_home() -> Path:
    """Materialize the judge codex-home seed (config + bedrock model catalog).

    Rewritten on every job submit — cheap, and it tracks host-catalog updates
    the same way a worker image rebuild would. The catalog falls back to `{}`
    when the host has none (mirrors worker_build._codex_catalog_bytes; that
    file embeds proprietary model instructions and is never committed).
    """
    seed = artifacts.JOBS_DIR.parent / "codex-judge-home"
    seed.mkdir(parents=True, exist_ok=True)
    catalog = seed / "bedrock-models.json"
    catalog.write_bytes(_codex_catalog_bytes())
    (seed / "config.toml").write_text(
        _JUDGE_CODEX_CONFIG.format(catalog=catalog), encoding="utf-8"
    )
    return seed


def _train_judge_env(params: dict[str, Any]) -> dict[str, str]:
    """Train-config counterpart of `_judge_exec_flags` (adapter kwarg names).

    Same reasoning: emitted for every mode so an escalation cannot land on a CLI
    that does not match the judge model's family."""
    backend, model = judge_exec_route(str(params["judge_model"]))
    return {
        "judge_backend": backend,
        "judge_model": model,
        "judge_effort": "low",
        "judge_sandbox_command": str(get_settings().skill_lab_judge_sandbox),
    }


def build_eval_command(
    *,
    skill_dir: Path,
    tasks_file: Path,
    out_dir: Path,
    params: dict[str, Any],
    assets_dir: Path | None = None,
) -> list[str]:
    command = [
        get_settings().skill_lab_python,
        str(EVAL_SCRIPT),
        "--skill", str(skill_dir),
        "--tasks", str(tasks_file),
        "--out_root", str(out_dir),
        "--target_backend", str(params["target_backend"]),
        "--model", str(params["target_model"]),
        "--optimizer_backend", "bedrock_chat",
        "--optimizer_model", str(params["judge_model"]),
        "--judge_mode", str(params["judge_mode"]),
        *_judge_exec_flags(params),
        "--workers", str(params["workers"]),
        "--timeout", str(params["timeout"]),
    ]
    if assets_dir is not None:
        command += ["--assets-dir", str(assets_dir)]
    if int(params.get("limit") or 0) > 0:
        command += ["--limit", str(params["limit"])]
    return command


# Studio taskgen bounds (count mirrors upstream PARAM_RANGES; guidance is free
# text folded into the generation prompt, capped so a paste can't blow it up).
TASKGEN_PARAM_BOUNDS = {"count": (1, 30), "timeout": (60, 3600)}
TASKGEN_GUIDANCE_MAX_CHARS = 4000
# Upstream studio floor: every skill of a multi-skill set must be targeted by
# at least this many distinct generated tasks.
TASKGEN_MIN_TASKS_PER_SKILL = 1
# Attachment bounds are deliberately tighter than the per-task asset limits: the
# generation agent decides how much of a document to pull into its context, while
# evaluation only copies bytes. Per-file size and the accepted formats still come
# from task_assets, so there is one place that decides what may be uploaded.
TASKGEN_MAX_ATTACHMENTS = 8
TASKGEN_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
# Where attachments live inside the generation work dir, and — by contract stated
# in the prompt — inside the evaluation work dir of a task that declares one.
TASKGEN_ATTACHMENT_DIR = "data"


def clamp_taskgen_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """Validated params for a taskgen job — the generation agent has no judge
    and no workers; it is one exec run (plus one validation-feedback retry)."""
    settings = get_settings()
    incoming = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    backend = str(incoming.get("target_backend") or TARGET_BACKENDS[0])
    if backend not in TARGET_BACKENDS:
        raise AppError(
            "skill_lab.bad_params",
            f"target_backend must be one of {'/'.join(TARGET_BACKENDS)}",
            status_code=422,
        )
    merged = {
        "target_backend": backend,
        "model": (
            settings.skill_lab_codex_target_model_id
            if backend == "codex_exec"
            else settings.skill_lab_target_model_id
        ),
        "count": 5,
        "guidance": "",
        "timeout": 900,
    }
    merged.update(incoming)
    for key, (low, high) in TASKGEN_PARAM_BOUNDS.items():
        try:
            value = int(merged[key])
        except (TypeError, ValueError):
            raise AppError(
                "skill_lab.bad_params", f"{key} must be an integer", status_code=422
            ) from None
        if not low <= value <= high:
            raise AppError(
                "skill_lab.bad_params",
                f"{key} must be between {low} and {high}",
                status_code=422,
            )
        merged[key] = value
    if not str(merged["model"]).strip():
        raise AppError("skill_lab.bad_params", "model must not be empty", status_code=422)
    guidance = str(merged["guidance"])
    if len(guidance) > TASKGEN_GUIDANCE_MAX_CHARS:
        raise AppError(
            "skill_lab.bad_params",
            f"guidance must be at most {TASKGEN_GUIDANCE_MAX_CHARS} characters",
            status_code=422,
        )
    merged["guidance"] = guidance
    return merged


def write_expansion_snapshot(
    job_dir: Path, taskset_id: str, target_split: str, tasks_by_split: dict[str, Any]
) -> Path:
    """The --existing-tasks snapshot generate_tasks.py decodes strictly: full
    current content of the target task set, so the agent avoids semantic
    duplicates and the reserved-id collision check has the complete id set."""
    job_dir.mkdir(parents=True, exist_ok=True)
    snapshot = job_dir / "existing_tasks.json"
    snapshot.write_text(
        json.dumps(
            {
                "taskset_id": taskset_id,
                "target_split": target_split,
                "tasks_by_split": tasks_by_split,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return snapshot


def build_taskgen_command(
    *,
    skill_dirs: list[Path],
    out_root: Path,
    params: dict[str, Any],
    expansion: tuple[Path, str] | None = None,
    attachments: Path | None = None,
    attachment_assets: Path | None = None,
) -> list[str]:
    """argv for scripts/generate_tasks.py; generated_tasks.json lands in out_root.

    Multi-skill mode mirrors upstream: the requested count is floored at one
    task per skill and --min-tasks-per-skill enforces coverage."""
    count = int(params["count"])
    if len(skill_dirs) > 1:
        count = max(count, len(skill_dirs) * TASKGEN_MIN_TASKS_PER_SKILL)
    command = [get_settings().skill_lab_python, str(TASKGEN_SCRIPT)]
    for skill_dir in skill_dirs:
        command += ["--skill", str(skill_dir)]
    command += [
        "--backend", str(params["target_backend"]),
        "--model", str(params["model"]),
        "--count", str(count),
        "--timeout", str(params["timeout"]),
        "--out_root", str(out_root),
    ]
    if len(skill_dirs) > 1:
        command += ["--min-tasks-per-skill", str(TASKGEN_MIN_TASKS_PER_SKILL)]
    if str(params.get("guidance") or "").strip():
        command += ["--guidance", str(params["guidance"])]
    if expansion is not None:
        snapshot_path, target_split = expansion
        command += ["--existing-tasks", str(snapshot_path), "--target-split", target_split]
    if attachments is not None and attachment_assets is not None:
        # The manifest names the documents; the assets root holds the bytes keyed
        # by digest. Both point into the job's immutable snapshot, never staging.
        command += [
            "--attachments", str(attachments),
            "--attachment-assets", str(attachment_assets),
        ]
    return command


def build_job_env(workspace: WorkspaceContext) -> dict[str, str]:
    """Allowlist env for the vendored CLI (foundation spec §4 contract)."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key in _BASE_ENV_KEYS or key in _AWS_CREDENTIAL_ENV
    }
    env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            # stdout goes to log.txt (a file → Python block-buffers); unbuffered
            # keeps the tail live AND preserves output when a cancel kills the
            # process mid-buffer — exactly when the operator wants the log.
            "PYTHONUNBUFFERED": "1",
            # The allowlist drops the locale vars a login shell would carry, so
            # under systemd the child would otherwise pick ASCII for stdout and
            # die on the first non-ASCII task title it prints.
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "SKILLOPT_EXEC_RUNNER": "agentcore",
            # The exec-job payload carries the CLIENT process's codex config into
            # the worker (agentcore_worker._apply_exec_config overrides the image
            # env with cfg.get("full_auto")/cfg.get("sandbox")), so these must be
            # set HERE, not in the Dockerfile. Live-verified 2026-08-18:
            # - codex >= 0.147 dropped `exec --full-auto`; false makes the
            #   harness pass `--sandbox <CODEX_EXEC_SANDBOX>` instead.
            # - codex's linux sandbox is bubblewrap, and the worker microVM's
            #   kernel can't run it (every command/file-read fails). The microVM
            #   IS the sandbox — one task per Firecracker VM, destroyed after —
            #   so OS-level sandboxing is disabled, the exact posture the
            #   claude_code_exec path has always run with (allow_file_edits +
            #   Bash, no OS sandbox).
            "CODEX_EXEC_FULL_AUTO": "false",
            "CODEX_EXEC_SANDBOX": "danger-full-access",
            # The agentic judge's claude CLI runs on THIS host (never the
            # worker) and must take the Bedrock path with the instance role.
            # Rollout claude runs in the worker with the image's own env, so
            # this key only affects the judge.
            "CLAUDE_CODE_USE_BEDROCK": "1",
            # Same for a codex judge (openai-family judge model): the vendored
            # harness isolates CODEX_HOME per call, and this seed swaps its
            # provider from codex's default `openai` to Bedrock Mantle.
            "SKILLOPT_JUDGE_CODEX_HOME": str(ensure_judge_codex_home()),
            "SKILLOPT_AGENTCORE_RUNTIME_ARN": str(
                workspace.resources.get("skill_lab_worker_runtime_arn") or ""
            ),
            "SKILLOPT_AGENTCORE_S3_BUCKET": str(
                workspace.resources.get("artifacts_bucket") or ""
            ),
            "SKILLOPT_AGENTCORE_S3_PREFIX": EXEC_JOBS_PREFIX,
            "SKILLOPT_AGENTCORE_REGION": workspace.region,
            "AWS_REGION": workspace.region,
        }
    )
    return env


def spawn(command: list[str], env: dict[str, str], log_file: IO[bytes]) -> subprocess.Popen:
    """Process-group leader with the job ceilings; stdout+stderr → log.txt."""
    return subprocess.Popen(
        command,
        cwd=str(VENDOR_ROOT),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        **build_spawn_kwargs(get_settings().model_copy(update=_SPAWN_CEILINGS)),
    )


def sweep_exec_jobs_prefix(workspace: WorkspaceContext, log: Callable[[str], None]) -> None:
    """Best-effort removal of exec-jobs S3 debris older than the TTL — the
    vendored runner never cleans up after itself (upstream leaves tarballs
    forever). Piggybacked on job completion; failures are logged, never fatal."""
    bucket = str(workspace.resources.get("artifacts_bucket") or "")
    if not bucket:
        return
    try:
        s3 = workspace.client("s3")
        cutoff = datetime.now(UTC) - _EXEC_JOBS_TTL
        paginator = s3.get_paginator("list_objects_v2")
        stale: list[dict[str, str]] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{EXEC_JOBS_PREFIX}/"):
            stale.extend(
                {"Key": obj["Key"]}
                for obj in page.get("Contents", [])
                if obj.get("LastModified") and obj["LastModified"] < cutoff
            )
        for start in range(0, len(stale), 1000):
            s3.delete_objects(
                Bucket=bucket, Delete={"Objects": stale[start : start + 1000], "Quiet": True}
            )
        if stale:
            log(f"exec-jobs janitor: removed {len(stale)} stale object(s)")
    except Exception as exc:  # noqa: BLE001 — hygiene must never fail a job
        log(f"exec-jobs janitor skipped: {exc}")


def remove_job_dir(job_id: str) -> None:
    shutil.rmtree(artifacts.job_dir(job_id), ignore_errors=True)


# ── training ───────────────────────────────────────────────────────────────

TRAIN_PARAM_BOUNDS = {"epochs": (1, 10), "learning_rate": (1, 16)}
GATE_METRICS = ("hard", "soft", "mixed")
BASE_TRAIN_CONFIG = VENDOR_ROOT / "configs" / "skilleval" / "default.yaml"
SINGLE_SPLIT_RATIO = "4:3:3"  # studio default for un-split task sets


MAX_TRAINABLE_FILES = 32


def _clamp_trainable_files(raw: Any) -> list[str]:
    """Optional multi-doc training whitelist (upstream `trainable_files`):
    relative paths inside the skill dir whose text co-evolves with SKILL.md as
    one bundle. Mirrors the vendored bundle codec's path safety rules without
    importing it (boundary rule); SKILL.md itself is always trainable and must
    not be listed. Existence is checked by the bundle-build step at submit."""
    if raw in (None, ""):
        return []
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise AppError(
            "skill_lab.bad_params",
            "trainable_files must be a list of relative paths",
            status_code=422,
        )
    cleaned: list[str] = []
    for item in raw:
        path = item.strip().replace("\\", "/")
        parts = [p for p in path.split("/") if p not in ("", ".")]
        if not parts or path.startswith("/") or ".." in parts or re.match(r"^[A-Za-z]:", path):
            raise AppError(
                "skill_lab.bad_params",
                f"trainable_files entry is not a safe relative path: {item!r}",
                status_code=422,
            )
        normalized = "/".join(parts)
        if normalized == "SKILL.md":
            raise AppError(
                "skill_lab.bad_params",
                "SKILL.md is always trainable — do not list it in trainable_files",
                status_code=422,
            )
        if normalized not in cleaned:
            cleaned.append(normalized)
    if len(cleaned) > MAX_TRAINABLE_FILES:
        raise AppError(
            "skill_lab.bad_params",
            f"trainable_files supports at most {MAX_TRAINABLE_FILES} files",
            status_code=422,
        )
    return cleaned


def clamp_train_params(params: dict[str, Any] | None) -> dict[str, Any]:
    merged = clamp_params(params)
    merged["trainable_files"] = _clamp_trainable_files((params or {}).get("trainable_files"))
    # soft gate by default: strict evidence-based judges rarely move the
    # binary hard score in few epochs, so a hard-gated run rejects genuinely
    # better candidates (live-observed on the logtriage demo); operators
    # wanting the stricter bar pick it in the wizard.
    extras = {"epochs": 1, "learning_rate": 4, "gate_metric": "soft"}
    extras.update(
        {k: v for k, v in (params or {}).items() if k in extras and v not in (None, "")}
    )
    for key, (low, high) in TRAIN_PARAM_BOUNDS.items():
        try:
            value = int(extras[key])
        except (TypeError, ValueError):
            raise AppError(
                "skill_lab.bad_params", f"{key} must be an integer", status_code=422
            ) from None
        if not low <= value <= high:
            raise AppError(
                "skill_lab.bad_params",
                f"{key} must be between {low} and {high}",
                status_code=422,
            )
        extras[key] = value
    if extras["gate_metric"] not in GATE_METRICS:
        raise AppError(
            "skill_lab.bad_params",
            f"gate_metric must be one of {'/'.join(GATE_METRICS)}",
            status_code=422,
        )
    merged.update(extras)
    return merged


def materialize_train_splits(
    taskset_dir: Path, mode: str, job_dir: Path
) -> tuple[dict[str, Any], bool]:
    """Env split config + whether a real test split exists.

    SplitDataLoader's split_dir layout is `<dir>/<split>/items.json` SUBDIRS,
    while tasksets store flat `<split>.json` files — so split-mode sets are
    copied into the loader's shape. A missing test split is backfilled with a
    copy of val and `evaluation.eval_test` turned off so the duplicate is never
    scored (studio parity). Single-mode sets use the loader's deterministic
    ratio split instead.
    """
    if mode == "single":
        return {
            "split_mode": "ratio",
            "data_path": str(taskset_dir / "tasks.json"),
            "split_ratio": SINGLE_SPLIT_RATIO,
            "split_output_dir": str(job_dir / "splits"),
            **(
                {"assets_dir": str(taskset_dir / "assets")}
                if (taskset_dir / "assets").is_dir()
                else {}
            ),
        }, True
    splits_dir = job_dir / "splits"
    has_test = (taskset_dir / "test.json").is_file()
    for split in ("train", "val", "test"):
        source = taskset_dir / f"{split}.json"
        if split == "test" and not has_test:
            source = taskset_dir / "val.json"
        target = splits_dir / split / "items.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return {
        "split_mode": "split_dir",
        "split_dir": str(splits_dir),
        **(
            {"assets_dir": str(taskset_dir / "assets")}
            if (taskset_dir / "assets").is_dir()
            else {}
        ),
    }, has_test


# Bundle codec CLI (`build` at submit, `split` at publish). Runs as a
# subprocess like every other vendored entry point (boundary rule); the module
# is pure stdlib, and `-m` resolves because the child's cwd is the vendor root.
BUNDLE_MODULE = "skillopt.envs.skilleval.bundle"


def _run_bundle_cli(args: list[str], log: Callable[[str], None]) -> None:
    result = subprocess.run(
        [get_settings().skill_lab_python, "-m", BUNDLE_MODULE, *args],
        cwd=VENDOR_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    for line in (result.stdout or "").splitlines():
        log(f"[bundle] {line}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise AppError(
            "skill_lab.bad_params",
            detail[-1] if detail else f"bundle {args[0]} failed",
            status_code=422,
        )


def build_seed_bundle(
    *, skill_dir: Path, files: list[str], out: Path, log: Callable[[str], None]
) -> Path:
    """Join the trainable files + SKILL.md into the single seed document the
    ReflACT trainer evolves (upstream studio's pre-train bundle step). Fails
    422 when a listed file is missing — at submit, not minutes into the run."""
    _run_bundle_cli(
        ["build", str(skill_dir), "--files", ",".join(files), "--out", str(out)], log
    )
    return out


def split_trained_bundle(
    *, bundle_file: Path, skill_dir: Path, out_dir: Path, log: Callable[[str], None]
) -> Path:
    """Split a trained bundle back into a deployable skill dir: frozen files
    copied from the original, trained sections overwritten (whitelist-safe —
    the codec drops sections whose path was never trainable)."""
    _run_bundle_cli(
        ["split", str(bundle_file), "--skill_dir", str(skill_dir), "--out_dir", str(out_dir)],
        log,
    )
    return out_dir


def build_train_config(
    *,
    skill_dir: Path,
    split_env: dict[str, Any],
    eval_test: bool,
    out_config: Path,
    params: dict[str, Any],
    seed_bundle: Path | None = None,
) -> Path:
    """Write the job's train YAML. `_base_` is the vendored default by absolute
    path — train.py resolves inheritance itself, so the backend never imports
    skillopt.config (boundary rule). Probe-verified: an absolute `_base_`
    resolves (os.path.join(dir, abs) is abs)."""
    trainable = list(params.get("trainable_files") or [])
    if bool(trainable) != (seed_bundle is not None):
        raise ValueError("trainable_files and seed_bundle must be passed together")
    config: dict[str, Any] = {
        "_base_": str(BASE_TRAIN_CONFIG),
        "model": {
            "target_backend": str(params["target_backend"]),
            "target": str(params["target_model"]),
            "optimizer_backend": "bedrock_chat",
            "optimizer": str(params["judge_model"]),
        },
        "train": {"num_epochs": int(params["epochs"])},
        "optimizer": {"learning_rate": int(params["learning_rate"])},
        "evaluation": {
            "gate_metric": str(params["gate_metric"]),
            "eval_test": bool(eval_test),
        },
        "env": {
            # Multi-doc training seeds from the bundle; the adapter needs the
            # matching whitelist (exact kwarg name `trainable_files`).
            "skill_init": str(seed_bundle if seed_bundle else skill_dir / "SKILL.md"),
            **({"trainable_files": trainable} if trainable else {}),
            "skill_dir": str(skill_dir),
            "judge_mode": str(params["judge_mode"]),
            # adapter kwargs (exact names): the agentic judge's exec CLI +
            # bwrap-sandboxed parsers run on the host; chat mode ignores these.
            # The backend/model pair is family-routed like the eval path.
            **_train_judge_env(params),
            "workers": int(params["workers"]),
            "timeout": int(params["timeout"]),
            "limit": int(params["limit"]),
            **split_env,
        },
    }
    out_config.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return out_config


def build_train_command(*, config_file: Path, out_dir: Path) -> list[str]:
    return [
        get_settings().skill_lab_python,
        str(TRAIN_SCRIPT),
        "--config", str(config_file),
        "--out_root", str(out_dir),
    ]
