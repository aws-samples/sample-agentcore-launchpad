"""GET /api/agents/{id}/source — the read-only CODE view.

Hermetic: the control client and S3 are stubs; S3 serves in-memory zips and
honours ``Range``. The properties asserted: each method reads its own source
(the Runtime's deployed package, the 方式A build context, the BYOC upload, a
Harness's configuration), a generated package lists only the platform's files and
is read with ranged GETs, contents respect the per-file / per-response caps with
``?path=`` for the rest, the Harness projection is allow-listed and masked, and
rows with no source are refused with a reason.
"""

import io
import json
import zipfile
from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
from botocore.exceptions import ClientError

import app.routers.agents as agents_router
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent, Deployment
from app.services import agent_source
from app.services.workspace import WorkspaceContext

BUCKET = "launchpad-artifacts-test"
RUNTIME_ID = "hr_assistant-AbCdEf1234"
HARNESS_ID = "hr-harness-XyZ987"
MODIFIED = datetime(2026, 10, 20, 9, 30, tzinfo=UTC)
MAIN_PY = "from bedrock_agentcore import BedrockAgentCoreApp\napp = BedrockAgentCoreApp()\n"


def _zip(files: Mapping[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


class StubS3:
    """In-memory objects; get_object honours ``Range`` and records every call."""

    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects
        self.gets: list[dict] = []

    def head_object(self, Bucket, Key, **kw):  # noqa: N803 — boto3 kwargs
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        return {"ContentLength": len(self.objects[Key]), "LastModified": MODIFIED}

    def get_object(self, Bucket, Key, Range=None, **kw):  # noqa: N803
        self.gets.append({"Key": Key, "Range": Range})
        data = self.objects.get(Key)
        if data is None:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject")
        if Range:
            start, end = (int(p) for p in Range.removeprefix("bytes=").split("-"))
            data = data[start : end + 1]
        return {"Body": io.BytesIO(data)}


class StubControl:
    def __init__(self, runtime: dict | None = None, harness: dict | None = None):
        self.runtime, self.harness = runtime, harness
        self.calls: list[str] = []

    def get_agent_runtime(self, **kw):
        self.calls.append("get_agent_runtime")
        if self.runtime is None:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "GetAgentRuntime")
        return self.runtime

    def get_harness(self, **kw):
        self.calls.append("get_harness")
        if self.harness is None:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "GetHarness")
        return {"harness": self.harness}


@pytest.fixture
def aws(monkeypatch):
    def install(*, objects=None, runtime=None, harness=None):
        s3 = StubS3(dict(objects or {}))
        control = StubControl(runtime, harness)
        monkeypatch.setattr(agents_router, "control_client", lambda _ws=None: control)
        monkeypatch.setattr(WorkspaceContext, "client", lambda self, service, **kw: s3)
        return s3, control

    return install


def _seed(
    *,
    method: str,
    name: str = "hr-assistant",
    resource_id: str | None = RUNTIME_ID,
    status: str = "active",
    spec: dict | None = None,
    workspace_id: str = DEFAULT_WORKSPACE_ID,
    deployment: str | None = None,
) -> str:
    db = SessionLocal()
    try:
        row = Agent(
            workspace_id=workspace_id,
            name=name,
            method=method,
            status=status,
            resource_id=resource_id,
            version="3",
            spec=spec or {},
        )
        db.add(row)
        db.flush()
        if deployment:
            db.add(Deployment(workspace_id=workspace_id, agent_id=row.id, status=deployment))
        db.commit()
        return row.id
    finally:
        db.close()


def _runtime(key: str = "agents/hr-assistant/deployment_package.zip", **extra) -> dict:
    return {
        "agentRuntimeId": RUNTIME_ID,
        "agentRuntimeVersion": "3",
        "agentRuntimeArtifact": {
            "codeConfiguration": {
                "code": {"s3": {"bucket": BUCKET, "prefix": key}},
                "runtime": "PYTHON_3_13",
                "entryPoint": ["opentelemetry-instrument", "main.py"],
            }
        },
        **extra,
    }


def _deployed_package(extra: dict[str, bytes | str] | None = None) -> bytes:
    files: dict[str, bytes | str] = {
        # pip-installed dependencies dominate a real package
        **{f"strands/mod_{i}.py": f"# dependency {i}\n" * 50 for i in range(300)},
        "boto3/__init__.py": "# boto3\n",
        "strands-1.47.0.dist-info/METADATA": "Name: strands\n",
        "main.py": MAIN_PY,
        "requirements.txt": "strands-agents[otel]>=1.0\n",
        "requirements.lock": "strands-agents==1.47.0 --hash=sha256:abc\n",
        "skills/hr-policy/SKILL.md": "---\nname: hr-policy\n---\n",
        "__pycache__/main.cpython-313.pyc": b"\x00\x01",
    }
    files.update(extra or {})
    return _zip(files)


