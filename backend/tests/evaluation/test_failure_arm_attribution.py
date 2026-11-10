"""N12-obs62 — every failure line of the evaluation worker and the hub's per-session failure
summary carry the session's ``arm``, so a canary's / experiment's replay failures can be
attributed to control or treatment WITHOUT the arm's own logs or a join on the assignment rows.

``arm`` is the routing truth the hub pre-assigned to the session (``routing.ARM_CONTROL`` =
``C`` / ``ARM_TREATMENT`` = ``T1`` — the same label ``experiment_assignments.arm`` and
``eval_results.arm`` carry; the canary's named endpoint is the session's ``qualifier``, not
its arm). A plain run has no arm: the key is present and ``None`` everywhere, additively.

Worker: ``invocations.jsonl`` lines (already), the replay error records (``results.jsonl``),
``done.json``'s ``replay_failures[]`` and the insights ``errors[]``. Hub:
``results_store.persist`` keeps a record's own arm when no assignment row exists (the
assignment still wins when there is one), ``normalize_result_record`` / ``run_results``
expose it per record (contract: additive ``RESULT_RECORD`` key), and
``summarize_session_failures`` → ``GET /api/eval/runs/{id}`` ``session_failures.sessions[].arm``.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ReadTimeoutError

import app.evaluation.service as svc
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import results_store
from app.evaluation.jobs import JobDone
from app.evaluation.routers import _run_out
from app.optimization import routing
from app.optimization.models import ExperimentAssignment
from app.services.strands_eval import jobs_batch
from tests.conftest import ws_ctx
from tests.evaluation.fakes import FakeExecutor
from tests.evaluation.test_jobs_replay_failures import (
    S1,
    S2,
    S3,
    Sink,
    _job,
    _spec,
    harness,  # noqa: F401 — registered here
)
from tests.evaluation.test_resume import get_run, make_run

C, T1 = routing.ARM_CONTROL, routing.ARM_TREATMENT
HELP, CORR = "Builtin.Helpfulness", "Builtin.Correctness"


def _timeout() -> ReadTimeoutError:
    return ReadTimeoutError(endpoint_url="https://bedrock-agentcore.cn-northwest-1.test")


def _canary_spec(**overrides) -> dict:
    """The traffic job's shape: every session pre-assigned to an arm, with the arm's endpoint."""
    spec = _spec(**overrides)
    arms = {S1: (C, "stableab12cd"), S2: (T1, "treatab12cd"), S3: (C, "stableab12cd")}
    for session in spec["sessions"]:
        arm, qualifier = arms[session["session_id"]]
        session.update({"arm": arm, "qualifier": qualifier})
    return spec


@pytest.fixture
def replay(request):
    """``test_jobs_replay_failures.harness``, looked up by name (the import above is the
    registration only — ruff F811 otherwise)."""
    return request.getfixturevalue("harness")


# ─── worker ──────────────────────────────────────────────────────────────────────────
def test_the_worker_stamps_the_arm_on_every_failure_line_of_an_unanswered_scenario(replay):
    h = replay({S2: [_timeout()]})  # the treatment arm never answers
    sink = Sink()

    jobs_batch.run_job(_job(_canary_spec()), sink, h.env)

    errored = next(r for r in sink.invocations if r["session_id"] == S2)
    assert errored["status"] == "failed" and errored["arm"] == T1
    # one error record per evaluator, each attributed to the arm
    bad = [r for r in sink.result_rows if r["session_id"] == S2]
    assert len(bad) == 2 and all(r["arm"] == T1 and r["error_kind"] == "infrastructure"
                                 for r in bad)
    # the done.json summary names the arm next to the scenario
    state, _error, summary = sink.finished
    assert state == "COMPLETED"
    assert summary["replay_failures"] == [{
        "session_id": S2, "scenario_id": "s2", "code": "ReadTimeoutError",
        "attempts": jobs_batch.TRANSIENT_SCENARIO_ATTEMPTS, "error": errored["error"],
        "arm": T1,
    }]
    # the control arm's answered sessions kept their arm on the invocation line and no error
    assert [(r["session_id"], r["arm"]) for r in sink.invocations if r["status"] == "ok"] == [
        (S1, C), (S3, C)]


def test_a_plain_run_carries_the_key_as_none(replay):
    h = replay({S1: [_timeout()]})
    sink = Sink()

    jobs_batch.run_job(_job(_spec()), sink, h.env)

    assert all("arm" in r and r["arm"] is None for r in sink.result_rows
               if r["session_id"] == S1)
    assert sink.finished[2]["replay_failures"][0]["arm"] is None


def test_the_insights_prologue_attributes_an_unanswered_scenario_to_its_arm(replay):
    h = replay({S2: [_timeout()]})
    sink = Sink()

    jobs_batch.run_job(_job(_canary_spec(), "insights"), sink, h.env)

    blob = sink.artifacts["insights.json"]
    [error] = blob["errors"]
    assert error["session_id"] == S2 and error["arm"] == T1
    assert error["error_kind"] == "infrastructure"


# ─── hub ─────────────────────────────────────────────────────────────────────────────
def _record(session: str, evaluator: str = HELP, *, error: str | None = None,
            error_type: str = "RuntimeError", arm: str | None = None) -> dict:
    return {
        "session_id": session, "trace_id": None, "evaluator_id": evaluator, "level": "TRACE",
        "score": None if error else 0.9, "label": None if error else "pass",
        "explanation": None if error else "fine",
        "error_type": error_type if error else None, "error_message": error, "arm": arm,
    }


def _replay_text(scenario: str) -> str:
    return jobs_batch._replay_failure_text({
        "scenario_id": scenario, "code": "ReadTimeoutError", "attempts": 3,
        "error": "Read timeout on endpoint URL",
    })


def _assign(session_id: str, arm: str) -> None:
    db = SessionLocal()
    try:
        db.add(ExperimentAssignment(workspace_id=DEFAULT_WORKSPACE_ID, owner_kind="canary",
                                    owner_id="can1", session_id=session_id, arm=arm))
        db.commit()
    finally:
        db.close()


def _persist(run_id: str, records: list[dict]) -> list[dict]:
    db = SessionLocal()
    try:
        results_store.persist(db, DEFAULT_WORKSPACE_ID, source="run", owner_id=run_id,
                              records=records)
        return results_store.read_run_records(db, run_id)
    finally:
        db.close()


def test_persist_keeps_the_records_own_arm_and_lets_an_assignment_row_win():
    _assign("s1", C)  # routing truth for s1 only
    rows = _persist("run_arm", [
        _record("s1", arm=T1),  # a (contrived) disagreeing record: the assignment wins
        _record("s2", error=_replay_text("q2"), error_type="ReadTimeoutError", arm=T1),
        _record("s3"),  # no assignment, no arm on the record → None
    ])
    by_session = {r["session_id"]: r["arm"] for r in rows}
    assert by_session == {"s1": C, "s2": T1, "s3": None}
    assert all("arm" in results_store.normalize_result_record(r) for r in rows)
    assert results_store.normalize_result_record(rows[1])["arm"] == T1


def test_the_session_failure_summary_names_the_arm_of_each_failed_session():
    records = [
        _record("s1", arm=C), _record("s1", CORR, arm=C),
        _record("s2", error=_replay_text("q2"), error_type="ReadTimeoutError", arm=T1),
        _record("s2", CORR, error=_replay_text("q2"), error_type="ReadTimeoutError", arm=T1),
        _record("s3", error="judge down", arm=C),  # a judge error on a control session
        _record("s4", error="judge down"),  # a session the hub knows no arm for
    ]
    summary = svc.summarize_session_failures(records, ["s1", "s2", "s3", "s4"])
    assert [(s["session_id"], s["kind"], s["arm"]) for s in summary["sessions"]] == [
        ("s2", "replay_failed", T1), ("s3", "evaluator_error", C),
        ("s4", "evaluator_error", None)]


def test_the_run_detail_and_the_run_results_api_carry_the_arm_additively():
    run_id = make_run(mode="evaluators", status="evaluating", evaluators=[HELP],
                      session_ids=["s1", "s2"], batch_eval_id="run_x")
    _assign("s2", T1)
    _persist(run_id, [
        _record("s1", arm=C),
        _record("s2", error=_replay_text("q2"), error_type="ReadTimeoutError"),  # arm via row
    ])
    svc._finish(run_id, "evaluators", [JobDone(state="COMPLETED", result_count=2)],
                workspace=ws_ctx(), executor=FakeExecutor(), job_ids=[run_id])
    run = get_run(run_id)
    assert run.status == "completed"
    [failed] = run.session_failures["sessions"]
    assert failed["session_id"] == "s2" and failed["kind"] == "replay_failed"
    assert failed["arm"] == T1  # the assignment row's arm, not an arm log
    assert _run_out(run, snapshot=None)["session_failures"]["sessions"][0]["arm"] == T1

    out = svc.run_results(run, workspace=ws_ctx())
    assert out["available"] is True
    arms = {s["session_id"]: {r["arm"] for r in s["results"]} for s in out["sessions"]}
    assert arms == {"s1": {C}, "s2": {T1}}
