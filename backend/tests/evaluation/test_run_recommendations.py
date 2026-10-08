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


@pytest.fixture(autouse=True)
def _fresh_catalog_cache():
    """The gateway / evaluator catalogs are cached per process; one test's stubbed
    catalog must never answer another's."""
    recs.clear_catalog_cache()
    yield
    recs.clear_catalog_cache()
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
    if not isinstance(control.list_evaluators.return_value, dict):
        # an unset MagicMock answers a truthy nextToken — pagination would never end
        control.list_evaluators.return_value = {"evaluators": []}
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


def _judge(evaluator_id: str, *, instructions="Rate the answer.", categorical=False):
    scale = ({"categorical": [{"label": "pass"}]} if categorical
             else {"numerical": [{"value": 1.0, "label": "pass"}]})
    return {"evaluatorId": evaluator_id, "evaluatorArn": f"arn:x:evaluator/{evaluator_id}",
            "evaluatorConfig": {"llmAsAJudge": {"instructions": instructions,
                                                "ratingScale": scale}}}


def _account(control):
    control.list_evaluators.return_value = {"evaluators": [
        {"evaluatorId": "Builtin.Helpfulness", "evaluatorType": "Builtin", "status": "ACTIVE"},
        {"evaluatorId": "ThirdParty.DeepEval.TaskCompletion", "evaluatorType": "ThirdParty",
         "evaluatorName": "TaskCompletion", "level": "TRACE", "status": "ACTIVE"},
        {"evaluatorId": "ThirdParty.DeepEval.Toxicity", "evaluatorType": "ThirdParty",
         "level": "TRACE", "status": "ACTIVE"},
        {"evaluatorId": "judge-num", "evaluatorType": "Custom", "evaluatorName": "tone",
         "level": "TRACE", "status": "ACTIVE"},
        {"evaluatorId": "judge-cat", "evaluatorType": "Custom", "level": "TRACE",
         "status": "ACTIVE"},
        {"evaluatorId": "judge-gt", "evaluatorType": "Custom", "level": "TRACE",
         "status": "ACTIVE"},
        {"evaluatorId": "judge-new", "evaluatorType": "Custom", "status": "CREATING"},
    ]}
    details = {
        "judge-num": _judge("judge-num"),
        "judge-cat": _judge("judge-cat", categorical=True),
        "judge-gt": _judge("judge-gt", instructions="Compare with {expected_response}."),
    }
    control.get_evaluator.side_effect = lambda evaluatorId: details[evaluatorId]
    return control


def test_every_usable_evaluator_is_offered_grouped_and_recommended(client, monkeypatch):
    control, _ = _stub(monkeypatch, control=_account(MagicMock()))
    run_id = _run("", evaluators=["Builtin.Refusal", "judge-num", "Builtin.Correctness",
                                  "judge-gt", "judge-deleted"])

    body = client.get(f"/api/eval/runs/{run_id}/recommendation-inputs").json()
    options = {o["id"]: o for o in body["evaluators"]}
    order = [o["id"] for o in body["evaluators"]]

    # the run's own usable evaluators lead, then the two recommended targets
    assert order[:4] == ["judge-num", "Builtin.Correctness",
                         "Builtin.GoalSuccessRate", "Builtin.Helpfulness"]
    assert {o["id"] for o in body["evaluators"] if o["recommended"]} == {
        "Builtin.GoalSuccessRate", "Builtin.Helpfulness"}
    assert options["judge-num"]["group"] == "run"
    assert options["judge-num"]["name"] == "tone"
    assert options["Builtin.Faithfulness"]["group"] == "builtin"
    assert options["ThirdParty.DeepEval.TaskCompletion"]["group"] == "third_party"
    assert body["default_evaluator"] == "Builtin.GoalSuccessRate"
    # nothing that cannot produce a usable signal from traces alone
    assert {e["id"]: e["reason"] for e in body["excluded_evaluators"]} == {
        "judge-deleted": "unavailable",
        "judge-gt": "ground_truth",
        "Builtin.Refusal": "lower_is_better",
        "Builtin.TrajectoryExactOrderMatch": "ground_truth",
        "Builtin.TrajectoryInOrderMatch": "ground_truth",
        "Builtin.TrajectoryAnyOrderMatch": "ground_truth",
        "ThirdParty.DeepEval.Toxicity": "lower_is_better",
        "judge-cat": "categorical",
    }
    assert "judge-new" not in options  # not ACTIVE yet
    # a managed third-party evaluator has no config to screen — never read back
    assert "ThirdParty.DeepEval.TaskCompletion" not in {
        c.kwargs["evaluatorId"] for c in control.get_evaluator.call_args_list}


