"""Online evaluation cross-surface reads (N12 E2-3): session-detail scores + Overview tile.

Both read ``eval_results`` rows written by the online scheduler (``source="online"``);
these tests pin the owner grouping, the polarity-normalised tile maths, the fail-soft
contract on the session detail and the endpoint cache.
"""

from datetime import UTC, datetime, timedelta

import pytest

import app.routers.overview as overview_mod
import app.services.observability as obs
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import online
from app.evaluation.models import EvalResult, OnlineEvalConfig
from tests.conftest import ws_ctx
from tests.test_observability import _fake_logs

SID = "s" * 64
AGENT_CFG = "oe_hr_assistant_4341b9-0GfiAU9TtP"
EXP_CFG = "exp_canary_1-AbCdEf1234"
GONE_CFG = "oe_deleted_config-0000000000"


@pytest.fixture(autouse=True)
def _fresh_caches():
    obs.reset_cache()
    overview_mod._cache.clear()
    yield
    obs.reset_cache()
    overview_mod._cache.clear()


def seed_config(db, config_id=AGENT_CFG, agent_id="a1", agent_name="hr-assistant",
                name="oe_hr_assistant_4341b9") -> OnlineEvalConfig:
    row = OnlineEvalConfig(
        workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id, agent_name=agent_name,
        config_id=config_id, config_arn=f"arn:aws-cn:launchpad::1:oec/{config_id}",
        name=name, service_name="svc.DEFAULT", log_group="/aws/bedrock-agentcore/runtimes/x",
        evaluators=["Builtin.Helpfulness"],
    )
    db.add(row)
    db.commit()
    return row


def record(db, config_id, evaluator, score, *, session_id=SID, label="Pass", at=None,
           ordinal=0, workspace_id=DEFAULT_WORKSPACE_ID):
    row = EvalResult(
        id=f"{config_id}:{session_id}:{evaluator}:{ordinal}", workspace_id=workspace_id,
        source="online", config_id=config_id, session_id=session_id, trace_id="t" * 32,
        evaluator_id=evaluator, level="SESSION", score=score, label=label,
        explanation=f"because {evaluator}", created_at=at or datetime.now(UTC),
    )
    db.add(row)
    db.commit()
    return row


# ── AC1: session scores grouped by config, agent-owned first ────────────────


def test_session_online_scores_groups_by_config_with_agent_first():
    db = SessionLocal()
    seed_config(db)
    seed_config(db, config_id=EXP_CFG, name="exp_canary_1")
    now = datetime.now(UTC)
    record(db, EXP_CFG, "Builtin.Correctness", 0.5, at=now - timedelta(minutes=3))
    record(db, AGENT_CFG, "Builtin.Refusal", 0.0, label="No", at=now - timedelta(minutes=2))
    record(db, AGENT_CFG, "Builtin.Helpfulness", 0.9, at=now - timedelta(minutes=1))
    record(db, GONE_CFG, "Builtin.Helpfulness", 0.8, at=now)  # its config was deleted
    record(db, AGENT_CFG, "Builtin.Helpfulness", 0.1, session_id="other-session", at=now)
    record(db, AGENT_CFG, "Builtin.Helpfulness", 0.2, at=now - timedelta(days=3), ordinal=9)

    out = online.session_online_scores(db, ws_ctx(), SID, 24)
    assert out["total"] == 4 and out["unavailable"] is False and out["configs_exist"] is True
    assert [c["config_id"] for c in out["configs"]] == [AGENT_CFG, GONE_CFG, EXP_CFG]
    assert [c["owner"] for c in out["configs"]] == ["agent", "agent", "experiment"]
    agent = out["configs"][0]
    assert agent["config_name"] == "oe_hr_assistant_4341b9"
    assert agent["agent"] == {"id": "a1", "name": "hr-assistant"}
    # newest record first within a config
    assert [r["evaluator_id"] for r in agent["records"]] == [
        "Builtin.Helpfulness", "Builtin.Refusal"]
    assert agent["records"][1]["score"] == 0.0 and agent["records"][1]["label"] == "No"
    assert set(agent["records"][0]) == {"time", "evaluator_id", "level", "score", "label",
                                         "explanation", "trace_id"}
    gone = out["configs"][1]
    assert gone["config_name"] is None and gone["agent"] is None
    assert out["configs"][2]["config_name"] == "exp_canary_1"
    db.close()


def test_session_online_scores_without_any_config():
    db = SessionLocal()
    out = online.session_online_scores(db, ws_ctx(), SID, 24)
    assert out == {"configs": [], "total": 0, "unavailable": False, "configs_exist": False}
    db.close()


# ── AC2: fail-soft on the session detail ───────────────────────────────────


