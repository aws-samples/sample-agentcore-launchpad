"""Task wizard · 日志 — an agent's runtime log streams, keyword-filtered.

Hermetic: a fake CloudWatch Logs client serves describe/filter pages, so the
stream → session mapping, both keyword paths and the caps are exercised
without AWS.
"""

import json

from botocore.exceptions import ClientError

from app.core.db import SessionLocal
from app.evaluation import log_streams, pipeline_routers
from app.services import aws_clients
from tests.evaluation.test_runs_flow import make_agent

GROUP = "/aws/bedrock-agentcore/runtimes/rt-1-DEFAULT"
SID_A = "5c4eb0b4-cbdc-4dad-9fa6-8224e94c7e83"
SID_B = "467a63dc-9d58-45aa-a323-f2e2b7052560"
NOW = 1_800_000_000_000


def _stream(name, last):
    return {"logStreamName": name, "firstEventTimestamp": last - 5000, "lastEventTimestamp": last}


STREAMS = [
    _stream("otel-rt-logs", NOW),
    _stream(f"2026/09/30/[runtime-logs-{SID_A}]30dacf09", NOW - 1000),
    _stream(f"2026/09/29/[runtime-logs-{SID_B}]761fde42", NOW - 2000),
    _stream("2026/09/01/[runtime-logs-old-session-0001]aa", NOW - 90 * 86_400_000),
]


def _otel(sid, text):
    return json.dumps({"body": text, "attributes": {"session.id": sid}})


class FakeLogs:
    def __init__(self, streams=STREAMS, events=(), filter_pages=1):
        self.streams = list(streams)
        self.events = list(events)
        self.filter_pages = filter_pages
        self.filter_calls = []

    def describe_log_streams(self, **kw):
        assert kw["orderBy"] == "LastEventTime" and kw["descending"] is True
        start = int(kw.get("nextToken") or 0)
        page = self.streams[start:start + 2]  # tiny pages exercise pagination
        nxt = start + 2
        return {"logStreams": page, **({"nextToken": str(nxt)} if nxt < len(self.streams) else {})}

    def filter_log_events(self, **kw):
        self.filter_calls.append(kw)
        n = len(self.filter_calls)
        return {"events": self.events if n == 1 else [],
                **({"nextToken": f"t{n}"} if n < self.filter_pages else {})}


def test_session_of_stream():
    assert log_streams.session_of_stream(STREAMS[1]["logStreamName"]) == SID_A
    assert log_streams.session_of_stream("otel-rt-logs") is None


def test_lists_recent_streams_newest_first_with_sessions():
    out = log_streams.list_streams(FakeLogs(), GROUP, since_ms=NOW - 86_400_000)
    assert [(r["session_id"], r["kind"]) for r in out["streams"]] == [
        (None, "shared"), (SID_A, "session"), (SID_B, "session")]
    assert out["truncated"] is False
    assert out["streams"][1]["last_event"].startswith("2027-01-15")
    assert all(r["match"] is None for r in out["streams"])


def test_keyword_matches_stream_name_case_insensitively():
    logs = FakeLogs()
    out = log_streams.list_streams(logs, GROUP, since_ms=NOW - 86_400_000, keyword="5C4EB0B4")
    assert [(r["session_id"], r["match"]) for r in out["streams"]] == [(SID_A, "name")]
    assert logs.filter_calls[0]["filterPattern"] == '"5C4EB0B4"'


def test_keyword_content_hits_credit_otel_events_to_their_session():
    events = [
        {"logStreamName": "otel-rt-logs", "message": _otel(SID_B, "PTO balance for EMP-042")},
        {"logStreamName": "otel-rt-logs", "message": _otel(SID_B, "EMP-042 again")},
        {"logStreamName": STREAMS[1]["logStreamName"], "message": "Received prompt: EMP-042"},
    ]
    out = log_streams.list_streams(
        FakeLogs(events=events), GROUP, since_ms=NOW - 86_400_000, keyword="EMP-042"
    )
    rows = {r["session_id"]: r for r in out["streams"]}
    assert set(rows) == {SID_A, SID_B}  # otel-rt-logs itself had no unattributed hit
    assert rows[SID_B]["kind"] == "session"  # credited to its own stream, not a slice
    assert rows[SID_B]["match"] == "content" and rows[SID_B]["matches"] == 2
    assert "EMP-042" in rows[SID_A]["snippet"]


