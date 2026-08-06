"""Stepwise experiment actions — guard matrix, accept/bundles wiring,
traffic dataset resolution, runner lifecycle, old-row compatibility."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import app.optimization.service as svc
from app.core.db import SessionLocal
from app.evaluation.models import EvalDataset
from app.models.ledger import Agent
from app.optimization.models import Experiment


def _mk_exp(**kw):
    db = SessionLocal()
    exp = Experiment(
        name="EXP-t", agent_id=kw.pop("agent_id", "a1"), agent_name="agent", **kw
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


def _inline(monkeypatch):
    """Run action threads synchronously so tests see final state."""
    monkeypatch.setattr(svc, "_spawn", lambda target: target())


# ─── guards ──────────────────────────────────────────────────────────────────
def test_every_action_requires_its_prerequisite(client):
    exp = _mk_exp(artifacts={"agent_meta": {}})
    for action in ["accept", "bundles", "gateway", "abtest", "traffic",
                   "verdict", "promote"]:
        res = client.post(f"/api/experiments/{exp.id}/action",
                          json={"action": action})
        assert res.status_code == 409, action
        assert res.json()["code"] == "experiment.stage_not_ready", action


def test_action_blocked_while_another_runs(client):
    exp = _mk_exp(running_action="recommend")
    res = client.post(f"/api/experiments/{exp.id}/action",
                      json={"action": "recommend"})
    assert res.status_code == 409
    assert res.json()["code"] == "experiment.action_in_flight"


def test_unknown_action_rejected(client):
    exp = _mk_exp()
    res = client.post(f"/api/experiments/{exp.id}/action",
                      json={"action": "explode"})
    assert res.status_code == 422


# ─── accept ──────────────────────────────────────────────────────────────────
def test_accept_persists_edited_config_and_unlocks_bundles(client):
    exp = _mk_exp(artifacts={"recommend": {"recommended_prompt": "rec"}})
    res = client.post(
        f"/api/experiments/{exp.id}/action",
        json={"action": "accept", "accepted_prompt": "edited",
              "accepted_tool_descriptions": {"calculator": "d2"}},
    )
    assert res.status_code == 200
    body = res.json()["experiment"]
    assert body["stage"] == "bundles"
    assert body["artifacts"]["recommend"]["accepted_prompt"] == "edited"
    assert body["artifacts"]["recommend"]["accepted_tool_descriptions"] == {
        "calculator": "d2"
    }
    # the original recommendation is retained alongside the edit
    assert body["artifacts"]["recommend"]["recommended_prompt"] == "rec"


def test_accept_defaults_to_recommended_prompt(client):
    exp = _mk_exp(artifacts={"recommend": {"recommended_prompt": "rec"}})
    res = client.post(f"/api/experiments/{exp.id}/action",
                      json={"action": "accept"})
    assert res.status_code == 200
    assert (res.json()["experiment"]["artifacts"]["recommend"]["accepted_prompt"]
            == "rec")


def test_accept_with_nothing_to_accept_is_400(client):
    exp = _mk_exp(artifacts={"recommend": {}})
    res = client.post(f"/api/experiments/{exp.id}/action",
                      json={"action": "accept"})
    assert res.status_code == 400
    assert res.json()["code"] == "experiment.accept_invalid"


def test_accept_falls_back_to_current_prompt_for_tool_only_rec(client):
    """A tool-description-only recommendation is acceptable — the treatment
    keeps the production prompt and changes only the descriptions."""
    exp = _mk_exp(artifacts={
        "agent_meta": {"system_prompt": "cur"},
        "recommend": {"tool_status": "COMPLETED",
                      "tool_descriptions": {"shell": "better"}},
    })
    res = client.post(
        f"/api/experiments/{exp.id}/action",
        json={"action": "accept",
              "accepted_tool_descriptions": {"shell": "better"}},
    )
    assert res.status_code == 200
    rec = res.json()["experiment"]["artifacts"]["recommend"]
    assert rec["accepted_prompt"] == "cur"
    assert rec["accepted_tool_descriptions"] == {"shell": "better"}


def test_accept_rejected_after_failed_system_prompt_rec(client):
    """A failed system-prompt job produced nothing — accepting the control
    prompt as the treatment would present the failure as an optimization."""
    exp = _mk_exp(artifacts={
        "agent_meta": {"system_prompt": "cur"},
        "recommend": {"system_prompt_status": "FAILED",
                      "system_prompt_error": "ValidationException: filtered"},
    })
    for payload in ({"action": "accept"},
                    {"action": "accept", "accepted_prompt": " cur "}):
        res = client.post(f"/api/experiments/{exp.id}/action", json=payload)
        assert res.status_code == 409, payload
        assert res.json()["code"] == "experiment.accept_rec_failed", payload
    assert "accepted_prompt" not in _reload(exp.id).artifacts["recommend"]


def test_accept_ignores_stale_fallback_prompt_of_a_failed_row(client):
    """Rows written before the guard pair a FAILED status with the old generic
    fallback text — that text must never become the treatment."""
    exp = _mk_exp(artifacts={
        "agent_meta": {"system_prompt": "cur"},
        "recommend": {"system_prompt_status": "FAILED",
                      "recommended_prompt": "cur\nUse the available tools…"},
    })
    res = client.post(f"/api/experiments/{exp.id}/action",
                      json={"action": "accept"})
    assert res.status_code == 409
    assert res.json()["code"] == "experiment.accept_rec_failed"


def test_accept_allows_operator_authored_prompt_after_failure(client):
    """The escape hatch: an operator may author the treatment prompt by hand."""
    exp = _mk_exp(artifacts={
        "agent_meta": {"system_prompt": "cur"},
        "recommend": {"system_prompt_status": "FAILED",
                      "system_prompt_error": "ValidationException: filtered"},
    })
    res = client.post(f"/api/experiments/{exp.id}/action",
                      json={"action": "accept", "accepted_prompt": "hand-written"})
    assert res.status_code == 200
    assert (res.json()["experiment"]["artifacts"]["recommend"]["accepted_prompt"]
            == "hand-written")


def test_recommend_rerun_preserves_prior_accept(monkeypatch):
    """Re-running recommend (retry path) must not drop an earlier accept."""
    exp = _mk_exp(artifacts={
        "agent_meta": {"arn": "arn:a", "resource_id": "r", "runtime_name": "n",
                       "system_prompt": "cur"},
        "recommend": {"recommended_prompt": "old-rec", "accepted_prompt": "kept",
                      "accepted_tool_descriptions": {"calculator": "d2"}},
    })
    monkeypatch.setattr(
        svc, "stage_recommend",
        lambda exp_id, agent, progress=svc._noop, **kw: {
            "recommended_prompt": "new-rec"},
    )
    svc.act_recommend(exp.id, svc._noop)
    rec = _reload(exp.id).artifacts["recommend"]
    assert rec["recommended_prompt"] == "new-rec"      # refreshed
    assert rec["accepted_prompt"] == "kept"            # earlier accept retained
    assert rec["accepted_tool_descriptions"] == {"calculator": "d2"}


def test_recommend_partial_rerun_keeps_other_type(monkeypatch):
    """Generating only tool descriptions must not wipe the prompt output —
    and must clear its own stale error keys."""
    exp = _mk_exp(artifacts={
        "agent_meta": {"system_prompt": "cur", "tools": {"shell": "d"}},
        "recommend": {"recommended_prompt": "sp-old", "explanation": "e",
                      "system_prompt_status": "COMPLETED",
                      "tool_status": "error", "tool_error": "Boom",
                      "tool_descriptions": {}},
    })
    monkeypatch.setattr(
        svc, "stage_recommend",
        lambda exp_id, agent, progress=svc._noop, **kw: {
            "tool_status": "COMPLETED",
            "tool_descriptions": {"shell": "better"},
            "analyzed_tools": {"shell": "d"}},
    )
    svc.act_recommend(exp.id, svc._noop, types=["tool_descriptions"])
    rec = _reload(exp.id).artifacts["recommend"]
    assert rec["recommended_prompt"] == "sp-old"       # untouched
    assert rec["tool_descriptions"] == {"shell": "better"}
    assert "tool_error" not in rec                     # stale error cleared


def test_recommend_action_passes_types_and_tools(client, monkeypatch):
    _inline(monkeypatch)
    captured: dict = {}

    def fake_stage(exp_id, agent, progress=svc._noop, types=svc.REC_TYPES,
                   tools=None):
        captured.update(types=types, tools=tools)
        return {"tool_status": "COMPLETED",
                "tool_descriptions": {"shell": "better"},
                "analyzed_tools": {"shell": "run bash"}}

    monkeypatch.setattr(svc, "stage_recommend", fake_stage)
    exp = _mk_exp(artifacts={"agent_meta": {"system_prompt": "cur",
                                            "tools": {}}})
    res = client.post(
        f"/api/experiments/{exp.id}/action",
        json={"action": "recommend",
              "recommend_types": ["tool_descriptions"],
              "recommend_tools": {"shell": "run bash"}},
    )
    assert res.status_code == 202
    assert captured["types"] == ("tool_descriptions",)
    assert captured["tools"] == {"shell": "run bash"}
    rec = _reload(exp.id).artifacts["recommend"]
    assert rec["tool_descriptions"] == {"shell": "better"}
    assert "recommended_prompt" not in rec


def test_recommend_rejects_unknown_or_empty_types(client):
    exp = _mk_exp(artifacts={"agent_meta": {"system_prompt": "cur"}})
    for bad in (["prompt"], []):
        res = client.post(f"/api/experiments/{exp.id}/action",
                          json={"action": "recommend", "recommend_types": bad})
        assert res.status_code == 422, bad


def _rec_agent(tools):
    return {"resource_id": "rid", "runtime_name": "rt", "system_prompt": "cur",
            "tools": tools}


def test_stage_recommend_runs_only_selected_types(monkeypatch):
    monkeypatch.setattr(svc, "data_client", lambda: MagicMock())
    calls: list[str] = []
    monkeypatch.setattr(
        svc.ac, "start_system_prompt_recommendation",
        lambda *a, **k: calls.append("sp") or {"recommendationId": "r1"})
    monkeypatch.setattr(
        svc.ac, "start_tool_description_recommendation",
        lambda *a, **k: calls.append("td") or {"recommendationId": "r2"})
    monkeypatch.setattr(svc.ac, "poll_recommendation", lambda *a, **k: {
        "status": "COMPLETED",
        "recommendationResult": {
            "systemPromptRecommendationResult": {
                "recommendedSystemPrompt": "better", "explanation": "x"},
            "toolDescriptionRecommendationResult": {"tools": [
                {"toolName": "shell", "recommendedToolDescription": "improved"},
                {"toolName": "noop", "recommendedToolDescription": ""}]},
        }})

    out = svc.stage_recommend("e1", _rec_agent({"shell": "old"}),
                              types=("system_prompt",))
    assert calls == ["sp"]
    assert out["recommended_prompt"] == "better"
    assert "tool_status" not in out and "tool_descriptions" not in out

    calls.clear()
    out = svc.stage_recommend("e1", _rec_agent({"shell": "old"}),
                              types=("tool_descriptions",))
    assert calls == ["td"]
    assert out["tool_descriptions"] == {"shell": "improved"}  # empty rec dropped
    assert out["analyzed_tools"] == {"shell": "old"}
    assert out["tool_status"] == "COMPLETED"
    assert "recommended_prompt" not in out


_NOT_TRACED_MSG = ("The following requested tools were not found in the "
                   "sampled agent traces: ['shell', 'file_operations']. "
                   "Ensure the agent traces contain invocations.")


def test_stage_recommend_retries_without_untraced_tools(monkeypatch):
    """The TD job rejects the whole list when any tool is absent from the
    traces — one retry with only the traced tools must follow."""
    monkeypatch.setattr(svc, "data_client", lambda: MagicMock())
    started: list[list[str]] = []

    def fake_start(client, *, name, tools, **kw):
        started.append([t["toolName"] for t in tools])
        return {"recommendationId": f"r{len(started)}"}

    def fake_poll(client, *, recommendation_id, **kw):
        if recommendation_id == "r1":
            return {"status": "FAILED", "recommendationResult": {
                "toolDescriptionRecommendationResult": {
                    "errorCode": "ValidationException",
                    "errorMessage": _NOT_TRACED_MSG}}}
        return {"status": "COMPLETED", "recommendationResult": {
            "toolDescriptionRecommendationResult": {"tools": [
                {"toolName": "kb___Retrieve",
                 "recommendedToolDescription": "improved"}]}}}

    monkeypatch.setattr(svc.ac, "start_tool_description_recommendation",
                        fake_start)
    monkeypatch.setattr(svc.ac, "poll_recommendation", fake_poll)
    agent = _rec_agent({"kb___Retrieve": "old", "shell": "s",
                        "file_operations": "f"})
    out = svc.stage_recommend("e1", agent, types=("tool_descriptions",))
    assert started == [["kb___Retrieve", "shell", "file_operations"],
                       ["kb___Retrieve"]]
    assert out["tool_status"] == "COMPLETED"
    assert out["tool_descriptions"] == {"kb___Retrieve": "improved"}
    assert out["analyzed_tools"] == {"kb___Retrieve": "old"}  # what succeeded


def test_stage_recommend_surfaces_job_error(monkeypatch):
    """Every listed tool untraced → nothing to retry with; the job's own
    error message must land in tool_error (the old code dropped it)."""
    monkeypatch.setattr(svc, "data_client", lambda: MagicMock())
    monkeypatch.setattr(
        svc.ac, "start_tool_description_recommendation",
        lambda *a, **k: {"recommendationId": "r1"})
    monkeypatch.setattr(svc.ac, "poll_recommendation", lambda *a, **k: {
        "status": "FAILED", "recommendationResult": {
            "toolDescriptionRecommendationResult": {
                "errorCode": "ValidationException",
                "errorMessage": _NOT_TRACED_MSG}}})
    agent = _rec_agent({"shell": "s", "file_operations": "f"})
    out = svc.stage_recommend("e1", agent, types=("tool_descriptions",))
    assert out["tool_status"] == "error"
    assert out["tool_error"].startswith("ValidationException")
    assert "not found in the sampled agent traces" in out["tool_error"]
    assert out["tool_descriptions"] == {}


def _sp_only(monkeypatch, poll_result):
    monkeypatch.setattr(svc, "data_client", lambda: MagicMock())
    monkeypatch.setattr(svc.ac, "start_system_prompt_recommendation",
                        lambda *a, **k: {"recommendationId": "r1"})
    monkeypatch.setattr(svc.ac, "poll_recommendation",
                        lambda *a, **k: poll_result)
    return svc.stage_recommend("e1", _rec_agent({}), types=("system_prompt",))


def test_stage_recommend_failed_prompt_job_reports_error_and_no_prompt(monkeypatch):
    """A FAILED job (e.g. safety filters rejecting the traces) must surface the
    failure — never a made-up prompt that reads as an AI recommendation."""
    out = _sp_only(monkeypatch, {
        "status": "FAILED", "recommendationResult": {
            "systemPromptRecommendationResult": {
                "errorCode": "ValidationException",
                "errorMessage": "flagged as a potential prompt attack"}}})
    assert out["system_prompt_status"] == "FAILED"
    assert out["system_prompt_error"] == (
        "ValidationException: flagged as a potential prompt attack")
    assert "recommended_prompt" not in out
    assert svc.system_prompt_rec_failed(out) is True


def test_stage_recommend_empty_prompt_result_counts_as_failure(monkeypatch):
    """COMPLETED with no prompt is nothing to accept either; with no AWS error
    text the job status becomes the operator-facing reason."""
    out = _sp_only(monkeypatch, {
        "status": "COMPLETED", "recommendationResult": {
            "systemPromptRecommendationResult": {"recommendedSystemPrompt": ""}}})
    assert out["system_prompt_status"] == "COMPLETED"
    assert out["system_prompt_error"] == "recommendation job ended COMPLETED"
    assert "recommended_prompt" not in out
    assert svc.system_prompt_rec_failed(out) is True


def test_stage_recommend_completed_prompt_has_no_error(monkeypatch):
    out = _sp_only(monkeypatch, {
        "status": "COMPLETED", "recommendationResult": {
            "systemPromptRecommendationResult": {
                "recommendedSystemPrompt": "better", "explanation": "why"}}})
    assert out == {"system_prompt_status": "COMPLETED",
                   "recommended_prompt": "better", "explanation": "why"}
    assert svc.system_prompt_rec_failed(out) is False


def test_system_prompt_rec_failed_ignores_tool_only_and_legacy_rows():
    # tool-description-only run — the prompt stage never ran
    assert svc.system_prompt_rec_failed(
        {"tool_status": "COMPLETED", "tool_descriptions": {"shell": "d"}}) is False
    # pre-status row that only stored the prompt
    assert svc.system_prompt_rec_failed({"recommended_prompt": "rec"}) is False


def test_recommend_rerun_clears_stale_prompt_failure(monkeypatch):
    exp = _mk_exp(artifacts={
        "agent_meta": {"system_prompt": "cur", "tools": {}},
        "recommend": {"system_prompt_status": "FAILED",
                      "system_prompt_error": "ValidationException: filtered"},
    })
    monkeypatch.setattr(
        svc, "stage_recommend",
        lambda exp_id, agent, progress=svc._noop, **kw: {
            "system_prompt_status": "COMPLETED", "recommended_prompt": "better",
            "explanation": ""},
    )
    svc.act_recommend(exp.id, svc._noop, types=["system_prompt"])
    rec = _reload(exp.id).artifacts["recommend"]
    assert rec["recommended_prompt"] == "better"
    assert "system_prompt_error" not in rec
    assert svc.system_prompt_rec_failed(rec) is False


def test_stage_recommend_without_tools_short_circuits(monkeypatch):
    monkeypatch.setattr(svc, "data_client", lambda: MagicMock())

    def boom(*a, **k):
        raise AssertionError("no tools → the TD API must not be called")

    monkeypatch.setattr(svc.ac, "start_tool_description_recommendation", boom)
    out = svc.stage_recommend("e1", _rec_agent({}), types=("tool_descriptions",))
    assert out == {"analyzed_tools": {}, "tool_status": "no-tools",
                   "tool_descriptions": {}}


def test_discover_agent_tools_from_spec_and_code():
    spec = {
        "tools": [{"name": "search", "description": "Search the registry"}],
        "code": ('@tool\ndef calculator(expression: str) -> str:\n'
                 '    """Evaluate a basic arithmetic expression.\n\n'
                 '    Args:\n        expression: the math\n    """\n'
                 '    return ""\n'),
        "code_bundle": {
            "main.py": ('@tool\ndef shell(command: str, timeout: int = 300):\n'
                        '    """Execute a bash command and return the results.\n'
                        '    Args:\n        command: The bash command\n    """\n'),
            "notes.md": "no tools here",
        },
    }
    assert svc.discover_agent_tools(spec) == {
        "search": "Search the registry",
        "calculator": "Evaluate a basic arithmetic expression.",
        "shell": "Execute a bash command and return the results.",
    }
    assert svc.discover_agent_tools({}) == {}


