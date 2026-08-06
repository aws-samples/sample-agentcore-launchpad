"""API contract tests — no AWS calls (deploy launch is stubbed)."""

import pytest

import app.routers.agents as agents_router
from app.core.db import SessionLocal
from app.models.ledger import Agent

SPEC = {
    "name": "api-test-agent",
    "method": "harness",
    "system_prompt": "Answer concisely.",
}


@pytest.fixture(autouse=True)
def no_real_deploy(monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: launched.append(jid))
    yield launched


def test_create_agent_returns_job_and_stages(client, no_real_deploy):
    res = client.post("/api/agents", json=SPEC)
    assert res.status_code == 202
    body = res.json()
    assert body["agent"]["status"] == "deploying"
    assert body["job_id"] and body["deployment_id"]
    assert no_real_deploy == [body["job_id"]]

    detail = client.get(f"/api/agents/{body['agent']['id']}").json()
    stages = detail["deployments"][0]["stages"]
    assert [s["name"] for s in stages] == [
        "generate", "package", "provision", "deploy", "register",
    ]
    assert all(s["status"] == "pending" for s in stages)
    assert body["agent"]["experiment_capability"]["eligible"] is False


def test_agent_api_projects_experiment_and_canary_capabilities(client):
    spec = {
        "name": "bundle-agent",
        "method": "zip_runtime",
        "system_prompt": "Answer concisely.",
    }
    body = client.post("/api/agents", json=spec).json()["agent"]
    assert body["experiment_capability"] == {
        "eligible": True,
        "system_prompt": True,
        "tool_descriptions": True,
        "reason": None,
        "reason_code": None,
    }
    assert body["canary_capability"] == {
        "eligible": False,
        "reason": "Canary agent must be active.",
        "reason_code": "not-active",
    }

    db = SessionLocal()
    agent = db.get(Agent, body["id"])
    agent.status = "active"
    agent.arn = (
        "arn:aws:bedrock-agentcore:us-west-2:111122223333:"
        "runtime/bundle_agent-abcdefghij"
    )
    db.commit()
    db.close()

    detail = client.get(f"/api/agents/{body['id']}").json()
    assert detail["canary_capability"] == {
        "eligible": True,
        "reason": None,
        "reason_code": None,
    }


def test_duplicate_name_conflict(client):
    assert client.post("/api/agents", json=SPEC).status_code == 202
    res = client.post("/api/agents", json=SPEC)
    assert res.status_code == 409
    assert res.json()["code"] == "agent.name_exists"


def test_unsupported_method_rejected(client, monkeypatch):
    # all four methods ship now — simulate a future/disabled method gate
    monkeypatch.setattr(agents_router, "SUPPORTED_METHODS", {"harness"})
    res = client.post("/api/agents", json={**SPEC, "method": "studio"})
    assert res.status_code == 400
    assert res.json()["code"] == "agent.method_not_available"


def test_invalid_spec_envelope(client):
    res = client.post("/api/agents", json={**SPEC, "name": "Bad Name!"})
    assert res.status_code == 422
    assert res.json()["code"] == "validation.invalid_request"


def test_invoke_requires_active(client):
    agent_id = client.post("/api/agents", json=SPEC).json()["agent"]["id"]
    res = client.post(f"/api/agents/{agent_id}/invoke", json={"prompt": "hi"})
    assert res.status_code == 409
    assert res.json()["code"] == "agent.not_active"


def test_invoke_active_agent(client, monkeypatch):
    agent_id = client.post("/api/agents", json=SPEC).json()["agent"]["id"]
    db = SessionLocal()
    agent = db.get(Agent, agent_id)
    agent.status = "active"
    agent.arn = "arn:aws:bedrock-agentcore:us-west-2:111:harness/x"
    db.commit()
    db.close()

    monkeypatch.setattr(
        agents_router,
        "invoke_agent_text",
        lambda agent, prompt, session_id=None, actor_id="default": {
            "text": "4",
            "session_id": "s" * 40,
        },
    )
    res = client.post(f"/api/agents/{agent_id}/invoke", json={"prompt": "2+2?"})
    assert res.status_code == 200
    body = res.json()
    assert body["text"] == "4" and body["session_id"] == "s" * 40


def test_delete_marks_ledger(client, monkeypatch):
    deleted: list[str] = []
    monkeypatch.setattr(
        agents_router.harness_method,
        "delete_agent_resources",
        lambda agent: deleted.append(agent.name),
    )
    agent_id = client.post("/api/agents", json=SPEC).json()["agent"]["id"]
    res = client.delete(f"/api/agents/{agent_id}")
    assert res.status_code == 200 and res.json()["deleted"] is True
    assert deleted == ["api-test-agent"]
    assert client.get("/api/agents").json()["agents"] == []  # deleted rows hidden


def _activate(agent_id: str) -> None:
    """Simulate a finished deploy: active with a live resource + ARN."""
    db = SessionLocal()
    agent = db.get(Agent, agent_id)
    agent.status = "active"
    agent.resource_id = "harness-xyz"
    agent.arn = "arn:aws:bedrock-agentcore:us-west-2:111:harness/xyz"
    db.commit()
    db.close()


