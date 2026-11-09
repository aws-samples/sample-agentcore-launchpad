"""Prompt drift (services/prompt_drift): the ledger prompt beside the Harness's latest
AWS version, the platform-appended sections ignored, and adopting the AWS prompt
through the ordinary re-publish. The control client is a stub; no AWS call is made."""

import pytest

import app.routers.agents as agents_router
from app.core.db import SessionLocal
from app.deployer import harness as harness_deployer
from app.models.ledger import Agent

SPEC = {"name": "drift-agent", "method": "harness", "system_prompt": "Be brief."}


class StubControl:
    def __init__(self, prompt: str, version: str = "3", **extra):
        self.harness = {"harnessId": "h-1", "harnessVersion": version,
                        "systemPrompt": [{"text": prompt}], **extra}
        self.calls: list[dict] = []

    def get_harness(self, **kw):
        self.calls.append(kw)
        return {"harness": self.harness}


@pytest.fixture(autouse=True)
def no_real_deploy(monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr(agents_router, "start_deploy_async", launched.append)
    return launched


@pytest.fixture
def control(monkeypatch):
    def install(prompt: str, **kw) -> StubControl:
        stub = StubControl(prompt, **kw)
        monkeypatch.setattr(agents_router, "control_client", lambda _ws=None: stub)
        return stub

    return install


def _create(client, spec=SPEC) -> str:
    agent_id = client.post("/api/agents", json=spec).json()["agent"]["id"]
    db = SessionLocal()
    agent = db.get(Agent, agent_id)
    agent.status, agent.resource_id, agent.version = "active", "h-1", "2"
    agent.arn = "arn:aws:bedrock-agentcore:us-west-2:111:harness/h-1"
    db.commit()
    db.close()
    return agent_id


def test_an_unchanged_prompt_is_in_sync(client, control):
    agent_id = _create(client)
    stub = control("Be brief.")
    out = client.get(f"/api/agents/{agent_id}/prompt-drift").json()
    assert out["status"] == "in_sync" and out["aws_version"] == "3"
    assert stub.calls == [{"harnessId": "h-1"}]  # the latest version


def test_the_appended_sections_are_not_drift(client, control):
    agent_id = _create(client)
    db = SessionLocal()
    from app.schemas.agent import AgentSpec

    spec = AgentSpec(**db.get(Agent, agent_id).spec)
    db.close()
    # what the deployer publishes: the operator's prompt + the platform's sections
    deployed = harness_deployer.build_create_params(spec, "arn:aws:iam::111:role/r", None)
    control(deployed["systemPrompt"][0]["text"])
    assert client.get(f"/api/agents/{agent_id}/prompt-drift").json()["status"] == "in_sync"


def test_a_console_edit_is_drift_and_names_other_changes(client, control):
    agent_id = _create(client)
    control("Be brief. Answer in French.", maxIterations=7)
    out = client.get(f"/api/agents/{agent_id}/prompt-drift").json()
    assert out["status"] == "drifted"
    assert out["ledger_prompt"] == "Be brief."
    assert out["aws_prompt"] == "Be brief. Answer in French."
    assert {"member": "max_iterations", "ledger": 100, "aws": 7} in out["other_differences"]


def test_adopting_republishes_the_aws_prompt(client, control, no_real_deploy):
    agent_id = _create(client)
    control("Be brief. Answer in French.")
    res = client.post(f"/api/agents/{agent_id}/prompt-drift/adopt", json={"aws_version": "3"})
    assert res.status_code == 202, res.text
    assert res.json()["adopted_from_version"] == "3" and no_real_deploy
    db = SessionLocal()
    assert db.get(Agent, agent_id).spec["system_prompt"] == "Be brief. Answer in French."
    db.close()
    snaps = client.get(f"/api/agents/{agent_id}/snapshots").json()["snapshots"]
    assert "AWS version 3" in (snaps[0].get("note") or "")


@pytest.mark.parametrize("prompt, version, code", [
    ("Be brief. Answer in French.", "4", "agent.prompt_drift_stale"),  # changed again
    ("Be brief.", "3", "agent.prompt_in_sync"),
])
def test_adopt_refusals(client, control, prompt, version, code):
    agent_id = _create(client)
    control(prompt, version=version)
    res = client.post(f"/api/agents/{agent_id}/prompt-drift/adopt", json={"aws_version": "3"})
    assert res.status_code == 409 and res.json()["code"] == code


def test_a_runtime_prompt_is_not_readable(client, control):
    agent_id = _create(client, {**SPEC, "name": "drift-zip", "method": "zip_runtime"})
    stub = control("unused")
    out = client.get(f"/api/agents/{agent_id}/prompt-drift").json()
    assert out["status"] == "unsupported" and out["reason"] == "runtime"
    assert not stub.calls
    res = client.post(f"/api/agents/{agent_id}/prompt-drift/adopt", json={"aws_version": "1"})
    assert res.json()["code"] == "agent.prompt_drift_unsupported"
