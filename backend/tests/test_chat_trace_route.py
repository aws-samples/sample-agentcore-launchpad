"""`GET /api/traces/{session_id}` — the Chat Inspector's trace rail (N-CHAT-obs2).

Route-level contract over a stubbed Logs Insights transport: the response carries the
fields the rail consumes; an id that is not a chat session of the caller's workspace is
404 (indistinguishable from a missing one); the CloudWatch deep links are built from the
workspace's partition — `console.amazonaws.cn` for a cn region, never a global literal.
"""

import json

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.models.ledger import Agent, ChatSession, Workspace
from app.services import traces

from .conftest import ws_ctx

SID = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90"
OTHER_SID = "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
SHARED = "aws/spans"
AGENT_GROUP = "/aws/bedrock-agentcore/runtimes/kb_deep_zip_eeaaea-ly9FpCDNz2-DEFAULT"


def _span(name: str, start_ns: int, end_ns: int, session_id: str = SID) -> str:
    return json.dumps(
        {
            "name": name,
            "traceId": "6a6a005492b3acc3e318e0f22ae9909e",
            "startTimeUnixNano": start_ns,
            "endTimeUnixNano": end_ns,
            "attributes": {"session.id": session_id},
        }
    )


def _row(message: str, group: str) -> dict[str, str]:
    return {"@message": message, "@log": f"111122223333:{group}"}


ROWS = [
    _row(_span("POST /invocations", 1_000_000_000, 3_000_000_000), SHARED),
    _row(_span("chat global.anthropic.claude-sonnet-5", 1_200_000_000, 2_000_000_000), SHARED),
    _row(_span("execute_tool kb_search", 1_400_000_000, 1_800_000_000), AGENT_GROUP),
]


class FakeInsights:
    def __init__(self, rows: list[dict[str, str]] | None = None, fail: bool = False):
        self.rows = rows if rows is not None else ROWS
        self.fail = fail
        self.calls: list[tuple[str, int]] = []

    def __call__(self, queries, hours, logs=None, log_groups=None, workspace=None):
        self.calls.append((next(iter(queries.values())), hours))
        if self.fail:
            raise AppError("observability.query_failed", "boom", status_code=502)
        return {"spans": list(self.rows)}


@pytest.fixture
def insights(monkeypatch):
    fake = FakeInsights()
    monkeypatch.setattr(traces, "run_insights_queries", fake)
    return fake


def _seed_session(session_id: str = SID, workspace_id: str = DEFAULT_WORKSPACE_ID) -> None:
    """A chat session row — what makes the id readable through the chat-history rule."""
    db = SessionLocal()
    try:
        if workspace_id != DEFAULT_WORKSPACE_ID and db.get(Workspace, workspace_id) is None:
            db.add(Workspace(id=workspace_id, name=workspace_id, account_id="999988887777",
                             region="us-west-2", resources={}))
            db.flush()
        agent = Agent(
            workspace_id=workspace_id, name=f"agent-{session_id[:6]}", method="zip_runtime",
            status="active", arn="arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/x",
            spec={"name": "a"},
        )
        db.add(agent)
        db.flush()
        db.add(ChatSession(workspace_id=workspace_id, agent_id=agent.id,
                           session_id=session_id, actor_id="river", turns=1))
        db.commit()
    finally:
        db.close()


# ── the route ────────────────────────────────────────────────────────────────────


