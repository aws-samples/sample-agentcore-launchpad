"""N17-obs414 — a failed AGENT turn is not an evaluation gap.

A turn whose agent span ended with status ERROR is never judged (N17-obs373, Global does the
same); it is the agent's failure, not the evaluation's. Counting it in the coverage
denominator made one failed turn push a run below the threshold (live re-score
da3a2c53be2d: 57 / 64 = 0.89). Now ``failed_turn`` units are left out of the ratio and
reported separately as ``agent_failures``.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.evaluation import run_coverage as rc


def _row(sid, trace, *, failed=False):
    return {"session_id": sid, "evaluator_id": "Builtin.Helpfulness", "level": "TRACE",
            "trace_id": trace, "score": None if failed else 0.8,
            "label": None if failed else "Good",
            "error_type": "span_status_error" if failed else None,
            "error_message": "turn t not scored: its agent span ended with status ERROR"
            if failed else None}


def test_failed_agent_turns_leave_the_ratio_and_are_reported_apart():
    sessions = [f"session-{i:04d}" for i in range(8)]
    run = SimpleNamespace(session_ids=sessions, evaluators=["Builtin.Helpfulness"],
                          extraction_check={"sessions": {s: {"anomalies": {"turns": 8}}
                                                         for s in sessions}})
    records = [_row(s, f"{s}-t{k}", failed=(k == 7 and i < 7))
               for i, s in enumerate(sessions) for k in range(8)]
    cov = rc.run_coverage(records, run, threshold=0.9)
    assert (cov["expected"], cov["judged"], cov["agent_failures"]) == (57, 57, 7)
    assert cov["coverage"] == 1.0 and cov["below_threshold"] is False
    assert "failed_turn" not in cov["failures"]
    [ev] = cov["evaluators"]
    assert (ev["expected"], ev["judged"], ev["agent_failures"]) == (57, 57, 7)


def test_real_evaluation_failures_still_count():
    run = SimpleNamespace(session_ids=["session-0001"], evaluators=["Builtin.Helpfulness"],
                          extraction_check={"sessions": {"session-0001": {
                              "anomalies": {"turns": 3}}}})
    timed_out = {**_row("session-0001", "t2"), "score": None, "label": None,
                 "error_type": "judge_deadline", "error_message": "deadline"}
    records = [_row("session-0001", "t0"), _row("session-0001", "t1", failed=True), timed_out]
    cov = rc.run_coverage(records, run, threshold=0.9)
    assert (cov["expected"], cov["judged"], cov["agent_failures"]) == (2, 1, 1)
    assert cov["failures"] == {"timeout": 1} and cov["below_threshold"] is True
