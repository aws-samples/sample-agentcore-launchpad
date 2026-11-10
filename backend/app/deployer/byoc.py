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

The platform NEVER executes the uploaded code on the Launchpad host: package
work is archive extraction, file checks and a pip *download/install into the
bundle directory* (wheels are unpacked, never imported or run).
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.naming import default_ecr_repo
from app.core.runtime_target import pip_platform_args
from app.deployer import adot_cn, log_retention
from app.deployer.environment import check_env_budget, runtime_environment
from app.deployer.pipeline import (
    MethodCapabilities,
    StageContext,
    StageResult,
    register_method,
)
from app.deployer.return_url import RETURN_URL_WARNING, register_return_url_stage
from app.models.ledger import Agent
from app.schemas.agent import AgentSpec, ByocConfig, parse_ecr_image_uri
from app.services import agent_iam, byoc_uploads, ecr, policy_enforcement
from app.services import inbound_auth as inbound_auth_service
from app.services.agentcore import runtime as rt
from app.services.agentcore.client import control_client
from app.services.requirements_txt import (
    RESOLVE_FIX_HINTS,
    RequirementsFileError,
    describe_exit,
    parse_requirements_txt,
    pip_python_version,
    summarize_resolver_failure,
)
from app.services.workspace import WorkspaceContext

from .image_build import (
    _image_ref,
    _recorded_digest_uri,
    build_and_push_image,
    platform_buildspec_path,
)
from .zip_runtime import (
    _compile_lock,
    _raise_if_stopped_with_the_service,
    find_adoptable_runtime,
    sanitize_runtime_name,
)

PACKAGE_KEY_TMPL = "agents/{name}/byoc_package.zip"


def _config(spec: AgentSpec) -> ByocConfig:
    # round 16: the creation-time validator must have refused this — kept as the
    # last line of defence for stored specs (tests/fail_early_registry.py)
    if spec.byoc is None:  # schema guarantees this; belt for hand-built rows
        raise RuntimeError("byoc spec has no byoc settings block")
    return spec.byoc


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


# N17-obs410: the deploy result names a bundle that cannot emit spans
NO_TELEMETRY_WARNING = "no telemetry: observability and evaluation unavailable"


def adot_status(src_root: Path, cfg: ByocConfig) -> dict[str, Any]:
    """``{instrumented, reason}`` of the BUILT bundle: instrumented when the ADOT distro and
    its ``opentelemetry-instrument`` launcher (``opentelemetry.instrumentation.
    auto_instrumentation``) are both installed at the bundle root — resolved from
    requirements.txt or vendored. Anything else starts as plain ``python <entrypoint>``: a
    missing launcher would fail the runtime at start, which no warning is worth."""
    names = [p.name for p in src_root.iterdir()]
    distro = (src_root / byoc_uploads.ADOT_PACKAGE).is_dir() or any(
        n.startswith(f"{byoc_uploads.ADOT_PACKAGE}-") and n.endswith(".dist-info")
        for n in names)
    launcher = (src_root / "opentelemetry" / "instrumentation" / "auto_instrumentation").is_dir()
    if distro and launcher:
        return {"instrumented": True, "reason": ""}
    if distro:
        return {"instrumented": False, "reason": (
            f"{byoc_uploads.ADOT_DISTRIBUTION} is in the bundle without its "
            "opentelemetry-instrument launcher (opentelemetry-instrumentation)")}

    def read(name: str) -> str | None:
        path = src_root / name
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None

    if byoc_uploads.declares_adot(read("requirements.txt")):
        return {"instrumented": False, "reason": (
            f"requirements.txt lists {byoc_uploads.ADOT_DISTRIBUTION} but "
            "install_requirements is off — nothing was installed"
            if not cfg.install_requirements else
            f"requirements.txt lists {byoc_uploads.ADOT_DISTRIBUTION} but it did not install")}
    if byoc_uploads.declares_adot(read("pyproject.toml")):
        return {"instrumented": False, "reason": (
            f"pyproject.toml lists {byoc_uploads.ADOT_DISTRIBUTION}, but only "
            "requirements.txt is installed — add it there")}
    return {"instrumented": False, "reason": (
        f"the bundle does not install {byoc_uploads.ADOT_DISTRIBUTION}")}


