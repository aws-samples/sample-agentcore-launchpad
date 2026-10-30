"""Unit coverage of an evaluation run — no silent failure (R3-f).

A run's headline scores are means over the rows that scored. Without the denominator a
run that judged half its turns reads like a run that judged all of them. This module states,
per evaluator and for the run:

* ``expected`` units: SESSION one per planned session; TRACE one per turn the session's
  extraction check counted (else the rows the worker wrote, at least one); TOOL_CALL the
  session's tool calls the worker counted (``tool_units``), 0 allowed — except the skill
  evaluators (SkillSelectionAccuracy / SkillInstructionFollowing and a custom judge on them),
  which judge one unit per skill LOAD (``skill_units``, N17-obs435); without the worker's
  count, the rows it wrote (at least one per session for a plain TOOL_CALL evaluator);
* ``judged`` units (a row without an error — a not-applicable verdict was judged), the error
  rows by :func:`failure_class`, and ``missing`` units: expected but no row at all — the
  silent kind (a session the job never reached, a turn the engine dropped);
* ``coverage = judged / expected`` and ``below_threshold`` against
  ``Settings.eval_coverage_min``. A ``failed_turn`` unit (the AGENT's turn failed — its span
  ended with status ERROR — so it is never judged, like Global) is the agent's failure, not an
  evaluation gap: it leaves the ratio (expected and judged) and is reported apart as
  ``agent_failures`` (N17-obs414).

:func:`publish_coverage` mirrors it as one CloudWatch EMF line (``Launchpad/Evaluations``,
``RunCoverage`` / ``UnscoredUnits``) for an alarm, when ``eval_results_log_group_prefix`` is
set — best effort, like the online results mirror.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from typing import Any

from botocore.exceptions import ClientError

from app.services.strands_eval.engine_core import SKILL_EVALUATORS
from app.services.strands_eval.evaluators import LEVELS

log = logging.getLogger(__name__)

FAILURE_CLASSES = ("timeout", "throttling", "context_overflow", "truncation", "failed_turn",
                   "infrastructure", "evaluator_error")
# (class, markers in the lower-cased "error_type error_message"), first match wins
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("failed_turn", ("span_status_error",)),
    ("context_overflow", ("contextwindowoverflow", "context window", "context length",
                          "too long", "maximum context", "prompt is too long")),
    ("truncation", ("judgebudgetexceeded", "maxtokensreached", "max_tokens", "truncat")),
    ("throttling", ("throttl", "toomanyrequests", "too many requests", "rate exceeded")),
    # the target / telemetry side (a TelemetryReadinessTimeout is not a judge timeout)
    ("infrastructure", ("telemetry", "sessionnotfound", "no spans found", "replay",
                        "failed after ", "infrastructure")),
    ("timeout", ("timeout", "timed out", "judge_deadline", "deadline")),
)
METRIC_NAMESPACE = "Launchpad/Evaluations"
RUNS_LOG_GROUP_SUFFIX = "runs"


def failure_class(record: dict[str, Any]) -> str | None:
    """Why a row did not score — ``None`` for a judged row."""
    if not (record.get("error_type") or record.get("error_message")):
        return None
    text = f"{record.get('error_type') or ''} {record.get('error_message') or ''}".lower()
    for klass, markers in _RULES:
        if any(marker in text for marker in markers):
            return klass
    return "evaluator_error"


def _turns(run: Any, session_id: str) -> int | None:
    checks = ((getattr(run, "extraction_check", None) or {}).get("sessions") or {})
    check = checks.get(session_id) or {}
    turns = (check.get("anomalies") or {}).get("turns")
    return int(turns) if isinstance(turns, int) and turns > 0 else None


def _tool_units(run: Any, session_id: str, *, skill: bool = False) -> int | None:
    """N17-obs416: the session's tool calls (``execute_tool`` spans) the worker counted; for a
    skill evaluator its skill loads (``skill_units``, N17-obs435) — the units each writes a
    row for. ``None`` when the worker reported no count (an older worker)."""
    value = (getattr(run, "skill_units" if skill else "tool_units", None) or {}).get(session_id)
    return int(value) if isinstance(value, int) and value >= 0 else None


def _expected(level: str, rows: int, turns: int | None, tools: int | None = None, *,
              skill: bool = False) -> int:
    if level == "SESSION":
        return 1
    if level == "TRACE" and turns is not None:
        return max(turns, rows)
    if level == "TOOL_CALL" and tools is not None:
        return max(tools, rows)  # 0 for a session without tool calls (N17-obs416) / loads
    if skill:
        # N17-obs435: an older worker counted no loads — never charge it its tool calls
        return rows
    return max(rows, 1)


def _skill_evaluators(run: Any) -> set[str]:
    """The run's evaluators judging one unit per skill load: the skill builtins plus the ones
    the hub named from the run's definitions (``skill_evaluators``)."""
    return {*SKILL_EVALUATORS, *(str(e) for e in (getattr(run, "skill_evaluators", None)
                                                  or []))}


