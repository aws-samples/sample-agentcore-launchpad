"""Platform-owned runtime environment injection (memory + shared Gateway)."""

from app.deployer.environment import runtime_environment
from app.schemas.agent import AgentSpec, MemoryConfig, ToolRef
from app.services.gateway_bootstrap import GATEWAY_SCOPE
from app.templates.gateway_support import provider_name

RESOURCES = {
    "memory_id": "launchpad_memory-abc123",
    "gateway_url": "https://launchpad-gw-x.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp",
    "oauth_provider_arn": (
        "arn:aws:bedrock-agentcore:us-west-2:111122223333:token-vault/default/"
        "oauth2credentialprovider/launchpad-gw-m2m"
    ),
}

PLAIN = AgentSpec(name="env-plain", method="zip_runtime", system_prompt="hi")
GATEWAY = PLAIN.model_copy(
    update={"tools": [ToolRef(type="gateway", name="launchpad-gw")]}
)


def test_plain_spec_gets_only_the_memory_id():
    """Regression for every existing agent: no gateway ToolRef ⇒ no new keys."""
    assert runtime_environment(PLAIN, RESOURCES) == {
        "LAUNCHPAD_MEMORY_ID": "launchpad_memory-abc123"
    }


def test_memory_disabled_spec_gets_nothing():
    spec = PLAIN.model_copy(
        update={"memory": MemoryConfig(short_term=False, long_term=False)}
    )
    assert runtime_environment(spec, RESOURCES) == {}


def test_gateway_spec_gets_url_provider_and_scope():
    env = runtime_environment(GATEWAY, RESOURCES)
    assert env["LAUNCHPAD_GATEWAY_URL"] == RESOURCES["gateway_url"]
    # the token exchange takes the provider NAME; it is derived from the ARN so
    # the two can never disagree
    assert env["LAUNCHPAD_GATEWAY_PROVIDER"] == "launchpad-gw-m2m"
    assert env["LAUNCHPAD_GATEWAY_SCOPE"] == GATEWAY_SCOPE
    assert "LAUNCHPAD_WORKLOAD_NAME" not in env  # unknown until the runtime exists


def test_workload_name_is_injected_when_known():
    env = runtime_environment(GATEWAY, RESOURCES, workload_name="wl-runtime-abc")
    assert env["LAUNCHPAD_WORKLOAD_NAME"] == "wl-runtime-abc"


def test_unbootstrapped_resources_leave_the_agent_gateway_less():
    """Half-set env would look configured and fail auth confusingly; absent env
    makes the generated client skip the gateway and keep its own tools."""
    assert runtime_environment(GATEWAY, {}) == {}
    assert runtime_environment(GATEWAY, {"gateway_url": "https://gw/mcp"}) == {}
    assert runtime_environment(GATEWAY, {"oauth_provider_arn": "x/prov"}) == {}


def test_user_env_is_preserved_and_platform_keys_win():
    spec = GATEWAY.model_copy(update={"env": {"MY_FLAG": "1"}})
    env = runtime_environment(spec, RESOURCES)
    assert env["MY_FLAG"] == "1"
    assert env["LAUNCHPAD_GATEWAY_PROVIDER"] == "launchpad-gw-m2m"


def test_mcp_and_kb_specs_do_not_get_gateway_env():
    """The shared-Gateway client is narrower than agent_iam._uses_gateway(): a
    remote MCP server is a different transport, and harness KBs ride the KB
    gateway, so neither should be handed launchpad-gw credentials."""
    remote = PLAIN.model_copy(
        update={"tools": [ToolRef(type="mcp", name="deepwiki",
                                 config={"url": "https://mcp.deepwiki.com/mcp"})]}
    )
    assert "LAUNCHPAD_GATEWAY_URL" not in runtime_environment(remote, RESOURCES)


def test_provider_name_handles_a_missing_arn():
    assert provider_name({}) == ""
    assert provider_name({"oauth_provider_arn": ""}) == ""


# --- runtimeUserId gating ---------------------------------------------------


def test_runtime_user_id_only_for_gateway_specs():
    """Supplying runtimeUserId is what makes the Runtime inject a workload token.
    Every other agent's invoke call must stay byte-identical to today's."""
    from app.templates.gateway_support import runtime_user_id

    assert runtime_user_id(PLAIN.model_dump(), "river") is None
    assert runtime_user_id({"tools": []}, "river") is None
    assert runtime_user_id(None, "river") is None
    assert runtime_user_id(GATEWAY.model_dump(), "river") == "river"
    # a remote MCP server is a different transport — no workload token needed
    assert runtime_user_id({"tools": [{"type": "mcp", "name": "deepwiki"}]}) is None
    # a foreign/discovered spec that would not validate as an AgentSpec is fine
    assert runtime_user_id({"tools": "not-a-list", "junk": 1}) is None
    # bounded and never empty (the API requires 1-1024 chars)
    assert runtime_user_id(GATEWAY.model_dump(), "") == "default"
    assert len(runtime_user_id(GATEWAY.model_dump(), "u" * 2000)) == 1024


def test_invoke_params_omit_runtime_user_id_unless_given():
    from app.services.agentcore.runtime import _runtime_invoke_params

    without = _runtime_invoke_params("arn:a", "hi", "s" * 33, "river", None)
    assert "runtimeUserId" not in without
    with_user = _runtime_invoke_params("arn:a", "hi", "s" * 33, "river", None, "river")
    assert with_user["runtimeUserId"] == "river"
    # everything else is unchanged, so a non-gateway agent's call is identical
    assert {k: v for k, v in with_user.items() if k != "runtimeUserId"} == without


def test_invoke_payload_carries_gateway_user_token_only_when_given():
    import json

    from app.services.agentcore.runtime import _runtime_invoke_params

    base = _runtime_invoke_params("arn:a", "hi", "s" * 33, "agent__demo", None)
    assert json.loads(base["payload"]) == {
        "prompt": "hi",
        "actor_id": "agent__demo",
    }
    authenticated = _runtime_invoke_params(
        "arn:a",
        "hi",
        "s" * 33,
        "agent__demo",
        None,
        "demo",
        "user-jwt",
    )
    assert authenticated["runtimeUserId"] == "demo"
    assert json.loads(authenticated["payload"]) == {
        "prompt": "hi",
        "actor_id": "agent__demo",
        "gateway_access_token": "user-jwt",
    }
