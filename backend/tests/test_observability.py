"""Observability service — tree building, cache TTL, mapper, transcript, API."""

import json

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.models.ledger import Agent, ChatMessage, ChatSession
from app.services import memory_backend
from app.services import observability as obs

from .conftest import ws_ctx


class _FakeMemoryBackend:
    """Just enough of the ``MemoryBackend`` Protocol for the transcript reads. Tests
    swap the three callables (raw-list stubs, as the pre-Protocol tests did) and the
    adapter wraps them in the Protocol's page shapes."""

    def __init__(self):
        self.actor_ids = lambda *a, **k: []
        self.events = lambda *a, **k: []
        self.records = lambda *a, **k: []

    def list_actors(self, workspace, next_token=None, max_results=None):
        return {"items": [{"actor_id": a} for a in self.actor_ids(workspace)], "next_token": None}

    def list_events(self, workspace, actor_id, session_id, include_payloads=True,
                    next_token=None, max_results=None):
        return {"items": list(self.events(workspace, actor_id, session_id, max_results)),
                "next_token": None}

    def list_records(self, workspace, namespace, strategy_id=None, next_token=None,
                     max_results=None):
        return {"items": list(self.records(workspace, namespace, max_results=max_results)),
                "next_token": None}


_FAKE = _FakeMemoryBackend()


@pytest.fixture(autouse=True)
def _memory_backend(client):
    # Depends on `client` so this fixture's `set_backend(_FAKE)` runs AFTER
    # `create_app()` (inside `client`) registers the real self-hosted backend
    # (N6) — otherwise autouse fixtures set up before explicitly-requested ones
    # of the same scope and `create_app()` would clobber the fake right back.
    global _FAKE
    _FAKE = _FakeMemoryBackend()
    memory_backend.set_backend(_FAKE)
    yield _FAKE
    memory_backend.set_backend(None)

BASE_NS = 1_700_000_000_000_000_000


def _span(span_id, parent, name, start_ms, end_ms, kind="INTERNAL", attrs=None, status="OK"):
    return {
        "traceId": "a" * 32,
        "spanId": span_id,
        **({"parentSpanId": parent} if parent else {}),
        "name": name,
        "kind": kind,
        "startTimeUnixNano": BASE_NS + start_ms * 1_000_000,
        "endTimeUnixNano": BASE_NS + end_ms * 1_000_000,
        "durationNano": (end_ms - start_ms) * 1_000_000,
        "attributes": attrs or {},
        "status": {"code": status},
    }


TREE_SPANS = [
    _span("root", None, "POST /invocations", 0, 10, kind="SERVER"),
    _span("agent", "root", "invoke_agent Strands Agents", 2, 8,
          attrs={"gen_ai.operation.name": "invoke_agent"}),
    _span("llm", "agent", "chat global.anthropic.claude-sonnet-4-6", 3, 7,
          attrs={"gen_ai.operation.name": "chat",
                 "gen_ai.request.model": "global.anthropic.claude-sonnet-4-6",
                 "gen_ai.usage.input_tokens": 1000, "gen_ai.usage.output_tokens": 100,
                 "gen_ai.usage.cache_read_input_tokens": 0,
                 "gen_ai.usage.cache_write_input_tokens": 0,
                 "session.id": "s" * 64}),
]


# Strands double-emission shape: wrapper + terminal chat spans, same tokens.
_LLM_USAGE = {"gen_ai.operation.name": "chat",
              "gen_ai.request.model": "global.anthropic.claude-sonnet-4-6",
              "gen_ai.usage.input_tokens": 500, "gen_ai.usage.output_tokens": 50,
              "gen_ai.usage.cache_read_input_tokens": 0,
              "gen_ai.usage.cache_write_input_tokens": 0}
DEDUP_SPANS = [
    _span("root2", None, "POST /invocations", 0, 10, kind="SERVER"),
    _span("wrap", "root2", "chat", 1, 9,
          attrs={**_LLM_USAGE, "gen_ai.system": "strands-agents"}),
    _span("term", "wrap", "chat global.anthropic.claude-sonnet-4-6", 1, 9,
          attrs={**_LLM_USAGE, "gen_ai.system": "aws.bedrock"}),
]

NATIVE_SPANS = [
    _span("native-root", None, "ClaudeAgentSDK.query", 0, 10, attrs={
        "openinference.span.kind": "AGENT",
        "llm.model_name": "global.anthropic.claude-sonnet-4-6",
        "llm.token_count.prompt": 700,
        "llm.token_count.completion": 42,
        "llm.token_count.prompt_details.cache_read": 100,
        "llm.token_count.prompt_details.cache_write": 10,
        "session.id": "n" * 64,
    }),
    _span("native-tool", "native-root", "ClaudeAgentSDK.tool", 2, 4, attrs={
        "openinference.span.kind": "TOOL",
        "tool.name": "calculator",
    }),
]


# trace with runtime-log-group resources + gen_ai message events attached
# N17-obs307: ADOT's botocore spans carry the caller's STS access-key id
FAKE_STS_KEY_ID = "ASIAEXAMPLE00000TEST"
CREDENTIAL_SPANS = [
    _span("root4", None, "POST /invocations", 0, 10, kind="SERVER",
          attrs={"session.id": "s" * 64}),
    _span("s3", "root4", "S3.GetObject", 2, 4, kind="CLIENT",
          attrs={"rpc.service": "S3", "aws.auth.account.access_key": FAKE_STS_KEY_ID,
                 "aws.auth.region": "cn-northwest-1",
                 "http.url": f"https://b.s3/k?X-Amz-Credential={FAKE_STS_KEY_ID}%2F2026"}),
]

MSG_SPANS = [
    {**_span("root3", None, "POST /invocations", 0, 10, kind="SERVER"),
     "resource": {"attributes": {
         "aws.log.group.names": "/aws/bedrock-agentcore/runtimes/test-DEFAULT"}}},
    {**_span("llm2", "root3", "chat global.anthropic.claude-sonnet-4-6", 1, 9,
             attrs={**_LLM_USAGE, "gen_ai.system": "aws.bedrock"}),
     "resource": {"attributes": {
         "aws.log.group.names": "/aws/bedrock-agentcore/runtimes/test-DEFAULT"}}},
]
MSG_EVENT = {
    "scope": {"name": "strands.telemetry.tracer"},
    "body": {
        "input": {"messages": [
            {"content": {"content": json.dumps([{"text": "How many days?"}])},
             "role": "user"}]},
        "output": {"messages": [
            {"content": {"message": json.dumps([{"text": "hello there"}]),
                         "finish_reason": "end_turn"}, "role": "assistant"}]},
    },
    "traceId": "d" * 32,
    "spanId": "llm2",
}


@pytest.fixture(autouse=True)
def fresh_cache():
    obs.reset_cache()
    yield
    obs.reset_cache()


# ── span tree ───────────────────────────────────────────────────────────────


def test_span_tree_nesting_and_offsets():
    tree = obs.build_span_tree(TREE_SPANS)
    assert tree["duration_ms"] == 10.0
    root = tree["tree"][0]
    assert root["name"] == "POST /invocations" and root["depth"] == 0
    agent = root["children"][0]
    assert agent["depth"] == 1 and agent["category"] == "agent"
    llm = agent["children"][0]
    assert llm["depth"] == 2 and llm["category"] == "llm"
    assert llm["start_offset_ms"] == 3.0 and llm["duration_ms"] == 4.0
    assert llm["offset_pct"] == 30.0 and llm["width_pct"] == 40.0
    assert "est_cost_usd" not in llm  # R13: no USD estimates on spans
    # flat rows keep raw attributes; tree nodes do not carry them
    flat_llm = next(s for s in tree["spans"] if s["span_id"] == "llm")
    assert flat_llm["attributes"]["session.id"] == "s" * 64
    assert "attributes" not in llm


def test_span_tree_orphan_becomes_root():
    spans = [TREE_SPANS[0], _span("lost", "missing-parent", "chat x", 1, 2)]
    tree = obs.build_span_tree(spans)
    assert {n["span_id"] for n in tree["tree"]} == {"root", "lost"}


