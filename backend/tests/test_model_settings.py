"""Every platform model is a setting: an override reaches each consumer.

Accounts without OpenAI or Anthropic access switch these (scripts/switch_models.py
writes LAUNCHPAD_* overrides); the defaults must stay exactly today's ids.
"""

import pytest

from app.core.config import Settings, get_settings

GLM = "global.zai.glm-5.3"


@pytest.fixture
def settings(monkeypatch):
    s = get_settings()

    def override(**values):
        for name, value in values.items():
            monkeypatch.setattr(s, name, value)

    return override


def test_defaults_are_unchanged():
    defaults = {name: f.default for name, f in Settings.model_fields.items()}
    assert defaults["platform_model_id"] == "global.anthropic.claude-sonnet-5"
    assert defaults["judge_model_id"] == "global.openai.gpt-6-sol"
    assert defaults["judge_fallback_model_id"] == "global.anthropic.claude-sonnet-5-5"
    assert defaults["assistant_model_id"] == "global.openai.gpt-6-sol"
    assert defaults["architect_model_id"] == "global.openai.gpt-6-astra"
    assert defaults["actor_model_id"] == "global.openai.gpt-6-luna"
    assert defaults["studio_fallback_model_id"] == "global.anthropic.claude-sonnet-5"
    assert defaults["claude_sdk_model_id"] == "global.anthropic.claude-sonnet-5"
    assert defaults["codegen_model"] == "global.anthropic.claude-sonnet-5"
    assert defaults["skill_lab_judge_model_id"] == "global.openai.gpt-6.1-sol"
    assert defaults["skill_lab_target_model_id"] == "global.anthropic.claude-opus-5-5"
    assert defaults["skill_lab_codex_target_model_id"] == "global.openai.gpt-6.1-sol"


@pytest.mark.parametrize(
    "model_id",
    [
        "global.openai.gpt-6.1-sol",
        "global.openai.gpt-6.1-terra",
        "global.openai.gpt-6-astra",
        "global.moonshotai.kimi-k3",
    ],
)
def test_codex_target_accepts_openai_and_kimi(model_id):
    from app.services import model_settings as ms

    assert ms.validate(ms.role("skill_lab_codex_target_model_id"), model_id) == model_id


@pytest.mark.parametrize("model_id", ["global.anthropic.claude-opus-5-5", GLM])
def test_codex_target_refuses_other_families(model_id):
    from app.core.errors import AppError
    from app.services import model_settings as ms

    with pytest.raises(AppError) as err:
        ms.validate(ms.role("skill_lab_codex_target_model_id"), model_id)
    assert err.value.code == "models.wrong_family"


@pytest.mark.parametrize(
    "model_id", ["global.anthropic.claude-opus-5-5", "global.anthropic.claude-sonnet-5-5"]
)
def test_claude_target_accepts_the_5_5_profiles(model_id):
    from app.services import model_settings as ms

    assert ms.validate(ms.role("skill_lab_target_model_id"), model_id) == model_id


def test_env_overrides_use_the_launchpad_prefix(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_JUDGE_MODEL_ID", GLM)
    monkeypatch.setenv("LAUNCHPAD_JUDGE_FALLBACK_MODEL_ID", "")
    monkeypatch.setenv("LAUNCHPAD_PLATFORM_MODEL_ID", GLM)
    monkeypatch.setenv("LAUNCHPAD_PROMPT_OPT_MODELS", f'["{GLM}","deepseek.v3.2"]')
    s = Settings()
    assert s.judge_model_id == s.platform_model_id == GLM
    assert s.judge_fallback_model_id == ""
    assert s.prompt_opt_models == [GLM, "deepseek.v3.2"]


# ── judge ───────────────────────────────────────────────────────────────────


class _Refusing:
    """create_evaluator that refuses the first model with the validation probe."""

    def __init__(self, refuse: set[str]):
        self.refuse, self.models = refuse, []

    def create_evaluator(self, **kwargs):
        model = kwargs["evaluatorConfig"]["llmAsAJudge"]["modelConfig"][
            "bedrockEvaluatorModelConfig"]["modelId"]
        self.models.append(model)
        if model in self.refuse:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {
                "Code": "ValidationException",
                "Message": "max_output_tokens integer_below_min_value"}}, "CreateEvaluator")
        return {"evaluatorId": "e1", "evaluatorArn": "arn:e1"}