def _stamp_telemetry(ctx: StageContext, agent: Agent, telemetry: dict[str, Any]) -> None:
    """Persist the package stage's ADOT verdict on the row (``spec.byoc.telemetry``): the
    deploy stage of this or a resumed job reads it, and the console renders it."""
    db = ctx.session()
    try:
        row = db.get(Agent, agent.id)
        spec = dict(row.spec if row is not None else agent.spec)
        # N17-obs465: the container kinds stamp their layer decision on the same record
        prior = (spec.get("byoc") or {}).get("telemetry") or {}
        telemetry = {**prior, **telemetry}
        spec["byoc"] = {**(spec.get("byoc") or {}), "telemetry": telemetry}
        if row is not None:  # a row deleted mid-package: the pipeline cancels the job
            row.spec = spec
            db.commit()
        agent.spec = spec
    finally:
        db.close()


def _runtime_environment(ctx: StageContext, agent: Agent, spec: AgentSpec) -> dict[str, str]:
    """The merged runtime environment of a BYOC agent: the shared derivation plus the
    model handles the member's own code reads (``MODEL_ID`` = the primary model,
    ``ALLOWED_MODEL_IDS`` the comma-separated permitted list; explicit spec.env values win
    for both). The budget is re-checked after the two additions (rev-r16 on N16-obs72)."""
    environment = runtime_environment(
        spec, ctx.workspace.resources, workload_name=agent.resource_id or "", agent_id=agent.id,
    )
    environment.setdefault("MODEL_ID", spec.model_id)
    environment.setdefault("ALLOWED_MODEL_IDS", ",".join(spec.allowed_model_ids))
    check_env_budget(environment, spec)
    return environment


