"""Why a run failed (or completed with errors), as a stable code + params (N17-obs450).

A run's ``error`` is an English sentence the console used to show verbatim (the task list's
需要关注 panel: "job 9e80f30c69c5 lost after attempt 3 (evaluating 3/10)"). The sentences
are written by ``evaluation.service`` in a handful of shapes. This module holds those shapes'
formatters (the writers use them) next to :func:`classify`, which reads any stored ``error``
back as ``{code, params}``. So the payload names the reason additively (``error_code`` /
``error_params``, the ``error`` text kept), and every existing row is classified too, not
only new ones. Same pattern as the canary verdict reasons (N17-obs443) and progress lines
(N17-obs447).

Codes: ``stopped_by_operator`` · ``job_lost {job_id, attempt, progress}`` · ``still_running
{minutes}`` · ``all_records_failed {total, unit, first_error, first_class}`` ·
``some_records_errored {count, total, unit, first_error, first_class}`` · ``app_error
{code, message}`` (a platform error code) · ``error {type, message, failure_class}`` (an
exception, the hub's or the worker's). ``unit`` is ``evaluator_records`` or
``insight_sessions``; ``*_class`` is ``run_coverage.failure_class`` of the error (timeout,
throttling, context_overflow, truncation, failed_turn, infrastructure — telemetry not ready
included — or evaluator_error). Text in none of these shapes classifies as ``None``: the
console shows it as it is.
"""

from __future__ import annotations

import re
from typing import Any

from app.evaluation.run_coverage import failure_class

STOP_REASON = "stopped by operator"
STILL_RUNNING_PREFIX = "evaluation job still"
RUN_FAILURE_CODES = frozenset({
    "stopped_by_operator", "job_lost", "still_running", "all_records_failed",
    "some_records_errored", "app_error", "error",
})
_UNITS = {"evaluator records": "evaluator_records", "insight sessions": "insight_sessions"}


def job_lost_text(job_id: str, attempt: int, message: str) -> str:
    return f"job {job_id} lost after attempt {attempt} ({message})"


def still_running_text(minutes: int) -> str:
    return (f"{STILL_RUNNING_PREFIX} running after the {minutes}-minute wait — the worker "
            "may still finish it; re-check the run to read the final result")


def records_text(*, count: int, total: int, unit: str, first: str, all_failed: bool) -> str:
    if all_failed:
        return f"all {total} {unit} failed — first error: {first}"
    return f"{count} of {total} {unit} errored — first error: {first}"


_JOB_LOST = re.compile(r"^job (\S+) lost after attempt (\d+) \((.*)\)$", re.S)
_STILL = re.compile(rf"^{re.escape(STILL_RUNNING_PREFIX)} running after the (\d+)-minute wait")
_ALL = re.compile(r"^all (\d+) (evaluator records|insight sessions) failed — first error: (.*)$",
                  re.S)
_SOME = re.compile(
    r"^(\d+) of (\d+) (evaluator records|insight sessions) errored — first error: (.*)$", re.S)
_APP = re.compile(r"^([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+): (.*)$", re.S)
_EXC = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Timeout|Exceeded|Failed|"
                  r"Stopped|Interrupt|Exit)|span_status_error): (.*)$", re.S)


def _class_of(text: str) -> str:
    kind, _, message = text.partition(": ")
    return failure_class({"error_type": kind, "error_message": message}) or "evaluator_error"


def classify(error: str | None) -> dict[str, Any] | None:
    """``{code, params}`` of a run's stored ``error``; ``None`` when there is none or it is
    in no known shape."""
    text = str(error or "").strip()
    if not text:
        return None
    if text == STOP_REASON:
        return {"code": "stopped_by_operator", "params": {}}
    if m := _JOB_LOST.match(text):
        return {"code": "job_lost",
                "params": {"job_id": m[1], "attempt": int(m[2]), "progress": m[3]}}
    if m := _STILL.match(text):
        return {"code": "still_running", "params": {"minutes": int(m[1])}}
    if m := _ALL.match(text):
        return {"code": "all_records_failed",
                "params": {"total": int(m[1]), "unit": _UNITS[m[2]], "first_error": m[3],
                           "first_class": _class_of(m[3])}}
    if m := _SOME.match(text):
        return {"code": "some_records_errored",
                "params": {"count": int(m[1]), "total": int(m[2]), "unit": _UNITS[m[3]],
                           "first_error": m[4], "first_class": _class_of(m[4])}}
    if m := _APP.match(text):
        return {"code": "app_error", "params": {"code": m[1], "message": m[2]}}
    if m := _EXC.match(text):
        return {"code": "error", "params": {"type": m[1], "message": m[2],
                                            "failure_class": _class_of(text)}}
    return None


def payload(error: str | None) -> dict[str, Any]:
    """The run payload's additive ``error_code`` / ``error_params``."""
    found = classify(error)
    return {"error_code": found["code"] if found else None,
            "error_params": found["params"] if found else None}
