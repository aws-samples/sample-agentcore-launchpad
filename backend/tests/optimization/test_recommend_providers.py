"""3rd-party recommendation providers — registry/discovery, router validation,
stage dispatch, evidence collection + sampling, the gepa_lite reflection round,
regeneration key ownership and treatment-bundle attribution."""

import json
from typing import Any

import pytest
from botocore.exceptions import ClientError

import app.optimization.service as svc
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation.models import EvalRun
from app.optimization import providers as rp
from app.optimization.models import Experiment
from app.optimization.providers import evidence as ev
from app.optimization.providers import gepa_lite
from app.optimization.providers.base import (
    EvaluatorRecord,
    EvidenceStats,
    OptimizeRequest,
    SessionEvidence,
)
from tests.conftest import ws_ctx

WS = ws_ctx()


# ─── helpers ─────────────────────────────────────────────────────────────────
def _mk_exp(**kw):
    db = SessionLocal()
    exp = Experiment(
        workspace_id=DEFAULT_WORKSPACE_ID,
        name="EXP-p", agent_id=kw.pop("agent_id", "a1"), agent_name="agent", **kw
    )
    db.add(exp)
    db.commit()
    db.refresh(exp)
    db.close()
    return exp


def _reload(exp_id: str) -> Experiment:
    db = SessionLocal()
    try:
        return db.get(Experiment, exp_id)
    finally:
        db.close()


def _mk_run(**kw) -> str:
    db = SessionLocal()
    run = EvalRun(
        workspace_id=DEFAULT_WORKSPACE_ID, agent_id=kw.pop("agent_id", "a1"),
        agent_name="agent", status=kw.pop("status", "completed"),
        batch_eval_id=kw.pop("batch_eval_id", "be-1"), **kw,
    )
    db.add(run)
    db.commit()
    rid = run.id
    db.close()
    return rid


def _record(sid: str, evaluator: str, score: float | None, explanation: str = "why",
            label: str | None = None, error: str | None = None) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "gen_ai.evaluation.name": evaluator,
        "session.id": sid,
        "aws.bedrock_agentcore.evaluation_level": "Session",
    }
    if score is not None:
        attrs["gen_ai.evaluation.score.value"] = score
        attrs["gen_ai.evaluation.explanation"] = explanation
    if label:
        attrs["gen_ai.evaluation.score.label"] = label
    if error:
        attrs["error.message"] = error
    return {"message": json.dumps({"name": "gen_ai.evaluation.result", "attributes": attrs})}


class FakeLogs:
    """get_log_events with real forward-token pagination semantics."""

    def __init__(self, pages: list[list[dict[str, Any]]]):
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    def get_log_events(self, **kw):
        self.calls.append(kw)
        idx = int(kw.get("nextToken") or 0)
        events = self.pages[idx] if idx < len(self.pages) else []
        # like AWS: the page after the last one is empty and repeats its token
        nxt = str(idx + 1) if idx < len(self.pages) else str(idx)
        return {"events": events, "nextForwardToken": nxt}


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


def _req(evidence, model="global.anthropic.claude-sonnet-5", max_chars=8000, **kw):
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


def _inline(monkeypatch):
    monkeypatch.setattr(svc, "_spawn", lambda target: target())


# ─── registry + discovery ────────────────────────────────────────────────────
def test_registry_lists_agentcore_first_and_matches_router_literal():
    ids = [p.id for p in rp.list_providers()]
    assert ids == ["agentcore", "gepa_lite"]
    assert tuple(ids) == rp.PROVIDER_IDS
    with pytest.raises(ValueError):
        rp.get_provider("nope")
    assert rp.get_provider(None).id == "agentcore"