# ─── generated packages (Strands template / Studio / converted twins) ───────────
@pytest.mark.parametrize("method", ["zip_runtime", "studio"])
def test_generated_package_lists_only_platform_files(client, aws, method):
    package = _deployed_package()
    s3, control = aws(
        objects={"agents/hr-assistant/deployment_package.zip": package}, runtime=_runtime()
    )
    agent_id = _seed(method=method, deployment="succeeded")

    res = client.get(f"/api/agents/{agent_id}/source")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["kind"] == "code"
    assert body["origin"] == "deployed_package"
    assert body["entrypoint"] == "main.py"
    assert body["runtime_version"] == "3"
    assert body["deployment_status"] == "succeeded"
    assert body["package_updated_at"] == MODIFIED.isoformat()
    assert [f["path"] for f in body["files"]] == [
        "main.py",
        "requirements.txt",
        "requirements.lock",
        "skills/hr-policy/SKILL.md",
    ]
    assert body["files"][0]["content"] == MAIN_PY
    assert body["files_total"] == 4
    assert control.calls == ["get_agent_runtime"]
    # the package is read by range, never downloaded whole
    assert s3.gets and all(g["Range"] for g in s3.gets)
    fetched = sum(
        int(end) - int(start) + 1
        for start, end in (g["Range"].removeprefix("bytes=").split("-") for g in s3.gets)
    )
    assert fetched < len(package)


def test_converted_twin_lists_its_bundle_files(client, aws):
    package = _deployed_package({"agent_lib/tools.py": "def tool(): ...\n"})
    aws(objects={"agents/hr-assistant/deployment_package.zip": package}, runtime=_runtime())
    spec = {"code_bundle": {"main.py": MAIN_PY, "agent_lib/tools.py": "def tool(): ...\n"}}
    agent_id = _seed(method="zip_runtime", spec=spec)

    body = client.get(f"/api/agents/{agent_id}/source").json()
    paths = [f["path"] for f in body["files"]]
    assert paths[:2] == ["main.py", "agent_lib/tools.py"]
    assert "strands/mod_0.py" not in paths


def test_caps_budget_and_single_file_read(client, aws, monkeypatch):
    monkeypatch.setattr(agent_source, "MAX_FILE_BYTES", 64)
    # main.py's 64 shown bytes + requirements.txt's 40 leave 6: the lock no longer fits
    monkeypatch.setattr(agent_source, "MAX_TOTAL_BYTES", 110)
    package = _deployed_package(
        {
            "main.py": "x = 1\n" * 40,  # 240 bytes → truncated at 64
            "requirements.txt": "a\n" * 20,  # 40 bytes, fits
            "requirements.lock": "b\n" * 40,  # 80 bytes, past the remaining budget
            "skills/hr-policy/logo.png": b"\x89PNG\x00",
        }
    )
    aws(objects={"agents/hr-assistant/deployment_package.zip": package}, runtime=_runtime())
    agent_id = _seed(method="zip_runtime")

    files = {f["path"]: f for f in client.get(f"/api/agents/{agent_id}/source").json()["files"]}
    assert files["main.py"]["truncated"] is True
    assert files["main.py"]["content"] == ("x = 1\n" * 40)[:64]
    assert files["requirements.txt"]["content"] == "a\n" * 20
    assert files["requirements.lock"]["omitted"] == "budget"
    assert files["requirements.lock"]["content"] is None
    assert files["skills/hr-policy/logo.png"]["omitted"] == "binary"

    one = client.get(f"/api/agents/{agent_id}/source", params={"path": "requirements.lock"})
    assert one.status_code == 200, one.text
    [lock] = one.json()["files"]
    assert lock["path"] == "requirements.lock"
    assert lock["content"] == ("b\n" * 40)[:64]
    missing = client.get(f"/api/agents/{agent_id}/source", params={"path": "strands/mod_0.py"})
    assert missing.status_code == 404
    assert missing.json()["code"] == "agent.source_file_not_found"


def test_failed_last_deploy_is_reported(client, aws):
    aws(
        objects={"agents/hr-assistant/deployment_package.zip": _deployed_package()},
        runtime=_runtime(),
    )
    agent_id = _seed(method="zip_runtime", deployment="failed")
    assert client.get(f"/api/agents/{agent_id}/source").json()["deployment_status"] == "failed"