def test_route_returns_the_rail_fields_for_an_owned_session(client, insights):
    _seed_session()
    res = client.get(f"/api/traces/{SID}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["session_id"] == SID
    assert body["span_count"] == 3
    assert {s["name"] for s in body["spans"]} == {
        "POST /invocations", "chat global.anthropic.claude-sonnet-5", "execute_tool kb_search",
    }
    for span in body["spans"]:
        assert set(span) >= {"name", "category", "start_ms", "duration_ms", "trace_id"}
    assert body["unavailable_reason"] is None
    assert body["lookback_hours"] == 3
    assert body["cloudwatch_url"].startswith("https://")
    assert "logs-insights" in body["cloudwatch_url"]
    assert body["log_group_url"].startswith("https://")
    # the default lookback reached the transport
    assert insights.calls == [(insights.calls[0][0], 3)]
    assert f'@message like "{SID}"' in insights.calls[0][0]


def test_route_passes_lookback_hours_and_bounds_it(client, insights):
    _seed_session()
    assert client.get(f"/api/traces/{SID}?lookback_hours=72").status_code == 200
    assert insights.calls[-1][1] == 72
    assert client.get(f"/api/traces/{SID}?lookback_hours=0").status_code == 422
    assert client.get(f"/api/traces/{SID}?lookback_hours=999").status_code == 422


def test_route_with_no_spans_is_200_and_empty(client, monkeypatch):
    monkeypatch.setattr(traces, "run_insights_queries", FakeInsights([]))
    _seed_session()
    body = client.get(f"/api/traces/{SID}").json()
    assert body["span_count"] == 0 and body["spans"] == []
    assert body["log_groups"] == []
    assert body["log_group"] == SHARED
    assert body["unavailable_reason"] is None
    # still a usable deep link: the shared destination with the session filter
    assert "aws*2fspans" in body["cloudwatch_url"]
    assert SID in body["cloudwatch_url"]


def test_route_degrades_when_the_query_fails(client, monkeypatch):
    monkeypatch.setattr(traces, "run_insights_queries", FakeInsights(fail=True))
    _seed_session()
    res = client.get(f"/api/traces/{SID}")
    assert res.status_code == 200
    assert res.json()["unavailable_reason"] == "observability.query_failed"
    assert res.json()["span_count"] == 0


def test_unknown_session_is_404_before_any_query(client, insights):
    res = client.get(f"/api/traces/{OTHER_SID}")
    assert res.status_code == 404
    assert res.json()["code"] == "chat.session_not_found"
    assert insights.calls == []


def test_another_workspaces_session_is_404(client, insights):
    _seed_session(OTHER_SID, workspace_id="ws-other")
    res = client.get(f"/api/traces/{OTHER_SID}")
    assert res.status_code == 404
    assert res.json()["code"] == "chat.session_not_found"
    assert insights.calls == []


def test_malformed_session_id_is_422(client, insights):
    # quote / pipe / backslash are excluded by the id shape (Logs Insights literal safety)
    assert client.get('/api/traces/abc"def|ghi').status_code in (404, 422)
    assert client.get("/api/traces/short").status_code == 422
    assert insights.calls == []


# ── partition-aware console links (C2) ─────────────────────────────────────────────


def test_cn_workspace_links_open_the_china_console(insights):
    result = traces.session_trace(SID, ws_ctx(region="cn-northwest-1"))
    for url in (result["cloudwatch_url"], result["log_group_url"]):
        assert url.startswith("https://cn-northwest-1.console.amazonaws.cn/cloudwatch/home?region=cn-northwest-1#")
        assert "console.aws.amazon.com" not in url
        assert ".amazonaws.com" not in url
    assert result["cloudwatch_url"].startswith(
        "https://cn-northwest-1.console.amazonaws.cn/cloudwatch/home?region=cn-northwest-1"
        "#logsV2:logs-insights$3FqueryDetail$3D"
    )


def test_global_workspace_links_open_the_global_console(insights):
    result = traces.session_trace(SID, ws_ctx(region="us-west-2"))
    assert result["cloudwatch_url"].startswith(
        "https://us-west-2.console.aws.amazon.com/cloudwatch/home"  # cn-lint: allow
        "?region=us-west-2#logsV2:logs-insights"
    )
    assert result["log_group_url"].startswith(
        "https://us-west-2.console.aws.amazon.com/cloudwatch/home"  # cn-lint: allow
        "?region=us-west-2#logsV2:log-groups/log-group/"
    )


def test_insights_link_carries_the_session_filter_sources_and_window(insights):
    """The deep link reproduces the rail's own query: both contributing groups as the
    source, the session id in the filter, `lookback_hours` as a relative window."""
    result = traces.session_trace(SID, ws_ctx(region="cn-northwest-1"), lookback_hours=72)
    url = result["cloudwatch_url"]
    # console encoding: structural `~ ( ) '` are `$257E $2528 $2529 $2527`, strings `*xx`
    assert "$257Estart$257E-259200$257EtimeType$257E$2527RELATIVE$257Eunit$257E$2527seconds" in url
    assert f"filter*20*40message*20like*20*22{SID}*22" in url
    assert "ispresent*28name*29*20or*20ispresent*28spanName*29" in url
    assert "$257Esource$257E$2528" in url
    assert "$2527aws*2fspans" in url
    assert "$2527*2faws*2fbedrock-agentcore*2fruntimes*2fkb_deep_zip_eeaaea-ly9FpCDNz2-DEFAULT" in url  # noqa: E501
    # no raw characters the console would mis-parse inside the hash
    tail = url.split("#", 1)[1]
    assert all(ch not in tail for ch in " \"'()|\n")


def test_log_group_link_double_encodes_the_group_path(insights):
    result = traces.session_trace(SID, ws_ctx(region="cn-northwest-1"))
    assert result["log_group"] == SHARED
    assert result["log_group_url"].endswith("#logsV2:log-groups/log-group/aws$252Fspans")
