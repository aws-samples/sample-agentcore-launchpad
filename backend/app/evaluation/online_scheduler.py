"""Online-evaluation scheduler (N12 E2-2; plan §B.3).

A hub daemon thread that, every ``Settings.online_eval_tick_s``, runs :func:`tick`:

1. **ingest** — for every ``online_samples`` group already ``dispatched`` (one job per
   (config, sampled_at) group, job id derivable from the rows — restart-safe without a
   job-id column), read the worker's ``results.jsonl`` through the executor seam, upsert
   ``eval_results`` (``source="online"``, ``id = record_id(config_id, session, evaluator,
   ordinal)`` so a re-dispatch never duplicates a row), mirror to CloudWatch (EMF) when
   ``eval_results_log_group_prefix`` is set, and settle the samples (``evaluated`` /
   ``failed``) once ``done.json`` exists or the job went stale;
2. **scan** — Logs Insights ``stats … by session_id`` per ENABLED config over
   ``[last_scanned_at − session_timeout − 5 min, now]`` and the job's log groups (the
   runtime group(s) plus ``aws/spans`` unless ``spans_source == runtime_logs`` — an agent
   with unified telemetry writes its content only there, P4.8): the ``scan`` query takes
   the last activity over EVERY record of the session; with filters a second ``matched``
   query lists the sessions with at least one matching record (P4.3 — the filters no
   longer decide when a session went idle). Keep the sessions idle for longer than
   ``session_timeout_minutes`` that matched and are not in ``online_samples`` yet, sample
   them deterministically (sha256 of ``config:session``), insert at most
   ``online_eval_batch`` new samples;
3. **dispatch** (scores mode) — one ``online_eval`` job per queued group through
   ``executor.select_executor`` (CA-8: never in-process in prod). Insights-mode configs only
   sample; their reports are on-demand insights runs (``online.start_report``).

A config the scheduler cannot serve (no worker, a query Logs Insights refuses) is marked
``status=FAILED`` with the reason and skipped until the operator resumes it; transient
errors (throttling, timeouts) are logged and retried on the next tick.

P4.6: a SAMPLE whose evaluation failed for a transient reason — its job failed or was lost,
the worker wrote nothing for it, or a result row failed on throttling / a timeout /
infrastructure (telemetry readiness included; ``run_coverage.failure_class``) — is dispatched
again after each ``Settings.online_eval_retry_backoff_s`` wait (``attempts`` counts the
dispatches, ``retry_at`` is the next one), while the job's query window still reaches the
session. An agent failure (``failed_turn``), an operator stop and any other error are final.
A retry is its own job (``job_id_for(…, attempt)``); its records replace the earlier
attempt's error rows and never a judged one.

N17-obs422: a sample is one activity SEGMENT of a session (``segment``; unique per config,
session and segment). Activity after the sampled segment's last record (``last_activity``)
by more than the session timeout — an idle-resume, which Global treats as a new session — is
the next segment once it is idle in turn: it covers only the traces that start after the
previous segment's last activity (``segment_after``) and up to its own (the spec's
``after`` / ``until``), and its rows key on ``<session>#<segment>`` so they never collide
with the earlier segment's, which stays as it was.

``main.py`` owns the start/stop calls; the hermetic suite sets
``LAUNCHPAD_ONLINE_EVAL_ENABLED=0`` and drives :func:`tick` directly with fakes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.evaluation import extraction_refs, jobs, online
from app.evaluation.executor import select_executor
from app.evaluation.jobs import JobSpec
from app.evaluation.judge_budget import (
    judge_budget,
    judge_budget_cap,
    judge_fallback_model_id,
    result_judges,
    row_judge,
)
from app.evaluation.models import EvalEvaluator, EvalResult, OnlineEvalConfig, OnlineSample
from app.evaluation.run_coverage import failure_class
from app.services import observability
from app.services.strands_eval.evaluators import LEVELS
from app.services.workspace import WorkspaceContext, context_for_workspace

log = logging.getLogger("launchpad.online_eval")

JOB_TYPE = "online_eval"
SCAN_OVERLAP = timedelta(minutes=5)
FRESH_LOOKBACK = timedelta(hours=1)  # a config scanned for the first time
MAX_SCAN_HOURS = 24 * 7
STAMP_FORMAT = "%Y%m%dT%H%M%S%f"
SAMPLE_QUEUED = "queued"          # scores mode: picked, waiting for a job
SAMPLE_DISPATCHED = "dispatched"  # scores mode: shipped in a job, waiting for results
SAMPLE_SAMPLED = "sampled"        # insights mode: picked; reports read these on demand
SAMPLE_EVALUATED = "evaluated"
SAMPLE_FAILED = "failed"
_OPERATORS = {
    "Equals": "=", "NotEquals": "!=", "GreaterThan": ">", "LessThan": "<",
    "GreaterThanOrEqual": ">=", "LessThanOrEqual": "<=",
}
_QUALIFIED_PREFIXES = ("attributes.", "resource.", "@")
# AppError codes that are the config's (or the workspace's) fault, not the weather
_CONFIG_FAULT_CODES = frozenset({
    "evaluation.worker_missing", "evaluation.executor_not_allowed",
    "evaluation.kind_unsupported",
})
_QUERY_FAULT_MARKERS = ("Malformed", "ended with status Failed")
# P4.6: result-row failure classes a later attempt can cure (failed_turn is the agent's)
TRANSIENT_CLASSES = frozenset({"throttling", "timeout", "infrastructure"})

ResolveWorkspace = Callable[[str | None], WorkspaceContext]
RunQueries = Callable[..., dict[str, list[dict[str, str]]]]
ExecutorFor = Callable[..., Any]
LogsFor = Callable[[WorkspaceContext], Any]


@dataclass
class TickReport:
    scanned: int = 0           # scan rows seen
    sampled: int = 0           # new online_samples rows
    dispatched: list[str] = field(default_factory=list)  # job ids submitted
    ingested: int = 0          # eval_results rows inserted
    failed_configs: list[str] = field(default_factory=list)


# ─── pure helpers ───────────────────────────────────────────────────────────


def filter_clause(filters: list[dict[str, Any]] | None) -> str:
    """The Logs Insights ``filter`` fragment for a config's ``FilterSpec`` list.

    Keys are span attributes unless already qualified (``attributes.`` / ``resource.`` /
    ``@``); values keep their type. Unsupported operators raise ``ValueError`` — the
    create/patch validation refuses them earlier, so hitting this is a bug.
    """
    parts: list[str] = []
    for spec in filters or []:
        key = str(spec.get("key") or "")
        field_name = key if key.startswith(_QUALIFIED_PREFIXES) else f"attributes.{key}"
        value = spec.get("value") or {}
        if "stringValue" in value:
            literal = json.dumps(str(value["stringValue"]))
        elif "doubleValue" in value:
            literal = repr(float(value["doubleValue"]))
        else:
            literal = "true" if value.get("booleanValue") else "false"
        operator = str(spec.get("operator") or "")
        if operator in _OPERATORS:
            parts.append(f"{field_name} {_OPERATORS[operator]} {literal}")
        elif operator == "Contains":
            parts.append(f"strcontains({field_name}, {literal})")
        elif operator == "NotContains":
            parts.append(f"not strcontains({field_name}, {literal})")
        else:
            raise ValueError(f"unsupported filter operator {operator!r}")
    return " and ".join(parts)


def scan_query(service_name: str, filters: list[dict[str, Any]] | None = None) -> str:
    """Idle-session scan: last/first activity + record count per session of one service."""
    where = (f"resource.attributes.service.name = {json.dumps(service_name)} "
             "and ispresent(attributes.session.id)")
    extra = filter_clause(filters)
    if extra:
        where = f"{where} and {extra}"
    return (
        "fields @timestamp\n"
        f"| filter {where}\n"
        "| stats max(@timestamp) as last, min(@timestamp) as first, count(*) as records "
        "by attributes.session.id as session_id\n"
        "| limit 10000"
    )


def idle_scan_queries(
    service_name: str, filters: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """N17-obs353 (P4.3): ``scan`` = last / first activity over every record of each
    session; with filters, ``matched`` = the sessions with at least one matching record.
    Folding the filters into ``scan`` made ``last`` the last MATCHING record — a session
    whose later records lack the attribute read as idle while still active."""
    queries = {"scan": scan_query(service_name)}
    if filter_clause(filters):
        queries["matched"] = scan_query(service_name, filters)
    return queries


def scan_log_groups(row: OnlineEvalConfig, settings: Any) -> list[str]:
    """The groups a config's sessions live in — what the scan and the job read (P4.8)."""
    groups = list(row.log_groups or [row.log_group])  # N17-obs337: a log source's groups
    if (getattr(settings, "observability_spans_source", "both") != "runtime_logs"
            and observability.SPANS_LOG_GROUP not in groups):
        groups.append(observability.SPANS_LOG_GROUP)
    return groups


