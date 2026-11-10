"""CloudWatch readiness gate for fresh AgentCore evaluation sessions (N12 E1-4).

Pure stdlib over an injected Logs client — shipped into the evaluation worker as
``launchpad_eval.telemetry`` (mirror test). The gate waits until the newest agent root
span of a session is complete AND its content (the ``body.input/output`` record the spans
provider reads) has been ingested for ``stability_seconds``; Logs Insights indexes lag
ingestion, so the ingestion age is the conservative watermark.

N17-obs336: the root is recognised by what it is, not by the literal default Strands name —
an ``invoke_agent`` span (``gen_ai.operation.name`` or the ``invoke_agent`` name prefix:
``invoke_agent TravelAgent``, TypeScript's ``invoke_agent Strands Agent``) with no agent
ancestor (an A2A peer's sits under the orchestrator's tool call); without one, the framework
root kinds of the Global framework pages (``openinference.span.kind`` AGENT / CHAIN,
``traceloop.span.kind == workflow``) at the top of their tree. Its content is the root's own
content record (Strands split mode — the rule is unchanged there), else, for another
framework, the content records of the root's trace, else content carried inline on the root
span record itself (unified telemetry), whose own ingestion is then the watermark, else
content inline on the span records of the root's trace (r17-bm: an ADOT Node LangGraph.js
root is a structural container — Global's TypeScript rule puts the conversation on its
``chat`` spans' ``gen_ai.input.messages`` / ``gen_ai.output.messages``), the newest of those.

``spans_source`` follows ``Settings.observability_spans_source`` (R-6): with Transaction
Search on (``both`` / ``spans``) the root span is probed in ``aws/spans``; in
``runtime_logs`` mode the runtime's own log group carries both the span and its content.
"""

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

ROOT_SPAN_NAME = "invoke_agent Strands Agents"  # the default Strands root (Python SDK)
SPANS_LOG_GROUP = "aws/spans"
AGENT_OPERATION = "invoke_agent"
# Framework root kinds (Global supported-frameworks pages), used when no invoke_agent span
_ROOT_KINDS: tuple[tuple[str, frozenset[str]], ...] = (
    ("openinference.span.kind", frozenset({"AGENT", "CHAIN"})),
    ("traceloop.span.kind", frozenset({"workflow"})),
)
# Content carried on the span record itself (unified telemetry, OpenInference, traceloop)
_INLINE_CONTENT_KEYS = ("input.value", "output.value", "traceloop.entity.input",
                        "traceloop.entity.output", "gen_ai.prompt", "gen_ai.completion",
                        "gen_ai.input.messages", "gen_ai.output.messages")


@dataclass(frozen=True)
class TelemetryProbe:
    root_span_id: str | None
    content_ingestion_ms: int | None
    framework: str | None = None
    detail: str | None = None

    @property
    def readiness_ingestion_ms(self) -> int | None:
        return self.content_ingestion_ms

    @property
    def paired(self) -> bool:
        return self.root_span_id is not None and self.readiness_ingestion_ms is not None


class TelemetryReadinessTimeout(RuntimeError):
    """The newest evaluation session never became safe to submit."""


def _filter_events(
    client: Any,
    *,
    log_group: str,
    session_id: str,
    start_time_ms: int | None,
) -> list[dict[str, Any]]:
    pattern = json.dumps(session_id)
    kwargs: dict[str, Any] = {
        "logGroupName": log_group,
        "filterPattern": pattern,
        "limit": 10_000,
    }
    if start_time_ms is not None:
        kwargs["startTime"] = start_time_ms
    events: list[dict[str, Any]] = []
    previous_token: str | None = None
    while True:
        response = client.filter_log_events(**kwargs)
        events.extend(response.get("events", []))
        token = response.get("nextToken")
        if not token or token == previous_token:
            return events
        previous_token = token
        kwargs["nextToken"] = token