def test_missing_package_is_409(client, aws):
    aws(objects={}, runtime=_runtime())
    agent_id = _seed(method="zip_runtime")
    res = client.get(f"/api/agents/{agent_id}/source")
    assert res.status_code == 409
    assert res.json()["code"] == "agent.source_unavailable"
    assert res.json()["detail"]["reason"] == "gone"


def test_runtime_gone_is_409(client, aws):
    aws(runtime=None)
    res = client.get(f"/api/agents/{_seed(method='zip_runtime')}/source")
    assert res.status_code == 409
    assert res.json()["code"] == "agent.source_unavailable"


# ─── 方式A build context ─────────────────────────────────────────────────────────
def test_container_method_reads_the_build_context(client, aws):
    context = _zip(
        {
            "Dockerfile": "FROM python:3.13\n",
            "main.py": MAIN_PY,
            "requirements.txt": "claude-agent-sdk\n",
            ".claude/skills/hr-policy/SKILL.md": "# skill\n",
        }
    )
    _, control = aws(objects={"builds/hr-assistant/source.zip": context})
    body = client.get(f"/api/agents/{_seed(method='container')}/source").json()
    assert body["origin"] == "build_context"
    assert [f["path"] for f in body["files"]][:3] == ["main.py", "Dockerfile", "requirements.txt"]
    assert control.calls == []


# ─── BYOC ───────────────────────────────────────────────────────────────────────
def _byoc_spec(kind: str, upload_id: str | None = "up1", **extra) -> dict:
    return {
        "byoc": {
            "artifact_kind": kind,
            "upload_id": upload_id,
            "entrypoint": "agent.py",
            "provenance": {
                "sha256": "f" * 64,
                "size_bytes": 1234,
                "original_filename": "agent.zip",
                "uploaded_by": "hr-demo",
                "uploaded_at": "2026-10-20T08:00:00+00:00",
            },
            **extra,
        }
    }


def _upload_objects(upload_id: str, files: Mapping[str, bytes | str], root: str) -> dict:
    prefix = f"byoc/{DEFAULT_WORKSPACE_ID}/{upload_id}"
    return {
        f"{prefix}/source.zip": _zip(files),
        f"{prefix}/manifest.json": json.dumps({"root_prefix": root}).encode(),
    }


def test_byoc_code_zip_shows_the_members_upload(client, aws):
    files = {
        "myagent/agent.py": MAIN_PY,
        "myagent/util/helpers.py": "def helper(): ...\n",
        "myagent/requirements.txt": "boto3\n",
        "myagent/__pycache__/agent.cpython-313.pyc": b"\x00",
        "myagent/.git/config": "[core]\n",
    }
    aws(objects=_upload_objects("up1", files, "myagent/"))
    agent_id = _seed(method="byoc", spec=_byoc_spec("code_zip"))

    body = client.get(f"/api/agents/{agent_id}/source").json()
    assert body["origin"] == "upload"
    assert body["artifact_kind"] == "code_zip"
    assert body["entrypoint"] == "agent.py"
    assert body["original_filename"] == "agent.zip"
    assert body["uploaded_by"] == "hr-demo"
    assert body["sha256"] == "f" * 64
    assert [f["path"] for f in body["files"]] == [
        "agent.py",
        "requirements.txt",
        "util/helpers.py",
    ]


def test_byoc_container_source_puts_the_dockerfile_first(client, aws):
    files = {"app.py": MAIN_PY, "Dockerfile": "FROM python:3.13\n"}
    aws(objects=_upload_objects("up2", files, ""))
    agent_id = _seed(method="byoc", spec=_byoc_spec("container_source", upload_id="up2"))
    body = client.get(f"/api/agents/{agent_id}/source").json()
    assert [f["path"] for f in body["files"]] == ["Dockerfile", "app.py"]
    assert body["entrypoint"] is None


def test_byoc_container_image_has_no_source(client, aws):
    s3, _ = aws()
    uri = "111122223333.dkr.ecr.us-west-2.amazonaws.com/agents:v1"
    spec = _byoc_spec("container_image", upload_id=None, image_uri=uri)
    body = client.get(f"/api/agents/{_seed(method='byoc', spec=spec)}/source").json()
    assert body["kind"] == "image"
    assert body["image_uri"] == uri
    assert body["image_digest"] == "f" * 64
    assert body["files"] == []
    assert s3.gets == []


def test_byoc_upload_gone_is_409(client, aws):
    aws(objects={})
    agent_id = _seed(method="byoc", spec=_byoc_spec("code_zip"))
    res = client.get(f"/api/agents/{agent_id}/source")
    assert res.status_code == 409
    assert res.json()["code"] == "agent.source_unavailable"