def test_discover_agent_tools_promoted_overrides_win():
    assert svc.discover_agent_tools({
        "tools": [{"name": "search", "description": "old"}],
        "tool_description_overrides": {"search": "promoted", "shell": "new"},
    }) == {"search": "promoted", "shell": "new"}


def test_experiment_capability_matrix():
    generated = SimpleNamespace(method="zip_runtime", spec={"protocol": "http"})
    assert svc.experiment_capability(generated) == {
        "eligible": True,
        "system_prompt": True,
        "tool_descriptions": True,
        "reason": None,
        "reason_code": None,
    }
    converted_v2 = SimpleNamespace(
        method="zip_runtime",
        spec={
            "protocol": "http",
            "source_harness": {"agent_id": "h1"},
            "code_bundle": {"main.py": f"{svc.graft_config_bundle.__module__}\n"
                            "# <launchpad-config-bundle:v2>\n"
                            "def resolve_system_prompt(): pass"},
        },
    )
    assert svc.experiment_capability(converted_v2)["tool_descriptions"] is True
    converted_v1 = SimpleNamespace(
        method="zip_runtime",
        spec={
            "source_harness": {"agent_id": "h1"},
            "code_bundle": {"main.py":
                "# ─── Launchpad platform contract: config bundles (A/B experiments)\n"
                "def resolve_system_prompt(): pass"},
        },
    )
    cap = svc.experiment_capability(converted_v1)
    assert cap["eligible"] is True and cap["tool_descriptions"] is False
    for row in [
        SimpleNamespace(method="harness", spec={}),
        SimpleNamespace(method="container", spec={}),
        SimpleNamespace(method="studio", spec={}),
        SimpleNamespace(method="zip_runtime", spec={"protocol": "a2a"}),
        SimpleNamespace(method="zip_runtime", spec={"code": "custom"}),
        SimpleNamespace(method="zip_runtime", spec={"code_bundle": {"main.py": "custom"}}),
    ]:
        assert svc.experiment_capability(row)["eligible"] is False


