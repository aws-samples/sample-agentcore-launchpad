"""Custom evaluator CRUD — create with full params, get, update, delete.

Stubbed control client; asserts the boto3 payload shapes for the three
definition kinds: llmAsAJudge (numerical rating scale + bedrock model config),
derived (base evaluator + model) and codeBased (Lambda ARN + timeout).
"""

from unittest.mock import MagicMock

FIVE_POINT_SCALE = [
    {"value": float(v), "label": f"level{v}", "definition": f"definition {v}"}
    for v in range(1, 6)
]

EVALUATOR_DETAIL = {
    "evaluatorId": "my_judge-abc123",
    "evaluatorName": "my_judge",
    "level": "SESSION",
    "description": "checks tone",
    "status": "ACTIVE",
    "evaluatorConfig": {
        "llmAsAJudge": {
            "instructions": "Rate the tone of {session}",
            "ratingScale": {"numerical": FIVE_POINT_SCALE},
            "modelConfig": {
                "bedrockEvaluatorModelConfig": {
                    "modelId": "global.anthropic.claude-sonnet-4-6"
                }
            },
        }
    },
}


def stub_control(monkeypatch):
    stub = MagicMock()
    stub.create_evaluator.return_value = {
        "evaluatorId": "my_judge-abc123",
        "evaluatorArn": "arn:aws:bedrock-agentcore:us-west-2:1:evaluator/my_judge-abc123",
    }
    stub.get_evaluator.return_value = EVALUATOR_DETAIL
    monkeypatch.setattr("app.evaluation.routers.control_client", lambda _ws=None: stub)
    return stub


def test_create_full_params_payload_shape(client, monkeypatch):
    stub = stub_control(monkeypatch)
    res = client.post("/api/eval/evaluators", json={
        "name": "my_judge",
        "instructions": "Rate the tone of {session}",
        "level": "SESSION",
        "description": "checks tone",
        "rating_scale": FIVE_POINT_SCALE,
    })
    assert res.status_code == 201
    assert res.json()["evaluator_id"] == "my_judge-abc123"

    kwargs = stub.create_evaluator.call_args.kwargs
    assert kwargs["evaluatorName"] == "my_judge"
    assert kwargs["level"] == "SESSION"
    assert kwargs["description"] == "checks tone"
    judge = kwargs["evaluatorConfig"]["llmAsAJudge"]
    assert judge["instructions"] == "Rate the tone of {session}"
    assert judge["ratingScale"]["numerical"] == FIVE_POINT_SCALE
    assert judge["modelConfig"]["bedrockEvaluatorModelConfig"]["modelId"] == (
        "global.anthropic.claude-sonnet-5"
    )
    assert kwargs["clientToken"]


def test_create_defaults_pass_fail_scale(client, monkeypatch):
    stub = stub_control(monkeypatch)
    res = client.post("/api/eval/evaluators", json={
        "name": "minimal_judge",
        "instructions": "Judge {assistant_turn} for helpfulness",
    })
    assert res.status_code == 201
    kwargs = stub.create_evaluator.call_args.kwargs
    assert kwargs["level"] == "TRACE"
    scale = kwargs["evaluatorConfig"]["llmAsAJudge"]["ratingScale"]["numerical"]
    assert {s["label"] for s in scale} == {"pass", "fail"}


def test_create_missing_placeholder_rejected(client, monkeypatch):
    stub = stub_control(monkeypatch)
    res = client.post("/api/eval/evaluators", json={
        "name": "no_placeholder",
        "instructions": "Judge the answer for helpfulness with no slot",
    })
    assert res.status_code == 422
    assert res.json()["code"] == "evaluator.missing_placeholder"
    stub.create_evaluator.assert_not_called()


