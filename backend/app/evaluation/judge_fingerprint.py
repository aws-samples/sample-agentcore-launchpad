"""The judge fingerprint — what a calibration report vouches for (Round 3 R3-e).

A digest of every LLM builtin judge's prompts as :func:`evaluators.build` constructs them
(the same texts ``tests/evaluation/test_global_templates.PINNED`` pins one by one), the
code-default judge model, the judge temperature and the judge models a calibration covers.
A change to any of them — a template under ``global_templates``, a strands-evals bump, a
judge model id — changes the fingerprint, and ``make verify`` then asks for a calibration
report recorded at the new one (``tests/evaluation/test_judge_calibration_gate.py``).

Hub side only (not in the eval worker zip): the gate and the workbench read it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncGenerator
from typing import Any

from strands.models import Model

from app.core.config import Settings
from app.evaluation.judge_budget import JUDGE_TEMPERATURE
from app.services.strands_eval import evaluators

# The judges a calibration report must cover (the CN catalog's two chat models)
CALIBRATED_JUDGES: tuple[str, ...] = ("global.zai.glm-5.3", "global.moonshotai.kimi-k3")
# A report vouches for a fingerprint only when it judged the full pair set this many times
MIN_REPORT_REPS = 3
PROMPT_ATTRS = ("system_prompt", "reference_system_prompt", "assertion_system_prompt", "rubric")


class _PromptOnly(Model):
    """A model that is never called: :func:`evaluators.build` needs one to construct a judge."""

    def update_config(self, **model_config: Any) -> None:  # pragma: no cover - never called
        raise NotImplementedError

    def get_config(self) -> dict[str, Any]:  # pragma: no cover - never called
        return {}

    async def structured_output(self, output_model: Any, prompt: Any, system_prompt: Any = None,
                                **kwargs: Any) -> AsyncGenerator[Any, None]:  # pragma: no cover
        raise NotImplementedError
        yield

    async def stream(self, messages: Any, tool_specs: Any = None, system_prompt: Any = None,
                     **kwargs: Any) -> AsyncGenerator[Any, None]:  # pragma: no cover
        raise NotImplementedError
        yield


def judge_prompts() -> dict[str, dict[str, str]]:
    """``{evaluator_id: {attr: text}}`` of every LLM builtin judge as built."""
    model = _PromptOnly()
    out: dict[str, dict[str, str]] = {}
    for evaluator_id in sorted(evaluators.BUILTIN_LEVELS):
        built = evaluators.build({"id": evaluator_id}, model)
        out[evaluator_id] = {attr: getattr(built, attr) for attr in PROMPT_ATTRS
                             if isinstance(getattr(built, attr, None), str)
                             and getattr(built, attr)}
    return out


def fingerprint_inputs() -> dict[str, Any]:
    """Everything the fingerprint covers, in a JSON-stable shape."""
    return {
        "prompts": judge_prompts(),
        "default_judge": Settings.model_fields["eval_judge_model_id"].default,
        "judge_temperature": JUDGE_TEMPERATURE,
        "calibrated_judges": list(CALIBRATED_JUDGES),
    }


def fingerprint() -> str:
    """16 hex characters of sha256 over :func:`fingerprint_inputs`."""
    blob = json.dumps(fingerprint_inputs(), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