def test_an_insights_run_still_offers_every_evaluator(client, monkeypatch):
    _stub(monkeypatch, control=_account(MagicMock()))
    run_id = _run("", mode="insights", evaluators=["Builtin.Insight.FailureAnalysis"])

    body = client.get(f"/api/eval/runs/{run_id}/recommendation-inputs").json()

    ids = [o["id"] for o in body["evaluators"]]
    assert ids[:2] == ["Builtin.GoalSuccessRate", "Builtin.Helpfulness"]
    assert "judge-num" in ids and "Builtin.Faithfulness" in ids
    assert "run" not in {o["group"] for o in body["evaluators"]}


def test_an_unlistable_account_still_offers_the_builtins(client, monkeypatch):
    control = MagicMock()
    control.list_evaluators.side_effect = RuntimeError("AccessDenied")
    _stub(monkeypatch, control=control)

    body = client.get(f"/api/eval/runs/{_run('')}/recommendation-inputs").json()

    # the run's own built-in stays in its group; no account evaluator can be listed
    assert {o["group"] for o in body["evaluators"]} == {"run", "builtin"}
    assert "Builtin.GoalSuccessRate" in {o["id"] for o in body["evaluators"]}


@pytest.mark.parametrize(("evaluator", "code"), [
    ("Builtin.Refusal", "recommendation.evaluator_lower_is_better"),
    ("Builtin.TrajectoryInOrderMatch", "recommendation.evaluator_ground_truth"),
    ("judge-gt", "recommendation.evaluator_ground_truth"),
])
def test_start_refuses_what_the_options_hide(client, monkeypatch, evaluator, code):
    _, data = _stub(monkeypatch, control=_account(MagicMock()))

    res = client.post(f"/api/eval/runs/{_run('')}/recommendations", json={
        "kinds": ["system_prompt"], "system_prompt": "x", "evaluator": evaluator})

    assert res.status_code == 422
    assert res.json()["code"] == code
    data.start_recommendation.assert_not_called()


def test_catalogs_are_cached_but_the_harness_is_read_live(client, monkeypatch):
    agent_id = _agent(method="harness", resource_id="hr_harness-1")
    control = _account(_harness_control())
    _stub(monkeypatch, control=control)
    run_id = _run(agent_id)

    first = client.get(f"/api/eval/runs/{run_id}/recommendation-inputs").json()
    second = client.get(f"/api/eval/runs/{run_id}/recommendation-inputs").json()

    assert first["tools"] == second["tools"] and first["evaluators"] == second["evaluators"]
    # one catalog read for two card loads …
    assert control.list_gateway_targets.call_count == 1
    assert control.get_gateway_target.call_count == 1
    assert control.list_evaluators.call_count == 1
    # … while the Harness (prompt, tool list) is read every time: edits show at once
    assert control.get_harness.call_count == 2


def test_the_catalog_cache_expires(client, monkeypatch):
    control = _account(MagicMock())
    _stub(monkeypatch, control=control)
    run_id = _run("")
    clock = [1000.0]
    monkeypatch.setattr(recs.time, "monotonic", lambda: clock[0])

    client.get(f"/api/eval/runs/{run_id}/recommendation-inputs")
    clock[0] += recs.CATALOG_TTL_S - 1
    client.get(f"/api/eval/runs/{run_id}/recommendation-inputs")
    assert control.list_evaluators.call_count == 1
    clock[0] += 2
    client.get(f"/api/eval/runs/{run_id}/recommendation-inputs")
    assert control.list_evaluators.call_count == 2