def test_get_session_degrades_online_scores_without_losing_traces(monkeypatch):
    db = SessionLocal()
    seed_config(db)
    monkeypatch.setattr(obs, "session_transcript",
                        lambda *a, **k: {"available": False, "reason": "not_platform_session"})

    def boom(*a, **k):
        raise RuntimeError("ledger locked")

    monkeypatch.setattr(online, "_result_rows", boom)
    result = obs.get_session(SID, "24h", db, ws_ctx(), logs=_fake_logs())
    assert len(result["traces"]) == 1  # AGG_ROW still rendered
    assert result["online_scores"] == {
        "configs": [], "total": 0, "unavailable": True, "configs_exist": False}
    db.close()


def test_get_session_carries_online_scores_when_the_ledger_has_them(monkeypatch):
    db = SessionLocal()
    monkeypatch.setattr(obs, "session_transcript",
                        lambda *a, **k: {"available": False, "reason": "not_platform_session"})
    record(db, GONE_CFG, "Builtin.Helpfulness", 0.7)
    result = obs.get_session(SID, "24h", db, ws_ctx(), logs=_fake_logs())
    scores = result["online_scores"]
    assert scores["configs_exist"] is False and scores["total"] == 1
    assert scores["configs"][0]["owner"] == "agent"
    assert scores["configs"][0]["config_name"] is None
    assert scores["configs"][0]["records"][0]["score"] == 0.7
    db.close()


# ── AC3: Overview quality maths + short-circuit + endpoint cache ─────────────


def test_online_quality_is_polarity_normalised_and_count_weighted():
    db = SessionLocal()
    seed_config(db)
    seed_config(db, config_id="oe_b-1", agent_id="a2", agent_name="b", name="oe_b")
    seed_config(db, config_id="oe_idle-1", agent_id="a3", agent_name="idle", name="oe_idle")
    seed_config(db, config_id=EXP_CFG, agent_id="a1", name="exp_canary_1")
    now = datetime.now(UTC)
    for i, score in enumerate((0.8, 0.8, 0.8)):
        record(db, AGENT_CFG, "Builtin.Helpfulness", score, session_id=f"s{i}", at=now)
    record(db, AGENT_CFG, "Builtin.Refusal", 0.1, session_id="s0", at=now)
    record(db, "oe_b-1", "Builtin.Helpfulness", 0.5, session_id="s9", at=now)
    record(db, "oe_b-1", "Builtin.Helpfulness", None, session_id="s8", at=now)  # errored
    record(db, EXP_CFG, "Builtin.Helpfulness", 0.0, session_id="s7", at=now)  # arm config
    record(db, AGENT_CFG, "Builtin.Helpfulness", 0.0, session_id="s6", at=now - timedelta(days=2))

    out = online.online_quality(db, ws_ctx())
    # (0.8*3 + (1-0.1)*1 + 0.5*1) / 5 = 3.8 / 5
    assert out["mean"] == 0.76
    assert out["scores"] == 5 and out["sessions"] == 4
    assert out["agents"] == 2 and out["configs"] == 3  # agent-owned configs only
    assert out["evaluators"] == [
        {"evaluator_id": "Builtin.Helpfulness", "mean": 0.725, "count": 4, "polarity": 1},
        {"evaluator_id": "Builtin.Refusal", "mean": 0.1, "count": 1, "polarity": -1},
    ]
    assert out["range"] == "24h"
    db.close()


def test_online_quality_configured_but_nothing_judged_yet():
    db = SessionLocal()
    seed_config(db)
    seed_config(db, config_id="oe_b-1", agent_id="a2", agent_name="b", name="oe_b")
    empty = online.online_quality(db, ws_ctx())
    assert empty["mean"] is None and empty["configs"] == 2 and empty["agents"] == 0
    db.close()


def test_online_quality_short_circuits_without_agent_configs(monkeypatch):
    db = SessionLocal()
    seed_config(db, config_id=EXP_CFG, name="exp_canary_1")  # arm configs do not count
    monkeypatch.setattr(online, "_result_rows",
                        lambda *a, **k: pytest.fail("no ledger scan expected"))
    out = online.online_quality(db, ws_ctx())
    assert out == {"range": "24h", "mean": None, "scores": 0, "sessions": 0,
                   "agents": 0, "configs": 0, "evaluators": []}
    db.close()


def test_overview_online_quality_endpoint_caches_per_workspace(client, monkeypatch):
    db = SessionLocal()
    seed_config(db)
    record(db, AGENT_CFG, "Builtin.Helpfulness", 0.9, session_id="s1")
    record(db, AGENT_CFG, "Builtin.Helpfulness", 0.9, session_id="s2")
    db.close()
    calls = []
    real = online.online_quality

    def counted(db_, workspace, *a, **k):
        calls.append(workspace.id)
        return real(db_, workspace, *a, **k)

    monkeypatch.setattr(overview_mod.online_eval, "online_quality", counted)
    first = client.get("/api/overview/online-quality")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["mean"] == 0.9 and body["scores"] == 2 and body["agents"] == 1
    assert body["cached"] is False
    second = client.get("/api/overview/online-quality")
    assert second.json()["cached"] is True
    assert calls == ["default"]
    third = client.get("/api/overview/online-quality?force=true")
    assert third.json()["cached"] is False and calls == ["default", "default"]