def _stage_generate(ctx: StageContext, agent: Agent) -> StageResult:
    """No code to generate — verify the artifact reference and stamp provenance. The merged
    runtime environment is derived first (no AWS call) so an over-budget map fails here
    with ``agent.env_budget_exceeded`` rather than at CreateAgentRuntime."""
    spec = AgentSpec(**agent.spec)
    cfg = _config(spec)
    policy_enforcement.stage_policy(ctx, agent, spec)  # logged once per job
    _runtime_environment(ctx, agent, spec)

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
        ctx.log(f"ECR image verified · {cfg.image_uri} · {digest}")
        return StageResult(detail=f"container_image · {digest[:19]}…")

    manifest = byoc_uploads.get_manifest(ctx.workspace, cfg.upload_id or "")
    provenance = {
        "sha256": manifest.get("sha256", ""),
        "size_bytes": manifest.get("size_bytes", 0),
        "original_filename": manifest.get("original_filename", ""),
        "uploaded_by": manifest.get("uploaded_by", ""),
        "uploaded_at": manifest.get("uploaded_at", ""),
    }
    _stamp_provenance(ctx, agent, provenance)
    detected = manifest.get("detected") or {}
    if cfg.artifact_kind == "code_zip" and not detected.get("agentcore_sdk_detected"):
        ctx.log(
            "note: no BedrockAgentCoreApp/@app.entrypoint marker found — the "
            "entrypoint must serve POST /invocations + GET /ping on :8080 itself"
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
    # round 16: the creation-time validator must have refused this — kept as the
    # last line of defence for stored specs (tests/fail_early_registry.py)
    if parsed is None:
        raise RuntimeError(f"'{image_uri}' is not a private ECR image URI")
    account_id, region = parsed
    if account_id != workspace.account_id or region != workspace.region:
        raise RuntimeError(
            f"image {image_uri} lives in {account_id}/{region}; this workspace "
            f"deploys from {workspace.account_id}/{workspace.region} only"
        )
    rest = image_uri.split(f".{workspace.dns_suffix}/", 1)[1]
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
        _raise_if_stopped_with_the_service("pip install", proc.returncode)
        raise RuntimeError(
            "pip install of the zip's locked requirements failed "
            f"({describe_exit(proc.returncode)}): "
            + summarize_resolver_failure(
                proc.stderr or "", python_version=pip_version, hints=RESOLVE_FIX_HINTS,
                returncode=proc.returncode,
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
    ctx: StageContext, agent: Agent, cfg: ByocConfig, bucket: str
) -> StageResult:
    # Agent names are only unique within a workspace. Each attempt owns its
    # directory, including while another job is resolving dependencies.
    with tempfile.TemporaryDirectory(prefix="launchpad_byoc_") as tmp:
        return _build_code_zip(ctx, agent, cfg, bucket, Path(tmp))


def _build_code_zip(
    ctx: StageContext, agent: Agent, cfg: ByocConfig, bucket: str, build_dir: Path
) -> StageResult:
    zip_path = build_dir / "upload.zip"
    byoc_uploads.download_upload(ctx.workspace, agent.workspace_id, cfg.upload_id or "",
                                 zip_path)
    src_root = byoc_uploads.extract_zip(zip_path, build_dir / "src")

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

    telemetry = adot_status(src_root, cfg)
    _stamp_telemetry(ctx, agent, telemetry)
    if telemetry["instrumented"]:
        ctx.log("ADOT launcher in the bundle — the runtime starts via opentelemetry-instrument")
    else:
        ctx.log(f"WARNING {NO_TELEMETRY_WARNING} — {telemetry['reason']}; the runtime starts "
                f"as `python {cfg.entrypoint}` (see docs: BYOC self-instrumentation)")

    final_zip = build_dir / "deployment_package.zip"
    _zip_tree(src_root, final_zip)
    size_mb = final_zip.stat().st_size / 1e6
    secs = time.monotonic() - t0

    s3_key = PACKAGE_KEY_TMPL.format(name=agent.name)
    ctx.workspace.client("s3").upload_file(str(final_zip), bucket, s3_key)
    ctx.scratch["s3_bucket"], ctx.scratch["s3_key"] = bucket, s3_key
    ctx.log(f"package {secs:.1f}s · {size_mb:.1f}MB → s3://{bucket}/{s3_key}")
    detail = f"code_zip · {size_mb:.1f}MB · s3 ✓"
    if installed:
        detail += f" · {installed} deps resolved (aarch64)"
    detail += " · ADOT ✓" if telemetry["instrumented"] else f" · {NO_TELEMETRY_WARNING}"
    return StageResult(detail=detail)


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
    # N17-obs465: a Node agent (its package.json depends on ADOT Node) in aws-cn gets the
    # platform's trace exporter layer on top of its own image
    package = src_root / "package.json"
    adot_range = adot_cn.node_adot_in_package_json(
        package.read_text(encoding="utf-8", errors="replace") if package.is_file() else None)
    apply, reason = adot_cn.decide(ctx.workspace.partition, cfg.cn_trace_exporter,
                                   adot_range is not None)
    if apply:
        adot_cn.stage_layer(src_root)
        ctx.log(f"Node agent (ADOT Node {adot_range}) in aws-cn — adding the platform's aws-cn "
                "trace exporter layer (N17-obs465; opt out: byoc.cn_trace_exporter=false)")
    archive = shutil.make_archive(str(build_dir / "context_src"), "zip", src_root)
    tag, mins = build_and_push_image(
        ctx, agent, archive, **({"extra_env": {"LAUNCHPAD_CN_TRACE_LAYER": "1"}} if apply else {}))
    digest = ctx.scratch["image_digest"]
    telemetry: dict[str, Any] = {"cn_exporter": reason,
                                 "runtime": "node" if package.is_file() else ""}
    if apply:
        registry, repo, _tag = _image_ref(ctx.workspace, agent)
        member = ecr.resolve_digest(ctx.workspace.client("ecr"), repo, f"{tag}-member")
        telemetry |= {"member_image": f"{registry}/{repo}@{member}",
                      "layer_image": ctx.scratch["image_uri"]}
        ctx.log(f"member image {registry}/{repo}@{member} · deployed with the aws-cn trace "
                f"exporter layer {ctx.scratch['image_uri']}")
    _stamp_telemetry(ctx, agent, telemetry)
    layer = " · aws-cn trace exporter layer" if apply else ""
    return StageResult(
        detail=f"codebuild · arm64 · {mins:.1f}m → :{tag} @ {digest[:19]}…{layer}")


def _package_container_image(ctx: StageContext, agent: Agent, cfg: ByocConfig) -> StageResult:
    """An existing ECR image: deployed as is, except a Node image in aws-cn, which gets the
    platform's trace exporter layer FROM it (N17-obs465) — the member's image is unchanged."""
    uri = cfg.image_uri or ""
    node: bool | None = False
    if ctx.workspace.partition == "aws-cn" and cfg.cn_trace_exporter:
        repo, image_id = _ecr_ref(ctx.workspace, uri)
        node = adot_cn.image_is_node(ctx.workspace.client("ecr"), repo, image_id)
    apply, reason = adot_cn.decide(ctx.workspace.partition, cfg.cn_trace_exporter, node)
    member = f"{uri.split('@')[0].rsplit(':', 1)[0]}@{cfg.provenance.sha256}" if (
        cfg.provenance and cfg.provenance.sha256) else uri
    if not apply:
        ctx.scratch["image_uri"] = uri
        _stamp_telemetry(ctx, agent, {"cn_exporter": reason, "member_image": member,
                                      "layer_image": "", "runtime": "node" if node else ""})
        if reason == "unknown_runtime":
            ctx.log("WARNING could not read the image config — deployed without the aws-cn "
                    "trace exporter layer (a Node image would have no traces)")
        return StageResult(skipped=True, detail="existing image — no build")
    bucket = ctx.workspace.resources.get("artifacts_bucket")
    if not bucket:
        raise RuntimeError(
            "artifacts_bucket missing from this workspace's resource map — run its bootstrap"
        )
    ctx.log(f"Node image in aws-cn — building the platform's aws-cn trace exporter layer FROM "
            f"{member} (N17-obs465; opt out: byoc.cn_trace_exporter=false)")
    with tempfile.TemporaryDirectory(prefix="launchpad_byoc_layer_") as tmp:
        root = Path(tmp) / "ctx"
        root.mkdir()
        shutil.copy2(platform_buildspec_path(), root / "buildspec.yml")
        adot_cn.stage_layer(root)
        archive = shutil.make_archive(str(Path(tmp) / "layer_ctx"), "zip", root)
        tag, mins = build_and_push_image(ctx, agent, archive, extra_env={
            "LAUNCHPAD_CN_TRACE_LAYER": "1", "LAUNCHPAD_MEMBER_IMAGE": uri})
    _stamp_telemetry(ctx, agent, {"cn_exporter": reason, "runtime": "node",
                                  "member_image": member, "layer_image": ctx.scratch["image_uri"]})
    ctx.log(f"member image {member} · deployed with the aws-cn trace exporter layer "
            f"{ctx.scratch['image_uri']}")
    return StageResult(detail=f"codebuild · arm64 · {mins:.1f}m → :{tag} · aws-cn trace "
                              f"exporter layer FROM {member[-19:]}")


def _ecr_ref(workspace: WorkspaceContext, image_uri: str) -> tuple[str, dict[str, str]]:
    """(repository, imageId) of a private ECR image URI of this workspace."""
    rest = image_uri.split(f".{workspace.dns_suffix}/", 1)[1]
    if "@sha256:" in rest:
        repo, _, ref = rest.partition("@")
        return repo, {"imageDigest": ref}
    repo, _, ref = rest.rpartition(":")
    return repo, {"imageTag": ref}


def _stage_package(ctx: StageContext, agent: Agent) -> StageResult:
    spec = AgentSpec(**agent.spec)
    cfg = _config(spec)
    if cfg.artifact_kind == "container_image":
        # deploy pins the URI the generate stage verified — or, for a Node image in aws-cn,
        # the platform's trace exporter layer built FROM it (N17-obs465)
        return _package_container_image(ctx, agent, cfg)
    # round 16: the creation-time validator must have refused this — kept as the
    # last line of defence for stored specs (tests/fail_early_registry.py)
    bucket = ctx.workspace.resources.get("artifacts_bucket")
    if not bucket:
        raise RuntimeError(
            "artifacts_bucket missing from this workspace's resource map — run its bootstrap"
        )
    if cfg.artifact_kind == "code_zip":
        return _package_code_zip(ctx, agent, cfg, bucket)
    return _package_container_source(ctx, agent, cfg)


def _stage_provision(ctx: StageContext, agent: Agent, iam_client: Any = None) -> StageResult:
    spec = AgentSpec(**agent.spec)
    role_arn, detail = agent_iam.provision_execution_role(
        agent, spec, get_settings(), ctx.workspace, ctx.log, iam=iam_client,
    )
    ctx.scratch["execution_role_arn"] = role_arn
    return StageResult(detail=detail)


def _instrumented(cfg: ByocConfig) -> bool:
    """The package stage's stamped ADOT verdict; a row packaged before it existed (or never
    packaged) starts uninstrumented, as before."""
    return bool(cfg.telemetry and cfg.telemetry.instrumented)


def _container_uri(ctx: StageContext, agent: Agent, cfg: ByocConfig) -> str:
    if cfg.artifact_kind == "container_image":
        # N17-obs465: the layer built FROM the member's image, when one was (a resumed job
        # reads it back from the row's stamp)
        return (ctx.scratch.get("image_uri") or (cfg.telemetry.layer_image if cfg.telemetry
                                                 else "") or cfg.image_uri or "")
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
        policy = policy_enforcement.stage_policy(ctx, row, spec)
        environment = _runtime_environment(ctx, row, spec)
        resolved_auth = inbound_auth_service.resolve_for_agent(row, db)
        authorizer = inbound_auth_service.authorizer_configuration(resolved_auth)

        def _kwargs() -> dict:
            if cfg.artifact_kind == "code_zip":
                return {
                    "s3_bucket": ctx.scratch.get("s3_bucket")
                    or ctx.workspace.resources.get("artifacts_bucket", ""),
                    "s3_key": ctx.scratch.get("s3_key")
                    or PACKAGE_KEY_TMPL.format(name=row.name),
                    "role_arn": role_arn,
                    "environment": environment,
                    "python_version": cfg.python_version,
                    "entrypoint": cfg.entrypoint,
                    # N17-obs410: the launcher only when the package stage found it in
                    # the built bundle — an absent one fails the runtime at start
                    "instrument": _instrumented(cfg),
                    # echoed on every update; omitted (None) = IAM/SigV4
                    "authorizer_configuration": authorizer,
                }
            return {
                "container_uri": _container_uri(ctx, row, cfg),
                "role_arn": role_arn,
                "environment": environment,
                "authorizer_configuration": authorizer,
            }

        create_fn = (
            rt.create_code_runtime if cfg.artifact_kind == "code_zip"
            else rt.create_container_runtime
        )
        update_fn = (
            rt.update_code_runtime if cfg.artifact_kind == "code_zip"
            else rt.update_container_runtime
        )

        if mode == "update" and row.resource_id:  # re-publish → new version, same ARN
            runtime_id = row.resource_id
            updated = agent_iam.retry_iam_propagation(
                lambda: update_fn(client, runtime_id=runtime_id, **_kwargs()),
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
        elif existing := find_adoptable_runtime(client, db, row, role_arn, ctx.log):
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
                lambda: create_fn(
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
        row.arn = ready["agentRuntimeArn"]
        # The inbound-auth snapshot lands only once the runtime is READY on the
        # version that carries it: a Create/Update that is accepted and then
        # fails leaves the ledger on the mode the live runtime still serves.
        # A resumed job (create already accepted, or an update re-issued)
        # reaches this line too, so the snapshot is never skipped.
        inbound_auth_service.record_deployed_auth(row, resolved_auth)
        row.version = str(ready.get("agentRuntimeVersion", row.version or "1"))
        db.commit()
        # stream A: member code never renders the template's principal carrier
        policy_enforcement.mark_applied(db, row, policy, job_id=ctx.job_id, carrier=False)
        # N11-obs56: bounded retention on the runtime's log group (fail-soft, idempotent)
        ctx.log(log_retention.apply_runtime_log_retention(ctx, runtime_id))
        # as_user return-URL allow-list on the runtime's auto-created workload
        # identity, reconciled on every deploy (create and redeploy)
        detail = f"READY · {ready['agentRuntimeArn']}"
        if not register_return_url_stage(client, spec, runtime_id, ctx.log):
            detail += f" · {RETURN_URL_WARNING}"
        if cfg.artifact_kind == "code_zip" and not _instrumented(cfg):
            detail += f" · {NO_TELEMETRY_WARNING}"  # N17-obs410: non-blocking
        return StageResult(detail=detail)
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

# N12-obs33/34: a BYOC runtime is versioned like any Runtime, but its source is the
# member's — the platform can neither verify bundle consumption nor mint a candidate
# version from an edited spec.
register_method(
    "byoc", STAGES,
    MethodCapabilities(
        user_source=True, runtime_versions=True,
        # creation-time domain (round 16): the member's own code decides its tools — a
        # ToolRef is an outbound-auth declaration only (IAM + LAUNCHPAD_OUTBOUND_AUTH), so
        # no type is rendered without auth; memory is env only; the artifact kinds below
        builtin_tools=frozenset(),
        tool_types=frozenset(),
        tool_auth_modes=frozenset({"as_agent", "as_user"}),
        native_tools=frozenset(),
        protocols=frozenset({"http"}),
        memory=True, knowledge_bases=False,
        byoc_kinds=frozenset({"code_zip", "container_source", "container_image"}),
        requires=frozenset({"artifacts_bucket"}),
    ),
)


def delete_agent_resources(
    agent: Agent, workspace: WorkspaceContext, ecr_client: Any = None
) -> str | None:
    """Runtime + staged upload objects + (container_source) the built image tags.
    Returns the runtime outcome (``rt.RUNTIME_DELETED`` / ``rt.RUNTIME_ABSENT``, N16-obs87)
    or ``None`` when the agent never had a runtime id.

    Everything after the runtime delete is best-effort: an orphan S3 object or
    ECR tag must never block deleting the agent."""
    outcome = None
    if agent.resource_id:
        outcome = rt.delete_runtime_if_present(control_client(workspace), agent.resource_id)
    cfg = (agent.spec or {}).get("byoc") or {}
    upload_id = cfg.get("upload_id")
    if upload_id:
        byoc_uploads.delete_upload_objects(workspace, agent.workspace_id, upload_id)
    if cfg.get("artifact_kind") == "container_source":
        # the builds this agent pushed are {name}-v{version} tags on the shared repo
        repo = (workspace.resources or {}).get("ecr_repo", default_ecr_repo())
        try:
            ecr = ecr_client or workspace.client("ecr")
            versions = range(1, int(agent.version or "1") + 1)
            ecr.batch_delete_image(
                repositoryName=repo,
                imageIds=[{"imageTag": f"{agent.name}-v{v}"} for v in versions],
            )
        except Exception:  # noqa: BLE001 — cleanup must never block a delete
            pass
    return outcome
