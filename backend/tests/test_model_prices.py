"""Price refresher — litellm conversion, merge semantics, persistence, API."""

import pytest
import yaml

import app.core.config as config_module
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.services import model_prices as mp

SOURCE = {
    "sample_spec": {"mode": "chat"},  # doc entry, no costs — skipped
    "claude-sonnet-4-6": {
        "input_cost_per_token": 3e-06, "output_cost_per_token": 1.5e-05,
        "cache_read_input_token_cost": 3e-07,
        "cache_creation_input_token_cost": 3.75e-06,
        "litellm_provider": "anthropic", "mode": "chat",
    },
    "anthropic.claude-sonnet-4-6": {
        "input_cost_per_token": 3e-06, "output_cost_per_token": 1.5e-05,
        "cache_read_input_token_cost": 3e-07,
        "cache_creation_input_token_cost": 3.75e-06,
        "litellm_provider": "bedrock_converse", "mode": "chat",
    },
    "global.anthropic.claude-sonnet-4-6": {
        "input_cost_per_token": 3e-06, "output_cost_per_token": 1.5e-05,
        "cache_read_input_token_cost": 3e-07,
        "cache_creation_input_token_cost": 3.75e-06,
        "litellm_provider": "bedrock_converse", "mode": "chat",
    },
    "us.anthropic.claude-sonnet-4-6": {  # regional premium
        "input_cost_per_token": 3.3e-06, "output_cost_per_token": 1.65e-05,
        "litellm_provider": "bedrock_converse", "mode": "chat",
    },
    "nvidia.nemotron-nano-3-30b": {
        "input_cost_per_token": 2e-07, "output_cost_per_token": 6e-07,
        "litellm_provider": "bedrock_converse", "mode": "chat",
    },
    # priced under the bare id + us. only — no global.* key (the real litellm
    # shape for Nova 2 Lite, which left global inference profiles unpriced)
    "amazon.nova-2-lite-v1:0": {
        "input_cost_per_token": 3e-07, "output_cost_per_token": 2.5e-06,
        "litellm_provider": "bedrock_converse", "mode": "chat",
    },
    "us.amazon.nova-2-lite-v1:0": {
        "input_cost_per_token": 3.3e-07, "output_cost_per_token": 2.75e-06,
        "litellm_provider": "bedrock_converse", "mode": "chat",
    },
    "claude-embedding": {  # non-chat — never considered
        "input_cost_per_token": 1e-07, "output_cost_per_token": 0.0,
        "litellm_provider": "anthropic", "mode": "embedding",
    },
    # Mantle model: litellm prices the Bedrock id ONLY under a provider path
    # with mode "responses"; the direct-provider ids carry different prices.
    "bedrock_mantle/openai.gpt-5.6-terra": {
        "input_cost_per_token": 2.2e-06, "output_cost_per_token": 1.32e-05,
        "litellm_provider": "bedrock_mantle", "mode": "responses",
    },
    "gpt-5.6-terra": {
        "input_cost_per_token": 2e-06, "output_cost_per_token": 1.2e-05,
        "litellm_provider": "openai", "mode": "chat",
    },
    "azure/gpt-5.6-terra": {
        "input_cost_per_token": 2e-06, "output_cost_per_token": 1.2e-05,
        "litellm_provider": "azure", "mode": "chat",
    },
    # priced under the bare model name only — needs the vendor-prefix strip
    "mistral-large-3": {
        "input_cost_per_token": 2e-06, "output_cost_per_token": 6e-06,
        "litellm_provider": "mistral", "mode": "chat",
    },
}


class FakeCWModels:
    def __init__(self, models):
        self.models = models

    def get_paginator(self, name):
        assert name == "list_metrics"
        pages = [{
            "Metrics": [
                {"Namespace": "bedrock-agentcore",
                 "MetricName": "gen_ai.client.token.usage",
                 "Dimensions": [{"Name": "gen_ai.request.model", "Value": m},
                                {"Name": "gen_ai.token.type", "Value": "input"}]}
                for m in self.models
            ]
        }]

        class P:
            def paginate(_self, **kwargs):
                return iter(pages)

        return P()