def idle_candidates(
    rows: list[dict[str, Any]], *, idle_before: datetime, known: set[str],
    matched: set[str] | None, config_id: str, percentage: float,
) -> list[str]:
    """The scan rows to sample: valid ids, idle since ``idle_before`` over all their records,
    not sampled before, matching the filters (``matched``; None = no filters) and in the
    deterministic sample — LONGEST-idle first (R3-f): the per-tick cap used to take the
    first rows in query order, and a session beyond it is re-found only while it stays in
    the scan window, so query order biased which sessions were ever scored; the oldest idle
    sessions leave the window first, so they go first."""
    picked: list[tuple[datetime, str]] = []
    seen = set(known)
    for item in rows:
        session_id = str(item.get("session_id") or "")
        last = _parse_ts(item.get("last"))
        if (not session_id or not observability.SESSION_ID_RE.match(session_id)
                or last is None or last > idle_before or session_id in seen):
            continue
        if matched is not None and session_id not in matched:
            continue  # idle, but no record of it matches the config's filters
        if not is_sampled(config_id, session_id, percentage):
            continue
        picked.append((last, session_id))
        seen.add(session_id)
    return [session_id for _last, session_id in sorted(picked)]


def resumed_candidates(
    rows: list[dict[str, Any]], *, idle_before: datetime, latest: dict[str, tuple[int, datetime]],
    timeout: timedelta, matched: set[str] | None, config_id: str, percentage: float,
) -> list[tuple[datetime, str, int, datetime]]:
    """N17-obs422: ``[(last, session_id, segment, after)]`` — the sessions sampled before
    (``latest``: session → (its last segment, that segment's last activity)) whose scan
    ``last`` lies more than ``timeout`` past that activity (an idle-resume, not a late
    record of the old segment) and is idle since ``idle_before`` in turn."""
    out: list[tuple[datetime, str, int, datetime]] = []
    for item in rows:
        session_id = str(item.get("session_id") or "")
        last = _parse_ts(item.get("last"))
        if session_id not in latest or last is None or last > idle_before:
            continue
        segment, previous = latest[session_id]
        if last <= previous + timeout:
            continue
        if matched is not None and session_id not in matched:
            continue
        if not is_sampled(config_id, session_id, percentage):
            continue
        out.append((last, session_id, segment + 1, previous))
    return sorted(out)