def test_get_evaluator_output_mapping(client, monkeypatch):
    stub_control(monkeypatch)
    res = client.get("/api/eval/evaluators/my_judge-abc123")
    assert res.status_code == 200
    body = res.json()
    assert body == {
        "id": "my_judge-abc123",
        "name": "my_judge",
        "level": "SESSION",
        "description": "checks tone",
        "definition": "judge",
        "instructions": "Rate the tone of {session}",
        "rating_scale": FIVE_POINT_SCALE,
        "model_id": "global.anthropic.claude-sonnet-4-6",
        "base_evaluator_id": None,
        "lambda_arn": None,
        "lambda_timeout_s": None,
        "evaluator_type": None,
        "provider": None,
        "status": "ACTIVE",
    }


def test_update_full_config_payload_shape(client, monkeypatch):
    stub = stub_control(monkeypatch)
    res = client.put("/api/eval/evaluators/my_judge-abc123", json={
        "instructions": "Rate the revised tone of {session}",
        "level": "SESSION",
        "description": "checks tone v2",
        "rating_scale": FIVE_POINT_SCALE,
    })
    assert res.status_code == 200
    kwargs = stub.update_evaluator.call_args.kwargs
    assert kwargs["evaluatorId"] == "my_judge-abc123"
    assert kwargs["level"] == "SESSION"
    assert kwargs["description"] == "checks tone v2"
    judge = kwargs["evaluatorConfig"]["llmAsAJudge"]
    assert judge["instructions"] == "Rate the revised tone of {session}"
    assert judge["ratingScale"]["numerical"] == FIVE_POINT_SCALE
    assert judge["modelConfig"]["bedrockEvaluatorModelConfig"]["modelId"]
    # response is the refreshed GetEvaluator mapping
    assert res.json()["id"] == "my_judge-abc123"


def test_update_builtin_rejected(client, monkeypatch):
    stub = stub_control(monkeypatch)
    res = client.put("/api/eval/evaluators/Builtin.Correctness", json={
        "instructions": "Rewrite the builtin with {context}",
    })
    assert res.status_code == 400
    assert res.json()["code"] == "evaluator.builtin_immutable"
    stub.update_evaluator.assert_not_called()


def test_update_missing_placeholder_rejected(client, monkeypatch):
    stub = stub_control(monkeypatch)
    res = client.put("/api/eval/evaluators/my_judge-abc123", json={
        "instructions": "No placeholder present in these words",
    })
    assert res.status_code == 422
    assert res.json()["code"] == "evaluator.missing_placeholder"
    stub.update_evaluator.assert_not_called()


# ─── derived (CustomDerived) evaluators ──────────────────────────────────────
DERIVED_DETAIL = {
    "evaluatorId": "my_derived-def456",
    "evaluatorName": "my_derived",
    "level": "SESSION",
    "description": "task completion on sonnet",
    "status": "ACTIVE",
    "evaluatorType": "CustomDerived",
    "provider": "DeepEval",
    "evaluatorConfig": {
        "derived": {
            "baseEvaluatorId": "ThirdParty.DeepEval.TaskCompletion",
            "modelConfig": {
                "bedrockEvaluatorModelConfig": {
                    "modelId": "global.anthropic.claude-sonnet-5"
                }
            },
        }
    },
}


def test_derived_create_builtin_base_payload_shape(client, monkeypatch):
    stub = stub_control(monkeypatch)
    res = client.post("/api/eval/evaluators", json={
        "name": "my_derived",
        "base_evaluator_id": "Builtin.Helpfulness",
        "model_id": "global.anthropic.claude-haiku-4-5",
    })
    assert res.status_code == 201
    kwargs = stub.create_evaluator.call_args.kwargs
    assert kwargs["evaluatorName"] == "my_derived"
    assert kwargs["level"] == "TRACE"  # resolved locally from the builtin base
    assert kwargs["evaluatorConfig"] == {
        "derived": {
            "baseEvaluatorId": "Builtin.Helpfulness",
            "modelConfig": {
                "bedrockEvaluatorModelConfig": {
                    "modelId": "global.anthropic.claude-haiku-4-5"
                }
            },
        }
    }
    assert kwargs["clientToken"]
    stub.get_evaluator.assert_not_called()  # builtin level needs no AWS lookup