def test_redeploy_updates_in_place(client, no_real_deploy):
    """Re-publish keeps the resource (UpdateHarness/UpdateAgentRuntime, new
    version) — it must NOT clear resource_id/arn, and the deploy job runs in
    'update' mode."""
    from app.models.ledger import Job

    created = client.post("/api/agents", json=SPEC).json()
    agent_id, first_job = created["agent"]["id"], created["job_id"]
    _activate(agent_id)

    res = client.post(f"/api/agents/{agent_id}/redeploy",
                      json={**SPEC, "system_prompt": "Now answer in French."})
    assert res.status_code == 202
    body = res.json()
    assert body["agent"]["status"] == "deploying"
    assert body["job_id"] != first_job
    assert no_real_deploy[-1] == body["job_id"]  # a new deploy job was launched

    detail = client.get(f"/api/agents/{agent_id}").json()
    assert detail["resource_id"] == "harness-xyz"  # SAME resource — updated in place
    assert detail["arn"] == "arn:aws:bedrock-agentcore:us-west-2:111:harness/xyz"  # ARN kept
    assert detail["spec"]["system_prompt"] == "Now answer in French."  # edited spec stored

    db = SessionLocal()
    assert db.get(Job, body["job_id"]).payload["mode"] == "update"  # update-mode pipeline
    db.close()

    listed = next(a for a in client.get("/api/agents").json()["agents"] if a["id"] == agent_id)
    assert listed["revision"] == 2  # two deployment rows now


def test_redeploy_rejects_name_or_method_change(client):
    agent_id = client.post("/api/agents", json=SPEC).json()["agent"]["id"]
    _activate(agent_id)
    for bad in ({"name": "renamed-agent"}, {"method": "container"}):
        res = client.post(f"/api/agents/{agent_id}/redeploy", json={**SPEC, **bad})
        assert res.status_code == 400
        assert res.json()["code"] == "agent.redeploy_immutable"


def test_redeploy_conflicts_while_deploying(client):
    # a freshly created agent is still "deploying" (pipeline stubbed) → no re-publish
    agent_id = client.post("/api/agents", json=SPEC).json()["agent"]["id"]
    res = client.post(f"/api/agents/{agent_id}/redeploy", json=SPEC)
    assert res.status_code == 409
    assert res.json()["code"] == "agent.deploy_in_progress"


def test_redeploy_not_found(client):
    res = client.post("/api/agents/nope/redeploy", json=SPEC)
    assert res.status_code == 404
    assert res.json()["code"] == "agent.not_found"


def test_job_not_found_envelope(client):
    res = client.get("/api/jobs/nope")
    assert res.status_code == 404
    assert res.json()["code"] == "job.not_found"


CONTAINER_SPEC = {
    "name": "sdk-fs-agent",
    "method": "container",
    "system_prompt": "hi",
    "tools": [{"type": "mcp", "name": "deepwiki", "config": {"url": "https://mcp.deepwiki.com/mcp"}}],
    "skills": ["s3://bkt/skills/web-analyzer/", "s3://bkt/agent-skills/ab12cd34/notes/"],
    "filesystem": {
        "session_storage": {"mount_path": "/mnt/workspace"},
        "s3_files": [{
            "access_point_arn":
                "arn:aws:s3files:us-west-2:111122223333:file-system/fs-a/access-point/ap-1",
            "mount_path": "/mnt/datasets",
        }],
        "efs": [],
    },
    "network": {"subnets": ["subnet-a"], "security_groups": ["sg-1"]},
}


def test_create_container_agent_with_capabilities_and_fs(client):
    res = client.post("/api/agents", json=CONTAINER_SPEC)
    assert res.status_code == 202
    agent_id = res.json()["agent"]["id"]
    spec = client.get(f"/api/agents/{agent_id}").json()["spec"]
    assert spec["skills"] == CONTAINER_SPEC["skills"]
    assert spec["tools"][0]["config"]["url"] == "https://mcp.deepwiki.com/mcp"
    assert spec["filesystem"]["session_storage"]["mount_path"] == "/mnt/workspace"
    assert spec["filesystem"]["s3_files"][0]["mount_path"] == "/mnt/datasets"
    assert spec["network"]["subnets"] == ["subnet-a"]


def test_container_spec_without_agent_sdk_defaults_to_claude(client):
    """Regression for every container agent stored before agent_sdk existed: the
    console's "Other Agent SDK" entrance has one member today, so an absent field
    must read back as it, with the Claude default model untouched."""
    res = client.post("/api/agents", json=CONTAINER_SPEC)
    assert res.status_code == 202
    spec = client.get(f"/api/agents/{res.json()['agent']['id']}").json()["spec"]
    assert "agent_sdk" not in CONTAINER_SPEC  # the payload really omits it
    assert spec["agent_sdk"] == "claude_agent_sdk"
    # the Claude Agent SDK can only drive Claude models — default stays Claude
    assert spec["model_source"] == "bedrock"
    assert "anthropic.claude" in spec["model_id"]


