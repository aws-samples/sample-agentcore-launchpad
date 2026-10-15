"""Budget reconciliation and AgentCore fees (budgets child 3).

Hermetic: Logs Insights, CloudWatch, AgentCore control and Cost Explorer are
stubbed. Row shapes follow the live probe
(.trellis/tasks/10-15-budgets-reconcile/research/live-probe.md): a span is bucketed by
the minute it ended (``bin(1m)``), the session query groups by (session, trace,
minute), the Gateway query by (trace, minute), online records by (config,
evaluator, minute), and a batch run's ``scores[].count`` is its result-record count.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.evaluation.models import EvalRun, OnlineEvalConfig
from app.main import create_app
from app.models.budgets import UsageEvent, UsageSession
from app.models.ledger import Agent, User
from app.optimization.models import Experiment
from app.services import (
    budget_calibration,
    budget_scheduler,
    budgets,
    online_budget,
    rates,
)
from app.services import budget_reconcile as br
from app.services import users as users_service

SONNET = "global.anthropic.claude-sonnet-5"  # $3 / $15 per 1M
UNPRICED = "acme.unpriced-model-v1"
ACCOUNT = "111122223333"
RUNTIME_ID = "strands_rt_ab12cd-AbCdEf1234"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:us-west-2:{ACCOUNT}:runtime/{RUNTIME_ID}"
RESOURCE = f"{RUNTIME_ARN}/runtime-endpoint/DEFAULT:DEFAULT"
HARNESS_RUNTIME_ID = "harness_store_assistant-Zz9Yy8Xx7W"
HARNESS_RESOURCE = (f"arn:aws:bedrock-agentcore:us-west-2:{ACCOUNT}:runtime/"
                    f"{HARNESS_RUNTIME_ID}/runtime-endpoint/DEFAULT:DEFAULT")
HARNESS_ARN = f"arn:aws:bedrock-agentcore:us-west-2:{ACCOUNT}:harness/store_assistant-Q1"
V1 = {"idle_s": 900.0, "max_lifetime_s": 28800.0, "platform_version": "V1", "known": True}
ADMIN = {"username": "operator", "password": "s3cret-pass"}
PEER = ("127.0.0.1", 4321)


def _safe_now() -> datetime:
    """Now, minute-aligned — but never in the first hours of a month, so every
    window and interval of a test stays in one period."""
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0)
    return max(now, month_start + timedelta(hours=4))


NOW = _safe_now()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    br.reset_runtime_cache()
    online_budget.reset()
    budget_calibration.reset_report_cache()
    infos: dict[str, dict] = {}
    monkeypatch.setattr(br, "runtime_info",
                        lambda ctx, ws, rid, default_idle: dict(infos.get(rid or "", V1)))
    # no AWS from the pause pass unless a test drives it
    monkeypatch.setattr(online_budget, "enforce", _no_pause)
    yield infos


def _no_pause(**_kw):
    return []


# ── ledger helpers ───────────────────────────────────────────────────────────


def _user(username="mia", role="member") -> str:
    db = SessionLocal()
    try:
        user = users_service.find_by_username(db, username)
        if user is None:
            user = User(username=username, username_key=username.lower(),
                        email=f"{username}@acme-corp.com", password_hash="x", role=role,
                        status="active")
            db.add(user)
            db.commit()
        return user.id
    finally:
        db.close()


def _limit(user_id, usd) -> None:
    db = SessionLocal()
    try:
        budgets.assign_user(db, user_id, level_id=None, override_usd=usd)
    finally:
        db.close()


def _agent(method="zip_runtime", model=SONNET, arn=RUNTIME_ARN) -> str:
    db = SessionLocal()
    try:
        spec = {"name": "rt-agent"}
        if model:
            spec["model_id"] = model
        agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=f"a-{method}", method=method,
                      status="active", arn=arn, resource_id=arn.rsplit("/", 1)[-1], spec=spec)
        db.add(agent)
        db.commit()
        return agent.id
    finally:
        db.close()


def _session(sid, user_id, *, agent_id=None, resource_id=RUNTIME_ARN, source="chat",
             first=None, last=None, reconciled_until=None, user_name="mia") -> None:
    db = SessionLocal()
    try:
        db.add(UsageSession(
            session_id=sid, origin_workspace_id=DEFAULT_WORKSPACE_ID, user_id=user_id,
            user_name=user_name, source=source, agent_id=agent_id, resource_id=resource_id,
            first_turn_at=first or NOW - timedelta(minutes=10),
            last_turn_at=last or NOW - timedelta(minutes=6), turns=2,
            reconciled_until=reconciled_until,
        ))
        db.commit()
    finally:
        db.close()


def _row(user_id, usd, *, kind="turn_estimate", session_id=None, estimated=True,
         created_at=None, detail=None, ref=(None, None), source="chat") -> str:
    db = SessionLocal()
    try:
        row = UsageEvent(user_id=user_id, user_name="mia", period=budgets.period_of(),
                         source=source, kind=kind, usd=usd, estimated=estimated,
                         session_id=session_id, origin_workspace_id=DEFAULT_WORKSPACE_ID,
                         detail=detail, ref_type=ref[0], ref_id=ref[1],
                         created_at=created_at or NOW - timedelta(minutes=7))
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _rows(**filters) -> list[UsageEvent]:
    db = SessionLocal()
    try:
        query = db.query(UsageEvent)
        for key, value in filters.items():
            query = query.filter(getattr(UsageEvent, key) == value)
        rows = query.order_by(UsageEvent.created_at, UsageEvent.id).all()
        for row in rows:
            db.expunge(row)
        return rows
    finally:
        db.close()


def _live(**filters) -> list[UsageEvent]:
    return [row for row in _rows(**filters) if row.superseded_by is None]


def _spend(user_id) -> float:
    db = SessionLocal()
    try:
        return budgets.spend(db, user_id, budgets.period_of())
    finally:
        db.close()


def _usage_session(sid) -> UsageSession:
    db = SessionLocal()
    try:
        row = db.get(UsageSession, (sid, DEFAULT_WORKSPACE_ID))
        db.expunge(row)
        return row
    finally:
        db.close()


def _rate(key) -> float:
    return rates.DEFAULT_AGENTCORE_RATES[key]


# ── the stubbed Logs Insights ────────────────────────────────────────────────


def _minute(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:00.000")


def span_row(sid, trace, minute: datetime, *, start: datetime | None = None,
             end: datetime | None = None, resource=RESOURCE, **values) -> dict:
    end = end or minute + timedelta(seconds=30)
    start = start or end - timedelta(seconds=20)
    row = {"session_id": sid, "traceId": trace, "minute": _minute(minute),
           "start_ns": str(int(start.timestamp() * 1e9)),
           "end_ns": str(int(end.timestamp() * 1e9)), "resource": resource}
    row.update({key: str(value) for key, value in values.items()})
    return row


class FakeLogs:
    """Answers each query by what it looks like; records every StartQuery."""

    def __init__(self, sessions=(), gateway=(), online=(), batch=(), runtime_sessions=(),
                 fail=False):
        self.sessions = list(sessions)
        self.gateway = list(gateway)
        self.online = list(online)
        self.batch = list(batch)
        self.runtime_sessions = list(runtime_sessions)
        self.fail = fail
        self.calls: list[dict] = []
        self._results: dict[str, list[dict]] = {}

    def start_query(self, **kwargs):
        if self.fail:
            from tests.conftest import client_error

            raise client_error("InternalServerError", "boom", "StartQuery")
        query = kwargs["queryString"]
        self.calls.append(kwargs)
        if "attributes.session.id in [" in query:
            ids = set(json.loads("[" + query.split("attributes.session.id in [", 1)[1]
                                 .split("]", 1)[0] + "]"))
            rows = [r for r in self.sessions if r["session_id"] in ids]
        elif "AgentCore\\.(Gateway|Policy)" in query:
            traces = set(json.loads("[" + query.split("traceId in [", 1)[1]
                                    .split("]", 1)[0] + "]"))
            rows = [r for r in self.gateway if r["traceId"] in traces]
        elif "batch-evaluations/results" in query:
            rows = self.batch
        elif "evaluations/results/" in query:
            rows = self.online
        else:
            rows = self.runtime_sessions
        qid = f"q{len(self.calls)}"
        self._results[qid] = rows
        return {"queryId": qid}

    def get_query_results(self, queryId):
        rows = self._results[queryId]
        return {"status": "Complete",
                "results": [[{"field": k, "value": v} for k, v in row.items()] for row in rows]}

    def stop_query(self, queryId):
        return {}


def _tick(logs: FakeLogs, now: datetime = NOW) -> dict:
    return br.tick(now, logs_for=lambda _ws: logs)


E = br.window_end(NOW)  # this tick's window end
IN = E - timedelta(minutes=4)  # a minute inside the first window


# ── (a) measured tokens ──────────────────────────────────────────────────────


def test_measured_tokens_supersede_the_session_estimates():
    member = _user()
    agent = _agent()
    _session("s-strands-0001", member, agent_id=agent)
    est1 = _row(member, 0.01, session_id="s-strands-0001")
    est2 = _row(member, 0.02, kind="job_estimate", session_id="s-strands-0001",
                detail={"estimate": "traffic_prompt"}, ref=("experiment", "exp1"))
    logs = FakeLogs(sessions=[
        span_row("s-strands-0001", "t1", IN, tokens_in=10_000, tokens_out=1_000, model=SONNET,
                 llm_calls=2),
        span_row("s-strands-0001", "t2", IN + timedelta(minutes=1), tokens_in=5_000,
                 tokens_out=500),
    ])

    outcome = _tick(logs)

    assert not outcome["failures"]
    measured = _live(session_id="s-strands-0001", kind="tokens")
    assert len(measured) == 1
    row = measured[0]
    assert row.estimated is False and row.user_id == member and row.source == "chat"
    assert (row.tokens_in, row.tokens_out, row.model_id) == (15_000, 1_500, SONNET)
    assert row.usd == pytest.approx((15_000 * 3 + 1_500 * 15) / 1e6)
    assert row.detail["reconciled"] and row.detail["provider_spans"]
    assert row.ref_type == "experiment" and row.ref_id == "exp1"  # the estimate's ref kept
    for est in (est1, est2):
        assert _rows(id=est)[0].superseded_by == row.id
    assert _usage_session("s-strands-0001").reconciled_until.replace(tzinfo=UTC) == E


def test_an_in_band_metered_session_is_never_re_measured():
    """A Harness / Claude SDK session already has exact rows: span tokens would
    count the same calls twice."""
    member = _user()
    _session("s-harness-0001", member, resource_id=HARNESS_ARN)
    exact = _row(member, 0.05, kind="tokens", estimated=False, session_id="s-harness-0001")
    logs = FakeLogs(sessions=[
        span_row("s-harness-0001", "t1", IN, tokens_in=10_000, tokens_out=1_000, model=SONNET,
                 mem_events=3, resource=HARNESS_RESOURCE),
    ])

    _tick(logs)

    tokens = _rows(session_id="s-harness-0001", kind="tokens")
    assert [r.id for r in tokens] == [exact] and tokens[0].superseded_by is None
    # the AgentCore fees are still charged: nothing in-band reports them
    assert len(_rows(session_id="s-harness-0001", kind="memory_event")) == 1


def test_an_unpriced_measurement_keeps_the_estimates_charged():
    member = _user()
    _session("s-unpriced-001", member, agent_id=_agent(model=UNPRICED))
    est = _row(member, 0.01, session_id="s-unpriced-001")
    logs = FakeLogs(sessions=[span_row("s-unpriced-001", "t1", IN, tokens_in=1_000,
                                       tokens_out=100, model=UNPRICED)])

    _tick(logs)

    measured = _rows(session_id="s-unpriced-001", kind="tokens")
    assert len(measured) == 1 and measured[0].usd is None and measured[0].detail["unpriced"]
    assert _rows(id=est)[0].superseded_by is None


def test_mantle_wrapper_only_tokens_are_priced_on_the_wrapper_sums():
    member = _user()
    _session("s-mantle-0001", member, agent_id=_agent())
    _row(member, 0.01, session_id="s-mantle-0001")
    logs = FakeLogs(sessions=[span_row("s-mantle-0001", "t1", IN, wrapper_in=2_000,
                                       wrapper_out=200, model=SONNET)])

    _tick(logs)

    row = _live(session_id="s-mantle-0001", kind="tokens")[0]
    assert (row.tokens_in, row.tokens_out) == (2_000, 200) and row.detail["wrapper_only"]


def test_no_wrapper_fallback_once_a_session_was_measured_on_provider_spans():
    """A Bedrock wrapper span ending a minute after its provider span must not be
    counted again in the next window."""
    member = _user()
    _session("s-boundary-01", member, agent_id=_agent(), reconciled_until=E - timedelta(minutes=10))
    _row(member, 0.0, kind="tokens", estimated=False, session_id="s-boundary-01",
         detail={"reconciled": True, "provider_spans": True})
    logs = FakeLogs(sessions=[span_row("s-boundary-01", "t1", IN, wrapper_in=2_000,
                                       wrapper_out=200)])

    _tick(logs)

    assert len(_rows(session_id="s-boundary-01", kind="tokens")) == 1


def test_a_stray_estimate_of_a_measured_session_is_replaced_by_its_measurement():
    """A turn that settled just after the window that counted its spans."""
    member = _user()
    _session("s-stray-00001", member, agent_id=_agent(), reconciled_until=E - timedelta(minutes=10))
    measured = _row(member, 0.04, kind="tokens", estimated=False, session_id="s-stray-00001",
                    detail={"reconciled": True, "provider_spans": True},
                    created_at=NOW - timedelta(minutes=12))
    stray = _row(member, 0.01, session_id="s-stray-00001", created_at=E - timedelta(minutes=9))

    _tick(FakeLogs())

    assert _rows(id=stray)[0].superseded_by == measured


def test_persona_actor_estimates_are_never_replaced_by_span_tokens():
    member = _user()
    _session("s-persona-001", member, agent_id=_agent(), source="eval")
    actor = _row(member, 0.03, kind="job_estimate", session_id="s-persona-001",
                 detail={"estimate": "persona_actor"}, source="eval")
    logs = FakeLogs(sessions=[span_row("s-persona-001", "t1", IN, tokens_in=1_000,
                                       tokens_out=100, model=SONNET)])

    _tick(logs)

    assert _rows(id=actor)[0].superseded_by is None


# ── (b) exact per-session fees ───────────────────────────────────────────────


T1, T2, T_FOREIGN = "6ad05fcb6179270f17d0337b304b15e6", "6ad05fcb6179270f17d0337b304b15e7", \
    "6ad05fcb6179270f17d0337b304b15e8"


def test_session_span_fees_are_counted_and_priced():
    member = _user()
    _session("s-fees-000001", member, agent_id=_agent())
    logs = FakeLogs(
        sessions=[
            span_row("s-fees-000001", T1, IN, mem_events=7, mem_retrievals=2,
                     web_searches=3, kb_queries=2, kb_agentic_queries=1, ci_sessions=1),
            # the same trace's root span ends after this window: only maps the trace
            span_row("s-fees-000001", T2, E + timedelta(minutes=1)),
        ],
        gateway=[
            {"traceId": T1, "minute": _minute(IN), "gateway_calls": "4", "policy_calls": "2"},
            # T2's gateway spans ended inside the window; its session spans after it
            {"traceId": T2, "minute": _minute(IN), "gateway_calls": "1", "policy_calls": "0"},
            # another box's (or an unknown) trace: never asked for, never charged here
            {"traceId": T_FOREIGN, "minute": _minute(IN), "gateway_calls": "9",
             "policy_calls": "9"},
        ],
    )

    _tick(logs)

    [gateway_query] = [c["queryString"] for c in logs.calls
                       if "AgentCore\\.(Gateway|Policy)" in c["queryString"]]
    assert T1 in gateway_query and T2 in gateway_query and T_FOREIGN not in gateway_query

    def one(kind):
        rows = _rows(session_id="s-fees-000001", kind=kind)
        assert len(rows) == 1, kind
        return rows[0]

    assert one("memory_event").usd == pytest.approx(7 * _rate("memory_event"))
    assert one("memory_retrieval").usd == pytest.approx(2 * _rate("memory_retrieval"))
    gateway = one("gateway_call")
    assert gateway.quantity == 5 and gateway.detail["policy_authorizations"] == 2
    assert gateway.usd == pytest.approx(5 * _rate("gateway_call")
                                        + 2 * _rate("policy_authorization"))
    assert one("web_search").usd == pytest.approx(3 * _rate("web_search_query"))
    kb = one("kb_query")
    assert kb.quantity == 3
    assert kb.usd == pytest.approx(2 * _rate("kb_query") + 1 * _rate("kb_agentic_query"))
    ci = one("code_interpreter")
    cal = rates.DEFAULT_CALIBRATION
    assert ci.estimated is True
    assert ci.usd == pytest.approx(
        cal["code_interpreter_gb_hours_per_session"] * _rate("code_interpreter_gb_hour")
        + cal["code_interpreter_vcpu_hours_per_session"] * _rate("code_interpreter_vcpu_hour"))
    for kind in ("memory_event", "gateway_call", "web_search", "kb_query"):
        assert one(kind).estimated is False and one(kind).user_id == member


def test_only_this_boxs_sessions_are_queried_and_charged():
    member = _user()
    _session("s-mine-000001", member, agent_id=_agent())
    logs = FakeLogs(sessions=[
        span_row("s-mine-000001", "t1", IN, mem_events=1),
        span_row("s-prod2-00001", "t9", IN, mem_events=50, tokens_in=99_999),
    ])

    _tick(logs)

    session_queries = [c["queryString"] for c in logs.calls
                       if "attributes.session.id in [" in c["queryString"]]
    assert session_queries and all('"s-mine-000001"' in q and "s-prod2" not in q
                                   for q in session_queries)
    assert {row.session_id for row in _rows()} == {"s-mine-000001"}


def test_consecutive_windows_never_count_a_span_twice():
    member = _user()
    _session("s-twice-00001", member, agent_id=_agent(), last=NOW - timedelta(minutes=1))
    logs = FakeLogs(sessions=[span_row("s-twice-00001", "t1", IN, mem_events=2)])

    _tick(logs)
    _tick(logs, NOW + timedelta(minutes=10))  # the same spans are still in the scan

    assert len(_rows(session_id="s-twice-00001", kind="memory_event")) == 1


def test_a_failed_window_is_retried_on_the_next_tick():
    member = _user()
    _session("s-retry-00001", member, agent_id=_agent())
    rows = [span_row("s-retry-00001", "t1", IN, mem_events=3)]

    failed = _tick(FakeLogs(sessions=rows, fail=True))

    assert failed["failures"] and failed["failures"][0]["part"] == "sessions"
    assert _usage_session("s-retry-00001").reconciled_until is None
    assert _rows(session_id="s-retry-00001") == []
    db = SessionLocal()
    try:
        state = br.load_state(db)
    finally:
        db.close()
    assert state["workspaces"][DEFAULT_WORKSPACE_ID]["last_error"]["part"] == "sessions"

    _tick(FakeLogs(sessions=rows), NOW + timedelta(minutes=10))

    assert len(_rows(session_id="s-retry-00001", kind="memory_event")) == 1
    assert _usage_session("s-retry-00001").reconciled_until is not None


def test_a_session_id_unsafe_for_a_query_is_never_interpolated():
    member = _user()
    _session('bad"id|drop-me', member, agent_id=_agent())
    logs = FakeLogs()

    _tick(logs)

    assert all("drop-me" not in c["queryString"] for c in logs.calls)
    assert _usage_session('bad"id|drop-me').reconciled_until is not None


def test_a_backlog_drains_over_several_ticks(monkeypatch):
    monkeypatch.setattr(br, "MAX_SESSIONS_PER_TICK", 2)
    member = _user()
    agent = _agent()
    for index in range(2):
        _session(f"s-backlog-{index:04d}", member, agent_id=agent,
                 last=NOW - timedelta(minutes=9 - index))
    # left for the next tick by the cap: read up to an hour ago, with a turn 30
    # minutes ago not read yet (older than the floor a complete pass would set)
    read_until = NOW - timedelta(minutes=60)
    _session("s-backlog-0002", member, agent_id=agent, last=NOW - timedelta(minutes=7),
             reconciled_until=read_until)
    left_over = span_row("s-backlog-0002", "t9", NOW - timedelta(minutes=30), mem_events=4)

    first = _tick(FakeLogs(sessions=[left_over]))
    assert first["totals"]["sessions"] == 2  # the oldest two
    assert _usage_session("s-backlog-0002").reconciled_until.replace(tzinfo=UTC) == read_until

    # a truncated pass never narrows later windows: the left-over session's span from
    # before the first tick is still read on the second
    second = _tick(FakeLogs(sessions=[left_over]), NOW + timedelta(minutes=10))
    assert second["totals"]["sessions"] >= 1
    assert len(_rows(session_id="s-backlog-0002", kind="memory_event")) == 1


def test_a_prefix_that_matches_no_log_group_reads_as_empty():
    """A fresh account has no online-results (or spans) groups yet: the SOURCE
    query is refused with a ValidationException, which must not fail every tick."""
    from app.services import observability
    from tests.conftest import client_error

    class NoGroups:
        def start_query(self, **_kw):
            raise client_error("ValidationException", "No log groups were found for the query.",
                               "StartQuery")

    assert observability.run_insights_queries(
        {"online": br.q_online_results()}, hours=1, logs=NoGroups()) == {"online": []}


# ── (c) Runtime compute ──────────────────────────────────────────────────────


def _compute_rows(sid):
    return _rows(session_id=sid, kind="runtime_compute")


def test_runtime_compute_charges_the_lifetime_plus_the_idle_tail():
    member = _user()
    _session("s-compute-001", member, agent_id=_agent())
    start = IN - timedelta(minutes=2)
    logs = FakeLogs(sessions=[span_row("s-compute-001", "t1", IN, start=start,
                                       end=IN + timedelta(seconds=40))])

    _tick(logs)

    [row] = _compute_rows("s-compute-001")
    seconds = (IN + timedelta(seconds=40) - start).total_seconds() + 900
    cal = rates.DEFAULT_CALIBRATION
    expected = seconds / 3600 * (cal["runtime_default_gb"] * _rate("runtime_v1_gb_hour")
                                 + cal["runtime_default_vcpu"] * _rate("runtime_v1_vcpu_hour"))
    assert row.estimated is True and row.user_id == member and row.source == "chat"
    assert row.quantity == pytest.approx(seconds / 3600, rel=1e-4)
    assert row.usd == pytest.approx(expected, rel=1e-4)
    assert row.detail["tail_s"] == 900 and row.detail["factor"] == "default"
    assert row.detail["runtime_id"] == RUNTIME_ID and row.detail["basis"] == "spans"


def test_a_harness_session_uses_the_harness_seed_and_v2_the_short_tail(_isolated):
    member = _user()
    _session("s-harness-002", member, resource_id=HARNESS_ARN)
    _isolated[HARNESS_RUNTIME_ID] = {**V1, "platform_version": "V2"}
    logs = FakeLogs(sessions=[span_row("s-harness-002", "t1", IN, resource=HARNESS_RESOURCE,
                                       start=IN, end=IN + timedelta(seconds=60))])

    _tick(logs)

    [row] = _compute_rows("s-harness-002")
    cal = rates.DEFAULT_CALIBRATION
    hours = (60 + br.V2_IDLE_RECLAIM_SECONDS) / 3600
    assert row.detail["tail_s"] == br.V2_IDLE_RECLAIM_SECONDS and row.detail["harness"]
    assert row.usd == pytest.approx(
        hours * (cal["runtime_harness_gb"] * _rate("runtime_v2_gb_hour")
                 + cal["runtime_harness_vcpu"] * _rate("runtime_v2_vcpu_hour")), rel=1e-4)


def _store_factor(runtime_id, gb, vcpu, sessions):
    db = SessionLocal()
    try:
        rates.store_runtime_factors(db, {runtime_id: {"gb": gb, "vcpu": vcpu,
                                                      "sessions": sessions}})
    finally:
        db.close()


@pytest.mark.parametrize("sessions, factor", [(3, "measured"), (2, "default")])
def test_a_measured_factor_applies_once_enough_sessions_back_it(sessions, factor):
    member = _user()
    _session("s-factor-0001", member, agent_id=_agent())
    _store_factor(RUNTIME_ID, 2.5, 0.05, sessions)
    logs = FakeLogs(sessions=[span_row("s-factor-0001", "t1", IN, start=IN,
                                       end=IN + timedelta(seconds=60))])

    _tick(logs)

    [row] = _compute_rows("s-factor-0001")
    assert row.detail["factor"] == factor
    gb = 2.5 if factor == "measured" else rates.DEFAULT_CALIBRATION["runtime_default_gb"]
    assert row.detail["gb"] == gb


def test_the_runtime_row_is_updated_in_place_until_the_session_idles_out():
    member = _user()
    _session("s-inplace-001", member, agent_id=_agent())
    t0 = IN
    _tick(FakeLogs(sessions=[span_row("s-inplace-001", "t1", t0, start=t0,
                                      end=t0 + timedelta(seconds=30))]))
    [first] = _compute_rows("s-inplace-001")

    # a second turn 8 minutes later: inside the 15-minute tail → the same microVM
    later = NOW + timedelta(minutes=10)
    t1 = t0 + timedelta(minutes=8)
    db = SessionLocal()
    try:
        db.get(UsageSession, ("s-inplace-001", DEFAULT_WORKSPACE_ID)).last_turn_at = t1
        db.commit()
    finally:
        db.close()
    _tick(FakeLogs(sessions=[span_row("s-inplace-001", "t2", t1, start=t1,
                                      end=t1 + timedelta(seconds=30))]), later)
    [updated] = _compute_rows("s-inplace-001")
    assert updated.id == first.id and updated.usd > first.usd
    assert updated.detail["end"] == pytest.approx((t1 + timedelta(seconds=30)).timestamp())
    assert updated.quantity == pytest.approx((8 * 60 + 30 + 900) / 3600, rel=1e-4)

    # an hour later: the tail expired, a new microVM → a new row; the old one is final
    t2 = t1 + timedelta(hours=1)
    db = SessionLocal()
    try:
        db.get(UsageSession, ("s-inplace-001", DEFAULT_WORKSPACE_ID)).last_turn_at = t2
        db.commit()
    finally:
        db.close()
    _tick(FakeLogs(sessions=[span_row("s-inplace-001", "t3", t2, start=t2,
                                      end=t2 + timedelta(seconds=30))]),
          t2 + timedelta(minutes=5))
    rows = _compute_rows("s-inplace-001")
    assert len(rows) == 2 and rows[0].id == first.id and rows[0].usd == updated.usd


def test_the_max_lifetime_ends_a_microvm():
    assert br.billable_seconds(0, 30_000, 900, 28_800) == 28_800
    assert br.billable_seconds(100, 160, 900, 28_800) == 960
    assert br.billable_seconds(100, 160, 120, 28_800) == 180


def test_a_session_without_spans_is_charged_from_its_settled_turns():
    member = _user()
    _session("s-nospans-001", member, agent_id=_agent(method="byoc"),
             first=NOW - timedelta(minutes=12), last=NOW - timedelta(minutes=6))

    _tick(FakeLogs())

    [row] = _compute_rows("s-nospans-001")
    assert row.detail["basis"] == "ledger"
    assert row.quantity == pytest.approx((6 * 60 + 900) / 3600, rel=1e-4)


def test_runtime_info_reads_the_lifecycle_and_falls_back(monkeypatch):
    monkeypatch.undo()  # the real runtime_info, over a stubbed control client
    from app.services.agentcore import runtime as rt

    calls = []

    def get_runtime(_client, runtime_id):
        calls.append(runtime_id)
        if runtime_id == "gone-1":
            raise RuntimeError("ResourceNotFoundException")
        return {"lifecycleConfiguration": {"idleRuntimeSessionTimeout": 300,
                                           "maxLifetime": 3600}}  # no platformVersion

    monkeypatch.setattr(rt, "get_runtime", get_runtime)
    monkeypatch.setattr("app.services.agentcore.client.control_client", lambda ctx: object())
    br.reset_runtime_cache()
    info = br.runtime_info(None, "default", "rt-1", 900)
    assert info == {"idle_s": 300.0, "max_lifetime_s": 3600.0, "platform_version": "V1",
                    "known": True}
    br.runtime_info(None, "default", "rt-1", 900)
    assert calls == ["rt-1"]  # cached
    gone = br.runtime_info(None, "default", "gone-1", 900)
    assert gone["known"] is False and gone["idle_s"] == 900 and gone["platform_version"] == "V1"


# ── (d) evaluations ──────────────────────────────────────────────────────────


def _run(created_by, *, scores, status="completed", batch="run_abc12345-0011223344",
         created_at=None) -> str:
    db = SessionLocal()
    try:
        run = EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id="a1", agent_name="target",
                      mode="evaluators", evaluators=[s["evaluatorId"] for s in scores],
                      status=status, batch_eval_id=batch, scores=scores,
                      created_by=created_by, created_by_name="mia",
                      created_at=created_at or NOW - timedelta(minutes=30),
                      updated_at=NOW - timedelta(minutes=10))
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


def _per_eval():
    cal, fee = rates.DEFAULT_CALIBRATION, rates.DEFAULT_AGENTCORE_RATES
    tin, tout = cal["builtin_eval_input_tokens"], cal["builtin_eval_output_tokens"]
    batch = (tin / 1000 * fee["evaluation_batch_input_per_1k"]
             + tout / 1000 * fee["evaluation_batch_output_per_1k"])
    online = (tin / 1000 * fee["evaluation_builtin_input_per_1k"]
              + tout / 1000 * fee["evaluation_builtin_output_per_1k"])
    judge_usd, _ = rates.price_tokens(get_settings().judge_model_id, tin, tout)
    return batch, online, fee["evaluation_custom"] + (judge_usd or 0.0)


def test_a_finished_run_is_charged_its_evaluation_count_and_the_judge_estimate_superseded():
    member = _user()
    run_id = _run(member, scores=[
        {"evaluatorId": "Builtin.ToolParameterAccuracy", "score": 0.9, "count": 22},
        {"evaluatorId": "Builtin.Helpfulness", "score": 0.5, "count": 12},
        {"evaluatorId": "StoreScenarioAssertions-czm4AIHGMU", "score": 0.8, "count": 6},
    ])
    judges = _row(member, 1.23, kind="job_estimate", source="eval",
                  detail={"estimate": "judges"}, ref=("eval_run", run_id))

    _tick(FakeLogs())

    batch_each, _online, custom_each = _per_eval()
    builtin = _live(ref_id=run_id, kind="evaluation_builtin")
    custom = _live(ref_id=run_id, kind="evaluation_custom")
    assert len(builtin) == 1 and len(custom) == 1
    assert builtin[0].quantity == 34 and builtin[0].usd == pytest.approx(34 * batch_each)
    assert custom[0].quantity == 6 and custom[0].usd == pytest.approx(6 * custom_each)
    assert builtin[0].user_id == member and builtin[0].source == "eval"
    assert builtin[0].estimated is True and builtin[0].detail["tier"] == "batch"
    assert _rows(id=judges)[0].superseded_by == builtin[0].id

    _tick(FakeLogs(), NOW + timedelta(minutes=10))  # counted once
    assert len(_rows(ref_id=run_id, kind="evaluation_builtin")) == 1


def test_a_run_without_counts_on_its_row_counts_its_result_records():
    member = _user()
    run_id = _run(member, status="failed", scores=[], batch="run_def67890-5566778899")
    _row(member, 0.5, kind="job_estimate", source="eval", detail={"estimate": "judges"},
         ref=("eval_run", run_id))
    logs = FakeLogs(batch=[
        {"batch_id": "run_def67890-5566778899", "evaluator": "Builtin.Correctness",
         "evaluations": "5"},
    ])

    _tick(logs)

    [row] = _live(ref_id=run_id, kind="evaluation_builtin")
    assert row.quantity == 5
    assert any("batch-evaluations/results" in c["queryString"]
               and "run_def67890-5566778899" in c["queryString"] for c in logs.calls)


def test_a_judge_estimate_with_no_evaluations_is_replaced_by_zero():
    member = _user()
    run_id = _run(member, status="stopped",
                  scores=[{"evaluatorId": "Builtin.Correctness", "score": None, "count": 0}])
    judges = _row(member, 0.9, kind="job_estimate", source="eval",
                  detail={"estimate": "judges"}, ref=("eval_run", run_id))

    _tick(FakeLogs())

    [row] = _rows(ref_id=run_id, kind="evaluation_builtin")
    assert row.usd == 0 and row.quantity == 0
    assert _rows(id=judges)[0].superseded_by == row.id


def test_runs_from_before_reconciliation_are_never_charged_back():
    member = _user()
    _tick(FakeLogs())  # the first tick dates the reconciler
    old = _run(member, created_at=NOW - timedelta(days=2),
               scores=[{"evaluatorId": "Builtin.Correctness", "score": 1, "count": 4}])

    _tick(FakeLogs(), NOW + timedelta(minutes=10))

    assert _rows(ref_id=old) == []


def _online_config(config_id, created_by, name="mia") -> None:
    db = SessionLocal()
    try:
        db.add(OnlineEvalConfig(workspace_id=DEFAULT_WORKSPACE_ID, agent_id="a1",
                                agent_name="target", config_id=config_id,
                                config_arn=f"arn:x:{config_id}", name=config_id.split("-")[0],
                                service_name="svc.DEFAULT", log_group="/lg",
                                created_by=created_by, created_by_name=name))
        db.commit()
    finally:
        db.close()


def _experiment(created_by, caller) -> str:
    db = SessionLocal()
    try:
        exp = Experiment(workspace_id=DEFAULT_WORKSPACE_ID, name="ab", agent_id="a1",
                         agent_name="target", created_by=created_by, created_by_name="owner",
                         artifacts={"action_callers": {"gateway": {"user_id": caller,
                                                                   "name": "caller"}}})
        db.add(exp)
        db.commit()
        return exp.id
    finally:
        db.close()


def test_online_evaluations_are_charged_to_the_config_payer():
    member = _user()
    owner = _user("owner")
    caller = _user("caller")
    _online_config("oe_target_ab12-AbCdEf1234", member)
    exp_id = _experiment(owner, caller)
    exp_config = f"exp_{exp_id[:8]}_oe1-ZyXwVu9876"
    _tick(FakeLogs())  # the first tick only starts the clock: no history charged
    later = NOW + timedelta(minutes=10)
    window = br.window_end(later) - timedelta(minutes=3)
    online = [
        {"config_id": "oe_target_ab12-AbCdEf1234", "evaluator": "Builtin.Helpfulness",
         "minute": _minute(window), "evaluations": "4"},
        {"config_id": "oe_target_ab12-AbCdEf1234", "evaluator": "MyJudge-Q1w2E3r4T5",
         "minute": _minute(window), "evaluations": "2"},
        {"config_id": exp_config, "evaluator": "Builtin.GoalSuccessRate",
         "minute": _minute(window), "evaluations": "3"},
        # free insights, another box's config, and an evaluation before the clock
        {"config_id": "oe_target_ab12-AbCdEf1234", "evaluator": "Builtin.Insight.UserIntent",
         "minute": _minute(window), "evaluations": "9"},
        {"config_id": "oe_elsewhere_99-QwErTy1234", "evaluator": "Builtin.Helpfulness",
         "minute": _minute(window), "evaluations": "50"},
        {"config_id": "oe_target_ab12-AbCdEf1234", "evaluator": "Builtin.Helpfulness",
         "minute": _minute(E - timedelta(minutes=30)), "evaluations": "70"},
    ]

    _tick(FakeLogs(online=online), later)

    _batch, online_each, custom_each = _per_eval()
    [builtin] = _rows(ref_id="oe_target_ab12-AbCdEf1234", kind="evaluation_builtin")
    [custom] = _rows(ref_id="oe_target_ab12-AbCdEf1234", kind="evaluation_custom")
    assert builtin.user_id == member and builtin.quantity == 4
    assert builtin.usd == pytest.approx(4 * online_each) and builtin.detail["tier"] == "online"
    assert custom.quantity == 2 and custom.usd == pytest.approx(2 * custom_each)
    [exp_row] = _rows(ref_id=exp_config)
    assert exp_row.user_id == caller and exp_row.source == "experiment"
    assert _rows(ref_id="oe_elsewhere_99-QwErTy1234") == []

    _tick(FakeLogs(online=online), later + timedelta(minutes=10))  # never twice
    assert len(_rows(ref_id="oe_target_ab12-AbCdEf1234")) == 2


# ── the standalone-config pause rule ─────────────────────────────────────────


def test_only_standalone_configs_of_over_budget_creators_are_paused(monkeypatch):
    from app.evaluation import online

    monkeypatch.setattr(online_budget, "enforce", REAL_ENFORCE)
    over = _user("over")
    fine = _user("fine")
    admin = _user("boss", role="admin")
    _limit(over, 1.0)
    _limit(fine, 100.0)
    _row(over, 2.0, kind="tokens", estimated=False)
    _row(admin, 50.0, kind="tokens", estimated=False)
    for config_id, creator in (("oe_over_aa-1111111111", over), ("oe_fine_bb-2222222222", fine),
                               ("oe_boss_cc-3333333333", admin)):
        _online_config(config_id, creator)
    reads, pauses = [], []

    def get_config(db, ws, config_id, control=None, **_kw):
        reads.append(config_id)
        return {"owner": "agent", "execution_status": "ENABLED", "config_id": config_id}

    def set_status(db, ws, config_id, *, enabled, control=None, **_kw):
        pauses.append((config_id, enabled))

    monkeypatch.setattr(online, "get_config", get_config)
    monkeypatch.setattr(online, "set_execution_status", set_status)

    paused = online_budget.enforce()

    assert [p["config_id"] for p in paused] == ["oe_over_aa-1111111111"]
    assert pauses == [("oe_over_aa-1111111111", False)]
    assert reads == ["oe_over_aa-1111111111"]  # exempt and under-limit creators: no read
    online_budget.enforce()
    assert reads == ["oe_over_aa-1111111111"]  # handled this period: not re-read


def test_an_experiment_owned_or_already_paused_config_is_left_alone(monkeypatch):
    from app.evaluation import online

    monkeypatch.setattr(online_budget, "enforce", REAL_ENFORCE)
    over = _user("over")
    _limit(over, 1.0)
    _row(over, 2.0, kind="tokens", estimated=False)
    _online_config("oe_x_aa-1111111111", over)
    _online_config("oe_y_bb-2222222222", over)
    status = {"oe_x_aa-1111111111": ("experiment", "ENABLED"),
              "oe_y_bb-2222222222": ("agent", "DISABLED")}
    pauses = []
    monkeypatch.setattr(online, "get_config", lambda db, ws, cid, control=None, **_k: {
        "owner": status[cid][0], "execution_status": status[cid][1]})
    monkeypatch.setattr(online, "set_execution_status",
                        lambda *a, **k: pauses.append(a))

    assert online_budget.enforce() == [] and pauses == []


REAL_ENFORCE = online_budget.enforce


# ── calibration ──────────────────────────────────────────────────────────────


class FakeCW:
    def __init__(self, sums: dict[str, tuple[float, float]]):
        self.sums = sums
        self.calls = []

    def get_metric_data(self, MetricDataQueries, StartTime, EndTime):
        self.calls.append(MetricDataQueries)
        out = []
        for query in MetricDataQueries:
            metric = query["MetricStat"]["Metric"]
            resource = next(d["Value"] for d in metric["Dimensions"] if d["Name"] == "Resource")
            values = self.sums.get(resource)
            if values is None:
                out.append({"Id": query["Id"], "Values": []})
                continue
            value = values[0] if metric["MetricName"] == budget_calibration.GB_METRIC else values[1]
            out.append({"Id": query["Id"], "Values": [value]})
        return {"MetricDataResults": out}


def _compute_row(runtime_id, arn, start: datetime, end: datetime):
    _row(None, 0.01, kind="runtime_compute", session_id=f"cal-{start.minute}",
         detail={"runtime_id": runtime_id, "runtime_arn": arn, "start": start.timestamp(),
                 "end": end.timestamp(), "tail_s": 900},
         created_at=start)


def test_calibration_divides_the_hours_metrics_by_span_session_hours():
    hour = budget_calibration.calibration_hour(NOW)
    _compute_row(RUNTIME_ID, RUNTIME_ARN, hour + timedelta(minutes=5),
                 hour + timedelta(minutes=6))
    # two sessions (one of them another box's) of 1 min each + the 15 min tail, all inside
    spans = [
        {"session_id": "a", "traceId": "t1", "resource": RESOURCE,
         "start_ns": str(int((hour + timedelta(minutes=5)).timestamp() * 1e9)),
         "end_ns": str(int((hour + timedelta(minutes=6)).timestamp() * 1e9))},
        {"session_id": "other-box", "traceId": "t2", "resource": RESOURCE,
         "start_ns": str(int((hour + timedelta(minutes=20)).timestamp() * 1e9)),
         "end_ns": str(int((hour + timedelta(minutes=21)).timestamp() * 1e9))},
        {"session_id": "z", "traceId": "t3", "resource": HARNESS_RESOURCE,  # not touched here
         "start_ns": str(int((hour + timedelta(minutes=5)).timestamp() * 1e9)),
         "end_ns": str(int((hour + timedelta(minutes=6)).timestamp() * 1e9))},
    ]
    session_h = 2 * (16 * 60) / 3600
    cw = FakeCW({RUNTIME_ARN: (1.5 * session_h, 0.02 * session_h)})

    out = budget_calibration.calibrate(NOW, logs_for=lambda _ws: FakeLogs(
        runtime_sessions=spans), cw_for=lambda _ws: cw)

    assert not out["failures"], out
    db = SessionLocal()
    try:
        factors = rates.runtime_factors(db)
        state = br.load_state(db)
    finally:
        db.close()
    entry = factors[RUNTIME_ID]
    assert entry["gb"] == pytest.approx(1.5) and entry["vcpu"] == pytest.approx(0.02)
    assert entry["sessions"] == 2 and entry["last_sample"]["session_hours"] == pytest.approx(
        session_h)
    assert state["calibration"]["samples"] == 1
    # only the runtimes this box touched are read from CloudWatch
    [[gb_query, _cpu_query]] = cw.calls
    assert gb_query["MetricStat"]["Metric"]["Dimensions"][1]["Value"] == RUNTIME_ARN
    # the hour is done: a second pass in the same hour is a no-op
    assert budget_calibration.calibrate(NOW, logs_for=lambda _ws: FakeLogs(),
                                        cw_for=lambda _ws: cw).get("skipped") is True


def test_an_implausible_or_missing_metric_keeps_the_defaults():
    hour = budget_calibration.calibration_hour(NOW)
    _compute_row(RUNTIME_ID, RUNTIME_ARN, hour + timedelta(minutes=5),
                 hour + timedelta(minutes=6))
    spans = [{"session_id": "a", "traceId": "t1", "resource": RESOURCE,
              "start_ns": str(int((hour + timedelta(minutes=5)).timestamp() * 1e9)),
              "end_ns": str(int((hour + timedelta(minutes=6)).timestamp() * 1e9))}]
    # 40 GB per session: the metric covers sessions the spans do not see
    out = budget_calibration.calibrate(NOW, logs_for=lambda _ws: FakeLogs(
        runtime_sessions=spans), cw_for=lambda _ws: FakeCW({RUNTIME_ARN: (40.0, 0.01)}))

    assert out["workspaces"][DEFAULT_WORKSPACE_ID]["dropped"][0]["reason"] == "implausible"
    db = SessionLocal()
    try:
        assert rates.runtime_factors(db) == {}
    finally:
        db.close()


def test_a_merge_weights_history_by_sessions_with_a_cap():
    old = {"gb": 8.0, "vcpu": 0.08, "sessions": 500, "hours": 30, "session_hours": 90.0}
    merged = budget_calibration.merge_factor(
        old, gb=4.0, vcpu=0.04, sessions=50, hours=10.0, hour=NOW, now=NOW,
        runtime_name="x", sample={})
    assert merged["gb"] == pytest.approx(6.0)  # history capped at 50 sessions of weight
    assert merged["sessions"] == 550 and merged["hours"] == 31


def test_runtime_factors_survive_an_admin_rate_edit():
    _store_factor(RUNTIME_ID, 2.0, 0.03, 9)
    db = SessionLocal()
    try:
        rates.set_agentcore_rates(db, rates={"memory_event": 0.00002},
                                  calibration_values={"runtime_default_gb": 1.2}, actor="a")
        assert rates.runtime_factors(db)[RUNTIME_ID]["gb"] == 2.0
        assert rates.calibration(db)["runtime_default_gb"] == 1.2
        with pytest.raises(AppError) as refused:  # measured factors: not admin-editable
            rates.set_agentcore_rates(db, rates=None, calibration_values={"runtimes": 1},
                                      actor="a")
        assert refused.value.code == "rates.unknown_key"
    finally:
        db.close()


# ── the scheduler ────────────────────────────────────────────────────────────


def _settings(enabled: bool, interval: int = 600):
    return SimpleNamespace(budgets_reconcile_enabled=enabled,
                           budgets_reconcile_interval_seconds=interval)


def test_the_escape_hatch_starts_no_thread(monkeypatch):
    monkeypatch.setattr(budget_scheduler, "_THREAD", None)
    monkeypatch.setattr(budget_scheduler.threading, "Thread",
                        lambda *a, **k: pytest.fail("no thread when switched off"))
    assert budget_scheduler.start(_settings(False)) is None


def test_on_by_default_and_at_most_one_thread(monkeypatch):
    import threading

    assert type(get_settings()).model_fields["budgets_reconcile_enabled"].default is True
    monkeypatch.setattr(budget_scheduler, "_THREAD", None)
    ran, sleeps = threading.Event(), []
    park = threading.Event()

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 2:
            park.wait()  # never wake up again mid-suite

    monkeypatch.setattr(budget_scheduler, "_sleep", fake_sleep)
    monkeypatch.setattr(budget_scheduler, "run_once", lambda now=None: ran.set() or {})
    thread = budget_scheduler.start(_settings(True, 5))
    assert ran.wait(5)
    assert budget_scheduler.start(_settings(True, 5)) is thread
    assert sleeps[:2] == [budget_scheduler.STARTUP_DELAY_SECONDS,
                          budget_scheduler.MIN_INTERVAL_SECONDS]


def test_a_tick_never_raises(monkeypatch):
    monkeypatch.setattr(br, "_workspaces", lambda db: (_ for _ in ()).throw(RuntimeError("x")))
    assert br.tick(NOW)["failures"] == []
    monkeypatch.setattr(br, "tick", lambda now=None: (_ for _ in ()).throw(RuntimeError("y")))
    assert "reconcile" not in budget_scheduler.run_once(NOW)


# ── HTTP: admin only, and the report charges nobody ──────────────────────────


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN["password"])
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


@pytest.fixture
def admin(app):
    with TestClient(app, client=PEER) as client:
        assert client.post("/api/auth/login", json=ADMIN).status_code == 200
        yield client


def _member_client(app) -> TestClient:
    client = TestClient(app, client=PEER)
    assert client.post("/api/auth/register", json={
        "username": "mia", "email": "mia@acme-corp.com", "password": "sufficient-pass",
    }).status_code == 201
    db = SessionLocal()
    try:
        user = users_service.find_by_username(db, "mia")
        user.status = users_service.STATUS_ACTIVE
        user.expires_at = datetime.now(UTC) + timedelta(days=7)
        users_service.set_workspace_grants(db, user, [DEFAULT_WORKSPACE_ID])
        db.commit()
    finally:
        db.close()
    assert client.post("/api/auth/login", json={"username": "mia",
                                                "password": "sufficient-pass"}).status_code == 200
    return client


@pytest.mark.parametrize("path", ["/api/budgets/reconcile-status",
                                  "/api/budgets/calibration-report"])
def test_the_reconcile_routes_are_admin_only(app, path):
    assert _member_client(app).get(path).status_code == 403


def test_reconcile_status_reports_the_last_tick_and_factors(admin):
    member = _user("dan")
    _session("s-status-0001", member, agent_id=_agent())
    _tick(FakeLogs(sessions=[span_row("s-status-0001", "t1", IN, mem_events=1)]))
    _store_factor(RUNTIME_ID, 1.4, 0.02, 5)

    body = admin.get("/api/budgets/reconcile-status").json()

    assert body["enabled"] is False  # the suite switches the thread off
    assert body["last_tick"]["window_end"] == br._iso(E)
    assert body["last_tick"]["sessions"] == 1 and body["last_tick"]["rows"] >= 2
    assert body["pending_sessions"] == 0 and body["lag_seconds"] is not None
    [factor] = body["calibration"]["factors"]
    assert factor["runtime_id"] == RUNTIME_ID and factor["used"] is True
    kinds = {k["kind"] for k in body["by_kind"]}
    assert {"memory_event", "runtime_compute"} <= kinds


def test_the_calibration_report_is_account_wide_and_charges_nobody(admin, monkeypatch):
    member = _user("dan")
    _row(member, 0.5, kind="runtime_compute")
    _row(member, 0.1, kind="evaluation_builtin")
    calls = []

    class FakeCE:
        def get_cost_and_usage(self, **kwargs):
            calls.append(kwargs)
            assert kwargs["Filter"]["Dimensions"]["Values"] == ["Amazon Bedrock AgentCore"]
            return {"ResultsByTime": [{"Estimated": True, "Groups": [
                {"Keys": ["USE1-Runtime:Consumption-based:Memory"],
                 "Metrics": {"UnblendedCost": {"Amount": "46.2"}}},
                {"Keys": ["USW2-Evaluations:Consumption-based:BuiltIn-Input:Tier2"],
                 "Metrics": {"UnblendedCost": {"Amount": "35.0"}}},
                {"Keys": ["USW2-Memory:Consumption-based:LTM-Storage:Built-in-memory"],
                 "Metrics": {"UnblendedCost": {"Amount": "0.25"}}},
            ]}]}

    monkeypatch.setattr(budget_calibration, "workspace_context",
                        lambda row: SimpleNamespace(client=lambda svc: FakeCE()))
    before = len(_rows())

    body = admin.get("/api/budgets/calibration-report").json()

    assert body["account_wide"] is True and body["charges_anyone"] is False
    [account] = body["accounts"]
    rows = {r["category"]: r for r in account["rows"]}
    assert rows["runtime"] == {"category": "runtime", "ledger_usd": 0.5,
                               "cost_explorer_usd": 46.2}
    assert rows["evaluations_builtin"]["cost_explorer_usd"] == 35.0
    assert account["overhead_usd"] == 0.25 and account["by_region"]["USE1"] == 46.2
    assert len(_rows()) == before  # report only
    again = admin.get("/api/budgets/calibration-report").json()
    assert again["cached"] is True and len(calls) == 1
    admin.get("/api/budgets/calibration-report?refresh=true")
    assert len(calls) == 2


@pytest.mark.parametrize("usage_type, category", [
    ("USE1-Runtime:Consumption-based:vCPU-v2", "runtime"),
    ("USW2-Evaluations:Consumption-based:CustomEvaluators:Tier1", "evaluations_custom"),
    ("USE1-WebSearchTool:Consumption-based:Queries", "web_search"),
    ("USW2-Memory:Consumption-based:Long-Term-Memory-Retrieval", "memory"),
    ("USW2-Memory:Consumption-based:STM-Storage", "overhead"),
    ("USW2-Gateway:Consumption-based:Tool-Indexing", "overhead"),
    ("USE1-Policy:Consumption-based:Authorization-API-Invocations", "gateway"),
    ("USE1-CodeInterpreter:Consumption-based:Memory", "code_interpreter"),
    ("DataTransfer-Regional-Bytes", "overhead"),
    ("USE1-Something:New", "other"),
])
def test_cost_explorer_usage_types_map_to_report_categories(usage_type, category):
    assert budget_calibration.ce_category(usage_type) == category


def test_a_truncated_chunk_is_split_and_read_again(monkeypatch):
    monkeypatch.setattr(br, "RESULT_LIMIT", 3)
    calls = []

    class Busy:
        def __init__(self):
            self.results = {}

        def start_query(self, **kwargs):
            ids = json.loads("[" + kwargs["queryString"].split("in [", 1)[1].split("]", 1)[0]
                             + "]")
            calls.append(ids)
            qid = f"q{len(calls)}"
            # every id yields two rows: four ids → eight rows, over the limit of 3
            self.results[qid] = [{"id": i, "n": str(k)} for i in ids for k in range(2)]
            return {"queryId": qid}

        def get_query_results(self, queryId):
            return {"status": "Complete", "results": [
                [{"field": k, "value": v} for k, v in row.items()]
                for row in self.results[queryId]]}

    rows = br.chunked_rows(Busy(), ["a", "b", "c", "d"], lambda ids: "SOURCE x | filter s in ["
                           + ", ".join(f'"{i}"' for i in ids) + "]", NOW, NOW)

    assert sorted(len(c) for c in calls) == [1, 1, 1, 1, 2, 2, 4]
    assert len(rows) == 8