def test_derived_create_thirdparty_base_resolves_level_via_get(client, monkeypatch):
    stub = stub_control(monkeypatch)
    stub.get_evaluator.return_value = {
        "evaluatorId": "ThirdParty.DeepEval.TaskCompletion",
        "level": "SESSION",
    }
    res = client.post("/api/eval/evaluators", json={
        "name": "my_derived",
        "base_evaluator_id": "ThirdParty.DeepEval.TaskCompletion",
    })
    assert res.status_code == 201
    assert stub.get_evaluator.call_args.kwargs["evaluatorId"] == (
        "ThirdParty.DeepEval.TaskCompletion"
    )
    kwargs = stub.create_evaluator.call_args.kwargs
    assert kwargs["level"] == "SESSION"
    derived = kwargs["evaluatorConfig"]["derived"]
    assert derived["baseEvaluatorId"] == "ThirdParty.DeepEval.TaskCompletion"


def test_derived_create_unknown_base_rejected(client, monkeypatch):
    stub = stub_control(monkeypatch)
    stub.get_evaluator.side_effect = RuntimeError("ResourceNotFoundException")
    res = client.post("/api/eval/evaluators", json={
        "name": "my_derived",
        "base_evaluator_id": "ThirdParty.DeepEval.NoSuchMetric",
    })
    assert res.status_code == 400
    assert res.json()["code"] == "evaluator.base_not_found"
    stub.create_evaluator.assert_not_called()


def test_create_both_definitions_rejected(client, monkeypatch):
    stub = stub_control(monkeypatch)
    res = client.post("/api/eval/evaluators", json={
        "name": "my_derived",
        "instructions": "Rate the tone of {session}",
        "base_evaluator_id": "Builtin.Helpfulness",
    })
    assert res.status_code == 400
    assert res.json()["code"] == "evaluator.definition_ambiguous"
    stub.create_evaluator.assert_not_called()


def test_create_neither_definition_rejected(client, monkeypatch):
    stub = stub_control(monkeypatch)
    res = client.post("/api/eval/evaluators", json={"name": "my_derived"})
    assert res.status_code == 400
    assert res.json()["code"] == "evaluator.definition_ambiguous"
    stub.create_evaluator.assert_not_called()


def test_derived_create_rating_scale_rejected(client, monkeypatch):
    stub = stub_control(monkeypatch)
    res = client.post("/api/eval/evaluators", json={
        "name": "my_derived",
        "base_evaluator_id": "Builtin.Helpfulness",
        "rating_scale": FIVE_POINT_SCALE,
    })
    assert res.status_code == 400
    assert res.json()["code"] == "evaluator.rating_scale_not_allowed"
    stub.create_evaluator.assert_not_called()


def test_get_derived_output_mapping(client, monkeypatch):
    stub = stub_control(monkeypatch)
    stub.get_evaluator.return_value = DERIVED_DETAIL
    res = client.get("/api/eval/evaluators/my_derived-def456")
    assert res.status_code == 200
    assert res.json() == {
        "id": "my_derived-def456",
        "name": "my_derived",
        "level": "SESSION",
        "description": "task completion on sonnet",
        "definition": "derived",
        "instructions": "",
        "rating_scale": [],
        "model_id": "global.anthropic.claude-sonnet-5",
        "base_evaluator_id": "ThirdParty.DeepEval.TaskCompletion",
        "lambda_arn": None,
        "lambda_timeout_s": None,
        "evaluator_type": "CustomDerived",
        "provider": "DeepEval",
        "status": "ACTIVE",
    }


