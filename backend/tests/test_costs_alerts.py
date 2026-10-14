"""Spend attribution and threshold alerts (ported from console-v3 T28/T29).

What is worth pinning: money is attributed to the right agent and person and never
silently under-reported, a rule cannot be created in a direction that could never fire, a
value the platform could not read becomes `unknown` rather than a comforting `ok`, a
firing rule notifies on the transition rather than on every pass — and only the
scheduler ever notifies. Every route is ADMIN; the rule never returns its webhook URL.
"""

import socket
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.core import db as db_module
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, Base, SessionLocal, assert_no_schema_drift
from app.core.errors import AppError
from app.main import create_app
from app.models.ledger import Agent, AlertRule, ChatSession, Workspace
from app.services import alerts as alert_service
from app.services import costs as cost_service
from app.services import users as users_service
from app.services import webhook_safety
from app.services.workspace import WorkspaceContext

PRICES = {"claude-sonnet": {"input": 3.0, "output": 15.0}}
PUBLIC_IP = "93.184.216.34"
ADMIN = {"username": "operator", "password": "s3cret-pass"}
PEER = ("127.0.0.1", 4321)


@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def ctx():
    return WorkspaceContext(account_id="111122223333", region="us-west-2", resources={})


@pytest.fixture
def public_dns(monkeypatch):
    """Every host resolves to one public address (webhook URLs are checked on save)."""

    def resolve(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


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


# ── spend attribution ────────────────────────────────────────────────────────────


def _stub_insights(monkeypatch, by_service, by_session):
    monkeypatch.setattr(cost_service, "logs_client", lambda workspace: object())
    monkeypatch.setattr(
        cost_service,
        "run_insights_queries",
        lambda queries, hours, logs=None, **kw: {
            "by_service": by_service,
            "by_session": by_session,
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
             "cache_read": "0", "cache_write": "0", "llm_calls": "4", "sessions": "2",
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
    assert agent_row["agent_name"] == "priced-agent"
    assert agent_row["known"] is True and agent_row["display_name"] == "计价助手"
    assert agent_row["sessions"] == 2

    actors = {row["actor"]: row for row in report["by_actor"]}
    assert actors["mei"]["est_cost_usd"] == pytest.approx(3.0)
    assert actors["mei"]["attributed"] is True
    # unattributed traffic is named, not dropped — a silent zero would be worse
    assert actors["—"]["est_cost_usd"] == pytest.approx(15.0)
    assert actors["—"]["attributed"] is False
    assert report["estimate"] is True


def test_another_workspaces_chat_session_does_not_attribute(monkeypatch, ctx):
    """The ChatSession join is workspace-scoped: a same-id session elsewhere is not ours."""
    db = SessionLocal()
    try:
        db.add(
            Workspace(id="west", name="West", account_id="444455556666",
                      region="us-west-1", bootstrap_status="ready")
        )
        db.add(ChatSession(workspace_id="west", agent_id="a", session_id="s-x",
                           actor_id="bob"))
        db.commit()
    finally:
        db.close()
    _stub_insights(
        monkeypatch,
        by_service=[],
        by_session=[{"session_id": "s-x", "tokens_in": "1000000", "tokens_out": "0",
                     "cache_read": "0", "cache_write": "0", "model": "claude-sonnet"}],
    )
    db = SessionLocal()
    try:
        report = cost_service.cost_report(db, ctx, range_key="24h")
    finally:
        db.close()
    assert [row["actor"] for row in report["by_actor"]] == ["—"]


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


def test_a_wrapper_only_provider_is_priced_on_its_wrapper_tokens(monkeypatch, ctx):
    """A Mantle (`openai.*`) provider emits only the Strands wrapper span, which the
    provider-span sums exclude: without the fallback such an agent showed 0 tokens and
    $0 (measured on dev, 2026-10-14: five services over 30 days). A row that has
    provider tokens keeps them — the wrapper duplicate is never added on top."""
    _stub_insights(
        monkeypatch,
        by_service=[
            {"service": "mantle-agent.DEFAULT", "tokens_in": "0", "tokens_out": "0",
             "cache_read": "0", "cache_write": "0", "wrapper_in": "1000000",
             "wrapper_out": "0", "llm_calls": "0", "wrapper_calls": "3",
             "model": "claude-sonnet"},
            {"service": "bedrock-agent.DEFAULT", "tokens_in": "1000000", "tokens_out": "0",
             "cache_read": "0", "cache_write": "0", "wrapper_in": "1000000",
             "wrapper_out": "0", "llm_calls": "2", "wrapper_calls": "2",
             "model": "claude-sonnet"},
        ],
        by_session=[
            {"session_id": "s-mantle", "tokens_in": "0", "tokens_out": "0",
             "cache_read": "0", "cache_write": "0", "wrapper_in": "0",
             "wrapper_out": "1000000", "model": "claude-sonnet"},
        ],
    )
    db = SessionLocal()
    try:
        report = cost_service.cost_report(db, ctx, range_key="24h")
    finally:
        db.close()
    rows = {row["service"]: row for row in report["by_agent"]}
    assert rows["mantle-agent.DEFAULT"]["tokens"] == 1_000_000
    assert rows["mantle-agent.DEFAULT"]["est_cost_usd"] == pytest.approx(3.0)
    assert rows["mantle-agent.DEFAULT"]["llm_calls"] == 3
    # provider sums present: priced once, not provider + wrapper
    assert rows["bedrock-agent.DEFAULT"]["tokens"] == 1_000_000
    assert rows["bedrock-agent.DEFAULT"]["llm_calls"] == 2
    assert report["total_est_cost_usd"] == pytest.approx(6.0)
    assert report["by_actor"][0]["est_cost_usd"] == pytest.approx(15.0)


def test_tokens_without_a_model_id_are_named_not_free(monkeypatch, ctx):
    _stub_insights(
        monkeypatch,
        by_service=[
            {"service": "anon", "tokens_in": "2000", "tokens_out": "0", "cache_read": "0",
             "cache_write": "0", "llm_calls": "1"},
            # a service with spans but no model and no tokens is nothing to price
            {"service": "tools-only", "tokens_in": "0", "tokens_out": "0"},
        ],
        by_session=[],
    )
    db = SessionLocal()
    try:
        report = cost_service.cost_report(db, ctx, range_key="24h")
    finally:
        db.close()
    assert report["unpriced_models"] == [cost_service.UNKNOWN_MODEL]


def test_a_service_with_no_ledger_agent_is_still_reported(monkeypatch, ctx):
    """An imported or deleted agent — or another console's agent on shared AWS — spent
    real money; hiding it would understate."""
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
    assert report["total_est_cost_usd"] == pytest.approx(3.0)


def test_a_runtime_service_name_is_attributed_to_its_agent(monkeypatch, ctx):
    """Spans name the runtime (`<runtime_id>.DEFAULT`), not the ledger name."""
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


def test_the_queries_read_both_span_layouts_and_require_a_span():
    for query in (cost_service.q_cost_by_service(), cost_service.q_cost_by_session()):
        assert "SOURCE logGroups(namePrefix:" in query
        assert "ispresent(startTimeUnixNano)" in query
        assert "latest(telemetry_model)" in query
        assert "sum(wrap_in) as wrapper_in" in query  # the wrapper-only fallback
    # past the 1000-session cap, the sessions dropped are the smallest, not arbitrary
    session_query = cost_service.q_cost_by_session()
    assert session_query.index("| sort tokens_out desc") < session_query.index("| limit 1000")


def test_the_range_is_whitelisted(client, monkeypatch):
    seen: list[int] = []

    def fake_run(queries, hours, logs=None, **kw):
        seen.append(hours)
        return {"by_service": [], "by_session": []}

    monkeypatch.setattr(cost_service, "logs_client", lambda workspace: object())
    monkeypatch.setattr(cost_service, "run_insights_queries", fake_run)
    monkeypatch.setattr(cost_service, "cached", lambda key, force, build: build())
    assert client.get("/api/costs", params={"range": "nonsense"}).status_code == 422
    ok = client.get("/api/costs", params={"range": "30d"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["hours"] == 720 and seen == [720]


def test_month_to_date_uses_the_elapsed_month(monkeypatch, ctx):
    """On the 1st at 02:00 the window is two hours, which is correct, not a bug."""
    seen: dict[str, int] = {}

    def fake_run(queries, hours, logs=None, **kw):
        seen["hours"] = hours
        return {"by_service": [{"service": "a", "tokens_in": "1000000", "tokens_out": "0",
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
    assert seen["hours"] == cost_service.month_hours()


def test_month_hours_is_clamped():
    assert cost_service.month_hours(datetime(2026, 10, 1, 0, 10, tzinfo=UTC)) == 1
    assert cost_service.month_hours(datetime(2026, 10, 1, 2, 30, tzinfo=UTC)) == 2
    assert cost_service.month_hours(datetime(2026, 10, 31, 23, 0, tzinfo=UTC)) == 720


def test_month_to_date_route(client, monkeypatch):
    monkeypatch.setattr(
        cost_service, "month_to_date",
        lambda db, ws, force=False: {"usd": 12.5, "hours": 40, "unpriced_models": ["x"]},
    )
    body = client.get("/api/costs/month-to-date").json()
    assert body == {
        "workspace_id": DEFAULT_WORKSPACE_ID, "est_cost_usd": 12.5, "hours": 40,
        "unpriced_models": ["x"], "estimate": True,
    }


# ── rule validation ──────────────────────────────────────────────────────────────


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
        # a fraction, not a percent: 5 would be 500 % and never fire
        ("error_rate", "above", "24h", 5, "alert.bad_threshold"),
        ("latency_p95_ms", "above", "24h", float("inf"), "alert.bad_threshold"),
        ("spend_usd", "above", "24h", float("nan"), "alert.bad_threshold"),
    ],
)
def test_validation_refusals(kind, comparison, window, threshold, code):
    with pytest.raises(AppError) as exc:
        alert_service.validate(kind, comparison, window, threshold)
    assert exc.value.code == code


def test_a_30_day_window_is_refused_for_dashboard_kinds():
    with pytest.raises(AppError) as exc:
        alert_service.validate("latency_p95_ms", "above", "30d", 500)
    assert exc.value.code == "alert.bad_window"
    alert_service.validate("cost_mtd_usd", "above", "30d", 10)  # cost ignores the window


def test_create_defaults_the_comparison_and_refuses_plain_http(client):
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
    assert refused.status_code == 400
    assert refused.json()["code"] == "alert.webhook_not_https"


def test_create_refuses_a_private_webhook(client, monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                                      ("10.0.0.5", port))],
    )
    refused = client.post(
        "/api/alerts",
        json={"kind": "error_rate", "threshold": 0.1,
              "webhook_url": "https://hooks.internal.example/x"},
    )
    assert refused.status_code == 400
    assert refused.json()["code"] == "alert.webhook_non_public"


def test_the_creator_is_the_caller_and_a_forged_one_is_ignored(client):
    """Auth disabled → the local operator, `(None, name)` — never the posted value."""
    created = client.post(
        "/api/alerts",
        json={"kind": "error_rate", "threshold": 0.2,
              "created_by": "forged", "created_by_name": "forged"},
    ).json()
    assert created["created_by"] is None
    assert created["created_by_name"] == get_settings().auth_username


def test_the_webhook_url_is_never_returned(client, public_dns):
    created = client.post(
        "/api/alerts",
        json={"kind": "error_rate", "threshold": 0.2,
              "webhook_url": "  https://hooks.slack.com/services/T/B/secret  "},
    )
    assert created.status_code == 201, created.text
    assert created.json()["has_webhook"] is True
    listed = client.get("/api/alerts")
    assert "hooks.slack.com" not in listed.text
    db = SessionLocal()
    try:
        stored = db.get(AlertRule, created.json()["id"]).webhook_url
    finally:
        db.close()
    assert stored == "https://hooks.slack.com/services/T/B/secret"  # trimmed


def test_crud_and_webhook_patch(client, public_dns):
    rule = client.post("/api/alerts", json={"kind": "error_rate", "threshold": 0.2}).json()
    assert client.get("/api/alerts").json()["rules"][0]["id"] == rule["id"]
    assert rule["has_webhook"] is False

    patched = client.patch(f"/api/alerts/{rule['id']}", json={"threshold": 0.5, "enabled": False})
    assert patched.status_code == 200
    assert patched.json()["threshold"] == 0.5 and patched.json()["enabled"] is False

    assert client.patch(f"/api/alerts/{rule['id']}", json={"window": "99h"}).status_code == 422
    # a dashboard kind refuses the 30-day window on edit too
    bad = client.patch(f"/api/alerts/{rule['id']}", json={"window": "30d"})
    assert bad.status_code == 400 and bad.json()["code"] == "alert.bad_window"

    hooked = client.patch(
        f"/api/alerts/{rule['id']}", json={"webhook_url": "https://hook.example/x"}
    )
    assert hooked.json()["has_webhook"] is True
    refused = client.patch(
        f"/api/alerts/{rule['id']}", json={"webhook_url": "https://u:p@hook.example/x"}
    )
    assert refused.status_code == 400 and refused.json()["code"] == "alert.webhook_credentials"
    # a refused edit leaves the stored destination alone; a name-only edit too
    assert client.patch(
        f"/api/alerts/{rule['id']}", json={"name": "renamed"}
    ).json()["has_webhook"] is True
    cleared = client.patch(f"/api/alerts/{rule['id']}", json={"webhook_url": None})
    assert cleared.json()["has_webhook"] is False

    assert client.delete(f"/api/alerts/{rule['id']}").status_code == 200
    assert client.get("/api/alerts").json()["rules"] == []


def test_an_unknown_rule_is_404(client):
    assert client.patch("/api/alerts/nope", json={"threshold": 1}).status_code == 404
    assert client.delete("/api/alerts/nope").status_code == 404


def test_rules_are_workspace_scoped(client):
    db = SessionLocal()
    try:
        db.add(Workspace(id="west", name="West", account_id="444455556666",
                         region="us-west-1", bootstrap_status="ready"))
        db.commit()
    finally:
        db.close()
    west_rule = client.post(
        "/api/alerts", json={"kind": "error_rate", "threshold": 0.2},
        headers={"X-Workspace": "west"},
    ).json()
    assert client.get("/api/alerts").json()["rules"] == []
    assert client.patch(f"/api/alerts/{west_rule['id']}", json={"threshold": 1}).status_code == 404
    assert client.delete(f"/api/alerts/{west_rule['id']}").status_code == 404
    listed = client.get("/api/alerts", headers={"X-Workspace": "west"}).json()["rules"]
    assert [row["id"] for row in listed] == [west_rule["id"]]


def test_the_scheduler_status_route(client, monkeypatch):
    assert client.get("/api/alerts/scheduler").json() == {
        "enabled": False, "interval_seconds": 300,
    }
    from app.routers import alerts as alerts_router

    monkeypatch.setattr(
        alerts_router, "get_settings",
        lambda: SimpleNamespace(alerts_scheduler_enabled=True,
                                alerts_scheduler_interval_seconds=600),
    )
    assert client.get("/api/alerts/scheduler").json() == {
        "enabled": True, "interval_seconds": 600,
    }


# ── evaluation ───────────────────────────────────────────────────────────────────


def _rule(**kwargs) -> str:
    db = SessionLocal()
    try:
        rule = AlertRule(
            workspace_id=kwargs.pop("workspace_id", DEFAULT_WORKSPACE_ID),
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


def _capture_posts(monkeypatch) -> list[tuple[str, dict]]:
    sent: list[tuple[str, dict]] = []

    def fake_post(url, body, **kwargs):
        sent.append((url, body))
        return True, "HTTP 200"

    monkeypatch.setattr(webhook_safety, "post_json", fake_post)
    return sent


def test_error_rate_fires_and_clears(monkeypatch, ctx):
    rule_id = _rule(kind="error_rate", threshold=0.1)
    monkeypatch.setattr(
        alert_service, "read_value", lambda rule, db, ws: (0.25, "25/100 traces failed")
    )
    db = SessionLocal()
    try:
        result = alert_service.evaluate_rules(db, ctx)
        assert result["firing"] == 1
        assert result["rules"][0]["state"] == "firing"
        assert result["rules"][0]["last_value"] == pytest.approx(0.25)

        monkeypatch.setattr(alert_service, "read_value", lambda rule, db, ws: (0.01, "ok"))
        again = alert_service.evaluate_rules(db, ctx)
        assert again["firing"] == 0 and again["rules"][0]["state"] == "ok"
        assert db.get(AlertRule, rule_id).last_fired_at is not None  # history kept
    finally:
        db.close()


def test_an_unreadable_value_is_unknown_not_ok(monkeypatch, ctx):
    _rule(kind="latency_p95_ms", threshold=500)
    monkeypatch.setattr(
        alert_service, "read_value", lambda rule, db, ws: (None, "no traffic in this window")
    )
    db = SessionLocal()
    try:
        result = alert_service.evaluate_rules(db, ctx)
    finally:
        db.close()
    assert result["rules"][0]["state"] == "unknown"
    assert result["unknown"] == 1
    assert result["firing"] == 0


def test_a_disabled_rule_is_skipped_without_reading(monkeypatch, ctx):
    _rule(kind="error_rate", threshold=0.1, enabled=False)
    calls: list[str] = []

    def spy(rule, db, ws):  # pragma: no cover - must not run
        calls.append(rule.id)
        return 1.0, "should not be read"

    monkeypatch.setattr(alert_service, "read_value", spy)
    db = SessionLocal()
    try:
        result = alert_service.evaluate_rules(db, ctx, notify_transitions=True)
    finally:
        db.close()
    assert result["rules"][0]["skipped"] == "disabled"
    assert calls == []


def test_notification_fires_on_the_transition_only(monkeypatch, ctx):
    _rule(kind="cost_mtd_usd", threshold=100, webhook_url="https://hook.example/x")
    sent = _capture_posts(monkeypatch)
    monkeypatch.setattr(alert_service, "read_value", lambda rule, db, ws: (250.0, "mtd"))
    db = SessionLocal()
    try:
        alert_service.evaluate_rules(db, ctx, notify_transitions=True)
        assert len(sent) == 1
        url, body = sent[0]
        assert url == "https://hook.example/x"
        assert "cost_mtd_usd = 250" in body["text"]
        assert set(body) == {"text", "workspace_id", "rule_id", "kind", "value",
                             "threshold", "comparison", "detail"}
        # still firing on the next pass — one notification, not two
        alert_service.evaluate_rules(db, ctx, notify_transitions=True)
        assert len(sent) == 1

        monkeypatch.setattr(alert_service, "read_value", lambda rule, db, ws: (10.0, "mtd"))
        alert_service.evaluate_rules(db, ctx, notify_transitions=True)
        monkeypatch.setattr(alert_service, "read_value", lambda rule, db, ws: (250.0, "mtd"))
        result = alert_service.evaluate_rules(db, ctx, notify_transitions=True)
        assert len(sent) == 2  # re-fired after recovering
        assert result["rules"][0]["last_notified_at"] is not None
        assert "webhook HTTP 200" in result["rules"][0]["last_detail"]
    finally:
        db.close()


def test_a_manual_pass_never_notifies_but_does_not_swallow_the_notification(
    monkeypatch, ctx
):
    """A manual re-check that sees the transition first posts nothing — and the next
    scheduled pass still delivers the episode's one notification (an admin clicking
    Re-check during an incident must not silence the on-call channel)."""
    rule_id = _rule(kind="error_rate", threshold=0.1, webhook_url="https://hook.example/x")
    sent = _capture_posts(monkeypatch)
    monkeypatch.setattr(alert_service, "read_value", lambda rule, db, ws: (0.9, "bad"))
    db = SessionLocal()
    try:
        result = alert_service.evaluate_rules(db, ctx)  # default: no notification
        assert result["firing"] == 1 and sent == []
        started = db.get(AlertRule, rule_id).last_fired_at
        alert_service.evaluate_rules(db, ctx)  # a second manual pass: still nothing
        assert sent == []
        alert_service.evaluate_rules(db, ctx, notify_transitions=True)
        assert len(sent) == 1  # the owed notification
        alert_service.evaluate_rules(db, ctx, notify_transitions=True)
        alert_service.evaluate_rules(db, ctx)
        assert len(sent) == 1  # once per episode
        # `last_fired_at` is when the episode started, not the latest firing pass
        assert db.get(AlertRule, rule_id).last_fired_at == started
    finally:
        db.close()


def test_a_webhook_added_while_firing_is_owed_the_current_episode(monkeypatch, ctx):
    rule_id = _rule(kind="error_rate", threshold=0.1)
    sent = _capture_posts(monkeypatch)
    monkeypatch.setattr(alert_service, "read_value", lambda rule, db, ws: (0.9, "bad"))
    db = SessionLocal()
    try:
        alert_service.evaluate_rules(db, ctx, notify_transitions=True)
        assert sent == []  # nowhere to send it
        db.get(AlertRule, rule_id).webhook_url = "https://hook.example/x"
        db.commit()
        alert_service.evaluate_rules(db, ctx, notify_transitions=True)
        alert_service.evaluate_rules(db, ctx, notify_transitions=True)
        assert len(sent) == 1
    finally:
        db.close()


def test_a_failed_webhook_does_not_hide_the_breach(monkeypatch, ctx):
    _rule(kind="error_rate", threshold=0.1, webhook_url="https://hook.example/down")
    monkeypatch.setattr(webhook_safety, "post_json", lambda url, body, **kw: (False, "URLError"))
    monkeypatch.setattr(alert_service, "read_value", lambda rule, db, ws: (0.9, "bad"))
    db = SessionLocal()
    try:
        result = alert_service.evaluate_rules(db, ctx, notify_transitions=True)
    finally:
        db.close()
    assert result["rules"][0]["state"] == "firing"
    assert "URLError" in result["rules"][0]["last_detail"]


def test_a_rule_without_a_webhook_evaluates_silently(monkeypatch, ctx):
    _rule(kind="error_rate", threshold=0.1)
    sent = _capture_posts(monkeypatch)
    monkeypatch.setattr(alert_service, "read_value", lambda rule, db, ws: (0.9, "bad"))
    db = SessionLocal()
    try:
        result = alert_service.evaluate_rules(db, ctx, notify_transitions=True)
    finally:
        db.close()
    assert result["rules"][0]["state"] == "firing"
    assert result["rules"][0]["last_notified_at"] is None
    assert sent == []


def test_the_evaluate_route_never_notifies(monkeypatch, client):
    """A console re-check must not page anyone — even when asked to."""
    _rule(kind="error_rate", threshold=0.1, webhook_url="https://hook.example/x")
    sent = _capture_posts(monkeypatch)
    monkeypatch.setattr(alert_service, "read_value", lambda rule, db, ws: (0.9, "bad"))
    body = client.post("/api/alerts/evaluate", params={"notify": "true"}).json()
    assert body["firing"] == 1
    assert sent == []


def test_breached_reads_both_directions():
    rule = SimpleNamespace(comparison="above", threshold=10.0)
    assert alert_service.breached(rule, 11.0) and not alert_service.breached(rule, 10.0)
    rule = SimpleNamespace(comparison="below", threshold=0.8)
    assert alert_service.breached(rule, 0.7) and not alert_service.breached(rule, 0.8)


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


def test_no_traffic_and_a_read_failure_are_unknown(monkeypatch, ctx):
    from app.services import observability

    rate = AlertRule(kind="error_rate", window="1h")
    spend = AlertRule(kind="cost_mtd_usd", window="24h")
    monkeypatch.setattr(observability, "get_dashboard", lambda window, ws: _dashboard(0, 0, 0))
    assert alert_service.read_value(rate, None, ctx)[0] is None

    def boom(*a, **k):
        raise RuntimeError("logs down")

    monkeypatch.setattr(observability, "get_dashboard", boom)
    monkeypatch.setattr(cost_service, "month_to_date_usd", boom)
    assert alert_service.read_value(rate, None, ctx)[0] is None
    assert alert_service.read_value(spend, None, ctx)[0] is None
    monkeypatch.setattr(cost_service, "cost_report", boom)
    window_spend = AlertRule(kind="spend_usd", window="7d")
    assert alert_service.read_value(window_spend, None, ctx)[0] is None


def test_window_spend_reads_the_cost_report_for_the_rules_window(monkeypatch, ctx):
    seen: list[str] = []

    def report(db, ws, *, range_key="24h", force=False):
        seen.append(range_key)
        return {"total_est_cost_usd": 12.5}

    monkeypatch.setattr(cost_service, "cost_report", report)
    rule = AlertRule(kind="spend_usd", window="30d")
    value, detail = alert_service.read_value(rule, None, ctx)
    assert value == 12.5
    assert seen == ["30d"]
    assert "30d" in detail
    alert_service.validate("spend_usd", "above", "30d", 10)
    with pytest.raises(AppError) as exc:
        alert_service.validate("spend_usd", "below", "24h", 10)
    assert exc.value.code == "alert.comparison_never_fires"


def test_quality_reads_the_online_snapshot(monkeypatch, ctx):
    from app.evaluation import online

    monkeypatch.setattr(
        online, "online_quality", lambda db, ws: {"mean": 0.62, "scores": 9, "sessions": 3}
    )
    value, detail = alert_service.read_value(AlertRule(kind="online_quality"), None, ctx)
    assert value == pytest.approx(0.62) and "9 score(s) over 3 session(s)" == detail
    monkeypatch.setattr(online, "online_quality", lambda db, ws: {"mean": None})
    assert alert_service.read_value(AlertRule(kind="online_quality"), None, ctx)[0] is None


# ── access: every route is ADMIN ─────────────────────────────────────────────────

ROUTES = [
    ("GET", "/api/costs"),
    ("GET", "/api/costs/month-to-date"),
    ("GET", "/api/alerts"),
    ("POST", "/api/alerts"),
    ("GET", "/api/alerts/scheduler"),
    ("PATCH", "/api/alerts/some-id"),
    ("DELETE", "/api/alerts/some-id"),
    ("POST", "/api/alerts/evaluate"),
]


@pytest.fixture
def auth_app(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN["password"])
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


def _member(app, username: str) -> tuple[str, TestClient]:
    password = "sufficient-pass"
    client = TestClient(app, client=PEER)
    registered = client.post(
        "/api/auth/register",
        json={"username": username, "email": f"{username}@acme-corp.com", "password": password},
    )
    assert registered.status_code == 201, registered.text
    db = SessionLocal()
    try:
        user = users_service.find_by_username(db, username)
        user.status = users_service.STATUS_ACTIVE
        user.expires_at = datetime.now(UTC) + timedelta(days=7)
        users_service.set_workspace_grants(db, user, [DEFAULT_WORKSPACE_ID])
        db.commit()
        user_id = user.id
    finally:
        db.close()
    login = client.post("/api/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200, login.text
    return user_id, client


def test_a_member_is_refused_every_route(auth_app):
    _, member = _member(auth_app, "mei")
    rule_id = _rule()
    for method, path in ROUTES:
        path = path.replace("some-id", rule_id)
        response = member.request(
            method, path, json={"kind": "error_rate", "threshold": 0.1}
            if method in ("POST", "PATCH") else None,
        )
        assert response.status_code == 403, (method, path, response.text)
        assert response.json()["code"] == "auth.forbidden"
    db = SessionLocal()
    try:
        assert db.get(AlertRule, rule_id) is not None  # the DELETE did nothing
    finally:
        db.close()


def test_an_admin_user_is_recorded_by_users_id(auth_app):
    user_id, client = _member(auth_app, "boss")
    db = SessionLocal()
    try:
        user = users_service.find_by_username(db, "boss")
        user.role = "admin"
        db.commit()
    finally:
        db.close()
    created = client.post("/api/alerts", json={"kind": "error_rate", "threshold": 0.2})
    assert created.status_code == 201, created.text
    assert created.json()["created_by"] == user_id
    assert created.json()["created_by_name"] == "boss"

    with TestClient(auth_app, client=PEER) as admin:
        assert admin.post("/api/auth/login", json=ADMIN).status_code == 200
        by_config_admin = admin.post(
            "/api/alerts", json={"kind": "error_rate", "threshold": 0.3}
        ).json()
    assert by_config_admin["created_by"] is None
    assert by_config_admin["created_by_name"] == ADMIN["username"]


# ── a console-v3-era ledger ──────────────────────────────────────────────────────


def test_a_branch_era_table_is_migrated_to_the_creator_convention(tmp_path):
    """console-v3 created `alert_rules` without `created_by_name`, with a username in
    `created_by`: it moves to the name, and the id becomes the matching `users.id`."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'v3.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE alert_rules DROP COLUMN created_by_name"))
        conn.execute(sa.text(
            "INSERT INTO users (id, username, username_key, email, password_hash, role, "
            "status, created_by, login_count, created_at, updated_at) VALUES "
            "('u-alice', 'alice', 'alice', 'alice@acme-corp.com', 'x', 'admin', 'active', "
            "'self', 0, '2026-10-01', '2026-10-01')"
        ))
        for rid, who in (("r1", "alice"), ("r2", "admin")):
            conn.execute(sa.text(
                "INSERT INTO alert_rules (id, workspace_id, kind, name, comparison, threshold, "
                "\"window\", enabled, state, created_by, created_at, updated_at) VALUES "
                f"('{rid}', 'default', 'error_rate', '', 'above', 0.1, '24h', 1, 'unknown', "
                f"'{who}', '2026-10-01', '2026-10-01')"
            ))
    db_module._migrate(engine)
    db_module._migrate(engine)  # a second run is a no-op
    assert_no_schema_drift(engine)
    with engine.begin() as conn:
        rows = dict(
            (row[0], (row[1], row[2]))
            for row in conn.execute(
                sa.text("SELECT id, created_by, created_by_name FROM alert_rules")
            )
        )
    assert rows == {"r1": ("u-alice", "alice"), "r2": (None, "admin")}


def _delete_rule(rule_id: str) -> None:
    """Delete a rule from another session — the console's DELETE during a pass."""
    other = SessionLocal()
    try:
        other.delete(other.get(AlertRule, rule_id))
        other.commit()
    finally:
        other.close()


def test_a_rule_deleted_while_its_value_is_read_is_skipped(monkeypatch, ctx):
    """A re-check runs for tens of seconds; deleting a rule meanwhile used to fail the
    whole pass with a stale UPDATE (500) and drop every other rule's result."""
    gone = _rule(name="gone", threshold=0.1, webhook_url="https://hook.example/x")
    kept = _rule(name="kept", threshold=0.1)
    sent = _capture_posts(monkeypatch)

    def read(rule, db, ws):
        if rule.id == gone:
            _delete_rule(gone)
            return 0.9, "breach"  # would fire and notify, had the rule survived
        return 0.01, "fine"

    monkeypatch.setattr(alert_service, "read_value", read)
    db = SessionLocal()
    try:
        result = alert_service.evaluate_rules(db, ctx, notify_transitions=True)
    finally:
        db.close()
    assert [row["id"] for row in result["rules"]] == [kept]
    assert result["rules"][0]["state"] == "ok"
    assert sent == []  # a deleted rule is never notified
    db = SessionLocal()
    try:
        assert db.get(AlertRule, gone) is None
        assert db.get(AlertRule, kept).state == "ok"
    finally:
        db.close()


def test_a_rule_deleted_between_the_check_and_the_write_does_not_fail_the_pass(
    monkeypatch, ctx
):
    gone = _rule(name="gone", threshold=0.1)
    kept = _rule(name="kept", threshold=0.1)

    def read(rule, db, ws):
        if rule.id == gone:
            _delete_rule(gone)
        return 0.01, "fine"

    monkeypatch.setattr(alert_service, "read_value", read)
    # the existence check races the DELETE and still sees the row
    monkeypatch.setattr(alert_service, "_still_exists", lambda db, rule_id: True)
    db = SessionLocal()
    try:
        result = alert_service.evaluate_rules(db, ctx)
    finally:
        db.close()
    assert [row["id"] for row in result["rules"]] == [kept]
    db = SessionLocal()
    try:
        assert db.get(AlertRule, kept).last_checked_at is not None
    finally:
        db.close()
