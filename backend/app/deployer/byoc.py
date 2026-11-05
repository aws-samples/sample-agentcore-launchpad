"""Bring Your Own Code (byoc) — deploy user-written agent code to Runtime.

    generate  → load the staged upload's manifest (or describe the ECR image)
                and stamp server-verified provenance onto the spec
    package   → code_zip: download → safe-extract → verify entrypoint →
                resolve requirements.txt for linux/aarch64 → zip → S3
                container_source: download → verify Dockerfile → CodeBuild → ECR
                container_image: verify the image exists in this account+region
    provision → per-agent least-privilege IAM execution role
    deploy    → CreateAgentRuntime (codeConfiguration or containerConfiguration)
                + poll READY; re-publish → UpdateAgentRuntime (new version)
    register  → the shared A2A registry record stage

An MCP server whose spec carries ``mcp.stdio_wrapper`` is a code_zip of a
stdio-only server: package adds the platform bridge
(``samples/mcp_stdio_bridge/``), which serves streamable HTTP on :8000/mcp and
runs ``command args…`` as its subprocess. ``base="python"`` stays a code
runtime with the bridge as its entrypoint; ``base="node"`` builds the zip into
an image on the bridge's Node base (CodeBuild → ECR) and deploys a container.

The platform NEVER executes the uploaded code on the Launchpad host: package
work is archive extraction, file checks and a pip *download/install into the
bundle directory* (wheels are unpacked, never imported or run).
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.core.config import REPO_ROOT, get_settings
from app.core.errors import AppError, NotFoundError, cleanup_error, is_not_found
from app.core.runtime_target import pip_platform_args
from app.deployer import model_profile
from app.deployer.environment import runtime_environment
from app.deployer.filesystem import filesystem_configurations, runtime_update_mounts
from app.deployer.input_contract import record_input_contract, stamp_input_contract
from app.deployer.mounts import describe as describe_mounts
from app.deployer.mounts import published_mount_paths, with_mounts_env
from app.deployer.network import describe as describe_network
from app.deployer.network import spec_vpc
from app.deployer.pipeline import StageContext, StageResult, register_method
from app.deployer.return_url import RETURN_URL_WARNING, register_return_url_stage
from app.models.ledger import Agent
from app.schemas.agent import AgentSpec, ByocConfig, McpStdioWrapper, parse_ecr_image_uri
from app.services import agent_iam, byoc_uploads, cost_tags, mcp_servers, project_storage
from app.services import inbound_auth as inbound_auth_service
from app.services.agentcore import runtime as rt
from app.services.agentcore.client import control_client
from app.services.requirements_txt import (
    RESOLVE_FIX_HINTS,
    RequirementsFileError,
    parse_requirements_txt,
    pip_python_version,
    summarize_resolver_failure,
)
from app.services.workspace import WorkspaceContext

from .container import (
    _image_ref,
    _recorded_digest_uri,
    build_and_push_image,
    platform_buildspec_path,
)
from .zip_runtime import _compile_lock, sanitize_runtime_name

logger = logging.getLogger(__name__)

PACKAGE_KEY_TMPL = "agents/{name}/byoc_package.zip"


def package_key(agent: Agent) -> str:
    """The code_zip package's S3 key, under `projects/<pid>/` for an agent of an
    isolated project (projects design §10.10)."""
    return project_storage.artifact_key(
        getattr(agent, "project_id", None), PACKAGE_KEY_TMPL.format(name=agent.name)
    )

# the stdio → streamable-HTTP bridge (§15.5); copied into wrapped bundles
STDIO_BRIDGE_DIR = REPO_ROOT / "samples" / "mcp_stdio_bridge"
STDIO_BRIDGE_FILE = "mcp_stdio_bridge.py"
# the bridge reads its subprocess argv from this env var (a JSON array)
STDIO_COMMAND_ENV = "MCP_STDIO_COMMAND"
# a first argument with one of these suffixes names a script that must ship in the zip
_STDIO_SCRIPT_SUFFIXES = (".py", ".js", ".mjs", ".cjs")


def _config(spec: AgentSpec) -> ByocConfig:
    if spec.byoc is None:  # schema guarantees this; belt for hand-built rows
        raise RuntimeError("byoc spec has no byoc settings block")
    return spec.byoc


def stdio_wrapper(spec: AgentSpec) -> McpStdioWrapper | None:
    """The stdio bridge settings of an MCP server spec, or None."""
    if spec.protocol != "mcp" or spec.mcp is None:
        return None
    return spec.mcp.stdio_wrapper


def runs_on_code_runtime(spec: AgentSpec) -> bool:
    """codeConfiguration (managed Python runtime) vs containerConfiguration:
    every code_zip except a stdio server whose base needs an image."""
    cfg = _config(spec)
    if cfg.artifact_kind != "code_zip":
        return False
    wrapper = stdio_wrapper(spec)
    return wrapper is None or not wrapper.builds_image


def _check_stdio_wrapper(spec: AgentSpec) -> McpStdioWrapper | None:
    wrapper = stdio_wrapper(spec)
    if wrapper is not None and _config(spec).artifact_kind != "code_zip":
        raise RuntimeError(
            "mcp.stdio_wrapper applies to artifact_kind=code_zip only — a Dockerfile "
            "context or image can run the bridge itself (samples/mcp_stdio_bridge/)"
        )
    return wrapper


# PYTHON_3_13 → 3.13 (the shape pip/uv take); shared with the upload pre-resolve
_pip_python_version = pip_python_version


def _requirements_lines(path: Path) -> list[str]:
    """The zip's requirements entries, per the pip file format (continuations,
    comments, markers) and the platform's supply-chain boundary (no includes,
    no URLs/VCS/paths, no index options — see `services/requirements_txt`).
    `--hash` options are dropped: the platform re-locks against its own deploy
    target and generates fresh hashes."""
    try:
        return parse_requirements_txt(path.read_text(encoding="utf-8"))
    except RequirementsFileError as exc:
        raise RuntimeError(f"the zip's requirements.txt was refused — {exc}") from exc


def _stamp_provenance(ctx: StageContext, agent: Agent, provenance: dict[str, Any]) -> None:
    """Persist server-verified provenance into the row's spec (idempotent).

    Overwrites whatever the client sent — provenance is server-owned; the spec
    field only exists so the console can render it back."""
    db = ctx.session()
    try:
        row = db.get(Agent, agent.id)
        spec = dict(row.spec)
        spec["byoc"] = {**(spec.get("byoc") or {}), "provenance": provenance}
        row.spec = spec
        db.commit()
        agent.spec = spec
    finally:
        db.close()


def _stamp_spec(ctx: StageContext, agent: Agent, update: Callable[[dict], None]) -> None:
    """Apply a server-owned edit to the row's spec (and the in-memory agent)."""
    db = ctx.session()
    try:
        row = db.get(Agent, agent.id)
        spec = dict(row.spec)
        update(spec)
        row.spec = spec
        db.commit()
        agent.spec = spec
    finally:
        db.close()