def test_a_failed_catalog_read_is_not_cached(client, monkeypatch):
    control = MagicMock()
    control.list_evaluators.side_effect = [RuntimeError("Throttling"),
                                           {"evaluators": []}]
    _stub(monkeypatch, control=control)
    run_id = _run("")

    client.get(f"/api/eval/runs/{run_id}/recommendation-inputs")
    client.get(f"/api/eval/runs/{run_id}/recommendation-inputs")

    assert control.list_evaluators.call_count == 2  # the failure was retried


def test_gateway_targets_are_read_in_parallel(monkeypatch):
    """A shared KB gateway has 15+ targets; reading them one by one cost ~5 s."""
    import threading

    control = MagicMock()
    control.list_gateway_targets.return_value = {
        "items": [{"targetId": f"t{i}"} for i in range(6)]}
    barrier = threading.Barrier(6, timeout=5)  # only passes if all 6 run at once

    def get_target(gatewayIdentifier, targetId):
        barrier.wait()
        return {"targetId": targetId, "name": f"kb{targetId}",
                "targetConfiguration": {"mcp": {"lambda": {"toolSchema": {"inlinePayload": [
                    {"name": "Retrieve", "description": f"KB {targetId}"}]}}}}}

    control.get_gateway_target.side_effect = get_target
    ws = MagicMock(id="w1", account_id="1", region="us-west-2")

    actions = recs._gateway_actions(control, ws, "gw-1")

    assert sorted(a["name"] for a in actions) == [f"kbt{i}___Retrieve" for i in range(6)]


def test_catalog_cache_keys_on_workspace_and_gateway():
    calls = []

    def load(tag):
        return lambda: calls.append(tag) or tag

    a = recs._cached(("gateway", "w1", "1", "us-west-2", "gw-1"), load("a"))
    b = recs._cached(("gateway", "w1", "1", "us-west-2", "gw-2"), load("b"))
    c = recs._cached(("gateway", "w2", "1", "us-east-1", "gw-1"), load("c"))
    again = recs._cached(("gateway", "w1", "1", "us-west-2", "gw-1"), load("x"))

    assert (a, b, c, again) == ("a", "b", "c", "a")
    assert calls == ["a", "b", "c"]


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


# ─── accept → a new Harness version ─────────────────────────────────────────
def _harness_agent(**fields) -> str:
    from app.schemas.agent import AgentSpec

    spec = AgentSpec(name="rec-agent", method="harness", system_prompt="old prompt").model_dump()
    return _agent(method="harness", spec=spec, version="1",
                  arn="arn:aws:bedrock-agentcore:us-west-2:1:harness/rec_agent-abcdefghij",
                  resource_id="rec_agent-abcdefghij", **fields)


def _rec(run_id: str, **fields) -> str:
    from app.evaluation.models import EvalRecommendation

    db = SessionLocal()
    try:
        row = EvalRecommendation(workspace_id=DEFAULT_WORKSPACE_ID, run_id=run_id,
                                 name="rec_sp", **{
                                     "recommendation_id": "rec-aws-1",
                                     "kind": "system_prompt", "status": "COMPLETED",
                                     "result": {"recommended_prompt": "new prompt"},
                                     **fields})
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _no_deploy(monkeypatch) -> list[str]:
    started: list[str] = []
    monkeypatch.setattr("app.routers.agents.start_deploy_async", started.append)
    return started