def test_derived_update_payload_shape(client, monkeypatch):
    stub = stub_control(monkeypatch)
    stub.get_evaluator.side_effect = lambda evaluatorId: {
        "my_derived-def456": DERIVED_DETAIL,
        "ThirdParty.DeepEval.TaskCompletion": {"level": "SESSION"},
    }[evaluatorId]
    res = client.put("/api/eval/evaluators/my_derived-def456", json={
        "base_evaluator_id": "ThirdParty.DeepEval.TaskCompletion",
        "model_id": "global.anthropic.claude-haiku-4-5",
        "description": "swapped to haiku",
    })
    assert res.status_code == 200
    kwargs = stub.update_evaluator.call_args.kwargs
    assert kwargs["evaluatorId"] == "my_derived-def456"
    assert kwargs["level"] == "SESSION"
    assert kwargs["description"] == "swapped to haiku"
    assert kwargs["evaluatorConfig"] == {
        "derived": {
            "baseEvaluatorId": "ThirdParty.DeepEval.TaskCompletion",
            "modelConfig": {
                "bedrockEvaluatorModelConfig": {
                    "modelId": "global.anthropic.claude-haiku-4-5"
                }
            },
        }
    }
    assert res.json()["base_evaluator_id"] == "ThirdParty.DeepEval.TaskCompletion"


def test_update_definition_mismatch_rejected(client, monkeypatch):
    stub = stub_control(monkeypatch)
    # instructions payload against a derived evaluator
    stub.get_evaluator.return_value = DERIVED_DETAIL
    res = client.put("/api/eval/evaluators/my_derived-def456", json={
        "instructions": "Rate the tone of {session}",
    })
    assert res.status_code == 400
    assert res.json()["code"] == "evaluator.definition_mismatch"
    # base_evaluator_id payload against an llmAsAJudge evaluator
    stub.get_evaluator.return_value = EVALUATOR_DETAIL
    res = client.put("/api/eval/evaluators/my_judge-abc123", json={
        "base_evaluator_id": "Builtin.Helpfulness",
    })
    assert res.status_code == 400
    assert res.json()["code"] == "evaluator.definition_mismatch"
    stub.update_evaluator.assert_not_called()


def test_update_thirdparty_rejected(client, monkeypatch):
    stub = stub_control(monkeypatch)
    res = client.put("/api/eval/evaluators/ThirdParty.DeepEval.TaskCompletion", json={
        "instructions": "Rewrite the managed prompt with {context}",
    })
    assert res.status_code == 400
    assert res.json()["code"] == "evaluator.builtin_immutable"
    stub.update_evaluator.assert_not_called()


def test_delete_managed_rejected(client, monkeypatch):
    stub = stub_control(monkeypatch)
    for evaluator_id in ("ThirdParty.AutoEval.Security", "Builtin.Correctness"):
        res = client.delete(f"/api/eval/evaluators/{evaluator_id}")
        assert res.status_code == 400
        assert res.json()["code"] == "evaluator.builtin_immutable"
    stub.delete_evaluator.assert_not_called()


# ─── code-based (Lambda) evaluators ──────────────────────────────────────────
LAMBDA_ARN = "arn:aws:lambda:us-west-2:111122223333:function:se019-eval-probe"
CODE_DETAIL = {
    "evaluatorId": "se019_probe-ghi789",
    "evaluatorName": "se019_probe",
    "level": "TRACE",
    "description": "lambda scorer",
    "status": "ACTIVE",
    "evaluatorType": "CustomCode",
    "evaluatorConfig": {
        "codeBased": {
            "lambdaConfig": {"lambdaArn": LAMBDA_ARN, "lambdaTimeoutInSeconds": 45}
        }
    },
}


def test_code_create_payload_shape_default_timeout(client, monkeypatch):
    stub = stub_control(monkeypatch)
    res = client.post("/api/eval/evaluators", json={
        "name": "se019_probe",
        "lambda_arn": LAMBDA_ARN,
        "level": "SESSION",
        "description": "lambda scorer",
    })
    assert res.status_code == 201
    kwargs = stub.create_evaluator.call_args.kwargs
    assert kwargs["evaluatorName"] == "se019_probe"
    assert kwargs["level"] == "SESSION"
    assert kwargs["description"] == "lambda scorer"
    assert kwargs["evaluatorConfig"] == {
        "codeBased": {
            "lambdaConfig": {"lambdaArn": LAMBDA_ARN, "lambdaTimeoutInSeconds": 60}
        }
    }
    assert kwargs["clientToken"]
    stub.get_evaluator.assert_not_called()  # no base to resolve, no AWS lookup