def run_coverage(records: list[dict[str, Any]], run: Any, *,
                 threshold: float) -> dict[str, Any]:
    """``{expected, judged, missing, coverage, below_threshold, threshold, failures,
    evaluators[{evaluator_id, level, expected, judged, missing, failures}]}``."""
    sessions = [str(s) for s in (getattr(run, "session_ids", None) or [])]
    rows: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    levels: dict[str, str] = {}
    for rec in records:
        evaluator = str(rec.get("evaluator_id") or "")
        rows[(evaluator, str(rec.get("session_id") or ""))].append(rec)
        levels.setdefault(evaluator, str(rec.get("level") or "TRACE"))
    for rec_session in {sid for (_e, sid) in rows}:
        if rec_session not in sessions:
            sessions.append(rec_session)
    evaluators = [str(e) for e in (getattr(run, "evaluators", None) or [])]
    evaluators += [e for e in levels if e not in evaluators]
    out: list[dict[str, Any]] = []
    totals = {"expected": 0, "judged": 0, "missing": 0, "agent_failures": 0}
    all_failures: dict[str, int] = defaultdict(int)
    skill_ids = _skill_evaluators(run)
    for evaluator in evaluators:
        level = levels.get(evaluator) or LEVELS.get(evaluator, "TRACE")
        skill = level == "TOOL_CALL" and evaluator in skill_ids
        expected = judged = missing = agent_failures = 0
        failures: dict[str, int] = defaultdict(int)
        for session_id in sessions:
            mine = rows.get((evaluator, session_id), [])
            want = _expected(level, len(mine), _turns(run, session_id),
                             _tool_units(run, session_id, skill=skill), skill=skill)
            expected += want
            missing += max(0, want - len(mine))
            for rec in mine:
                klass = failure_class(rec)
                if klass is None:
                    judged += 1
                elif klass == "failed_turn":
                    agent_failures += 1  # N17-obs414: the agent failed, not the evaluation
                else:
                    failures[klass] += 1
                    all_failures[klass] += 1
        expected -= agent_failures
        out.append({"evaluator_id": evaluator, "level": level, "expected": expected,
                    "judged": judged, "missing": missing, "failures": dict(failures),
                    "agent_failures": agent_failures})
        totals["expected"] += expected
        totals["judged"] += judged
        totals["missing"] += missing
        totals["agent_failures"] += agent_failures
    coverage = round(totals["judged"] / totals["expected"], 4) if totals["expected"] else 1.0
    return {**totals, "coverage": coverage, "threshold": float(threshold),
            "below_threshold": coverage < float(threshold), "failures": dict(all_failures),
            "evaluators": out}


def coverage_emf(run: Any, coverage: dict[str, Any], *, at_ms: int | None = None
                 ) -> dict[str, Any]:
    """One EMF line: ``RunCoverage`` (0–1) and ``UnscoredUnits`` (expected − judged), with the
    agent dimension — what a CloudWatch alarm watches (e.g. RunCoverage < 0.9)."""
    unscored = int(coverage.get("expected") or 0) - int(coverage.get("judged") or 0)
    return {
        "_aws": {
            "Timestamp": int(at_ms if at_ms is not None else time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": METRIC_NAMESPACE,
                "Dimensions": [["agent"], []],
                "Metrics": [{"Name": "RunCoverage", "Unit": "None"},
                            {"Name": "UnscoredUnits", "Unit": "Count"}],
            }],
        },
        "agent": str(getattr(run, "agent_name", None) or "log-source"),
        "runId": str(getattr(run, "id", "")),
        "mode": getattr(run, "mode", None),
        "RunCoverage": coverage.get("coverage"),
        "UnscoredUnits": unscored,
        "expected": coverage.get("expected"),
        "missing": coverage.get("missing"),
        "belowThreshold": bool(coverage.get("below_threshold")),
        "failures": coverage.get("failures") or {},
    }


def publish_coverage(logs: Any, prefix: str, run: Any, coverage: dict[str, Any]) -> bool:
    """Write the EMF line to ``<prefix>/runs`` (stream ``runs-YYYYMMDD``); ``False`` when off
    (no prefix) or when CloudWatch refused — never raises: the ledger is authoritative."""
    if not prefix:
        return False
    group = f"{prefix.rstrip('/')}/{RUNS_LOG_GROUP_SUFFIX}"
    stream = time.strftime("runs-%Y%m%d", time.gmtime())
    event = {"timestamp": int(time.time() * 1000),
             "message": json.dumps(coverage_emf(run, coverage), default=str)}
    for attempt in (0, 1):
        try:
            logs.put_log_events(logGroupName=group, logStreamName=stream, logEvents=[event])
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if attempt == 0 and code == "ResourceNotFoundException":
                try:
                    from app.evaluation.online_scheduler import _ensure_log_stream

                    _ensure_log_stream(logs, group, stream)
                    continue
                except Exception as inner:  # noqa: BLE001
                    log.warning("run coverage mirror to %s failed: %s", group, inner)
                    return False
            log.warning("run coverage mirror to %s failed: %s", group, exc)
            return False
        except Exception as exc:  # noqa: BLE001 — the mirror is advisory
            log.warning("run coverage mirror to %s failed: %s", group, exc)
            return False
    return False
