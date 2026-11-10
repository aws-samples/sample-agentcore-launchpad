"""Insights on strands ``detectors`` + a judge summariser (plan §B.4) — the baseline
``EvalRun.insights`` trees ``{failures, userIntents, executionSummaries}`` whose clusters the
restored ``InsightsPanel.tsx`` / ``InsightClusters.tsx`` read
(``name/category, description, percentage, affectedSessionCount, affectedSessions[{sessionId,
userMessages, approachTaken, finalOutcome}], subCategories[{name, rootCauses[{name,
recommendation}]}]``).

Pure library (inlined into the worker): the judge is an injected ``strands.models.Model``.
N-SCN-bug2: the detectors and the summariser run through ``model_calls.call_with_budget`` —
a call that hits the judge's output limit is re-run with the budget doubled (the one shared
judge object is raised for that call only, ``model_budget``) up to ``budget_cap``, at most
twice; the session's error then names the budget.
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Callable, Iterator
from typing import Any

from pydantic import BaseModel, Field
from strands import Agent
from strands_evals.detectors import diagnose_session
from strands_evals.detectors.constants import CONFIDENCE_MAP
from strands_evals.types.detector import ConfidenceLevel, FailureDetectionStructuredOutput

from app.services.strands_eval import model_calls

log = logging.getLogger(__name__)

FAILURE_ANALYSIS = "Builtin.Insight.FailureAnalysis"
USER_INTENT = "Builtin.Insight.UserIntent"
EXECUTION_SUMMARY = "Builtin.Insight.ExecutionSummary"
INSIGHT_KINDS = (FAILURE_ANALYSIS, USER_INTENT, EXECUTION_SUMMARY)

SUMMARISER_SYSTEM_PROMPT = (
    "You summarise one conversation between a user and an AI agent for an operations "
    "dashboard. Answer with the structured fields only: a short, reusable intent label "
    "(2-5 words, generic enough to group similar conversations), one sentence describing "
    "the user's intent, a short label for the approach the agent took, and one sentence "
    "describing what the agent did and how it ended."
)

# N17-obs448: the labels and sentences are written in the dominant language of the analysed
# sessions (`dominant_language`) — the same language for every session of the run, so the
# same intent gets the same label. Only the OUTPUT language: what is summarised and how the
# labels are grouped is unchanged.
OUTPUT_LANGUAGES = {"zh": "Simplified Chinese (简体中文)", "en": "English"}
DEFAULT_OUTPUT_LANGUAGE = "en"
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_WORD = re.compile(r"[A-Za-z]{2,}")


def summariser_prompt(language: str = DEFAULT_OUTPUT_LANGUAGE) -> str:
    """The summariser's system prompt with its output language (an unknown code → English)."""
    name = OUTPUT_LANGUAGES.get(language, OUTPUT_LANGUAGES[DEFAULT_OUTPUT_LANGUAGE])
    return (f"{SUMMARISER_SYSTEM_PROMPT} Write every label and sentence in {name}, whatever "
            "language the tool calls or system text are in.")


def dominant_language(texts: list[str], default: str = DEFAULT_OUTPUT_LANGUAGE) -> str:
    """``zh`` or ``en`` — whichever the user / agent text of the sessions is mostly in: Han
    characters against Latin words (one Han character ≈ one word); ``default`` when there is
    no text to judge by."""
    han = sum(len(_HAN.findall(t or "")) for t in texts)
    latin = sum(len(_LATIN_WORD.findall(t or "")) for t in texts)
    if not han and not latin:
        return default
    return "zh" if han > latin else "en"


class SessionSummary(BaseModel):
    intent_label: str = Field(description="2-5 word generic label of the user's intent")
    intent: str = Field(description="one sentence: what the user wanted")
    summary_label: str = Field(description="2-5 word label of the agent's approach")
    summary: str = Field(description="one sentence: what the agent did and the outcome")


def _label_key(label: str) -> str:
    # N17-obs448: Unicode word characters, so a Chinese label is its own key (the old
    # `[^a-z0-9]` folded every non-ASCII label into "other"); ASCII labels key as before
    return re.sub(r"[\W_]+", " ", str(label or "").lower()).strip() or "other"