def _json_message(event: dict[str, Any]) -> dict[str, Any] | None:
    try:
        value = json.loads(event.get("message") or "")
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _event_time_ms(event: dict[str, Any], key: str = "timestamp") -> int:
    try:
        return int(event.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _attributes(message: dict[str, Any]) -> dict[str, Any]:
    attributes = message.get("attributes")
    return attributes if isinstance(attributes, dict) else {}


def is_agent_span(message: dict[str, Any]) -> bool:
    """An ``invoke_agent`` span record, whatever the agent is called."""
    name = str(message.get("name") or "")
    return (_attributes(message).get("gen_ai.operation.name") == AGENT_OPERATION
            or name == AGENT_OPERATION or name.startswith(f"{AGENT_OPERATION} "))


def root_kind(message: dict[str, Any]) -> str | None:
    """The framework root kind of a span record (``openinference`` / ``traceloop``)."""
    attributes = _attributes(message)
    for key, values in _ROOT_KINDS:
        if attributes.get(key) in values:
            return key.split(".", 1)[0]
    return None


def framework_of(message: dict[str, Any]) -> str:
    if is_agent_span(message):
        scope = str((message.get("scope") or {}).get("name") or "")
        return "strands" if scope.startswith("strands") or message.get("name") == ROOT_SPAN_NAME \
            else "genai"
    return root_kind(message) or "unknown"


def _inline_content(message: dict[str, Any]) -> bool:
    events = message.get("events")
    if isinstance(events, list) and any(
            isinstance(e, dict) and str(e.get("name") or "").startswith("gen_ai.")
            for e in events):
        return True
    attributes = _attributes(message)
    return any(attributes.get(key) for key in _INLINE_CONTENT_KEYS)


def _span_failed(message: dict[str, Any]) -> bool:
    """The span record ended with status ERROR (OTLP ``status.code``)."""
    status = message.get("status")
    code = status.get("code") if isinstance(status, dict) else None
    return str(code or "").upper() in ("ERROR", "STATUS_CODE_ERROR", "2")


def _roots(spans: list[tuple[dict[str, Any], dict[str, Any]]],
           ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """The agent roots among the session's span records ``(event, message)``: the
    ``invoke_agent`` spans with no ``invoke_agent`` ancestor, else the framework-kind spans
    with no ancestor of the same family."""
    by_id = {str(m.get("spanId")): m for _e, m in spans}
    for is_candidate in (is_agent_span, lambda m: root_kind(m) is not None):
        candidates = [(e, m) for e, m in spans if is_candidate(m)]
        if not candidates:
            continue
        roots = []
        for event, message in candidates:
            seen: set[str] = set()
            parent = message.get("parentSpanId")
            nested = False
            while isinstance(parent, str) and parent in by_id and parent not in seen:
                seen.add(parent)
                if is_candidate(by_id[parent]):
                    nested = True
                    break
                parent = by_id[parent].get("parentSpanId")
            if not nested:
                roots.append((event, message))
        return roots or candidates
    return []


def probe_latest_session(
    client: Any,
    *,
    session_id: str,
    content_log_group: str,
    start_time_ms: int | None = None,
    spans_source: str = "both",
) -> TelemetryProbe:
    """Find the newest complete agent root span of the session and its content."""
    root_group = content_log_group if spans_source == "runtime_logs" else SPANS_LOG_GROUP
    spans: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for event in _filter_events(
        client,
        log_group=root_group,
        session_id=session_id,
        start_time_ms=start_time_ms,
    ):
        message = _json_message(event)
        if not message or "name" not in message:
            continue
        span_id = message.get("spanId")
        if _attributes(message).get("session.id") != session_id or not isinstance(span_id, str):
            continue
        spans.append((event, message))

    roots = _roots(spans)
    if not roots:
        return TelemetryProbe(root_span_id=None, content_ingestion_ms=None)
    root_event, root = max(roots, key=lambda pair: _event_time_ms(pair[0]))
    latest_span_id = str(root["spanId"])
    framework = framework_of(root)
    root_trace = root.get("traceId")

    # N17-obs415: a turn that FAILED (max_tokens / max iterations / a runtime timeout) ends
    # its agent span with status ERROR and never writes the final answer — its content record
    # carries only ``input``. Waiting for an ``output`` would wait out the whole window; the
    # failed root pairs on its input-only record (else on the root span record itself), and
    # the engine writes the failed-turn rows (N17-obs373).
    failed = _span_failed(root)
    own: list[int] = []
    in_trace: list[int] = []
    for event in _filter_events(
        client,
        log_group=content_log_group,
        session_id=session_id,
        start_time_ms=start_time_ms,
    ):
        message = _json_message(event)
        if not message:
            continue
        body = message.get("body")
        if not (
            _attributes(message).get("session.id") == session_id
            and isinstance(body, dict)
            and "input" in body
            and ("output" in body
                 or (failed and message.get("spanId") == latest_span_id))
        ):
            continue
        ingestion = int(event.get("ingestionTime") or event.get("timestamp") or 0)
        if message.get("spanId") == latest_span_id:
            own.append(ingestion)
        elif root_trace and message.get("traceId") == root_trace:
            in_trace.append(ingestion)
    matching = own or ([] if framework == "strands" else in_trace)
    if not matching and (_inline_content(root) or failed):
        matching = [int(root_event.get("ingestionTime") or root_event.get("timestamp") or 0)]
    if not matching and framework != "strands" and root_trace:
        matching = [int(e.get("ingestionTime") or e.get("timestamp") or 0) for e, m in spans
                    if m.get("traceId") == root_trace and _inline_content(m)]
    return TelemetryProbe(
        root_span_id=latest_span_id,
        content_ingestion_ms=max(matching, default=None),
        framework=framework,
        detail=(
            None
            if matching
            else f"content log for {framework} span {latest_span_id} is missing"
        ),
    )


def _probe_rank(probe: TelemetryProbe) -> int:
    """How far a probe got: 0 nothing, 1 root span found, 2 content log paired."""
    if probe.paired:
        return 2
    return 1 if probe.root_span_id is not None else 0


def wait_for_evaluation_telemetry(
    client: Any,
    *,
    session_id: str,
    content_log_group: str,
    start_time_ms: int | None = None,
    stability_seconds: int,
    timeout_seconds: int,
    poll_seconds: float = 5.0,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    spans_source: str = "both",
    content_log_groups: Sequence[str] = (),
) -> TelemetryProbe:
    """Wait until the newest root is complete and its content log stable for the engine.

    CloudWatch can expose telemetry before Logs Insights can query it. Since there is no
    readiness API, the content log's ingestion age is the conservative index watermark.

    ``content_log_groups`` (CN additive, N-SCN-bug3): further groups the session's content
    log may live in — a canary replays into one runtime log group **per named endpoint**, so
    when the caller cannot tell which arm served the session every candidate is probed on
    each poll and the first paired, stable one wins. The timeout detail reports the most
    advanced probe (a found root span beats a missing one).
    """
    groups = [content_log_group] + [g for g in content_log_groups if g != content_log_group]
    deadline = clock() + timeout_seconds
    last_probe = TelemetryProbe(root_span_id=None, content_ingestion_ms=None)
    while True:
        best: TelemetryProbe | None = None
        now = clock()
        for group in groups:
            probe = probe_latest_session(
                client,
                session_id=session_id,
                content_log_group=group,
                start_time_ms=start_time_ms,
                spans_source=spans_source,
            )
            if best is None or _probe_rank(probe) > _probe_rank(best):
                best = probe
            readiness_ingestion_ms = probe.readiness_ingestion_ms
            if probe.paired and readiness_ingestion_ms is not None:
                age_seconds = max(0.0, now - readiness_ingestion_ms / 1000)
                if age_seconds >= stability_seconds:
                    return probe
        last_probe = best if best is not None else last_probe
        readiness_ingestion_ms = last_probe.readiness_ingestion_ms
        if now >= deadline:
            if last_probe.detail:
                detail = last_probe.detail
            elif last_probe.root_span_id is None:
                detail = "root span is missing"
            elif readiness_ingestion_ms is None:
                detail = f"content log for span {last_probe.root_span_id} is missing"
            else:
                age = max(0.0, now - readiness_ingestion_ms / 1000)
                detail = (
                    f"content log age is {age:.1f}s, below the required "
                    f"{stability_seconds}s"
                )
            raise TelemetryReadinessTimeout(
                f"evaluation telemetry did not become ready for session {session_id}: {detail}"
            )
        sleep(min(poll_seconds, max(0.0, deadline - now)))