def _create(client, model_id):
    from app.evaluation import agentcore_eval as ac

    return ac.create_llm_judge_evaluator(
        client, name="j", instructions="Rate {assistant_turn}",
        rating_scale=[{"value": 0.0, "label": "no", "definition": "no"},
                      {"value": 1.0, "label": "yes", "definition": "yes"}],
        model_id=model_id)


def test_judge_default_follows_settings(settings):
    from app.assistant.evaluation_plan import DerivedEvaluator, JudgeEvaluator
    from app.dlc import cost
    from app.evaluation import agentcore_eval as ac
    from app.evaluation.routers import JudgeCreate, JudgeUpdate

    settings(judge_model_id=GLM)
    assert ac.judge_default_model_id() == GLM
    assert JudgeCreate(name="j", instructions="Rate {assistant_turn} well").model_id == GLM
    assert JudgeUpdate().model_id == GLM
    assert JudgeEvaluator(kind="judge", key="k", title="t", name="j",
                          instructions="Rate {assistant_turn} well").model_id == GLM
    assert DerivedEvaluator(kind="derived", key="k", title="t", name="d",
                            base_evaluator_id="Builtin.Helpfulness").model_id == GLM
    assert cost._default_judge_model() == GLM


def test_judge_fallback_follows_settings(settings):
    settings(judge_fallback_model_id=GLM)
    client = _Refusing({"global.openai.gpt-6-sol"})
    out = _create(client, "global.openai.gpt-6-sol")
    assert client.models == ["global.openai.gpt-6-sol", GLM]
    assert out["launchpadModelFallback"]["used"] == GLM


def test_an_empty_judge_fallback_disables_the_retry(settings):
    from botocore.exceptions import ClientError

    from app.evaluation import agentcore_eval as ac

    settings(judge_fallback_model_id="")
    assert ac.judge_fallback_model("global.openai.gpt-6-sol") is None
    client = _Refusing({"global.openai.gpt-6-sol"})
    with pytest.raises(ClientError):
        _create(client, "global.openai.gpt-6-sol")
    assert client.models == ["global.openai.gpt-6-sol"]  # no second attempt


# ── assistant proposal ──────────────────────────────────────────────────────


def test_proposal_default_follows_settings(settings):
    from app.assistant import service
    from app.assistant.proposal import PROPOSAL_DEFAULT_MODEL_ID, ProposalContent

    assert service.protocol_preamble() == service.PROTOCOL_PREAMBLE  # default: unchanged
    settings(assistant_model_id=GLM)
    assert ProposalContent(name="ordinary-agent", system_prompt="Be helpful.").model_id == GLM
    preamble = service.protocol_preamble()
    assert f"`{GLM}` / `\"bedrock\"`" in preamble
    assert f"`{PROPOSAL_DEFAULT_MODEL_ID}` / `\"bedrock\"`" not in preamble


# ── architect system agent ──────────────────────────────────────────────────


def test_architect_install_default_follows_settings(settings):
    from app.system_agents.presets import ARCHITECT

    assert ARCHITECT.default_options().model_id == "global.openai.gpt-6-astra"
    assert ARCHITECT.default_options().reasoning_effort == "high"
    settings(architect_model_id=GLM)
    options = ARCHITECT.default_options()
    # reasoning_effort is OpenAI-only: a GLM install must not carry it
    assert options.model_id == GLM and options.reasoning_effort is None


# ── studio canvas fallback / Claude Agent SDK ───────────────────────────────


def test_studio_fallback_follows_settings(settings):
    from app.schemas.agent import AgentSpec
    from app.services import agent_iam

    flow = {"nodes": [{"id": "a", "type": "agent", "data": {}}], "edges": []}
    spec = AgentSpec(name="flow-agent", method="studio", code="print(1)", system_prompt="p",
                     studio_flow=flow)
    assert agent_iam.studio_node_models(spec) == [
        ("AWS Bedrock", "global.anthropic.claude-sonnet-5")]
    settings(studio_fallback_model_id=GLM)
    assert agent_iam.studio_node_models(spec) == [("AWS Bedrock", GLM)]


def test_claude_sdk_default_follows_settings(settings):
    from app.schemas.agent import AgentSpec

    assert AgentSpec(name="sdk-agent", method="container", system_prompt="p").model_id == (
        "global.anthropic.claude-sonnet-5")
    settings(claude_sdk_model_id="us.anthropic.claude-sonnet-5")
    assert AgentSpec(name="sdk-agent", method="container", system_prompt="p").model_id == (
        "us.anthropic.claude-sonnet-5")


