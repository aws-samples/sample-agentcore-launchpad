"""Session trace rail — one span reader, shared with the Observability views.

The defect being fixed, measured on a real session: the old `filter_log_events` reader
returned **0 spans** at `lookback_hours=72` while 32 spans for that session sat in
`aws/spans` the whole time, because that API scans forward from `startTime` with bounded
pagination.

The multi-group source is forward compatibility for AgentCore's per-agent span
destination (AWS documents it as the default for newer agents). In this account those
per-agent groups currently hold **no spans** — only application logs and OTEL log
records — so the per-agent spans in these fixtures are hypothetical, not observed. What
*is* observed is that those groups' log records match a session-id substring, which is
why the reader must accept only real spans.
"""

import json

import pytest

from app.core.errors import AppError
from app.services import traces

from .conftest import ws_ctx

SID = "bc796356c8bf4349a846de46870fcf633c82b513faa244a49da2be8e3fdc1fdf"
SHARED = "aws/spans"
AGENT_GROUP = "/aws/bedrock-agentcore/runtimes/kb_deep_zip_eeaaea-ly9FpCDNz2-DEFAULT"
ACCOUNT = "434444145045"


def _span(name: str, start_ns: int, end_ns: int) -> str:
    return json.dumps(
        {
            "name": name,
            "traceId": "6a6a005492b3acc3e318e0f22ae9909e",
            "startTimeUnixNano": start_ns,
            "endTimeUnixNano": end_ns,
            "attributes": {"session.id": SID},
        }
    )


def _row(message: str, group: str) -> dict[str, str]:
    # run_insights_queries flattens each result row to {field: value}; `@log` is
    # account-qualified, which is why the group name is the trailing segment.
    return {"@message": message, "@log": f"{ACCOUNT}:{group}"}


class FakeInsights:
    def __init__(self, rows: list[dict[str, str]] | None = None, fail: bool = False):
        self.rows = rows if rows is not None else DEFAULT_ROWS
        self.fail = fail
        self.queries: list[str] = []
        self.hours: list[int] = []

    def __call__(self, queries, hours, logs=None, log_groups=None, workspace=None):
        query = next(iter(queries.values()))
        self.queries.append(query)
        self.hours.append(hours)
        if self.fail:
            raise AppError("observability.query_failed", "boom", status_code=502)
        # Honour the query's namePrefix list the way Logs Insights would, so a reader
        # that narrows back to one log group actually loses rows here instead of the
        # stub handing them over regardless.
        prefixes = [p for p in (SHARED, "/aws/bedrock-agentcore/runtimes/") if p in query]
        visible = [
            r
            for r in self.rows
            if any((r.get("@log") or "").split(":")[-1].startswith(p) for p in prefixes)
        ]
        return {"spans": visible}


DEFAULT_ROWS = [
    _row(_span("POST /invocations", 1_000_000_000, 3_000_000_000), SHARED),
    _row(_span("chat global.anthropic.claude-sonnet-5", 1_200_000_000, 2_000_000_000), SHARED),
    _row(_span("execute_tool kb_search", 1_400_000_000, 1_800_000_000), AGENT_GROUP),
    _row(_span("Bedrock AgentCore.RetrieveMemoryRecords", 1_500_000_000, 1_600_000_000),
         AGENT_GROUP),
]


@pytest.fixture
def insights(monkeypatch):
    fake = FakeInsights()
    monkeypatch.setattr(traces, "run_insights_queries", fake)
    return fake


def test_spans_from_both_log_groups_reach_the_rail(insights):
    """Forward compatibility, not an observed loss: if an agent ever writes spans to its
    own group (AWS's documented default for newer agents), the rail must include them."""
    result = traces.session_trace(SID, ws_ctx())

    assert result["span_count"] == 4
    names = {s["name"] for s in result["spans"]}
    assert "POST /invocations" in names          # aws/spans
    assert "execute_tool kb_search" in names     # per-agent group

    assert result["log_groups"] == [SHARED, AGENT_GROUP] or result["log_groups"] == [
        AGENT_GROUP,
        SHARED,
    ]
    assert set(result["log_groups"]) == {SHARED, AGENT_GROUP}
    assert result["unavailable_reason"] is None