def test_canary_capability_is_independent_from_bundle_consumption():
    runtime_arn = (
        "arn:aws:bedrock-agentcore:us-west-2:111122223333:"
        "runtime/challenger-abcdefghij"
    )
    custom_runtime = SimpleNamespace(
        method="zip_runtime", status="active", arn=runtime_arn,
        spec={"protocol": "http", "code": "custom runtime"},
    )
    assert svc.experiment_capability(custom_runtime)["eligible"] is False
    assert svc.canary_capability(custom_runtime) == {
        "eligible": True, "reason": None, "reason_code": None,
    }

    studio = SimpleNamespace(method="studio", status="active", arn=runtime_arn, spec={})
    assert svc.canary_capability(studio) == {
        "eligible": True, "reason": None, "reason_code": None,
    }

    # Container candidate minting via CodeBuild is a follow-up — ineligible today.
    container = SimpleNamespace(method="container", status="active", arn=runtime_arn, spec={})
    container_cap = svc.canary_capability(container)
    assert container_cap["eligible"] is False
    assert container_cap["reason_code"] == "container-followup"

    incompatible = [
        SimpleNamespace(method="zip_runtime", status="deploying", arn=runtime_arn, spec={}),
        SimpleNamespace(method="harness", status="active", arn="arn:harness/x", spec={}),
        SimpleNamespace(
            method="zip_runtime", status="active", arn=runtime_arn,
            spec={"protocol": "a2a"},
        ),
        SimpleNamespace(method="zip_runtime", status="active", arn=None, spec={}),
    ]
    for row in incompatible:
        assert svc.canary_capability(row)["eligible"] is False


