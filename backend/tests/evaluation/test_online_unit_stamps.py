"""N17-obs420 — online TOOL_CALL / skill rows name the call they judged, like batch rows.

The online worker already stamps each record through the batch path (``engine_core
.scoring_units`` → ``unit_records``: ``trace_id`` / ``span_id`` / ``tool_name``), but the
hub's ingest (``online_scheduler._upsert_results``) dropped every field except ``trace_id``:
eval-wb measured 770 of 770 online TOOL_CALL rows with ``span_id`` / ``tool_name`` null.
Real corpus sessions run through ``jobs_online.run`` → the scheduler's ingest, compared
with the batch engine's rows for the same session.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app.core.db import SessionLocal
from app.evaluation import jobs
from app.evaluation import online_scheduler as sched
from app.evaluation.executor import JobSink, MemoryStore
from app.evaluation.jobs import JobSpec
from app.evaluation.models import EvalResult
from app.services.strands_eval import engine_core, jobs_online
from app.services.strands_eval.spans_provider import SPANS_LOG_GROUP, LaunchpadSpansProvider
from tests.evaluation import corpus_support as cs
from tests.evaluation.fakes import FakeJudge
from tests.evaluation.test_online_configs import ledger_row, make_agent
from tests.evaluation.test_online_scheduler import NOW, _again, harness, scan_row

TOOLS = "3cdd38248181"  # tool calls in turns 1 and 2
TWO_LOADS = "d371e407437945f7a28dfa43"  # the same skill loaded in turns 1 and 3
CASES = [(TOOLS, "Builtin.ToolSelectionAccuracy"), (TOOLS, "Builtin.ToolParameterAccuracy"),
         (TWO_LOADS, "Builtin.SkillSelectionAccuracy")]


def _online_records(corpus: dict, evaluator: str, job_id: str) -> list[dict]:
    job = JobSpec(
        job_id=job_id, job_type="online_eval",
        workspace={"account_id": "111122223333", "region": "cn-northwest-1"},
        sink={"bucket": "b", "prefix": jobs.job_prefix(job_id, 1)},
        spec={"config_id": "oe_stamp-1", "mode": "scores",
              "sessions": [{"session_id": corpus["session_id"]}],
              "log_groups": [corpus["log_group"], SPANS_LOG_GROUP],
              "evaluator_defs": [{"id": evaluator}], "judge_model_id": "fake-judge"})
    store = MemoryStore()
    jobs_online.run(job, JobSink(store, job), logs_client=cs.GroupLogs(corpus["records"]),
                    judge_factory=lambda *_a, **_k: FakeJudge(), sleep=lambda _s: None)
    raw = store.get(jobs.results_key(job_id, 1)).decode()
    return [json.loads(line) for line in raw.splitlines()]


def _batch_units(corpus: dict, evaluator: str) -> list[tuple]:
    provider = LaunchpadSpansProvider(cs.GroupLogs(corpus["records"]),
                                      [corpus["log_group"], SPANS_LOG_GROUP])
    rows = engine_core.evaluate_session(
        provider, corpus["session_id"],
        engine_core.EvaluatorSet([{"id": evaluator}], lambda *_a, **_k: FakeJudge()), None,
        job_id="j", sleep=lambda _s: None)
    return [(r["trace_id"], r["span_id"], r["tool_name"]) for r in rows]


@pytest.mark.parametrize(("session", "evaluator"), CASES)
def test_online_rows_carry_the_batch_unit_stamps(session, evaluator):
    corpus = cs.load_session(session)
    config = f"oe_stamp-{evaluator.rsplit('.', 1)[-1].lower()}"
    db = SessionLocal()
    agent = make_agent(db)
    ledger_row(db, agent, config, sampling_percentage=100.0, session_timeout_minutes=1,
               evaluators=[evaluator])
    executor, _, kwargs = harness(rows=[scan_row(corpus["session_id"],
                                                 NOW - timedelta(minutes=5))])
    sched.tick(db, NOW, **kwargs)
    job_id = executor.submitted[0].job_id
    records = _online_records(corpus, evaluator, job_id)
    executor.complete(job_id, records)
    sched.tick(db, NOW + timedelta(minutes=1), **_again(kwargs))

    rows = sorted(db.query(EvalResult).filter_by(config_id=config).all(), key=lambda r: r.id)
    by_id = {jobs.record_id(config, r["session_id"], r["evaluator_id"], i): r
             for i, r in enumerate(records)}
    stored = [(r.trace_id, r.span_id, r.tool_name) for r in rows]
    assert len(rows) == len(records) >= 2
    assert all(trace and span and tool for trace, span, tool in stored)  # was: span / tool null
    assert all((r.span_id, r.tool_name) == (by_id[r.id]["span_id"], by_id[r.id]["tool_name"])
               for r in rows)
    assert sorted(stored) == sorted(_batch_units(corpus, evaluator))  # the batch stamps
    db.close()
