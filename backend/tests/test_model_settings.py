"""Platform model roles: defaults, consumers, console overrides, the probe.

Every model the platform itself calls or pre-selects is a Settings field; an
administrator overrides one on the Models page (ledger ``platform_settings``). The
override must reach each consumer, survive every ``get_settings.cache_clear()``
and a restart, and a reset must return the role to env / yaml / default.
"""

from datetime import UTC, datetime, timedelta

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.services import model_settings
from tests.test_route_policy import ADMIN_CREDS, MEMBER_CREDS

GPT_SOL = "global.openai.gpt-6-sol"
SONNET_5 = "global.anthropic.claude-sonnet-5"
GLM = "global.zai.glm-5.3"


@pytest.fixture
def settings(monkeypatch):
    """Patch the cached Settings (stands in for env / launchpad.yaml)."""
    current = get_settings()

    def override(**values):
        for name, value in values.items():
            monkeypatch.setattr(current, name, value)

    return override


def _db_call(fn, *args):
    db = SessionLocal()
    try:
        return fn(db, *args)
    finally:
        db.close()


# ── defaults ────────────────────────────────────────────────────────────────


def test_defaults_are_mains_values_with_the_unified_agent_default():
    defaults = {name: f.default for name, f in Settings.model_fields.items()}
    assert defaults["agent_default_model_id"] == GPT_SOL
    assert defaults["claude_sdk_model_id"] == SONNET_5
    assert defaults["judge_model_id"] == GPT_SOL
    assert defaults["judge_fallback_model_id"] == "global.anthropic.claude-sonnet-5-5"
    assert defaults["assistant_model_id"] == GPT_SOL
    assert defaults["architect_model_id"] == "global.openai.gpt-6-astra"
    assert defaults["actor_model_id"] == "global.openai.gpt-6-luna"
    assert defaults["studio_fallback_model_id"] == SONNET_5
    assert defaults["codegen_model"] == SONNET_5
    assert defaults["prompt_opt_default_model_id"] == "global.anthropic.claude-opus-5"
    assert defaults["skill_lab_judge_model_id"] == "us.openai.gpt-6-sol"
    assert defaults["skill_lab_target_model_id"] == "global.anthropic.claude-opus-5-5"
    assert defaults["skill_lab_codex_target_model_id"] == GPT_SOL
    assert "platform_model_id" not in defaults  # no consumer on this build
    assert {r.field for r in model_settings.ROLES} == {
        "agent_default_model_id", "claude_sdk_model_id", "judge_model_id",
        "judge_fallback_model_id", "assistant_model_id", "architect_model_id",
        "actor_model_id", "studio_fallback_model_id", "codegen_model",
        "prompt_opt_default_model_id", "skill_lab_judge_model_id",
        "skill_lab_target_model_id", "skill_lab_codex_target_model_id",
    }


