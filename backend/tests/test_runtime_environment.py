"""Platform-owned runtime environment injection (memory + shared Gateway)."""

import json

from app.deployer.environment import runtime_environment
from app.schemas.agent import AgentSpec, KnowledgeBaseRef, MemoryConfig, ToolRef
from app.services import embedding_backend

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

# Every spec gets these two now (spec C3/C4): the templates read them to exchange
# the workload token for the Global Bedrock API key.
MODEL_ENV = {
    "LAUNCHPAD_MODEL_REGION": "us-west-2",
    "LAUNCHPAD_MODEL_CREDENTIAL": "launchpad-global-bedrock",
}


def test_plain_spec_gets_only_the_memory_id():
    """Regression for every existing agent: no gateway ToolRef ⇒ no new keys."""
    assert runtime_environment(PLAIN, RESOURCES) == {
        **MODEL_ENV,
        "LAUNCHPAD_MEMORY_ID": "launchpad_memory-abc123",
    }


def test_memory_disabled_spec_gets_nothing():
    spec = PLAIN.model_copy(
        update={"memory": MemoryConfig(short_term=False, long_term=False)}
    )
    assert runtime_environment(spec, RESOURCES) == MODEL_ENV


def test_a_spec_pinned_memory_overrides_the_workspace_default():
    spec = PLAIN.model_copy(
        update={"memory": MemoryConfig(short_term=True, memory_id="team_notes-XYZ789")}
    )
    assert runtime_environment(spec, RESOURCES) == {
        **MODEL_ENV,
        "LAUNCHPAD_MEMORY_ID": "team_notes-XYZ789",
    }
    # the pin also works before bootstrap has recorded a workspace memory
    assert runtime_environment(spec, {}) == {
        **MODEL_ENV,
        "LAUNCHPAD_MEMORY_ID": "team_notes-XYZ789",
    }


# --- CN memory/KB (N6): the self-hosted substrate keys ------------------------

SUBSTRATE = {
    **RESOURCES,
    "memory_bucket": "bkt",
    "memory_prefix": "memory",
    "memory_os_endpoint": "search-mem.example",
    "memory_queue_url": "https://sqs.example/q",
}


def test_substrate_resources_add_the_memory_env():
    block = embedding_backend.extract_model_block()
    assert block, "the hermetic settings name the memory_extract role's default row"
    assert runtime_environment(PLAIN, SUBSTRATE) == {
        **MODEL_ENV,
        "LAUNCHPAD_MEMORY_ID": "launchpad_memory-abc123",
        "LAUNCHPAD_MEMORY_BUCKET": "bkt",
        "LAUNCHPAD_MEMORY_PREFIX": "memory",
        "LAUNCHPAD_MEMORY_OS_ENDPOINT": "search-mem.example",
        "LAUNCHPAD_MEMORY_QUEUE_URL": "https://sqs.example/q",
        "LAUNCHPAD_MEMORY_LONG_TERM": "0",
        # F-LOAD-02 (N17-obs533): the memory_extract role's row rides next to the queue URL
        "LAUNCHPAD_MEMORY_EXTRACT_MODEL": json.dumps(block, separators=(",", ":"),
                                                    sort_keys=True),
    }


def test_the_extract_model_block_rides_with_the_queue_url_and_is_absent_without_a_row(
        monkeypatch):
    """F-LOAD-02 (N17-obs533, ten-student simulation 2026-10-30): the Runtime's own
    `memory_extract` messages carried no `extract_model`, so the extractor ran its env fallback
    model for every agent-side turn whatever the role said. The deployer now bakes the hub's
    `extract_model_block()` (incl. the F-LOAD-03 `temperature` capability) into
    `LAUNCHPAD_MEMORY_EXTRACT_MODEL` as compact JSON wherever it writes the queue URL; a role
    without a catalog row writes nothing (the rendered library then omits the block)."""
    block = {"model_id": "global.moonshotai.kimi-k3", "kind": "bedrock", "base_url": None,
             "api_model_name": "global.moonshotai.kimi-k3",
             "credential_provider": "launchpad-global-bedrock", "provider": "bedrock",
             "model_region": "us-west-2", "catalog_version": 3, "temperature": False}
    monkeypatch.setattr(embedding_backend, "extract_model_block", lambda: block)
    env = runtime_environment(PLAIN, SUBSTRATE)
    assert json.loads(env["LAUNCHPAD_MEMORY_EXTRACT_MODEL"]) == block
    assert " " not in env["LAUNCHPAD_MEMORY_EXTRACT_MODEL"]  # compact: one env value
    # written exactly where the queue URL is: no substrate → neither key
    assert "LAUNCHPAD_MEMORY_EXTRACT_MODEL" not in runtime_environment(PLAIN, RESOURCES)
    # the role names no row → the key is absent, the Lambda's env fallback stands
    monkeypatch.setattr(embedding_backend, "extract_model_block", lambda: None)
    env = runtime_environment(PLAIN, SUBSTRATE)
    assert env["LAUNCHPAD_MEMORY_QUEUE_URL"] and "LAUNCHPAD_MEMORY_EXTRACT_MODEL" not in env