# ─── Harness ────────────────────────────────────────────────────────────────────
HARNESS = {
    "harnessId": HARNESS_ID,
    "harnessName": "hr_harness",
    "arn": f"arn:aws:bedrock-agentcore:us-west-2:111122223333:harness/{HARNESS_ID}",
    "status": "READY",
    "harnessVersion": "4",
    "executionRoleArn": "arn:aws:iam::111122223333:role/private-role",
    "createdAt": datetime(2026, 10, 1, tzinfo=UTC),
    "updatedAt": MODIFIED,
    "model": {"bedrockModelConfig": {"modelId": "global.openai.gpt-6-sol"}},
    "systemPrompt": [{"text": "You are the HR assistant."}],
    "tools": [
        {
            "type": "remote_mcp",
            "name": "crm",
            "config": {
                "remoteMcp": {
                    "url": "https://mcp.example.com/mcp?api_key=do-not-leak",
                    "headers": {"X-Api-Key": "do-not-leak"},
                }
            },
        },
        {
            "type": "agentcore_gateway",
            "name": "gw",
            "config": {
                "agentCoreGateway": {
                    "gatewayArn": "arn:aws:bedrock-agentcore:us-west-2:1:gateway/g",
                    "outboundAuth": {
                        "oauth": {
                            "providerArn": "arn:p",
                            "scopes": ["s"],
                            "customParameters": {"login_hint": "do-not-leak"},
                        }
                    },
                }
            },
        },
        {"type": "agentcore_code_interpreter", "name": "ci"},
    ],
    "allowedTools": ["*"],
    "skills": [{"s3": {"uri": "s3://b/skills/hr-policy/"}}],
    "memory": {"agentCoreMemoryConfiguration": {"arn": "arn:mem"}},
    "environment": {"agentCoreRuntimeEnvironment": {"networkConfiguration": {}}},
    "environmentArtifact": {"containerConfiguration": {"containerUri": "private"}},
    "environmentVariables": {"PRIVATE_TOKEN": "do-not-leak"},
    "authorizerConfiguration": {"customJWTAuthorizer": {"allowedClients": ["c"]}},
    "maxIterations": 20,
}


def test_harness_config_is_allow_listed_and_masked(client, aws):
    s3, control = aws(harness=HARNESS)
    agent_id = _seed(method="harness", resource_id=HARNESS_ID)

    res = client.get(f"/api/agents/{agent_id}/source")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["kind"] == "harness_config"
    assert body["harness_version"] == "4"
    config = body["config"]
    for key in (
        "arn",
        "executionRoleArn",
        "environment",
        "environmentArtifact",
        "environmentVariables",
        "authorizerConfiguration",
    ):
        assert key not in config
    assert "do-not-leak" not in res.text
    assert config["systemPrompt"] == [{"text": "You are the HR assistant."}]
    assert config["updatedAt"] == MODIFIED.isoformat()
    mcp = config["tools"][0]["config"]["remoteMcp"]
    assert mcp["url"] == "https://mcp.example.com/mcp?******"
    assert mcp["headers"] == {"X-Api-Key": "******"}
    oauth = config["tools"][1]["config"]["agentCoreGateway"]["outboundAuth"]["oauth"]
    assert oauth["customParameters"] == {"login_hint": "******"}
    assert oauth["scopes"] == ["s"]
    assert config["tools"][2] == {"type": "agentcore_code_interpreter", "name": "ci"}
    assert control.calls == ["get_harness"]
    assert s3.gets == []


# ─── refusals ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "method,resource_id,status,reason",
    [
        ("discovered_runtime", RUNTIME_ID, "active", "method"),
        ("zip_runtime", None, "failed", "no_resource"),
        ("harness", None, "failed", "no_resource"),
        ("zip_runtime", RUNTIME_ID, "deleted", "deleted"),
    ],
)
def test_no_source_is_409_with_a_reason(client, aws, method, resource_id, status, reason):
    _, control = aws()
    agent_id = _seed(method=method, resource_id=resource_id, status=status)
    res = client.get(f"/api/agents/{agent_id}/source")
    assert res.status_code == 409, res.text
    assert res.json()["code"] == "agent.source_unavailable"
    assert res.json()["detail"]["reason"] == reason
    assert res.json()["message"]
    assert control.calls == []


def test_agent_from_another_workspace_is_404(client, aws):
    _, control = aws()
    agent_id = _seed(method="zip_runtime", workspace_id="acct-other")
    res = client.get(f"/api/agents/{agent_id}/source")
    assert res.status_code == 404
    assert res.json()["code"] == "agent.not_found"
    assert control.calls == []