def test_accept_republishes_the_harness_with_the_recommended_prompt(client, monkeypatch):
    _stub(monkeypatch)
    started = _no_deploy(monkeypatch)
    agent_id = _harness_agent()
    run_id = _run(agent_id)
    rec_id = _rec(run_id)

    res = client.post(f"/api/eval/runs/{run_id}/recommendations/{rec_id}/accept")

    assert res.status_code == 202, res.text
    body = res.json()
    assert started == [body["job_id"]]
    accepted = body["recommendation"]["accepted"]
    assert accepted["agent_id"] == agent_id
    assert accepted["previous_version"] == "1"
    assert accepted["job_id"] == body["job_id"]
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        assert agent.spec["system_prompt"] == "new prompt"
        assert agent.status == "deploying"
    finally:
        db.close()
    # accepted once
    again = client.post(f"/api/eval/runs/{run_id}/recommendations/{rec_id}/accept")
    assert again.status_code == 409
    assert again.json()["code"] == "recommendation.already_accepted"
    assert len(started) == 1


def test_accept_drops_the_kb_section_the_deployer_appends(client, monkeypatch):
    """The recommendation revised the LIVE prompt (spec prompt + the platform's KB
    section); storing that verbatim made the next deploy append the section twice."""
    from app.deployer.harness import _kb_prompt
    from app.schemas.agent import AgentSpec

    _stub(monkeypatch)
    _no_deploy(monkeypatch)
    spec = AgentSpec(name="rec-agent", method="harness", system_prompt="old prompt",
                     knowledge_bases=[{"kb_id": "KB12345678", "name": "earnings"}])
    agent_id = _agent(method="harness", spec=spec.model_dump(), version="1",
                      arn="arn:aws:bedrock-agentcore:us-west-2:1:harness/rec_agent-abcdefghij",
                      resource_id="rec_agent-abcdefghij")
    run_id = _run(agent_id)
    live = "revised prompt" + _kb_prompt(spec)
    rec_id = _rec(run_id, result={"recommended_prompt": live})

    res = client.post(f"/api/eval/runs/{run_id}/recommendations/{rec_id}/accept")

    assert res.status_code == 202, res.text
    db = SessionLocal()
    try:
        assert db.get(Agent, agent_id).spec["system_prompt"] == "revised prompt"
    finally:
        db.close()


def test_strip_generated_prompt_tolerates_a_reflowed_kb_section():
    from app.deployer.harness import strip_generated_prompt
    from app.schemas.agent import AgentSpec

    spec = AgentSpec(name="rec-agent", method="harness", system_prompt="p",
                     knowledge_bases=[{"kb_id": "KB12345678", "name": "earnings"}])
    reflowed = ("keep me\n## Knowledge bases\nRetrieval tools are mounted for you. (edited)\n"
                "- earnings …\nGround answers on retrieved content and cite sources when you "
                "use them.\nand this tail")
    assert strip_generated_prompt(spec, reflowed) == "keep me\nand this tail"
    assert strip_generated_prompt(spec, "no section") == "no section"


@pytest.mark.parametrize(("rec_fields", "code", "status"), [
    ({"kind": "tool_descriptions", "result": {"tools": {}}}, "recommendation.accept_kind", 400),
    ({"status": "IN_PROGRESS", "result": {}}, "recommendation.not_completed", 409),
    ({"result": {"recommended_prompt": "  "}}, "recommendation.not_completed", 409),
])
def test_accept_refuses_what_cannot_become_a_version(client, monkeypatch, rec_fields, code,
                                                     status):
    _stub(monkeypatch)
    started = _no_deploy(monkeypatch)
    run_id = _run(_harness_agent())
    rec_id = _rec(run_id, **rec_fields)

    res = client.post(f"/api/eval/runs/{run_id}/recommendations/{rec_id}/accept")

    assert res.status_code == status
    assert res.json()["code"] == code
    assert started == []


def test_accept_needs_a_platform_harness(client, monkeypatch):
    _stub(monkeypatch)
    started = _no_deploy(monkeypatch)
    run_id = _run(_agent(method="zip_runtime", spec={"system_prompt": "x"}))
    rec_id = _rec(run_id)

    res = client.post(f"/api/eval/runs/{run_id}/recommendations/{rec_id}/accept")

    assert res.status_code == 400
    assert res.json()["code"] == "recommendation.accept_not_harness"
    assert started == []


