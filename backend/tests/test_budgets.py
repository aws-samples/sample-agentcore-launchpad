"""Per-user spend budgets (core): levels, precedence, the ledger, prices, the gate,
the in-band meters and My usage.

Shapes come from the live probe (task research/live-probe.md, dev, 2026-10-15):
Harness sends one ``metadata`` usage event per MODEL CALL (a one-tool turn sent
two), and the Claude Agent SDK ``complete.usage`` uses Anthropic field names.
"""

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
import yaml
from fastapi.testclient import TestClient

import app.routers.public_api as public_api
import app.services.chat as chat_service
from app.core import config as config_module
from app.core.billing_policy import BILLING, Billable
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.core.route_policy import ADMIN as ROLE_ADMIN
from app.core.route_policy import ROUTE_POLICY
from app.main import create_app
from app.models.budgets import BudgetLevel, UsageEvent, UsageSession
from app.models.ledger import Agent, ApiKey, User
from app.routers.apikeys import hash_key
from app.routers.auth import Identity
from app.services import budgets, rates, usage_meter
from app.services import users as users_service
from app.services.agentcore import harness as hc
from app.services.agentcore import runtime as rt
from app.services.usage_meter import UsageMeter, normalize_usage

ADMIN = {"username": "operator", "password": "s3cret-pass"}
PEER = ("127.0.0.1", 4321)
SONNET = "global.anthropic.claude-sonnet-5"  # $3 / $15 per 1M (built-in "sonnet-5")
UNPRICED = "acme.unpriced-model-v1"

# research/live-probe.md: Harness hr-database, one tool use → two metadata events
HARNESS_METADATA = [
    {"usage": {"inputTokens": 5859, "outputTokens": 32, "totalTokens": 5891},
     "metrics": {"latencyMs": 1234}},
    {"usage": {"inputTokens": 5982, "outputTokens": 41, "totalTokens": 6023},
     "metrics": {"latencyMs": 1341}},
]
HARNESS_TURN_USD = (11841 * 3 + 73 * 15) / 1e6
# research/live-probe.md: Claude Agent SDK container cc-knowledge, complete.usage
CLAUDE_SDK_USAGE = {
    "input_tokens": 2, "cache_creation_input_tokens": 22129, "cache_read_input_tokens": 0,
    "output_tokens": 4,
    "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
    "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 22129},
    "duration_ms": 2067, "num_turns": 1, "total_cost_usd": 0.08485575,
}
# 22129 × $3.75 (cache write = 1.25 × input) + 2 × $3 + 4 × $15, per 1M
CLAUDE_SDK_USD = (22129 * 3.75 + 2 * 3 + 4 * 15) / 1e6


# ── helpers ──────────────────────────────────────────────────────────────────


def _user(username="mia", role="member", level_id=None, override=None) -> str:
    db = SessionLocal()
    try:
        user = User(
            username=username, username_key=username.lower(),
            email=f"{username}@acme-corp.com", password_hash="x", role=role,
            status="active", budget_level_id=level_id, budget_override_usd=override,
        )
        db.add(user)
        db.commit()
        return user.id
    finally:
        db.close()


def _level(name="basic", usd=10.0, default=False) -> str:
    db = SessionLocal()
    try:
        level = BudgetLevel(name=name, monthly_budget_usd=usd, is_default=default)
        db.add(level)
        db.commit()
        return level.id
    finally:
        db.close()


def _spent(user_id, usd, *, period=None, source="chat", superseded_by=None,
           estimated=False, user_name=None) -> str:
    db = SessionLocal()
    try:
        row = UsageEvent(user_id=user_id, user_name=user_name, period=period or
                         budgets.period_of(), source=source, usd=usd,
                         superseded_by=superseded_by, estimated=estimated)
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
        rows = query.order_by(UsageEvent.created_at).all()
        for row in rows:
            db.expunge(row)
        return rows
    finally:
        db.close()


def _payer(user_id) -> budgets.Payer:
    db = SessionLocal()
    try:
        return budgets.payer_for_user_id(db, user_id)
    finally:
        db.close()


def _agent(method="harness", model=SONNET, name="budget-agent", arn=None) -> str:
    db = SessionLocal()
    try:
        spec = {"name": name}
        if model is not None:
            spec["model_id"] = model
        agent = Agent(
            workspace_id=DEFAULT_WORKSPACE_ID, name=name, method=method, status="active",
            arn=arn or ("arn:aws:bedrock-agentcore:us-west-2:111122223333:harness/h-1"
                        if method == "harness"
                        else "arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/r-1"),
            spec=spec,
        )
        db.add(agent)
        db.commit()
        return agent.id
    finally:
        db.close()


def _load_agent(agent_id) -> Agent:
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        db.expunge(agent)
        return agent
    finally:
        db.close()


# ── the period ───────────────────────────────────────────────────────────────


