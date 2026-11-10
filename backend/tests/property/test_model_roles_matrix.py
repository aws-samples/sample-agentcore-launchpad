"""E21 — model roles matrix (round 16, channel A; model-registry.md §12).

Roles × model states (enabled / disabled / no tool capability / deleted / other provider
disabled / embedding row / empty / unknown) × process events (``get_settings.cache_clear``,
a racing sibling rebuild, a second-process re-sync) → every set / reset outcome must match
§12.3 (422 reasons exactly; 409 ``in_use`` on disable / delete of a referenced model and
its provider; nothing refused reaches the ledger or the journal), and a DIFFERENTIAL of the
precedence console > env > yaml > default for every role.

Findings (the register names them): N16-obs22 (a reset landed on a default disabled while
overridden — FIXED: 422 + `default_disabled` flag), N16-obs24 (the two Skill Lab role fields
accepted a non-catalog env / yaml value — FIXED: same validator), N16-obs23 (a second
process's override row is not a referrer for disable / delete until this process re-reads
the roles — strict xfail, open).
"""

from __future__ import annotations

import pytest
import yaml

from app.core import config as config_module
from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.models.model_registry import PlatformSetting
from app.schemas import model_catalog
from app.services import model_registry, model_roles

pytestmark = [pytest.mark.property, pytest.mark.siliconflow]

GLM = "global.zai.glm-5.3"
KIMI = "global.moonshotai.kimi-k3"
TITAN = "amazon.titan-embed-text-v2:0"
ROLES_PREFIX = "/api/settings/model-roles"
DEFAULTS = "/api/model-defaults"
PROVIDERS = "/api/settings/model-providers"
ROLE_NAMES = [r.name for r in model_roles.ROLES]
STATES = ("enabled_tools", "no_tools", "disabled_model", "disabled_provider", "deleted",
          "embedding", "empty", "whitespace", "not_in_catalog")


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.delenv("LAUNCHPAD_SILICONFLOW_API_KEY", raising=False)
    yield
    model_catalog.reset()
    get_settings.cache_clear()


def _provider(client, name: str, enabled: bool = True) -> str:
    created = client.post(PROVIDERS, json={
        "name": name, "kind": "openai_compatible", "label": name.title(),
        "base_url": f"https://api.siliconflow.cn/{name}/v1"})
    assert created.status_code == 201, created.text
    provider_id = created.json()["id"]
    if not enabled:
        off = client.put(f"{PROVIDERS}/{provider_id}", json={"enabled": False})
        assert off.status_code == 200, off.text
    return provider_id


def _model(client, provider_id: str, name: str, *, tools: bool = True, enabled: bool = True,
           kind: str = "chat") -> dict:
    body = {"api_model_name": name, "kind": kind, "probe": False, "enabled": enabled,
            "overrides": {"tools": tools}}
    res = client.post(f"{PROVIDERS}/{provider_id}/models", json=body)
    assert res.status_code == 201, res.text
    return res.json()


def _role(body, name):
    return next(r for r in body["roles"] if r["role"] == name)


def _rows() -> dict[str, str]:
    db = SessionLocal()
    try:
        return {row.key: row.value for row in db.query(PlatformSetting)}
    finally:
        db.close()


def _journal(client) -> list[str]:
    items = client.get(f"{PROVIDERS}/changes").json()["items"]
    return [c["operation"] for c in items if c["operation"].startswith("model_role.")]


class World:
    """One provider with four rows + one disabled provider with one row."""

    def __init__(self, client) -> None:
        self.client = client
        self.acme = _provider(client, "acme")
        self.tools = _model(client, self.acme, "acme-org/Tools-7B")
        self.plain = _model(client, self.acme, "acme-org/Plain-7B", tools=False)
        self.off = _model(client, self.acme, "acme-org/Off-7B", enabled=False)
        gone = _model(client, self.acme, "acme-org/Gone-7B")
        assert client.delete(f"{PROVIDERS}/{self.acme}/models/{gone['id']}").status_code == 204
        self.gone_id = gone["model_id"]
        self.other = _provider(client, "other")
        self.other_row = _model(client, self.other, "other-org/Chat-7B")
        assert client.put(f"{PROVIDERS}/{self.other}", json={"enabled": False}).status_code == 200

    def candidate(self, state: str) -> str:
        return {
            "enabled_tools": self.tools["model_id"], "no_tools": self.plain["model_id"],
            "disabled_model": self.off["model_id"], "disabled_provider": self.other_row["model_id"],
            "deleted": self.gone_id, "embedding": TITAN, "empty": "", "whitespace": "   ",
            "not_in_catalog": "global.anthropic.claude-sonnet-5",
        }[state]


