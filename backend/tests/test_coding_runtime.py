"""Coding runtime (app/coding): role policy, image tag, runtime drift, uninstall, and
the low-level action call — all against stub clients."""

import io
import json
import re
import shutil
from types import SimpleNamespace
from typing import Any

import pytest

from app.coding import image_build, infra, runtime_client
from app.coding.runtime_client import CodingRuntimeError
from app.core.config import get_settings
from app.services import workspace_iam
from app.services.agentcore import runtime as rt
from tests.conftest import client_error, ws_ctx

ACCOUNT = "111122223333"
REGION = "us-west-2"
REPO_ARN = f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/launchpad-agents"
MODELS = ["global.openai.gpt-6.1-sol", "global.openai.gpt-6-sol", "global.openai.gpt-6-astra"]
SESSION = "s" * 40


# ─── settings ────────────────────────────────────────────────────────────────


def test_coding_settings_defaults():
    settings = get_settings()
    assert settings.coding_models == MODELS
    assert settings.coding_default_model == "global.openai.gpt-6-sol"
    assert settings.coding_efforts == ["low", "medium", "high", "xhigh"]
    assert settings.coding_default_effort == "medium"


# ─── role policy ─────────────────────────────────────────────────────────────


def _policy(models=MODELS) -> dict[str, Any]:
    return workspace_iam.coding_runtime_role_policy(ACCOUNT, REGION, REPO_ARN, models)


def test_role_policy_grants_exactly_the_coding_models_and_the_default_project():
    statements = {s["Sid"]: s for s in _policy()["Statement"]}
    assert set(statements) == {"EcrAuth", "EcrPull", "Logs", "CodingModels"}
    models = statements["CodingModels"]
    assert models["Action"] == ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    assert models["Resource"] == [
        "arn:aws:bedrock:*::foundation-model/openai.gpt-6.1-sol",
        f"arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/global.openai.gpt-6.1-sol",
        "arn:aws:bedrock:*::foundation-model/openai.gpt-6-sol",
        f"arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/global.openai.gpt-6-sol",
        "arn:aws:bedrock:*::foundation-model/openai.gpt-6-astra",
        f"arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/global.openai.gpt-6-astra",
        # bedrock-runtime /openai/v1 authorizes against the project too (live 2026-10-19)
        f"arn:aws:bedrock:{REGION}:{ACCOUNT}:project/default",
    ]
    assert statements["EcrPull"]["Resource"] == REPO_ARN
    assert all(":log-group:/aws/bedrock-agentcore/runtimes/*" in r
               for r in statements["Logs"]["Resource"])


def test_role_policy_has_no_s3_no_mantle_and_no_wildcard_beyond_the_ecr_token():
    doc = _policy()
    actions = [a for s in doc["Statement"] for a in s["Action"]]
    assert not [a for a in actions if a.startswith(("s3:", "bedrock-mantle:", "bedrock-agentcore:",
                                                   "iam:", "sts:", "secretsmanager:", "kms:"))]
    assert "bedrock-mantle" not in json.dumps(doc)
    wildcard = [s["Sid"] for s in doc["Statement"] if s["Resource"] == "*"]
    assert wildcard == ["EcrAuth"]
    assert all(s["Effect"] == "Allow" and "Condition" not in s for s in doc["Statement"])


def test_role_policy_never_widens_on_an_empty_model():
    resources = _policy(["", "global.openai.gpt-6-sol"])["Statement"][3]["Resource"]
    assert "*" not in resources
    assert "arn:aws:bedrock:*::foundation-model/*" not in resources


def test_role_name_is_regional_and_not_an_agent_role():
    assert infra.role_name("us-west-2") == "launchpad-coding-runtime"
    assert infra.role_name("us-east-1") == "launchpad-coding-runtime-us-east-1"
    assert not infra.role_name("us-east-1").startswith("launchpad-agent-")


# ─── image ───────────────────────────────────────────────────────────────────


