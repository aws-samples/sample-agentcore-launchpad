"""Gate G2 (D21): an empty registry behaves byte-identically to the pre-D21 build.

`tests/fixtures/model_catalog_pre_d21.json` was captured by executing the pre-D21
`backend/app/schemas/agent.py` (cn/integration @ 58e44a50) verbatim — every module constant,
`model_catalog_entries()` / `model_catalog_lines()` prose, `model_provider_table()` and the
`/api/capabilities` projection. The tests compare (a) the built-in snapshot the holder carries
before any seed, (b) the registry seeded from it by `create_app()` and (c) the live
`/api/capabilities` body against that capture on every pre-existing key.

R13 (N13-obs7): the SiliconFlow provider and its four rows left the seed by user decision, so
the capture is compared on its **Bedrock half** (`_bedrock_only` drops every SiliconFlow key,
entry and prose line); the Bedrock rows must still be byte-identical.
"""

import json
from pathlib import Path

import pytest

from app.schemas import agent as schema
from app.schemas import model_catalog
from app.services import model_registry, opensearch_http


def _bedrock_only(value):
    """The capture minus everything SiliconFlow: dict keys naming it (model ids, the provider)
    and list entries mentioning it (catalog entries, prose lines)."""
    if isinstance(value, dict):
        return {k: _bedrock_only(v) for k, v in value.items() if "siliconflow" not in k.lower()}
    if isinstance(value, list):
        return [_bedrock_only(v) for v in value if "siliconflow" not in json.dumps(v).lower()]
    return value


FIXTURE = _bedrock_only(json.loads(
    (Path(__file__).parent / "fixtures" / "model_catalog_pre_d21.json").read_text(
        encoding="utf-8")
))
assert FIXTURE["ALLOWED_MODEL_IDS"] == ["global.zai.glm-5.3", "global.moonshotai.kimi-k3"]
# The only keys D21 adds (plan G2); everything else must be identical.
ENTRY_ADDITIVE = {"kind", "source", "enabled"}
PROVIDER_ADDITIVE = {"id", "enabled", "probe_state"}
SF_ENV = "LAUNCHPAD_SILICONFLOW_API_KEY"
SF_RECORD = {"model_credentials": {"siliconflow": {"configured": True}}}


def _strip(row: dict, keys: set[str]) -> dict:
    return {k: v for k, v in row.items() if k not in keys}


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.delenv(SF_ENV, raising=False)
    yield
    model_catalog.reset()


def _assert_views_match_fixture() -> None:
    # C5 (M1-e): the pre-D21 module constants are gone; the function views over the
    # registry snapshot must answer exactly what the constants answered
    assert schema.providers() == FIXTURE["PROVIDERS"]
    assert list(schema.providers()) == list(FIXTURE["PROVIDERS"])
    assert schema.model_catalog_table() == FIXTURE["MODEL_CATALOG"]
    assert list(schema.model_catalog_table()) == list(FIXTURE["MODEL_CATALOG"])
    for model_id, entry in schema.model_catalog_table().items():  # key ORDER too
        assert list(entry) == list(FIXTURE["MODEL_CATALOG"][model_id]), model_id
    assert schema.model_capabilities() == FIXTURE["MODEL_CAPABILITIES"]
    assert schema.model_default_max_tokens() == FIXTURE["MODEL_DEFAULT_MAX_TOKENS"]
    assert list(schema.allowed_model_ids()) == FIXTURE["ALLOWED_MODEL_IDS"]
    assert schema.allowed_model_ids() == tuple(FIXTURE["ALLOWED_MODEL_IDS"])
    for name in FIXTURE["PROVIDERS"]:
        assert schema.provider_row(name) is schema.providers()[name]
    assert schema.model_provider_table() == FIXTURE["model_provider_table"]
    assert schema.judge_budget_defaults() == FIXTURE["judge_budget_defaults"]
    entries = schema.model_catalog_entries()
    assert [_strip(e, ENTRY_ADDITIVE) for e in entries] == FIXTURE["model_catalog_entries"]
    assert schema.model_catalog_lines() == FIXTURE["model_catalog_lines"]
    assert schema.model_catalog_lines(SF_RECORD) == FIXTURE["model_catalog_lines_configured"]
    for model_id in FIXTURE["ALLOWED_MODEL_IDS"]:
        assert schema.inference_param_support(model_id) == (
            FIXTURE["inference_param_support"][model_id])
        assert schema.default_runtime_max_tokens(model_id) == (
            FIXTURE["default_runtime_max_tokens"][model_id])
        assert schema.supports_native_images(model_id) is (
            FIXTURE["supports_native_images"][model_id])


def test_the_builtin_snapshot_equals_the_pre_d21_tables():
    """Before any seed (and in every test that never builds the app) the holder carries
    `BUILTIN_CATALOG`; every legacy view over it is byte-identical to the capture."""
    model_catalog.reset()
    assert model_catalog.current() is model_catalog.BUILTIN_CATALOG
    assert model_catalog.current().version == 0
    _assert_views_match_fixture()
    # the additive keys carry the built-in provenance
    for entry in schema.model_catalog_entries():
        assert entry["kind"] == "chat" and entry["source"] == "builtin" and entry["enabled"]


