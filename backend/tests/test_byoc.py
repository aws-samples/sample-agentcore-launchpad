"""BYOC (bring your own code): zip validation, spec validators, upload endpoint,
deployer stages and the delete path — all hermetic (AWS stubbed, no pip runs)."""

import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.deployer import byoc as byoc_dep
from app.deployer.pipeline import StageContext
from app.models.ledger import Agent, Deployment
from app.routers import agents as agents_router
from app.schemas.agent import DEFAULT_MODEL_ID, AgentSpec
from app.services import byoc_uploads
from app.services.agentcore import runtime as rt
from tests.conftest import ws_ctx

ECR_IMAGE = "111122223333.dkr.ecr.us-west-2.amazonaws.com/my-agents:v1"


# ── zip building helpers ─────────────────────────────────────────────────────

def make_zip(path: Path, files: dict[str, bytes], symlink: str | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = 0o120777 << 16  # symlink mode bits
            zf.writestr(info, "target")
    return path


def zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


SDK_MAIN = (
    b"from bedrock_agentcore.runtime import BedrockAgentCoreApp\n"
    b"app = BedrockAgentCoreApp()\n"
    b"@app.entrypoint\n"
    b"def invoke(payload):\n"
    b"    return {'result': payload['prompt']}\n"
)


# ── zip validation + detection ───────────────────────────────────────────────

def test_validate_and_detect_reports_shape(tmp_path):
    path = make_zip(tmp_path / "a.zip", {
        "main.py": SDK_MAIN,
        "helper.py": b"x = 1\n",
        "requirements.txt": b"requests==2.32.3\n",
        "pkg/deep.py": b"",
    })
    report = byoc_uploads.validate_and_detect(path)
    assert report["entries_count"] == 4
    assert report["root_prefix"] == ""
    detected = report["detected"]
    assert detected["entrypoint_candidates"][0] == "main.py"
    assert detected["has_requirements"] is True
    assert detected["has_dockerfile"] is False
    assert detected["agentcore_sdk_detected"] is True


def test_validate_and_detect_normalizes_single_top_dir(tmp_path):
    path = make_zip(tmp_path / "a.zip", {
        "myagent/main.py": b"print('hi')\n",
        "myagent/Dockerfile": b"FROM python:3.13-slim\n",
    })
    report = byoc_uploads.validate_and_detect(path)
    assert report["root_prefix"] == "myagent/"
    assert report["detected"]["entrypoint_candidates"] == ["main.py"]
    assert report["detected"]["has_dockerfile"] is True
    assert report["detected"]["agentcore_sdk_detected"] is False


def test_validate_rejects_zip_slip(tmp_path):
    path = make_zip(tmp_path / "a.zip", {"../evil.py": b""})
    with pytest.raises(AppError) as err:
        byoc_uploads.validate_and_detect(path)
    assert err.value.code == "byoc.zip_entry_unsafe"


def test_validate_rejects_absolute_path(tmp_path):
    path = make_zip(tmp_path / "a.zip", {"/etc/passwd": b""})
    with pytest.raises(AppError) as err:
        byoc_uploads.validate_and_detect(path)
    assert err.value.code == "byoc.zip_entry_unsafe"


def test_validate_rejects_symlink(tmp_path):
    path = make_zip(tmp_path / "a.zip", {"main.py": b""}, symlink="link.py")
    with pytest.raises(AppError) as err:
        byoc_uploads.validate_and_detect(path)
    assert err.value.code == "byoc.zip_entry_unsafe"


def test_validate_rejects_empty_zip(tmp_path):
    path = make_zip(tmp_path / "a.zip", {})
    with pytest.raises(AppError) as err:
        byoc_uploads.validate_and_detect(path)
    assert err.value.code == "byoc.zip_empty"


def test_validate_rejects_not_a_zip(tmp_path):
    path = tmp_path / "a.zip"
    path.write_bytes(b"definitely not a zip")
    with pytest.raises(AppError) as err:
        byoc_uploads.validate_and_detect(path)
    assert err.value.code == "byoc.zip_invalid"


def test_validate_rejects_uncompressed_bomb(tmp_path, monkeypatch):
    monkeypatch.setattr(byoc_uploads, "MAX_UNCOMPRESSED_BYTES", 1024)
    path = make_zip(tmp_path / "a.zip", {"big.bin": b"0" * 4096})
    with pytest.raises(AppError) as err:
        byoc_uploads.validate_and_detect(path)
    assert err.value.code == "byoc.zip_uncompressed_too_large"


def test_validate_rejects_too_many_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(byoc_uploads, "MAX_ENTRIES", 3)
    path = make_zip(tmp_path / "a.zip", {f"f{i}.py": b"" for i in range(4)})
    with pytest.raises(AppError) as err:
        byoc_uploads.validate_and_detect(path)
    assert err.value.code == "byoc.zip_too_many_entries"


def test_extract_zip_unwraps_top_dir(tmp_path):
    path = make_zip(tmp_path / "a.zip", {
        "myagent/main.py": b"print('hi')\n",
        "myagent/sub/mod.py": b"",
    })
    root = byoc_uploads.extract_zip(path, tmp_path / "out")
    assert root == tmp_path / "out" / "myagent"
    assert (root / "main.py").is_file()
    assert (root / "sub" / "mod.py").is_file()


# ── spec validators ──────────────────────────────────────────────────────────

def _byoc_spec(**over) -> dict:
    return {
        "name": "byoc-agent",
        "method": "byoc",
        "byoc": {"artifact_kind": "code_zip", "upload_id": "u1"},
        **over,
    }


def test_byoc_spec_allows_empty_system_prompt():
    spec = AgentSpec(**_byoc_spec())
    assert spec.system_prompt == ""
    assert spec.byoc.entrypoint == "main.py"
    assert spec.byoc.python_version == "PYTHON_3_13"


def test_non_byoc_still_requires_system_prompt():
    with pytest.raises(ValidationError, match="system_prompt"):
        AgentSpec(name="a-agent", method="harness")


def test_non_byoc_refuses_byoc_block():
    with pytest.raises(ValidationError, match="byoc settings"):
        AgentSpec(
            name="a-agent", method="harness", system_prompt="s",
            byoc={"artifact_kind": "code_zip", "upload_id": "u1"},
        )


def test_byoc_requires_config_block():
    with pytest.raises(ValidationError, match="requires the byoc settings"):
        AgentSpec(name="byoc-agent", method="byoc")


def test_byoc_zip_kinds_require_upload_id():
    for kind in ("code_zip", "container_source"):
        with pytest.raises(ValidationError, match="upload_id"):
            AgentSpec(**_byoc_spec(byoc={"artifact_kind": kind}))


def test_byoc_container_image_requires_private_ecr():
    with pytest.raises(ValidationError, match="image_uri"):
        AgentSpec(**_byoc_spec(byoc={"artifact_kind": "container_image"}))
    for bad in (
        "public.ecr.aws/foo/bar:1",
        "docker.io/library/python:3.13",
        "999988887777.dkr.ecr.us-west-2.amazonaws.com/repo",  # no tag/digest
    ):
        with pytest.raises(ValidationError):
            AgentSpec(**_byoc_spec(byoc={"artifact_kind": "container_image",
                                         "image_uri": bad}))
    spec = AgentSpec(**_byoc_spec(byoc={"artifact_kind": "container_image",
                                        "image_uri": ECR_IMAGE}))
    assert spec.byoc.image_uri == ECR_IMAGE


def test_byoc_refuses_a2a_and_attachments():
    with pytest.raises(ValidationError, match="HTTP"):
        AgentSpec(**_byoc_spec(protocol="a2a"))
    for field, value in (
        ("tools", [{"type": "builtin", "name": "browser"}]),
        ("toolkits", ["hr_assistant"]),
        ("skills", ["s3://b/skills/x/"]),
        ("knowledge_bases", [{"kb_id": "KB123"}]),
    ):
        with pytest.raises(ValidationError, match="not supported by the byoc"):
            AgentSpec(**_byoc_spec(**{field: value}))
    with pytest.raises(ValidationError, match="uploaded artifact"):
        AgentSpec(**_byoc_spec(code="print('x')"))
    with pytest.raises(ValidationError, match="requirements.txt"):
        AgentSpec(**_byoc_spec(requirements=["requests==2.32.3"]))


def test_byoc_allowed_models_defaults_to_model_id():
    # backward compat: every spec written before the field existed reads back
    # as a single-model list headed by spec.model_id
    spec = AgentSpec(**_byoc_spec(model_id="global.anthropic.claude-haiku-4-5"))
    assert spec.byoc.allowed_models is None
    assert spec.allowed_model_ids == ["global.anthropic.claude-haiku-4-5"]


def test_byoc_allowed_models_sets_primary_when_model_id_omitted():
    spec = AgentSpec(**_byoc_spec(byoc={
        "artifact_kind": "code_zip", "upload_id": "u1",
        "allowed_models": ["us.model.one", "us.model.two"],
    }))
    assert spec.model_id == "us.model.one"
    assert spec.allowed_model_ids == ["us.model.one", "us.model.two"]


def test_byoc_model_id_moves_to_the_front_of_allowed_models():
    spec = AgentSpec(**_byoc_spec(
        model_id="us.model.two",
        byoc={"artifact_kind": "code_zip", "upload_id": "u1",
              "allowed_models": ["us.model.one", "us.model.two"]},
    ))
    assert spec.allowed_model_ids == ["us.model.two", "us.model.one"]


def test_byoc_model_id_must_be_in_allowed_models():
    with pytest.raises(ValidationError, match="must be one of byoc.allowed_models"):
        AgentSpec(**_byoc_spec(
            model_id="us.model.other",
            byoc={"artifact_kind": "code_zip", "upload_id": "u1",
                  "allowed_models": ["us.model.one"]},
        ))


def test_byoc_allowed_models_shape():
    base = {"artifact_kind": "code_zip", "upload_id": "u1"}
    with pytest.raises(ValidationError, match="at least 1"):
        AgentSpec(**_byoc_spec(byoc={**base, "allowed_models": []}))
    with pytest.raises(ValidationError, match="unique"):
        AgentSpec(**_byoc_spec(byoc={**base,
                                     "allowed_models": ["us.model.one", "us.model.one"]}))
    with pytest.raises(ValidationError, match="empty"):
        AgentSpec(**_byoc_spec(byoc={**base, "allowed_models": ["us.model.one", "  "]}))
    with pytest.raises(ValidationError, match="at most 20"):
        AgentSpec(**_byoc_spec(byoc={**base,
                                     "allowed_models": [f"us.model.m{i}" for i in range(21)]}))


def test_non_byoc_refuses_allowed_models():
    # allowed_models lives inside the byoc block, which every other method refuses
    with pytest.raises(ValidationError, match="byoc settings"):
        AgentSpec(
            name="a-agent", method="zip_runtime", system_prompt="s",
            byoc={"artifact_kind": "code_zip", "upload_id": "u1",
                  "allowed_models": ["us.model.one"]},
        )


def test_non_byoc_allowed_model_ids_is_the_single_model():
    spec = AgentSpec(name="a-agent", method="harness", system_prompt="s",
                     model_id="us.model.one")
    assert spec.allowed_model_ids == ["us.model.one"]


def test_byoc_entrypoint_shape():
    with pytest.raises(ValidationError, match="entrypoint"):
        AgentSpec(**_byoc_spec(byoc={"artifact_kind": "code_zip", "upload_id": "u1",
                                     "entrypoint": "../main.py"}))
    with pytest.raises(ValidationError, match="entrypoint"):
        AgentSpec(**_byoc_spec(byoc={"artifact_kind": "code_zip", "upload_id": "u1",
                                     "entrypoint": "run.sh"}))


# ── upload endpoint ──────────────────────────────────────────────────────────

class StubS3:
    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}

    def upload_file(self, filename, bucket, key):
        self.objects[(bucket, key)] = Path(filename).read_bytes()

    def download_file(self, bucket, key, filename):
        Path(filename).write_bytes(self.objects[(bucket, key)])

    def put_object(self, Bucket, Key, Body, **_kw):
        self.objects[(Bucket, Key)] = Body if isinstance(Body, bytes) else Body.encode()

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise _no_such_key()
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)


