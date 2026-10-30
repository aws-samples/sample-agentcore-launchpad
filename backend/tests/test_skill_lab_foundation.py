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
        "skillopt/model/opencode_backend.py",
    ):
        assert (VENDOR_ROOT / rel).is_file(), f"missing {rel}"
    # Subprocess runs bytecode-compile the vendored tree on disk (that's fine —
    # the global .gitignore keeps __pycache__ out of commits); what must stay
    # clean is everything that reaches the worker image build context.
    context_names = [name for name, _ in worker_build._iter_context_files()]
    assert not [n for n in context_names if "__pycache__" in n or n.endswith(".pyc")]
    # delete-not-flag (D16): the former executors are gone from the vendored tree
    assert not (VENDOR_ROOT / "deploy" / "agentcore" / "codex-config.toml").exists()
    assert not (VENDOR_ROOT / "skillopt" / "model" / "claude_backend.py").exists()


def test_worker_cli_version_pin_matches_the_dockerfile():
    """The Dockerfile ARG is the build input; the setting only mirrors it, so the
    two copies of the pin drift silently without this. The tarball sha256 is pinned
    beside it and checked at build time (spike §9)."""
    dockerfile = (VENDOR_ROOT / "deploy" / "agentcore" / "Dockerfile").read_text()
    pin = re.search(r"^ARG OPENCODE_VERSION=(\S+)", dockerfile, re.MULTILINE)
    assert pin, "the worker Dockerfile lost its OPENCODE_VERSION pin"
    assert pin.group(1) == get_settings().skill_lab_worker_opencode_version == "1.18.35"
    assert re.search(r"^ARG OPENCODE_SHA256=[0-9a-f]{64}$", dockerfile, re.MULTILINE)
    assert "sha256sum -c" in dockerfile
    # CN image contract (D16): arm64 single-binary from the npm mirror, no Node, no
    # claude.ai installer, no public.ecr.aws / Docker Hub base
    for forbidden in ("claude.ai/install", "public.ecr.aws", "github.com", "npm install -g"):
        assert forbidden not in dockerfile, forbidden
    assert "opencode-linux-arm64" in dockerfile
    for arg in ("BASE_IMAGE", "APT_MIRROR", "PIP_INDEX_URL", "PIP_FALLBACK_INDEX_URL",
                "PIP_EXTRA_INDEX_URL", "NPM_CONFIG_REGISTRY"):
        assert f"ARG {arg}=" in dockerfile, arg
    assert "ln -s /usr/bin/rg" in dockerfile  # spike finding 2


def test_worker_dockerfile_installs_python_deps_through_uv_from_the_build_arg():
    """N13-bug1 / N11-obs25 / N11-r3-bug7: `pip install -i` swallowed the mirror's TLS
    failure as "No matching distribution found for boto3"; the deps go through uv with
    the index taken from the build args — no literal index anywhere but the ARG defaults.
    The image is only ever built inside CodeBuild, whose egress tuna 4xx-blocks, so the
    ARG defaults are the BUILD mirrors (NJU primary, Tencent fallback), never tuna and
    never Aliyun (uv-incompatible). `uv` itself is fetched with pip from the primary and,
    on failure, from the fallback; the uv step lists the primary as the preferred index
    and the fallback as the default (lowest-priority) index, then retries on the fallback
    alone — uv's `--extra-index-url` would have *out-ranked* the primary."""
    dockerfile = (VENDOR_ROOT / "deploy" / "agentcore" / "Dockerfile").read_text()
    code = "\n".join(line for line in dockerfile.splitlines() if not line.startswith("#"))
    # pip: primary || fallback
    assert re.search(
        r'pip install --no-cache-dir --index-url "\$\{PIP_INDEX_URL\}" "uv==\$\{UV_VERSION\}"'
        r'[^\n]*\\\n\s*\|\| pip install --no-cache-dir --index-url '
        r'"\$\{PIP_FALLBACK_INDEX_URL\}" "uv==\$\{UV_VERSION\}"',
        code,
    )
    assert not re.search(r"pip install[^\n]* -i ", dockerfile)
    # uv: primary preferred, fallback as the default index, opt-in extra, then retry
    block = next(b for b in code.split("\n\n") if "uv pip install" in b)  # the one RUN
    uv_steps = [s for s in block.split("||") if "uv pip install" in s]
    assert len(uv_steps) == 2, uv_steps
    first, retry = uv_steps
    assert '--index "${PIP_INDEX_URL}"' in first
    assert '--default-index "${PIP_FALLBACK_INDEX_URL}"' in first
    assert '${PIP_EXTRA_INDEX_URL:+--extra-index-url "${PIP_EXTRA_INDEX_URL}"}' in first
    assert "--extra-index-url" not in first.replace(
        '${PIP_EXTRA_INDEX_URL:+--extra-index-url "${PIP_EXTRA_INDEX_URL}"}', "")
    assert '--default-index "${PIP_FALLBACK_INDEX_URL}"' in retry
    assert "PIP_INDEX_URL" not in retry
    # the package list is shared, not duplicated
    assert "boto3 bedrock-agentcore openai pyyaml" in code
    assert code.count("boto3 bedrock-agentcore") == 1
    for line in dockerfile.splitlines():
        if "pypi" in line or "/simple" in line:
            assert line.startswith("ARG ") or line.startswith("#"), line
    assert re.search(r"^ARG PIP_INDEX_URL=https://mirror\.nju\.edu\.cn/pypi/web/simple$",
                     dockerfile, re.MULTILINE)
    assert re.search(
        r"^ARG PIP_FALLBACK_INDEX_URL=https://mirrors\.cloud\.tencent\.com/pypi/simple$",
        dockerfile, re.MULTILINE)
    assert "tuna" not in code
    assert "aliyun.com/pypi" not in code


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