def _stamp_mcp_inbound(ctx: StageContext, agent: Agent) -> None:
    """MCP server: its own Cognito client, and the JWT authorizer allowing only it.

    Stamped before provision and deploy so both read a JWT-mode spec — the role
    gets the inbound workload-token grant, the runtime the authorizer."""
    client_id = mcp_servers.ensure_inbound_client(ctx.workspace, agent, ctx.log)

    def update(spec: dict) -> None:
        spec["mcp"] = {**(spec.get("mcp") or {}), "client_id": client_id}
        spec["inbound_auth"] = mcp_servers.inbound_auth(ctx.workspace, client_id)

    _stamp_spec(ctx, agent, update)
    ctx.log(f"MCP inbound · JWT (workspace pool) · allowed client {client_id}")


def _stage_generate(ctx: StageContext, agent: Agent) -> StageResult:
    """No code to generate — verify the artifact reference and stamp provenance."""
    spec = AgentSpec(**agent.spec)
    cfg = _config(spec)
    wrapper = _check_stdio_wrapper(spec)

    if cfg.artifact_kind == "container_image":
        digest, pushed_at = describe_image(ctx.workspace, cfg.image_uri or "")
        provenance = {
            "sha256": digest,
            "size_bytes": 0,
            "original_filename": cfg.image_uri or "",
            "uploaded_by": "",
            "uploaded_at": pushed_at,
        }
        _stamp_provenance(ctx, agent, provenance)
        if spec.protocol == "mcp":
            _stamp_mcp_inbound(ctx, agent)
        ctx.log(f"ECR image verified · {cfg.image_uri} · {digest}")
        return StageResult(detail=f"container_image · {digest[:19]}…")

    manifest = byoc_uploads.get_manifest(ctx.workspace, cfg.upload_id or "")
    staged_in = manifest.get("project_id")
    if staged_in and agent.project_id and staged_in != agent.project_id:
        # checked here, not at create: the request path would pay an S3 read per create
        raise NotFoundError("byoc.upload_not_found", "upload not found")
    provenance = {
        "sha256": manifest.get("sha256", ""),
        "size_bytes": manifest.get("size_bytes", 0),
        "original_filename": manifest.get("original_filename", ""),
        "uploaded_by": manifest.get("uploaded_by", ""),
        "uploaded_at": manifest.get("uploaded_at", ""),
    }
    detected = manifest.get("detected") or {}
    if cfg.artifact_kind == "code_zip":
        # server-owned like the rest of provenance; re-judged from the current
        # upload on every publish, and persisted so a resumed deploy reads it
        provenance["adot_launcher"] = byoc_uploads.adot_launcher(
            detected, cfg.install_requirements
        )
    _stamp_provenance(ctx, agent, provenance)
    if spec.protocol == "mcp":
        _stamp_mcp_inbound(ctx, agent)
    if wrapper is not None:
        ctx.log(
            f"stdio MCP server · bridge on :8000/mcp → {' '.join(wrapper.argv())} "
            f"({wrapper.base} base)"
        )
    if (
        spec.protocol != "mcp"
        and cfg.artifact_kind == "code_zip"
        and not detected.get("agentcore_sdk_detected")
    ):
        ctx.log(
            "note: no BedrockAgentCoreApp/@app.entrypoint marker found — the "
            "entrypoint must serve POST /invocations + GET /ping on :8080 itself"
        )
    if detected.get("adot_detected") and not provenance.get("adot_launcher"):
        ctx.log(
            "note: aws-opentelemetry-distro is only listed in requirements.txt and "
            "install_requirements=false — no ADOT launcher, no spans"
        )
    ctx.log(
        f"{cfg.artifact_kind} · {provenance['original_filename']} · "
        f"{provenance['size_bytes'] / 1e6:.1f}MB · sha256 {provenance['sha256'][:12]} · "
        f"uploaded by {provenance['uploaded_by'] or 'unknown'}"
    )
    return StageResult(
        detail=f"{cfg.artifact_kind} · sha256 {provenance['sha256'][:12]}…"
    )