def test_period_is_the_utc_calendar_month():
    assert budgets.period_of(datetime(2026, 10, 31, 23, 59, tzinfo=UTC)) == "2026-10"
    # 01:00 on Nov 1st in UTC+8 is still October in UTC
    east = datetime(2026, 11, 1, 1, 0, tzinfo=timezone(timedelta(hours=8)))
    assert budgets.period_of(east) == "2026-10"
    assert budgets.resets_at("2026-10") == "2026-11-01T00:00:00+00:00"
    assert budgets.resets_at("2026-12") == "2027-01-01T00:00:00+00:00"
    with pytest.raises(AppError):
        budgets.validate_period("2026-13")


# ── precedence and exemptions ────────────────────────────────────────────────


def test_nothing_is_enforced_until_a_level_exists():
    member = _user()
    payer = _payer(member)
    assert payer.limit_usd is None and payer.basis == budgets.BASIS_UNLIMITED
    db = SessionLocal()
    try:
        _spent(member, 1000.0)
        budgets.check(db, payer, model_id=UNPRICED)  # unlimited: never refused
    finally:
        db.close()


def test_member_precedence_override_then_level_then_default():
    default = _level("default", 5.0, default=True)
    gold = _level("gold", 50.0)
    plain = _user("plain")
    levelled = _user("levelled", level_id=gold)
    overridden = _user("overridden", level_id=gold, override=7.5)
    only_override = _user("only-override", override=2.0)
    assert (_payer(plain).limit_usd, _payer(plain).basis, _payer(plain).level_id) == (
        5.0, budgets.BASIS_DEFAULT, default)
    assert (_payer(levelled).limit_usd, _payer(levelled).basis) == (50.0, budgets.BASIS_LEVEL)
    payer = _payer(overridden)
    assert (payer.limit_usd, payer.basis, payer.level_name) == (
        7.5, budgets.BASIS_OVERRIDE, "gold")
    assert _payer(only_override).limit_usd == 2.0


def test_admins_are_exempt_until_assigned():
    _level("default", 5.0, default=True)
    gold = _level("gold", 50.0)
    admin = _user("boss", role="admin")
    assert _payer(admin).limit_usd is None
    assert _payer(admin).basis == budgets.BASIS_EXEMPT_ADMIN  # the default does not apply
    levelled_admin = _user("boss2", role="admin", level_id=gold)
    assert _payer(levelled_admin).limit_usd == 50.0
    assert _payer(_user("boss3", role="admin", override=1.0)).limit_usd == 1.0


def test_principals_without_an_account_are_exempt():
    _level("default", 0.0, default=True)  # a $0 default would refuse every member
    db = SessionLocal()
    try:
        config_admin = budgets.payer_for_identity(db, Identity(username="admin", role="admin"))
        assert config_admin.user_id is None and config_admin.limit_usd is None
        assert config_admin.basis == budgets.BASIS_EXEMPT_PRINCIPAL
        legacy = ApiKey(workspace_id=DEFAULT_WORKSPACE_ID, name="k", prefix="p",
                        key_hash="h" * 64, created_by=None)
        assert budgets.payer_for_api_key(db, legacy).limit_usd is None
        member = _user("keyholder")
        keyed = ApiKey(workspace_id=DEFAULT_WORKSPACE_ID, name="k2", prefix="p",
                       key_hash="g" * 64, created_by=member, created_by_name="keyholder")
        payer = budgets.payer_for_api_key(db, keyed)
        assert payer.user_id == member and payer.limit_usd == 0.0
    finally:
        db.close()


# ── spend and the check ──────────────────────────────────────────────────────


def test_spend_is_the_sum_of_live_rows_in_the_period():
    member, other = _user("a"), _user("b")
    _spent(member, 1.0)
    _spent(member, 2.0, estimated=True)
    estimate = _spent(member, 5.0)
    measured = _spent(member, 0.5)
    db = SessionLocal()
    try:
        db.get(UsageEvent, estimate).superseded_by = measured  # reconciled: replaced
        db.commit()
    finally:
        db.close()
    _spent(member, 7.0, period="2026-01")
    _spent(other, 9.0)
    _spent(member, None)  # an unpriced row adds nothing
    db = SessionLocal()
    try:
        assert budgets.spend(db, member, budgets.period_of()) == pytest.approx(3.5)
        assert budgets.spend(db, member, "2026-01") == pytest.approx(7.0)
        assert budgets.spend(db, None, budgets.period_of()) == 0.0
    finally:
        db.close()


def test_check_refuses_at_the_limit_with_the_402_contract():
    level = _level("small", 2.0)
    member = _user(level_id=level)
    _spent(member, 1.99)
    db = SessionLocal()
    try:
        budgets.check(db, _payer(member))  # under: allowed
        _spent(member, 0.01)
        with pytest.raises(AppError) as refused:
            budgets.check(db, _payer(member))
    finally:
        db.close()
    err = refused.value
    assert err.status_code == 402 and err.code == "budget.exceeded"
    period = budgets.period_of()
    assert err.detail == {
        "used_usd": pytest.approx(2.0), "limit_usd": 2.0, "period": period,
        "resets_at": budgets.resets_at(period), "level": "small",
    }