# ── hub bootstrap (N10v3-bug1) ─────────────────────────────────────────────


def _stub_hub_worker(monkeypatch, logged):
    """The hub stage's AWS/host steps as cheap fakes; only the seeding is under test."""
    from app.services import bootstrap as bs

    resources = {
        "skill_lab_worker_role_arn": "arn:role/skill-lab",
        "skill_lab_worker_runtime_arn": "arn:rt/skill-lab",
        "skill_lab_worker_runtime_id": "rt-1",
        "skill_lab_worker_image_tag": "skill-lab-worker-abc",
        "skill_lab_worker_image_digest": "sha256:abc",
    }
    monkeypatch.setattr(infra, "ensure_skill_lab_worker", lambda ws, wid, log: resources)
    monkeypatch.setattr(infra, "ensure_skill_lab_venv", lambda log: "/venv/bin/python")
    return bs, resources


def test_hub_bootstrap_seeds_the_demo_samples(monkeypatch):
    """`make bootstrap` (hub path) provisioned the worker + venv but never seeded the
    vendored skilleval demo, so a fresh hub had 0 sample skills / 0 sample task sets —
    only the spoke workspace stage seeded them."""
    from app.skill_lab import samples

    logged: list[str] = []
    bs, resources = _stub_hub_worker(monkeypatch, logged)
    monkeypatch.setattr(bs, "print", lambda msg, **_kw: logged.append(msg), raising=False)
    calls: list[tuple] = []

    def fake_seed(db, workspace_id, workspace, log=lambda _m: None):
        calls.append((workspace_id, workspace.account_id, workspace.region, workspace.resources))
        log("sample task set created · sample-logtriage-tasks")
        return samples.SampleSeedResult(skills_created=3, tasksets_created=2, tasksets_present=3)

    monkeypatch.setattr(samples, "seed_skill_lab_samples", fake_seed)

    out = bs.ensure_skill_lab("111", "cn-northwest-1", {"artifacts_bucket": "bkt"})

    assert out == {**resources, "python": "/venv/bin/python"}  # resource keys unchanged
    # seeded once, on the default (hub) workspace, with the worker resources visible
    assert len(calls) == 1
    workspace_id, account, region, seen_resources = calls[0]
    assert (workspace_id, account, region) == ("default", "111", "cn-northwest-1")
    assert seen_resources["skill_lab_worker_runtime_arn"] == "arn:rt/skill-lab"
    assert seen_resources["artifacts_bucket"] == "bkt"
    # logged like the other stage lines
    assert any("3 sample skills · 2 sample task sets seeded" in line for line in logged)
    assert any("sample-logtriage-tasks" in line for line in logged)
    assert not any("WARNING" in line for line in logged)  # 3 present vs 3 present


