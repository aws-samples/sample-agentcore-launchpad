"""Recommendations started from an evaluation run (V2 评估任务 → 优化建议).

The run's batch evaluation pins the traces; the inputs come from a live GetHarness
for a Managed Harness, from the Launchpad spec otherwise, and from the operator
when neither can supply them.
"""

import json
from unittest.mock import MagicMock

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import recommendations as recs
from app.evaluation.models import EvalRun
from app.models.ledger import Agent

BATCH_ARN = "arn:aws:bedrock-agentcore:us-west-2:1:batch-evaluation/be-1"
GW_ARN = "arn:aws:bedrock-agentcore:us-west-2:1:gateway/gw-abc"


def _agent(**fields) -> str:
    db = SessionLocal()
    try:
        agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=fields.pop("name", "rec-agent"),
                      status="active", **fields)
        db.add(agent)
        db.commit()
        return agent.id
    finally:
        db.close()


def _run(agent_id: str = "", **fields) -> str:
    db = SessionLocal()
    try:
        run = EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                      agent_name="rec-agent", **{
                          "mode": "evaluators", "status": "completed",
                          "batch_eval_id": "be-1", "evaluators": ["Builtin.Correctness"],
                          **fields})
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


def _harness_control() -> MagicMock:
    control = MagicMock()
    control.get_harness.return_value = {"harness": {
        "systemPrompt": [{"text": "You are the HR assistant."}, {"text": "Cite policy."}],
        "allowedTools": ["@leave_calc", "@launchpad_gw/hr-database___get_*", "shell"],
        "tools": [
            {"type": "inline_function", "name": "leave_calc",
             "config": {"inlineFunction": {"description": "Compute leave balance."}}},
            {"type": "inline_function", "name": "not_allowed",
             "config": {"inlineFunction": {"description": "Never selected."}}},
            {"type": "agentcore_gateway", "name": "launchpad_gw",
             "config": {"agentCoreGateway": {"gatewayArn": GW_ARN}}},
            {"type": "remote_mcp", "name": "docs",
             "config": {"remoteMcp": {"url": "https://example.com/mcp"}}},
        ],
    }}
    control.list_gateway_targets.return_value = {"items": [{"targetId": "t1"}]}
    control.get_gateway_target.return_value = {
        "targetId": "t1", "name": "hr-database",
        "targetConfiguration": {"mcp": {"lambda": {"toolSchema": {"inlinePayload": [
            {"name": "get_employee", "description": "Look up one employee."},
            {"name": "create_payout", "description": "Pay someone."},
        ]}}}},
    }
    return control


def _span(session_id: str, tool: str) -> dict:
    return {"name": f"execute_tool {tool}", "scope": {"name": "strands.telemetry"},
            "attributes": {"session.id": session_id, "gen_ai.tool.name": tool}}


def _stub(monkeypatch, control=None, data=None, spans=None):
    """AWS stubs; ``spans`` maps session id → span docs the Logs Insights query returns."""
    control = control or MagicMock()
    data = data or MagicMock()
    data.get_batch_evaluation.return_value = {"batchEvaluationArn": BATCH_ARN}
    monkeypatch.setattr(recs, "control_client", lambda _ws: control)
    monkeypatch.setattr(recs, "data_client", lambda _ws: data)
    queries: list[dict] = []

    def insights(queries_in, hours, **_kw):
        queries.append({"sessions": sorted(queries_in), "hours": hours})
        return {sid: [{"@message": json.dumps(doc)} for doc in (spans or {}).get(sid, [])]
                + [{"@message": "plain log line"}] for sid in queries_in}

    monkeypatch.setattr("app.services.observability.run_insights_queries", insights)
    data.queries = queries
    return control, data


# ─── allowedTools grammar ───────────────────────────────────────────────────
@pytest.mark.parametrize(("allowed", "alias", "tool", "expected"), [
    (None, "gw", "t___x", True),
    ([], "gw", "t___x", True),
    (["*"], "gw", "t___x", True),
    (["@gw"], "gw", "t___x", True),
    (["@gw/t___*"], "gw", "t___x", True),
    (["@gw/t___*"], "gw", "other___x", False),
    (["@g*"], "gw", "t___x", True),
    (["gw"], "gw", "t___x", False),  # a plain name selects builtins only
    (["@calc"], "calc", None, True),
    (["@calc"], "other", None, False),
])
def test_allowed_tools_selection(allowed, alias, tool, expected):
    assert recs._selected(allowed, alias, tool) is expected


