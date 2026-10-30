"""Eval accuracy item 1 — the extraction reconciliation on every evaluation.

Each evaluated session's per-turn extracted answers (what the judges read) are compared with
an independent reference: the answers the worker collected while replaying (dataset / batch
runs, canary traffic), else the platform chat ledger's answer bubbles (session-scope runs,
SCORE NOW, online), else the check is ``unverifiable``. The worker cases replay REAL corpus
records (``tests/fixtures/eval_corpus``) through the production provider.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from app.evaluation import extraction_refs, jobs, service, telemetry
from app.evaluation.jobs import JobDone
from app.services import chat_ledger
from app.services.strands_eval import engine_core, extraction_check, jobs_batch, jobs_online
from app.services.strands_eval.spans_provider import LaunchpadSpansProvider
from tests.evaluation import corpus_support as cs

TWO_TURN = cs.load_session("35f49b824328")
PREAMBLE_SESSION = cs.load_session("257f17b16cbb")
ANSWERS = [t["answer"] for t in TWO_TURN["expected"]["turns"]]
PROMPTS = [t["user_prompt"] for t in TWO_TURN["expected"]["turns"]]


# ── the normalisation rule ────────────────────────────────────────────────────────

@pytest.mark.parametrize(("a", "b"), [
    ("已查到：450 元/晚", "已查到：450 元/晚\n"),
    ("第一段 第二段", "第一段\n第二段"),  # blocks joined by a space vs a newline
    ("**住宿上限** 450 元", "住宿上限 450 元"),  # markdown emphasis
    ("ＡＢＣ１２３", "ABC123"),  # NFKC: full-width ASCII
    ("a\u200bb", "ab"),  # zero-width
])
def test_normalise_tolerates_whitespace_markdown_and_width(a, b):
    assert extraction_check.normalise(a) == extraction_check.normalise(b)


@pytest.mark.parametrize(("a", "b"), [
    ("450 元/晚", "550 元/晚"),
    ("我先查一下差旅标准", "已查到：成都住宿上限 450 元/晚"),
    ("可以。", "可以？"),
])
def test_normalise_keeps_every_word_digit_and_punctuation(a, b):
    assert extraction_check.normalise(a) != extraction_check.normalise(b)


# ── per-session check ─────────────────────────────────────────────────────────────

def _ref(answers: list[Any], source: str = extraction_check.SOURCE_REPLAY) -> dict[str, Any]:
    return extraction_check.reference(source, answers)


def test_reconciliation_ok_when_every_turn_matches():
    result = extraction_check.check(["答案一\n", "**答案二**"], _ref(["答案一", "答案二"]))
    assert result["status"] == "ok"
    assert result["source"] == "replay"
    assert result["turns_checked"] == 2 and result["mismatches"] == []


def test_reconciliation_mismatch_names_the_turn_and_both_heads():
    result = extraction_check.check(["我先查一下差旅标准和您的职级信息。", "答案二"],
                                    _ref(["已查到：成都住宿上限 450 元/晚", "答案二"]))
    assert result["status"] == "mismatch"
    assert result["mismatch_count"] == 1
    assert result["mismatches"] == [{"turn": 1, "expected_head": "已查到：成都住宿上限 450 元/晚",
                                     "extracted_head": "我先查一下差旅标准和您的职级信息。"}]


def test_reconciliation_a_missing_or_extra_turn_is_a_mismatch():
    # a replay retried in the same session: the spans hold both attempts' turns
    extra = extraction_check.check(["", "答案"], _ref(["答案"]))
    assert extra["status"] == "mismatch" and extra["turns_checked"] == 2
    missing = extraction_check.check(["答案"], _ref(["答案", "第二轮"]))
    assert missing["mismatches"] == [{"turn": 2, "expected_head": "第二轮",
                                      "extracted_head": "(missing)"}]


def test_reconciliation_unverifiable_without_a_reference_or_a_comparable_turn():
    assert extraction_check.check(["答案"], None)["status"] == "unverifiable"
    assert extraction_check.check(["答案"], None)["source"] is None
    # a ledger answer cut at the token limit (None) is skipped, not compared
    skipped = extraction_check.check(["答案"], _ref([None], "chat_ledger"))
    assert skipped["status"] == "unverifiable" and skipped["turns_checked"] == 0
    partial = extraction_check.check(["答案", "二"], _ref([None, "二"], "chat_ledger"))
    assert partial["status"] == "ok" and partial["turns_checked"] == 1


def test_reconciliation_mismatches_are_bounded():
    result = extraction_check.check([str(i) for i in range(9)], _ref(["x"] * 9))
    assert result["mismatch_count"] == 9
    assert len(result["mismatches"]) == extraction_check.MISMATCHES_MAX


# ── anomaly counters ──────────────────────────────────────────────────────────────

def test_anomaly_counters_empty_tool_json_and_repeated_previous():
    answers = ["", '{"employee_id": "EMP-1024", "name": "Maya Chen"}', "答案", "答案\n",
               '[{"text": "x"}]', "{不是 JSON"]
    assert extraction_check.anomalies(answers) == {
        "turns": 6, "empty": 1, "tool_json_like": 2, "repeated_previous": 1}


def test_run_summary_counts_sessions_turns_and_the_empty_rate():
    checks = {
        "s1": extraction_check.check(["答案", ""], _ref(["答案", ""])),
        "s2": extraction_check.check(["前言"], _ref(["最终答案"])),
        "s3": extraction_check.check(["", ""], None),
    }
    summary = extraction_check.summarise(checks)
    assert summary["status"] == "mismatch"
    assert (summary["sessions"], summary["ok"], summary["mismatch"],
            summary["unverifiable"]) == (3, 1, 1, 1)
    assert summary["turns_checked"] == 3
    assert summary["mismatched_sessions"] == ["s2"]
    # two empty turns in a row are empties, not a repeated answer
    assert summary["anomalies"] == {"turns": 5, "empty": 3, "tool_json_like": 0,
                                    "repeated_previous": 0, "empty_rate": 0.6}
    assert extraction_check.summarise({"s3": checks["s3"]})["status"] == "unverifiable"
    assert extraction_check.summarise({"s1": checks["s1"]})["status"] == "ok"


# ── the worker: batch replay, session scope, online ───────────────────────────────

class Sink:
    def __init__(self) -> None:
        self.job = types.SimpleNamespace(job_id="j-check")
        self.rows: list[dict] = []
        self.invocations: list[dict] = []
        self.finished: tuple[str, str | None, dict] | None = None

    def cancelled(self) -> bool:
        return False

    def status(self, *_a, **_kw) -> None:
        pass

    def invocation(self, record) -> None:
        self.invocations.append(record)

    def results(self, records) -> None:
        self.rows.extend(records)

    def result(self, record) -> None:
        self.rows.append(record)

    def finish(self, state, error=None, summary=None) -> None:
        self.finished = (state, error, summary or {})


@pytest.fixture
def no_judges(monkeypatch):
    """Spans are ready at once and judges score nothing — the check runs before them."""
    monkeypatch.setattr(telemetry, "probe_latest_session", lambda client, **kw:
                        telemetry.TelemetryProbe(root_span_id="r", content_ingestion_ms=0))
    monkeypatch.setattr(engine_core, "evaluate_with_budget", lambda *a, **kw: [])


def _batch(corpus: dict[str, Any], answers: list[str] | None, *, replay: bool = True,
           references: dict[str, Any] | None = None) -> dict[str, Any]:
    sid = corpus["session_id"]
    prompts = [t["user_prompt"] for t in corpus["expected"]["turns"]]
    replies = list(answers or [])
    spec: dict[str, Any] = {
        "run_id": "run-check", "target": {"runtime_arn": "rt", "protocol": "HTTP"},
        "sessions": [{"session_id": sid, "scenario_id": "s1", "prompts": prompts,
                      "replay": replay}],
        "replay": replay,
        "wait": {"seconds": 0, "log_groups": [corpus["log_group"], "aws/spans"],
                 "content_log_group": corpus["log_group"], "spans_source": "both",
                 "lookback_hours": 48},
        "evaluator_defs": [{"id": "Builtin.Helpfulness"}], "ground_truth": {},
        "judge_model_id": "judge",
    }
    if references is not None:
        spec["extraction_references"] = references
    env = jobs_batch.WorkerEnv(
        logs_client=cs.GroupLogs(corpus["records"]), data_client=None,
        judge_factory=lambda m, **kw: None, invoke=lambda p, s, spec_: {
            "text": replies.pop(0), "session_id": s},
        sleep=lambda s: None, clock=lambda: 1_000.0)
    sink = Sink()
    job = jobs.JobSpec(job_id="j-check", job_type="batch_eval", attempt=1, workspace={},
                       sink={}, spec=spec)
    jobs_batch.run_job(job, sink, env)
    assert sink.finished is not None and sink.finished[0] == "COMPLETED", sink.finished
    # the collected answers feed the check only — never the invocations.jsonl line
    assert all("answers" not in line for line in sink.invocations)
    return sink.finished[2]["extraction_checks"][sid]


def test_batch_replay_reconciles_ok_against_the_collected_answers(no_judges):
    check = _batch(TWO_TURN, ANSWERS)
    assert check["status"] == "ok" and check["source"] == "replay"
    assert check["turns_checked"] == 2
    assert check["anomalies"] == {"turns": 2, "empty": 0, "tool_json_like": 0,
                                  "repeated_previous": 0}


def test_batch_replay_flags_a_turn_the_judges_would_score_on_other_text(no_judges):
    # the runtime streamed the preamble as the answer of turn 1 (an obs315-class divergence)
    check = _batch(TWO_TURN, ["我来帮你查一下", ANSWERS[1]])
    assert check["status"] == "mismatch"
    assert [m["turn"] for m in check["mismatches"]] == [1]
    assert check["mismatches"][0]["expected_head"] == "我来帮你查一下"
    assert check["mismatches"][0]["extracted_head"].startswith("上海属于")


def test_session_scope_run_uses_the_hub_ledger_reference_else_the_runtime_logs(no_judges):
    ref = {TWO_TURN["session_id"]: _ref(ANSWERS, "chat_ledger")}
    assert _batch(TWO_TURN, None, replay=False, references=ref)["status"] == "ok"
    # N17-obs316: no ledger bubble (a replay-created / API session) — the session's own
    # runtime content records are the reference, so the check is ok / mismatch, not
    # unverifiable
    check = _batch(TWO_TURN, None, replay=False)
    assert check["status"] == "ok" and check["source"] == "runtime_logs"
    assert check["turns_checked"] == 2 and check["anomalies"]["turns"] == 2


@pytest.mark.parametrize("name", [p.stem for p in cs.corpus_files()])
@pytest.mark.parametrize("mode", ["both", "runtime_only"])
def test_every_corpus_session_reconciles_ok_against_its_runtime_logs(name, mode):
    corpus = cs.load_session(name)
    groups = ([corpus["log_group"], "aws/spans"] if mode == "both" else [corpus["log_group"]])
    provider = LaunchpadSpansProvider(cs.GroupLogs(corpus["records"]), groups)
    data = provider.get_evaluation_data(corpus["session_id"])
    ref = provider.origin_reference(corpus["session_id"])
    result = extraction_check.session_check(data, ref)
    assert ref["source"] == "runtime_logs" and result["status"] == "ok", result
    # a failed turn (no end_turn answer) is not compared
    failed = sum(1 for t in ref["turns"] if t is None)
    assert result["turns_checked"] == len(ref["turns"]) - failed


def test_runtime_logs_reference_catches_the_pre_obs315_extraction():
    from strands_evals.mappers import CloudWatchSessionMapper

    corpus = cs.load_session("257f17b16cbb")
    provider = LaunchpadSpansProvider(cs.GroupLogs(corpus["records"]),
                                      [corpus["log_group"], "aws/spans"],
                                      mapper=CloudWatchSessionMapper())
    data = provider.get_evaluation_data(corpus["session_id"])
    result = extraction_check.session_check(data,
                                            provider.origin_reference(corpus["session_id"]))
    assert result["status"] == "mismatch" and result["source"] == "runtime_logs"


def test_online_job_falls_back_to_the_runtime_logs_reference():
    corpus = TWO_TURN
    sid = corpus["session_id"]
    spec = {"config_id": "cfg", "mode": "scores", "sessions": [{"session_id": sid}],
            "log_groups": [corpus["log_group"], "aws/spans"], "spans_source": "both",
            "evaluator_defs": [], "judge_model_id": "judge"}
    sink = Sink()
    jobs_online.run(types.SimpleNamespace(job_id="j-online2", spec=spec), sink,
                    logs_client=cs.GroupLogs(corpus["records"]),
                    judge_factory=lambda *a, **kw: None)
    check = sink.finished[2]["extraction_checks"][sid]
    assert check["status"] == "ok" and check["source"] == "runtime_logs"


def test_online_job_reconciles_each_sampled_session(monkeypatch):
    corpus = PREAMBLE_SESSION
    sid = corpus["session_id"]
    spec = {"config_id": "cfg", "mode": "scores", "sessions": [{"session_id": sid}],
            "log_groups": [corpus["log_group"], "aws/spans"], "spans_source": "both",
            "evaluator_defs": [], "judge_model_id": "judge",
            "extraction_references": {sid: _ref(["我来帮您查询成都的住宿标准。"], "chat_ledger")}}
    sink = Sink()
    jobs_online.run(types.SimpleNamespace(job_id="j-online", spec=spec), sink,
                    logs_client=cs.GroupLogs(corpus["records"]),
                    judge_factory=lambda *a, **kw: None)
    check = sink.finished[2]["extraction_checks"][sid]
    assert check["status"] == "mismatch" and check["source"] == "chat_ledger"


# ── the hub: ledger references, the run fold, the payloads ────────────────────────

def test_ledger_reference_is_the_last_answer_bubble_of_each_user_turn():
    ws, agent, sid = "ws-check", "agent-check", "sess-ledger-" + "a" * 20
    for role, text, truncated in [
        ("user", "成都住宿标准？", False), ("agent", "我先查一下。", False), ("tool", "", False),
        ("agent", "已查到：450 元/晚", False),
        ("user", "那北京呢？", False), ("error", "boom", False),
        ("user", "写一篇长文", False), ("agent", "很长…（已达长度上限）", True),
    ]:
        chat_ledger.save_message(ws, agent, sid, role, text, truncated=truncated)
    refs = extraction_refs.references_for(ws, [sid, "sess-unknown-" + "b" * 20])
    assert set(refs) == {sid}
    ref = refs[sid]
    assert ref["source"] == "chat_ledger"
    assert [t and t["head"] for t in ref["turns"]] == ["已查到：450 元/晚", "", None]
    result = extraction_check.check(["已查到：450 元/晚", "", "很长"], ref)
    assert result["status"] == "ok" and result["turns_checked"] == 2
    assert extraction_refs.references_for("ws-other", [sid]) == {}


def test_plan_spec_ships_only_the_chosen_sessions_references():
    plan = service.RunPlan(
        run_id="r", job_type="batch_eval", target={}, replay=False, wait={},
        sessions=[{"session_id": "a"}, {"session_id": "b"}],
        extraction_references={"a": _ref(["x"]), "b": _ref(["y"])})
    assert set(plan.spec([{"session_id": "b"}])["extraction_references"]) == {"b"}
    plan.extraction_references = {}
    assert "extraction_references" not in plan.spec()


def test_run_fold_merges_the_shards_and_summarises():
    def done(checks: dict[str, Any]) -> JobDone:
        return JobDone(state="COMPLETED", summary={"extraction_checks": checks})

    folded = service.fold_extraction_checks([
        done({"s1": extraction_check.check(["a"], _ref(["a"]))}),
        done({"s2": extraction_check.check(["b"], _ref(["c"]))}),
        JobDone(state="COMPLETED", summary={}),
    ])
    assert set(folded["sessions"]) == {"s1", "s2"}
    assert folded["summary"]["status"] == "mismatch"
    assert folded["summary"]["mismatched_sessions"] == ["s2"]
    assert service.fold_extraction_checks([JobDone(state="COMPLETED")]) is None