def test_hub_bootstrap_sample_seeding_degrades_instead_of_failing(monkeypatch):
    from app.skill_lab import samples

    logged: list[str] = []
    bs, resources = _stub_hub_worker(monkeypatch, logged)
    monkeypatch.setattr(bs, "print", lambda msg, **_kw: logged.append(msg), raising=False)

    def boom(*_a, **_k):
        raise RuntimeError("registry backend unavailable")

    monkeypatch.setattr(samples, "seed_skill_lab_samples", boom)
    out = bs.ensure_skill_lab("111", "cn-northwest-1", {})
    assert out == {**resources, "python": "/venv/bin/python"}
    assert any("sample seeding skipped" in line and "RuntimeError" in line for line in logged)


def test_hub_bootstrap_warns_when_the_task_sets_fall_behind_the_skills(monkeypatch):
    """N11-r5-bug3 (live, cold host): `3 sample skills · 0 sample task sets seeded` read
    like a partial success. Fewer task sets present than skills present is a WARNING
    line; "nothing new this run" (all existing) is not."""
    from app.skill_lab import samples

    logged: list[str] = []
    bs, _resources = _stub_hub_worker(monkeypatch, logged)
    monkeypatch.setattr(bs, "print", lambda msg, **_kw: logged.append(msg), raising=False)
    monkeypatch.setattr(
        samples, "seed_skill_lab_samples",
        lambda *a, **k: samples.SampleSeedResult(skills_created=3, tasksets_present=0),
    )
    bs.ensure_skill_lab("111", "cn-northwest-1", {})
    warning = next(line for line in logged if "WARNING" in line)
    assert "0 sample task sets present vs 3 sample skills" in warning
    assert "3 task set(s) missing" in warning

    logged.clear()
    monkeypatch.setattr(
        samples, "seed_skill_lab_samples",
        lambda *a, **k: samples.SampleSeedResult(tasksets_present=3),  # all existed already
    )
    bs.ensure_skill_lab("111", "cn-northwest-1", {})
    assert any("0 sample skills · 0 sample task sets seeded" in line for line in logged)
    assert not any("WARNING" in line for line in logged)

    logged.clear()
    monkeypatch.setattr(
        samples, "seed_skill_lab_samples", lambda *a, **k: samples.SampleSeedResult(available=False)
    )
    bs.ensure_skill_lab("111", "cn-northwest-1", {})
    assert any("samples unavailable" in line for line in logged)
    assert not any("WARNING" in line for line in logged)