def _no_such_key():
    exc_type = type("NoSuchKey", (Exception,), {})
    return exc_type("missing")


@pytest.fixture
def stub_s3(monkeypatch):
    from app.services import workspace as workspace_mod

    s3 = StubS3()
    monkeypatch.setattr(
        workspace_mod.WorkspaceContext,
        "client",
        lambda self, name, **_kw: s3 if name == "s3" else pytest.fail(f"client {name}"),
    )
    return s3


@pytest.fixture(autouse=True)
def stub_preresolve(monkeypatch):
    """Keep the suite hermetic: the upload-time requirements pre-resolve runs
    the real uv against the package index; stand in a canned success. Tests of
    the check itself use `requirements_txt.preresolve` with a stub runner."""
    monkeypatch.setattr(
        byoc_uploads,
        "check_requirements",
        lambda path, root, python_version="PYTHON_3_13": {
            "status": "ok", "package_count": 1, "error": None
        },
    )


def test_upload_endpoint_stages_and_reads_back(client, stub_s3):
    data = zip_bytes({"main.py": SDK_MAIN, "requirements.txt": b"requests==2.32.3\n"})
    res = client.post(
        "/api/agents/uploads", files={"file": ("agent.zip", data, "application/zip")}
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["upload_id"]
    assert body["size_bytes"] == len(data)
    assert body["detected"]["agentcore_sdk_detected"] is True
    assert body["detected"]["entrypoint_candidates"] == ["main.py"]
    assert body["uploaded_by"]  # the resolved console identity
    key = ("launchpad-artifacts-test", f"byoc/default/{body['upload_id']}/source.zip")
    assert stub_s3.objects[key] == data

    detail = client.get(f"/api/agents/uploads/{body['upload_id']}")
    assert detail.status_code == 200
    assert detail.json()["sha256"] == body["sha256"]


def test_upload_endpoint_reports_the_requirements_check(client, stub_s3, monkeypatch):
    """The manifest carries the pre-resolve verdict, keyed to the python_version
    the wizard sent — a failed resolve surfaces before any deploy is attempted."""
    seen = {}

    def fake_check(path, root, python_version="PYTHON_3_13"):
        seen["python_version"] = python_version
        return {"status": "failed", "package_count": None,
                "error": "google-re2 publishes no wheel installable …"}

    monkeypatch.setattr(byoc_uploads, "check_requirements", fake_check)
    data = zip_bytes({"main.py": SDK_MAIN, "requirements.txt": b"google-re2==1.0\n"})
    res = client.post(
        "/api/agents/uploads?python_version=PYTHON_3_11",
        files={"file": ("agent.zip", data, "application/zip")},
    )
    assert res.status_code == 201, res.text
    assert seen["python_version"] == "PYTHON_3_11"
    reqs = res.json()["detected"]["requirements"]
    assert reqs["status"] == "failed"
    assert "google-re2" in reqs["error"]


def test_upload_endpoint_skips_the_check_without_requirements(client, stub_s3):
    data = zip_bytes({"main.py": SDK_MAIN})
    res = client.post(
        "/api/agents/uploads", files={"file": ("agent.zip", data, "application/zip")}
    )
    assert res.status_code == 201
    assert res.json()["detected"]["requirements"]["status"] == "skipped"


def test_upload_endpoint_refuses_unknown_python_version(client, stub_s3):
    res = client.post(
        "/api/agents/uploads?python_version=PYTHON_2_7",
        files={"file": ("agent.zip", zip_bytes({"main.py": SDK_MAIN}), "application/zip")},
    )
    assert res.status_code == 422
    assert res.json()["code"] == "byoc.invalid_python_version"


def test_upload_endpoint_refuses_non_zip(client, stub_s3):
    res = client.post(
        "/api/agents/uploads", files={"file": ("agent.tar", b"x", "application/x-tar")}
    )
    assert res.status_code == 400
    assert res.json()["code"] == "byoc.invalid_upload"


def test_upload_endpoint_refuses_invalid_archive(client, stub_s3):
    res = client.post(
        "/api/agents/uploads", files={"file": ("agent.zip", b"not a zip", "application/zip")}
    )
    assert res.status_code == 422
    assert res.json()["code"] == "byoc.zip_invalid"


def test_upload_endpoint_enforces_stream_cap(client, stub_s3, monkeypatch):
    monkeypatch.setattr(byoc_uploads, "MAX_ZIP_BYTES", 10)
    res = client.post(
        "/api/agents/uploads",
        files={"file": ("agent.zip", zip_bytes({"main.py": b"x" * 100}), "application/zip")},
    )
    assert res.status_code == 413
    assert res.json()["code"] == "byoc.upload_too_large"


def test_upload_content_length_guard(client, stub_s3):
    res = client.post(
        "/api/agents/uploads",
        content=b"x",
        headers={
            "Content-Type": "multipart/form-data; boundary=x",
            "Content-Length": str(byoc_uploads.UPLOAD_REQUEST_MAX_BYTES + 1),
        },
    )
    assert res.status_code == 413
    assert res.json()["code"] == "byoc.upload_request_too_large"


def test_upload_detail_404_for_unknown_id(client, stub_s3):
    res = client.get("/api/agents/uploads/nope123")
    assert res.status_code == 404
    assert res.json()["code"] == "byoc.upload_not_found"


def test_create_byoc_agent_via_api(client, stub_s3, monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: launched.append(jid))
    res = client.post("/api/agents", json=_byoc_spec())
    assert res.status_code == 202, res.text
    assert res.json()["agent"]["method"] == "byoc"
    assert launched


def test_create_and_redeploy_round_trip_allowed_models(client, stub_s3, monkeypatch):
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: None)
    body = _byoc_spec(byoc={
        "artifact_kind": "code_zip", "upload_id": "u1",
        "allowed_models": ["us.model.one", "us.model.two"],
    })
    res = client.post("/api/agents", json=body)
    assert res.status_code == 202, res.text
    agent = res.json()["agent"]
    assert agent["spec"]["model_id"] == "us.model.one"  # primary = first entry
    assert agent["spec"]["byoc"]["allowed_models"] == ["us.model.one", "us.model.two"]

    # the stored spec redeploys as read back, with the list edited
    db = SessionLocal()
    db.get(Agent, agent["id"]).status = "active"  # simulate the finished deploy
    db.commit()
    db.close()
    spec = agent["spec"]
    spec["byoc"]["allowed_models"] = ["us.model.two", "us.model.three"]
    spec["model_id"] = "us.model.two"
    res = client.post(f"/api/agents/{agent['id']}/redeploy", json=spec)
    assert res.status_code == 202, res.text
    stored = res.json()["agent"]["spec"]
    assert stored["model_id"] == "us.model.two"
    assert stored["byoc"]["allowed_models"] == ["us.model.two", "us.model.three"]