def describe_image(
    workspace: WorkspaceContext, image_uri: str, ecr_client: Any = None
) -> tuple[str, str]:
    """(imageDigest, imagePushedAt ISO) of a private ECR image in THIS
    account+region — refuses other accounts/regions and missing images."""
    parsed = parse_ecr_image_uri(image_uri)
    if parsed is None:
        raise RuntimeError(f"'{image_uri}' is not a private ECR image URI")
    account_id, region = parsed
    if account_id != workspace.account_id or region != workspace.region:
        raise RuntimeError(
            f"image {image_uri} lives in {account_id}/{region}; this workspace "
            f"deploys from {workspace.account_id}/{workspace.region} only"
        )
    rest = image_uri.split(".amazonaws.com/", 1)[1]
    if "@sha256:" in rest:
        repo, _, ref = rest.partition("@")
        image_id = {"imageDigest": ref}
    else:
        repo, _, ref = rest.rpartition(":")
        image_id = {"imageTag": ref}
    ecr = ecr_client or workspace.client("ecr")
    detail = ecr.describe_images(repositoryName=repo, imageIds=[image_id])
    images = detail.get("imageDetails", [])
    if not images:
        raise RuntimeError(f"image {image_uri} not found in ECR")
    pushed = images[0].get("imagePushedAt")
    pushed_at = pushed.isoformat() if hasattr(pushed, "isoformat") else str(pushed or "")
    return images[0].get("imageDigest", ""), pushed_at