def _conversation_text(session: Any) -> str:
    """The user prompts and agent answers of a session — what its language is judged by
    (tool calls / arguments are not the conversation's language)."""
    parts: list[str] = []
    for trace in getattr(session, "traces", []) or []:
        for span in getattr(trace, "spans", []) or []:
            for field in ("user_prompt", "agent_response"):
                value = getattr(span, field, None)
                if value:
                    parts.append(str(value))
    return "\n".join(parts)


def _transcript(session: Any, limit: int = 6000) -> tuple[str, str, str]:
    """(rendered transcript, first user prompt, last agent response) of a session."""
    lines: list[str] = []
    first_prompt = ""
    last_response = ""
    for trace in getattr(session, "traces", []) or []:
        for span in getattr(trace, "spans", []) or []:
            prompt = getattr(span, "user_prompt", None)
            response = getattr(span, "agent_response", None)
            tool_call = getattr(span, "tool_call", None)
            if prompt:
                first_prompt = first_prompt or str(prompt)
                lines.append(f"USER: {prompt}")
            if tool_call is not None:
                lines.append(f"TOOL: {getattr(tool_call, 'name', '')}"
                             f"({getattr(tool_call, 'arguments', '')})")
            if response:
                last_response = str(response)
                lines.append(f"AGENT: {response}")
    text = "\n".join(lines)
    return text[:limit], first_prompt, last_response


def summarize_session(session: Any, judge: Any,
                      language: str = DEFAULT_OUTPUT_LANGUAGE) -> SessionSummary:
    transcript, _prompt, _response = _transcript(session)
    agent = Agent(model=judge, system_prompt=summariser_prompt(language), callback_handler=None)
    result = agent(f"Conversation:\n{transcript}\n\nSummarise it.",
                   structured_output_model=SessionSummary)
    summary = getattr(result, "structured_output", None)
    if not isinstance(summary, SessionSummary):
        raise RuntimeError("the summariser returned no structured output")
    return summary


# ── N17-obs449: failure detection that cannot silently read as "no failures" ──────────────
# strands-evals' detector calls the judge's raw `stream` in text mode and parses the text; a
# reply cut at max_tokens (nothing raises on that path) or one that does not parse is turned
# into an EMPTY failure list with only a log warning. Every text-mode call of one diagnosis is
# recorded here: a max_tokens stop raises `model_calls.OutputTruncated` (call_with_budget
# retries at a higher budget), an unparseable reply raises `FailureDetectionUnparseable` — after
# the retries either one is the session's error row, never "no failures". The taxonomy, the
# confidence threshold and the clustering are the library's / unchanged.
DETECTION_TRUNCATED = "failure_detection_truncated"
# The failure detector's starting output budget (bounded by the run's cap): it reasons over the
# whole taxonomy + session, and a reasoning model (GLM-5.3) spent the 4096 judge default inside
# its reasoning block on 40/40 live sessions — each such attempt pays the full prompt again, so
# the detector starts where the retry ladder would have ended. The summariser keeps the judge's
# own budget.
DETECTION_START_BUDGET = 16384
DETECTION_UNPARSEABLE = "failure_detection_unparseable"
DETECTION_EMPTY = "failure_detection_empty"  # end_turn with no text and no payload anywhere
DETECTION_FILTERED = "failure_detection_filtered"  # the provider's content filter / guardrail
DETECTION_CONTEXT = "failure_detection_context"  # the prompt exceeded the model's window
# Bedrock ConverseStream stop reasons that end a reply with no usable answer and that a larger
# budget cannot change — the session's error row, no retry
_FILTER_STOPS = frozenset({"content_filtered", "guardrail_intervened"})
_CONTEXT_STOPS = frozenset({"model_context_window_exceeded"})


class FailureDetectionUnparseable(RuntimeError):
    """The failure detector's reply is not the ``{"errors": [...]}`` JSON it asks for."""

    kind = DETECTION_UNPARSEABLE

    def __init__(self, text: str) -> None:
        head = " ".join(str(text or "").split())[:120]
        super().__init__(f"failure detection reply is not parseable JSON: {head!r}")


class FailureDetectionStopped(RuntimeError):
    """The detector's reply ended with no answer for a reason a larger budget cannot fix: an
    empty end_turn, the provider's content filter, or the context window."""

    def __init__(self, kind: str, stop: str | None, reasoning_chars: int) -> None:
        self.kind = kind
        super().__init__(f"failure detection reply ended with no answer (stop={stop}, "
                         f"reasoning_chars={reasoning_chars})")


