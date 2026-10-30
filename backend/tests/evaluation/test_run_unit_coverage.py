"""R3-f — no silent failure: every run states the units it expected and the units it
scored, flags each unscored row by cause, warns below a coverage threshold and can mirror
the coverage to CloudWatch; online sampling takes no order bias.

* expected units per evaluator: SESSION one per planned session; TRACE one per extracted turn
  (the session's extraction check); TOOL_CALL the rows the worker wrote (the calls it saw); a
  planned session with no row at all is MISSING — the silent kind;
* every error row carries ``failure_class``: timeout | throttling | context_overflow |
  truncation | failed_turn | infrastructure | evaluator_error;
* ``coverage = judged / expected`` (a not-applicable verdict was judged); below
  ``Settings.eval_coverage_min`` (default 0.9) the run payload says so and the console warns;
* with ``eval_results_log_group_prefix`` set, finishing a run writes one EMF line
  (``Launchpad/Evaluations`` ``RunCoverage`` / ``UnscoredUnits``) an alarm can watch.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.evaluation import online_scheduler as sched
from app.evaluation import run_coverage as rc

S1, S2, S3 = "session-aaaa-0001", "session-aaaa-0002", "session-aaaa-0003"


def _row(sid, evaluator, level, *, score=0.8, error_type=None, message=None, trace=None,
         label="Good"):
    return {"session_id": sid, "evaluator_id": evaluator, "level": level,
            "score": None if error_type else score, "label": None if error_type else label,
            "error_type": error_type, "error_message": message, "trace_id": trace}


def _run(session_ids, evaluators, checks=None):
    return SimpleNamespace(session_ids=session_ids, evaluators=evaluators,
                           extraction_check={"sessions": checks or {}})


def _turns(n):
    return {"anomalies": {"turns": n}}


@pytest.mark.parametrize(("error_type", "message", "klass"), [
    ("lambda_timeout", "the Lambda timed out", "timeout"),
    ("ReadTimeoutError", "Read timeout on endpoint URL", "timeout"),
    ("UpstreamTransientExhausted", "judge errored on every one of 3 attempts: read timed out",
     "timeout"),
    ("ThrottlingException", "Rate exceeded", "throttling"),
    ("ModelThrottledException", "too many requests", "throttling"),
    ("ContextWindowOverflowException", "input is too long for the model", "context_overflow"),
    ("ValidationException", "prompt is too long: 210000 tokens > 200000 maximum",
     "context_overflow"),
    ("JudgeBudgetExceeded", "the judge hit max_tokens at 8192", "truncation"),
    ("MaxTokensReachedException", "max_tokens", "truncation"),
    ("span_status_error", "turn not scored: its agent span ended with status ERROR",
     "failed_turn"),
    ("TelemetryReadinessTimeout", "telemetry never became ready", "infrastructure"),
    ("SessionNotFoundError", "no spans found", "infrastructure"),
    ("invalid_label", "label 'great' not on the scale", "evaluator_error"),
])
def test_every_error_row_is_classified(error_type, message, klass):
    assert rc.failure_class(_row(S1, "E", "TRACE", error_type=error_type, message=message)) \
        == klass
    assert rc.failure_class(_row(S1, "E", "TRACE")) is None


def test_expected_vs_scored_per_evaluator_including_missing_units():
    run = _run([S1, S2, S3], ["Builtin.Helpfulness", "Builtin.GoalSuccessRate"],
               checks={S1: _turns(3), S2: _turns(2), S3: _turns(1)})
    records = [
        # TRACE: S1 3 turns (one timed out), S2 2 turns (one throttled), S3 nothing (silent)
        _row(S1, "Builtin.Helpfulness", "TRACE", trace="t1"),
        _row(S1, "Builtin.Helpfulness", "TRACE", trace="t2"),
        _row(S1, "Builtin.Helpfulness", "TRACE", trace="t3", error_type="ReadTimeoutError",
             message="read timeout"),
        _row(S2, "Builtin.Helpfulness", "TRACE", trace="t4"),
        _row(S2, "Builtin.Helpfulness", "TRACE", trace="t5", error_type="ThrottlingException",
             message="Rate exceeded"),
        # SESSION: S1 and S2 scored, S3 truncated
        _row(S1, "Builtin.GoalSuccessRate", "SESSION"),
        _row(S2, "Builtin.GoalSuccessRate", "SESSION", score=None, label="NOT_APPLICABLE"),
        _row(S3, "Builtin.GoalSuccessRate", "SESSION", error_type="JudgeBudgetExceeded",
             message="max_tokens"),
    ]
    cov = rc.run_coverage(records, run, threshold=0.9)
    by = {e["evaluator_id"]: e for e in cov["evaluators"]}
    helpful = by["Builtin.Helpfulness"]
    assert (helpful["expected"], helpful["judged"], helpful["missing"]) == (6, 3, 1)
    assert helpful["failures"] == {"timeout": 1, "throttling": 1}
    gsr = by["Builtin.GoalSuccessRate"]
    assert (gsr["expected"], gsr["judged"], gsr["missing"]) == (3, 2, 0)
    assert gsr["failures"] == {"truncation": 1}
    assert (cov["expected"], cov["judged"], cov["missing"]) == (9, 5, 1)
    assert cov["coverage"] == pytest.approx(5 / 9, abs=1e-4)
    assert cov["below_threshold"] is True and cov["threshold"] == 0.9
    assert cov["failures"] == {"timeout": 1, "throttling": 1, "truncation": 1}


def test_a_clean_run_is_full_coverage():
    run = _run([S1], ["Builtin.Helpfulness"], checks={S1: _turns(2)})
    records = [_row(S1, "Builtin.Helpfulness", "TRACE", trace="a"),
               _row(S1, "Builtin.Helpfulness", "TRACE", trace="b")]
    cov = rc.run_coverage(records, run, threshold=0.9)
    assert (cov["coverage"], cov["below_threshold"], cov["missing"]) == (1.0, False, 0)


def test_without_an_extraction_check_a_trace_evaluator_expects_its_rows_or_one():
    run = _run([S1, S2], ["Builtin.Helpfulness"])
    cov = rc.run_coverage([_row(S1, "Builtin.Helpfulness", "TRACE", trace="a"),
                           _row(S1, "Builtin.Helpfulness", "TRACE", trace="b")], run,
                          threshold=0.5)
    assert (cov["expected"], cov["judged"], cov["missing"]) == (3, 2, 1)


def test_the_emf_line_carries_the_coverage_metrics():
    run = SimpleNamespace(id="run-1", agent_name="agent-x", mode="evaluators")
    line = rc.coverage_emf(run, {"coverage": 0.5, "expected": 10, "judged": 5,
                                 "missing": 2, "below_threshold": True, "threshold": 0.9},
                           at_ms=1_800_000_000_000)
    metrics = line["_aws"]["CloudWatchMetrics"][0]
    assert metrics["Namespace"] == "Launchpad/Evaluations"
    assert {m["Name"] for m in metrics["Metrics"]} == {"RunCoverage", "UnscoredUnits"}
    assert line["RunCoverage"] == 0.5 and line["UnscoredUnits"] == 5
    assert line["agent"] == "agent-x" and line["runId"] == "run-1"


def test_publish_is_best_effort_and_off_without_a_prefix():
    class Logs:
        def __init__(self):
            self.events = []

        def put_log_events(self, **kw):
            self.events.append(kw)

        def create_log_group(self, **kw):
            pass

        def create_log_stream(self, **kw):
            pass

    logs = Logs()
    run = SimpleNamespace(id="run-1", agent_name="a", mode="evaluators")
    cov = {"coverage": 1.0, "expected": 1, "judged": 1, "missing": 0, "below_threshold": False,
           "threshold": 0.9}
    assert rc.publish_coverage(logs, "", run, cov) is False
    assert rc.publish_coverage(logs, "/launchpad/evaluations/results/", run, cov) is True
    [call] = logs.events
    assert call["logGroupName"] == "/launchpad/evaluations/results/runs"
    assert json.loads(call["logEvents"][0]["message"])["RunCoverage"] == 1.0

    class Broken(Logs):
        def put_log_events(self, **kw):
            raise RuntimeError("boom")

    assert rc.publish_coverage(Broken(), "/p", run, cov) is False  # never raises


# ─── online sampling bias ────────────────────────────────────────────────────────────


def test_sampling_is_independent_of_the_session_id_shape():
    """The hash ignores structure: sequential, prefixed and uuid-like ids sample alike."""
    shapes = {
        "sequential": [f"session-{i:06d}" for i in range(4000)],
        "prefixed": [f"probe-hr-{i}-{i * 7 % 13}" for i in range(4000)],
        "uuidish": [f"{i:08x}-0000-4000-8000-{i * 2654435761 % 2**48:012x}"
                    for i in range(4000)],
    }
    for name, ids in shapes.items():
        rate = sum(sched.is_sampled("oe_bias", s, 25.0) for s in ids) / len(ids)
        assert abs(rate - 0.25) < 0.03, (name, rate)  # ±4σ at n = 4000


def test_the_per_tick_cap_takes_the_longest_idle_sessions_first():
    """The cap used to take the first N rows in query order; the overflow is re-found only
    while it stays in the scan window, so ordering is a bias toward query order. Now the
    oldest-idle sessions go first (they leave the window first)."""
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    rows = [{"session_id": f"session-{i:04d}", "last": (now - timedelta(minutes=20 + i))
             .strftime("%Y-%m-%d %H:%M:%S.000")} for i in range(30)]
    picked = sched.idle_candidates(rows, idle_before=now - timedelta(minutes=15), known=set(),
                                   matched=None, config_id="oe_cap", percentage=100.0)
    assert picked[:3] == ["session-0029", "session-0028", "session-0027"]
    assert len(picked) == 30


# ─── the run lifecycle: computed at finish, on the payload, mirrored when configured ──


def test_finishing_a_run_stores_its_unit_coverage_and_the_payload_shows_it(monkeypatch):
    import app.evaluation.service as svc
    from app.evaluation.routers import _run_out
    from tests.evaluation.test_resume import get_run
    from tests.evaluation.test_run_record_errors import _record, _settle

    published: list = []
    monkeypatch.setattr(svc.run_coverage, "publish_coverage",
                        lambda logs, prefix, run, cov: published.append((prefix, cov)))
    records = [_record("s1", "Builtin.Helpfulness"),
               _record("s2", "Builtin.Helpfulness", error="judge timed out")]
    run = get_run(_settle(records))
    cov = run.unit_coverage
    assert (cov["expected"], cov["judged"], cov["coverage"]) == (2, 1, 0.5)
    assert cov["failures"] == {"timeout": 1} and cov["below_threshold"] is True
    assert _run_out(run, snapshot=None)["unit_coverage"] == cov
    assert published == []  # no results-mirror prefix configured → no CloudWatch line

    from app.core.config import get_settings

    monkeypatch.setenv("LAUNCHPAD_EVAL_RESULTS_LOG_GROUP_PREFIX", "/launchpad/evaluations/results")
    get_settings.cache_clear()
    try:
        monkeypatch.setattr(svc.WorkspaceContext, "client", lambda self, service: object(),
                            raising=False)
        get_run(_settle(records))
    finally:
        get_settings.cache_clear()
    assert [p for p, _c in published] == ["/launchpad/evaluations/results"]
