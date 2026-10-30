"""Runs flow on the evaluation worker (N12 E1-4).

The run queue dispatches one job to the executor and mirrors its artefacts onto the row:
``queued → invoking → waiting → evaluating → completed|failed|stopped``. Hermetic: the
in-process executor runs the worker's own dispatcher (``jobs_batch.run_job``) against a
recording invoker, a Logs double that answers every session query with the span fixture
rewritten for that session, and a schema-driven ``FakeJudge``.
"""

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import app.evaluation.service as svc
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation.executor import InProcessExecutor
from app.evaluation.models import EvalDataset, EvalResult, EvalRun
from app.models.ledger import Agent
from app.services.strands_eval import jobs_batch
from app.services.strands_eval.sink import MemoryStore
from tests.evaluation.fakes import FakeJudge, FakeLogs

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "spans_session.json").read_text()
)
GROUP = "/aws/bedrock-agentcore/runtimes/rt-1-DEFAULT"


def make_agent(db, name="eval-agent", method="zip_runtime") -> Agent:
    agent = Agent(
        workspace_id=DEFAULT_WORKSPACE_ID, name=name, method=method, status="active",
        arn="arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/rt-1",
        resource_id="rt-1", spec={"name": name},
    )
    db.add(agent)
    db.commit()
    return agent


class DynamicFakeLogs(FakeLogs):
    """Answers every ``… = '<sid>' …`` Insights query with the fixture's records
    rewritten for that session id (the run's ids are minted at plan time)."""

    def __init__(self, missing: set[str] | None = None) -> None:
        super().__init__({})
        self.missing = set(missing or ())

    def start_query(self, **kwargs):
        self.started.append(kwargs)
        query = kwargs.get("queryString", "")
        qid = f"q{len(self.started)}"
        rows: list[dict] = []
        if "= '" in query and "parentSpanId" not in query:
            sid = query.split("= '", 1)[1].split("'", 1)[0]
            if sid not in self.missing:
                rows = [{"@message": json.dumps({
                    **r, "attributes": {**r["attributes"], "session.id": sid}})}
                    for r in FIXTURE["records"]]
        self._results[qid] = rows
        return {"queryId": qid}


class Env(SimpleNamespace):
    """What ``stub_environment`` hands a test: the executor, its store, the recorders."""


def stub_environment(monkeypatch, *, invoke_errors=None, judge=None, telemetry_error=None):
    """Wire the hermetic worker behind the run service.

    Returns ``(env, calls)`` — ``calls["n"]`` counts replay invocations, ``calls["prompts"]``
    / ``calls["sessions"]`` record them; ``env.submitted`` holds every JobSpec dispatched,
    ``env.telemetry_waits`` every readiness wait, ``env.executor`` / ``env.store`` the
    in-process executor and its artefact store."""
    calls: dict = {"n": 0, "prompts": [], "sessions": [], "specs": []}
    errors = list(invoke_errors or [])

    def invoke(prompt, session_id, session_spec):
        calls["n"] += 1
        calls["prompts"].append(prompt)
        calls["sessions"].append(session_id)
        calls["specs"].append(session_spec)
        if errors:
            raise errors.pop(0)
        return {"text": "42", "session_id": session_id}

    judge = judge or FakeJudge(overrides={"score": "Very helpful", "reasoning": "clear"})
    logs = DynamicFakeLogs()
    worker_env = jobs_batch.WorkerEnv(
        logs_client=logs, data_client=None, judge_factory=lambda model_id: judge,
        default_model_id="global.zai.glm-5.3", invoke=invoke, spans_source="runtime_logs",
        sleep=lambda _s: None, judge_concurrency=2,
    )
    store = MemoryStore()
    executor = InProcessExecutor(store, runner=lambda job, sink: jobs_batch.run_job(
        job, sink, worker_env))
    submitted: list = []
    real_submit = executor.submit

    def submit(ws, job):
        submitted.append(job)
        return real_submit(ws, job)

    executor.submit = submit  # type: ignore[method-assign]
    telemetry_waits: list[dict] = []

    def wait(client, **kwargs):
        telemetry_waits.append(kwargs)
        if telemetry_error is not None:
            raise telemetry_error

    monkeypatch.setattr(jobs_batch, "wait_for_evaluation_telemetry", wait)
    monkeypatch.setattr(svc, "select_executor", lambda settings, job_type: executor)
    monkeypatch.setattr(svc, "POLL_INTERVAL_S", 0.02)
    monkeypatch.setattr(svc.rt, "get_runtime",
                        lambda client, rid: {"agentRuntimeName": "eval_agent_abc123"})
    monkeypatch.setattr(svc, "control_client", lambda _ws=None: MagicMock())
    discovered: list[dict] = []
    monkeypatch.setattr(svc, "discover_sessions", lambda ws, **kw: discovered.append(kw) or [
        "window-session-1" + "x" * 30, "window-session-2" + "x" * 30])
    env = Env(executor=executor, store=store, submitted=submitted, telemetry_waits=telemetry_waits,
              judge=judge, logs=logs, discovered=discovered, worker_env=worker_env)
    return env, calls


