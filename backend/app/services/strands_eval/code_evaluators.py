"""Code (Lambda) evaluators on Global AgentCore's contract (N17-obs327).

Global (``code-based-evaluators.html``) invokes the evaluator's Lambda with::

    {schemaVersion: "1.0", evaluatorId, evaluatorName, evaluationLevel,
     evaluationInput: {sessionSpans: [<raw ADOT span / log documents>]},
     evaluationReferenceInputs: [<ground truth, filtered per level>],
     evaluationTarget: None (SESSION) | {traceIds} (TRACE) | {traceIds, spanIds} (TOOL_CALL)}

and reads back ``{label, value?, explanation?}`` | ``{errorCode, errorMessage}``. This module
sends exactly that, so a Lambda written for Global — the AgentCore SDK's
``@custom_code_based_evaluator`` reads ``sessionSpans`` and the target ids — runs unchanged.
The CN case fields the reviewed handler (``assistant/lambda_runtime/handler.py``) reads stay
in ``evaluationInput`` next to ``sessionSpans`` and the package's evaluator name stays in
``evaluator`` (additive: an already-deployed package keeps working).

- ``sessionSpans`` are the session's documents as the Global collector reads them
  (:class:`SessionSpans`: the same Logs Insights filter over the job's log groups — span
  records and content records, ids as ADOT wrote them, credentials masked), fetched once per
  session and only when a code evaluator runs.
- ``evaluationReferenceInputs`` come from the dataset truth (:func:`reference_inputs`): one
  session-level entry (assertions, expected trajectory) and one per turn with an expected
  response, paired with the turn's trace by ``engine_core.turn_cases``; a TRACE call gets the
  session entry plus its own turn's, a TOOL_CALL call the session entry only.
- The engine already hands a TRACE evaluator one case per turn; its target is that turn's
  trace.

Shipped into the worker zip with the rest of the package (``launchpad_eval``).
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.config import Config
from botocore.exceptions import ReadTimeoutError
from strands_evals.evaluators import Evaluator
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput
from strands_evals.types.trace import Session

from app.services.strands_eval import engine_core
from app.services.strands_eval import evaluators as evaluator_lib
from app.services.strands_eval.redaction import redact_value
from app.services.strands_eval.spans_provider import SESSION_ID_RE, query_records

SCHEMA_VERSION = "1.0"
# The Global collector's query (bedrock_agentcore.evaluation CloudWatchAgentSpanCollector):
# every ADOT document of the session — span records (``aws/spans``) and the content records
# the runtime group carries — sorted as ingested.
SESSION_SPANS_QUERY = (
    "fields @timestamp, @message\n"
    "| filter attributes.session.id = {session_id}\n"
    "| filter ispresent(scope.name)\n"
    "| filter ispresent(traceId)\n"
    "| filter ispresent(spanId)\n"
    "| sort @timestamp asc\n"
    "| limit 10000"
)
QUERY_TIMEOUT_S = 55.0
# Global: "The maximum input payload size sent to the Lambda function is 6 MB."
PAYLOAD_LIMIT_BYTES = 6 * 1024 * 1024
# Global: ``lambdaTimeoutInSeconds`` 1–300 (default 60); the client waits a little longer
# than the function may run, so the Lambda's own timeout answers first (N17-obs333)
LAMBDA_TIMEOUT_RANGE_S = (1, 300)
DEFAULT_LAMBDA_TIMEOUT_S = 60
CLIENT_TIMEOUT_MARGIN_S = 5
LAMBDA_CONNECT_TIMEOUT_S = 10
_QUERY_FAILED = ("Failed", "Cancelled", "Timeout", "Unknown")


def _is_adot_document(item: Any) -> bool:
    return isinstance(item, dict) and "scope" in item and "traceId" in item and "spanId" in item


class SessionSpans:
    """``session_spans(session_id) -> [ADOT document]`` over ``log_groups`` (the job's
    runtime group(s) and ``aws/spans``), cached per session for the job."""

    def __init__(
        self, logs_client: Any, log_groups: list[str], *, lookback_hours: int = 24,
        end_time: datetime | None = None, timeout_s: float = QUERY_TIMEOUT_S,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        records_guard: Callable[[str, list[dict[str, Any]]], None] | None = None,
    ) -> None:
        self._client = logs_client
        self._records_guard = records_guard
        self._log_groups = [g for g in log_groups if g]
        self._lookback_hours = max(1, int(lookback_hours))
        self._end_time = end_time
        self._timeout_s = float(timeout_s)
        self._sleep, self._clock = sleep, clock
        self._cache: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def __call__(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            if session_id in self._cache:
                return self._cache[session_id]
        spans = self._fetch(session_id)
        with self._lock:
            self._cache[session_id] = spans
        return spans

    def _fetch(self, session_id: str) -> list[dict[str, Any]]:
        if not SESSION_ID_RE.match(session_id or ""):
            raise ValueError(f"invalid session id for a spans query: {session_id!r}")
        end = self._end_time or datetime.now(tz=UTC)
        start = end - timedelta(hours=self._lookback_hours)
        query_id = self._client.start_query(
            logGroupNames=list(self._log_groups), startTime=int(start.timestamp()),
            endTime=int(end.timestamp()),
            queryString=SESSION_SPANS_QUERY.format(session_id=json.dumps(session_id)),
        )["queryId"]
        deadline = self._clock() + self._timeout_s
        delay = 0.5
        while True:
            result = self._client.get_query_results(queryId=query_id)
            status = result.get("status")
            if status == "Complete":
                break
            if status in _QUERY_FAILED:
                raise RuntimeError(f"session spans query for {session_id} ended {status}")
            if self._clock() >= deadline:
                raise RuntimeError(f"session spans query for {session_id} did not complete "
                                   f"within {self._timeout_s:g} s")
            self._sleep(delay)
            delay = min(delay * 2, 5.0)
        rows = result.get("results") or []
        if self._records_guard is not None:
            documents = query_records(rows, strict=True)
            self._records_guard(session_id, documents)
        else:
            # Preserve the background collector's tolerant parsing, including null values.
            documents = []
            for row in rows:
                for item in row:
                    if item.get("field") != "@message":
                        continue
                    try:
                        documents.append(json.loads(item.get("value") or ""))
                    except (TypeError, ValueError):
                        continue
        documents = [redact_value(d) for d in documents if _is_adot_document(d)]
        # the Global collector's order (span records by end time; content records first)
        documents.sort(key=lambda d: int(d.get("endTimeUnixNano") or 0))
        return documents


def clamp_timeout(timeout_s: Any) -> int:
    low, high = LAMBDA_TIMEOUT_RANGE_S
    try:
        value = int(timeout_s or DEFAULT_LAMBDA_TIMEOUT_S)
    except (TypeError, ValueError):
        value = DEFAULT_LAMBDA_TIMEOUT_S
    return max(low, min(high, value))


def lambda_config(timeout_s: Any) -> Config:
    """The botocore config of a code evaluator's client (N17-obs333): read timeout = the
    evaluator's ``lambda_timeout_s`` + a margin, and ONE attempt — a botocore retry would
    re-invoke the function behind the engine's back."""
    return Config(read_timeout=clamp_timeout(timeout_s) + CLIENT_TIMEOUT_MARGIN_S,
                  connect_timeout=LAMBDA_CONNECT_TIMEOUT_S,
                  retries={"total_max_attempts": 1, "mode": "standard"})