def test_native_claude_span_tree_uses_openinference_attributes():
    tree = obs.build_span_tree(NATIVE_SPANS)
    root = tree["tree"][0]
    assert root["category"] == "agent"
    assert root["model"] == "global.anthropic.claude-sonnet-4-6"
    # N17-obs439: OpenInference's prompt count (700) includes its cache breakdown, so the
    # uncached input is 700 - 100 - 10
    assert root["tokens"] == {
        "input": 590.0,
        "output": 42.0,
        "cache_read": 100.0,
        "cache_write": 10.0,
    }
    tool = root["children"][0]
    assert tool["category"] == "tool"
    assert tool["tool_name"] == "calculator"


def test_categorize_span_contract():
    cases = [
        ("chat global.anthropic.claude-sonnet-4-6", {}, None, "llm"),
        ("execute_tool hr-database___get_employee", {}, None, "tool"),
        ("Bedrock AgentCore.ListEvents", {}, None, "memory"),
        ("Bedrock AgentCore.CreateEvent", {}, None, "memory"),
        ("mcp tools/call hr-database___get_employee", {}, None, "gateway"),
        ("Bedrock AgentCore.GetResourceOauth2Token", {}, None, "gateway"),
        ("POST /invocations", {}, "SERVER", "http"),
        ("GET", {"http.method": "GET"}, "CLIENT", "http"),
        ("invoke_agent Strands Agents", {"gen_ai.operation.name": "invoke_agent"},
         None, "agent"),
        ("execute_event_loop_cycle", {}, None, "agent"),
        ("something-else", {}, "CLIENT", "other"),
        # strong signals beat substring needles (review finding #11)
        ("execute_tool search_memory", {}, None, "tool"),
        ("execute_tool mcp_lookup", {"gen_ai.tool.name": "mcp_lookup"}, None, "tool"),
        ("chat agent helper", {"gen_ai.operation.name": "chat"}, None, "llm"),
        ("ClaudeAgentSDK.query", {"openinference.span.kind": "AGENT"}, None, "agent"),
        ("ClaudeAgentSDK.tool", {"openinference.span.kind": "TOOL"}, None, "tool"),
        ("tool callback", {"tool.name": "calculator"}, None, "tool"),
    ]
    for name, attrs, kind, expected in cases:
        assert obs.categorize_span(name, attrs, kind) == expected, name


def test_query_builders_reject_unvalidated_ids():
    # Defense in depth: ids are interpolated into Logs Insights query strings,
    # so the builders themselves refuse anything outside the id alphabets —
    # even if a future caller skips the router validation.
    from app.core.errors import AppError

    with pytest.raises(AppError):
        obs.q_trace_spans('deadbeef" | fields @message | filter "x')
    with pytest.raises(AppError):
        obs.q_trace_aggregates(session_id='x" or traceId like "')
    with pytest.raises(AppError):
        obs.q_trace_aggregates(session_id="short")
    with pytest.raises(AppError):
        obs.q_session_spans('01KKRNGA47RK9XD3ZKDWX1R5WD#feishu#x" | fields @message')


def test_query_builders_accept_composite_external_session_ids():
    # Regression: an external (Feishu) caller mints runtimeSessionIds such as
    # `<ulid>#feishu#<chat_id>`. The list view showed the session (ids come
    # straight from the spans) but the detail route 422'd on `#`, which the
    # console rendered as "session not found".
    sid = "01KKRNGA47RK9XD3ZKDWX1R5WD#feishu#oc_115a912b912ff5efb7895263ccc1ea70"
    assert f'attributes.session.id = "{sid}"' in obs.q_trace_aggregates(session_id=sid)
    assert f'attributes.session.id = "{sid}"' in obs.q_session_spans(sid)


def test_llm_aggregation_excludes_framework_wrapper_spans():
    # Strands double-emits every LLM call (wrapper system=strands-agents +
    # terminal system=aws.bedrock with identical tokens); the conditional-sum
    # fields must exclude the wrapper or all token sums double.
    query = obs.q_trace_aggregates()
    assert 'strcontains(coalesce(attributes.gen_ai.system, ""), "strands-agents")' in query
    assert (
        'strcontains(coalesce(attributes.gen_ai.operation.name, ""), "chat")'
        in query
    )


def test_native_claude_query_builders_coalesce_openinference_fields():
    aggregate_queries = (obs.q_trace_aggregates(), obs.q_session_aggregates())
    for query in aggregate_queries:
        assert (
            "coalesce(attributes.gen_ai.request.model, attributes.llm.model_name) "
            "as telemetry_model"
            in query
        )
        assert "attributes.llm.token_count.prompt" in query
        assert "attributes.llm.token_count.completion" in query
        assert "attributes.llm.token_count.prompt_details.cache_read" in query
        assert "attributes.llm.token_count.prompt_details.cache_write" in query
        assert "attributes.openinference.span.kind" in query
        assert 'coalesce(attributes.gen_ai.operation.name, "")' in query

    model_query = obs.q_tokens_by_model()
    assert "attributes.llm.model_name" in model_query
    assert "attributes.openinference.span.kind" in model_query
    assert 'coalesce(attributes.gen_ai.operation.name, "")' in model_query
    assert "by telemetry_model as model" in model_query

    tool_query = obs.q_top_tools()
    assert "coalesce(attributes.gen_ai.tool.name, attributes.tool.name) as tool" in tool_query
    assert "by tool" in tool_query


def test_trace_meta_dedupes_wrapper_llm_spans(client, mocked_aws):
    res = client.get(f"/api/observability/traces/{'c' * 32}")
    assert res.status_code == 200
    meta = res.json()["meta"]
    # wrapper (strands-agents) + terminal (aws.bedrock) with identical tokens
    # → only the terminal span counts
    assert meta["llm_count"] == 1
    assert meta["tokens"]["input"] == 500 and meta["tokens"]["output"] == 50


def test_trace_meta_counts_native_agent_root_as_token_call():
    trace_id = "e" * 32
    fake = FakeLogs({
        f'filter traceId = "{trace_id}"': [
            {"@message": json.dumps({**span, "traceId": trace_id})}
            for span in NATIVE_SPANS
        ]
    })
    db = SessionLocal()
    result = obs.get_trace(trace_id, "24h", db, ws_ctx(), logs=fake)
    db.close()
    meta = result["meta"]
    assert meta["llm_count"] == 1
    # N17-obs439: total = uncached input + output + cache read + cache write — for an
    # OpenInference span still its prompt (700) + completion (42)
    assert meta["tokens"] == {
        "input": 590,
        "output": 42,
        "cache_read": 100,
        "cache_write": 10,
        "total": 742,
    }
    native = next(span for span in result["spans"] if span["span_id"] == "native-root")
    assert native["category"] == "agent"
    assert native["model"] == "global.anthropic.claude-sonnet-4-6"


def test_session_filtered_query_is_well_formed():
    # Regression: the session variant once emitted a leading "| filter" with no
    # pipe before the next stage — a Logs Insights syntax error (502 in e2e).
    query = obs.q_trace_aggregates(session_id="abc12345").strip()
    assert query.startswith("SOURCE logGroups(")
    assert 'attributes.session.id = "abc12345"' in query
    assert '\n| fields' in query
    assert not query.startswith("|")


def test_global_span_queries_cover_legacy_and_unified_groups():
    queries = [
        obs.q_trace_aggregates(),
        obs.q_root_spans(),
        obs.q_session_aggregates(),
        obs.q_dashboard_series("24h"),
        obs.q_dashboard_totals(),
        obs.q_dashboard_distincts(),
        obs.q_top_tools(),
        obs.q_trace_spans("a" * 32),
    ]
    for query in queries:
        assert query.strip().startswith(obs.SPANS_SOURCE)
        assert "ispresent(startTimeUnixNano)" in query
    assert "aws/spans" in obs.SPANS_SOURCE
    assert "/aws/bedrock-agentcore/runtimes/" in obs.SPANS_SOURCE


