"""Online evaluation cross-surface reads: session-detail scores + Overview tile.

Both read the results log groups through a prefix SOURCE query; these tests pin
the query shape, the owner grouping, the polarity-normalised tile maths, the
fail-soft contract on the session detail, and the endpoint cache.
"""

from unittest.mock import MagicMock

import pytest

import app.routers.overview as overview_mod
import app.services.observability as obs
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.evaluation import online
from app.evaluation.models import OnlineEvalConfig
from tests.conftest import ws_ctx
from tests.test_observability import _fake_logs

SID = "s" * 64
AGENT_CFG = "oe_hr_assistant_4341b9-0GfiAU9TtP"
EXP_CFG = "exp_canary_1-AbCdEf1234"
EXT_CFG = "ext-config-1"


@pytest.fixture(autouse=True)
def _fresh_caches():
    online.reset_names_cache()
    obs.reset_cache()
    overview_mod._cache.clear()
    yield
    online.reset_names_cache()
    obs.reset_cache()
    overview_mod._cache.clear()


def seed_config(db, config_id=AGENT_CFG, agent_id="a1", agent_name="hr-assistant",
                name="oe_hr_assistant_4341b9") -> OnlineEvalConfig:
    row = OnlineEvalConfig(
        workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id, agent_name=agent_name,
        config_id=config_id, config_arn=f"arn:aws:bedrock-agentcore:us-west-2:1:oec/{config_id}",
        name=name, service_name="svc.DEFAULT", log_group="/aws/bedrock-agentcore/runtimes/x",
    )
    db.add(row)
    db.commit()
    return row


def record(config_id, evaluator, score, *, label="Pass", t="2026-09-02 10:00:00.000"):
    return {"time": t, "config_id": config_id, "evaluator": evaluator, "level": "SESSION",
            "score": str(score), "label": label, "explanation": f"because {evaluator}",
            "trace_id": "t" * 32}


# ── AC1: session scores query + owner grouping ──────────────────────────────


def test_session_query_targets_the_results_prefix_and_encodes_the_session_id():
    q = online.session_scores_query(SID)
    assert q.lstrip().startswith("SOURCE logGroups(namePrefix: "
                                 "['/aws/bedrock-agentcore/evaluations/results/'])")
    assert f'attributes.session.id = "{SID}"' in q
    assert 'filter name = "gen_ai.evaluation.result"' in q
    # the quoting can't be broken out of — the id is JSON-encoded
    assert '"' + json_escape('x" or 1=1') + '"' in online.session_scores_query('x" or 1=1')


def json_escape(s: str) -> str:
    import json
    return json.dumps(s)[1:-1]


def test_parse_session_scores_groups_by_config_with_agent_first():
    db = SessionLocal()
    row = seed_config(db)
    ledger = {row.config_id: row}
    db.close()
    names = {EXP_CFG: "exp_canary_1", EXT_CFG: "someone_elses"}
    rows = [
        record(EXT_CFG, "Builtin.Helpfulness", 0.8),
        record(AGENT_CFG, "Builtin.Refusal", 0.0, label="No"),
        record(AGENT_CFG, "Builtin.Helpfulness", 0.9),
        record(EXP_CFG, "Builtin.Correctness", 0.5),
        {"time": "x", "config_id": "", "evaluator": "Builtin.Helpfulness"},  # dropped
    ]
    out = online.parse_session_scores(rows, ledger, names)
    assert out["total"] == 4 and out["unavailable"] is False
    assert [c["owner"] for c in out["configs"]] == ["agent", "experiment", "external"]
    agent = out["configs"][0]
    assert agent["config_name"] == "oe_hr_assistant_4341b9"
    assert agent["agent"] == {"id": "a1", "name": "hr-assistant"}
    assert [r["evaluator_id"] for r in agent["records"]] == [
        "Builtin.Refusal", "Builtin.Helpfulness"]
    assert agent["records"][0]["score"] == 0.0 and agent["records"][0]["label"] == "No"
    assert out["configs"][1]["config_name"] == "exp_canary_1"
    assert out["configs"][2]["agent"] is None


def test_session_online_scores_resolves_names_only_for_non_ledger_configs(monkeypatch):
    db = SessionLocal()
    seed_config(db)
    calls = []

    def run(queries, hours, **kw):
        calls.append((set(queries), hours))
        return {"scores": [record(AGENT_CFG, "Builtin.Helpfulness", 1.0)]}

    control = MagicMock()
    control.list_online_evaluation_configs.side_effect = AssertionError("not needed")
    out = online.session_online_scores(db, ws_ctx(), SID, 24, run_queries=run, control=control)
    assert calls == [({"scores"}, 24)]
    assert out["configs_exist"] is True and out["total"] == 1
    assert out["configs"][0]["owner"] == "agent"

    # an unknown config id → one List call, cached afterwards
    control.list_online_evaluation_configs.side_effect = None
    control.list_online_evaluation_configs.return_value = {
        "onlineEvaluationConfigs": [{"onlineEvaluationConfigId": EXP_CFG, "name": "exp_c"}]}

    def run2(queries, hours, **kw):
        return {"scores": [record(EXP_CFG, "Builtin.Helpfulness", 1.0)]}

    out = online.session_online_scores(db, ws_ctx(), SID, 24, run_queries=run2, control=control)
    out = online.session_online_scores(db, ws_ctx(), SID, 24, run_queries=run2, control=control)
    assert control.list_online_evaluation_configs.call_count == 1
    assert out["configs"][0]["owner"] == "experiment"
    assert out["configs"][0]["config_name"] == "exp_c"
    db.close()


