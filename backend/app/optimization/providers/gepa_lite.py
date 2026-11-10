"""``gepa_lite`` — one GEPA-style reflective round, run as a ``recommend`` job on the
evaluation worker (N12 E2-4).

The reflection itself lives in the pure library ``services/strands_eval/jobs_recommend.py``
(inlined into the worker). This module is the backend-side adapter: it registers the
provider, turns the backend's evidence dataclasses into the job spec (``build_spec``),
and offers the same reflection in-process (``optimize``) for the hermetic suite — with
the reflection model built only by
``aws_clients.global_bedrock_model`` (lead ruling: no regional ``bedrock-runtime``).
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
    SessionEvidence,
    evidence_dict,
    stats_dict,
)
from app.services import aws_clients
from app.services.strands_eval import jobs_recommend as lib

PROVIDER_ID = lib.PROVIDER_ID
REFLECTION_SYSTEM = lib.REFLECTION_SYSTEM
COMPONENTS_TEXT = lib.COMPONENTS_TEXT
COMPRESS_SYSTEM = lib.COMPRESS_SYSTEM
RETRY_SUFFIX = lib.RETRY_SUFFIX
TRUNCATED_SUFFIX = lib.TRUNCATED_SUFFIX
TOOL_DESC_MAX_CHARS = lib.TOOL_DESC_MAX_CHARS
MAX_TOKENS_CEILING = lib.MAX_TOKENS_CEILING
EXPLANATION_MAX = lib.EXPLANATION_MAX
parse_reflection = lib.parse_reflection
describe_unparseable = lib.describe_unparseable
validate_tool_descriptions = lib.validate_tool_descriptions


def render_reflective_dataset(
    evidence: list[SessionEvidence],
    own_tools: list[str] | None = None,
    with_tools: bool = False,
    known_tool_names: list[str] | None = None,
) -> str:
    return lib.render_reflective_dataset(
        [evidence_dict(ev) for ev in evidence], own_tools=own_tools, with_tools=with_tools,
        known_tool_names=known_tool_names,
    )


def build_user_message(req: OptimizeRequest) -> str:
    return lib.build_user_message(
        current_prompt=req.current_prompt, evidence=[evidence_dict(ev) for ev in req.evidence],
        stats=stats_dict(req.stats), tools=dict(req.tools), components=tuple(req.components),
        extra_feedback=list(req.extra_feedback),
    )


class GepaLiteProvider:
    id = PROVIDER_ID
    label = "GEPA-lite · single reflective round (evaluation worker)"
    requires_source = True
    supports = ("system_prompt", "tool_descriptions")
    job_type = lib.JOB_TYPE
    target_evaluator = False  # N17-obs453: it reads every evaluator record of the run

    @property
    def enabled(self) -> bool:
        return True

    def models(self) -> list[dict[str, str]]:
        return [
            {"model_id": m, "label": registry.label_for_model_id(m)}
            for m in get_settings().prompt_opt_models
        ]

    def default_model_id(self) -> str | None:
        return get_settings().prompt_opt_default_model_id

    def build_spec(self, req: OptimizeRequest) -> dict[str, Any]:
        """The ``recommend`` job spec — everything the worker needs, as plain JSON."""
        return {
            "provider": self.id,
            "model_id": req.model_id,
            "current_prompt": req.current_prompt,
            "components": list(req.components),
            "tools": dict(req.tools),
            "evidence": [evidence_dict(ev) for ev in req.evidence],
            "stats": stats_dict(req.stats),
            "extra_feedback": list(req.extra_feedback),
            "max_chars": int(req.max_chars),
            "max_tokens": int(req.max_tokens),
            "max_tokens_cap": int(req.max_tokens_cap),  # CN additive (N-SCN-bug2)
            "trace_source": dict(req.trace_source),
        }

    def optimize(
        self,
        req: OptimizeRequest,
        progress: Progress,
        converse: ConverseFn | None = None,
    ) -> OptimizeResult:
        """The same reflection, in-process (the hermetic suite)."""
        fn = converse or lib.model_converse(
            aws_clients.global_bedrock_model(
                req.model_id, region=get_settings().model_region, max_tokens=req.max_tokens,
            )
        )
        out = lib.reflect(
            converse=fn, model_id=req.model_id, current_prompt=req.current_prompt,
            evidence=[evidence_dict(ev) for ev in req.evidence], stats=stats_dict(req.stats),
            tools=dict(req.tools), components=tuple(req.components),
            extra_feedback=list(req.extra_feedback), max_chars=req.max_chars,
            max_tokens=req.max_tokens, progress=progress, max_tokens_cap=req.max_tokens_cap,
        )
        return OptimizeResult.from_dict(out)


registry.register_provider(GepaLiteProvider())