def test_spans_source_narrows_to_spans_only(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("LAUNCHPAD_OBSERVABILITY_SPANS_SOURCE", "spans")
    get_settings.cache_clear()
    try:
        query = obs.q_trace_aggregates()
        assert "aws/spans" in query
        assert "/aws/bedrock-agentcore/runtimes/" not in query
    finally:
        get_settings.cache_clear()


def test_spans_source_narrows_to_runtime_logs_only(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("LAUNCHPAD_OBSERVABILITY_SPANS_SOURCE", "runtime_logs")
    get_settings.cache_clear()
    try:
        query = obs.q_session_aggregates()
        assert "/aws/bedrock-agentcore/runtimes/" in query
        assert "'aws/spans'" not in query
    finally:
        get_settings.cache_clear()


def test_spans_source_default_both_matches_the_public_constant():
    # Default settings must keep every pre-existing assertion against the
    # module-level SPANS_SOURCE constant true (test_observability.py already has
    # several `query.strip().startswith(obs.SPANS_SOURCE)` checks elsewhere).
    assert obs.q_dashboard_totals().strip().startswith(obs.SPANS_SOURCE)


def test_span_aggregates_preserve_string_metadata_with_latest():
    trace_query = obs.q_trace_aggregates()
    session_query = obs.q_session_aggregates()
    assert "latest(attributes.session.id) as session_id" in trace_query
    for query in (trace_query, session_query):
        assert "latest(resource.attributes.service.name) as service" in query
        assert "latest(telemetry_model) as model" in query
        assert "max(resource.attributes.service.name)" not in query
        assert "max(telemetry_model)" not in query


def test_parse_message_events_from_runtime_log_record():
    # Shape observed live in /aws/bedrock-agentcore/runtimes/*-DEFAULT
    # (otel-rt-logs): strands.telemetry.tracer log records with
    # body.input/output.messages whose content is a JSON string of blocks.
    record = {
        "scope": {"name": "strands.telemetry.tracer"},
        "severityNumber": 9,
        "body": {
            "output": {"messages": [{
                "content": {
                    "message": json.dumps([
                        {"text": "Sure! Let me look that up right away."},
                        {"toolUse": {"toolUseId": "tooluse_X",
                                     "name": "hr-database___get_employee",
                                     "input": {"employee_id": "EMP-4096"}}},
                    ]),
                    "finish_reason": "tool_use",
                },
                "role": "assistant",
            }]},
            "input": {"messages": [
                {"content": {"content": json.dumps([{"text": "You are the HR assistant."}])},
                 "role": "system"},
                {"content": {"content": json.dumps([{"text": "x" * 5000}])},
                 "role": "user"},
            ]},
        },
        "traceId": "6a" * 16,
        "spanId": "cd2fb023cc6041cc",
    }
    by_span = obs.parse_message_events([{"@message": json.dumps(record)}])
    entry = by_span["cd2fb023cc6041cc"]
    assert [m["role"] for m in entry["input"]] == ["system", "user"]
    assert len(entry["input"][1]["blocks"][0]["text"]) == obs.MESSAGE_TEXT_CAP  # truncated
    out = entry["output"][0]
    assert out["finish_reason"] == "tool_use"
    assert out["blocks"][0] == {"type": "text",
                                "text": "Sure! Let me look that up right away."}
    assert out["blocks"][1]["type"] == "tool_use"
    assert out["blocks"][1]["name"] == "hr-database___get_employee"
    assert '"EMP-4096"' in out["blocks"][1]["input"]
    # non-message records (plain logs, unparsable) are ignored
    assert obs.parse_message_events([{"@message": "not json"},
                                     {"@message": json.dumps({"spanId": "x"})}]) == {}
    # content may be a plain string (bedrock-runtime scope events) — no crash
    plain = {"spanId": "s1", "traceId": "t",
             "body": {"input": {"messages": [
                 {"content": "raw prompt text", "role": "user"}]}}}
    parsed = obs.parse_message_events([{"@message": json.dumps(plain)}])
    assert parsed["s1"]["input"][0]["blocks"][0]["text"] == "raw prompt text"


def test_trace_detail_attaches_span_messages(client, mocked_aws):
    res = client.get(f"/api/observability/traces/{'d' * 32}")
    assert res.status_code == 200
    spans = res.json()["spans"]
    llm = next(s for s in spans if s["span_id"] == "llm2")
    assert llm["messages"]["output"][0]["blocks"][0]["text"] == "hello there"
    assert llm["messages"]["input"][0]["role"] == "user"
    root = next(s for s in spans if s["span_id"] == "root3")
    assert root["messages"] is None



def test_trace_detail_masks_credential_attributes(client, mocked_aws):
    """N17-obs307: the access-key id never leaves the API — the key stays, its value
    reads `[redacted]`, a key id inside another value is masked in place."""
    res = client.get(f"/api/observability/traces/{'e' * 32}")
    assert res.status_code == 200
    assert FAKE_STS_KEY_ID not in res.text
    s3 = next(s for s in res.json()["spans"] if s["span_id"] == "s3")
    assert s3["attributes"]["aws.auth.account.access_key"] == "[redacted]"
    assert s3["attributes"]["http.url"] == (
        "https://b.s3/k?X-Amz-Credential=[redacted]%2F2026")
    assert s3["attributes"]["aws.auth.region"] == "cn-northwest-1"
    assert s3["attributes"]["rpc.service"] == "S3"
    # masking runs before the cache: a cached read is masked too
    again = client.get(f"/api/observability/traces/{'e' * 32}")
    assert FAKE_STS_KEY_ID not in again.text


def test_build_span_tree_masks_credential_attributes():
    tree = obs.build_span_tree(CREDENTIAL_SPANS)
    s3 = next(s for s in tree["spans"] if s["span_id"] == "s3")
    assert s3["attributes"]["aws.auth.account.access_key"] == "[redacted]"
    assert FAKE_STS_KEY_ID not in json.dumps(tree)
    # the caller's span list is not mutated (raw spans still feed service / log groups)
    assert CREDENTIAL_SPANS[1]["attributes"]["aws.auth.account.access_key"] == FAKE_STS_KEY_ID


# ── fakes ───────────────────────────────────────────────────────────────────


class FakeLogs:
    """Logs Insights stub: routes queries to canned rows by marker substring."""

    def __init__(self, rows_by_marker):
        self.rows_by_marker = rows_by_marker
        self.start_calls = 0
        self.start_kwargs = []
        self._queries = {}

    def start_query(self, **kwargs):
        self.start_calls += 1
        self.start_kwargs.append(kwargs)
        qid = f"q{self.start_calls}"
        self._queries[qid] = kwargs["queryString"]
        return {"queryId": qid}

    def get_query_results(self, queryId):
        query = self._queries[queryId]
        rows = []
        for marker, canned in self.rows_by_marker.items():
            if marker in query:
                rows = canned
                break
        return {
            "status": "Complete",
            "results": [[{"field": k, "value": str(v)} for k, v in row.items()]
                        for row in rows],
        }


class FakeCW:
    def __init__(self, metrics=None, values=None):
        self.metrics = metrics or []
        self.values = values or {}

    def get_paginator(self, name):
        assert name == "list_metrics"
        pages = [{"Metrics": self.metrics}]

        class P:
            def paginate(_, **kwargs):
                return iter(pages)

        return P()

    def get_metric_data(self, MetricDataQueries, StartTime, EndTime):
        return {
            "MetricDataResults": [
                {"Id": q["Id"], "Values": self.values.get(q["Id"], [])}
                for q in MetricDataQueries
            ]
        }


AGG_ROW = {
    "traceId": "b" * 32, "span_count": 9, "llm_count": 1, "tokens_in": 1828,
    "tokens_out": 34, "cache_read": 0, "cache_write": 0, "error_count": 0,
    "start_ns": BASE_NS, "end_ns": BASE_NS + 3_200_000_000,
    "session_id": "s" * 64, "service": "harness_hr_assistant.DEFAULT",
    "model": "global.anthropic.claude-sonnet-4-6", "model_count": 1,
}
ROOT_ROW = {
    "name": "POST /invocations", "traceId": "b" * 32,
    "service": "harness_hr_assistant.DEFAULT", "durationNano": 3_200_000_000,
    "startTimeUnixNano": BASE_NS, "status_code": "UNSET",
}
SESSION_ROW = {
    "session_id": "s" * 64, "traces": 2, "llm_calls": 3, "tokens_in": 5000,
    "tokens_out": 200, "errors": 0, "first_ns": BASE_NS,
    "last_ns": BASE_NS + 60_000_000_000, "service": "harness_hr_assistant.DEFAULT",
    "model": "global.anthropic.claude-sonnet-4-6",
}


def _fake_logs():
    return FakeLogs({
        "by traceId": [AGG_ROW],
        "fields name, traceId": [ROOT_ROW],
        "by attributes.session.id as session_id": [SESSION_ROW],
        "by bin(": [{"bucket": "2026-07-10 00:00:00.000", "traces": 5, "errors": 1,
                     "p50_nano": 3_100_000_000, "p95_nano": 11_800_000_000}],
        # totals query: same aggregates as series but no bin() — must come after
        "pct(durationNano": [{"traces": 5, "errors": 1, "p50_nano": 3_100_000_000,
                              "p95_nano": 11_800_000_000}],
        "count_distinct(attributes.session.id) as sessions": [
            {"sessions": 3, "agents": 2}],
        "by tool": [
            {"tool": "hr-database___get_employee", "calls": 9, "errors": 1}],
        f'filter traceId = "{"c" * 32}"': [
            {"@message": json.dumps({**s, "traceId": "c" * 32})} for s in DEDUP_SPANS],
        # message-events query (runtime log groups) must route before the
        # span query for the same trace id
        f'filter traceId = "{"d" * 32}"\n| fields @message, spanId': [
            {"@message": json.dumps(MSG_EVENT)}],
        f'filter traceId = "{"e" * 32}"': [
            {"@message": json.dumps({**s, "traceId": "e" * 32})} for s in CREDENTIAL_SPANS],
        f'filter traceId = "{"d" * 32}"': [
            {"@message": json.dumps({**s, "traceId": "d" * 32})} for s in MSG_SPANS],
        "fields @message": [{"@message": json.dumps(s)} for s in TREE_SPANS],
    })


def _seed_agent(name="hr-assistant", resource_id="hr_assistant-Flr7ibmASq",
                status="active", method="harness"):
    db = SessionLocal()
    agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=name, method=method,
                  status=status, resource_id=resource_id,
                  arn=f"arn:aws:bedrock-agentcore:us-west-2:1:harness/{resource_id}")
    db.add(agent)
    db.commit()
    agent_id = agent.id
    db.close()
    return agent_id


# ── cache TTL ───────────────────────────────────────────────────────────────


def test_cache_second_call_hits_no_aws(monkeypatch):
    fake = _fake_logs()
    db = SessionLocal()
    first = obs.list_traces("24h", db, ws_ctx(), logs=fake)
    calls_after_first = fake.start_calls
    second = obs.list_traces("24h", db, ws_ctx(), logs=fake)
    db.close()
    assert calls_after_first == 2  # aggregates + roots
    assert fake.start_calls == calls_after_first  # cache hit → no new queries
    assert first["cache"]["hit"] is False and second["cache"]["hit"] is True
    assert second["traces"] == first["traces"]


def test_global_queries_use_source_instead_of_enumerated_log_groups():
    fake = _fake_logs()
    db = SessionLocal()
    obs.list_traces("24h", db, ws_ctx(), logs=fake)
    db.close()
    assert len(fake.start_kwargs) == 2
    for call in fake.start_kwargs:
        assert call["queryString"].strip().startswith(obs.SPANS_SOURCE)
        assert "logGroupName" not in call
        assert "logGroupNames" not in call


def test_explicit_runtime_group_query_keeps_log_group_names():
    fake = _fake_logs()
    groups = ["/aws/bedrock-agentcore/runtimes/test-DEFAULT"]
    obs.run_insights_queries(
        {"events": obs.q_trace_message_events("d" * 32)},
        1,
        logs=fake,
        log_groups=groups,
    )
    call = fake.start_kwargs[0]
    assert call["logGroupNames"] == groups
    assert not call["queryString"].strip().startswith("SOURCE ")


def test_cache_force_bypasses_and_ttl_expires(monkeypatch):
    fake = _fake_logs()
    db = SessionLocal()
    obs.list_traces("24h", db, ws_ctx(), logs=fake)
    obs.list_traces("24h", db, ws_ctx(), force=True, logs=fake)
    assert fake.start_calls == 4  # force re-ran both queries
    base = obs._now()
    monkeypatch.setattr(obs, "_now", lambda: base + obs.CACHE_TTL_SECONDS + 1)
    obs.list_traces("24h", db, ws_ctx(), logs=fake)
    db.close()
    assert fake.start_calls == 6  # TTL expired → re-queried


# ── agent mapper ────────────────────────────────────────────────────────────


def test_agent_mapper_ledger_and_fallback():
    _seed_agent()
    _seed_agent(name="eval-target", resource_id="eval_target_e02c0f-RNlJ17DBlt")
    db = SessionLocal()
    mapper = obs.build_agent_mapper(db, "default")
    db.close()
    assert mapper("harness_hr_assistant.DEFAULT") == "hr-assistant"
    assert mapper("eval_target_e02c0f.DEFAULT") == "eval-target"
    assert mapper("clawbot-agent-runtime") == "clawbot-agent-runtime"  # raw fallback
    assert mapper(None) == "unknown"


def test_agent_mapper_prefers_active_over_deleted():
    _seed_agent(name="old-name", resource_id="hr_assistant-AAAA", status="deleted")
    _seed_agent(name="hr-assistant", resource_id="hr_assistant-Flr7ibmASq")
    db = SessionLocal()
    mapper = obs.build_agent_mapper(db, "default")
    db.close()
    assert mapper("harness_hr_assistant.DEFAULT") == "hr-assistant"


# ── transcript ──────────────────────────────────────────────────────────────


def test_transcript_no_ledger_row_and_no_memory_is_unavailable(monkeypatch):
    monkeypatch.setattr(_FAKE, "actor_ids", lambda *a, **k: [])
    monkeypatch.setattr(_FAKE, "events", lambda *a, **k: [])
    db = SessionLocal()
    result = obs.session_transcript(db, "external-session-id-123", ws_ctx())
    db.close()
    assert result == {"available": False, "reason": "not_platform_session"}


def test_transcript_memory_error_degrades(monkeypatch):
    agent_id = _seed_agent()
    db = SessionLocal()
    db.add(ChatSession(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                       session_id="s" * 64, actor_id="river"))
    db.commit()

    def boom(*args, **kwargs):
        raise RuntimeError("memory down")

    monkeypatch.setattr(_FAKE, "events", boom)
    result = obs.session_transcript(db, "s" * 64, ws_ctx())
    db.close()
    assert result["available"] is False and result["reason"] == "memory_unavailable"
    assert "memory down" in result["detail"]


def test_transcript_orders_turns(monkeypatch):
    agent_id = _seed_agent()
    db = SessionLocal()
    db.add(ChatSession(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                       session_id="s" * 64, actor_id="river"))
    db.commit()
    events = [
        {"eventTimestamp": "2026-07-10T02:00:00", "payload": [
            {"conversational": {"role": "USER", "content": {"text": "second q"}}}]},
        {"eventTimestamp": "2026-07-10T01:00:00", "payload": [
            {"conversational": {"role": "USER", "content": {"text": "first q"}}},
            {"conversational": {"role": "ASSISTANT", "content": {"text": "first a"}}}]},
    ]
    monkeypatch.setattr(_FAKE, "events", lambda *a, **k: events)
    monkeypatch.setattr(_FAKE, "records", lambda *a, **k: [{"id": 1}])
    result = obs.session_transcript(db, "s" * 64, ws_ctx())
    db.close()
    assert result["available"] is True and result["agent_name"] == "hr-assistant"
    assert [t["text"] for t in result["turns"]] == ["first q", "first a", "second q"]
    assert result["long_term_records"] == 2


def test_transcript_reconciles_incomplete_memory_from_chat_ledger(monkeypatch):
    agent_id = _seed_agent()
    session_id = "s" * 64
    db = SessionLocal()
    db.add(
        ChatSession(
            workspace_id=DEFAULT_WORKSPACE_ID,
            agent_id=agent_id,
            session_id=session_id,
            actor_id="runtime-diagnostic",
        )
    )
    db.add_all(
        [
            ChatMessage(
                workspace_id=DEFAULT_WORKSPACE_ID,
                agent_id=agent_id,
                session_id=session_id,
                role="user",
                text="first question",
            ),
            ChatMessage(
                workspace_id=DEFAULT_WORKSPACE_ID,
                agent_id=agent_id,
                session_id=session_id,
                role="agent",
                text="first answer",
            ),
            ChatMessage(
                workspace_id=DEFAULT_WORKSPACE_ID,
                agent_id=agent_id,
                session_id=session_id,
                role="user",
                text="latest question",
            ),
            ChatMessage(
                workspace_id=DEFAULT_WORKSPACE_ID,
                agent_id=agent_id,
                session_id=session_id,
                role="agent",
                text="latest answer",
            ),
        ]
    )
    db.commit()
    monkeypatch.setattr(
        _FAKE,
        "events",
        lambda *a, **k: [
            {
                "eventTimestamp": "2026-07-10T01:00:00",
                "payload": [
                    {
                        "conversational": {
                            "role": "USER",
                            "content": {"text": "first question"},
                        }
                    },
                    {
                        "conversational": {
                            "role": "ASSISTANT",
                            "content": {"text": "first answer"},
                        }
                    },
                ],
            }
        ],
    )
    monkeypatch.setattr(_FAKE, "records", lambda *a, **k: [])

    result = obs.session_transcript(db, session_id, ws_ctx())
    db.close()

    assert result["origin"] == "ledger"
    assert [turn["text"] for turn in result["turns"]] == [
        "first question",
        "first answer",
        "latest question",
        "latest answer",
    ]


# ── transcript: sessions in NO platform ledger (gateway traffic, /v1 callers) ─


EXTERNAL_SID = "ee760f57-2757-4761-947b-83f1ec6fa022"


def _events_for(actor_map):
    """memory.list_events stub: {actor_id: [texts]} → conversational events."""
    probed = []

    def fake_events(_ws, actor_id, session_id, max_results=20, memory_id=None):
        probed.append(actor_id)
        return [
            {"eventTimestamp": f"2026-07-28T0{i}:00:00", "payload": [
                {"conversational": {"role": "USER", "content": {"text": text}}}]}
            for i, text in enumerate(actor_map.get(actor_id, []), start=1)
        ]

    return fake_events, probed


def test_transcript_external_session_reads_bare_default_actor(monkeypatch):
    """Experiment gateway traffic: the runtime persisted the conversation under
    the BARE "default" actor and no ledger row exists — the transcript must
    still resolve."""
    agent_id = _seed_agent()
    fake_events, probed = _events_for({"default": ["second q", "first q"]})
    monkeypatch.setattr(_FAKE, "actor_ids", lambda *a, **k: [])
    monkeypatch.setattr(_FAKE, "events", fake_events)
    db = SessionLocal()
    agent = db.get(Agent, agent_id)
    result = obs.session_transcript(db, EXTERNAL_SID, ws_ctx(), agent=agent)
    db.close()
    assert result["available"] is True and result["source"] == "external"
    assert result["actor_id"] == "default" and probed == ["default"]
    assert result["agent_id"] == agent_id and result["agent_name"] == "hr-assistant"
    assert result["origin"] == "memory" and result["long_term_records"] is None
    # oldest event first, regardless of the order memory returned them
    assert [t["text"] for t in result["turns"]] == ["second q", "first q"]


def test_transcript_external_session_prefers_agent_scoped_actor(monkeypatch):
    """`/v1` sessions write memory under scoped_actor(agent, "api") but create no
    chat ledger row. The scoped actor is probed before the shared default."""
    agent_id = _seed_agent()
    scoped = f"{agent_id}__api"
    fake_events, probed = _events_for({scoped: ["hi from api"], "default": ["nope"]})
    monkeypatch.setattr(_FAKE, "actor_ids", lambda *a, **k: [scoped])
    monkeypatch.setattr(_FAKE, "events", fake_events)
    db = SessionLocal()
    agent = db.get(Agent, agent_id)
    result = obs.session_transcript(db, EXTERNAL_SID, ws_ctx(), agent=agent)
    db.close()
    assert result["actor_id"] == scoped and probed == [scoped]  # default not probed
    assert [t["text"] for t in result["turns"]] == ["hi from api"]


def test_transcript_external_session_falls_back_to_content_logs(monkeypatch):
    """Nothing in Memory (an agent with memory disabled): with the session's
    start known, the traced agent's runtime content logs carry the turns."""
    from datetime import UTC, datetime

    db = SessionLocal()
    agent = db.get(Agent, _seed_agent())
    monkeypatch.setattr(_FAKE, "actor_ids", lambda *a, **k: [])
    monkeypatch.setattr(_FAKE, "events", lambda *a, **k: [])
    monkeypatch.setattr(obs, "_eval_content_log_group", lambda ag, ws: f"/logs/{ag.id}")
    seen: list = []

    def fake_logs_turns(log_group, session_id, started_at, workspace):
        seen.append((log_group, session_id, started_at))
        return [{"role": "USER", "text": "q", "at": "t"},
                {"role": "ASSISTANT", "text": "a", "at": "t"}]

    monkeypatch.setattr(obs, "eval_turns_from_content_logs", fake_logs_turns)
    # no start → no scan: the window would be guesswork
    assert obs.session_transcript(db, EXTERNAL_SID, ws_ctx(), agent=agent) == {
        "available": False, "reason": "not_platform_session"}
    start = datetime(2026, 10, 15, 6, 22, 56, tzinfo=UTC)
    result = obs.session_transcript(db, EXTERNAL_SID, ws_ctx(), agent=agent, started_at=start)
    assert seen == [(f"/logs/{agent.id}", EXTERNAL_SID, start)]
    assert (result["available"], result["origin"], result["source"]) == (True, "logs", "external")
    assert result["actor_id"] is None and result["agent_id"] == agent.id
    assert [t["text"] for t in result["turns"]] == ["q", "a"]

    def broken(*args, **kwargs):
        raise RuntimeError("logs down")

    monkeypatch.setattr(obs, "eval_turns_from_content_logs", broken)
    assert obs.session_transcript(db, EXTERNAL_SID, ws_ctx(), agent=agent, started_at=start) == {
        "available": False, "reason": "not_platform_session"}
    db.close()


REPLAY_SID = "4e1c3f0a-replay-0001-scenario-hr-golden-00000001"


def _seed_replay_run(agent_id, session_id=REPLAY_SID, created_by=None, workspace_id=None):
    """A dataset-replay run that minted `session_id` for one scenario (N17-obs432)."""
    from datetime import UTC, datetime

    from app.evaluation.models import EvalRun

    db = SessionLocal()
    run = EvalRun(workspace_id=workspace_id or DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                  agent_name="hr-assistant", dataset_id="ds0000000001",
                  dataset_name="hr-eval-demo-golden", status="completed",
                  session_ids=[session_id, "other-replayed-session-0002"],
                  scenario_map={session_id: {"scenario_id": "s1", "input": "q"},
                                "other-replayed-session-0002": {"scenario_id": "s2"}},
                  created_by=created_by,
                  created_at=datetime(2026, 10, 25, 6, 56, 49, tzinfo=UTC))
    db.add(run)
    db.commit()
    run_id = run.id
    db.close()
    return run_id


def test_transcript_replay_session_reads_content_logs_from_the_run(monkeypatch):
    """N17-obs432: a dataset-replay session (agent memory off, no chat ledger row, no start
    time from the caller) is the platform's own — the run that replayed it names the agent
    and the content-log start, and the transcript is the agent's own content records."""
    from datetime import UTC, datetime

    agent_id = _seed_agent()
    run_id = _seed_replay_run(agent_id)
    monkeypatch.setattr(_FAKE, "actor_ids", lambda *a, **k: [])
    monkeypatch.setattr(_FAKE, "events", lambda *a, **k: [])  # memory disabled
    monkeypatch.setattr(obs, "_eval_content_log_group", lambda ag, ws: f"/logs/{ag.id}")
    seen: list = []

    def fake_logs_turns(log_group, session_id, started_at, workspace):
        seen.append((log_group, session_id, started_at))
        return [{"role": "USER", "text": "年假几天？", "at": "t"},
                {"role": "ASSISTANT", "text": "按工龄计算", "at": "t"}]

    monkeypatch.setattr(obs, "eval_turns_from_content_logs", fake_logs_turns)
    db = SessionLocal()
    # neither the agent nor the start is given — the drawer sends only the session id
    result = obs.session_transcript(db, REPLAY_SID, ws_ctx())
    started = seen[0][2]
    assert seen == [(f"/logs/{agent_id}", REPLAY_SID, started)]
    assert started.replace(tzinfo=UTC) == datetime(2026, 10, 25, 6, 56, 49, tzinfo=UTC)
    assert (result["available"], result["origin"], result["source"]) == (True, "logs", "eval")
    assert result["run_id"] == run_id and result["agent_id"] == agent_id
    assert [(t["role"], t["text"]) for t in result["turns"]] == [
        ("USER", "年假几天？"), ("ASSISTANT", "按工龄计算")]
    # a caller-supplied start (the session's first span) is kept
    early = datetime(2026, 10, 25, 6, 50, tzinfo=UTC)
    obs.session_transcript(db, REPLAY_SID, ws_ctx(), started_at=early)
    assert seen[-1][2] == early
    # the run's agent wins over a caller's attribution: it is the agent the run drove
    other = db.get(Agent, _seed_agent(name="other", resource_id="other-1"))
    obs.session_transcript(db, REPLAY_SID, ws_ctx(), agent=other)
    assert seen[-1][0] == f"/logs/{agent_id}"
    db.close()


def test_transcript_replay_lookup_claims_only_replayed_sessions(monkeypatch):
    """A session no run replayed stays unavailable: an id only listed in a run's
    session_ids (an explicit-session run evaluates someone else's session) or replayed by
    another workspace's run is not the platform's to show; a hidden run is not used."""
    from app.services.visibility import Viewer

    agent_id = _seed_agent()
    monkeypatch.setattr(_FAKE, "actor_ids", lambda *a, **k: [])
    monkeypatch.setattr(_FAKE, "events", lambda *a, **k: [])
    monkeypatch.setattr(obs, "_eval_content_log_group", lambda ag, ws: f"/logs/{ag.id}")
    monkeypatch.setattr(obs, "eval_turns_from_content_logs", lambda *a, **k: [
        {"role": "USER", "text": "q", "at": "t"}])
    db = SessionLocal()
    from app.evaluation.models import EvalRun

    db.add(EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id, agent_name="x",
                   session_ids=[EXTERNAL_SID], scenario_map=None))
    db.commit()
    _seed_replay_run(agent_id, session_id="foreign-replayed-session-01",
                     workspace_id="other-ws")
    for sid in (EXTERNAL_SID, "foreign-replayed-session-01",
                # a LIKE wildcard in the id never matches another session
                REPLAY_SID.replace("-", "_")):
        assert obs.session_transcript(db, sid, ws_ctx()) == {
            "available": False, "reason": "not_platform_session"}, sid
    # the run's own visibility gate: a viewer the run is hidden from reads it as before
    _seed_replay_run(agent_id, created_by="some-member")
    hidden_from = Viewer(user_id="another", filters=True)
    assert obs.session_transcript(db, REPLAY_SID, ws_ctx(), viewer=hidden_from) == {
        "available": False, "reason": "not_platform_session"}
    assert obs.session_transcript(db, REPLAY_SID, ws_ctx())["available"] is True
    db.close()


