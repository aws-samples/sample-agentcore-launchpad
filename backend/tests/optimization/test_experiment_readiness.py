from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from app.core.db import SessionLocal
from app.core.errors import AppError
from app.evaluation.models import EvalRun
from app.models.ledger import Agent
from app.optimization import readiness
from app.optimization.models import Experiment


@pytest.fixture(autouse=True)
def clear_readiness_cache():
    readiness.reset_cache()
    yield
    readiness.reset_cache()


def _agent(*, tools: list[dict] | None = None, toolkits: list[str] | None = None) -> Agent:
    db = SessionLocal()
    row = Agent(
        name="readiness-agent",
        method="zip_runtime",
        status="active",
        arn="arn:aws:bedrock-agentcore:us-west-2:123:runtime/readiness",
        resource_id="rt-readiness",
        spec={
            "system_prompt": "help",
            "tools": tools or [],
            "toolkits": toolkits or [],
        },
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    db.expunge(row)
    db.close()
    return row


def _project(agent: Agent, monkeypatch, rows: dict, *, lookback_hours: int = 24):
    monkeypatch.setattr(
        readiness,
        "resolve_telemetry",
        lambda _agent: ('runtime_"quoted".DEFAULT', "/aws/runtime"),
    )
    captured: dict = {}

    def run(queries, hours):
        captured.update(queries=queries, hours=hours)
        return rows

    monkeypatch.setattr(readiness, "run_insights_queries", run)
    db = SessionLocal()
    try:
        result = readiness.project_readiness(
            agent,
            db,
            lookback_hours=lookback_hours,
            force=True,
        )
    finally:
        db.close()
    return result, captured


@pytest.mark.parametrize(
    ("summary", "tools", "expected_state"),
    [
        ([], [], "missing"),
        ([{"trace_count": "2", "session_count": "1"}], [], "sparse"),
        (
            [{"trace_count": "5", "session_count": "3"}],
            [{"tool": "lookup"}],
            "ready",
        ),
    ],
)
def test_readiness_projects_missing_sparse_and_ready(
    monkeypatch, summary, tools, expected_state
):
    agent = _agent(tools=[{"name": "lookup", "description": "Find a record"}])
    result, _ = _project(
        agent,
        monkeypatch,
        {"summary": summary, "tools": tools},
    )
    assert result["state"] == expected_state


def test_readiness_query_uses_dual_read_source_safe_service_and_tool_coverage(
    monkeypatch,
):
    agent = _agent(
        tools=[
            {"name": "lookup", "description": "Find a record"},
            {"name": "write", "description": "Update a record"},
        ]
    )
    result, captured = _project(
        agent,
        monkeypatch,
        {
            "summary": [{
                "trace_count": "8",
                "session_count": "4",
                "latest_trace_ns": "1754265600000000000",
            }],
            "tools": [{"tool": "lookup", "calls": "3"}],
        },
    )

    assert captured["hours"] == 24
    assert all(readiness.SPANS_SOURCE in query for query in captured["queries"].values())
    assert all(
        "ispresent(startTimeUnixNano)" in query
        for query in captured["queries"].values()
    )
    assert (
        'resource.attributes.service.name = "runtime_\\\"quoted\\\".DEFAULT"'
        in captured["queries"]["summary"]
    )
    assert "count_distinct(traceId)" in captured["queries"]["summary"]
    assert result["trace_count"] == 8
    assert result["session_count"] == 4
    assert result["observed_tools"] == ["lookup"]
    assert result["expected_tools"] == ["lookup", "write"]
    assert result["missing_tools"] == ["write"]
    assert result["state"] == "sparse"
    assert result["latest_trace_at"] == "2025-08-04T00:00:00+00:00"


def test_readiness_uses_selected_lookback_window(monkeypatch):
    agent = _agent()
    result, captured = _project(
        agent,
        monkeypatch,
        {"summary": [], "tools": []},
        lookback_hours=168,
    )

    assert captured["hours"] == 168
    assert result["lookback_hours"] == 168


def test_readiness_query_failure_is_unavailable(monkeypatch):
    agent = _agent()
    monkeypatch.setattr(
        readiness,
        "resolve_telemetry",
        lambda _agent: ("runtime.DEFAULT", "/aws/runtime"),
    )

    def fail(*_args, **_kwargs):
        raise AppError("observability.query_failed", "secret AWS detail")

    monkeypatch.setattr(readiness, "run_insights_queries", fail)
    db = SessionLocal()
    try:
        result = readiness.project_readiness(agent, db, force=True)
    finally:
        db.close()

    assert result["state"] == "unavailable"
    assert result["message"] == "CloudWatch trace readiness could not be verified."
    assert "secret" not in result["message"]


def test_readiness_joins_newest_run_without_using_it_as_trace_proof(monkeypatch):
    agent = _agent()
    db = SessionLocal()
    older = EvalRun(
        agent_id=agent.id,
        agent_name=agent.name,
        status="completed",
        session_ids=["old"],
        created_at=datetime.now(UTC) - timedelta(hours=1),
    )
    latest = EvalRun(
        agent_id=agent.id,
        agent_name=agent.name,
        status="waiting",
        session_ids=["s1", "s2"],
        created_at=datetime.now(UTC),
    )
    db.add_all([older, latest])
    db.commit()
    latest_id = latest.id
    db.close()

    result, _ = _project(agent, monkeypatch, {"summary": [], "tools": []})

    assert result["state"] == "missing"
    assert result["latest_run"]["id"] == latest_id
    assert result["latest_run"]["status"] == "waiting"
    assert result["latest_run"]["session_count"] == 2


def test_readiness_cache_and_force_bypass(monkeypatch):
    agent = _agent()
    monkeypatch.setattr(
        readiness,
        "resolve_telemetry",
        lambda _agent: ("runtime.DEFAULT", "/aws/runtime"),
    )
    calls = 0

    def run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "summary": [{"trace_count": "3", "session_count": "3"}],
                "tools": [],
            }
        return {"summary": [], "tools": []}

    monkeypatch.setattr(readiness, "run_insights_queries", run)
    db = SessionLocal()
    try:
        assert readiness.project_readiness(agent, db)["state"] == "ready"
        assert readiness.project_readiness(agent, db)["state"] == "ready"
        assert readiness.project_readiness(agent, db, force=True)["state"] == "missing"
    finally:
        db.close()
    assert calls == 2


