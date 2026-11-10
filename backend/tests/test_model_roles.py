"""Model roles (r15-e — the useful subset of upstream #275): defaults, console overrides, the
catalog rules a role obeys, the referrer rule, the journal and the route posture.

Every model the platform itself calls or pre-selects is a Settings field and a role of
``services/model_roles.py``; an administrator overrides one on Settings → Model Providers →
Model roles (ledger ``platform_settings``). The override must reach each consumer, survive
every ``get_settings.cache_clear()`` and a restart, and a reset must return the role to
env / yaml / default. Adapted from upstream ``tests/test_model_settings.py``: the CN roles,
the catalog-backed validation (in the catalog, enabled, tool-capable where the role runs
tools) and the registry's referrer rule replace upstream's family / regex checks and the
Converse probe (a role's model is probed through the registry's own re-probe route).
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.schemas import model_catalog
from app.schemas import model_providers as contract
from app.services import model_registry, model_roles
from tests.test_route_policy import ADMIN_CREDS, MEMBER_CREDS

GLM = "global.zai.glm-5.3"
KIMI = "global.moonshotai.kimi-k3"
TITAN = "amazon.titan-embed-text-v2:0"
ROLES_PREFIX = "/api/settings/model-roles"
DEFAULTS = "/api/model-defaults"
ROLE_KEYS = {"role", "field", "model_id", "default", "overridden_by", "updated_at",
             "needs_tools", "allow_empty", "default_disabled",
             "kind", "feature"}  # N17-obs252: kind (chat | embedding) + the console feature

# R13 (N13-obs7): the OpenAI-compatible rows are admin-registered on top of the Bedrock seed
pytestmark = pytest.mark.siliconflow


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.delenv("LAUNCHPAD_SILICONFLOW_API_KEY", raising=False)
    yield
    model_catalog.reset()


def _db_call(fn, *args):
    db = SessionLocal()
    try:
        return fn(db, *args)
    finally:
        db.close()


def _role(body, name):
    return next(r for r in body["roles"] if r["role"] == name)


def _acme_model(client, name="acme-org/Foo-7B", tools=True, enabled=True):
    """A manual chat row on a fresh OpenAI-compatible provider (no probe)."""
    created = client.post("/api/settings/model-providers", json={
        "name": "acme", "kind": "openai_compatible", "label": "Acme",
        "base_url": "https://api.siliconflow.cn/acme/v1"})
    if created.status_code == 409:  # already there from this test
        provider_id = client.get("/api/settings/model-providers/acme").json()["id"]
    else:
        assert created.status_code == 201, created.text
        provider_id = created.json()["id"]
    response = client.post(f"/api/settings/model-providers/{provider_id}/models", json={
        "api_model_name": name, "kind": "chat", "probe": False, "enabled": enabled,
        "overrides": {"tools": tools}})
    assert response.status_code == 201, response.text
    return provider_id, response.json()


# ── defaults ────────────────────────────────────────────────────────────────


def test_defaults_are_todays_config_values():
    """GLM 5.3 everywhere, Kimi K3 for the Skill Lab target — today's config, unchanged."""
    defaults = {name: f.default for name, f in Settings.model_fields.items()}
    assert defaults["agent_default_model_id"] == GLM
    assert defaults["eval_judge_model_id"] == GLM
    assert defaults["eval_judge_fallback_model_id"] == ""
    assert defaults["eval_actor_model_id"] == GLM
    assert defaults["prompt_opt_default_model_id"] == GLM
    assert defaults["skill_lab_judge_model_id"] == GLM
    assert defaults["skill_lab_target_model_id"] == KIMI
    # N17-obs252: the memory extraction summariser and the Studio coding agent are roles too
    assert defaults["memory_extract_model_id"] == GLM
    assert defaults["codegen_model"] == KIMI
    assert [r.name for r in model_roles.ROLES] == [
        "agent_default", "judge", "judge_fallback", "failure_detection", "actor", "prompt_opt",
        "skill_lab_judge", "skill_lab_target", "memory_extract", "codegen"]
    # N17-obs449: the insights failure detector's judge — Kimi K3 (its root-cause step forces a
    # structured-output tool call, so it needs tools)
    assert defaults["eval_failure_detection_model_id"] == KIMI
    assert {r.name for r in model_roles.ROLES if r.needs_tools} == {
        "agent_default", "failure_detection", "actor", "skill_lab_target"}
    assert {r.name: r.feature for r in model_roles.ROLES if r.feature} == {
        "skill_lab_judge": "skill_lab", "skill_lab_target": "skill_lab", "codegen": "studio"}
    assert all(r.kind == "chat" for r in model_roles.ROLES)
    assert (model_roles.EMBEDDING_ROLE.kind, model_roles.EMBEDDING_ROLE.field) == ("embedding", "")
    assert model_roles.role("embedding") is model_roles.EMBEDDING_ROLE
    assert {r.name for r in model_roles.ROLES if r.allow_empty} == {"judge_fallback"}
    # not ported (user decision 2026-10-19): no assistant / Claude SDK / Studio / codex role
    assert not {"assistant_model_id", "claude_sdk_model_id", "studio_fallback_model_id",
                "skill_lab_codex_target_model_id"} & set(defaults)


