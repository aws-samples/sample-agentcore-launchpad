"""Response-shape contract of ``/api/settings/model-providers`` — pinned exact key sets so the
backend (M1-a) and the console's typed client cannot drift.

Frontend pairing: cn/m1-c a143c8cf, api.ts MODEL_PROVIDERS_BASE (`frontend/src/lib/api.ts`:
``ModelProviderOut``,
``ModelRowOut``, ``ModelProbeRecord``, ``ModelProviderProbeOut``, ``ModelProbeOut``,
``ModelProviderCredentialOut``, ``ModelWorkspaceFlags``, ``ModelCatalogWarning``,
``ModelProvidersPage``, ``ModelCatalogChange``; ``MODEL_PROVIDERS_BASE`` =
``/api/settings/model-providers``). Additive keys are allowed only by editing this file in
the same change as `docs/cn/interfaces/model-registry.md`.
"""

from app.core import route_policy
from app.schemas import model_catalog
from app.schemas import model_providers as contract
from app.services import model_registry

PREFIX = "/api/settings/model-providers"

# `ModelProviderOut` — every key the console types; `model_count` is the one backend-additive
# key (the console ignores unknown keys at runtime).
PROVIDER_OUT = {
    "id", "name", "kind", "label", "base_url", "model_region",
    "credential_provider", "credential_ssm_parameter", "credential_source", "credential_env",
    "required", "builtin", "enabled", "id_prefix", "probe", "configured", "key_version",
    "key_modified_at", "created_by", "created_at", "updated_by", "updated_at",
    "model_count",  # backend-additive
    # N17-obs283: ONE credential shape — flat; `key_last4` replaced the nested mirror's `last4`
    "key_last4",
    # N17-obs203: owner-bound providers — creator display name, scope, the caller's "mine" flag
    "created_by_name", "scope", "owned",
}
PROVIDER_PROBE = {"state", "latency_ms", "models_seen", "error", "checked_at"}
# `ModelRowOut` (= `ModelCapabilityFields` + identity); `provider` (name) is backend-additive.
MODEL_OUT = {
    "id", "provider_id", "model_id", "api_model_name", "label", "kind", "enabled", "is_default",
    "tools", "streaming", "vision", "inference_params", "default_max_tokens",
    "max_output_tokens", "default_judge_max_tokens", "embedding_dimension",
    "embedding_max_chars", "source", "capability_sources", "probe", "sort_order",
    "created_by", "created_at", "updated_by", "updated_at",
    "provider",
}
INFERENCE_PARAMS = {"temperature", "top_p"}
CAPABILITY_SOURCE_KEYS = {
    "tools", "streaming", "vision", "temperature", "top_p", "default_max_tokens",
    "max_output_tokens", "default_judge_max_tokens", "embedding_dimension",
}
MODEL_PROBE_RECORD = {"checked_at", "latency_ms", "findings", "errors", "raw_hint"}
MODEL_PROBE_OUT = {"findings", "diff", "errors", "raw_hint", "latency_ms", "applied"}
PROBE_DIFF_ENTRY = {"stored", "probed", "source"}
CREDENTIAL_OUT = {"configured", "created", "rotated", "workspaces_updated", "key_version",
                  "last4"}
WORKSPACE_FLAGS = {"id", "name", "disabled_models"}
WORKSPACE_FLAGS_ECHO = WORKSPACE_FLAGS | {"workspace_id"}  # PUT echo keeps workspace_id (M8)
WARNING = {"code", "message", "detail"}
PAGE = {"catalog_version", "providers", "models", "workspaces", "warnings"}
CHANGE = {"id", "operation", "actor", "target_id", "before", "after", "created_at"}
CREATE_PROVIDER = {"name", "kind", "label", "base_url", "model_region",
                   "credential_ssm_parameter", "enabled",
                   "scope",  # N17-obs203: personal | workspace (workspace = administrators)
                   "api_key"}  # N17-obs203 §3: WRITE-ONLY — probed, written to SSM, dropped
# What the console's `providerBody()` (`frontend/src/v2/pages/modelProviders/actions.tsx`) and
# the edit drawer PUT — mirrored literally (an openai_compatible row: `model_region: null`, never
# ""). `name` / `kind` must NOT be here: `ProviderUpdate` forbids them (D21-obs8).
CONSOLE_PROVIDER_UPDATE_KEYS = {"label", "base_url", "model_region", "credential_ssm_parameter",
                                "enabled"}