def test_accept_of_another_runs_recommendation_is_not_found(client, monkeypatch):
    _stub(monkeypatch)
    _no_deploy(monkeypatch)
    agent_id = _harness_agent()
    rec_id = _rec(_run(agent_id))

    res = client.post(f"/api/eval/runs/{_run(agent_id)}/recommendations/{rec_id}/accept")

    assert res.status_code == 404


# ─── 3rd-party provider (GEPA-lite) ─────────────────────────────────────────
SOURCE = {"kind": "batch_evaluation", "run_id": "r", "batch_eval_id": "be-1",
          "results_log_group": "/aws/bedrock-agentcore/evaluations/x", "results_log_stream": "s"}


def _provider_stubs(monkeypatch, outcome: dict | Exception) -> list[dict]:
    """Run the provider thread inline; ``outcome`` is what the reflection returns."""
    from app.optimization import service as opt_service

    calls: list[dict] = []
    monkeypatch.setattr(recs, "_spawn", lambda fn: fn())
    monkeypatch.setattr(opt_service, "resolve_recommend_source", lambda *_a: dict(SOURCE))

    def reflect(exp_id, agent, workspace, progress, **kw):
        calls.append({"agent": agent, **kw})
        progress("reflecting…")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(opt_service, "_third_party_prompt_recommendation", reflect)
    return calls


def test_a_provider_recommendation_never_starts_an_aws_job(client, monkeypatch):
    _, data = _stub(monkeypatch)
    calls = _provider_stubs(monkeypatch, {
        "system_prompt_status": "COMPLETED", "recommended_prompt": "Better prompt.",
        "explanation": "fixed the date rule", "provider_meta": {"evidence_sessions": 6},
    })
    agent_id = _harness_agent()
    run_id = _run(agent_id)

    res = client.post(f"/api/eval/runs/{run_id}/recommendations", json={
        "kinds": ["system_prompt"], "input_source": "harness", "system_prompt": "Old.",
        "provider": "gepa_lite", "model_id": "global.anthropic.claude-sonnet-5",
    })

    assert res.status_code == 201, res.text
    row = res.json()["recommendations"][0]
    assert row["recommendation_id"].startswith(recs.PROVIDER_JOB_PREFIX + "gepa_lite-")
    data.start_recommendation.assert_not_called()
    assert calls[0]["provider_id"] == "gepa_lite"
    assert calls[0]["model_id"] == "global.anthropic.claude-sonnet-5"
    assert calls[0]["agent"]["system_prompt"] == "Old."
    assert calls[0]["source"] == SOURCE
    listed = client.get(f"/api/eval/runs/{run_id}/recommendations").json()["recommendations"][0]
    assert listed["status"] == "COMPLETED"
    assert listed["result"]["recommended_prompt"] == "Better prompt."
    assert listed["result"]["provider"] == "gepa_lite"
    assert listed["result"]["provider_model_id"] == "global.anthropic.claude-sonnet-5"
    assert listed["evaluator"] is None
    data.get_recommendation.assert_not_called()


@pytest.mark.parametrize("outcome", [
    {"system_prompt_status": "FAILED", "system_prompt_error": "no scored sessions"},
    RuntimeError("bedrock throttled"),
])
def test_a_failed_provider_run_reads_as_failed(client, monkeypatch, outcome):
    _stub(monkeypatch)
    _provider_stubs(monkeypatch, outcome)
    run_id = _run(_harness_agent())

    client.post(f"/api/eval/runs/{run_id}/recommendations", json={
        "kinds": ["system_prompt"], "system_prompt": "Old.", "provider": "gepa_lite"})

    row = client.get(f"/api/eval/runs/{run_id}/recommendations").json()["recommendations"][0]
    assert row["status"] == "FAILED"
    assert ("no scored sessions" in row["error"]) or ("bedrock throttled" in row["error"])
    assert "recommended_prompt" not in row["result"]