def test_create_refuses_model_id_outside_allowed_models(client, stub_s3):
    body = _byoc_spec(
        model_id="us.model.other",
        byoc={"artifact_kind": "code_zip", "upload_id": "u1",
              "allowed_models": ["us.model.one"]},
    )
    res = client.post("/api/agents", json=body)
    assert res.status_code == 422
    assert "allowed_models" in res.text


# ── deployer stages ──────────────────────────────────────────────────────────

RESOURCES = {
    "artifacts_bucket": "bkt",
    "execution_role_arn": "arn:role",
    "codebuild_project": "launchpad-agent-builder",
    "ecr_repo": "launchpad-agents",
}


def _mk_agent(spec: AgentSpec) -> tuple[str, str]:
    db = SessionLocal()
    agent = Agent(
        workspace_id=DEFAULT_WORKSPACE_ID, name=spec.name, method="byoc",
        status="deploying", spec=spec.model_dump(),
    )
    db.add(agent)
    db.flush()
    dep = Deployment(
        workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent.id,
        stages=[{"name": s, "status": "pending", "detail": ""}
                for s in ("generate", "package", "provision", "deploy", "register")],
    )
    db.add(dep)
    db.commit()
    ids = agent.id, dep.id
    db.close()
    return ids


def _get_agent(agent_id: str) -> Agent:
    db = SessionLocal()
    agent = db.get(Agent, agent_id)
    db.close()
    return agent