def test_env_overrides_use_the_launchpad_prefix(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AGENT_DEFAULT_MODEL_ID", KIMI)
    monkeypatch.setenv("LAUNCHPAD_EVAL_JUDGE_FALLBACK_MODEL_ID", "")
    s = Settings()
    assert s.agent_default_model_id == KIMI and s.eval_judge_fallback_model_id == ""
    monkeypatch.setenv("LAUNCHPAD_EVAL_JUDGE_FALLBACK_MODEL_ID", "not-a-catalog-model")
    with pytest.raises(ValueError, match="model.not_allowed"):
        Settings()


def test_the_contract_pins_the_role_row_key_set():
    assert set(contract.ModelRoleOut.model_fields) == ROLE_KEYS
    assert set(contract.ModelRolesOut.model_fields) == {"roles"}
    assert set(contract.ModelRoleSetIn.model_fields) == {"model_id"}


# ── consumers ───────────────────────────────────────────────────────────────


def test_a_spec_without_a_model_gets_the_agent_default(client):
    from app.assistant.proposal import ProposalContent
    from app.schemas.agent import AgentSpec, agent_default_model_id

    assert agent_default_model_id() == GLM
    spec = AgentSpec(name="plain-agent", method="harness", system_prompt="p")
    assert spec.model_id == GLM
    assert client.put(f"{ROLES_PREFIX}/agent_default", json={"model_id": KIMI}).status_code == 200
    assert agent_default_model_id() == KIMI
    assert AgentSpec(name="plain-agent", method="harness", system_prompt="p").model_id == KIMI
    # a proposal without a model follows too; the architect's preamble states the live id
    assert ProposalContent(name="ordinary-agent", system_prompt="Be helpful.").model_id == KIMI
    from app.assistant import service

    assert f"`{KIMI}` / `\"bedrock\"`" in service.protocol_preamble()
    assert f"`{GLM}` / `\"bedrock\"`" in service.PROTOCOL_PREAMBLE  # the constant is pinned
    # a stored spec carries its own model_id: unaffected
    stored = AgentSpec.model_validate({"name": "old-agent", "method": "harness",
                                       "system_prompt": "p", "model_id": GLM})
    assert stored.model_id == GLM
    client.delete(f"{ROLES_PREFIX}/agent_default")
    assert AgentSpec(name="plain-agent", method="harness", system_prompt="p").model_id == GLM
    assert service.protocol_preamble() == service.PROTOCOL_PREAMBLE


def test_architect_catalog_default_tag_follows_the_agent_default_role(client, monkeypatch):
    """N17-obs298: the architect prose has ONE "default" for an agent model — the effective
    `agent_default` role. The provider-registry default row (the 默认 column) still flags
    GLM, but the catalog lines tag only the overridden role model, the protocol preamble's
    `model_id` default line names the same id, and the install-time skill catalog tags none
    (it would go stale when the role moves)."""
    from app.assistant import service
    from app.schemas import agent as schema
    from app.system_agents import presets

    assert client.put(f"{ROLES_PREFIX}/agent_default", json={"model_id": KIMI}).status_code == 200
    entries = {e["model_id"]: e for e in schema.model_catalog_entries()}
    assert entries[GLM]["default"] is True and entries[KIMI]["default"] is False  # row flag kept
    tagged = [ln for ln in schema.model_catalog_lines() if " (default" in ln.split(" — ")[0]]
    assert len(tagged) == 1 and tagged[0].startswith(f"- `{KIMI}` (default")
    assert f"`{KIMI}` / `\"bedrock\"`" in service.protocol_preamble()
    section = service.catalog_section({"resources": {}})
    assert [ln for ln in section.splitlines() if " (default" in ln.split(" — ")[0]] == tagged
    skill = presets.render_model_catalog_section().splitlines()
    assert not any(ln.startswith("- `") and " (default" in ln.split(" — ")[0] for ln in skill)
    # a role model the visible catalog does not list → nothing is tagged
    monkeypatch.setattr(schema, "agent_default_model_id", lambda: "global.example.gone-1")
    assert not any(" (default" in ln.split(" — ")[0] for ln in schema.model_catalog_lines())
    monkeypatch.undo()
    client.delete(f"{ROLES_PREFIX}/agent_default")
    tagged = [ln for ln in schema.model_catalog_lines() if " (default" in ln.split(" — ")[0]]
    assert len(tagged) == 1 and tagged[0].startswith(f"- `{GLM}` (default")


def test_consumers_read_the_override_without_a_restart(client):
    from app.evaluation.judge_budget import judge_budget

    assert client.put(f"{ROLES_PREFIX}/judge", json={"model_id": KIMI}).status_code == 200
    settings = get_settings()
    assert settings.eval_judge_model_id == KIMI
    # the judge budget of "no explicit model" is now Kimi's (2048), not GLM's (4096)
    assert judge_budget(None, settings) == judge_budget(KIMI, settings)
    assert client.put(f"{ROLES_PREFIX}/skill_lab_target", json={"model_id": GLM}).status_code == 200
    assert get_settings().skill_lab_target_model_id == GLM


# ── console overrides ───────────────────────────────────────────────────────


def test_override_set_reset_and_read(client):
    body = client.get(DEFAULTS).json()
    # N17-obs252: the registry's active embedding row is listed last
    assert [r["role"] for r in body["roles"]] == [r.name for r in model_roles.ROLES] + ["embedding"]
    assert all(set(r) == ROLE_KEYS for r in body["roles"])
    row = _role(body, "judge")
    assert row == {"role": "judge", "field": "eval_judge_model_id", "model_id": GLM,
                   "default": GLM, "overridden_by": None, "updated_at": None,
                   "needs_tools": False, "allow_empty": False, "default_disabled": False,
                   "kind": "chat", "feature": None}
    assert _role(body, "skill_lab_target")["feature"] == "skill_lab"
    assert _role(body, "codegen")["feature"] == "studio"
    saved = client.put(f"{ROLES_PREFIX}/judge", json={"model_id": KIMI})
    assert saved.status_code == 200, saved.text
    assert saved.json()["model_id"] == KIMI and saved.json()["default"] == GLM
    assert saved.json()["overridden_by"] == get_settings().auth_username
    assert saved.json()["updated_at"]
    assert _role(client.get(DEFAULTS).json(), "judge")["model_id"] == KIMI
    assert model_roles.overrides() == {"eval_judge_model_id": KIMI}

    reset = client.delete(f"{ROLES_PREFIX}/judge")
    assert reset.status_code == 200
    assert reset.json()["model_id"] == GLM and reset.json()["overridden_by"] is None
    assert get_settings().eval_judge_model_id == GLM
    # clearing a role that has no override is a no-op
    assert client.delete(f"{ROLES_PREFIX}/judge").status_code == 200
    assert model_roles.overrides() == {}


def test_an_override_survives_settings_cache_clear(client):
    """Default-workspace resource writes clear the settings cache at runtime; the rebuilt
    Settings must come back with the override on it."""
    assert client.put(f"{ROLES_PREFIX}/actor", json={"model_id": KIMI}).status_code == 200
    get_settings.cache_clear()
    assert get_settings().eval_actor_model_id == KIMI
    get_settings.cache_clear()
    assert get_settings().eval_actor_model_id == KIMI
    # and a reset after a rebuild still returns to the default
    client.delete(f"{ROLES_PREFIX}/actor")
    assert get_settings().eval_actor_model_id == GLM
    get_settings.cache_clear()
    assert get_settings().eval_actor_model_id == GLM


def test_a_concurrent_rebuild_keeps_the_default_and_reset_working(client):
    """Two threads missing the cleared cache at once both build through the hook; the one
    lru_cache keeps may not be the one that recorded the baseline. Its default must still be
    the env / yaml / built-in value, and a reset must still revert."""
    assert client.put(f"{ROLES_PREFIX}/judge", json={"model_id": KIMI}).status_code == 200
    get_settings.cache_clear()
    cached = get_settings()
    get_settings.__wrapped__()  # the sibling build, hooked last, not cached
    row = _role(client.get(DEFAULTS).json(), "judge")
    assert row["model_id"] == KIMI and row["default"] == GLM
    client.delete(f"{ROLES_PREFIX}/judge")
    assert get_settings() is cached and cached.eval_judge_model_id == GLM


def test_an_override_persists_across_a_restart(client):
    assert client.put(f"{ROLES_PREFIX}/prompt_opt", json={"model_id": KIMI}).status_code == 200
    # a new process: no snapshot, a fresh Settings — env / yaml / default only
    model_roles.reset_overrides()
    get_settings.cache_clear()
    assert get_settings().prompt_opt_default_model_id == GLM
    # its startup loads the ledger rows (create_app does this after init_db)
    _db_call(model_roles.load_overrides)
    assert get_settings().prompt_opt_default_model_id == KIMI
    get_settings.cache_clear()
    assert get_settings().prompt_opt_default_model_id == KIMI
    # a whole new app loads it the same way
    model_roles.reset_overrides()
    from app.main import create_app

    create_app()
    assert get_settings().prompt_opt_default_model_id == KIMI


def test_a_second_process_write_is_picked_up_by_the_read(client):
    """The page read re-syncs the snapshot when the ledger rows differ from it."""
    db = SessionLocal()
    try:
        from app.models.model_registry import PlatformSetting

        db.add(PlatformSetting(key="skill_lab_judge_model_id", value=KIMI, updated_by="other"))
        db.commit()
    finally:
        db.close()
    assert get_settings().skill_lab_judge_model_id == GLM
    row = _role(client.get(DEFAULTS).json(), "skill_lab_judge")
    assert row["model_id"] == KIMI and row["overridden_by"] == "other"
    assert get_settings().skill_lab_judge_model_id == KIMI


@pytest.fixture
def env_settings(monkeypatch):
    """Set a `LAUNCHPAD_*` variable and rebuild the cached Settings — and rebuild it again
    afterwards, so the env-sourced object never outlives the variable (xdist siblings)."""
    def set_env(name: str, value: str) -> None:
        monkeypatch.setenv(name, value)
        get_settings.cache_clear()

    yield set_env
    monkeypatch.undo()
    get_settings.cache_clear()


def test_console_beats_env_and_reset_returns_to_env(client, env_settings):
    env_settings("LAUNCHPAD_PROMPT_OPT_DEFAULT_MODEL_ID", KIMI)
    row = _role(client.get(DEFAULTS).json(), "prompt_opt")
    assert row["model_id"] == row["default"] == KIMI
    saved = client.put(f"{ROLES_PREFIX}/prompt_opt", json={"model_id": GLM}).json()
    assert saved["model_id"] == GLM and saved["default"] == KIMI
    get_settings.cache_clear()
    assert get_settings().prompt_opt_default_model_id == GLM  # console > env
    reset = client.delete(f"{ROLES_PREFIX}/prompt_opt").json()
    assert reset["model_id"] == KIMI


# ── what a role accepts ─────────────────────────────────────────────────────


def test_unknown_role_is_404(client):
    for method in ("PUT", "DELETE"):
        res = client.request(method, f"{ROLES_PREFIX}/assistant",
                             json={"model_id": GLM} if method == "PUT" else None)
        assert res.status_code == 404 and res.json()["code"] == "models.unknown_role", res.text
    assert model_roles.overrides() == {}


def test_a_role_accepts_only_enabled_tool_capable_catalog_chat_rows(client):
    def put(name, model_id):
        return client.put(f"{ROLES_PREFIX}/{name}", json={"model_id": model_id})

    def refused(res, role_name, model_id, reason):
        assert res.status_code == 422, res.text
        body = res.json()
        assert body["code"] == "models.model_not_allowed"
        assert body["detail"] == {"role": role_name, "model_id": model_id, "reason": reason}

    refused(put("judge", "global.anthropic.claude-sonnet-5"), "judge",
            "global.anthropic.claude-sonnet-5", "not_in_catalog")
    refused(put("judge", TITAN), "judge", TITAN, "not_chat")
    refused(put("judge", ""), "judge", "", "empty")
    assert put("judge", "  ").json()["detail"]["reason"] == "empty"
    # an unknown / empty body key set is the generic request validation error
    assert client.put(f"{ROLES_PREFIX}/judge", json={"model": GLM}).status_code == 422
    # a disabled row (D21 disable semantics) and a row without tool use
    provider_id, plain = _acme_model(client, "acme-org/NoTools", tools=False)
    _, off = _acme_model(client, "acme-org/Off", enabled=False)
    refused(put("judge", off["model_id"]), "judge", off["model_id"], "disabled")
    refused(put("agent_default", off["model_id"]), "agent_default", off["model_id"], "disabled")
    # a plain-Converse role takes a row without tools; a role that runs tools refuses it
    assert put("judge", plain["model_id"]).status_code == 200
    for name in ("agent_default", "actor", "skill_lab_target"):
        refused(put(name, plain["model_id"]), name, plain["model_id"], "no_tool_use")
    # a disabled PROVIDER disables its rows too
    client.delete(f"{ROLES_PREFIX}/judge")
    disabled = client.put(f"/api/settings/model-providers/{provider_id}", json={"enabled": False})
    assert disabled.status_code == 200, disabled.text
    refused(put("prompt_opt", plain["model_id"]), "prompt_opt", plain["model_id"], "disabled")
    # the judge fallback may be disabled ("" = none)
    off_fallback = put("judge_fallback", "")
    assert off_fallback.status_code == 200 and off_fallback.json()["model_id"] == ""
    assert get_settings().eval_judge_fallback_model_id == ""
    assert put("judge_fallback", KIMI).json()["model_id"] == KIMI
    # nothing refused reached the ledger
    assert set(model_roles.overrides()) == {"eval_judge_fallback_model_id"}


# ── the referrer rule (409 in_use through a role) ──────────────────────────


def test_a_model_a_role_points_at_cannot_be_disabled_or_deleted(client):
    provider_id, row = _acme_model(client, "acme-org/Judge-7B")
    ref = f"/api/settings/model-providers/{provider_id}/models/{row['id']}"
    # free of roles: disable / enable / delete all fine
    assert client.put(ref, json={"enabled": False}).status_code == 200
    assert client.put(ref, json={"enabled": True}).status_code == 200
    pointed = client.put(f"{ROLES_PREFIX}/judge", json={"model_id": row["model_id"]})
    assert pointed.status_code == 200, pointed.text
    for attempt in (client.put(ref, json={"enabled": False}), client.delete(ref)):
        assert attempt.status_code == 409, attempt.text
        body = attempt.json()
        assert body["code"] == "model.in_use"
        refs = body["detail"]["referrers"]
        assert [(r["kind"], r["id"], r["role"], r["model_ids"]) for r in refs
                if r["kind"] == "setting"] == [
            ("setting", "eval_judge_model_id", "judge", [row["model_id"]])]
    # the provider hosting it cannot be disabled or deleted either
    off = client.put(f"/api/settings/model-providers/{provider_id}", json={"enabled": False})
    assert off.status_code == 409 and off.json()["code"] == "model_provider.in_use"
    assert off.json()["detail"]["referrers"][0]["role"] == "judge"
    gone = client.delete(f"/api/settings/model-providers/{provider_id}")
    assert gone.status_code == 409 and gone.json()["code"] == "model_provider.in_use"
    # repoint the role: everything is allowed again
    assert client.delete(f"{ROLES_PREFIX}/judge").status_code == 200
    assert client.put(ref, json={"enabled": False}).status_code == 200
    assert client.put(ref, json={"enabled": True}).status_code == 200
    assert client.delete(ref).status_code == 204
    # rev-r25 F3 (N17-obs252): the roles whose DEFAULT is Kimi (Skill Lab target, Studio
    # coding agent) belong to HIDDEN features — they no longer block a disable of the Kimi row;
    # the roles view flags their default as unusable instead
    kimi_ref = f"/api/settings/model-providers/bedrock/models/{KIMI}"
    # N17-obs449: the failure-detection judge (a VISIBLE role) defaults to Kimi and blocks it …
    kimi = client.put(kimi_ref, json={"enabled": False})
    assert kimi.status_code == 409 and [r["role"] for r in kimi.json()["detail"]["referrers"]
                                        if r.get("role")] == ["failure_detection"]
    # … until it is repointed
    assert client.put(f"{ROLES_PREFIX}/failure_detection",
                      json={"model_id": GLM}).status_code == 200
    kimi = client.put(kimi_ref, json={"enabled": False})
    assert kimi.status_code == 200, kimi.text
    assert _role(client.get(DEFAULTS).json(), "skill_lab_target")["default_disabled"] is True
    assert client.put(kimi_ref, json={"enabled": True}).status_code == 200
    # … and with nothing hidden they block it exactly as before
    get_settings().hidden_role_features = []
    try:
        kimi = client.put(kimi_ref, json={"enabled": False})
        assert kimi.status_code == 409 and kimi.json()["code"] == "model.in_use"
        assert [r["role"] for r in kimi.json()["detail"]["referrers"]] == [
            "skill_lab_target", "codegen"]
    finally:
        get_settings().hidden_role_features = ["skill_lab", "studio"]


def test_referrers_read_the_effective_role_values(client):
    db = SessionLocal()
    try:
        # rev-r25 F3: skill_lab_target / codegen point at Kimi by default but belong to
        # hidden features → not referrers; the optimizer list stays one
        # N17-obs449: + the failure-detection judge, whose default is Kimi
        assert [(r["id"], r.get("role")) for r in model_registry.referrers(db, {KIMI})] == [
            ("eval_failure_detection_model_id", "failure_detection"), ("prompt_opt_models", None)]
        assert client.put(f"{ROLES_PREFIX}/judge", json={"model_id": KIMI}).status_code == 200
        assert [(r["id"], r.get("role")) for r in model_registry.referrers(db, {KIMI})] == [
            ("eval_judge_model_id", "judge"),
            ("eval_failure_detection_model_id", "failure_detection"), ("prompt_opt_models", None)]
        get_settings().hidden_role_features = []  # nothing hidden: every role is a referrer
        try:
            assert [(r["id"], r.get("role")) for r in model_registry.referrers(db, {KIMI})] == [
                ("eval_judge_model_id", "judge"),
                ("eval_failure_detection_model_id", "failure_detection"),
                ("skill_lab_target_model_id", "skill_lab_target"),
                ("codegen_model", "codegen"), ("prompt_opt_models", None)]
        finally:
            get_settings().hidden_role_features = ["skill_lab", "studio"]
    finally:
        db.close()


# ── the journal ─────────────────────────────────────────────────────────────


def test_every_role_write_is_one_audit_only_journal_row(client):
    before = client.get("/api/settings/model-providers").json()["catalog_version"]
    changes = client.get("/api/settings/model-providers/changes").json()["items"]
    assert client.put(f"{ROLES_PREFIX}/actor", json={"model_id": KIMI}).status_code == 200
    assert client.delete(f"{ROLES_PREFIX}/actor").status_code == 200
    assert client.delete(f"{ROLES_PREFIX}/actor").status_code == 200  # no-op: no row
    after = client.get("/api/settings/model-providers/changes").json()["items"]
    new = after[: len(after) - len(changes)]
    assert [c["operation"] for c in new] == ["model_role.reset", "model_role.set"]
    reset, set_ = new
    actor = get_settings().auth_username
    assert set_["target_id"] == "actor" and set_["actor"] == actor
    assert set_["before"] == {"field": "eval_actor_model_id", "model_id": GLM,
                              "overridden_by": None}
    assert set_["after"] == {"field": "eval_actor_model_id", "model_id": KIMI,
                             "overridden_by": actor}
    assert reset["before"] == {"field": "eval_actor_model_id", "model_id": KIMI,
                               "overridden_by": actor}
    assert reset["after"] == {"field": "eval_actor_model_id", "model_id": GLM,
                              "overridden_by": None}
    # audit-only: the catalog version did not move (nothing a runtime or picker list observes)
    assert client.get("/api/settings/model-providers").json()["catalog_version"] == before
    assert {"model_role.set", "model_role.reset"} <= model_registry.AUDIT_ONLY_OPERATIONS
    # and no key material anywhere near the rows
    assert "sk-" not in str(new) and "Bearer" not in str(new)


# ── authorization ───────────────────────────────────────────────────────────


def test_route_policy_rows():
    from app.core.route_policy import ADMIN, MEMBER, ROUTE_POLICY, WORKSPACE_EXEMPT

    assert ROUTE_POLICY[("GET", DEFAULTS)] == MEMBER
    for key in (("PUT", f"{ROLES_PREFIX}/{{role}}"), ("DELETE", f"{ROLES_PREFIX}/{{role}}")):
        assert ROUTE_POLICY[key] == ADMIN
        assert key in WORKSPACE_EXEMPT
    assert ("GET", DEFAULTS) in WORKSPACE_EXEMPT


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


def test_a_member_reads_the_defaults_but_cannot_write(gated_client):
    assert gated_client.get(DEFAULTS).status_code == 401  # anonymous
    # an administrator overrides the judge; the member must not learn who (N17-obs251)
    assert gated_client.post("/api/auth/login", json=ADMIN_CREDS).status_code == 200
    assert gated_client.put(f"{ROLES_PREFIX}/judge", json={"model_id": KIMI}).status_code == 200
    assert _role(gated_client.get(DEFAULTS).json(), "judge")["overridden_by"] == \
        ADMIN_CREDS["username"]
    assert gated_client.post("/api/auth/logout").status_code in (200, 204)
    _sign_in_member(gated_client)
    read = gated_client.get(DEFAULTS)
    assert read.status_code == 200 and _role(read.json(), "agent_default")["model_id"] == GLM
    judge = _role(read.json(), "judge")
    assert judge["model_id"] == KIMI and judge["overridden_by"] == ""  # overridden, no name
    assert _role(read.json(), "agent_default")["overridden_by"] is None
    assert all(set(r) == ROLE_KEYS for r in read.json()["roles"])  # same shape, no secrets
    assert not any("key" in k or "secret" in k for k in ROLE_KEYS)
    for method, path, body in (
        ("PUT", f"{ROLES_PREFIX}/judge", {"model_id": KIMI}),
        ("DELETE", f"{ROLES_PREFIX}/judge", None),
    ):
        res = gated_client.request(method, path, json=body)
        assert res.status_code == 403 and res.json()["code"] == "auth.forbidden", path
    assert model_roles.overrides() == {"eval_judge_model_id": KIMI}  # the admin's, untouched


def test_an_admin_write_records_who(gated_client):
    assert gated_client.post("/api/auth/login", json=ADMIN_CREDS).status_code == 200
    saved = gated_client.put(f"{ROLES_PREFIX}/actor", json={"model_id": KIMI})
    assert saved.status_code == 200, saved.text
    assert saved.json()["overridden_by"] == ADMIN_CREDS["username"]
    row = _role(gated_client.get(DEFAULTS).json(), "actor")
    assert row["overridden_by"] == ADMIN_CREDS["username"] and row["model_id"] == KIMI


# ── N17-obs252: every platform model choice reads a role ─────────────────────


def test_new_evaluator_default_follows_the_judge_role_and_an_explicit_model_wins(client):
    from app.evaluation.models import EvalEvaluator

    def create(name, **extra):
        res = client.post("/api/eval/evaluators", json={
            "name": name, "instructions": "Judge {assistant_turn} for helpfulness", **extra})
        assert res.status_code == 201, res.text
        db = SessionLocal()
        try:
            return db.get(EvalEvaluator, res.json()["evaluator_id"]).model_id
        finally:
            db.close()

    assert create("j_default") == GLM
    assert client.put(f"{ROLES_PREFIX}/judge", json={"model_id": KIMI}).status_code == 200
    assert create("j_follows") == KIMI  # no restart, no explicit model
    assert create("j_explicit", model_id=GLM) == GLM  # the member's choice wins
    # the OpenAPI default stays the CN constant (contract CA-1c); the live value is the role
    from app.evaluation import routers as eval_routers

    assert eval_routers.JudgeCreate.model_fields["model_id"].default == GLM
    assert eval_routers.JudgeUpdate.model_fields["model_id"].default == GLM
    client.delete(f"{ROLES_PREFIX}/judge")
    assert create("j_reset") == GLM


def test_studio_fallback_and_plan_judges_read_the_roles_per_call(client):
    from app.assistant import evaluation_plan as plan
    from app.templates.studio_agent import render_model_prelude

    def rendered_default() -> str:
        # the model constant of the shared block (`__LAUNCHPAD_MODEL_ID__` rendered)
        line = next(ln for ln in render_model_prelude(None).splitlines()
                    if ln.startswith("MODEL_ID = "))
        return line.split("=", 1)[1].strip().strip('"')

    assert rendered_default() == GLM
    assert plan.default_judge_model() == GLM
    assert client.put(f"{ROLES_PREFIX}/agent_default", json={"model_id": KIMI}).status_code == 200
    assert client.put(f"{ROLES_PREFIX}/judge", json={"model_id": KIMI}).status_code == 200
    assert rendered_default() == KIMI
    assert plan.default_judge_model() == KIMI


def test_memory_extract_and_codegen_roles_are_settings_backed(client):
    from app.codegen import config as codegen_config

    body = client.get(DEFAULTS).json()
    assert _role(body, "memory_extract")["model_id"] == GLM
    assert _role(body, "codegen")["model_id"] == KIMI
    assert client.put(f"{ROLES_PREFIX}/memory_extract", json={"model_id": KIMI}).status_code == 200
    assert get_settings().memory_extract_model_id == KIMI
    assert client.put(f"{ROLES_PREFIX}/codegen", json={"model_id": GLM}).status_code == 200
    assert get_settings().codegen_model == GLM and codegen_config.get_model() == GLM
    assert client.delete(f"{ROLES_PREFIX}/codegen").json()["model_id"] == KIMI
    # a role never takes an embedding row
    bad = client.put(f"{ROLES_PREFIX}/memory_extract", json={"model_id": TITAN})
    assert bad.status_code == 422 and bad.json()["detail"]["reason"] == "not_chat"


def _second_embedding_row(client):
    res = client.post("/api/settings/model-providers/bedrock/models", json={
        "api_model_name": "amazon.titan-embed-text-v1", "kind": "embedding", "probe": False,
        "overrides": {"embedding_dimension": 768}})
    assert res.status_code == 201, res.text
    return res.json()["model_id"]


def test_embedding_role_is_the_active_registry_row_behind_the_reindex_guard(client):
    from app.core.errors import AppError
    from app.schemas.agent import active_embedding

    row = _role(client.get(DEFAULTS).json(), "embedding")
    assert row == {"role": "embedding", "field": "", "model_id": TITAN, "default": TITAN,
                   "overridden_by": None, "updated_at": None, "needs_tools": False,
                   "allow_empty": False, "default_disabled": False, "kind": "embedding",
                   "feature": None}
    other = _second_embedding_row(client)
    # a chat row is refused for the embedding role
    bad = client.put(f"{ROLES_PREFIX}/embedding", json={"model_id": GLM})
    assert bad.status_code == 422 and bad.json()["detail"]["reason"] == "not_embedding"

    # documents in a vector index: the registry's guard refuses the flip, nothing changes
    def guard(_db, model):
        raise AppError("embedding.reindex_required", "indices hold vectors",
                       {"indices": [{"name": "launchpad-memory", "docs": 3}],
                        "model_from": TITAN, "model_to": model.model_id}, status_code=409)

    model_registry.set_embedding_guard(guard)
    refused = client.put(f"{ROLES_PREFIX}/embedding", json={"model_id": other})
    assert refused.status_code == 409 and refused.json()["code"] == "embedding.reindex_required"
    assert active_embedding().model_id == TITAN
    assert _role(client.get(DEFAULTS).json(), "embedding")["model_id"] == TITAN

    # an empty substrate: the flip goes through the registry (journal + catalog version)
    model_registry.set_embedding_guard(lambda _db, _row: None)
    version = client.get("/api/settings/model-providers").json()["catalog_version"]
    saved = client.put(f"{ROLES_PREFIX}/embedding", json={"model_id": other})
    assert saved.status_code == 200, saved.text
    assert saved.json()["model_id"] == other and saved.json()["kind"] == "embedding"
    assert saved.json()["overridden_by"] == get_settings().auth_username
    assert active_embedding().model_id == other
    assert client.get("/api/settings/model-providers").json()["catalog_version"] > version
    # reset = the built-in Titan row again, through the same guarded path
    model_registry.set_embedding_guard(guard)
    assert client.delete(f"{ROLES_PREFIX}/embedding").status_code == 409
    model_registry.set_embedding_guard(lambda _db, _row: None)
    reset = client.delete(f"{ROLES_PREFIX}/embedding")
    assert reset.status_code == 200 and reset.json()["model_id"] == TITAN
    assert reset.json()["overridden_by"] is None and active_embedding().model_id == TITAN


# ── rev-r25 F3: hidden roles never block; a dead model falls back ────────────


def test_a_hidden_roles_override_is_dropped_when_its_model_goes_away(client, caplog):
    import logging

    provider_id, row = _acme_model(client, "acme-org/Coder-7B")
    ref = f"/api/settings/model-providers/{provider_id}/models/{row['id']}"
    # the Skill Lab target (hidden feature) is overridden to the acme model
    assert client.put(f"{ROLES_PREFIX}/skill_lab_target",
                      json={"model_id": row["model_id"]}).status_code == 200
    assert get_settings().skill_lab_target_model_id == row["model_id"]
    with caplog.at_level(logging.WARNING, logger="launchpad.model_roles"):
        off = client.put(ref, json={"enabled": False})  # not blocked: the role is hidden
    assert off.status_code == 200, off.text
    # the override was dropped (journaled reset), the role is back at its built-in default
    assert get_settings().skill_lab_target_model_id == KIMI
    assert "eval_judge_model_id" not in model_roles.overrides()
    assert "skill_lab_target_model_id" not in model_roles.overrides()
    assert any("skill_lab_target" in r.message and "override was dropped" in r.message
               for r in caplog.records)
    body = client.get("/api/settings/model-providers/changes").json()
    assert body["items"][0]["operation"] == "model_role.reset"
    # a VISIBLE role pointing at the same model is still a 409 — nothing weakened
    assert client.put(ref, json={"enabled": True}).status_code == 200
    pointed = client.put(f"{ROLES_PREFIX}/judge", json={"model_id": row["model_id"]})
    assert pointed.status_code == 200
    assert client.put(ref, json={"enabled": False}).status_code == 409


def test_role_resolution_falls_back_to_the_default_when_the_model_is_dead(
    client, caplog, monkeypatch,
):
    import logging

    from app.schemas.agent import agent_default_model_id, judge_default_model_id

    assert client.put(f"{ROLES_PREFIX}/agent_default", json={"model_id": KIMI}).status_code == 200
    assert agent_default_model_id() == KIMI and model_roles.resolve("agent_default") == KIMI
    # the catalog as the consumers see it, without the Kimi row (the registry refresher would
    # republish a `publish()`ed variant, so the read itself is patched)
    current = model_catalog.current()
    without = model_catalog.Catalog(  # `extended()` merges rows — build the trimmed one directly
        version=current.version, providers=dict(current.providers),
        models={mid: r for mid, r in current.models.items() if mid != KIMI},
        workspace_disabled=dict(current.workspace_disabled), revision=current.revision)
    monkeypatch.setattr(model_catalog, "current", lambda: without)
    model_roles._warned.clear()
    with caplog.at_level(logging.WARNING, logger="launchpad.model_roles"):
        assert model_roles.resolve("agent_default") == GLM
        assert agent_default_model_id() == GLM
        assert model_roles.resolve("agent_default") == GLM  # a second call warns no more
    warnings = [r for r in caplog.records if "agent_default" in r.message]
    assert len(warnings) == 1 and "not_in_catalog" in warnings[0].message
    assert judge_default_model_id() == GLM  # untouched roles resolve as before
    monkeypatch.undo()
    assert agent_default_model_id() == KIMI  # the override itself was never dropped
