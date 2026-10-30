"""``--global``: the same sessions through Global AgentCore ``Evaluate`` (us-west-2).

The user approved sending these DEV records to the managed service (2026-10-23, the
eval-accuracy loop's oracle). Only ``bedrock-agentcore:Evaluate`` is called — built-in
evaluator ids only, so nothing is created on the Global side (custom evaluators would need
an evaluator resource there and are skipped).

Credentials: a COMMERCIAL-partition profile on this box (``--global-profile``, default
``default``; checked with ``sts get-caller-identity`` before any call). Where only another
tool holds such credentials (e.g. the AWS MCP server), run the requests there and hand the
responses in with ``--global-results <file.jsonl>`` (one ``{"evaluatorId", "sessionId",
"rep"?, "evaluationResults": [...]}`` per line) — the metrics are computed the same way.

``sessionSpans`` mirrors the bedrock-agentcore SDK's CloudWatch span collector: every
record of the session from ``aws/spans`` and the runtime group that carries
``scope.name``, ``traceId`` and ``spanId``, sorted by ``endTimeUnixNano`` (stable). Every
response is cached under ``.eval-cache/global/`` by a hash of the request and the rep index,
so a re-run of the report costs nothing; ``--global-reps N`` re-asks N times to measure the
service's own rerun variance.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from evalwb.cache import RUNTIME, SPANS, RecordCache, SessionRecords
from evalwb.reconcile import agent_tool_spans, own_services

GLOBAL_REGION = "us-west-2"  # region-boundary: allow — the user-approved Global oracle
TARGET_CHUNK = 10


class OnlyEvaluate:
    """The Global client with ONE reachable call: ``evaluate`` (diff-global ``gclient.py``)."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def evaluate(self, **request: Any) -> dict[str, Any]:
        return self._client.evaluate(**request)


def global_client(profile: str) -> OnlyEvaluate:
    import boto3
    from botocore.config import Config

    session = boto3.Session(profile_name=profile, region_name=GLOBAL_REGION)
    partition = session.client("sts").get_caller_identity()["Arn"].split(":")[1]
    if partition != "aws":
        raise RuntimeError(f"profile {profile!r} is a {partition} principal, not a commercial "
                           "one — pass --global-profile or --global-results")
    return OnlyEvaluate(session.client("bedrock-agentcore", config=Config(
        read_timeout=300, retries={"max_attempts": 4, "mode": "adaptive"})))


def _service(rec: dict[str, Any]) -> str:
    return str(((rec.get("resource") or {}).get("attributes") or {}).get("service.name") or "")


def foreign_spans(entry: SessionRecords) -> int:
    """Records of the session that belong to ANOTHER runtime (a called A2A peer: the
    propagated trace and ``session.id`` under the peer's ``service.name``)."""
    services = own_services(entry)
    return sum(1 for r in entry.group(SPANS) if _service(r) and _service(r) not in services)


def session_spans(entry: SessionRecords, *, own_only: bool = False) -> list[dict[str, Any]]:
    """The SDK collector's ``sessionSpans``. ``own_only`` (the A2A workaround,
    ``--global-own-spans``) drops a called peer's span records: their log events live in the
    PEER's runtime group, so Global rejects the whole session with
    ``LogEventMissingException``. Records without a ``service.name`` are kept."""
    services = own_services(entry) if own_only else set()

    def ok(rec: dict[str, Any]) -> bool:
        if services and _service(rec) and _service(rec) not in services:
            return False
        return ((rec.get("attributes") or {}).get("session.id") == entry.session_id
                and "scope" in rec and bool(rec.get("traceId")) and bool(rec.get("spanId")))

    pool = [r for r in entry.group(SPANS) if ok(r)] + [r for r in entry.group(RUNTIME) if ok(r)]
    return sorted(pool, key=lambda r: int(r.get("endTimeUnixNano") or 0))