# ── the console's source of truth ───────────────────────────────────────────


def test_defaults_endpoint_reports_the_configured_ids(client, settings):
    body = client.get("/api/models/defaults").json()
    assert body["judge_model_id"] == "global.openai.gpt-6-sol"
    assert body["actor_model_id"] == "global.openai.gpt-6-luna"
    assert body["agent_default_model_id"] == GLM
    settings(judge_model_id=GLM, actor_model_id=GLM, judge_fallback_model_id="")
    body = client.get("/api/models/defaults").json()
    assert body["judge_model_id"] == body["actor_model_id"] == GLM
    assert body["judge_fallback_model_id"] == ""


# ── the admin Models page: console overrides ────────────────────────────────


@pytest.fixture
def restore_settings():
    """Console writes mutate the cached Settings; put the baseline back afterwards."""
    yield
    from app.core.db import SessionLocal
    from app.services import model_settings

    db = SessionLocal()
    try:
        model_settings.apply_overrides(db)  # the table is wiped by clean_tables first
    finally:
        db.close()
    get_settings.cache_clear()


def _role(body, field):
    return next(r for r in body["roles"] if r["field"] == field)


def test_console_override_applies_at_once_and_reset_restores(client, restore_settings):
    from app.evaluation import agentcore_eval as ac

    row = _role(client.get("/api/models/settings").json(), "judge_model_id")
    assert row["source"] == "default" and row["value"] == row["default"]
    assert row["env"] == "LAUNCHPAD_JUDGE_MODEL_ID" and row["probe"] == "judge"

    saved = client.put("/api/models/settings/judge_model_id", json={"model_id": GLM})
    assert saved.status_code == 200, saved.text
    assert saved.json()["source"] == "console" and saved.json()["value"] == GLM
    # no restart: the consumers read the cached Settings the write updated
    assert ac.judge_default_model_id() == GLM
    assert client.get("/api/models/defaults").json()["judge_model_id"] == GLM

    reset = client.delete("/api/models/settings/judge_model_id").json()
    assert reset["source"] == "default" and reset["value"] == "global.openai.gpt-6-sol"
    assert ac.judge_default_model_id() == "global.openai.gpt-6-sol"


def test_console_beats_env_and_env_is_reported(client, monkeypatch, restore_settings):
    monkeypatch.setenv("LAUNCHPAD_PLATFORM_MODEL_ID", "us.amazon.nova-pro-v1:0")
    get_settings.cache_clear()
    row = _role(client.get("/api/models/settings").json(), "platform_model_id")
    assert row["source"] == "env" and row["value"] == "us.amazon.nova-pro-v1:0"
    assert row["env_value"] == "us.amazon.nova-pro-v1:0"
    row = client.put("/api/models/settings/platform_model_id", json={"model_id": GLM}).json()
    assert row["source"] == "console" and row["value"] == GLM
    # a fresh process loads the ledger override over env
    get_settings.cache_clear()
    from app.core.db import SessionLocal
    from app.services import model_settings

    db = SessionLocal()
    try:
        model_settings.apply_overrides(db)
    finally:
        db.close()
    assert get_settings().platform_model_id == GLM
    row = client.delete("/api/models/settings/platform_model_id").json()
    assert row["source"] == "env" and row["value"] == "us.amazon.nova-pro-v1:0"


def test_bad_values_are_refused(client, restore_settings):
    assert client.put("/api/models/settings/nope", json={"model_id": GLM}).status_code == 404
    bad = client.put("/api/models/settings/judge_model_id", json={"model_id": "not an id"})
    assert bad.status_code == 400
    assert client.put("/api/models/settings/judge_model_id",
                      json={"model_id": ""}).status_code == 400
    # Claude-only features refuse another family
    wrong = client.put("/api/models/settings/codegen_model", json={"model_id": GLM})
    assert wrong.status_code == 400 and "anthropic" in wrong.text
    # the judge fallback may be disabled
    ok = client.put("/api/models/settings/judge_fallback_model_id", json={"model_id": ""})
    assert ok.status_code == 200 and ok.json()["value"] == ""


