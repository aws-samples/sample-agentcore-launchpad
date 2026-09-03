"""Provider contract for the RECOMMEND stage's system-prompt generator.

A provider turns *evidence* (scored sessions from one completed batch
evaluation) plus the current system prompt into one recommended prompt. It
writes nothing itself: ``optimization.service.stage_recommend`` maps the
``OptimizeResult`` onto the same ``recommend`` artifact keys the AgentCore
recommendation job fills, so every downstream stage (accept → bundles → …)
is provider-agnostic apart from attribution.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from app.services.workspace import WorkspaceContext

Progress = Callable[[str], None]

# One Converse-shaped text completion: (model_id, system, messages, max_tokens)
# → (text, usage). Injected in tests; ``bedrock_lm.converse_text`` in production.
ConverseFn = Callable[[str, str, list[dict[str, str]], int], tuple[str, dict[str, Any]]]


@dataclass
class EvaluatorRecord:
    """One ``gen_ai.evaluation.result`` record of a batch evaluation."""

    evaluator_id: str
    score: float | None
    label: str | None
    explanation: str | None
    level: str | None = None
    error: str | None = None


@dataclass
class SessionEvidence:
    session_id: str
    turns: list[dict[str, str]]  # [{"role": "user"|"assistant", "text": ...}]
    records: list[EvaluatorRecord]
    # polarity-normalised mean of the scored records (+1 = better), None when
    # the session carries only errored / unscored records
    mean_score: float | None


@dataclass
class EvidenceStats:
    sessions_scored: int = 0  # sessions with ≥1 numeric score in the stream
    sessions_selected: int = 0  # after the sampling policy
    sessions_with_transcript: int = 0
    records: int = 0  # result records read from the stream (all sessions)


@dataclass
class OptimizeRequest:
    current_prompt: str
    agent: dict[str, Any]  # the experiment's agent_meta snapshot
    trace_source: dict[str, Any]
    evidence: list[SessionEvidence]
    stats: EvidenceStats
    model_id: str
    workspace: WorkspaceContext
    max_chars: int
    extra_feedback: list[str] = field(default_factory=list)  # e.g. insight clusters
    max_tokens: int = 4096


@dataclass
class OptimizeResult:
    status: str  # "COMPLETED" | "FAILED" — mirrors the AWS job status vocabulary
    recommended_prompt: str | None = None
    explanation: str = ""
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def failed(cls, error: str, **meta: Any) -> OptimizeResult:
        return cls(status="FAILED", error=error[:300], meta=meta)


class PromptOptimizationProvider(Protocol):
    id: str
    label: str
    requires_source: bool  # needs a pinned evaluation run (scored evidence)
    supports: tuple[str, ...]  # recommendation types this provider generates

    def models(self) -> list[dict[str, str]]: ...

    def default_model_id(self) -> str | None: ...

    def optimize(
        self,
        req: OptimizeRequest,
        progress: Progress,
        converse: ConverseFn | None = None,
    ) -> OptimizeResult: ...
