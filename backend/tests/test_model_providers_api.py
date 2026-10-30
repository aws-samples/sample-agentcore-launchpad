"""`/api/settings/model-providers` (D21, plan §3) over HTTP: exact response shapes (the
contract M1-c codes against), admin-only, CRUD, per-workspace flags, the journal, the 501
probe / credential stubs and their hook slot, no secret echo, capabilities parity."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import create_app
from app.schemas import model_catalog
from app.schemas import model_providers as contract
from app.services import model_registry
from app.services import users as users_service

PREFIX = "/api/settings/model-providers"
SF = "siliconflow.zai-org/GLM-5.3"
ACME_URL = "https://api.siliconflow.cn/acme/v1"

PROVIDER_KEYS = {"id", "name", "kind", "label", "base_url", "model_region",
                 "credential_provider", "credential_ssm_parameter", "credential_source",
                 "credential_env", "required", "builtin", "enabled", "id_prefix",
                 "probe", "configured", "key_version", "key_modified_at", "model_count",
                 "key_last4",  # N17-obs283: flat, replaces the nested `credential` mirror
                 "created_by", "created_at", "updated_by", "updated_at",
                 "created_by_name", "scope", "owned"}  # N17-obs203 owner-bound additive keys
WORKSPACE_FLAGS_OUT_KEYS = {"id", "name", "disabled_models", "workspace_id"}
PROVIDER_PROBE_KEYS = {"state", "checked_at", "latency_ms", "models_seen", "error"}
MODEL_KEYS = {"id", "provider_id", "provider", "model_id", "api_model_name", "label", "kind",
              "enabled", "is_default", "tools", "streaming", "vision", "inference_params",
              "default_max_tokens", "max_output_tokens", "default_judge_max_tokens",
              "embedding_dimension", "embedding_max_chars", "source", "capability_sources",
              "probe", "sort_order", "created_by", "created_at", "updated_by", "updated_at"}
REGISTRY_KEYS = {"catalog_version", "providers", "models", "workspaces", "warnings"}
WORKSPACE_KEYS = {"id", "name", "disabled_models"}
CHANGE_KEYS = {"id", "operation", "actor", "target_id", "before", "after", "created_at"}
CAPABILITIES_MODEL_KEYS = {"model_id", "label", "provider", "provider_kind", "provider_label",
                           "api_model_name", "tools", "streaming", "vision", "default",
                           "available", "kind", "source", "enabled"}
CAPABILITIES_PROVIDER_KEYS = {"kind", "label", "credential_provider", "configured",
                              "ssm_parameter", "api_key_env", "id", "enabled", "probe_state"}


# R13 (N13-obs7): SiliconFlow is admin-registered on top of the Bedrock seed in every test here
pytestmark = pytest.mark.siliconflow


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.delenv("LAUNCHPAD_SILICONFLOW_API_KEY", raising=False)
    monkeypatch.delenv("LAUNCHPAD_ACME_API_KEY", raising=False)
    yield
    model_registry.set_probe_hooks(None)
    model_catalog.reset()


def _acme(client, **extra):
    body = {"name": "acme", "kind": "openai_compatible", "label": "Acme", "base_url": ACME_URL,
            **extra}
    response = client.post(PREFIX, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _model(client, provider_id, name="acme-org/Foo-7B", **extra):
    response = client.post(f"{PREFIX}/{provider_id}/models",
                           json={"api_model_name": name, "kind": "chat", "probe": False, **extra})
    assert response.status_code == 201, response.text
    return response.json()


# ── contract shapes (M1-c codes against these literally) ─────────────────────────────
def test_the_pydantic_contract_pins_every_response_key_set():
    """The frozen contract: the Out models' fields ARE the key sets asserted below, so the
    halves cannot drift without this test moving."""
    assert set(contract.ProviderOut.model_fields) == PROVIDER_KEYS
    assert not hasattr(contract, "ProviderCredentialOut")  # N17-obs283: one (flat) shape
    assert set(contract.ProviderProbeOut.model_fields) == PROVIDER_PROBE_KEYS
    assert set(contract.ModelOut.model_fields) == MODEL_KEYS
    assert set(contract.RegistryOut.model_fields) == REGISTRY_KEYS
    assert set(contract.WorkspaceOut.model_fields) == WORKSPACE_KEYS
    assert set(contract.ChangeOut.model_fields) == CHANGE_KEYS
    assert set(contract.WorkspaceFlagsOut.model_fields) == WORKSPACE_FLAGS_OUT_KEYS
    assert set(contract.CredentialOut.model_fields) == {"configured", "created", "rotated",
                                                        "workspaces_updated", "key_version",
                                                        "last4"}
    assert set(contract.ModelProbeOut.model_fields) == {"findings", "diff", "errors", "raw_hint",
                                                        "latency_ms", "applied"}
    assert set(contract.ModelProbeRecordOut.model_fields) == {"checked_at", "latency_ms",
                                                              "findings", "errors", "raw_hint"}
    assert set(contract.WarningOut.model_fields) == {"code", "message", "detail"}
    assert set(contract.InferenceParamsOut.model_fields) == {"temperature", "top_p"}
    # request bodies: every field the plan names, nothing a key could travel in
    assert set(contract.ProviderCreate.model_fields) == {
        "name", "kind", "label", "base_url", "model_region", "credential_ssm_parameter",
        "enabled", "scope", "api_key"}
    assert set(contract.ProviderUpdate.model_fields) == {
        "label", "base_url", "model_region", "credential_ssm_parameter", "enabled", "scope",
        "api_key"}
    assert set(contract.ModelCreate.model_fields) == {
        "api_model_name", "kind", "label", "probe", "overrides", "enabled"}
    assert set(contract.ModelUpdate.model_fields) == {
        "label", "enabled", "is_default", "sort_order", "tools", "streaming", "vision",
        "inference_params", "default_max_tokens", "max_output_tokens",
        "default_judge_max_tokens", "embedding_dimension", "embedding_max_chars"}
    assert set(contract.WorkspaceFlagsIn.model_fields) == {"disabled_models"}
    assert set(contract.ModelProbeIn.model_fields) == {"apply"}
    assert set(contract.CredentialRegisterIn.model_fields) == {"api_key"}
    # N17-obs203 §3: the typed key travels in exactly three WRITE-ONLY request bodies; no
    # response model has a field a key could travel in
    for name, model in vars(contract).items():
        if isinstance(model, type) and issubclass(model, contract.BaseModel):
            secretish = {f for f in model.model_fields
                         if "key" == f or f.endswith("_key") or "secret" in f}
            if name in ("ProviderCreate", "ProviderUpdate", "CredentialRegisterIn"):
                assert secretish == {"api_key"}, name
            else:
                assert not secretish, name


def test_get_registry_returns_the_seeded_catalog_in_the_contract_shape(client):
    """The Bedrock seed + the administrator's SiliconFlow provider (R13, N13-obs7: SiliconFlow
    is no longer a built-in row — `builtin: false`, listed after the seed)."""
    body = client.get(PREFIX).json()
    assert set(body) == REGISTRY_KEYS and body["warnings"] == []
    assert body["catalog_version"] == model_catalog.current().version >= 1
    assert [p["name"] for p in body["providers"]] == ["bedrock", "siliconflow"]
    for provider in body["providers"]:
        assert set(provider) == PROVIDER_KEYS
        assert provider["key_version"] is None and provider["key_modified_at"] is None
        assert provider["key_last4"] is None  # no console-typed key on the seed rows
        assert set(provider["probe"]) == PROVIDER_PROBE_KEYS and provider["probe"]["state"] == (
            "never")
        assert provider["enabled"] and provider["id"].startswith("mp_")
        assert provider["builtin"] is (provider["name"] == "bedrock")
    bedrock, sf = body["providers"]
    assert bedrock["required"] and bedrock["model_count"] == 3 and bedrock["model_region"] == (
        "us-west-2")
    assert (bedrock["credential_provider"], bedrock["credential_ssm_parameter"],
            bedrock["credential_source"], bedrock["credential_env"], bedrock["configured"]) == (
        "launchpad-global-bedrock", "launchpad-global-bedrock-api-key", "host_env",
        "LAUNCHPAD_GLOBAL_BEDROCK_API_KEY", True)
    assert (sf["credential_provider"], sf["credential_ssm_parameter"], sf["credential_source"],
            sf["credential_env"], sf["configured"]) == (
        "launchpad-siliconflow", "launchpad-siliconflow-api-key", "ssm",
        "LAUNCHPAD_SILICONFLOW_API_KEY", False)
    assert sf["model_count"] == 4 and sf["base_url"] == "https://api.siliconflow.cn/v1"
    assert [m["model_id"] for m in body["models"]] == [
        "global.zai.glm-5.3", "global.moonshotai.kimi-k3", "amazon.titan-embed-text-v2:0", SF,
        "siliconflow.deepseek-ai/DeepSeek-V4-Pro", "siliconflow.Qwen/Qwen3.8-27B",
        "siliconflow.Pro/moonshotai/Kimi-K2.6"]
    for model in body["models"]:
        assert set(model) == MODEL_KEYS and set(model["inference_params"]) == {"temperature",
                                                                               "top_p"}
        source = "builtin" if model["provider"] == "bedrock" else "manual"  # R13
        assert model["source"] == source and model["probe"] is None
        assert set(model["capability_sources"].values()) == {source}
    glm = body["models"][0]
    assert glm["is_default"] and glm["kind"] == "chat" and glm["default_max_tokens"] == 16384
    assert glm["inference_params"] == {"temperature": True, "top_p": True}
    kimi = body["models"][1]
    assert kimi["inference_params"]["temperature"] is False and kimi["vision"] is True
    titan = body["models"][2]
    assert titan["kind"] == "embedding" and titan["is_default"] and titan[
        "embedding_dimension"] == 1024
    assert body["workspaces"] == [{"id": "default", "name": "Default", "disabled_models": []}]


# ── authorization ────────────────────────────────────────────────────────────────────
def test_member_reach_anonymous_401_and_the_admin_only_remainder(monkeypatch):
    """N17-obs203 (user decision 2026-10-21): the registry is no longer administrators-only.
    `test_route_policy.py` pins the table; this pins the HTTP shape for a member: anonymous
    401 everywhere; the journal and the flag matrix 403 `auth.forbidden`; the built-in Bedrock
    row (NULL creator = an administrator's) is READABLE but every write on it answers 403
    `model_provider.forbidden`; the D21-obs8 body check never runs before the owner gate.
    The owner paths themselves are `tests/test_model_provider_ownership.py`."""
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", "operator")
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", "s3cret-pass")
    get_settings.cache_clear()
    try:
        app = create_app()
        with TestClient(app, client=("127.0.0.1", 4321)) as anon:
            assert anon.get(PREFIX).status_code == 401
            creds = {"username": "qa-user", "email": "qa@acme-corp.com",
                     "password": "sufficient-pass"}
            assert anon.post("/api/auth/register", json=creds).status_code == 201
            db = SessionLocal()
            try:
                user = users_service.find_by_username(db, "qa-user")
                user.status = users_service.STATUS_ACTIVE
                user.expires_at = datetime.now(UTC) + timedelta(days=7)
                db.commit()
            finally:
                db.close()
            assert anon.post("/api/auth/login", json={"username": "qa-user",
                                                      "password": "sufficient-pass"}
                             ).status_code == 200
            # admin-only remainder: the journal and the per-workspace flag matrix
            for method, path in (("GET", f"{PREFIX}/changes"),
                                 ("PUT", f"{PREFIX}/workspaces/default/models")):
                response = anon.request(method, path, json={})
                assert response.status_code == 403, (method, path, response.text)
                assert response.json()["code"] == "auth.forbidden"
            # reads of the shared (workspace-scope) rows are every member's
            page = anon.get(PREFIX)
            assert page.status_code == 200 and page.json()["workspaces"] == []
            assert [p["name"] for p in page.json()["providers"]] == ["bedrock"]
            assert anon.get(f"{PREFIX}/bedrock").status_code == 200
            # writes on a row the member did not create: 403 model_provider.forbidden
            for method, path in (("PUT", f"{PREFIX}/bedrock"), ("DELETE", f"{PREFIX}/bedrock"),
                                 ("POST", f"{PREFIX}/bedrock/probe"),
                                 ("POST", f"{PREFIX}/bedrock/credential"),
                                 ("POST", f"{PREFIX}/bedrock/models"),
                                 ("PUT", f"{PREFIX}/bedrock/models/x"),
                                 ("DELETE", f"{PREFIX}/bedrock/models/x"),
                                 ("POST", f"{PREFIX}/bedrock/models/x/probe")):
                response = anon.request(method, path, json={})
                assert response.status_code == 403, (method, path, response.text)
                assert response.json()["code"] == "model_provider.forbidden"
                assert response.json()["detail"]["provider_id"]
            # the D21-obs8 body check runs AFTER the owner gate: a member never learns the schema
            member_put = anon.put(f"{PREFIX}/bedrock", json={"name": "x", "enabled": False})
            assert member_put.status_code == 403
            assert member_put.json()["code"] == "model_provider.forbidden"
            # sharing is an administrator's: a member cannot create a workspace-scope provider
            shared = anon.post(PREFIX, json={"name": "mine", "kind": "openai_compatible",
                                             "label": "Mine", "base_url": ACME_URL,
                                             "scope": "workspace"})
            assert shared.status_code == 403
            assert shared.json()["code"] == "model_provider.forbidden"
            assert "models.manage" not in users_service.AGENT_PERMISSIONS
    finally:
        get_settings.cache_clear()


# ── providers over HTTP ──────────────────────────────────────────────────────────────
def test_provider_crud_over_http(client, monkeypatch):
    version = client.get("/api/capabilities").json()["catalog_version"]
    created = _acme(client)
    assert set(created) == PROVIDER_KEYS and created["name"] == "acme"
    assert (created["credential_provider"], created["credential_ssm_parameter"],
            created["credential_source"], created["credential_env"], created["configured"]) == (
        "launchpad-acme", "launchpad-acme-api-key", "ssm", "LAUNCHPAD_ACME_API_KEY", False)
    assert created["id_prefix"] == "acme." and created["model_count"] == 0
    # N17-obs203: `created_by` is the ownership key (users.id — None for the row-less config
    # admin), `created_by_name` the display name; an administrator's row is workspace-scope
    assert created["created_by"] is None and created["created_at"]
    assert created["created_by_name"] == get_settings().auth_username
    assert created["scope"] == "workspace" and created["owned"] is False
    assert client.get(f"{PREFIX}/{created['id']}").json() == created
    assert client.get(f"{PREFIX}/acme").json()["id"] == created["id"]  # by name too
    assert client.get("/api/capabilities").json()["catalog_version"] == version + 1
    providers = client.get("/api/capabilities").json()["model_providers"]
    assert providers["acme"]["id"] == created["id"] and providers["acme"]["configured"] is False
    assert set(providers["acme"]) == CAPABILITIES_PROVIDER_KEYS
    updated = client.put(f"{PREFIX}/{created['id']}", json={"label": "Acme Cloud",
                                                            "enabled": False})
    assert updated.status_code == 200 and updated.json()["label"] == "Acme Cloud"
    assert updated.json()["enabled"] is False
    assert client.get("/api/capabilities").json()["model_providers"]["acme"]["enabled"] is False
    # N17-obs203: any public https host is accepted; the operator LOCK mode (a non-empty
    # allowlist) and the denylist still answer host_not_allowed naming their setting
    assert client.put(f"{PREFIX}/{created['id']}", json={"base_url": "https://api.example.com"}
                      ).json()["base_url"] == "https://api.example.com"
    monkeypatch.setattr(get_settings(), "model_provider_allowed_hosts", ["*.siliconflow.cn"])
    locked = client.put(f"{PREFIX}/{created['id']}", json={"base_url": "https://api.ppinfra.com"})
    assert locked.status_code == 422 and locked.json()["code"] == "model_provider.host_not_allowed"
    assert locked.json()["detail"]["setting"] == "model_provider_allowed_hosts"
    monkeypatch.setattr(get_settings(), "model_provider_allowed_hosts", [])
    monkeypatch.setattr(get_settings(), "model_provider_denied_hosts", ["api.ppinfra.com"])
    denied = client.put(f"{PREFIX}/{created['id']}", json={"base_url": "https://api.ppinfra.com"})
    assert denied.status_code == 422 and denied.json()["code"] == "model_provider.host_not_allowed"
    assert denied.json()["detail"]["setting"] == "model_provider_denied_hosts"
    monkeypatch.setattr(get_settings(), "model_provider_denied_hosts", [])
    # immutable / unknown keys: a named 422 instead of the generic one (D21-obs8)
    rejected = client.put(f"{PREFIX}/{created['id']}", json={"name": "other"})
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "model_provider.invalid_update"
    assert rejected.json()["detail"]["fields"] == ["name"]
    assert rejected.json()["detail"]["immutable"] == ["name"]
    assert "name" in rejected.json()["message"]
    assert client.post(PREFIX, json={"name": "acme", "kind": "openai_compatible",
                                     "label": "A", "base_url": ACME_URL}).status_code == 409
    assert client.post(PREFIX, json={"name": "xbed", "kind": "bedrock", "label": "X"}
                       ).status_code == 201
    assert client.post(PREFIX, json={"name": "bad", "kind": "openai_compatible", "label": "B",
                                     "base_url": "https://169.254.169.254/v1"}
                       ).json()["code"] == "model_provider.invalid_base_url"
    assert client.delete(f"{PREFIX}/bedrock").json()["code"] == "model_provider.builtin"
    assert client.delete(f"{PREFIX}/{created['id']}").status_code == 204
    assert client.get(f"{PREFIX}/{created['id']}").status_code == 404
    assert client.get(f"{PREFIX}/mp_000000000000").json()["code"] == "model_provider.not_found"
    assert "acme" not in client.get("/api/capabilities").json()["model_providers"]


# ── models over HTTP ─────────────────────────────────────────────────────────────────
def test_model_crud_over_http_and_the_pickers_follow(client):
    acme = _acme(client)
    probing = client.post(f"{PREFIX}/{acme['id']}/models", json={"api_model_name": "acme-org/X"})
    # probe-on-add is the default and M1-b's hooks are live: without a registered credential
    # the probe cannot reach the model (no request leaves the host) and the insert is refused
    # with the findings — `probe: false` is the manual row (plan §7.4)
    assert probing.status_code == 422, probing.text
    assert probing.json()["code"] == "model.probe_failed"
    assert probing.json()["detail"]["findings"] == {}
    assert probing.json()["detail"]["errors"]["exists"].startswith("credential_unavailable")
    created = _model(client, acme["id"])
    assert set(created) == MODEL_KEYS and created["model_id"] == "acme.acme-org/Foo-7B"
    assert created["source"] == "manual" and created["label"] == "Foo-7B (Acme)"
    assert created["provider"] == "acme" and created["provider_id"] == acme["id"]
    assert client.get(f"{PREFIX}/{acme['id']}").json()["model_count"] == 1
    rows = {m["model_id"]: m for m in client.get("/api/capabilities").json()["models"]}
    assert set(rows[created["model_id"]]) == CAPABILITIES_MODEL_KEYS
    assert rows[created["model_id"]]["source"] == "manual" and rows[created["model_id"]][
        "available"] is False
    # update by row id, then by Launchpad id (with its slash)
    by_id = client.put(f"{PREFIX}/{acme['id']}/models/{created['id']}",
                       json={"tools": False, "inference_params": {"temperature": False}})
    assert by_id.status_code == 200 and by_id.json()["tools"] is False
    assert by_id.json()["inference_params"] == {"temperature": False, "top_p": True}
    assert by_id.json()["capability_sources"]["tools"] == "manual"
    by_model_id = client.put(f"{PREFIX}/{acme['id']}/models/{created['model_id']}",
                             json={"label": "Foo", "sort_order": 3})
    assert by_model_id.status_code == 200 and by_model_id.json()["label"] == "Foo"
    assert by_model_id.json()["sort_order"] == 3
    assert client.put(f"{PREFIX}/{acme['id']}/models/{created['id']}", json={"bogus": 1}
                      ).json()["code"] == "validation.invalid_request"
    assert client.put(f"{PREFIX}/{acme['id']}/models/{created['id']}",
                      json={"default_judge_max_tokens": 10}).status_code == 422
    assert client.post(f"{PREFIX}/{acme['id']}/models",
                       json={"api_model_name": "acme-org/Foo-7B", "probe": False}
                       ).json()["code"] == "model.exists"
    assert client.put(f"{PREFIX}/bedrock/models/{created['id']}", json={"label": "x"}
                      ).json()["code"] == "model.not_found"  # wrong provider → 404
    assert client.delete(f"{PREFIX}/bedrock/models/global.moonshotai.kimi-k3"
                         ).json()["code"] == "model.builtin"
    assert client.delete(f"{PREFIX}/bedrock/models/global.zai.glm-5.3"
                         ).json()["code"] == "model.builtin"
    disabled = client.put(f"{PREFIX}/siliconflow/models/{SF}", json={"enabled": False})
    assert disabled.status_code == 200 and disabled.json()["enabled"] is False
    assert SF not in {m["model_id"] for m in client.get("/api/capabilities").json()["models"]}
    assert client.delete(f"{PREFIX}/{acme['id']}/models/{created['model_id']}"
                         ).status_code == 204
    assert client.get(f"{PREFIX}/{acme['id']}").json()["model_count"] == 0
    assert created["model_id"] not in {
        m["model_id"] for m in client.get("/api/capabilities").json()["models"]}


def test_probe_and_credential_routes_answer_501_until_m1b_registers(client):
    # create_app() registers M1-b's hooks; this test pins the contract of the EMPTY slot
    # (a build without them — the console's `isProbeUnavailable` codes against it)
    model_registry.set_probe_hooks(None)
    for path, code in ((f"{PREFIX}/siliconflow/probe", "model_providers.probe_unavailable"),
                       (f"{PREFIX}/siliconflow/credential",
                        "model_providers.credential_unavailable"),
                       (f"{PREFIX}/siliconflow/models/{SF}/probe",
                        "model_providers.probe_unavailable")):
        response = client.post(path, json={})
        assert response.status_code == 501, (path, response.text)
        assert response.json()["code"] == code
    assert client.post(f"{PREFIX}/mp_000000000000/probe").status_code == 404


class _FakeHooks:
    """What M1-b registers: secret-free dicts in the contract shapes."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.missing = False

    def probe_provider(self, db, provider, actor):
        self.calls.append(("provider", provider.name, actor))
        provider.probe = {"state": "ok", "checked_at": "2026-10-14T10:00:00+00:00",
                          "latency_ms": 88, "models_seen": ["acme-org/Foo-7B"], "error": None}
        db.commit()
        model_registry.refresh(db)
        return dict(provider.probe)

    def register_credential(self, db, provider, actor):
        self.calls.append(("credential", provider.name, actor))
        return {"configured": True, "created": True, "rotated": False, "workspaces_updated": 1,
                "key_version": 3}

    def forget_credential_record(self, db, provider):  # N17-obs229
        self.calls.append(("forget_record", provider.name))
        return 0

    def clear_api_key(self, db, provider, actor):  # N17-obs228
        self.calls.append(("clear_key", provider.name, actor))
        return {"ssm_parameter": provider.credential_ssm_parameter, "ssm_deleted": False,
                "identity_deleted": False, "error": None}

    def probe_model(self, db, provider, api_model_name, kind, model, *, apply, actor):
        self.calls.append(("model", api_model_name, model.model_id if model else None, apply))
        if self.missing:
            return {"findings": {"exists": False}, "errors": {"exists": "404 model not found"},
                    "record": None}
        findings = {"exists": True, "tools": False, "temperature": False, "streaming": True}
        if model is None:
            return {"findings": findings,
                    "record": {"checked_at": "t", "latency_ms": 5, "findings": findings,
                               "errors": {}, "raw_hint": {"context_length": 128000}}}
        return {"findings": findings,
                "diff": {"tools": {"stored": model.tools, "probed": False,
                                   "source": model.capability_sources.get("tools")}},
                "applied": bool(apply), "latency_ms": 7}


def test_registered_hooks_drive_probe_credential_and_probe_on_add(client):
    hooks = _FakeHooks()
    model_registry.set_probe_hooks(hooks)
    acme = _acme(client)
    probe = client.post(f"{PREFIX}/{acme['id']}/probe")
    assert probe.status_code == 200 and set(probe.json()) == PROVIDER_PROBE_KEYS
    assert probe.json()["models_seen"] == ["acme-org/Foo-7B"]
    assert client.get(f"{PREFIX}/{acme['id']}").json()["probe"]["state"] == "ok"
    assert client.get("/api/capabilities").json()["model_providers"]["acme"]["probe_state"] == (
        "ok")
    cred = client.post(f"{PREFIX}/{acme['id']}/credential", json={})
    assert cred.status_code == 200 and cred.json() == {
        "configured": True, "created": True, "rotated": False, "workspaces_updated": 1,
        "key_version": 3, "last4": None}
    created = client.post(f"{PREFIX}/{acme['id']}/models",
                          json={"api_model_name": "acme-org/Foo-7B",
                                "overrides": {"vision": True}})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["source"] == "manual"  # an override was applied after the probe
    assert body["tools"] is False and body["inference_params"]["temperature"] is False
    assert body["capability_sources"]["tools"] == "probed"
    assert body["capability_sources"]["temperature"] == "probed"
    assert body["capability_sources"]["vision"] == "manual" and body["vision"] is True
    assert body["probe"]["raw_hint"] == {"context_length": 128000}
    pure = client.post(f"{PREFIX}/{acme['id']}/models", json={"api_model_name": "acme-org/P"})
    assert pure.status_code == 201 and pure.json()["source"] == "probed"
    # rev-r8 M3: a probe that cannot find the model refuses the insert with its findings
    hooks.missing = True
    failed = client.post(f"{PREFIX}/{acme['id']}/models", json={"api_model_name": "acme-org/Gone"})
    assert failed.status_code == 422 and failed.json()["code"] == "model.probe_failed"
    assert failed.json()["detail"] == {"findings": {"exists": False},
                                       "errors": {"exists": "404 model not found"}}
    hooks.missing = False
    assert client.get(f"{PREFIX}/{acme['id']}").json()["model_count"] == 2
    reprobe = client.post(f"{PREFIX}/{acme['id']}/models/{body['id']}/probe", json={"apply": True})
    assert reprobe.status_code == 200 and set(reprobe.json()) == {
        "findings", "diff", "errors", "raw_hint", "latency_ms", "applied"}
    assert reprobe.json()["applied"] is True and reprobe.json()["latency_ms"] == 7
    assert reprobe.json()["diff"]["tools"] == {
        "stored": False, "probed": False, "source": "probed"}
    assert hooks.calls[-1] == ("model", "acme-org/Foo-7B", "acme.acme-org/Foo-7B", True)
    assert hooks.calls[0] == ("provider", "acme", get_settings().auth_username)


# ── per-workspace flags ──────────────────────────────────────────────────────────────
def test_workspace_flags_over_http_scope_the_capabilities(client):
    before = {m["model_id"] for m in client.get("/api/capabilities").json()["models"]}
    assert SF in before
    response = client.put(f"{PREFIX}/workspaces/default/models",
                          json={"disabled_models": [SF, "global.moonshotai.kimi-k3"]})
    assert response.status_code == 200, response.text
    assert response.json() == {"id": "default", "name": "Default", "workspace_id": "default",
                               "disabled_models": ["global.moonshotai.kimi-k3", SF]}
    page = client.get(PREFIX).json()
    assert page["workspaces"] == [{"id": "default", "name": "Default",
                                   "disabled_models": ["global.moonshotai.kimi-k3", SF]}]
    after = {m["model_id"] for m in client.get("/api/capabilities").json()["models"]}
    assert after == before - {SF, "global.moonshotai.kimi-k3"}
    # the rows themselves stay enabled hub-wide
    assert all(m["enabled"] for m in page["models"])
    assert client.put(f"{PREFIX}/workspaces/default/models", json={"disabled_models": ["nope"]}
                      ).json()["code"] == "model.unknown"
    assert client.put(f"{PREFIX}/workspaces/ghost/models", json={"disabled_models": []}
                      ).status_code == 404
    assert client.put(f"{PREFIX}/workspaces/default/models", json={"disabled_models": []}
                      ).json()["disabled_models"] == []
    assert {m["model_id"] for m in client.get("/api/capabilities").json()["models"]} == before


# ── journal ──────────────────────────────────────────────────────────────────────────
def test_changes_journal_is_newest_first_and_pages(client):
    acme = _acme(client)
    model = _model(client, acme["id"])
    client.put(f"{PREFIX}/{acme['id']}/models/{model['id']}", json={"tools": False})
    client.put(f"{PREFIX}/workspaces/default/models", json={"disabled_models": [SF]})
    body = client.get(f"{PREFIX}/changes").json()
    assert set(body) == {"items"}
    items = body["items"]
    # … then the administrator's SiliconFlow registration (R13 fixture), then the seed
    assert [c["operation"] for c in items] == ["workspace.flags", "model.update", "model.create",
                                               "provider.create", *["model.create"] * 4,
                                               "provider.create", "seed"]
    for item in items:
        assert set(item) == CHANGE_KEYS and item["actor"]
    assert items[0]["id"] == client.get("/api/capabilities").json()["catalog_version"]
    assert items[1]["before"]["tools"] is True and items[1]["after"]["tools"] is False
    assert items[1]["target_id"] == model["id"] and items[3]["after"]["name"] == "acme"
    assert items[-1]["actor"] == "system" and items[-1]["before"] is None
    page2 = client.get(f"{PREFIX}/changes", params={"limit": 2, "before": items[1]["id"]}).json()
    assert [c["id"] for c in page2["items"]] == [items[2]["id"], items[3]["id"]]
    assert client.get(f"{PREFIX}/changes", params={"limit": 0}).status_code == 422


# ── capabilities parity + no secret echo ─────────────────────────────────────────────
def test_capabilities_additive_keys_follow_the_registry(client):
    body = client.get("/api/capabilities").json()
    assert set(body) >= {"catalog_version", "embedding", "models", "model_providers"}
    for row in body["models"]:
        assert set(row) == CAPABILITIES_MODEL_KEYS
        assert row["kind"] == "chat" and row["enabled"] is True
        # R13: the seed is `builtin`; the administrator's SiliconFlow rows are `manual`
        assert row["source"] == ("builtin" if row["provider"] == "bedrock" else "manual")
    for provider in body["model_providers"].values():
        assert set(provider) == CAPABILITIES_PROVIDER_KEYS
        assert provider["id"].startswith("mp_") and provider["enabled"] and provider[
            "probe_state"] == "never"
    assert body["embedding"] == {"model_id": "amazon.titan-embed-text-v2:0", "dimension": 1024,
                                 "provider": "bedrock"}


def test_no_response_or_journal_row_echoes_a_key(client, monkeypatch):
    leak = "sk-live-LEAKED-0123456789abcdef"
    monkeypatch.setenv("LAUNCHPAD_ACME_API_KEY", leak)
    monkeypatch.setenv("LAUNCHPAD_SILICONFLOW_API_KEY", leak)
    acme = _acme(client)
    _model(client, acme["id"])
    client.put(f"{PREFIX}/workspaces/default/models", json={"disabled_models": [SF]})
    for path in (PREFIX, f"{PREFIX}/{acme['id']}", f"{PREFIX}/changes", "/api/capabilities"):
        text = client.get(path).text
        assert leak not in text, path
        assert "sk-live" not in text
    page = client.get(PREFIX).json()
    acme_out = next(p for p in page["providers"] if p["name"] == "acme")
    assert acme_out["configured"] is True  # the env var exists …
    assert acme_out["credential_env"] == "LAUNCHPAD_ACME_API_KEY"  # … its NAME is shown
    assert client.get("/api/capabilities").json()["model_providers"]["acme"]["configured"]