@pytest.fixture(autouse=True)
def restore_settings():
    yield
    get_settings.cache_clear()  # tests below repoint CONFIG_FILE


def test_entry_conversion_per_million():
    entry = mp._entry_from_litellm(SOURCE["global.anthropic.claude-sonnet-4-6"])
    assert entry == {"input": 3.0, "output": 15.0, "cache_read": 0.3,
                     "cache_write": 3.75}
    assert mp._entry_from_litellm(SOURCE["sample_spec"]) is None


def test_refresh_map_merge_semantics():
    current = {"sonnet-4-6": {"input": 99.0, "output": 99.0},
               "mystery-model": {"input": 1.0, "output": 2.0}}
    seen = ["us.anthropic.claude-sonnet-4-6", "nvidia.nemotron-nano-3-30b",
            "unknown.model-nobody-prices"]
    prices, updated, added = mp.refresh_map(current, seen, SOURCE)
    # seen models gain exact full-id entries (regional premium preserved)
    assert prices["us.anthropic.claude-sonnet-4-6"]["input"] == pytest.approx(3.3)
    assert prices["nvidia.nemotron-nano-3-30b"]["output"] == pytest.approx(0.6)
    assert set(added) == {"us.anthropic.claude-sonnet-4-6",
                          "nvidia.nemotron-nano-3-30b"}
    # short key refreshed from the canonical (preferred-provider) entry
    assert prices["sonnet-4-6"] == {"input": 3.0, "output": 15.0,
                                    "cache_read": 0.3, "cache_write": 3.75}
    assert "sonnet-4-6" in updated
    # unmatched key preserved untouched
    assert prices["mystery-model"] == {"input": 1.0, "output": 2.0}


def test_refresh_map_prices_cross_region_ids_from_the_bare_id():
    """A global.* inference profile the source doesn't list must still get an
    entry, taken from the bare model id — and an exact regional hit must keep
    its own premium rather than the bare price."""
    prices, _, added = mp.refresh_map(
        {},
        ["global.amazon.nova-2-lite-v1:0", "us.amazon.nova-2-lite-v1:0"],
        SOURCE,
    )
    assert prices["global.amazon.nova-2-lite-v1:0"] == {"input": 0.3, "output": 2.5}
    assert prices["us.amazon.nova-2-lite-v1:0"] == {"input": 0.33, "output": 2.75}
    assert set(added) == {"global.amazon.nova-2-lite-v1:0",
                         "us.amazon.nova-2-lite-v1:0"}
    # a model absent from the source under any prefix stays unpriced
    prices, _, _ = mp.refresh_map({}, ["global.vendor.not-listed-v1:0"], SOURCE)
    assert prices == {}


def test_refresh_map_prices_mantle_ids_from_provider_path():
    """A Mantle id (openai.gpt-5.6-terra) is priced from the provider-path
    entry (bedrock_mantle/..., mode "responses") — carrying the Mantle
    premium, not the direct-OpenAI or Azure price."""
    prices, _, added = mp.refresh_map({}, ["openai.gpt-5.6-terra"], SOURCE)
    assert added == ["openai.gpt-5.6-terra"]
    assert prices["openai.gpt-5.6-terra"] == {"input": 2.2, "output": 13.2}


def test_refresh_map_strips_any_vendor_prefix():
    """`eu.mistral.mistral-large-3` resolves via region + vendor strip to the
    bare `mistral-large-3` source entry."""
    prices, _, added = mp.refresh_map({}, ["eu.mistral.mistral-large-3"], SOURCE)
    assert added == ["eu.mistral.mistral-large-3"]
    assert prices["eu.mistral.mistral-large-3"] == {"input": 2.0, "output": 6.0}


def test_candidate_ids_never_strip_version_dots():
    # "gpt-5" carries a hyphen, so the leading dot-segment is not a vendor token
    assert mp._candidate_ids("gpt-5.6-terra") == ["gpt-5.6-terra"]
    assert mp._candidate_ids("global.anthropic.claude-sonnet-4-6") == [
        "global.anthropic.claude-sonnet-4-6",
        "anthropic.claude-sonnet-4-6",
        "claude-sonnet-4-6",
    ]


