"""N17-obs449 — the insights FailureAnalysis detector runs on its own model role.

Diagnostic 2026-10-25 (R1 s02 + R1's known wrong answer 84d73d0d…5401): GLM-5.3 reasoned past
16384 output tokens on the detector prompt with no answer; Kimi K3 answered both within ~8k
tokens and flagged the 9-vs-6-days answer (hall-misunderstand / task-instruction
non-compliance). ``failure_detection`` (Settings.eval_failure_detection_model_id, default Kimi
K3) is the detector's judge; every other judge call stays on ``judge`` (GLM-5.3). It is
stamped on the run (``judge_inference.failure_detection``), shipped in the insights job spec
and built by the worker.
"""

from __future__ import annotations

import json

from app.core.db import SessionLocal
from app.services.strands_eval import insights, jobs_batch
from tests.evaluation.fakes import FakeJudge
from tests.evaluation.test_jobs_insights_replay import _job, _spec
from tests.evaluation.test_runs_flow import make_agent, stub_environment, wait_terminal

KIMI, GLM = "global.moonshotai.kimi-k3", "global.zai.glm-5.3"


def test_an_insights_run_records_and_ships_the_failure_detection_judge(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="fd-agent")
    db.close()
    judge = FakeJudge(text='{"errors": []}', by_tool={"SessionSummary": {
        "intent_label": "Leave balance", "intent": "asks", "summary_label": "HR lookup",
        "summary": "looked up"}})
    env, _calls = stub_environment(monkeypatch, judge=judge)
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "mode": "insights", "session_ids": ["s1" + "x" * 32],
        "wait_seconds": 0})
    assert res.status_code == 201, res.text
    run = wait_terminal(client, res.json()["id"])
    (job,) = env.submitted
    assert job.spec["judge_model_id"] == GLM
    assert job.spec["failure_detection_model_id"] == KIMI
    fd = run["judge_inference"]["failure_detection"]
    assert fd["model_id"] == KIMI and fd["max_tokens"] == insights.DETECTION_START_BUDGET
    assert run["judge_inference"]["model_id"] == GLM  # the run's judge is unchanged


def test_an_insights_run_without_failure_analysis_and_a_batch_run_carry_none(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="fd-agent-2")
    db.close()
    env, _calls = stub_environment(monkeypatch)
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "mode": "insights", "insights": ["Builtin.Insight.UserIntent"],
        "session_ids": ["s1" + "x" * 32], "wait_seconds": 0})
    assert res.status_code == 201, res.text
    run = wait_terminal(client, res.json()["id"])
    assert "failure_detection" not in (run["judge_inference"] or {})
    assert "failure_detection_model_id" not in env.submitted[-1].spec
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "evaluators": ["Builtin.Helpfulness"],
        "session_ids": ["s2" + "x" * 32], "wait_seconds": 0})
    assert res.status_code == 201, res.text
    wait_terminal(client, res.json()["id"])
    assert "failure_detection_model_id" not in env.submitted[-1].spec


def test_the_worker_runs_failure_analysis_on_the_detection_judge(monkeypatch):
    seen: list[dict] = []

    def diagnose(provider, session_ids, kinds, judge, *, detection_judge=None, **_kw):
        seen.append({"judge": judge, "detection_judge": detection_judge})
        return {"failures": []}

    monkeypatch.setattr(jobs_batch.insights, "diagnose_sessions", diagnose)
    built: list[str] = []

    def factory(model_id, **_kw):
        built.append(model_id)
        return f"judge:{model_id}"

    env = jobs_batch.WorkerEnv(logs_client=object(), data_client=None, judge_factory=factory,
                               default_model_id="default-judge", spans_source="runtime_logs")

    class Sink:
        job = type("J", (), {"job_id": "j1"})()

        def status(self, *a, **k): ...
        def invocation(self, *a, **k): ...
        def artifact(self, *a, **k): ...
        def finish(self, *a, **k): ...
        def cancelled(self): return False

    spec = {**_spec(replay=False), "judge_model_id": GLM, "failure_detection_model_id": KIMI}
    jobs_batch.run_job(_job(spec), Sink(), env)
    assert seen == [{"judge": f"judge:{GLM}", "detection_judge": f"judge:{KIMI}"}]
    seen.clear()
    jobs_batch.run_job(_job(_spec(replay=False)), Sink(), env)  # an older hub: none named
    assert seen[0]["detection_judge"] is None


def test_diagnose_sessions_sends_only_failure_analysis_to_the_detection_judge(monkeypatch):
    from app.core.db import DEFAULT_WORKSPACE_ID
    from tests.conftest import ws_ctx
    from tests.test_strands_engine import FAILURE_JSON, RCA_JSON, SID, engine_with  # noqa: F401

    summariser = FakeJudge(by_tool={"SessionSummary": {
        "intent_label": "Leave balance", "intent": "asks", "summary_label": "HR lookup",
        "summary": "looked up"}}, model_id=GLM)
    detector = FakeJudge(text=FAILURE_JSON, by_tool={"RCAStructuredOutput": json.loads(RCA_JSON)},
                         model_id=KIMI)
    engine = engine_with(judge=summariser)
    provider = engine.provider(ws_ctx(id=DEFAULT_WORKSPACE_ID, region="cn-northwest-1"), ["g"])
    out = insights.diagnose_sessions(provider, [SID], list(insights.INSIGHT_KINDS), summariser,
                                     detection_judge=detector)
    assert [c["name"] for c in out["failures"]] == ["Tool misuse"]
    assert out["userIntents"][0]["name"] == "Leave balance"
    assert out["failureDetection"][0]["judge"] == KIMI
    detector_tools = {t for c in detector.calls if c["kind"] == "stream" for t in c["tools"]}
    summariser_tools = {t for c in summariser.calls if c["kind"] == "stream" for t in c["tools"]}
    assert not any("SessionSummary" in t for t in detector_tools)
    assert any("SessionSummary" in t for t in summariser_tools)
    assert not any("RCA" in t for t in summariser_tools)
