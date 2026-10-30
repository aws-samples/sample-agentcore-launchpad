"""`services/model_registry.py` + the snapshot holder (D21): seed, CRUD, journal versions,
refresh / stamp invalidation, the function views, enable flags, referrers, no secret at rest."""

import json

import pytest

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError, NotFoundError
from app.models.ledger import Agent
from app.models.model_registry import (
    Model,
    ModelCatalogChange,
    ModelProvider,
    WorkspaceModelSetting,
)
from app.schemas import agent as schema
from app.schemas import model_catalog
from app.schemas.agent import AgentSpec
from app.services import model_registry

BASE = {"name": "registry-probe", "method": "zip_runtime", "system_prompt": "p"}
SF = "siliconflow.zai-org/GLM-5.3"
SF_ENV = "LAUNCHPAD_SILICONFLOW_API_KEY"
SF_RECORD = {"model_credentials": {"siliconflow": {"configured": True}}}
ACME_URL = "https://api.siliconflow.cn/acme/v1"  # on the default host allowlist


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.delenv(SF_ENV, raising=False)
    monkeypatch.delenv("LAUNCHPAD_ACME_API_KEY", raising=False)
    # D21-obs2: the holder may carry state from create_app()/ensure_seeded() in this worker;
    # reset BEFORE the body so every test starts from the built-in catalog (order-independent)
    model_catalog.reset()
    yield
    model_registry.set_probe_hooks(None)
    model_registry.set_embedding_guard(None)
    model_catalog.reset()


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def seeded(db):
    return model_registry.ensure_seeded(db)


@pytest.fixture
def empty_registry(db):
    """D21-obs2: the shared per-process temp ledger may already carry the rows an earlier
    `create_app()` / `ensure_seeded()` inserted (conftest's `clean_tables` wipes AFTER each
    test, so the first test of a worker sees them). Empty the four registry tables before
    the body so the test means the same alone and under xdist."""
    for table in (WorkspaceModelSetting, ModelCatalogChange, Model, ModelProvider):
        db.query(table).delete()
    db.commit()
    model_catalog.reset()
    yield db


def _acme(db, **extra) -> ModelProvider:
    return model_registry.create_provider(db, {
        "name": "acme", "kind": "openai_compatible", "label": "Acme", "base_url": ACME_URL,
        **extra}, actor="op")


def _acme_model(db, provider, name="acme-org/Foo-7B", **extra) -> Model:
    return model_registry.create_model(db, provider, {"api_model_name": name, "kind": "chat",
                                                      **extra}, actor="op")


# ── seed / resolution order ──────────────────────────────────────────────────────────
def test_empty_ledger_resolves_to_the_builtin_catalog(empty_registry):
    db = empty_registry
    assert db.query(Model).count() == 0 and db.query(ModelCatalogChange).count() == 0
    assert model_registry.load_catalog(db) is model_catalog.BUILTIN_CATALOG
    assert model_registry.stamp(db) == 0


def test_seed_inserts_todays_rows_once_and_publishes(db):
    """R13 (N13-obs7): a fresh ledger seeds Global Bedrock (GLM 5.3, Kimi K3, Titan v2) only —
    no OpenAI-compatible provider."""
    first = model_registry.ensure_seeded(db)
    assert first is model_catalog.current() and first.version >= 1
    assert db.query(ModelProvider).count() == 1 and db.query(Model).count() == 3
    assert {p.name for p in db.query(ModelProvider)} == {"bedrock"}
    assert {m.model_id for m in db.query(Model)} == {
        "global.zai.glm-5.3", "global.moonshotai.kimi-k3", "amazon.titan-embed-text-v2:0"}
    bedrock = db.query(ModelProvider).filter_by(name="bedrock").one()
    assert bedrock.required and bedrock.builtin and bedrock.credential_source == "host_env"
    assert bedrock.credential_ssm_parameter == "launchpad-global-bedrock-api-key"
    assert bedrock.model_region == "us-west-2" and bedrock.id_prefix == ""
    for row in db.query(Model):
        assert row.source == "builtin"
        assert set(row.capability_sources) == set(model_catalog.CAPABILITY_SOURCE_FIELDS)
        assert set(row.capability_sources.values()) == {"builtin"}
    second = model_registry.ensure_seeded(db)
    assert second.version == first.version and db.query(Model).count() == 3
    seed = [c for c in model_registry.list_changes(db) if c.operation == "seed"]
    assert len(seed) == 1 and set(seed[0].after) == {"providers", "models"}
    assert seed[0].after["providers"] == ["bedrock"]


def test_an_admin_registers_siliconflow_with_an_ssm_parameter_name(db, seeded):
    """R13 (N13-obs7): the provider TYPE stays supported — an administrator adds SiliconFlow
    through the registry API with the SSM parameter NAME of its key (never the key), and the
    rows the seed used to carry come back with the same ids, names and credential names."""
    from tests import model_fixtures

    assert "siliconflow" not in model_catalog.current().providers
    provider = model_fixtures.register_siliconflow(db)
    assert not provider.builtin and not provider.required and provider.enabled
    assert provider.kind == "openai_compatible" and provider.id_prefix == "siliconflow."
    assert provider.credential_source == "ssm"
    assert provider.credential_ssm_parameter == "launchpad-siliconflow-api-key"
    assert provider.credential_provider == "launchpad-siliconflow"
    assert provider.credential_env == "LAUNCHPAD_SILICONFLOW_API_KEY"
    snapshot = model_catalog.current()
    assert snapshot.version > seeded.version
    assert [m for m in snapshot.allowed_ids() if m.startswith("siliconflow.")] == [
        SF, "siliconflow.deepseek-ai/DeepSeek-V4-Pro", "siliconflow.Qwen/Qwen3.8-27B",
        "siliconflow.Pro/moonshotai/Kimi-K2.6"]
    row = snapshot.models[SF]
    assert row.source == "manual" and row.label == "GLM-5.3 (SiliconFlow)" and row.tools
    assert row.default_max_tokens == 8192 and row.default_judge_max_tokens == 4096
    assert schema.providers()["siliconflow"] == {
        "kind": "openai_compatible", "label": "SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "credential_provider": "launchpad-siliconflow",
        "ssm_parameter": "launchpad-siliconflow-api-key",
        "api_key_env": "LAUNCHPAD_SILICONFLOW_API_KEY", "required": False,
        "id_prefix": "siliconflow."}
    # unconfigured until the parameter exists — the key is never part of the registry
    assert schema.configured_providers() == {"bedrock": True, "siliconflow": False}
    assert schema.configured_providers(SF_RECORD)["siliconflow"] is True
    # the journal recorded the admin's writes, not a seed
    ops = [c.operation for c in model_registry.list_changes(db, limit=10)]
    assert ops[:5] == ["model.create"] * 4 + ["provider.create"]