def expected(role: model_roles.Role, state: str) -> str | None:
    """§12.3 rule 1: "A role accepts only a model that is in the catalog AND enabled (model
    row and provider), is a chat row, and — for needs_tools roles — has tools: true.
    Otherwise 422 models.model_not_allowed {role, model_id, reason}, reason ∈ empty |
    not_in_catalog | not_chat | disabled | no_tool_use. '' is accepted only where allow_empty."
    The order below is the order §12.3 lists the reasons in; the state names map 1:1."""
    if state in ("empty", "whitespace"):
        return None if role.allow_empty else "empty"
    if state in ("deleted", "not_in_catalog"):
        return "not_in_catalog"
    if state == "embedding":
        return "not_chat"
    if state in ("disabled_model", "disabled_provider"):
        return "disabled"
    if state == "no_tools":
        return "no_tool_use" if role.needs_tools else None
    return None


# ── roles × states × process events ──────────────────────────────────────────


@pytest.mark.parametrize("role_name", ROLE_NAMES)
def test_set_outcomes_match_the_contract_for_every_state(client, role_name):
    world = World(client)
    role = model_roles.role(role_name)
    baseline = _role(client.get(DEFAULTS).json(), role_name)
    assert baseline["overridden_by"] is None and baseline["model_id"] == baseline["default"]
    for state in STATES:
        value = world.candidate(state)
        before_rows, before_journal = _rows(), _journal(client)
        res = client.put(f"{ROLES_PREFIX}/{role_name}", json={"model_id": value})
        reason = expected(role, state)
        if reason is not None:
            assert res.status_code == 422, (state, res.text)
            assert res.json()["code"] == "models.model_not_allowed"
            assert res.json()["detail"] == {"role": role_name, "model_id": value.strip(),
                                            "reason": reason}, state
            # nothing refused reaches the ledger, the journal or the Settings
            assert _rows() == before_rows and _journal(client) == before_journal
            assert getattr(get_settings(), role.field) == baseline["model_id"]
            continue
        assert res.status_code == 200, (state, res.text)
        stored = value.strip()
        assert res.json()["model_id"] == stored and res.json()["default"] == baseline["default"]
        assert _rows()[role.field] == stored
        assert _journal(client)[0] == "model_role.set"
        # §12.2: "get_settings.cache_clear() never reverts an override"; "a racing sibling
        # build keeps the true default"; "effective(db) re-syncs when the ledger rows differ
        # from the snapshot (a second process wrote)"; load_overrides at startup
        assert getattr(get_settings(), role.field) == stored
        get_settings.cache_clear()
        assert getattr(get_settings(), role.field) == stored, "cache_clear reverted the role"
        get_settings.cache_clear()
        get_settings()
        get_settings.__wrapped__()  # a racing sibling build, hooked last, not cached
        assert getattr(get_settings(), role.field) == stored
        row = _role(client.get(DEFAULTS).json(), role_name)
        assert (row["model_id"], row["default"]) == (stored, baseline["default"]), state
        model_roles.reset_overrides()  # a second process: cold snapshot ...
        get_settings.cache_clear()
        assert getattr(get_settings(), role.field) == baseline["default"]
        with SessionLocal() as db:
            model_roles.load_overrides(db)  # ... reads the ledger on startup
        assert getattr(get_settings(), role.field) == stored
        # and the reset returns to the default from every one of those states
        reset = client.delete(f"{ROLES_PREFIX}/{role_name}")
        assert reset.status_code == 200 and reset.json()["model_id"] == baseline["default"]
        assert reset.json()["overridden_by"] is None and role.field not in _rows()
        assert getattr(get_settings(), role.field) == baseline["default"]
        get_settings.cache_clear()
        assert getattr(get_settings(), role.field) == baseline["default"]