def test_a_provider_row_left_running_by_a_restart_reads_as_interrupted(client, monkeypatch):
    _, data = _stub(monkeypatch)
    run_id = _run(_harness_agent())
    _rec(run_id, recommendation_id=recs.PROVIDER_JOB_PREFIX + "gepa_lite-abc",
         status="IN_PROGRESS", result={"provider": "gepa_lite"})

    row = client.get(f"/api/eval/runs/{run_id}/recommendations").json()["recommendations"][0]

    assert row["status"] == "FAILED"
    assert "interrupted" in row["error"]
    data.get_recommendation.assert_not_called()


def test_a_provider_needs_an_agent_run(client, monkeypatch):
    _stub(monkeypatch)
    _provider_stubs(monkeypatch, {})
    run_id = _run("")

    res = client.post(f"/api/eval/runs/{run_id}/recommendations", json={
        "kinds": ["system_prompt"], "system_prompt": "Old.", "provider": "gepa_lite"})

    assert res.status_code == 422
    assert res.json()["code"] == "recommendation.provider_needs_agent"


def test_an_unknown_provider_is_refused(client, monkeypatch):
    _stub(monkeypatch)
    run_id = _run(_harness_agent())
    res = client.post(f"/api/eval/runs/{run_id}/recommendations", json={
        "kinds": ["system_prompt"], "system_prompt": "Old.", "provider": "mystery"})
    assert res.status_code == 422


def test_a_provider_recommendation_can_be_accepted(client, monkeypatch):
    _stub(monkeypatch)
    started = _no_deploy(monkeypatch)
    _provider_stubs(monkeypatch, {"system_prompt_status": "COMPLETED",
                                  "recommended_prompt": "Better prompt."})
    agent_id = _harness_agent()
    run_id = _run(agent_id)
    rec_id = client.post(f"/api/eval/runs/{run_id}/recommendations", json={
        "kinds": ["system_prompt"], "system_prompt": "Old.", "provider": "gepa_lite",
    }).json()["recommendations"][0]["id"]

    res = client.post(f"/api/eval/runs/{run_id}/recommendations/{rec_id}/accept")

    assert res.status_code == 202, res.text
    assert len(started) == 1
    db = SessionLocal()
    try:
        assert db.get(Agent, agent_id).spec["system_prompt"] == "Better prompt."
    finally:
        db.close()


# ─── accept a reviewed (edited) prompt ──────────────────────────────────────
def test_accept_publishes_the_reviewed_prompt_and_records_the_edit(client, monkeypatch):
    _stub(monkeypatch)
    started = _no_deploy(monkeypatch)
    agent_id = _harness_agent()
    run_id = _run(agent_id)
    rec_id = _rec(run_id)

    res = client.post(f"/api/eval/runs/{run_id}/recommendations/{rec_id}/accept",
                      json={"system_prompt": "  new prompt, minus the loosened rule  "})

    assert res.status_code == 202, res.text
    assert res.json()["recommendation"]["accepted"]["edited"] is True
    assert len(started) == 1
    db = SessionLocal()
    try:
        published = db.get(Agent, agent_id).spec["system_prompt"]
        assert published == "new prompt, minus the loosened rule"
    finally:
        db.close()


def test_accepting_the_unchanged_text_is_not_an_edit(client, monkeypatch):
    _stub(monkeypatch)
    _no_deploy(monkeypatch)
    run_id = _run(_harness_agent())
    rec_id = _rec(run_id)

    res = client.post(f"/api/eval/runs/{run_id}/recommendations/{rec_id}/accept",
                      json={"system_prompt": "new prompt"})

    assert res.status_code == 202, res.text
    assert res.json()["recommendation"]["accepted"]["edited"] is False


