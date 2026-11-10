"""Production gap P2.2 — a hung judge call is bounded by a wall-clock deadline.

The judge client layers multiply (300 s read timeout × botocore 3 attempts × our 3 transient
attempts ≈ 45 min) and nothing capped one judge call: a hung call held its session — and its
worker slot — that long before it became an error row. ``deadlines.DeadlineEvaluatorSet``
runs every JUDGE call under ``Settings.eval_judge_deadline_s`` (carried on the job spec as
``judge_deadline_s``); past it the unit is a ``judge_deadline`` error row at once — typed so
neither the throttling back-off nor the transient retry re-runs it.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from app.evaluation import run_coverage
from app.services.strands_eval import deadlines, engine_core, runtime_invoke
from tests.evaluation.code_eval_support import CORPUS, GROUPS, SID
from tests.evaluation.corpus_support import GroupLogs
from tests.evaluation.fakes import FakeJudge

RELEASE = threading.Event()


class Hung:
    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, case: Any) -> list:
        self.calls += 1
        RELEASE.wait(10)
        return []


@pytest.fixture(autouse=True)
def _release():
    RELEASE.clear()
    yield
    RELEASE.set()  # let the abandoned call threads finish


def test_a_hung_call_raises_a_typed_deadline_error_fast():
    hung = Hung()
    wrapped = deadlines.DeadlineEvaluator(hung, deadline_s=0.2, label="judge X")
    started = time.monotonic()
    with pytest.raises(deadlines.JudgeDeadlineExceeded) as exc:
        wrapped.evaluate(object())
    assert time.monotonic() - started < 2
    assert exc.value.error_type == "judge_deadline" and "0.2 s" in str(exc.value)
    assert not runtime_invoke.is_upstream_transient(exc.value)  # never re-run as transient
    assert run_coverage.failure_class({"error_type": exc.value.error_type,
                                       "error_message": str(exc.value)}) == "timeout"


def test_a_fast_call_returns_its_outputs_and_keeps_the_evaluator_surface():
    class Fast:
        name = "Builtin.X"

        def evaluate(self, case):
            return ["ok", case]

    wrapped = deadlines.DeadlineEvaluator(Fast(), deadline_s=5, label="judge X")
    assert wrapped.evaluate("c") == ["ok", "c"] and wrapped.name == "Builtin.X"


class HungJudge(FakeJudge):
    """A judge model whose stream never answers (until the test releases it)."""

    def __init__(self) -> None:
        super().__init__()
        self.started = 0

    async def stream(self, *args, **kwargs):
        self.started += 1
        RELEASE.wait(10)
        async for event in super().stream(*args, **kwargs):
            yield event


def _evaluate(judge, deadline_s):
    logs = GroupLogs(CORPUS["records"])
    from app.services.strands_eval.spans_provider import LaunchpadSpansProvider

    provider = LaunchpadSpansProvider(logs, GROUPS)
    evaluators = deadlines.DeadlineEvaluatorSet(
        [{"id": "Builtin.GoalSuccessRate"}], lambda _m, **_kw: judge, deadline_s=deadline_s)
    return engine_core.evaluate_session(provider, SID, evaluators, None, job_id="run1",
                                        sleep=lambda _s: None)


def test_a_hung_judge_becomes_one_error_row_within_the_deadline():
    judge = HungJudge()
    started = time.monotonic()
    [row] = _evaluate(judge, deadline_s=0.3)
    assert time.monotonic() - started < 3
    assert (row["score"], row["error_type"]) == (None, "judge_deadline")
    assert judge.started == 1  # not retried: the deadline is final for the unit
    assert run_coverage.failure_class(row) == "timeout"


def test_a_healthy_judge_scores_as_before():
    RELEASE.set()
    [row] = _evaluate(HungJudge(), deadline_s=30)
    assert row["error_type"] is None and row["score"] is not None


def test_deterministic_evaluators_are_never_wrapped():
    evaluators = deadlines.DeadlineEvaluatorSet(
        [{"id": "Builtin.TrajectoryExactOrderMatch"}], lambda _m, **_kw: None, deadline_s=1)
    built = evaluators.evaluator({"id": "Builtin.TrajectoryExactOrderMatch"})
    assert not isinstance(built, deadlines.DeadlineEvaluator)


def test_the_job_specs_carry_the_hub_deadline(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("LAUNCHPAD_EVAL_JUDGE_DEADLINE_S", "123")
    get_settings.cache_clear()
    try:
        assert get_settings().eval_judge_deadline_s == 123
        assert deadlines.deadline_of({"judge_deadline_s": 77}) == 77
        assert deadlines.deadline_of({}) == deadlines.DEFAULT_JUDGE_DEADLINE_S
    finally:
        get_settings.cache_clear()
