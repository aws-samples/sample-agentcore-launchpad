"""Custom evaluator CRUD — create with full params, get, update, delete.

Stubbed control client; asserts the boto3 payload shapes (llmAsAJudge with
numerical rating scale + bedrock model config).
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
        "instructions": "Rate the tone of {session}",
        "rating_scale": FIVE_POINT_SCALE,
        "model_id": "global.anthropic.claude-sonnet-4-6",
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