def test_transcript_external_session_reads_content_logs_without_a_memory_backend(monkeypatch):
    """CN: no memory backend registered is not the end — the content logs still answer."""
    from datetime import UTC, datetime

    db = SessionLocal()
    agent = db.get(Agent, _seed_agent())
    memory_backend.set_backend(None)
    monkeypatch.setattr(obs, "_eval_content_log_group", lambda ag, ws: f"/logs/{ag.id}")
    monkeypatch.setattr(obs, "eval_turns_from_content_logs", lambda *a, **k: [
        {"role": "USER", "text": "q", "at": "t"}])
    start = datetime(2026, 10, 15, 6, 22, 56, tzinfo=UTC)
    assert obs.session_transcript(db, EXTERNAL_SID, ws_ctx(), agent=agent) == {
        "available": False, "reason": "memory backend not registered"}
    result = obs.session_transcript(db, EXTERNAL_SID, ws_ctx(), agent=agent, started_at=start)
    assert (result["available"], result["origin"]) == (True, "logs")
    db.close()


def test_eval_content_log_group_is_the_runtime_group():
    db = SessionLocal()
    agent = db.get(Agent, _seed_agent())
    assert obs._eval_content_log_group(agent, ws_ctx()) == (
        f"{obs.RUNTIME_LOG_GROUP_PREFIX}{agent.resource_id}-DEFAULT")
    agent.method = "discovered_runtime"
    assert obs._eval_content_log_group(agent, ws_ctx()) is None
    agent.method = "zip_runtime"
    agent.resource_id = None
    assert obs._eval_content_log_group(agent, ws_ctx()) is None
    db.close()