def test_a_ledger_seeded_before_r13_keeps_its_siliconflow_rows(db, seeded):
    """R13 (N13-obs7): the seed runs once — a ledger that already carries the pre-R13
    built-in SiliconFlow rows is left exactly as it is (nothing removed, nothing re-flagged)."""
    legacy = ModelProvider(
        name="siliconflow", kind="openai_compatible", label="SiliconFlow",
        base_url="https://api.siliconflow.cn/v1", credential_provider="launchpad-siliconflow",
        credential_ssm_parameter="launchpad-siliconflow-api-key", credential_source="ssm",
        credential_env="LAUNCHPAD_SILICONFLOW_API_KEY", required=False, builtin=True,
        enabled=True, id_prefix="siliconflow.", probe=dict(model_registry.PROBE_NEVER),
        created_by="system", updated_by="system")
    db.add(legacy)
    db.flush()
    db.add(Model(
        provider_id=legacy.id, model_id=SF, api_model_name="zai-org/GLM-5.3",
        label="GLM-5.3 (SiliconFlow)", kind="chat", enabled=True, is_default=False,
        tools=True, streaming=True, vision=False,
        inference_params={"temperature": True, "top_p": True}, default_max_tokens=8192,
        max_output_tokens=None, default_judge_max_tokens=4096, embedding_dimension=None,
        embedding_max_chars=None, source="builtin",
        capability_sources=dict.fromkeys(model_catalog.CAPABILITY_SOURCE_FIELDS, "builtin"),
        probe=None, sort_order=2, created_by="system", updated_by="system"))
    db.commit()
    before = (db.query(ModelProvider).count(), db.query(Model).count())
    # the startup sequence: `init_db()` (the migration funnel — N17-obs282 moved the obs207
    # unpin there) then `ensure_seeded()`
    from app.core.db import init_db

    init_db()
    db.expire_all()
    again = model_registry.ensure_seeded(db)
    assert (db.query(ModelProvider).count(), db.query(Model).count()) == before == (2, 4)
    kept = db.query(ModelProvider).filter_by(name="siliconflow").one()
    # N17-obs207: the legacy preset row is kept but is NO LONGER built in (deletable) — the
    # hub Bedrock row is the only undeletable provider; its model rows keep `source: builtin`
    assert not kept.builtin and not kept.required
    assert kept.enabled and kept.credential_source == "ssm"
    assert db.query(Model).filter_by(model_id=SF).one().source == "builtin"
    assert SF in again.allowed_ids() and not again.providers["siliconflow"].builtin
    assert again.providers["bedrock"].builtin
    assert len([c for c in model_registry.list_changes(db) if c.operation == "seed"]) == 1


def test_the_legacy_unpin_is_a_startup_migration_never_a_read(db, seeded, caplog):
    """N17-obs282 (design simplification C7): the N17-obs207 data fix (a pre-R13 `builtin`
    SiliconFlow row becomes deletable) is a migration in `core/db._migrate` — it runs in
    `init_db()`, idempotently, and no longer on every `ensure_seeded()` (which the registry
    page's GET calls on each read)."""
    import logging

    from app.core import db as core_db

    legacy = ModelProvider(
        name="siliconflow", kind="openai_compatible", label="SiliconFlow",
        base_url="https://api.siliconflow.cn/v1", credential_provider="launchpad-siliconflow",
        credential_ssm_parameter="launchpad-siliconflow-api-key", credential_source="ssm",
        credential_env="LAUNCHPAD_SILICONFLOW_API_KEY", required=True, builtin=True,
        enabled=False, id_prefix="siliconflow.", probe=dict(model_registry.PROBE_NEVER),
        created_by="system", updated_by="system")
    db.add(legacy)
    db.commit()
    model_registry.ensure_seeded(db)  # the read path: no data fix any more
    db.expire_all()
    row = db.query(ModelProvider).filter_by(name="siliconflow").one()
    assert row.builtin is True and row.required is True
    assert not hasattr(model_registry, "LEGACY_UNPINNED_PROVIDERS")
    assert core_db.LEGACY_UNPINNED_PROVIDERS == ("siliconflow",)
    caplog.set_level(logging.INFO, logger="launchpad.model_registry")
    core_db.init_db()  # the startup funnel flips it …
    db.expire_all()
    row = db.query(ModelProvider).filter_by(name="siliconflow").one()
    assert row.builtin is False and row.required is False
    assert "no longer built in" in caplog.text
    assert model_registry.list_changes(db, target_id=row.id) == []  # no journal row
    bedrock = db.query(ModelProvider).filter_by(name="bedrock").one()
    assert bedrock.builtin is True  # … the hub Bedrock row is never touched
    caplog.clear()
    core_db.init_db()  # … and is idempotent: nothing to change, no log line
    assert "no longer built in" not in caplog.text


# ── providers ────────────────────────────────────────────────────────────────────────
def test_create_provider_derives_names_and_bumps_the_version(db, seeded):
    row = _acme(db)
    assert row.id.startswith("mp_") and row.credential_provider == "launchpad-acme"
    assert row.credential_ssm_parameter == "launchpad-acme-api-key"
    assert row.credential_env == "LAUNCHPAD_ACME_API_KEY" and row.credential_source == "ssm"
    assert row.id_prefix == "acme." and not row.builtin and not row.required and row.enabled
    assert row.base_url == ACME_URL and row.model_region is None
    snapshot = model_catalog.current()
    assert snapshot.version == seeded.version + 1 == model_registry.stamp(db)
    assert "acme" in snapshot.providers and "acme" in schema.providers()
    assert schema.providers()["acme"] == {
        "kind": "openai_compatible", "label": "Acme", "base_url": ACME_URL,
        "credential_provider": "launchpad-acme", "ssm_parameter": "launchpad-acme-api-key",
        "api_key_env": "LAUNCHPAD_ACME_API_KEY", "required": False, "id_prefix": "acme."}
    assert schema.configured_providers() == {"bedrock": True, "acme": False}
    change = model_registry.list_changes(db)[0]
    assert change.operation == "provider.create" and change.target_id == row.id
    assert change.before is None and change.after["name"] == "acme" and change.actor == "op"


