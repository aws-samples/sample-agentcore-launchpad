"""N17-obs415 — a session whose last turn FAILED is ready at once (the readiness gate of
N17-obs400 must not wait for a final answer that never comes).

Live run 60739ef94b7f: a max-iterations ending and a runtime-timeout ending each waited the
full window, then got 10 × TelemetryReadinessTimeout ("root span / content log missing or
younger than 180 s"), where Round 2 gave span_status_error failed-turn rows at no wait. An
ERROR-ended or killed turn's agent span ends with status ERROR and its content record carries
only ``input`` — the probe paired only on a record with input AND output. Now a root span
that ended with status ERROR is paired by its input-only content record (or, without one, by
the root span record itself); the session flows to the engine, which writes the N17-obs373
failed-turn rows (``failed_turn``, an agent failure in coverage — N17-obs414).
Replayed on the REAL corpus records of both endings.
"""

from __future__ import annotations

import json
import types
from typing import Any

import pytest

from app.evaluation import jobs, run_coverage, telemetry
from app.services.strands_eval import jobs_batch
from app.services.strands_eval.spans_provider import SPANS_LOG_GROUP
from tests.evaluation.corpus_support import GroupLogs, load_session
from tests.evaluation.fakes import FakeJudge

SHAPES = {
    # 4 turns: ok / model read timeout after tools / 2 × runtime timeout (span ERROR)
    "runtime_timeout": "3cdd38248181",
    # 3 turns, the last one stopped on max_tokens (agent span ERROR)
    "max_tokens_after_tool": "probe-glm-mt3-e12df09adf",
}


class CorpusLogs(GroupLogs):
    """The corpus as CloudWatch: Logs Insights (GroupLogs) + filter_log_events with each
    record's own event time as timestamp and ingestion (hours old)."""

    def filter_log_events(self, **kwargs: Any) -> dict[str, Any]:
        group = kwargs["logGroupName"]
        want = SPANS_LOG_GROUP if group == SPANS_LOG_GROUP else "runtime"
        return {"events": [{"message": json.dumps(x["record"]), "timestamp": x["timestamp"],
                            "ingestionTime": x["timestamp"]}
                           for x in self.records if x["group"] == want]}


@pytest.mark.parametrize("name", list(SHAPES))
def test_the_probe_pairs_a_session_whose_last_turn_failed(name):
    corpus = load_session(SHAPES[name])
    probe = telemetry.probe_latest_session(
        CorpusLogs(corpus["records"]), session_id=corpus["session_id"],
        content_log_group=corpus["log_group"])
    assert probe.paired, probe.detail
    assert probe.root_span_id is not None


class Sink:
    def __init__(self) -> None:
        self.job = types.SimpleNamespace(job_id="j-415")
        self.rows: list[dict] = []
        self.finished: tuple | None = None

    def cancelled(self) -> bool:
        return False

    def status(self, *_a: Any, **_kw: Any) -> None:
        pass

    def invocation(self, _r: Any) -> None:
        pass

    def results(self, records: list[dict]) -> None:
        self.rows.extend(records)

    def result(self, record: dict) -> None:
        self.rows.append(record)

    def finish(self, state: str, error: Any = None, summary: Any = None) -> None:
        self.finished = (state, error, summary or {})


@pytest.mark.parametrize("name", list(SHAPES))
def test_a_failed_turn_session_is_ready_on_the_first_probe_and_gets_failed_turn_rows(name):
    corpus = load_session(SHAPES[name])
    slept: list[float] = []
    last = max(x["timestamp"] for x in corpus["records"]) / 1000
    spec = {
        "run_id": "run-415", "target": {"runtime_arn": "rt", "protocol": "HTTP"},
        "sessions": [{"session_id": corpus["session_id"], "scenario_id": None, "prompts": [],
                      "replay": False}],
        "replay": False,
        "wait": {"seconds": 180, "log_groups": [corpus["log_group"], SPANS_LOG_GROUP],
                 "content_log_group": corpus["log_group"], "spans_source": "both",
                 "lookback_hours": 48},
        "evaluator_defs": [{"id": "Builtin.Helpfulness"}], "ground_truth": {},
        "judge_model_id": "judge",
    }
    now = {"t": last + 3600.0}

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now["t"] += max(float(seconds), 1.0)

    env = jobs_batch.WorkerEnv(logs_client=CorpusLogs(corpus["records"]), data_client=None,
                               judge_factory=lambda m, **kw: FakeJudge(),
                               sleep=sleep, clock=lambda: now["t"])
    sink = Sink()
    jobs_batch.run_job(jobs.JobSpec(job_id="j-415", job_type="batch_eval", attempt=1,
                                    workspace={}, sink={}, spec=spec), sink, env)
    assert sink.finished[0] == "COMPLETED", sink.finished
    assert slept == []  # ready on the first probe — no wait
    types_ = [r["error_type"] for r in sink.rows]
    assert "TelemetryReadinessTimeout" not in types_
    assert "span_status_error" in types_  # the failed turn(s): never judged
    assert any(t is None for t in types_)  # the turns that answered were judged
    failed = [r for r in sink.rows if r["error_type"] == "span_status_error"]
    assert {run_coverage.failure_class(r) for r in failed} == {"failed_turn"}