def test_transcript_external_probe_error_degrades(monkeypatch):
    """Neither leg of the probe may raise into the session payload — an
    unattributable session degrades to the plain empty state."""
    def boom(*args, **kwargs):
        raise RuntimeError("memory down")

    agent_id = _seed_agent()
    db = SessionLocal()
    agent = db.get(Agent, agent_id)

    # ListActors fails (agent known, so the scoped-actor lookup runs)
    monkeypatch.setattr(_FAKE, "actor_ids", boom)
    monkeypatch.setattr(_FAKE, "events", lambda *a, **k: [])
    assert obs.session_transcript(db, EXTERNAL_SID, ws_ctx(), agent=agent) == {
        "available": False, "reason": "not_platform_session"}

    # ListEvents fails
    monkeypatch.setattr(_FAKE, "actor_ids", lambda *a, **k: [])
    monkeypatch.setattr(_FAKE, "events", boom)
    assert obs.session_transcript(db, EXTERNAL_SID, ws_ctx(), agent=agent) == {
        "available": False, "reason": "not_platform_session"}
    db.close()


def test_get_session_hands_the_traced_agent_to_the_transcript(monkeypatch):
    """The agent hint for a non-ledger session can only come from its spans."""
    agent_id = _seed_agent()
    seen: dict = {}

    def fake_transcript(db, session_id, workspace, agent=None, started_at=None, **_):
        seen["agent_id"] = agent.id if agent is not None else None
        seen["started_at"] = started_at
        return {"available": False, "reason": "not_platform_session"}

    monkeypatch.setattr(obs, "session_transcript", fake_transcript)
    db = SessionLocal()
    result = obs.get_session("s" * 64, "24h", db, ws_ctx(), logs=_fake_logs())
    db.close()
    assert seen["agent_id"] == agent_id  # resolved from AGG_ROW's service name
    # the session's first span anchors a content-log fallback
    assert seen["started_at"].isoformat() == result["summary"]["first"]
    assert result["transcript"]["available"] is False


