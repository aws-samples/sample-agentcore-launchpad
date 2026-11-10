"""Online evaluation insights mode (N12 E2-1/E2-3): config lifecycle, reports.

A config carries ``insights`` (+ optional clustering frequencies) INSTEAD of
``evaluators``. Reports are on-demand insights runs over the sessions the scheduler
sampled for the config in the chosen window (plan §B.4 — no AWS-scheduled clustering
jobs in CN; ``clustering_frequencies`` is stored for the UI but drives nothing).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.evaluation import online
from app.evaluation.models import EvalRun, OnlineEvalConfig, OnlineSample
from tests.evaluation.test_online_configs import ledger_row, make_agent, stub_environment

INS = list(online.INSIGHT_IDS)


def insights_row(db, agent, config_id, name="oe_eval_agent_iiiiii", *, freqs=("DAILY",),
                 insights=INS, **cols):
    return ledger_row(db, agent, config_id, name, mode="insights", evaluators=[],
                      insights=list(insights), clustering_frequencies=list(freqs),
                      sampling_percentage=100.0, **cols)


def sample(db, config_id, session_id, *, at=None, status="sampled"):
    row = OnlineSample(workspace_id=DEFAULT_WORKSPACE_ID, config_id=config_id,
                       session_id=session_id, status=status,
                       sampled_at=at or datetime.now(UTC))
    db.add(row)
    db.commit()
    return row


# ── create ──────────────────────────────────────────────────────────────────


def test_create_insights_mode_stores_insights_and_no_evaluators(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    stub_environment(monkeypatch)
    res = client.post("/api/eval/online", json={
        "agent_id": agent.id, "mode": "insights",
        "insights": INS[:2], "clustering_frequencies": ["DAILY", "WEEKLY"],
    })
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["mode"] == "insights" and body["evaluators"] == []
    assert body["insights"] == INS[:2] and body["clustering_frequencies"] == ["DAILY", "WEEKLY"]
    assert body["sampling_percentage"] == 100.0  # insights default
    row = db.query(OnlineEvalConfig).one()
    assert row.mode == "insights" and row.evaluators == [] and row.insights == INS[:2]
    assert row.clustering_frequencies == ["DAILY", "WEEKLY"]
    db.close()


def test_create_insights_without_clustering_and_custom_sampling(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    stub_environment(monkeypatch)
    res = client.post("/api/eval/online", json={
        "agent_id": agent.id, "mode": "insights", "insights": INS, "sampling_percentage": 25,
    })
    assert res.status_code == 201, res.text
    assert res.json()["clustering_frequencies"] == []
    assert res.json()["sampling_percentage"] == 25.0
    db.close()


def test_create_scores_mode_is_unchanged_and_defaults_to_10(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    stub_environment(monkeypatch)
    res = client.post("/api/eval/online", json={
        "agent_id": agent.id, "evaluators": ["Builtin.Helpfulness"]})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["mode"] == "scores" and body["insights"] == []
    assert body["sampling_percentage"] == 10.0
    db.close()


@pytest.mark.parametrize("body", [
    {"mode": "insights", "insights": INS, "evaluators": ["Builtin.Helpfulness"]},
    {"mode": "insights"},
    {"mode": "scores", "evaluators": ["Builtin.Helpfulness"], "insights": INS},
    {"mode": "insights", "insights": INS + ["Builtin.Insight.Extra"]},
    {"mode": "insights", "insights": INS, "clustering_frequencies": ["HOURLY"]},
    {"mode": "insights", "insights": INS,
     "clustering_frequencies": ["DAILY", "WEEKLY", "MONTHLY", "DAILY"]},
])
def test_create_rejects_mixed_or_invalid_kinds(client, monkeypatch, body):
    db = SessionLocal()
    agent = make_agent(db)
    stub_environment(monkeypatch)
    res = client.post("/api/eval/online", json={"agent_id": agent.id, **body})
    assert res.status_code == 422, res.text
    assert db.query(OnlineEvalConfig).count() == 0
    db.close()


def test_validate_insights_service_level():
    ids, freqs = online.validate_insights([INS[0], INS[0], INS[1]], ["WEEKLY"])
    assert ids == INS[:2] and freqs == ["WEEKLY"]
    with pytest.raises(AppError) as exc:
        online.validate_insights(["Builtin.Insight.Nope"], [])
    assert exc.value.code == "online_eval.mode_conflict"


def test_rows_report_mode(client):
    db = SessionLocal()
    agent = make_agent(db)
    ledger_row(db, agent, "oe_a-1")
    insights_row(db, agent, "oe_i-1")
    rows = {r["config_id"]: r for r in client.get("/api/eval/online").json()["configs"]}
    assert rows["oe_a-1"]["mode"] == "scores"
    assert rows["oe_i-1"]["mode"] == "insights"
    assert rows["oe_i-1"]["insights"] == INS
    assert rows["oe_i-1"]["clustering_frequencies"] == ["DAILY"]
    db.close()


# ── patch ───────────────────────────────────────────────────────────────────


def test_patch_insights_mode_replaces_complete_lists(client):
    db = SessionLocal()
    agent = make_agent(db)
    insights_row(db, agent, "oe_i-1")

    res = client.patch("/api/eval/online/oe_i-1", json={"insights": INS[:1]})
    assert res.status_code == 200, res.text
    assert res.json()["insights"] == INS[:1] and res.json()["clustering_frequencies"] == ["DAILY"]

    res = client.patch("/api/eval/online/oe_i-1", json={"clustering_frequencies": []})
    assert res.status_code == 200, res.text
    assert res.json()["clustering_frequencies"] == [] and res.json()["insights"] == INS[:1]

    # the wrong kind for the mode → 422, row untouched
    res = client.patch("/api/eval/online/oe_i-1", json={"evaluators": ["Builtin.Helpfulness"]})
    assert res.status_code == 422 and res.json()["code"] == "online_eval.mode_conflict"
    db.expire_all()
    row = db.query(OnlineEvalConfig).one()
    assert row.evaluators == [] and row.insights == INS[:1] and row.clustering_frequencies == []
    db.close()


def test_patch_scores_mode_rejects_insights(client):
    db = SessionLocal()
    agent = make_agent(db)
    ledger_row(db, agent, "oe_a-1", name="oe_x")
    res = client.patch("/api/eval/online/oe_a-1", json={"insights": INS})
    assert res.status_code == 422 and res.json()["code"] == "online_eval.mode_conflict"
    res = client.patch("/api/eval/online/oe_a-1", json={"clustering_frequencies": ["DAILY"]})
    assert res.status_code == 422
    db.close()


# ── reports list / detail ───────────────────────────────────────────────────


def _run(db, agent, config_id, *, status, batch_eval_id=None, created_at=None, **fields):
    run = EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent.id, agent_name=agent.name,
                  dataset_name=f"online:{config_id}", mode="insights", evaluators=INS,
                  status=status, batch_eval_id=batch_eval_id, **fields)
    if created_at is not None:
        run.created_at = created_at
    db.add(run)
    db.commit()
    return run


def test_reports_list_console_runs_newest_first(client):
    db = SessionLocal()
    agent = make_agent(db)
    insights_row(db, agent, "oe_i-1")
    t = lambda h: datetime(2026, 9, 2, h, 0, tzinfo=UTC)  # noqa: E731
    done = _run(db, agent, "oe_i-1", status="completed", batch_eval_id="job-1", created_at=t(10),
                session_ids=["s1", "s2", "s3"],
                insights={"failures": [{"clusterId": 1, "name": "DB missing"}]})
    queued = _run(db, agent, "oe_i-1", status="queued", created_at=t(12))
    failed = _run(db, agent, "oe_i-1", status="failed", batch_eval_id="job-0", created_at=t(8),
                  session_ids=["s9"], error="judge down")
    _run(db, agent, "oe_z-9", status="completed", batch_eval_id="job-x", created_at=t(11))
    # an evaluators-mode run of the agent is not a report
    other = EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent.id, agent_name=agent.name,
                    dataset_name="window:24h", mode="evaluators", status="completed")
    db.add(other)
    db.commit()

    res = client.get("/api/eval/online/oe_i-1/reports")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["config_id"] == "oe_i-1" and body["mode"] == "insights"
    assert body["aws_unavailable"] is False
    rows = body["reports"]
    assert [(r["batch_id"], r["origin"], r["run_id"]) for r in rows] == [
        (None, "console", queued.id),
        ("job-1", "console", done.id),
        ("job-0", "console", failed.id),
    ]
    assert rows[0]["status"] == "QUEUED" and rows[0]["run_status"] == "queued"
    assert rows[1]["sessions"] == {"completed": 3, "failed": 0, "in_progress": 0, "total": 3}
    assert rows[1]["insights"] == INS and rows[1]["error"] is None
    assert rows[2]["sessions"] == {"completed": 0, "failed": 1, "in_progress": 0, "total": 1}
    assert rows[2]["error"] == "judge down"
    assert set(rows[0]) == {"batch_id", "name", "status", "run_status", "created_at",
                            "updated_at", "insights", "sessions", "origin", "run_id", "error",
                            "created_by", "created_by_name"}  # Family B step 1 (additive)
    db.close()


def test_report_detail_reads_the_run_and_rejects_foreign_ids(client):
    db = SessionLocal()
    agent = make_agent(db)
    insights_row(db, agent, "oe_i-1")
    ledger_row(db, agent, "oe_z-9", name="oe_zzz")
    mine = _run(db, agent, "oe_i-1", status="completed", batch_eval_id="job-1",
                session_ids=["s1", "s2"],
                insights={"failures": [{"clusterId": 1, "name": "DB missing"}],
                          "userIntents": []})
    other = _run(db, agent, "oe_z-9", status="completed", batch_eval_id="job-x")

    res = client.get("/api/eval/online/oe_i-1/reports/job-1")
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == {"batch_id", "name", "status", "created_at", "updated_at",
                         "time_range", "sessions", "insights", "error_details"}
    assert body["batch_id"] == "job-1" and body["status"] == "COMPLETED"
    assert body["insights"] == {"failures": [{"clusterId": 1, "name": "DB missing"}],
                                "userIntents": []}
    assert body["sessions"]["completed"] == 2 and body["error_details"] == []
    # the run id resolves too (a queued report has no job id yet)
    assert client.get(f"/api/eval/online/oe_i-1/reports/{mine.id}").status_code == 200
    assert client.get("/api/eval/online/oe_i-1/reports/job-x").status_code == 404
    assert client.get(f"/api/eval/online/oe_i-1/reports/{other.id}").status_code == 404
    db.close()


# ── run report now ──────────────────────────────────────────────────────────


def test_start_report_submits_an_insights_run_over_the_sampled_sessions(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    insights_row(db, agent, "oe_i-1")
    ledger_row(db, agent, "oe_a-1")
    insights_row(db, agent, "exp_1_oe1-9999999999", name="exp_1_oe1")
    now = datetime.now(UTC)
    sample(db, "oe_i-1", "s-recent-1", at=now - timedelta(hours=1))
    sample(db, "oe_i-1", "s-recent-2", at=now - timedelta(hours=5))
    sample(db, "oe_i-1", "s-old", at=now - timedelta(hours=30))
    sample(db, "oe_a-1", "s-other-config", at=now)
    seen = {}

    def fake_submit(**kw):
        seen.update(kw)
        run = EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent.id,
                      agent_name=agent.name, dataset_name=kw["dataset_name"], mode=kw["mode"],
                      evaluators=kw["evaluators"], status="queued", queue_position=1)
        db.add(run)
        db.commit()
        return run

    monkeypatch.setattr(online.eval_service, "submit_run", fake_submit)
    res = client.post("/api/eval/online/oe_i-1/reports", json={"range": "6h"})
    assert res.status_code == 202, res.text
    assert res.json()["status"] == "queued" and res.json()["range"] == "6h"
    assert res.json()["config_id"] == "oe_i-1" and res.json()["run_id"]
    assert seen["mode"] == "insights" and seen["insights"] == INS and seen["evaluators"] == INS
    assert seen["dataset_name"] == "online:oe_i-1" and seen["dataset_items"] == []
    assert seen["agent"].id == agent.id
    assert sorted(seen["session_ids"]) == ["s-recent-1", "s-recent-2"]
    assert "online_config_arn" not in seen and "time_range" not in seen

    # scores-mode config → 422; experiment-owned → 403; bad range → 422
    assert client.post("/api/eval/online/oe_a-1/reports", json={}).status_code == 422
    assert client.post("/api/eval/online/exp_1_oe1-9999999999/reports",
                       json={}).status_code == 403
    assert client.post("/api/eval/online/oe_i-1/reports", json={"range": "1y"}).status_code == 422
    db.close()


def test_start_report_without_sampled_sessions_is_409(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    insights_row(db, agent, "oe_i-1")
    sample(db, "oe_i-1", "s-old", at=datetime.now(UTC) - timedelta(days=3))
    monkeypatch.setattr(online.eval_service, "submit_run",
                        lambda **kw: pytest.fail("no run must be submitted"))
    res = client.post("/api/eval/online/oe_i-1/reports", json={"range": "24h"})
    assert res.status_code == 409
    assert res.json()["code"] == "online_eval.no_sampled_sessions"
    db.close()


def test_results_endpoint_is_empty_for_insights_configs(client):
    db = SessionLocal()
    agent = make_agent(db)
    insights_row(db, agent, "oe_i-1")
    body = client.get("/api/eval/online/oe_i-1/results").json()
    assert body["evaluators"] == [] and body["recent"] == []
    assert body["log_group"] == online.results_log_group("oe_i-1")
    db.close()