def _stage_ctx(agent_id: str, deployment_id: str, workspace) -> StageContext:
    return StageContext(
        agent_id=agent_id, deployment_id=deployment_id, job_id="j1", workspace=workspace
    )


def _client_router(monkeypatch, clients: dict):
    from app.services import workspace as workspace_mod

    monkeypatch.setattr(
        workspace_mod.WorkspaceContext,
        "client",
        lambda self, name, **_kw: clients[name],
    )


def _seed_upload(s3: StubS3, upload_id: str, files: dict[str, bytes], **manifest_over):
    data = zip_bytes(files)
    s3.objects[("bkt", f"byoc/default/{upload_id}/source.zip")] = data
    manifest = {
        "upload_id": upload_id,
        "workspace_id": "default",
        "sha256": "ab" * 32,
        "size_bytes": len(data),
        "original_filename": "agent.zip",
        "uploaded_by": "river",
        "uploaded_at": "2026-09-17T00:00:00+00:00",
        "detected": {
            "entrypoint_candidates": ["main.py"],
            "has_requirements": False,
            "has_dockerfile": "Dockerfile" in files,
            "agentcore_sdk_detected": True,
        },
        **manifest_over,
    }
    s3.objects[("bkt", f"byoc/default/{upload_id}/manifest.json")] = json.dumps(
        manifest
    ).encode()


def test_generate_stage_stamps_provenance(monkeypatch):
    s3 = StubS3()
    _client_router(monkeypatch, {"s3": s3})
    _seed_upload(s3, "u1", {"main.py": SDK_MAIN})
    spec = AgentSpec(**_byoc_spec())
    agent_id, dep_id = _mk_agent(spec)
    ctx = _stage_ctx(agent_id, dep_id, ws_ctx(RESOURCES))

    result = byoc_dep._stage_generate(ctx, _get_agent(agent_id))
    assert "code_zip" in result.detail

    stored = _get_agent(agent_id).spec["byoc"]["provenance"]
    assert stored["uploaded_by"] == "river"
    assert stored["sha256"] == "ab" * 32
    assert stored["original_filename"] == "agent.zip"