def wait_terminal(client, run_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/eval/runs/{run_id}").json()
        if run["status"] in ("completed", "failed", "stopped"):
            return run
        time.sleep(0.05)
    return client.get(f"/api/eval/runs/{run_id}").json()


def result_rows(run_id):
    with SessionLocal() as db:
        return db.query(EvalResult).filter(EvalResult.run_id == run_id).all()


def test_active_run_completes_with_scores(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    dataset = EvalDataset(
        workspace_id=DEFAULT_WORKSPACE_ID,
        name="mini", items=[{"prompt": "2+2?"}, {"prompt": "3+3?"}, {"prompt": "4+4?"}])
    db.add(dataset)
    db.commit()
    env, calls = stub_environment(monkeypatch)

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "dataset_id": dataset.id,
        "evaluators": ["Builtin.Correctness", "Builtin.Helpfulness"],
        "wait_seconds": 0,
    })
    assert res.status_code == 201
    run_id = res.json()["id"]
    run = wait_terminal(client, run_id)
    assert run["status"] == "completed", run.get("error")
    assert calls["n"] == 3 and calls["prompts"] == ["2+2?", "3+3?", "4+4?"]
    assert len(run["session_ids"]) == 3 and all(len(s) == 64 for s in run["session_ids"])
    assert calls["sessions"] == run["session_ids"]  # the backend minted them, the worker used them
    (wait,) = env.telemetry_waits
    assert wait["session_id"] == run["session_ids"][-1]
    assert wait["content_log_group"] == GROUP and wait["stability_seconds"] == 0
    assert run["batch_eval_id"] == run_id  # one job, named after the run
    scores = {s["evaluatorId"]: s for s in run["scores"]}
    assert set(scores) == {"Builtin.Correctness", "Builtin.Helpfulness"}
    assert scores["Builtin.Helpfulness"]["score"] == 0.833
    assert all(s["count"] == 3 for s in scores.values())
    assert run["error"] is None and run["budget_stops"] == []
    assert len(result_rows(run_id)) == 6  # 3 sessions × 2 evaluators
    (job,) = env.submitted
    assert job.job_type == "batch_eval" and job.job_id == run_id and job.attempt == 1
    assert job.spec["target"]["runtime_arn"] == agent.arn
    assert job.spec["wait"]["log_groups"] == ["aws/spans", GROUP]
    assert job.spec["evaluator_defs"] == [{"id": "Builtin.Correctness"},
                                          {"id": "Builtin.Helpfulness"}]
    assert [s["session_id"] for s in job.spec["sessions"]] == run["session_ids"]
    assert job.sink["bucket"] == "launchpad-artifacts-test"
    results = client.get(f"/api/eval/runs/{run_id}/results").json()
    assert results["available"] is True and results["count"] == 6
    assert [s["session_id"] for s in results["sessions"]] == run["session_ids"]
    db.close()