class LambdaClients:
    """One Lambda client per code-evaluator timeout, built on first use by
    ``make(timeout_s)`` (the hub: the workspace funnel with :func:`lambda_config`; the
    worker: its own client with the same config) and reused for every unit of the job."""

    def __init__(self, make: Callable[[int], Any]) -> None:
        self._make = make
        self._clients: dict[int, Any] = {}
        self._lock = threading.Lock()

    def for_timeout(self, timeout_s: Any) -> Any:
        timeout = clamp_timeout(timeout_s)
        with self._lock:
            if timeout not in self._clients:
                self._clients[timeout] = self._make(timeout)
            return self._clients[timeout]


@dataclass
class CodeEvaluatorRuntime:
    """What a code evaluator needs besides its definition: the Lambda client, the
    session's documents (``sessionSpans``) and the dataset truth by session id."""

    lambda_client: Any
    session_spans: Callable[[str], list[dict[str, Any]]] | None = None
    ground_truth: Mapping[str, dict[str, Any]] = field(default_factory=dict)
    records_guard: Callable[[str, list[dict[str, Any]]], None] | None = None


def runtime_for(
    lambda_client: Any, *, logs_client: Any, log_groups: list[str], lookback_hours: int = 24,
    ground_truth: Mapping[str, dict[str, Any]] | None = None,
    records_guard: Callable[[str, list[dict[str, Any]]], None] | None = None,
) -> CodeEvaluatorRuntime | None:
    """The job's runtime (``None`` without a Lambda client: code evaluators are off).
    The session documents are only queried when a code evaluator actually runs."""
    if lambda_client is None:
        return None
    if isinstance(lambda_client, CodeEvaluatorRuntime):
        return (replace(lambda_client, records_guard=records_guard)
                if records_guard is not None else lambda_client)
    return CodeEvaluatorRuntime(
        lambda_client,
        session_spans=SessionSpans(logs_client, list(log_groups), lookback_hours=lookback_hours,
                                   records_guard=records_guard),
        ground_truth=dict(ground_truth or {}),
        records_guard=records_guard,
    )


