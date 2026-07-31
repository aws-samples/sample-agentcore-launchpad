"""Harness method: payload mapping, deploy/poll happy + failure paths, invoke."""

import pytest

from app.deployer.harness import build_create_params
from app.schemas.agent import DEFAULT_MODEL_ID, AgentSpec
from app.services.agentcore import harness as hc

MEM_ARN = "arn:aws:bedrock-agentcore:us-west-2:111:memory/launchpad_memory-x"
ROLE_ARN = "arn:aws:iam::111:role/launchpad-agent-execution-role"


def spec(**over):
    base = {
        "name": "hr-assistant-v3",
        "method": "harness",
        "system_prompt": "You are an HR assistant.",
    }
    base.update(over)
    return AgentSpec(**base)


def test_build_params_basics():
    params = build_create_params(spec(), ROLE_ARN, MEM_ARN)
    assert params["harnessName"] == "hr_assistant_v3"  # hyphens not allowed
    assert params["executionRoleArn"] == ROLE_ARN
    assert params["model"]["bedrockModelConfig"]["modelId"] == DEFAULT_MODEL_ID
    assert params["systemPrompt"] == [{"text": "You are an HR assistant."}]
    assert params["memory"] == {"agentCoreMemoryConfiguration": {"arn": MEM_ARN}}


def test_build_params_mantle_source_uses_responses_api_format():
    """Bedrock Mantle models stay on the bedrockModelConfig union branch (no API
    key) and are distinguished only by apiFormat=responses."""
    s = spec(model_source="mantle", model_id="openai.gpt-5.6-sol")
    params = build_create_params(s, ROLE_ARN, MEM_ARN)
    assert params["model"] == {
        "bedrockModelConfig": {"modelId": "openai.gpt-5.6-sol", "apiFormat": "responses"}
    }


def test_build_params_bedrock_source_uses_converse_stream():
    s = spec(model_source="bedrock")
    params = build_create_params(s, ROLE_ARN, MEM_ARN)
    assert params["model"] == {
        "bedrockModelConfig": {
            "modelId": DEFAULT_MODEL_ID,
            "apiFormat": "converse_stream",
        }
    }


def test_spec_without_model_source_defaults_to_bedrock():
    """Regression for every agent stored before model_source existed: absent →
    bedrock → Converse, never Mantle."""
    stored = {
        "name": "hr-assistant-v3",
        "method": "harness",
        "system_prompt": "You are an HR assistant.",
        "model_id": DEFAULT_MODEL_ID,
    }
    s = AgentSpec(**stored)
    assert s.model_source == "bedrock"
    params = build_create_params(s, ROLE_ARN, MEM_ARN)
    assert params["model"]["bedrockModelConfig"]["apiFormat"] == "converse_stream"


def test_build_params_remote_mcp():
    """mcp-type ToolRefs (registry-picked remote servers) map to remote_mcp;
    entries without a url are dropped rather than sent malformed."""
    s = spec(tools=[
        {"type": "mcp", "name": "deepwiki", "config": {"url": "https://mcp.deepwiki.com/mcp"}},
        {"type": "mcp", "name": "no-url"},  # config empty → skipped
    ])
    params = build_create_params(s, ROLE_ARN, MEM_ARN)
    assert params["tools"] == [
        {"type": "remote_mcp", "name": "deepwiki",
         "config": {"remoteMcp": {"url": "https://mcp.deepwiki.com/mcp"}}},
    ]


def test_build_params_tools_skills_env_no_memory():
    s = spec(
        tools=[
            {"type": "builtin", "name": "code-interpreter"},
            {"type": "builtin", "name": "browser"},
            {"type": "gateway", "name": "hr-database"},  # ignored until phase 6
        ],
        skills=["skills/expense-report-writer"],
        env={"FOO": "bar"},
        memory={"short_term": False, "long_term": False},
    )
    params = build_create_params(s, ROLE_ARN, MEM_ARN)
    assert params["tools"] == [
        {"type": "agentcore_code_interpreter", "name": "code-interpreter"},
        {"type": "agentcore_browser", "name": "browser"},
    ]
    assert params["skills"] == [{"path": "skills/expense-report-writer"}]
    assert params["environmentVariables"] == {"FOO": "bar"}
    assert "memory" not in params


