"""Skill Lab foundation: worker build context, infra idempotency, boundaries."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.services import workspace_bootstrap
from app.skill_lab import infra, worker_build

APP_DIR = Path(__file__).resolve().parents[1] / "app"
VENDOR_ROOT = Path(__file__).resolve().parents[2] / "vendor" / "skillopt"


# ── boundary guards ────────────────────────────────────────────────────────


def test_backend_never_imports_vendored_skillopt():
    """The vendored tree runs as a subprocess only (funnel + import-time-env
    rationale in app/skill_lab/__init__.py). An in-process import is a bug."""
    pattern = re.compile(r"^\s*(from|import)\s+skillopt\b")
    offenders = [
        str(path)
        for path in APP_DIR.rglob("*.py")
        for line in path.read_text(encoding="utf-8").splitlines()
        if pattern.match(line)
    ]
    assert offenders == []


def test_vendored_tree_shape():
    for rel in (
        "LICENSE",
        "LAUNCHPAD_DEVIATIONS.md",
        "requirements-launchpad.txt",
        "scripts/evaluate_skill.py",
        "scripts/train.py",
        "skillopt/model/bedrock_chat.py",
        "skillopt/model/agentcore_worker.py",
        "skillopt/prompts/analyst_error.md",
        "configs/_base_/default.yaml",
        "configs/skilleval/default.yaml",
        "deploy/agentcore/Dockerfile",
        "deploy/agentcore/codex-config.toml",
    ):
        assert (VENDOR_ROOT / rel).is_file(), f"missing {rel}"
    # Subprocess runs bytecode-compile the vendored tree on disk (that's fine —
    # the global .gitignore keeps __pycache__ out of commits); what must stay
    # clean is everything that reaches the worker image build context.
    context_names = [name for name, _ in worker_build._iter_context_files()]
    assert not [n for n in context_names if "__pycache__" in n or n.endswith(".pyc")]


def test_worker_cli_version_pin_matches_the_dockerfile():
    """The Dockerfile ARG is the build input; the setting only mirrors it, so the
    two copies of the pin drift silently without this."""
    dockerfile = (VENDOR_ROOT / "deploy" / "agentcore" / "Dockerfile").read_text()
    pin = re.search(r"^ARG CLAUDE_CLI_VERSION=(\S+)", dockerfile, re.MULTILINE)
    assert pin, "the worker Dockerfile lost its CLAUDE_CLI_VERSION pin"
    assert pin.group(1) == get_settings().skill_lab_worker_cli_version
    codex_pin = re.search(r"^ARG CODEX_CLI_VERSION=(\S+)", dockerfile, re.MULTILINE)
    assert codex_pin, "the worker Dockerfile lost its CODEX_CLI_VERSION pin"
    assert codex_pin.group(1) == get_settings().skill_lab_worker_codex_version


def test_skill_lab_stage_registered_but_not_required():
    assert "skill-lab" in workspace_bootstrap.STAGE_ORDER
    assert "skill-lab" in workspace_bootstrap.STAGES
    assert not any(
        key.startswith("skill_lab") for key in workspace_bootstrap.REQUIRED_RESOURCE_KEYS
    )


def test_skill_lab_stage_degrades_instead_of_failing_the_workspace(monkeypatch):
    """A raised failure would leave the workspace `failed`, which blocks every
    non-read request on it — for a feature whose keys are not required."""
    logged: list[str] = []

    def boom(*_args, **_kwargs):
        raise RuntimeError("codebuild BUILD phase FAILED")

    monkeypatch.setattr(infra, "ensure_skill_lab_worker", boom)
    ctx = SimpleNamespace(
        workspace=_fake_workspace({}),
        workspace_id="default",
        log=logged.append,
        record=lambda _values: pytest.fail("nothing to record on failure"),
    )
    detail = workspace_bootstrap.STAGES["skill-lab"](ctx)
    assert detail.startswith("unavailable · RuntimeError")
    assert logged and "unavailable" in logged[0]


# ── worker build context ───────────────────────────────────────────────────


@pytest.fixture
def fake_vendor(tmp_path, monkeypatch):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n")
    buildspec = tmp_path / "buildspec.yml"
    buildspec.write_text("version: 0.2\n")
    pkg = tmp_path / "skillopt"
    (pkg / "model").mkdir(parents=True)
    (pkg / "model" / "worker.py").write_text("WORKER = 1\n")
    (pkg / "__pycache__").mkdir()
    (pkg / "__pycache__" / "junk.pyc").write_bytes(b"x")
    codex_config = tmp_path / "codex-config.toml"
    codex_config.write_text('model = "openai.gpt-5.6-sol"\n')
    monkeypatch.setattr(worker_build, "WORKER_DOCKERFILE", dockerfile)
    monkeypatch.setattr(worker_build, "BUILDSPEC_TEMPLATE", buildspec)
    monkeypatch.setattr(worker_build, "WORKER_PACKAGE_DIR", pkg)
    monkeypatch.setattr(worker_build, "WORKER_CODEX_CONFIG", codex_config)
    # Keep the context independent of this host's real ~/.codex catalog.
    monkeypatch.setattr(
        worker_build,
        "get_settings",
        lambda: get_settings().model_copy(
            update={"skill_lab_codex_catalog_path": str(tmp_path / "bedrock-models.json")}
        ),
    )
    return tmp_path


def test_context_hash_stable_and_content_sensitive(fake_vendor):
    first = worker_build.context_content_hash()
    assert first == worker_build.context_content_hash()
    (fake_vendor / "skillopt" / "model" / "worker.py").write_text("WORKER = 2\n")
    assert worker_build.context_content_hash() != first
    assert worker_build.image_tag(first) == f"skill-lab-worker-{first[:12]}"


def test_assemble_worker_context_excludes_pycache(fake_vendor, tmp_path):
    target = worker_build.assemble_worker_context(tmp_path / "ctx")
    assert (target / "Dockerfile").is_file()
    assert (target / "buildspec.yml").is_file()
    assert (target / "skillopt" / "model" / "worker.py").is_file()
    assert not (target / "skillopt" / "__pycache__").exists()


def test_context_carries_codex_home_with_host_catalog(fake_vendor, tmp_path):
    """codex-home is assembled (config from the vendored tree, catalog from the
    host path) and both blobs are part of the content hash — a catalog change
    must roll the image tag or the runtime keeps a stale codex config."""
    without_catalog = worker_build.context_content_hash()
    target = worker_build.assemble_worker_context(tmp_path / "ctx")
    assert (target / "codex-home" / "config.toml").read_text().startswith("model = ")
    # No host catalog → upstream build_and_push.sh's `{}` fallback.
    catalog = target / "codex-home" / "model-catalogs" / "bedrock-models.json"
    assert catalog.read_bytes() == b"{}"

    (fake_vendor / "bedrock-models.json").write_text('{"models": []}')
    assert worker_build.context_content_hash() != without_catalog
    target = worker_build.assemble_worker_context(tmp_path / "ctx2")
    catalog = target / "codex-home" / "model-catalogs" / "bedrock-models.json"
    assert catalog.read_bytes() == b'{"models": []}'


class _FakeEcr:
    def __init__(self, tags: set[str]):
        self.tags = tags

    def describe_images(self, repositoryName, imageIds):
        tag = imageIds[0]["imageTag"]
        if tag not in self.tags:
            error = Exception("not found")
            error.response = {"Error": {"Code": "ImageNotFoundException"}}
            raise error
        return {"imageDetails": [{"imageDigest": "sha256:" + "a" * 8, "imageTags": [tag]}]}


def _fake_workspace(clients: dict, resources: dict | None = None):
    base = {
        "artifacts_bucket": "bkt",
        "codebuild_project": "launchpad-agent-builder",
        "ecr_repo": "launchpad-agents",
        "ecr_repo_uri": "111122223333.dkr.ecr.us-west-2.amazonaws.com/launchpad-agents",
    }
    base.update(resources or {})
    return SimpleNamespace(
        resources=base,
        region="us-west-2",
        account_id="111122223333",
        client=lambda service, **_kw: clients[service],
    )


def test_ensure_worker_image_skips_when_tag_exists(fake_vendor):
    tag = worker_build.image_tag()
    workspace = _fake_workspace({"ecr": _FakeEcr({tag})})
    result = worker_build.ensure_worker_image(workspace)
    assert result["tag"] == tag
    assert result["uri"].endswith("@sha256:" + "a" * 8)


def test_ensure_worker_image_builds_when_missing(fake_vendor, monkeypatch):
    tag = worker_build.image_tag()
    ecr_client = _FakeEcr(set())
    uploads: list[tuple] = []
    s3 = SimpleNamespace(upload_file=lambda archive, bucket, key: uploads.append((bucket, key)))
    codebuild = SimpleNamespace()
    started: dict = {}
    monkeypatch.setattr(
        worker_build.cb,
        "start_image_build",
        lambda client, **kw: started.update(kw) or "build/1",
    )
    monkeypatch.setattr(worker_build.cb, "wait_build", lambda client, bid, on_phase=None: None)

    def resolve(client, repo, wanted_tag):
        ecr_client.tags.add(wanted_tag)
        return "sha256:" + "b" * 8

    monkeypatch.setattr(worker_build.ecr, "resolve_digest", resolve)
    workspace = _fake_workspace({"ecr": ecr_client, "s3": s3, "codebuild": codebuild})
    result = worker_build.ensure_worker_image(workspace)
    assert uploads == [("bkt", worker_build.SOURCE_ZIP_KEY)]
    assert started["image_tag"] == tag == result["tag"]
    assert started["ecr_repo"] == "launchpad-agents"


def test_ensure_worker_image_requires_base_resources(fake_vendor):
    workspace = SimpleNamespace(resources={}, region="us-west-2", client=lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="base bootstrap"):
        worker_build.ensure_worker_image(workspace)


# ── runtime ensure ─────────────────────────────────────────────────────────


class _FakeControl:
    def __init__(self, existing=None):
        self.runtime = existing
        self.created = []
        self.updated = []

    def list_agent_runtimes(self, **_kw):
        return {"agentRuntimes": [self.runtime] if self.runtime else []}

    def get_agent_runtime(self, agentRuntimeId):
        assert self.runtime and agentRuntimeId == self.runtime["agentRuntimeId"]
        return self.runtime

    def create_agent_runtime(self, **params):
        self.created.append(params)
        self.runtime = {
            "agentRuntimeId": "rt-123",
            "agentRuntimeName": infra.WORKER_RUNTIME_NAME,
            "agentRuntimeArn": "arn:rt-123",
            "status": "READY",
            "agentRuntimeArtifact": params["agentRuntimeArtifact"],
            "environmentVariables": params.get("environmentVariables", {}),
        }
        return self.runtime

    def update_agent_runtime(self, **params):
        self.updated.append(params)
        self.runtime["agentRuntimeArtifact"] = params["agentRuntimeArtifact"]
        self.runtime["environmentVariables"] = params.get("environmentVariables", {})
        return self.runtime


@pytest.fixture
def control(monkeypatch):
    fake = _FakeControl()
    monkeypatch.setattr(
        "app.services.agentcore.client.control_client", lambda ws: fake
    )
    return fake


def _ready_runtime(uri, env):
    return {
        "agentRuntimeId": "rt-123",
        "agentRuntimeName": infra.WORKER_RUNTIME_NAME,
        "agentRuntimeArn": "arn:rt-123",
        "status": "READY",
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": uri}},
        "environmentVariables": env,
    }


def test_runtime_created_with_lifecycle_and_storage(control):
    workspace = _fake_workspace({})
    result = infra.ensure_worker_runtime(workspace, image_uri="repo@sha256:x", role_arn="arn:role")
    assert result == {"runtime_id": "rt-123", "runtime_arn": "arn:rt-123"}
    params = control.created[0]
    assert params["lifecycleConfiguration"] == infra.WORKER_LIFECYCLE
    assert params["filesystemConfigurations"] == [
        {"sessionStorage": {"mountPath": "/mnt/workspace"}}
    ]
    assert params["environmentVariables"]["CLAUDE_CODE_USE_BEDROCK"] == "1"


def test_runtime_noop_when_unchanged(control):
    workspace = _fake_workspace({})
    env = infra.worker_runtime_environment("us-west-2")
    control.runtime = _ready_runtime("repo@sha256:x", env)
    infra.ensure_worker_runtime(workspace, image_uri="repo@sha256:x", role_arn="arn:role")
    assert control.created == [] and control.updated == []


def test_runtime_matching_but_not_ready_is_waited_out(control, monkeypatch):
    """An interrupted create leaves image+env already matching; returning then
    would report a runtime that never came up as provisioned."""
    workspace = _fake_workspace({})
    env = infra.worker_runtime_environment("us-west-2")
    control.runtime = _ready_runtime("repo@sha256:x", env) | {"status": "CREATING"}
    statuses = iter(["CREATING", "READY"])
    plain_get = control.get_agent_runtime

    def flipping(agentRuntimeId):
        detail = plain_get(agentRuntimeId)
        detail["status"] = next(statuses, "READY")
        return detail

    monkeypatch.setattr(control, "get_agent_runtime", flipping)
    result = infra.ensure_worker_runtime(
        workspace, image_uri="repo@sha256:x", role_arn="arn:role"
    )
    assert result == {"runtime_id": "rt-123", "runtime_arn": "arn:rt-123"}
    assert control.created == [] and control.updated == []


def test_runtime_updated_on_image_drift(control):
    workspace = _fake_workspace({})
    env = infra.worker_runtime_environment("us-west-2")
    control.runtime = _ready_runtime("repo@sha256:OLD", env)
    infra.ensure_worker_runtime(workspace, image_uri="repo@sha256:NEW", role_arn="arn:role")
    assert control.created == []
    assert control.updated[0]["agentRuntimeArtifact"]["containerConfiguration"][
        "containerUri"
    ] == "repo@sha256:NEW"


# ── venv provisioning ──────────────────────────────────────────────────────


def test_venv_provision_and_stamp_skip(tmp_path, monkeypatch):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("boto3\n")
    venv_dir = tmp_path / "venv"
    python = venv_dir / "bin" / "python"
    monkeypatch.setattr(infra, "REQUIREMENTS_FILE", requirements)
    monkeypatch.setattr(infra, "VENV_DIR", venv_dir)
    monkeypatch.setattr(infra, "VENV_STAMP", venv_dir / ".requirements.sha256")
    monkeypatch.setattr(
        infra, "get_settings", lambda: SimpleNamespace(skill_lab_python=str(python))
    )
    monkeypatch.setattr(infra.shutil, "which", lambda name: "/usr/bin/uv")
    calls: list[list[str]] = []

    def fake_run(cmd, check):
        calls.append(cmd)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text("")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(infra.subprocess, "run", fake_run)
    assert infra.ensure_skill_lab_venv() == str(python)
    assert len(calls) == 2  # uv venv + uv pip install
    assert infra.ensure_skill_lab_venv() == str(python)
    assert len(calls) == 2  # stamped → skipped

    requirements.write_text("boto3\nnumpy\n")
    infra.ensure_skill_lab_venv()
    assert len(calls) == 4  # requirements changed → reprovisioned


def test_venv_interpreter_symlinks_resolved(tmp_path, monkeypatch):
    """A uv-managed interpreter links venv python through uv's version-less
    ALIAS dir; the judge sandbox binds only the resolved runtime dirs, so the
    alias hop dangles inside bwrap (live: prod us-east-1, 2026-08-20). The
    provisioner re-points absolute symlinks at their final target; relative
    intra-venv links stay untouched."""
    store = tmp_path / "store"
    real = store / "cpython-3.12.13" / "bin" / "python3.12"
    real.parent.mkdir(parents=True)
    real.write_text("")
    alias = store / "cpython-3.12"
    alias.symlink_to(store / "cpython-3.12.13")

    requirements = tmp_path / "requirements.txt"
    requirements.write_text("boto3\n")
    venv_dir = tmp_path / "venv"
    python = venv_dir / "bin" / "python"
    monkeypatch.setattr(infra, "REQUIREMENTS_FILE", requirements)
    monkeypatch.setattr(infra, "VENV_DIR", venv_dir)
    monkeypatch.setattr(infra, "VENV_STAMP", venv_dir / ".requirements.sha256")
    monkeypatch.setattr(
        infra, "get_settings", lambda: SimpleNamespace(skill_lab_python=str(python))
    )
    monkeypatch.setattr(infra.shutil, "which", lambda name: "/usr/bin/uv")

    def fake_run(cmd, check):
        python.parent.mkdir(parents=True, exist_ok=True)
        if not python.exists():
            python.symlink_to(alias / "bin" / "python3.12")  # through the alias
            (python.parent / "python3").symlink_to("python")  # relative link
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(infra.subprocess, "run", fake_run)
    infra.ensure_skill_lab_venv()
    import os

    assert os.readlink(python) == str(real)  # alias hop resolved away
    assert os.readlink(python.parent / "python3") == "python"  # untouched
