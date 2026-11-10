"""P3.1 / P3.2 (N17-obs349 / obs350) — the judge model and its inference config are recorded.

Before: the hub-wide ``eval_judge_model_id`` was copied into the job spec only — no run,
online config or result row said which judge scored it; online read the setting at EACH
dispatch, so a Settings → Model roles change switched the judge mid-series; and every
scoring judge asks for ``temperature=0`` while ``build_chat_model`` silently drops it for a
model whose catalog row rejects it (Kimi K3), which then samples at its provider default.
"""

from datetime import timedelta

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import jobs
from app.evaluation import online_scheduler as sched
from app.evaluation.judge_budget import judge_inference, result_judges
from app.evaluation.models import EvalEvaluator, EvalResult, EvalRun, OnlineEvalConfig
from tests.evaluation.test_online_configs import ledger_row
from tests.evaluation.test_online_configs import make_agent as make_online_agent
from tests.evaluation.test_online_configs import stub_environment as stub_online
from tests.evaluation.test_online_scheduler import NOW, harness, scan_row
from tests.evaluation.test_runs_flow import (
    EvalDataset,
    make_agent,
    result_rows,
    stub_environment,
    wait_terminal,
)

GLM = "global.zai.glm-5.3"
KIMI = "global.moonshotai.kimi-k3"
ROLES = "/api/settings/model-roles"


def test_the_inference_config_says_when_temperature_is_not_applied():
    glm = judge_inference(GLM)
    assert glm["model_id"] == GLM and glm["temperature"] == 0.0
    assert glm["temperature_applied"] is True and glm["max_tokens"] == 4096
    kimi = judge_inference(KIMI)
    assert kimi["temperature"] == "provider default" and kimi["temperature_applied"] is False
    assert kimi["max_tokens"] == 2048


def test_result_judges_name_each_rows_own_model_and_none_for_programmatic_evaluators():
    judges = result_judges([
        {"id": "Builtin.Helpfulness"},
        {"id": "cust_tone", "definition": "judge", "model_id": KIMI},
        {"id": "Builtin.TrajectoryInOrderMatch"},
        {"id": "cust_code", "definition": "code"},
    ], GLM)
    assert judges["Builtin.Helpfulness"]["model_id"] == GLM
    assert judges["cust_tone"]["model_id"] == KIMI
    assert judges["cust_tone"]["temperature_applied"] is False
    assert judges["Builtin.TrajectoryInOrderMatch"] is None and judges["cust_code"] is None


def _dataset(db, name):
    dataset = EvalDataset(workspace_id=DEFAULT_WORKSPACE_ID, name=name, items=[
        {"scenario_id": f"s{k}", "turns": [{"input": prompt}],
         "expected_trajectory": ["hr_lookup"]}
        for k, prompt in enumerate(("2+2?", "3+3?"))])
    db.add(dataset)
    db.commit()
    return dataset


def test_a_run_records_the_default_judge_on_the_row_and_every_result(client, monkeypatch):
    db = SessionLocal()
    agent, dataset = make_agent(db, name="judge-rec-1"), _dataset(db, "judge-rec-1")
    env, _ = stub_environment(monkeypatch)
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "dataset_id": dataset.id, "wait_seconds": 0,
        "evaluators": ["Builtin.Helpfulness", "Builtin.TrajectoryInOrderMatch"]})
    assert res.status_code == 201, res.text
    assert res.json()["judge_model_id"] == GLM
    assert res.json()["judge_inference"]["temperature"] == 0.0
    run = wait_terminal(client, res.json()["id"])
    assert run["status"] == "completed", run.get("error")
    assert env.submitted[0].spec["judge_model_id"] == GLM
    rows = result_rows(run["id"])
    assert rows and {r.evaluator_id for r in rows} == {
        "Builtin.Helpfulness", "Builtin.TrajectoryInOrderMatch"}
    for row in rows:
        if row.evaluator_id == "Builtin.Helpfulness":
            assert row.judge_model_id == GLM and row.judge_inference["temperature_applied"]
        else:  # a programmatic matcher has no judge
            assert row.judge_model_id is None and row.judge_inference is None
    results = client.get(f"/api/eval/runs/{run['id']}/results").json()
    judged = [r for s in results["sessions"] for r in s["results"]
              if r["evaluator_id"] == "Builtin.Helpfulness"]
    assert judged and all(r["judge"]["model_id"] == GLM for r in judged)
    db.close()


def test_a_run_may_choose_its_judge_and_kimi_records_provider_default(client, monkeypatch):
    db = SessionLocal()
    agent, dataset = make_agent(db, name="judge-rec-2"), _dataset(db, "judge-rec-2")
    env, _ = stub_environment(monkeypatch)
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "dataset_id": dataset.id, "wait_seconds": 0,
        "evaluators": ["Builtin.Helpfulness"], "judge_model_id": KIMI})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["judge_model_id"] == KIMI
    assert body["judge_inference"] == {"model_id": KIMI, "temperature": "provider default",
                                       "temperature_applied": False, "max_tokens": 2048}
    run = wait_terminal(client, body["id"])
    assert run["status"] == "completed", run.get("error")
    assert env.submitted[0].spec["judge_model_id"] == KIMI
    assert {r.judge_model_id for r in result_rows(run["id"])} == {KIMI}
    db.close()