def test_long_term_flag_and_kb_endpoint():
    spec = PLAIN.model_copy(update={
        "memory": MemoryConfig(short_term=True, long_term=True),
        "knowledge_bases": [KnowledgeBaseRef(kb_id="KB1")],
    })
    env = runtime_environment(spec, SUBSTRATE)
    assert env["LAUNCHPAD_MEMORY_LONG_TERM"] == "1"
    assert env["LAUNCHPAD_KB_OS_ENDPOINT"] == "search-mem.example"
    assert "LAUNCHPAD_KB_OS_ENDPOINT" not in runtime_environment(PLAIN, SUBSTRATE)


def test_memory_off_or_no_substrate_adds_no_memory_keys():
    """Without the endpoint the rendered library must see 'memory off' for the substrate
    keys (only the legacy id remains); a memory-less spec gets nothing even with it."""
    off = PLAIN.model_copy(update={"memory": MemoryConfig(short_term=False, long_term=False)})
    assert runtime_environment(off, SUBSTRATE) == MODEL_ENV
    env = runtime_environment(PLAIN, RESOURCES)
    assert env == {**MODEL_ENV, "LAUNCHPAD_MEMORY_ID": "launchpad_memory-abc123"}
    # a KB mount without the substrate: no endpoint key (the tools fold the error)
    kb = PLAIN.model_copy(update={"knowledge_bases": [KnowledgeBaseRef(kb_id="KB1")]})
    assert "LAUNCHPAD_KB_OS_ENDPOINT" not in runtime_environment(kb, RESOURCES)


def test_substrate_bucket_falls_back_to_the_artifacts_bucket():
    resources = {k: v for k, v in SUBSTRATE.items() if k not in ("memory_bucket", "memory_prefix")}
    env = runtime_environment(PLAIN, {**resources, "artifacts_bucket": "artifacts"})
    assert env["LAUNCHPAD_MEMORY_BUCKET"] == "artifacts"
    assert env["LAUNCHPAD_MEMORY_PREFIX"] == "memory"


def test_every_spec_gets_the_model_region_and_credential():
    spec = PLAIN.model_copy(update={"model_region": "us-east-1", "model_credential": "team-key"})
    env = runtime_environment(spec, {})
    assert env["LAUNCHPAD_MODEL_REGION"] == "us-east-1"
    assert env["LAUNCHPAD_MODEL_CREDENTIAL"] == "team-key"


def test_workload_name_is_injected_for_every_spec_once_known():
    env = runtime_environment(PLAIN, {}, workload_name="rt_abc-0123456789")
    assert env["LAUNCHPAD_WORKLOAD_NAME"] == "rt_abc-0123456789"
    assert "LAUNCHPAD_WORKLOAD_NAME" not in runtime_environment(PLAIN, {})


def test_gateway_spec_gets_only_the_url_and_tool_patterns():
    """AWS_IAM Gateway, SigV4-signed with the runtime's own execution role: no
    OAuth provider/scope to resolve, and no outbound token to mint — so no
    workload identity name is needed either."""
    env = runtime_environment(GATEWAY, RESOURCES)
    assert env["LAUNCHPAD_GATEWAY_URL"] == RESOURCES["gateway_url"]
    assert "LAUNCHPAD_GATEWAY_PROVIDER" not in env
    assert "LAUNCHPAD_GATEWAY_SCOPE" not in env
    assert "LAUNCHPAD_WORKLOAD_NAME" not in env



def test_unbootstrapped_resources_leave_the_agent_gateway_less():
    """Absent gateway env makes the generated client skip the gateway and keep its own
    tools; the URL alone (no OAuth provider ARN) is sufficient now. The model env is
    always present (every agent fetches the Global Bedrock key)."""
    assert runtime_environment(GATEWAY, {}) == MODEL_ENV
    env = runtime_environment(GATEWAY, {"gateway_url": "https://gw/mcp"})
    assert env["LAUNCHPAD_GATEWAY_URL"] == "https://gw/mcp"
    assert MODEL_ENV.items() <= env.items()
    # an oauth_provider_arn with no gateway_url is simply irrelevant now
    assert runtime_environment(GATEWAY, {"oauth_provider_arn": "x/prov"}) == MODEL_ENV