@pytest.mark.parametrize("data, code", [
    ({"base_url": "http://api.siliconflow.cn/v1"}, "model_provider.invalid_base_url"),
    ({"base_url": "https://user:pw@api.siliconflow.cn/v1"}, "model_provider.invalid_base_url"),
    ({"base_url": "https://10.0.0.8/v1"}, "model_provider.invalid_base_url"),
    ({"base_url": "https://169.254.169.254/latest"}, "model_provider.invalid_base_url"),
    ({"base_url": None}, "model_provider.invalid_base_url"),
    ({"credential_ssm_parameter": "my-key"}, "model_provider.invalid_ssm_parameter"),
    ({"name": "Acme"}, "model_provider.invalid_name"),
    ({"kind": "anthropic"}, "model_provider.invalid_kind"),
])
def test_create_provider_refuses_bad_input_before_writing(db, seeded, data, code):
    version = model_registry.stamp(db)
    with pytest.raises(AppError) as exc:
        _acme(db, **data)
    assert exc.value.code == code and exc.value.status_code == 422
    assert model_registry.stamp(db) == version and db.query(ModelProvider).count() == 1


def test_host_allowlist_follows_the_operator_setting_and_wildcards():
    """N17-obs203 (user decision 2026-10-21): the operator allowlist is no longer a
    precondition — an EMPTY list (the new default) lets any public https host through; a
    NON-EMPTY list is lock mode and still restricts (exact or `*.suffix`); the denylist is
    checked always. The SSRF half (IP literal, private resolution) is unchanged."""
    assert get_settings().model_provider_allowed_hosts == []
    assert get_settings().model_provider_denied_hosts == []
    # default: any public https host, with or without a trailing slash
    assert model_registry.validate_base_url("https://api.siliconflow.cn/v1/", "openai_compatible"
                                            ) == "https://api.siliconflow.cn/v1"
    assert model_registry.validate_base_url("https://api.ppinfra.com/v3/openai",
                                            "openai_compatible") == "https://api.ppinfra.com/v3/openai"
    assert model_registry.validate_base_url("https://api.acme.example/v1", "openai_compatible")
    # lock mode: a non-empty allowlist restricts (exact + wildcard)
    assert model_registry.validate_base_url("https://api.acme.example/v1", "openai_compatible",
                                            ["api.acme.example"])
    assert model_registry.validate_base_url("https://cn.siliconflow.cn/x", "openai_compatible",
                                            ["*.siliconflow.cn"])
    with pytest.raises(AppError) as exc:
        model_registry.validate_base_url("https://api.acme.example/v1", "openai_compatible",
                                         ["*.other.example"])
    assert exc.value.code == "model_provider.host_not_allowed"
    assert exc.value.detail["setting"] == "model_provider_allowed_hosts"
    assert exc.value.detail["mode"] == "lock" and "lock mode" in exc.value.message
    # denylist: refused whatever the allowlist says, exact or wildcard
    for denied in (["api.acme.example"], ["*.acme.example"]):
        with pytest.raises(AppError) as exc:
            model_registry.validate_base_url("https://api.acme.example/v1", "openai_compatible",
                                             ["api.acme.example"], denied)
        assert exc.value.code == "model_provider.host_not_allowed"
        assert exc.value.detail["setting"] == "model_provider_denied_hosts"
        assert exc.value.detail["mode"] == "deny"
    # the SSRF half still holds without any list
    for bad in ("https://10.0.0.8/v1", "https://[::1]/v1", "http://api.acme.example/v1"):
        with pytest.raises(AppError) as exc:
            model_registry.validate_base_url(bad, "openai_compatible")
        assert exc.value.code == "model_provider.invalid_base_url", bad
    assert model_registry.validate_base_url(None, "bedrock") is None
    with pytest.raises(AppError):
        model_registry.validate_base_url("https://api.siliconflow.cn", "bedrock")


