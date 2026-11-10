"""N17-obs421 — online rows carry their judge tokens, and each sample folds them.

The online worker counts every judge call through the same ``deadlines.UsageLedger`` as a
batch job and stamps each unit's tokens on its first record (P8.3), but the hub's ingest
dropped ``token_usage``: online spend was invisible. Now each ``eval_results`` row keeps its
record's ``token_usage`` and ``online_samples.token_usage`` is the sum of the sample's rows,
summed over its attempts (P4.6 retries).
"""

from __future__ import annotations

import json
from datetime import timedelta

from app.core.db import SessionLocal
from app.evaluation import jobs
from app.evaluation import online_scheduler as sched
from app.evaluation.executor import JobSink, MemoryStore
from app.evaluation.jobs import JobDone, JobSpec
from app.evaluation.models import EvalResult, OnlineSample
from app.services.strands_eval import jobs_online
from app.services.strands_eval.spans_provider import SPANS_LOG_GROUP
from tests.evaluation import corpus_support as cs
from tests.evaluation.fakes import FakeJudge
from tests.evaluation.test_online_configs import ledger_row, make_agent
from tests.evaluation.test_online_scheduler import NOW, _again, harness, scan_row

SESSION = "3cdd38248181"
EVALUATORS = ["Builtin.Helpfulness", "Builtin.ToolSelectionAccuracy"]
KEYS = ("inputTokens", "outputTokens", "totalTokens")


def _run_worker(corpus: dict, job_id: str) -> tuple[list[dict], JobDone]:
    job = JobSpec(
        job_id=job_id, job_type="online_eval",
        workspace={"account_id": "111122223333", "region": "cn-northwest-1"},
        sink={"bucket": "b", "prefix": jobs.job_prefix(job_id, 1)},
        spec={"config_id": "oe_tokens-1", "mode": "scores",
              "sessions": [{"session_id": corpus["session_id"]}],
              "log_groups": [corpus["log_group"], SPANS_LOG_GROUP],
              "evaluator_defs": [{"id": e} for e in EVALUATORS], "judge_model_id": "fake"})
    store = MemoryStore()
    jobs_online.run(job, JobSink(store, job), logs_client=cs.GroupLogs(corpus["records"]),
                    judge_factory=lambda *_a, **_k: FakeJudge(), sleep=lambda _s: None)
    records = [json.loads(line) for line in
               store.get(jobs.results_key(job_id, 1)).decode().splitlines()]
    done = JobDone.from_dict(json.loads(store.get(jobs.done_key(job_id, 1)).decode()))
    return records, done


def test_online_rows_keep_their_tokens_and_the_sample_folds_them():
    corpus = cs.load_session(SESSION)
    db = SessionLocal()
    agent = make_agent(db)
    ledger_row(db, agent, "oe_tokens-1", sampling_percentage=100.0,
               session_timeout_minutes=1, evaluators=EVALUATORS)
    executor, _, kwargs = harness(rows=[scan_row(corpus["session_id"],
                                                 NOW - timedelta(minutes=5))])
    sched.tick(db, NOW, **kwargs)
    job_id = executor.submitted[0].job_id
    records, done = _run_worker(corpus, job_id)
    assert done.summary["token_usage"]["totalTokens"] > 0
    executor.complete(job_id, records, summary=done.summary)
    sched.tick(db, NOW + timedelta(minutes=1), **_again(kwargs))

    rows = db.query(EvalResult).filter_by(config_id="oe_tokens-1").all()
    ordinals: dict[tuple, int] = {}
    by_id = {}
    for r in records:  # the ingest's ids: (config, session, evaluator, ordinal)
        key = (r["session_id"], r["evaluator_id"])
        by_id[jobs.record_id("oe_tokens-1", *key, ordinals.get(key, 0))] = r
        ordinals[key] = ordinals.get(key, 0) + 1
    assert len(rows) == len(records) and any(r.get("token_usage") for r in records)
    assert all(row.token_usage == by_id[row.id].get("token_usage") for row in rows)
    sample = db.query(OnlineSample).filter_by(config_id="oe_tokens-1").one()
    assert sample.status == "evaluated"
    assert sample.token_usage == {k: sum((r.token_usage or {}).get(k, 0) for r in rows)
                                  for k in KEYS}
    assert sample.token_usage == {k: done.summary["token_usage"][k] for k in KEYS}
    db.close()


def test_a_retried_sample_sums_the_tokens_of_its_attempts():
    db = SessionLocal()
    agent = make_agent(db)
    ledger_row(db, agent, "oe_tokens-2", sampling_percentage=100.0, session_timeout_minutes=1)
    executor, _, kwargs = harness(rows=[scan_row("session-t", NOW - timedelta(minutes=5))])
    sched.tick(db, NOW, **kwargs)

    def rec(job, **extra):
        return {"session_id": "session-t", "trace_id": "t" * 32,
                "evaluator_id": "Builtin.Helpfulness", "level": "TRACE",
                "id": jobs.record_id(job, "session-t", "Builtin.Helpfulness", 0), **extra}

    first = executor.submitted[0].job_id
    executor.complete(first, [rec(first, score=None, error_type="UpstreamTransientExhausted",
                                  error_message="ThrottlingException (3 attempts)",
                                  token_usage={"inputTokens": 5, "outputTokens": 0,
                                               "totalTokens": 5})])
    sched.tick(db, NOW + timedelta(minutes=1), **_again(kwargs))
    sample = db.query(OnlineSample).filter_by(config_id="oe_tokens-2").one()
    assert sample.token_usage == {"inputTokens": 5, "outputTokens": 0, "totalTokens": 5}
    sched.tick(db, sched._aware(sample.retry_at) + timedelta(seconds=1), **_again(kwargs))
    second = executor.submitted[-1].job_id
    assert second.endswith("-a2")
    executor.complete(second, [rec(second, score=1.0, label="Very Helpful",
                                   token_usage={"inputTokens": 7, "outputTokens": 3,
                                                "totalTokens": 10})])
    sched.tick(db, NOW + timedelta(hours=1), **_again(kwargs))
    db.expire_all()
    sample = db.query(OnlineSample).filter_by(config_id="oe_tokens-2").one()
    assert sample.status == "evaluated"
    assert sample.token_usage == {"inputTokens": 12, "outputTokens": 3, "totalTokens": 15}
    db.close()
