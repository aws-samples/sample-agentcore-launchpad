"""Online evaluation insights mode: config lifecycle, report attribution, on-demand
reports. Pins the AWS shapes proven live 2026-09-02 (research/report-attribution.md):
`insights` XOR `evaluators` on a config, config-sourced batches echo neither, the
source ARN is only on Get, and Start with onlineEvaluationConfigSource carries no
evaluators/insights."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

import app.evaluation.service as svc
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.evaluation import online
from app.evaluation.models import EvalRun
from tests.evaluation.test_online_configs import (
    LOG_GROUP,
    SERVICE,
    T0,
    aws_detail,
    ledger_row,
    make_agent,
    stub_environment,
)

INS = list(online.INSIGHT_IDS)


@pytest.fixture(autouse=True)
def _fresh_attribution():
    online.reset_attribution_cache()
    yield
    online.reset_attribution_cache()


def insights_detail(config_id, name, *, freqs=("DAILY",), insights=INS, **kw):
    d = aws_detail(config_id, name, evaluators=(), **kw)
    d["insights"] = [{"insightId": i} for i in insights]
    if freqs:
        d["clusteringConfig"] = {"frequencies": list(freqs)}
    return d


# ── AC1: create ─────────────────────────────────────────────────────────────


def test_create_insights_mode_sends_insights_and_no_evaluators(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    control, _ = stub_environment(monkeypatch)
    res = client.post("/api/eval/online", json={
        "agent_id": agent.id, "mode": "insights",
        "insights": INS[:2], "clustering_frequencies": ["DAILY", "WEEKLY"],
    })
    assert res.status_code == 201, res.text
    body = res.json()
    kw = control.create_online_evaluation_config.call_args.kwargs
    assert "evaluators" not in kw
    assert kw["insights"] == [{"insightId": i} for i in INS[:2]]
    assert kw["clusteringConfig"] == {"frequencies": ["DAILY", "WEEKLY"]}
    assert kw["rule"]["samplingConfig"]["samplingPercentage"] == 100.0  # AWS default
    assert body["mode"] == "insights" and body["evaluators"] == []
    assert body["insights"] == INS[:2] and body["clustering_frequencies"] == ["DAILY", "WEEKLY"]
    db.close()


def test_create_insights_without_clustering_omits_the_key(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    control, _ = stub_environment(monkeypatch)
    res = client.post("/api/eval/online", json={
        "agent_id": agent.id, "mode": "insights", "insights": INS, "sampling_percentage": 25,
    })
    assert res.status_code == 201, res.text
    kw = control.create_online_evaluation_config.call_args.kwargs
    assert "clusteringConfig" not in kw
    assert kw["rule"]["samplingConfig"]["samplingPercentage"] == 25.0
    assert res.json()["clustering_frequencies"] == []
    db.close()


def test_create_scores_mode_is_unchanged_and_defaults_to_10(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    control, _ = stub_environment(monkeypatch)
    res = client.post("/api/eval/online", json={
        "agent_id": agent.id, "evaluators": ["Builtin.Helpfulness"]})
    assert res.status_code == 201, res.text
    kw = control.create_online_evaluation_config.call_args.kwargs
    assert kw["evaluators"] == [{"evaluatorId": "Builtin.Helpfulness"}]
    assert "insights" not in kw and "clusteringConfig" not in kw
    assert kw["rule"]["samplingConfig"]["samplingPercentage"] == 10.0
    assert res.json()["mode"] == "scores"
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
    control, _ = stub_environment(monkeypatch)
    res = client.post("/api/eval/online", json={"agent_id": agent.id, **body})
    assert res.status_code == 422, res.text
    control.create_online_evaluation_config.assert_not_called()
    db.close()


def test_validate_insights_service_level():
    ids, freqs = online.validate_insights([INS[0], INS[0], INS[1]], ["WEEKLY"])
    assert ids == INS[:2] and freqs == ["WEEKLY"]
    with pytest.raises(AppError) as exc:
        online.validate_insights(["Builtin.Insight.Nope"], [])
    assert exc.value.code == "online_eval.mode_conflict"


def test_rows_report_mode(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    control, details = stub_environment(monkeypatch, {
        "oe_a-1": aws_detail("oe_a-1", "oe_eval_agent_aaaaaa"),
        "oe_i-1": insights_detail("oe_i-1", "oe_eval_agent_iiiiii"),
    })
    ledger_row(db, agent, "oe_a-1")
    ledger_row(db, agent, "oe_i-1", name="oe_eval_agent_iiiiii")
    rows = {r["config_id"]: r for r in client.get("/api/eval/online").json()["configs"]}
    assert rows["oe_a-1"]["mode"] == "scores"
    assert rows["oe_i-1"]["mode"] == "insights"
    assert rows["oe_i-1"]["insights"] == INS
    assert rows["oe_i-1"]["clustering_frequencies"] == ["DAILY"]
    db.close()


# ── AC2: patch ──────────────────────────────────────────────────────────────


def test_patch_insights_mode_sends_complete_top_level_lists(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    control, details = stub_environment(monkeypatch, {
        "oe_i-1": insights_detail("oe_i-1", "oe_eval_agent_iiiiii")})
    ledger_row(db, agent, "oe_i-1", name="oe_eval_agent_iiiiii")

    res = client.patch("/api/eval/online/oe_i-1", json={"insights": INS[:1]})
    assert res.status_code == 200, res.text
    kw = control.update_online_evaluation_config.call_args.kwargs
    assert kw["insights"] == [{"insightId": INS[0]}]
    assert "clusteringConfig" not in kw and "rule" not in kw and "evaluators" not in kw

    res = client.patch("/api/eval/online/oe_i-1", json={"clustering_frequencies": []})
    assert res.status_code == 200, res.text
    kw = control.update_online_evaluation_config.call_args.kwargs
    assert kw["clusteringConfig"] == {"frequencies": []}  # clears clustering
    assert "insights" not in kw

    # the wrong kind for the mode → 422, no AWS call
    control.update_online_evaluation_config.reset_mock()
    res = client.patch("/api/eval/online/oe_i-1", json={"evaluators": ["Builtin.Helpfulness"]})
    assert res.status_code == 422 and res.json()["code"] == "online_eval.mode_conflict"
    control.update_online_evaluation_config.assert_not_called()
    db.close()


def test_patch_scores_mode_rejects_insights(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    control, _ = stub_environment(monkeypatch, {"oe_a-1": aws_detail("oe_a-1", "oe_x")})
    ledger_row(db, agent, "oe_a-1", name="oe_x")
    res = client.patch("/api/eval/online/oe_a-1", json={"insights": INS})
    assert res.status_code == 422 and res.json()["code"] == "online_eval.mode_conflict"
    res = client.patch("/api/eval/online/oe_a-1", json={"clustering_frequencies": ["DAILY"]})
    assert res.status_code == 422
    control.update_online_evaluation_config.assert_not_called()
    db.close()


# ── AC3: reports list ───────────────────────────────────────────────────────

ARN_I = "arn:aws:bedrock-agentcore:us-west-2:1:online-evaluation-config/oe_i-1"
ARN_OTHER = "arn:aws:bedrock-agentcore:us-west-2:1:online-evaluation-config/oe_z-9"


def batch(batch_id, created, *, source=None, evaluators=None, insights=None, status="COMPLETED",
          completed=3):
    summary = {
        "batchEvaluationId": batch_id, "batchEvaluationName": batch_id.split("-")[0],
        "status": status, "createdAt": created, "updatedAt": created,
        "evaluationResults": {"numberOfSessionsCompleted": completed,
                              "numberOfSessionsFailed": 0, "numberOfSessionsInProgress": 0},
    }
    if evaluators:
        summary["evaluators"] = [{"evaluatorId": e} for e in evaluators]
    if insights:
        summary["insights"] = [{"insightId": i} for i in insights]
    detail = dict(summary)
    if source:
        detail["dataSourceConfig"] = {"onlineEvaluationConfigSource": {
            "onlineEvaluationConfigArn": source,
            "timeRange": {"startTime": T0, "endTime": T0}}}
    else:
        detail["dataSourceConfig"] = {"cloudWatchLogs": {"serviceNames": [SERVICE],
                                                         "logGroupNames": [LOG_GROUP]}}
    return summary, detail


def data_stub(batches):
    """List serves summaries (deliberately NOT newest-first); Get serves details."""
    data = MagicMock()
    summaries = [s for s, _ in batches]
    details = {d["batchEvaluationId"]: d for _, d in batches}
    data.list_batch_evaluations.return_value = {"batchEvaluations": summaries}
    data.get_batch_evaluation.side_effect = (
        lambda batchEvaluationId: details[batchEvaluationId])  # noqa: N803
    return data


def test_reports_attribute_by_source_arn_and_merge_console_runs(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    control, _ = stub_environment(monkeypatch, {
        "oe_i-1": insights_detail("oe_i-1", "oe_eval_agent_iiiiii")})
    ledger_row(db, agent, "oe_i-1", name="oe_eval_agent_iiiiii")
    t = lambda h: datetime(2026, 9, 2, h, 0, tzinfo=UTC)  # noqa: E731
    batches = [
        batch("sched_a-1111111111", t(8), source=ARN_I),                 # AWS scheduled
        batch("run_cons-2222222222", t(10), source=ARN_I),                # console run (ledger)
        batch("sched_z-3333333333", t(9), source=ARN_OTHER),              # another config
        batch("run_old-4444444444", t(11), evaluators=["Builtin.Helpfulness"]),  # console eval run
        batch("ins_old-5555555555", t(12), insights=INS),                 # console insights run
    ]
    data = data_stub(batches)
    monkeypatch.setattr(online, "data_client", lambda _ctx: data)
    run = EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent.id, agent_name=agent.name,
                  dataset_name="online:oe_i-1", mode="insights", evaluators=INS,
                  status="completed", batch_eval_id="run_cons-2222222222")
    queued = EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent.id, agent_name=agent.name,
                     dataset_name="online:oe_i-1", mode="insights", evaluators=INS,
                     status="queued")
    db.add_all([run, queued])
    db.commit()

    res = client.get("/api/eval/online/oe_i-1/reports")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["mode"] == "insights"
    rows = body["reports"]
    assert [(r["batch_id"], r["origin"]) for r in rows] == [
        (None, "console"),                        # queued, no batch yet (newest created_at)
        ("run_cons-2222222222", "console"),
        ("sched_a-1111111111", "aws_scheduled"),
    ]
    assert rows[0]["status"] == "QUEUED" and rows[0]["run_id"] == queued.id
    assert rows[1]["run_id"] == run.id and rows[1]["sessions"]["completed"] == 3
    assert rows[2]["insights"] == INS and rows[2]["run_id"] is None
    # Only the two unknown source-less batches needed a Get (console-run batches are
    # excluded by their evaluators/insights keys or the ledger); cached afterwards.
    assert data.get_batch_evaluation.call_count == 2
    client.get("/api/eval/online/oe_i-1/reports")
    assert data.get_batch_evaluation.call_count == 2
    db.close()


def test_report_detail_parses_trees_and_rejects_foreign_batches(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    stub_environment(monkeypatch, {"oe_i-1": insights_detail("oe_i-1", "oe_eval_agent_iiiiii")})
    ledger_row(db, agent, "oe_i-1", name="oe_eval_agent_iiiiii")
    mine = batch("sched_a-1111111111", T0, source=ARN_I)
    mine[1]["failureAnalysisResult"] = {"failures": [{"clusterId": 1, "name": "DB missing"}]}
    mine[1]["userIntentResult"] = {"userIntents": []}
    other = batch("sched_z-3333333333", T0, source=ARN_OTHER)
    monkeypatch.setattr(online, "data_client", lambda _ctx: data_stub([mine, other]))
    res = client.get("/api/eval/online/oe_i-1/reports/sched_a-1111111111")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["insights"] == {"failures": [{"clusterId": 1, "name": "DB missing"}],
                                "userIntents": []}
    assert body["sessions"]["completed"] == 3 and body["time_range"]["startTime"]
    assert client.get("/api/eval/online/oe_i-1/reports/sched_z-3333333333").status_code == 404
    db.close()


# ── AC4: run report now ─────────────────────────────────────────────────────


def test_start_report_submits_an_online_sourced_insights_run(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    control, details = stub_environment(monkeypatch, {
        "oe_i-1": insights_detail("oe_i-1", "oe_eval_agent_iiiiii"),
        "oe_a-1": aws_detail("oe_a-1", "oe_eval_agent_aaaaaa"),
        "exp_1_oe1-9999999999": insights_detail("exp_1_oe1-9999999999", "exp_1_oe1"),
    })
    ledger_row(db, agent, "oe_i-1", name="oe_eval_agent_iiiiii")
    ledger_row(db, agent, "oe_a-1")
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
    assert seen["mode"] == "insights" and seen["insights"] == INS
    assert seen["dataset_name"] == "online:oe_i-1" and seen["dataset_items"] == []
    assert seen["online_config_arn"] == ARN_I
    span = seen["time_range"]["endTime"] - seen["time_range"]["startTime"]
    assert span.total_seconds() == 6 * 3600

    # scores-mode config → 422; experiment-owned → 403; bad range → 422
    assert client.post("/api/eval/online/oe_a-1/reports", json={}).status_code == 422
    assert client.post("/api/eval/online/exp_1_oe1-9999999999/reports",
                       json={}).status_code == 403
    assert client.post("/api/eval/online/oe_i-1/reports", json={"range": "1y"}).status_code == 422
    db.close()


def test_execute_run_online_source_skips_invoke_and_inherits_analysis(monkeypatch):
    """The Start call carries the config source and NO evaluators/insights (AWS rejects
    them); no invocation, no telemetry wait; insights parsed on completion."""
    db = SessionLocal()
    agent = make_agent(db)
    run = EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent.id, agent_name=agent.name,
                  dataset_name="online:oe_i-1", mode="insights", evaluators=INS, status="queued")
    db.add(run)
    db.commit()
    run_id = run.id
    db.close()
    data = MagicMock()
    data.start_batch_evaluation.return_value = {"batchEvaluationId": "run_ab-1234567890"}
    data.get_batch_evaluation.return_value = {
        "status": "COMPLETED",
        "evaluationResults": {"numberOfSessionsCompleted": 2},
        "userIntentResult": {"userIntents": [{"clusterId": 1, "name": "HR lookups"}]},
    }
    monkeypatch.setattr(svc, "data_client", lambda _ws=None: data)
    invoke = MagicMock(side_effect=AssertionError("no invocation expected"))
    monkeypatch.setattr(svc.rt, "invoke_runtime_text", invoke)
    monkeypatch.setattr(svc, "_wait_for_fresh_telemetry",
                        MagicMock(side_effect=AssertionError("no telemetry wait")))
    from tests.conftest import ws_ctx
    tr = {"startTime": T0, "endTime": T0}
    svc.execute_run(
        run_id, workspace=ws_ctx(), agent_arn="arn:agent", method="zip_runtime",
        service_name=SERVICE, log_group=LOG_GROUP, items=[], evaluators=INS, mode="insights",
        wait_seconds=0, time_range=tr, insights=INS, online_config_arn=ARN_I,
    )
    kw = data.start_batch_evaluation.call_args.kwargs
    assert kw["dataSourceConfig"] == {"onlineEvaluationConfigSource": {
        "onlineEvaluationConfigArn": ARN_I, "timeRange": tr}}
    assert "evaluators" not in kw and "insights" not in kw and "cloudWatchLogs" not in str(kw)
    assert kw["batchEvaluationName"] == f"run_{run_id[:8]}"
    db = SessionLocal()
    row = db.get(EvalRun, run_id)
    assert row.status == "completed" and row.batch_eval_id == "run_ab-1234567890"
    assert row.insights == {"userIntents": [{"clusterId": 1, "name": "HR lookups"}]}
    db.close()


def test_reports_take_session_counts_from_get_when_the_summary_has_none(client, monkeypatch):
    """List summaries of config-sourced batches carry no evaluationResults (live);
    the row reads them from the (cached) Get detail; in-flight batches are re-read."""
    db = SessionLocal()
    agent = make_agent(db)
    stub_environment(monkeypatch, {"oe_i-1": insights_detail("oe_i-1", "oe_eval_agent_iiiiii")})
    ledger_row(db, agent, "oe_i-1", name="oe_eval_agent_iiiiii")
    done_s, done_d = batch("sched_a-1111111111", T0, source=ARN_I, completed=2)
    done_s.pop("evaluationResults")
    live_s, live_d = batch("sched_b-2222222222", T0, source=ARN_I, status="IN_PROGRESS")
    live_s.pop("evaluationResults")
    data = data_stub([(done_s, done_d), (live_s, live_d)])
    monkeypatch.setattr(online, "data_client", lambda _ctx: data)
    rows = {r["batch_id"]: r for r in client.get("/api/eval/online/oe_i-1/reports")
            .json()["reports"]}
    assert rows["sched_a-1111111111"]["sessions"]["completed"] == 2
    assert rows["sched_a-1111111111"]["sessions"]["total"] == 2  # summed when AWS omits it
    assert rows["sched_b-2222222222"]["status"] == "IN_PROGRESS"
    assert data.get_batch_evaluation.call_count == 2
    client.get("/api/eval/online/oe_i-1/reports")
    assert data.get_batch_evaluation.call_count == 3  # only the in-flight one re-read
    db.close()


def test_reports_survive_a_batch_listing_failure(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    stub_environment(monkeypatch, {"oe_i-1": insights_detail("oe_i-1", "oe_eval_agent_iiiiii")})
    ledger_row(db, agent, "oe_i-1", name="oe_eval_agent_iiiiii")
    db.add(EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent.id, agent_name=agent.name,
                   dataset_name="online:oe_i-1", mode="insights", evaluators=INS,
                   status="evaluating", batch_eval_id="run_x-1234567890"))
    db.commit()
    data = MagicMock()
    data.list_batch_evaluations.side_effect = RuntimeError("ThrottlingException")
    monkeypatch.setattr(online, "data_client", lambda _ctx: data)
    body = client.get("/api/eval/online/oe_i-1/reports").json()
    assert body["aws_unavailable"] is True
    assert [r["origin"] for r in body["reports"]] == ["console"]
    assert body["reports"][0]["status"] == "EVALUATING"
    db.close()