@pytest.mark.parametrize("role_name", ROLE_NAMES)
def test_a_referenced_model_and_its_provider_are_pinned_until_repointed(client, role_name):
    world = World(client)
    ref = f"{PROVIDERS}/{world.acme}/models/{world.tools['id']}"
    pointed = client.put(f"{ROLES_PREFIX}/{role_name}", json={"model_id": world.tools["model_id"]})
    assert pointed.status_code == 200, pointed.text
    if model_roles.role_hidden(model_roles.role(role_name)):
        # rev-r25 F3 (N17-obs252): a role of a HIDDEN console feature (Skill Lab, the Studio
        # coding agent) is no referrer — the administrator cannot see it to repoint it. The
        # disable goes through, the role's override is dropped (back to its default), and the
        # model is really gone for it; a deleted id is still refused as not_in_catalog.
        field = model_roles.role(role_name).field
        assert model_registry.setting_referrers({world.tools["model_id"]}) == []
        assert client.put(ref, json={"enabled": False}).status_code == 200
        assert getattr(get_settings(), field) == Settings.model_fields[field].default
        assert field not in _rows()  # the console override is gone (journaled reset)
        assert not model_catalog.current().model_enabled(world.tools["model_id"])
        assert client.put(ref, json={"enabled": True}).status_code == 200
        assert client.delete(ref).status_code == 204
        gone = client.put(f"{ROLES_PREFIX}/{role_name}",
                          json={"model_id": world.tools["model_id"]})
        assert gone.status_code == 422 and gone.json()["detail"]["reason"] == "not_in_catalog"
        return
    # §12.3 rule 2: "deleting a model / provider a role points at is 409 model.in_use /
    # model_provider.in_use … and disabling such a model or its provider is now 409 too
    # ({referrers: [setting rows only], suggest: repoint})"
    for attempt in (client.put(ref, json={"enabled": False}), client.delete(ref)):
        assert attempt.status_code == 409, attempt.text
        body = attempt.json()
        assert body["code"] == "model.in_use"
        setting_refs = [r for r in body["detail"]["referrers"] if r["kind"] == "setting"]
        assert [(r["id"], r["role"], r["model_ids"]) for r in setting_refs] == [
            (model_roles.role(role_name).field, role_name, [world.tools["model_id"]])]
    for attempt in (client.put(f"{PROVIDERS}/{world.acme}", json={"enabled": False}),
                    client.delete(f"{PROVIDERS}/{world.acme}")):
        assert attempt.status_code == 409 and attempt.json()["code"] == "model_provider.in_use"
        assert role_name in {r.get("role") for r in attempt.json()["detail"]["referrers"]}
    # the model is still enabled and still the role's: nothing half-applied
    assert model_catalog.current().model_enabled(world.tools["model_id"])
    assert getattr(get_settings(), model_roles.role(role_name).field) == world.tools["model_id"]
    # repoint (reset): everything is allowed again
    assert client.delete(f"{ROLES_PREFIX}/{role_name}").status_code == 200
    assert client.put(ref, json={"enabled": False}).status_code == 200
    assert client.put(ref, json={"enabled": True}).status_code == 200
    assert client.delete(ref).status_code == 204
    # a role pointed at a now-deleted id is refused as not_in_catalog
    gone = client.put(f"{ROLES_PREFIX}/{role_name}", json={"model_id": world.tools["model_id"]})
    assert gone.status_code == 422 and gone.json()["detail"]["reason"] == "not_in_catalog"


# ── the precedence differential: console > env > yaml > default ──────────────


@pytest.fixture
def layers(monkeypatch, tmp_path):
    """Set / clear the yaml and env layers of one Settings field and rebuild the cache."""
    yaml_file = tmp_path / "launchpad.yaml"
    monkeypatch.setattr(config_module, "CONFIG_FILE", yaml_file)

    class Layers:
        def yaml(self, field: str, value: str | None) -> None:
            data = yaml.safe_load(yaml_file.read_text()) if yaml_file.is_file() else {}
            data = data or {}
            if value is None:
                data.pop(field, None)
            else:
                data[field] = value
            yaml_file.write_text(yaml.safe_dump(data))
            get_settings.cache_clear()

        def env(self, field: str, value: str | None) -> None:
            name = f"LAUNCHPAD_{field.upper()}"
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
            get_settings.cache_clear()

    yield Layers()
    monkeypatch.undo()
    get_settings.cache_clear()


