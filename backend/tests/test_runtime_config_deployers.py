"""Hermetic Runtime create/update/canary requests preserve agent billing intent."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from app.core.db import SessionLocal
from app.deployer import byoc, container, zip_runtime
from app.deployer.pipeline import StageContext
from app.models.ledger import Agent
from app.optimization import canary_infra
from app.schemas.agent import AgentSpec
from app.services.agentcore import runtime as rt
from tests.conftest import ws_ctx
from tests.test_runtime_config import BASELINE, CONSUMPTION, ShapeControl, spec_input

ROLE = "arn:aws:iam::111122223333:role/agent-exec"
ARN = "arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/billing_rt-abcdefghij"
RESOURCES = {"artifacts_bucket": "billing-artifacts", "execution_role_arn": ROLE}


class LiveControl(ShapeControl):
    def __init__(self, *, failure=False, baseline=True):
        super().__init__()
        self.failure = failure
        self.reads = 0
        self.live = {
            "agentRuntimeVersion": "1", "agentRuntimeArn": ARN, "status": "READY",
            "roleArn": ROLE, "platformVersion": "V2",
            "filesystemConfigurations": [{"sessionStorage": {"mountPath": "/mnt/workspace"}}],
            **({"resourceConfigurations": [{"minimumMemoryInMiB": 2048}]} if baseline else {}),
        }

    def get_agent_runtime(self, **params):
        assert params in (
            {"agentRuntimeId": "billing_rt-abcdefghij"},
            {"agentRuntimeId": "billing_rt-abcdefghij", "agentRuntimeVersion": "1"},
        )
        self.reads += 1
        if self.failure:
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
                              "GetAgentRuntime")
        return deepcopy(self.live)

    def create_agent_runtime(self, **params):
        response = super().create_agent_runtime(**params)
        return {**response, "agentRuntimeArn": ARN}

    def update_agent_runtime(self, **params):
        response = super().update_agent_runtime(**params)
        return {**response, "agentRuntimeArn": ARN}

    def tag_resource(self, **params):
        return {}


CASES = ["zip_runtime", "studio", "container", "code_zip", "container_source",
         "container_image", "mcp_code", "mcp_image", "mcp_node"]


def deploy_spec(case, config):
    method = case if case in {"zip_runtime", "studio", "container"} else "byoc"
    raw = spec_input(method)
    if method == "byoc":
        kind = {"mcp_code": "code_zip", "mcp_image": "container_image",
                "mcp_node": "code_zip"}.get(case, case)
        raw["byoc"] = {"artifact_kind": kind}
        if kind == "container_image":
            raw["byoc"]["image_uri"] = (
                "111122223333.dkr.ecr.us-west-2.amazonaws.com/agents:latest"
            )
        else:
            raw["byoc"]["upload_id"] = "upload1"
        if case.startswith("mcp"):
            raw.update(protocol="mcp", mcp={"target_name": "billing-tools"})
            if case == "mcp_node":
                raw["mcp"]["stdio_wrapper"] = {"base": "node", "command": "node",
                                                "args": ["server.js"]}
    if config is not None:
        raw["runtime_config"] = config
    return AgentSpec(**raw)


def stage_fixture(monkeypatch, case, config, *, mode="update", resource=True, failure=False,
                  baseline=True):
    spec = deploy_spec(case, config)
    module = {"zip_runtime": zip_runtime, "studio": zip_runtime,
              "container": container}.get(case, byoc)
    stub = LiveControl(failure=failure, baseline=baseline)
    monkeypatch.setattr(module, "control_client", lambda _ws: stub)
    monkeypatch.setattr(rt, "wait_runtime_ready", lambda *a, **k: {
        "agentRuntimeVersion": "2", "agentRuntimeArn": ARN, "status": "READY",
    })
    monkeypatch.setattr(module.model_profile, "model_environment", lambda *a, **k: {})
    if module in {zip_runtime, byoc}:
        monkeypatch.setattr(module, "register_return_url_stage", lambda *a, **k: True)
    with SessionLocal() as db:
        agent = Agent(workspace_id="default", name=spec.name, method=spec.method,
                      status="active", spec=spec.model_dump(), version="1")
        if resource:
            agent.resource_id, agent.arn = "billing_rt-abcdefghij", ARN
        db.add(agent)
        db.commit()
        db.expunge(agent)
    ctx = StageContext(agent_id=agent.id, deployment_id="dep1", job_id="job1",
                       workspace=ws_ctx(RESOURCES), scratch={
                           "mode": mode, "execution_role_arn": ROLE,
                           "s3_bucket": "billing-artifacts", "s3_key": "agent/package.zip",
                           "image_uri": (
                               "111122223333.dkr.ecr.us-west-2.amazonaws.com/agents:latest"
                           ),
                       })
    return module, ctx, agent, stub


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("mode", ["create", "update"])
@pytest.mark.parametrize("config", [BASELINE, CONSUMPTION, None])
def test_every_runtime_deployer_passes_explicit_settings_or_preserves_legacy(
    monkeypatch, case, mode, config
):
    module, ctx, agent, stub = stage_fixture(
        monkeypatch, case, config, mode=mode, resource=mode == "update"
    )
    before = deepcopy(agent.spec)
    module._stage_deploy(ctx, agent)
    params = stub.created if mode == "create" else stub.updated
    if config is not None:
        assert params["platformVersion"] == "V2"
    else:
        assert "platformVersion" not in params  # older create jobs and all legacy updates
    expected = ([{"minimumMemoryInMiB": 4096}] if config == BASELINE else
                [{"minimumMemoryInMiB": 2048}] if config is None and mode == "update" else None)
    if expected is None:
        assert "resourceConfigurations" not in params
    else:
        assert params["resourceConfigurations"] == expected
    assert stub.reads == (1 if mode == "update" else 0)  # same read as mount preservation
    assert params["roleArn"] == ROLE
    assert params["filesystemConfigurations"] == [
        {"sessionStorage": {"mountPath": "/mnt/workspace"}}
    ]
    if case.startswith("mcp"):
        assert params["protocolConfiguration"] == {"serverProtocol": "MCP"}
        assert "MODEL_ID" not in params.get("environmentVariables", {})
    with SessionLocal() as db:
        assert db.get(Agent, agent.id).spec == before  # no legacy migration at deploy time


@pytest.mark.parametrize("case", CASES)
def test_legacy_update_read_failure_aborts_before_any_runtime_mutation(monkeypatch, case):
    module, ctx, agent, stub = stage_fixture(monkeypatch, case, None, failure=True)
    with pytest.raises(ClientError, match="denied"):
        module._stage_deploy(ctx, agent)
    assert stub.created is None and stub.updated is None and stub.reads == 1


@pytest.mark.parametrize("case", ["zip_runtime", "container", "code_zip", "container_image"])
def test_legacy_live_consumption_and_failed_update_without_resource_get_no_new_default(
    monkeypatch, case
):
    module, ctx, agent, stub = stage_fixture(monkeypatch, case, None, baseline=False)
    module._stage_deploy(ctx, agent)
    assert "platformVersion" not in stub.updated and "resourceConfigurations" not in stub.updated
    # update mode with no resource can represent a first deploy that failed before
    # AWS accepted it. Deploy-stage fallback must NOT apply the new creation default.
    module, ctx, agent, stub = stage_fixture(monkeypatch, case, None, resource=False)
    module._stage_deploy(ctx, agent)
    assert "platformVersion" not in stub.created and "resourceConfigurations" not in stub.created


@pytest.mark.parametrize("method", ["zip_runtime", "studio"])
@pytest.mark.parametrize("role", ["candidate", "restore"])
@pytest.mark.parametrize("config", [BASELINE, CONSUMPTION, None])
def test_canary_mint_preserves_live_billing_and_restore_uses_original_or_explicit_choice(
    monkeypatch, tmp_path, method, role, config
):
    spec = deploy_spec(method, config)
    stub = LiveControl()
    agent = SimpleNamespace(name=spec.name, resource_id="billing_rt-abcdefghij",
                            project_id=None, workspace_id="default", spec=spec.model_dump(),
                            version="1")
    original = deepcopy(vars(agent))
    monkeypatch.setattr(canary_infra, "_generate_code", lambda _spec: ("print(1)", "test"))
    monkeypatch.setattr(canary_infra, "build_agent_zip",
                        lambda *a, **k: (tmp_path / "pkg.zip", None))
    monkeypatch.setattr(rt, "wait_runtime_ready", lambda *a, **k: {"status": "READY"})
    uploads = []
    result = canary_infra.mint_candidate_version(
        agent=agent, edited_spec=spec, control_client=stub, workspace=ws_ctx(RESOURCES),
        canary_id="can123", role=role, original_version="1",
        uploader=lambda *args: uploads.append(args),
        build_root=tmp_path,
    )
    assert result[:2] == ("1", "2")
    assert role in uploads[0][2]
    assert stub.reads == (2 if role == "restore" and config is None else 1)
    assert vars(agent) == original
    assert stub.updated["roleArn"] == ROLE
    if config is None:
        if role == "restore":
            assert stub.updated["platformVersion"] == "V2"
        else:
            assert "platformVersion" not in stub.updated
        assert stub.updated["resourceConfigurations"] == [{"minimumMemoryInMiB": 2048}]
    elif config == BASELINE:
        assert stub.updated["platformVersion"] == "V2"
        assert stub.updated["resourceConfigurations"] == [{"minimumMemoryInMiB": 4096}]
    else:
        assert stub.updated["platformVersion"] == "V2"
        assert "resourceConfigurations" not in stub.updated


@pytest.mark.parametrize("role", ["candidate", "restore"])
def test_canary_live_read_failure_aborts_before_build_upload_or_update(monkeypatch, role):
    stub = LiveControl(failure=True)
    spec = deploy_spec("zip_runtime", None)
    agent = SimpleNamespace(resource_id="billing_rt-abcdefghij", project_id=None)

    def forbidden(*args, **kwargs):
        pytest.fail("failed preservation read must not build or upload")

    monkeypatch.setattr(canary_infra, "_generate_code", forbidden)
    with pytest.raises(ClientError, match="denied"):
        canary_infra.mint_candidate_version(
            agent=agent, edited_spec=spec, control_client=stub, workspace=ws_ctx(RESOURCES),
            canary_id="can123", role=role, original_version="1", uploader=forbidden,
        )
    assert stub.updated is None