def test_unpriced_models_are_refused_for_budgeted_payers_only():
    level = _level("small", 2.0)
    budgeted, unlimited = _user("budgeted", level_id=level), _user("free")
    db = SessionLocal()
    try:
        with pytest.raises(AppError) as refused:
            budgets.check(db, _payer(budgeted), model_id=UNPRICED)
        assert refused.value.status_code == 402
        assert refused.value.code == "budget.model_unpriced"
        assert refused.value.detail == {"model_id": UNPRICED}
        budgets.check(db, _payer(budgeted), model_id=SONNET)  # priced: allowed
        budgets.check(db, _payer(unlimited), model_id=UNPRICED)  # never refused
    finally:
        db.close()


def test_an_imported_harness_with_no_model_is_refused_but_an_opaque_runtime_is_not():
    level = _level("small", 2.0)
    payer = _payer(_user(level_id=level))
    discovered = _load_agent(_agent(method="discovered_runtime", model=None, name="imported"))
    discovered.spec = {"discovery": {"resource_type": "harness"}}
    runtime = _load_agent(_agent(method="zip_runtime", model=None, name="opaque"))
    db = SessionLocal()
    try:
        with pytest.raises(AppError) as refused:
            budgets.check_agent(db, payer, discovered)
        assert refused.value.code == "budget.model_unpriced"
        budgets.check_agent(db, payer, runtime)  # charged the flat estimate instead
    finally:
        db.close()


# ── prices ───────────────────────────────────────────────────────────────────


def test_price_tokens_uses_the_map_and_the_cache_defaults():
    usd, priced = rates.price_tokens(SONNET, 2, 4, 0, 22129)
    assert priced and usd == pytest.approx(CLAUDE_SDK_USD)
    # explicit cache rates win over the factors (gpt-6-sol: read 0.20, write 2.50)
    usd, _ = rates.price_tokens("global.openai.gpt-6-sol", 1e6, 0, 1e6, 1e6)
    assert usd == pytest.approx(2.0 + 0.20 + 2.50)
    assert rates.price_tokens(UNPRICED, 100, 100) == (None, False)
    assert rates.is_unpriced(UNPRICED) and not rates.is_unpriced(SONNET)
    assert not rates.is_unpriced(None)


def test_price_overrides_win_and_survive_cache_clear():
    db = SessionLocal()
    try:
        rates.set_price_override(db, UNPRICED, {"input": 1.0, "output": 2.0}, "admin")
        rates.set_price_override(db, "sonnet-5", {"input": 30.0, "output": 150.0}, "admin")
    finally:
        db.close()
    for _ in range(2):
        assert rates.price_tokens(UNPRICED, 1e6, 1e6) == (pytest.approx(3.0), True)
        assert rates.price_tokens(SONNET, 1e6, 0)[0] == pytest.approx(30.0)
        get_settings.cache_clear()  # the price refresher and resource writes do this
    # the Spend page reads the same merged map
    from app.services.observability import estimate_cost

    assert estimate_cost(UNPRICED, 1e6, 0) == pytest.approx(1.0)
    # the base map never carries an override: a reset returns the built-in
    assert rates.base_model_prices()["sonnet-5"]["input"] == 3.0
    assert UNPRICED not in rates.base_model_prices()
    db = SessionLocal()
    try:
        entry = rates.clear_price_override(db, "sonnet-5", "admin")
        assert entry["rates"]["input"] == 3.0 and not entry["overridden"]
        rates.clear_price_override(db, UNPRICED, "admin")
    finally:
        db.close()
    assert rates.is_unpriced(UNPRICED)


def test_the_price_refresher_never_persists_an_override(tmp_path, monkeypatch):
    from app.services import model_prices as mp
    from tests.test_model_prices import SOURCE, FakeCWModels

    cfg = tmp_path / "launchpad.yaml"
    cfg.write_text(yaml.safe_dump({"region": "us-west-2"}), encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_FILE", cfg)
    get_settings.cache_clear()
    db = SessionLocal()
    try:
        rates.set_price_override(db, "acme.only-override", {"input": 4, "output": 8}, "admin")
    finally:
        db.close()
    monkeypatch.setattr(mp, "_span_models", lambda _ws, logs=None, hours=0: [])
    mp.refresh_model_prices(cw=FakeCWModels([]), fetch=lambda url: SOURCE)
    on_disk = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert "acme.only-override" not in on_disk["model_prices"]
    # … and it is applied again on the rebuilt settings
    assert get_settings().model_prices["acme.only-override"]["input"] == 4.0
    get_settings.cache_clear()


def test_agentcore_rates_are_seeded_and_editable():
    db = SessionLocal()
    try:
        view = rates.agentcore_rates_view(db)
        assert view["rates"]["runtime_v1_gb_hour"] == 0.00945
        assert view["rates"]["evaluation_custom"] == 0.0015
        assert view["calibration"]["runtime_idle_timeout_seconds"] == 900
        changed = rates.set_agentcore_rates(
            db, rates={"web_search_query": 0.008}, calibration_values={"runtime_harness_gb": 6},
            actor="admin",
        )
        assert changed["rates"]["web_search_query"] == 0.008
        assert changed["calibration"]["runtime_harness_gb"] == 6.0
        reset = rates.set_agentcore_rates(db, rates={"web_search_query": None},
                                          calibration_values=None, actor="admin")
        assert reset["rates"]["web_search_query"] == 0.007
        with pytest.raises(AppError):
            rates.set_agentcore_rates(db, rates={"typo_rate": 1}, calibration_values=None,
                                      actor="admin")
        with pytest.raises(AppError):
            rates.set_agentcore_rates(db, rates={"kb_query": -1}, calibration_values=None,
                                      actor="admin")
    finally:
        db.close()


# ── the meters ───────────────────────────────────────────────────────────────


def _meter(user_id="u-1", *, opaque=False, model=SONNET, source="chat", session="s" * 64):
    return UsageMeter(user_id=user_id, user_name="mia", source=source,
                      origin_workspace_id=DEFAULT_WORKSPACE_ID, agent_id="a-1",
                      session_id=session, model_id=model, opaque=opaque)


def _harness_events(metadata=HARNESS_METADATA):
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"contentBlockIndex": 0,
                               "start": {"toolUse": {"toolUseId": "t1", "name": "lookup"}}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "tool_use"}},
        {"metadata": metadata[0]},
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Annual leave: 15"}}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": metadata[1]},
    ]