def test_an_unknown_judge_model_is_refused_before_any_run(client, monkeypatch):
    db = SessionLocal()
    agent, dataset = make_agent(db, name="judge-rec-3"), _dataset(db, "judge-rec-3")
    _, calls = stub_environment(monkeypatch)
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "dataset_id": dataset.id, "wait_seconds": 0,
        "evaluators": ["Builtin.Helpfulness"], "judge_model_id": "anthropic.claude-x"})
    assert res.status_code == 422, res.text
    assert calls["n"] == 0
    assert db.query(EvalRun).filter(EvalRun.agent_id == agent.id).count() == 0
    db.close()


def test_the_default_follows_the_judge_role_and_a_rerun_keeps_its_sources_judge(
    client, monkeypatch
):
    db = SessionLocal()
    agent, dataset = make_agent(db, name="judge-rec-4"), _dataset(db, "judge-rec-4")
    stub_environment(monkeypatch)
    assert client.put(f"{ROLES}/judge", json={"model_id": KIMI}).status_code == 200
    try:
        first = client.post("/api/eval/runs", json={
            "agent_id": agent.id, "dataset_id": dataset.id, "wait_seconds": 0,
            "evaluators": ["Builtin.Helpfulness"]}).json()
        assert first["judge_model_id"] == KIMI
        wait_terminal(client, first["id"])
    finally:
        assert client.delete(f"{ROLES}/judge").status_code in (200, 204)
    rerun = client.post(f"/api/eval/runs/{first['id']}/rerun", json={})
    assert rerun.status_code == 201, rerun.text
    assert rerun.json()["judge_model_id"] == KIMI  # the role is back to GLM; the rerun is not
    wait_terminal(client, rerun.json()["id"])
    db.close()


def test_an_online_config_pins_its_judge_at_create(client, monkeypatch):
    db = SessionLocal()
    agent = make_online_agent(db)
    stub_online(monkeypatch)
    res = client.post("/api/eval/online", json={
        "agent_id": agent.id, "evaluators": ["Builtin.Helpfulness"]})
    assert res.status_code == 201, res.text
    assert res.json()["judge_model_id"] == GLM
    assert res.json()["judge_inference"]["temperature"] == 0.0
    row = db.query(OnlineEvalConfig).filter_by(config_id=res.json()["config_id"]).one()
    assert row.judge_model_id == GLM
    db.close()


def test_online_dispatch_uses_the_snapshot_and_stamps_the_results():
    db = SessionLocal()
    agent = make_online_agent(db)
    db.add(EvalEvaluator(id="cust_tone_j", workspace_id=DEFAULT_WORKSPACE_ID,
                         name="cust_tone_j", definition="judge", instructions="Rate {context}",
                         rating_scale=[{"label": "good", "value": 1.0}], level="TRACE",
                         model_id=GLM))
    db.commit()
    ledger_row(db, agent, "oe_judge-1", sampling_percentage=100.0, session_timeout_minutes=1,
               evaluators=["Builtin.Helpfulness", "cust_tone_j"], judge_model_id=KIMI,
               judge_inference=judge_inference(KIMI))
    executor, _, kwargs = harness(rows=[scan_row("session-j1", NOW - timedelta(minutes=5))])
    sched.tick(db, NOW, **kwargs)
    (job,) = executor.submitted
    # the hub setting is GLM; the series keeps the judge it was created with
    assert job.spec["judge_model_id"] == KIMI and job.spec["judge_max_tokens"] == 2048

    def rec(evaluator):
        return {"session_id": "session-j1", "trace_id": "t" * 32, "evaluator_id": evaluator,
                "level": "TRACE", "score": 1.0, "label": "good", "explanation": "fine",
                "error_type": None, "error_message": None,
                "id": jobs.record_id(job.job_id, "session-j1", evaluator, 0)}

    executor.complete(job.job_id, [rec("Builtin.Helpfulness"), rec("cust_tone_j")],
                      summary={"scores": []})
    sched.tick(db, NOW + timedelta(minutes=1),
               **{**kwargs, "run_queries": harness(rows=[])[2]["run_queries"]})
    rows = {r.evaluator_id: r for r in db.query(EvalResult).filter_by(config_id="oe_judge-1")}
    assert rows["Builtin.Helpfulness"].judge_model_id == KIMI
    assert rows["Builtin.Helpfulness"].judge_inference["temperature"] == "provider default"
    assert rows["cust_tone_j"].judge_model_id == GLM  # a custom judge keeps its own model
    db.close()
