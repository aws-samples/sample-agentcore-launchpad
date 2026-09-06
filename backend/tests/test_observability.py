"""Observability service — tree building, cost, cache TTL, mapper, transcript, API."""

import json

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.models.ledger import Agent, ChatMessage, ChatSession
from app.services import observability as obs

from .conftest import ws_ctx

BASE_NS = 1_700_000_000_000_000_000
PRICES = {"sonnet-4-6": {"input": 3.0, "output": 15.0},
          "nemotron-nano": {"input": 0.2, "output": 0.6}}


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
    tree = obs.build_span_tree(TREE_SPANS, prices=PRICES)
    assert tree["duration_ms"] == 10.0
    root = tree["tree"][0]
    assert root["name"] == "POST /invocations" and root["depth"] == 0
    agent = root["children"][0]
    assert agent["depth"] == 1 and agent["category"] == "agent"
    llm = agent["children"][0]
    assert llm["depth"] == 2 and llm["category"] == "llm"
    assert llm["start_offset_ms"] == 3.0 and llm["duration_ms"] == 4.0
    assert llm["offset_pct"] == 30.0 and llm["width_pct"] == 40.0
    assert llm["est_cost_usd"] == pytest.approx(0.0045)
    # flat rows keep raw attributes; tree nodes do not carry them
    flat_llm = next(s for s in tree["spans"] if s["span_id"] == "llm")
    assert flat_llm["attributes"]["session.id"] == "s" * 64
    assert "attributes" not in llm


def test_span_tree_orphan_becomes_root():
    spans = [TREE_SPANS[0], _span("lost", "missing-parent", "chat x", 1, 2)]
    tree = obs.build_span_tree(spans)
    assert {n["span_id"] for n in tree["tree"]} == {"root", "lost"}


def test_native_claude_span_tree_uses_openinference_attributes():
    tree = obs.build_span_tree(NATIVE_SPANS, prices=PRICES)
    root = tree["tree"][0]
    assert root["category"] == "agent"
    assert root["model"] == "global.anthropic.claude-sonnet-4-6"
    assert root["tokens"] == {
        "input": 700.0,
        "output": 42.0,
        "cache_read": 100.0,
        "cache_write": 10.0,
    }
    assert root["est_cost_usd"] is not None
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
    assert meta["tokens"] == {
        "input": 700,
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


# ── cost estimator ──────────────────────────────────────────────────────────


def test_cost_known_and_unknown_model():
    known = obs.estimate_cost("global.anthropic.claude-sonnet-4-6", 1_000_000, 100_000,
                              prices=PRICES)
    assert known == pytest.approx(3.0 + 1.5)
    assert obs.estimate_cost("mystery-model-9000", 500, 50, prices=PRICES) is None
    assert obs.estimate_cost(None, 500, 50, prices=PRICES) is None


def test_cost_cache_tokens_use_default_factors():
    cost = obs.estimate_cost("claude-sonnet-4-6", 0, 0, cache_read=1_000_000,
                             cache_write=1_000_000, prices=PRICES)
    assert cost == pytest.approx(3.0 * 0.1 + 3.0 * 1.25)


def test_match_price_prefers_longest_key():
    prices = {"sonnet": {"input": 1.0, "output": 1.0},
              "sonnet-4-6": {"input": 3.0, "output": 15.0}}
    assert obs.match_price("global.anthropic.claude-sonnet-4-6", prices)["input"] == 3.0


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
    monkeypatch.setattr(obs.memory, "list_actor_ids", lambda *a, **k: [])
    monkeypatch.setattr(obs.memory, "list_events", lambda *a, **k: [])
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

    monkeypatch.setattr(obs.memory, "list_events", boom)
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
    monkeypatch.setattr(obs.memory, "list_events", lambda *a, **k: events)
    monkeypatch.setattr(obs.memory, "list_records", lambda *a, **k: [{"id": 1}])
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
        obs.memory,
        "list_events",
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
    monkeypatch.setattr(obs.memory, "list_records", lambda *a, **k: [])

    result = obs.session_transcript(db, session_id, ws_ctx())
    db.close()

    assert result["origin"] == "ledger"
    assert [turn["text"] for turn in result["turns"]] == [
        "first question",
        "first answer",
        "latest question",
        "latest answer",
    ]


def test_transcript_falls_back_to_eval_run_session(monkeypatch):
    """Eval-run sessions have no chat ledger row; the transcript comes from the
    BARE "default" actor the eval invoker passed to the runtime."""
    from app.evaluation.models import EvalRun

    agent_id = _seed_agent()
    sid = "e" * 64
    db = SessionLocal()
    run = EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                  agent_name="hr-assistant", mode="evaluators",
                  evaluators=[], status="completed", session_ids=[sid])
    db.add(run)
    db.commit()
    run_id = run.id

    seen: dict = {}

    def fake_events(_ws, actor_id, session_id, max_results=20, memory_id=None):
        seen["actor"] = actor_id
        envelope = json.dumps(
            {"message": {"role": "user", "content": [{"text": "PTO balance?"}]}}
        )
        return [{"eventTimestamp": "2026-07-13T01:00:00", "payload": [
            {"conversational": {"role": "USER", "content": {"text": envelope}}},
            {"conversational": {"role": "ASSISTANT", "content": {"text": "15 days"}}},
        ]}]

    monkeypatch.setattr(obs.memory, "list_events", fake_events)
    monkeypatch.setattr(obs.memory, "list_records", lambda *a, **k: [])
    result = obs.session_transcript(db, sid, ws_ctx())
    db.close()
    assert result["available"] is True
    assert result["source"] == "eval" and result["run_id"] == run_id
    assert result["actor_id"] == "default" and seen["actor"] == "default"
    assert result["agent_name"] == "hr-assistant"
    assert [t["text"] for t in result["turns"]] == ["PTO balance?", "15 days"]


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
    monkeypatch.setattr(obs.memory, "list_actor_ids", lambda *a, **k: [])
    monkeypatch.setattr(obs.memory, "list_events", fake_events)
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
    monkeypatch.setattr(obs.memory, "list_actor_ids", lambda *a, **k: [scoped])
    monkeypatch.setattr(obs.memory, "list_events", fake_events)
    db = SessionLocal()
    agent = db.get(Agent, agent_id)
    result = obs.session_transcript(db, EXTERNAL_SID, ws_ctx(), agent=agent)
    db.close()
    assert result["actor_id"] == scoped and probed == [scoped]  # default not probed
    assert [t["text"] for t in result["turns"]] == ["hi from api"]