def test_normalize_reads_both_namings_and_treats_missing_keys_as_zero():
    bedrock = normalize_usage(HARNESS_METADATA[0]["usage"])
    assert (bedrock.tokens_in, bedrock.tokens_out, bedrock.cache_read, bedrock.cache_write) == (
        5859, 32, 0, 0)
    anthropic = normalize_usage(CLAUDE_SDK_USAGE)
    assert (anthropic.tokens_in, anthropic.tokens_out, anthropic.cache_read,
            anthropic.cache_write) == (2, 4, 0, 22129)
    assert normalize_usage({"latencyMs": 3}) is None
    assert normalize_usage("nope") is None


def test_harness_meter_sums_every_model_call_into_one_turn_row():
    meter = _meter()
    events = list(hc.iter_harness_stream(iter(_harness_events()), on_usage=meter.on_usage))
    assert sum("metadata" in e for e in events) == 2  # still yielded to the caller
    row_id = meter.settle()
    [row] = _rows()
    assert row.id == row_id and row.kind == "tokens" and not row.estimated
    assert (row.tokens_in, row.tokens_out) == (5859 + 5982, 32 + 41)
    assert row.usd == pytest.approx(HARNESS_TURN_USD)
    assert row.detail == {"model_calls": 2}
    assert row.user_id == "u-1" and row.period == budgets.period_of()


def test_harness_usage_after_a_failure_is_still_charged():
    meter = _meter()
    events = _harness_events()
    events[-2] = {"messageStop": {"stopReason": "max_tokens"}}  # a limit stop
    with pytest.raises(AppError):
        list(hc.iter_harness_stream(iter(events), on_usage=meter.on_usage))
    meter.settle(failed=True)
    [row] = _rows()
    assert row.tokens_in == 11841 and row.detail["failed"] is True


def test_claude_sdk_complete_usage_is_kept_and_priced_with_the_map():
    meter = _meter(opaque=True)
    payload = {"event": "complete", "result": "hi", "usage": CLAUDE_SDK_USAGE}
    events = list(rt._runtime_payload_events(payload, meter.on_usage))
    assert events == [{"event": "complete", "data": {"text": "hi"}}]
    meter.settle()
    [row] = _rows()
    assert (row.tokens_in, row.tokens_out, row.cache_read, row.cache_write) == (2, 4, 0, 22129)
    assert row.usd == pytest.approx(CLAUDE_SDK_USD)  # not the CLI's total_cost_usd
    assert row.detail["cli_total_cost_usd"] == pytest.approx(0.08485575)


def test_runtime_converse_metadata_is_one_call_and_a_turn_aggregate_wins():
    meter = _meter(opaque=True)
    for payload in (
        {"metadata": {"usage": {"inputTokens": 100, "outputTokens": 10}, "metrics": {}}},
        {"event": {"metadata": {"usage": {"inputTokens": 200, "outputTokens": 20},
                                "metrics": {"latencyMs": 1}}}},
    ):
        assert list(rt._runtime_payload_events(payload, meter.on_usage)) == []
    meter.settle()
    [row] = _rows()
    assert (row.tokens_in, row.tokens_out) == (300, 30)
    # when an agent reports both per-call events and a whole-turn aggregate, the
    # aggregate is the turn (adding both would count the same tokens twice)
    meter.on_usage({"inputTokens": 300, "outputTokens": 30})
    meter.on_usage(CLAUDE_SDK_USAGE, "turn")
    meter.settle()
    second = _rows()[-1]
    assert second.tokens_in == 2 and second.detail["model_calls_ignored"] == 1


def test_an_opaque_turn_without_usage_is_charged_the_estimate():
    meter = _meter(opaque=True)
    meter.settle()
    [row] = _rows()
    assert row.kind == "turn_estimate" and row.estimated and row.usd == 0.01
    assert (row.quantity, row.unit) == (1.0, "turn")
    db = SessionLocal()
    try:
        budgets.set_budget_settings(db, {"opaque_turn_estimate_usd": 0.05}, "admin")
    finally:
        db.close()
    meter.settle()
    assert _rows()[-1].usd == 0.05
    meter.settle(failed=True)  # a failed silent turn records nothing
    assert len(_rows()) == 2
    harness = _meter(opaque=False)
    harness.settle()  # a Harness turn with no metadata is not estimated
    assert len(_rows()) == 2