def test_image_tag_is_a_stable_content_hash_of_the_build_context():
    tag = image_build.image_tag()
    assert re.fullmatch(r"coding-runtime-[0-9a-f]{12}", tag)
    assert image_build.image_tag() == tag
    names = [name for name, _ in image_build._iter_context_files()]
    assert {"Dockerfile", "codex-config.toml", "buildspec.yml", "src/server.mjs",
            "src/workspace.mjs", "templates/AGENTS.md", "templates/gitignore"} <= set(names)
    assert not [n for n in names if n.startswith("test/")], "tests never reach the image"


def test_image_tag_changes_with_sources_but_not_with_tests(tmp_path, monkeypatch):
    copy = tmp_path / "image"
    shutil.copytree(image_build.IMAGE_DIR, copy)
    monkeypatch.setattr(image_build, "IMAGE_DIR", copy)
    base = image_build.image_tag()
    (copy / "test" / "extra.test.mjs").write_text("// a new test\n")
    assert image_build.image_tag() == base
    (copy / "src" / "server.mjs").write_text((copy / "src" / "server.mjs").read_text() + "\n")
    assert image_build.image_tag() != base


def test_assembled_context_is_what_the_dockerfile_copies(tmp_path):
    ctx = image_build.assemble_context(tmp_path / "ctx")
    assert (ctx / "Dockerfile").is_file() and (ctx / "buildspec.yml").is_file()
    assert (ctx / "src" / "server.mjs").is_file() and (ctx / "templates" / "AGENTS.md").is_file()
    assert not (ctx / "test").exists()


def test_dockerfile_pins_codex_and_bakes_the_toolkit():
    text = (image_build.IMAGE_DIR / "Dockerfile").read_text()
    assert "ARG CODEX_VERSION=0.163.0" in text
    assert "aws-core@agent-toolkit-for-aws" in text and "aws-agents@agent-toolkit-for-aws" in text
    assert "FROM public.ecr.aws/docker/library/node:22" in text
    commands = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    codex_calls = [ln for ln in commands if re.search(r"(^|[\s=&])codex (plugin|--version)", ln)]
    assert len(codex_calls) == 4
    for line in codex_calls:
        assert "</dev/null" in line, f"codex blocks on an open stdin: {line.strip()}"
    config = (image_build.IMAGE_DIR / "codex-config.toml").read_text()
    assert 'model_provider = "amazon-bedrock-runtime"' in config
    assert not re.search(r"^\s*region\s*=", config, re.M), "the region is passed at spawn"
    settings_lines = [ln for ln in config.splitlines() if not ln.lstrip().startswith("#")]
    assert not [ln for ln in settings_lines if "mantle" in ln.lower()], "never Mantle"


def test_agents_template_states_the_byoc_contract():
    text = (image_build.IMAGE_DIR / "templates" / "AGENTS.md").read_text()
    for needle in ("8080", "POST /invocations", "GET /ping", '"prompt"', '"actor_id"',
                   "requirements.txt", "Dockerfile", "MODEL_ID", "ALLOWED_MODEL_IDS", "arm64",
                   "1 GB"):
        assert needle in text, needle


# ─── runtime drift ───────────────────────────────────────────────────────────

IMAGE = "111122223333.dkr.ecr.us-west-2.amazonaws.com/launchpad-agents@sha256:abc"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/launchpad-coding-runtime"
ENV = {"CODING_MODELS": ",".join(MODELS), "CODING_EFFORTS": "low,medium,high,xhigh",
       "CODING_RUNTIME_VERSION": "coding-runtime-0123456789ab"}


def _detail(**over) -> dict[str, Any]:
    detail = {
        "agentRuntimeId": "rt-1",
        "agentRuntimeArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/rt-1",
        "status": "READY",
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": IMAGE}},
        "environmentVariables": dict(ENV),
        "roleArn": ROLE,
        "filesystemConfigurations": [{"sessionStorage": {"mountPath": "/mnt/workspace"}}],
        "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
        "networkConfiguration": {"networkMode": "PUBLIC"},
    }
    detail.update(over)
    return detail


def _drift(detail):
    return infra.drift(detail, image_uri=IMAGE, role_arn=ROLE, environment=ENV)


def test_drift_is_empty_for_an_in_sync_runtime():
    assert _drift(_detail()) == []
    trailing = _detail(
        filesystemConfigurations=[{"sessionStorage": {"mountPath": "/mnt/workspace/"}}]
    )
    assert _drift(trailing) == []