CONSOLE_PROVIDER_UPDATE_PAYLOAD = {
    "label": "SiliconFlow", "base_url": "https://api.siliconflow.cn/v1", "model_region": None,
    "credential_ssm_parameter": "launchpad-siliconflow-api-key", "enabled": False,
}
CREATE_MODEL = {"api_model_name", "kind", "label", "probe", "overrides", "enabled"}
CAPABILITY_FIELDS_BODY = {"tools", "streaming", "vision", "inference_params",
                          "default_max_tokens", "max_output_tokens", "default_judge_max_tokens",
                          "embedding_dimension", "embedding_max_chars"}
PATCH_MODEL = CAPABILITY_FIELDS_BODY | {"label", "enabled", "is_default", "sort_order"}
OPERATIONS = {
    "provider.create", "provider.update", "provider.delete", "provider.credential",
    "provider.probe", "model.create", "model.update", "model.delete", "model.probe",
    "workspace.flags", "seed",
    "model_role.set", "model_role.reset",  # r15-e: api.ts `ModelCatalogOperation`
    "provider.key",  # N17-obs203 §3: a console-typed key reached SSM (audit-only)
}
# M1-d adds `preset_snapshot_behind`; M1-e (C5) removed the seed-time override warning
WARNING_CODES_M1A = {"probe_failed"}
REFERRER_KINDS = {"agent", "evaluator", "experiment", "setting"}


def test_response_models_match_the_console_types():
    assert set(contract.ProviderOut.model_fields) == PROVIDER_OUT
    assert set(contract.ProviderProbeOut.model_fields) == PROVIDER_PROBE
    assert set(contract.ModelOut.model_fields) == MODEL_OUT
    assert set(contract.InferenceParamsOut.model_fields) == INFERENCE_PARAMS
    assert set(contract.ModelProbeRecordOut.model_fields) == MODEL_PROBE_RECORD
    assert set(contract.ModelProbeOut.model_fields) == MODEL_PROBE_OUT
    assert set(contract.ModelProbeDiffEntry.model_fields) == PROBE_DIFF_ENTRY
    assert set(contract.CredentialOut.model_fields) == CREDENTIAL_OUT
    assert set(contract.WorkspaceOut.model_fields) == WORKSPACE_FLAGS
    assert set(contract.WorkspaceFlagsOut.model_fields) == WORKSPACE_FLAGS_ECHO
    # N17-obs283 (design simplification 10.3): the nested credential mirror is gone
    assert not hasattr(contract, "ProviderCredentialOut")
    # api.ts `ModelProbeOut.applied?: boolean`, followed literally
    assert contract.ModelProbeOut.model_fields["applied"].annotation is bool
    assert set(contract.WarningOut.model_fields) == WARNING
    assert set(contract.RegistryOut.model_fields) == PAGE
    assert set(contract.ChangeOut.model_fields) == CHANGE
    assert set(contract.ChangesOut.model_fields) == {"items"}
    # enumerations
    assert set(contract.ProviderKind.__args__) == {"bedrock", "openai_compatible"}
    assert set(contract.ModelKind.__args__) == {"chat", "embedding"}
    assert set(contract.CredentialSource.__args__) == {"ssm", "host_env"}
    assert set(contract.ProbeState.__args__) == {"never", "ok", "failed"}
    assert set(contract.RowSource.__args__) == {"builtin", "probed", "manual"}
    assert set(model_catalog.FIELD_SOURCES) == {"builtin", "probed", "manual", "unknown"}
    assert set(model_catalog.CAPABILITY_SOURCE_FIELDS) == CAPABILITY_SOURCE_KEYS


def test_request_bodies_match_the_console_types():
    assert set(contract.ProviderCreate.model_fields) == CREATE_PROVIDER
    assert set(contract.ProviderUpdate.model_fields) == CREATE_PROVIDER - {"name", "kind"}
    assert set(contract.ModelCreate.model_fields) == CREATE_MODEL
    assert set(contract.ModelCapabilityOverrides.model_fields) == CAPABILITY_FIELDS_BODY
    assert set(contract.InferenceParamsIn.model_fields) == INFERENCE_PARAMS  # nested, as in Out
    assert set(contract.ModelUpdate.model_fields) == PATCH_MODEL
    assert set(contract.WorkspaceFlagsIn.model_fields) == {"disabled_models"}
    assert set(contract.ModelProbeIn.model_fields) == {"apply"}
    assert set(contract.CredentialRegisterIn.model_fields) == {"api_key"}  # N17-obs203 §3
    assert contract.ModelCreate.model_fields["probe"].default is True  # probe-on-add default