def agent_traces(entry: SessionRecords) -> list[str]:
    """Trace ids with an ``invoke_agent`` span — for a LangGraph session, an invoke agent span
    by its library's classifying attribute (OpenInference ``CHAIN`` / ``AGENT``, Traceloop
    ``workflow``: the TypeScript libraries set no ``invoke_agent``, r17-bm) — in start order
    (the turns Global scores)."""
    from app.services.strands_eval.langgraph_mapper import _kind_attribute, is_langgraph

    records = entry.group(SPANS)
    langgraph = is_langgraph(records)
    starts: dict[str, int] = {}
    for rec in records:
        attributes = rec.get("attributes") or {}
        if (attributes.get("gen_ai.operation.name") == "invoke_agent"
                or str(rec.get("name") or "").startswith("invoke_agent")
                or (langgraph and _kind_attribute(rec) == "agent")):
            start = int(rec.get("startTimeUnixNano") or 0)
            starts[rec["traceId"]] = min(starts.get(rec["traceId"], start), start)
    return sorted(starts, key=starts.__getitem__)


def tool_spans(entry: SessionRecords) -> list[str]:
    """The agent's own ``execute_tool`` span ids in start order (the TOOL_CALL targets):
    Strands scope only (never ADOT's ``mcp tools/call`` spans — Global rejects the whole
    request), and never a called A2A peer's spans (another runtime's ``service.name``)."""
    return [r["spanId"] for r in agent_tool_spans(entry)]


def tool_units(entry: SessionRecords) -> list[str]:
    """:func:`tool_spans` as trace-scoped ``<trace>:<span>`` ids — the metrics' TOOL_CALL
    units (a bare span id repeats across turns, N17-obs315 MT2)."""
    return [f"{r.get('traceId') or ''}:{r['spanId']}" for r in agent_tool_spans(entry)]


def reference_inputs(session_id: str, truth: dict[str, Any] | None, traces: list[str]
                     ) -> list[dict[str, Any]]:
    """The SDK's ``EvaluationClient._build_reference_inputs`` shape, sent with every
    evaluator like the SDK does: one session entry (assertions, ``expectedTrajectory``) and
    the expected response per agent trace when the truth has one per turn and the turns line
    up with the traces (the SDK's dict form), else on the LAST agent trace. A scenario that
    DEFINES an empty trajectory sends ``toolNames: []`` — Global reads it as "no tools
    expected"; an absent one sends nothing."""
    truth = truth or {}
    refs: list[dict[str, Any]] = []
    session_ref: dict[str, Any] = {"context": {"spanContext": {"sessionId": session_id}}}
    if truth.get("assertions"):
        session_ref["assertions"] = [{"text": a} for a in truth["assertions"]]
    # an explicit list is "no tools expected" when empty (the engine's obs376 rule)
    if (isinstance(truth.get("expected_trajectory"), list)
            and (truth["expected_trajectory"] or truth.get("expected_trajectory_defined", True))):
        session_ref["expectedTrajectory"] = {"toolNames": list(truth["expected_trajectory"])}
    if len(session_ref) > 1:
        refs.append(session_ref)
    turns = [t.get("expected_response") for t in truth.get("turns") or [] if isinstance(t, dict)]
    if traces and turns and len(turns) == len(traces) and any(turns):
        # per-turn truth (obs322 / obs324): the SDK's dict form, one expected response per trace
        refs += [{"context": {"spanContext": {"sessionId": session_id, "traceId": trace}},
                  "expectedResponse": {"text": text}}
                 for trace, text in zip(traces, turns, strict=True) if text]
    elif truth.get("expected_response") and traces:
        refs.append({"context": {"spanContext": {"sessionId": session_id,
                                                 "traceId": traces[-1]}},
                     "expectedResponse": {"text": truth["expected_response"]}})
    return refs


def requests_for(entry: SessionRecords, evaluator_id: str, level: str,
                 truth: dict[str, Any] | None, *, own_only: bool = False
                 ) -> list[dict[str, Any]]:
    spans = session_spans(entry, own_only=own_only)
    traces = agent_traces(entry)
    refs = reference_inputs(entry.session_id, truth, traces)
    base: dict[str, Any] = {"evaluatorId": evaluator_id,
                            "evaluationInput": {"sessionSpans": spans}}
    if refs:
        base["evaluationReferenceInputs"] = refs
    if level == "TRACE":
        targets = [{"traceIds": traces[i:i + TARGET_CHUNK]}
                   for i in range(0, len(traces), TARGET_CHUNK)]
    elif level == "TOOL_CALL":
        ids = tool_spans(entry)
        targets = [{"spanIds": ids[i:i + TARGET_CHUNK]} for i in range(0, len(ids), TARGET_CHUNK)]
    else:
        targets = [{}]
    return [{**base, **({"evaluationTarget": t} if t else {})} for t in targets]