def test_admin_routes_are_admin_only():
    from app.core.route_policy import ADMIN, MEMBER, ROUTE_POLICY

    assert ROUTE_POLICY[("GET", "/api/models/settings")] == ADMIN
    assert ROUTE_POLICY[("PUT", "/api/models/settings/{field}")] == ADMIN
    assert ROUTE_POLICY[("DELETE", "/api/models/settings/{field}")] == ADMIN
    assert ROUTE_POLICY[("POST", "/api/model-probe")] == ADMIN
    assert ROUTE_POLICY[("GET", "/api/models/defaults")] == MEMBER
    assert ROUTE_POLICY[("GET", "/api/model-catalog")] == MEMBER


# ── catalog + probe ─────────────────────────────────────────────────────────


class _Bedrock:
    def list_inference_profiles(self, **_):
        return {"inferenceProfileSummaries": [
            {"inferenceProfileId": GLM, "inferenceProfileName": "GLM 5.3", "status": "ACTIVE"},
            {"inferenceProfileId": "global.cohere.embed-v4:0", "status": "ACTIVE"},
        ]}

    def list_foundation_models(self, **_):
        return {"modelSummaries": [
            {"modelId": "deepseek.v3.2", "modelName": "DeepSeek V3.2",
             "inferenceTypesSupported": ["ON_DEMAND"]},
            {"modelId": "zai.glm-4.6", "inferenceTypesSupported": ["PROVISIONED"]},
        ]}


class _Runtime:
    def __init__(self, deny=()):
        self.deny, self.calls = set(deny), []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["modelId"] in self.deny:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "AccessDeniedException",
                                         "Message": "no access"}}, "Converse")
        return {"output": {"message": {"content": [{"text": "OK"}]}}}


class _Control:
    def __init__(self):
        self.created, self.deleted = [], []

    def create_evaluator(self, **kwargs):
        self.created.append(kwargs)
        return {"evaluatorId": kwargs["evaluatorName"] + "-x"}

    def delete_evaluator(self, evaluatorId):
        self.deleted.append(evaluatorId)


class _Ctx:
    account_id, region = "111122223333", "us-east-1"

    def __init__(self, runtime=None):
        self.runtime, self.control = runtime or _Runtime(), _Control()

    def client(self, service, **_):
        return {"bedrock": _Bedrock(), "bedrock-runtime": self.runtime,
                "bedrock-agentcore-control": self.control}[service]


def test_catalog_lists_invocable_text_models():
    from app.services import model_settings

    out = model_settings.catalog(_Ctx(), force=True)
    ids = [m["model_id"] for m in out["models"]]
    assert ids == ["deepseek.v3.2", GLM]  # embeddings + provisioned-only dropped
    assert out["models"][1]["provider"] == "zai"


def test_judge_probe_converses_and_creates_then_deletes_an_evaluator():
    from app.services import model_settings

    ctx = _Ctx()
    out = model_settings.probe(ctx, "judge_model_id", GLM)
    assert out["ok"] and [c["name"] for c in out["checks"]] == ["converse", "evaluator"]
    assert ctx.runtime.calls[0]["inferenceConfig"] == {"maxTokens": 16}
    name = ctx.control.created[0]["evaluatorName"]
    assert name.startswith("zz_probe_") and ctx.control.deleted == [name + "-x"]


def test_actor_probe_checks_forced_tool_choice_and_reports_denials():
    from app.services import model_settings

    ctx = _Ctx(_Runtime(deny={"global.openai.gpt-6-luna"}))
    out = model_settings.probe(ctx, "actor_model_id", "global.openai.gpt-6-luna")
    assert not out["ok"]
    assert [c["name"] for c in out["checks"]] == ["converse", "tool_choice"]
    assert out["checks"][0]["error"].startswith("AccessDeniedException")
    assert ctx.runtime.calls[1]["toolConfig"]["toolChoice"] == {"any": {}}


def test_new_agent_default_follows_settings(settings):
    from app.schemas.agent import AgentSpec

    assert AgentSpec(name="plain-agent", method="harness", system_prompt="p").model_id == GLM
    settings(agent_default_model_id="us.amazon.nova-pro-v1:0")
    assert AgentSpec(name="plain-agent", method="harness",
                     system_prompt="p").model_id == "us.amazon.nova-pro-v1:0"


# ── DeepSeek / Kimi agents (live-probed 2026-10-12: Converse + tool use + stream) ──

TOOL_CAPABLE = ["deepseek.v3.2", "global.moonshotai.kimi-k3", "us.moonshotai.kimi-k3",
                "moonshotai.kimi-k2.5", "moonshot.kimi-k2-thinking"]


