"""P2.2 follow-up — a run has a wall-clock upper bound and still COMPLETES.

The per-call judge deadline (§31.7) bounds one unit; the run as a whole was bounded only by
the hub giving up after ``eval_batch_wait_s`` and marking it failed "still running". The hub
now sends ``run_deadline_s`` = ``eval_batch_wait_s`` − (5 judge-call deadlines — the most an
in-flight unit can take: the call, two budget doublings, two rescues) − a margin, so the
worker's job ends first: once the deadline passes it starts no further unit, writes one
``run_deadline`` error row per evaluator of every session it never reached
(``failure_class`` timeout), and completes with ``run_deadline_reached``.
"""

from __future__ import annotations

from app.evaluation import run_coverage
from app.services.strands_eval import deadlines
from tests.evaluation.test_token_usage import Sink


class JumpingClock:
    """Every read is 100 s later — any deadline in seconds passes at once."""

    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        self.t += 100.0
        return self.t


def _batch(session_ids, run_deadline_s, clock=None):
    from app.evaluation import jobs
    from app.services.strands_eval import jobs_batch
    from tests.evaluation.fakes import FakeJudge
    from tests.test_strands_engine import logs_with

    spec = {
        "run_id": "run-dl", "target": {"runtime_arn": "rt", "protocol": "HTTP"},
        "sessions": [{"session_id": s, "scenario_id": None, "prompts": [], "replay": False}
                     for s in session_ids],
        "replay": False,
        "wait": {"seconds": 0, "log_groups": ["/aws/bedrock-agentcore/runtimes/x-DEFAULT"],
                 "spans_source": "runtime_logs", "lookback_hours": 48},
        "evaluator_defs": [{"id": "Builtin.GoalSuccessRate"}, {"id": "Builtin.Helpfulness"}],
        "ground_truth": {}, "judge_model_id": "judge", "run_deadline_s": run_deadline_s,
    }
    kwargs = {"clock": clock} if clock else {}
    env = jobs_batch.WorkerEnv(logs_client=logs_with(*session_ids), data_client=None,
                               judge_factory=lambda m, **kw: FakeJudge(),
                               sleep=lambda s: None, judge_concurrency=1, **kwargs)
    sink = Sink()
    jobs_batch.run_job(jobs.JobSpec(job_id="j-dl", job_type="batch_eval", attempt=1,
                                    workspace={}, sink={}, spec=spec), sink, env)
    return sink


def test_a_run_past_its_deadline_completes_with_typed_rows_for_the_unreached_sessions():
    from tests.test_strands_engine import SID as S1
    from tests.test_strands_engine import SID2 as S2

    sink = _batch([S1, S2], run_deadline_s=1, clock=JumpingClock())
    state, _err, summary = sink.finished
    assert state == "COMPLETED" and summary["run_deadline_reached"] is True
    assert {(r["session_id"], r["evaluator_id"]) for r in sink.rows} == {
        (s, e) for s in (S1, S2) for e in ("Builtin.GoalSuccessRate", "Builtin.Helpfulness")}
    assert {r["error_type"] for r in sink.rows} == {"run_deadline"}
    assert {run_coverage.failure_class(r) for r in sink.rows} == {"timeout"}


def test_a_run_within_its_deadline_is_unchanged():
    from tests.test_strands_engine import SID as S1

    sink = _batch([S1], run_deadline_s=3600)
    state, _err, summary = sink.finished
    assert state == "COMPLETED" and "run_deadline_reached" not in summary
    assert all(r["error_type"] is None for r in sink.rows)


def test_the_hub_derives_a_run_deadline_that_ends_before_it_gives_up():
    assert deadlines.run_deadline_s(7200, 300) == 7200 - 5 * 300 - deadlines.RUN_DEADLINE_MARGIN_S
    assert deadlines.run_deadline_s(600, 300) == deadlines.MIN_RUN_DEADLINE_S  # floor
    from app.core.config import get_settings

    settings = get_settings()
    derived = deadlines.run_deadline_s(settings.eval_batch_wait_s, settings.eval_judge_deadline_s)
    assert derived < settings.eval_batch_wait_s


def test_the_batch_run_spec_carries_the_derived_run_deadline():
    from app.core.config import get_settings
    from app.evaluation.service import RunPlan

    plan = RunPlan(run_id="r", job_type="batch_eval", target={}, sessions=[], replay=False,
                   wait={}, evaluator_defs=[], ground_truth={}, judge_model_id="j")
    settings = get_settings()
    assert plan.spec([])["run_deadline_s"] == deadlines.run_deadline_s(
        settings.eval_batch_wait_s, settings.eval_judge_deadline_s)