def test_query_uses_the_shared_multi_group_source(insights):
    """Pins the fix: a regression to a single log group fails here."""
    traces.session_trace(SID, ws_ctx())
    query = insights.queries[0]
    assert traces.SPANS_SOURCE in query
    assert "aws/spans" in query
    assert "/aws/bedrock-agentcore/runtimes/" in query
    # substring match, not attributes.session.id equality — see session_span_query
    assert f'@message like "{SID}"' in query


def test_primary_group_is_the_biggest_contributor(monkeypatch):
    rows = [
        _row(_span("a", 1_000_000_000, 2_000_000_000), AGENT_GROUP),
        _row(_span("b", 1_000_000_000, 2_000_000_000), AGENT_GROUP),
        _row(_span("c", 1_000_000_000, 2_000_000_000), SHARED),
    ]
    monkeypatch.setattr(traces, "run_insights_queries", FakeInsights(rows))
    result = traces.session_trace(SID, ws_ctx())

    assert result["log_group"] == AGENT_GROUP
    assert result["log_groups"][0] == AGENT_GROUP
    # the deep link must escape a nested group path, not only aws/spans
    assert "$252Faws$252Fbedrock-agentcore$252Fruntimes" in result["cloudwatch_url"]


def test_lookback_hours_is_passed_through(insights):
    traces.session_trace(SID, ws_ctx(), lookback_hours=168)
    assert insights.hours == [168]


def test_query_failure_leaves_an_empty_rail_without_raising(monkeypatch):
    monkeypatch.setattr(traces, "run_insights_queries", FakeInsights(fail=True))
    result = traces.session_trace(SID, ws_ctx())

    assert result["span_count"] == 0 and result["spans"] == []
    assert result["unavailable_reason"] == "observability.query_failed"
    # still linkable somewhere sensible
    assert result["log_group"] == SHARED
    assert result["log_groups"] == []


def test_no_spans_falls_back_to_the_shared_group_link(monkeypatch):
    monkeypatch.setattr(traces, "run_insights_queries", FakeInsights([]))
    result = traces.session_trace(SID, ws_ctx())

    assert result["span_count"] == 0
    assert result["log_groups"] == []
    assert result["log_group"] == SHARED
    assert "aws$252Fspans" in result["cloudwatch_url"]
    assert result["unavailable_reason"] is None


def test_malformed_message_is_skipped(monkeypatch):
    rows = [
        {"@message": "not json", "@log": f"{ACCOUNT}:{SHARED}"},
        _row(_span("ok", 1_000_000_000, 2_000_000_000), SHARED),
        {"@log": f"{ACCOUNT}:{SHARED}"},  # no message at all
    ]
    monkeypatch.setattr(traces, "run_insights_queries", FakeInsights(rows))
    result = traces.session_trace(SID, ws_ctx())
    assert result["span_count"] == 1


def test_response_keeps_the_fields_the_chat_rail_consumes(insights):
    """frontend/src/pages/Chat.tsx reads exactly these."""
    result = traces.session_trace(SID, ws_ctx())
    for field in ("span_count", "spans", "cloudwatch_url"):
        assert field in result
    for span in result["spans"]:
        assert set(span) >= {"name", "category", "start_ms", "duration_ms", "trace_id"}


def test_route_returns_the_rail(client, monkeypatch):
    import app.routers.governance as gov

    monkeypatch.setattr(gov.trace_service, "run_insights_queries", FakeInsights())
    res = client.get(f"/api/traces/{SID}")
    assert res.status_code == 200
    body = res.json()
    assert body["span_count"] == 4
    assert set(body["log_groups"]) == {SHARED, AGENT_GROUP}