@pytest.mark.parametrize("model_id", TOOL_CAPABLE)
def test_deepseek_and_kimi_agents_build_on_every_path(model_id):
    from app.deployer.harness import model_config
    from app.schemas.agent import AgentSpec
    from app.services import agent_iam
    from app.services.agent_iam import RoleContext
    from app.templates.strands_agent import render_main_py

    harness = AgentSpec(name="ds-agent", method="harness", system_prompt="p", model_id=model_id)
    assert model_config(harness) == {"modelId": model_id, "apiFormat": "converse_stream"}
    zip_spec = AgentSpec(name="ds-agent", method="zip_runtime", system_prompt="p",
                         model_id=model_id)
    assert f'MODEL_ID = "{model_id}"' in render_main_py(zip_spec)
    ctx = RoleContext(account_id="123456789012", region="us-east-1",
                      artifacts_bucket="b", ecr_repo_arn="arn:aws:ecr:us-east-1:1:repository/r")
    resources = agent_iam.model_resources(model_id, ctx)
    bare = model_id.split(".", 1)[1] if model_id.startswith(("global.", "us.")) else model_id
    assert f"arn:aws:bedrock:*::foundation-model/{bare}" in resources
    flow = {"nodes": [{"id": "a", "type": "agent",
                       "data": {"modelProvider": "AWS Bedrock", "modelId": model_id}}]}
    studio = AgentSpec(name="ds-flow", method="studio", code="print(1)", system_prompt="p",
                       studio_flow=flow)
    assert agent_iam.studio_node_models(studio) == [("AWS Bedrock", model_id)]


def test_deepseek_r1_is_refused_for_tool_roles_only(client, restore_settings):
    r1 = "us.deepseek.r1-v1:0"
    for field in ("agent_default_model_id", "assistant_model_id", "actor_model_id",
                  "architect_model_id", "studio_fallback_model_id"):
        bad = client.put(f"/api/models/settings/{field}", json={"model_id": r1})
        assert bad.status_code == 400 and "tool use" in bad.text, field
    for field in ("platform_model_id", "judge_model_id", "prompt_opt_default_model_id"):
        assert client.put(f"/api/models/settings/{field}",
                          json={"model_id": r1}).status_code == 200, field


def test_tool_roles_probe_tool_use():
    from app.services import model_settings

    ctx = _Ctx()
    out = model_settings.probe(ctx, "assistant_model_id", "deepseek.v3.2")
    assert [c["name"] for c in out["checks"]] == ["converse", "tool_use"]
    assert "toolConfig" in ctx.runtime.calls[1]


@pytest.mark.parametrize(("model_id", "priced"), [
    ("deepseek.v3.2", True), ("deepseek.v3-v1:0", True), ("moonshotai.kimi-k2.5", True),
    ("moonshot.kimi-k2-thinking", True), ("global.zai.glm-5.3", True),
    ("global.moonshotai.kimi-k3", True), ("us.deepseek.r1-v1:0", True),
    ("global.openai.gpt-6-sol", True),  # from its Bedrock model card
    ("global.openai.gpt-6-luna", True),  # main's built-in Luna price
])
def test_deepseek_and_kimi_prices(model_id, priced):
    from app.services.observability import match_price

    defaults = Settings.model_fields["model_prices"].default
    assert (match_price(model_id, defaults) is not None) is priced
    if model_id == "global.moonshotai.kimi-k3":
        assert match_price(model_id, defaults)["input"] == 3.00
        assert match_price("us.moonshotai.kimi-k3", defaults)["input"] == 3.30


def test_a_console_override_survives_a_runtime_cache_clear(client, restore_settings):
    """The price refresher and a bootstrap call get_settings.cache_clear() at runtime;
    the rebuilt Settings must still carry the console value (core/config overlay)."""
    from app.evaluation import agentcore_eval as ac

    assert client.put("/api/models/settings/judge_model_id",
                      json={"model_id": GLM}).status_code == 200
    get_settings.cache_clear()  # what model_prices.refresh / workspace bootstrap do
    assert get_settings().judge_model_id == GLM
    assert ac.judge_default_model_id() == GLM
    row = _role(client.get("/api/models/settings").json(), "judge_model_id")
    assert row["source"] == "console" and row["value"] == GLM
    client.delete("/api/models/settings/judge_model_id")
    get_settings.cache_clear()
    assert get_settings().judge_model_id == "global.openai.gpt-6-sol"