@pytest.mark.parametrize("over, reason", [
    ({"agentRuntimeArtifact": {"containerConfiguration": {"containerUri": IMAGE + "0"}}}, "image"),
    ({"environmentVariables": {**ENV, "CODING_MODELS": "x"}}, "environment"),
    ({"roleArn": ROLE + "-old"}, "role"),
    ({"filesystemConfigurations": []}, "filesystem"),
    ({"filesystemConfigurations": [{"sessionStorage": {"mountPath": "/mnt/data"}}]}, "filesystem"),
    ({"lifecycleConfiguration": {"idleRuntimeSessionTimeout": 300, "maxLifetime": 28800}},
     "lifecycle"),
    ({"lifecycleConfiguration": {}}, "lifecycle"),
    ({"networkConfiguration": {"networkMode": "VPC"}}, "network"),
])
def test_drift_names_what_changed(over, reason):
    assert _drift(_detail(**over)) == [reason]


class StubControl:
    def __init__(self, existing: list[dict] | None = None, statuses: list[str] | None = None):
        self.existing = existing or []
        self.statuses = list(statuses or [])
        self.calls: list[tuple[str, dict]] = []
        self.detail = _detail()

    def list_agent_runtimes(self, **kwargs):
        return {"agentRuntimes": self.existing}

    def create_agent_runtime(self, **kwargs):
        self.calls.append(("create", kwargs))
        return {"agentRuntimeId": "rt-1"}

    def update_agent_runtime(self, **kwargs):
        self.calls.append(("update", kwargs))
        return {}

    def get_agent_runtime(self, agentRuntimeId):
        self.calls.append(("get", {"agentRuntimeId": agentRuntimeId}))
        status = self.statuses.pop(0) if self.statuses else self.detail["status"]
        if status == "GONE":
            raise client_error("ResourceNotFoundException")
        return {**self.detail, "status": status}

    def delete_agent_runtime(self, agentRuntimeId):
        self.calls.append(("delete", {"agentRuntimeId": agentRuntimeId}))

    def ops(self):
        return [op for op, _ in self.calls]


SUMMARY = {"agentRuntimeName": infra.RUNTIME_NAME, "agentRuntimeId": "rt-1",
           "agentRuntimeArn": _detail()["agentRuntimeArn"]}


@pytest.fixture
def control(monkeypatch):
    holder: dict[str, StubControl] = {}

    def install(stub: StubControl) -> StubControl:
        holder["stub"] = stub
        monkeypatch.setattr(infra, "control_client", lambda _ctx: stub)
        return stub

    return install


def _ensure(**kw):
    return infra.ensure_runtime(ws_ctx(), image_uri=IMAGE, role_arn=ROLE, environment=ENV, **kw)


def test_create_passes_session_storage_lifecycle_and_env(control):
    stub = control(StubControl(existing=[]))
    out = _ensure()
    assert out == {"runtime_id": "rt-1", "runtime_arn": _detail()["agentRuntimeArn"]}
    (op, params), *_ = stub.calls
    assert op == "create"
    assert params["agentRuntimeName"] == "launchpad_coding_runtime"
    assert params["filesystemConfigurations"] == [
        {"sessionStorage": {"mountPath": "/mnt/workspace"}}
    ]
    assert params["lifecycleConfiguration"] == {
        "idleRuntimeSessionTimeout": 900, "maxLifetime": 28800}
    assert params["networkConfiguration"] == {"networkMode": "PUBLIC"}
    assert params["environmentVariables"] == ENV
    assert params["roleArn"] == ROLE


def test_an_in_sync_ready_runtime_is_left_alone(control):
    stub = control(StubControl(existing=[SUMMARY]))
    logs: list[str] = []
    _ensure(log=logs.append)
    assert "update" not in stub.ops() and "create" not in stub.ops()
    assert any("up to date" in m for m in logs)


def test_drift_updates_and_always_echoes_the_filesystem(control):
    stub = control(StubControl(existing=[SUMMARY]))
    stub.detail = _detail(roleArn=ROLE + "-old")
    logs: list[str] = []
    _ensure(log=logs.append)
    updates = [p for op, p in stub.calls if op == "update"]
    assert len(updates) == 1
    assert updates[0]["filesystemConfigurations"] == [
        {"sessionStorage": {"mountPath": "/mnt/workspace"}}
    ]
    assert updates[0]["lifecycleConfiguration"] == infra.LIFECYCLE
    assert updates[0]["roleArn"] == ROLE
    assert any("role changed" in m for m in logs)