def test_host_lists_are_read_from_settings_when_not_passed(monkeypatch):
    """The Settings fields drive the default call (the routers pass nothing)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "model_provider_allowed_hosts", ["*.siliconflow.cn"])
    assert model_registry.validate_base_url("https://api.siliconflow.cn/v1", "openai_compatible")
    with pytest.raises(AppError) as exc:
        model_registry.validate_base_url("https://api.ppinfra.com/v3", "openai_compatible")
    assert exc.value.detail == {"host": "api.ppinfra.com", "mode": "lock",
                                "setting": "model_provider_allowed_hosts",
                                "allowed": ["*.siliconflow.cn"]}
    monkeypatch.setattr(settings, "model_provider_allowed_hosts", [])
    monkeypatch.setattr(settings, "model_provider_denied_hosts", ["api.ppinfra.com"])
    with pytest.raises(AppError) as exc:
        model_registry.validate_base_url("https://api.ppinfra.com/v3", "openai_compatible")
    assert exc.value.detail["setting"] == "model_provider_denied_hosts"
    assert model_registry.validate_base_url("https://api.acme.example/v1", "openai_compatible")


def test_update_and_delete_provider(db, seeded):
    row = _acme(db)
    model_registry.update_provider(db, row, {"label": "Acme Cloud", "enabled": False,
                                             "credential_ssm_parameter": "launchpad-acme2-api-key"},
                                   "op2")
    assert row.label == "Acme Cloud" and not row.enabled
    assert row.credential_ssm_parameter == "launchpad-acme2-api-key" and row.updated_by == "op2"
    assert schema.providers()["acme"]["ssm_parameter"] == "launchpad-acme2-api-key"
    # N17-obs203: a public host is accepted without an allowlist; the SSRF half still refuses
    model_registry.update_provider(db, row, {"base_url": "https://api.example.com/"}, "op")
    assert row.base_url == "https://api.example.com"
    with pytest.raises(AppError) as exc:
        model_registry.update_provider(db, row, {"base_url": "https://10.0.0.8/v1"}, "op")
    assert exc.value.code == "model_provider.invalid_base_url"
    model = _acme_model(db, row)
    assert model.model_id in schema.model_catalog_table()
    model_registry.delete_provider(db, row, "op")
    assert "acme" not in model_catalog.current().providers
    assert model.model_id not in schema.model_catalog_table() and db.query(Model).count() == 3
    ops = [c.operation for c in model_registry.list_changes(db)]
    assert ops[:5] == ["provider.delete", "model.create", "provider.update", "provider.update",
                       "provider.create"]
    with pytest.raises(NotFoundError):
        model_registry.get_provider(db, row.id)


# ── models ───────────────────────────────────────────────────────────────────────────
def test_create_model_without_probe_uses_kind_defaults_as_manual(db, seeded):
    acme = _acme(db)
    row = _acme_model(db, acme)
    assert row.id.startswith("md_") and row.model_id == "acme.acme-org/Foo-7B"
    assert row.label == "Foo-7B (Acme)" and row.kind == "chat" and row.enabled
    assert row.tools and row.streaming and not row.vision
    assert row.inference_params == {"temperature": True, "top_p": True}
    assert row.default_max_tokens == 8192 and row.default_judge_max_tokens == 4096
    assert row.source == "manual" and set(row.capability_sources.values()) == {"manual"}
    assert row.sort_order > 2 and not row.is_default  # after the three seeded rows
    # visible to every legacy view, in picker order (appended)
    assert list(schema.allowed_model_ids())[-1] == row.model_id
    entry = schema.model_catalog_table()[row.model_id]
    assert entry["provider"] == "acme" and entry["kind"] == "openai_compatible"
    assert entry["base_url"] == ACME_URL and entry["credential_provider"] == "launchpad-acme"
    assert tuple(entry) == model_catalog.LEGACY_MODEL_KEYS
    assert schema.model_capabilities()[row.model_id]["tools"] is True
    assert schema.model_default_max_tokens()[row.model_id] == 8192
    assert schema.model_provider_table()[row.model_id]["api_model_name"] == "acme-org/Foo-7B"
    assert schema.judge_budget_defaults()[row.model_id] == 4096
    assert schema.api_model_name(row.model_id) == "acme-org/Foo-7B"
    assert schema.is_openai_compatible_model(row.model_id)
    assert schema.model_provider(row.model_id) is schema.providers()["acme"]
    assert schema.byoc_model_target(row.model_id) == ("openai_compatible", "acme-org/Foo-7B")
    entries = {e["model_id"]: e for e in schema.model_catalog_entries()}
    assert entries[row.model_id]["source"] == "manual" and entries[row.model_id]["kind"] == "chat"
    assert entries[row.model_id]["available"] is False  # no key for acme anywhere
    assert schema.assert_model_allowed(row.model_id) == row.model_id
    assert AgentSpec(**BASE, model_id=row.model_id).model_id == row.model_id
    with pytest.raises(schema.ModelRefusal) as exc:
        schema.assert_model_available(row.model_id)
    assert exc.value.code == "model.provider_unconfigured" and "launchpad-acme-api-key" in str(
        exc.value)
    assert schema.model_descriptor(row.model_id) == {
        "model_id": row.model_id, "kind": "openai_compatible", "model_kind": "chat",
        "base_url": ACME_URL, "api_model_name": "acme-org/Foo-7B",
        "credential_provider": "launchpad-acme", "streaming": True,
        "inference_params": {"temperature": True, "top_p": True}, "judge_max_tokens": 4096,
        "default_max_tokens": 8192, "max_output_tokens": None,
        "catalog_version": model_catalog.current().version,
        # M1-d additive: the registry provider name + its model_region (None for an
        # OpenAI-compatible endpoint); embedding rows also carry `embedding`
        "provider": "acme", "model_region": None,
    }


def test_create_model_with_probed_findings_and_overrides_records_sources(db, seeded):
    acme = _acme(db)
    probed = _acme_model(db, acme, name="acme-org/Probed",
                         probe_record={"checked_at": "2026-10-14T10:00:00+00:00",
                                       "latency_ms": 120, "findings": {"tools": True},
                                       "errors": {}, "raw_hint": {}})
    assert probed.source == "manual"  # nothing probed: kind defaults
    row = model_registry.create_model(db, acme, {
        "api_model_name": "acme-org/Bar", "kind": "chat",
        "probe_record": {"checked_at": "x", "latency_ms": 1, "findings": {}, "errors": {},
                         "raw_hint": {}},
    }, "op", probed={"tools": False, "temperature": False, "streaming": True, "exists": True})
    assert row.source == "probed" and not row.tools
    assert row.inference_params == {"temperature": False, "top_p": True}
    assert row.capability_sources["tools"] == "probed"
    assert row.capability_sources["temperature"] == "probed"
    assert row.capability_sources["streaming"] == "probed"
    assert row.capability_sources["vision"] == "manual" and row.probe["latency_ms"] == 1
    # a judge-only row: catalog member, refused as an agent model, accepted as a judge
    assert schema.assert_model_allowed(row.model_id) == row.model_id
    with pytest.raises(schema.ModelRefusal, match="model.tools_unsupported"):
        schema.assert_agent_model_allowed(row.model_id)
    assert schema.inference_param_support(row.model_id) == {"temperature": False}
    assert schema.supported_model_config(row.model_id, {"max_tokens": 1, "temperature": 0.1}
                                         ) == {"max_tokens": 1}
    # overrides win over probed values and mark the row manual
    manual = model_registry.create_model(db, acme, {
        "api_model_name": "acme-org/Baz", "kind": "chat",
        "overrides": {"tools": True, "vision": True, "inference_params": {"top_p": False},
                      "default_max_tokens": 32768, "max_output_tokens": 65536},
    }, "op", probed={"tools": False})
    assert manual.source == "manual" and manual.tools and manual.vision
    assert manual.capability_sources["tools"] == "manual"
    assert manual.inference_params == {"temperature": True, "top_p": False}
    assert manual.default_max_tokens == 32768 and manual.max_output_tokens == 65536
    assert schema.model_max_output_tokens(manual.model_id) == 65536
    assert schema.supports_native_images(manual.model_id) is True
    with pytest.raises(AppError) as exc:
        _acme_model(db, acme, name="acme-org/Bar")
    assert exc.value.code == "model.exists" and exc.value.status_code == 409
    with pytest.raises(AppError) as exc:
        _acme_model(db, acme, name="acme-org/Emb", kind="embedding")
    assert exc.value.code == "model.invalid" and "embedding_dimension" in exc.value.message
    emb = _acme_model(db, acme, name="acme-org/Emb", kind="embedding",
                      overrides={"embedding_dimension": 768})
    assert emb.kind == "embedding" and emb.embedding_dimension == 768 and not emb.is_default
    assert emb.model_id not in schema.allowed_model_ids()  # embedding rows are never chat rows
    assert model_catalog.current().embedding_model().model_id == "amazon.titan-embed-text-v2:0"


@pytest.mark.siliconflow(registry=True)
def test_update_model_flips_sources_and_guards_the_default(db, seeded):
    sf = model_registry.get_provider(db, "siliconflow")
    row = model_registry.get_model(db, sf, SF)
    assert row.source == "manual"  # R13: an admin-registered row, not a seeded one
    model_registry.update_model(db, sf, row, {"tools": False, "label": "GLM via SF"}, "op")
    assert row.source == "manual" and row.capability_sources["tools"] == "manual"
    assert row.capability_sources["vision"] == "manual" and row.label == "GLM via SF"
    assert schema.model_capabilities()[SF]["tools"] is False
    assert schema.model_catalog_table()[SF]["label"] == "GLM via SF"
    # by Launchpad id with a slash, and by row id
    assert model_registry.get_model(db, sf, row.id) is row
    bedrock = model_registry.get_provider(db, "bedrock")
    glm = model_registry.get_model(db, bedrock, "global.zai.glm-5.3")
    kimi = model_registry.get_model(db, bedrock, "global.moonshotai.kimi-k3")
    assert glm.is_default and not kimi.is_default
    with pytest.raises(AppError) as exc:
        model_registry.update_model(db, bedrock, glm, {"enabled": False}, "op")
    assert exc.value.code == "model.is_default" and exc.value.status_code == 409
    with pytest.raises(AppError) as exc:
        model_registry.update_model(db, bedrock, glm, {"is_default": False}, "op")
    assert exc.value.code == "model.is_default"
    with pytest.raises(AppError) as exc:  # default chat model needs a configured provider
        model_registry.update_model(db, sf, row, {"is_default": True}, "op")
    assert exc.value.code == "model.default_invalid" and exc.value.status_code == 422
    model_registry.update_model(db, bedrock, kimi, {"is_default": True}, "op")
    db.refresh(glm)
    assert kimi.is_default and not glm.is_default
    entries = {e["model_id"]: e["default"] for e in schema.model_catalog_entries()}
    assert entries["global.moonshotai.kimi-k3"] is True and entries["global.zai.glm-5.3"] is False
    assert model_catalog.current().default_chat_model().model_id == "global.moonshotai.kimi-k3"
    # judge-only rows cannot be the default chat model
    model_registry.update_model(db, bedrock, glm, {"tools": False}, "op")
    with pytest.raises(AppError) as exc:
        model_registry.update_model(db, bedrock, glm, {"is_default": True}, "op")
    assert exc.value.code == "model.default_invalid"


@pytest.mark.siliconflow(registry=True)
def test_disabled_model_is_hidden_from_pickers_but_stored_specs_parse(db, seeded):
    sf = model_registry.get_provider(db, "siliconflow")
    row = model_registry.get_model(db, sf, SF)
    model_registry.update_model(db, sf, row, {"enabled": False}, "op")
    assert not model_catalog.current().model_enabled(SF)
    assert SF not in {e["model_id"] for e in schema.model_catalog_entries(SF_RECORD)}
    assert all(f"`{SF}`" not in line for line in schema.model_catalog_lines(SF_RECORD))
    # stored specs parse
    assert SF in schema.allowed_model_ids() and SF in schema.model_catalog_table()
    spec = AgentSpec(**BASE, model_id=SF)
    assert spec.model_id == SF
    assert schema.model_available(SF, SF_RECORD) is False
    with pytest.raises(schema.ModelRefusal) as exc:
        schema.assert_spec_models_available(spec, SF_RECORD)
    assert exc.value.code == "model.not_allowed" and "disabled in the model registry" in str(
        exc.value)
    with pytest.raises(schema.ModelRefusal):
        schema.assert_agent_model_available(SF, resources=SF_RECORD)
    # the architect's validator names the reason and lists only enabled, configured ids
    from app.assistant.proposal import model_catalog_errors

    errors = model_catalog_errors(SF, SF_RECORD)
    assert len(errors) == 1 and "disabled in the model registry" in errors[0]
    assert f"`{SF}`" not in errors[0].split("choose one of:")[1]
    model_registry.update_model(db, sf, row, {"enabled": True}, "op")
    assert schema.assert_spec_models_available(spec, SF_RECORD) is None
    assert model_catalog_errors(SF, SF_RECORD) == []


@pytest.mark.siliconflow(registry=True)
def test_workspace_flags_scope_availability_to_one_workspace(db, seeded):
    flags = model_registry.set_workspace_flags(db, "default", [SF, "global.moonshotai.kimi-k3"],
                                               "op")
    disabled = ["global.moonshotai.kimi-k3", SF]
    assert flags == {"id": "default", "name": "Default", "disabled_models": disabled,
                     "workspace_id": "default"}
    snapshot = model_catalog.current()
    assert snapshot.workspace_disabled == {"default": frozenset(disabled)}
    assert snapshot.model_enabled(SF) and not snapshot.model_enabled(SF, "default")
    assert schema.model_available("global.moonshotai.kimi-k3") is True
    assert schema.model_available("global.moonshotai.kimi-k3", workspace_id="default") is False
    assert schema.model_available("global.moonshotai.kimi-k3", workspace_id="other") is True
    ids_default = {e["model_id"] for e in schema.model_catalog_entries(SF_RECORD, "default")}
    ids_other = {e["model_id"] for e in schema.model_catalog_entries(SF_RECORD, "other")}
    assert "global.moonshotai.kimi-k3" not in ids_default and SF not in ids_default
    assert {"global.moonshotai.kimi-k3", SF} <= ids_other
    with pytest.raises(schema.ModelRefusal) as exc:
        schema.assert_model_available("global.moonshotai.kimi-k3", workspace_id="default")
    assert exc.value.code == "model.not_allowed" and "for this workspace" in exc.value.reason
    schema.assert_model_available("global.moonshotai.kimi-k3", workspace_id="other")
    assert model_registry.workspace_flags(db) == {"default": disabled}
    assert model_registry.workspaces_dict(db) == [
        {"id": "default", "name": "Default", "disabled_models": disabled}]
    change = model_registry.list_changes(db)[0]
    assert change.operation == "workspace.flags" and change.target_id == "default"
    assert change.before == {"disabled_models": []} and change.after == {
        "disabled_models": disabled}
    # replace semantics, unknown ids, unknown workspace
    assert model_registry.set_workspace_flags(db, "default", [], "op")["disabled_models"] == []
    assert model_catalog.current().workspace_disabled == {}
    with pytest.raises(AppError) as exc:
        model_registry.set_workspace_flags(db, "default", ["nope"], "op")
    assert exc.value.code == "model.unknown" and exc.value.status_code == 422
    with pytest.raises(NotFoundError):
        model_registry.set_workspace_flags(db, "ghost", [], "op")


def test_delete_model_rules_and_referrers(db, seeded, monkeypatch):
    bedrock = model_registry.get_provider(db, "bedrock")
    kimi = model_registry.get_model(db, bedrock, "global.moonshotai.kimi-k3")
    with pytest.raises(AppError) as exc:
        model_registry.delete_model(db, bedrock, kimi, "op")
    assert exc.value.code == "model.builtin" and exc.value.status_code == 409
    acme = _acme(db)
    row = _acme_model(db, acme)
    db.add(Agent(name="uses-acme", method="zip_runtime", status="active", workspace_id="default",
                 spec={**BASE, "model_id": row.model_id}))
    db.add(Agent(name="byoc-acme", method="byoc", status="active", workspace_id="default",
                 spec={"name": "byoc-acme", "method": "byoc", "model_id": "global.zai.glm-5.3",
                       "byoc": {"allowed_models": ["global.zai.glm-5.3", row.model_id]}}))
    db.add(Agent(name="gone", method="zip_runtime", status="deleted", workspace_id="default",
                 spec={**BASE, "model_id": row.model_id}))
    from app.evaluation.models import EvalEvaluator

    db.add(EvalEvaluator(workspace_id="default", name="acme-judge", definition="judge",
                         model_id=row.model_id))
    db.commit()
    refs = model_registry.referrers(db, {row.model_id})
    assert [(r["kind"], r["name"]) for r in refs] == [
        ("agent", "uses-acme"), ("agent", "byoc-acme"), ("evaluator", "acme-judge")]
    assert all(r["model_ids"] == [row.model_id] for r in refs)
    with pytest.raises(AppError) as exc:
        model_registry.delete_model(db, acme, row, "op")
    assert exc.value.code == "model.in_use" and exc.value.status_code == 409
    assert exc.value.detail["suggest"] == "disable" and len(exc.value.detail["referrers"]) == 3
    with pytest.raises(AppError) as exc:
        model_registry.delete_provider(db, acme, "op")
    assert exc.value.code == "model_provider.in_use"
    # settings referrers: every model ROLE's effective value (r15-e — the judge, moved here,
    # and the Skill Lab target whose default IS Kimi) + the optimizer's model list (its
    # default is the two Bedrock rows, so Kimi is already named there)
    monkeypatch.setattr(get_settings(), "eval_judge_model_id", "global.moonshotai.kimi-k3")
    refs = model_registry.referrers(db, {"global.moonshotai.kimi-k3"})
    assert [(r["kind"], r["id"]) for r in refs] == [("setting", "eval_judge_model_id"),
                                                   # N17-obs449: its default IS Kimi
                                                   ("setting", "eval_failure_detection_model_id"),
                                                   ("setting", "prompt_opt_models")]
    # rev-r25 F3: the Skill Lab target and the Studio coding agent are roles of HIDDEN
    # features — they never block a disable / delete (not referrers)
    assert [r.get("role") for r in refs] == ["judge", "failure_detection", None]
    assert refs[2]["model_ids"] == ["global.moonshotai.kimi-k3"]
    assert model_registry.referrers(db, {"siliconflow.Qwen/Qwen3.8-27B"}) == []
    # disabling stays possible while referenced; deleting works once the referrers are gone
    model_registry.update_model(db, acme, row, {"enabled": False}, "op")
    for agent in db.query(Agent):
        agent.status = "deleted"
    db.query(EvalEvaluator).delete()
    db.commit()
    version = model_registry.stamp(db)
    model_registry.delete_model(db, acme, row, "op")
    assert row.model_id not in schema.model_catalog_table()
    assert model_registry.stamp(db) == version + 1 == model_catalog.current().version
    assert model_registry.list_changes(db)[0].operation == "model.delete"
    with pytest.raises(NotFoundError):
        model_registry.get_model(db, acme, row.model_id)


# ── snapshot holder: refresh, stamp, TTL, proxies ────────────────────────────────────
@pytest.mark.siliconflow(registry=True)
def test_a_credential_only_journal_row_does_not_advance_the_catalog_version(db, seeded):
    """D21-obs5: `provider.credential` rows are audit (listed by `/changes`) but excluded
    from `stamp()` — a key registration / rotation changes nothing a runtime or preset
    snapshot can observe, so it must not raise `preset_snapshot_behind` or mark every
    deployment's `catalog_version` as behind."""
    from datetime import UTC, datetime

    from app.services import model_credential

    version = model_registry.stamp(db)
    assert version == seeded.version
    assert "provider.credential" in model_registry.AUDIT_ONLY_OPERATIONS
    row = db.query(ModelProvider).filter_by(name="siliconflow").one()
    journaled = model_credential.record_registered_key(
        db, row, version=3, modified_at=datetime(2026, 10, 14, 12, 0, tzinfo=UTC),
        created=True, actor="admin")
    assert journaled is True
    change = model_registry.list_changes(db, limit=1)[0]
    assert change.operation == "provider.credential" and change.id > version
    assert model_registry.stamp(db) == version == model_catalog.current().version
    assert model_registry._refresher() is None  # nothing to republish for a second process
    # a no-op registration (nothing created, nothing rotated) writes no row at all
    assert model_credential.record_registered_key(
        db, row, version=3, modified_at=None, created=False, rotated=False, actor="admin"
    ) is False
    assert model_registry.list_changes(db, limit=1)[0].id == change.id
    # the next catalog-shaping write skips past the credential row's id
    _acme(db)
    assert model_registry.stamp(db) > change.id
    assert model_registry.stamp(db) == model_catalog.current().version


