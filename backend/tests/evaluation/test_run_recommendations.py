"""Recommendations started from an evaluation run (V2 评估任务 → 优化建议) — N12 E2-4.

The run's ``eval_results`` rows are the evidence; the inputs come from the Launchpad
spec or the operator; the provider runs as a worker job submitted through the executor
seam (``recommendation_id`` == job id), and every read of a non-terminal row polls it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.evaluation import jobs
from app.evaluation import recommendations as recs
from app.evaluation.jobs import JobDone, JobStatus
from app.evaluation.models import EvalEvaluator, EvalRecommendation, EvalResult, EvalRun
from app.models.ledger import Agent
from app.optimization.providers import evidence as ev
from tests.evaluation.fakes import FakeExecutor

MODEL = "global.zai.glm-5.3"
SESSIONS = ["sess-0001", "sess-0002", "sess-0003"]
SCORES = {"sess-0001": 0.2, "sess-0002": 0.9, "sess-0003": 0.5}


def _agent(**fields) -> str:
    db = SessionLocal()
    try:
        defaults = {"method": "zip_runtime", "resource_id": "rt-1", "spec": {}}
        agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=fields.pop("name", "rec-agent"),
                      status="active", **{**defaults, **fields})
        db.add(agent)
        db.commit()
        return agent.id
    finally:
        db.close()


def _results(run_id: str, session_ids=SESSIONS, evaluator="Builtin.Correctness",
             scores: dict[str, float] | None = None) -> None:
    db = SessionLocal()
    try:
        for sid in session_ids:
            score = (scores or SCORES).get(sid, 0.5)
            db.add(EvalResult(
                id=jobs.record_id(run_id, sid, evaluator, 0), workspace_id=DEFAULT_WORKSPACE_ID,
                source="run", run_id=run_id, session_id=sid, trace_id="t" * 32,
                evaluator_id=evaluator, level="TRACE", score=score, label="ok",
                explanation=f"because {sid}",
            ))
        db.commit()
    finally:
        db.close()


def _run(agent_id: str = "", *, results: bool = True, **fields) -> str:
    db = SessionLocal()
    try:
        run = EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                      agent_name="rec-agent", **{
                          "mode": "evaluators", "status": "completed",
                          "evaluators": ["Builtin.Correctness"],
                          "session_ids": list(SESSIONS), **fields})
        db.add(run)
        db.commit()
        run_id = run.id
    finally:
        db.close()
    if results and fields.get("session_ids", SESSIONS):
        _results(run_id, fields.get("session_ids", SESSIONS))
    return run_id


def _stub(monkeypatch, executor: FakeExecutor | None = None) -> FakeExecutor:
    """No AWS: the executor seam, inline threads, scripted transcripts / tool evidence."""
    executor = executor or FakeExecutor()
    monkeypatch.setattr(recs, "select_executor", lambda _settings, job_type: executor)
    monkeypatch.setattr(recs, "_spawn", lambda fn: fn())
    monkeypatch.setattr(ev, "default_transcript", lambda db, ws, agent: (
        lambda sid: [{"role": "USER", "text": f"q {sid}", "at": "2026-01-01T00:00:00"},
                     {"role": "ASSISTANT", "text": f"a {sid}", "at": "2026-01-01T00:00:05"}]))
    monkeypatch.setattr(ev, "default_tool_turns", lambda ws, agent, started: (
        lambda sid: [{"role": "TOOL_CALL", "id": "c1", "name": "lookup", "input": {"q": 1}},
                     {"role": "TOOL_RESULT", "id": "c1", "name": "lookup", "status": "success",
                      "text": "found"}]))
    monkeypatch.setattr(ev, "default_tool_spans", lambda ws, since: (lambda sids: {}))
    recs._live_provider_jobs.clear()
    return executor


def _rows(client, run_id):
    return client.get(f"/api/eval/runs/{run_id}/recommendations").json()["recommendations"]


# ─── inputs ─────────────────────────────────────────────────────────────────
def test_runtime_agent_inputs_come_from_the_spec(client, monkeypatch):
    agent_id = _agent(method="zip_runtime", resource_id="rt-1", spec={
        "system_prompt": "Be concise.",
        "tools": [{"type": "builtin", "name": "calculator", "description": "Do math."}],
    })
    _stub(monkeypatch)
    body = client.get(f"/api/eval/runs/{_run(agent_id)}/recommendation-inputs").json()
    assert body["source"] == "spec"
    assert body["system_prompt"] == "Be concise."
    assert [t["name"] for t in body["tools"]] == ["calculator"]
    assert body["eligible"] is True and body["tools_eligible"] is True
    assert body["agent_method"] == "zip_runtime"


def test_byoc_and_log_source_runs_need_manual_inputs(client, monkeypatch):
    _stub(monkeypatch)
    byoc = _agent(method="byoc", resource_id="rt-9", spec={"name": "byo"})
    for run_id in (_run(byoc), _run("", log_source={"service_name": "x.DEFAULT",
                                                     "log_group_names": ["/g"]})):
        body = client.get(f"/api/eval/runs/{run_id}/recommendation-inputs").json()
        assert body["source"] == "manual"
        assert body["system_prompt"] == "" and body["tools"] == []


def _judge(db, evaluator_id, *, name=None, instructions="Rate the answer.", scale=None,
           status="ACTIVE", definition="judge"):
    row = EvalEvaluator(
        id=evaluator_id, workspace_id=DEFAULT_WORKSPACE_ID, name=name or evaluator_id,
        definition=definition, instructions=instructions,
        rating_scale=scale if scale is not None else [{"label": "pass", "value": 1.0}],
        level="TRACE", status=status,
    )
    db.add(row)
    db.commit()


def _seed_custom_evaluators():
    db = SessionLocal()
    _judge(db, "judge-num", name="tone")
    _judge(db, "judge-cat", scale=[{"label": "pass"}, {"label": "fail"}])
    _judge(db, "judge-gt", instructions="Compare with {expected_response}.")
    _judge(db, "judge-new", status="CREATING")
    _judge(db, "judge-code", definition="code", instructions=None, scale=[])
    db.close()


def test_every_usable_evaluator_is_offered_grouped_and_recommended(client, monkeypatch):
    _stub(monkeypatch)
    _seed_custom_evaluators()
    run_id = _run("", evaluators=["Builtin.Refusal", "judge-num", "Builtin.Correctness",
                                  "judge-gt", "judge-deleted"])

    body = client.get(f"/api/eval/runs/{run_id}/recommendation-inputs").json()
    options = {o["id"]: o for o in body["evaluators"]}
    order = [o["id"] for o in body["evaluators"]]

    # the run's own usable evaluators lead, then the two recommended targets
    assert order[:4] == ["judge-num", "Builtin.Correctness",
                         "Builtin.GoalSuccessRate", "Builtin.Helpfulness"]
    assert {o["id"] for o in body["evaluators"] if o["recommended"]} == {
        "Builtin.GoalSuccessRate", "Builtin.Helpfulness"}
    assert options["judge-num"]["group"] == "run" and options["judge-num"]["name"] == "tone"
    assert options["Builtin.Faithfulness"]["group"] == "builtin"
    assert options["judge-code"]["group"] == "custom"
    assert body["default_evaluator"] == "Builtin.GoalSuccessRate"
    # nothing that cannot produce a usable signal from traces alone
    assert {e["id"]: e["reason"] for e in body["excluded_evaluators"]} == {
        "judge-deleted": "unavailable",
        "judge-gt": "ground_truth",
        "Builtin.Refusal": "lower_is_better",
        "Builtin.TrajectoryExactOrderMatch": "ground_truth",
        "Builtin.TrajectoryInOrderMatch": "ground_truth",
        "Builtin.TrajectoryAnyOrderMatch": "ground_truth",
        "judge-cat": "categorical",
    }
    assert "judge-new" not in options  # not ACTIVE


def test_an_insights_run_still_offers_every_evaluator(client, monkeypatch):
    _stub(monkeypatch)
    _seed_custom_evaluators()
    run_id = _run("", mode="insights", evaluators=["Builtin.Insight.FailureAnalysis"])
    body = client.get(f"/api/eval/runs/{run_id}/recommendation-inputs").json()
    ids = [o["id"] for o in body["evaluators"]]
    assert ids[:2] == ["Builtin.GoalSuccessRate", "Builtin.Helpfulness"]
    assert "judge-num" in ids and "Builtin.Faithfulness" in ids
    assert "run" not in {o["group"] for o in body["evaluators"]}


@pytest.mark.parametrize(("evaluator", "code", "status"), [
    ("Builtin.Refusal", "recommendation.evaluator_lower_is_better", 422),
    ("Builtin.TrajectoryInOrderMatch", "recommendation.evaluator_ground_truth", 422),
    ("judge-gt", "recommendation.evaluator_ground_truth", 422),
    ("judge-cat", "recommendation.evaluator_categorical", 422),
    ("judge-ghost", "recommendation.evaluator_unknown", 400),
    ("Builtin.Nope", "recommendation.evaluator_unknown", 400),
])
def test_start_refuses_what_the_options_hide(client, monkeypatch, evaluator, code, status):
    # N17-obs453: only a provider that optimises toward a target evaluator takes one
    executor = _stub(monkeypatch)
    monkeypatch.setattr(get_settings(), "prompt_opt_harness_optimizer_enabled", True)
    _seed_custom_evaluators()
    res = client.post(f"/api/eval/runs/{_run(_agent())}/recommendations", json={
        "kinds": ["system_prompt"], "system_prompt": "x", "evaluator": evaluator,
        "provider": "harness_optimizer"})
    assert res.status_code == status
    assert res.json()["code"] == code
    assert executor.submitted == []


# ─── start ──────────────────────────────────────────────────────────────────
def test_start_submits_one_worker_job_per_kind(client, monkeypatch):
    executor = _stub(monkeypatch)
    agent_id = _agent(method="zip_runtime", resource_id="rt-1", spec={"system_prompt": "Help."})
    run_id = _run(agent_id)
    _results(run_id, ["sess-0001"], evaluator="Builtin.Helpfulness", scores={"sess-0001": 1.0})
    # another run's rows must never leak into this run's evidence
    _results(_run(agent_id, results=False), ["sess-0009"])

    res = client.post(f"/api/eval/runs/{run_id}/recommendations", json={
        "kinds": ["system_prompt", "tool_descriptions"], "input_source": "manual",
        "system_prompt": "  Help users.  ", "evaluator": "Builtin.Helpfulness",
        "tools": [{"name": "lookup", "description": "Find an order."}],
    })
    assert res.status_code == 201, res.text
    rows = res.json()["recommendations"]
    assert [r["kind"] for r in rows] == ["system_prompt", "tool_descriptions"]
    assert all(r["status"] == "IN_PROGRESS" for r in rows)
    assert all(r["recommendation_id"].startswith("provider-gepa_lite-") for r in rows)
    assert all(r["name"] == f"gepa_lite · {MODEL}" for r in rows)
    assert rows[0]["system_prompt"] == "Help users."
    # N17-obs453: gepa_lite reads every evaluator of the run and takes no target — the sent
    # evaluator is not stored as if it had been optimised against
    assert rows[0]["evaluator"] is None and rows[0]["optimized_toward"] is None
    assert rows[1]["tools"] == {"lookup": "Find an order."} and rows[1]["evaluator"] is None
    assert rows[0]["result"] == {"provider": "gepa_lite", "provider_model_id": MODEL}
    # the background thread (inline here) recorded the submission on the row
    assert _rows(client, run_id)[0]["result"]["progress"] == "submitted to the evaluation worker"

    assert [j.job_type for j in executor.submitted] == ["recommend", "recommend"]
    assert [j.job_id for j in executor.submitted] == [r["recommendation_id"] for r in rows]
    sp, td = (j.spec for j in executor.submitted)
    assert sp["provider"] == "gepa_lite" and sp["model_id"] == MODEL
    assert sp["components"] == ["system_prompt"] and sp["current_prompt"] == "Help users."
    assert sp["max_chars"] == recs.PROMPT_MAX_CHARS
    assert sp["max_tokens"] == get_settings().prompt_opt_max_tokens
    assert sp["trace_source"] == {"kind": "eval_results", "run_id": run_id}
    # evidence: this run's eval_results only, worst session first, transcripts attached
    # sess-0001 = mean(0.2, 1.0) = 0.6 → worst first is 0003 (0.5), 0001 (0.6), 0002 (0.9)
    assert [e["session_id"] for e in sp["evidence"]] == ["sess-0003", "sess-0001", "sess-0002"]
    assert sp["stats"]["records"] == 4 and sp["stats"]["sessions_scored"] == 3
    by_sid = {e["session_id"]: e for e in sp["evidence"]}
    assert {r["evaluator_id"] for r in by_sid["sess-0001"]["records"]} == {
        "Builtin.Correctness", "Builtin.Helpfulness"}
    assert [t["role"] for t in by_sid["sess-0001"]["turns"]] == ["user", "assistant"]
    assert td["components"] == ["tool_descriptions"]
    assert td["tools"] == {"lookup": "Find an order."}
    assert td["stats"]["tool_calls_seen"] == 3  # the stubbed content-log turn per session
    assert [t["role"] for t in td["evidence"][0]["turns"]] == [
        "user", "assistant", "tool_call", "tool_result"]
    job = executor.submitted[0]
    assert job.sink["prefix"] == jobs.job_prefix(job.job_id, 1)
    assert job.workspace["region"]


@pytest.mark.parametrize(("payload", "code"), [
    ({"kinds": ["system_prompt"], "system_prompt": "   "}, "recommendation.system_prompt_required"),
    ({"kinds": ["tool_descriptions"], "tools": []}, "recommendation.tools_required"),
    ({"kinds": ["tool_descriptions"], "tools": [{"name": "a", "description": ""}]},
     "recommendation.tool_description_required"),
])
def test_missing_inputs_are_refused_before_any_job(client, monkeypatch, payload, code):
    executor = _stub(monkeypatch)
    res = client.post(f"/api/eval/runs/{_run(_agent())}/recommendations", json=payload)
    assert res.status_code == 422
    assert res.json()["code"] == code
    assert executor.submitted == []


def test_an_unfinished_run_or_one_without_results_cannot_seed_a_recommendation(
    client, monkeypatch,
):
    executor = _stub(monkeypatch)
    res = client.post(f"/api/eval/runs/{_run(_agent(), status='evaluating')}/recommendations",
                      json={"kinds": ["system_prompt"], "system_prompt": "x"})
    assert res.status_code == 409 and res.json()["code"] == "recommendation.run_not_completed"
    bare = _run(_agent(), results=False)
    res = client.post(f"/api/eval/runs/{bare}/recommendations",
                      json={"kinds": ["system_prompt"], "system_prompt": "x"})
    assert res.status_code == 409 and res.json()["code"] == "recommendation.run_no_batch"
    inputs = client.get(f"/api/eval/runs/{bare}/recommendation-inputs").json()
    assert inputs["eligible"] is False and inputs["reason_code"] == "run_no_batch"
    assert executor.submitted == []


def test_provider_gating(client, monkeypatch):
    executor = _stub(monkeypatch)
    run_id = _run(_agent())
    base = {"kinds": ["system_prompt"], "system_prompt": "x"}
    res = client.post(f"/api/eval/runs/{run_id}/recommendations",
                      json={**base, "provider": "mystery"})
    assert res.status_code == 422  # the request Literal refuses it
    res = client.post(f"/api/eval/runs/{run_id}/recommendations",
                      json={**base, "provider": "harness_optimizer"})
    assert res.status_code == 422
    assert res.json()["code"] == "recommendation.provider_disabled"
    monkeypatch.setattr(get_settings(), "prompt_opt_harness_optimizer_enabled", True)
    res = client.post(f"/api/eval/runs/{run_id}/recommendations", json={
        "kinds": ["tool_descriptions"], "tools": [{"name": "t", "description": "d"}],
        "provider": "harness_optimizer"})
    assert res.status_code == 422
    assert res.json()["code"] == "recommendation.kind_unsupported_by_provider"
    res = client.post(f"/api/eval/runs/{run_id}/recommendations",
                      json={**base, "provider": "harness_optimizer"})
    assert res.status_code == 201, res.text
    assert executor.submitted[-1].job_type == "optimize"
    assert executor.submitted[-1].spec["provider"] == "harness_optimizer"
    assert executor.submitted[-1].spec["epochs"] == get_settings().prompt_opt_epochs
    res = client.post(f"/api/eval/runs/{run_id}/recommendations",
                      json={**base, "model_id": "global.anthropic.claude-opus-5"})
    assert res.status_code == 422 and res.json()["code"] == "model.not_allowed"
    res = client.post(f"/api/eval/runs/{run_id}/recommendations",
                      json={**base, "model_id": "global.moonshotai.kimi-k3"})
    assert res.status_code == 201
    assert executor.submitted[-1].spec["model_id"] == "global.moonshotai.kimi-k3"


def test_a_recommendation_needs_an_agent_run(client, monkeypatch):
    _stub(monkeypatch)
    res = client.post(f"/api/eval/runs/{_run('')}/recommendations",
                      json={"kinds": ["system_prompt"], "system_prompt": "Old."})
    assert res.status_code == 422
    assert res.json()["code"] == "recommendation.provider_needs_agent"


def test_a_missing_worker_is_refused_before_any_row_exists(client, monkeypatch):
    _stub(monkeypatch)

    def no_worker(_settings, job_type):
        raise AppError("evaluation.worker_missing", "no worker", status_code=503)

    monkeypatch.setattr(recs, "select_executor", no_worker)
    run_id = _run(_agent())
    res = client.post(f"/api/eval/runs/{run_id}/recommendations",
                      json={"kinds": ["system_prompt"], "system_prompt": "Old."})
    assert res.status_code == 503 and res.json()["code"] == "evaluation.worker_missing"
    assert _rows(client, run_id) == []


def test_an_evidence_failure_lands_on_the_row(client, monkeypatch):
    executor = _stub(monkeypatch)

    def boom(db, ws, agent):
        raise RuntimeError("memory down")

    monkeypatch.setattr(ev, "default_transcript", boom)
    run_id = _run(_agent())
    client.post(f"/api/eval/runs/{run_id}/recommendations",
                json={"kinds": ["system_prompt"], "system_prompt": "Old."})
    row = _rows(client, run_id)[0]
    assert row["status"] == "FAILED" and "memory down" in row["error"]
    assert executor.submitted == []


# ─── refresh ────────────────────────────────────────────────────────────────
def _started(client, monkeypatch, kinds=("system_prompt",), **extra):
    executor = _stub(monkeypatch)
    run_id = _run(_agent())
    body = {"kinds": list(kinds), "system_prompt": "Old.",
            "tools": [{"name": "lookup", "description": "d"}], **extra}
    res = client.post(f"/api/eval/runs/{run_id}/recommendations", json=body)
    assert res.status_code == 201, res.text
    return executor, run_id, res.json()["recommendations"]


def test_reading_mirrors_worker_progress_until_done(client, monkeypatch):
    executor, run_id, (row,) = _started(client, monkeypatch)
    job_id = row["recommendation_id"]
    executor.statuses[job_id] = JobStatus(
        phase="evaluating", progress={"done": 0, "total": 1, "message": "reflecting…"})
    listed = _rows(client, run_id)[0]
    assert listed["status"] == "IN_PROGRESS" and listed["result"]["progress"] == "reflecting…"
    executor.complete(job_id, [], summary={"recommendation": {
        "status": "COMPLETED", "recommended_prompt": "New.", "explanation": "Sharper.",
        "meta": {"calls": 1, "evidence_sessions": 3}}})
    listed = _rows(client, run_id)[0]
    assert listed["status"] == "COMPLETED" and listed["error"] is None
    assert listed["result"] == {"provider": "gepa_lite", "provider_model_id": MODEL,
                                "recommended_prompt": "New.", "explanation": "Sharper.",
                                "provider_meta": {"calls": 1, "evidence_sessions": 3}}
    # terminal: the executor is never consulted again
    executor.dones.clear()
    executor.statuses.clear()
    assert _rows(client, run_id)[0]["status"] == "COMPLETED"


def test_a_completed_job_without_text_reads_as_failed(client, monkeypatch):
    executor, run_id, (row,) = _started(client, monkeypatch)
    executor.complete(row["recommendation_id"], [], summary={"recommendation": {
        "status": "FAILED", "error": "no scored sessions in the selected run", "meta": {}}})
    listed = _rows(client, run_id)[0]
    assert listed["status"] == "FAILED"
    assert listed["error"] == "no scored sessions in the selected run"
    assert "recommended_prompt" not in listed["result"]


def test_a_failed_worker_job_reads_as_failed(client, monkeypatch):
    executor, run_id, (row,) = _started(client, monkeypatch)
    executor.dones[row["recommendation_id"]] = JobDone(state="FAILED",
                                                      error="RuntimeError: bearer missing")
    listed = _rows(client, run_id)[0]
    assert listed["status"] == "FAILED" and "bearer missing" in listed["error"]


def test_tool_recommendations_are_mapped_per_tool(client, monkeypatch):
    executor, run_id, (row,) = _started(client, monkeypatch, kinds=("tool_descriptions",))
    executor.complete(row["recommendation_id"], [], summary={"recommendation": {
        "status": "FAILED", "error": "instruction frozen — tool descriptions only",
        "tool_status": "COMPLETED", "tool_descriptions": {"lookup": "Better d."},
        "tool_explanation": "- clearer", "meta": {"tool_calls_seen": 3}}})
    listed = _rows(client, run_id)[0]
    assert listed["status"] == "COMPLETED"
    assert listed["result"]["tools"] == {"lookup": {"description": "Better d.",
                                                    "explanation": "- clearer"}}
    assert listed["result"]["provider_meta"] == {"tool_calls_seen": 3}


def test_tool_side_settled_by_the_precheck_reads_as_failed(client, monkeypatch):
    executor, run_id, (row,) = _started(client, monkeypatch, kinds=("tool_descriptions",))
    executor.complete(row["recommendation_id"], [], summary={"recommendation": {
        "status": "FAILED", "tool_status": "no-tool-calls",
        "tool_error": "no tool calls in the selected run", "tool_descriptions": {}}})
    listed = _rows(client, run_id)[0]
    assert listed["status"] == "FAILED" and "no tool calls" in listed["error"]


def test_a_stale_worker_job_reads_as_lost(client, monkeypatch):
    executor, run_id, (row,) = _started(client, monkeypatch)
    stale_s = get_settings().eval_job_stale_s
    old = (datetime.now(UTC) - timedelta(seconds=stale_s + 60)).isoformat()
    executor.statuses[row["recommendation_id"]] = JobStatus(phase="evaluating", updated_at=old)
    listed = _rows(client, run_id)[0]
    assert listed["status"] == "FAILED" and "lost" in listed["error"]


def test_a_row_left_running_by_a_restart_reads_as_interrupted(client, monkeypatch):
    _stub(monkeypatch)
    run_id = _run(_agent())
    db = SessionLocal()
    db.add(EvalRecommendation(
        workspace_id=DEFAULT_WORKSPACE_ID, run_id=run_id, name="gepa_lite · m",
        recommendation_id="provider-gepa_lite-abcdef012345", kind="system_prompt",
        status="IN_PROGRESS", result={"provider": "gepa_lite"}))
    db.add(EvalRecommendation(
        workspace_id=DEFAULT_WORKSPACE_ID, run_id=run_id, name="rec_sp",
        recommendation_id="rec-aws-legacy", kind="system_prompt", status="PENDING"))
    db.commit()
    db.close()
    rows = {r["recommendation_id"]: r for r in _rows(client, run_id)}
    assert rows["provider-gepa_lite-abcdef012345"]["status"] == "FAILED"
    assert "interrupted" in rows["provider-gepa_lite-abcdef012345"]["error"]
    assert rows["rec-aws-legacy"]["status"] == "FAILED"
    assert "Global AgentCore job" in rows["rec-aws-legacy"]["error"]


def test_a_live_row_without_worker_status_yet_stays_in_progress(client, monkeypatch):
    executor, run_id, (row,) = _started(client, monkeypatch)
    executor.statuses.clear()  # the worker has not written status.json yet (cold start)
    assert _rows(client, run_id)[0]["status"] == "IN_PROGRESS"


def test_another_workspaces_run_is_not_found(client, monkeypatch):
    _stub(monkeypatch)
    db = SessionLocal()
    run = EvalRun(workspace_id="other-ws", agent_id="", agent_name="x", status="completed")
    db.add(run)
    db.commit()
    run_id = run.id
    db.close()
    assert client.get(f"/api/eval/runs/{run_id}/recommendations").status_code == 404
    assert client.post(f"/api/eval/runs/{run_id}/recommendations",
                       json={"kinds": ["system_prompt"], "system_prompt": "x"}
                       ).status_code == 404


# ─── accept → a new Harness version ─────────────────────────────────────────
def _harness_agent(**fields) -> str:
    from app.schemas.agent import AgentSpec

    spec = AgentSpec(name="rec-agent", method="harness", system_prompt="old prompt").model_dump()
    return _agent(method="harness", spec=spec, version="1",
                  arn="arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/rec_agent-abcdefghij",
                  resource_id="rec_agent-abcdefghij", **fields)


def _rec(run_id: str, **fields) -> str:
    db = SessionLocal()
    try:
        row = EvalRecommendation(workspace_id=DEFAULT_WORKSPACE_ID, run_id=run_id,
                                 name="gepa_lite · m", **{
                                     "recommendation_id": "provider-gepa_lite-aaaaaaaaaaaa",
                                     "kind": "system_prompt", "status": "COMPLETED",
                                     "result": {"recommended_prompt": "new prompt"},
                                     **fields})
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _no_deploy(monkeypatch) -> list[str]:
    started: list[str] = []
    monkeypatch.setattr("app.routers.agents.start_deploy_async", started.append)
    return started


def test_accept_republishes_the_harness_with_the_recommended_prompt(client, monkeypatch):
    _stub(monkeypatch)
    started = _no_deploy(monkeypatch)
    agent_id = _harness_agent()
    run_id = _run(agent_id)
    rec_id = _rec(run_id)

    res = client.post(f"/api/eval/runs/{run_id}/recommendations/{rec_id}/accept")

    assert res.status_code == 202, res.text
    body = res.json()
    assert started == [body["job_id"]]
    accepted = body["recommendation"]["accepted"]
    assert accepted["agent_id"] == agent_id
    assert accepted["previous_version"] == "1"
    assert accepted["job_id"] == body["job_id"]
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        assert agent.spec["system_prompt"] == "new prompt"
        assert agent.status == "deploying"
    finally:
        db.close()
    again = client.post(f"/api/eval/runs/{run_id}/recommendations/{rec_id}/accept")
    assert again.status_code == 409
    assert again.json()["code"] == "recommendation.already_accepted"
    assert len(started) == 1


def test_accept_drops_the_kb_section_the_deployer_appends(client, monkeypatch):
    """The recommendation revised the LIVE prompt (spec prompt + the platform's KB
    section); storing that verbatim made the next deploy append the section twice."""
    from app.schemas.agent import AgentSpec
    from app.templates.kb_support import kb_prompt_section, mounted_kbs

    _stub(monkeypatch)
    _no_deploy(monkeypatch)
    from app.models.ledger import KnowledgeBase

    spec = AgentSpec(name="rec-agent", method="harness", system_prompt="old prompt",
                     knowledge_bases=[{"kb_id": "KB12345678", "name": "earnings"}])
    db = SessionLocal()  # round 16: a re-publish refuses a KB this workspace has no row for
    try:
        db.add(KnowledgeBase(kb_id="KB12345678", workspace_id=DEFAULT_WORKSPACE_ID,
                             name="earnings", status="ACTIVE"))
        db.commit()
    finally:
        db.close()
    agent_id = _agent(method="harness", spec=spec.model_dump(), version="1",
                      arn="arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/rec_agent-abc",
                      resource_id="rec_agent-abcdefghij")
    run_id = _run(agent_id)
    live = "revised prompt" + kb_prompt_section(mounted_kbs(spec))
    rec_id = _rec(run_id, result={"recommended_prompt": live})

    res = client.post(f"/api/eval/runs/{run_id}/recommendations/{rec_id}/accept")

    assert res.status_code == 202, res.text
    db = SessionLocal()
    try:
        assert db.get(Agent, agent_id).spec["system_prompt"] == "revised prompt"
    finally:
        db.close()


def test_strip_generated_prompt_tolerates_a_reflowed_kb_section():
    from app.evaluation.generated_prompt import strip_generated_prompt
    from app.schemas.agent import AgentSpec

    spec = AgentSpec(name="rec-agent", method="harness", system_prompt="p",
                     knowledge_bases=[{"kb_id": "KB12345678", "name": "earnings"}])
    reflowed = ("keep me\n## Knowledge bases\nTwo retrieval tools are mounted for you. "
                "(edited)\n- earnings …\nGround answers on the retrieved passages and cite "
                "their sources; say so explicitly when retrieval returns nothing relevant."
                "\nand this tail")
    assert strip_generated_prompt(spec, reflowed) == "keep me\nand this tail"
    assert strip_generated_prompt(spec, "no section") == "no section"


@pytest.mark.parametrize(("rec_fields", "code", "status"), [
    ({"kind": "tool_descriptions", "result": {"tools": {}}}, "recommendation.accept_kind", 400),
    ({"status": "IN_PROGRESS", "result": {}}, "recommendation.not_completed", 409),
    ({"result": {"recommended_prompt": "  "}}, "recommendation.not_completed", 409),
])
def test_accept_refuses_what_cannot_become_a_version(client, monkeypatch, rec_fields, code,
                                                     status):
    _stub(monkeypatch)
    started = _no_deploy(monkeypatch)
    run_id = _run(_harness_agent())
    rec_id = _rec(run_id, **rec_fields)

    res = client.post(f"/api/eval/runs/{run_id}/recommendations/{rec_id}/accept")

    assert res.status_code == status
    assert res.json()["code"] == code
    assert started == []


# ─── N12-obs44: accept is gated on ONE predicate, shared with the console ───────
def _spec_agent(method: str, **fields) -> str:
    from app.schemas.agent import AgentSpec

    spec = AgentSpec(name="rec-agent", method=method, system_prompt="old prompt").model_dump()
    return _agent(method=method, spec={**spec, **fields.pop("spec_extra", {})}, version="1",
                  resource_id="rec_agent-abcdefghij", **fields)


def test_accept_republishes_a_strands_zip_runtime_agent_too(client, monkeypatch):
    # live (obs44): the V2 card hid ACCEPT for a zip_runtime agent although the Strands
    # template takes its prompt from the spec exactly like the Harness does
    _stub(monkeypatch)
    started = _no_deploy(monkeypatch)
    agent_id = _spec_agent("zip_runtime")
    run_id = _run(agent_id)
    inputs = client.get(f"/api/eval/runs/{run_id}/recommendation-inputs").json()
    assert inputs["agent_method"] == "zip_runtime"
    assert inputs["accept_eligible"] is True and inputs["accept_ineligible_reason"] is None

    res = client.post(f"/api/eval/runs/{run_id}/recommendations/{_rec(run_id)}/accept")

    assert res.status_code == 202, res.text
    assert started == [res.json()["job_id"]]
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        assert agent.spec["system_prompt"] == "new prompt" and agent.status == "deploying"
    finally:
        db.close()


@pytest.mark.parametrize(("agent_fields", "reason"), [
    ({"method": "studio", "spec": {"system_prompt": "x", "code": "print(1)"}}, "method"),
    ({"method": "byoc", "spec": {"name": "byo"}}, "method"),
    ({"method": "discovered_runtime", "spec": {}}, "method"),
    ({"method": "harness", "spec": {"system_prompt": "x"}, "system_key": "frontdesk"},
     "system_preset"),
    ({"method": "zip_runtime", "spec": {"system_prompt": "x", "code_bundle": {"key": "k"}}},
     "converted_locked"),
])
def test_accept_refuses_an_agent_whose_prompt_a_republish_would_not_change(
    client, monkeypatch, agent_fields, reason,
):
    _stub(monkeypatch)
    started = _no_deploy(monkeypatch)
    run_id = _run(_agent(**agent_fields))
    # the read model the console gates the button on says the same thing, up front
    inputs = client.get(f"/api/eval/runs/{run_id}/recommendation-inputs").json()
    assert inputs["accept_eligible"] is False
    assert inputs["accept_ineligible_reason"] == reason

    res = client.post(f"/api/eval/runs/{run_id}/recommendations/{_rec(run_id)}/accept")

    assert res.status_code == 400
    assert res.json()["code"] == "recommendation.accept_not_harness"
    assert res.json()["detail"]["reason"] == reason
    assert started == []


def test_prompt_accept_ineligibility_mirrors_the_republish_guards():
    from types import SimpleNamespace as A

    assert recs.prompt_accept_ineligibility(None) == "agent_missing"
    for method in sorted(recs.PROMPT_REPUBLISHABLE_METHODS):
        assert recs.prompt_accept_ineligibility(
            A(method=method, system_key=None, spec={"system_prompt": "p"})) is None
    assert recs.PROMPT_REPUBLISHABLE_METHODS == {"harness", "zip_runtime"}
    assert recs.prompt_accept_ineligibility(
        A(method="studio", system_key=None, spec={})) == "method"
    assert recs.prompt_accept_ineligibility(
        A(method="harness", system_key="preset", spec={})) == "system_preset"
    assert recs.prompt_accept_ineligibility(
        A(method="zip_runtime", system_key=None, spec={"code_bundle": {"key": "k"}})
    ) == "converted_locked"
    # method is checked first: a system preset of an ineligible method is "method"
    assert recs.prompt_accept_ineligibility(A(method="byoc", system_key="p", spec={})) == "method"


def test_recommendation_inputs_without_an_agent_are_not_acceptable(client, monkeypatch):
    _stub(monkeypatch)
    inputs = client.get(f"/api/eval/runs/{_run('')}/recommendation-inputs").json()
    assert inputs["agent_method"] is None
    assert inputs["accept_eligible"] is False
    assert inputs["accept_ineligible_reason"] == "agent_missing"


def test_accept_of_another_runs_recommendation_is_not_found(client, monkeypatch):
    _stub(monkeypatch)
    _no_deploy(monkeypatch)
    agent_id = _harness_agent()
    rec_id = _rec(_run(agent_id))

    res = client.post(f"/api/eval/runs/{_run(agent_id)}/recommendations/{rec_id}/accept")

    assert res.status_code == 404


def test_accept_publishes_the_reviewed_prompt_and_records_the_edit(client, monkeypatch):
    _stub(monkeypatch)
    started = _no_deploy(monkeypatch)
    agent_id = _harness_agent()
    run_id = _run(agent_id)
    rec_id = _rec(run_id)

    res = client.post(f"/api/eval/runs/{run_id}/recommendations/{rec_id}/accept",
                      json={"system_prompt": "  new prompt, minus the loosened rule  "})

    assert res.status_code == 202, res.text
    assert res.json()["recommendation"]["accepted"]["edited"] is True
    assert len(started) == 1
    db = SessionLocal()
    try:
        published = db.get(Agent, agent_id).spec["system_prompt"]
        assert published == "new prompt, minus the loosened rule"
    finally:
        db.close()


def test_accepting_the_unchanged_text_is_not_an_edit(client, monkeypatch):
    _stub(monkeypatch)
    _no_deploy(monkeypatch)
    run_id = _run(_harness_agent())
    rec_id = _rec(run_id)

    res = client.post(f"/api/eval/runs/{run_id}/recommendations/{rec_id}/accept",
                      json={"system_prompt": "new prompt"})

    assert res.status_code == 202, res.text
    assert res.json()["recommendation"]["accepted"]["edited"] is False


def test_an_empty_reviewed_prompt_is_refused(client, monkeypatch):
    _stub(monkeypatch)
    started = _no_deploy(monkeypatch)
    run_id = _run(_harness_agent())
    rec_id = _rec(run_id)

    res = client.post(f"/api/eval/runs/{run_id}/recommendations/{rec_id}/accept",
                      json={"system_prompt": "   "})

    assert res.status_code == 422
    assert res.json()["code"] == "recommendation.accept_prompt_empty"
    assert started == []


@pytest.mark.siliconflow
def test_an_unconfigured_provider_model_is_refused_before_the_job_is_queued(client,
                                                                            monkeypatch):
    """N-R6-obs1: the optimizer would only fail inside the worker without the key."""
    sf = "siliconflow.zai-org/GLM-5.3"
    monkeypatch.delenv("LAUNCHPAD_SILICONFLOW_API_KEY", raising=False)
    executor = _stub(monkeypatch)
    run_id = _run(_agent())
    base = {"kinds": ["system_prompt"], "system_prompt": "x"}
    res = client.post(f"/api/eval/runs/{run_id}/recommendations", json={**base, "model_id": sf})
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "model.provider_unconfigured"
    assert "launchpad-siliconflow-api-key" in res.json()["message"]
    assert executor.submitted == []
    # the discovery payload says which models the picker must disable
    providers = {p["id"]: p
                 for p in client.get("/api/eval/recommendation-providers").json()["providers"]}
    for prov in providers.values():
        for m in prov["models"]:
            assert m["available"] is (not m["model_id"].startswith("siliconflow."))
    monkeypatch.setenv("LAUNCHPAD_SILICONFLOW_API_KEY", "k")
    res = client.post(f"/api/eval/runs/{run_id}/recommendations", json={**base, "model_id": sf})
    assert res.status_code == 201, res.text
    assert executor.submitted[-1].spec["model_id"] == sf