def test_package_stage_code_zip_no_requirements(monkeypatch, tmp_path):
    s3 = StubS3()
    _client_router(monkeypatch, {"s3": s3})
    _seed_upload(s3, "u1", {"main.py": SDK_MAIN, "helper.py": b"x=1\n"})
    spec = AgentSpec(**_byoc_spec())
    agent_id, dep_id = _mk_agent(spec)
    ctx = _stage_ctx(agent_id, dep_id, ws_ctx(RESOURCES))

    result = byoc_dep._stage_package(ctx, _get_agent(agent_id))
    assert "code_zip" in result.detail
    assert ctx.scratch["s3_key"] == "agents/byoc-agent/byoc_package.zip"
    packaged = s3.objects[("bkt", "agents/byoc-agent/byoc_package.zip")]
    with zipfile.ZipFile(io.BytesIO(packaged)) as zf:
        assert set(zf.namelist()) == {"main.py", "helper.py"}


def test_package_stage_missing_entrypoint_fails(monkeypatch):
    s3 = StubS3()
    _client_router(monkeypatch, {"s3": s3})
    _seed_upload(s3, "u1", {"app.py": SDK_MAIN})
    spec = AgentSpec(**_byoc_spec())  # entrypoint defaults to main.py
    agent_id, dep_id = _mk_agent(spec)
    ctx = _stage_ctx(agent_id, dep_id, ws_ctx(RESOURCES))

    with pytest.raises(RuntimeError, match="entrypoint 'main.py' not found"):
        byoc_dep._stage_package(ctx, _get_agent(agent_id))


def test_package_stage_resolves_requirements(monkeypatch):
    s3 = StubS3()
    _client_router(monkeypatch, {"s3": s3})
    _seed_upload(s3, "u1", {"main.py": SDK_MAIN, "requirements.txt": b"requests==2.32.3\n"})
    spec = AgentSpec(**_byoc_spec())
    agent_id, dep_id = _mk_agent(spec)
    ctx = _stage_ctx(agent_id, dep_id, ws_ctx(RESOURCES))

    calls = []

    def fake_resolve(src_root, build_dir, python_version, log, **_kw):
        calls.append((src_root, python_version))
        (src_root / "requests").mkdir()
        (src_root / "requests" / "__init__.py").write_text("")
        return 1

    monkeypatch.setattr(byoc_dep, "resolve_requirements_into", fake_resolve)
    result = byoc_dep._stage_package(ctx, _get_agent(agent_id))
    assert calls and calls[0][1] == "PYTHON_3_13"
    assert "1 deps resolved" in result.detail
    packaged = s3.objects[("bkt", "agents/byoc-agent/byoc_package.zip")]
    with zipfile.ZipFile(io.BytesIO(packaged)) as zf:
        assert "requests/__init__.py" in zf.namelist()


def test_resolve_requirements_pip_args(tmp_path):
    """The install resolves for the runtime target, never for this host."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "requirements.txt").write_text("requests==2.32.3\n")
    build = tmp_path / "build"
    build.mkdir()
    commands = []

    def runner(args, **_kw):
        commands.append(args)
        if "compile" in args:
            out = args[args.index("-o") + 1]
            Path(out).write_text("requests==2.32.3 \\\n  --hash=sha256:deadbeef\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    count = byoc_dep.resolve_requirements_into(
        src, build, "PYTHON_3_11", lambda _m: None, pip_runner=runner
    )
    assert count == 1
    compile_cmd = commands[0]
    # the resolve targets the member's python and the configured platform…
    assert compile_cmd[compile_cmd.index("--python-version") + 1] == "3.11"
    assert "aarch64-manylinux_2_28" in compile_cmd
    install = commands[-1]
    assert "--require-hashes" in install
    # …and the install carries the full tag ladder down to manylinux2014: pip
    # does not widen --platform itself, and most wheels are tagged 2_17.
    assert "manylinux_2_28_aarch64" in install
    assert "manylinux2014_aarch64" in install
    assert install[install.index("--python-version") + 1] == "3.11"
    assert (src / "requirements.lock").exists()


def test_resolve_requirements_refuses_boundary_violations(tmp_path):
    """An uploaded requirements.txt cannot pull from outside the platform index."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "requirements.txt").write_text("--extra-index-url https://mirror.example\n")

    def never(args, **_kw):  # pragma: no cover - must not be reached
        raise AssertionError("no subprocess may run for a refused file")

    with pytest.raises(RuntimeError, match="requirements.txt was refused"):
        byoc_dep.resolve_requirements_into(
            src, tmp_path / "build", "PYTHON_3_13", lambda _m: None, pip_runner=never
        )


def test_package_stage_container_source_uses_codebuild(monkeypatch):
    s3 = StubS3()

    class StubCodeBuild:
        def start_build(self, **kwargs):
            self.started = kwargs
            return {"build": {"id": "b-1"}}

        def batch_get_builds(self, ids):
            return {"builds": [{"id": ids[0], "currentPhase": "COMPLETED",
                                "buildStatus": "SUCCEEDED", "phases": []}]}

    class StubEcr:
        def describe_images(self, repositoryName, imageIds):
            return {"imageDetails": [{"imageDigest": "sha256:" + "0" * 64}]}

    codebuild = StubCodeBuild()
    _client_router(monkeypatch, {"s3": s3, "codebuild": codebuild, "ecr": StubEcr()})
    monkeypatch.setattr(
        "app.deployer.container.get_settings",
        lambda: SimpleNamespace(image_scan_enabled=False),
    )
    _seed_upload(s3, "u2", {"Dockerfile": b"FROM python:3.13-slim\n", "main.py": SDK_MAIN})
    spec = AgentSpec(**_byoc_spec(
        byoc={"artifact_kind": "container_source", "upload_id": "u2"}
    ))
    agent_id, dep_id = _mk_agent(spec)
    ctx = _stage_ctx(agent_id, dep_id, ws_ctx(RESOURCES))

    result = byoc_dep._stage_package(ctx, _get_agent(agent_id))
    assert "codebuild" in result.detail
    assert codebuild.started["projectName"] == "launchpad-agent-builder"
    assert ("bkt", "builds/byoc-agent/source.zip") in s3.objects
    assert ctx.scratch["image_uri"].endswith("@sha256:" + "0" * 64)