def test_snippet_flattens_escaped_json():
    message = '{"body": "…\\\\n\\\\n**Tip** check \\\\"battery\\\\" EMP-042"}'
    assert log_streams._snippet(message, "EMP-042").endswith('**Tip** check "battery" EMP-042"}')


def test_filter_scan_is_capped_and_hidden_sessions_are_dropped():
    logs = FakeLogs(filter_pages=log_streams.MAX_FILTER_PAGES + 5)
    out = log_streams.list_streams(
        logs, GROUP, since_ms=NOW - 86_400_000, keyword="x", visible=lambda s: s != SID_A
    )
    assert len(logs.filter_calls) == log_streams.MAX_FILTER_PAGES
    assert out["truncated"] is True
    out = log_streams.list_streams(
        FakeLogs(), GROUP, since_ms=NOW - 86_400_000, visible=lambda s: s != SID_A
    )
    assert SID_A not in [r["session_id"] for r in out["streams"]]


HARNESS_STREAMS = [
    _stream("otel-rt-logs", NOW),
    *[_stream(f"2026/09/30/[runtime-logs]vm-{i:04d}", NOW - i) for i in range(1, 6)],
]


def test_harness_sessions_are_otel_slices(monkeypatch):
    events = [
        {"logStreamName": "otel-rt-logs", "message": _otel(SID_A, "a"), "timestamp": NOW - 50},
        {"logStreamName": "otel-rt-logs", "message": _otel(SID_A, "b"), "timestamp": NOW - 10},
        {"logStreamName": "otel-rt-logs", "message": _otel(SID_B, "c"), "timestamp": NOW - 900},
    ]
    logs = FakeLogs(streams=HARNESS_STREAMS, events=events)
    out = log_streams.list_streams(logs, GROUP, since_ms=NOW - 86_400_000)
    assert logs.filter_calls[0]["logStreamNames"] == ["otel-rt-logs"]
    slices = [r for r in out["streams"] if r["kind"] == "otel_session"]
    assert [r["session_id"] for r in slices] == [SID_A, SID_B]
    assert slices[0]["stream"] == "otel-rt-logs" and slices[0]["match"] is None
    assert slices[0]["first_event"] < slices[0]["last_event"]
    assert sum(r["kind"] == "shared" for r in out["streams"]) == 6

    logs = FakeLogs(streams=HARNESS_STREAMS, events=events)
    out = log_streams.list_streams(logs, GROUP, since_ms=NOW - 86_400_000, keyword="a")
    assert logs.filter_calls[0]["logStreamNames"] == ["otel-rt-logs"]
    assert [r["session_id"] for r in out["streams"] if r["kind"] == "otel_session"] == [
        SID_A, SID_B]

    # the per-microVM streams hitting the cap hides no session
    monkeypatch.setattr(log_streams, "MAX_STREAMS", 3)
    out = log_streams.list_streams(
        FakeLogs(streams=HARNESS_STREAMS, events=events), GROUP, since_ms=0
    )
    assert out["truncated"] is False


def test_missing_log_group_is_empty():
    class Missing(FakeLogs):
        def describe_log_streams(self, **kw):
            error = {"Error": {"Code": "ResourceNotFoundException"}}
            raise ClientError(error, "DescribeLogStreams")

    assert log_streams.list_streams(Missing(), GROUP, since_ms=0)["streams"] == []


def test_route_resolves_the_agent_log_group(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="streams-agent")
    db.close()
    logs = FakeLogs()
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: logs)
    monkeypatch.setattr(
        pipeline_routers, "resolve_telemetry", lambda agent, ws, logs=None: ("svc.DEFAULT", GROUP)
    )
    monkeypatch.setattr(log_streams, "MAX_STREAMS", 2)
    res = client.get(f"/api/eval/agents/{agent.id}/log-streams", params={"hours": 336})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["log_group"] == GROUP and body["hours"] == 336 and body["q"] is None
    # NOW is in the future relative to the wall clock, so every stream is recent;
    # the cap stops the listing at two
    assert len(body["streams"]) == 2 and body["truncated"] is True

    assert client.get("/api/eval/agents/nope/log-streams").status_code == 400


