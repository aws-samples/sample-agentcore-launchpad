"""Provider registry — concrete providers register themselves on import
(``providers/__init__.py`` imports them for that side effect, the same pattern
``deployer/pipeline.register_method`` uses)."""

from __future__ import annotations

from typing import Any

from app.optimization.providers.base import PromptOptimizationProvider
from app.schemas.agent import model_available

# CN (N12, CA-1b): the AgentCore recommendation job is gone; both self-built providers
# run on the evaluation worker. The router Literal mirrors this tuple.
DEFAULT_PROVIDER = "gepa_lite"
PROVIDER_IDS: tuple[str, ...] = ("gepa_lite", "harness_optimizer")

_PROVIDERS: dict[str, PromptOptimizationProvider] = {}


def register_provider(provider: PromptOptimizationProvider) -> None:
    _PROVIDERS[provider.id] = provider


def get_provider(provider_id: str | None) -> PromptOptimizationProvider:
    pid = provider_id or DEFAULT_PROVIDER
    try:
        return _PROVIDERS[pid]
    except KeyError:
        raise ValueError(f"unknown recommendation provider '{pid}'") from None


def provider_enabled(provider: PromptOptimizationProvider) -> bool:
    return bool(getattr(provider, "enabled", True))


def takes_target(provider: PromptOptimizationProvider) -> bool:
    """N17-obs453: whether the provider optimises toward a target evaluator."""
    return bool(getattr(provider, "target_evaluator", False))


def list_providers() -> list[PromptOptimizationProvider]:
    """In :data:`PROVIDER_IDS` order (the default first), then registration order."""
    order = {pid: i for i, pid in enumerate(PROVIDER_IDS)}
    return sorted(_PROVIDERS.values(), key=lambda p: (order.get(p.id, len(order)), p.id))


def describe_providers() -> list[dict[str, Any]]:
    """The console's discovery payload (no AWS call). ``enabled`` lets the picker grey
    out a provider the workspace has switched off (``harness_optimizer`` by default)."""
    return [
        {
            "id": p.id,
            "label": p.label,
            "requires_source": p.requires_source,
            "supports": list(p.supports),
            # CN additive (N-R6-obs1): `available` = the model's provider is configured
            "models": [{**m, "available": model_available(str(m.get("model_id", "")))}
                       for m in p.models()],
            "default_model_id": p.default_model_id(),
            "enabled": provider_enabled(p),
            # N17-obs453 (additive): the provider optimises toward one target evaluator
            "target_evaluator": takes_target(p),
        }
        for p in list_providers()
    ]


# Labels for the ids the settings default list carries; anything else is shown
# as its id. Mirrors frontend/src/lib/models.ts wording.
_MODEL_LABELS = {
    "global.zai.glm-5.3": "GLM 5.3 (global)",
    "global.moonshotai.kimi-k3": "Kimi K3 (global)",
}


def label_for_model_id(model_id: str) -> str:
    return _MODEL_LABELS.get(model_id, model_id)