def test_usage_sessions_are_upserted_and_keep_the_first_payer():
    _meter("u-1").settle()
    second = _meter("u-2")
    second.on_usage({"inputTokens": 1, "outputTokens": 1})
    second.settle()
    db = SessionLocal()
    try:
        [row] = db.query(UsageSession).all()
        assert (row.user_id, row.turns, row.source) == ("u-1", 2, "chat")
        assert row.origin_workspace_id == DEFAULT_WORKSPACE_ID and row.agent_id == "a-1"
    finally:
        db.close()


def test_evaluate_token_usage_is_exact_and_never_a_payer_session():
    meter = UsageMeter(user_id="u-1", user_name="mia", source="observability",
                       origin_workspace_id=DEFAULT_WORKSPACE_ID)
    meter.evaluate_usage("Builtin.Helpfulness",
                         {"inputTokens": 1000, "outputTokens": 100, "totalTokens": 1100},
                         evaluated_session="judged-session")
    meter.evaluate_usage("Builtin.Correctness", None)  # an error row: nothing billed
    meter.evaluate_usage("my-judge-abc", {"inputTokens": 1e6, "outputTokens": 0})
    builtin, custom = _rows()
    assert builtin.kind == "evaluation_builtin" and not builtin.estimated
    assert builtin.usd == pytest.approx(1 * 0.0024 + 0.1 * 0.012)
    assert (builtin.ref_type, builtin.ref_id, builtin.session_id) == (
        "session", "judged-session", None)
    # custom: the per-evaluation fee + the judge's tokens at the platform judge model
    judge_usd, _ = rates.price_tokens(get_settings().judge_model_id, 1e6, 0)
    assert custom.kind == "evaluation_custom" and custom.estimated
    assert custom.usd == pytest.approx(0.0015 + judge_usd)
    db = SessionLocal()
    try:
        assert db.query(UsageSession).count() == 0
    finally:
        db.close()


def test_observability_evaluate_meters_each_result(monkeypatch):
    from app.services import observability as obs
    from tests.conftest import ws_ctx
    from tests.test_observability import SCORE_SESSION, SCORE_SPANS, FakeDataPlane, _score_logs

    meter = UsageMeter(user_id="u-1", user_name="mia", source="observability")
    rows = [{"@message": json.dumps(s)} for s in SCORE_SPANS]
    obs.evaluate_session(SCORE_SESSION, "24h", ["Builtin.Helpfulness"], ws_ctx(),
                         logs=_score_logs(rows), data=FakeDataPlane(), meter=meter)
    [row] = _rows()
    assert (row.tokens_in, row.tokens_out) == (1200, 80)  # the canned tokenUsage
    assert row.source == "observability" and row.ref_id == SCORE_SESSION


def test_a_meter_that_cannot_write_never_raises(monkeypatch):
    def broken():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(usage_meter, "SessionLocal", broken)
    meter = _meter(opaque=True)
    meter.on_usage({"inputTokens": 1, "outputTokens": 1})
    assert meter.settle() is None
    assert meter.turn_estimate() is None
    assert meter.evaluate_usage("Builtin.X", {"inputTokens": 1}) is None
    meter.on_usage(object())  # garbage in: ignored


# ── HTTP: the gate, the payer, My usage, the admin routes ────────────────────


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