def test_skill_builtins_pass_through_and_thirdparty_ids_are_refused(client, monkeypatch):
    """Skill builtins are catalog entries and reach the worker verbatim; third-party
    managed evaluators do not exist in CN, so their ids are refused before any replay."""
    db = SessionLocal()
    agent = make_agent(db, name="eval-agent-3p")
    dataset = EvalDataset(
        workspace_id=DEFAULT_WORKSPACE_ID, name="mini-3p", items=[{"prompt": "x"}])
    db.add(dataset)
    db.commit()
    env, calls = stub_environment(monkeypatch)

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "dataset_id": dataset.id,
        "evaluators": ["Builtin.SkillInstructionFollowing", "ThirdParty.AutoEval.Security"],
        "wait_seconds": 0,
    })
    assert res.status_code == 422
    assert res.json()["code"] == "run.evaluator_not_found"
    assert calls["n"] == 0

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "dataset_id": dataset.id,
        "evaluators": ["Builtin.SkillInstructionFollowing"], "wait_seconds": 0,
    })
    assert res.status_code == 201
    run = wait_terminal(client, res.json()["id"])
    assert run["status"] == "completed", run.get("error")
    assert env.submitted[0].spec["evaluator_defs"] == [{"id": "Builtin.SkillInstructionFollowing"}]
    db.close()


def test_run_failure_recorded(client, monkeypatch):
    """A scenario the agent cannot be invoked for (non-transient error) fails the run with
    the reason; the row can then be removed, a completed run never."""
    db = SessionLocal()
    agent = make_agent(db, name="eval-agent-fail")
    dataset = EvalDataset(workspace_id=DEFAULT_WORKSPACE_ID, name="mini2", items=[{"prompt": "x"}])
    db.add(dataset)
    db.commit()
    stub_environment(monkeypatch, invoke_errors=[ValueError("the runtime is gone")])

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "dataset_id": dataset.id, "wait_seconds": 0,
    })
    run_id = res.json()["id"]
    run = wait_terminal(client, run_id)
    assert run["status"] == "failed"
    assert "the runtime is gone" in run["error"] and "ValueError" in run["error"]
    assert run["batch_eval_id"] == run_id
    # a failed run can be removed from the ledger; the field name is kept, the value is
    # the job id (there is no AWS batch to leave in place)
    res = client.delete(f"/api/eval/runs/{run_id}")
    assert res.status_code == 200, res.text
    assert res.json()["deleted"] is True and res.json()["status"] == "failed"
    assert res.json()["aws_batch_left_in_place"] == run["batch_eval_id"]
    assert client.get(f"/api/eval/runs/{run_id}").status_code == 404
    assert client.delete(f"/api/eval/runs/{run_id}").status_code == 404
    done = EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent.id, agent_name=agent.name,
                   status="completed")
    db.add(done)
    db.commit()
    res = client.delete(f"/api/eval/runs/{done.id}")
    assert res.status_code == 409 and res.json()["code"] == "run.not_deletable"
    db.close()


def test_transient_invoke_errors_are_replayed_then_the_run_completes(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="eval-agent-flaky")
    dataset = EvalDataset(workspace_id=DEFAULT_WORKSPACE_ID, name="flaky", items=[{"prompt": "x"}])
    db.add(dataset)
    db.commit()
    env, calls = stub_environment(
        monkeypatch, invoke_errors=[RuntimeError("internal server error: hiccup")])
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "dataset_id": dataset.id, "wait_seconds": 0,
        "evaluators": ["Builtin.Helpfulness"],
    })
    run = wait_terminal(client, res.json()["id"])
    assert run["status"] == "completed", run.get("error")
    assert calls["n"] == 2 and len(set(calls["sessions"])) == 1  # same pre-assigned session
    raw = env.store.get(f"evaluations/{run['id']}/a1/invocations.jsonl").decode()
    assert json.loads(raw.splitlines()[0])["retries"] == 1
    db.close()