# ─── CloudWatch-only sources ────────────────────────────────────────────────
def test_generic_group_sessions_come_from_event_session_ids():
    streams = [_stream("task/web/1", NOW), _stream("task/web/2", NOW - 10)]
    events = [
        {"logStreamName": "task/web/2", "message": _otel(SID_A, "hi"), "timestamp": NOW - 20},
        {"logStreamName": "task/web/1", "message": "plain line", "timestamp": NOW - 5},
    ]
    logs = FakeLogs(streams=streams, events=events)
    out = log_streams.list_streams(logs, "/ecs/agent", since_ms=NOW - 86_400_000)
    assert "logStreamNames" not in logs.filter_calls[0]  # no otel-rt-logs: scan the group
    [slice_] = [r for r in out["streams"] if r["kind"] == "otel_session"]
    assert (slice_["session_id"], slice_["stream"]) == (SID_A, "task/web/2")


def test_list_log_groups_paginates_and_caps(monkeypatch):
    class Groups:
        def __init__(self):
            self.calls = []

        def describe_log_groups(self, **kw):
            self.calls.append(kw)
            n = len(self.calls)
            return {"logGroups": [{"logGroupName": f"/g/{n}-{i}", "creationTime": NOW}
                                  for i in range(2)], "nextToken": f"t{n}"}

    monkeypatch.setattr(log_streams, "MAX_LOG_GROUPS", 4)
    logs = Groups()
    out = log_streams.list_log_groups(logs, "claw")
    assert [g["name"] for g in out["log_groups"]] == ["/g/1-0", "/g/1-1", "/g/2-0", "/g/2-1"]
    assert out["truncated"] is True and logs.calls[0]["logGroupNamePattern"] == "claw"


def test_services_suggest_spans_plus_the_resource_log_group():
    rows = [
        {"service": "clawbot-agent-runtime", "spans": "181", "sessions": "1",
         "last_ns": "1790726775671032060", "log_groups": "/ecs/clawbot"},
        {"service": "rt.DEFAULT", "spans": "3", "sessions": "2", "last_ns": "1790726775000000000"},
    ]
    owned = type("A", (), {"id": "a1", "name": "hr-bot"})()
    out = log_streams.services_from_rows(rows, lambda s: owned if s == "rt.DEFAULT" else None)
    assert out[0]["log_group_names"] == ["aws/spans", "/ecs/clawbot"] and out[0]["agent"] is None
    assert out[0]["last_seen"].startswith("2026-09-30")
    assert out[1]["log_group_names"] == ["aws/spans"]
    assert out[1]["agent"] == {"id": "a1", "name": "hr-bot"}


def test_evaluable_scopes_follow_the_supported_frameworks_rule():
    ok = ["strands.telemetry.tracer", "strands-agents", "opentelemetry.instrumentation.langchain",
          "openinference.instrumentation.claude_agent_sdk",
          "@aws/aws-distro-opentelemetry-instrumentation-vercel-ai",
          "@arizeai/openinference-instrumentation-openai-agents"]
    no = ["clawbot-agent-runtime", "mycompany.agent.tracing",
          "opentelemetry.instrumentation.botocore.bedrock-runtime",
          "opentelemetry.instrumentation.starlette"]
    assert all(log_streams.evaluable_scope(s) for s in ok)
    assert not any(log_streams.evaluable_scope(s) for s in no)
    rows = [{"service": "claw", "spans": "1", "sessions": "1", "last_ns": "1"},
            {"service": "rt.DEFAULT", "spans": "1", "sessions": "1", "last_ns": "1"},
            {"service": "unknown", "spans": "1", "sessions": "1", "last_ns": "1"}]
    scope_rows = [{"service": "claw", "scope": "clawbot-agent-runtime"},
                  {"service": "rt.DEFAULT", "scope": "opentelemetry.instrumentation.starlette"},
                  {"service": "rt.DEFAULT", "scope": "strands.telemetry.tracer"}]
    out = {s["service_name"]: s for s in log_streams.services_from_rows(rows, None, scope_rows)}
    assert out["claw"]["evaluable"] is False and out["claw"]["scopes"] == ["clawbot-agent-runtime"]
    assert out["rt.DEFAULT"]["evaluable"] is True
    assert out["unknown"]["evaluable"] is None