def test_package_stage_container_source_ships_platform_buildspec(monkeypatch):
    """The CodeBuild source zip must carry the PLATFORM buildspec — a
    buildspec.yml inside the upload is overwritten, never executed."""
    s3 = StubS3()

    class StubCodeBuild:
        def start_build(self, **kwargs):
            return {"build": {"id": "b-1"}}

        def batch_get_builds(self, ids):
            return {"builds": [{"id": ids[0], "currentPhase": "COMPLETED",
                                "buildStatus": "SUCCEEDED", "phases": []}]}

    class StubEcr:
        def describe_images(self, repositoryName, imageIds):
            return {"imageDetails": [{"imageDigest": "sha256:" + "0" * 64}]}

    _client_router(monkeypatch, {"s3": s3, "codebuild": StubCodeBuild(), "ecr": StubEcr()})
    monkeypatch.setattr(
        "app.deployer.container.get_settings",
        lambda: SimpleNamespace(image_scan_enabled=False),
    )
    _seed_upload(s3, "u2", {
        "Dockerfile": b"FROM python:3.13-slim\n",
        "main.py": SDK_MAIN,
        "buildspec.yml": b"version: 0.2\nphases:\n  build:\n    commands:\n      - evil\n",
    })
    spec = AgentSpec(**_byoc_spec(
        byoc={"artifact_kind": "container_source", "upload_id": "u2"}
    ))
    agent_id, dep_id = _mk_agent(spec)
    ctx = _stage_ctx(agent_id, dep_id, ws_ctx(RESOURCES))

    byoc_dep._stage_package(ctx, _get_agent(agent_id))
    source = s3.objects[("bkt", "builds/byoc-agent/source.zip")]
    with zipfile.ZipFile(io.BytesIO(source)) as zf:
        shipped = zf.read("buildspec.yml")
    from app.deployer.container import platform_buildspec_path

    assert shipped == platform_buildspec_path().read_bytes()
    assert b"evil" not in shipped


def test_package_stage_container_source_requires_dockerfile(monkeypatch):
    s3 = StubS3()
    _client_router(monkeypatch, {"s3": s3})
    _seed_upload(s3, "u2", {"main.py": SDK_MAIN})
    spec = AgentSpec(**_byoc_spec(
        byoc={"artifact_kind": "container_source", "upload_id": "u2"}
    ))
    agent_id, dep_id = _mk_agent(spec)
    ctx = _stage_ctx(agent_id, dep_id, ws_ctx(RESOURCES))

    with pytest.raises(RuntimeError, match="no Dockerfile"):
        byoc_dep._stage_package(ctx, _get_agent(agent_id))


def test_package_stage_container_image_skips(monkeypatch):
    spec = AgentSpec(**_byoc_spec(
        byoc={"artifact_kind": "container_image", "image_uri": ECR_IMAGE}
    ))
    agent_id, dep_id = _mk_agent(spec)
    ctx = _stage_ctx(agent_id, dep_id, ws_ctx(RESOURCES))

    result = byoc_dep._stage_package(ctx, _get_agent(agent_id))
    assert result.skipped
    assert ctx.scratch["image_uri"] == ECR_IMAGE


def test_generate_stage_container_image_verifies_account(monkeypatch):
    class StubEcr:
        def describe_images(self, repositoryName, imageIds):
            assert repositoryName == "my-agents"
            assert imageIds == [{"imageTag": "v1"}]
            return {"imageDetails": [{"imageDigest": "sha256:" + "1" * 64,
                                      "imagePushedAt": None}]}

    _client_router(monkeypatch, {"ecr": StubEcr()})
    spec = AgentSpec(**_byoc_spec(
        byoc={"artifact_kind": "container_image", "image_uri": ECR_IMAGE}
    ))
    agent_id, dep_id = _mk_agent(spec)
    ctx = _stage_ctx(agent_id, dep_id, ws_ctx(RESOURCES))

    result = byoc_dep._stage_generate(ctx, _get_agent(agent_id))
    assert "container_image" in result.detail
    assert _get_agent(agent_id).spec["byoc"]["provenance"]["sha256"].startswith("sha256:")


def test_describe_image_refuses_other_account():
    other = "999988887777.dkr.ecr.us-west-2.amazonaws.com/repo:tag"
    with pytest.raises(RuntimeError, match="this workspace deploys from"):
        byoc_dep.describe_image(ws_ctx(RESOURCES), other, ecr_client=object())


class StubRuntimeControl:
    def __init__(self):
        self.created_with = None
        self.updated_with = None
        self.deleted = []
        self.exceptions = SimpleNamespace(
            ResourceNotFoundException=type("ResourceNotFoundException", (Exception,), {})
        )

    def create_agent_runtime(self, **kwargs):
        self.created_with = kwargs
        return {"agentRuntimeId": "rt-1", "agentRuntimeArn": "arn:rt-1",
                "agentRuntimeVersion": "1", "status": "CREATING"}

    def update_agent_runtime(self, **kwargs):
        self.updated_with = kwargs
        return {"agentRuntimeId": kwargs["agentRuntimeId"], "agentRuntimeArn": "arn:rt-1",
                "agentRuntimeVersion": "2", "status": "UPDATING"}

    def get_agent_runtime(self, agentRuntimeId):
        return {"agentRuntimeId": agentRuntimeId, "agentRuntimeArn": "arn:rt-1",
                "agentRuntimeVersion": "1", "status": "READY"}

    def delete_agent_runtime(self, agentRuntimeId):
        self.deleted.append(agentRuntimeId)


