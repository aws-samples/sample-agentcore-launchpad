"""``harness_optimizer`` — Strands harness-optimizer contrastive reflection, run as an
``optimize`` job on the evaluation worker (N12 E2-5; plan §B.5, risk R-4).

Thin submitter: the backend pre-collects the evidence, the dataset prompts and the target
runtime ARN; the worker-side ``strands_eval/jobs_optimize.py`` runs the ``Trainer`` with a
rollout engine that writes a temporary config-bundle version per epoch and invokes the
target runtime with the bundle pointer. **Disabled by default**
(``Settings.prompt_opt_harness_optimizer_enabled``): the optimizer's reflection agent
carries a ``shell`` tool (strands_tools) to read the trace files it writes — acceptable on
an isolated worker microVM, but an operator opt-in. ``system_prompt`` only (PyPI 0.0.1
ships ``SystemPromptFormula`` / ``ContextExpansionFormula`` only).
"""

from __future__ import annotations

from typing import Any

from app.core.config import get_settings
from app.optimization.providers import registry
from app.optimization.providers.base import (
    ConverseFn,
    OptimizeRequest,
    OptimizeResult,
    Progress,
    evidence_dict,
    stats_dict,
)

PROVIDER_ID = "harness_optimizer"
JOB_TYPE = "optimize"


class HarnessOptimizerProvider:
    id = PROVIDER_ID
    label = "Strands harness-optimizer · contrastive reflection (evaluation worker)"
    requires_source = True
    supports = ("system_prompt",)
    job_type = JOB_TYPE
    target_evaluator = True  # N17-obs453: the `optimize` job is rewarded by `evaluator_def`

    @property
    def enabled(self) -> bool:
        return bool(get_settings().prompt_opt_harness_optimizer_enabled)

    def models(self) -> list[dict[str, str]]:
        return [
            {"model_id": m, "label": registry.label_for_model_id(m)}
            for m in get_settings().prompt_opt_models
        ]

    def default_model_id(self) -> str | None:
        return get_settings().prompt_opt_default_model_id

    def build_spec(self, req: OptimizeRequest) -> dict[str, Any]:
        """The ``optimize`` job spec. ``dataset_prompts`` / ``target`` / ``bundle`` are
        filled by ``recommendations`` from the run (E2-5); the evidence seeds the first
        reflection like ``gepa_lite``."""
        settings = get_settings()
        return {
            "provider": self.id,
            "model_id": req.model_id,
            "current_prompt": req.current_prompt,
            "components": ["system_prompt"],
            "evidence": [evidence_dict(ev) for ev in req.evidence],
            "stats": stats_dict(req.stats),
            "extra_feedback": list(req.extra_feedback),
            "max_chars": int(req.max_chars),
            "max_tokens": int(req.max_tokens),
            "max_tokens_cap": int(req.max_tokens_cap),  # CN additive (N-SCN-bug2)
            "epochs": int(settings.prompt_opt_epochs),
            "trace_source": dict(req.trace_source),
            "agent": dict(req.agent),
        }

    def optimize(
        self,
        req: OptimizeRequest,
        progress: Progress,
        converse: ConverseFn | None = None,
    ) -> OptimizeResult:
        raise NotImplementedError(
            "harness_optimizer runs only as an `optimize` job on the evaluation worker "
            "(strands_eval/jobs_optimize); use recommendations.start"
        )


registry.register_provider(HarnessOptimizerProvider())