def test_cli_bootstrap_path_creates_the_skill_lab_tables_before_seeding(tmp_path):
    """N11-r5-bug3, reproduced the way a cold host hits it: a FRESH process that
    imports only what scripts/bootstrap.py imports (never app.main, never the Skill
    Lab router), runs the registry stage's `ensure_local_registry_backend()` (the
    first `init_db` of the run) and then seeds the samples. Before the fix the ledger
    had no `skill_lab_tasksets` table at that point and the three task sets were
    skipped with `no such table`; the in-process tests never saw it because conftest
    imports the whole API app."""
    import json
    import subprocess
    import sys

    ledger = tmp_path / "cold.db"
    config = tmp_path / "launchpad.yaml"
    config.write_text(
        "account_id: '111122223333'\n"
        "resources:\n"
        "  execution_role_arn: arn:aws-cn:iam::111122223333:role/launchpad-test\n"
        "  artifacts_bucket: launchpad-artifacts-test\n",
        encoding="utf-8",
    )
    script = f"""
import json, sys
import app.core.config as config_mod
config_mod.CONFIG_FILE = __import__("pathlib").Path({str(config)!r})
config_mod.get_settings.cache_clear()
loaded_before = sorted(m for m in sys.modules if m.startswith("app.skill_lab"))

# exactly the CLI's first ledger touch: the registry stage installs the local backend
from app.services import bootstrap as bs
bs.ensure_local_registry_backend()

from sqlalchemy import inspect
from app.core.db import engine
tables_after_init = sorted(inspect(engine).get_table_names())

# the Skill Lab stage, with only AWS (S3) faked — the registry backend is the real
# local one, the task-set rows are real rows in the fresh ledger
from app.services import aws_clients
class FakeS3:
    def upload_file(self, src, bucket, key): pass
    def delete_object(self, Bucket, Key): pass
    def delete_objects(self, Bucket, Delete, **kw): return {{"Deleted": []}}
    def get_paginator(self, op):  # N17-obs269: the register prune lists the prefix
        class _Pages:
            def paginate(self, **kw): yield {{"Contents": []}}
        return _Pages()
aws_clients.client = lambda *a, **k: FakeS3()
from app.skill_lab import tasksets as taskset_svc
taskset_svc.TASKSETS_DIR = __import__("pathlib").Path({str(tmp_path / "tasksets")!r})
from app.core.db import SessionLocal
from app.services.workspace import WorkspaceContext
from app.skill_lab import samples
lines = []
ws = WorkspaceContext(account_id="111122223333", region="cn-northwest-1",
                      resources={{"artifacts_bucket": "bkt"}})
db = SessionLocal()
try:
    detail = samples.ensure_skill_lab_samples(db, "default", ws, log=lines.append)
finally:
    db.close()
print(json.dumps({{"loaded_before": loaded_before, "tables_after_init": tables_after_init,
                  "detail": detail, "lines": lines}}))
"""
    env = {
        **dict(__import__("os").environ),
        "LAUNCHPAD_DATABASE_URL": f"sqlite:///{ledger}",
        "LAUNCHPAD_ALLOW_OPEN_CONSOLE": "true",
        "LAUNCHPAD_MODEL_PRICES_REFRESH_HOURS": "0",
        "LAUNCHPAD_ONLINE_EVAL_ENABLED": "0",
    }
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, env=env, check=False,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    # the repro is only honest if the Skill Lab models were NOT imported by the CLI path
    assert out["loaded_before"] == [], out["loaded_before"]
    assert "skill_lab_tasksets" in out["tables_after_init"]
    assert "skill_lab_jobs" in out["tables_after_init"]
    assert out["detail"] == "3 sample skills · 3 sample task sets seeded"
    assert not any("skipped" in line for line in out["lines"]), out["lines"]
    assert sum("sample task set created" in line for line in out["lines"]) == 3


def test_hub_bootstrap_seeding_is_real_and_idempotent(monkeypatch, tmp_path):
    """The REAL `ensure_skill_lab_samples` against the temp ledger, with only AWS (S3)
    stubbed and NO registry backend pre-installed: the hub stage must install the local
    backend itself (it used to work only because the registry stage ran first), the
    sample skills land as AGENT_SKILLS records, the sample task sets as rows, and a
    second run creates nothing new."""
    from app.services import aws_clients, registry_backend
    from app.skill_lab import tasksets as taskset_svc
    from app.skill_lab.models import SkillLabTaskset

    class FakeS3:
        uploaded: list[str] = []

        def upload_file(self, src, bucket, key):
            self.uploaded.append(key)

        def delete_object(self, Bucket, Key):  # noqa: N803 (boto3 kwarg names)
            pass

        def delete_objects(self, Bucket, Delete, **kw):  # noqa: N803 — boto kwargs
            for obj in Delete.get("Objects", []):
                self.delete_object(Bucket=Bucket, Key=obj["Key"])
            return {"Deleted": [{"Key": o["Key"]} for o in Delete.get("Objects", [])]}

        def get_paginator(self, op):  # N17-obs269: the register prune lists the prefix
            class _Pages:
                def paginate(self, **kw):
                    yield {"Contents": []}

            return _Pages()

    fake_s3 = FakeS3()
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: fake_s3)
    monkeypatch.setattr(taskset_svc, "TASKSETS_DIR", tmp_path / "tasksets")
    logged: list[str] = []
    bs, _resources = _stub_hub_worker(monkeypatch, logged)
    monkeypatch.setattr(bs, "print", lambda msg, **_kw: logged.append(msg), raising=False)
    assert registry_backend.backend_or_none() is None  # conftest reset: nothing installed

    def skill_names() -> list[str]:
        from tests.conftest import ws_ctx

        records = registry_backend.get_backend().list_records(
            ws_ctx({"artifacts_bucket": "bkt"}, id="default")
        )
        return sorted(r["name"] for r in records if r["name"].startswith("sample-"))

    def sample_tasksets() -> int:
        from app.core.db import SessionLocal

        db = SessionLocal()
        try:
            return db.query(SkillLabTaskset).filter(SkillLabTaskset.sample.is_(True)).count()
        finally:
            db.close()

    bs.ensure_skill_lab("111122223333", "us-west-2", {"artifacts_bucket": "bkt"})
    assert registry_backend.backend_or_none() is not None  # installed by the stage itself
    assert skill_names() == ["sample-logtriage", "sample-logtriage-v2", "sample-report"]
    assert sample_tasksets() == 3
    assert fake_s3.uploaded  # the bundles went to S3 (the only AWS touch)
    assert any("3 sample skills · 3 sample task sets seeded" in line for line in logged)

    uploads_before = len(fake_s3.uploaded)
    logged.clear()
    bs.ensure_skill_lab("111122223333", "us-west-2", {"artifacts_bucket": "bkt"})
    assert skill_names() == ["sample-logtriage", "sample-logtriage-v2", "sample-report"]
    assert sample_tasksets() == 3
    assert len(fake_s3.uploaded) == uploads_before  # nothing re-uploaded
    assert any("0 sample skills · 0 sample task sets seeded" in line for line in logged)
    assert sum("exists" in line for line in logged) == 6