def test_providers_endpoint_shape_follows_settings(client):
    res = client.get("/api/experiments/providers")
    assert res.status_code == 200
    provs = {p["id"]: p for p in res.json()["providers"]}
    assert provs["agentcore"]["requires_source"] is False
    assert provs["agentcore"]["models"] == []
    assert provs["agentcore"]["default_model_id"] is None
    assert "tool_descriptions" in provs["agentcore"]["supports"]
    g = provs["gepa_lite"]
    assert g["requires_source"] is True
    assert g["supports"] == ["system_prompt", "tool_descriptions"]
    settings = get_settings()
    assert [m["model_id"] for m in g["models"]] == settings.prompt_opt_models
    assert g["default_model_id"] == settings.prompt_opt_default_model_id
    assert g["models"][0]["label"] != g["models"][0]["model_id"]  # catalog label


# ─── router validation ───────────────────────────────────────────────────────
def test_gepa_lite_requires_a_pinned_source(client, monkeypatch):
    spawned: list = []
    monkeypatch.setattr(svc, "run_action", lambda *a, **k: spawned.append(a))
    exp = _mk_exp(artifacts={"agent_meta": {"system_prompt": "cur"}})
    res = client.post(f"/api/experiments/{exp.id}/action",
                      json={"action": "recommend", "recommend_provider": "gepa_lite"})
    assert res.status_code == 422
    assert res.json()["code"] == "experiment.provider_requires_source"
    assert spawned == []
    assert "recommend" not in _reload(exp.id).artifacts


def test_gepa_lite_tool_descriptions_only_also_needs_a_source(client, monkeypatch):
    """gepa_lite now owns tool descriptions too, so the source rule applies to a
    tool-only request; the AgentCore default keeps bypassing it."""
    captured: dict = {}
    monkeypatch.setattr(svc, "run_action", lambda *a, **k: captured.update(ok=True))
    exp = _mk_exp(artifacts={"agent_meta": {"system_prompt": "cur"}})
    res = client.post(f"/api/experiments/{exp.id}/action",
                      json={"action": "recommend", "recommend_provider": "gepa_lite",
                            "recommend_types": ["tool_descriptions"]})
    assert res.status_code == 422
    assert res.json()["code"] == "experiment.provider_requires_source"
    assert captured == {}
    res = client.post(f"/api/experiments/{exp.id}/action",
                      json={"action": "recommend", "recommend_types": ["tool_descriptions"]})
    assert res.status_code == 202
    assert captured == {"ok": True}


def test_unknown_provider_and_bad_model_id_are_422(client):
    exp = _mk_exp(artifacts={"agent_meta": {"system_prompt": "cur"}})
    res = client.post(f"/api/experiments/{exp.id}/action",
                      json={"action": "recommend", "recommend_provider": "dspy"})
    assert res.status_code == 422
    res = client.post(f"/api/experiments/{exp.id}/action",
                      json={"action": "recommend", "recommend_model_id": "bad id!"})
    assert res.status_code == 422
    assert res.json()["code"] == "experiment.model_id_invalid"


def test_provider_and_model_reach_act_recommend(client, monkeypatch):
    captured: dict = {}

    def fake_act(exp_id, progress, types=None, tools=None, source=None,
                 provider=None, model_id=None):
        captured.update(provider=provider, model_id=model_id, source=source)

    monkeypatch.setattr(svc, "act_recommend", fake_act)
    _inline(monkeypatch)
    monkeypatch.setattr(svc, "resolve_recommend_source",
                        lambda *_a, **_k: {"kind": "batch_evaluation", "run_id": "r1"})
    exp = _mk_exp(artifacts={"agent_meta": {"system_prompt": "cur"}})
    res = client.post(
        f"/api/experiments/{exp.id}/action",
        json={"action": "recommend", "recommend_provider": "gepa_lite",
              "recommend_model_id": "global.anthropic.claude-opus-5",
              "recommend_source_run_id": "r1"},
    )
    assert res.status_code == 202
    assert captured["provider"] == "gepa_lite"
    assert captured["model_id"] == "global.anthropic.claude-opus-5"


