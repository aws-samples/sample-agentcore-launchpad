"""GET /api/eval/evaluators — catalog classification.

Builtins (incl. the skill evaluators) come from the local dicts; live
ListEvaluators rows are classified third_party vs custom and project their
evaluatorType/provider. The AWS call is fail-soft: builtins render without it.
"""

from unittest.mock import MagicMock

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

    # builtins appear exactly once (live Builtin.Helpfulness echo skipped)
    assert sum(1 for r in rows if r["id"] == "Builtin.Helpfulness") == 1
    helpful = by_id["Builtin.Helpfulness"]
    assert helpful["source"] == "builtin"
    assert helpful["evaluator_type"] == "Builtin"
    assert helpful["provider"] == "AWS"

    # skill builtins at TOOL_CALL level
    for skill_id in ("Builtin.SkillSelectionAccuracy", "Builtin.SkillInstructionFollowing"):
        assert by_id[skill_id]["level"] == "TOOL_CALL"
        assert by_id[skill_id]["source"] == "builtin"

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
    assert all(r["source"] == "builtin" for r in body["evaluators"])
    assert body["builtin_count"] == len(body["evaluators"])
    assert any(r["id"] == "Builtin.SkillSelectionAccuracy" for r in body["evaluators"])
