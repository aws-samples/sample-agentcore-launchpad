"""Runtime zip fast path (Strands / Studio agents).

    generate  → render the Strands template from the AgentSpec
    package   → pip install (ARM64 wheels) → zip → S3 artifacts bucket
    provision → reuse the shared execution role
    deploy    → CreateAgentRuntime + poll READY (5–15 min tolerated)
    register  → create/refresh the A2A registry record (auto-submit)

Package/deploy internals adapted from agentcore_eva_opt backend/app/deployer.py
(github.com/xiehust/agentcore_eva_opt); reworked to use the shared CDK bucket
and role instead of per-agent resources.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.runtime_target import TARGET_PYTHON, pip_platform_args, uv_platform
from app.deployer import build_dirs, log_retention
from app.deployer.environment import runtime_environment
from app.deployer.filesystem import filesystem_configurations
from app.deployer.pipeline import (
    MethodCapabilities,
    StageContext,
    StageInterrupted,
    StageResult,
    killed_by_signal,
    register_method,
)
from app.deployer.return_url import RETURN_URL_WARNING, register_return_url_stage
from app.models.ledger import Agent
from app.schemas.agent import AgentSpec
from app.services import agent_iam, policy_enforcement
from app.services import inbound_auth as inbound_auth_service
from app.services.agentcore import runtime as rt
from app.services.agentcore.client import control_client
from app.services.requirements_txt import (
    RESOLVE_FIX_HINTS,
    describe_exit,
    summarize_resolver_failure,
    terminated_sentence,
)
from app.services.skill_ingest import SKILL_BUNDLE_MAX_BYTES, SKILL_NAME_RE
from app.services.workspace import WorkspaceContext
from app.templates.strands_agent import base_requirements, render_main_py


def sanitize_runtime_name(name: str) -> str:
    """Runtime names must be alphanumeric/underscore; suffix keeps them unique.

    The base is shared with the execution role's workload-identity scope
    (``agent_iam.own_workload_identity_arn``), which pins the 6-hex suffix
    length — change both together."""
    suffix = uuid.uuid4().hex[: agent_iam.RUNTIME_SUFFIX_HEX_LEN]
    return f"{agent_iam.runtime_name_base(name)}_{suffix}"


# Runtime states a resumed deploy may take over; CREATE_FAILED/UPDATE_FAILED/DELETING
# are left alone (the former would only fail the poll, the latter is going away).
ADOPTABLE_RUNTIME_STATUSES = frozenset({"CREATING", "UPDATING", "READY"})
_RUNTIME_SUFFIX_RE = re.compile(rf"^_[0-9a-f]{{{agent_iam.RUNTIME_SUFFIX_HEX_LEN}}}$")


def find_adoptable_runtime(
    client: Any, db: Any, row: Agent, role_arn: str | None, log: Callable[[str], None]
) -> dict[str, Any] | None:
    """The Runtime a dead process already created for ``row``, if exactly one exists.

    N10v2-bug1: ``CreateAgentRuntime`` is accepted, the backend restarts before the
    ledger commit (or the resumed stage fails earlier), and a naive re-run issues a
    second create — orphaning the first runtime behind the row. The runtime name is
    ``<runtime_name_base(name)>_<6 hex>`` with a random suffix, so the lookup is by the
    deterministic base and the 6-hex shape; because bases are not unique (``a-b`` and
    ``a_b`` share one, and so may same-named agents in two workspaces), a candidate is
    adopted only when its ``roleArn`` is the role this deploy is about to use (per-agent
    roles make that exact) **and** no other ledger row already records it (the guard
    that still holds on the shared role). Anything else → ``None`` → create as before.
    A denied ``ListAgentRuntimes`` is logged and treated as "nothing to adopt".
    """
    base = agent_iam.runtime_name_base(row.name)
    try:
        summaries = rt.list_runtimes(client)
    except Exception as exc:  # noqa: BLE001 — listing is an optimisation, never a blocker
        log(f"ListAgentRuntimes failed ({type(exc).__name__}: {exc}) — creating a new runtime")
        return None
    candidates: list[dict[str, Any]] = []
    for summary in summaries:
        name = str(summary.get("agentRuntimeName") or "")
        runtime_id = summary.get("agentRuntimeId")
        if not runtime_id or not name.startswith(base):
            continue
        if not _RUNTIME_SUFFIX_RE.match(name[len(base):]):
            continue
        try:
            detail = rt.get_runtime(client, runtime_id)
        except Exception as exc:  # noqa: BLE001 — deleted between list and get, or denied
            log(f"GetAgentRuntime {runtime_id} failed ({type(exc).__name__}) — not adopting it")
            continue
        if detail.get("status") not in ADOPTABLE_RUNTIME_STATUSES:
            continue
        owner_role = detail.get("roleArn")
        if role_arn and owner_role and owner_role != role_arn:
            continue
        taken = (
            db.query(Agent)
            .filter(Agent.resource_id == runtime_id, Agent.id != row.id)
            .first()
        )
        if taken is not None:
            continue
        candidates.append({**detail, "agentRuntimeName": name})
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        log(
            f"{len(candidates)} runtimes named {base}_* could be this agent's — "
            "ambiguous, creating a new runtime"
        )
    return None


# The deploy target: AgentCore Runtime zips run ARM64 on Python 3.13. The
# manylinux level (default manylinux_2_28 — AL2023/glibc 2.34, measured
# 2026-09-18) is defined once in app/core/runtime_target.py because the resolve
# and the install must agree — resolving for this host and installing for
# aarch64 would produce a lock that does not match the artifact.

LOCK_FILENAME = "requirements.lock"


def _build_env() -> dict[str, str]:
    """Ambient environment plus the configured PyPI mirror — `uv pip compile`
    and `pip install` both read these; nothing else in this module should set
    `UV_INDEX_URL`/`UV_DEFAULT_INDEX`/`PIP_INDEX_URL` directly.

    uv 0.9.10 (this repo's pinned version) deprecates `UV_INDEX_URL` in favor
    of `UV_DEFAULT_INDEX` — both are set to the same value so the mirror
    applies regardless of which uv release actually runs the resolve.
    """
    settings = get_settings()
    env = {
        **os.environ,
        "UV_INDEX_URL": settings.pip_index_url,
        "UV_DEFAULT_INDEX": settings.pip_index_url,
        "PIP_INDEX_URL": settings.pip_index_url,
    }
    if settings.pip_extra_index_url:
        # N11-obs25: the optional secondary index — uv's additional indexes (UV_INDEX)
        # and pip's extra index — consulted after the primary mirror.
        env["UV_INDEX"] = settings.pip_extra_index_url
        env["PIP_EXTRA_INDEX_URL"] = settings.pip_extra_index_url
    return env


def _resolver_detail(raw: str, limit: int = 600) -> str:
    """The resolver's own words, bounded and single-line — the summary above names the
    package, this names the cause (N13-bug1 / N11-obs25: a mirror serving an expired
    certificate surfaced only as "no matching distribution" until the raw text was
    kept)."""
    text = " ".join((raw or "").split())
    if not text:
        return ""
    if len(text) > limit:
        text = "…" + text[-limit:]
    return f" (resolver output: {text})"


def _compile_lock(
    requirements: list[str],
    build_dir: Path,
    compile_runner: Callable[..., Any],
    python_version: str | None = None,
) -> Path:
    """Resolve the declared requirements into a fully hashed lockfile.

    Without this, a build installs whatever the index serves at the moment it runs
    — including for the platform's own ranged pins — so nothing about an artifact
    is reproducible and there is no record of what went into it.

    Deliberately no fallback: if the resolve fails, the stage fails. A supply-chain
    control that silently reverts to the unchecked install is decoration.
    """
    declared = build_dir / "requirements.in"
    lock = build_dir / LOCK_FILENAME
    declared.write_text("\n".join(requirements) + "\n", encoding="utf-8")
    proc = compile_runner(
        [
            "uv", "pip", "compile", str(declared),
            "--generate-hashes", "--quiet",
            "--python-version", python_version or TARGET_PYTHON,
            "--python-platform", uv_platform(),
            # The install below is binary-only. Resolve from that same artifact
            # set, or uv can lock an sdist-only release that pip then cannot
            # install for the Runtime's ARM64 binary-only target.
            "--only-binary=:all:",
            "-o", str(lock),
        ],
        capture_output=True,
        text=True,
        env=_build_env(),
    )
    if proc.returncode != 0:
        raw = proc.stderr or proc.stdout or ""
        _raise_if_stopped_with_the_service("requirements resolve (uv)", proc.returncode)
        raise RuntimeError(
            f"could not resolve the requirements into a hashed lockfile "
            f"({describe_exit(proc.returncode)}): "
            + summarize_resolver_failure(
                raw,
                python_version=python_version or TARGET_PYTHON,
                hints=RESOLVE_FIX_HINTS,
                returncode=proc.returncode,
            )
            + _resolver_detail(raw)
        )
    return lock


def _raise_if_stopped_with_the_service(what: str, returncode: int | None) -> None:
    """N17-obs113: a build subprocess a signal killed (`systemctl restart` SIGTERMs the
    whole control group; an operator's kill; the OOM killer) is not a failed build — the
    stage is interrupted: the pipeline resumes it at the next start when the service is
    stopping, else re-runs it once in-process; `failure` is the terminal message when the
    retry is killed too."""
    if killed_by_signal(returncode):
        raise StageInterrupted(
            f"{what} was killed ({describe_exit(returncode)}) — the stage re-runs",
            failure=f"{what} failed ({describe_exit(returncode)}): "
            + terminated_sentence(returncode or 0),
        )


def build_zip(
    code: str,
    requirements: list[str],
    build_dir: Path,
    pip_runner: Callable[..., Any] = subprocess.run,
    on_pkg_ready: Callable[[Path], None] | None = None,
    compile_runner: Callable[..., Any] | None = None,
    on_lock_ready: Callable[[int], None] | None = None,
) -> Path:
    """Resolve → lock → hash-verified install → zip.

    ``on_pkg_ready`` is invoked with the assembled package directory after the
    entrypoint is written but before zipping — the hook used to stage bundle
    files and Skill snapshots (the recursive walk below picks them up).
    ``on_lock_ready`` receives the number of locked packages, for the job log.
    """
    if build_dir.exists():
        shutil.rmtree(build_dir)
    pkg_dir = build_dir / "pkg"
    pkg_dir.mkdir(parents=True)

    lock = _compile_lock(requirements, build_dir, compile_runner or pip_runner)
    locked_lines = [
        line for line in lock.read_text(encoding="utf-8").splitlines()
        if "==" in line and not line.lstrip().startswith("#")
    ]
    if on_lock_ready is not None:
        on_lock_ready(len(locked_lines))

    proc = pip_runner(
        [
            sys.executable, "-m", "pip", "install",
            # --require-hashes: every artifact must match the lock, so a
            # compromised or re-uploaded distribution fails the build instead of
            # shipping.
            "--require-hashes", "-r", str(lock),
            "-t", str(pkg_dir),
            *pip_platform_args(),
            "--only-binary=:all:",
            "--python-version", TARGET_PYTHON,
            "--quiet",
        ],
        capture_output=True,
        text=True,
        env=_build_env(),
    )
    if proc.returncode != 0:
        _raise_if_stopped_with_the_service("pip install", proc.returncode)
        raise RuntimeError(
            f"pip install of the locked requirements failed ({describe_exit(proc.returncode)}): "
            + summarize_resolver_failure(
                proc.stderr or "", hints=RESOLVE_FIX_HINTS, returncode=proc.returncode
            )
        )

    (pkg_dir / "main.py").write_text(code, encoding="utf-8")
    (pkg_dir / "requirements.txt").write_text("\n".join(requirements) + "\n", encoding="utf-8")
    # The lock ships inside the artifact: what is deployed carries the exact
    # record of what it was built from.
    (pkg_dir / LOCK_FILENAME).write_text(lock.read_text(encoding="utf-8"), encoding="utf-8")

    if on_pkg_ready is not None:
        on_pkg_ready(pkg_dir)

    zip_path = build_dir / "deployment_package.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(pkg_dir):
            for name in files:
                if name.endswith(".pyc") or "__pycache__" in root:
                    continue
                full = Path(root) / name
                zf.write(full, full.relative_to(pkg_dir))
    return zip_path


# Studio's generated code references skills as os.path.join(_skills_dir, "<name>");
# the runtime resolves that to Path(__file__).parent/"skills"/<name>. This is the
# same emission pattern upstream extracts (agentcore_deployment_service.py).
_SKILL_REF_RE = re.compile(r'os\.path\.join\(\s*_skills_dir\s*,\s*"([a-z0-9-]+)"\s*\)')
_SKILL_BUNDLE_MAX_BYTES = SKILL_BUNDLE_MAX_BYTES  # shared with the ingest producer


def extract_skill_names(code: str) -> list[str]:
    """Studio skill names the generated code references — unique, source order."""
    names: list[str] = []
    for name in _SKILL_REF_RE.findall(code):
        if name not in names:
            names.append(name)
    return names


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    """s3://bucket/prefix/ → (bucket, prefix)."""
    rest = uri[len("s3://"):] if uri.startswith("s3://") else uri
    bucket, _, prefix = rest.partition("/")
    return bucket, prefix


def _approved_skill_paths(
    workspace: WorkspaceContext, log: Callable[[str], None]
) -> dict[str, str]:
    """{skill name → s3 prefix uri} for every APPROVED AGENT_SKILLS record."""
    from app.services import registry_backend

    backend = registry_backend.backend_or_none()
    if backend is None:
        log("skill registry unavailable — skills skipped")
        return {}
    try:
        paths: dict[str, str] = {}
        for summary in backend.list_records(workspace, "AGENT_SKILLS", "APPROVED"):
            record = backend.get_record(workspace, summary["recordId"])
            try:
                definition = json.loads(
                    record["descriptors"]["agentSkills"]["skillDefinition"]["inlineContent"]
                )
            except (KeyError, TypeError, ValueError):
                continue  # malformed descriptor — skip, never break the deploy
            path = definition.get("path") or ""
            if path:
                paths[record["name"]] = path
        return paths
    except Exception as exc:  # registry lookup must never break a deploy
        log(f"skill registry lookup failed ({type(exc).__name__}) — skills skipped")
        return {}


def _download_skill_prefix(
    s3_client: Any,
    bucket: str,
    prefix: str,
    dest: Path,
    name: str,
    log: Callable[[str], None],
) -> tuple[int, int]:
    """Download every object under s3://bucket/prefix into dest/, preserving the
    relative path. Returns (file_count, byte_count); skips the whole skill when
    its cumulative size exceeds the per-skill cap."""
    objects: list[dict] = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        objects.extend(page.get("Contents", []))
    total = sum(obj.get("Size", 0) for obj in objects)
    if total > _SKILL_BUNDLE_MAX_BYTES:
        log(f"skill '{name}' is {total / 1e6:.1f}MB — exceeds 50MB cap, skipped")
        return 0, 0
    dest_root = dest.resolve()
    count = 0
    for obj in objects:
        key = obj["Key"]
        rel = key[len(prefix):].lstrip("/") if key.startswith(prefix) else key
        if not rel or key.endswith("/"):
            continue
        target = (dest / rel).resolve()
        if not str(target).startswith(str(dest_root) + os.sep):
            log(f"skill '{name}' object '{key}' escapes bundle dir — skipped")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        s3_client.download_file(bucket, key, str(target))
        count += 1
    return count, total


def bundle_skills(
    spec: AgentSpec,
    code: str,
    pkg_dir: Path,
    log: Callable[[str], None],
    workspace: WorkspaceContext,
    *,
    skill_records: dict[str, str] | None = None,
    s3_client: Any = None,
) -> dict[str, Any]:
    """Bundle the Skills owned by this artifact into ``pkg_dir/skills/{name}/``.

    Studio remains code-reference-driven. Platform-generated zip runtimes use
    the explicit ``spec.skills`` prefixes selected in the wizard. Converted
    Harness bundles keep their exported request-time fetcher and therefore do
    not receive a second package-time snapshot. Any Skill issue logs + skips.
    """
    if spec.method == "studio":
        return bundle_skills_into(
            code, pkg_dir, log, workspace, skill_records=skill_records, s3_client=s3_client
        )
    if spec.method == "zip_runtime" and spec.code_bundle is None:
        return bundle_skill_paths_into(
            spec.skills, pkg_dir, log, workspace, s3_client=s3_client
        )
    return {"bundled": [], "files": 0, "bytes": 0}


def bundle_skills_into(
    code: str,
    dest_parent: Path,
    log: Callable[[str], None],
    workspace: WorkspaceContext,
    *,
    skill_records: dict[str, str] | None = None,
    s3_client: Any = None,
) -> dict[str, Any]:
    """Method-agnostic core of ``bundle_skills``: download every APPROVED skill
    referenced by ``code`` into ``dest_parent/skills/{name}/``. Shared by the
    deploy-time packager (which gates on ``spec.method == "studio"``) and the
    studio local-debug executor (which has no AgentSpec — it bundles skills into
    the run's temp workdir so ``Path(__file__).parent/"skills"`` resolves them).
    Any skill issue logs + skips — never raises."""
    names = extract_skill_names(code)
    if not names:
        return {"bundled": [], "files": 0, "bytes": 0}
    if skill_records is None:
        skill_records = _approved_skill_paths(workspace, log)
    pairs: list[tuple[str, str]] = []
    for name in names:
        path = skill_records.get(name)
        if not path:
            log(f"skill '{name}' not found in registry — skipped")
            continue
        pairs.append((name, path))
    return _download_named_skills(pairs, dest_parent, log, workspace, s3_client=s3_client)


def bundle_skill_paths_into(
    paths: list[str],
    dest_parent: Path,
    log: Callable[[str], None],
    workspace: WorkspaceContext,
    *,
    s3_client: Any = None,
) -> dict[str, Any]:
    """``spec.skills`` consumer: download each explicit
    ``s3://bucket/…/{name}/`` prefix into ``dest_parent/skills/{name}/`` — the
    name is the prefix tail. Used by generated zip and container artifacts; no
    registry lookup, no code parsing, and the same log-and-skip posture as
    ``bundle_skills_into``."""
    pairs: list[tuple[str, str]] = []
    for raw_path in paths:
        path = raw_path.strip()
        if not path:
            continue
        name = path.rstrip("/").rsplit("/", 1)[-1]
        if SKILL_NAME_RE.fullmatch(name) is None:
            log(f"skill path has invalid name '{name}' — skipped")
            continue
        pairs.append((name, path))
    return _download_named_skills(pairs, dest_parent, log, workspace, s3_client=s3_client)


def _download_named_skills(
    pairs: list[tuple[str, str]],
    dest_parent: Path,
    log: Callable[[str], None],
    workspace: WorkspaceContext,
    *,
    s3_client: Any = None,
) -> dict[str, Any]:
    """Download each (name, s3 prefix uri) into dest_parent/skills/{name}/."""
    if not pairs:
        return {"bundled": [], "files": 0, "bytes": 0}
    if s3_client is None:
        s3_client = workspace.client("s3")

    bundled: list[str] = []
    total_files = 0
    total_bytes = 0
    for name, path in pairs:
        bucket, prefix = _parse_s3_uri(path)
        dest = dest_parent / "skills" / name
        try:
            files, size = _download_skill_prefix(s3_client, bucket, prefix, dest, name, log)
        except Exception as exc:  # a bad skill can't sink the whole deploy
            log(f"skill '{name}' download failed ({type(exc).__name__}) — skipped")
            shutil.rmtree(dest, ignore_errors=True)
            continue
        if files == 0:
            shutil.rmtree(dest, ignore_errors=True)
            continue
        bundled.append(name)
        total_files += files
        total_bytes += size

    if bundled:
        log(
            f"skills bundled: {', '.join(bundled)} "
            f"({total_files} files, {total_bytes / 1024:.1f} KB)"
        )
    return {"bundled": bundled, "files": total_files, "bytes": total_bytes}


def _preset_template(agent: Agent) -> str | None:
    """A ``zip_runtime`` system preset's template name (``PRESETS[system_key].template``),
    None for every ordinary agent (plan §C.1 — no AgentSpec field carries it)."""
    if not getattr(agent, "system_key", None):
        return None
    from app.system_agents.presets import get_preset

    preset = get_preset(agent.system_key or "")
    return preset.template if preset else None


def _preset_lifecycle(agent: Agent) -> dict[str, int] | None:
    if not getattr(agent, "system_key", None):
        return None
    from app.system_agents.presets import get_preset

    preset = get_preset(agent.system_key or "")
    return dict(preset.lifecycle) if preset and preset.lifecycle else None


def _generate_code(
    spec: AgentSpec, template: str | None = None, *, principal_carrier: bool = False,
) -> tuple[str, str]:
    """(code, source label) — studio artifacts arrive pre-generated; a system preset's
    ``template`` (the evaluation worker) renders its own entrypoint. ``principal_carrier``
    (stream A3) renders the per-user principal carrier into the shared Strands template only —
    the other sources (worker, converted bundle, studio, A2A) carry their own code."""
    if template == "eval_worker":
        from app.templates.eval_worker import render_main_py as render_worker

        return render_worker(spec), "evaluation worker template"
    if spec.code_bundle:
        # harness conversion: the bundle's main.py is already the entrypoint
        # (config-bundle contract grafted at convert time)
        return spec.code_bundle["main.py"], "harness export bundle"
    if spec.method == "studio" and spec.code:
        from app.templates.studio_agent import adapt_studio_code

        return adapt_studio_code(spec.code, spec), "studio artifact (adapted)"
    if spec.protocol == "a2a":
        from app.templates.strands_a2a_agent import render_a2a_main_py

        return render_a2a_main_py(spec), "strands A2A template"
    return (
        render_main_py(spec, principal_carrier=principal_carrier),
        "strands template"
        + (" + principal carrier" if principal_carrier else ""),
    )


def _principal_carrier(ctx: StageContext, agent: Agent, spec: AgentSpec) -> bool:
    """Stream A3: the job's one carrier decision — the same answer the narrowing used to
    arm or disarm the guard (``policy_enforcement.job_carrier_decision``)."""
    return policy_enforcement.job_carrier_decision(ctx, agent, spec)[0]


def write_bundle_files(spec: AgentSpec, pkg_dir: Path) -> int:
    """Drop a code_bundle's non-entrypoint files into the package dir."""
    written = 0
    for rel, content in (spec.code_bundle or {}).items():
        if rel == "main.py":
            continue  # build_zip already wrote the entrypoint
        dest = pkg_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        written += 1
    return written


STUDIO_EXTRA_REQUIREMENTS = [
    # studio's generator imports the strands_tools catalog
    "strands-agents-tools",
]


def platform_requirements(
    method: str, model_source: str, protocol: str = "http"
) -> list[str]:
    """Every requirement the package stage adds on top of `spec.requirements`.

    The one source of truth for the platform's own half of the install set, shared
    by two consumers that MUST agree:

    * `_method_requirements` below, which feeds the hashed lockfile, and
    * `resolve_pins` (`app/schemas/requirements.py`), which needs these entries in
      its compile input so a pin it produces is one this lockfile can satisfy.

    Resolving a spec requirement without them pins it against a different
    dependency graph than the build uses — see the docstring there for the
    `mcp==2.0.0` failure that motivates the sharing.

    Takes the three fields it reads as scalars rather than an `AgentSpec`, because
    every caller that needs pinning is holding *unpinned* requirements and so
    cannot construct one (`AgentSpec` validates `requirements` are pinned).
    """
    if protocol == "a2a":
        from app.templates.strands_a2a_agent import a2a_base_requirements

        return a2a_base_requirements()
    if method == "studio":
        # The canvas bakes the model into its code and knows which providers its
        # nodes use, so it supplies its own extras via spec.requirements;
        # spec.model_source is inert for studio specs.
        extra = STUDIO_EXTRA_REQUIREMENTS
    else:
        extra = []
    return base_requirements() + extra


def _method_requirements(spec: AgentSpec, template: str | None = None) -> list[str]:
    if template == "eval_worker":
        from app.templates.eval_worker import base_requirements as worker_requirements

        return worker_requirements() + spec.requirements
    return (
        platform_requirements(spec.method, spec.model_source, spec.protocol)
        + spec.requirements
    )


def _stage_generate(ctx: StageContext, agent: Agent) -> StageResult:
    spec = AgentSpec(**agent.spec)
    template = _preset_template(agent)
    # the deploy-time policy context (the principal carrier), computed once per job and
    # logged here; Global parity: nothing is narrowed, no guard is rendered
    policy_enforcement.stage_policy(ctx, agent, spec)
    # rev-r16 on N16-obs72: the merged environment (spec + platform keys) is derived here,
    # before any AWS call, so an over-budget map fails this stage with
    # ``agent.env_budget_exceeded`` instead of the deploy stage's ValidationException
    runtime_environment(
        spec, ctx.workspace.resources, workload_name=agent.resource_id or "",
        agent_id=agent.id,
    )
    code, source = _generate_code(spec, template,
                                  principal_carrier=_principal_carrier(ctx, agent, spec))
    requirements = _method_requirements(spec, template)
    ctx.scratch["code"] = code
    ctx.scratch["requirements"] = requirements
    ctx.log(f"{source} · {len(code)} bytes · model {spec.model_id}")
    return StageResult(detail=f"{source} · {len(code)} bytes")


# N12-bug10: build dirs live INSIDE the checkout (git-ignored data/), owned by whoever runs
# the backend — never a shared /tmp/launchpad_build_<name> that a root-run bootstrap could
# leave root-owned for the service user to trip over (PermissionError on
# deployment_package.zip); the checkout-local dir also survives /tmp cleaning. The root and
# the per-agent path live in `deployer/build_dirs.py` together with their retention
# (N17-obs151: removed on agent delete, swept at startup); `build_dir_for` is re-exported
# here for the Harness method, which assembles its package in the very same place.
build_dir_for = build_dirs.build_dir_for


def _stage_package(ctx: StageContext, agent: Agent) -> StageResult:
    from app.deployer.input_contract import record_input_contract

    # round 16: the creation-time validator must have refused this — kept as the
    # last line of defence for stored specs (tests/fail_early_registry.py)
    bucket = ctx.workspace.resources.get("artifacts_bucket")
    if not bucket:
        raise RuntimeError(
            "artifacts_bucket missing from this workspace's resource map — run its bootstrap"
        )
    spec = AgentSpec(**agent.spec)
    template = _preset_template(agent)
    code = ctx.scratch.get("code") or _generate_code(
        spec, template, principal_carrier=_principal_carrier(ctx, agent, spec),
    )[0]
    requirements = ctx.scratch.get("requirements") or _method_requirements(spec, template)

    build_dir = build_dir_for(agent.name)
    t0 = time.monotonic()
    bundled: dict[str, Any] = {}

    def _on_pkg_ready(pkg_dir: Path) -> None:
        if spec.code_bundle:
            count = write_bundle_files(spec, pkg_dir)
            ctx.log(f"bundle files staged: {count} (+ main.py)")
        if template == "eval_worker":
            from app.templates.eval_worker import write_library

            ctx.log(f"evaluation engine library staged: {write_library(pkg_dir)} files")
        bundled.update(bundle_skills(spec, code, pkg_dir, ctx.log, ctx.workspace))

    zip_path = build_zip(
        code,
        requirements,
        build_dir,
        on_pkg_ready=_on_pkg_ready,
        on_lock_ready=lambda count: ctx.log(
            f"requirements locked · {count} packages pinned with hashes"
        ),
    )
    pip_secs = time.monotonic() - t0
    size_mb = zip_path.stat().st_size / 1e6

    s3_key = f"agents/{agent.name}/deployment_package.zip"
    ctx.workspace.client("s3").upload_file(str(zip_path), bucket, s3_key)
    ctx.scratch["s3_bucket"], ctx.scratch["s3_key"] = bucket, s3_key
    record_input_contract(ctx, code)
    ctx.log(f"pip+zip {pip_secs:.1f}s · {size_mb:.1f}MB → s3://{bucket}/{s3_key}")
    detail = f"pip+zip {pip_secs:.1f}s · {size_mb:.1f}MB · s3 ✓"
    if bundled.get("bundled"):
        detail += f" · skills: {', '.join(bundled['bundled'])}"
    return StageResult(detail=detail)


def _stage_provision(ctx: StageContext, agent: Agent, iam_client: Any = None) -> StageResult:
    spec = AgentSpec(**agent.spec)
    settings = get_settings()
    role_arn, detail = agent_iam.provision_execution_role(
        agent, spec, settings, ctx.workspace, ctx.log, iam=iam_client,
    )
    if agent.system_key:  # a system preset is never deployed on the shared role
        from app.system_agents.service import require_dedicated_role

        require_dedicated_role(agent, role_arn, ctx.workspace, settings)
    ctx.scratch["execution_role_arn"] = role_arn
    return StageResult(detail=detail)


def _needs_workload_pin(client: Any, runtime_id: str, ready: dict[str, Any]) -> bool:
    """Observed state, not a mode heuristic: pin only when the live environment does not
    already name this runtime's workload identity (``ready`` is the READY
    GetAgentRuntime detail; re-read when it carries no environment block)."""
    from app.templates.identity_support import ENV_WORKLOAD

    live = ready.get("environmentVariables")
    if not isinstance(live, dict):
        live = rt.get_runtime(client, runtime_id).get("environmentVariables") or {}
    return live.get(ENV_WORKLOAD) != runtime_id


def _stage_deploy(ctx: StageContext, agent: Agent) -> StageResult:
    resources = ctx.workspace.resources
    if agent.system_key:
        # Defensive: a resumed job may re-enter here with no provision step in this
        # attempt's scratch, so the role would otherwise resolve to the workspace
        # resource map's shared execution_role_arn — the same guard provision runs,
        # re-checked against whatever role this attempt is actually about to send.
        from app.system_agents.service import require_dedicated_role

        role_arn = ctx.scratch.get("execution_role_arn") or resources.get(
            "execution_role_arn", ""
        )
        require_dedicated_role(agent, role_arn or None, ctx.workspace, get_settings())
    client = control_client(ctx.workspace)
    mode = ctx.scratch.get("mode", "create")
    db = ctx.session()
    try:
        row = db.get(Agent, agent.id)
        # Resolve inbound auth ONCE per deploy (spec > workspace default > IAM)
        # and snapshot the choice onto the row below — consumers (Chat, /v1,
        # the console badge) read the snapshot, never re-resolve.
        resolved_auth = inbound_auth_service.resolve_for_agent(row, db)
        authorizer = inbound_auth_service.authorizer_configuration(resolved_auth)
        # the deploy-time policy context (scratch), recomputed on a resumed job
        policy = policy_enforcement.stage_policy(ctx, row, AgentSpec(**row.spec))

        def _kwargs() -> dict:
            spec = AgentSpec(**row.spec)
            environment = runtime_environment(
                spec, resources, workload_name=row.resource_id or "", agent_id=row.id,
            )
            return {
                "s3_bucket": ctx.scratch.get("s3_bucket")
                or resources.get("artifacts_bucket", ""),
                "s3_key": ctx.scratch.get("s3_key")
                or f"agents/{row.name}/deployment_package.zip",
                "role_arn": ctx.scratch.get("execution_role_arn")
                or resources.get("execution_role_arn", ""),
                "environment": environment,
                # A2A runtimes must echo the protocol on update too —
                # UpdateAgentRuntime resets an omitted protocolConfiguration
                "protocol": spec.protocol,
                # Same contract as protocol: echoed on every update. None (IAM)
                # omits the field, which resets a JWT runtime back to SigV4 —
                # that IS the JWT→IAM transition.
                "authorizer_configuration": authorizer,
                # managed session storage — likewise reset when omitted on update
                "filesystem_configurations": filesystem_configurations(spec) or None,
                # Runtime lifecycle (idle timeout / max lifetime) of a system preset —
                # the evaluation worker's 8 h async budget (plan §B.0, R-15). N12-bug7:
                # UpdateAgentRuntime treats an omitted lifecycleConfiguration as "reset to
                # the defaults" (900 s idle), so it is echoed on EVERY call that touches
                # the runtime — create, re-publish and the A2A workload-name pin — not
                # only at create time. None (ordinary agent) omits the field.
                "lifecycle": _preset_lifecycle(row),
            }

        # N10v3-bug5 (F4 pin): LAUNCHPAD_WORKLOAD_NAME = this runtime's own workload
        # identity (its runtime id), which does not exist until CreateAgentRuntime
        # returns. Once the runtime is READY the live environment is read back: when it
        # does not name itself yet, one follow-up UpdateAgentRuntime pins it (an update
        # needs READY first). A re-publish, a resumed or adopted create whose env already
        # carries it → no-op. Live correction (N12-bug8 / r4-F): the pin is informational
        # only — a runtime-linked workload identity CANNOT self-mint a token (Identity
        # refuses GetWorkloadAccessToken: "linked to a service"), so an A2A server gets
        # its key solely from the token the Runtime injects when the CALLER sends
        # runtimeUserId (every Launchpad caller now does); the template tries the
        # self-mint once, then disables it for the process.
        is_a2a = AgentSpec(**row.spec).protocol == "a2a"

        if mode == "update" and row.resource_id:  # re-publish → UpdateAgentRuntime (new version)
            runtime_id = row.resource_id
            updated = agent_iam.retry_iam_propagation(
                lambda: rt.update_code_runtime(client, runtime_id=runtime_id, **_kwargs()),
                ctx.log,
            )
            row.version = str(updated.get("agentRuntimeVersion", row.version or "1"))
            db.commit()
            ctx.log(
                f"UpdateAgentRuntime accepted · runtimeId {runtime_id} · "
                f"new version {row.version} · inbound auth {resolved_auth.mode}"
            )
        elif row.resource_id:
            runtime_id = row.resource_id
            ctx.log(f"resuming — runtime {runtime_id} already created, polling status")
        elif existing := find_adoptable_runtime(
            client, db, row, _kwargs()["role_arn"], ctx.log
        ):
            # N10v2-bug1: the previous process created it but never recorded it
            runtime_id = existing["agentRuntimeId"]
            row.resource_id = runtime_id
            row.arn = existing["agentRuntimeArn"]
            row.version = str(existing.get("agentRuntimeVersion", "1"))
            db.commit()
            ctx.log(
                f"adopting runtime {runtime_id} ({existing['agentRuntimeName']}, "
                f"{existing.get('status')}) created before the restart — "
                "no second CreateAgentRuntime"
            )
        else:
            created = agent_iam.retry_iam_propagation(
                lambda: rt.create_code_runtime(
                    client, runtime_name=sanitize_runtime_name(row.name), **_kwargs()
                ),
                ctx.log,
            )
            runtime_id = created["agentRuntimeId"]
            row.resource_id = runtime_id
            row.arn = created["agentRuntimeArn"]
            row.version = str(created.get("agentRuntimeVersion", "1"))
            db.commit()  # the identity is on the ledger the moment the API accepts
            ctx.log(
                f"CreateAgentRuntime accepted · runtimeId {runtime_id} · {row.arn} · "
                f"inbound auth {resolved_auth.mode}"
            )

        ready = rt.wait_runtime_ready(
            client, runtime_id, on_status=lambda s: ctx.log(f"runtime status: {s}")
        )
        if is_a2a and _needs_workload_pin(client, runtime_id, ready):
            ctx.log(
                f"A2A runtime: pinning LAUNCHPAD_WORKLOAD_NAME={runtime_id} (names the "
                "runtime's own workload identity; the key itself comes from the token "
                "the caller's runtimeUserId makes the Runtime inject) — UpdateAgentRuntime"
            )
            updated = agent_iam.retry_iam_propagation(
                lambda: rt.update_code_runtime(client, runtime_id=runtime_id, **_kwargs()),
                ctx.log,
            )
            row.version = str(updated.get("agentRuntimeVersion", row.version or "1"))
            db.commit()
            ready = rt.wait_runtime_ready(
                client, runtime_id, on_status=lambda s: ctx.log(f"runtime status: {s}")
            )
        row.arn = ready["agentRuntimeArn"]
        # The inbound-auth snapshot lands only once the runtime is READY on the
        # version that carries it: a Create/Update that is accepted and then
        # fails leaves the ledger on the mode the live runtime still serves.
        # A resumed job (create already accepted, or an update re-issued)
        # reaches this line too, so the snapshot is never skipped.
        inbound_auth_service.record_deployed_auth(row, resolved_auth)
        row.version = str(ready.get("agentRuntimeVersion", row.version or "1"))
        from app.deployer.input_contract import stamp_input_contract

        stamp_input_contract(ctx, db, row)
        db.commit()
        # the live version now renders this policy context — clear the stale badge,
        # snapshot it on the row and journal `bundle.apply`
        carrier, decides = policy_enforcement.job_carrier_decision(ctx, row, AgentSpec(**row.spec))
        policy_enforcement.mark_applied(db, row, policy, job_id=ctx.job_id, carrier=carrier,
                                        interceptor=decides)
        # N11-obs56: bounded retention on the runtime's log group (fail-soft, idempotent)
        ctx.log(log_retention.apply_runtime_log_retention(ctx, runtime_id))
        # as_user return-URL allow-list on the runtime's auto-created workload
        # identity, reconciled on every deploy (create and redeploy)
        detail = f"READY · {ready['agentRuntimeArn']}"
        if not register_return_url_stage(client, AgentSpec(**row.spec), runtime_id, ctx.log):
            detail += f" · {RETURN_URL_WARNING}"
        return StageResult(detail=detail)
    finally:
        db.close()


def _stage_register(ctx: StageContext, agent: Agent) -> StageResult:
    from app.deployer.registration import register_stage

    if _preset_template(agent):
        # a system worker is not a discoverable agent: no A2A record, no Skill record
        return StageResult(skipped=True, detail="system worker · registry skipped")
    return register_stage(ctx, agent)


STAGES = {
    "generate": _stage_generate,
    "package": _stage_package,
    "provision": _stage_provision,
    "deploy": _stage_deploy,
    "register": _stage_register,
}

# N12-obs33/34: the Strands template applies routed config bundles (its `_config_bundle()`
# reader) and is rebuilt from the spec on every deploy; Studio canvas output is member code
# the platform wraps but never rewrites (its bundle shim is opt-in), so a studio agent can
# be canaried (candidate = re-adapted artifact) but not bundle-experimented.
register_method(
    "zip_runtime", STAGES,
    MethodCapabilities(
        config_bundle=True, runtime_versions=True, candidate_mint=True,
        # creation-time domain (round 16): the Strands template renders the shared Gateway
        # client, rest tools + authenticated rest / mcp through the identity client
        # (identity_support) and ask_<peer> A2A tools; it has NO builtin channel and no
        # unauthenticated remote-MCP client (N16-obs2 / N16-obs3 — both were accepted and
        # silently dropped before this declaration refused them)
        builtin_tools=frozenset(),
        tool_types=frozenset({"gateway", "rest", "a2a"}),
        tool_auth_modes=frozenset({"as_agent", "as_user"}),
        native_tools=frozenset(),
        protocols=frozenset({"http", "a2a"}),
        memory=True, knowledge_bases=True,
        requires=frozenset({"artifacts_bucket"}),
    ),
)
register_method(  # studio agents ride the same zip fast path
    "studio", STAGES,
    MethodCapabilities(
        user_source=True, runtime_versions=True, candidate_mint=True,
        # the canvas generates its own code (adapt_studio_code grafts only the bundle shim
        # and the model prelude): a gateway ToolRef is honoured as IAM grant + env for that
        # code; nothing else is rendered into it
        builtin_tools=frozenset(),
        tool_types=frozenset({"gateway"}),
        tool_auth_modes=frozenset(),
        native_tools=frozenset(),
        protocols=frozenset({"http"}),
        memory=True, knowledge_bases=False,
        requires=frozenset({"artifacts_bucket"}),
    ),
)


def delete_agent_resources(agent: Agent, workspace: WorkspaceContext) -> str | None:
    """``rt.RUNTIME_DELETED`` / ``rt.RUNTIME_ABSENT`` (N16-obs87: read back first, an
    AccessDenied on an already-gone id is not a refusal); ``None`` without a runtime id."""
    if not agent.resource_id:
        return None
    return rt.delete_runtime_if_present(control_client(workspace), agent.resource_id)