def test_readiness_cache_is_partitioned_by_lookback_window(monkeypatch):
    agent = _agent()
    monkeypatch.setattr(
        readiness,
        "resolve_telemetry",
        lambda _agent: ("runtime.DEFAULT", "/aws/runtime"),
    )
    windows: list[int] = []

    def run(_queries, hours):
        windows.append(hours)
        return {"summary": [], "tools": []}

    monkeypatch.setattr(readiness, "run_insights_queries", run)
    db = SessionLocal()
    try:
        readiness.project_readiness(agent, db, lookback_hours=24)
        readiness.project_readiness(agent, db, lookback_hours=168)
        readiness.project_readiness(agent, db, lookback_hours=24)
    finally:
        db.close()

    assert windows == [24, 168]


def test_readiness_get_is_static_route(client, monkeypatch):
    agent = _agent()
    captured: dict = {}

    def project(row, db, *, lookback_hours, force=False):
        captured.update(lookback_hours=lookback_hours, force=force)
        return {
            "agent_id": row.id,
            "lookback_hours": lookback_hours,
            "state": "ready",
            "trace_count": 4,
            "session_count": 3,
            "latest_trace_at": None,
            "observed_tools": [],
            "expected_tools": [],
            "missing_tools": [],
            "latest_run": None,
            "message": None,
        }

    monkeypatch.setattr(
        readiness,
        "project_readiness",
        project,
    )

    response = client.get(
        "/api/experiments/readiness",
        params={"agent_id": agent.id, "lookback_hours": 168, "force": "true"},
    )

    assert response.status_code == 200
    assert response.json()["state"] == "ready"
    assert response.json()["lookback_hours"] == 168
    assert captured == {"lookback_hours": 168, "force": True}