def _detection_payload(text: str) -> FailureDetectionStructuredOutput | None:
    """The detector reply as the library would read it, or None when it does not parse."""
    from strands_evals.detectors.failure_detector import _extract_json

    try:
        return FailureDetectionStructuredOutput.model_validate_json(_extract_json(text))
    except Exception:  # noqa: BLE001 — any shape the library would drop
        return None


@contextlib.contextmanager
def _recording_text_calls(judge: Any) -> Iterator[list[dict[str, Any]]]:
    """Record ``{text, stop}`` of every text-mode (no tool) ``stream`` call made on ``judge``
    while the block runs; the structured-output calls (root-cause analysis) pass through —
    the Agent loop already raises on their max_tokens."""
    calls: list[dict[str, Any]] = []
    inner = getattr(judge, "stream", None)
    if inner is None:
        yield calls
        return

    async def recording(messages: Any, tool_specs: Any = None, *args: Any, **kwargs: Any):
        record: dict[str, Any] | None = (
            None if tool_specs else {"text": "", "reasoning_chars": 0, "stop": None})
        if record is not None:
            calls.append(record)
        async for event in inner(messages, tool_specs, *args, **kwargs):
            if record is not None and isinstance(event, dict):
                delta = (event.get("contentBlockDelta") or {}).get("delta") or {}
                if isinstance(delta.get("text"), str):
                    record["text"] += delta["text"]
                reasoning = delta.get("reasoningContent")
                if isinstance(reasoning, dict) and isinstance(reasoning.get("text"), str):
                    record["reasoning_chars"] += len(reasoning["text"])  # counted, never read
                stop = (event.get("messageStop") or {}).get("stopReason")
                if stop:
                    record["stop"] = str(stop)
            yield event

    had_own = "stream" in vars(judge) if hasattr(judge, "__dict__") else False
    try:
        judge.stream = recording
    except Exception:  # noqa: BLE001 — a model that refuses the hook is checked by nothing
        yield calls
        return
    try:
        yield calls
    finally:
        if had_own:
            judge.stream = inner
        else:
            with contextlib.suppress(Exception):
                del judge.stream


def _low_confidence_dropped(calls: list[dict[str, Any]], threshold: float) -> int:
    """Findings the detector reported below the threshold (the library drops them) — one per
    (location, category); a chunked detection's overlapping chunks count a finding once."""
    seen: set[tuple[str, str]] = set()
    for call in calls:
        payload = _detection_payload(call["text"])
        for err in payload.errors if payload else []:
            for category, conf in zip(err.category, err.confidence, strict=False):
                value = CONFIDENCE_MAP.get(conf.lower() if isinstance(conf, str) else "", 0.0)
                if value < threshold:
                    seen.add((str(err.location), str(category)))
    return len(seen)


def detect_session_failures(session: Any, judge: Any, confidence: ConfidenceLevel,
                            ) -> tuple[Any, int]:
    """``(diagnosis, low-confidence findings dropped)`` of one session — raising instead of an
    empty diagnosis when a detection reply has no answer.

    Per reply: ``max_tokens`` → ``OutputTruncated`` (the caller's budget ladder retries); a
    content-filter / guardrail or context-window stop → ``FailureDetectionStopped`` (no retry);
    an ``end_turn`` with no text → ``FailureDetectionStopped(empty)`` (the reasoning block is the
    model's private draft, never read as its answer); text that does not parse →
    ``FailureDetectionUnparseable``."""
    with _recording_text_calls(judge) as calls:
        diagnosis = diagnose_session(session, model=judge, confidence_threshold=confidence)
    for call in calls:
        # the stop reason is the one fact the library never logs (N17-obs449: 40/40 live
        # replies were EMPTY) — a reasoning model that spends its budget thinking stops at
        # max_tokens before its text block
        log.info("failure detection reply: stop=%s text_chars=%d reasoning_chars=%d "
                 "max_tokens=%s", call["stop"], len(call["text"]), call["reasoning_chars"],
                 model_calls.model_max_tokens(judge))
        stop = call["stop"]
        if stop == "max_tokens":
            raise model_calls.OutputTruncated(model_calls.model_max_tokens(judge) or 0,
                                              "failure detection")
        if stop in _FILTER_STOPS:
            raise FailureDetectionStopped(DETECTION_FILTERED, stop, call["reasoning_chars"])
        if stop in _CONTEXT_STOPS:
            raise FailureDetectionStopped(DETECTION_CONTEXT, stop, call["reasoning_chars"])
        if not call["text"].strip():
            raise FailureDetectionStopped(DETECTION_EMPTY, stop, call["reasoning_chars"])
        if _detection_payload(call["text"]) is None:
            raise FailureDetectionUnparseable(call["text"])
    return diagnosis, _low_confidence_dropped(calls, CONFIDENCE_MAP[confidence])