# ─── bundles ─────────────────────────────────────────────────────────────────
def test_bundles_consume_accepted_config(client, monkeypatch):
    captured: dict = {}

    def fake_stage_bundles(exp_id, agent, treatment_prompt, treatment_tds=None):
        captured.update(prompt=treatment_prompt, tds=treatment_tds, agent=agent)
        return {"control": {"bundle_id": "b1", "arn": "arn:c", "version": "1"},
                "treatment": {"bundle_id": "b2", "arn": "arn:t", "version": "1"}}

    monkeypatch.setattr(svc, "stage_bundles", fake_stage_bundles)
    exp = _mk_exp(artifacts={
        "agent_meta": {"arn": "arn:a", "system_prompt": "cur"},
        "recommend": {"recommended_prompt": "rec", "accepted_prompt": "edited",
                      "accepted_tool_descriptions": {"calculator": "d2"}},
    })
    res = client.post(f"/api/experiments/{exp.id}/action",
                      json={"action": "bundles"})
    assert res.status_code == 200
    assert captured["prompt"] == "edited"
    assert captured["tds"] == {"calculator": "d2"}
    body = res.json()["experiment"]
    assert body["artifacts"]["bundles"]["control"]["arn"] == "arn:c"
    assert body["stage"] == "bundles"


def test_bundles_without_accept_is_blocked(client):
    exp = _mk_exp(artifacts={"recommend": {"recommended_prompt": "rec"}})
    res = client.post(f"/api/experiments/{exp.id}/action",
                      json={"action": "bundles"})
    assert res.status_code == 409
    assert res.json()["code"] == "experiment.stage_not_ready"


# ─── traffic ─────────────────────────────────────────────────────────────────
def test_resolve_traffic_prompts_kinds():
    legacy = SimpleNamespace(kind="legacy",
                             items=[{"prompt": "p1"}, {"prompt": "  "}])
    assert svc.resolve_traffic_prompts(legacy) == ["p1"]

    predefined = SimpleNamespace(
        kind="predefined",
        items=[{"turns": [{"input": "t1"}, {"input": "later"}]}, {"turns": []}],
    )
    assert svc.resolve_traffic_prompts(predefined) == ["t1"]

    # imported-JSON scenarios carry dict turn inputs — must unwrap, not str()
    dict_input = SimpleNamespace(
        kind="predefined",
        items=[{"turns": [{"input": {"content": "hi there"}}]},
               {"turns": [{"input": {"prompt": "second"}}]}],
    )
    assert svc.resolve_traffic_prompts(dict_input) == ["hi there", "second"]

    with pytest.raises(ValueError):
        svc.resolve_traffic_prompts(SimpleNamespace(kind="simulated", items=[]))
    with pytest.raises(ValueError):
        svc.resolve_traffic_prompts(SimpleNamespace(kind="legacy", items=[]))


def _traffic_ready_artifacts():
    return {"abtest": {"ab_test_id": "ab1"},
            "gateway": {"gateway_url": "https://gw", "target_v1": "t1"}}


def test_traffic_action_uses_dataset_prompts(client, monkeypatch):
    _inline(monkeypatch)
    sent: dict = {}

    def fake_send(gateway_url, target, prompts, poster=None, signer=None,
                  progress=svc._noop):
        sent.update(url=gateway_url, target=target, prompts=list(prompts))
        return {"session_ids": ["s1"], "sent": len(prompts), "failed": 0}

    monkeypatch.setattr(svc, "send_gateway_traffic", fake_send)
    db = SessionLocal()
    ds = EvalDataset(name="traffic-ds", kind="legacy", items=[{"prompt": "p1"}])
    db.add(ds)
    db.commit()
    ds_id = ds.id
    db.close()

    exp = _mk_exp(artifacts=_traffic_ready_artifacts())
    res = client.post(f"/api/experiments/{exp.id}/action",
                      json={"action": "traffic", "dataset_id": ds_id})
    assert res.status_code == 202
    assert sent["prompts"] == ["p1"]
    row = _reload(exp.id)
    assert row.running_action is None
    assert row.stage == "traffic"
    assert row.artifacts["traffic"]["dataset_name"] == "traffic-ds"
    assert row.artifacts["traffic"]["dataset_id"] == ds_id