def test_build_params_multiple_gateway_auth_modes():
    s = spec(
        tools=[
            {"type": "gateway", "name": "finance"},
            {"type": "gateway", "name": "analytics"},
        ]
    )
    params = build_create_params(
        s,
        ROLE_ARN,
        MEM_ARN,
        gateway_attachments=[
            {
                "gateway_arn": "arn:gw:iam",
                "gateway_name": "finance-gw",
                "outbound_auth": {"awsIam": {}},
            },
            {
                "gateway_arn": "arn:gw:none",
                "gateway_name": "analytics-gw",
                "outbound_auth": {"none": {}},
            },
        ],
    )
    assert params["tools"] == [
        {
            "type": "agentcore_gateway",
            "name": "finance_gw",
            "config": {
                "agentCoreGateway": {
                    "gatewayArn": "arn:gw:iam",
                    "outboundAuth": {"awsIam": {}},
                }
            },
        },
        {
            "type": "agentcore_gateway",
            "name": "analytics_gw",
            "config": {
                "agentCoreGateway": {
                    "gatewayArn": "arn:gw:none",
                    "outboundAuth": {"none": {}},
                }
            },
        },
    ]


def test_build_params_s3_skills_use_s3_source():
    """Registry skills are S3 prefixes and must ride {"s3": {"uri": …}} — the
    `path` member is a FILESYSTEM path; s3 URIs there deploy fine but the
    harness silently never loads the skill. Legacy `…/SKILL.md` entries
    normalize to their directory."""
    s = spec(skills=[
        "s3://bkt/skills/pirate-speak/",
        "s3://bkt/skills/meeting-summarizer/SKILL.md",  # pre-bundle record format
        "core-skills/builtin",  # non-s3 → filesystem path passthrough
    ])
    params = build_create_params(s, ROLE_ARN, MEM_ARN)
    assert params["skills"] == [
        {"s3": {"uri": "s3://bkt/skills/pirate-speak/"}},
        {"s3": {"uri": "s3://bkt/skills/meeting-summarizer/"}},
        {"path": "core-skills/builtin"},
    ]


def test_wrap_params_for_update_wraps_memory():
    """UpdateHarness's memory shape is {"optionalValue": …}, unlike create —
    sending the create shape raises ParamValidationError (unknown parameter
    agentCoreMemoryConfiguration)."""
    params = build_create_params(spec(), ROLE_ARN, MEM_ARN)
    update = hc.wrap_params_for_update(params)
    assert "harnessName" not in update
    assert update["memory"] == {
        "optionalValue": {"agentCoreMemoryConfiguration": {"arn": MEM_ARN}}
    }
    assert update["systemPrompt"] == params["systemPrompt"]  # create shapes reused
    assert "memory" in params  # input not mutated into the wrapped shape


def test_wrap_params_for_update_keeps_model_source():
    """UpdateHarnessRequest.model is the same union shape as create, so the
    adapter passes it through untouched — re-publish keeps the chosen source."""
    params = build_create_params(
        spec(model_source="mantle", model_id="openai.gpt-5.6-sol"), ROLE_ARN, MEM_ARN
    )
    update = hc.wrap_params_for_update(params)
    assert update["model"] == params["model"]
    assert update["model"]["bedrockModelConfig"]["apiFormat"] == "responses"


def test_wrap_params_for_update_disables_absent_memory():
    """No memory in the params (spec memory off) must DETACH the old config —
    omitting the key would mean "keep it"."""
    s = spec(memory={"short_term": False, "long_term": False})
    update = hc.wrap_params_for_update(build_create_params(s, ROLE_ARN, MEM_ARN))
    assert update["memory"] == {"optionalValue": {"disabled": {}}}


