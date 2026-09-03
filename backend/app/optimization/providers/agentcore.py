"""The built-in AgentCore recommendation job as a *listed* provider.

Discovery-only: ``stage_recommend`` keeps calling the ``StartRecommendation``
wrappers directly so the default path stays byte-identical (same AWS calls, same
artifact keys, no ``provider`` attribution). This object exists so the console
learns the id, label and capabilities from one endpoint for every provider.
"""

from __future__ import annotations

from app.optimization.providers import registry
from app.optimization.providers.base import (
    ConverseFn,
    OptimizeRequest,
    OptimizeResult,
    Progress,
)


class AgentCoreProvider:
    id = registry.DEFAULT_PROVIDER
    label = "AgentCore recommendation (StartRecommendation)"
    requires_source = False
    supports = ("system_prompt", "tool_descriptions")

    def models(self) -> list[dict[str, str]]:
        return []  # the job picks its own judge; no model choice is exposed

    def default_model_id(self) -> str | None:
        return None

    def optimize(
        self,
        req: OptimizeRequest,
        progress: Progress,
        converse: ConverseFn | None = None,
    ) -> OptimizeResult:
        raise NotImplementedError(
            "the agentcore provider runs through stage_recommend's native path"
        )


registry.register_provider(AgentCoreProvider())
