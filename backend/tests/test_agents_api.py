"""API contract tests — no AWS calls (deploy launch is stubbed)."""

import pytest

import app.routers.agents as agents_router
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent, UserGrant
from app.services import oauth_sessions
from tests.conftest import settle_deploy_jobs

SPEC = {
    "name": "api-test-agent",
    "method": "zip_runtime",
    "system_prompt": "Answer concisely.",
}


@pytest.fixture(autouse=True)
def no_real_deploy(monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: launched.append(jid))
    yield launched


def test_create_agent_harness_method_runs_the_five_stage_pipeline(client, no_real_deploy):
    """harness (N7's self-built Harness) runs the same five-stage pipeline as
    every other method — no special-case refusal."""
    res = client.post("/api/agents", json={**SPEC, "method": "harness",
                                           "native_tools": ["shell"], "max_tokens": 4096})
    assert res.status_code == 202, res.text
    body = res.json()
    assert no_real_deploy == [body["job_id"]]
    stages = client.get(f"/api/agents/{body['agent']['id']}").json()["deployments"][0]["stages"]
    assert [s["name"] for s in stages] == ["generate", "package", "provision", "deploy", "register"]


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


def test_harness_builtin_the_method_cannot_render_is_422_before_any_row(
    client, no_real_deploy, monkeypatch
):
    """N15-obs16: `office-agent` (builtin `browser`) was accepted by POST /api/agents and
    failed at the deploy job's FIRST stage with HarnessRenderError. The request boundary
    now refuses a builtin the method does not render — no agent row, no job."""
    import dataclasses

    from app.deployer import pipeline

    # round 16: the gate reads the method's declared domain (MethodCapabilities), so an
    # unrendered builtin is simulated by narrowing the declaration, not the render map
    caps = pipeline.method_capabilities("harness")
    monkeypatch.setitem(pipeline._CAPABILITIES, "harness",
                        dataclasses.replace(caps, builtin_tools=frozenset({"code-interpreter"})))
    res = client.post("/api/agents", json={
        **SPEC, "method": "harness",
        "tools": [{"type": "builtin", "name": "browser"},
                  {"type": "builtin", "name": "code-interpreter"}],
    })
    assert res.status_code == 422, res.text
    body = res.json()
    assert body["code"] == "agent.method_unsupported"  # agent.builtin_unsupported's successor
    assert body["detail"] == {"method": "harness", "field": "tools.builtin", "value": "browser",
                              "supported": ["code-interpreter"],
                              "legacy_code": "agent.builtin_unsupported"}
    assert no_real_deploy == [] and client.get("/api/agents").json()["agents"] == []
    # a rendered builtin passes the gate (the five-stage pipeline starts as before)
    res = client.post("/api/agents", json={
        **SPEC, "method": "harness", "tools": [{"type": "builtin", "name": "code-interpreter"}],
    })
    assert res.status_code == 202, res.text