@pytest.mark.parametrize("lookback_hours", [23, 721])
def test_readiness_rejects_out_of_range_window(client, lookback_hours):
    agent = _agent()
    response = client.get(
        "/api/experiments/readiness",
        params={"agent_id": agent.id, "lookback_hours": lookback_hours},
    )
    assert response.status_code == 422


def test_create_rejects_confirmed_zero_before_writing(client, monkeypatch):
    agent = _agent()
    captured: dict = {}

    def project(*_args, **kwargs):
        captured.update(kwargs)
        return {"state": "missing", "agent_id": agent.id}

    monkeypatch.setattr(
        readiness,
        "project_readiness",
        project,
    )
    start = MagicMock()
    monkeypatch.setattr("app.optimization.routers.service.start_experiment", start)

    response = client.post(
        "/api/experiments",
        json={"agent_id": agent.id, "lookback_hours": 168},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "experiment.trace_required"
    assert captured["lookback_hours"] == 168
    assert start.call_count == 0
    db = SessionLocal()
    try:
        assert db.query(Experiment).count() == 0
    finally:
        db.close()


def test_create_fails_open_when_readiness_is_unavailable(client, monkeypatch):
    agent = _agent()
    monkeypatch.setattr(
        readiness,
        "project_readiness",
        lambda *_args, **_kwargs: {"state": "unavailable", "agent_id": agent.id},
    )

    def start(row):
        db = SessionLocal()
        exp = Experiment(
            name="EXP-ready",
            agent_id=row.id,
            agent_name=row.name,
            status="ready",
        )
        db.add(exp)
        db.commit()
        db.refresh(exp)
        db.expunge(exp)
        db.close()
        return exp

    monkeypatch.setattr("app.optimization.routers.service.start_experiment", start)
    response = client.post("/api/experiments", json={"agent_id": agent.id})

    assert response.status_code == 201


HR_TOOL_NAMES = [
    "get_benefits_summary",
    "get_pay_stub",
    "get_pto_balance",
    "lookup_hr_policy",
    "submit_pto_request",
]


def test_toolkit_agent_expects_exactly_its_toolkit_tools(monkeypatch):
    """A toolkit agent's spec.code is None, so expected_tools cannot come from the
    docstring regex — it comes from the toolkit registry. And the template's own
    calculator/current_utc_time must NOT be expected: an unexercised expected tool
    pins state at "sparse" forever, which is the trap the converted Runtime hit
    with file_operations/shell."""
    agent = _agent(toolkits=["hr_assistant"])
    result, _ = _project(
        agent,
        monkeypatch,
        {
            "summary": [{"trace_count": "9", "session_count": "5"}],
            "tools": [{"tool": name} for name in HR_TOOL_NAMES],
        },
    )
    assert result["expected_tools"] == HR_TOOL_NAMES
    assert "calculator" not in result["expected_tools"]
    assert "current_utc_time" not in result["expected_tools"]
    assert result["missing_tools"] == []
    assert result["state"] == "ready"


def test_toolkit_agent_is_sparse_until_every_toolkit_tool_is_observed(monkeypatch):
    agent = _agent(toolkits=["hr_assistant"])
    result, _ = _project(
        agent,
        monkeypatch,
        {
            "summary": [{"trace_count": "9", "session_count": "5"}],
            "tools": [{"tool": "get_pto_balance"}, {"tool": "calculator"}],
        },
    )
    # an *observed* tool that is not expected is fine; the four unobserved ones are not
    assert result["missing_tools"] == [n for n in HR_TOOL_NAMES if n != "get_pto_balance"]
    assert result["state"] == "sparse"