# ─── the envelope ──────────────────────────────────────────────────────────


def session_id_of(case: EvaluationData) -> str:
    session = case.actual_trajectory
    if isinstance(session, Session) and session.session_id:
        return str(session.session_id)
    return str(case.name or "")


def _last_trace_id(case: EvaluationData) -> str | None:
    session = case.actual_trajectory
    if not isinstance(session, Session):
        return None
    return next((t.trace_id for t in reversed(session.traces) if t.trace_id), None)


def targets_of(level: str, case: EvaluationData) -> list[dict[str, list[str]] | None]:
    """The ``evaluationTarget`` of every call one case fans out to (N17-obs333): SESSION
    one call, ``None``; TRACE one call, the judged turn (the engine's per-turn case ends at
    it); TOOL_CALL one call per tool span ``{traceIds: [t], spanIds: [s]}``, in the order
    of ``engine_core.tool_units`` — the order the engine stamps the rows with."""
    if level == "TRACE":
        trace_id = _last_trace_id(case)
        return [{"traceIds": [trace_id]} if trace_id else None]
    if level == "TOOL_CALL":
        return [{"traceIds": [u["trace_id"]], "spanIds": [u["span_id"]]}
                for u in engine_core.tool_units(case)]
    return [None]


def _assertions(case: EvaluationData, truth: Mapping[str, Any] | None) -> list[str]:
    if truth is not None and "assertions" in truth:
        return [str(a) for a in truth.get("assertions") or [] if str(a).strip()]
    return [line for line in str(case.expected_assertion or "").splitlines() if line.strip()]


def _expected_trajectory(case: EvaluationData, truth: Mapping[str, Any] | None) -> Any:
    if truth is not None and "expected_trajectory" in truth:
        return truth.get("expected_trajectory")
    return case.expected_trajectory


def _expected_responses(case: EvaluationData,
                        truth: Mapping[str, Any] | None) -> dict[str, str]:
    """``{trace_id: expected response}`` per turn (the engine's own turn ↔ trace pairing);
    a case with one joined expected response and no per-turn truth pins it on the last
    turn, as the Global SDK does with a string."""
    out = {trace_id: str(turn.expected_output)
           for trace_id, turn in engine_core.turn_cases(case, dict(truth) if truth else None)
           if trace_id and turn.expected_output}
    if not out and not (truth or {}).get("turns") and case.expected_output:
        last = _last_trace_id(case)
        if last:
            out[last] = str(case.expected_output)
    return out


