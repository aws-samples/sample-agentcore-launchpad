"""Provider registry — concrete providers register themselves on import
(``providers/__init__.py`` imports them for that side effect, the same pattern
``deployer/pipeline.register_method`` uses)."""

from __future__ import annotations

from typing import Any

from app.optimization.providers.base import PromptOptimizationProvider

DEFAULT_PROVIDER = "agentcore"
# Static so the router can spell the request Literal; a test pins it to the
# registered set.
PROVIDER_IDS: tuple[str, ...] = ("agentcore", "gepa_lite")

_PROVIDERS: dict[str, PromptOptimizationProvider] = {}


def register_provider(provider: PromptOptimizationProvider) -> None:
    _PROVIDERS[provider.id] = provider


def get_provider(provider_id: str | None) -> PromptOptimizationProvider:
    pid = provider_id or DEFAULT_PROVIDER
    try:
        return _PROVIDERS[pid]
    except KeyError:
        raise ValueError(f"unknown recommendation provider '{pid}'") from None


def list_providers() -> list[PromptOptimizationProvider]:
    """``agentcore`` first, then registration order."""
    return sorted(_PROVIDERS.values(), key=lambda p: (p.id != DEFAULT_PROVIDER,))


def describe_providers() -> list[dict[str, Any]]:
    """The console's discovery payload (no AWS call)."""
    return [
        {
            "id": p.id,
            "label": p.label,
            "requires_source": p.requires_source,
            "supports": list(p.supports),
            "models": p.models(),
            "default_model_id": p.default_model_id(),
        }
        for p in list_providers()
    ]


# Labels for the ids the settings default list carries; anything else is shown
# as its id. Mirrors frontend/src/lib/models.ts wording.
_MODEL_LABELS = {
    "global.anthropic.claude-sonnet-5": "Claude Sonnet 5 (global)",
    "global.anthropic.claude-opus-5": "Claude Opus 5 (global)",
    "global.anthropic.claude-sonnet-4-6": "Claude Sonnet 4.6 (global)",
    "us.openai.gpt-5.6-sol": "GPT-5.6 Sol (us)",
    "us.amazon.nova-2-lite-v1:0": "Nova 2 Lite (us)",
}


def label_for_model_id(model_id: str) -> str:
    return _MODEL_LABELS.get(model_id, model_id)