def sampling_floor(row: OnlineEvalConfig) -> datetime | None:
    """N17-obs444: the moment the config last started sampling — its resume
    (``sampling_since``) or, never paused, its creation. Only activity after it is sampled."""
    stamp = row.sampling_since or row.created_at
    return _aware(stamp) if stamp is not None else None


def _bounded_after(after: datetime | None, floor: datetime | None,
                   now: datetime) -> datetime | None:
    """A segment's ``after`` raised to the floor (its traces start after both). A floor older
    than any scan or job query reaches (``MAX_SCAN_HOURS``) excludes nothing and adds no
    bound."""
    if floor is None or now - floor > timedelta(hours=MAX_SCAN_HOURS):
        return after
    return floor if after is None or floor > after else after


def _segment_cutoff(sample: OnlineSample, timeout: timedelta) -> datetime:
    """The last activity a sample covers; an older row recorded none — it was sampled at
    least ``timeout`` after its last record."""
    if sample.last_activity is not None:
        return _aware(sample.last_activity)
    return _aware(sample.sampled_at) - timeout


def record_session(sample_session: str, segment: int | None) -> str:
    """The session key of a sample's ``eval_results`` ids (N17-obs422): the session id for
    the first segment (ids unchanged), ``<session>#<segment>`` for a later one."""
    return sample_session if int(segment or 1) <= 1 else f"{sample_session}#{int(segment)}"


def is_sampled(config_id: str, session_id: str, percentage: float) -> bool:
    """Deterministic sampling: the same session is always in or out for a config."""
    digest = hashlib.sha256(f"{config_id}:{session_id}".encode()).hexdigest()[:8]
    return int(digest, 16) / 2**32 < float(percentage) / 100.0


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def job_id_for(config_id: str, sampled_at: datetime, attempt: int = 1) -> str:
    """One job per (config, sampled_at, attempt) group — derivable from the rows after a
    restart; a retry (P4.6) is a new job, its attempt in the id."""
    stem = f"oe-{config_id}-{_aware(sampled_at).strftime(STAMP_FORMAT)}"
    return stem if attempt <= 1 else f"{stem}-a{attempt}"


def _parse_ts(value: Any) -> datetime | None:
    """Logs Insights timestamps (``YYYY-MM-DD HH:MM:SS.mmm``) → aware UTC."""
    if not value:
        return None
    text = str(value)
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    try:
        return _aware(datetime.fromisoformat(text))
    except ValueError:
        return None