def test_telemetry_timeout_fails_without_evaluating(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="eval-agent-telemetry-timeout")
    dataset = EvalDataset(
        workspace_id=DEFAULT_WORKSPACE_ID, name="telemetry-timeout", items=[{"prompt": "x"}])
    db.add(dataset)
    db.commit()
    from app.evaluation.telemetry import TelemetryReadinessTimeout

    stub_environment(monkeypatch, telemetry_error=TelemetryReadinessTimeout(
        "evaluation telemetry did not become ready"))

    res = client.post(
        "/api/eval/runs",
        json={"agent_id": agent.id, "dataset_id": dataset.id, "wait_seconds": 0},
    )
    run = wait_terminal(client, res.json()["id"])
    assert run["status"] == "failed"
    assert "telemetry did not become ready" in run["error"]
    assert result_rows(run["id"]) == []  # nothing was judged
    db.close()


def test_existing_sessions_skip_replay_but_not_the_telemetry_wait(client, monkeypatch):
    """N17-obs400: an explicit session is not replayed, but its telemetry is still waited
    for (one readiness probe of the session; an old one passes at once)."""
    db = SessionLocal()
    agent = make_agent(db, name="existing-session-agent")
    env, calls = stub_environment(monkeypatch)
    session_id = "existing-session-" + "x" * 24

    res = client.post(
        "/api/eval/runs",
        json={
            "agent_id": agent.id,
            "session_ids": [session_id],
            "evaluators": ["Builtin.Correctness"],
            "wait_seconds": 180,
        },
    )
    assert res.status_code == 201
    run = wait_terminal(client, res.json()["id"])
    assert run["status"] == "completed", run.get("error")
    assert calls["n"] == 0
    assert [w["session_id"] for w in env.telemetry_waits] == [session_id]
    assert env.submitted[0].spec["replay"] is False
    assert run["session_ids"] == [session_id] and len(result_rows(run["id"])) == 1
    db.close()


def test_statuses_mirror_the_workers_phases(client, monkeypatch):
    """The row walks queued → invoking → waiting → evaluating → completed (status.json)."""
    db = SessionLocal()
    agent = make_agent(db, name="phases-agent")
    dataset = EvalDataset(workspace_id=DEFAULT_WORKSPACE_ID, name="ph", items=[{"prompt": "x"}])
    db.add(dataset)
    db.commit()
    env, _ = stub_environment(monkeypatch)
    seen: list[str] = []
    real_update = svc._update

    def spy(run_id, **fields):
        if "status" in fields:
            seen.append(fields["status"])
        real_update(run_id, **fields)

    monkeypatch.setattr(svc, "_update", spy)
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "dataset_id": dataset.id, "wait_seconds": 0,
        "evaluators": ["Builtin.Helpfulness"],
    })
    run = wait_terminal(client, res.json()["id"])
    assert run["status"] == "completed", run.get("error")
    assert seen[0] == "invoking" and seen[-1] == "completed"
    assert set(seen) <= {"invoking", "waiting", "evaluating", "completed"}
    # the worker's last status is `evaluating 1/1`
    status = json.loads(env.store.get(f"evaluations/{run['id']}/a1/status.json"))
    assert status["phase"] == "evaluating" and status["progress"]["message"] == "evaluating 1/1"
    db.close()


def test_a_missing_worker_is_503_before_any_row(client, monkeypatch):
    from app.core.errors import AppError

    db = SessionLocal()
    agent = make_agent(db, name="no-worker-agent")
    dataset = EvalDataset(workspace_id=DEFAULT_WORKSPACE_ID, name="nw", items=[{"prompt": "x"}])
    db.add(dataset)
    db.commit()

    def missing(settings, job_type):
        raise AppError("evaluation.worker_missing", "no worker", status_code=503)

    monkeypatch.setattr(svc, "select_executor", missing)
    monkeypatch.setattr(svc.rt, "get_runtime",
                        lambda client, rid: {"agentRuntimeName": "eval_agent_abc123"})
    monkeypatch.setattr(svc, "control_client", lambda _ws=None: MagicMock())
    before = db.query(EvalRun).count()
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "dataset_id": dataset.id, "wait_seconds": 0,
    })
    assert res.status_code == 503 and res.json()["code"] == "evaluation.worker_missing"
    assert db.query(EvalRun).count() == before
    db.close()


