"""Managed session storage on the harness and Strands zip methods.

Create attaches ``spec.filesystem.session_storage``; a re-publish echoes it because
the two update APIs differ (probed live 2026-10-04): UpdateAgentRuntime clears an
omitted ``filesystemConfigurations``, UpdateHarness keeps an omitted ``environment``
and replaces the list wholesale when one is sent.
"""

import botocore.session
import pytest
from botocore.validate import ParamValidator
from pydantic import ValidationError

from app.deployer.harness import build_create_params
from app.schemas.agent import AgentSpec
from app.services.agentcore import harness as hc
from app.services.agentcore import runtime as rt

from .conftest import ws_ctx
from .test_harness_deployer import StubControl
from .test_zip_runtime_deployer import StubRuntimeControl, _fake_settings, _fake_ws

ROLE_ARN = "arn:aws:iam::111122223333:role/launchpad-agent-execution-role"
S3_AP = "arn:aws:s3files:us-west-2:111122223333:file-system/fs-abc/access-point/ap-1"
EFS_AP = "arn:aws:elasticfilesystem:us-west-2:111122223333:access-point/fsap-0123"
SESSION = {"sessionStorage": {"mountPath": "/mnt/workspace"}}


def _spec(method: str, **over) -> AgentSpec:
    return AgentSpec(name="fs-agent", method=method, system_prompt="hi", **over)


def _validate(operation: str, params: dict) -> None:
    model = botocore.session.get_session().get_service_model("bedrock-agentcore-control")
    report = ParamValidator().validate(params, model.operation_model(operation).input_shape)
    assert not report.has_errors(), report.generate_report()


# ─── schema ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("method", ["harness", "zip_runtime"])
def test_byo_mounts_are_container_only(method):
    with pytest.raises(ValidationError, match="container method only"):
        _spec(
            method,
            filesystem={"efs": [{"access_point_arn": EFS_AP, "mount_path": "/mnt/tools"}]},
            network={"subnets": ["subnet-a"], "security_groups": ["sg-a"]},
        )


@pytest.mark.parametrize("method", ["harness", "zip_runtime", "studio"])
def test_session_storage_allowed_on_non_container_methods(method):
    extra = {"code": "print('x')"} if method == "studio" else {}
    spec = _spec(method, filesystem={"session_storage": {"mount_path": "/mnt/data"}}, **extra)
    assert spec.filesystem.session_storage.mount_path == "/mnt/data"


# ─── harness ─────────────────────────────────────────────────────────────────
def test_harness_create_params_carry_session_storage():
    params = build_create_params(_spec("harness"), ROLE_ARN, None)
    assert params["environment"] == {
        "agentCoreRuntimeEnvironment": {"filesystemConfigurations": [SESSION]}
    }
    _validate("CreateHarness", params)


def test_harness_create_params_omit_environment_when_disabled():
    params = build_create_params(_spec("harness", filesystem={"session_storage": None}),
                                 ROLE_ARN, None)
    assert "environment" not in params


def test_environment_for_update_swaps_only_the_session_entry():
    live = {"environment": {"agentCoreRuntimeEnvironment": {
        # read-only backing-runtime identifiers UpdateHarness refuses
        "agentRuntimeArn": "arn:rt", "agentRuntimeId": "rt-1", "agentRuntimeName": "harness_x",
        "networkConfiguration": {"networkMode": "VPC", "networkModeConfig": {
            "subnets": ["subnet-a"], "securityGroups": ["sg-a"]}},
        "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 600},
        "filesystemConfigurations": [
            {"sessionStorage": {"mountPath": "/mnt/old"}},
            {"efsAccessPoint": {"accessPointArn": EFS_AP, "mountPath": "/mnt/tools"}},
        ],
    }}}
    env = hc.environment_for_update(live, [SESSION])
    assert env == {"agentCoreRuntimeEnvironment": {
        "networkConfiguration": live["environment"]["agentCoreRuntimeEnvironment"][
            "networkConfiguration"],
        "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 600},
        "filesystemConfigurations": [
            SESSION,
            {"efsAccessPoint": {"accessPointArn": EFS_AP, "mountPath": "/mnt/tools"}},
        ],
    }}
    _validate("UpdateHarness", {"harnessId": "h-123", "environment": env})


def test_environment_for_update_sends_empty_list_to_detach():
    live = {"environment": {"agentCoreRuntimeEnvironment": {
        "filesystemConfigurations": [SESSION]}}}
    assert hc.environment_for_update(live, []) == {
        "agentCoreRuntimeEnvironment": {"filesystemConfigurations": []}
    }
    # a harness GetHarness reports without any environment block
    assert hc.environment_for_update({}, [SESSION]) == {
        "agentCoreRuntimeEnvironment": {"filesystemConfigurations": [SESSION]}
    }