@pytest.mark.parametrize("role_name", ROLE_NAMES)
def test_precedence_console_env_yaml_default_for_every_role(client, layers, role_name):
    # §12.2: "Precedence: console override > LAUNCHPAD_* env > launchpad.yaml > built-in
    # default"; "a reset returns to the env/yaml value" (CLAUDE.md config precedence). Each
    # layer is flipped on the SAME process and read back through one Settings rebuild.
    role = model_roles.role(role_name)
    world = World(client)
    yaml_value = _model(client, world.acme, "acme-org/Yaml-7B")["model_id"]
    env_value = _model(client, world.acme, "acme-org/Env-7B")["model_id"]
    console_value = world.tools["model_id"]
    builtin = Settings.model_fields[role.field].default

    def row():
        body = _role(client.get(DEFAULTS).json(), role_name)
        return body["model_id"], body["default"], body["overridden_by"]

    assert row() == (builtin, builtin, None)
    layers.yaml(role.field, yaml_value)
    assert row() == (yaml_value, yaml_value, None), "yaml beats the built-in default"
    layers.env(role.field, env_value)
    assert row() == (env_value, env_value, None), "env beats yaml"
    saved = client.put(f"{ROLES_PREFIX}/{role_name}", json={"model_id": console_value})
    assert saved.status_code == 200, saved.text
    assert row() == (console_value, env_value, get_settings().auth_username), "console beats env"
    get_settings.cache_clear()
    assert getattr(get_settings(), role.field) == console_value
    # the referrer rule follows the EFFECTIVE value only — and (rev-r25 F3) never names a
    # role of a hidden console feature
    referred = {r["role"] for r in model_registry.setting_referrers({console_value})}
    if model_roles.role_hidden(role):
        assert role_name not in referred
    else:
        assert role_name in referred
    assert role_name not in {r["role"] for r in model_registry.setting_referrers({env_value})}
    # reset returns to env, not yaml, not the built-in
    assert client.delete(f"{ROLES_PREFIX}/{role_name}").json()["model_id"] == env_value
    assert row() == (env_value, env_value, None)
    layers.env(role.field, None)
    assert row() == (yaml_value, yaml_value, None), "without env the yaml value is back"
    layers.yaml(role.field, None)
    assert row() == (builtin, builtin, None), "without yaml the built-in default is back"


@pytest.mark.parametrize("role_name", ROLE_NAMES)
def test_env_and_yaml_values_must_be_catalog_models_for_every_role(client, layers, role_name):
    role = model_roles.role(role_name)
    layers.env(role.field, "not-a-catalog-model")
    with pytest.raises(ValueError, match="model.not_allowed"):
        Settings()


# ── the two states the matrix found (strict xfails) ──────────────────────────


def test_a_reset_never_lands_a_role_on_a_disabled_model(client, layers):
    world = World(client)
    default_row = _model(client, world.acme, "acme-org/Default-7B")
    layers.env("eval_judge_model_id", default_row["model_id"])
    assert _role(client.get(DEFAULTS).json(), "judge")["model_id"] == default_row["model_id"]
    assert client.put(f"{ROLES_PREFIX}/judge",
                      json={"model_id": world.tools["model_id"]}).status_code == 200
    # the env default is not the effective value, so disabling it is allowed ...
    off = client.put(f"{PROVIDERS}/{world.acme}/models/{default_row['id']}",
                     json={"enabled": False})
    assert off.status_code == 200, off.text
    # ... the read flags the role (N16-obs22 ruling) ...
    assert _role(client.get(DEFAULTS).json(), "judge")["default_disabled"] is True
    # ... and the reset refuses instead of handing the role a disabled model
    reset = client.delete(f"{ROLES_PREFIX}/judge")
    assert reset.status_code == 422, reset.text
    assert reset.json()["code"] == "models.model_not_allowed"
    assert reset.json()["detail"] == {"role": "judge", "model_id": default_row["model_id"],
                                      "reason": "disabled", "suggest": "repoint"}
    assert get_settings().eval_judge_model_id == world.tools["model_id"]  # still overridden
    assert model_catalog.current().model_enabled(get_settings().eval_judge_model_id)
    # re-enable the default: the flag clears and the reset goes through
    assert client.put(f"{PROVIDERS}/{world.acme}/models/{default_row['id']}",
                      json={"enabled": True}).status_code == 200
    assert _role(client.get(DEFAULTS).json(), "judge")["default_disabled"] is False
    assert client.delete(f"{ROLES_PREFIX}/judge").json()["model_id"] == default_row["model_id"]


@pytest.mark.xfail(
    strict=True,
    reason="N16-obs23: `setting_referrers` reads the cached Settings only; an override row "
           "written by a second process (visible on the next GET /api/model-defaults) is not "
           "a referrer for disable / delete until this process re-reads the roles, so the "
           "model a sibling process just pointed a role at can be disabled (200, not 409)",
)
def test_a_second_process_override_pins_the_model_it_points_at(client):
    world = World(client)
    with SessionLocal() as db:
        db.add(PlatformSetting(key="eval_judge_model_id", value=world.tools["model_id"],
                               updated_by="other-process"))
        db.commit()
    ref = f"{PROVIDERS}/{world.acme}/models/{world.tools['id']}"
    off = client.put(ref, json={"enabled": False})
    assert off.status_code == 409, off.text
    assert off.json()["code"] == "model.in_use"
    assert _role(client.get(DEFAULTS).json(), "judge")["model_id"] == world.tools["model_id"]