def test_container_agent_accepts_explicit_agent_sdk(client):
    res = client.post(
        "/api/agents",
        json={**CONTAINER_SPEC, "name": "sdk-explicit", "agent_sdk": "claude_agent_sdk"},
    )
    assert res.status_code == 202
    spec = client.get(f"/api/agents/{res.json()['agent']['id']}").json()["spec"]
    assert spec["agent_sdk"] == "claude_agent_sdk"


def test_container_agent_unknown_agent_sdk_422(client):
    res = client.post(
        "/api/agents",
        json={**CONTAINER_SPEC, "name": "sdk-unknown", "agent_sdk": "some_future_sdk"},
    )
    assert res.status_code == 422
    assert res.json()["code"] == "validation.invalid_request"


def test_container_agent_byo_without_vpc_422(client):
    bad = {**CONTAINER_SPEC, "name": "sdk-bad-agent"}
    bad.pop("network")
    res = client.post("/api/agents", json=bad)
    assert res.status_code == 422
    assert res.json()["code"] == "validation.invalid_request"


def test_container_agent_invalid_mount_422(client):
    bad = {
        **CONTAINER_SPEC,
        "name": "sdk-bad-mount",
        "filesystem": {"session_storage": {"mount_path": "/data/x"}},
        "network": None,
    }
    res = client.post("/api/agents", json=bad)
    assert res.status_code == 422


def test_container_redeploy_preserves_fs_fields(client, no_real_deploy):
    agent_id = client.post("/api/agents", json=CONTAINER_SPEC).json()["agent"]["id"]
    _activate(agent_id)
    edited = {**CONTAINER_SPEC, "filesystem": {**CONTAINER_SPEC["filesystem"],
                                               "session_storage": None}}
    res = client.post(f"/api/agents/{agent_id}/redeploy", json=edited)
    assert res.status_code == 202
    spec = client.get(f"/api/agents/{agent_id}").json()["spec"]
    assert spec["filesystem"]["session_storage"] is None  # user disabled it
    assert spec["filesystem"]["s3_files"]  # BYO mount kept


# --- platform toolkits (AgentSpec.toolkits) ---------------------------------

TOOLKIT_SPEC = {
    "name": "hr-toolkit-agent",
    "method": "zip_runtime",
    "system_prompt": "You are a helpful HR Assistant for Acme Corp.",
    "toolkits": ["hr_assistant"],
}


def test_toolkit_agent_is_accepted_and_stays_experiment_eligible(client):
    """The load-bearing invariant: a toolkit is a spec FIELD, so the generated
    source is never written to spec.code/code_bundle and eligibility survives."""
    body = client.post("/api/agents", json=TOOLKIT_SPEC).json()["agent"]
    assert body["experiment_capability"] == {
        "eligible": True,
        "system_prompt": True,
        "tool_descriptions": True,
        "reason": None,
        "reason_code": None,
    }
    spec = client.get(f"/api/agents/{body['id']}").json()["spec"]
    assert spec["toolkits"] == ["hr_assistant"]
    assert spec.get("code") is None
    assert spec.get("code_bundle") is None


def test_toolkit_survives_redeploy(client, no_real_deploy):
    agent_id = client.post("/api/agents", json=TOOLKIT_SPEC).json()["agent"]["id"]
    _activate(agent_id)
    res = client.post(f"/api/agents/{agent_id}/redeploy", json=TOOLKIT_SPEC)
    assert res.status_code == 202
    detail = client.get(f"/api/agents/{agent_id}").json()
    assert detail["spec"]["toolkits"] == ["hr_assistant"]
    assert detail["experiment_capability"]["eligible"] is True


def test_zip_spec_without_toolkits_reads_back_empty(client):
    body = client.post(
        "/api/agents",
        json={"name": "no-toolkit", "method": "zip_runtime", "system_prompt": "Hi."},
    ).json()["agent"]
    assert client.get(f"/api/agents/{body['id']}").json()["spec"]["toolkits"] == []


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"method": "harness"}, id="harness"),
        pytest.param({"method": "container"}, id="container"),
        pytest.param({"method": "studio", "code": "print('x')"}, id="studio"),
        pytest.param({"protocol": "a2a"}, id="a2a"),
        pytest.param({"code_bundle": {"main.py": "print('x')"}}, id="code-bundle"),
        pytest.param({"toolkits": ["hr_assistant", "hr_assistant"]}, id="duplicate"),
        pytest.param({"toolkits": ["not_a_toolkit"]}, id="unknown"),
    ],
)
def test_toolkits_rejected_where_the_template_never_renders(client, overrides):
    res = client.post(
        "/api/agents", json={**TOOLKIT_SPEC, "name": "toolkit-bad", **overrides}
    )
    assert res.status_code == 422
    assert res.json()["code"] == "validation.invalid_request"
