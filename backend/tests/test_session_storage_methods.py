"""Managed session storage on the Strands zip method.

Create attaches ``spec.filesystem.session_storage``; a re-publish echoes it because
UpdateAgentRuntime clears an omitted ``filesystemConfigurations`` (probed live
2026-10-04).
"""

import pytest

from app.schemas.agent import AgentSpec

from .test_zip_runtime_deployer import StubRuntimeControl, _fake_settings, _fake_ws

SESSION = {"sessionStorage": {"mountPath": "/mnt/workspace"}}


def _spec(method: str, **over) -> AgentSpec:
    return AgentSpec(name="fs-agent", method=method, system_prompt="hi", **over)


# ─── schema ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("method", ["harness", "zip_runtime", "studio"])
def test_session_storage_allowed_on_non_container_methods(method):
    extra = {"code": "print('x')"} if method == "studio" else {}
    spec = _spec(method, filesystem={"session_storage": {"mount_path": "/mnt/data"}}, **extra)
    assert spec.filesystem.session_storage.mount_path == "/mnt/data"


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


# ─── Strands zip runtime ─────────────────────────────────────────────────────
def test_code_runtime_wrappers_pass_filesystem_only_when_set():
    from app.services.agentcore import runtime as rt

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