def test_traffic_action_requires_dataset(client):
    exp = _mk_exp(artifacts=_traffic_ready_artifacts())
    res = client.post(f"/api/experiments/{exp.id}/action",
                      json={"action": "traffic"})
    assert res.status_code == 422
    assert res.json()["code"] == "experiment.dataset_required"
    assert "traffic" not in _reload(exp.id).artifacts


def test_traffic_rejects_simulated_and_missing_datasets(client):
    db = SessionLocal()
    ds = EvalDataset(name="sim-ds", kind="simulated",
                     items=[{"actor_profile": {}}])
    db.add(ds)
    db.commit()
    ds_id = ds.id
    db.close()

    exp = _mk_exp(artifacts=_traffic_ready_artifacts())
    res = client.post(f"/api/experiments/{exp.id}/action",
                      json={"action": "traffic", "dataset_id": ds_id})
    assert res.status_code == 422
    assert res.json()["code"] == "experiment.dataset_unsupported"

    res = client.post(f"/api/experiments/{exp.id}/action",
                      json={"action": "traffic", "dataset_id": "nope"})
    assert res.status_code == 404


# ─── runner lifecycle ────────────────────────────────────────────────────────
def test_run_action_failure_keeps_stage_and_stores_error(monkeypatch):
    _inline(monkeypatch)
    exp = _mk_exp(stage="bundles")

    def boom(progress):
        raise RuntimeError("kaput")

    svc.run_action(exp.id, "gateway", boom)
    row = _reload(exp.id)
    assert row.running_action is None
    assert row.progress is None
    assert row.stage == "bundles"  # retry stays possible
    assert row.error.startswith("gateway: ")  # UI pins failures to the button
    assert "RuntimeError: kaput" in row.error


def test_run_action_success_clears_error_and_persists(monkeypatch):
    _inline(monkeypatch)
    exp = _mk_exp(stage="bundles", error="stale failure")
    seen: list[str] = []

    def ok(progress):
        progress("halfway")
        seen.append(_reload(exp.id).progress)
        svc._update(exp.id, stage="gateway",
                    artifact={"gateway": {"gateway_id": "g1"}})

    svc.run_action(exp.id, "gateway", ok)
    row = _reload(exp.id)
    assert seen == ["halfway"]  # progress visible to pollers mid-action
    assert row.running_action is None and row.progress is None
    assert row.error is None
    assert row.stage == "gateway"
    assert row.artifacts["gateway"]["gateway_id"] == "g1"


def test_create_bundle_idempotent_adopts_on_conflict(monkeypatch):
    """A retried bundles action must adopt the bundle a prior run created."""
    class Conflict(Exception):
        pass

    Conflict.__name__ = "ConflictException"

    def raise_conflict(control, **kwargs):
        raise Conflict("name taken")

    monkeypatch.setattr(svc.ac, "create_configuration_bundle", raise_conflict)
    control = MagicMock()
    control.list_configuration_bundles.return_value = {
        "bundles": [{"bundleName": "exp_x_control", "bundleId": "b1",
                     "bundleArn": "arn:b1"}],
    }
    control.get_configuration_bundle.return_value = {"versionId": "3"}
    out = svc.create_bundle_idempotent(
        control, agent_arn="arn:a", bundle_name="exp_x_control",
        system_prompt="p", tool_descriptions={}, commit_message="m",
    )
    assert out == {"bundleId": "b1", "bundleArn": "arn:b1", "versionId": "3"}
    control.get_configuration_bundle.assert_called_once_with(bundleId="b1")

    # unknown name → the original conflict propagates (nothing to adopt)
    control.list_configuration_bundles.return_value = {"bundles": []}
    with pytest.raises(Conflict):
        svc.create_bundle_idempotent(
            control, agent_arn="arn:a", bundle_name="exp_y_control",
            system_prompt="p", tool_descriptions={}, commit_message="m",
        )


def test_startup_sweep_clears_stale_running_actions():
    stuck = _mk_exp(running_action="recommend", progress="polling…")
    idle = _mk_exp(agent_id="a2")
    cleared = svc.clear_stale_running_actions()
    assert cleared == [stuck.id]
    row = _reload(stuck.id)
    assert row.running_action is None and row.progress is None
    assert row.error.startswith("recommend: interrupted by a backend restart")
    assert _reload(idle.id).error is None


# ─── backward compatibility (old auto-pipeline rows) ────────────────────────
def _old_pipeline_artifacts():
    return {
        "recommend": {"recommended_prompt": "rec", "explanation": "",
                      "tool_descriptions": {}},
        "bundles": {"control": {"bundle_id": "b1", "arn": "arn:c", "version": "1"},
                    "treatment": {"bundle_id": "b2", "arn": "arn:t",
                                  "version": "1"}},
        "gateway": {"gateway_id": "g1", "gateway_arn": "arn:g",
                    "gateway_url": "https://gw", "target_v1": "t1",
                    "target_id_v1": "tid1", "online_eval_arn": "arn:oe",
                    "online_eval_id": "oe1"},
        "abtest": {"ab_test_id": "ab1", "variants": []},
        "traffic": {"session_ids": ["s1"], "sent": 12, "failed": 0},
        "verdict": {"metrics": [], "verdict": "insufficient-n", "n": 4},
    }


def test_old_pipeline_row_serializes_with_new_fields(client):
    exp = _mk_exp(status="ready", stage="verdict",
                  artifacts=_old_pipeline_artifacts())
    body = client.get(f"/api/experiments/{exp.id}").json()
    assert body["running_action"] is None
    assert body["progress"] is None
    assert body["artifacts"]["verdict"]["verdict"] == "insufficient-n"


def test_legacy_promotion_projects_ready_without_mutating_row(client):
    artifacts = {
        **_old_pipeline_artifacts(),
        "promote": {
            "before_weights": {"C": 50, "T1": 50},
            "after_weights": {"C": 1, "T1": 99},
        },
    }
    exp = _mk_exp(status="promoted", stage="promote", artifacts=artifacts)
    body = client.get(f"/api/experiments/{exp.id}").json()
    assert body["status"] == "ready"
    assert body["artifacts"]["promote"]["after_weights"]["T1"] == 99
    assert _reload(exp.id).status == "promoted"  # read-time projection only


def test_old_pipeline_row_still_promotes_and_rebundles(client, monkeypatch):
    exp = _mk_exp(status="ready", stage="verdict",
                  artifacts=_old_pipeline_artifacts())
    _inline(monkeypatch)
    monkeypatch.setattr(
        svc, "act_promote",
        lambda exp_id, progress: svc._update(
            exp_id,
            status="promoted",
            artifact={"promote": {
                "ab_test_status": "STOPPED",
                "deployment_id": "d1",
            }},
        ),
    )
    res = client.post(f"/api/experiments/{exp.id}/action",
                      json={"action": "promote"})
    assert res.status_code == 202
    # bundles retry stays open even though the row predates accepted_*
    assert svc.stage_not_ready_reason(_reload(exp.id), "bundles") is None