def test_deploy_stage_code_zip_payload(monkeypatch):
    stub = StubRuntimeControl()
    monkeypatch.setattr(byoc_dep, "control_client", lambda _ws=None: stub)
    spec = AgentSpec(**_byoc_spec(
        byoc={"artifact_kind": "code_zip", "upload_id": "u1",
              "entrypoint": "serve.py", "python_version": "PYTHON_3_11"},
        env={"MODEL_ID": "custom.model"},
        memory={"short_term": False, "long_term": False},
    ))
    agent_id, dep_id = _mk_agent(spec)
    ctx = _stage_ctx(agent_id, dep_id, ws_ctx(RESOURCES))
    ctx.scratch.update({
        "s3_bucket": "bkt", "s3_key": "agents/byoc-agent/byoc_package.zip",
        "execution_role_arn": "arn:agent-role",
    })

    result = byoc_dep._stage_deploy(ctx, _get_agent(agent_id))
    assert result.detail.startswith("READY")
    cfg = stub.created_with["agentRuntimeArtifact"]["codeConfiguration"]
    assert cfg["runtime"] == "PYTHON_3_11"
    # no ADOT launcher: user zips don't vendor opentelemetry-instrument
    assert cfg["entryPoint"] == ["serve.py"]
    assert cfg["code"]["s3"] == {"bucket": "bkt",
                                 "prefix": "agents/byoc-agent/byoc_package.zip"}
    assert stub.created_with["roleArn"] == "arn:agent-role"
    # a user-provided MODEL_ID wins over the spec.model_id injection
    assert stub.created_with["environmentVariables"] == {
        "MODEL_ID": "custom.model",
        "ALLOWED_MODEL_IDS": DEFAULT_MODEL_ID,  # no list ⇒ the primary alone
    }
    assert _get_agent(agent_id).resource_id == "rt-1"


def test_deploy_stage_injects_model_id_when_env_has_none(monkeypatch):
    # The execution role permits only spec.model_id, so the deployer must tell
    # the user code which model it may call.
    stub = StubRuntimeControl()
    monkeypatch.setattr(byoc_dep, "control_client", lambda _ws=None: stub)
    spec = AgentSpec(**_byoc_spec(
        model_id="global.anthropic.claude-haiku-4-5",
        memory={"short_term": False, "long_term": False},
    ))
    agent_id, dep_id = _mk_agent(spec)
    ctx = _stage_ctx(agent_id, dep_id, ws_ctx(RESOURCES))
    ctx.scratch.update({
        "s3_bucket": "bkt", "s3_key": "agents/byoc-agent/byoc_package.zip",
        "execution_role_arn": "arn:agent-role",
    })

    byoc_dep._stage_deploy(ctx, _get_agent(agent_id))
    env = stub.created_with["environmentVariables"]
    assert env["MODEL_ID"] == "global.anthropic.claude-haiku-4-5"
    # without an allowed_models list the union is just the primary
    assert env["ALLOWED_MODEL_IDS"] == "global.anthropic.claude-haiku-4-5"


def test_deploy_stage_injects_allowed_model_ids_list(monkeypatch):
    stub = StubRuntimeControl()
    monkeypatch.setattr(byoc_dep, "control_client", lambda _ws=None: stub)
    spec = AgentSpec(**_byoc_spec(
        byoc={"artifact_kind": "code_zip", "upload_id": "u1",
              "allowed_models": ["us.model.one", "us.model.two"]},
        memory={"short_term": False, "long_term": False},
    ))
    agent_id, dep_id = _mk_agent(spec)
    ctx = _stage_ctx(agent_id, dep_id, ws_ctx(RESOURCES))
    ctx.scratch.update({
        "s3_bucket": "bkt", "s3_key": "agents/byoc-agent/byoc_package.zip",
        "execution_role_arn": "arn:agent-role",
    })

    byoc_dep._stage_deploy(ctx, _get_agent(agent_id))
    env = stub.created_with["environmentVariables"]
    assert env["MODEL_ID"] == "us.model.one"  # primary = allowed_models[0]
    assert env["ALLOWED_MODEL_IDS"] == "us.model.one,us.model.two"


def test_deploy_stage_user_allowed_model_ids_wins(monkeypatch):
    stub = StubRuntimeControl()
    monkeypatch.setattr(byoc_dep, "control_client", lambda _ws=None: stub)
    spec = AgentSpec(**_byoc_spec(
        byoc={"artifact_kind": "code_zip", "upload_id": "u1",
              "allowed_models": ["us.model.one", "us.model.two"]},
        env={"ALLOWED_MODEL_IDS": "my,own,list"},
        memory={"short_term": False, "long_term": False},
    ))
    agent_id, dep_id = _mk_agent(spec)
    ctx = _stage_ctx(agent_id, dep_id, ws_ctx(RESOURCES))
    ctx.scratch.update({
        "s3_bucket": "bkt", "s3_key": "agents/byoc-agent/byoc_package.zip",
        "execution_role_arn": "arn:agent-role",
    })

    byoc_dep._stage_deploy(ctx, _get_agent(agent_id))
    env = stub.created_with["environmentVariables"]
    assert env["ALLOWED_MODEL_IDS"] == "my,own,list"
    assert env["MODEL_ID"] == "us.model.one"  # injection of the primary is independent