# ── worker build context ───────────────────────────────────────────────────


@pytest.fixture
def fake_vendor(tmp_path, monkeypatch):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n")
    pkg = tmp_path / "skillopt"
    (pkg / "model").mkdir(parents=True)
    (pkg / "model" / "worker.py").write_text("WORKER = 1\n")
    (pkg / "__pycache__").mkdir()
    (pkg / "__pycache__" / "junk.pyc").write_bytes(b"x")
    monkeypatch.setattr(worker_build, "WORKER_DOCKERFILE", dockerfile)
    monkeypatch.setattr(worker_build, "WORKER_PACKAGE_DIR", pkg)
    return tmp_path


def test_context_hash_stable_and_content_sensitive(fake_vendor):
    ws = _fake_workspace({})
    first = worker_build.context_content_hash(ws)
    assert first == worker_build.context_content_hash(ws)
    (fake_vendor / "skillopt" / "model" / "worker.py").write_text("WORKER = 2\n")
    assert worker_build.context_content_hash(ws) != first
    assert worker_build.image_tag(first) == f"skill-lab-worker-{first[:12]}"


def test_assemble_worker_context_excludes_pycache(fake_vendor, tmp_path):
    target = worker_build.assemble_worker_context(tmp_path / "ctx", _fake_workspace({}))
    assert (target / "Dockerfile").is_file()
    assert (target / "buildspec.yml").is_file()
    assert (target / "skillopt" / "model" / "worker.py").is_file()
    assert not (target / "skillopt" / "__pycache__").exists()
    assert not (target / "codex-home").exists()


def test_buildspec_carries_the_cn_mirrors_and_the_pin(fake_vendor, tmp_path, monkeypatch):
    """The buildspec is rendered from Settings (every China mirror as a build arg,
    the OpenCode pin) and hashed into the tag — a mirror or pin change rolls the
    image; the ECR base-image mirror is used only when the operator enabled it."""
    ws = _fake_workspace({})
    target = worker_build.assemble_worker_context(tmp_path / "ctx", ws)
    spec = (target / "buildspec.yml").read_text()
    settings = get_settings()
    assert "--platform linux/arm64" in spec
    # N11-r3-bug7: the BUILD mirror + its fallback, never the host mirror
    assert settings.codebuild_pip_index_url != settings.pip_index_url
    assert f'--build-arg PIP_INDEX_URL="{settings.codebuild_pip_index_url}"' in spec
    assert (f'--build-arg PIP_FALLBACK_INDEX_URL="{settings.codebuild_pip_fallback_index_url}"'
            in spec)
    assert settings.pip_index_url not in spec
    assert '--build-arg PIP_EXTRA_INDEX_URL=""' in spec  # N11-obs25: empty unless configured
    assert f'--build-arg NPM_CONFIG_REGISTRY="{settings.npm_registry}"' in spec
    assert f'--build-arg APT_MIRROR="{settings.apt_mirror}"' in spec
    pin = settings.skill_lab_worker_opencode_version
    assert f'--build-arg OPENCODE_VERSION="{pin}"' in spec
    public = worker_build.WORKER_BASE_IMAGE_PUBLIC_MIRROR
    assert f'--build-arg BASE_IMAGE="{public}"' in spec
    before = worker_build.context_content_hash(ws)

    monkeypatch.setattr(
        worker_build,
        "get_settings",
        lambda: settings.model_copy(update={"base_image_mirror_enabled": True}),
    )
    assert worker_build.context_content_hash(ws) != before
    mirrored = worker_build.render_buildspec(ws)
    ecr_mirror = "111122223333.dkr.ecr.us-west-2.amazonaws.com/base/python:3.13-slim"
    assert f'--build-arg BASE_IMAGE="{ecr_mirror}"' in mirrored


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
        dns_suffix="amazonaws.com",
        client=lambda service, **_kw: clients[service],
    )


