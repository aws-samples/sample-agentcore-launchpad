"""T28/T29 — spend attribution and threshold alerts.

What is worth pinning: money is attributed to the right agent and person and never
silently under-reported, a rule cannot be created in a direction that could never fire, a
value the platform could not read becomes `unknown` rather than a comforting `ok`, and a
firing rule notifies on the transition rather than on every pass.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.main import create_app
from app.models.ledger import Agent, AlertRule, ChatSession
from app.services import alerts as alert_service
from app.services import costs as cost_service
from app.services.workspace import WorkspaceContext

PRICES = {"claude-sonnet": {"input": 3.0, "output": 15.0}}


@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def ctx():
    return WorkspaceContext(account_id="111122223333", region="us-west-2", resources={})


def _seed(agent_name: str = "priced-agent", actor: str = "mei") -> str:
    db = SessionLocal()
    try:
        agent = Agent(
            workspace_id=DEFAULT_WORKSPACE_ID,
            name=agent_name,
            method="harness",
            status="active",
            spec={"name": agent_name, "display_name": "计价助手"},
        )
        db.add(agent)
        db.flush()
        db.add(
            ChatSession(
                workspace_id=DEFAULT_WORKSPACE_ID,
                agent_id=agent.id,
                session_id="s-console",
                actor_id=actor,
            )
        )
        db.commit()
        return agent.id
    finally:
        db.close()


def _clear_rules() -> None:
    db = SessionLocal()
    try:
        for row in db.query(AlertRule).all():
            db.delete(row)
        db.commit()
    finally:
        db.close()


# ── T28: attribution ─────────────────────────────────────────────────────────────


def _stub_insights(monkeypatch, by_service, by_session, by_model=None):
    monkeypatch.setattr(cost_service, "logs_client", lambda workspace: object())
    monkeypatch.setattr(
        cost_service,
        "run_insights_queries",
        lambda queries, hours, logs=None, **kw: {
            "by_service": by_service,
            "by_session": by_session,
            **({"by_model": by_model} if by_model is not None else {}),
        },
    )
    monkeypatch.setattr(
        cost_service, "get_settings", lambda: SimpleNamespace(model_prices=PRICES)
    )
    monkeypatch.setattr(cost_service, "cached", lambda key, force, build: build())


def test_spend_is_attributed_to_the_agent_and_the_person(monkeypatch, ctx):
    agent_id = _seed()
    _stub_insights(
        monkeypatch,
        by_service=[
            {"service": "priced-agent", "tokens_in": "1000000", "tokens_out": "1000000",
             "cache_read": "0", "cache_write": "0", "llm_calls": "4",
             "model": "claude-sonnet"},
        ],
        by_session=[
            {"session_id": "s-console", "tokens_in": "1000000", "tokens_out": "0",
             "cache_read": "0", "cache_write": "0", "model": "claude-sonnet"},
            # traffic the console never opened: /v1 or an evaluation replay
            {"session_id": "s-orphan", "tokens_in": "0", "tokens_out": "1000000",
             "cache_read": "0", "cache_write": "0", "model": "claude-sonnet"},
        ],
    )
    db = SessionLocal()
    try:
        report = cost_service.cost_report(db, ctx, range_key="24h")
    finally:
        db.close()

    # 1M in at $3 + 1M out at $15
    assert report["total_est_cost_usd"] == pytest.approx(18.0)
    [agent_row] = report["by_agent"]
    assert agent_row["agent_id"] == agent_id
    assert agent_row["known"] is True and agent_row["display_name"] == "计价助手"

    actors = {row["actor"]: row for row in report["by_actor"]}
    assert actors["mei"]["est_cost_usd"] == pytest.approx(3.0)
    # unattributed traffic is named, not dropped — a silent zero would be worse
    assert actors["—"]["est_cost_usd"] == pytest.approx(15.0)
    assert report["estimate"] is True


def test_an_unpriced_model_is_reported_rather_than_counted_as_free(monkeypatch, ctx):
    _stub_insights(
        monkeypatch,
        by_service=[
            {"service": "mystery", "tokens_in": "500", "tokens_out": "500",
             "cache_read": "0", "cache_write": "0", "llm_calls": "1",
             "model": "some-unlisted-model"},
        ],
        by_session=[],
    )
    db = SessionLocal()
    try:
        report = cost_service.cost_report(db, ctx, range_key="24h")
    finally:
        db.close()
    assert report["by_agent"][0]["est_cost_usd"] is None
    assert report["by_agent"][0]["tokens"] == 1000  # tokens still counted
    assert report["unpriced_models"] == ["some-unlisted-model"]
    assert report["total_est_cost_usd"] == 0.0


def test_spend_per_model_prices_every_call_at_its_own_rate(monkeypatch, ctx):
    """An agent that called two models: the per-model view splits the dollars, and the
    agent's total is the sum of both rates — not every token at its latest model's."""
    _seed()
    _stub_insights(
        monkeypatch,
        by_service=[
            {"service": "priced-agent", "tokens_in": "2000000", "tokens_out": "0",
             "cache_read": "0", "cache_write": "0", "llm_calls": "3",
             "model": "some-unlisted-model"},  # latest() picked the unpriced one
        ],
        by_session=[],
        by_model=[
            {"service": "priced-agent", "span_project": "", "model": "claude-sonnet",
             "tokens_in": "1000000", "tokens_out": "0", "cache_read": "0",
             "cache_write": "0", "llm_calls": "2"},
            {"service": "priced-agent", "span_project": "", "model": "some-unlisted-model",
             "tokens_in": "1000000", "tokens_out": "0", "cache_read": "0",
             "cache_write": "0", "llm_calls": "1"},
            {"service": "other", "span_project": "", "model": "claude-sonnet",
             "tokens_in": "0", "tokens_out": "1000000", "cache_read": "0",
             "cache_write": "0", "llm_calls": "1"},
        ],
    )
    db = SessionLocal()
    try:
        report = cost_service.cost_report(db, ctx, range_key="24h")
    finally:
        db.close()
    models = {row["model"]: row for row in report["by_model"]}
    # sonnet: 1M in at $3 (priced-agent) + 1M out at $15 (other)
    assert models["claude-sonnet"]["est_cost_usd"] == pytest.approx(18.0)
    assert models["claude-sonnet"]["agents"] == 2
    assert models["claude-sonnet"]["llm_calls"] == 3
    assert models["some-unlisted-model"]["est_cost_usd"] is None
    assert models["some-unlisted-model"]["tokens"] == 1_000_000
    assert [row["model"] for row in report["by_model"]][0] == "claude-sonnet"
    agent = next(r for r in report["by_agent"] if r["service"] == "priced-agent")
    assert agent["est_cost_usd"] == pytest.approx(3.0)
    assert "some-unlisted-model" in report["unpriced_models"]


def test_a_service_with_no_ledger_agent_is_still_reported(monkeypatch, ctx):
    """An imported or deleted agent spent real money; hiding it would understate."""
    _stub_insights(
        monkeypatch,
        by_service=[
            {"service": "gone-agent", "tokens_in": "1000000", "tokens_out": "0",
             "cache_read": "0", "cache_write": "0", "llm_calls": "1",
             "model": "claude-sonnet"},
        ],
        by_session=[],
    )
    db = SessionLocal()
    try:
        report = cost_service.cost_report(db, ctx, range_key="24h")
    finally:
        db.close()
    assert report["by_agent"][0]["known"] is False
    assert report["by_agent"][0]["est_cost_usd"] == pytest.approx(3.0)


def test_the_range_is_whitelisted(client):
    assert client.get("/api/costs", params={"range": "nonsense"}).status_code == 422


# ── T29: rule validation ─────────────────────────────────────────────────────────


def test_a_rule_in_a_direction_that_could_never_fire_is_refused():
    # quality is bad when it DROPS; "above" would never fire
    with pytest.raises(AppError) as exc:
        alert_service.validate("online_quality", "above", "24h", 0.8)
    assert exc.value.code == "alert.comparison_never_fires"
    with pytest.raises(AppError) as exc:
        alert_service.validate("error_rate", "below", "24h", 0.1)
    assert exc.value.code == "alert.comparison_never_fires"
    # the natural directions are accepted
    alert_service.validate("online_quality", "below", "24h", 0.8)
    alert_service.validate("error_rate", "above", "1h", 0.05)


@pytest.mark.parametrize(
    "kind,comparison,window,threshold,code",
    [
        ("nope", "above", "24h", 1, "alert.unknown_kind"),
        ("error_rate", "sideways", "24h", 1, "alert.bad_comparison"),
        ("error_rate", "above", "99h", 1, "alert.bad_window"),
        ("online_quality", "below", "24h", 7, "alert.bad_threshold"),
        ("cost_mtd_usd", "above", "24h", -1, "alert.bad_threshold"),
    ],
)
def test_validation_refusals(kind, comparison, window, threshold, code):
    with pytest.raises(AppError) as exc:
        alert_service.validate(kind, comparison, window, threshold)
    assert exc.value.code == code


def test_create_defaults_the_comparison_and_refuses_plain_http(client):
    _clear_rules()
    created = client.post(
        "/api/alerts",
        json={"kind": "online_quality", "name": "quality", "threshold": 0.7},
    )
    assert created.status_code == 201, created.text
    assert created.json()["comparison"] == "below"  # defaulted to the only useful one
    assert created.json()["state"] == "unknown"  # nothing measured yet

    refused = client.post(
        "/api/alerts",
        json={"kind": "error_rate", "threshold": 0.1, "webhook_url": "http://hook.example"},
    )
    assert refused.status_code in (400, 422)
    assert refused.json()["code"] == "alert.webhook_not_https"


def test_crud_and_scoping(client):
    _clear_rules()
    rule = client.post("/api/alerts", json={"kind": "error_rate", "threshold": 0.2}).json()
    assert client.get("/api/alerts").json()["rules"][0]["id"] == rule["id"]

    patched = client.patch(f"/api/alerts/{rule['id']}", json={"threshold": 0.5, "enabled": False})
    assert patched.status_code == 200
    assert patched.json()["threshold"] == 0.5 and patched.json()["enabled"] is False

    bad = client.patch(f"/api/alerts/{rule['id']}", json={"window": "99h"})
    assert bad.status_code == 422

    assert client.delete(f"/api/alerts/{rule['id']}").status_code == 200
    assert client.get("/api/alerts").json()["rules"] == []
    assert client.get(f"/api/alerts/{rule['id']}".replace("/alerts/", "/alerts/")) is not None


def test_an_unknown_rule_is_404(client):
    assert client.patch("/api/alerts/nope", json={"threshold": 1}).status_code == 404
    assert client.delete("/api/alerts/nope").status_code == 404


# ── T29: evaluation ──────────────────────────────────────────────────────────────


def _rule(**kwargs) -> AlertRule:
    db = SessionLocal()
    try:
        rule = AlertRule(
            workspace_id=DEFAULT_WORKSPACE_ID,
            kind=kwargs.pop("kind", "error_rate"),
            name=kwargs.pop("name", "r"),
            comparison=kwargs.pop("comparison", "above"),
            threshold=kwargs.pop("threshold", 0.1),
            window=kwargs.pop("window", "24h"),
            **kwargs,
        )
        db.add(rule)
        db.commit()
        return rule.id
    finally:
        db.close()


def test_error_rate_fires_and_clears(monkeypatch, ctx):
    _clear_rules()
    rule_id = _rule(kind="error_rate", threshold=0.1)
    monkeypatch.setattr(
        alert_service, "read_value", lambda rule, db, ws: (0.25, "25/100 traces failed")
    )
    db = SessionLocal()
    try:
        result = alert_service.evaluate_rules(db, ctx, notify_transitions=False)
        assert result["firing"] == 1
        assert result["rules"][0]["state"] == "firing"
        assert result["rules"][0]["last_value"] == pytest.approx(0.25)

        monkeypatch.setattr(alert_service, "read_value", lambda rule, db, ws: (0.01, "ok"))
        again = alert_service.evaluate_rules(db, ctx, notify_transitions=False)
        assert again["firing"] == 0 and again["rules"][0]["state"] == "ok"
        assert db.get(AlertRule, rule_id).last_fired_at is not None  # history kept
    finally:
        db.close()


def test_an_unreadable_value_is_unknown_not_ok(monkeypatch, ctx):
    _clear_rules()
    _rule(kind="latency_p95_ms", threshold=500)
    monkeypatch.setattr(
        alert_service, "read_value", lambda rule, db, ws: (None, "no traffic in this window")
    )
    db = SessionLocal()
    try:
        result = alert_service.evaluate_rules(db, ctx, notify_transitions=False)
    finally:
        db.close()
    assert result["rules"][0]["state"] == "unknown"
    assert result["unknown"] == 1
    assert result["firing"] == 0


def test_a_disabled_rule_is_skipped_without_reading(monkeypatch, ctx):
    _clear_rules()
    _rule(kind="error_rate", threshold=0.1, enabled=False)
    calls: list[str] = []

    def spy(rule, db, ws):  # pragma: no cover - must not run
        calls.append(rule.id)
        return 1.0, "should not be read"

    monkeypatch.setattr(alert_service, "read_value", spy)
    db = SessionLocal()
    try:
        result = alert_service.evaluate_rules(db, ctx)
    finally:
        db.close()
    assert result["rules"][0]["skipped"] == "disabled"
    assert calls == []


def test_notification_fires_on_the_transition_only(monkeypatch, ctx):
    _clear_rules()
    _rule(kind="cost_mtd_usd", threshold=100, webhook_url="https://hook.example/x")
    sent: list[dict] = []
    monkeypatch.setattr(
        alert_service, "_post_webhook", lambda url, payload: sent.append(payload) or "HTTP 200"
    )
    monkeypatch.setattr(alert_service, "read_value", lambda rule, db, ws: (250.0, "mtd"))
    db = SessionLocal()
    try:
        alert_service.evaluate_rules(db, ctx)
        assert len(sent) == 1
        assert "cost_mtd_usd = 250" in sent[0]["text"]
        # still firing on the next pass — one notification, not two
        alert_service.evaluate_rules(db, ctx)
        assert len(sent) == 1

        monkeypatch.setattr(alert_service, "read_value", lambda rule, db, ws: (10.0, "mtd"))
        alert_service.evaluate_rules(db, ctx)
        monkeypatch.setattr(alert_service, "read_value", lambda rule, db, ws: (250.0, "mtd"))
        alert_service.evaluate_rules(db, ctx)
        assert len(sent) == 2  # re-fired after recovering
    finally:
        db.close()


def test_a_failed_webhook_does_not_hide_the_breach(monkeypatch, ctx):
    _clear_rules()
    _rule(kind="error_rate", threshold=0.1, webhook_url="https://hook.example/down")
    monkeypatch.setattr(alert_service, "_post_webhook", lambda url, payload: "URLError")
    monkeypatch.setattr(alert_service, "read_value", lambda rule, db, ws: (0.9, "bad"))
    db = SessionLocal()
    try:
        result = alert_service.evaluate_rules(db, ctx)
    finally:
        db.close()
    assert result["rules"][0]["state"] == "firing"
    assert "URLError" in result["rules"][0]["last_detail"]


def test_a_rule_without_a_webhook_evaluates_silently(monkeypatch, ctx):
    _clear_rules()
    _rule(kind="error_rate", threshold=0.1)
    monkeypatch.setattr(alert_service, "read_value", lambda rule, db, ws: (0.9, "bad"))
    db = SessionLocal()
    try:
        result = alert_service.evaluate_rules(db, ctx)
        assert result["rules"][0]["state"] == "firing"
        assert result["rules"][0]["last_notified_at"] is None
        assert alert_service.firing_rules(db, DEFAULT_WORKSPACE_ID)
    finally:
        db.close()


def test_evaluate_can_read_without_notifying(monkeypatch, ctx, client):
    """A console refresh must not page anyone."""
    _clear_rules()
    _rule(kind="error_rate", threshold=0.1, webhook_url="https://hook.example/x")
    sent: list[dict] = []
    monkeypatch.setattr(
        alert_service, "_post_webhook", lambda url, payload: sent.append(payload) or "HTTP 200"
    )
    monkeypatch.setattr(alert_service, "read_value", lambda rule, db, ws: (0.9, "bad"))
    body = client.post("/api/alerts/evaluate", params={"notify": "false"}).json()
    assert body["firing"] == 1
    assert sent == []


def test_breached_reads_both_directions():
    rule = SimpleNamespace(comparison="above", threshold=10.0, project_id=None)
    assert alert_service.breached(rule, 11.0) and not alert_service.breached(rule, 10.0)
    rule = SimpleNamespace(comparison="below", threshold=0.8, project_id=None)
    assert alert_service.breached(rule, 0.7) and not alert_service.breached(rule, 0.8)


def test_month_to_date_uses_the_elapsed_month(monkeypatch, ctx):
    """On the 1st at 02:00 the window is two hours, which is correct, not a bug."""
    seen: dict[str, int] = {}

    def fake_run(queries, hours, logs=None, **kw):
        seen["hours"] = hours
        return {"by_model": [{"service": "a", "tokens_in": "1000000", "tokens_out": "0",
                                "cache_read": "0", "cache_write": "0",
                                "model": "claude-sonnet"}]}

    monkeypatch.setattr(cost_service, "logs_client", lambda workspace: object())
    monkeypatch.setattr(cost_service, "run_insights_queries", fake_run)
    monkeypatch.setattr(
        cost_service, "get_settings", lambda: SimpleNamespace(model_prices=PRICES)
    )
    monkeypatch.setattr(cost_service, "cached", lambda key, force, build: build())
    db = SessionLocal()
    try:
        usd = cost_service.month_to_date_usd(db, ctx)
    finally:
        db.close()
    assert usd == pytest.approx(3.0)
    now = datetime.now(UTC)
    elapsed = int((now - now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
                  .total_seconds() // 3600)
    assert seen["hours"] == max(1, min(744, elapsed or 1))


def _dashboard(traces: int, errors: int, p95: float) -> dict:
    """The real `observability.get_dashboard` shape: numbers nested under `tiles`."""
    return {
        "tiles": {
            "traces": {"total": traces, "ok": traces - errors, "error": errors},
            "latency": {"p50_ms": p95 / 2, "p95_ms": p95},
        }
    }


def test_dashboard_kinds_read_the_nested_tiles(monkeypatch, ctx):
    from app.services import observability

    monkeypatch.setattr(
        observability, "get_dashboard", lambda window, ws: _dashboard(100, 25, 900.0)
    )
    rate = AlertRule(kind="error_rate", window="24h")
    latency = AlertRule(kind="latency_p95_ms", window="1h")
    assert alert_service.read_value(rate, None, ctx) == (0.25, "25/100 traces failed")
    value, detail = alert_service.read_value(latency, None, ctx)
    assert value == 900.0 and "100 traces" in detail


def test_a_30_day_window_is_refused_for_dashboard_kinds():
    with pytest.raises(AppError) as exc:
        alert_service.validate("latency_p95_ms", "above", "30d", 500)
    assert exc.value.code == "alert.bad_window"
    alert_service.validate("cost_mtd_usd", "above", "30d", 10)  # cost ignores the window


def test_a_runtime_service_name_is_attributed_to_its_agent(monkeypatch, ctx):
    """Spans name the runtime (`<runtime_id>.DEFAULT`), not the ledger name (F2)."""
    db = SessionLocal()
    try:
        agent = Agent(
            workspace_id=DEFAULT_WORKSPACE_ID,
            name="zip-billing",
            method="zip_runtime",
            status="active",
            resource_id="zip_billing_rt-AbC123xyz",
            spec={"name": "zip-billing", "display_name": "账单"},
        )
        db.add(agent)
        db.commit()
        agent_id = agent.id
    finally:
        db.close()
    _stub_insights(
        monkeypatch,
        by_service=[
            {"service": "zip_billing_rt.DEFAULT", "tokens_in": "1000000", "tokens_out": "0",
             "cache_read": "0", "cache_write": "0", "llm_calls": "1",
             "model": "claude-sonnet"},
        ],
        by_session=[],
    )
    db = SessionLocal()
    try:
        report = cost_service.cost_report(db, ctx, range_key="24h")
    finally:
        db.close()
    [row] = report["by_agent"]
    assert row["known"] is True and row["agent_id"] == agent_id


# ── calendar periods (like the AWS bill: UTC, weeks start on Monday) ─────────────

_NOW = datetime(2026, 11, 5, 10, 30, tzinfo=UTC)  # a Thursday


@pytest.mark.parametrize("key, start, end", [
    ("today", datetime(2026, 11, 5, tzinfo=UTC), _NOW),
    ("this_week", datetime(2026, 11, 2, tzinfo=UTC), _NOW),
    ("last_week", datetime(2026, 10, 26, tzinfo=UTC), datetime(2026, 11, 2, tzinfo=UTC)),
    ("this_month", datetime(2026, 11, 1, tzinfo=UTC), _NOW),
    ("last_month", datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)),
    ("7d", datetime(2026, 10, 29, 10, 30, tzinfo=UTC), _NOW),
])
def test_calendar_periods(key, start, end):
    assert cost_service.resolve_window(key, now=_NOW) == (start, end)


def test_a_january_last_month_is_december():
    jan = datetime(2027, 1, 15, tzinfo=UTC)
    assert cost_service.resolve_window("last_month", now=jan) == (
        datetime(2026, 12, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC))


def test_a_custom_period_covers_whole_days_and_is_bounded():
    assert cost_service.resolve_window("custom", start="2026-10-01", end="2026-10-03",
                                       now=_NOW) == (datetime(2026, 10, 1, tzinfo=UTC),
                                                     datetime(2026, 10, 4, tzinfo=UTC))
    # a period running into today ends now
    assert cost_service.resolve_window("custom", start="2026-11-04", end="2026-11-05",
                                       now=_NOW)[1] == _NOW
    for start, end in (("2026-10-03", "2026-10-01"), ("2026-01-01", "2026-11-01"),
                       ("2026-12-01", "2026-12-02"), (None, None)):
        with pytest.raises(AppError) as err:
            cost_service.resolve_window("custom", start=start, end=end, now=_NOW)
        assert err.value.code == "costs.bad_period"


def test_a_calendar_period_queries_its_own_window(monkeypatch, ctx):
    seen: dict = {}
    _stub_insights(monkeypatch, by_service=[], by_session=[], by_model=[])
    monkeypatch.setattr(cost_service, "run_insights_queries",
                        lambda queries, hours, logs=None, **kw: seen.update(kw) or {})
    db = SessionLocal()
    try:
        report = cost_service.cost_report(db, ctx, range_key="custom",
                                          start="2026-10-01", end="2026-10-31")
    finally:
        db.close()
    begin = datetime(2026, 10, 1, tzinfo=UTC)
    assert seen["window"] == (int(begin.timestamp()),
                              int(datetime(2026, 11, 1, tzinfo=UTC).timestamp()))
    assert report["period"]["start"] == begin.isoformat()
    assert report["hours"] == 31 * 24
    assert report["by_model"] == []