def test_an_in_sync_runtime_that_is_not_ready_is_waited_for_not_updated(control):
    stub = control(StubControl(existing=[SUMMARY], statuses=["UPDATING", "READY"]))
    _ensure()
    assert "update" not in stub.ops()
    assert stub.ops() == ["get", "get"]


def test_a_failed_runtime_raises(control):
    stub = control(StubControl(existing=[SUMMARY], statuses=["CREATE_FAILED", "CREATE_FAILED"]))
    stub.detail = _detail(failureReason="image pull failed")
    with pytest.raises(RuntimeError, match="CREATE_FAILED"):
        _ensure()


# ─── uninstall ───────────────────────────────────────────────────────────────


class StubIam:
    def __init__(self, tags: list[dict] | None, policies=("launchpad-coding-runtime",)):
        self.tags = tags
        self.policies = list(policies)
        self.calls: list[str] = []

    def list_role_tags(self, RoleName):
        if self.tags is None:
            raise client_error("NoSuchEntity")
        return {"Tags": self.tags}

    def list_role_policies(self, RoleName):
        return {"PolicyNames": self.policies}

    def delete_role_policy(self, RoleName, PolicyName):
        self.calls.append(f"delete_role_policy:{PolicyName}")

    def list_attached_role_policies(self, RoleName):
        return {"AttachedPolicies": []}

    def delete_role(self, RoleName):
        self.calls.append(f"delete_role:{RoleName}")


def _ctx_with_iam(iam):
    """A workspace stand-in whose only client is the stub IAM (control is patched)."""
    return SimpleNamespace(
        id="default", account_id=ACCOUNT, region=REGION, resources={"ecr_repo": "launchpad-agents"},
        client=lambda service, **_kw: {"iam": iam}[service],
    )


def test_uninstall_deletes_the_runtime_waits_then_deletes_our_role(control, monkeypatch):
    monkeypatch.setattr(infra, "_sleep", lambda _s: None)
    stub = control(StubControl(existing=[SUMMARY], statuses=["DELETING", "GONE"]))
    iam = StubIam([{"Key": "launchpad:workspace", "Value": "default"}])
    order: list[str] = []
    keys = infra.uninstall_coding_runtime(_ctx_with_iam(iam), "default",
                                          lambda stage, msg: order.append(stage))
    assert stub.ops() == ["delete", "get", "get"]
    assert iam.calls == ["delete_role_policy:launchpad-coding-runtime",
                         "delete_role:launchpad-coding-runtime"]
    assert order.index("role") > order.index("wait"), "role after the runtime is gone"
    assert keys == {k: None for k in infra.RESOURCE_KEYS}


def test_uninstall_leaves_a_role_that_is_not_ours(control):
    control(StubControl(existing=[]))
    for tags in ([], [{"Key": "launchpad:workspace", "Value": "other"}]):
        iam = StubIam(tags)
        infra.uninstall_coding_runtime(_ctx_with_iam(iam), "default")
        assert iam.calls == []
    infra.uninstall_coding_runtime(_ctx_with_iam(StubIam(None)), "default")  # role absent


def test_uninstall_times_out_instead_of_deleting_the_role_early(control, monkeypatch):
    monkeypatch.setattr(infra, "_sleep", lambda _s: None)
    monkeypatch.setattr(infra, "RUNTIME_GONE_TIMEOUT_S", -1)
    control(StubControl(existing=[SUMMARY], statuses=["DELETING"] * 5))
    iam = StubIam([{"Key": "launchpad:workspace", "Value": "default"}])
    with pytest.raises(TimeoutError):
        infra.uninstall_coding_runtime(_ctx_with_iam(iam), "default")
    assert iam.calls == []


