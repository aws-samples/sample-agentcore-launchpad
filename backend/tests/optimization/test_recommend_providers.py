"""Recommendation providers (N12 E2-4): registry / discovery, router validation, evidence
from ``eval_results``, the ``gepa_lite`` reflection (in-process twin and the worker-side
``recommend`` handler) and ``model_converse`` over a strands Model. The experiment-side
seams left with the configuration experiment (N13-obs2)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterable
from typing import Any

import pytest
from botocore.exceptions import ClientError

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import jobs
from app.evaluation.executor import JobSink, MemoryStore
from app.evaluation.jobs import JobDone, JobSpec
from app.evaluation.models import EvalResult, EvalRun
from app.optimization import providers as rp
from app.optimization.providers import evidence as ev
from app.optimization.providers import gepa_lite
from app.optimization.providers.base import (
    EvaluatorRecord,
    EvidenceStats,
    OptimizeRequest,
    SessionEvidence,
)
from app.services.strands_eval import jobs_recommend
from tests.conftest import ws_ctx
from tests.evaluation.fakes import FakeModel

WS = ws_ctx()


def _noop(_msg: str) -> None:
    """A progress sink."""


def _noop(_msg: str) -> None:
    """A progress sink."""
MODEL = "global.zai.glm-5.3"
# ─── helpers ─────────────────────────────────────────────────────────────────
def _mk_run(**kw) -> str:
    db = SessionLocal()
    run = EvalRun(
        workspace_id=DEFAULT_WORKSPACE_ID, agent_id=kw.pop("agent_id", "a1"),
        agent_name="agent", status=kw.pop("status", "completed"), **kw,
    )
    db.add(run)
    db.commit()
    rid = run.id
    db.close()
    return rid


def _row(run_id: str, sid: str, evaluator: str, score: float | None, *, explanation="why",
         label=None, error=None, ordinal=0) -> None:
    db = SessionLocal()
    db.add(EvalResult(
        id=jobs.record_id(run_id, sid, evaluator, ordinal), workspace_id=DEFAULT_WORKSPACE_ID,
        source="run", run_id=run_id, session_id=sid, evaluator_id=evaluator, level="SESSION",
        score=score, label=label, explanation=explanation if score is not None else None,
        error_type="RuntimeError" if error else None, error_message=error,
    ))
    db.commit()
    db.close()


def _record(sid: str, evaluator: str, score: float | None, explanation: str = "why",
            label: str | None = None, error: str | None = None) -> dict[str, Any]:
    return {"session_id": sid, "evaluator_id": evaluator, "score": score,
            "explanation": explanation if score is not None else None, "label": label,
            "level": "SESSION", "error_message": error}


def _evidence(n: int, scores: list[float] | None = None) -> list[SessionEvidence]:
    out = []
    for i in range(n):
        s = (scores or [0.5] * n)[i]
        out.append(SessionEvidence(
            session_id=f"s{i}",
            turns=[{"role": "user", "text": f"q{i}"}, {"role": "assistant", "text": f"a{i}"}],
            records=[EvaluatorRecord("Builtin.Helpfulness", s, "Ok", "meh")],
            mean_score=s,
        ))
    return out


def _req(evidence, model=MODEL, max_chars=8000, **kw):
    stats = EvidenceStats(
        sessions_scored=len(evidence), sessions_selected=len(evidence),
        sessions_with_transcript=sum(1 for e in evidence if e.turns),
        records=sum(len(e.records) for e in evidence),
    )
    return OptimizeRequest(
        current_prompt="You are helpful.", agent={"id": "a1"}, trace_source={},
        evidence=evidence, stats=stats, model_id=model, workspace=WS,
        max_chars=max_chars, **kw,
    )


def _converse(replies: list[str]):
    calls: list[dict[str, Any]] = []
    it = iter(replies)

    def fn(model_id, system, messages, max_tokens):
        calls.append({"model": model_id, "system": system, "messages": messages,
                      "max_tokens": max_tokens})
        return next(it), {"input_tokens": 10, "output_tokens": 5}

    fn.calls = calls  # type: ignore[attr-defined]
    return fn


# ─── registry + discovery ────────────────────────────────────────────────────
def test_registry_lists_gepa_lite_first_and_matches_the_router_literal():
    ids = [p.id for p in rp.list_providers()]
    assert ids == ["gepa_lite", "harness_optimizer"]
    assert tuple(ids) == rp.PROVIDER_IDS and rp.DEFAULT_PROVIDER == "gepa_lite"
    with pytest.raises(ValueError):
        rp.get_provider("nope")
    with pytest.raises(ValueError):
        rp.get_provider("agentcore")  # the AgentCore job is gone (C5)
    assert rp.get_provider(None).id == "gepa_lite"
    assert rp.get_provider("gepa_lite").job_type == "recommend"
    assert rp.get_provider("harness_optimizer").job_type == "optimize"


def test_provider_enabled_flags_follow_settings(monkeypatch):
    assert rp.provider_enabled(rp.get_provider("gepa_lite")) is True
    harness = rp.get_provider("harness_optimizer")
    assert rp.provider_enabled(harness) is False  # R-4: shell tool → operator opt-in
    monkeypatch.setattr(get_settings(), "prompt_opt_harness_optimizer_enabled", True)
    assert rp.provider_enabled(harness) is True


def test_providers_endpoint_shape_follows_settings(client):
    res = client.get("/api/eval/recommendation-providers")
    assert res.status_code == 200
    provs = {p["id"]: p for p in res.json()["providers"]}
    assert list(provs) == ["gepa_lite", "harness_optimizer"]
    settings = get_settings()
    g = provs["gepa_lite"]
    assert g["requires_source"] is True and g["enabled"] is True
    assert g["supports"] == ["system_prompt", "tool_descriptions"]
    assert [m["model_id"] for m in g["models"]] == settings.prompt_opt_models
    assert g["default_model_id"] == settings.prompt_opt_default_model_id
    assert g["models"][0]["label"] != g["models"][0]["model_id"]  # catalog label
    h = provs["harness_optimizer"]
    assert h["enabled"] is False and h["supports"] == ["system_prompt"]
    assert h["requires_source"] is True
    assert [m["model_id"] for m in h["models"]] == settings.prompt_opt_models


# ─── router validation ───────────────────────────────────────────────────────
# ─── stage dispatch ──────────────────────────────────────────────────────────
# ─── evidence (eval_results rows) ────────────────────────────────────────────
def test_read_run_records_reads_only_the_runs_rows_in_order():
    rid, other = _mk_run(), _mk_run()
    _row(rid, "s1", "Builtin.Helpfulness", 1.0)
    _row(rid, "s2", "Builtin.Helpfulness", 0.0, label="No")
    _row(rid, "s2", "Builtin.Correctness", None, error="judge timeout")
    _row(other, "s9", "Builtin.Helpfulness", 0.5)
    db = SessionLocal()
    records = ev.read_run_records(db, rid, DEFAULT_WORKSPACE_ID)
    db.close()
    assert [(r["session_id"], r["evaluator_id"]) for r in records] == [
        ("s1", "Builtin.Helpfulness"), ("s2", "Builtin.Helpfulness"), ("s2", "Builtin.Correctness")]
    assert records[1]["label"] == "No" and records[2]["error_message"] == "judge timeout"
    assert set(records[0]) == {"session_id", "trace_id", "evaluator_id", "level", "score",
                               "label", "explanation", "error_type", "error_message"}


def test_group_and_polarity_mean():
    grouped = ev.group_records([
        _record("s1", "Builtin.Helpfulness", 1.0),
        _record("s1", "Builtin.Refusal", 1.0, label="Yes"),
        _record("s1", "Builtin.Correctness", None, error="judge timeout"),
        _record("s2", "Builtin.Helpfulness", 0.5),
    ])
    assert set(grouped) == {"s1", "s2"}
    # Refusal is a penalty: (1.0 + -1.0) / 2 = 0 — a refusing session is not "good"
    assert ev.mean_polarized(grouped["s1"]) == 0.0
    assert grouped["s1"][2].error == "judge timeout"
    assert ev.mean_polarized([EvaluatorRecord("x", None, None, None)]) is None


def test_select_sessions_worst_first_with_contrast():
    grouped = {
        f"s{i:02d}": [EvaluatorRecord("Builtin.Helpfulness", i / 40, None, None)]
        for i in range(40)
    }
    grouped["unscored"] = [EvaluatorRecord("Builtin.Helpfulness", None, None, None, error="e")]
    chosen = ev.select_sessions(grouped, 30)
    assert len(chosen) == 30
    assert "unscored" not in chosen
    # 6 contrast slots (20 % of 30) go to the best sessions …
    assert {"s39", "s38", "s37", "s36", "s35", "s34"} <= set(chosen)
    # … the rest are the 24 worst, in worst-first reading order
    assert chosen[:24] == [f"s{i:02d}" for i in range(24)]
    # small pools are returned whole, worst first
    small = {k: grouped[k] for k in ("s05", "s01", "s03")}
    assert ev.select_sessions(small, 30) == ["s01", "s03", "s05"]


def test_select_sessions_contrast_floor_is_three():
    grouped = {f"s{i:02d}": [EvaluatorRecord("e", i / 10, None, None)] for i in range(10)}
    chosen = ev.select_sessions(grouped, 5)
    assert {"s09", "s08", "s07"} <= set(chosen)
    assert chosen[:2] == ["s00", "s01"]


def test_truncate_text_keeps_head_and_tail():
    text = "A" * 1200 + "B" * 500 + "C" * 300
    out = ev.truncate_text(text)
    assert out.startswith("A" * 1200) and out.endswith("C" * 300)
    assert "500 chars omitted" in out
    assert ev.truncate_text("short") == "short"


def test_collect_evidence_is_fail_soft_per_transcript():
    records = [_record("s1", "e", 0.0), _record("s2", "e", 1.0)]

    def transcript(sid):
        if sid == "s1":
            raise RuntimeError("memory down")
        return [{"role": "user", "text": "  "}, {"role": "assistant", "text": "x" * 2000}]

    evidence, stats = ev.collect_evidence(records=records, transcript=transcript,
                                          max_sessions=30)
    assert [e.session_id for e in evidence] == ["s1", "s2"]
    assert evidence[0].turns == []
    assert len(evidence[1].turns) == 1  # blank turn dropped, long one truncated
    assert "omitted" in evidence[1].turns[0]["text"]
    assert stats.sessions_scored == 2 and stats.sessions_selected == 2
    assert stats.sessions_with_transcript == 1 and stats.records == 2
    payload = ev.evidence_payload(evidence, stats)
    assert payload["stats"]["records"] == 2
    assert payload["evidence"][1]["records"][0] == {
        "evaluator_id": "e", "score": 1.0, "label": None, "explanation": "why",
        "level": "SESSION", "error": None}


def test_insight_feedback_flattens_failure_tree():
    lines = ev.insight_feedback({"failures": [
        {"category": "Accuracy", "subCategories": [
            {"subCategory": "math", "rootCauses": [
                {"rootCause": "skips units", "recommendation": "state units"}]}]},
        {"category": "flat", "rootCauses": [{"rootCause": "no sub"}]},
        {"category": "detector", "rootCauses": [
            {"rootCause": "tool misuse", "fix_recommendation": "describe the tool"}]},
    ]})
    assert lines[0] == "Accuracy / math: skips units → state units"
    assert lines[1] == "flat: no sub"
    assert lines[2] == "detector: tool misuse → describe the tool"
    assert ev.insight_feedback(None) == []


# ─── gepa_lite provider (in-process twin) ────────────────────────────────────
def test_gepa_lite_happy_path_and_meta():
    prov = gepa_lite.GepaLiteProvider()
    converse = _converse(["```json\n" + json.dumps({
        "diagnosis": "d", "changes": ["c1", "c2"], "revised_prompt": "P2"}) + "\n```"])
    res = prov.optimize(_req(_evidence(2)), _noop, converse=converse)
    assert res.status == "COMPLETED"
    assert res.recommended_prompt == "P2"
    assert res.explanation == "d\n- c1\n- c2"
    assert res.meta["changes"] == ["c1", "c2"]
    assert res.meta["calls"] == 1 and res.meta["input_tokens"] == 10
    assert res.meta["evidence_sessions"] == 2
    assert "latency_ms" in res.meta
    call = converse.calls[0]
    assert call["model"] == MODEL
    assert "8000 characters" in call["system"]
    assert "You are helpful." in call["messages"][0]["text"]


def test_gepa_lite_no_scored_sessions_makes_no_call():
    prov = gepa_lite.GepaLiteProvider()
    converse = _converse([])
    evidence = _evidence(1)
    evidence[0].mean_score = None
    res = prov.optimize(_req(evidence), _noop, converse=converse)
    assert res.status == "FAILED"
    assert "no scored sessions" in res.error
    assert converse.calls == []


def test_gepa_lite_retries_unparseable_json_once():
    prov = gepa_lite.GepaLiteProvider()
    converse = _converse(["sorry, here is prose", json.dumps({"revised_prompt": "P"})])
    res = prov.optimize(_req(_evidence(1)), _noop, converse=converse)
    assert res.status == "COMPLETED" and res.recommended_prompt == "P"
    assert len(converse.calls) == 2
    assert "ONLY the JSON" in converse.calls[1]["messages"][0]["text"]
    converse = _converse(["nope", "still nope"])
    res = prov.optimize(_req(_evidence(1)), _noop, converse=converse)
    assert res.status == "FAILED" and "parseable JSON" in res.error
    assert res.recommended_prompt is None


def test_gepa_lite_client_error_becomes_failed_with_aws_code():
    prov = gepa_lite.GepaLiteProvider()

    def converse(*_a):
        raise ClientError({"Error": {"Code": "AccessDeniedException",
                                     "Message": "no model access"}}, "Converse")

    res = prov.optimize(_req(_evidence(1)), _noop, converse=converse)
    assert res.status == "FAILED"
    assert res.error == "AccessDeniedException: no model access"
    assert res.recommended_prompt is None


def test_gepa_lite_over_budget_compresses_once_then_fails():
    prov = gepa_lite.GepaLiteProvider()
    long = "x" * 120
    converse = _converse([json.dumps({"revised_prompt": long}),
                          json.dumps({"revised_prompt": "y" * 90})])
    res = prov.optimize(_req(_evidence(1), max_chars=100), _noop, converse=converse)
    assert res.status == "COMPLETED" and res.recommended_prompt == "y" * 90
    assert "100 characters" in converse.calls[1]["system"]
    converse = _converse([json.dumps({"revised_prompt": long}),
                          json.dumps({"revised_prompt": long})])
    res = prov.optimize(_req(_evidence(1), max_chars=100), _noop, converse=converse)
    assert res.status == "FAILED"
    assert "exceeds 100 chars" in res.error
    assert res.recommended_prompt is None
    assert res.meta["calls"] == 2


def test_render_reflective_dataset_shows_errors_and_labels():
    evidence = _evidence(1)
    evidence[0].records.append(EvaluatorRecord("Builtin.Correctness", None, None, None,
                                               error="judge timeout"))
    evidence[0].records[0].label = "Helpful"
    text = gepa_lite.render_reflective_dataset(evidence)
    assert "Builtin.Helpfulness = 0.5 (Helpful) — \"meh\"" in text
    assert "evaluator error — judge timeout" in text
    assert "Inputs:\n  - q0" in text and "Generated Outputs:\n  - a0" in text


def test_gepa_lite_default_converse_builds_the_model_through_the_judge_factory(monkeypatch):
    seen: dict[str, Any] = {}

    def fake_model(model_id, *, region, max_tokens=1024, **cfg):
        seen.update(model_id=model_id, region=region, max_tokens=max_tokens)
        return FakeModel(texts=[json.dumps({"diagnosis": "d", "revised_prompt": "P2"})])

    monkeypatch.setattr(gepa_lite.aws_clients, "global_bedrock_model", fake_model)
    res = gepa_lite.GepaLiteProvider().optimize(_req(_evidence(1)), _noop)
    assert res.status == "COMPLETED" and res.recommended_prompt == "P2"
    assert seen == {"model_id": MODEL, "region": get_settings().model_region, "max_tokens": 4096}


def test_build_spec_is_plain_json_for_the_worker():
    req = _req(_evidence(2), components=("system_prompt", "tool_descriptions"),
               tools={"t": "d"}, extra_feedback=["fix the tone"])
    spec = gepa_lite.GepaLiteProvider().build_spec(req)
    assert set(spec) == {"provider", "model_id", "current_prompt", "components", "tools",
                         "evidence", "stats", "extra_feedback", "max_chars", "max_tokens",
                         "max_tokens_cap",
                         "trace_source"}
    assert spec["components"] == ["system_prompt", "tool_descriptions"]
    assert spec["evidence"][0]["records"][0]["evaluator_id"] == "Builtin.Helpfulness"
    json.dumps(spec)  # serialisable as a job payload


# ─── worker-side recommend handler + model_converse ──────────────────────────
JOB_ID = "provider-gepa_lite-0123456789ab"


def _job(spec: dict[str, Any]) -> JobSpec:
    return JobSpec(job_id=JOB_ID, job_type="recommend",
                   workspace={"account_id": "1", "region": "cn-northwest-1"},
                   sink={"bucket": "b", "prefix": jobs.job_prefix(JOB_ID, 1)}, spec=spec)


def test_recommend_handler_runs_the_reflection_and_reports_in_done_json():
    store = MemoryStore()
    req = _req(_evidence(2))
    job = _job(gepa_lite.GepaLiteProvider().build_spec(req))
    factory_calls: list[tuple] = []

    def judge_factory(model_id, **kw):
        factory_calls.append((model_id, kw))
        return FakeModel(texts=[json.dumps({"diagnosis": "d", "changes": ["c"],
                                            "revised_prompt": "P2"})])

    jobs_recommend.run(job, JobSink(store, job), judge_factory=judge_factory)
    assert factory_calls == [(MODEL, {"max_tokens": 4096, "temperature": 0.0})]
    done = JobDone.from_dict(json.loads(store.get(jobs.done_key(JOB_ID, 1)).decode()))
    assert done.state == "COMPLETED" and done.summary["provider"] == "gepa_lite"
    rec = done.summary["recommendation"]
    assert rec["status"] == "COMPLETED" and rec["recommended_prompt"] == "P2"
    assert rec["explanation"] == "d\n- c" and rec["meta"]["calls"] == 1
    artifact = json.loads(store.get(jobs.artifact_key(JOB_ID, 1, "recommendation.json")).decode())
    assert artifact == rec


class _ReasoningModel(FakeModel):
    """GLM / Kimi stream a reasoning block before the answer — never part of the text."""

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs
                     ) -> AsyncIterable[dict[str, Any]]:
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {}}}
        yield {"contentBlockDelta": {"delta": {"reasoningContent": {"text": "thinking…"}}}}
        yield {"contentBlockStop": {}}
        yield {"contentBlockDelta": {"delta": {"text": self.texts.pop(0)}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}
        yield {"metadata": {"usage": {"inputTokens": 7, "outputTokens": 3, "totalTokens": 10},
                            "metrics": {"latencyMs": 1}}}


def test_model_converse_returns_text_only_with_usage_and_applies_max_tokens():
    model = _ReasoningModel(texts=['{"revised_prompt": "P"}'])
    converse = jobs_recommend.model_converse(model)
    text, usage = converse(MODEL, "sys", [{"role": "user", "text": "hi"}], 777)
    assert text == '{"revised_prompt": "P"}'
    assert usage == {"input_tokens": 7, "output_tokens": 3, "stop_reason": "end_turn"}
    assert model.get_config()["max_tokens"] == 777
    with pytest.raises(ValueError, match="Model object"):
        jobs_recommend.model_converse("global.zai.glm-5.3")  # a str would default to Claude


# ─── experiment-side seams (W-E3) ────────────────────────────────────────────
# ─── N-SCN-bug2: the reflection under the output budget ──────────────────────
class _CutOffModel(FakeModel):
    """Streams part of the answer, then stops with ``max_tokens`` — strands raises
    MaxTokensReachedException after appending the partial message to the history."""

    def __init__(self, partial: str, **kw):
        super().__init__(**kw)
        self.partial = partial

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs
                     ) -> AsyncIterable[dict[str, Any]]:
        self.calls.append({"kind": "stream", "max_tokens": self.get_config().get("max_tokens")})
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {}}}
        yield {"contentBlockDelta": {"delta": {"reasoningContent": {"text": "thinking…"}}}}
        yield {"contentBlockStop": {}}
        yield {"contentBlockDelta": {"delta": {"text": self.partial}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "max_tokens"}}


def test_model_converse_hands_back_the_partial_text_on_a_max_tokens_stop():
    """Counting run #2 (b): strands raises on a max_tokens stop before `reflect` ever saw a
    stop_reason, so its budget retry was dead code and the stage failed generically."""
    model = _CutOffModel('{"diagnosis": "the agent', model_id=MODEL)
    text, usage = jobs_recommend.model_converse(model)(MODEL, "sys", [{"role": "user",
                                                                       "text": "hi"}], 512)
    assert text == '{"diagnosis": "the agent' and usage["stop_reason"] == "max_tokens"
    assert model.get_config()["max_tokens"] == 512


def test_a_cut_off_reflection_that_still_parses_is_accepted_without_a_retry():
    prov = gepa_lite.GepaLiteProvider()
    calls: list[int] = []

    def converse(model_id, system, messages, max_tokens):
        calls.append(max_tokens)
        return (json.dumps({"diagnosis": "d", "revised_prompt": "P2"}) + " and then it was cut",
                {"input_tokens": 1, "output_tokens": max_tokens, "stop_reason": "max_tokens"})

    res = prov.optimize(_req(_evidence(1), max_tokens=4096), _noop, converse=converse)
    assert res.status == "COMPLETED" and res.recommended_prompt == "P2"
    assert calls == [4096]


def test_a_truncated_reflection_doubles_the_budget_at_most_twice_and_names_the_last():
    prov = gepa_lite.GepaLiteProvider()
    calls: list[tuple[int, bool]] = []
    notes: list[str] = []

    def converse(model_id, system, messages, max_tokens):
        calls.append((max_tokens, messages[0]["text"].endswith(jobs_recommend.TRUNCATED_SUFFIX)))
        return '{"diagnosis": "cut', {"input_tokens": 1, "output_tokens": max_tokens,
                                      "stop_reason": "max_tokens"}

    res = prov.optimize(_req(_evidence(1), max_tokens=8192, max_tokens_cap=16384), notes.append,
                        converse=converse)
    assert calls == [(8192, False), (16384, True)]  # 8192 → 16384 = the cap, then stop
    assert res.status == "FAILED" and res.recommended_prompt is None
    assert "truncated by the token limit" in res.error
    assert "reflection exceeded 16384 output tokens after 2 attempts" in res.error
    assert "raise prompt_opt_max_tokens" in res.error
    assert any("retrying with 16384 tokens (attempt 2/3)" in n for n in notes)

    res = prov.optimize(_req(_evidence(1), max_tokens=4096, max_tokens_cap=16384), _noop,
                        converse=converse)
    assert "reflection exceeded 16384 output tokens after 3 attempts" in res.error


def test_recommend_job_spec_carries_the_cap_to_the_worker_reflection():
    store = MemoryStore()
    req = _req(_evidence(2), max_tokens=1024, max_tokens_cap=2048)
    spec = gepa_lite.GepaLiteProvider().build_spec(req)
    assert spec["max_tokens_cap"] == 2048
    job = _job(spec)
    budgets: list[int] = []

    class _Tight(FakeModel):
        async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
            budget = int(self.get_config()["max_tokens"])
            budgets.append(budget)
            if budget < 2048:
                yield {"messageStart": {"role": "assistant"}}
                yield {"contentBlockDelta": {"delta": {"text": '{"diag'}}}
                yield {"contentBlockStop": {}}
                yield {"messageStop": {"stopReason": "max_tokens"}}
                return
            async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
                yield event

    jobs_recommend.run(job, JobSink(store, job), judge_factory=lambda model_id, **kw: _Tight(
        texts=[json.dumps({"diagnosis": "d", "revised_prompt": "P2"})]))
    done = JobDone.from_dict(json.loads(store.get(jobs.done_key(JOB_ID, 1)).decode()))
    assert budgets == [1024, 2048]
    assert done.summary["recommendation"]["status"] == "COMPLETED"
    assert done.summary["recommendation"]["recommended_prompt"] == "P2"
