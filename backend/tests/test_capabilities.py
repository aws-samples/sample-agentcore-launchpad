"""`GET /api/capabilities` — which console modules this deployment exposes."""

import pytest


def test_capabilities_lists_the_kept_features(client):
    body = client.get("/api/capabilities").json()
    assert body["features"] == [
        "agents", "chat", "registry", "memory", "knowledge_bases",
        "identity", "gateway", "observability", "workspaces", "users", "videos",
        "evaluation", "optimization", "skill_lab", "policy",
    ]
    # CN additive (N12-obs41): the evaluator forms hide the Lambda "Code" definition
    # while the workspace has not opted in
    assert body["evaluation"] == {"code_evaluators_enabled": False}
    # D20 P1-d: the additive `policy` block rides along while the engine is registered;
    # D21 additive: `catalog_version` (the registry journal id) and the active `embedding`
    # round 16 additive: `methods` — the per-method creation-time option domain
    # CN additive (N17-obs258): `tools.browser_signing` — Web Bot Auth is unavailable in aws-cn
    # N17-obs463: `display_timezone` — the zone the API's times are in (console formats in it)
    assert set(body) == {"features", "evaluation", "models", "model_providers", "policy",
                         "catalog_version", "embedding", "methods", "tools", "display_timezone"}
    assert body["tools"] == {"browser_signing": False}
    assert body["policy"] == {"engines": 0, "attached_gateways": 0}
    assert isinstance(body["catalog_version"], int) and body["catalog_version"] >= 1
    assert body["embedding"] == {"model_id": "amazon.titan-embed-text-v2:0", "dimension": 1024,
                                 "provider": "bedrock"}


def test_capabilities_hide_policy_until_the_engine_is_registered(client):
    """Plan §2.6 / G2: `policy` is a capability only with a registered Cedar engine — the
    feature flag alone (routers mounted, answering 503) must not light the Policy tabs."""
    from app.services import policy_backend

    policy_backend.set_backend(None)
    body = client.get("/api/capabilities").json()
    assert "policy" not in body["features"] and "policy" not in body
    assert body["features"][-1] == "skill_lab"


def test_capabilities_policy_block_follows_the_workspace_settings(client):
    """The block is `policy_engines.capability_summary` (Global model): how many policy engines
    the workspace holds and how many gateways are attached to them."""
    from app.core.db import SessionLocal
    from app.models.policy import PolicyEngineAttachment
    from app.services import policy_engines
    from tests.policy_fakes import BASELINE

    db = SessionLocal()
    try:
        engine = policy_engines.create_engine(db, "default", name="hr_engine", operator="t")
        policy_engines.create_engine(db, "default", name="spare", operator="t")
        from app.services import policy_store

        policy_store.create_policy(db, engine, name="baseline", statement=BASELINE, operator="t")
        db.add(PolicyEngineAttachment(gateway_id="gw-one", engine_id=engine.id, mode="ENFORCE"))
        db.commit()
    finally:
        db.close()
    body = client.get("/api/capabilities").json()
    assert body["policy"] == {"engines": 2, "attached_gateways": 1}


@pytest.mark.siliconflow
def test_capabilities_exposes_the_model_catalog_with_its_provider(client):
    """CN additive (N-R6): the console pickers merge these rows (SiliconFlow rows are
    admin-registered — R13 — so the frontend cannot hard-code them)."""
    from app.schemas.agent import allowed_model_ids, providers

    body = client.get("/api/capabilities").json()
    rows = {row["model_id"]: row for row in body["models"]}
    assert list(rows) == list(allowed_model_ids())
    glm = rows["global.zai.glm-5.3"]
    assert glm["provider"] == "bedrock" and glm["provider_kind"] == "bedrock"
    assert glm["default"] is True and glm["tools"] is True
    sf = rows["siliconflow.zai-org/GLM-5.3"]
    assert sf["provider"] == "siliconflow" and sf["provider_kind"] == "openai_compatible"
    assert sf["provider_label"] == "SiliconFlow" and sf["api_model_name"] == "zai-org/GLM-5.3"
    assert sf["tools"] is True and sf["streaming"] is True and sf["default"] is False
    for row in rows.values():
        assert set(row) == {"model_id", "label", "provider", "provider_kind", "provider_label",
                            "api_model_name", "tools", "streaming", "vision", "default",
                            "available",
                            # D21 additive: the registry dimension
                            "kind", "source", "enabled"}
        assert row["kind"] == "chat" and row["enabled"] is True
        assert row["source"] == ("builtin" if row["provider"] == "bedrock" else "manual")
        assert "base_url" not in row and "api_key" not in str(row).lower()
    from app.schemas import model_catalog

    snapshot = model_catalog.current()
    assert body["model_providers"] == {
        name: {"kind": r["kind"], "label": r["label"],
               "credential_provider": r["credential_provider"],
               # CN additive (N-R6-obs1): configured flag + the parameter to create
               "configured": name == "bedrock",
               "ssm_parameter": r["ssm_parameter"], "api_key_env": r["api_key_env"],
               # D21 additive: registry row id, enable flag, last probe state
               "id": snapshot.providers[name].id, "enabled": True, "probe_state": "never"}
        for name, r in providers().items()
    }
    assert all(p["id"].startswith("mp_") for p in body["model_providers"].values())