def test_the_seeded_registry_equals_the_builtin_catalog(client):
    """`create_app()` seeded the ledger from the built-in tables; the snapshot now comes from
    rows (ids, version ≥ 1) and every view is still identical to the capture."""
    snapshot = model_catalog.current()
    assert snapshot is not model_catalog.BUILTIN_CATALOG and snapshot.version >= 1
    builtin = model_catalog.BUILTIN_CATALOG
    assert snapshot.legacy_providers() == builtin.legacy_providers()
    assert snapshot.legacy_models() == builtin.legacy_models()
    assert snapshot.legacy_capabilities() == builtin.legacy_capabilities()
    assert snapshot.legacy_default_max_tokens() == builtin.legacy_default_max_tokens()
    assert snapshot.allowed_ids() == builtin.allowed_ids()
    assert list(snapshot.models) == list(builtin.models)  # picker order incl. the embedding row
    for model_id, row in snapshot.models.items():
        expected = builtin.models[model_id]
        assert row.id and row.id.startswith("md_") and row.source == "builtin"
        for field in ("provider", "provider_kind", "kind", "label", "api_model_name",
                      "base_url", "credential_provider", "tools", "streaming", "vision",
                      "temperature", "top_p", "default_max_tokens", "max_output_tokens",
                      "default_judge_max_tokens", "embedding_dimension",
                      "embedding_max_chars", "enabled", "default"):
            assert getattr(row, field) == getattr(expected, field), (model_id, field)
    for prow in snapshot.providers.values():
        assert prow.id and prow.id.startswith("mp_") and prow.builtin and prow.enabled
    _assert_views_match_fixture()


def test_the_embedding_row_is_titan_v2_exactly_as_opensearch_http_has_it(client):
    row = model_catalog.current().embedding_model()
    assert row is not None and row.kind == "embedding" and row.default and row.enabled
    assert row.model_id == opensearch_http.EMBED_MODEL_ID == "amazon.titan-embed-text-v2:0"
    assert row.embedding_dimension == opensearch_http.EMBED_DIMENSIONS == 1024
    assert row.embedding_max_chars == opensearch_http.EMBED_MAX_CHARS == 8000
    assert row.provider == "bedrock" and row.credential_provider == "launchpad-global-bedrock"
    assert schema.active_embedding() is row
    # never a chat row: pickers, templates and judges never see it
    assert row.model_id not in schema.allowed_model_ids()
    assert row.model_id not in schema.model_catalog_table()
    assert row.model_id not in schema.model_provider_table()


def test_capabilities_keeps_every_pre_d21_key_byte_identical(client):
    body = client.get("/api/capabilities").json()
    assert [_strip(r, ENTRY_ADDITIVE) for r in body["models"]] == FIXTURE["capabilities_models"]
    assert {name: _strip(p, PROVIDER_ADDITIVE) for name, p in body["model_providers"].items()
            } == FIXTURE["capabilities_model_providers"]
    assert body["catalog_version"] == model_catalog.current().version >= 1
    assert body["embedding"] == {"model_id": "amazon.titan-embed-text-v2:0", "dimension": 1024,
                                 "provider": "bedrock"}


def test_the_seed_is_journaled_once_and_idempotent(client):
    from app.core.db import SessionLocal

    db = SessionLocal()
    try:
        version = model_registry.stamp(db)
        changes = model_registry.list_changes(db)
        assert [c.operation for c in changes] == ["seed"]
        assert changes[0].after["providers"] == ["bedrock"]  # R13: no SiliconFlow in the seed
        assert changes[0].after["models"] == [*FIXTURE["ALLOWED_MODEL_IDS"],
                                              "amazon.titan-embed-text-v2:0"]
        assert set(changes[0].after) == {"providers", "models"}
        # a second start adds nothing and keeps the version
        again = model_registry.ensure_seeded(db)
        assert again.version == version == model_registry.stamp(db)
        assert len(model_registry.list_changes(db)) == 1
    finally:
        db.close()


def test_the_builtin_catalog_is_bedrock_only_and_the_spec_grammar_needs_a_provider_row():
    """R13 (N13-obs7): `builtin_catalog()` takes no seed spec any more — it is
    `build_model_catalog()` (the two Bedrock rows) + Titan v2. The spec grammar survives for
    admin tooling and tests, on an explicit provider row (`tests/model_fixtures.py`)."""
    from tests.model_fixtures import SILICONFLOW_PROVIDER

    catalog = model_catalog.builtin_catalog()
    assert catalog.version == 0 and set(catalog.providers) == {"bedrock"}
    assert catalog.allowed_ids() == ("global.zai.glm-5.3", "global.moonshotai.kimi-k3")
    assert list(catalog.models) == [*catalog.allowed_ids(), "amazon.titan-embed-text-v2:0"]
    assert catalog.legacy_models() == schema.build_model_catalog()
    spec = "Qwen/Qwen3-8B:notools+max=4096+judge=2048, zai-org/GLM-4.5V:vision"
    with pytest.raises(ValueError, match="provider_row"):
        schema.build_model_catalog(spec)
    rows = schema.build_model_catalog(spec, SILICONFLOW_PROVIDER)
    assert list(rows) == [*catalog.allowed_ids(), "siliconflow.Qwen/Qwen3-8B",
                          "siliconflow.zai-org/GLM-4.5V"]
    assert rows["siliconflow.Qwen/Qwen3-8B"]["tools"] is False
    assert rows["siliconflow.zai-org/GLM-4.5V"]["vision"] is True


def test_legacy_key_sets_are_pinned():
    """The legacy row shapes every pre-D21 consumer indexes never grow (templates render
    them byte-for-byte)."""
    for entry in schema.model_catalog_table().values():
        assert tuple(entry) == model_catalog.LEGACY_MODEL_KEYS
    for row in schema.providers().values():
        assert tuple(row) == model_catalog.LEGACY_PROVIDER_KEYS
    assert set(FIXTURE["MODEL_CATALOG"]["global.zai.glm-5.3"]) == set(
        model_catalog.LEGACY_MODEL_KEYS)
    assert set(FIXTURE["PROVIDERS"]["bedrock"]) == set(model_catalog.LEGACY_PROVIDER_KEYS)