class _ABData:
    def __init__(self, status="RUNNING", fail_stop=False):
        self.status = status
        self.fail_stop = fail_stop
        self.updates: list[str] = []

    def get_ab_test(self, **kwargs):
        return {"abTestId": kwargs["abTestId"], "executionStatus": self.status}

    def update_ab_test(self, **kwargs):
        status = kwargs["executionStatus"]
        self.updates.append(status)
        if self.fail_stop:
            raise RuntimeError("stop rejected")
        self.status = status
        return {"executionStatus": status}


def _promotion_fixture(*, legacy=False):
    db = SessionLocal()
    agent = Agent(
        name="promotion-target",
        method="zip_runtime",
        status="active",
        arn="arn:rt",
        resource_id="rt-1",
        version="1",
        spec={
            "name": "promotion-target",
            "method": "zip_runtime",
            "system_prompt": "old prompt",
        },
    )
    db.add(agent)
    db.commit()
    artifacts = {
        **_old_pipeline_artifacts(),
        "agent_meta": {
            "system_prompt": "old prompt",
            "tools": {"calculator": "old calculator"},
        },
        "recommend": {
            "recommended_prompt": "recommended prompt",
            "accepted_prompt": "accepted prompt",
            "accepted_tool_descriptions": {"calculator": "better calculator"},
        },
    }
    if legacy:
        artifacts["promote"] = {
            "before_weights": {"C": 50, "T1": 50},
            "after_weights": {"C": 1, "T1": 99},
        }
    exp = Experiment(
        name="EXP-promotion",
        agent_id=agent.id,
        agent_name=agent.name,
        status="promoted" if legacy else "ready",
        stage="promote" if legacy else "verdict",
        artifacts=artifacts,
    )
    db.add(exp)
    db.commit()
    ids = (agent.id, exp.id)
    db.close()
    return ids


def _complete_deploy(job_id: str, *, failed=False):
    from app.models.ledger import Deployment, Job

    db = SessionLocal()
    job = db.get(Job, job_id)
    deployment = db.get(Deployment, job.payload["deployment_id"])
    agent = db.get(Agent, job.payload["agent_id"])
    if failed:
        job.status = "failed"
        job.error = "runtime update failed"
        deployment.status = "failed"
        agent.status = "failed"
    else:
        job.status = "succeeded"
        deployment.status = "succeeded"
        agent.status = "active"
        agent.version = "2"
    db.commit()
    db.close()


def test_official_promotion_stops_applies_deploys_then_marks_promoted(monkeypatch):
    agent_id, exp_id = _promotion_fixture()
    data = _ABData()
    monkeypatch.setattr(svc, "data_client", lambda: data)

    def complete(job_id):
        assert _reload(exp_id).status == "ready"
        assert "promote" not in _reload(exp_id).artifacts
        _complete_deploy(job_id)

    monkeypatch.setattr(svc, "execute_deploy_job", complete)

    result = svc.act_promote(exp_id, svc._noop)

    assert data.updates == ["STOPPED"]
    assert result["ab_test_status"] == "STOPPED"
    assert result["agent_version"] == "2"
    row = _reload(exp_id)
    assert row.status == "promoted"
    assert row.artifacts["promote"]["deployment_id"]
    assert "after_weights" not in row.artifacts["promote"]
    db = SessionLocal()
    agent = db.get(Agent, agent_id)
    assert agent.spec["system_prompt"] == "accepted prompt"
    assert agent.spec["tool_description_overrides"] == {
        "calculator": "better calculator"
    }
    job = db.get(svc.Job, result["job_id"])
    assert job.payload["mode"] == "update"
    assert job.payload["skip_register"] is True
    db.close()


def test_promotion_retry_skips_already_stopped_test(monkeypatch):
    _, exp_id = _promotion_fixture()
    data = _ABData(status="STOPPED")
    monkeypatch.setattr(svc, "data_client", lambda: data)
    monkeypatch.setattr(svc, "execute_deploy_job", _complete_deploy)
    svc.act_promote(exp_id, svc._noop)
    assert data.updates == []


def test_promotion_stop_failure_does_not_deploy_or_mark_success(monkeypatch):
    _, exp_id = _promotion_fixture()
    data = _ABData(fail_stop=True)
    monkeypatch.setattr(svc, "data_client", lambda: data)
    monkeypatch.setattr(
        svc, "create_deployment",
        lambda *a, **k: pytest.fail("deployment must not start"),
    )
    with pytest.raises(RuntimeError, match="stop rejected"):
        svc.act_promote(exp_id, svc._noop)
    row = _reload(exp_id)
    assert row.status == "ready"
    assert "promote" not in row.artifacts


def test_promotion_deployment_failure_keeps_retryable_ready_state(monkeypatch):
    _, exp_id = _promotion_fixture()
    monkeypatch.setattr(svc, "data_client", lambda: _ABData())
    monkeypatch.setattr(
        svc, "execute_deploy_job",
        lambda job_id: _complete_deploy(job_id, failed=True),
    )
    with pytest.raises(RuntimeError, match="production deployment failed"):
        svc.act_promote(exp_id, svc._noop)
    row = _reload(exp_id)
    assert row.status == "ready"
    assert "promote" not in row.artifacts
    assert row.artifacts["promotion_attempt"]["ab_test_status"] == "STOPPED"


def test_legacy_completion_preserves_prior_shift(monkeypatch):
    _, exp_id = _promotion_fixture(legacy=True)
    monkeypatch.setattr(svc, "data_client", lambda: _ABData(status="STOPPED"))
    monkeypatch.setattr(svc, "execute_deploy_job", _complete_deploy)
    result = svc.act_promote(exp_id, svc._noop)
    assert result["prior_shift"] == {"C": 1, "T1": 99}
    assert svc.promotion_complete(_reload(exp_id).artifacts)


@pytest.mark.parametrize("action", ["canary", "ramp"])
def test_combined_canary_actions_moved_to_independent_api(client, action):
    exp = _mk_exp(artifacts={"verdict": {"verdict": "treatment-wins"}})
    res = client.post(
        f"/api/experiments/{exp.id}/action",
        json={"action": action},
    )
    assert res.status_code == 410
    assert res.json()["code"] == "experiment.action_moved"
    assert res.json()["detail"]["runtime_canaries_path"] == "/api/runtime-canaries"