# ── per-session results (scores + judge explanations) ───────────────────────
def _seed_run(**overrides):
    fields = dict(
        workspace_id=DEFAULT_WORKSPACE_ID, agent_id="a1", agent_name="eval-agent",
        mode="evaluators", evaluators=["Builtin.Helpfulness"], status="completed",
        session_ids=["s-a" + "x" * 30, "s-b" + "x" * 30], batch_eval_id="be-123",
        scores=[{"evaluatorId": "Builtin.Helpfulness", "score": 0.5}],
    )
    fields.update(overrides)
    db = SessionLocal()
    run = EvalRun(**fields)
    db.add(run)
    db.commit()
    run_id = run.id
    db.close()
    return run_id


def _seed_results(run_id, rows):
    from app.evaluation import results_store

    with SessionLocal() as db:
        results_store.persist(db, DEFAULT_WORKSPACE_ID, source="run", owner_id=run_id,
                              records=rows)


def test_run_results_groups_judge_records_per_session(client, monkeypatch):
    sid_a, sid_b = "s-a" + "x" * 30, "s-b" + "x" * 30
    run_id = _seed_run()
    _seed_results(run_id, [
        # store order differs from the run's session order — the run's wins
        {"session_id": sid_b, "evaluator_id": "Builtin.Helpfulness", "level": "TRACE",
         "score": 0.33, "label": "Somewhat Unhelpful",
         "explanation": "The agent used the wrong tool."},
        {"session_id": sid_a, "evaluator_id": "Builtin.Helpfulness", "level": "TRACE",
         "score": 1.0, "label": "Very Helpful", "explanation": "Answered fully."},
        {"session_id": sid_a, "evaluator_id": "Builtin.ToolParameterAccuracy", "level": "TOOL_CALL",
         "score": 0.0, "label": "No", "explanation": "employee_id was invented."},
    ])
    res = client.get(f"/api/eval/runs/{run_id}/results")
    assert res.status_code == 200
    body = res.json()
    assert body["available"] is True and body["count"] == 3 and body["truncated"] is False
    assert [s["session_id"] for s in body["sessions"]] == [sid_a, sid_b]
    rows = body["sessions"][0]["results"]
    assert rows[0] == {
        "evaluator_id": "Builtin.Helpfulness", "level": "TRACE", "score": 1.0,
        "label": "Very Helpful", "explanation": "Answered fully.",
        "error_type": None, "error_message": None,
        "arm": None,  # N12-obs62: additive; a plain run's sessions have no arm
        "judge": None,  # P3.1 / P3.2: additive; rows seeded without a judge stamp
        # N17-obs317: additive — rows seeded without a turn / call / reconciliation stamp
        "trace_id": None, "span_id": None, "tool_name": None, "extraction_status": None,
        "judge_model_id": None, "judge_inference": None,
        # N17-obs391: additive — the run has no scenario map (no dataset replayed)
        "scenario_id": None, "scenario_label": None,
        "failure_class": None,  # R3-f: additive — a judged row
        "token_usage": None,  # P8.3: additive — rows seeded without tokens
    }
    assert rows[1]["level"] == "TOOL_CALL" and rows[1]["score"] == 0.0
    assert body["sessions"][1]["results"][0]["explanation"] == "The agent used the wrong tool."


def test_run_results_degrades_instead_of_failing(client, monkeypatch):
    # an insights run has no per-evaluator judgements at all
    res = client.get(f"/api/eval/runs/{_seed_run(mode='insights')}/results").json()
    assert res == {**res, "available": False, "reason": "insights_run", "sessions": []}
    # a run that never dispatched a job
    res = client.get(f"/api/eval/runs/{_seed_run(batch_eval_id=None)}/results").json()
    assert res["available"] is False and res["reason"] == "no_batch"
    # still judging — results.jsonl is being written, don't read half of it
    res = client.get(f"/api/eval/runs/{_seed_run(status='evaluating')}/results").json()
    assert res["available"] is False and res["reason"] == "run_active"
    # job known, but no record was ever ingested
    res = client.get(f"/api/eval/runs/{_seed_run()}/results").json()
    assert res["available"] is False and res["reason"] == "stream_missing"
    assert {"run_id", "batch_eval_id", "available", "sessions", "count", "truncated"} <= set(res)


