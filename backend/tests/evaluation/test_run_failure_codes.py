"""N17-obs450 — a run's failure reason is a stable code + params, worded by the console.

The V2 task list's 需要关注 panel (and the task detail, the assistant's run history) showed a
run's ``error`` verbatim in English: "job 9e80f30c69c5 lost after attempt 3 (evaluating
3/10)". ``run_failure.classify`` reads the stored text back as ``{code, params}`` and the run
payloads (``_run_out``, the scorecard's runs) carry it additively as ``error_code`` /
``error_params``, the ``error`` text kept. Reading the text means every existing run is
classified too. The writers format through the same module, so writer and parser cannot
drift (round-trip tests below). The real texts are every shape on the default host's 95
runs (read-only probe, 2026-10-25).
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from app.evaluation import run_failure, service
from app.evaluation.run_failure import RUN_FAILURE_CODES, classify

# the default host's run errors, one per shape (verbatim)
LIVE = [
    ("job 9e80f30c69c5 lost after attempt 3 (evaluating 3/10)", "job_lost",
     {"job_id": "9e80f30c69c5", "attempt": 3, "progress": "evaluating 3/10"}),
    ("stopped by operator", "stopped_by_operator", {}),
    ("26 of 36 evaluator records errored — first error: span_status_error: turn "
     "6adc7fb4729a9f9006b0005345eb0547 not scored: its agent span ended with status ERROR "
     "(the turn failed — timeout, model or tool error)", "some_records_errored",
     {"count": 26, "total": 36, "unit": "evaluator_records", "first_class": "failed_turn"}),
    ("20 of 30 evaluator records errored — first error: TelemetryReadinessTimeout: scenario "
     "None answered but its telemetry never became ready: TelemetryReadinessTimeout: "
     "evaluation telemetry did not become ready for session probe-end within 300 s",
     "some_records_errored",
     {"count": 20, "total": 30, "unit": "evaluator_records", "first_class": "infrastructure"}),
    ("1 of 160 evaluator records errored — first error: StructuredOutputException: The model "
     "failed to invoke the structured output tool even after it was forced.",
     "some_records_errored", {"count": 1, "total": 160, "first_class": "evaluator_error"}),
    ("1 of 40 evaluator records errored — first error: JudgeBudgetExceeded: judge "
     "cust_342799d60cd0 exceeded 16384 output tokens after 3 attempts",
     "some_records_errored", {"count": 1, "total": 40, "first_class": "truncation"}),
    ("3 of 18 evaluator records errored — first error: RuntimeError: scenario "
     "sc03-hr-policy-zh failed after 1/3 attempts: RuntimeError: runtime returned error",
     "some_records_errored", {"count": 3, "total": 18}),
]


@pytest.mark.parametrize(("text", "code", "params"), LIVE)
def test_every_live_shape_classifies(text, code, params):
    found = classify(text)
    assert found["code"] == code
    assert {k: found["params"][k] for k in params} == params


def test_the_writers_and_the_parser_round_trip():
    assert classify(run_failure.job_lost_text("j1", 2, "no status")) == {
        "code": "job_lost", "params": {"job_id": "j1", "attempt": 2, "progress": "no status"}}
    assert classify(run_failure.still_running_text(30)) == {
        "code": "still_running", "params": {"minutes": 30}}
    assert classify(service.STOP_REASON)["code"] == "stopped_by_operator"
    summary = {"count": 4, "total": 4, "first_type": "ThrottlingException",
               "first_message": "Rate exceeded"}
    status, text = service._errors_verdict("evaluators", summary)
    assert status == "failed" and classify(text) == {
        "code": "all_records_failed",
        "params": {"total": 4, "unit": "evaluator_records",
                   "first_error": "ThrottlingException: Rate exceeded",
                   "first_class": "throttling"}}
    status, text = service._errors_verdict("insights", {**summary, "count": 1})
    assert status == "completed" and classify(text)["params"]["unit"] == "insight_sessions"


def test_platform_codes_and_exceptions():
    assert classify("run.no_sessions: no sessions found in the last 24 h") == {
        "code": "app_error",
        "params": {"code": "run.no_sessions", "message": "no sessions found in the last 24 h"}}
    found = classify("TimeoutError: the judge call timed out after 300 s")
    assert found["code"] == "error"
    assert found["params"]["type"] == "TimeoutError"
    assert found["params"]["failure_class"] == "timeout"
    worker = classify("TelemetryReadinessTimeout: telemetry never became ready")
    assert worker["params"]["failure_class"] == "infrastructure"


@pytest.mark.parametrize("text", [None, "", "something we never wrote"])
def test_no_error_or_an_unknown_shape_has_no_code(text):
    assert classify(text) is None
    assert run_failure.payload(text) == {"error_code": None, "error_params": None}


def test_the_run_payloads_carry_the_code():
    from app.evaluation import routers, scorecard
    from app.evaluation.models import EvalRun

    row = EvalRun(id="run1", workspace_id="default", agent_id="a", agent_name="n",
                  mode="evaluators", status="failed",
                  error="job 9e80f30c69c5 lost after attempt 3 (x)")
    out = routers._run_out(row, snapshot=None)
    assert out["error"] == "job 9e80f30c69c5 lost after attempt 3 (x)"  # the text stays
    assert (out["error_code"], out["error_params"]["attempt"]) == ("job_lost", 3)
    card = scorecard._run_entry(row, {}, None)
    assert (card["error"], card["error_code"]) == (row.error, "job_lost")


def test_the_console_mirrors_every_code_and_both_locales_word_them():
    root = pathlib.Path(__file__).parents[3] / "frontend/src"
    src = (root / "v2/runFailure.ts").read_text()
    block = re.search(r"RUN_FAILURE_CODES = \[(.*?)\]", src, re.S)
    assert block, "v2/runFailure.ts must export RUN_FAILURE_CODES"
    assert set(re.findall(r'"([a-z_]+)"', block.group(1))) == RUN_FAILURE_CODES
    for locale in ("en", "zh-CN"):
        strings = json.loads((root / f"locales/{locale}/common.json").read_text())
        assert set(strings["runFailure"]["code"]) == RUN_FAILURE_CODES, locale