# ─── inputs ─────────────────────────────────────────────────────────────────
def test_harness_inputs_come_from_get_harness(client, monkeypatch):
    agent_id = _agent(method="harness", resource_id="hr_harness-1",
                      spec={"system_prompt": "stale spec prompt"})
    control, _ = _stub(monkeypatch, control=_harness_control())

    body = client.get(f"/api/eval/runs/{_run(agent_id)}/recommendation-inputs").json()

    assert body["source"] == "harness"
    # the deployed prompt, not the ledger spec's copy
    assert body["system_prompt"] == "You are the HR assistant.\nCite policy."
    assert {t["name"]: t["description"] for t in body["tools"]} == {
        "leave_calc": "Compute leave balance.",
        "hr-database___get_employee": "Look up one employee.",
    }
    assert body["notes"] == [{"code": "remote_mcp_runtime_only", "tool": "docs", "detail": ""}]
    assert control.list_gateway_targets.call_args.kwargs["gatewayIdentifier"] == "gw-abc"
    assert body["eligible"] is True


def test_unreadable_gateway_is_a_note_not_a_failure(client, monkeypatch):
    agent_id = _agent(method="harness", resource_id="hr_harness-1")
    control = _harness_control()
    control.list_gateway_targets.side_effect = RuntimeError("AccessDenied")
    _stub(monkeypatch, control=control)

    body = client.get(f"/api/eval/runs/{_run(agent_id)}/recommendation-inputs").json()

    assert [t["name"] for t in body["tools"]] == ["leave_calc"]
    assert body["notes"][0]["code"] == "gateway_unreadable"


@pytest.mark.parametrize(("spec", "source"), [
    ({"system_prompt": "Prompt kept in the ledger."}, "spec"),
    ({"name": "gone"}, "manual"),
])
def test_a_deleted_harness_falls_back_instead_of_failing(client, monkeypatch, spec, source):
    """Measured on prod 2026-09-30: runs outlive their Harness. The card must still
    offer input boxes (the traces are intact), not a 502."""
    agent_id = _agent(method="harness", resource_id="gone_harness-1", spec=spec)
    control = MagicMock()
    control.get_harness.side_effect = RuntimeError(
        "ResourceNotFoundException: Agent with name gone_harness-1 not found.")
    _stub(monkeypatch, control=control)

    res = client.get(f"/api/eval/runs/{_run(agent_id)}/recommendation-inputs")

    assert res.status_code == 200
    body = res.json()
    assert body["source"] == source
    assert body["eligible"] is True
    assert [n["code"] for n in body["notes"]] == ["harness_unreadable"]
    assert "ResourceNotFoundException" in body["notes"][0]["detail"]


def test_runtime_agent_inputs_come_from_the_spec(client, monkeypatch):
    agent_id = _agent(method="zip_runtime", resource_id="rt-1", spec={
        "system_prompt": "Be concise.",
        "tools": [{"type": "builtin", "name": "calculator", "description": "Do math."}],
    })
    _stub(monkeypatch)

    body = client.get(f"/api/eval/runs/{_run(agent_id)}/recommendation-inputs").json()

    assert body["source"] == "spec"
    assert body["system_prompt"] == "Be concise."
    assert [t["name"] for t in body["tools"]] == ["calculator"]


def test_byoc_and_log_source_runs_need_manual_inputs(client, monkeypatch):
    _stub(monkeypatch)
    byoc = _agent(method="byoc", resource_id="rt-9", spec={"name": "byo"})
    for run_id in (_run(byoc), _run("", log_source={"service_name": "x.DEFAULT",
                                                     "log_group_names": ["/g"]})):
        body = client.get(f"/api/eval/runs/{run_id}/recommendation-inputs").json()
        assert body["source"] == "manual"
        assert body["system_prompt"] == "" and body["tools"] == []


def test_lower_is_better_evaluators_are_never_optimization_targets(client, monkeypatch):
    _stub(monkeypatch)
    run_id = _run("", evaluators=["Builtin.Harmfulness", "judge-x", "Builtin.Correctness"])

    body = client.get(f"/api/eval/runs/{run_id}/recommendation-inputs").json()

    assert body["evaluators"] == [
        "judge-x", "Builtin.Correctness", "Builtin.GoalSuccessRate", "Builtin.Helpfulness",
    ]


