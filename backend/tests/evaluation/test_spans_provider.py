"""N12 E0-5 — `LaunchpadSpansProvider`: the strands `CloudWatchProvider` with an injected
logs client (funnel) and a log-group LIST (per-endpoint runtime groups + aws/spans)."""

import json
from pathlib import Path

import pytest

from app.services.strands_eval.spans_provider import (
    SESSION_ID_RE,
    SPANS_LOG_GROUP,
    LaunchpadSpansProvider,
)
from tests.evaluation.fakes import FakeLogs

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "spans_session.json").read_text()
)
SID, GROUP = FIXTURE["session_id"], FIXTURE["log_group"]


def _logs():
    return FakeLogs({
        f"filter attributes.session.id = '{SID}' | sort": [
            {"@message": json.dumps(r)} for r in FIXTURE["records"]],
        "fields spanId, parentSpanId": [
            {"spanId": r["spanId"], "parentSpanId": r.get("parentSpanId", "")}
            for r in FIXTURE["records"] if "startTimeUnixNano" in r],
    })


def test_constructs_no_boto3_client_and_queries_the_given_groups():
    logs = _logs()
    provider = LaunchpadSpansProvider(logs, [GROUP, SPANS_LOG_GROUP], lookback_hours=6)
    assert provider.get_evaluation_data(SID)["trajectory"].session_id == SID
    groups = [q["logGroupNames"] for q in logs.started]
    assert groups and all(g == [GROUP, SPANS_LOG_GROUP] or g == [SPANS_LOG_GROUP] for g in groups)
    assert all("logGroupName" not in q for q in logs.started)
    # the window is hours, not the library's whole days
    first = logs.started[0]
    assert 0 < first["endTime"] - first["startTime"] <= 6 * 3600 + 1


def test_yields_a_session_with_agent_invocation_output_and_tool_calls():
    from strands_evals.types.trace import AgentInvocationSpan, Session, ToolExecutionSpan

    data = LaunchpadSpansProvider(_logs(), [GROUP]).get_evaluation_data(SID)
    session = data["trajectory"]
    assert isinstance(session, Session) and session.session_id == SID
    spans = [s for t in session.traces for s in t.spans]
    agent = [s for s in spans if isinstance(s, AgentInvocationSpan)]
    assert agent and "12 vacation days" in agent[-1].agent_response
    assert data["output"] and "12 vacation days" in data["output"]
    tools = [s for s in spans if isinstance(s, ToolExecutionSpan)]
    assert [t.tool_call.name for t in tools] == ["hr_lookup"]
    assert tools[0].tool_call.arguments == {"employee": "ada"}


def test_session_helper_returns_the_trajectory():
    from strands_evals.types.trace import Session

    assert isinstance(LaunchpadSpansProvider(_logs(), [GROUP]).session(SID), Session)


def test_runtime_logs_mode_skips_the_aws_spans_hierarchy_query():
    logs = _logs()
    LaunchpadSpansProvider(logs, [GROUP], spans_source="runtime_logs").get_evaluation_data(SID)
    assert all("parentSpanId" not in q["queryString"] for q in logs.started)
    assert all(q["logGroupNames"] == [GROUP] for q in logs.started)


def test_both_mode_runs_the_hierarchy_query_on_aws_spans():
    logs = _logs()
    LaunchpadSpansProvider(logs, [GROUP], spans_source="both").get_evaluation_data(SID)
    hierarchy = [q for q in logs.started if "parentSpanId" in q["queryString"]]
    assert hierarchy and hierarchy[0]["logGroupNames"] == [SPANS_LOG_GROUP]


@pytest.mark.parametrize("bad", ["short", "has space " + "x" * 10, "quote'x" + "y" * 10])
def test_rejects_session_ids_that_could_escape_the_query(bad):
    logs = _logs()
    with pytest.raises(ValueError, match="session"):
        LaunchpadSpansProvider(logs, [GROUP]).get_evaluation_data(bad)
    assert logs.started == []


def test_session_id_pattern_matches_observability():
    from app.services.observability import SESSION_ID_RE as OBS_RE

    assert SESSION_ID_RE.pattern == OBS_RE.pattern


def test_no_spans_is_a_session_not_found():
    from strands_evals.providers.exceptions import SessionNotFoundError

    with pytest.raises(SessionNotFoundError):
        LaunchpadSpansProvider(FakeLogs({}), [GROUP]).get_evaluation_data(SID)


def test_requires_at_least_one_log_group():
    with pytest.raises(ValueError):
        LaunchpadSpansProvider(FakeLogs({}), [])


def test_records_reach_the_mapper_with_credentials_masked():
    """N17-obs307: the records every judge prompt / detector / evidence read is built from
    carry `[redacted]` for ADOT's `aws.auth.account.access_key` (and any key id in a value)."""
    from strands_evals.mappers import detect_otel_mapper

    key_id = "ASIAEXAMPLE00000TEST"
    records = [
        {**r, "attributes": {**(r.get("attributes") or {}),
                             "aws.auth.account.access_key": key_id}}
        for r in FIXTURE["records"]
    ]
    records[0]["attributes"]["http.url"] = f"https://x?X-Amz-Credential={key_id}"
    logs = FakeLogs({
        f"filter attributes.session.id = '{SID}' | sort": [
            {"@message": json.dumps(r)} for r in records],
    })
    seen: list[dict] = []

    class SpyMapper:
        def map_to_session(self, spans, session_id):
            seen.extend(spans)
            return detect_otel_mapper(spans).map_to_session(spans, session_id)

    data = LaunchpadSpansProvider(logs, [GROUP], spans_source="runtime_logs",
                                  mapper=SpyMapper()).get_evaluation_data(SID)
    assert seen and key_id not in json.dumps(seen, default=str)
    assert any((s.get("attributes") or {}).get("aws.auth.account.access_key") == "[redacted]"
               for s in seen)
    assert key_id not in data["trajectory"].model_dump_json()
    assert "12 vacation days" in data["output"]  # the conversation itself is untouched