def _is_config_fault(exc: AppError) -> bool:
    if exc.code in _CONFIG_FAULT_CODES:
        return True
    if exc.code == "observability.query_failed":
        return any(marker in str(exc) for marker in _QUERY_FAULT_MARKERS)
    return False


# ─── evaluator definitions shipped to the worker ────────────────────────────


def evaluator_defs(db: Session, workspace_id: str | None, ids: list[str]) -> list[dict[str, Any]]:
    """Builtin ids as ``{"id"}``; custom ids as their ``eval_evaluators`` row (the worker has
    no ledger). An id that is neither stays ``{"id"}`` — the worker records it as an error."""
    custom = [i for i in ids if i not in LEVELS]
    rows: dict[str, EvalEvaluator] = {}
    if custom:
        rows = {
            r.id: r for r in db.scalars(select(EvalEvaluator).where(
                EvalEvaluator.workspace_id == workspace_id, EvalEvaluator.id.in_(custom)))
        }
    out: list[dict[str, Any]] = []
    for evaluator_id in ids:
        row = rows.get(evaluator_id)
        if row is None:
            out.append({"id": evaluator_id})
            continue
        out.append({
            "id": row.id, "name": row.name, "definition": row.definition,
            "instructions": row.instructions,
            "rating_scale": list(row.rating_scale or []), "model_id": row.model_id,
            "base_evaluator_id": row.base_evaluator_id, "lambda_arn": row.lambda_arn,
            "lambda_timeout_s": row.lambda_timeout_s, "level": row.level,
        })
    return out


# ─── EMF mirror ─────────────────────────────────────────────────────────────


def emf_record(config_id: str, record: dict[str, Any], at: datetime) -> dict[str, Any]:
    """One ``gen_ai.evaluation.result`` line in the Global results-log shape (+ EMF header)."""
    ms = int(_aware(at).timestamp() * 1000)
    attributes: dict[str, Any] = {
        "session.id": record.get("session_id"),
        "gen_ai.evaluation.name": record.get("evaluator_id"),
        "aws.bedrock_agentcore.evaluation_level": record.get("level"),
        "gen_ai.evaluation.explanation": record.get("explanation"),
    }
    if record.get("score") is not None:
        attributes["gen_ai.evaluation.score.value"] = record["score"]
    if record.get("label") is not None:
        attributes["gen_ai.evaluation.score.label"] = record["label"]
    if record.get("error_message"):
        attributes["error.type"] = record.get("error_type")
        attributes["error.message"] = record.get("error_message")
    line: dict[str, Any] = {
        "_aws": {
            "Timestamp": ms,
            "CloudWatchMetrics": [{
                "Namespace": "Launchpad/Evaluations",
                "Dimensions": [["evaluator", "onlineEvaluationConfigId"]],
                "Metrics": [{"Name": "score", "Unit": "None"}],
            }],
        },
        "evaluator": record.get("evaluator_id"),
        "onlineEvaluationConfigId": config_id,
        "name": "gen_ai.evaluation.result",
        "traceId": record.get("trace_id"),
        "attributes": attributes,
    }
    if record.get("score") is not None:
        line["score"] = record["score"]
    return line


def mirror_records(
    logs: Any, log_group: str, config_id: str, records: list[dict[str, Any]], at: datetime,
) -> None:
    """Best effort: a mirror failure never blocks the ledger write."""
    if not records:
        return
    stream = f"online-{_aware(at).strftime('%Y%m%d')}"
    events = [{"timestamp": int(_aware(at).timestamp() * 1000),
               "message": json.dumps(emf_record(config_id, rec, at), default=str)}
              for rec in records]
    for attempt in (0, 1):
        try:
            logs.put_log_events(logGroupName=log_group, logStreamName=stream, logEvents=events)
            return
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if attempt == 0 and code == "ResourceNotFoundException":
                _ensure_log_stream(logs, log_group, stream)
                continue
            log.warning("online eval mirror to %s failed: %s", log_group, exc)
            return
        except Exception as exc:  # noqa: BLE001 — the mirror is advisory
            log.warning("online eval mirror to %s failed: %s", log_group, exc)
            return


def _ensure_log_stream(logs: Any, log_group: str, stream: str) -> None:
    for call in (lambda: logs.create_log_group(logGroupName=log_group),
                 lambda: logs.create_log_stream(logGroupName=log_group, logStreamName=stream)):
        try:
            call()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ResourceAlreadyExistsException":
                raise


# ─── the tick ───────────────────────────────────────────────────────────────


def _attempt(sample: OnlineSample) -> int:
    return int(sample.attempts or 1)