def test_env_overrides_use_the_launchpad_prefix(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_JUDGE_MODEL_ID", GLM)
    monkeypatch.setenv("LAUNCHPAD_JUDGE_FALLBACK_MODEL_ID", "")
    s = Settings()
    assert s.judge_model_id == GLM and s.judge_fallback_model_id == ""


# ── agent specs ─────────────────────────────────────────────────────────────


def test_a_spec_without_a_model_gets_the_agent_default(settings):
    from app.schemas.agent import AgentSpec

    for method in ("harness", "zip_runtime"):
        spec = AgentSpec(name="plain-agent", method=method, system_prompt="p")
        assert spec.model_id == GPT_SOL and spec.model_source == "bedrock"
    settings(agent_default_model_id="us.amazon.nova-pro-v1:0")
    assert AgentSpec(name="plain-agent", method="harness",
                     system_prompt="p").model_id == "us.amazon.nova-pro-v1:0"
    # a stored spec carries its own model_id: unaffected
    stored = AgentSpec.model_validate({"name": "old-agent", "method": "harness",
                                       "system_prompt": "p", "model_id": SONNET_5})
    assert stored.model_id == SONNET_5


def test_a_container_spec_without_a_model_gets_claude_never_the_agent_default(settings):
    from app.schemas.agent import AgentSpec

    spec = AgentSpec(name="sdk-agent", method="container", system_prompt="p")
    assert spec.model_id == SONNET_5
    settings(claude_sdk_model_id="us.anthropic.claude-sonnet-5",
             agent_default_model_id=GLM)
    assert AgentSpec(name="sdk-agent", method="container",
                     system_prompt="p").model_id == "us.anthropic.claude-sonnet-5"
    # an explicit id is the caller's call
    explicit = AgentSpec(name="sdk-agent", method="container", system_prompt="p",
                         model_id="global.anthropic.claude-opus-5")
    assert explicit.model_id == "global.anthropic.claude-opus-5"
    # round trip: the resolved id is stored, so a re-read keeps it
    again = AgentSpec.model_validate(spec.model_dump())
    assert again.model_id == SONNET_5


def test_no_code_reads_the_static_model_id_default():
    """``AgentSpec.model_id`` is a default_factory now, so its ``.default`` is
    PydanticUndefined — a reader must call ``agent_default_model_id()`` instead."""
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders = [
        str(path.relative_to(app_dir)) for path in app_dir.rglob("*.py")
        if 'model_fields["model_id"].default' in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_studio_fallback_is_its_own_role_not_the_agent_default(settings):
    from app.schemas.agent import AgentSpec
    from app.services import agent_iam

    flow = {"nodes": [{"id": "a", "type": "agent", "data": {}}], "edges": []}
    spec = AgentSpec(name="flow-agent", method="studio", code="print(1)", system_prompt="p",
                     studio_flow=flow)
    assert agent_iam.studio_node_models(spec) == [("AWS Bedrock", SONNET_5)]
    settings(agent_default_model_id=GLM)  # moving the agent default moves nothing here
    assert agent_iam.studio_node_models(spec) == [("AWS Bedrock", SONNET_5)]
    settings(studio_fallback_model_id="us.anthropic.claude-sonnet-5")
    assert agent_iam.studio_node_models(spec) == [
        ("AWS Bedrock", "us.anthropic.claude-sonnet-5")]


# ── other consumers ─────────────────────────────────────────────────────────


class _Refusing:
    """create_evaluator refusing some models with the judge validation probe."""

    def __init__(self, refuse: set[str]):
        self.refuse, self.models = refuse, []

    def create_evaluator(self, **kwargs):
        model = kwargs["evaluatorConfig"]["llmAsAJudge"]["modelConfig"][
            "bedrockEvaluatorModelConfig"]["modelId"]
        self.models.append(model)
        if model in self.refuse:
            raise ClientError({"Error": {
                "Code": "ValidationException",
                "Message": "max_output_tokens integer_below_min_value"}}, "CreateEvaluator")
        return {"evaluatorId": "e1", "evaluatorArn": "arn:e1"}


def _create_judge(client, model_id):
    from app.evaluation import agentcore_eval as ac

    return ac.create_llm_judge_evaluator(
        client, name="j", instructions="Rate {assistant_turn}",
        rating_scale=[{"value": 0.0, "label": "no", "definition": "no"},
                      {"value": 1.0, "label": "yes", "definition": "yes"}],
        model_id=model_id)


def test_judge_default_follows_settings(settings):
    from app.assistant.evaluation_plan import DerivedEvaluator, JudgeEvaluator
    from app.evaluation import agentcore_eval as ac
    from app.evaluation.routers import JudgeCreate, JudgeUpdate

    assert ac.judge_default_model_id() == GPT_SOL
    settings(judge_model_id=GLM)
    assert ac.judge_default_model_id() == GLM
    assert JudgeCreate(name="j", instructions="Rate {assistant_turn} well").model_id == GLM
    assert JudgeUpdate().model_id == GLM
    assert JudgeEvaluator(kind="judge", key="k", title="t", name="j",
                          instructions="Rate {assistant_turn} well").model_id == GLM
    assert DerivedEvaluator(kind="derived", key="k", title="t", name="d",
                            base_evaluator_id="Builtin.Helpfulness").model_id == GLM


def test_judge_fallback_follows_settings_and_empty_disables_it(settings):
    from app.evaluation import agentcore_eval as ac

    settings(judge_fallback_model_id=GLM)
    client = _Refusing({GPT_SOL})
    out = _create_judge(client, GPT_SOL)
    assert client.models == [GPT_SOL, GLM]
    assert out["launchpadModelFallback"]["used"] == GLM

    settings(judge_fallback_model_id="")
    assert ac.judge_fallback_model(GPT_SOL) is None
    client = _Refusing({GPT_SOL})
    with pytest.raises(ClientError):
        _create_judge(client, GPT_SOL)
    assert client.models == [GPT_SOL]


def test_proposal_default_and_preamble_follow_settings(settings):
    from app.assistant import service
    from app.assistant.proposal import PROPOSAL_DEFAULT_MODEL_ID, ProposalContent

    assert service.protocol_preamble() == service.PROTOCOL_PREAMBLE
    settings(assistant_model_id=GLM)
    assert ProposalContent(name="ordinary-agent", system_prompt="Be helpful.").model_id == GLM
    preamble = service.protocol_preamble()
    assert f"`{GLM}` / `\"bedrock\"`" in preamble
    assert f"`{PROPOSAL_DEFAULT_MODEL_ID}` / `\"bedrock\"`" not in preamble


def test_architect_install_default_follows_settings(settings):
    from app.system_agents import presets
    from app.system_agents.presets import ARCHITECT, InstallOptions

    assert ARCHITECT.default_options().model_id == "global.openai.gpt-6-astra"
    assert ARCHITECT.default_options().reasoning_effort == "high"
    settings(architect_model_id=GLM)
    options = ARCHITECT.default_options()
    # reasoning_effort is OpenAI-only: a GLM install must not carry it
    assert options.model_id == GLM and options.reasoning_effort is None
    assert presets.default_settings(ARCHITECT)["model_id"] == GLM
    assert presets.build_spec(ARCHITECT, "bucket", InstallOptions()).model_id == GLM
    # an installed preset keeps its stored id
    stored = {"model_id": "global.openai.gpt-6-astra"}
    assert presets.effective_settings(ARCHITECT, stored)["model_id"] == (
        "global.openai.gpt-6-astra")


# ── console overrides ───────────────────────────────────────────────────────


def _role(body, field):
    return next(r for r in body["roles"] if r["role"] == field)


def test_override_set_reset_and_read(client):
    from app.evaluation import agentcore_eval as ac

    row = _role(client.get("/api/model-defaults").json(), "judge_model_id")
    assert row == {
        "role": "judge_model_id", "model_id": GPT_SOL, "default": GPT_SOL,
        "overridden": False, "constraint": None, "allow_empty": False,
        "needs_tools": False, "updated_by": None, "updated_at": None,
    }
    saved = client.put("/api/admin/models/judge_model_id", json={"model_id": GLM})
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["model_id"] == GLM and body["default"] == GPT_SOL and body["overridden"]
    assert body["updated_by"] == "admin" and body["updated_at"]
    # no restart: consumers read the cached Settings the write updated
    assert ac.judge_default_model_id() == GLM
    assert _role(client.get("/api/model-defaults").json(), "judge_model_id")["model_id"] == GLM

    reset = client.delete("/api/admin/models/judge_model_id")
    assert reset.status_code == 200
    assert reset.json()["model_id"] == GPT_SOL and not reset.json()["overridden"]
    assert ac.judge_default_model_id() == GPT_SOL
    # clearing a role that has no override is a no-op
    assert client.delete("/api/admin/models/judge_model_id").status_code == 200


def test_an_override_survives_settings_cache_clear(client):
    """The price refresher and default-workspace writes clear the settings cache at
    runtime; the rebuilt Settings must come back with the override on it."""
    assert client.put("/api/admin/models/judge_model_id",
                      json={"model_id": GLM}).status_code == 200
    get_settings.cache_clear()
    assert get_settings().judge_model_id == GLM
    get_settings.cache_clear()
    from app.evaluation import agentcore_eval as ac

    assert ac.judge_default_model_id() == GLM
    # and a reset after a rebuild still returns to the default
    client.delete("/api/admin/models/judge_model_id")
    assert get_settings().judge_model_id == GPT_SOL
    get_settings.cache_clear()
    assert get_settings().judge_model_id == GPT_SOL


def test_a_concurrent_rebuild_keeps_the_default_and_reset_working(client):
    """Two threads missing the cleared cache at once both build through the hook; the
    one lru_cache keeps may not be the one that recorded the baseline. Its default
    must still be the env / yaml / built-in value, and a reset must still revert."""
    assert client.put("/api/admin/models/judge_model_id",
                      json={"model_id": GLM}).status_code == 200
    get_settings.cache_clear()
    cached = get_settings()
    get_settings.__wrapped__()  # the sibling build, hooked last, not cached
    row = _role(client.get("/api/model-defaults").json(), "judge_model_id")
    assert row["model_id"] == GLM and row["default"] == GPT_SOL
    client.delete("/api/admin/models/judge_model_id")
    assert get_settings() is cached and cached.judge_model_id == GPT_SOL


def test_an_override_survives_the_price_refresh(client, monkeypatch, tmp_path):
    import app.core.config as config_module
    from app.services import model_prices as mp

    assert client.put("/api/admin/models/actor_model_id",
                      json={"model_id": "global.openai.gpt-6-sol"}).status_code == 200
    monkeypatch.setattr(config_module, "CONFIG_FILE", tmp_path / "launchpad.yaml")
    monkeypatch.setattr(mp, "_seen_models", lambda _ws, cw=None: [])
    monkeypatch.setattr(mp, "_span_models", lambda _ws, logs=None, hours=0: [])
    mp.refresh_model_prices(fetch=lambda url: {})
    assert get_settings().actor_model_id == "global.openai.gpt-6-sol"


def test_an_override_persists_across_a_restart(client):
    assert client.put("/api/admin/models/assistant_model_id",
                      json={"model_id": GLM}).status_code == 200
    # a new process: no snapshot, a fresh Settings — env / yaml / default only
    model_settings.reset_overrides()
    get_settings.cache_clear()
    assert get_settings().assistant_model_id == GPT_SOL
    # its startup loads the ledger rows (create_app does this after init_db)
    _db_call(model_settings.load_overrides)
    assert get_settings().assistant_model_id == GLM
    get_settings.cache_clear()
    assert get_settings().assistant_model_id == GLM
    # a whole new app loads it the same way
    model_settings.reset_overrides()
    from app.main import create_app

    create_app()
    assert get_settings().assistant_model_id == GLM


def test_console_beats_env_and_reset_returns_to_env(client, monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_PROMPT_OPT_DEFAULT_MODEL_ID", "us.amazon.nova-pro-v1:0")
    get_settings.cache_clear()
    row = _role(client.get("/api/model-defaults").json(), "prompt_opt_default_model_id")
    assert row["model_id"] == row["default"] == "us.amazon.nova-pro-v1:0"
    saved = client.put("/api/admin/models/prompt_opt_default_model_id",
                       json={"model_id": GLM}).json()
    assert saved["model_id"] == GLM and saved["default"] == "us.amazon.nova-pro-v1:0"
    get_settings.cache_clear()
    assert get_settings().prompt_opt_default_model_id == GLM  # console > env
    reset = client.delete("/api/admin/models/prompt_opt_default_model_id").json()
    assert reset["model_id"] == "us.amazon.nova-pro-v1:0"


def test_role_validation(client):
    def put(field, model_id):
        return client.put(f"/api/admin/models/{field}", json={"model_id": model_id})

    unknown = put("platform_model_id", GLM)
    assert unknown.status_code == 404 and unknown.json()["code"] == "models.unknown_role"
    assert put("judge_model_id", "not an id").json()["code"] == "models.invalid_id"
    assert put("judge_model_id", "").json()["code"] == "models.empty"
    # Claude-only features refuse another family
    for field in ("claude_sdk_model_id", "skill_lab_target_model_id", "codegen_model"):
        wrong = put(field, GPT_SOL)
        assert wrong.status_code == 400 and wrong.json()["code"] == "models.wrong_family"
        assert put(field, "us.anthropic.claude-sonnet-5").status_code == 200
    assert put("skill_lab_codex_target_model_id", SONNET_5).status_code == 400
    assert put("skill_lab_judge_model_id", GLM).status_code == 400
    assert put("skill_lab_judge_model_id", SONNET_5).status_code == 200
    # a model known to refuse tool use is refused for a role that runs tools only
    r1 = "us.deepseek.r1-v1:0"
    assert put("agent_default_model_id", r1).json()["code"] == "models.no_tool_use"
    assert put("judge_model_id", r1).status_code == 200
    # the judge fallback may be disabled
    off = put("judge_fallback_model_id", "")
    assert off.status_code == 200 and off.json()["model_id"] == ""
    # nothing refused reached the ledger
    assert set(model_settings.overrides()) == {
        "claude_sdk_model_id", "skill_lab_target_model_id", "codegen_model",
        "skill_lab_judge_model_id", "judge_model_id", "judge_fallback_model_id",
    }


# ── the probe (stubbed bedrock-runtime) ─────────────────────────────────────


class _Runtime:
    def __init__(self, deny=()):
        self.deny, self.calls = set(deny), []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["modelId"] in self.deny:
            raise ClientError({"Error": {"Code": "AccessDeniedException",
                                         "Message": "no access"}}, "Converse")
        return {"stopReason": "end_turn",
                "output": {"message": {"content": [{"text": "OK"}]}}}


class _Ctx:
    account_id, region = "111122223333", "us-west-2"

    def __init__(self, runtime):
        self.runtime, self.services = runtime, []

    def client(self, service, **_):
        self.services.append(service)
        return self.runtime


def test_probe_converse_and_tool_use():
    ctx = _Ctx(_Runtime())
    out = model_settings.probe(GPT_SOL, "converse", ctx)
    assert out["ok"] and out["check"] == "converse" and out["region"] == "us-west-2"
    assert isinstance(out["latency_ms"], int) and "error_code" not in out
    assert ctx.services == ["bedrock-runtime"]
    assert ctx.runtime.calls[0]["inferenceConfig"] == {"maxTokens": 16}
    assert "toolConfig" not in ctx.runtime.calls[0]

    out = model_settings.probe(GPT_SOL, "tool_use", ctx)
    assert out["ok"] and out["check"] == "tool_use"
    tool_config = ctx.runtime.calls[1]["toolConfig"]
    assert tool_config["toolChoice"] == {"auto": {}}
    assert tool_config["tools"][0]["toolSpec"]["name"] == "reply"


def test_probe_reports_the_aws_error():
    ctx = _Ctx(_Runtime(deny={GPT_SOL}))
    out = model_settings.probe(GPT_SOL, "converse", ctx)
    assert out["ok"] is False
    assert out["error_code"] == "AccessDeniedException" and out["message"] == "no access"


def test_probe_endpoint_runs_in_the_default_workspace(client, monkeypatch):
    runtime = _Runtime()
    seen = {}

    def fake_client(service, ctx, cache_token=None, **cfg):
        seen.update(service=service, account=ctx.account_id, region=ctx.region)
        return runtime

    from app.services import aws_clients

    monkeypatch.setattr(aws_clients, "client", fake_client)
    res = client.post("/api/admin/models/probe", json={"model_id": GPT_SOL,
                                                       "check": "tool_use"})
    assert res.status_code == 200, res.text
    assert res.json()["ok"] and res.json()["check"] == "tool_use"
    settings = get_settings()
    assert seen == {"service": "bedrock-runtime", "account": settings.account_id,
                    "region": settings.region}
    bad = client.post("/api/admin/models/probe", json={"model_id": GPT_SOL, "check": "judge"})
    assert bad.status_code == 422
    assert client.post("/api/admin/models/probe",
                       json={"model_id": "bad id"}).json()["code"] == "models.invalid_id"


# ── authorization ───────────────────────────────────────────────────────────


def test_route_policy_rows():
    from app.core.route_policy import ADMIN, MEMBER, ROUTE_POLICY, WORKSPACE_EXEMPT

    assert ROUTE_POLICY[("GET", "/api/model-defaults")] == MEMBER
    for key in (("PUT", "/api/admin/models/{role}"), ("DELETE", "/api/admin/models/{role}"),
                ("POST", "/api/admin/models/probe")):
        assert ROUTE_POLICY[key] == ADMIN
        assert key in WORKSPACE_EXEMPT
    assert ("GET", "/api/model-defaults") in WORKSPACE_EXEMPT


@pytest.fixture
def gated_client(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN_CREDS["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN_CREDS["password"])
    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app(), client=("127.0.0.1", 4321)) as client:
        yield client
    get_settings.cache_clear()


def _sign_in_member(client):
    from app.services import users as users_service

    assert client.post("/api/auth/register", json=MEMBER_CREDS).status_code == 201
    db = SessionLocal()
    try:
        user = users_service.find_by_username(db, MEMBER_CREDS["username"])
        user.status = users_service.STATUS_ACTIVE
        user.expires_at = datetime.now(UTC) + timedelta(days=7)
        db.commit()
    finally:
        db.close()
    login = client.post("/api/auth/login", json={
        "username": MEMBER_CREDS["username"], "password": MEMBER_CREDS["password"]})
    assert login.status_code == 200 and login.json()["role"] == "member"


def test_a_member_reads_but_cannot_write_or_probe(gated_client, monkeypatch):
    from app.services import aws_clients

    def no_aws(*_a, **_k):
        raise AssertionError("a refused request must not reach AWS")

    monkeypatch.setattr(aws_clients, "client", no_aws)
    assert gated_client.get("/api/model-defaults").status_code == 401  # anonymous
    _sign_in_member(gated_client)
    read = gated_client.get("/api/model-defaults")
    assert read.status_code == 200 and _role(read.json(), "agent_default_model_id")[
        "model_id"] == GPT_SOL
    for method, path, body in (
        ("PUT", "/api/admin/models/judge_model_id", {"model_id": GLM}),
        ("DELETE", "/api/admin/models/judge_model_id", None),
        ("POST", "/api/admin/models/probe", {"model_id": GPT_SOL, "check": "converse"}),
    ):
        res = gated_client.request(method, path, json=body)
        assert res.status_code == 403 and res.json()["code"] == "auth.forbidden", path
    assert model_settings.overrides() == {}


def test_an_admin_write_records_who(gated_client):
    login = gated_client.post("/api/auth/login", json=ADMIN_CREDS)
    assert login.status_code == 200
    saved = gated_client.put("/api/admin/models/actor_model_id",
                             json={"model_id": "global.openai.gpt-6-terra"})
    assert saved.status_code == 200, saved.text
    assert saved.json()["updated_by"] == ADMIN_CREDS["username"]
