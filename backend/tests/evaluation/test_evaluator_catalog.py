"""GET /api/eval/evaluators — catalog classification.

Builtins (incl. the skill evaluators) come from the local dicts; live
ListEvaluators rows are classified third_party vs custom and project their
evaluatorType/provider. The AWS call is fail-soft: builtins render without it.
"""

from unittest.mock import MagicMock

GENERAL_PROMPT_TEMPLATE_EVALUATORS = {
    "Builtin.GoalSuccessRate": "SESSION",
    "Builtin.Helpfulness": "TRACE",
    "Builtin.Correctness": "TRACE",
    "Builtin.Faithfulness": "TRACE",
    "Builtin.ResponseRelevance": "TRACE",
    "Builtin.ContextRelevance": "TRACE",
    "Builtin.Conciseness": "TRACE",
    "Builtin.Coherence": "TRACE",
    "Builtin.InstructionFollowing": "TRACE",
    "Builtin.Refusal": "TRACE",
    "Builtin.Harmfulness": "TRACE",
    "Builtin.Stereotyping": "TRACE",
    "Builtin.ToolSelectionAccuracy": "TOOL_CALL",
    "Builtin.ToolParameterAccuracy": "TOOL_CALL",
}
SKILL_EVALUATORS = {
    "Builtin.SkillSelectionAccuracy": "TOOL_CALL",
    "Builtin.SkillInstructionFollowing": "TOOL_CALL",
}
TRAJECTORY_EVALUATORS = {
    "Builtin.TrajectoryExactOrderMatch": "SESSION",
    "Builtin.TrajectoryInOrderMatch": "SESSION",
    "Builtin.TrajectoryAnyOrderMatch": "SESSION",
}
EXPECTED_BUILTIN_EVALUATORS = {
    **GENERAL_PROMPT_TEMPLATE_EVALUATORS,
    **SKILL_EVALUATORS,
    **TRAJECTORY_EVALUATORS,
}


LIVE_EVALUATORS = [
    {
        "evaluatorId": "ThirdParty.DeepEval.TaskCompletion",
        "evaluatorName": "Task Completion",
        "level": "SESSION",
        "status": "ACTIVE",
        "evaluatorType": "ThirdParty",
        "provider": "DeepEval",
    },
    {
        "evaluatorId": "ThirdParty.AutoEval.Security",
        "evaluatorName": "Security",
        "level": "SESSION",
        "status": "ACTIVE",
        "evaluatorType": "ThirdParty",
        "provider": "AutoEval",
    },
    {
        "evaluatorId": "my_judge-abc123",
        "evaluatorName": "my_judge",
        "level": "TRACE",
        "status": "ACTIVE",
        "evaluatorType": "Custom",
        "provider": "Custom",
    },
    {
        "evaluatorId": "my_derived-def456",
        "evaluatorName": "my_derived",
        "level": "SESSION",
        "status": "ACTIVE",
        "evaluatorType": "CustomDerived",
        "provider": "DeepEval",
    },
    # the account listing echoes builtins too — must not duplicate the local rows
    {
        "evaluatorId": "Builtin.Helpfulness",
        "evaluatorName": "Helpfulness",
        "level": "TRACE",
        "status": "ACTIVE",
        "evaluatorType": "Builtin",
        "provider": "AWS",
    },
]


def stub_control(monkeypatch, error=None):
    stub = MagicMock()
    if error is not None:
        stub.list_evaluators.side_effect = error
    else:
        stub.list_evaluators.return_value = {"evaluators": LIVE_EVALUATORS}
    monkeypatch.setattr("app.evaluation.routers.control_client", lambda _ws=None: stub)
    return stub


def test_catalog_classification(client, monkeypatch):
    stub_control(monkeypatch)
    res = client.get("/api/eval/evaluators")
    assert res.status_code == 200
    rows = res.json()["evaluators"]
    by_id = {r["id"]: r for r in rows}

    builtin_rows = [r for r in rows if r["source"] == "builtin"]
    builtin_by_id = {r["id"]: r for r in builtin_rows}

    # The local catalog is exact, with no duplicate from the live builtin echo.
    assert len(GENERAL_PROMPT_TEMPLATE_EVALUATORS) == 14
    assert len(SKILL_EVALUATORS) == 2
    assert len(TRAJECTORY_EVALUATORS) == 3
    assert len(builtin_rows) == len(EXPECTED_BUILTIN_EVALUATORS) == 19
    assert len(builtin_by_id) == len(builtin_rows)
    assert {evaluator_id: row["level"] for evaluator_id, row in builtin_by_id.items()} == (
        EXPECTED_BUILTIN_EVALUATORS
    )
    assert res.json()["builtin_count"] == 19
    assert all(r["evaluator_type"] == "Builtin" for r in builtin_rows)
    assert all(r["provider"] == "AWS" for r in builtin_rows)

    # Ground-truth is required only by the three session-level trajectory matchers.
    assert {
        r["id"] for r in builtin_rows if r.get("requires_ground_truth")
    } == set(TRAJECTORY_EVALUATORS)
    assert all(
        builtin_by_id[evaluator_id]["level"] == "TOOL_CALL"
        for evaluator_id in SKILL_EVALUATORS
    )

    # third-party rows carry provider + source
    for tp_id, provider in (
        ("ThirdParty.DeepEval.TaskCompletion", "DeepEval"),
        ("ThirdParty.AutoEval.Security", "AutoEval"),
    ):
        assert by_id[tp_id]["source"] == "third_party"
        assert by_id[tp_id]["evaluator_type"] == "ThirdParty"
        assert by_id[tp_id]["provider"] == provider

    # custom + derived rows stay source=custom with their own evaluator_type
    assert by_id["my_judge-abc123"]["source"] == "custom"
    assert by_id["my_judge-abc123"]["evaluator_type"] == "Custom"
    assert by_id["my_derived-def456"]["source"] == "custom"
    assert by_id["my_derived-def456"]["evaluator_type"] == "CustomDerived"
    assert by_id["my_derived-def456"]["provider"] == "DeepEval"


def test_thirdparty_id_prefix_classifies_without_evaluator_type(client, monkeypatch):
    stub = stub_control(monkeypatch)
    stub.list_evaluators.return_value = {
        "evaluators": [{"evaluatorId": "ThirdParty.DeepEval.Bias", "level": "TRACE"}]
    }
    res = client.get("/api/eval/evaluators")
    row = next(r for r in res.json()["evaluators"] if r["id"] == "ThirdParty.DeepEval.Bias")
    assert row["source"] == "third_party"


def test_catalog_fail_soft_builtins_only(client, monkeypatch):
    stub_control(monkeypatch, error=RuntimeError("no account access"))
    res = client.get("/api/eval/evaluators")
    assert res.status_code == 200
    body = res.json()
    rows = body["evaluators"]
    assert all(r["source"] == "builtin" for r in rows)
    assert body["builtin_count"] == len(rows) == 19
    assert {r["id"]: r["level"] for r in rows} == EXPECTED_BUILTIN_EVALUATORS
    context_relevance = next(r for r in rows if r["id"] == "Builtin.ContextRelevance")
    assert context_relevance["level"] == "TRACE"