def test_keyword_query_escapes_regex():
    q = log_streams.keyword_query("a.b/c (x)")
    assert r"like /(?i)a\.b\/c\ \(x\)/" in q or r"like /(?i)a\.b\/c \(x\)/" in q


def test_sessions_from_rows_shapes_and_filters():
    rows = [
        {"session_id": SID_A, "records": "11", "traces": "2", "first": "2026-09-29 06:37:07.936",
         "last": "2026-09-29 06:37:11.263", "log": "123:aws/spans", "stream": "default"},
        {"session_id": SID_B, "traces": "1", "last": "2026-09-28 01:00:00.000"},
    ]
    out = log_streams.sessions_from_rows(rows)
    first = out["streams"][0]
    assert first["stream"] == "aws/spans · default" and first["kind"] == "session"
    assert first["last_event"] == "2026-09-29T06:37:11.263000+00:00" and first["traces"] == 2
    hits = [{"session_id": SID_B, "hits": "3", "sample": "… the EMP-042 record"}]
    out = log_streams.sessions_from_rows(rows, hits, keyword="emp-042")
    assert [(r["session_id"], r["matches"]) for r in out["streams"]] == [(SID_B, 3)]
    assert "EMP-042" in out["streams"][0]["snippet"]
    out = log_streams.sessions_from_rows(rows, visible=lambda s: s != SID_A)
    assert [r["session_id"] for r in out["streams"]] == [SID_B]


def test_discovery_routes(client, monkeypatch):
    seen = {}

    def fake_queries(queries, hours, logs=None, log_groups=None, workspace=None):
        seen.update(queries=queries, hours=hours, log_groups=log_groups)
        if "services" in queries:
            return {"scopes": [], "services": [
                {"service": "clawbot-agent-runtime", "spans": "9", "sessions": "1",
                 "last_ns": "1790726775671032060", "log_groups": "/ecs/clawbot"},
                {"service": "other.DEFAULT", "spans": "1", "sessions": "1", "last_ns": "1"}]}
        return {"sessions": [{"session_id": SID_A, "traces": "1", "last": "2026-09-29 06:37:11.263",
                              "log": "1:aws/spans", "stream": "default"}],
                "hits": [{"session_id": SID_A, "hits": "1", "sample": "Mavic"}]}

    monkeypatch.setattr(pipeline_routers.observability, "run_insights_queries", fake_queries)
    res = client.get("/api/eval/log-services", params={"q": "CLAW", "log_group": "/ecs/clawbot"})
    assert res.status_code == 200, res.text
    assert [s["service_name"] for s in res.json()["services"]] == ["clawbot-agent-runtime"]
    assert seen["log_groups"] == ["aws/spans", "/ecs/clawbot"] and seen["hours"] == 168

    res = client.get("/api/eval/log-sessions", params={
        "service_name": "clawbot-agent-runtime", "log_group": ["aws/spans", "/ecs/clawbot"],
        "q": "mavic", "hours": 24})
    assert res.status_code == 200, res.text
    assert [r["session_id"] for r in res.json()["streams"]] == [SID_A]
    assert 'service.name = "clawbot-agent-runtime"' in seen["queries"]["sessions"]
    assert "hits" in seen["queries"]
    assert client.get("/api/eval/log-sessions", params={
        "service_name": 'x" or 1', "log_group": "aws/spans"}).status_code == 422
    assert client.get("/api/eval/log-sessions", params={
        "service_name": "svc"}).status_code == 422  # log_group required