def test_capabilities_reports_the_code_evaluator_opt_in(client, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "eval_code_evaluators_enabled", True)
    assert client.get("/api/capabilities").json()["evaluation"] == {
        "code_evaluators_enabled": True}


def test_capabilities_honours_settings(client, monkeypatch):
    from app.core import config

    monkeypatch.setenv("LAUNCHPAD_FEATURES", '["agents","chat"]')
    config.get_settings.cache_clear()
    try:
        assert client.get("/api/capabilities").json()["features"] == ["agents", "chat"]
    finally:
        config.get_settings.cache_clear()


def test_unknown_feature_is_rejected():
    from app.core.config import Settings

    with pytest.raises(ValueError):
        Settings(features=["agents", "holograms"])


# ─── N-R6-obs1: models of an unconfigured provider are listed but marked unavailable ────
SF = "siliconflow.zai-org/GLM-5.3"
SF_ENV = "LAUNCHPAD_SILICONFLOW_API_KEY"


def _flags(client):
    body = client.get("/api/capabilities").json()
    return ({row["model_id"]: row["available"] for row in body["models"]},
            {name: p["configured"] for name, p in body["model_providers"].items()})


@pytest.mark.siliconflow
def test_capabilities_mark_models_of_an_unconfigured_provider_unavailable(client, monkeypatch):
    """No bootstrap record and no hub key: the SiliconFlow rows stay listed (the console
    explains why they are disabled) with `available: false`; Bedrock is always configured."""
    monkeypatch.delenv(SF_ENV, raising=False)
    available, configured = _flags(client)
    assert available["global.zai.glm-5.3"] is True and available["global.moonshotai.kimi-k3"]
    assert available[SF] is False
    assert all(not ok for model_id, ok in available.items() if model_id.startswith("siliconflow."))
    assert configured == {"bedrock": True, "siliconflow": False}
    providers = client.get("/api/capabilities").json()["model_providers"]["siliconflow"]
    assert providers["ssm_parameter"] == "launchpad-siliconflow-api-key"
    assert providers["api_key_env"] == SF_ENV
    assert "k" not in providers.values() and "api_key\"" not in str(providers)


@pytest.mark.siliconflow
def test_capabilities_follow_the_hub_env_when_the_workspace_has_no_record(client, monkeypatch):
    monkeypatch.setenv(SF_ENV, "sf-test-key")
    available, configured = _flags(client)
    assert available[SF] is True and configured["siliconflow"] is True
    # the key itself never appears in the payload
    assert "sf-test-key" not in client.get("/api/capabilities").text


@pytest.mark.siliconflow
def test_capabilities_prefer_the_workspace_bootstrap_record(client, monkeypatch):
    """The bootstrap's `resources.model_credentials` record says whether the Identity
    credential provider exists — it wins over the hub env both ways."""
    from tests.conftest import set_default_resources

    monkeypatch.setenv(SF_ENV, "sf-test-key")
    set_default_resources({"model_credentials": {
        "bedrock": {"name": "launchpad-global-bedrock", "configured": True},
        "siliconflow": {"name": "launchpad-siliconflow", "configured": False}}})
    available, configured = _flags(client)
    assert available[SF] is False and configured["siliconflow"] is False
    monkeypatch.delenv(SF_ENV)
    set_default_resources({"model_credentials": {
        "siliconflow": {"name": "launchpad-siliconflow", "arn": "a", "configured": True}}})
    available, configured = _flags(client)
    assert available[SF] is True and configured["siliconflow"] is True
    assert configured["bedrock"] is True  # required → always


def test_capabilities_publish_the_method_declaration(client):
    """Round 16 (N15-obs16): `methods[<name>]` is `MethodCapabilities.as_dict()` for every
    registered method — the wire form the wizard disables options from and the frozen
    fixture the frontend vitest reads (`tests/test_method_capabilities.py`)."""
    import json
    from pathlib import Path

    body = client.get("/api/capabilities").json()
    frozen = json.loads((Path(__file__).parent / "fixtures" / "method_capabilities.json")
                        .read_text(encoding="utf-8"))
    assert body["methods"] == frozen
    assert body["methods"]["harness"]["builtin_tools"] == ["browser", "code-interpreter"]
    assert body["methods"]["zip_runtime"]["builtin_tools"] == []
    assert body["methods"]["byoc"]["byoc_kinds"] == [
        "code_zip", "container_image", "container_source",
    ]


def test_capabilities_offer_browser_signing_only_outside_china(client):
    """N17-obs258: the flag is the workspace partition's, so a non-cn workspace keeps it."""
    from app.core.db import SessionLocal
    from app.models.ledger import Workspace
    from app.routers.workspaces import WORKSPACE_HEADER

    db = SessionLocal()
    try:
        db.add(Workspace(id="caps-usw2", name="caps-usw2", account_id="444455556666",
                         region="us-west-2", bootstrap_status="ready", resources={}))
        db.commit()
    finally:
        db.close()

    cn = client.get("/api/capabilities").json()
    commercial = client.get("/api/capabilities", headers={WORKSPACE_HEADER: "caps-usw2"}).json()

    assert cn["tools"] == {"browser_signing": False}
    assert commercial["tools"] == {"browser_signing": True}