def _detection_error_kind(exc: BaseException) -> str | None:
    if isinstance(exc, FailureDetectionUnparseable | FailureDetectionStopped):
        return exc.kind
    if isinstance(exc, model_calls.JudgeBudgetExceeded | model_calls.OutputTruncated):
        return DETECTION_TRUNCATED
    return None


def _percentage(count: int, total: int) -> float:
    return round(100.0 * count / total, 1) if total else 0.0


def _failure_clusters(diagnoses: dict[str, Any], total: int) -> list[dict[str, Any]]:
    by_category: dict[str, dict[str, Any]] = {}
    for session_id, diagnosis in diagnoses.items():
        root_causes = {rc.failure_span_id: rc for rc in getattr(diagnosis, "root_causes", [])}
        for failure in getattr(diagnosis, "failures", []):
            categories = [str(c) for c in (failure.category or [])] or ["Unclassified"]
            evidence = " ".join(str(e) for e in (failure.evidence or []))[:400]
            rca = root_causes.get(failure.span_id)
            for category in categories:
                cluster = by_category.setdefault(category, {
                    "name": category, "category": category, "description": evidence,
                    "affectedSessions": [], "_sessions": set(), "_causes": {},
                })
                if session_id not in cluster["_sessions"]:
                    cluster["_sessions"].add(session_id)
                    cluster["affectedSessions"].append({"sessionId": session_id})
                if rca is not None:
                    fix_type = str(getattr(rca, "fix_type", "") or "OTHERS")
                    causes = cluster["_causes"].setdefault(fix_type, [])
                    cause = {"name": str(rca.root_cause_explanation or "")[:400],
                             "recommendation": str(rca.fix_recommendation or "")[:600]}
                    if cause not in causes:
                        causes.append(cause)
    clusters = []
    for cluster in by_category.values():
        sessions = cluster.pop("_sessions")
        causes = cluster.pop("_causes")
        cluster["affectedSessionCount"] = len(sessions)
        cluster["percentage"] = _percentage(len(sessions), total)
        cluster["subCategories"] = [
            {"name": fix_type, "rootCauses": root_causes}
            for fix_type, root_causes in causes.items()
        ]
        # CN-only convenience field (the UI ignores it): the first fix recommendation
        cluster["fix_recommendation"] = next(
            (c["recommendation"] for cs in causes.values() for c in cs if c["recommendation"]),
            None,
        )
        clusters.append(cluster)
    clusters.sort(key=lambda c: (-c["affectedSessionCount"], c["name"]))
    return clusters


def _summary_clusters(
    summaries: dict[str, tuple[SessionSummary, str, str]], total: int, *, kind: str
) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for session_id, (summary, first_prompt, last_response) in summaries.items():
        label = summary.intent_label if kind == USER_INTENT else summary.summary_label
        description = summary.intent if kind == USER_INTENT else summary.summary
        cluster = by_key.setdefault(_label_key(label), {
            "name": str(label).strip() or "Other", "description": description,
            "affectedSessions": [],
        })
        member: dict[str, Any] = {"sessionId": session_id}
        if kind == USER_INTENT:
            member["userMessages"] = [first_prompt] if first_prompt else []
        else:
            member["approachTaken"] = summary.summary
            member["finalOutcome"] = last_response[:400]
        cluster["affectedSessions"].append(member)
    clusters = []
    for index, cluster in enumerate(by_key.values(), 1):
        cluster["clusterId"] = index
        cluster["affectedSessionCount"] = len(cluster["affectedSessions"])
        cluster["percentage"] = _percentage(cluster["affectedSessionCount"], total)
        clusters.append(cluster)
    clusters.sort(key=lambda c: (-c["affectedSessionCount"], c["name"]))
    return clusters


