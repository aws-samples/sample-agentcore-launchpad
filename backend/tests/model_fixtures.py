"""SiliconFlow as an ADMIN-REGISTERED OpenAI-compatible provider (R13, N13-obs7).

Until round 13 the built-in seed shipped a SiliconFlow provider (`required: False`, key from
SSM) and four chat rows. A fresh ledger now seeds Global Bedrock + Titan v2 only; SiliconFlow
is what an administrator adds through `/api/settings/model-providers` with the SSM parameter
NAME of its key. These helpers register exactly that provider — the rows the seed used to
carry — for the tests that exercise the `openai_compatible` kind, so their historical
expectations (ids, labels, credential names, budgets) hold unchanged.

Two ways in, picked by the autouse `siliconflow_marker` fixture (`conftest.py`) from
`@pytest.mark.siliconflow` / `pytestmark = pytest.mark.siliconflow`:

* **snapshot** — a test that never builds the app reads the holder: `publish_siliconflow()`
  publishes `current()` + the provider + its rows (`extended()`, M1-e style).
* **registry** — a test with the `client` fixture, or marked `siliconflow(registry=True)`:
  `register_siliconflow()` seeds Bedrock (`ensure_seeded`) and then goes through
  `model_registry.create_provider` / `create_model` — the admin path, journaled, published.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from app.schemas import model_catalog as mc
from app.services import model_registry

SILICONFLOW = "siliconflow"
SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
SILICONFLOW_SSM = "launchpad-siliconflow-api-key"
SILICONFLOW_ENV = "LAUNCHPAD_SILICONFLOW_API_KEY"
SILICONFLOW_CREDENTIAL = "launchpad-siliconflow"
# The legacy provider row the seed carried before R13 (`BUILTIN_PROVIDERS["siliconflow"]`).
SILICONFLOW_PROVIDER: dict[str, Any] = {
    "name": SILICONFLOW,
    "kind": mc.PROVIDER_OPENAI_COMPATIBLE,
    "label": "SiliconFlow",
    "base_url": SILICONFLOW_BASE_URL,
    "credential_provider": SILICONFLOW_CREDENTIAL,
    "ssm_parameter": SILICONFLOW_SSM,
    "api_key_env": SILICONFLOW_ENV,
    "required": False,
    "id_prefix": "siliconflow.",
}
# The four chat rows the seed carried before R13 (`DEFAULT_SILICONFLOW_MODELS`).
SILICONFLOW_MODELS = (
    "zai-org/GLM-5.3:tools,deepseek-ai/DeepSeek-V4-Pro:tools,Qwen/Qwen3.8-27B:tools,"
    "Pro/moonshotai/Kimi-K2.6:tools"
)
SF_GLM = "siliconflow.zai-org/GLM-5.3"


def siliconflow_entries(spec: str = SILICONFLOW_MODELS) -> dict[str, dict[str, Any]]:
    """Legacy catalog rows (`MODEL_CATALOG` shape) of ``spec`` on the SiliconFlow provider."""
    return {model_id: entry
            for model_id, entry in mc.build_model_catalog(spec, SILICONFLOW_PROVIDER).items()
            if entry["provider"] == SILICONFLOW}


def siliconflow_provider_row(**overrides: Any) -> mc.ProviderRow:
    row = mc.provider_row_from_legacy(
        SILICONFLOW, SILICONFLOW_PROVIDER, builtin=False,
        credential_source=mc.CREDENTIAL_SOURCE_SSM)
    return dataclasses.replace(row, **overrides) if overrides else row


def siliconflow_catalog(base: mc.Catalog | None = None, spec: str = SILICONFLOW_MODELS,
                        *, source: str = mc.SOURCE_MANUAL) -> mc.Catalog:
    """``base`` (default: the current snapshot) + the SiliconFlow provider and rows."""
    base = base if base is not None else mc.current()
    start = len(base.models)
    models = {
        model_id: mc.model_row_from_legacy(entry, source=source, default=False,
                                           sort_order=start + index)
        for index, (model_id, entry) in enumerate(siliconflow_entries(spec).items())
    }
    # keep the embedding row last, as the seed and the registry list it
    embedding = {k: v for k, v in base.models.items() if v.kind == mc.MODEL_KIND_EMBEDDING}
    chat = {k: v for k, v in base.models.items() if v.kind != mc.MODEL_KIND_EMBEDDING}
    return mc.Catalog(
        version=base.version,
        providers={**base.providers, SILICONFLOW: siliconflow_provider_row()},
        models={**chat, **models, **embedding},
        workspace_disabled=dict(base.workspace_disabled),
    )


def publish_siliconflow(spec: str = SILICONFLOW_MODELS) -> mc.Catalog:
    """Snapshot path: publish `current()` + SiliconFlow (tests without an app)."""
    return mc.publish(siliconflow_catalog(mc.current(), spec))


def register_siliconflow(db=None, spec: str = SILICONFLOW_MODELS, *, actor: str = "tests",
                         enabled: bool = True):
    """Registry path: what an administrator does — `POST /api/settings/model-providers`
    with the SSM parameter name, then one `POST …/models` per row. Seeds Bedrock first so
    the ledger looks like a fresh start plus the admin's work. Returns the provider row."""
    from app.core.db import SessionLocal

    own = db is None
    db = db or SessionLocal()
    try:
        model_registry.ensure_seeded(db, actor=actor)
        provider = model_registry.create_provider(db, {
            "name": SILICONFLOW, "kind": mc.PROVIDER_OPENAI_COMPATIBLE,
            "label": SILICONFLOW_PROVIDER["label"], "base_url": SILICONFLOW_BASE_URL,
            "credential_ssm_parameter": SILICONFLOW_SSM, "enabled": enabled,
        }, actor)
        for api_model_name, traits in mc.parse_provider_models(spec):
            overrides: dict[str, Any] = {
                "tools": bool(traits.get("tools", True)),
                "streaming": bool(traits.get("streaming", True)),
                "vision": bool(traits.get("vision", False)),
            }
            if "max" in traits:
                overrides["default_max_tokens"] = int(traits["max"])
            if "judge" in traits:
                overrides["default_judge_max_tokens"] = int(traits["judge"])
            model_registry.create_model(
                db, provider, {"api_model_name": api_model_name, "overrides": overrides}, actor)
        db.refresh(provider)
        return provider
    finally:
        if own:
            db.close()