class StubControl:
    """Captures create kwargs; walks a scripted status sequence on get."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.created_with = None
        self.updated_with = None
        self.version = "1"
        self.deleted = []

    def create_harness(self, **kwargs):
        self.created_with = kwargs
        return {"harness": {"harnessId": "h-123", "arn": "arn:h-123", "status": "CREATING"}}

    def update_harness(self, **kwargs):
        self.updated_with = kwargs
        self.version = "2"  # UpdateHarness publishes a new version
        return {"harness": {"harnessId": kwargs["harnessId"], "arn": "arn:h-123",
                            "status": "UPDATING", "harnessVersion": self.version}}

    def get_harness(self, harnessId):
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {
            "harness": {
                "harnessId": harnessId,
                "arn": "arn:h-123",
                "status": status,
                "harnessVersion": self.version,
                "failureReason": "role cannot be assumed" if "FAILED" in status else None,
            }
        }


def test_deploy_stage_update_mode_uses_update_harness(monkeypatch):
    """Re-publish (mode=update) with a live resource must call UpdateHarness
    (new version, same harnessId) — never CreateHarness."""
    from app.core.db import SessionLocal
    from app.deployer import harness as harness_deploy
    from app.deployer.pipeline import StageContext
    from app.models.ledger import Agent

    db = SessionLocal()
    agent = Agent(name="hr-assistant-v3", method="harness", status="active",
                  resource_id="h-123", arn="arn:h-123", version="1",
                  spec=spec().model_dump())
    db.add(agent)
    db.commit()
    agent_id = agent.id
    db.close()

    stub = StubControl(["READY"])
    monkeypatch.setattr(harness_deploy, "control_client", lambda: stub)

    ctx = StageContext(agent_id=agent_id, deployment_id="d1", job_id="j1")
    ctx.scratch["mode"] = "update"  # no create_params → stage regenerates them
    db = SessionLocal()
    agent = db.get(Agent, agent_id)
    db.close()
    result = harness_deploy._stage_deploy(ctx, agent)

    assert stub.updated_with is not None and stub.created_with is None
    assert stub.updated_with["harnessId"] == "h-123"
    assert "harnessName" not in stub.updated_with  # update drops the immutable name
    assert stub.updated_with["systemPrompt"] == [{"text": "You are an HR assistant."}]
    # memory is ALWAYS sent update-shaped: {"optionalValue": config-or-disabled}
    assert set(stub.updated_with["memory"]) == {"optionalValue"}
    assert "READY" in result.detail
    db = SessionLocal()
    assert db.get(Agent, agent_id).version == "2"  # new version recorded
    db.close()


def test_wait_ready_polls_to_ready():
    stub = StubControl(["CREATING", "CREATING", "READY"])
    sleeps = []
    harness = hc.wait_harness_ready(stub, "h-123", sleeper=sleeps.append)
    assert harness["status"] == "READY"
    assert len(sleeps) == 2


def test_wait_ready_raises_on_create_failed():
    stub = StubControl(["CREATING", "CREATE_FAILED"])
    with pytest.raises(RuntimeError, match="CREATE_FAILED.*role cannot be assumed"):
        hc.wait_harness_ready(stub, "h-123", sleeper=lambda _: None)


def test_wait_ready_times_out():
    stub = StubControl(["CREATING"])
    with pytest.raises(TimeoutError):
        hc.wait_harness_ready(stub, "h-123", timeout_s=0, sleeper=lambda _: None)


class StubData:
    def __init__(self, events):
        self.events = events
        self.invoked_with = None

    def invoke_harness(self, **kwargs):
        self.invoked_with = kwargs
        return {"stream": iter(self.events)}


def test_invoke_harness_text_concatenates_stream():
    stub = StubData(
        [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"delta": {"text": "2+2 "}}},
            {"contentBlockDelta": {"delta": {"text": "= 4"}}},
            {"messageStop": {"stopReason": "end_turn"}},
        ]
    )
    result = hc.invoke_harness_text(stub, "arn:h-123", "what is 2+2?")
    assert result["text"] == "2+2 = 4"
    assert len(result["session_id"]) >= 33
    assert stub.invoked_with["messages"] == [
        {"role": "user", "content": [{"text": "what is 2+2?"}]}
    ]


def test_invoke_harness_text_surfaces_runtime_error():
    stub = StubData([{"runtimeClientError": {"message": "boom"}}])
    with pytest.raises(RuntimeError, match="runtime client error"):
        hc.invoke_harness_text(stub, "arn:h-123", "hi")