def test_code_create_explicit_timeout_and_qualified_arn(client, monkeypatch):
    stub = stub_control(monkeypatch)
    res = client.post("/api/eval/evaluators", json={
        "name": "se019_probe",
        "lambda_arn": LAMBDA_ARN + ":live",
        "lambda_timeout_s": 300,
    })
    assert res.status_code == 201
    kwargs = stub.create_evaluator.call_args.kwargs
    assert kwargs["level"] == "TRACE"  # JudgeCreate default, not resolved from anywhere
    lambda_config = kwargs["evaluatorConfig"]["codeBased"]["lambdaConfig"]
    assert lambda_config == {"lambdaArn": LAMBDA_ARN + ":live", "lambdaTimeoutInSeconds": 300}


def test_code_create_timeout_out_of_range_rejected(client, monkeypatch):
    stub = stub_control(monkeypatch)
    for timeout in (0, 301):
        res = client.post("/api/eval/evaluators", json={
            "name": "se019_probe",
            "lambda_arn": LAMBDA_ARN,
            "lambda_timeout_s": timeout,
        })
        assert res.status_code == 422, timeout
    stub.create_evaluator.assert_not_called()


def test_code_create_malformed_arn_rejected(client, monkeypatch):
    stub = stub_control(monkeypatch)
    for arn in (
        "arn:aws:lambda:us-west-2:111122223333:layer:not-a-function",
        "arn:aws:iam::111122223333:role/x",
        "se019-eval-probe",
    ):
        res = client.post("/api/eval/evaluators", json={"name": "p", "lambda_arn": arn})
        assert res.status_code == 422, arn
    stub.create_evaluator.assert_not_called()


def test_code_create_region_mismatch_rejected(client, monkeypatch):
    stub = stub_control(monkeypatch)
    # the default workspace row is us-west-2 (conftest)
    res = client.post("/api/eval/evaluators", json={
        "name": "se019_probe",
        "lambda_arn": "arn:aws:lambda:us-east-1:111122223333:function:se019-eval-probe",
    })
    assert res.status_code == 422
    body = res.json()
    assert body["code"] == "evaluator.lambda_region_mismatch"
    assert body["detail"] == {"lambda_region": "us-east-1", "workspace_region": "us-west-2"}
    stub.create_evaluator.assert_not_called()


def test_code_create_with_instructions_ambiguous(client, monkeypatch):
    stub = stub_control(monkeypatch)
    res = client.post("/api/eval/evaluators", json={
        "name": "se019_probe",
        "lambda_arn": LAMBDA_ARN,
        "instructions": "Rate the tone of {session}",
    })
    assert res.status_code == 400
    assert res.json()["code"] == "evaluator.definition_ambiguous"
    res = client.post("/api/eval/evaluators", json={
        "name": "se019_probe",
        "lambda_arn": LAMBDA_ARN,
        "base_evaluator_id": "Builtin.Helpfulness",
    })
    assert res.status_code == 400
    assert res.json()["code"] == "evaluator.definition_ambiguous"
    stub.create_evaluator.assert_not_called()


def test_code_create_rating_scale_rejected(client, monkeypatch):
    stub = stub_control(monkeypatch)
    res = client.post("/api/eval/evaluators", json={
        "name": "se019_probe",
        "lambda_arn": LAMBDA_ARN,
        "rating_scale": FIVE_POINT_SCALE,
    })
    assert res.status_code == 400
    assert res.json()["code"] == "evaluator.rating_scale_not_allowed"
    stub.create_evaluator.assert_not_called()


def test_get_code_output_mapping(client, monkeypatch):
    stub = stub_control(monkeypatch)
    stub.get_evaluator.return_value = CODE_DETAIL
    res = client.get("/api/eval/evaluators/se019_probe-ghi789")
    assert res.status_code == 200
    assert res.json() == {
        "id": "se019_probe-ghi789",
        "name": "se019_probe",
        "level": "TRACE",
        "description": "lambda scorer",
        "definition": "code",
        "instructions": "",
        "rating_scale": [],
        "model_id": None,
        "base_evaluator_id": None,
        "lambda_arn": LAMBDA_ARN,
        "lambda_timeout_s": 45,
        "evaluator_type": "CustomCode",
        "provider": None,
        "status": "ACTIVE",
    }