def _member(app, username: str, role: str = "member") -> tuple[str, TestClient]:
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
        user.role = role
        user.expires_at = datetime.now(UTC) + timedelta(days=7)
        users_service.set_workspace_grants(db, user, [DEFAULT_WORKSPACE_ID])
        db.commit()
        user_id = user.id
    finally:
        db.close()
    login = client.post("/api/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200, login.text
    return user_id, client


def _assign(user_id, level_id=None, override=None) -> None:
    db = SessionLocal()
    try:
        budgets.assign_user(db, user_id, level_id=level_id, override_usd=override)
    finally:
        db.close()


def _concrete(path_format: str) -> str:
    return "/".join("budget-probe" if seg.startswith("{") else seg
                    for seg in path_format.split("/"))


# every console route the dependency gates (the action dispatchers gate per action,
# the admin-only probe answers a member 403 first)
GATED_CONSOLE_ROUTES = sorted(
    key for key, row in BILLING.items()
    if isinstance(row, Billable) and row.actions is None and key[1].startswith("/api/")
    and ROUTE_POLICY.get(key) != ROLE_ADMIN
)


@pytest.mark.parametrize(("method", "path_format"), GATED_CONSOLE_ROUTES)
def test_every_billable_route_refuses_a_member_at_the_limit(app, method, path_format):
    member, client = _member(app, "spender")
    _assign(member, _level("tiny", 1.0))
    _spent(member, 1.0)
    response = client.request(method, _concrete(path_format), json={})
    assert response.status_code == 402, (path_format, response.text)
    body = response.json()
    assert body["code"] == "budget.exceeded"
    assert body["detail"]["limit_usd"] == 1.0 and body["detail"]["used_usd"] == 1.0
    assert body["detail"]["period"] == budgets.period_of()


def test_the_default_level_catches_members_without_one(app):
    member, client = _member(app, "defaulted")
    _level("everyone", 0.5, default=True)
    _spent(member, 0.5)
    response = client.post(f"/api/chat/{_agent()}", json={"prompt": "hi"})
    assert response.status_code == 402 and response.json()["code"] == "budget.exceeded"


def test_exempt_and_unlimited_callers_are_never_refused(app, admin, monkeypatch):
    _level("everyone", 0.0, default=True)
    agent_id = _agent(method="zip_runtime", model=UNPRICED)
    monkeypatch.setattr(chat_service, "invoke_agent_events",
                        lambda *a, **k: iter([{"event": "delta", "data": {"text": "ok"}}]))
    # the config admin (no users row) and a registered admin without a level
    _spent(None, 999.0, user_name="operator")
    assert admin.post(f"/api/chat/{agent_id}", json={"prompt": "hi"}).status_code == 200
    boss, boss_client = _member(app, "boss", role="admin")
    _spent(boss, 999.0)
    assert boss_client.post(f"/api/chat/{agent_id}", json={"prompt": "hi"}).status_code == 200
    # a member with no level and no default is unlimited
    db = SessionLocal()
    try:
        db.query(BudgetLevel).delete()
        db.commit()
    finally:
        db.close()
    member, member_client = _member(app, "unlimited")
    _spent(member, 999.0)
    response = member_client.post(f"/api/chat/{agent_id}", json={"prompt": "hi"})
    assert response.status_code == 200 and "event: done" in response.text


def test_an_unpriced_agent_model_refuses_a_budgeted_member(app):
    member, client = _member(app, "pricey")
    _assign(member, _level("plenty", 100.0))
    response = client.post(f"/api/chat/{_agent(model=UNPRICED)}", json={"prompt": "hi"})
    assert response.status_code == 402
    assert response.json() == {
        "code": "budget.model_unpriced",
        "message": response.json()["message"],
        "detail": {"model_id": UNPRICED},
    }
    assert UNPRICED in response.json()["message"]


def test_a_chat_turn_is_charged_to_the_callers_user_id(app, monkeypatch):
    member, client = _member(app, "chatter")
    agent_id = _agent(method="harness")

    class FakeData:
        def invoke_harness(self, **_kwargs):
            return {"stream": iter(_harness_events())}

    monkeypatch.setattr(chat_service, "data_client", lambda _ws: FakeData())
    response = client.post(f"/api/chat/{agent_id}", json={"prompt": "leave?"})
    assert response.status_code == 200 and "event: done" in response.text
    [row] = _rows(user_id=member)
    assert row.user_name == "chatter" and row.source == "chat" and row.agent_id == agent_id
    assert row.usd == pytest.approx(HARNESS_TURN_USD) and row.model_id == SONNET
    db = SessionLocal()
    try:
        session = db.query(UsageSession).one()
        assert session.user_id == member and session.session_id == row.session_id
    finally:
        db.close()
    # and it shows on the caller's own usage
    usage = client.get("/api/me/usage").json()
    assert usage["used_usd"] == pytest.approx(HARNESS_TURN_USD)
    assert usage["by_source"][0]["source"] == "chat" and usage["unlimited"] is True


def test_a_metering_failure_never_breaks_a_chat_turn(app, monkeypatch):
    _, client = _member(app, "resilient")
    agent_id = _agent(method="zip_runtime")

    def broken():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(usage_meter, "SessionLocal", broken)
    monkeypatch.setattr(chat_service, "invoke_agent_events",
                        lambda *a, **k: iter([{"event": "delta", "data": {"text": "ok"}}]))
    response = client.post(f"/api/chat/{agent_id}", json={"prompt": "hi"})
    assert response.status_code == 200
    assert "event: done" in response.text and "event: error" not in response.text


def _key(created_by=None, name=None, raw="lp_live_budgetkey00") -> str:
    db = SessionLocal()
    try:
        db.add(ApiKey(workspace_id=DEFAULT_WORKSPACE_ID, name="k", prefix="lp_live_bud…",
                      key_hash=hash_key(raw), created_by=created_by, created_by_name=name))
        db.commit()
    finally:
        db.close()
    return raw


def test_v1_is_charged_to_the_key_creator_and_refused_at_their_limit(app, monkeypatch):
    member, _ = _member(app, "integrator")
    raw = _key(member, "integrator")
    agent_id = _agent(method="zip_runtime")

    def fake_invoke(agent, prompt, **kwargs):
        kwargs["on_usage"]({"inputTokens": 1000, "outputTokens": 100})
        return {"text": "ok", "session_id": "v" * 64}

    monkeypatch.setattr(public_api, "invoke_agent_text", fake_invoke)
    with TestClient(app, client=PEER) as client:
        ok = client.post(f"/v1/agents/{agent_id}/invoke", json={"prompt": "hi"},
                         headers={"X-Api-Key": raw})
        assert ok.status_code == 200, ok.text
        [row] = _rows(user_id=member)
        assert row.source == "v1" and (row.ref_type, row.tokens_in) == ("api_key", 1000)
        db = SessionLocal()
        try:  # the session ledger /v1 never had
            assert db.get(UsageSession, ("v" * 64, DEFAULT_WORKSPACE_ID)).user_id == member
        finally:
            db.close()
        _assign(member, _level("tiny", 0.001))
        for path in (f"/v1/agents/{agent_id}/invoke", f"/v1/agents/{agent_id}/invoke-stream"):
            refused = client.post(path, json={"prompt": "hi"}, headers={"X-Api-Key": raw})
            assert refused.status_code == 402, path
            assert refused.json()["code"] == "budget.exceeded"


def test_a_v1_key_with_no_creator_is_exempt(app, monkeypatch):
    _level("everyone", 0.0, default=True)
    raw = _key(None, None, raw="lp_live_legacy0000")
    agent_id = _agent(method="zip_runtime")
    monkeypatch.setattr(public_api, "invoke_agent_text",
                        lambda *a, **k: {"text": "ok", "session_id": "w" * 64})
    with TestClient(app, client=PEER) as client:
        response = client.post(f"/v1/agents/{agent_id}/invoke", json={"prompt": "hi"},
                               headers={"X-Api-Key": raw})
    assert response.status_code == 200, response.text
    [row] = _rows()
    assert row.user_id is None and row.kind == "turn_estimate"  # opaque, nothing reported


def test_my_usage_shows_only_the_callers_rows(app):
    alice, alice_client = _member(app, "alice")
    bob, _ = _member(app, "bob")
    _assign(alice, _level("basic", 10.0))
    _spent(alice, 2.0, source="chat")
    _spent(alice, 1.0, source="eval", estimated=True)
    _spent(bob, 50.0, source="chat")
    usage = alice_client.get("/api/me/usage").json()
    assert usage["user_id"] == alice and usage["level"]["name"] == "basic"
    assert (usage["used_usd"], usage["limit_usd"], usage["remaining_usd"]) == (3.0, 10.0, 7.0)
    assert usage["estimated_usd"] == 1.0 and usage["percent_used"] == 30.0
    assert [s["source"] for s in usage["by_source"]] == ["chat", "eval"]
    assert usage["period"] == budgets.period_of() and usage["unlimited"] is False
    assert usage["resets_at"] == budgets.resets_at(budgets.period_of())


ADMIN_BUDGET_ROUTES = [
    ("GET", "/api/budgets/levels"), ("POST", "/api/budgets/levels"),
    ("PATCH", "/api/budgets/levels/x"), ("DELETE", "/api/budgets/levels/x"),
    ("GET", "/api/budgets/people"), ("PUT", "/api/budgets/users/x"),
    ("GET", "/api/budgets/settings"), ("PUT", "/api/budgets/settings"),
    ("GET", "/api/admin/model-prices"), ("PUT", "/api/admin/model-prices/x.y"),
    ("DELETE", "/api/admin/model-prices/x.y"), ("GET", "/api/admin/agentcore-rates"),
    ("PUT", "/api/admin/agentcore-rates"),
]


def test_members_are_refused_every_admin_budget_route(app):
    _, client = _member(app, "curious")
    for method, path in ADMIN_BUDGET_ROUTES:
        response = client.request(method, path, json={})
        assert response.status_code == 403, (method, path)
        assert response.json()["code"] == "auth.forbidden"


def test_admin_level_crud_and_assignment(admin, app):
    created = admin.post("/api/budgets/levels",
                         json={"name": "Basic", "monthly_budget_usd": 5, "is_default": True})
    assert created.status_code == 201, created.text
    basic = created.json()
    assert basic["is_default"] and basic["user_count"] == 0
    assert admin.post("/api/budgets/levels",
                      json={"name": "basic", "monthly_budget_usd": 1}).status_code == 409
    pro = admin.post("/api/budgets/levels",
                     json={"name": "Pro", "monthly_budget_usd": 50, "is_default": True}).json()
    listing = admin.get("/api/budgets/levels").json()
    assert listing["default_level_id"] == pro["id"]  # at most one default
    assert [lv["is_default"] for lv in listing["levels"]].count(True) == 1
    patched = admin.patch(f"/api/budgets/levels/{basic['id']}",
                          json={"monthly_budget_usd": 7.5}).json()
    assert patched["monthly_budget_usd"] == 7.5 and patched["name"] == "Basic"
    assert admin.post("/api/budgets/levels",
                      json={"name": "bad", "monthly_budget_usd": -1}).status_code == 422

    member, _ = _member(app, "assignee")
    assigned = admin.put(f"/api/budgets/users/{member}",
                         json={"level_id": basic["id"], "override_usd": None}).json()
    assert assigned["limit_usd"] == 7.5 and assigned["basis"] == "level"
    in_use = admin.delete(f"/api/budgets/levels/{basic['id']}")
    assert in_use.status_code == 409 and in_use.json()["code"] == "budget.level_in_use"
    assert in_use.json()["detail"]["users"] == 1
    overridden = admin.put(f"/api/budgets/users/{member}",
                           json={"level_id": None, "override_usd": 3}).json()
    assert overridden["limit_usd"] == 3.0 and overridden["basis"] == "override"
    assert admin.delete(f"/api/budgets/levels/{basic['id']}").status_code == 200
    assert admin.put("/api/budgets/users/nobody",
                     json={"level_id": None, "override_usd": None}).status_code == 404
    assert admin.put(f"/api/budgets/users/{member}",
                     json={"level_id": "missing", "override_usd": None}).status_code == 404

    _spent(member, 2.0, source="chat")
    _spent(member, 0.5, source="eval", estimated=True)
    _spent(None, 4.0, user_name="operator")
    people = admin.get("/api/budgets/people").json()
    row = next(p for p in people["people"] if p["user_id"] == member)
    assert (row["limit_usd"], row["used_usd"], row["remaining_usd"]) == (3.0, 2.5, 0.5)
    assert row["top_source"] == "chat" and row["estimated_share"] == 0.2
    assert row["override_usd"] == 3.0 and row["basis"] == "override"
    [exempt] = people["unattributed"]  # the config admin's spend, no account
    assert (exempt["user_id"], exempt["user_name"], exempt["used_usd"]) == (
        None, "operator", 4.0)
    assert admin.get("/api/budgets/people?period=2026-13").status_code == 422


def test_admin_price_editor_and_settings(admin):
    _agent(model=UNPRICED)
    listing = admin.get("/api/admin/model-prices").json()
    assert {"model_id": UNPRICED, "seen_in": ["agents"]} in listing["unpriced_models"]
    assert listing["cache_factors"] == {"cache_read": 0.1, "cache_write": 1.25}
    sonnet = next(p for p in listing["prices"] if p["model_id"] == "sonnet-5")
    assert sonnet["rates"]["input"] == 3.0 and not sonnet["overridden"]

    model = "arn:aws:bedrock:us-west-2:111122223333:custom-model/acme.x:0"  # slashes, colon
    put = admin.put(f"/api/admin/model-prices/{model}", json={"input": 1, "output": 2})
    assert put.status_code == 200, put.text
    assert put.json()["overridden"] and put.json()["rates"]["output"] == 2.0
    assert admin.put(f"/api/admin/model-prices/{UNPRICED}",
                     json={"input": 0.5, "output": 1.5, "cache_read": 0.05}).status_code == 200
    listing = admin.get("/api/admin/model-prices").json()
    assert all(m["model_id"] != UNPRICED for m in listing["unpriced_models"])
    assert listing["prices"][0]["overridden"]  # overrides first
    assert admin.delete(f"/api/admin/model-prices/{model}").status_code == 200
    assert admin.delete(f"/api/admin/model-prices/{model}").status_code == 404
    assert admin.put("/api/admin/model-prices/x", json={"input": 1, "output": 1}).status_code \
        == 400  # too short to be a model id

    rates_view = admin.get("/api/admin/agentcore-rates").json()
    assert rates_view["units"]["runtime_v1_gb_hour"] == "GB-hour"
    changed = admin.put("/api/admin/agentcore-rates",
                        json={"rates": {"gateway_call": 0.00001}}).json()
    assert changed["rates"]["gateway_call"] == 0.00001 and changed["updated_by"] == "operator"
    assert admin.put("/api/admin/agentcore-rates",
                     json={"rates": {"nope": 1}}).status_code == 400

    settings = admin.get("/api/budgets/settings").json()
    assert settings["opaque_turn_estimate_usd"] == 0.01
    updated = admin.put("/api/budgets/settings", json={"opaque_turn_estimate_usd": 0.02}).json()
    assert updated["opaque_turn_estimate_usd"] == 0.02 and updated["defaults"] == {
        "opaque_turn_estimate_usd": 0.01}


def test_users_list_carries_the_assignment(admin, app):
    member, _ = _member(app, "listed")
    _assign(member, _level("basic", 5.0), override=2.0)
    item = next(u for u in admin.get("/api/users").json()["items"] if u["id"] == member)
    assert item["budget_override_usd"] == 2.0 and item["budget_level_id"]


def test_a_budgeted_admin_is_refused_the_probe(app):
    boss, client = _member(app, "budgeted-boss", role="admin")
    _assign(boss, _level("admins", 1.0))
    _spent(boss, 1.0)
    response = client.post("/api/admin/models/probe", json={"model_id": SONNET})
    assert response.status_code == 402


def test_assignments_survive_a_ledger_upgrade(tmp_path):
    """The two users columns come back on a ledger that predates them."""
    import sqlalchemy as sa

    from app.core import db as db_module
    from app.core.db import Base, schema_drift

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE users DROP COLUMN budget_level_id"))
        conn.execute(sa.text("ALTER TABLE users DROP COLUMN budget_override_usd"))
    assert schema_drift(engine) == {"users": ["budget_level_id", "budget_override_usd"]}
    db_module._migrate(engine)
    db_module._migrate(engine)  # idempotent
    assert schema_drift(engine) == {}