# ─── stage dispatch ──────────────────────────────────────────────────────────
def test_default_provider_never_touches_the_provider_path(monkeypatch):
    called: list = []
    monkeypatch.setattr(svc, "_third_party_prompt_recommendation",
                        lambda *a, **k: called.append(1))
    fake_data = type("D", (), {})()
    monkeypatch.setattr(svc, "data_client", lambda ws: fake_data)
    monkeypatch.setattr(svc.ac, "start_system_prompt_recommendation",
                        lambda *a, **k: {"recommendationId": "r"})
    monkeypatch.setattr(svc.ac, "poll_recommendation", lambda *a, **k: {
        "status": "COMPLETED",
        "recommendationResult": {"systemPromptRecommendationResult": {
            "recommendedSystemPrompt": "better", "explanation": "e"}},
    })
    agent = {"resource_id": "rid", "runtime_name": "rt", "system_prompt": "cur", "tools": {}}
    out = svc.stage_recommend("e1", agent, WS, types=("system_prompt",))
    assert called == []
    assert out["recommended_prompt"] == "better"
    assert "provider" not in out  # AgentCore artifact stays byte-identical


def test_gepa_lite_dispatch_skips_start_recommendation(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("StartRecommendation must not be called")

    monkeypatch.setattr(svc.ac, "start_system_prompt_recommendation", boom)
    monkeypatch.setattr(svc, "data_client", lambda ws: object())
    monkeypatch.setattr(
        svc, "_third_party_prompt_recommendation",
        lambda *a, **k: {"provider": k["provider_id"], "provider_model_id": k["model_id"],
                         "system_prompt_status": "COMPLETED", "recommended_prompt": "p"},
    )
    agent = {"resource_id": "rid", "runtime_name": "rt", "system_prompt": "cur", "tools": {}}
    out = svc.stage_recommend("e1", agent, WS, types=("system_prompt",),
                              provider="gepa_lite", model_id="m",
                              source={"run_id": "r", "batch_evaluation_arn": "arn:be"})
    assert out["provider"] == "gepa_lite"
    # the pinned source is recorded as the trace source, exactly as on the AWS path
    assert out["trace_source"]["run_id"] == "r"
    assert out["trace_source"]["batch_evaluation_arn"] == "arn:be"


def test_resolve_source_carries_results_stream(monkeypatch):
    rid = _mk_run()
    monkeypatch.setattr(svc, "data_client", lambda ws: object())
    monkeypatch.setattr(svc.ac, "get_batch_evaluation", lambda *_a, **_k: {
        "batchEvaluationArn": "arn:be",
        "outputConfig": {"cloudWatchConfig": {"logGroupName": "/g", "logStreamName": "run-x"}},
    })
    src = svc.resolve_recommend_source("a1", rid, WS)
    assert src["results_log_group"] == "/g"
    assert src["results_log_stream"] == "run-x"


def test_third_party_path_writes_attributed_keys(monkeypatch):
    rid = _mk_run(mode="insights", insights={"failures": [
        {"category": "Tone", "subCategories": [{"subCategory": "curt", "rootCauses": [
            {"rootCause": "one-word answers", "recommendation": "explain"}]}]}]})
    logs = FakeLogs([[_record("s1", "Builtin.Helpfulness", 0.0, "too short"),
                      _record("s2", "Builtin.Helpfulness", 1.0, "great")]])
    converse = _converse([json.dumps({
        "diagnosis": "Answers are too short.",
        "changes": ["Ask the agent to explain its reasoning"],
        "revised_prompt": "You are helpful. Explain your reasoning.",
    })])
    out = svc._third_party_prompt_recommendation(
        "e1", {"id": "a1", "system_prompt": "You are helpful."}, WS, svc._noop,
        provider_id="gepa_lite", model_id=None,
        source={"run_id": rid, "results_log_group": "/g", "results_log_stream": "run-x"},
        transcript=lambda sid: [{"role": "user", "text": "hi"},
                                {"role": "assistant", "text": "yo"}],
        logs=logs, converse=converse,
    )
    assert out["system_prompt_status"] == "COMPLETED"
    assert out["recommended_prompt"] == "You are helpful. Explain your reasoning."
    assert out["provider"] == "gepa_lite"
    assert out["provider_model_id"] == get_settings().prompt_opt_default_model_id
    assert out["provider_meta"]["evidence_sessions"] == 2
    assert out["provider_meta"]["evidence_records"] == 2
    assert "explain its reasoning" in out["explanation"]
    # insight clusters reached the reflection prompt as extra feedback
    assert "one-word answers" in converse.calls[0]["messages"][0]["text"]
    # worst session is rendered first
    body = converse.calls[0]["messages"][0]["text"]
    assert body.index("session s1") < body.index("session s2")


def test_third_party_path_without_results_stream_fails_cleanly():
    out = svc._third_party_prompt_recommendation(
        "e1", {"id": "a1", "system_prompt": "cur"}, WS, svc._noop,
        provider_id="gepa_lite", model_id="m", source={"run_id": "r"},
    )
    assert out["system_prompt_status"] == "FAILED"
    assert "results log stream" in out["system_prompt_error"]
    assert "recommended_prompt" not in out
    assert svc.system_prompt_rec_failed(out)


# ─── evidence ────────────────────────────────────────────────────────────────
def test_read_result_records_paginates_until_token_repeats():
    logs = FakeLogs([
        [_record("s1", "Builtin.Helpfulness", 1.0)],
        [_record("s2", "Builtin.Helpfulness", 0.0), {"message": "not json"}],
        [{"message": json.dumps({"attributes": {"something.else": 1}})}],
    ])
    attrs = ev.read_result_records(logs, "/g", "run-x")
    assert [a["session.id"] for a in attrs] == ["s1", "s2"]
    # three pages + the empty end-of-stream page whose token repeats
    assert len(logs.calls) == 4
    assert logs.calls[0].get("nextToken") is None
    assert logs.calls[-1]["nextToken"] == "3"


def test_group_and_polarity_mean():
    grouped = ev.group_records([
        json.loads(_record("s1", "Builtin.Helpfulness", 1.0)["message"])["attributes"],
        json.loads(_record("s1", "Builtin.Refusal", 1.0, label="Yes")["message"])["attributes"],
        json.loads(_record("s1", "Builtin.Correctness", None,
                           error="judge timeout")["message"])["attributes"],
        json.loads(_record("s2", "Builtin.Helpfulness", 0.5)["message"])["attributes"],
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
    logs = FakeLogs([[_record("s1", "e", 0.0), _record("s2", "e", 1.0)]])

    def transcript(sid):
        if sid == "s1":
            raise RuntimeError("memory down")
        return [{"role": "user", "text": "  "}, {"role": "assistant", "text": "x" * 2000}]

    evidence, stats = ev.collect_evidence(
        workspace=WS, log_group="/g", log_stream="run-x", transcript=transcript,
        max_sessions=30, logs=logs,
    )
    assert [e.session_id for e in evidence] == ["s1", "s2"]
    assert evidence[0].turns == []
    assert len(evidence[1].turns) == 1  # blank turn dropped, long one truncated
    assert "omitted" in evidence[1].turns[0]["text"]
    assert stats.sessions_scored == 2 and stats.sessions_selected == 2
    assert stats.sessions_with_transcript == 1 and stats.records == 2


def test_insight_feedback_flattens_failure_tree():
    lines = ev.insight_feedback({"failures": [
        {"category": "Accuracy", "subCategories": [
            {"subCategory": "math", "rootCauses": [
                {"rootCause": "skips units", "recommendation": "state units"}]}]},
        {"category": "flat", "rootCauses": [{"rootCause": "no sub"}]},
    ]})
    assert lines[0] == "Accuracy / math: skips units → state units"
    assert lines[1] == "flat: no sub"
    assert ev.insight_feedback(None) == []


# ─── gepa_lite provider ──────────────────────────────────────────────────────
def test_gepa_lite_happy_path_and_meta():
    prov = gepa_lite.GepaLiteProvider()
    converse = _converse(["```json\n" + json.dumps({
        "diagnosis": "d", "changes": ["c1", "c2"], "revised_prompt": "P2"}) + "\n```"])
    res = prov.optimize(_req(_evidence(2)), svc._noop, converse=converse)
    assert res.status == "COMPLETED"
    assert res.recommended_prompt == "P2"
    assert res.explanation == "d\n- c1\n- c2"
    assert res.meta["changes"] == ["c1", "c2"]
    assert res.meta["calls"] == 1 and res.meta["input_tokens"] == 10
    assert res.meta["evidence_sessions"] == 2
    assert "latency_ms" in res.meta
    call = converse.calls[0]
    assert call["model"] == "global.anthropic.claude-sonnet-5"
    assert "8000 characters" in call["system"]
    assert "You are helpful." in call["messages"][0]["text"]


def test_gepa_lite_no_scored_sessions_makes_no_call():
    prov = gepa_lite.GepaLiteProvider()
    converse = _converse([])
    evidence = _evidence(1)
    evidence[0].mean_score = None
    res = prov.optimize(_req(evidence), svc._noop, converse=converse)
    assert res.status == "FAILED"
    assert "no scored sessions" in res.error
    assert converse.calls == []


def test_gepa_lite_retries_unparseable_json_once():
    prov = gepa_lite.GepaLiteProvider()
    converse = _converse(["sorry, here is prose", json.dumps({"revised_prompt": "P"})])
    res = prov.optimize(_req(_evidence(1)), svc._noop, converse=converse)
    assert res.status == "COMPLETED" and res.recommended_prompt == "P"
    assert len(converse.calls) == 2
    assert "ONLY the JSON" in converse.calls[1]["messages"][0]["text"]
    converse = _converse(["nope", "still nope"])
    res = prov.optimize(_req(_evidence(1)), svc._noop, converse=converse)
    assert res.status == "FAILED" and "parseable JSON" in res.error
    assert res.recommended_prompt is None


def test_gepa_lite_client_error_becomes_failed_with_aws_code():
    prov = gepa_lite.GepaLiteProvider()

    def converse(*_a):
        raise ClientError({"Error": {"Code": "AccessDeniedException",
                                     "Message": "no model access"}}, "Converse")

    res = prov.optimize(_req(_evidence(1)), svc._noop, converse=converse)
    assert res.status == "FAILED"
    assert res.error == "AccessDeniedException: no model access"
    assert res.recommended_prompt is None


def test_gepa_lite_over_budget_compresses_once_then_fails():
    prov = gepa_lite.GepaLiteProvider()
    long = "x" * 120
    converse = _converse([json.dumps({"revised_prompt": long}),
                          json.dumps({"revised_prompt": "y" * 90})])
    res = prov.optimize(_req(_evidence(1), max_chars=100), svc._noop, converse=converse)
    assert res.status == "COMPLETED" and res.recommended_prompt == "y" * 90
    assert "100 characters" in converse.calls[1]["system"]
    converse = _converse([json.dumps({"revised_prompt": long}),
                          json.dumps({"revised_prompt": long})])
    res = prov.optimize(_req(_evidence(1), max_chars=100), svc._noop, converse=converse)
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


# ─── regeneration + attribution downstream ──────────────────────────────────
def test_regenerating_with_agentcore_clears_provider_keys(monkeypatch):
    _inline(monkeypatch)
    monkeypatch.setattr(svc, "_agent_meta", lambda exp, ws: {"system_prompt": "cur"})
    monkeypatch.setattr(svc, "stage_recommend", lambda *a, **k: {
        "system_prompt_status": "COMPLETED", "recommended_prompt": "aws", "explanation": ""})
    exp = _mk_exp(artifacts={"agent_meta": {"system_prompt": "cur"}, "recommend": {
        "provider": "gepa_lite", "provider_model_id": "m", "provider_meta": {"x": 1},
        "system_prompt_status": "COMPLETED", "recommended_prompt": "gepa",
        "accepted_prompt": "edited", "tool_status": "COMPLETED", "tool_descriptions": {}}})
    svc.act_recommend(exp.id, svc._noop, types=["system_prompt"])
    rec = _reload(exp.id).artifacts["recommend"]
    assert rec["recommended_prompt"] == "aws"
    for key in ("provider", "provider_model_id", "provider_meta"):
        assert key not in rec
    assert rec["accepted_prompt"] == "edited"  # earlier accept survives
    assert rec["tool_status"] == "COMPLETED"  # the other generator's output too


def test_accept_records_whether_the_seed_was_edited(client):
    exp = _mk_exp(artifacts={"recommend": {"recommended_prompt": "rec", "provider": "gepa_lite"}})
    res = client.post(f"/api/experiments/{exp.id}/action",
                      json={"action": "accept", "accepted_prompt": "rec"})
    assert res.json()["experiment"]["artifacts"]["recommend"]["accepted_edited"] is False
    res = client.post(f"/api/experiments/{exp.id}/action",
                      json={"action": "accept", "accepted_prompt": "rec, edited"})
    rec = res.json()["experiment"]["artifacts"]["recommend"]
    assert rec["accepted_edited"] is True
    assert rec["provider"] == "gepa_lite"  # attribution survives an edit
    # an unchanged prompt with a hand-edited tool description is an edit too
    res = client.post(f"/api/experiments/{exp.id}/action",
                      json={"action": "accept", "accepted_prompt": "rec",
                            "accepted_tool_descriptions": {"t": "hand-written"}})
    assert res.json()["experiment"]["artifacts"]["recommend"]["accepted_edited"] is True


def test_recommendation_attribution_text():
    assert svc.recommendation_attribution(None) is None
    assert svc.recommendation_attribution({"provider": "agentcore"}) is None
    assert svc.recommendation_attribution({"provider": "gepa_lite"}) == "gepa_lite"
    assert svc.recommendation_attribution(
        {"provider": "gepa_lite", "provider_model_id": "m1"}) == "gepa_lite · m1"


def test_treatment_bundle_commit_message_carries_attribution(monkeypatch):
    messages: list[str] = []

    def fake_create(control, **kw):
        messages.append(kw["commit_message"])
        return {"bundleId": "b", "bundleArn": "arn:b", "versionId": "1"}

    monkeypatch.setattr(svc, "create_bundle_idempotent", fake_create)
    monkeypatch.setattr(svc, "control_client", lambda ws: object())
    agent = {"arn": "arn:a", "system_prompt": "cur", "tools": {"t": "d"}}
    svc.stage_bundles("e1", agent, "treat", WS, None, attribution="gepa_lite · m1")
    assert messages[1] == "treatment — accepted recommendation (gepa_lite · m1)"
    messages.clear()
    svc.stage_bundles("e1", agent, "treat", WS, None)
    assert messages[1] == "treatment — accepted recommendation"


def test_action_bundles_passes_attribution_only_for_third_party(client, monkeypatch):
    captured: list[dict] = []

    def fake_stage_bundles(exp_id, agent, treatment_prompt, _ws, tds=None, **kw):
        captured.append(kw)
        return {"control": {"arn": "c"}, "treatment": {"arn": "t"}}

    monkeypatch.setattr(svc, "stage_bundles", fake_stage_bundles)
    exp = _mk_exp(artifacts={"agent_meta": {"arn": "arn:a", "system_prompt": "cur"},
                             "recommend": {"accepted_prompt": "x", "provider": "gepa_lite",
                                           "provider_model_id": "m"}})
    assert client.post(f"/api/experiments/{exp.id}/action",
                       json={"action": "bundles"}).status_code == 200
    assert captured[-1] == {"attribution": "gepa_lite · m"}
    exp2 = _mk_exp(artifacts={"agent_meta": {"arn": "arn:a", "system_prompt": "cur"},
                              "recommend": {"accepted_prompt": "x"}})
    assert client.post(f"/api/experiments/{exp2.id}/action",
                       json={"action": "bundles"}).status_code == 200
    assert captured[-1] == {}