def test_an_empty_reviewed_prompt_is_refused(client, monkeypatch):
    _stub(monkeypatch)
    started = _no_deploy(monkeypatch)
    run_id = _run(_harness_agent())
    rec_id = _rec(run_id)

    res = client.post(f"/api/eval/runs/{run_id}/recommendations/{rec_id}/accept",
                      json={"system_prompt": "   "})

    assert res.status_code == 422
    assert res.json()["code"] == "recommendation.accept_prompt_empty"
    assert started == []


# ─── save a manual revision before accepting ────────────────────────────────
def test_a_saved_edit_is_published_on_accept(client, monkeypatch):
    _stub(monkeypatch)
    started = _no_deploy(monkeypatch)
    agent_id = _harness_agent()
    run_id = _run(agent_id)
    rec_id = _rec(run_id)
    url = f"/api/eval/runs/{run_id}/recommendations/{rec_id}"

    res = client.put(f"{url}/edit", json={"system_prompt": "  new prompt, tightened  "})
    assert res.status_code == 200, res.text
    edit = res.json()["recommendation"]["edit"]
    assert edit["prompt"] == "new prompt, tightened" and edit["by"] and edit["at"]
    # the generated text stays untouched next to the revision
    assert res.json()["recommendation"]["result"]["recommended_prompt"] == "new prompt"
    listed = client.get(f"/api/eval/runs/{run_id}/recommendations").json()["recommendations"]
    assert listed[0]["edit"]["prompt"] == "new prompt, tightened"
    assert started == []  # saving publishes nothing

    res = client.post(f"{url}/accept")
    assert res.status_code == 202, res.text
    assert res.json()["recommendation"]["accepted"]["edited"] is True
    db = SessionLocal()
    try:
        assert db.get(Agent, agent_id).spec["system_prompt"] == "new prompt, tightened"
    finally:
        db.close()
    # an accepted recommendation is locked
    locked = client.put(f"{url}/edit", json={"system_prompt": "again"})
    assert locked.status_code == 409 and locked.json()["code"] == "recommendation.already_accepted"


@pytest.mark.parametrize("body", [{"system_prompt": None}, {"system_prompt": "  "},
                                  {"system_prompt": "new prompt"}])
def test_clearing_or_restoring_the_text_drops_the_edit(client, monkeypatch, body):
    _stub(monkeypatch)
    _no_deploy(monkeypatch)
    run_id = _run(_harness_agent())
    rec_id = _rec(run_id)
    url = f"/api/eval/runs/{run_id}/recommendations/{rec_id}"
    assert client.put(f"{url}/edit", json={"system_prompt": "changed"}).json()[
        "recommendation"]["edit"]["prompt"] == "changed"

    res = client.put(f"{url}/edit", json=body)
    assert res.status_code == 200 and res.json()["recommendation"]["edit"] is None
    res = client.post(f"{url}/accept")
    assert res.status_code == 202 and res.json()["recommendation"]["accepted"]["edited"] is False


@pytest.mark.parametrize(("rec_fields", "code", "status"), [
    ({"kind": "tool_descriptions", "result": {"tools": {}}}, "recommendation.edit_kind", 400),
    ({"status": "IN_PROGRESS", "result": {}}, "recommendation.not_completed", 409),
])
def test_only_a_completed_prompt_recommendation_is_editable(client, monkeypatch, rec_fields,
                                                            code, status):
    _stub(monkeypatch)
    run_id = _run(_harness_agent())
    rec_id = _rec(run_id, **rec_fields)
    res = client.put(f"/api/eval/runs/{run_id}/recommendations/{rec_id}/edit",
                     json={"system_prompt": "x"})
    assert res.status_code == status and res.json()["code"] == code


def test_editing_another_runs_recommendation_is_not_found(client, monkeypatch):
    _stub(monkeypatch)
    agent_id = _harness_agent()
    rec_id = _rec(_run(agent_id))
    res = client.put(f"/api/eval/runs/{_run(agent_id)}/recommendations/{rec_id}/edit",
                     json={"system_prompt": "x"})
    assert res.status_code == 404
