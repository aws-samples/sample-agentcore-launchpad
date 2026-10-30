# ruff: noqa: F811  — pytest fixtures are imported by name from tests.evaluation.test_scorecard
"""E14 — insights window boundaries (round 16, channel A).

An insights run over a time window (`POST /api/eval/runs {mode: insights, lookback_hours}`):

* every window inside the API bound (1 … 336 h) that resolves to ZERO sessions is refused
  with 409 `run.no_sessions` BEFORE any row exists (N13-obs19), naming the hours it looked
  at — whatever the hours;
* a window with sessions is accepted, encodes its scope as `window:<N>h`, pins the ids it
  found, and never re-reads the window;
* 0 h, 337 h and the charter's 720 h are outside the bound: 422 from the request schema,
  no Logs Insights query at all;
* the same window as an EVALUATORS run is not resolved at submit (plan time keeps it).

Contract sources: `docs/api.md` row `POST /api/eval/runs` ("Insights windows … zero sessions
→ 409 `run.no_sessions` (`detail.lookback_hours`) before any row or job (N13-obs19) …
Evaluators-mode windows are still resolved on the queue worker"), `evaluation-engine.md` §23
("Re-run" — the r13-h rulings). **The 1–336 h bound of `lookback_hours` is documented for the
log-streams / log-services routes only, not for `POST /api/eval/runs`** — flagged in the N16
register; the tests read it from the request schema's refusal, not from a number.

The run queue is stubbed (no worker); the Logs Insights discovery is a seam the test scripts.
"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from sqlalchemy import delete

import app.evaluation.service as svc
from app.core.db import SessionLocal
from app.evaluation.models import EvalRun
from tests.evaluation.test_scorecard import _agent, queued  # noqa: F401 — fixture by name

SCALE = float(os.environ.get("LAUNCHPAD_PROPERTY_SCALE", "1"))
SESSIONS = ["window-session-1" + "x" * 30, "window-session-2" + "x" * 30]
MAX_HOURS = 336


def _runs() -> list[EvalRun]:
    db = SessionLocal()
    try:
        return db.query(EvalRun).all()
    finally:
        db.close()


def _wipe_runs() -> None:
    db = SessionLocal()
    try:
        db.execute(delete(EvalRun))
        db.commit()
    finally:
        db.close()


@pytest.fixture
def discovery(monkeypatch):
    """`svc.discover_sessions` scripted: `found` is what the window resolves to; every call's
    keyword arguments are recorded."""
    state = {"found": [], "calls": []}
    monkeypatch.setattr(svc, "discover_sessions",
                        lambda ws, **kw: state["calls"].append(kw) or list(state["found"]))
    return state


def _insights(client, agent: str, hours: int):
    return client.post("/api/eval/runs", json={
        "agent_id": agent, "mode": "insights", "lookback_hours": hours, "wait_seconds": 0})


@settings(max_examples=max(5, int(30 * SCALE)), deadline=None, database=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.integers(min_value=1, max_value=MAX_HOURS))
@example(1)
@example(MAX_HOURS)
def test_a_zero_session_window_is_409_for_every_hour_count_in_the_bound(client, queued,
                                                                        discovery, hours):
    agent = _agent(name=f"empty-{hours}")
    discovery["calls"].clear()
    res = _insights(client, agent, hours)
    assert res.status_code == 409, res.text
    body = res.json()
    assert body["code"] == "run.no_sessions"
    assert body["detail"] == {"lookback_hours": hours}
    assert f"last {hours} h" in body["message"]
    (query,) = discovery["calls"]  # resolved once, over exactly the requested window
    assert query["time_range"]["endTime"] - query["time_range"]["startTime"] == timedelta(
        hours=hours)
    assert _runs() == []


@settings(max_examples=max(5, int(20 * SCALE)), deadline=None, database=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.integers(min_value=1, max_value=MAX_HOURS),
       st.lists(st.sampled_from(SESSIONS), min_size=1, max_size=2, unique=True))
def test_a_window_with_sessions_pins_them_and_encodes_its_scope(client, queued, discovery,
                                                                hours, found):
    agent = _agent(name=f"full-{hours}")
    discovery["found"] = found
    discovery["calls"].clear()
    try:
        res = _insights(client, agent, hours)
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["dataset_name"] == f"window:{hours}h"
        assert body["session_ids"] == found and body["mode"] == "insights"
        assert len(discovery["calls"]) == 1
        again = client.get(f"/api/eval/runs/{body['id']}").json()
        assert again["session_ids"] == found  # pinned on the row, not re-read
        assert len(discovery["calls"]) == 1
    finally:
        _wipe_runs()


@pytest.mark.parametrize("hours", [0, -1, MAX_HOURS + 1, 720])
def test_hours_outside_the_bound_are_a_schema_422_without_a_query(client, queued, discovery,
                                                                  hours):
    agent = _agent()
    res = _insights(client, agent, hours)
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "validation.invalid_request"
    assert discovery["calls"] == [] and _runs() == []


def test_an_evaluators_window_is_not_resolved_at_submit(client, queued, discovery):
    agent = _agent()
    res = client.post("/api/eval/runs", json={
        "agent_id": agent, "lookback_hours": 24, "evaluators": ["Builtin.Correctness"],
        "wait_seconds": 0})
    assert res.status_code == 201, res.text
    assert res.json()["dataset_name"] == "window:24h" and res.json()["session_ids"] == []
    assert discovery["calls"] == []