class EnvStubControl(StubControl):
    def __init__(self, statuses, environment):
        super().__init__(statuses)
        self.environment = environment

    def get_harness(self, harnessId):
        out = super().get_harness(harnessId)
        out["harness"]["environment"] = self.environment
        return out


def _agent_row(method: str, spec: AgentSpec, **over):
    from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
    from app.models.ledger import Agent

    db = SessionLocal()
    agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=spec.name, method=method,
                  status="active", spec=spec.model_dump(), **over)
    db.add(agent)
    db.commit()
    agent_id = agent.id
    db.close()
    return agent_id


def _load(agent_id: str):
    from app.core.db import SessionLocal
    from app.models.ledger import Agent

    db = SessionLocal()
    try:
        return db.get(Agent, agent_id)
    finally:
        db.close()


@pytest.mark.parametrize(
    ("session", "expected"),
    [({"mount_path": "/mnt/data"}, [{"sessionStorage": {"mountPath": "/mnt/data"}}]),
     (None, [])],
)
def test_harness_republish_echoes_session_storage(monkeypatch, session, expected):
    from app.deployer import harness as harness_deploy
    from app.deployer.pipeline import StageContext

    spec = AgentSpec(name="fs-harness-upd", method="harness", system_prompt="hi",
                     filesystem={"session_storage": session})
    agent_id = _agent_row("harness", spec, resource_id="h-123", arn="arn:h-123", version="1")
    stub = EnvStubControl(["READY"], {"agentCoreRuntimeEnvironment": {
        "agentRuntimeId": "rt-1", "filesystemConfigurations": [SESSION]}})
    monkeypatch.setattr(harness_deploy, "control_client", lambda _ws=None: stub)

    ctx = StageContext(agent_id=agent_id, deployment_id="d1", job_id="j1", workspace=ws_ctx())
    ctx.scratch["mode"] = "update"
    harness_deploy._stage_deploy(ctx, _load(agent_id))

    assert stub.updated_with["environment"] == {
        "agentCoreRuntimeEnvironment": {"filesystemConfigurations": expected}
    }


# ─── Strands zip runtime ─────────────────────────────────────────────────────
def test_code_runtime_wrappers_pass_filesystem_only_when_set():
    stub = StubRuntimeControl(["READY"])
    rt.create_code_runtime(stub, runtime_name="a_1", s3_bucket="bkt", s3_key="k.zip",
                           role_arn="arn:role", filesystem_configurations=[SESSION])
    rt.update_code_runtime(stub, runtime_id="rt-1", s3_bucket="bkt", s3_key="k.zip",
                           role_arn="arn:role")
    assert stub.created_with["filesystemConfigurations"] == [SESSION]
    assert "filesystemConfigurations" not in stub.updated_with


@pytest.mark.parametrize("mode", ["create", "update"])
def test_zip_deploy_mounts_session_storage(monkeypatch, mode):
    from app.deployer import zip_runtime as zr
    from app.deployer.pipeline import StageContext

    spec = AgentSpec(name=f"fs-zip-{mode}", method="zip_runtime", system_prompt="hi",
                     filesystem={"session_storage": {"mount_path": "/mnt/scratch"}})
    over = {"resource_id": "rt-1", "arn": "arn:rt-1", "version": "1"} if mode == "update" else {}
    agent_id = _agent_row("zip_runtime", spec, **over)
    stub = StubRuntimeControl(["READY"])
    monkeypatch.setattr(zr, "control_client", lambda _ws=None: stub)
    monkeypatch.setattr(zr, "get_settings", _fake_settings)

    ctx = StageContext(agent_id=agent_id, deployment_id="d1", job_id="j1", workspace=_fake_ws())
    ctx.scratch["mode"] = mode
    zr._stage_deploy(ctx, _load(agent_id))

    sent = stub.updated_with if mode == "update" else stub.created_with
    assert sent["filesystemConfigurations"] == [{"sessionStorage": {"mountPath": "/mnt/scratch"}}]


def test_zip_deploy_without_session_storage_sends_no_mounts(monkeypatch):
    from app.deployer import zip_runtime as zr
    from app.deployer.pipeline import StageContext

    spec = AgentSpec(name="fs-zip-off", method="zip_runtime", system_prompt="hi",
                     filesystem={"session_storage": None})
    agent_id = _agent_row("zip_runtime", spec, resource_id="rt-1", arn="arn:rt-1", version="1")
    stub = StubRuntimeControl(["READY"])
    monkeypatch.setattr(zr, "control_client", lambda _ws=None: stub)
    monkeypatch.setattr(zr, "get_settings", _fake_settings)

    ctx = StageContext(agent_id=agent_id, deployment_id="d1", job_id="j1", workspace=_fake_ws())
    ctx.scratch["mode"] = "update"
    zr._stage_deploy(ctx, _load(agent_id))

    # omission IS the detach on UpdateAgentRuntime
    assert "filesystemConfigurations" not in stub.updated_with