def test_tool_catalog_builtin_rows_name_the_methods_that_render_them(client, monkeypatch):
    """The V2 wizard reads `methods` off each builtin row to disable what would 422."""
    import app.routers.tools as tools_router

    monkeypatch.setattr(tools_router.mcp_client, "tools_list", lambda ctx: [])
    rows = {t["name"]: t for t in client.get("/api/tools").json()["tools"]
            if t["source"] == "builtin"}
    assert set(rows) == {"code-interpreter", "browser"}
    assert rows["code-interpreter"]["methods"] == ["harness"]
    from app.harness_tool_access import BUILTIN_RUNTIME_TOOLS

    assert rows["browser"]["methods"] == (["harness"] if "browser" in BUILTIN_RUNTIME_TOOLS else [])


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
        lambda agent, prompt, session_id=None, actor_id="default", **_kw: {
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
    monkeypatch.setattr(agents_router.teardown, "cleanup_residue", lambda *a, **k: [])
    monkeypatch.setattr(
        agents_router.zip_method,
        "delete_agent_resources",
        lambda agent, _ws: deleted.append(agent.name),
    )
    agent_id = client.post("/api/agents", json=SPEC).json()["agent"]["id"]
    settle_deploy_jobs(agent_id)  # a delete during the deploy is 409 (N16-obs83)
    db = SessionLocal()
    oauth_sessions.record_pending(
        db, DEFAULT_WORKSPACE_ID, session_uri="urn:s1", provider="team-idp",
        user_id="alice", agent_id=agent_id, tool="t", scopes=["openid"],
    )
    res = client.delete(f"/api/agents/{agent_id}")
    assert res.status_code == 200 and res.json()["deleted"] is True
    assert deleted == ["api-test-agent"]
    assert client.get("/api/agents").json()["agents"] == []  # deleted rows hidden
    # its workload identity is gone, so are the users' grants on it
    db.expire_all()
    assert db.query(UserGrant).filter_by(agent_id=agent_id).count() == 0
    db.close()


def test_delete_of_a_harness_agent_tears_down_the_runtime(client, monkeypatch, no_real_deploy):
    """A self-built harness is Runtime-backed exactly like zip_runtime, so its
    delete goes through the same teardown call — not the old no-op log line."""
    deleted: list[str] = []
    monkeypatch.setattr(agents_router.teardown, "cleanup_residue", lambda *a, **k: [])
    monkeypatch.setattr(
        agents_router.zip_method,
        "delete_agent_resources",
        lambda agent, _ws: deleted.append(agent.name),
    )
    agent_id = client.post(
        "/api/agents", json={**SPEC, "name": "harness-del-agent", "method": "harness"}
    ).json()["agent"]["id"]
    settle_deploy_jobs(agent_id)
    res = client.delete(f"/api/agents/{agent_id}")
    assert res.status_code == 200 and res.json()["deleted"] is True
    assert deleted == ["harness-del-agent"]


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


def test_failed_redeploy_leaves_the_agent_active_with_a_last_deploy_marker(client, no_real_deploy):
    """N10v3-bug7 read model: after a failed update the row is still `active`, invokable
    (no 409 agent.not_active), and carries `last_deploy_failed` + `last_deploy_error`;
    the next redeploy clears the marker."""
    from app.deployer import pipeline
    from app.models.ledger import Deployment
    from app.services.runtime_discovery import require_invoke_capability

    created = client.post("/api/agents", json=SPEC).json()
    agent_id = created["agent"]["id"]
    _activate(agent_id)
    db = SessionLocal()
    first = db.get(Deployment, created["deployment_id"])
    first.status = "succeeded"
    db.commit()
    db.close()

    body = client.post(f"/api/agents/{agent_id}/redeploy",
                       json={**SPEC, "system_prompt": "v2 prompt"}).json()
    db = SessionLocal()
    pipeline._finish(db, body["job_id"], body["deployment_id"], agent_id,
                     error="RuntimeError: could not resolve the requirements into a hashed "
                           "lockfile")
    db.close()

    detail = client.get(f"/api/agents/{agent_id}").json()
    assert detail["status"] == "active"
    assert detail["last_deploy_failed"] is True
    assert "hashed lockfile" in detail["last_deploy_error"]
    assert detail["invoke_capability"]["eligible"] is True
    assert detail["deployments"][0]["status"] == "failed"
    listed = next(a for a in client.get("/api/agents").json()["agents"] if a["id"] == agent_id)
    assert listed["last_deploy_failed"] is True and listed["deployment"]["status"] == "failed"
    db = SessionLocal()
    require_invoke_capability(db.get(Agent, agent_id))  # Chat's gate: no 409
    db.close()

    # a new attempt clears the marker while it runs
    again = client.post(f"/api/agents/{agent_id}/redeploy", json=SPEC).json()
    assert again["agent"]["status"] == "deploying"
    assert again["agent"]["last_deploy_failed"] is False
    assert again["agent"]["last_deploy_error"] is None


def test_agent_view_carries_the_canary_capability(client, no_real_deploy):
    """N12-bug2: the CN view dropped `canary_capability` and the canary Start page crashed
    on `.eligible`. The verdict is computed by the same function the canary router refuses
    with. (`experiment_capability` left with the configuration experiment — N13-obs2.)"""
    from app.optimization.capability import canary_capability

    # eligible: an ACTIVE zip_runtime HTTP agent with a Runtime ARN
    agent_id = client.post("/api/agents", json=SPEC).json()["agent"]["id"]
    _activate(agent_id)
    db = SessionLocal()
    db.get(Agent, agent_id).arn = (
        "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/xyz")
    db.commit()
    db.close()
    detail = client.get(f"/api/agents/{agent_id}").json()
    assert "experiment_capability" not in detail
    assert detail["canary_capability"] == {"eligible": True, "reason": None, "reason_code": None}

    # N12-obs34: a CN self-built Harness is a Runtime — eligible once it has a Runtime ARN;
    # without one the canary verdict names the missing ARN, never the method
    harness = client.post("/api/agents", json={**SPEC, "name": "api-test-harness",
                                                "method": "harness"}).json()["agent"]["id"]
    _activate(harness)  # `_activate` stamps a harness/ ARN, i.e. no Runtime ARN yet
    row = client.get(f"/api/agents/{harness}").json()
    assert row["canary_capability"]["eligible"] is False
    assert row["canary_capability"]["reason_code"] == "no-runtime-arn"
    db = SessionLocal()
    db.get(Agent, harness).arn = (
        "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/hr")
    db.commit()
    db.close()
    row = client.get(f"/api/agents/{harness}").json()
    assert row["canary_capability"] == {"eligible": True, "reason": None, "reason_code": None}
    db = SessionLocal()
    for aid in (agent_id, harness):
        a = db.get(Agent, aid)
        listed = next(x for x in client.get("/api/agents").json()["agents"] if x["id"] == aid)
        assert listed["canary_capability"] == canary_capability(a)
    db.close()


def test_redeploy_rejects_name_or_method_change(client):
    agent_id = client.post("/api/agents", json=SPEC).json()["agent"]["id"]
    _activate(agent_id)
    for bad in ({"name": "renamed-agent"}, {"method": "studio"}):
        res = client.post(f"/api/agents/{agent_id}/redeploy", json={**SPEC, **bad})
        assert res.status_code == 400
        assert res.json()["code"] == "agent.redeploy_immutable"


CONVERTED_SPEC = {
    **SPEC,
    "method": "zip_runtime",
    "code_bundle": {"main.py": "print('exported')\n"},
    "source_harness": {"agent_id": "src", "harness_arn": "arn:aws:bedrock-agentcore:x:1:harness/h"},
}


def test_redeploy_of_a_converted_agent_keeps_its_baked_prompt_and_model(client, no_real_deploy):
    agent_id = client.post("/api/agents", json=CONVERTED_SPEC).json()["agent"]["id"]
    _activate(agent_id)
    for bad, field in (({"model_id": "global.moonshotai.kimi-k3"}, "model_id"),
                       ({"system_prompt": "Now answer in French."}, "system_prompt")):
        res = client.post(f"/api/agents/{agent_id}/redeploy", json={**CONVERTED_SPEC, **bad})
        assert res.status_code == 400
        assert res.json()["code"] == "agent.converted_locked"
        assert res.json()["detail"]["fields"] == [field]
    # any other edit still re-publishes
    ok = client.post(f"/api/agents/{agent_id}/redeploy",
                     json={**CONVERTED_SPEC, "memory": {"short_term": False, "long_term": False}})
    assert ok.status_code == 202


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


def test_inbound_auth_switch_route_no_longer_exists(client):
    """R1 (N3 W2 Task 3): inbound auth is AWS_IAM-only, so the in-place JWT
    switch has no mechanism left and the route itself is gone."""
    agent_id = client.post("/api/agents", json=SPEC).json()["agent"]["id"]
    res = client.post(f"/api/agents/{agent_id}/inbound-auth", json={"inbound_auth": None})
    assert res.status_code == 404


def test_agent_out_always_reports_iam(client):
    agent = client.post("/api/agents", json=SPEC).json()["agent"]
    assert agent["inbound_auth_mode"] == "iam"
    assert agent["inbound_auth_config"] is None


# --- platform toolkits (AgentSpec.toolkits) ---------------------------------

TOOLKIT_SPEC = {
    "name": "hr-toolkit-agent",
    "method": "zip_runtime",
    "system_prompt": "You are a helpful HR Assistant for Acme Corp.",
    "toolkits": ["hr_assistant"],
}


def test_toolkit_agent_is_accepted(client):
    """A toolkit is a spec FIELD, so the generated source is never written to
    spec.code/code_bundle."""
    body = client.post("/api/agents", json=TOOLKIT_SPEC).json()["agent"]
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


# ── inference / loop knobs on the ordinary path (shared configure page) ──────────
# The wizard's configure page now carries max_tokens / reasoning_effort (harness)
# and max_iterations / timeout_seconds for ordinary agents too. These pin what the
# ordinary create/redeploy routes do with them, so the shared form can round-trip
# a stored value without special-casing.

def test_ordinary_create_without_knobs_reads_back_schema_defaults(client):
    created = client.post("/api/agents", json=SPEC)
    assert created.status_code == 202, created.text
    stored = client.get(f"/api/agents/{created.json()['agent']['id']}").json()["spec"]
    assert stored["max_tokens"] is None and stored["reasoning_effort"] is None
    assert (stored["max_iterations"], stored["timeout_seconds"]) == (100, 600)


@pytest.mark.parametrize(
    "overrides",
    [
        {"method": "zip_runtime", "max_tokens": 4096},  # harness-only knob
        {"reasoning_effort": "high"},  # Claude on Converse: the effort has no wire shape
        {"model_id": "global.anthropic.claude-sonnet-5"},  # not in the CN catalog (C3)
        {"max_iterations": 0},
        {"timeout_seconds": 5},
    ],
)
def test_ordinary_knob_pairings_the_schema_refuses_are_422(client, overrides):
    res = client.post("/api/agents", json={**SPEC, **overrides})
    assert res.status_code == 422, res.text


def test_a_deleted_agent_reads_as_not_found(client, monkeypatch):
    """N10v2-bug7: the detail route answered 200 with ``status: deleted`` while
    ``/v1`` said 404 for the same row — every console entrance now agrees on 404."""
    monkeypatch.setattr(agents_router.teardown, "cleanup_residue", lambda *a, **k: [])
    monkeypatch.setattr(agents_router.zip_method, "delete_agent_resources", lambda a, w: None)
    agent_id = client.post("/api/agents", json=SPEC).json()["agent"]["id"]
    settle_deploy_jobs(agent_id)
    assert client.delete(f"/api/agents/{agent_id}").status_code == 200
    for method, path in (
        ("GET", f"/api/agents/{agent_id}"),
        ("GET", f"/api/agents/{agent_id}/versions"),
        ("GET", f"/api/agents/{agent_id}/identity"),
        ("POST", f"/api/agents/{agent_id}/invoke"),
        ("POST", f"/api/agents/{agent_id}/redeploy"),
        ("DELETE", f"/api/agents/{agent_id}"),
    ):
        body = {"prompt": "hi"} if path.endswith("/invoke") else SPEC
        res = client.request(method, path, json=body if method == "POST" else None)
        assert res.status_code == 404, (method, path, res.text)
        assert res.json()["code"] == "agent.not_found"


# ─── N-R6-obs1: a model of an unconfigured provider is refused at create / redeploy ───────
SF = "siliconflow.zai-org/GLM-5.3"
SF_ENV = "LAUNCHPAD_SILICONFLOW_API_KEY"


@pytest.mark.siliconflow
def test_create_refuses_a_model_whose_provider_is_not_configured(client, monkeypatch,
                                                                 no_real_deploy):
    monkeypatch.delenv(SF_ENV, raising=False)
    res = client.post("/api/agents", json={**SPEC, "model_id": SF})
    assert res.status_code == 422, res.text
    body = res.json()
    assert body["code"] == "model.provider_unconfigured"
    assert body["detail"] == {"model_id": SF}
    assert "SiliconFlow" in body["message"] and "launchpad-siliconflow-api-key" in body["message"]
    assert not body["message"].startswith("model.")  # readable reason, code in the envelope
    assert no_real_deploy == [] and client.get("/api/agents").json()["agents"] == []
    # configured (hub env, no record) → the ordinary 202
    monkeypatch.setenv(SF_ENV, "k")
    assert client.post("/api/agents", json={**SPEC, "model_id": SF}).status_code == 202


@pytest.mark.siliconflow
def test_create_honours_the_workspace_record_over_the_hub_env(client, monkeypatch,
                                                              no_real_deploy):
    from tests.conftest import set_default_resources

    bucket = {"artifacts_bucket": "launchpad-artifacts-test"}  # round 16: needed from stage 1
    monkeypatch.setenv(SF_ENV, "k")
    set_default_resources({**bucket, "model_credentials": {"siliconflow": {"configured": False}}})
    res = client.post("/api/agents", json={**SPEC, "model_id": SF})
    assert res.status_code == 422 and res.json()["code"] == "model.provider_unconfigured"
    monkeypatch.delenv(SF_ENV)
    set_default_resources({**bucket, "model_credentials": {"siliconflow": {"configured": True}}})
    assert client.post("/api/agents", json={**SPEC, "model_id": SF}).status_code == 202


@pytest.mark.siliconflow
def test_create_refuses_a_studio_node_model_whose_provider_is_not_configured(
        client, monkeypatch, no_real_deploy):
    """N-R6-obs2: a canvas node may carry an OpenAI-compatible catalog model; the publish is
    held to a configured provider exactly like `model_id` (the spec itself parses)."""
    monkeypatch.delenv(SF_ENV, raising=False)
    canvas = {
        "name": "studio-sf-agent", "method": "studio", "system_prompt": "canvas",
        "code": "from strands.models import BedrockModel\n"
                f"m = BedrockModel(model_id={SF!r})\n"
                "async def main(user_input, messages):\n    print(user_input)\n",
        "studio_flow": {"nodes": [{"id": "a1", "type": "agent",
                                   "data": {"modelProvider": "AWS Bedrock", "modelId": SF}}],
                        "edges": [], "graphMode": False},
    }
    res = client.post("/api/agents", json=canvas)
    assert res.status_code == 422, res.text
    body = res.json()
    assert body["code"] == "model.provider_unconfigured"
    assert "studio node modelId" in body["message"] and SF in body["message"]
    assert "launchpad-siliconflow-api-key" in body["message"]
    assert no_real_deploy == [] and client.get("/api/agents").json()["agents"] == []
    monkeypatch.setenv(SF_ENV, "k")
    assert client.post("/api/agents", json=canvas).status_code == 202


@pytest.mark.siliconflow
def test_redeploy_refuses_a_model_whose_provider_is_not_configured(client, monkeypatch,
                                                                   no_real_deploy):
    monkeypatch.delenv(SF_ENV, raising=False)
    created = client.post("/api/agents", json=SPEC).json()
    agent_id = created["agent"]["id"]
    _activate(agent_id)
    res = client.post(f"/api/agents/{agent_id}/redeploy", json={**SPEC, "model_id": SF})
    assert res.status_code == 422 and res.json()["code"] == "model.provider_unconfigured"
    detail = client.get(f"/api/agents/{agent_id}").json()
    assert detail["spec"]["model_id"] == "global.zai.glm-5.3"  # nothing stored
    assert detail["status"] == "active" and len(no_real_deploy) == 1  # no job launched
