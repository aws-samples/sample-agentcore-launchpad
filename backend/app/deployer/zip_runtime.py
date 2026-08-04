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

import boto3

from app.core.config import get_settings
from app.deployer.environment import runtime_environment
from app.deployer.pipeline import StageContext, StageResult, register_method
from app.models.ledger import Agent
from app.schemas.agent import AgentSpec
from app.services import agent_iam
from app.services.agentcore import runtime as rt
from app.services.agentcore.client import control_client
from app.services.skill_ingest import SKILL_BUNDLE_MAX_BYTES, SKILL_NAME_RE
from app.templates.strands_agent import base_requirements, render_main_py


def sanitize_runtime_name(name: str) -> str:
    """Runtime names must be alphanumeric/underscore; suffix keeps them unique."""
    base = re.sub(r"[^A-Za-z0-9_]", "_", name).strip("_")[:40] or "agent"
    return f"{base}_{uuid.uuid4().hex[:6]}"


# The deploy target: AgentCore Runtime zips run ARM64 on Python 3.13. Named once
# because the resolve and the install must agree — resolving for this host and
# installing for aarch64 would produce a lock that does not match the artifact.
TARGET_PYTHON = "3.13"
TARGET_PIP_PLATFORM = "manylinux2014_aarch64"
TARGET_UV_PLATFORM = "aarch64-manylinux2014"

LOCK_FILENAME = "requirements.lock"


def _compile_lock(
    requirements: list[str],
    build_dir: Path,
    compile_runner: Callable[..., Any],
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
            "--python-version", TARGET_PYTHON,
            "--python-platform", TARGET_UV_PLATFORM,
            # The install below is binary-only. Resolve from that same artifact
            # set, or uv can lock an sdist-only release that pip then cannot
            # install for the Runtime's ARM64 manylinux2014 target.
            "--only-binary=:all:",
            "-o", str(lock),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-2000:]
        raise RuntimeError(
            f"could not resolve {requirements} into a hashed lockfile: {detail} "
            "(the backend needs the `uv` CLI on PATH and access to the package index)"
        )
    return lock


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
            "--platform", TARGET_PIP_PLATFORM,
            "--only-binary=:all:",
            "--python-version", TARGET_PYTHON,
            "--quiet",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()[-2000:]
        raise RuntimeError(f"pip install failed for {requirements}: {stderr}")

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


def _approved_skill_paths(log: Callable[[str], None]) -> dict[str, str]:
    """{skill name → s3 prefix uri} for every APPROVED AGENT_SKILLS record."""
    try:
        from app.services.registry_console import attachable_records

        return {s["name"]: s["path"] for s in attachable_records().get("skills", [])}
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
            code, pkg_dir, log, skill_records=skill_records, s3_client=s3_client
        )
    if spec.method == "zip_runtime" and spec.code_bundle is None:
        return bundle_skill_paths_into(spec.skills, pkg_dir, log, s3_client=s3_client)
    return {"bundled": [], "files": 0, "bytes": 0}