def test_get_session_reads_roots_of_its_own_traces_only(monkeypatch):
    """Roots are fetched for the session's trace ids (an unfiltered 7d roots
    scan was the slow half, and its newest-N cap could miss older sessions)."""
    monkeypatch.setattr(obs, "session_transcript",
                        lambda *a, **k: {"available": False, "reason": "not_platform_session"})
    fake = _fake_logs()
    db = SessionLocal()
    result = obs.get_session("s" * 64, "7d", db, ws_ctx(), logs=fake, force=True)
    db.close()
    roots = [k["queryString"] for k in fake.start_kwargs
             if "fields name, traceId" in k["queryString"]]
    assert len(roots) == 1 and f'traceId in ["{"b" * 32}"]' in roots[0]
    assert result["traces"][0]["root_operation"] == "POST /invocations"


def test_get_session_without_traces_skips_the_roots_query(monkeypatch):
    monkeypatch.setattr(obs, "session_transcript",
                        lambda *a, **k: {"available": False, "reason": "not_platform_session"})
    fake = _fake_logs()
    fake.rows_by_marker["by traceId"] = []
    db = SessionLocal()
    result = obs.get_session("z" * 64, "7d", db, ws_ctx(), logs=fake, force=True)
    db.close()
    assert result["traces"] == []
    assert not any("fields name, traceId" in k["queryString"] for k in fake.start_kwargs)