def reference_inputs(
    session_id: str, level: str, case: EvaluationData, truth: Mapping[str, Any] | None,
    target: Mapping[str, list[str]] | None,
) -> list[dict[str, Any]]:
    """``evaluationReferenceInputs`` filtered as Global filters them: SESSION all entries,
    TRACE the session entry plus the target trace's, TOOL_CALL the session entry."""
    out: list[dict[str, Any]] = []
    session_ref: dict[str, Any] = {"context": {"spanContext": {"sessionId": session_id}}}
    assertions = _assertions(case, truth)
    if assertions:
        session_ref["assertions"] = [{"text": a} for a in assertions]
    trajectory = _expected_trajectory(case, truth)
    if isinstance(trajectory, list):
        session_ref["expectedTrajectory"] = {"toolNames": [str(t) for t in trajectory]}
    if len(session_ref) > 1:
        out.append(session_ref)
    if level == "TOOL_CALL":
        return out
    wanted = set((target or {}).get("traceIds") or []) if level == "TRACE" else None
    for trace_id, text in _expected_responses(case, truth).items():
        if wanted is None or trace_id in wanted:
            out.append({"context": {"spanContext": {"sessionId": session_id,
                                                    "traceId": trace_id}},
                        "expectedResponse": {"text": text}})
    return out


def launchpad_case(level: str, case: EvaluationData) -> dict[str, Any]:
    """The CN case fields the reviewed handler reads (additive). A TRACE call's
    ``actual_trajectory`` is its own turn's calls — the engine's turn case carries the
    earlier turns as history, which a per-turn rule must not count."""
    trajectory = case.actual_trajectory
    if level == "TRACE" and isinstance(trajectory, Session) and trajectory.traces:
        trajectory = Session(session_id=trajectory.session_id, traces=trajectory.traces[-1:])
    return {
        "input": case.input,
        "actual_output": case.actual_output,
        "expected_output": case.expected_output,
        "expected_assertion": case.expected_assertion,
        "expected_trajectory": case.expected_trajectory,
        "actual_trajectory": evaluator_lib.actual_tool_names(trajectory),
    }


def envelope(
    *, evaluator_id: str, evaluator_name: str, level: str, case: EvaluationData,
    session_spans: list[dict[str, Any]], truth: Mapping[str, Any] | None,
    target: Mapping[str, list[str]] | None,
) -> dict[str, Any]:
    session_id = session_id_of(case)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "evaluatorId": evaluator_id,
        "evaluatorName": evaluator_name,
        "evaluationLevel": level,
        "evaluationInput": {"sessionSpans": session_spans, **launchpad_case(level, case)},
        "evaluationReferenceInputs": reference_inputs(session_id, level, case, truth, target),
        "evaluationTarget": dict(target) if target is not None else None,
        # the reviewed handler's package key (``rules.json`` is keyed by the evaluator name)
        "evaluator": evaluator_name,
    }


# ─── the answer (N17-obs328) ───────────────────────────────────────────────


class CodeEvaluatorError(EvaluationOutput):
    """A unit the Lambda did not score: an error row (``results.records_from_outputs``
    writes ``error_type`` / ``error_message``, no score, no label) — never a 0.0 FAIL in
    the mean. Returned rather than raised, so the judge retry never re-invokes a Lambda
    whose message happens to read "timed out"."""

    score: float | None = None  # type: ignore[assignment]
    test_pass: bool = False
    error_type: str
    error_message: str


def _error(error_type: str, message: str) -> CodeEvaluatorError:
    return CodeEvaluatorError(error_type=error_type, error_message=str(message)[:2000])


def _is_timeout(error_type: str, message: str) -> bool:
    return error_type == "Sandbox.Timedout" or "Task timed out" in message