def test_transcript_external_session_labels_owning_experiment(monkeypatch):
    from app.optimization.models import Experiment

    agent_id = _seed_agent()
    db = SessionLocal()
    experiment = Experiment(
        workspace_id=DEFAULT_WORKSPACE_ID,
        name="EXP-hr-assistant", agent_id=agent_id, agent_name="hr-assistant",
        artifacts={"traffic": {"session_ids": ["other-sid", EXTERNAL_SID]}},
    )
    db.add(experiment)
    db.commit()
    exp_id, exp_name = experiment.id, experiment.name
    fake_events, _ = _events_for({"default": ["prompt from traffic"]})
    monkeypatch.setattr(obs.memory, "list_actor_ids", lambda *a, **k: [])
    monkeypatch.setattr(obs.memory, "list_events", fake_events)
    result = obs.session_transcript(
        db, EXTERNAL_SID, ws_ctx(), agent=db.get(Agent, agent_id)
    )
    db.close()
    assert result["source"] == "experiment"
    assert result["experiment_id"] == exp_id and result["experiment_name"] == exp_name


def test_transcript_external_probe_error_degrades(monkeypatch):
    """Neither leg of the probe may raise into the session payload — an
    unattributable session degrades to the plain empty state."""
    def boom(*args, **kwargs):
        raise RuntimeError("memory down")

    agent_id = _seed_agent()
    db = SessionLocal()
    agent = db.get(Agent, agent_id)

    # ListActors fails (agent known, so the scoped-actor lookup runs)
    monkeypatch.setattr(obs.memory, "list_actor_ids", boom)
    monkeypatch.setattr(obs.memory, "list_events", lambda *a, **k: [])
    assert obs.session_transcript(db, EXTERNAL_SID, ws_ctx(), agent=agent) == {
        "available": False, "reason": "not_platform_session"}

    # ListEvents fails
    monkeypatch.setattr(obs.memory, "list_actor_ids", lambda *a, **k: [])
    monkeypatch.setattr(obs.memory, "list_events", boom)
    assert obs.session_transcript(db, EXTERNAL_SID, ws_ctx(), agent=agent) == {
        "available": False, "reason": "not_platform_session"}
    db.close()