# ─── start ──────────────────────────────────────────────────────────────────
def test_start_pins_the_batch_and_the_chosen_evaluator(client, monkeypatch):
    spans = {sid: [_span(sid, "lookup")] for sid in ("sess-0001", "sess-0002")}
    _, data = _stub(monkeypatch, spans=spans)
    data.start_recommendation.side_effect = [{"recommendationId": "rec-sp"},
                                             {"recommendationId": "rec-td"}]
    run_id = _run("", session_ids=["sess-0001", "sess-0002"])

    res = client.post(f"/api/eval/runs/{run_id}/recommendations", json={
        "kinds": ["system_prompt", "tool_descriptions"], "input_source": "manual",
        "system_prompt": "  Help users.  ", "evaluator": "Builtin.Helpfulness",
        "tools": [{"name": "lookup", "description": "Find an order."}],
    })

    assert res.status_code == 201, res.text
    rows = res.json()["recommendations"]
    assert [(r["kind"], r["recommendation_id"]) for r in rows] == [
        ("system_prompt", "rec-sp"), ("tool_descriptions", "rec-td")]
    sp, td = (c.kwargs for c in data.start_recommendation.call_args_list)
    sp_config = sp["recommendationConfig"]["systemPromptRecommendationConfig"]
    assert sp_config["systemPrompt"] == {"text": "Help users."}
    assert sp_config["agentTraces"] == {"batchEvaluation": {"batchEvaluationArn": BATCH_ARN}}
    assert sp_config["evaluationConfig"]["evaluators"] == [
        {"evaluatorArn": "arn:aws:bedrock-agentcore:::evaluator/Builtin.Helpfulness"}]
    td_config = td["recommendationConfig"]["toolDescriptionRecommendationConfig"]
    assert td_config["toolDescription"]["toolDescriptionText"]["tools"] == [
        {"toolName": "lookup", "toolDescription": {"text": "Find an order."}}]
    # a tool job refuses a batchEvaluation source (live 2026-09-30): the same
    # sessions' spans go inline instead, non-JSON log rows dropped
    assert td_config["agentTraces"] == {
        "sessionSpans": spans["sess-0001"] + spans["sess-0002"]}
    assert [q["sessions"] for q in data.queries] == [["sess-0001", "sess-0002"]]
    assert recs.SPAN_LOOKBACK_MARGIN_H <= data.queries[0]["hours"] <= recs.SPAN_LOOKBACK_MAX_H
    assert all(len(c.kwargs["name"]) <= 48 for c in data.start_recommendation.call_args_list)


@pytest.mark.parametrize(("payload", "code"), [
    ({"kinds": ["system_prompt"], "system_prompt": "   "}, "recommendation.system_prompt_required"),
    ({"kinds": ["tool_descriptions"], "tools": []}, "recommendation.tools_required"),
    ({"kinds": ["tool_descriptions"], "tools": [{"name": "a", "description": ""}]},
     "recommendation.tool_description_required"),
])
def test_missing_inputs_are_refused_before_any_aws_call(client, monkeypatch, payload, code):
    _, data = _stub(monkeypatch)

    res = client.post(f"/api/eval/runs/{_run('')}/recommendations", json=payload)

    assert res.status_code == 422
    assert res.json()["code"] == code
    data.start_recommendation.assert_not_called()


def test_an_unfinished_run_cannot_seed_a_recommendation(client, monkeypatch):
    _, data = _stub(monkeypatch)
    run_id = _run("", status="evaluating")

    res = client.post(f"/api/eval/runs/{run_id}/recommendations",
                      json={"kinds": ["system_prompt"], "system_prompt": "x"})

    assert res.status_code == 409
    assert res.json()["code"] == "recommendation.run_not_completed"
    data.start_recommendation.assert_not_called()


def test_a_categorical_judge_is_refused(client, monkeypatch):
    control, data = _stub(monkeypatch)
    control.get_evaluator.return_value = {"evaluatorArn": "arn:x", "evaluatorConfig": {
        "llmAsAJudge": {"ratingScale": {"categorical": [{"label": "pass"}]}}}}

    res = client.post(f"/api/eval/runs/{_run('')}/recommendations", json={
        "kinds": ["system_prompt"], "system_prompt": "x", "evaluator": "judge-cat"})

    assert res.status_code == 422
    assert res.json()["code"] == "recommendation.evaluator_categorical"
    data.start_recommendation.assert_not_called()