def resolve_requirements_into(
    src_root: Path,
    build_dir: Path,
    python_version: str,
    log: Any,
    pip_runner: Any = subprocess.run,
    compile_runner: Any = None,
) -> int:
    """Hash-locked install of the zip's requirements.txt into the bundle root
    for the Runtime target (linux/aarch64). Returns the locked package count;
    0 when there is nothing to resolve. Wheels are unpacked, never executed."""
    req_file = src_root / "requirements.txt"
    if not req_file.exists():
        return 0
    requirements = _requirements_lines(req_file)
    if not requirements:
        return 0
    pip_version = _pip_python_version(python_version)
    lock = _compile_lock(
        requirements, build_dir, compile_runner or pip_runner, python_version=pip_version
    )
    locked = [
        line for line in lock.read_text(encoding="utf-8").splitlines()
        if "==" in line and not line.lstrip().startswith("#")
    ]
    log(f"requirements locked · {len(locked)} packages pinned with hashes")
    proc = pip_runner(
        [
            sys.executable, "-m", "pip", "install",
            "--require-hashes", "-r", str(lock),
            "-t", str(src_root),
            *pip_platform_args(),
            "--only-binary=:all:",
            "--python-version", pip_version,
            "--quiet",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "pip install of the zip's locked requirements failed: "
            + summarize_resolver_failure(
                proc.stderr or "", python_version=pip_version, hints=RESOLVE_FIX_HINTS
            )
        )
    # the lock ships inside the artifact — the record of what was installed
    shutil.copy2(lock, src_root / "requirements.lock")
    return len(locked)


def _zip_tree(src_root: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(src_root):
            if "__pycache__" in root:
                continue
            for name in files:
                if name.endswith(".pyc"):
                    continue
                full = Path(root) / name
                zf.write(full, full.relative_to(src_root))


def _package_code_zip(
    ctx: StageContext,
    agent: Agent,
    cfg: ByocConfig,
    bucket: str,
    wrapper: McpStdioWrapper | None = None,
) -> StageResult:
    # Agent names are only unique within a workspace. Each attempt owns its
    # directory, including while another job is resolving dependencies.
    with tempfile.TemporaryDirectory(prefix="launchpad_byoc_") as tmp:
        if wrapper is not None and wrapper.builds_image:
            return _build_stdio_image(ctx, agent, cfg, wrapper, Path(tmp))
        return _build_code_zip(ctx, agent, cfg, bucket, Path(tmp), wrapper)


def _check_stdio_script(src_root: Path, wrapper: McpStdioWrapper) -> None:
    """``python server.py`` / ``node build/index.js``: the script must be in the
    zip — caught here instead of as a crash loop behind the bridge."""
    script = wrapper.args[0] if wrapper.args else ""
    if not script.endswith(_STDIO_SCRIPT_SUFFIXES):
        return  # `-m pkg`, a binary on PATH, an npx package… — nothing to check
    if script.startswith("/") or ".." in Path(script).parts:
        raise RuntimeError(
            f"stdio_wrapper script '{script}' must be a relative path inside the zip"
        )
    if not (src_root / script).is_file():
        raise RuntimeError(
            f"stdio_wrapper script '{script}' not found in the uploaded zip"
        )


def _add_stdio_bridge(ctx: StageContext, src_root: Path) -> None:
    """Copy the platform bridge into the bundle root (it owns that file name)."""
    target = src_root / STDIO_BRIDGE_FILE
    if target.exists():
        ctx.log(f"upload carries its own {STDIO_BRIDGE_FILE} — replaced by the platform's")
    shutil.copy2(STDIO_BRIDGE_DIR / STDIO_BRIDGE_FILE, target)


def _build_code_zip(
    ctx: StageContext,
    agent: Agent,
    cfg: ByocConfig,
    bucket: str,
    build_dir: Path,
    wrapper: McpStdioWrapper | None = None,
) -> StageResult:
    zip_path = build_dir / "upload.zip"
    byoc_uploads.download_upload(ctx.workspace, agent.workspace_id, cfg.upload_id or "",
                                 zip_path)
    src_root = byoc_uploads.extract_zip(zip_path, build_dir / "src")

    if wrapper is not None:
        # the bridge is the entrypoint; the member's entrypoint field is unused
        _check_stdio_script(src_root, wrapper)
    else:
        entry = src_root / cfg.entrypoint
        if not entry.is_file():
            raise RuntimeError(
                f"entrypoint '{cfg.entrypoint}' not found in the uploaded zip — "
                "pick one of the detected candidates or re-upload"
            )

    t0 = time.monotonic()
    installed = 0
    if cfg.install_requirements:
        installed = resolve_requirements_into(
            src_root, build_dir, cfg.python_version, ctx.log
        )
        if installed == 0:
            ctx.log("no requirements.txt in the zip — bundle shipped as uploaded")
    else:
        ctx.log("install_requirements=false — bundle shipped as uploaded")
    if wrapper is not None:
        _add_stdio_bridge(ctx, src_root)
        ctx.log(f"stdio bridge added · entrypoint {STDIO_BRIDGE_FILE}")

    final_zip = build_dir / "deployment_package.zip"
    _zip_tree(src_root, final_zip)
    size_mb = final_zip.stat().st_size / 1e6
    secs = time.monotonic() - t0

    s3_key = package_key(agent)
    ctx.workspace.client("s3").upload_file(str(final_zip), bucket, s3_key)
    ctx.scratch["s3_bucket"], ctx.scratch["s3_key"] = bucket, s3_key
    # the entrypoint opts into native attachments the same way generated code
    # does; the stdio bridge never does (an MCP server takes no chat files)
    record_input_contract(
        ctx, "" if wrapper is not None else entry.read_text(encoding="utf-8", errors="replace")
    )
    ctx.log(f"package {secs:.1f}s · {size_mb:.1f}MB → s3://{bucket}/{s3_key}")
    detail = f"code_zip · {size_mb:.1f}MB · s3 ✓"
    if installed:
        detail += f" · {installed} deps resolved (aarch64)"
    if wrapper is not None:
        detail += " · stdio bridge"
    return StageResult(detail=detail)


def _build_stdio_image(
    ctx: StageContext,
    agent: Agent,
    cfg: ByocConfig,
    wrapper: McpStdioWrapper,
    build_dir: Path,
) -> StageResult:
    """A stdio server whose base needs an image: the zip + the bridge + the
    bridge's base Dockerfile + the platform buildspec → CodeBuild → ECR."""
    zip_path = build_dir / "upload.zip"
    byoc_uploads.download_upload(ctx.workspace, agent.workspace_id, cfg.upload_id or "",
                                 zip_path)
    src_root = byoc_uploads.extract_zip(zip_path, build_dir / "src")
    _check_stdio_script(src_root, wrapper)
    _add_stdio_bridge(ctx, src_root)
    # the platform owns the recipe: an uploaded Dockerfile / buildspec is replaced
    for name, source in (
        ("Dockerfile", STDIO_BRIDGE_DIR / f"Dockerfile.{wrapper.base}"),
        ("buildspec.yml", platform_buildspec_path()),
    ):
        if (src_root / name).exists():
            ctx.log(f"upload carries its own {name} — replaced by the platform's")
        shutil.copy2(source, src_root / name)
    archive = shutil.make_archive(str(build_dir / "context_src"), "zip", src_root)
    tag, mins = build_and_push_image(ctx, agent, archive)
    digest = ctx.scratch["image_digest"]
    return StageResult(
        detail=f"stdio bridge · {wrapper.base} base · codebuild · arm64 · {mins:.1f}m "
        f"→ :{tag} @ {digest[:19]}…"
    )


def _package_container_source(
    ctx: StageContext, agent: Agent, cfg: ByocConfig
) -> StageResult:
    with tempfile.TemporaryDirectory(prefix="launchpad_byoc_") as tmp:
        return _build_container_source(ctx, agent, cfg, Path(tmp))


def _build_container_source(
    ctx: StageContext, agent: Agent, cfg: ByocConfig, build_dir: Path
) -> StageResult:
    zip_path = build_dir / "upload.zip"
    byoc_uploads.download_upload(ctx.workspace, agent.workspace_id, cfg.upload_id or "",
                                 zip_path)
    # extract_zip normalizes a single top-level dir, so a `zip -r ctx.zip myagent/`
    # upload still presents its Dockerfile at the build-context root
    src_root = byoc_uploads.extract_zip(zip_path, build_dir / "src")
    if not (src_root / "Dockerfile").is_file():
        raise RuntimeError(
            "no Dockerfile at the zip root — a container_source upload must "
            "carry the docker build context (Dockerfile + code)"
        )
    # CodeBuild reads buildspec.yml from the source zip; the platform owns the
    # build recipe, so this overwrites any buildspec the member uploaded —
    # their Dockerfile is the only build input they control.
    if (src_root / "buildspec.yml").exists():
        ctx.log("upload carries its own buildspec.yml — replaced by the platform's")
    shutil.copy2(platform_buildspec_path(), src_root / "buildspec.yml")
    archive = shutil.make_archive(str(build_dir / "context_src"), "zip", src_root)
    tag, mins = build_and_push_image(ctx, agent, archive)
    digest = ctx.scratch["image_digest"]
    return StageResult(detail=f"codebuild · arm64 · {mins:.1f}m → :{tag} @ {digest[:19]}…")


def _stage_package(ctx: StageContext, agent: Agent) -> StageResult:
    spec = AgentSpec(**agent.spec)
    cfg = _config(spec)
    wrapper = _check_stdio_wrapper(spec)
    if cfg.artifact_kind == "container_image":
        # nothing to build — deploy pins the URI the generate stage verified
        ctx.scratch["image_uri"] = cfg.image_uri
        return StageResult(skipped=True, detail="existing image — no build")
    bucket = ctx.workspace.resources.get("artifacts_bucket")
    if not bucket:
        raise RuntimeError(
            "artifacts_bucket missing from this workspace's resource map — run its bootstrap"
        )
    if cfg.artifact_kind == "code_zip":
        return _package_code_zip(ctx, agent, cfg, bucket, wrapper)
    return _package_container_source(ctx, agent, cfg)


def _stage_provision(ctx: StageContext, agent: Agent, iam_client: Any = None) -> StageResult:
    spec = AgentSpec(**agent.spec)
    role_arn, detail = agent_iam.provision_execution_role(
        agent, spec, get_settings(), ctx.workspace, ctx.log, iam=iam_client
    )
    ctx.scratch["execution_role_arn"] = role_arn
    return StageResult(detail=detail)


def _adot_launcher(row: Agent) -> bool:
    """The generate stage's ADOT decision, read from the persisted spec (the
    raw dict: the field is server-owned and absent from the schema)."""
    provenance = ((row.spec or {}).get("byoc") or {}).get("provenance") or {}
    return bool(provenance.get("adot_launcher"))


def _container_uri(ctx: StageContext, agent: Agent, cfg: ByocConfig) -> str:
    if cfg.artifact_kind == "container_image":
        return cfg.image_uri or ""
    registry, repo, tag = _image_ref(ctx.workspace, agent)
    return (
        ctx.scratch.get("image_uri")
        or _recorded_digest_uri(ctx, registry, repo)
        # only reached when the digest record is gone; the tag is mutable, so
        # this is a fallback, not a path to rely on
        or f"{registry}/{repo}:{tag}"
    )


def _stage_deploy(ctx: StageContext, agent: Agent) -> StageResult:
    # A resumed job skips its successful provision stage, but scratch is
    # process-local. Reconcile the role idempotently instead of silently
    # switching the workload to the workspace's broader shared role.
    if not ctx.scratch.get("execution_role_arn"):
        _stage_provision(ctx, agent)
    client = control_client(ctx.workspace)
    mode = ctx.scratch.get("mode", "create")
    db = ctx.session()
    try:
        row = db.get(Agent, agent.id)
        spec = AgentSpec(**row.spec)
        cfg = _config(spec)
        role_arn = ctx.scratch["execution_role_arn"]
        environment = runtime_environment(spec, ctx.workspace.resources, agent_id=row.id,
                                          workspace_id=row.workspace_id or "")
        # The per-agent execution role scopes bedrock:InvokeModel to exactly
        # spec.allowed_model_ids (agent_iam.allowed_model_resources) — hand the
        # permitted ids to the user code: MODEL_ID is the primary (= model_id),
        # ALLOWED_MODEL_IDS the full comma-separated list. Explicit spec.env
        # values win for both.
        if spec.protocol != "mcp":  # an MCP server is granted no model
            environment.setdefault("MODEL_ID", spec.model_id)
            environment.setdefault("ALLOWED_MODEL_IDS", ",".join(spec.allowed_model_ids))
            # §10.8: an isolated project's profile ARN for the primary model, for code
            # that opts in (MODEL_ID stays the spec's id — user code may parse it)
            environment.update(model_profile.model_environment(
                row.project_id, spec, ctx.workspace, ctx.log
            ))
        # the BYO mounts' manifest (storage-mounts §3.2) — platform-owned, wins over spec.env
        with_mounts_env(environment, spec)
        wrapper = stdio_wrapper(spec)
        if wrapper is not None:
            # the bridge's subprocess argv — platform-owned, so it wins over spec.env
            environment[STDIO_COMMAND_ENV] = json.dumps(wrapper.argv())
        code_runtime = runs_on_code_runtime(spec)
        # echoed on every update: an omitted protocolConfiguration resets to HTTP
        protocol = spec.protocol
        resolved_auth = inbound_auth_service.resolve_for_agent(row, db)
        authorizer = inbound_auth_service.authorizer_configuration(resolved_auth)
        instrument = code_runtime and _adot_launcher(row)
        if code_runtime:
            ctx.log(f"telemetry: ADOT launcher {'on' if instrument else 'off'}")
        # both artifact kinds carry the spec's network (design §12.6), on create and
        # on every update
        vpc = spec_vpc(spec, row.workspace_id)
        ctx.log(f"network: {describe_network(vpc)}")
        # session storage + BYO mounts (storage-mounts §1.5, §3.2), on create and on
        # every update — UpdateAgentRuntime detaches an omitted list. An MCP server
        # mounts them too: its tools may serve files.
        mounts = filesystem_configurations(spec) or None
        if detail := describe_mounts(spec):
            ctx.log(detail)

        # cost-tags §1.2: the full set (agent id, workspace) on create and update
        tags = cost_tags.agent_tags(row, row.workspace_id)

        def _kwargs() -> dict:
            if code_runtime:
                return {
                    "s3_bucket": ctx.scratch.get("s3_bucket")
                    or ctx.workspace.resources.get("artifacts_bucket", ""),
                    "s3_key": ctx.scratch.get("s3_key") or package_key(row),
                    "role_arn": role_arn,
                    "environment": environment,
                    "python_version": cfg.python_version,
                    "entrypoint": STDIO_BRIDGE_FILE if wrapper else cfg.entrypoint,
                    # only when the upload carries the ADOT distro: an absent
                    # opentelemetry-instrument launcher fails at start
                    "instrument": instrument,
                    # echoed on every update; omitted (None) = IAM/SigV4
                    "authorizer_configuration": authorizer,
                    "protocol": protocol,
                    "filesystem_configurations": mounts,
                    "vpc": vpc,
                    "tags": tags,
                }
            return {
                "container_uri": _container_uri(ctx, row, cfg),
                "role_arn": role_arn,
                "environment": environment,
                "authorizer_configuration": authorizer,
                "protocol": protocol,
                "filesystem_configurations": mounts,
                "vpc": vpc,
                "tags": tags,
            }

        create_fn = rt.create_code_runtime if code_runtime else rt.create_container_runtime
        update_fn = rt.update_code_runtime if code_runtime else rt.update_container_runtime

        if mode == "update" and row.resource_id:  # re-publish → new version, same ARN
            runtime_id = row.resource_id
            # keep the mounts added outside the platform (UpdateAgentRuntime would
            # detach them): the spec's entries + the live runtime's foreign ones
            update_kwargs = {**_kwargs(), "filesystem_configurations": runtime_update_mounts(
                client, runtime_id, spec,
                managed_paths=published_mount_paths(db, row.id), vpc=vpc, log=ctx.log)}
            updated = agent_iam.retry_iam_propagation(
                lambda: update_fn(client, runtime_id=runtime_id, **update_kwargs),
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
        else:
            created = agent_iam.retry_iam_propagation(
                lambda: create_fn(
                    client, runtime_name=sanitize_runtime_name(row.name), **_kwargs()
                ),
                ctx.log,
            )
            runtime_id = created["agentRuntimeId"]
            row.resource_id = runtime_id
            row.arn = created["agentRuntimeArn"]
            row.version = str(created.get("agentRuntimeVersion", "1"))
            db.commit()
            ctx.log(
                f"CreateAgentRuntime accepted · runtimeId {runtime_id} · "
                f"inbound auth {resolved_auth.mode}"
            )

        try:
            ready = rt.wait_runtime_ready(
                client, runtime_id, on_status=lambda s: ctx.log(f"runtime status: {s}")
            )
        except Exception:
            if instrument:
                ctx.log(
                    "hint: the runtime started under opentelemetry-instrument — if "
                    "the ADOT distro did not install, remove aws-opentelemetry-distro "
                    "from requirements.txt or build from a Dockerfile instead"
                )
            raise
        row.arn = ready["agentRuntimeArn"]
        # The inbound-auth snapshot lands only once the runtime is READY on the
        # version that carries it: a Create/Update that is accepted and then
        # fails leaves the ledger on the mode the live runtime still serves.
        # A resumed job (create already accepted, or an update re-issued)
        # reaches this line too, so the snapshot is never skipped.
        inbound_auth_service.record_deployed_auth(row, resolved_auth)
        row.version = str(ready.get("agentRuntimeVersion", row.version or "1"))
        stamp_input_contract(ctx, db, row)
        db.commit()
        # as_user return-URL allow-list on the runtime's auto-created workload
        # identity, reconciled on every deploy (create and redeploy)
        detail = f"READY · {ready['agentRuntimeArn']}"
        if not register_return_url_stage(client, spec, runtime_id, ctx.log):
            detail += f" · {RETURN_URL_WARNING}"
        return StageResult(detail=detail)
    finally:
        db.close()


def _stage_register(ctx: StageContext, agent: Agent) -> StageResult:
    if (agent.spec or {}).get("protocol") == "mcp":
        return _register_mcp_server(ctx, agent)
    from app.deployer.registration import register_stage

    return register_stage(ctx, agent)


def _register_mcp_server(ctx: StageContext, agent: Agent) -> StageResult:
    """An MCP server registers on the Gateway (not as an A2A record): its OAuth2
    provider, then the MCP-server target, whose creation / re-sync lists its tools."""
    db = ctx.session()
    try:
        row = db.get(Agent, agent.id)
        spec = AgentSpec(**row.spec)
        mcp = spec.mcp
        if mcp is None or not mcp.client_id:  # generate always stamps it
            raise RuntimeError("MCP server has no inbound client — re-run the deploy")
        provider_arn = mcp_servers.ensure_provider(ctx.workspace, row, mcp.client_id, ctx.log)
        target_id, outcome = mcp_servers.ensure_target(
            ctx.workspace,
            row,
            target_name=mcp.target_name,
            provider_arn=provider_arn,
            previous_target_id=mcp.target_id,
            log=ctx.log,
        )
    finally:
        db.close()

    def update(stored: dict) -> None:
        stored["mcp"] = {
            **(stored.get("mcp") or {}),
            "provider_arn": provider_arn,
            "target_id": target_id,
        }

    _stamp_spec(ctx, agent, update)
    if target_id is None:  # withheld until the tool list is approved (§15.6)
        detail = f"gateway target {mcp.target_name} {outcome} · awaiting tool review"
    else:
        detail = f"gateway target {mcp.target_name} {outcome} · READY"
        try:
            tools = mcp_servers.gateway_tools(ctx.workspace, mcp.target_name)
            ctx.log(
                f"{len(tools)} tool(s) on the Gateway: "
                + (", ".join(t["name"] for t in tools[:20]) or "none")
            )
            detail += f" · {len(tools)} tool(s)"
        except AppError as exc:  # the target is READY; listing is a read-back only
            ctx.log(f"tools/list through the Gateway skipped: {exc.message}")
    ctx.log(
        "Registry: the Gateway-level record is not rewritten here (an update sends "
        "an approved record back to DRAFT) — refresh it from Governance → Gateway → "
        "Registry import when the catalog should list these tools"
    )
    return StageResult(detail=detail)


STAGES = {
    "generate": _stage_generate,
    "package": _stage_package,
    "provision": _stage_provision,
    "deploy": _stage_deploy,
    "register": _stage_register,
}

register_method("byoc", STAGES)


def delete_agent_resources(
    agent: Agent, workspace: WorkspaceContext, ecr_client: Any = None
) -> None:
    """Runtime + staged upload objects + the built image tags (container_source,
    or a code_zip stdio server on the Node base).

    Everything after the runtime delete is best-effort: an orphan S3 object or
    ECR tag must never block deleting the agent."""
    pending: AppError | None = None
    is_mcp = (agent.spec or {}).get("protocol") == "mcp"
    if is_mcp:
        # the Gateway target first: it points at the runtime about to go
        mcp_servers.teardown_target(workspace, agent)
    if agent.resource_id:
        client = control_client(workspace)
        # a gated agent serves through named endpoints; AgentCore refuses to delete a
        # runtime that still has them (Agent-DLC §6)
        from app.dlc.releases import delete_endpoints, delete_runtime_or_defer

        delete_endpoints(agent, client)
        try:
            delete_runtime_or_defer(agent, client)
        except AppError as exc:
            # the S3 / ECR cleanup below is ours either way; the sweep finishes the runtime
            pending = exc
    if is_mcp:
        mcp_servers.release_identity(
            workspace, agent, lambda msg: logger.warning("agent %s: %s", agent.id, msg)
        )
    cfg = (agent.spec or {}).get("byoc") or {}
    upload_id = cfg.get("upload_id")
    if upload_id:
        byoc_uploads.delete_upload_objects(workspace, agent.workspace_id, upload_id)
    wrapper = ((agent.spec or {}).get("mcp") or {}).get("stdio_wrapper") or {}
    built_image = cfg.get("artifact_kind") == "container_source" or (
        is_mcp and cfg.get("artifact_kind") == "code_zip" and wrapper.get("base") == "node"
    )
    if built_image:
        # the builds this agent pushed are {name}-v{version} tags on the shared repo,
        # or on an isolated project's own (and the shared one for builds before it)
        for repo in project_storage.ecr_repos(workspace, getattr(agent, "project_id", None)):
            _delete_image_tags(agent, repo, workspace, ecr_client)
    if pending is not None:
        raise pending


def _delete_image_tags(agent: Agent, repo: str, workspace: WorkspaceContext,
                       ecr_client: Any = None) -> None:
    try:
        ecr = ecr_client or workspace.client("ecr")
        versions = range(1, int(agent.version or "1") + 1)
        resp = ecr.batch_delete_image(
            repositoryName=repo,
            imageIds=[{"imageTag": f"{agent.name}-v{v}"} for v in versions],
        )
    except Exception as exc:  # noqa: BLE001 — cleanup must never block a delete
        if not is_not_found(exc):
            logger.warning(
                "byoc delete %s: ECR tags %s-v* in %s left behind: %s",
                agent.name, agent.name, repo, cleanup_error(exc),
            )
        return
    # per-image failures come back in the response, not as an exception
    for failure in (resp or {}).get("failures") or []:
        if failure.get("failureCode") != "ImageNotFound":
            logger.warning(
                "byoc delete %s: ECR image %s in %s left behind: %s: %s",
                agent.name,
                (failure.get("imageId") or {}).get("imageTag", "?"),
                repo,
                failure.get("failureCode"),
                failure.get("failureReason", ""),
            )