@pytest.mark.siliconflow(registry=True)
def test_probe_journal_rows_are_audit_only_and_leave_the_refresher_idle(db, seeded):
    """D21-obs6: two admin probes (one ok, one failed) advanced the live `catalog_version`
    1 → 3 although no catalog content changed. `provider.probe` and `model.probe` rows are
    audit (listed by `/changes`) but excluded from `stamp()`, like `provider.credential`;
    the 30 s stamp check of a second process stays idle after them."""
    version = model_registry.stamp(db)
    assert version == seeded.version == model_catalog.current().version
    assert {"provider.probe", "model.probe"} <= model_registry.AUDIT_ONLY_OPERATIONS
    sf = db.query(ModelProvider).filter_by(name="siliconflow").one()
    kimi = db.query(Model).filter_by(model_id="global.moonshotai.kimi-k3").one()
    ok = model_registry._journal(db, "provider.probe", "admin", sf.id,
                                 {"probe": dict(model_registry.PROBE_NEVER)},
                                 {"probe": {"state": "ok", "models_seen": []}})
    failed = model_registry._journal(db, "provider.probe", "admin", sf.id,
                                     {"probe": {"state": "ok"}},
                                     {"probe": {"state": "failed", "error": "401"}})
    probed = model_registry._journal(db, "model.probe", "admin", kimi.id,
                                     {"tools": True}, {"tools": True})
    db.commit()
    rows = model_registry.list_changes(db, limit=3)
    assert [r.id for r in rows] == [probed.id, failed.id, ok.id]  # all three are listed …
    assert [r.operation for r in rows] == ["model.probe", "provider.probe", "provider.probe"]
    assert probed.id > version
    assert model_registry.stamp(db) == version == model_catalog.current().version  # … no bump
    assert model_registry._refresher() is None  # nothing to republish for a second process
    # the next catalog-shaping write skips past the probe rows' ids
    _acme(db)
    assert model_registry.stamp(db) > probed.id
    assert model_registry.stamp(db) == model_catalog.current().version