def test_user_env_is_preserved_and_platform_keys_win():
    spec = GATEWAY.model_copy(update={"env": {"MY_FLAG": "1"}})
    env = runtime_environment(spec, RESOURCES)
    assert env["MY_FLAG"] == "1"
    assert env["LAUNCHPAD_GATEWAY_URL"] == RESOURCES["gateway_url"]


def test_mcp_and_kb_specs_do_not_get_gateway_env():
    """The shared-Gateway client is narrower than agent_iam._uses_gateway(): a
    remote MCP server is a different transport, and harness KBs ride the KB
    gateway, so neither should be handed launchpad-gw credentials."""
    remote = PLAIN.model_copy(
        update={"tools": [ToolRef(type="mcp", name="deepwiki",
                                 config={"url": "https://mcp.deepwiki.com/mcp"})]}
    )
    assert "LAUNCHPAD_GATEWAY_URL" not in runtime_environment(remote, RESOURCES)


# --- runtimeUserId gating ---------------------------------------------------


def test_runtime_user_id_is_sent_for_every_agent():
    """Supplying runtimeUserId is what makes the Runtime inject a workload token, and
    every generated agent now exchanges that token for the Global Bedrock API key."""
    from app.templates.gateway_support import runtime_user_id

    assert runtime_user_id(PLAIN.model_dump(), "river") == "river"
    assert runtime_user_id({"tools": []}, "river") == "river"
    assert runtime_user_id(None, "river") == "river"
    assert runtime_user_id({"tools": "not-a-list", "junk": 1}, "river") == "river"
    auth_tools = [{"type": "rest", "name": "t", "auth": {"connection": "c"}}]
    # bounded and never empty (the API requires 1-1024 chars)
    assert runtime_user_id({"tools": auth_tools}, "") == "default"
    assert len(runtime_user_id({"tools": auth_tools}, "u" * 2000)) == 1024


def test_invoke_params_omit_runtime_user_id_unless_given():
    from app.services.agentcore.runtime import _runtime_invoke_params

    without = _runtime_invoke_params("arn:a", "hi", "s" * 33, "river", None)
    assert "runtimeUserId" not in without
    with_user = _runtime_invoke_params("arn:a", "hi", "s" * 33, "river", None, "river")
    assert with_user["runtimeUserId"] == "river"
    # everything else is unchanged, so a non-gateway agent's call is identical
    assert {k: v for k, v in with_user.items() if k != "runtimeUserId"} == without


def test_gateway_tool_filter_is_target_scoped_per_record():
    spec = PLAIN.model_copy(update={"tools": [
        ToolRef(type="gateway", name="hr-database",
                config={"record_id": "r1", "gateway_id": "g",
                        "tools": ["hr-database___get_employee"]}),
        ToolRef(type="gateway", name="office-facts", config={"record_id": "r2", "gateway_id": "g"}),
    ]})
    env = runtime_environment(spec, RESOURCES)
    assert env["LAUNCHPAD_GATEWAY_TOOLS"] == "hr-database___get_employee,office-facts___*"


# ─── routed config bundles (N12 E3-5): the runtime reads bundle JSON from S3 ─────
def test_bundle_bucket_is_injected_once_the_workspace_has_an_artifacts_bucket():
    env = runtime_environment(PLAIN, {**RESOURCES, "artifacts_bucket": "lp-artifacts"})
    assert env["LAUNCHPAD_BUNDLE_BUCKET"] == "lp-artifacts"
    # every Runtime method's generated code carries the same _config_bundle() seam
    studio = PLAIN.model_copy(update={"method": "studio"})
    assert runtime_environment(studio, {"artifacts_bucket": "lp-artifacts"})[
        "LAUNCHPAD_BUNDLE_BUCKET"] == "lp-artifacts"


def test_bundle_bucket_is_absent_before_bootstrap():
    """No artifacts bucket yet ⇒ no key: the rendered reader treats a missing bucket as
    "no routed bundle" instead of failing on an empty name."""
    assert "LAUNCHPAD_BUNDLE_BUCKET" not in runtime_environment(PLAIN, RESOURCES)


# --- N16-obs72: the platform's reserved share of the Runtime's 50-variable cap -----------