def test_console_provider_update_payload_validates_and_name_kind_do_not():
    """D21-obs8 (user-reported: disabling SiliconFlow failed): the console's PUT body must be a
    subset of `ProviderUpdate`; adding the immutable `name` / `kind` is a validation error."""
    import pytest
    from pydantic import ValidationError

    assert set(CONSOLE_PROVIDER_UPDATE_PAYLOAD) == CONSOLE_PROVIDER_UPDATE_KEYS
    assert CONSOLE_PROVIDER_UPDATE_KEYS <= set(contract.ProviderUpdate.model_fields)
    body = contract.ProviderUpdate(**CONSOLE_PROVIDER_UPDATE_PAYLOAD)
    assert body.model_dump(exclude_unset=True) == CONSOLE_PROVIDER_UPDATE_PAYLOAD
    # a Bedrock row's body (`base_url: null`, a region, no SSM parameter) validates as well
    contract.ProviderUpdate(label="Global Bedrock", base_url=None, model_region="us-west-2",
                            credential_ssm_parameter=None, enabled=True)
    with pytest.raises(ValidationError):
        contract.ProviderUpdate(**CONSOLE_PROVIDER_UPDATE_PAYLOAD, name="siliconflow")
    with pytest.raises(ValidationError):
        contract.ProviderUpdate(**CONSOLE_PROVIDER_UPDATE_PAYLOAD, kind="openai_compatible")
    with pytest.raises(ValidationError):  # "" is not a region; the console sends null instead
        contract.ProviderUpdate(**{**CONSOLE_PROVIDER_UPDATE_PAYLOAD, "model_region": ""})
    assert model_registry.E_PROVIDER_INVALID_UPDATE == "model_provider.invalid_update"


# N17-obs203 §3: the ONLY models a key may travel in — request bodies, write-only
KEY_CARRYING_REQUESTS = {"ProviderCreate", "ProviderUpdate", "CredentialRegisterIn"}


def test_no_response_model_has_a_field_a_key_could_travel_in():
    """Every response model is secret-free by construction; the typed key (N17-obs203 §3)
    exists in exactly the three write-only request bodies, under the one field name the
    validation handler already redacts (`core/errors.is_sensitive_field("api_key")`)."""
    for name, model in vars(contract).items():
        if isinstance(model, type) and issubclass(model, contract.BaseModel):
            for field in model.model_fields:
                if name in KEY_CARRYING_REQUESTS and field == "api_key":
                    assert issubclass(model, contract._In), name  # a request body, never an Out
                    continue
                # `*_tokens` are output budgets, not bearer tokens
                assert field != "key" and not field.endswith("_key") and "secret" not in field \
                    and "api_key" not in field and "password" not in field, (name, field)
    from app.core.errors import is_sensitive_field

    assert is_sensitive_field("api_key")


def test_error_and_warning_codes_match_the_console():
    assert model_registry.E_PROBE_UNAVAILABLE == "model_providers.probe_unavailable"
    # M7: distinct code; the console's `isProbeUnavailable` recognises both
    assert model_registry.E_CREDENTIAL_UNAVAILABLE == "model_providers.credential_unavailable"
    assert model_registry.E_MODEL_PROBE_FAILED == "model.probe_failed"
    assert model_registry.E_MODEL_IN_USE == "model.in_use"
    assert model_registry.E_PROVIDER_IN_USE == "model_provider.in_use"
    assert model_registry.E_PROVIDER_BUILTIN == "model_provider.builtin"
    assert {model_registry.W_PROBE_FAILED} == WARNING_CODES_M1A
    from app.models.model_registry import CHANGE_OPERATIONS

    assert set(CHANGE_OPERATIONS) == OPERATIONS