def test_ensure_coding_runtime_orders_role_image_runtime(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(infra, "ensure_role", lambda *a, **k: calls.append("role") or ROLE)
    monkeypatch.setattr(image_build, "ensure_image", lambda *a, **k: calls.append("image") or {
        "tag": "coding-runtime-0123456789ab", "digest": "sha256:abc", "uri": IMAGE})

    def fake_runtime(_ctx, **kw):
        calls.append("runtime")
        assert kw["environment"]["CODING_RUNTIME_VERSION"] == "coding-runtime-0123456789ab"
        assert kw["environment"]["CODING_MODELS"] == ",".join(MODELS)
        return {"runtime_id": "rt-1", "runtime_arn": "arn:rt"}

    monkeypatch.setattr(infra, "ensure_runtime", fake_runtime)
    keys = infra.ensure_coding_runtime(ws_ctx(), "default")
    assert calls == ["role", "image", "runtime"]
    assert keys == {
        "coding_runtime_role_arn": ROLE, "coding_runtime_image_tag": "coding-runtime-0123456789ab",
        "coding_runtime_image_digest": "sha256:abc", "coding_runtime_id": "rt-1",
        "coding_runtime_arn": "arn:rt", "coding_runtime_models": MODELS,
    }


# ─── the action call ─────────────────────────────────────────────────────────


class StubData:
    def __init__(self, body: Any = None, raises: Exception | None = None):
        self.body = body
        self.raises = raises
        self.params: dict[str, Any] = {}

    def invoke_agent_runtime(self, **params):
        self.params = params
        if self.raises:
            raise self.raises
        raw = self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()
        return {"response": io.BytesIO(raw)}


def test_invoke_runtime_json_sends_the_payload_verbatim():
    stub = StubData({"ok": True})
    assert rt.invoke_runtime_json(ws_ctx(), "arn:rt", SESSION, {"action": "x"}, client=stub) == {
        "ok": True}
    assert stub.params["agentRuntimeArn"] == "arn:rt"
    assert stub.params["runtimeSessionId"] == SESSION
    assert stub.params["contentType"] == "application/json"
    assert json.loads(stub.params["payload"]) == {"action": "x"}


def test_call_returns_the_result_and_keeps_none_values():
    stub = StubData({"ok": True, "action": "write_file", "boot_id": "b", "result": {"sha256": "h"}})
    out = runtime_client.call(ws_ctx(), "arn:rt", SESSION, "write_file", client=stub,
                              path="a.txt", content="x", expected_sha256=None)
    assert out == {"sha256": "h"}
    assert json.loads(stub.params["payload"]) == {
        "action": "write_file", "path": "a.txt", "content": "x", "expected_sha256": None}


def test_call_maps_a_container_error_to_its_code():
    stub = StubData({"ok": False, "action": "write_file", "boot_id": "b1",
                     "error": {"code": "conflict", "message": "changed", "sha256": "cur"}})
    with pytest.raises(CodingRuntimeError) as err:
        runtime_client.call(ws_ctx(), "arn:rt", SESSION, "write_file", client=stub)
    assert err.value.code == "conflict"
    assert err.value.message == "changed"
    assert err.value.detail == {"sha256": "cur"}
    assert err.value.boot_id == "b1"


@pytest.mark.parametrize("stub, code", [
    (StubData(raises=client_error("ThrottlingException", "slow down")), "runtime_unavailable"),
    (StubData(b"<html>oops</html>"), "runtime_protocol"),
    (StubData({"something": "else"}), "runtime_protocol"),
])
def test_call_failures_without_a_container_answer(stub, code):
    with pytest.raises(CodingRuntimeError) as err:
        runtime_client.call(ws_ctx(), "arn:rt", SESSION, "status", client=stub)
    assert err.value.code == code
    if code == "runtime_unavailable":
        assert err.value.aws_code == "ThrottlingException"


def test_call_refuses_short_session_ids_and_a_missing_runtime():
    with pytest.raises(ValueError):
        runtime_client.call(ws_ctx(), "arn:rt", "short", "status", client=StubData({"ok": True}))
    with pytest.raises(CodingRuntimeError) as err:
        runtime_client.call(ws_ctx(), "", SESSION, "status", client=StubData({"ok": True}))
    assert err.value.code == "runtime_unavailable"
    assert len(runtime_client.new_session_id()) >= runtime_client.MIN_SESSION_ID_LEN