def test_q_root_spans_rejects_bad_trace_ids():
    with pytest.raises(AppError):
        obs.q_root_spans(['"] or 1=1'])


def test_transcript_decodes_harness_envelopes(monkeypatch):
    agent_id = _seed_agent()
    db = SessionLocal()
    db.add(ChatSession(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                       session_id="s" * 64, actor_id="river"))
    db.commit()
    envelope = json.dumps(
        {"message": {"role": "user", "content": [{"text": "How many vacation days?"}]}}
    )
    tool_turn = json.dumps(
        {"message": {"role": "user",
                     "content": [{"toolResult": {"status": "success"}}]}}
    )
    events = [
        {"eventTimestamp": "2026-07-10T01:00:00", "payload": [
            {"conversational": {"role": "USER", "content": {"text": envelope}}},
            {"conversational": {"role": "USER", "content": {"text": tool_turn}}},
            {"conversational": {"role": "ASSISTANT", "content": {"text": "plain text"}}},
        ]},
    ]
    monkeypatch.setattr(_FAKE, "events", lambda *a, **k: events)
    monkeypatch.setattr(_FAKE, "records", lambda *a, **k: [])
    result = obs.session_transcript(db, "s" * 64, ws_ctx())
    db.close()
    assert [t["text"] for t in result["turns"]] == [
        "How many vacation days?", "plain text"]  # tool-result turn dropped


# ── API endpoints (mocked boto3) ────────────────────────────────────────────


@pytest.fixture
def mocked_aws(monkeypatch):
    fake_logs = _fake_logs()
    fake_cw = FakeCW(
        metrics=[
            {"Namespace": "bedrock-agentcore", "MetricName": "gen_ai.client.token.usage",
             "Dimensions": [
                 {"Name": "gen_ai.request.model",
                  "Value": "global.anthropic.claude-sonnet-4-6"},
                 {"Name": "gen_ai.token.type", "Value": ttype},
             ]}
            for ttype in ("input", "output")
        ],
        values={"m0": [386_000.0], "m1": [26_000.0]},
    )
    monkeypatch.setattr(obs, "logs_client", lambda _ws=None: fake_logs)
    monkeypatch.setattr(obs, "cw_client", lambda _ws=None: fake_cw)
    return fake_logs


def test_dashboard_endpoint_shape(client, mocked_aws):
    # _fake_logs cans no rows for the tokens-by-model span query, so this also
    # covers the fallback: empty span aggregation → CloudWatch metric sums.
    res = client.get("/api/observability/dashboard?range=24h")
    assert res.status_code == 200
    body = res.json()
    tiles = body["tiles"]
    assert tiles["traces"] == {"total": 5, "ok": 4, "error": 1}
    assert tiles["sessions"] == {"total": 3, "agents": 2}
    assert tiles["error_rate"] == 0.2
    assert tiles["latency"] == {"p50_ms": 3100.0, "p95_ms": 11800.0}
    assert tiles["tokens"]["input"] == 386_000 and tiles["tokens"]["output"] == 26_000
    assert "est_cost_usd" not in tiles["tokens"] and "prices_meta" not in body
    assert body["series"][0]["traces"] == 5
    assert body["tokens_by_model"][0]["model"] == "global.anthropic.claude-sonnet-4-6"
    assert body["top_tools"][0]["success_rate"] == pytest.approx(88.9)
    assert body["cache"]["hit"] is False


def test_dashboard_tokens_prefer_span_aggregation(client, monkeypatch):
    """Span sums win over the CloudWatch metric (which only Harness runtimes
    emit); wrapper-only models fall back to their wrapper sums; models with no
    tokens at all are dropped."""
    fake_logs = _fake_logs()
    fake_logs.rows_by_marker["by telemetry_model as model"] = [
        {"model": "global.anthropic.claude-sonnet-4-6", "tokens_in": 1000,
         "tokens_out": 100, "wrapper_in": 1000, "wrapper_out": 100},
        {"model": "openai.gpt-5.6-terra", "tokens_in": 0, "tokens_out": 0,
         "wrapper_in": 500, "wrapper_out": 50},
        {"model": "ghost", "tokens_in": 0, "tokens_out": 0,
         "wrapper_in": 0, "wrapper_out": 0},
    ]
    monkeypatch.setattr(obs, "logs_client", lambda _ws=None: fake_logs)
    # CW metrics would report different numbers — they must NOT be consulted
    monkeypatch.setattr(obs, "cw_client", lambda _ws=None: FakeCW(
        metrics=[{"Namespace": "bedrock-agentcore",
                  "MetricName": "gen_ai.client.token.usage",
                  "Dimensions": [
                      {"Name": "gen_ai.request.model", "Value": "cw-only-model"},
                      {"Name": "gen_ai.token.type", "Value": "input"},
                  ]}],
        values={"m0": [999_999.0]},
    ))
    body = client.get("/api/observability/dashboard?range=24h").json()
    rows = body["tokens_by_model"]
    assert [r["model"] for r in rows] == [
        "global.anthropic.claude-sonnet-4-6", "openai.gpt-5.6-terra"]
    assert rows[0]["input"] == 1000 and rows[0]["output"] == 100  # not wrapper-doubled
    assert rows[1]["input"] == 500 and rows[1]["output"] == 50  # wrapper fallback
    assert body["tiles"]["tokens"]["input"] == 1500
    assert body["tiles"]["tokens"]["output"] == 150


def test_traces_endpoint_rows_and_filters(client, mocked_aws):
    _seed_agent()
    res = client.get("/api/observability/traces?range=24h")
    assert res.status_code == 200
    row = res.json()["traces"][0]
    assert row["trace_id"] == "b" * 32
    assert row["agent"] == "hr-assistant"
    assert row["root_operation"] == "POST /invocations"
    assert row["duration_ms"] == 3200.0
    assert row["tokens"]["total"] == 1862
    assert "est_cost_usd" not in row
    assert row["status"] == "ok"
    filtered = client.get("/api/observability/traces?status=error").json()
    assert filtered["count"] == 0
    by_session = client.get(f"/api/observability/traces?session={'s' * 64}").json()
    assert by_session["count"] == 1


def test_trace_detail_endpoint_tree(client, mocked_aws):
    res = client.get(f"/api/observability/traces/{'a' * 32}")
    assert res.status_code == 200
    body = res.json()
    assert body["meta"]["span_count"] == 3 and body["meta"]["llm_count"] == 1
    assert body["meta"]["tokens"]["input"] == 1000
    assert body["meta"]["session_id"] == "s" * 64
    assert body["tree"][0]["children"][0]["children"][0]["category"] == "llm"
    assert body["spans"][0]["attributes"] is not None