def test_refresh_follows_the_journal_stamp(db, seeded):
    model_catalog.publish(model_catalog.BUILTIN_CATALOG)  # a stale process
    assert model_catalog.current().version == 0
    assert model_registry._refresher().version == seeded.version
    model_registry.install_refresher()
    assert model_catalog.force_refresh().version == seeded.version
    assert model_registry._refresher() is None  # nothing changed → no republish
    _acme(db)  # a write publishes in-process AND advances the stamp
    assert model_catalog.current().version == model_registry.stamp(db) == seeded.version + 1


def test_the_refresher_compares_the_content_revision_alone(db, seeded):
    """N17-obs281 (design simplification C1): `_refresher` reads ONE ledger stamp per check —
    the content revision. `stamp()` is the max over a subset of the rows `content_stamp()`
    covers and the journal is append-only, so an equal revision implies an equal version; the
    former second `SELECT max(id)` (the version compare) never changed the outcome."""
    import sqlalchemy as sa

    from app.core.db import engine

    statements: list[str] = []

    def count(conn, cursor, statement, *args):
        if "model_catalog_changes" in statement:
            statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", count)
    try:
        assert model_registry._refresher() is None  # idle: nothing changed
    finally:
        sa.event.remove(engine, "before_cursor_execute", count)
    assert len(statements) == 1, statements
    # a personal provider moves the revision only — the refresher still reloads it
    model_catalog.publish(model_catalog.current())
    stale = model_catalog.current()
    row = model_registry.build_provider(db, {"name": "mine", "kind": "openai_compatible",
                                             "label": "Mine", "base_url": ACME_URL,
                                             "scope": "personal"}, "ada")
    model_registry.insert_provider(db, row, "ada")
    model_catalog.publish(stale)  # a second process that has not seen the write
    assert model_registry.stamp(db) == stale.version  # the version did not move …
    reloaded = model_registry._refresher()
    assert reloaded is not None and "mine" in reloaded.providers  # … the revision did