def diagnose_sessions(
    provider: Any,
    session_ids: list[str],
    kinds: list[str],
    judge: Any,
    *,
    stop: Callable[[], bool] | None = None,
    progress: Callable[[str], None] | None = None,
    confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM,
    budget_cap: int = model_calls.DEFAULT_BUDGET_CAP,
    default_language: str = DEFAULT_OUTPUT_LANGUAGE,
    detection_judge: Any = None,
) -> dict[str, Any]:
    """The insight trees for ``kinds`` (any subset of :data:`INSIGHT_KINDS`) over the
    sessions the provider can read; a session that cannot be read or judged is skipped
    (its error is listed under ``errors``), never a crashed run. ``detection_judge`` (the
    ``failure_detection`` model role, N17-obs449) runs FailureAnalysis; ``judge`` everything
    else (and FailureAnalysis too when none is given)."""
    wanted = [k for k in INSIGHT_KINDS if k in (kinds or INSIGHT_KINDS)]
    detector = detection_judge if detection_judge is not None else judge

    def model_id_of(model: Any) -> str | None:
        return str((getattr(model, "get_config", dict)() or {}).get("model_id") or "") or None

    def budgeted(label: str, fn: Callable[[], Any], start: int | None = None,
                 model: Any = None) -> Any:
        model = judge if model is None else model
        budget = model_calls.model_max_tokens(model) or model_calls.DEFAULT_BUDGET

        def call(max_tokens: int) -> Any:
            with model_calls.model_budget(model, max_tokens):
                return fn()

        first = min(max(budget, start), budget_cap) if start else budget
        return model_calls.call_with_budget(call, budget=first, cap=budget_cap, label=label,
                                            model_id=model_id_of(model))

    diagnoses: dict[str, Any] = {}
    summaries: dict[str, tuple[SessionSummary, str, str]] = {}
    errors: list[dict[str, str]] = []
    detection = {"sessions": 0, "incomplete": 0, "lowConfidenceDropped": 0}
    total = len(session_ids)
    # read every session first: the summaries' language is the sessions' dominant one
    # (N17-obs448), the same for the whole run
    sessions: list[tuple[str, Any]] = []
    for session_id in session_ids:
        if stop and stop():
            break
        try:
            sessions.append((session_id, provider.session(session_id)))
        except Exception as exc:  # noqa: BLE001
            errors.append({"session_id": session_id, "error": f"{type(exc).__name__}: {exc}"[:300]})
    language = dominant_language([_conversation_text(s) for _sid, s in sessions],
                                 default=default_language)
    for index, (session_id, session) in enumerate(sessions, 1):
        if stop and stop():
            break
        if FAILURE_ANALYSIS in wanted:
            detection["sessions"] += 1
            try:
                diagnoses[session_id], dropped = budgeted(
                    "failure-analysis judge",
                    lambda _s=session: detect_session_failures(_s, detector, confidence),
                    start=DETECTION_START_BUDGET, model=detector,
                )
                detection["lowConfidenceDropped"] += dropped
            except Exception as exc:  # noqa: BLE001
                row = {"session_id": session_id,
                       "error": f"FailureAnalysis {type(exc).__name__}: {exc}"[:300]}
                kind = _detection_error_kind(exc)
                if kind:  # N17-obs449: detection did not complete — never "no failures"
                    row["kind"] = kind
                    detection["incomplete"] += 1
                errors.append(row)
        if USER_INTENT in wanted or EXECUTION_SUMMARY in wanted:
            try:
                _text, first_prompt, last_response = _transcript(session)
                summaries[session_id] = (
                    budgeted("summariser",
                             lambda _s=session: summarize_session(_s, judge, language)),
                    first_prompt, last_response,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append({"session_id": session_id,
                               "error": f"Summary {type(exc).__name__}: {exc}"[:300]})
        if progress:
            progress(f"analysing {index}/{total}")
    out: dict[str, Any] = {}
    if FAILURE_ANALYSIS in wanted:
        out["failures"] = _failure_clusters(diagnoses, total)
        # N17-obs449: a list (the hub concatenates each shard's list keys) of
        # {sessions, incomplete, lowConfidenceDropped, threshold} — what "0 clusters" means
        out["failureDetection"] = [{**detection, "threshold": str(confidence.value),
                                    "judge": model_id_of(detector)}]
    if USER_INTENT in wanted:
        out["userIntents"] = _summary_clusters(summaries, total, kind=USER_INTENT)
    if EXECUTION_SUMMARY in wanted:
        out["executionSummaries"] = _summary_clusters(summaries, total, kind=EXECUTION_SUMMARY)
    if errors:
        out["errors"] = errors
    return out