def test_list_marks_code_rows(client, monkeypatch):
    stub = stub_control(monkeypatch)
    stub.list_evaluators.return_value = {
        "evaluators": [
            {"evaluatorId": "se019_probe-ghi789", "evaluatorType": "CustomCode"},
            {"evaluatorId": "my_derived-def456", "evaluatorType": "CustomDerived"},
            {"evaluatorId": "my_judge-abc123", "evaluatorType": "Custom"},
            {"evaluatorId": "ThirdParty.DeepEval.TaskCompletion", "evaluatorType": "ThirdParty"},
        ]
    }
    res = client.get("/api/eval/evaluators")
    assert res.status_code == 200
    by_id = {r["id"]: r for r in res.json()["evaluators"] if r["source"] != "builtin"}
    assert by_id["se019_probe-ghi789"]["definition"] == "code"
    assert by_id["my_derived-def456"]["definition"] == "derived"
    assert by_id["my_judge-abc123"]["definition"] == "judge"
    assert by_id["ThirdParty.DeepEval.TaskCompletion"]["definition"] is None


def test_code_update_payload_shape(client, monkeypatch):
    stub = stub_control(monkeypatch)
    stub.get_evaluator.return_value = CODE_DETAIL
    res = client.put("/api/eval/evaluators/se019_probe-ghi789", json={
        "lambda_arn": LAMBDA_ARN,
        "lambda_timeout_s": 120,
        "level": "SESSION",
        "description": "lambda scorer v2",
    })
    assert res.status_code == 200
    kwargs = stub.update_evaluator.call_args.kwargs
    assert kwargs["evaluatorId"] == "se019_probe-ghi789"
    assert kwargs["level"] == "SESSION"
    assert kwargs["description"] == "lambda scorer v2"
    assert kwargs["evaluatorConfig"] == {
        "codeBased": {
            "lambdaConfig": {"lambdaArn": LAMBDA_ARN, "lambdaTimeoutInSeconds": 120}
        }
    }
    assert kwargs["clientToken"]
    assert res.json()["definition"] == "code"


def test_code_update_region_mismatch_rejected(client, monkeypatch):
    stub = stub_control(monkeypatch)
    stub.get_evaluator.return_value = CODE_DETAIL
    res = client.put("/api/eval/evaluators/se019_probe-ghi789", json={
        "lambda_arn": "arn:aws:lambda:eu-west-1:111122223333:function:elsewhere",
    })
    assert res.status_code == 422
    assert res.json()["code"] == "evaluator.lambda_region_mismatch"
    stub.update_evaluator.assert_not_called()


def test_update_code_definition_mismatch_rejected(client, monkeypatch):
    stub = stub_control(monkeypatch)
    # judge payload against a code-based evaluator — would convert it
    stub.get_evaluator.return_value = CODE_DETAIL
    res = client.put("/api/eval/evaluators/se019_probe-ghi789", json={
        "instructions": "Rate the tone of {session}",
    })
    assert res.status_code == 400
    assert res.json()["code"] == "evaluator.definition_mismatch"
    assert res.json()["detail"] == {"current": "code", "payload": "judge"}
    # derived payload against a code-based evaluator
    res = client.put("/api/eval/evaluators/se019_probe-ghi789", json={
        "base_evaluator_id": "Builtin.Helpfulness",
    })
    assert res.status_code == 400
    assert res.json()["code"] == "evaluator.definition_mismatch"
    # code payload against a judge and against a derived evaluator
    for current in (EVALUATOR_DETAIL, DERIVED_DETAIL):
        stub.get_evaluator.return_value = current
        res = client.put(f"/api/eval/evaluators/{current['evaluatorId']}", json={
            "lambda_arn": LAMBDA_ARN,
        })
        assert res.status_code == 400
        assert res.json()["code"] == "evaluator.definition_mismatch"
    stub.update_evaluator.assert_not_called()