def _key(request: dict[str, Any], rep: int) -> str:
    raw = json.dumps([request, rep], sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()[:24]


def evaluate(client: Any, cache: RecordCache, request: dict[str, Any], rep: int
             ) -> dict[str, Any]:
    """One ``Evaluate`` call (or its cached response): ``{ok, results | error}``."""
    key = _key(request, rep)
    cached = cache.load_json("global", key)
    if cached is not None:
        return cached
    try:
        out = client.evaluate(**request)
        response = {"ok": True, "results": out.get("evaluationResults", [])}
    except Exception as exc:  # noqa: BLE001 — an errored request is a report line
        response = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:1500]}"}
    if response["ok"]:  # never cache a failure: the next run asks again
        cache.save_json("global", key, response)
    return response


def to_records(results: list[dict[str, Any]], level: str, rep: int) -> list[dict[str, Any]]:
    out = []
    for r in results:
        context = (r.get("context") or {}).get("spanContext") or {}
        out.append({
            "session_id": context.get("sessionId"), "trace_id": context.get("traceId"),
            "span_id": context.get("spanId"), "evaluator_id": r.get("evaluatorId"),
            "level": level, "score": r.get("value"), "label": r.get("label"),
            "explanation": r.get("explanation"), "error_type": r.get("errorCode"),
            "error_message": r.get("errorMessage"), "rep": rep,
            "ignored_reference_fields": r.get("ignoredReferenceInputFields"),
        })
    return out


def run(client: Any, cache: RecordCache, entries: list[SessionRecords],
        evaluator_ids: list[str], truth: dict[str, dict[str, Any]], *, reps: int = 1,
        own_spans: bool = False,
        log: Callable[[str], None] = print) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """``(records, skipped)`` — every builtin evaluator × session × rep. With ``own_spans``
    a session that carries a peer's spans is sent with its own-service spans only, and its
    records are marked ``own_spans_only``."""
    from app.services.strands_eval import evaluators as evaluator_lib

    records: list[dict[str, Any]] = []
    skipped = [{"id": e, "reason": "not a Global builtin (no CreateEvaluator)"}
               for e in evaluator_ids if e not in evaluator_lib.LEVELS]
    chosen = [e for e in evaluator_ids if e in evaluator_lib.LEVELS]
    for evaluator_id in chosen:
        level = evaluator_lib.LEVELS[evaluator_id]
        for entry in entries:
            own_only = own_spans and foreign_spans(entry) > 0
            for request in requests_for(entry, evaluator_id, level, truth.get(entry.session_id),
                                        own_only=own_only):
                for rep in range(reps):
                    response = evaluate(client, cache, request, rep)
                    if not response["ok"]:
                        log(f"  Global {evaluator_id} {entry.session_id[:12]} rep {rep}: "
                            f"{response['error'][:200]}")
                        records.append({"session_id": entry.session_id, "trace_id": None,
                                        "span_id": None, "evaluator_id": evaluator_id,
                                        "level": level, "score": None, "label": None,
                                        "explanation": None, "error_type": "RequestFailed",
                                        "error_message": response["error"], "rep": rep,
                                        "own_spans_only": own_only})
                        continue
                    records.extend({**rec, "own_spans_only": own_only}
                                   for rec in to_records(response["results"], level, rep))
        log(f"  Global {evaluator_id}: done")
    return records, skipped


def load_results(path: str) -> list[dict[str, Any]]:
    """``--global-results``: responses produced elsewhere (one JSON object per line)."""
    from app.services.strands_eval import evaluators as evaluator_lib

    records: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            item = json.loads(line)
            level = evaluator_lib.LEVELS.get(str(item.get("evaluatorId")), "TRACE")
            records.extend(to_records(item.get("evaluationResults") or [], level,
                                      int(item.get("rep") or 0)))
    return records