def test_configuration_gateway_rejects_active_canary_before_dispatch(
    client, monkeypatch,
):
    exp = _mk_exp(artifacts={"bundles": {"control": {}, "treatment": {}}})
    control = MagicMock()
    control.list_gateways.return_value = {
        "items": [{"name": svc.EXP_GATEWAY_NAME, "gatewayId": "gw-1"}]
    }
    control.get_gateway.return_value = {
        "gatewayId": "gw-1",
        "gatewayArn": "arn:gateway",
    }
    data = MagicMock()
    data.list_ab_tests.return_value = {
        "abTests": [
            {
                "abTestId": "canary-ab",
                "name": "can_12345678_target",
                "gatewayArn": "arn:gateway",
                "executionStatus": "RUNNING",
            }
        ]
    }
    monkeypatch.setattr(svc, "control_client", lambda: control)
    monkeypatch.setattr(svc, "data_client", lambda: data)
    dispatched: list[str] = []
    monkeypatch.setattr(
        svc,
        "run_action",
        lambda exp_id, action, fn: dispatched.append(action),
    )

    res = client.post(
        f"/api/experiments/{exp.id}/action",
        json={"action": "gateway"},
    )

    assert res.status_code == 409
    assert res.json()["code"] == "experiment.gateway_busy"
    assert dispatched == []


def test_configuration_cleanup_keeps_legacy_canary_resources(monkeypatch):
    exp = _mk_exp(
        artifacts={
            "bundles": {
                "control": {"bundle_id": "bundle-c"},
                "treatment": {"bundle_id": "bundle-t"},
            },
            "gateway": {
                "gateway_id": "gw-shared",
                "target_id_v1": "target-v1",
            },
            "abtest": {"ab_test_id": "bundle-ab"},
            "canary": {
                "canary_ab_test_id": "legacy-canary-ab",
                "target_id_v2": "target-v2",
            },
        }
    )
    control = MagicMock()
    control.list_online_evaluation_configs.return_value = {
        "onlineEvaluationConfigs": [
            {
                "onlineEvaluationConfigId": "oe-1",
                "onlineEvaluationConfigName": f"exp_{exp.id[:8]}_oe1",
            }
        ]
    }
    captured: dict = {}
    monkeypatch.setattr(svc, "control_client", lambda: control)
    monkeypatch.setattr(svc, "data_client", MagicMock)
    monkeypatch.setattr(
        svc.ac,
        "cleanup_resources",
        lambda control, data, **kwargs: (
            captured.update(kwargs)
            or [{"category": "cleanup", "status": "deleted", "detail": ""}]
        ),
    )

    svc.act_cleanup(exp.id)

    assert captured["ab_test_ids"] == ["bundle-ab", "legacy-canary-ab"]
    assert captured["target_ids"] == ["target-v1", "target-v2"]
    assert captured["delete_gateway"] is False


# ─── create defers all stage work ────────────────────────────────────────────
def test_create_defers_all_stage_work(client, monkeypatch):
    db = SessionLocal()
    agent = Agent(name="step-agent", method="zip_runtime", status="active",
                  arn="arn:rt", resource_id="rt-9",
                  spec={"system_prompt": "sys"})
    db.add(agent)
    db.commit()
    agent_id = agent.id
    db.close()

    monkeypatch.setattr(svc, "control_client", lambda: MagicMock())
    monkeypatch.setattr(svc, "rt_name", lambda control, rid: "RTName")
    monkeypatch.setattr(
        "app.optimization.routers.readiness.project_readiness",
        lambda *_args, **_kwargs: {"state": "ready"},
    )
    res = client.post("/api/experiments", json={"agent_id": agent_id})
    assert res.status_code == 201
    body = res.json()
    assert body["stage"] == "recommend"
    assert body["running_action"] is None
    assert set(body["artifacts"]) == {"agent_meta"}
    meta = body["artifacts"]["agent_meta"]
    assert meta["runtime_name"] == "RTName"
    assert meta["system_prompt"] == "sys"


@pytest.mark.parametrize(
    ("method", "spec", "code"),
    [
        ("harness", {}, "experiment.method_unsupported"),
        ("container", {}, "experiment.agent_unsupported"),
        ("studio", {}, "experiment.agent_unsupported"),
        ("zip_runtime", {"protocol": "a2a"}, "experiment.protocol_unsupported"),
        ("zip_runtime", {"code": "custom runtime"}, "experiment.agent_unsupported"),
        (
            "zip_runtime",
            {"code_bundle": {"main.py": "custom runtime"}},
            "experiment.agent_unsupported",
        ),
    ],
)
def test_create_rejects_unverified_bundle_consumers(client, method, spec, code):
    db = SessionLocal()
    agent = Agent(
        name=f"unsupported-{method}",
        method=method,
        status="active",
        arn="arn:rt",
        resource_id="rt-9",
        spec={"system_prompt": "sys", **spec},
    )
    db.add(agent)
    db.commit()
    agent_id = agent.id
    db.close()
    res = client.post("/api/experiments", json={"agent_id": agent_id})
    assert res.status_code == 400
    assert res.json()["code"] == code


# ─── online evaluators (gateway stage) ───────────────────────────────────────
def _gateway_ready_exp():
    return _mk_exp(artifacts={
        "agent_meta": {"arn": "arn:a", "resource_id": "r-1", "runtime_name": "rt"},
        "bundles": {"control": {}, "treatment": {}},
    })


def _capture_gateway(monkeypatch):
    """Free the gateway lock and capture what act_gateway is handed."""
    monkeypatch.setattr(svc, "assert_shared_gateway_available",
                        lambda **kw: None)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        svc, "run_action",
        lambda exp_id, action, fn: seen.update(action=action, fn=fn),
    )
    monkeypatch.setattr(
        svc, "act_gateway",
        lambda exp_id, progress, evaluators=None: seen.update(
            evaluators=list(evaluators or [])),
    )
    return seen


def test_gateway_defaults_to_the_builtin_pair(client, monkeypatch):
    exp = _gateway_ready_exp()
    seen = _capture_gateway(monkeypatch)
    res = client.post(f"/api/experiments/{exp.id}/action",
                      json={"action": "gateway"})
    assert res.status_code == 202
    seen["fn"](svc._noop)
    assert seen["evaluators"] == list(svc.ONLINE_EVAL_DEFAULT)


def test_gateway_passes_chosen_evaluators_in_order(client, monkeypatch):
    exp = _gateway_ready_exp()
    seen = _capture_gateway(monkeypatch)
    res = client.post(
        f"/api/experiments/{exp.id}/action",
        json={"action": "gateway", "online_evaluators": [
            "Builtin.InstructionFollowing", "Builtin.Refusal",
            "Builtin.InstructionFollowing", "  ", "fund_fact_grounding-b9y",
        ]},
    )
    assert res.status_code == 202
    seen["fn"](svc._noop)
    # duplicates and blanks collapse, order preserved, custom ids pass through
    assert seen["evaluators"] == [
        "Builtin.InstructionFollowing", "Builtin.Refusal",
        "fund_fact_grounding-b9y",
    ]