def test_routes_match_the_console_client():
    registry_routes = {k for k in route_policy.ROUTE_POLICY if k[1].startswith(PREFIX)}
    assert registry_routes == {
        ("GET", PREFIX), ("POST", PREFIX), ("GET", f"{PREFIX}/changes"),
        ("PUT", f"{PREFIX}/workspaces/{{workspace_id}}/models"),
        ("GET", f"{PREFIX}/{{provider_id}}"), ("PUT", f"{PREFIX}/{{provider_id}}"),
        ("DELETE", f"{PREFIX}/{{provider_id}}"),
        ("POST", f"{PREFIX}/{{provider_id}}/credential"),
        ("POST", f"{PREFIX}/{{provider_id}}/probe"),
        ("POST", f"{PREFIX}/{{provider_id}}/models"),
        ("PUT", f"{PREFIX}/{{provider_id}}/models/{{model_ref}}"),
        ("DELETE", f"{PREFIX}/{{provider_id}}/models/{{model_ref}}"),
        ("POST", f"{PREFIX}/{{provider_id}}/models/{{model_ref}}/probe"),
    }
    # N17-obs203: owner-bound providers — the CRUD is every member's (the handler adds the
    # visibility 404 + creator-or-admin 403); the journal and the flag matrix stay admin-only
    admin_only = {("GET", f"{PREFIX}/changes"),
                  ("PUT", f"{PREFIX}/workspaces/{{workspace_id}}/models")}
    for k in registry_routes:
        expected = route_policy.ADMIN if k in admin_only else route_policy.MEMBER
        assert route_policy.ROUTE_POLICY[k] == expected, k
    assert set(contract.ProviderScope.__args__) == {"personal", "workspace"}


def test_live_payloads_carry_exactly_the_pinned_keys(client, monkeypatch):
    monkeypatch.delenv("LAUNCHPAD_SILICONFLOW_API_KEY", raising=False)
    try:
        page = client.get(PREFIX).json()
        assert set(page) == PAGE
        for provider in page["providers"]:
            assert set(provider) == PROVIDER_OUT and set(provider["probe"]) == PROVIDER_PROBE
        for model in page["models"]:
            assert set(model) == MODEL_OUT
            assert set(model["inference_params"]) == INFERENCE_PARAMS
            assert set(model["capability_sources"]) == CAPABILITY_SOURCE_KEYS
            assert model["probe"] is None
        for ws in page["workspaces"]:
            assert set(ws) == WORKSPACE_FLAGS
        flags = client.put(f"{PREFIX}/workspaces/default/models",
                           json={"disabled_models": ["global.moonshotai.kimi-k3"]}).json()
        assert set(flags) == WORKSPACE_FLAGS_ECHO and flags["id"] == flags["workspace_id"]
        for change in client.get(f"{PREFIX}/changes").json()["items"]:
            assert set(change) == CHANGE and change["operation"] in OPERATIONS
        # 409 in_use detail: referrers keyed `kind`
        from app.core.db import SessionLocal
        from app.models.ledger import Agent

        acme = client.post(PREFIX, json={"name": "acme", "kind": "openai_compatible",
                                         "label": "Acme",
                                         "base_url": "https://api.siliconflow.cn/x"}).json()
        model = client.post(f"{PREFIX}/{acme['id']}/models",
                            json={"api_model_name": "a/b", "kind": "chat", "probe": False}).json()
        db = SessionLocal()
        try:
            db.add(Agent(name="pin-ref", method="zip_runtime", status="active",
                         workspace_id="default",
                         spec={"name": "pin-ref", "method": "zip_runtime",
                               "model_id": model["model_id"]}))
            db.commit()
        finally:
            db.close()
        refused = client.delete(f"{PREFIX}/{acme['id']}/models/{model['id']}")
        assert refused.status_code == 409 and refused.json()["code"] == "model.in_use"
        for ref in refused.json()["detail"]["referrers"]:
            assert ref["kind"] in REFERRER_KINDS and {"kind", "id", "name"} <= set(ref)
            assert ref["type"] == ref["kind"]  # M5: `type` kept alongside `kind`
        # 501 stubs: codes the console's `isProbeUnavailable` recognises — the contract of
        # the EMPTY slot (create_app() registers M1-b's hooks; clear them for this check)
        model_registry.set_probe_hooks(None)
        for path, code in ((f"{PREFIX}/{acme['id']}/probe", "model_providers.probe_unavailable"),
                           (f"{PREFIX}/{acme['id']}/credential",
                            "model_providers.credential_unavailable"),
                           (f"{PREFIX}/{acme['id']}/models/{model['id']}/probe",
                            "model_providers.probe_unavailable")):
            response = client.post(path, json={})
            assert response.status_code == 501 and response.json()["code"] == code
    finally:
        model_catalog.reset()