def test_ttl_stamp_check_runs_at_most_every_ttl_and_is_fail_soft(monkeypatch):
    calls: list[int] = []

    def refresher():
        calls.append(1)
        return None

    model_catalog.set_refresher(refresher)
    model_catalog.current()
    assert calls == []  # the stamp was just set by set_refresher
    monkeypatch.setattr(model_catalog, "REFRESH_TTL_S", 0.0)
    model_catalog.current()
    model_catalog.current()
    assert len(calls) == 2
    monkeypatch.setattr(model_catalog, "REFRESH_TTL_S", 30.0)
    model_catalog.current()
    assert len(calls) == 2
    assert model_catalog.force_refresh() is model_catalog.current() and len(calls) == 3

    def boom():
        raise RuntimeError("ledger locked")

    model_catalog.set_refresher(boom)
    assert model_catalog.force_refresh() is model_catalog.BUILTIN_CATALOG  # kept, no raise
    model_catalog.reset()
    model_catalog.current()  # no refresher installed → plain read


def test_view_functions_answer_the_tuple_and_dicts_the_removed_constants_did():
    """C5 (M1-e): `ALLOWED_MODEL_IDS` / `PROVIDERS` / `MODEL_CATALOG` / `MODEL_CAPABILITIES` /
    `MODEL_DEFAULT_MAX_TOKENS` no longer exist as module names; the function views answer
    the same shapes from the snapshot, with stable identity per snapshot."""
    model_catalog.reset()
    for gone in ("ALLOWED_MODEL_IDS", "PROVIDERS", "MODEL_CATALOG", "MODEL_CAPABILITIES",
                 "MODEL_DEFAULT_MAX_TOKENS"):
        assert not hasattr(schema, gone), gone
    ids = schema.allowed_model_ids()
    assert isinstance(ids, tuple)
    assert ids == ("global.zai.glm-5.3", "global.moonshotai.kimi-k3")  # R13: Bedrock only
    assert list(ids) == list(model_catalog.BUILTIN_CATALOG.allowed_ids())
    assert "global.zai.glm-5.3" in ids and "nope" not in ids
    providers = schema.providers()
    assert set(providers) == {"bedrock"} and len(providers) == 1
    assert providers["bedrock"] is schema.providers()["bedrock"]  # stable identity per snapshot
    assert schema.provider_row("bedrock") is providers["bedrock"]
    with pytest.raises(KeyError):
        schema.provider_row("acme")
    assert set(schema.model_catalog_table()) == set(ids)
    assert set(schema.model_capabilities()) == set(ids)
    assert set(schema.model_default_max_tokens()) == set(ids)
    assert schema.model_provider("global.zai.glm-5.3") is providers["bedrock"]
    assert schema.model_provider("custom.byoc") is providers["bedrock"]  # outside the catalog
    # a variant snapshot is PUBLISHED (what the overlay monkeypatching used to do) and every
    # view, guard and prose renderer follows it; `extended` leaves the source untouched
    import dataclasses

    fake = "global.example.new-model-9"
    glm = model_catalog.BUILTIN_CATALOG.models["global.zai.glm-5.3"]
    variant = model_catalog.extended(
        model_catalog.BUILTIN_CATALOG,
        models={fake: dataclasses.replace(glm, model_id=fake, label=fake, api_model_name=fake,
                                          default=False, vision=True)})
    assert fake not in model_catalog.BUILTIN_CATALOG.models and variant.version == 0
    model_catalog.publish(variant)
    assert schema.assert_model_allowed(fake) == fake
    assert schema.allowed_model_ids()[-1] == fake and fake in schema.model_catalog_table()
    assert schema.supports_native_images(fake) is True
    assert fake in {e["model_id"] for e in schema.model_catalog_entries()}
    assert any(f"`{fake}`" in line for line in schema.model_catalog_lines())
    model_catalog.reset()
    assert fake not in schema.allowed_model_ids()