def test_a_numeric_custom_judge_is_passed_by_its_arn(client, monkeypatch):
    control, data = _stub(monkeypatch)
    control.get_evaluator.return_value = {
        "evaluatorArn": "arn:aws:bedrock-agentcore:us-west-2:1:evaluator/judge-num",
        "evaluatorConfig": {"llmAsAJudge": {"ratingScale": {"numerical": [{"value": 1}]}}}}
    data.start_recommendation.return_value = {"recommendationId": "rec-1"}

    res = client.post(f"/api/eval/runs/{_run('')}/recommendations", json={
        "kinds": ["system_prompt"], "system_prompt": "x", "evaluator": "judge-num"})

    assert res.status_code == 201
    config = data.start_recommendation.call_args.kwargs["recommendationConfig"]
    assert config["systemPromptRecommendationConfig"]["evaluationConfig"]["evaluators"] == [
        {"evaluatorArn": "arn:aws:bedrock-agentcore:us-west-2:1:evaluator/judge-num"}]


# ─── refresh ────────────────────────────────────────────────────────────────
def test_reading_refreshes_until_the_job_completes(client, monkeypatch):
    _, data = _stub(monkeypatch)
    data.start_recommendation.return_value = {"recommendationId": "rec-1"}
    run_id = _run("")
    client.post(f"/api/eval/runs/{run_id}/recommendations",
                json={"kinds": ["system_prompt"], "system_prompt": "Old."})

    data.get_recommendation.return_value = {"status": "IN_PROGRESS"}
    row = client.get(f"/api/eval/runs/{run_id}/recommendations").json()["recommendations"][0]
    assert row["status"] == "IN_PROGRESS" and row["result"] == {}

    data.get_recommendation.return_value = {"status": "COMPLETED", "recommendationResult": {
        "systemPromptRecommendationResult": {"recommendedSystemPrompt": "New.",
                                             "explanation": "Sharper."}}}
    row = client.get(f"/api/eval/runs/{run_id}/recommendations").json()["recommendations"][0]
    assert row["status"] == "COMPLETED"
    assert row["result"] == {"recommended_prompt": "New.", "explanation": "Sharper."}

    # terminal: never read from AWS again
    calls = data.get_recommendation.call_count
    client.get(f"/api/eval/runs/{run_id}/recommendations")
    assert data.get_recommendation.call_count == calls


def test_a_completed_job_without_text_reads_as_failed(client, monkeypatch):
    _, data = _stub(monkeypatch)
    data.start_recommendation.return_value = {"recommendationId": "rec-1"}
    run_id = _run("")
    client.post(f"/api/eval/runs/{run_id}/recommendations",
                json={"kinds": ["system_prompt"], "system_prompt": "Old."})
    data.get_recommendation.return_value = {"status": "COMPLETED", "recommendationResult": {
        "systemPromptRecommendationResult": {"errorCode": "INSUFFICIENT_TRACES",
                                             "errorMessage": "need more sessions"}}}

    row = client.get(f"/api/eval/runs/{run_id}/recommendations").json()["recommendations"][0]

    assert row["status"] == "FAILED"
    assert row["error"] == "INSUFFICIENT_TRACES: need more sessions"
    assert row["result"] == {}


def test_tools_the_job_still_rejects_are_dropped_on_one_retry(client, monkeypatch):
    # "unused" is mentioned (so the loose pre-filter keeps it) but never called
    mention = {"name": "chat", "scope": {"name": "s"}, "attributes": {
        "session.id": "sess-0001", "gen_ai.prompt": "do not call unused"}}
    _, data = _stub(monkeypatch, spans={"sess-0001": [_span("sess-0001", "used"), mention]})
    data.start_recommendation.side_effect = [{"recommendationId": "rec-1"},
                                             {"recommendationId": "rec-2"}]
    run_id = _run("", session_ids=["sess-0001"])
    client.post(f"/api/eval/runs/{run_id}/recommendations", json={
        "kinds": ["tool_descriptions"],
        "tools": [{"name": "used", "description": "a"}, {"name": "unused", "description": "b"}],
    })
    data.get_recommendation.return_value = {"status": "FAILED", "recommendationResult": {
        "toolDescriptionRecommendationResult": {
            "errorCode": "ValidationException",
            "errorMessage": "Tools not found in the sampled agent traces: ['unused']"}}}

    row = client.get(f"/api/eval/runs/{run_id}/recommendations").json()["recommendations"][0]

    assert row["status"] == "PENDING"
    assert row["recommendation_id"] == "rec-2"
    assert row["skipped_tools"] == ["unused"]
    assert row["tools"] == {"used": "a"}
    assert row["name"].endswith(recs.RETRY_SUFFIX)
    retried = data.start_recommendation.call_args.kwargs
    assert retried["name"] == row["name"]
    listed = retried["recommendationConfig"]["toolDescriptionRecommendationConfig"][
        "toolDescription"]["toolDescriptionText"]["tools"]
    assert [t["toolName"] for t in listed] == ["used"]

    data.get_recommendation.return_value = {"status": "COMPLETED", "recommendationResult": {
        "toolDescriptionRecommendationResult": {"tools": [
            {"toolName": "used", "recommendedToolDescription": "Better a.",
             "explanation": "Clearer."}]}}}
    row = client.get(f"/api/eval/runs/{run_id}/recommendations").json()["recommendations"][0]
    assert row["status"] == "COMPLETED"
    assert row["result"] == {"tools": {"used": {"description": "Better a.",
                                                "explanation": "Clearer."}}}