def test_ensure_worker_image_skips_when_tag_exists(fake_vendor):
    workspace = _fake_workspace({})
    tag = worker_build.image_tag(worker_build.context_content_hash(workspace))
    workspace = _fake_workspace({"ecr": _FakeEcr({tag})})
    result = worker_build.ensure_worker_image(workspace)
    assert result["tag"] == tag
    assert result["uri"].endswith("@sha256:" + "a" * 8)


def test_ensure_worker_image_builds_when_missing(fake_vendor, monkeypatch):
    tag = worker_build.image_tag(worker_build.context_content_hash(_fake_workspace({})))
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
            "agentRuntimeName": infra.worker_runtime_name(),
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
        "agentRuntimeName": infra.worker_runtime_name(),
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
    env = params["environmentVariables"]
    assert params["agentRuntimeName"] == "launchpad_skill_lab_worker"
    # the executor seam's worker contract (D16): no key material, the OpenAI-compatible
    # root built with the partition helper, the credential provider to exchange at
    assert env["SKILLOPT_OPENCODE_BASE_URL"].endswith("/openai/v1")
    assert env["LAUNCHPAD_MODEL_CREDENTIAL"] == "launchpad-global-bedrock"
    assert env["AWS_REGION"] == "us-west-2"
    assert "amazonaws.com" in env["SKILLOPT_OPENCODE_NO_PROXY"]
    assert not {k for k in env if "KEY" in k or "TOKEN" in k or k.startswith(("CLAUDE", "CODEX"))}


def test_runtime_noop_when_unchanged(control):
    workspace = _fake_workspace({})
    env = infra.worker_runtime_environment(_fake_workspace({}))
    control.runtime = _ready_runtime("repo@sha256:x", env)
    infra.ensure_worker_runtime(workspace, image_uri="repo@sha256:x", role_arn="arn:role")
    assert control.created == [] and control.updated == []


def test_runtime_matching_but_not_ready_is_waited_out(control, monkeypatch):
    """An interrupted create leaves image+env already matching; returning then
    would report a runtime that never came up as provisioned."""
    workspace = _fake_workspace({})
    env = infra.worker_runtime_environment(_fake_workspace({}))
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
    env = infra.worker_runtime_environment(_fake_workspace({}))
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


def test_buildspec_preflights_both_pypi_indexes_before_docker_build(fake_vendor, tmp_path):
    """N11-r3-bug7: both live worker builds died inside `docker build` with pip's opaque
    "No matching distribution found" 0.2 s after "Looking in indexes" — an immediate 4xx
    from tuna to the CodeBuild egress. The buildspec now curls `<index>/uv/` for the
    primary AND the fallback in pre_build, prints each HTTP code, and fails the build
    before `docker build` when neither index answers."""
    import yaml

    ws = _fake_workspace({})
    target = worker_build.assemble_worker_context(tmp_path / "ctx", ws)
    spec = (target / "buildspec.yml").read_text()
    settings = get_settings()
    parsed = yaml.safe_load(spec)
    pre = "\n".join(parsed["phases"]["pre_build"]["commands"])
    build = "\n".join(parsed["phases"]["build"]["commands"])
    assert "curl -sS -o /dev/null -w '%{http_code}'" in pre
    assert settings.codebuild_pip_index_url in pre
    assert settings.codebuild_pip_fallback_index_url in pre
    assert "/uv/" in pre
    assert "exit 1" in pre  # fails fast, before docker build
    assert "curl" not in build and "docker build" in build
    assert spec.index("http_code") < spec.index("docker build --platform")