@pytest.mark.parametrize(
    "evaluators",
    [
        ["Builtin.TrajectoryInOrderMatch"],          # needs dataset ground truth
        ["Builtin.Helpfulness", "Builtin.Nope"],     # unknown built-in
    ],
)
def test_gateway_rejects_unusable_evaluators_before_dispatch(
    client, monkeypatch, evaluators,
):
    exp = _gateway_ready_exp()
    seen = _capture_gateway(monkeypatch)
    res = client.post(f"/api/experiments/{exp.id}/action",
                      json={"action": "gateway", "online_evaluators": evaluators})
    assert res.status_code == 400
    assert res.json()["code"] == "experiment.evaluator_unsupported"
    assert "action" not in seen  # never dispatched


@pytest.mark.parametrize(
    "evaluators",
    [[], [f"Builtin.Helpfulness{i}" for i in range(svc.ONLINE_EVAL_MAX + 1)]],
)
def test_gateway_evaluator_list_bounds(client, evaluators):
    exp = _gateway_ready_exp()
    res = client.post(f"/api/experiments/{exp.id}/action",
                      json={"action": "gateway", "online_evaluators": evaluators})
    assert res.status_code == 422


def test_stage_gateway_records_evaluators_on_the_artifact(monkeypatch):
    control = MagicMock()
    monkeypatch.setattr(svc, "control_client", lambda: control)
    monkeypatch.setattr(svc, "ensure_experiment_gateway",
                        lambda progress, control: {"gateway_id": "gw-1"})
    monkeypatch.setattr(svc, "create_runtime_target_idempotent",
                        lambda *a, **kw: "tgt-1")
    control.create_online_evaluation_config.return_value = {
        "onlineEvaluationConfigArn": "arn:oe", "onlineEvaluationConfigId": "oe-1",
    }
    result = svc.stage_gateway(
        "exp1234567890", {"arn": "arn:a", "resource_id": "r-1", "runtime_name": "rt"},
        evaluators=["Builtin.Refusal", "Builtin.InstructionFollowing"],
    )
    assert result["online_evaluators"] == [
        "Builtin.Refusal", "Builtin.InstructionFollowing"]
    sent = control.create_online_evaluation_config.call_args.kwargs["evaluators"]
    assert sent == [{"evaluatorId": "Builtin.Refusal"},
                    {"evaluatorId": "Builtin.InstructionFollowing"}]


def test_discover_agent_tools_reads_platform_toolkits():
    """A toolkit agent's spec.code is None by design, so the docstring regex can
    never see its tools — they come from the toolkit registry instead."""
    tools = svc.discover_agent_tools({"toolkits": ["hr_assistant"]})
    assert sorted(tools) == [
        "get_benefits_summary",
        "get_pay_stub",
        "get_pto_balance",
        "lookup_hr_policy",
        "submit_pto_request",
    ]
    assert tools["get_pto_balance"].startswith("Return the current PTO balance")
    # the template's always-emitted tools are NOT expected for a toolkit agent
    assert "calculator" not in tools and "current_utc_time" not in tools
    # promoted overrides still win over the toolkit default
    promoted = svc.discover_agent_tools({
        "toolkits": ["hr_assistant"],
        "tool_description_overrides": {"get_pay_stub": "promoted"},
    })
    assert promoted["get_pay_stub"] == "promoted"


def test_toolkit_agent_eligibility_survives_promotion():
    """Promotion only rewrites code_bundle for converted harnesses. A toolkit
    agent has no source_harness, so nothing is written into spec.code_bundle and
    experiment_capability cannot flip to custom-source-unverified."""
    spec = {"protocol": "http", "toolkits": ["hr_assistant"]}
    row = SimpleNamespace(method="zip_runtime", spec=spec)
    assert svc.experiment_capability(row) == {
        "eligible": True,
        "system_prompt": True,
        "tool_descriptions": True,
        "reason": None,
        "reason_code": None,
    }
    # Mirror act_promote's spec rewrite (service.py: update name/method/prompt/
    # overrides → AgentSpec(**spec_data) → the `if spec.source_harness` bundle
    # graft → agent.spec = spec.model_dump()) and assert the graft is skipped.
    from app.schemas.agent import AgentSpec

    promoted = AgentSpec(**{
        **spec,
        "name": "hr-toolkit-agent",
        "method": "zip_runtime",
        "system_prompt": "treatment prompt",
        "tool_description_overrides": {"get_pay_stub": "treatment description"},
    })
    assert promoted.source_harness is None  # → the code_bundle branch is not entered
    stored = promoted.model_dump()
    assert stored["code"] is None and stored["code_bundle"] is None
    promoted_row = SimpleNamespace(method="zip_runtime", spec=stored)
    assert svc.experiment_capability(promoted_row)["eligible"] is True
    assert svc.discover_agent_tools(stored)["get_pay_stub"] == "treatment description"
    assert stored["toolkits"] == ["hr_assistant"]  # a second experiment can run


def test_gateway_tools_keep_a_zip_runtime_experiment_eligible():
    """Requirement B: gateway tools must not push an agent into a non-eligible
    branch. They are spec.tools entries, which experiment_capability never reads."""
    row = SimpleNamespace(
        method="zip_runtime",
        spec={"protocol": "http", "tools": [{"type": "gateway", "name": "launchpad-gw"}]},
    )
    assert svc.experiment_capability(row) == {
        "eligible": True,
        "system_prompt": True,
        "tool_descriptions": True,
        "reason": None,
        "reason_code": None,
    }
    # A gateway ToolRef names a SERVER, not a tool: its tools arrive namespaced
    # (hr-database___get_employee) and only at runtime, so the bare name can never
    # be observed in telemetry. Leaving it in expected_tools pinned readiness at
    # "sparse" forever — measured live.
    assert "launchpad-gw" not in svc.discover_agent_tools(row.spec)
    assert svc.discover_agent_tools(row.spec) == {}


def test_discover_agent_tools_skips_gateway_servers_but_keeps_builtins():
    spec = {
        "tools": [
            {"type": "gateway", "name": "hr-database"},
            {"type": "mcp", "name": "deepwiki"},
            {"type": "builtin", "name": "code-interpreter"},
            {"name": "legacy-attachment", "description": "no type field"},
        ],
        "toolkits": ["hr_assistant"],
    }
    tools = svc.discover_agent_tools(spec)
    # server names would never be observed -> would pin readiness at "sparse"
    assert "hr-database" not in tools and "deepwiki" not in tools
    # a builtin's name IS what the model calls
    assert "code-interpreter" in tools
    # an untyped entry keeps its old behaviour
    assert tools["legacy-attachment"] == "no type field"
    assert "get_pto_balance" in tools