def test_a_second_rejection_is_final(client, monkeypatch):
    _, data = _stub(monkeypatch, spans={"sess-0001": [_span("sess-0001", "a b c")]})
    data.start_recommendation.side_effect = [{"recommendationId": "rec-1"},
                                             {"recommendationId": "rec-2"}]
    run_id = _run("", session_ids=["sess-0001"])
    client.post(f"/api/eval/runs/{run_id}/recommendations", json={
        "kinds": ["tool_descriptions"],
        "tools": [{"name": n, "description": n} for n in ("a", "b", "c")]})

    def reject(name):
        return {"status": "FAILED", "recommendationResult": {
            "toolDescriptionRecommendationResult": {"errorMessage":
                f"Tools not found in the sampled agent traces: ['{name}']"}}}

    data.get_recommendation.return_value = reject("c")
    client.get(f"/api/eval/runs/{run_id}/recommendations")
    data.get_recommendation.return_value = reject("b")
    row = client.get(f"/api/eval/runs/{run_id}/recommendations").json()["recommendations"][0]

    assert row["status"] == "FAILED"
    assert data.start_recommendation.call_count == 2


def test_untraced_tools_are_skipped_before_the_job_starts(client, monkeypatch):
    _, data = _stub(monkeypatch, spans={"sess-0001": [_span("sess-0001", "kb___Retrieve")]})
    data.start_recommendation.return_value = {"recommendationId": "rec-1"}
    run_id = _run("", session_ids=["sess-0001"])

    row = client.post(f"/api/eval/runs/{run_id}/recommendations", json={
        "kinds": ["tool_descriptions"],
        "tools": [{"name": "kb___Retrieve", "description": "Search the KB."},
                  {"name": "other_kb___Retrieve", "description": "Another KB."}],
    }).json()["recommendations"][0]

    assert row["tools"] == {"kb___Retrieve": "Search the KB."}
    assert row["skipped_tools"] == ["other_kb___Retrieve"]
    # the tool-only path never needs the batch
    data.get_batch_evaluation.assert_not_called()


@pytest.mark.parametrize(("session_ids", "spans", "tools", "status", "code"), [
    ([], {}, ["lookup_order"], 409, "recommendation.run_no_sessions"),
    (["sess-0001"], {}, ["lookup_order"], 409, "recommendation.no_spans"),
    (["sess-0001"], {"sess-0001": [_span("sess-0001", "other_tool")]}, ["lookup_order"],
     422, "recommendation.tools_not_traced"),
])
def test_tool_jobs_need_traced_sessions(
    client, monkeypatch, session_ids, spans, tools, status, code,
):
    _, data = _stub(monkeypatch, spans=spans)
    run_id = _run("", session_ids=session_ids)

    res = client.post(f"/api/eval/runs/{run_id}/recommendations", json={
        "kinds": ["system_prompt", "tool_descriptions"], "system_prompt": "x",
        "tools": [{"name": n, "description": "d"} for n in tools]})

    assert res.status_code == status
    assert res.json()["code"] == code
    # refused before either kind started — never a half-started pair
    data.start_recommendation.assert_not_called()


def test_a_window_run_offers_prompt_recommendations_only(client, monkeypatch):
    _stub(monkeypatch)

    body = client.get(f"/api/eval/runs/{_run('', session_ids=[])}/recommendation-inputs").json()

    assert body["eligible"] is True
    assert body["tools_eligible"] is False


def test_another_workspaces_run_is_not_found(client, monkeypatch):
    _stub(monkeypatch)
    db = SessionLocal()
    run = EvalRun(workspace_id="other-ws", agent_id="", agent_name="x",
                  status="completed", batch_eval_id="be-9")
    db.add(run)
    db.commit()
    run_id = run.id
    db.close()

    assert client.get(f"/api/eval/runs/{run_id}/recommendations").status_code == 404
    assert client.post(f"/api/eval/runs/{run_id}/recommendations",
                       json={"kinds": ["system_prompt"], "system_prompt": "x"}
                       ).status_code == 404