def test_get_session_hands_the_traced_agent_to_the_transcript(monkeypatch):
    """The agent hint for a non-ledger session can only come from its spans."""
    agent_id = _seed_agent()
    seen: dict = {}

    def fake_transcript(db, session_id, workspace, agent=None):
        seen["agent_id"] = agent.id if agent is not None else None
        return {"available": False, "reason": "not_platform_session"}

    monkeypatch.setattr(obs, "session_transcript", fake_transcript)
    db = SessionLocal()
    result = obs.get_session("s" * 64, "24h", db, ws_ctx(), logs=_fake_logs())
    db.close()
    assert seen["agent_id"] == agent_id  # resolved from AGG_ROW's service name
    assert result["transcript"]["available"] is False


def _content_record(trace_id, ts_ns, body, session_id="e" * 64):
    return json.dumps({
        "scope": {"name": "strands.telemetry.tracer"},
        "timeUnixNano": ts_ns,
        "traceId": trace_id,
        "attributes": {"event.name": "strands.telemetry.tracer", "session.id": session_id},
        "body": body,
    })


class FakeLogsClient:
    """filter_log_events stub with one-page pagination."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def filter_log_events(self, **kwargs):
        self.calls.append(kwargs)
        page = self.pages[min(len(self.calls) - 1, len(self.pages) - 1)]
        out = {"events": [{"message": m} for m in page]}
        if len(self.calls) < len(self.pages):
            out["nextToken"] = f"tok-{len(self.calls)}"
        return out


def test_eval_turns_from_content_logs_groups_by_trace():
    sid = "e" * 64
    # invocation 2 arrives first in the log page — ordering must follow time
    page1 = [
        _content_record("trace-2", 2_000, {
            "input": {"messages": [
                {"content": {"content": '[{"text": "Q2?"}]'}, "role": "user"}]},
            "output": {"messages": [
                {"content": {"message": "A2", "finish_reason": "end_turn"},
                 "role": "assistant"}]},
        }),
    ]
    page2 = [
        # model-level record: toolUse output (no text) + user input
        _content_record("trace-1", 1_000, {
            "input": {"messages": [
                {"content": {"content": '[{"text": "Q1?"}]'}, "role": "user"}]},
            "output": {"messages": [
                {"content": {"message": '[{"toolUse": {"name": "calc"}}]',
                             "finish_reason": "tool_use"}, "role": "assistant"}]},
        }),
        # agent-level record: plain-string system + final end_turn answer
        _content_record("trace-1", 1_500, {
            "input": {"messages": [
                {"content": "system prompt", "role": "system"},
                {"content": {"content": '[{"text": "Q1?"}]'}, "role": "user"},
                {"content": {"content": '[{"toolResult": {"status": "ok"}}]'},
                 "role": "tool"}]},
            "output": {"messages": [
                {"content": {"message": "A1", "finish_reason": "end_turn"},
                 "role": "assistant"}]},
        }),
        json.dumps({"attributes": {"session.id": "other"}, "body": {}}),  # filtered out
        "not json at all",  # skipped
    ]
    logs = FakeLogsClient([page1, page2])
    turns = obs.eval_turns_from_content_logs("/lg", sid, None, ws_ctx(), logs=logs)
    assert [(t["role"], t["text"]) for t in turns] == [
        ("USER", "Q1?"), ("ASSISTANT", "A1"),
        ("USER", "Q2?"), ("ASSISTANT", "A2"),
    ]
    assert logs.calls[0]["filterPattern"] == f'"{sid}"'
    assert "startTime" in logs.calls[0]  # load-bearing: scan is oldest-first


def test_eval_turns_from_content_logs_accepts_native_claude_event():
    sid = "n" * 64
    record = json.dumps(
        {
            "scope": {
                "name": "openinference.instrumentation.claude_agent_sdk"
            },
            "timeUnixNano": 2_000,
            "traceId": "native-trace",
            "attributes": {"session.id": sid},
            "body": {
                "input": {
                    "messages": [{"role": "user", "content": "Native question"}]
                },
                "output": {
                    "messages": [{"role": "assistant", "content": "Native answer"}]
                },
            },
        }
    )
    logs = FakeLogsClient([[record]])

    turns = obs.eval_turns_from_content_logs(
        "/native-runtime", sid, None, ws_ctx(), logs=logs
    )

    assert [(turn["role"], turn["text"]) for turn in turns] == [
        ("USER", "Native question"),
        ("ASSISTANT", "Native answer"),
    ]


@pytest.mark.parametrize("method", ["zip_runtime", "container"])
def test_transcript_eval_falls_back_to_content_logs(monkeypatch, method):
    """Runtime-backed agents write no memory events — the transcript is rebuilt
    from the runtime's OTEL content logs. Insights re-runs reuse session ids,
    so the CREATOR run (oldest match) anchors the log window and run_id."""
    from datetime import datetime

    from app.evaluation.models import EvalRun

    agent_id = _seed_agent(method=method)
    sid = "f" * 64
    db = SessionLocal()
    creator = EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                      agent_name="hr-assistant", mode="evaluators",
                      evaluators=[], status="completed", session_ids=[sid],
                      created_at=datetime(2026, 7, 11, 1, 0, 0))
    insights = EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                       agent_name="hr-assistant", mode="insights",
                       evaluators=[], status="completed", session_ids=[sid],
                       created_at=datetime(2026, 7, 11, 3, 0, 0))
    db.add_all([creator, insights])
    db.commit()
    creator_id = creator.id

    monkeypatch.setattr(obs.memory, "list_events", lambda *a, **k: [])
    monkeypatch.setattr(obs.memory, "list_records", lambda *a, **k: [])
    seen: dict = {}

    def fake_logs_turns(log_group, session_id, started_at, logs=None):
        seen.update(log_group=log_group, started_at=started_at)
        return [{"role": "USER", "text": "hi", "at": "t"}]

    monkeypatch.setattr(obs, "eval_turns_from_content_logs", fake_logs_turns)
    result = obs.session_transcript(db, sid, ws_ctx())
    db.close()
    assert result["available"] is True and result["origin"] == "logs"
    assert result["run_id"] == creator_id  # not the insights re-run
    assert seen["started_at"] == datetime(2026, 7, 11, 1, 0, 0)
    assert "-DEFAULT" in seen["log_group"]
    assert [t["text"] for t in result["turns"]] == ["hi"]


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
    monkeypatch.setattr(obs.memory, "list_events", lambda *a, **k: events)
    monkeypatch.setattr(obs.memory, "list_records", lambda *a, **k: [])
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
    assert tiles["tokens"]["est_cost_usd"] == pytest.approx(1.548)
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
    assert row["est_cost_usd"] == pytest.approx(0.005994)
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
    monkeypatch.setattr(obs.memory, "list_events", lambda *a, **k: [])
    monkeypatch.setattr(obs.memory, "list_records", lambda *a, **k: [])

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
    assert client.get(
        "/api/observability/traces?session=bad$chars"
    ).status_code == 422
    assert client.get("/api/observability/traces?status=weird").status_code == 422


# ── SCORE NOW: on-demand session scoring via the data-plane Evaluate API ────


SCORE_SESSION = "s" * 64
SCORE_SPANS = [{**s, "attributes": {**s["attributes"], "session.id": SCORE_SESSION},
                "scope": {"name": "strands.telemetry"}} for s in TREE_SPANS]


def _score_result(evaluator_id, **overrides):
    return {
        "evaluatorArn": "arn:aws:bedrock-agentcore:us-west-2:111122223333:evaluator/"
        + evaluator_id,
        "evaluatorId": evaluator_id,
        "evaluatorName": evaluator_id.split(".")[-1],
        "value": 0.8,
        "label": "Helpful",
        "explanation": "The agent answered the question directly.",
        "context": {"spanContext": {"sessionId": SCORE_SESSION, "traceId": "a" * 32}},
        "tokenUsage": {"inputTokens": 1200, "outputTokens": 80, "totalTokens": 1280},
        **overrides,
    }


class FakeDataPlane:
    """`bedrock-agentcore` data-plane stub: records Evaluate calls, answers canned."""

    def __init__(self, results_by_evaluator=None, raise_code=None):
        self.results_by_evaluator = results_by_evaluator or {}
        self.raise_code = raise_code
        self.calls = []

    def evaluate(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_code:
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": self.raise_code, "Message": "unsupported span format"}},
                "Evaluate",
            )
        evaluator_id = kwargs["evaluatorId"]
        return {"evaluationResults": self.results_by_evaluator.get(
            evaluator_id, [_score_result(evaluator_id)])}


def _score_logs(rows):
    # the session query is the only one these tests run; route it by the
    # session filter so a different query would surface as an empty result
    return FakeLogs({f'attributes.session.id = "{SCORE_SESSION}"': rows})


@pytest.fixture
def score_stack(monkeypatch):
    def install(rows, data=None):
        fake_logs = _score_logs(rows)
        fake_data = data or FakeDataPlane()
        monkeypatch.setattr(obs, "logs_client", lambda _ws=None: fake_logs)
        monkeypatch.setattr(obs, "data_client", lambda _ws=None: fake_data)
        return fake_logs, fake_data
    return install


def test_session_spans_query_is_the_on_demand_guide_shape():
    q = obs.q_session_spans(SCORE_SESSION)
    assert obs.SPANS_SOURCE in q  # both telemetry layouts
    assert "ispresent(scope.name)" in q and "ispresent(attributes.session.id)" in q
    assert f'attributes.session.id = "{SCORE_SESSION}"' in q
    assert "fields @message" in q and "sort @timestamp asc" in q
    assert f"limit {obs.SESSION_SPANS_LIMIT}" in q
    with pytest.raises(AppError):
        obs.q_session_spans('bad"id')


def test_score_now_parses_messages_and_calls_evaluate_per_evaluator(client, score_stack):
    rows = [{"@message": json.dumps(s)} for s in SCORE_SPANS]
    rows.insert(1, {"@message": "2026-09-06 stdout line that is not JSON"})
    rows.append({"@message": json.dumps(["a", "list", "not", "a", "span"])})
    fake_logs, fake_data = score_stack(rows)

    res = client.post(
        f"/api/observability/sessions/{SCORE_SESSION}/evaluate",
        json={"evaluator_ids": ["Builtin.Helpfulness", "Builtin.Correctness"],
              "range": "7d"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["session_id"] == SCORE_SESSION and body["range"] == "7d"
    assert body["span_count"] == len(SCORE_SPANS)  # non-JSON + non-object rows skipped
    # one Logs Insights query over a 7d window, one Evaluate call per evaluator
    assert fake_logs.start_calls == 1
    start, end = fake_logs.start_kwargs[0]["startTime"], fake_logs.start_kwargs[0]["endTime"]
    assert end - start == 7 * 24 * 3600
    assert [c["evaluatorId"] for c in fake_data.calls] == [
        "Builtin.Helpfulness", "Builtin.Correctness"]
    assert fake_data.calls[0]["evaluationInput"] == {"sessionSpans": SCORE_SPANS}
    assert "evaluationTarget" not in fake_data.calls[0]  # session-level only
    assert len(body["results"]) == 2
    row = body["results"][0]
    assert row["evaluator_id"] == "Builtin.Helpfulness"
    assert row["evaluator_name"] == "Helpfulness"
    assert row["value"] == 0.8 and row["label"] == "Helpful"
    assert row["explanation"].startswith("The agent answered")
    assert row["token_usage"] == {"input": 1200, "output": 80, "total": 1280}
    assert row["span_context"]["sessionId"] == SCORE_SESSION
    assert row["error_code"] is None and row["error_message"] is None


def test_score_now_without_spans_is_409_with_readiness_hint(client, score_stack):
    _, fake_data = score_stack([])
    res = client.post(
        f"/api/observability/sessions/{SCORE_SESSION}/evaluate",
        json={"evaluator_ids": ["Builtin.Helpfulness"]},
    )
    assert res.status_code == 409
    body = res.json()
    assert body["code"] == "observability.session_spans_missing"
    assert "minutes" in body["detail"]["hint"]
    assert body["detail"]["range"] == "24h"  # default window
    assert fake_data.calls == []  # never reaches Evaluate


def test_score_now_partial_failure_is_an_error_row_not_an_exception(client, score_stack):
    failing = _score_result("Builtin.Faithfulness", value=None, label=None,
                            explanation=None, tokenUsage=None,
                            errorCode="ValidationException",
                            errorMessage="no tool spans to judge")
    failing = {k: v for k, v in failing.items() if v is not None}
    _, fake_data = score_stack(
        [{"@message": json.dumps(s)} for s in SCORE_SPANS],
        data=FakeDataPlane({"Builtin.Faithfulness": [failing], "Builtin.NoResult": []}),
    )
    res = client.post(
        f"/api/observability/sessions/{SCORE_SESSION}/evaluate",
        json={"evaluator_ids": ["Builtin.Helpfulness", "Builtin.Faithfulness",
                                "Builtin.NoResult", "Builtin.Helpfulness"]},
    )
    assert res.status_code == 200, res.text
    rows = res.json()["results"]
    # duplicate id collapsed → 3 evaluators scored, in request order
    assert [c["evaluatorId"] for c in fake_data.calls] == [
        "Builtin.Helpfulness", "Builtin.Faithfulness", "Builtin.NoResult"]
    assert [r["evaluator_id"] for r in rows] == [
        "Builtin.Helpfulness", "Builtin.Faithfulness", "Builtin.NoResult"]
    ok, failed, empty = rows
    assert ok["error_code"] is None and ok["value"] == 0.8
    assert failed["error_code"] == "ValidationException"
    assert failed["error_message"] == "no tool spans to judge"
    assert failed["value"] is None and failed["label"] is None
    assert failed["token_usage"] is None
    # an evaluator answering with zero results still gets a row the UI can show
    assert empty["error_code"] == "NoResult" and empty["value"] is None


def test_score_now_rejects_bad_bodies(client, score_stack):
    _, fake_data = score_stack([{"@message": json.dumps(s)} for s in SCORE_SPANS])
    url = f"/api/observability/sessions/{SCORE_SESSION}/evaluate"
    too_many = client.post(url, json={"evaluator_ids": [f"Builtin.E{i}" for i in range(6)]})
    assert too_many.status_code == 422
    assert too_many.json()["code"] == "validation.invalid_request"
    assert client.post(url, json={"evaluator_ids": []}).status_code == 422
    assert client.post(url, json={"evaluator_ids": ["Builtin.Helpfulness"],
                                  "range": "99h"}).status_code == 422
    assert client.post(url, json={"evaluator_ids": ['bad"id']}).status_code == 422
    assert client.post("/api/observability/sessions/ab/evaluate",
                       json={"evaluator_ids": ["Builtin.Helpfulness"]}).status_code == 422
    assert fake_data.calls == []


def test_score_now_aws_validation_error_maps_to_4xx_envelope(client, score_stack):
    score_stack([{"@message": json.dumps(s)} for s in SCORE_SPANS],
                data=FakeDataPlane(raise_code="ValidationException"))
    res = client.post(
        f"/api/observability/sessions/{SCORE_SESSION}/evaluate",
        json={"evaluator_ids": ["Builtin.Helpfulness"]},
    )
    assert res.status_code == 400
    body = res.json()
    assert body["code"] == "aws.validation"
    assert "unsupported span format" in body["message"]
    assert "An error occurred" not in body["message"]


def test_evaluate_session_spans_wrapper_shape():
    from app.services.agentcore import evaluation as ace

    fake = FakeDataPlane()
    out = ace.evaluate_session_spans(fake, evaluator_id="Builtin.Helpfulness",
                                     spans=SCORE_SPANS)
    assert fake.calls == [{"evaluatorId": "Builtin.Helpfulness",
                           "evaluationInput": {"sessionSpans": SCORE_SPANS}}]
    assert out[0]["evaluatorId"] == "Builtin.Helpfulness"
    with pytest.raises(ValueError):
        ace.evaluate_session_spans(fake, evaluator_id="Builtin.Helpfulness", spans=[])
