"""N17-obs435 — the skill evaluators expect one unit per skill LOAD, not per tool call.

SkillSelectionAccuracy / SkillInstructionFollowing are TOOL_CALL evaluators that write one
row per skill-load call (``engine_core.skill_cases``, N17-obs402), but ``run_coverage`` took
their expected units from the worker's ``tool_units`` (every ``execute_tool`` span), so a fully
judged run read as half judged (live run 5bd1df2a1f6a: 3 sessions, 7 tool calls, 3 skill loads
→ SSA 7/3/4, SIF 7/3/4, 「已评分 9/17 个预期单元（53%）」 below threshold). The worker now also
reports ``skill_units`` (``{session_id: skill loads}``, counted by the same
``engine_core.skill_loads`` the rows are anchored to) and that is the skill evaluators'
expected count; a session without a load expects none, so its not-applicable row is judged,
never missing. The other TOOL_CALL evaluators keep ``tool_units``.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.evaluation import jobs, run_coverage
from app.evaluation import service as svc
from app.evaluation.jobs import JobDone
from app.services.strands_eval import jobs_batch
from app.services.strands_eval.spans_provider import SPANS_LOG_GROUP
from tests.evaluation import corpus_support as cs
from tests.evaluation.fakes import FakeJudge
from tests.evaluation.test_coverage_tool_units import Sink

SSA, SIF = "Builtin.SkillSelectionAccuracy", "Builtin.SkillInstructionFollowing"
TSA, COR = "Builtin.ToolSelectionAccuracy", "Builtin.Correctness"
S1, S2, S3 = "session-sk-0001", "session-sk-0002", "session-sk-0003"


def _row(sid, evaluator, level, *, label="Yes", score=1.0, trace="t"):
    return {"session_id": sid, "evaluator_id": evaluator, "level": level, "trace_id": trace,
            "score": score, "label": label, "error_type": None, "error_message": None}


def _by(cov):
    return {e["evaluator_id"]: (e["expected"], e["judged"], e["missing"])
            for e in cov["evaluators"]}


def test_the_live_run_shape_is_fully_covered():
    # run 5bd1df2a1f6a: 7 tool calls over 3 sessions, one skill load each, every load judged
    run = SimpleNamespace(session_ids=[S1, S2, S3], evaluators=[SSA, SIF, COR],
                          extraction_check={"sessions": {s: {"anomalies": {"turns": 1}}
                                                         for s in (S1, S2, S3)}},
                          tool_units={S1: 2, S2: 3, S3: 2}, skill_units={S1: 1, S2: 1, S3: 1})
    rows = [_row(s, e, "TOOL_CALL") for s in (S1, S2, S3) for e in (SSA, SIF)]
    rows += [_row(s, COR, "TRACE") for s in (S1, S2, S3)]
    cov = run_coverage.run_coverage(rows, run, threshold=0.9)
    assert _by(cov) == {SSA: (3, 3, 0), SIF: (3, 3, 0), COR: (3, 3, 0)}
    assert (cov["expected"], cov["judged"], cov["missing"]) == (9, 9, 0)
    assert cov["coverage"] == 1.0 and cov["below_threshold"] is False


def test_skill_loads_set_the_skill_evaluators_expected_and_tool_calls_the_others():
    run = SimpleNamespace(session_ids=[S1], evaluators=[SSA, SIF, TSA], extraction_check={},
                          tool_units={S1: 5}, skill_units={S1: 2})
    rows = [_row(S1, SSA, "TOOL_CALL")]  # one load's row lost
    rows += [_row(S1, SIF, "TOOL_CALL") for _ in range(2)]
    rows += [_row(S1, TSA, "TOOL_CALL") for _ in range(5)]
    cov = run_coverage.run_coverage(rows, run, threshold=0.9)
    assert _by(cov) == {SSA: (2, 1, 1), SIF: (2, 2, 0), TSA: (5, 5, 0)}


def test_a_session_without_a_skill_load_is_never_missing():
    run = SimpleNamespace(session_ids=[S1, S2], evaluators=[SSA], extraction_check={},
                          tool_units={S1: 3, S2: 4}, skill_units={S1: 0, S2: 0})
    # S1: the evaluator's own not-applicable row (judged); S2: no row at all
    rows = [_row(S1, SSA, "TOOL_CALL", label="Not applicable", score=None)]
    cov = run_coverage.run_coverage(rows, run, threshold=0.9)
    assert _by(cov) == {SSA: (1, 1, 0)}
    assert cov["below_threshold"] is False


def test_a_custom_judge_on_a_skill_builtin_counts_loads_too():
    # is_skill_evaluator: a custom evaluator based on a skill builtin runs per load
    run = SimpleNamespace(session_ids=[S1], evaluators=["custom-skill"], extraction_check={},
                          tool_units={S1: 6}, skill_units={S1: 1},
                          skill_evaluators=["custom-skill"])
    cov = run_coverage.run_coverage([_row(S1, "custom-skill", "TOOL_CALL")], run,
                                    threshold=0.9)
    assert _by(cov) == {"custom-skill": (1, 1, 0)}


def test_a_worker_without_skill_units_never_charges_tool_calls_to_the_skill_evaluators():
    # the eval worker before this fix reports tool_units only
    run = SimpleNamespace(session_ids=[S1], evaluators=[SSA, TSA], extraction_check={},
                          tool_units={S1: 4})
    rows = [_row(S1, SSA, "TOOL_CALL")] + [_row(S1, TSA, "TOOL_CALL") for _ in range(4)]
    cov = run_coverage.run_coverage(rows, run, threshold=0.9)
    assert _by(cov) == {SSA: (1, 1, 0), TSA: (4, 4, 0)}


def test_the_hub_names_the_runs_skill_evaluators_from_its_snapshot():
    run = SimpleNamespace(evaluator_snapshot=[
        {"definition": {"id": SSA}},
        {"definition": {"id": "custom-skill", "definition": "judge",
                        "base_evaluator_id": SIF}},
        {"definition": {"id": "custom-tool", "definition": "judge", "level": "TOOL_CALL"}},
    ], evaluators=[SSA, "custom-skill", "custom-tool"])
    assert svc.skill_evaluator_ids(run) == [SSA, "custom-skill"]
    assert svc.skill_evaluator_ids(SimpleNamespace(evaluators=[SIF, TSA])) == [SIF]


def test_the_hub_folds_the_shards_skill_counts():
    dones = [JobDone(state="COMPLETED", result_count=0, summary={"skill_units": {"a": 1}}),
             JobDone(state="COMPLETED", result_count=0, summary={"skill_units": {"b": 0}}),
             JobDone(state="COMPLETED", result_count=0, summary={})]
    assert svc.fold_skill_units(dones) == {"a": 1, "b": 0}
    assert svc.fold_skill_units([JobDone(state="COMPLETED", result_count=0, summary={})]) \
        is None  # an older worker: the hub keeps no count


def test_the_worker_counts_the_loads_the_skill_rows_are_anchored_to():
    # real corpus session: turn 1 loads wbtest-trip-plan, turn 3 loads it again and calls the
    # calculator — 3 tool calls, 2 skill loads, 2 skill rows
    corpus = cs.load(cs.CORPUS_DIR / "d371e407437945f7a28dfa43.json")
    sid = corpus["session_id"]
    spec = {
        "run_id": "run-sk", "target": {"runtime_arn": "rt", "protocol": "HTTP"},
        "sessions": [{"session_id": sid, "scenario_id": None, "prompts": [], "replay": False}],
        "replay": False,
        "wait": {"seconds": 0, "log_groups": [corpus["log_group"], SPANS_LOG_GROUP],
                 "spans_source": "both", "lookback_hours": 48},
        "evaluator_defs": [{"id": SSA}, {"id": TSA}], "ground_truth": {},
        "judge_model_id": "judge",
    }
    env = jobs_batch.WorkerEnv(logs_client=cs.GroupLogs(corpus["records"]), data_client=None,
                               judge_factory=lambda m, **kw: FakeJudge(), sleep=lambda s: None)
    sink = Sink()
    jobs_batch.run_job(jobs.JobSpec(job_id="j-sk", job_type="batch_eval", attempt=1,
                                    workspace={}, sink={}, spec=spec), sink, env)
    summary = sink.finished[2]
    assert summary["tool_units"] == {sid: 3}
    assert summary["skill_units"] == {sid: 2}
    assert sum(r["evaluator_id"] == SSA for r in sink.rows) == 2