class FakeSpanLogs:
    """Minimal Logs Insights fake for _span_models."""

    def __init__(self, models):
        self.models = models

    def start_query(self, **kwargs):
        return {"queryId": "q1"}

    def get_query_results(self, queryId):
        return {
            "status": "Complete",
            "results": [
                [{"field": "model", "value": m}, {"field": "n", "value": "3"}]
                for m in self.models
            ],
        }


def test_span_models_discovers_from_spans():
    assert mp._span_models(logs=FakeSpanLogs(["openai.gpt-5.6-terra", "x"])) == [
        "openai.gpt-5.6-terra", "x"]


def test_default_prices_cover_global_nova_2_lite():
    """Stock config (no refresh yet) still estimates Nova 2 Lite cost."""
    from app.services.observability import match_price

    defaults = Settings.model_fields["model_prices"].default
    assert match_price("global.amazon.nova-2-lite-v1:0", defaults) == {
        "input": 0.3, "output": 2.5,
    }


def test_refresh_persists_and_applies(tmp_path, monkeypatch):
    cfg = tmp_path / "launchpad.yaml"
    cfg.write_text(yaml.safe_dump({
        "region": "us-west-2",
        "model_prices": {"sonnet-4-6": {"input": 99.0, "output": 99.0}},
    }), encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_FILE", cfg)
    get_settings.cache_clear()
    assert get_settings().model_prices["sonnet-4-6"]["input"] == 99.0

    monkeypatch.setattr(mp, "_span_models", lambda logs=None, hours=0: [])
    result = mp.refresh_model_prices(
        cw=FakeCWModels(["global.anthropic.claude-sonnet-4-6"]),
        fetch=lambda url: SOURCE,
    )
    assert result["meta"]["added"] == ["global.anthropic.claude-sonnet-4-6"]
    assert "sonnet-4-6" in result["meta"]["updated"]
    # persisted to the config file with metadata
    on_disk = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert on_disk["model_prices"]["sonnet-4-6"]["input"] == 3.0
    assert on_disk["model_prices_meta"]["updated_at"]
    assert on_disk["region"] == "us-west-2"  # other config keys survive
    # settings cache cleared → estimator sees the new prices immediately
    assert get_settings().model_prices["sonnet-4-6"]["input"] == 3.0
    # exact full-id key wins over the short key for that model (longest match)
    from app.services.observability import match_price
    exact = match_price("global.anthropic.claude-sonnet-4-6",
                        get_settings().model_prices)
    assert exact["cache_read"] == pytest.approx(0.3)


def test_refresh_rejects_non_https(monkeypatch):
    with pytest.raises(AppError):
        mp.refresh_model_prices(source_url="http://insecure.example/prices.json",
                                fetch=lambda url: SOURCE)


def test_due_logic(tmp_path, monkeypatch):
    cfg = tmp_path / "launchpad.yaml"
    cfg.write_text(yaml.safe_dump({"model_prices_meta": {}}), encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_FILE", cfg)
    get_settings.cache_clear()
    assert mp._due(24) is True  # never refreshed
    cfg.write_text(yaml.safe_dump(
        {"model_prices_meta": {"updated_at": "2026-07-10T00:00:00+00:00"}}),
        encoding="utf-8")
    get_settings.cache_clear()
    assert mp._due(24 * 365 * 100) is False  # refreshed within the window


def test_prices_endpoints(client, monkeypatch, tmp_path):
    listing = client.get("/api/observability/prices")
    assert listing.status_code == 200
    body = listing.json()
    assert "sonnet-4-6" in body["prices"] and body["refresh_hours"] >= 0

    cfg = tmp_path / "launchpad.yaml"
    cfg.write_text(yaml.safe_dump({"model_prices": {"sonnet-4-6": {
        "input": 99.0, "output": 99.0}}}), encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_FILE", cfg)
    get_settings.cache_clear()
    monkeypatch.setattr(mp, "_seen_models", lambda cw=None: [])
    monkeypatch.setattr(mp, "_span_models", lambda logs=None, hours=0: [])
    monkeypatch.setattr(mp, "_fetch_source", lambda url: SOURCE)
    res = client.post("/api/observability/prices/refresh")
    assert res.status_code == 200
    assert "sonnet-4-6" in res.json()["meta"]["updated"]