def test_views_equal_the_published_snapshot(db, seeded):
    acme = _acme(db)
    _acme_model(db, acme)
    snapshot = model_catalog.current()
    assert schema.providers() is snapshot.legacy_providers()
    assert schema.model_catalog_table() is snapshot.legacy_models()
    assert schema.model_capabilities() is snapshot.legacy_capabilities()
    assert schema.model_default_max_tokens() is snapshot.legacy_default_max_tokens()
    assert schema.allowed_model_ids() == snapshot.allowed_ids()
    assert schema.providers() == snapshot.legacy_providers()
    assert schema.catalog_version() == snapshot.version
    assert schema.model_row("acme.acme-org/Foo-7B") is snapshot.models["acme.acme-org/Foo-7B"]
    assert schema.model_row("nope") is None and schema.model_descriptor("nope") is None


# ── no secret at rest, in the journal or in any dict ─────────────────────────────────
@pytest.mark.siliconflow(registry=True)
def test_registry_never_stores_or_returns_a_key(db, seeded, monkeypatch):
    leak = "sk-live-LEAKED-0123456789abcdef"
    monkeypatch.setenv("LAUNCHPAD_ACME_API_KEY", leak)
    monkeypatch.setenv(SF_ENV, leak)
    acme = _acme(db)
    _acme_model(db, acme)
    model_registry.set_workspace_flags(db, "default", [SF], "op")
    payload = json.dumps(model_registry.registry_dict(db))
    journal = json.dumps([model_registry.change_dict(c) for c in model_registry.list_changes(db)])
    assert leak not in payload and leak not in journal
    assert "api_key\"" not in payload.replace("api_key_env", "") and "secret" not in payload
    # configured-ness is reported, the value never is
    page = model_registry.registry_dict(db)
    acme_out = next(p for p in page["providers"] if p["name"] == "acme")
    assert acme_out["credential_provider"] == "launchpad-acme"
    assert acme_out["credential_ssm_parameter"] == "launchpad-acme-api-key"
    assert acme_out["credential_source"] == "ssm" and acme_out["configured"] is True
    assert acme_out["credential_env"] == "LAUNCHPAD_ACME_API_KEY"
    assert acme_out["key_version"] is None and acme_out["key_modified_at"] is None
    for table in (ModelProvider, Model):
        for row in db.query(table):
            assert leak not in json.dumps({c.name: getattr(row, c.name)
                                           for c in table.__table__.columns}, default=str)


@pytest.mark.siliconflow(registry=True)
def test_no_seed_time_override_exists_and_a_failed_probe_is_warned(db, seeded, monkeypatch):
    """C5 (M1-e): `Settings.siliconflow_models` / LAUNCHPAD_SILICONFLOW_MODELS were removed;
    the environment variable is inert and no `*_ignored` warning exists. R13 (N13-obs7): the
    seed carries no SiliconFlow literal either — the rows here are the administrator's."""
    from app.core.config import Settings

    assert "siliconflow_models" not in Settings.model_fields
    assert not hasattr(model_registry, "override_ignored")
    assert not hasattr(model_catalog, "DEFAULT_SILICONFLOW_MODELS")
    assert model_registry.warnings(db) == []
    monkeypatch.setenv("LAUNCHPAD_SILICONFLOW_MODELS", "Qwen/Qwen3-8B")
    assert model_registry.warnings(db) == []
    model_registry.ensure_seeded(db)
    assert "siliconflow.Qwen/Qwen3-8B" not in schema.allowed_model_ids()
    assert SF in schema.allowed_model_ids()
    seed = [c for c in model_registry.list_changes(db) if c.operation == "seed"]
    assert len(seed) == 1 and "siliconflow_seed" not in seed[0].after
    # a failed provider probe is surfaced as a warning
    sf = model_registry.get_provider(db, "siliconflow")
    sf.probe = {"state": "failed", "error": "401 auth_failed", "checked_at": "t"}
    db.commit()
    assert [w["code"] for w in model_registry.warnings(db)] == ["probe_failed"]


def test_probe_and_credential_slots_answer_501_until_registered():
    for what, code in (("probe", "model_providers.probe_unavailable"),
                       ("credential", "model_providers.credential_unavailable")):
        with pytest.raises(AppError) as exc:
            model_registry.require_probe_hooks(what)
        assert exc.value.code == code and exc.value.status_code == 501
    assert model_registry.probe_hooks() is None
    marker = object()
    model_registry.set_probe_hooks(marker)
    assert model_registry.require_probe_hooks("probe") is marker


def test_embedding_guard_slot_is_consulted_on_default_and_disable(db, seeded):
    calls: list[str] = []

    def guard(_db, row):
        calls.append(row.model_id)
        raise AppError("embedding.reindex_required", "indices hold vectors",
                       {"indices": [{"name": "launchpad-memory", "docs": 3}]}, status_code=409)

    model_registry.set_embedding_guard(guard)
    bedrock = model_registry.get_provider(db, "bedrock")
    titan = model_registry.get_model(db, bedrock, "amazon.titan-embed-text-v2:0")
    acme = _acme(db)
    other = _acme_model(db, acme, name="acme-org/Emb", kind="embedding",
                        overrides={"embedding_dimension": 1024})
    with pytest.raises(AppError) as exc:
        model_registry.update_model(db, acme, other, {"is_default": True}, "op")
    assert exc.value.code == "embedding.reindex_required" and calls == [other.model_id]
    with pytest.raises(AppError) as exc:  # the active row is the default: refused as such
        model_registry.update_model(db, bedrock, titan, {"enabled": False}, "op")
    assert exc.value.code == "model.is_default"
    assert model_catalog.current().embedding_model().model_id == titan.model_id


def test_migration_restores_the_registry_indexes(tmp_path):
    import sqlalchemy as sa

    from app.core import db as db_module
    from app.core.db import Base

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    db_module.import_model_modules()
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP INDEX uq_workspace_model_settings_workspace_model"))
        conn.execute(sa.text("DROP INDEX ix_models_kind_enabled"))
    db_module._migrate(engine)
    db_module._migrate(engine)  # idempotent
    names = {i["name"]: i for i in sa.inspect(engine).get_indexes("workspace_model_settings")}
    assert names["uq_workspace_model_settings_workspace_model"]["unique"]
    assert "ix_workspace_model_settings_workspace_id" in names
    assert "ix_models_kind_enabled" in {i["name"] for i in sa.inspect(engine).get_indexes("models")}
    assert "workspace_model_settings" in db_module.WORKSPACE_SCOPED_TABLES
    assert "app.models.model_registry" in db_module.MODEL_MODULES
    db_module.assert_no_schema_drift(engine)