def _groups(samples: list[OnlineSample]) -> dict[tuple[datetime, int], list[OnlineSample]]:
    groups: dict[tuple[datetime, int], list[OnlineSample]] = defaultdict(list)
    for sample in samples:
        groups[(_aware(sample.sampled_at), _attempt(sample))].append(sample)
    return dict(sorted(groups.items()))


def _transient_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in records if failure_class(r) in TRANSIENT_CLASSES]


def _fail(sample: OnlineSample, row: OnlineEvalConfig, error: str, *, transient: bool,
          now: datetime, settings: Any) -> None:
    """Settle a failed attempt (P4.6): a transient failure gets the next retry while the
    backoff list has one left and the job window (``_job_spec``'s lookback) still reaches
    the session; anything else is final."""
    sample.status, sample.evaluated_at, sample.retry_at = SAMPLE_FAILED, now, None
    waits = [int(w) for w in getattr(settings, "online_eval_retry_backoff_s", []) or []]
    attempt = _attempt(sample)
    if transient and attempt <= len(waits):
        due = now + timedelta(seconds=waits[attempt - 1])
        timeout_h = math.ceil(int(row.session_timeout_minutes or 15) / 60)
        age_h = math.ceil((due - _aware(sample.sampled_at)).total_seconds() / 3600)
        if age_h + 2 + timeout_h <= MAX_SCAN_HOURS:
            sample.retry_at = due
            error = f"{error} (attempt {attempt}; retry {attempt} of {len(waits)} due)"
    elif transient and waits:
        error = f"{error} (attempt {attempt}; retries exhausted)"
    sample.error = error


USAGE_KEYS = ("inputTokens", "outputTokens", "totalTokens")


def _fold_usage(sample: OnlineSample, records: list[dict[str, Any]]) -> None:
    """N17-obs421: add this attempt's judge tokens (the session's rows) to the sample."""
    usages = [r["token_usage"] for r in records if str(r.get("session_id")) == sample.session_id
              and isinstance(r.get("token_usage"), dict)]
    if not usages:
        return
    total = dict(sample.token_usage or dict.fromkeys(USAGE_KEYS, 0))
    for key in USAGE_KEYS:
        total[key] = int(total.get(key) or 0) + sum(int(u.get(key) or 0) for u in usages)
    sample.token_usage = total


def _settle(sample: OnlineSample, row: OnlineEvalConfig, done: Any,
            records: list[dict[str, Any]], now: datetime, settings: Any) -> None:
    _fold_usage(sample, records)
    mine = [r for r in records if str(r.get("session_id")) == sample.session_id]
    if done.state == "COMPLETED" and mine:
        transient = _transient_rows(mine)
        if not transient:
            sample.status, sample.error, sample.retry_at = SAMPLE_EVALUATED, None, None
            sample.evaluated_at = now
            return
        classes = sorted({str(failure_class(r)) for r in transient})
        first = str(transient[0].get("error_message") or transient[0].get("error_type"))
        _fail(sample, row, f"{len(transient)} of {len(mine)} results failed transiently "
              f"({', '.join(classes)}): {first[:300]}", transient=True, now=now,
              settings=settings)
    elif done.state == "COMPLETED":
        _fail(sample, row, "no result for this session in the worker output",
              transient=True, now=now, settings=settings)
    else:  # FAILED: the job's infrastructure; STOPPED: an operator's stop is final
        _fail(sample, row, str(done.error or f"worker job ended {done.state}"),
              transient=done.state != "STOPPED", now=now, settings=settings)


def _samples(db: Session, config_id: str, status: str) -> list[OnlineSample]:
    return list(db.scalars(select(OnlineSample).where(
        OnlineSample.config_id == config_id, OnlineSample.status == status)).all())