# ── AC2: fail-soft on the session detail ───────────────────────────────────


def test_get_session_degrades_online_scores_without_losing_traces(monkeypatch):
    db = SessionLocal()
    seed_config(db)
    monkeypatch.setattr(obs, "session_transcript",
                        lambda *a, **k: {"available": False, "reason": "not_platform_session"})

    def boom(*a, **k):
        raise AppError("observability.query_failed", "Logs Insights start_query failed",
                       status_code=502)

    monkeypatch.setattr(online, "run_insights_queries", boom)
    result = obs.get_session(SID, "24h", db, ws_ctx(), logs=_fake_logs())
    assert len(result["traces"]) == 1  # AGG_ROW still rendered
    assert result["online_scores"] == {
        "configs": [], "total": 0, "unavailable": True, "configs_exist": True}
    db.close()


def test_get_session_carries_online_scores_when_the_query_works(monkeypatch):
    db = SessionLocal()
    monkeypatch.setattr(obs, "session_transcript",
                        lambda *a, **k: {"available": False, "reason": "not_platform_session"})
    monkeypatch.setattr(
        online, "run_insights_queries",
        lambda q, h, **kw: {"scores": [record(EXT_CFG, "Builtin.Helpfulness", 0.7)]})
    monkeypatch.setattr(online, "config_names", lambda *a, **k: {})
    result = obs.get_session(SID, "24h", db, ws_ctx(), logs=_fake_logs())
    scores = result["online_scores"]
    assert scores["configs_exist"] is False and scores["total"] == 1
    assert scores["configs"][0]["owner"] == "external"
    assert scores["configs"][0]["config_name"] is None
    db.close()


# ── AC3: Overview quality maths + short-circuit + endpoint cache ─────────────


def test_quality_query_restricts_to_agent_configs():
    qs = online.quality_queries([AGENT_CFG, "other-1"])
    assert set(qs) == {"pairs", "totals"}
    for q in qs.values():
        assert q.lstrip().startswith("SOURCE logGroups(namePrefix:")
        assert f'onlineEvaluationConfigId in ["{AGENT_CFG}", "other-1"]' in q
    assert ("by attributes.gen_ai.evaluation.name as evaluator, onlineEvaluationConfigId"
            in qs["pairs"])


def test_parse_quality_is_polarity_normalised_and_count_weighted():
    db = SessionLocal()
    a = seed_config(db)
    b = seed_config(db, config_id="oe_b-1", agent_id="a2", agent_name="b", name="oe_b")
    ledger = {a.config_id: a, b.config_id: b}
    db.close()
    def pair(evaluator, config_id, mean, count):
        return {"evaluator": evaluator, "config_id": config_id, "mean": mean, "count": count}

    rows = {
        "pairs": [
            pair("Builtin.Helpfulness", AGENT_CFG, "0.8", "3"),
            pair("Builtin.Refusal", AGENT_CFG, "0.1", "1"),
            pair("Builtin.Helpfulness", "oe_b-1", "0.5", "1"),
            pair("", "oe_b-1", "1", "9"),  # dropped
        ],
        "totals": [{"sessions": "4", "configs": "2"}],
    }
    out = online.parse_quality(rows, ledger)
    # (0.8*3 + (1-0.1)*1 + 0.5*1) / 5 = 3.8 / 5
    assert out["mean"] == 0.76
    assert out["scores"] == 5 and out["sessions"] == 4
    assert out["agents"] == 2 and out["configs"] == 2
    assert out["evaluators"] == [
        {"evaluator_id": "Builtin.Helpfulness", "mean": 0.725, "count": 4, "polarity": 1},
        {"evaluator_id": "Builtin.Refusal", "mean": 0.1, "count": 1, "polarity": -1},
    ]
    # configured but nothing judged yet: the tile must not read "no configs"
    empty = online.parse_quality({"pairs": [], "totals": []}, ledger)
    assert empty["mean"] is None and empty["configs"] == 2 and empty["agents"] == 0


def test_online_quality_short_circuits_without_agent_configs():
    db = SessionLocal()

    def run(*a, **k):
        raise AssertionError("no query expected")

    out = online.online_quality(db, ws_ctx(), run_queries=run)
    assert out == {"range": "24h", "mean": None, "scores": 0, "sessions": 0,
                   "agents": 0, "configs": 0, "evaluators": []}
    db.close()


def test_overview_online_quality_endpoint_caches_per_workspace(client, monkeypatch):
    db = SessionLocal()
    seed_config(db)
    db.close()
    calls = []

    def run(queries, hours, **kw):
        calls.append(hours)
        return {"pairs": [{"evaluator": "Builtin.Helpfulness", "config_id": AGENT_CFG,
                           "mean": "0.9", "count": "2"}],
                "totals": [{"sessions": "2", "configs": "1"}]}

    monkeypatch.setattr(online, "run_insights_queries", run)
    first = client.get("/api/overview/online-quality")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["mean"] == 0.9 and body["scores"] == 2 and body["agents"] == 1
    assert body["cached"] is False
    second = client.get("/api/overview/online-quality")
    assert second.json()["cached"] is True
    assert calls == [24]
    third = client.get("/api/overview/online-quality?force=true")
    assert third.json()["cached"] is False and calls == [24, 24]