def _fixture_answers() -> list[str]:
    from app.services.strands_eval import extraction_check
    from app.services.strands_eval.spans_provider import LaunchpadSpansProvider

    provider = LaunchpadSpansProvider(DynamicFakeLogs(), [GROUP], spans_source="runtime_logs")
    return extraction_check.extracted_turns(
        provider.get_evaluation_data(FIXTURE["session_id"]))


def test_run_reconciles_every_session_and_flags_a_mismatch(client, monkeypatch):
    # eval accuracy item 1: the worker compares each replayed turn's collected answer with
    # what the spans extraction hands the judges; the run payload carries the summary, the
    # per-session results each session's check
    (answer,) = _fixture_answers()
    db = SessionLocal()
    agent = make_agent(db, name="eval-agent-check")
    dataset = EvalDataset(workspace_id=DEFAULT_WORKSPACE_ID, name="mini-check",
                          items=[{"prompt": "2+2?"}, {"prompt": "3+3?"}])
    db.add(dataset)
    db.commit()
    env, calls = stub_environment(monkeypatch)
    replies = [answer, "我先查一下。"]  # session 2's replay answered something else
    env.worker_env.invoke = lambda prompt, sid, spec: {"text": replies.pop(0),
                                                       "session_id": sid}
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "dataset_id": dataset.id,
        "evaluators": ["Builtin.Helpfulness"], "wait_seconds": 0})
    run = wait_terminal(client, res.json()["id"])
    assert run["status"] == "completed", run.get("error")
    check = run["extraction_check"]
    assert check["status"] == "mismatch"
    assert (check["sessions"], check["ok"], check["mismatch"]) == (2, 1, 1)
    assert check["mismatched_sessions"] == [run["session_ids"][1]]
    assert check["anomalies"]["turns"] == 2 and check["anomalies"]["empty_rate"] == 0.0
    results = client.get(f"/api/eval/runs/{run['id']}/results").json()
    assert results["extraction_check"] == check
    first, second = results["sessions"]
    assert first["extraction_check"]["status"] == "ok"
    assert second["extraction_check"]["status"] == "mismatch"
    assert second["extraction_check"]["mismatches"][0]["expected_head"] == "我先查一下。"
    db.close()


def test_run_and_its_results_name_each_sessions_dataset_scenario(client, monkeypatch):
    # N17-obs391: scenario_map lived only in the ledger — a client could not attribute a
    # session's results to its dataset scenario
    sid_a, sid_b = "s-a" + "x" * 30, "s-b" + "x" * 30
    run_id = _seed_run(scenario_map={
        sid_a: {"scenario_id": "refund-1", "input": "How do I get a refund?"},
        sid_b: {"scenario_id": "leave-2", "input": "How many leave days do I have?"}})
    _seed_results(run_id, [
        {"session_id": sid_b, "evaluator_id": "Builtin.Helpfulness", "level": "TRACE",
         "score": 0.5, "label": "Neutral/Mixed", "explanation": "ok"}])
    run = client.get(f"/api/eval/runs/{run_id}").json()
    assert run["session_scenarios"] == [
        # split (D3-d) and the turn counts (D3-c) — null on a row whose map predates them
        {"session_id": sid_a, "scenario_id": "refund-1",
         "scenario_label": "How do I get a refund?", "split": None,
         "turns_defined": None, "turns_replayed": None},
        {"session_id": sid_b, "scenario_id": "leave-2",
         "scenario_label": "How many leave days do I have?", "split": None,
         "turns_defined": None, "turns_replayed": None}]
    body = client.get(f"/api/eval/runs/{run_id}/results").json()
    (session,) = body["sessions"]
    assert (session["scenario_id"], session["scenario_label"]) == (
        "leave-2", "How many leave days do I have?")
    assert session["results"][0]["scenario_id"] == "leave-2"
    assert session["results"][0]["scenario_label"] == "How many leave days do I have?"
    plain = client.get(f"/api/eval/runs/{_seed_run()}").json()
    assert plain["session_scenarios"] == []