def _upsert_results(
    db: Session, row: OnlineEvalConfig, records: list[dict[str, Any]], now: datetime,
    segments: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Insert the records not yet in ``eval_results``; ids key on the config, so the same
    judgement from a re-dispatched job (or a repeated ingest) is a no-op."""
    ordinals: dict[tuple[str, str], int] = defaultdict(int)
    inserted: list[dict[str, Any]] = []
    # P3.1 / P3.2: each row names the judge (and inference config) that produced it
    judges = result_judges(evaluator_defs(db, row.workspace_id, list(row.evaluators or [])),
                           _judge_model_of(row, get_settings()))
    for rec in records:
        session_id = str(rec.get("session_id") or "")
        evaluator_id = str(rec.get("evaluator_id") or "")
        if not session_id or not evaluator_id:
            continue
        ordinal = ordinals[(session_id, evaluator_id)]
        ordinals[(session_id, evaluator_id)] += 1
        result_id = jobs.record_id(
            row.config_id, record_session(session_id, (segments or {}).get(session_id)),
            evaluator_id, ordinal)
        existing = db.get(EvalResult, result_id)
        if existing is not None and not (existing.error_type or existing.error_message):
            continue  # a judged row is never replaced
        score = rec.get("score")
        if existing is not None:  # P4.6: a retry's record replaces the earlier error row
            if not _replaces(existing, rec):
                continue
            db.delete(existing)
            db.flush()
        db.add(EvalResult(
            id=result_id, workspace_id=row.workspace_id, source="online",
            run_id=None, config_id=row.config_id, session_id=session_id,
            trace_id=rec.get("trace_id"), evaluator_id=evaluator_id, level=rec.get("level"),
            # N17-obs420: the judged call of a TOOL_CALL / skill row and the turn's
            # reconciliation, as the worker stamped them (the batch path's unit stamps)
            span_id=rec.get("span_id"), tool_name=rec.get("tool_name"),
            extraction_status=rec.get("extraction_status"),
            token_usage=rec.get("token_usage"),  # N17-obs421: the unit's judge tokens
            score=None if score is None else float(score), label=rec.get("label"),
            explanation=rec.get("explanation"), error_type=rec.get("error_type"),
            error_message=rec.get("error_message"), arm=None, created_at=now,
            judge_model_id=(row_judge(rec, judges)[1] or {}).get("model_id"),
            judge_inference=row_judge(rec, judges)[1],
        ))
        inserted.append(rec)
    return inserted


def _replaces(existing: EvalResult, rec: dict[str, Any]) -> bool:
    return (existing.score, existing.label, existing.error_type, existing.error_message) != (
        rec.get("score"), rec.get("label"), rec.get("error_type"), rec.get("error_message"))


def _ingest(
    db: Session, row: OnlineEvalConfig, ctx: WorkspaceContext, now: datetime, settings: Any,
    executor_for: ExecutorFor, logs_for: LogsFor | None, report: TickReport,
) -> None:
    dispatched = _samples(db, row.config_id, SAMPLE_DISPATCHED)
    if not dispatched:
        return
    executor = executor_for(settings, job_type=JOB_TYPE)
    stale_s = int(getattr(settings, "eval_job_stale_s", 600))
    prefix = str(getattr(settings, "eval_results_log_group_prefix", "") or "")
    for (stamp, attempt), samples in _groups(dispatched).items():
        job_id = job_id_for(row.config_id, stamp, attempt)
        records = executor.ingest(ctx, job_id)
        inserted = _upsert_results(db, row, records, now,
                                   {s.session_id: int(s.segment or 1) for s in samples})
        if inserted:
            db.commit()
            report.ingested += len(inserted)
            if prefix:
                logs = logs_for(ctx) if logs_for else ctx.client("logs")
                mirror_records(logs, f"{prefix.rstrip('/')}/{row.config_id}", row.config_id,
                               inserted, now)
        done = executor.done(ctx, job_id)
        if done is not None:
            checks = done.summary.get("extraction_checks") or {}
            for sample in samples:
                sample.extraction_check = checks.get(sample.session_id)
                _settle(sample, row, done, records, now, settings)
            db.commit()
            continue
        status = executor.status(ctx, job_id)
        lost = (
            jobs.is_stale(status, now=now, stale_s=stale_s) if status is not None
            else (now - stamp).total_seconds() > stale_s
        )
        if lost:
            for sample in samples:
                _fold_usage(sample, records)
                _fail(sample, row, f"worker job {job_id} lost (no progress for {stale_s} s)",
                      transient=True, now=now, settings=settings)
            db.commit()


def _scan(
    db: Session, row: OnlineEvalConfig, ctx: WorkspaceContext, now: datetime, settings: Any,
    run_queries: RunQueries, report: TickReport,
) -> None:
    timeout = timedelta(minutes=int(row.session_timeout_minutes or 15))
    base = _aware(row.last_scanned_at) if row.last_scanned_at else now - FRESH_LOOKBACK
    since = base - timeout - SCAN_OVERLAP
    hours = max(1, min(MAX_SCAN_HOURS, math.ceil((now - since).total_seconds() / 3600)))
    answers = run_queries(
        idle_scan_queries(row.service_name, row.filters or []), hours,
        log_groups=scan_log_groups(row, settings), workspace=ctx,
    )
    rows = answers["scan"]
    matched = ({str(item.get("session_id") or "") for item in answers.get("matched") or []}
               if "matched" in answers else None)
    report.scanned += len(rows)
    idle_before = now - timeout
    latest: dict[str, tuple[int, datetime]] = {}  # session → (last segment, its last activity)
    for sample in db.scalars(select(OnlineSample).where(
            OnlineSample.config_id == row.config_id)):
        segment = int(sample.segment or 1)
        if segment > latest.get(sample.session_id, (0, now))[0]:
            latest[sample.session_id] = (segment, _segment_cutoff(sample, timeout))
    lasts = {str(item.get("session_id") or ""): _parse_ts(item.get("last")) for item in rows}
    first = idle_candidates(rows, idle_before=idle_before, known=set(latest), matched=matched,
                            config_id=row.config_id, percentage=row.sampling_percentage)
    floor = sampling_floor(row)
    candidates = sorted(
        # N17-obs444: a segment needs activity after the floor, and its job scores only the
        # traces that start after it — never what came before the config sampled
        [(last, sid, segment, _bounded_after(after, floor, now))
         for last, sid, segment, after in (
             [(lasts[sid], sid, 1, None) for sid in first]
             + resumed_candidates(rows, idle_before=idle_before, latest=latest,
                                  timeout=timeout, matched=matched, config_id=row.config_id,
                                  percentage=row.sampling_percentage))
         if floor is None or last > floor],
        key=lambda c: (c[0], c[1]))  # longest-idle first (R3-f), resumed segments too
    status = SAMPLE_QUEUED if online.mode_of(row) == "scores" else SAMPLE_SAMPLED
    batch = int(getattr(settings, "online_eval_batch", 20))
    for last, session_id, segment, after in candidates[:batch]:
        db.add(OnlineSample(workspace_id=row.workspace_id, config_id=row.config_id,
                            session_id=session_id, status=status, sampled_at=now,
                            segment=segment, last_activity=last, segment_after=after))
    report.sampled += min(len(candidates), batch)
    row.last_scanned_at = now
    db.commit()


def _judge_model_of(row: OnlineEvalConfig, settings: Any) -> str | None:
    """The config's judge: its create-time snapshot (P3.2), else — a row created before
    the snapshot existed — the hub setting at this dispatch, as before."""
    return getattr(row, "judge_model_id", None) or getattr(settings, "eval_judge_model_id",
                                                           None)


def _session_item(sample: OnlineSample) -> dict[str, Any]:
    item: dict[str, Any] = {"session_id": sample.session_id}
    if sample.segment_after is not None:
        item["after"] = _aware(sample.segment_after).isoformat()
    if sample.last_activity is not None:
        item["until"] = _aware(sample.last_activity).isoformat()
    return item


def _job_spec(
    db: Session, row: OnlineEvalConfig, ctx: WorkspaceContext, settings: Any, job_id: str,
    stamp: datetime, samples: list[OnlineSample],
) -> JobSpec:
    session_ids = [s.session_id for s in samples]
    spans_source = getattr(settings, "observability_spans_source", "both")
    log_groups = scan_log_groups(row, settings)
    timeout_h = math.ceil(int(row.session_timeout_minutes or 15) / 60)
    age_h = math.ceil(max(0.0, (datetime.now(UTC) - stamp).total_seconds()) / 3600)
    return JobSpec(
        job_id=job_id,
        job_type=JOB_TYPE,
        workspace={"account_id": ctx.account_id, "region": ctx.region},
        sink={"bucket": ctx.resources.get("artifacts_bucket", ""),
              "prefix": jobs.job_prefix(job_id, 1)},
        spec={
            "config_id": row.config_id,
            "mode": "scores",
            "service_name": row.service_name,
            # N17-obs422: each sample's activity segment — its traces start after ``after``
            # (the previous segment's last activity) and no later than ``until`` (its own)
            "sessions": [_session_item(s) for s in samples],
            "log_groups": log_groups,
            "spans_source": spans_source,
            "lookback_hours": min(MAX_SCAN_HOURS, max(1, age_h + 2 + timeout_h)),
            "evaluator_defs": evaluator_defs(db, row.workspace_id, list(row.evaluators or [])),
            # P3.2: the judge snapshotted on the config (an older row: the setting now)
            "judge_model_id": _judge_model_of(row, settings),
            # N17-obs385: the model a judge out of output budget is re-tried on
            "judge_fallback_model_id": judge_fallback_model_id(),
            "judge_deadline_s": int(getattr(settings, "eval_judge_deadline_s", 300)),  # P2.2
            # N-SCN-bug2: the model-aware starting budget (the setting is its floor) and,
            # CN additive, the cap the worker's doubling retries stop at
            "judge_max_tokens": judge_budget(_judge_model_of(row, settings), settings),
            "judge_max_tokens_cap": judge_budget_cap(_judge_model_of(row, settings), settings),
            # eval accuracy item 1: the chat ledger's answers of the sampled sessions that
            # are platform chat sessions — the worker reconciles each extraction with them
            "extraction_references": extraction_refs.ledger_references(
                db, row.workspace_id, list(session_ids)),
        },
        deadline_s=min(jobs.DEFAULT_DEADLINE_S,
                       int(getattr(settings, "eval_job_budget_s", jobs.DEFAULT_DEADLINE_S))),
    )


def _requeue(db: Session, row: OnlineEvalConfig, now: datetime) -> None:
    """P4.6: failed samples whose retry is due go back to the queue as the next attempt."""
    due = [s for s in _samples(db, row.config_id, SAMPLE_FAILED)
           if s.retry_at is not None and _aware(s.retry_at) <= now]
    for sample in due:
        sample.status, sample.retry_at = SAMPLE_QUEUED, None
        sample.attempts = _attempt(sample) + 1
    if due:
        db.commit()


def _dispatch(
    db: Session, row: OnlineEvalConfig, ctx: WorkspaceContext, now: datetime, settings: Any,
    executor_for: ExecutorFor, report: TickReport,
) -> None:
    queued = _samples(db, row.config_id, SAMPLE_QUEUED)
    if not queued:
        return
    executor = executor_for(settings, job_type=JOB_TYPE)  # CA-8 is enforced here
    for (stamp, attempt), samples in _groups(queued).items():
        job_id = job_id_for(row.config_id, stamp, attempt)
        spec = _job_spec(db, row, ctx, settings, job_id, stamp, samples)
        executor.submit(ctx, spec)
        for sample in samples:
            sample.status = SAMPLE_DISPATCHED
        db.commit()
        report.dispatched.append(job_id)


def tick(
    db: Session,
    now: datetime | None = None,
    *,
    settings: Any = None,
    resolve_workspace: ResolveWorkspace = context_for_workspace,
    run_queries: RunQueries = observability.run_insights_queries,
    executor_for: ExecutorFor = select_executor,
    logs_for: LogsFor | None = None,
) -> TickReport:
    """One scheduler pass over every ENABLED, non-FAILED config (all workspaces)."""
    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    report = TickReport()
    rows = list(db.scalars(select(OnlineEvalConfig).where(
        OnlineEvalConfig.execution_status == "ENABLED")).all())
    for row in rows:
        if row.status == "FAILED":
            continue
        try:
            ctx = resolve_workspace(row.workspace_id)
        except LookupError:
            log.warning("online eval %s: workspace %s is gone", row.config_id, row.workspace_id)
            continue
        try:
            _ingest(db, row, ctx, now, settings, executor_for, logs_for, report)
            _scan(db, row, ctx, now, settings, run_queries, report)
            if online.mode_of(row) == "scores":
                _requeue(db, row, now)
                _dispatch(db, row, ctx, now, settings, executor_for, report)
        except AppError as exc:
            db.rollback()
            if _is_config_fault(exc):
                online.mark_failed(db, row, f"{exc.code}: {exc}")
                report.failed_configs.append(row.config_id)
                log.warning("online eval %s disabled: %s: %s", row.config_id, exc.code, exc)
            else:
                log.warning("online eval %s: transient %s: %s", row.config_id, exc.code, exc)
        except Exception:  # noqa: BLE001 — one config's trouble never stops the others
            db.rollback()
            log.exception("online eval %s: tick failed", row.config_id)
    return report


# ─── lifecycle (main.py owns the calls) ─────────────────────────────────────

_thread: threading.Thread | None = None
_stop = threading.Event()


def _loop(settings: Any) -> None:
    interval = max(1.0, float(getattr(settings, "online_eval_tick_s", 60)))
    while not _stop.wait(interval):
        db = SessionLocal()
        try:
            tick(db, settings=settings)
        except Exception:  # noqa: BLE001 — the loop must survive
            log.exception("online eval scheduler tick failed")
        finally:
            db.close()


def scheduler_running() -> bool:
    return _thread is not None and _thread.is_alive()


def start_online_scheduler(settings: Any) -> threading.Thread | None:
    """Start the scheduler thread when ``settings.online_eval_enabled`` (idempotent)."""
    global _thread
    if not getattr(settings, "online_eval_enabled", False):
        return None
    if scheduler_running():
        return _thread
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(settings,), daemon=True,
                               name="online-eval-scheduler")
    _thread.start()
    return _thread


def stop_online_scheduler() -> None:
    """Stop the scheduler thread (idempotent)."""
    global _thread
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=5)
    _thread = None
