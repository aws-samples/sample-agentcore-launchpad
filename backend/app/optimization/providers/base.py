"""Provider contract for the recommendation generators (N12 E2-4).

A provider turns *evidence* (scored sessions of one completed evaluation run, read from
``eval_results`` and joined with each session's transcript) plus the current system
prompt / tool descriptions into one ``OptimizeResult``. In CN every provider runs on the
evaluation worker as a job: ``build_spec`` turns an ``OptimizeRequest`` into the job
spec, ``job_type`` names the worker handler (``recommend`` → ``strands_eval/jobs_recommend``,
``optimize`` → ``strands_eval/jobs_optimize``). ``optimize`` is the in-process twin of the
reflection (SCORE-NOW-sized use: the hermetic suite).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from app.services.workspace import WorkspaceContext

Progress = Callable[[str], None]

# One Converse-shaped text completion: (model_id, system, messages, max_tokens)
# → (text, usage). Injected in tests; `jobs_recommend.model_converse(model)` in production.
ConverseFn = Callable[[str, str, list[dict[str, str]], int], tuple[str, dict[str, Any]]]


@dataclass
class EvaluatorRecord:
    """One ``eval_results`` row of a run, as the optimizer sees it."""

    evaluator_id: str
    score: float | None
    label: str | None
    explanation: str | None
    level: str | None = None
    error: str | None = None


@dataclass
class SessionEvidence:
    session_id: str
    # [{"role": "user"|"assistant", "text": ...}] plus, when tool evidence was
    # collected, {"role": "tool_call", "id", "name", "input"} /
    # {"role": "tool_result", "id", "name", "status", "text"} in conversation order
    turns: list[dict[str, Any]]
    records: list[EvaluatorRecord]
    # polarity-normalised mean of the scored records (+1 = better), None when
    # the session carries only errored / unscored records
    mean_score: float | None


@dataclass
class EvidenceStats:
    sessions_scored: int = 0  # sessions with ≥1 numeric score
    sessions_selected: int = 0  # after the sampling policy
    sessions_with_transcript: int = 0
    records: int = 0  # result records read (all sessions)
    # tool evidence (only when collected)
    sessions_with_tool_calls: int = 0
    tool_calls_seen: int = 0
    # {tool name: {"calls": n, "errors": n, "description_seen": str|None}} —
    # what the traces show the model actually called / saw (spans + content logs)
    observed_tools: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class OptimizeRequest:
    current_prompt: str
    agent: dict[str, Any]  # the agent_meta snapshot (id, name, system_prompt, tools)
    trace_source: dict[str, Any]
    evidence: list[SessionEvidence]
    stats: EvidenceStats
    model_id: str
    workspace: WorkspaceContext
    max_chars: int
    extra_feedback: list[str] = field(default_factory=list)  # e.g. insight clusters
    max_tokens: int = 4096
    # N-SCN-bug2: the output-limit retries double max_tokens up to here (CN additive)
    max_tokens_cap: int = 16384
    # which recommendation types this call must produce; anything not listed is
    # frozen (the provider must not propose changes to it)
    components: tuple[str, ...] = ("system_prompt",)
    # the agent's OWN tools (discovered from its spec/code) → current description;
    # the only tools whose descriptions a provider may rewrite
    tools: dict[str, str] = field(default_factory=dict)


@dataclass
class OptimizeResult:
    status: str  # "COMPLETED" | "FAILED" — mirrors the Global job status vocabulary
    recommended_prompt: str | None = None
    explanation: str = ""
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    # tool-description component (only when requested): validated
    # {name: new description} — {} means "no changes suggested"; tool_status
    # mirrors the artifact vocabulary: COMPLETED | error | no-tools | no-tool-calls
    tool_descriptions: dict[str, str] | None = None
    tool_status: str | None = None
    tool_error: str | None = None
    tool_explanation: str = ""

    @classmethod
    def failed(cls, error: str, **meta: Any) -> OptimizeResult:
        return cls(status="FAILED", error=error[:300], meta=meta)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OptimizeResult:
        """The worker's ``done.json.summary.recommendation`` / ``recommendation.json``."""
        return cls(
            status=str(data.get("status") or "FAILED"),
            recommended_prompt=data.get("recommended_prompt"),
            explanation=str(data.get("explanation") or ""),
            error=data.get("error"),
            meta=dict(data.get("meta") or {}),
            tool_descriptions=data.get("tool_descriptions"),
            tool_status=data.get("tool_status"),
            tool_error=data.get("tool_error"),
            tool_explanation=str(data.get("tool_explanation") or ""),
        )


def evidence_dict(ev: SessionEvidence) -> dict[str, Any]:
    """The plain-dict shape the worker-side reflection reads."""
    return {
        "session_id": ev.session_id,
        "turns": list(ev.turns),
        "records": [asdict(r) for r in ev.records],
        "mean_score": ev.mean_score,
    }


def stats_dict(stats: EvidenceStats) -> dict[str, Any]:
    return asdict(stats)


class PromptOptimizationProvider(Protocol):
    id: str
    label: str
    requires_source: bool  # needs a pinned evaluation run (scored evidence)
    supports: tuple[str, ...]  # recommendation types this provider generates
    job_type: str  # the worker job kind this provider runs as
    # N17-obs453: it optimises toward ONE target evaluator (the recommendation's `evaluator`);
    # False = it reflects on every evaluator record of the run and takes no target
    target_evaluator: bool

    @property
    def enabled(self) -> bool: ...

    def models(self) -> list[dict[str, str]]: ...

    def default_model_id(self) -> str | None: ...

    def build_spec(self, req: OptimizeRequest) -> dict[str, Any]: ...

    def optimize(
        self,
        req: OptimizeRequest,
        progress: Progress,
        converse: ConverseFn | None = None,
    ) -> OptimizeResult: ...