def test_sessions_endpoints(client, mocked_aws, monkeypatch):
    agent_id = _seed_agent()
    db = SessionLocal()
    db.add(ChatSession(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                       session_id="s" * 64, actor_id="river"))
    db.commit()
    db.close()
    monkeypatch.setattr(_FAKE, "events", lambda *a, **k: [])
    monkeypatch.setattr(_FAKE, "records", lambda *a, **k: [])

    listing = client.get("/api/observability/sessions?range=24h")
    assert listing.status_code == 200
    row = listing.json()["sessions"][0]
    assert row["session_id"] == "s" * 64 and row["platform"] is True
    assert row["agent"] == "hr-assistant" and row["traces"] == 2

    detail = client.get(f"/api/observability/sessions/{'s' * 64}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["summary"]["traces"] == 1  # one trace row canned for this session
    assert body["transcript"]["available"] is True


def test_validation_rejects_bad_inputs(client):
    bad_range = client.get("/api/observability/dashboard?range=99h")
    assert bad_range.status_code == 422
    assert bad_range.json()["code"] == "validation.invalid_request"
    assert client.get("/api/observability/traces/not-a-trace-id").status_code == 422
    assert client.get("/api/observability/traces/ABC123").status_code == 422
    assert client.get("/api/observability/sessions/ab").status_code == 422  # too short
    assert client.get("/api/observability/sessions/" + "s" * 257).status_code == 422
    assert client.get('/api/observability/sessions/abcdefgh"x').status_code == 422
    assert client.get(
        "/api/observability/traces?session=bad$chars"
    ).status_code == 422
    assert client.get("/api/observability/traces?status=weird").status_code == 422


def test_session_routes_accept_composite_external_ids(client, mocked_aws, monkeypatch):
    # `#` must survive the URL (percent-encoded by the console) and the router
    # pattern; the detail route then runs the normal session build.
    monkeypatch.setattr(_FAKE, "events", lambda *a, **k: [])
    monkeypatch.setattr(_FAKE, "records", lambda *a, **k: [])
    sid = "01KKRNGA47RK9XD3ZKDWX1R5WD#feishu#oc_115a912b912ff5efb7895263ccc1ea70"
    encoded = sid.replace("#", "%23")
    detail = client.get(f"/api/observability/sessions/{encoded}")
    assert detail.status_code == 200
    assert detail.json()["session_id"] == sid
    assert client.get(f"/api/observability/traces?session={encoded}").status_code == 200


def test_session_transcript_endpoint_skips_logs_insights(client, mocked_aws, monkeypatch):
    """The eval result drawer's read: the conversation alone, no span query;
    agent_id attributes an unclaimed session, a foreign workspace's id is dropped."""
    agent_id = _seed_agent()
    seen: list = []
    starts: list = []

    def fake_transcript(db, session_id, workspace, agent=None, started_at=None, **_):
        seen.append(agent.id if agent is not None else None)
        starts.append(started_at)
        return {"available": True, "turns": [{"role": "USER", "text": "hi", "at": "t"}]}

    monkeypatch.setattr(obs, "session_transcript", fake_transcript)
    url = f"/api/observability/sessions/{'s' * 64}/transcript"
    res = client.get(f"{url}?agent_id={agent_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["session_id"] == "s" * 64
    assert body["transcript"]["turns"][0]["text"] == "hi"
    assert mocked_aws.start_calls == 0  # no Logs Insights query

    db = SessionLocal()
    foreign = Agent(workspace_id="other-ws", name="x", method="harness",
                    status="active", resource_id="x-1")
    db.add(foreign)
    db.commit()
    foreign_id = foreign.id
    db.close()
    client.get(f"{url}?agent_id={foreign_id}")
    client.get(url)
    assert seen == [agent_id, None, None]
    client.get(f"{url}?since=2026-10-15T06:22:56%2B00:00")
    assert [str(s) if s else None for s in starts] == [
        None, None, None, "2026-10-15 06:22:56+00:00"]
    assert client.get(f"{url}?agent_id=bad$id").status_code == 422
    assert client.get(f"{url}?since=yesterday").status_code == 422


# ── session previews (task wizard's trajectory picker) ──────────────────────


def test_transcript_preview_is_first_input_and_last_output():
    turns = [
        {"role": "USER", "text": "  first question  "},
        {"role": "ASSISTANT", "text": "first answer"},
        {"role": "USER", "text": "follow-up"},
        {"role": "ASSISTANT", "text": "x" * 900},
    ]
    row = obs.transcript_preview("s" * 64, {"available": True, "turns": turns})
    assert row == {
        "session_id": "s" * 64, "available": True, "reason": None,
        "input": "first question", "output": "x" * obs.PREVIEW_TEXT_CAP, "user_turns": 2,
    }
    # an unanswered question still previews; nothing said at all does not
    asked = obs.transcript_preview("q" * 64, {"available": True, "turns": turns[:1]})
    assert (asked["available"], asked["input"], asked["output"]) == (True, "first question", None)
    empty = obs.transcript_preview("e" * 64, {"available": True, "turns": []})
    assert (empty["available"], empty["reason"], empty["user_turns"]) == (False, "no_turns", 0)
    gone = obs.transcript_preview("g" * 64, {"available": False, "reason": "not_platform_session"})
    assert (gone["available"], gone["reason"], gone["input"]) == (
        False, "not_platform_session", None)


def test_session_transcripts_skip_long_term_reads_and_degrade_per_session(monkeypatch):
    agent_id = _seed_agent()
    db = SessionLocal()
    db.add(ChatSession(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                       session_id="s" * 64, actor_id="river"))
    db.add_all([
        ChatMessage(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                    session_id="s" * 64, role="user", text="hello"),
        ChatMessage(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                    session_id="s" * 64, role="agent", text="hi there"),
    ])
    db.commit()
    db.close()

    def no_long_term(*args, **kwargs):
        raise AssertionError("a preview must not read long-term records")

    seen_agents: list = []

    def list_actor_ids(workspace, *a, **k):
        seen_agents.append(workspace.id)
        raise RuntimeError("memory down")

    monkeypatch.setattr(_FAKE, "events", lambda *a, **k: [])
    monkeypatch.setattr(_FAKE, "records", no_long_term)
    monkeypatch.setattr(_FAKE, "actor_ids", list_actor_ids)
    out = obs.session_transcripts({"s" * 64: None, "x" * 64: None}, ws_ctx(), agent_id)
    assert list(out) == ["s" * 64, "x" * 64]
    chat = out["s" * 64]
    assert chat["origin"] == "ledger" and chat["long_term_records"] is None
    assert [t["text"] for t in chat["turns"]] == ["hello", "hi there"]
    # an unclaimed session is probed under the given agent; its failure stays its own
    assert out["x" * 64] == {"available": False, "reason": "not_platform_session"}
    assert seen_agents == [ws_ctx().id]  # the scoped-actor probe ran once, for "x"

    def boom(*args, **kwargs):
        raise RuntimeError("ledger locked")

    monkeypatch.setattr(obs, "session_transcript", boom)
    assert obs.session_transcripts({"s" * 64: None}, ws_ctx()) == {
        "s" * 64: {"available": False, "reason": "read_failed"}}
    assert obs.session_transcripts({}, ws_ctx()) == {}


def test_session_previews_endpoint_reads_one_page(client, mocked_aws, monkeypatch):
    from datetime import UTC, datetime

    agent_id = _seed_agent()
    calls: list = []

    def fake_transcripts(sessions, workspace, agent_id=None, **_):
        calls.append((dict(sessions), agent_id))
        return {sid: {"available": True, "turns": [
            {"role": "USER", "text": f"q-{sid[:2]}"},
            {"role": "ASSISTANT", "text": f"a-{sid[:2]}"}]} for sid in sessions}

    monkeypatch.setattr(obs, "session_transcripts", fake_transcripts)
    url = "/api/observability/session-previews"
    a, b = "a" * 40, "b#feishu#oc_1"
    res = client.post(url, json={"agent_id": agent_id, "sessions": [
        {"session_id": a, "first": "2026-10-15T06:22:56+00:00"},
        {"session_id": b}, {"session_id": a}]})
    assert res.status_code == 200, res.text
    previews = res.json()["previews"]
    assert [(p["session_id"], p["input"], p["output"]) for p in previews] == [
        (a, "q-aa", "a-aa"), (b, "q-b#", "a-b#")]
    # de-duplicated, one batch, each session's start handed on
    assert calls == [({a: datetime(2026, 10, 15, 6, 22, 56, tzinfo=UTC), b: None}, agent_id)]
    assert mocked_aws.start_calls == 0  # no Logs Insights query

    assert client.post(url, json={"sessions": []}).status_code == 422
    assert client.post(url, json={"sessions": [{"session_id": 'bad"id-123'}]}).status_code == 422
    too_many = [{"session_id": f"sess-{i:04d}"} for i in range(obs.MAX_PREVIEW_SESSIONS + 1)]
    assert client.post(url, json={"sessions": too_many}).status_code == 422
    assert client.post(url, json={"sessions": [{"session_id": a}],
                                  "agent_id": "bad$id"}).status_code == 422


# ── session spans query (shared shape with the trace/session views) ────────


SCORE_SESSION = "s" * 64


def test_session_spans_query_is_the_on_demand_guide_shape():
    q = obs.q_session_spans(SCORE_SESSION)
    assert obs.SPANS_SOURCE in q  # both telemetry layouts
    assert "ispresent(scope.name)" in q and "ispresent(attributes.session.id)" in q
    assert f'attributes.session.id = "{SCORE_SESSION}"' in q
    assert "fields @message" in q and "sort @timestamp asc" in q
    assert f"limit {obs.SESSION_SPANS_LIMIT}" in q
    with pytest.raises(AppError):
        obs.q_session_spans('bad"id')