def test_deploy_stage_container_image_payload(monkeypatch):
    stub = StubRuntimeControl()
    monkeypatch.setattr(byoc_dep, "control_client", lambda _ws=None: stub)
    spec = AgentSpec(**_byoc_spec(
        byoc={"artifact_kind": "container_image", "image_uri": ECR_IMAGE},
        memory={"short_term": False, "long_term": False},
    ))
    agent_id, dep_id = _mk_agent(spec)
    ctx = _stage_ctx(agent_id, dep_id, ws_ctx(RESOURCES))
    ctx.scratch["execution_role_arn"] = "arn:agent-role"

    byoc_dep._stage_deploy(ctx, _get_agent(agent_id))
    artifact = stub.created_with["agentRuntimeArtifact"]
    assert artifact == {"containerConfiguration": {"containerUri": ECR_IMAGE}}


def test_deploy_stage_update_mode_publishes_new_version(monkeypatch):
    stub = StubRuntimeControl()
    monkeypatch.setattr(byoc_dep, "control_client", lambda _ws=None: stub)
    spec = AgentSpec(**_byoc_spec(memory={"short_term": False, "long_term": False}))
    agent_id, dep_id = _mk_agent(spec)
    db = SessionLocal()
    db.get(Agent, agent_id).resource_id = "rt-1"
    db.commit()
    db.close()
    ctx = _stage_ctx(agent_id, dep_id, ws_ctx(RESOURCES))
    ctx.scratch.update({"mode": "update", "execution_role_arn": "arn:agent-role"})

    byoc_dep._stage_deploy(ctx, _get_agent(agent_id))
    assert stub.updated_with["agentRuntimeId"] == "rt-1"
    cfg = stub.updated_with["agentRuntimeArtifact"]["codeConfiguration"]
    assert cfg["entryPoint"] == ["main.py"]


# ── delete path ──────────────────────────────────────────────────────────────

def test_delete_removes_runtime_upload_and_images(monkeypatch):
    stub = StubRuntimeControl()
    monkeypatch.setattr(byoc_dep, "control_client", lambda _ws=None: stub)
    s3 = StubS3()
    s3.objects[("bkt", "byoc/default/u2/source.zip")] = b"z"
    s3.objects[("bkt", "byoc/default/u2/manifest.json")] = b"{}"

    class StubEcr:
        def __init__(self):
            self.deleted = None

        def batch_delete_image(self, repositoryName, imageIds):
            self.deleted = (repositoryName, imageIds)

    ecr = StubEcr()
    _client_router(monkeypatch, {"s3": s3, "ecr": ecr})
    spec = AgentSpec(**_byoc_spec(
        byoc={"artifact_kind": "container_source", "upload_id": "u2"}
    ))
    agent = Agent(
        workspace_id=DEFAULT_WORKSPACE_ID, name="byoc-agent", method="byoc",
        status="active", spec=spec.model_dump(), resource_id="rt-1", version="2",
    )

    byoc_dep.delete_agent_resources(agent, ws_ctx(RESOURCES), ecr_client=ecr)
    assert stub.deleted == ["rt-1"]
    assert ("bkt", "byoc/default/u2/source.zip") not in s3.objects
    assert ("bkt", "byoc/default/u2/manifest.json") not in s3.objects
    assert ecr.deleted == ("launchpad-agents",
                           [{"imageTag": "byoc-agent-v1"}, {"imageTag": "byoc-agent-v2"}])


def test_delete_code_zip_skips_ecr(monkeypatch):
    stub = StubRuntimeControl()
    monkeypatch.setattr(byoc_dep, "control_client", lambda _ws=None: stub)
    s3 = StubS3()
    s3.objects[("bkt", "byoc/default/u1/source.zip")] = b"z"
    _client_router(monkeypatch, {"s3": s3})
    spec = AgentSpec(**_byoc_spec())
    agent = Agent(
        workspace_id=DEFAULT_WORKSPACE_ID, name="byoc-agent", method="byoc",
        status="active", spec=spec.model_dump(), resource_id="rt-1",
    )

    byoc_dep.delete_agent_resources(agent, ws_ctx(RESOURCES))
    assert stub.deleted == ["rt-1"]
    assert ("bkt", "byoc/default/u1/source.zip") not in s3.objects


def test_router_delete_dispatches_byoc(monkeypatch):
    calls = []
    monkeypatch.setattr(agents_router.byoc_method, "delete_agent_resources",
                        lambda agent, ws: calls.append(agent.id))
    monkeypatch.setattr(agents_router.agent_iam, "delete_execution_role",
                        lambda *a, **k: True)
    agent = Agent(id="x1", workspace_id=DEFAULT_WORKSPACE_ID, name="byoc-agent",
                  method="byoc", status="active", spec={})
    assert agents_router._delete_agent_resources(agent, ws_ctx(RESOURCES)) is True
    assert calls == ["x1"]


# ── capability projections ───────────────────────────────────────────────────

def test_byoc_capabilities_degrade_as_custom_source():
    from app.optimization.service import canary_capability, experiment_capability

    agent = SimpleNamespace(
        method="byoc", status="active", arn="arn:aws:...:runtime/rt-1",
        spec=_byoc_spec(), system_key=None,
    )
    exp = experiment_capability(agent)
    assert exp["eligible"] is False
    assert exp["reason_code"] == "custom-source-unverified"
    can = canary_capability(agent)
    assert can["eligible"] is False
    assert can["reason_code"] == "custom-source-unverified"


def test_create_code_runtime_default_contract_unchanged():
    """Platform zips keep PYTHON_3_13 + opentelemetry-instrument main.py."""
    stub = StubRuntimeControl()
    rt.create_code_runtime(
        stub, runtime_name="a_1", s3_bucket="b", s3_key="k", role_arn="r"
    )
    cfg = stub.created_with["agentRuntimeArtifact"]["codeConfiguration"]
    assert cfg["runtime"] == "PYTHON_3_13"
    assert cfg["entryPoint"] == ["opentelemetry-instrument", "main.py"]