def bundle_skills_into(
    code: str,
    dest_parent: Path,
    log: Callable[[str], None],
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
        skill_records = _approved_skill_paths(log)
    pairs: list[tuple[str, str]] = []
    for name in names:
        path = skill_records.get(name)
        if not path:
            log(f"skill '{name}' not found in registry — skipped")
            continue
        pairs.append((name, path))
    return _download_named_skills(pairs, dest_parent, log, s3_client=s3_client)


def bundle_skill_paths_into(
    paths: list[str],
    dest_parent: Path,
    log: Callable[[str], None],
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
    return _download_named_skills(pairs, dest_parent, log, s3_client=s3_client)


def _download_named_skills(
    pairs: list[tuple[str, str]],
    dest_parent: Path,
    log: Callable[[str], None],
    *,
    s3_client: Any = None,
) -> dict[str, Any]:
    """Download each (name, s3 prefix uri) into dest_parent/skills/{name}/."""
    if not pairs:
        return {"bundled": [], "files": 0, "bytes": 0}
    if s3_client is None:
        s3_client = boto3.client("s3", region_name=get_settings().region)

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


def _generate_code(spec: AgentSpec) -> tuple[str, str]:
    """(code, source label) — studio artifacts arrive pre-generated."""
    if spec.code_bundle:
        # harness conversion: the bundle's main.py is already the entrypoint
        # (config-bundle contract grafted at convert time)
        return spec.code_bundle["main.py"], "harness export bundle"
    if spec.method == "studio" and spec.code:
        from app.templates.studio_agent import adapt_studio_code

        return adapt_studio_code(spec.code), "studio artifact (adapted)"
    if spec.protocol == "a2a":
        from app.templates.strands_a2a_agent import render_a2a_main_py

        return render_a2a_main_py(spec), "strands A2A template"
    return render_main_py(spec), "strands template"


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
    # studio's generator imports the strands_tools catalog (incl. mem0_memory)
    "strands-agents-tools[mem0_memory]",
]

# Carries `openai` + `aws-bedrock-token-generator`, which the template's
# OpenAIResponsesModel(bedrock_mantle_config=...) branch needs. Emitted as its
# own line next to the base strands-agents[otel] pin — pip intersects both specs
# for the same project, and this is the shape the canvas publish path produces.
#
# The floor is NOT the base pin's >=1.0: the `openai.gpt-5.*` → /openai/v1
# base-path split that the default Mantle model needs landed in 1.46, and
# `bedrock_mantle_config` is a keyword argument, so an older resolution would
# fail at first invoke instead of at package time. 1.47 is the verified SDK.
#
# `openai` is named again on purpose. The extra only asks for >=1.68, but
# strands' openai_responses module imports `openai` 2.x APIs at module scope, so
# a resolver that settles on 1.x would package cleanly and ImportError at
# container start. Today pip picks 2.x anyway; this makes that deterministic
# rather than a property of when the zip happened to be built.
MANTLE_EXTRA_REQUIREMENTS = ["strands-agents[openai]>=1.47,<2", "openai>=2,<3"]


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
    elif model_source == "mantle":
        extra = MANTLE_EXTRA_REQUIREMENTS
    else:
        extra = []
    return base_requirements() + extra


def _method_requirements(spec: AgentSpec) -> list[str]:
    return (
        platform_requirements(spec.method, spec.model_source, spec.protocol)
        + spec.requirements
    )


def _stage_generate(ctx: StageContext, agent: Agent) -> StageResult:
    spec = AgentSpec(**agent.spec)
    code, source = _generate_code(spec)
    requirements = _method_requirements(spec)
    ctx.scratch["code"] = code
    ctx.scratch["requirements"] = requirements
    ctx.log(f"{source} · {len(code)} bytes · model {spec.model_id}")
    return StageResult(detail=f"{source} · {len(code)} bytes")


def _stage_package(ctx: StageContext, agent: Agent) -> StageResult:
    settings = get_settings()
    bucket = settings.resources.get("artifacts_bucket")
    if not bucket:
        raise RuntimeError("artifacts_bucket missing from config — run scripts/bootstrap.py")
    spec = AgentSpec(**agent.spec)
    code = ctx.scratch.get("code") or _generate_code(spec)[0]
    requirements = ctx.scratch.get("requirements") or _method_requirements(spec)

    build_dir = Path(f"/tmp/launchpad_build_{agent.name}")
    t0 = time.monotonic()
    bundled: dict[str, Any] = {}

    def _on_pkg_ready(pkg_dir: Path) -> None:
        if spec.code_bundle:
            count = write_bundle_files(spec, pkg_dir)
            ctx.log(f"bundle files staged: {count} (+ main.py)")
        bundled.update(bundle_skills(spec, code, pkg_dir, ctx.log))

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
    boto3.client("s3", region_name=settings.region).upload_file(str(zip_path), bucket, s3_key)
    ctx.scratch["s3_bucket"], ctx.scratch["s3_key"] = bucket, s3_key
    ctx.log(f"pip+zip {pip_secs:.1f}s · {size_mb:.1f}MB → s3://{bucket}/{s3_key}")
    detail = f"pip+zip {pip_secs:.1f}s · {size_mb:.1f}MB · s3 ✓"
    if bundled.get("bundled"):
        detail += f" · skills: {', '.join(bundled['bundled'])}"
    return StageResult(detail=detail)


def _stage_provision(ctx: StageContext, agent: Agent, iam_client: Any = None) -> StageResult:
    spec = AgentSpec(**agent.spec)
    role_arn, detail = agent_iam.provision_execution_role(
        agent, spec, get_settings(), ctx.log, iam=iam_client
    )
    ctx.scratch["execution_role_arn"] = role_arn
    return StageResult(detail=detail)


def _stage_deploy(ctx: StageContext, agent: Agent) -> StageResult:
    settings = get_settings()
    client = control_client()
    mode = ctx.scratch.get("mode", "create")
    db = ctx.session()
    try:
        row = db.get(Agent, agent.id)

        def _kwargs() -> dict:
            spec = AgentSpec(**row.spec)
            environment = runtime_environment(spec, settings.resources)
            return {
                "s3_bucket": ctx.scratch.get("s3_bucket")
                or settings.resources.get("artifacts_bucket", ""),
                "s3_key": ctx.scratch.get("s3_key")
                or f"agents/{row.name}/deployment_package.zip",
                "role_arn": ctx.scratch.get("execution_role_arn")
                or settings.resources.get("execution_role_arn", ""),
                "environment": environment,
                # A2A runtimes must echo the protocol on update too —
                # UpdateAgentRuntime resets an omitted protocolConfiguration
                "protocol": spec.protocol,
            }

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
                f"new version {row.version}"
            )
        elif row.resource_id:
            runtime_id = row.resource_id
            ctx.log(f"resuming — runtime {runtime_id} already created, polling status")
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
            db.commit()
            ctx.log(f"CreateAgentRuntime accepted · runtimeId {runtime_id}")

        ready = rt.wait_runtime_ready(
            client, runtime_id, on_status=lambda s: ctx.log(f"runtime status: {s}")
        )
        row.arn = ready["agentRuntimeArn"]
        row.version = str(ready.get("agentRuntimeVersion", row.version or "1"))
        db.commit()
        return StageResult(detail=f"READY · {ready['agentRuntimeArn']}")
    finally:
        db.close()


def _stage_register(ctx: StageContext, agent: Agent) -> StageResult:
    from app.deployer.registration import register_stage

    return register_stage(ctx, agent)


STAGES = {
    "generate": _stage_generate,
    "package": _stage_package,
    "provision": _stage_provision,
    "deploy": _stage_deploy,
    "register": _stage_register,
}

register_method("zip_runtime", STAGES)
register_method("studio", STAGES)  # studio agents ride the same zip fast path


def delete_agent_resources(agent: Agent) -> None:
    if not agent.resource_id:
        return
    client = control_client()
    try:
        rt.delete_runtime(client, agent.resource_id)
    except client.exceptions.ResourceNotFoundException:
        pass
