"""``Builtin.ContextRelevance`` — a Launchpad-only judge (N17-obs403).

There is no strands twin and no Global template: Global Evaluate answers "Builtin evaluator
… does not exist" for this id. The judge reads the turn's history with the tool results (the
free ``OutputEvaluator`` rubric it replaces saw only the input and the answer, so it guessed
at the context) and answers on a fixed three-label scale that the structured output enforces
(the rubric emitted RELEVANT / RELEVANCE / fully_relevant / FULLY_RELEVANT).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, cast

from pydantic import BaseModel, Field
from strands import Agent
from strands.models import Model
from strands_evals.evaluators import Evaluator
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput
from strands_evals.types.trace import EvaluationLevel

from app.services.strands_eval import global_templates


class ContextRelevanceScore(StrEnum):
    """The fixed Launchpad scale of ``Builtin.ContextRelevance`` (N17-obs403)."""

    NOT_RELEVANT = "Not Relevant"
    PARTIALLY_RELEVANT = "Partially Relevant"
    FULLY_RELEVANT = "Fully Relevant"


class ContextRelevanceRating(BaseModel):
    """Structured output for the context-relevance judge."""

    reasoning: str = Field(description="Step by step reasoning to derive the final score")
    score: ContextRelevanceScore = Field(description="Categorical context-relevance rating")


CONTEXT_RELEVANCE_VALUES = {
    ContextRelevanceScore.NOT_RELEVANT: 0.0,
    ContextRelevanceScore.PARTIALLY_RELEVANT: 0.5,
    ContextRelevanceScore.FULLY_RELEVANT: 1.0,
}


class ContextRelevanceEvaluator(Evaluator[Any, Any]):
    """``Builtin.ContextRelevance``: the last turn's history (with tool results) and answer,
    judged on :class:`ContextRelevanceScore` with
    ``global_templates.CONTEXT_RELEVANCE`` (N17-obs403)."""

    evaluation_level = EvaluationLevel.TRACE_LEVEL

    def __init__(self, *, model: Model, name: str,
                 system_prompt: str = global_templates.CONTEXT_RELEVANCE) -> None:
        super().__init__(name=name)
        self.model = model
        self.system_prompt = system_prompt

    def _output(self, rating: ContextRelevanceRating) -> list[EvaluationOutput]:
        value = CONTEXT_RELEVANCE_VALUES[rating.score]
        return [EvaluationOutput(score=value, test_pass=value >= 0.5,
                                 reason=rating.reasoning, label=rating.score.value)]

    def _prompt(self, evaluation_case: EvaluationData) -> str:
        return self._format_trace_level_prompt(self._get_last_turn(evaluation_case))

    def evaluate(self, evaluation_case: EvaluationData) -> list[EvaluationOutput]:
        agent = Agent(model=self.model, system_prompt=self.system_prompt, callback_handler=None)
        result = agent(self._prompt(evaluation_case),
                       structured_output_model=ContextRelevanceRating)
        return self._output(cast(ContextRelevanceRating, result.structured_output))

    async def evaluate_async(self, evaluation_case: EvaluationData) -> list[EvaluationOutput]:
        agent = Agent(model=self.model, system_prompt=self.system_prompt, callback_handler=None)
        result = await agent.invoke_async(self._prompt(evaluation_case),
                                          structured_output_model=ContextRelevanceRating)
        return self._output(cast(ContextRelevanceRating, result.structured_output))