def test_the_reserved_env_budget_covers_every_key_a_maximal_configuration_injects(monkeypatch):
    """Lead ruling 2026-10-19: `AgentSpec.env` is capped at 50 − PLATFORM_ENV_RESERVED and
    the reserved count must never undercount what the deployers inject. A configuration
    with every optional feature on must produce EXACTLY `PLATFORM_ENV_KEYS` (plus byoc's two
    `setdefault`s, listed there too) — a new injected key fails here until it is listed and
    the schema cap lowered — and the two modules' constants must agree."""
    from app.core import naming
    from app.core.config import get_settings
    from app.deployer import environment as env_mod
    from app.schemas import agent as agent_schema
    from app.services import memory_selfhosted

    assert env_mod.PLATFORM_ENV_RESERVED == agent_schema.PLATFORM_ENV_RESERVED == 28
    assert agent_schema.AGENT_ENV_MAX_VARS + env_mod.PLATFORM_ENV_RESERVED == 50
    assert agent_schema.RUNTIME_ENV_LIMIT == 50
    assert len(set(env_mod.PLATFORM_ENV_KEYS)) == len(env_mod.PLATFORM_ENV_KEYS)
    # the literal names in the tuple are the modules' constants
    assert {memory_selfhosted.ENV_MEMORY_HALF_LIFE_DAYS, memory_selfhosted.ENV_MEMORY_TTL_DAYS,
            } <= set(env_mod.PLATFORM_ENV_KEYS)
    # Global parity: the policy module writes no runtime key any more
    assert not any(k.startswith("LAUNCHPAD_POLICY_") for k in env_mod.PLATFORM_ENV_KEYS)

    # everything on: both memory tiers with recall knobs, a KB, the Gateway, an as_user tool,
    # a named environment, both continuation knobs changed
    monkeypatch.setattr(memory_selfhosted, "recall_env_for", lambda mid: {
        memory_selfhosted.ENV_MEMORY_HALF_LIFE_DAYS: "7",
        memory_selfhosted.ENV_MEMORY_TTL_DAYS: "30",
    })
    monkeypatch.setattr(naming, "current_env_name", lambda: "t1")
    monkeypatch.setattr(embedding_backend, "extract_model_block", lambda: {
        "model_id": "global.zai.glm-5.3", "kind": "bedrock", "temperature": True})
    settings = get_settings()
    monkeypatch.setattr(settings, "max_tokens_continuations", 3)
    monkeypatch.setattr(settings, "continuation_max_tokens", 64)
    spec = PLAIN.model_copy(update={
        "memory": MemoryConfig(short_term=True, long_term=True),
        "knowledge_bases": [KnowledgeBaseRef(kb_id="kb1", name="kb")],
        "tools": [ToolRef(type="gateway", name="launchpad-gw"),
                  ToolRef(type="rest", name="crm", config={"url": "https://example.com"},
                          auth={"connection": "crm-conn", "kind": "oauth2", "mode": "as_user"})],
        "env": {"MY_VAR": "1"},
    })
    resources = {
        **RESOURCES, "artifacts_bucket": "artifacts", "memory_bucket": "mem",
        "memory_queue_url": "https://sqs/q", "memory_os_endpoint": "https://os.example",
        "memory_prefix": "memory",
    }
    environment = env_mod.runtime_environment(
        spec, resources, workload_name="wl-1", agent_id="ag-1",
    )
    environment.setdefault("MODEL_ID", spec.model_id)  # deployer/byoc.py on top
    environment.setdefault("ALLOWED_MODEL_IDS", "x")
    # protocol a2a only (N16-obs90) — a zip_runtime http spec above never carries it
    environment.setdefault(env_mod.ENV_A2A_ACTOR_KEY, "x")
    injected = set(environment) - set(spec.env)
    assert injected == set(env_mod.PLATFORM_ENV_KEYS), (
        sorted(injected ^ set(env_mod.PLATFORM_ENV_KEYS)))
    assert len(injected) <= env_mod.PLATFORM_ENV_RESERVED
    assert len(injected) + agent_schema.AGENT_ENV_MAX_VARS <= 50


def test_a2a_runtimes_get_the_per_agent_actor_key_and_http_runtimes_do_not(monkeypatch):
    """N16-obs90: ``LAUNCHPAD_A2A_ACTOR_KEY`` = ``a2a_actor.actor_key(agent_id)`` is injected
    for protocol ``a2a`` only, is per agent, and is derived from (never equal to) the console
    secret — nothing is stored for it."""
    from app.deployer import environment as env_mod
    from app.services import a2a_actor

    real = a2a_actor.actor_key
    monkeypatch.setattr(a2a_actor, "actor_key",
                        lambda agent_id, secret=None: real(agent_id, secret="fake-secret"))
    a2a = PLAIN.model_copy(update={"protocol": "a2a"})
    env_a2a = runtime_environment(a2a, RESOURCES, agent_id="ag-1")
    env_http = runtime_environment(PLAIN, RESOURCES, agent_id="ag-1")
    key = env_a2a[env_mod.ENV_A2A_ACTOR_KEY]
    assert key == real("ag-1", secret="fake-secret") and len(key) == 64
    assert key != real("ag-2", secret="fake-secret")
    assert key != "fake-secret"
    assert env_mod.ENV_A2A_ACTOR_KEY not in env_http
    assert env_mod.ENV_A2A_ACTOR_KEY not in runtime_environment(a2a, RESOURCES)  # no agent id yet