def output_of(response: Mapping[str, Any], raw: bytes) -> EvaluationOutput:
    """One Lambda answer → one output, Global's semantics:

    - ``FunctionError`` (unhandled exception; Lambda timeout) → ``lambda_function_error`` /
      ``lambda_timeout``;
    - ``{errorCode, errorMessage}`` → ``error_type`` = the code;
    - no ``label`` (Global requires one) → ``missing_label``; a ``value`` that is not a
      number → ``invalid_value``; a body that is not a JSON object → ``invalid_response``;
    - a label without ``value`` → the label alone, ``score`` None (never averaged as 0);
    - ``{label, value}`` → ``score`` = value."""
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return _error("invalid_response", f"the Lambda answered non-JSON: {raw[:200]!r}")
    if response.get("FunctionError"):
        fields = body if isinstance(body, dict) else {}
        kind = str(fields.get("errorType") or response.get("FunctionError") or "")
        message = str(fields.get("errorMessage") or "")
        if _is_timeout(kind, message):
            return _error("lambda_timeout", f"the Lambda timed out: {message}")
        return _error("lambda_function_error", f"{kind}: {message}")
    if not isinstance(body, dict):
        return _error("invalid_response",
                      f"the Lambda answered a {type(body).__name__}, not an object")
    if body.get("errorCode"):
        return _error(str(body["errorCode"]), str(body.get("errorMessage") or ""))
    label = body.get("label")
    if label is None or not str(label).strip():
        return _error("missing_label", "the Lambda answered without a label (Global requires "
                                       "`label` on every success response)")
    reason = body.get("explanation")
    value = body.get("value")
    if value is None:
        return evaluator_lib.CategoricalOutput(test_pass=False, label=str(label), reason=reason)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return _error("invalid_value", f"the Lambda's value {value!r} is not a number")
    return EvaluationOutput(score=float(value), test_pass=float(value) >= 1.0,
                            label=str(label), reason=reason)


# ─── the evaluator ─────────────────────────────────────────────────────────


class LambdaEvaluator(Evaluator[Any, Any]):
    """A code evaluator: the Global envelope → the Lambda → ``{label, value?,
    explanation?}`` | ``{errorCode, errorMessage}``. ``lambda_client`` is a
    :class:`CodeEvaluatorRuntime` (a bare client works, without session documents)."""

    def __init__(self, *, lambda_arn: str, lambda_client: Any, timeout_s: int, name: str,
                 evaluator_name: str | None = None, level: str = "TRACE") -> None:
        super().__init__(name=name)
        self.lambda_arn = lambda_arn
        self.runtime = (lambda_client if isinstance(lambda_client, CodeEvaluatorRuntime)
                        else CodeEvaluatorRuntime(lambda_client))
        self.lambda_client = self.runtime.lambda_client
        self.timeout_s = timeout_s
        self.evaluator_name = evaluator_name or name
        self.level = level

    def payloads(self, case: EvaluationData) -> list[dict[str, Any]]:
        """One envelope per unit the case fans out to (:func:`targets_of`)."""
        session_id = session_id_of(case)
        spans = self.runtime.session_spans(session_id) if self.runtime.session_spans else []
        if self.runtime.records_guard is not None:
            self.runtime.records_guard(session_id, spans)
        truth = self.runtime.ground_truth.get(session_id)
        return [envelope(evaluator_id=self.name, evaluator_name=self.evaluator_name,
                         level=self.level, case=case, session_spans=spans, truth=truth,
                         target=target)
                for target in targets_of(self.level, case)]

    def _client(self) -> Any:
        clients = self.lambda_client
        return (clients.for_timeout(self.timeout_s) if isinstance(clients, LambdaClients)
                else clients)

    def _invoke(self, payload: dict[str, Any]) -> EvaluationOutput:
        data = json.dumps(payload, default=str).encode("utf-8")
        if len(data) > PAYLOAD_LIMIT_BYTES:
            return _error("payload_too_large",
                          f"the evaluation payload is {len(data)} bytes, over the Lambda "
                          f"limit of {PAYLOAD_LIMIT_BYTES} (the session's documents)")
        try:
            response = self._client().invoke(
                FunctionName=self.lambda_arn, InvocationType="RequestResponse", Payload=data,
            )
        except ReadTimeoutError:
            # the client gave up after the evaluator's timeout + margin: a timeout row, not
            # a transient to retry (a retry would run the whole Lambda again)
            return _error("lambda_timeout",
                          f"no answer within the evaluator's {clamp_timeout(self.timeout_s)} s "
                          f"timeout (+{CLIENT_TIMEOUT_MARGIN_S} s)")
        return output_of(response, response["Payload"].read())

    def evaluate(self, evaluation_case: EvaluationData) -> list[EvaluationOutput]:
        """One output per unit, in unit order (a TOOL_CALL case: one per tool call)."""
        return [self._invoke(payload) for payload in self.payloads(evaluation_case)]

    async def evaluate_async(self, evaluation_case: EvaluationData) -> list[EvaluationOutput]:
        return self.evaluate(evaluation_case)
