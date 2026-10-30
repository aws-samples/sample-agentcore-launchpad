"""Custom evaluator CRUD — create with full params, get, update, delete (N12 E1-1).

The Global baseline asserted the boto3 ``CreateEvaluator`` / ``UpdateEvaluator`` payloads;
in CN the evaluator *is* the ``eval_evaluators`` row (CA-3), so the same requests are
asserted against the row and the frozen response keys. Status codes, error codes and the
``_evaluator_out`` mapping are the baseline's.
"""

import pytest

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.evaluation.models import EvalEvaluator

FIVE_POINT_SCALE = [
    {"value": float(v), "label": f"level{v}", "definition": f"definition {v}"}
    for v in range(1, 6)
]
LAMBDA_ARN = "arn:aws-cn:lambda:cn-northwest-1:111122223333:function:se019-eval-probe"


def _row(evaluator_id: str) -> EvalEvaluator | None:
    db = SessionLocal()
    try:
        row = db.get(EvalEvaluator, evaluator_id)
        if row is not None:
            db.refresh(row)
            db.expunge(row)
        return row
    finally:
        db.close()


def _create_judge(client, **overrides):
    body = {
        "name": "my_judge",
        "instructions": "Rate the tone of {context}",
        "level": "SESSION",
        "description": "checks tone",
        "rating_scale": FIVE_POINT_SCALE,
    }
    body.update(overrides)
    return client.post("/api/eval/evaluators", json=body)


# ─── LLM-as-a-judge ───────────────────────────────────────────────────────────
def test_create_full_params_writes_the_row(client):
    res = _create_judge(client)
    assert res.status_code == 201
    body = res.json()
    assert set(body) == {"evaluator_id", "arn", "model_fallback", "created_by", "created_by_name"}
    assert body["created_by"] is None and body["created_by_name"] == get_settings().auth_username
    assert body["evaluator_id"].startswith("cust_")
    assert body["arn"].startswith("arn:aws-cn:bedrock-agentcore:") and body["arn"].endswith(
        f"evaluator/{body['evaluator_id']}"
    )
    assert body["model_fallback"] is None  # no judge-model probe in CN
    row = _row(body["evaluator_id"])
    assert row.name == "my_judge" and row.level == "SESSION" and row.description == "checks tone"
    assert row.definition == "judge"
    assert row.instructions == "Rate the tone of {context}"
    assert row.rating_scale == FIVE_POINT_SCALE
    assert row.model_id == "global.zai.glm-5.3"
    assert row.workspace_id == "default" and row.status == "ACTIVE"


def test_create_defaults_pass_fail_scale(client):
    res = client.post("/api/eval/evaluators", json={
        "name": "minimal_judge",
        "instructions": "Judge {assistant_turn} for helpfulness",
    })
    assert res.status_code == 201
    row = _row(res.json()["evaluator_id"])
    assert row.level == "TRACE"
    assert {s["label"] for s in row.rating_scale} == {"pass", "fail"}


def test_create_missing_placeholder_rejected(client):
    res = client.post("/api/eval/evaluators", json={
        "name": "no_placeholder",
        "instructions": "Judge the answer for helpfulness with no slot",
    })
    assert res.status_code == 422
    assert res.json()["code"] == "evaluator.missing_placeholder"


def test_create_refuses_a_model_outside_the_catalog(client):
    """Spec C3: a judge runs only on the CN catalog models — no silent fallback."""
    res = _create_judge(client, model_id="global.anthropic.claude-sonnet-4-6")
    assert res.status_code == 422
    assert res.json()["code"] == "model.not_allowed"
    assert _rows_named("my_judge") == []


@pytest.mark.siliconflow
def test_create_refuses_a_judge_model_whose_provider_is_not_configured(client, monkeypatch):
    """N-R6-obs1: a catalog model without its provider's key is refused at the judge
    definition — 422 `model.provider_unconfigured` naming the SSM parameter."""
    sf = "siliconflow.zai-org/GLM-5.3"
    monkeypatch.delenv("LAUNCHPAD_SILICONFLOW_API_KEY", raising=False)
    res = _create_judge(client, model_id=sf)
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "model.provider_unconfigured"
    assert "launchpad-siliconflow-api-key" in res.json()["message"]
    assert _rows_named("my_judge") == []
    monkeypatch.setenv("LAUNCHPAD_SILICONFLOW_API_KEY", "k")
    res = _create_judge(client, model_id=sf)
    assert res.status_code == 201, res.text
    evaluator_id = res.json()["evaluator_id"]
    # the update path is held to the same guard (`update_judge` → `_require_model`)
    monkeypatch.delenv("LAUNCHPAD_SILICONFLOW_API_KEY")
    res = client.put(f"/api/eval/evaluators/{evaluator_id}", json={
        "instructions": "Rate the tone of {context} again", "level": "SESSION",
        "description": "checks tone", "rating_scale": FIVE_POINT_SCALE, "model_id": sf})
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "model.provider_unconfigured"
    assert _row(evaluator_id).instructions == "Rate the tone of {context}"  # unchanged


def test_create_accepts_the_alternative_catalog_model(client):
    res = _create_judge(client, model_id="global.moonshotai.kimi-k3")
    assert res.status_code == 201
    assert _row(res.json()["evaluator_id"]).model_id == "global.moonshotai.kimi-k3"


def _rows_named(name: str) -> list[EvalEvaluator]:
    db = SessionLocal()
    try:
        return db.query(EvalEvaluator).filter(EvalEvaluator.name == name).all()
    finally:
        db.close()


def test_create_duplicate_name_is_409(client):
    assert _create_judge(client).status_code == 201
    res = _create_judge(client)
    assert res.status_code == 409
    assert res.json()["code"] == "evaluator.name_taken"


def test_get_evaluator_output_mapping(client):
    evaluator_id = _create_judge(client).json()["evaluator_id"]
    res = client.get(f"/api/eval/evaluators/{evaluator_id}")
    assert res.status_code == 200
    assert res.json() == {
        "id": evaluator_id,
        "name": "my_judge",
        "level": "SESSION",
        "description": "checks tone",
        "definition": "judge",
        "instructions": "Rate the tone of {context}",
        "rating_scale": FIVE_POINT_SCALE,
        "model_id": "global.zai.glm-5.3",
        "base_evaluator_id": None,
        "lambda_arn": None,
        "lambda_timeout_s": None,
        "evaluator_type": "Custom",
        "provider": "Custom",
        "status": "ACTIVE",
        # P7.1 (additive): version 1 at create, its definition hash
        "version": 1, "definition_hash": res.json()["definition_hash"],
        "pass_threshold": 0.7,  # N17-obs425
        "requires_ground_truth": False, "ground_truth_fields": [],  # N17-obs437
        "created_by": None,
        "created_by_name": get_settings().auth_username,
    }


def test_get_builtin_renders_the_catalog_entry(client):
    res = client.get("/api/eval/evaluators/Builtin.Helpfulness")
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == "Builtin.Helpfulness" and body["level"] == "TRACE"
    assert body["evaluator_type"] == "Builtin" and body["provider"] == "AWS"
    assert body["definition"] == "judge" and body["status"] == "ACTIVE"


def test_get_unknown_evaluator_is_404(client):
    res = client.get("/api/eval/evaluators/cust_doesnotexist")
    assert res.status_code == 404
    assert res.json()["code"] == "evaluator.not_found"


def test_get_evaluator_of_another_workspace_is_404(client):
    evaluator_id = _create_judge(client).json()["evaluator_id"]
    db = SessionLocal()
    try:
        db.get(EvalEvaluator, evaluator_id).workspace_id = "other"
        db.commit()
    finally:
        db.close()
    assert client.get(f"/api/eval/evaluators/{evaluator_id}").status_code == 404


def test_update_full_config_rewrites_the_row(client):
    evaluator_id = _create_judge(client).json()["evaluator_id"]
    res = client.put(f"/api/eval/evaluators/{evaluator_id}", json={
        "instructions": "Rate the revised tone of {context}",
        "level": "SESSION",
        "description": "checks tone v2",
        "rating_scale": FIVE_POINT_SCALE,
        "model_id": "global.moonshotai.kimi-k3",
    })
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == evaluator_id and body["model_fallback"] is None
    assert body["instructions"] == "Rate the revised tone of {context}"
    row = _row(evaluator_id)
    assert row.description == "checks tone v2" and row.model_id == "global.moonshotai.kimi-k3"
    assert row.rating_scale == FIVE_POINT_SCALE


def test_update_builtin_rejected(client):
    res = client.put("/api/eval/evaluators/Builtin.Correctness", json={
        "instructions": "Rewrite the builtin with {context}",
    })
    assert res.status_code == 400
    assert res.json()["code"] == "evaluator.builtin_immutable"


def test_update_missing_placeholder_rejected(client):
    evaluator_id = _create_judge(client).json()["evaluator_id"]
    res = client.put(f"/api/eval/evaluators/{evaluator_id}", json={
        "instructions": "No placeholder present in these words",
    })
    assert res.status_code == 422
    assert res.json()["code"] == "evaluator.missing_placeholder"
    assert _row(evaluator_id).instructions == "Rate the tone of {context}"


def test_update_unknown_is_404(client):
    res = client.put("/api/eval/evaluators/cust_missing", json={
        "instructions": "Rate the tone of {context}",
    })
    assert res.status_code == 404
    assert res.json()["code"] == "evaluator.not_found"


# ─── derived (CustomDerived) evaluators ──────────────────────────────────────
def _create_derived(client, **overrides):
    body = {"name": "my_derived", "base_evaluator_id": "Builtin.Helpfulness"}
    body.update(overrides)
    return client.post("/api/eval/evaluators", json=body)


def test_derived_create_builtin_base_resolves_level_locally(client):
    res = _create_derived(client, model_id="global.moonshotai.kimi-k3")
    assert res.status_code == 201
    row = _row(res.json()["evaluator_id"])
    assert row.definition == "derived"
    assert row.level == "TRACE"  # the base's level, resolved from the local catalog
    assert row.base_evaluator_id == "Builtin.Helpfulness"
    assert row.model_id == "global.moonshotai.kimi-k3"
    assert row.instructions is None and row.rating_scale == []


def test_derived_create_session_base_takes_session_level(client):
    res = _create_derived(client, base_evaluator_id="Builtin.GoalSuccessRate")
    assert res.status_code == 201
    assert _row(res.json()["evaluator_id"]).level == "SESSION"


def test_derived_create_unknown_base_rejected(client):
    """Third-party managed evaluators do not exist in CN — any non-catalog base is unknown."""
    for base in ("ThirdParty.DeepEval.TaskCompletion", "Builtin.NoSuchJudge",
                 "Builtin.TrajectoryInOrderMatch"):
        res = _create_derived(client, base_evaluator_id=base)
        assert res.status_code == 400, base
        assert res.json()["code"] == "evaluator.base_not_found"
    assert _rows_named("my_derived") == []


def test_create_both_definitions_rejected(client):
    res = client.post("/api/eval/evaluators", json={
        "name": "my_derived",
        "instructions": "Rate the tone of {context}",
        "base_evaluator_id": "Builtin.Helpfulness",
    })
    assert res.status_code == 400
    assert res.json()["code"] == "evaluator.definition_ambiguous"


def test_create_neither_definition_rejected(client):
    res = client.post("/api/eval/evaluators", json={"name": "my_derived"})
    assert res.status_code == 400
    assert res.json()["code"] == "evaluator.definition_ambiguous"


def test_derived_create_rating_scale_rejected(client):
    res = _create_derived(client, rating_scale=FIVE_POINT_SCALE)
    assert res.status_code == 400
    assert res.json()["code"] == "evaluator.rating_scale_not_allowed"


def test_get_derived_output_mapping(client):
    evaluator_id = _create_derived(client, description="helpfulness on kimi",
                                   model_id="global.moonshotai.kimi-k3").json()["evaluator_id"]
    res = client.get(f"/api/eval/evaluators/{evaluator_id}")
    assert res.status_code == 200
    assert res.json() == {
        "id": evaluator_id,
        "name": "my_derived",
        "level": "TRACE",
        "description": "helpfulness on kimi",
        "definition": "derived",
        "instructions": "",
        "rating_scale": [],
        "model_id": "global.moonshotai.kimi-k3",
        "base_evaluator_id": "Builtin.Helpfulness",
        "lambda_arn": None,
        "lambda_timeout_s": None,
        "evaluator_type": "CustomDerived",
        "provider": "AWS",
        "status": "ACTIVE",
        # P7.1 (additive): version 1 at create, its definition hash
        "version": 1, "definition_hash": res.json()["definition_hash"],
        "pass_threshold": 0.7,  # N17-obs425
        "requires_ground_truth": False, "ground_truth_fields": [],  # N17-obs437
        "created_by": None,
        "created_by_name": get_settings().auth_username,
    }


def test_derived_update_rewrites_base_and_level(client):
    evaluator_id = _create_derived(client).json()["evaluator_id"]
    res = client.put(f"/api/eval/evaluators/{evaluator_id}", json={
        "base_evaluator_id": "Builtin.GoalSuccessRate",
        "model_id": "global.moonshotai.kimi-k3",
        "description": "swapped to kimi",
    })
    assert res.status_code == 200
    assert res.json()["base_evaluator_id"] == "Builtin.GoalSuccessRate"
    row = _row(evaluator_id)
    assert row.level == "SESSION" and row.description == "swapped to kimi"
    assert row.model_id == "global.moonshotai.kimi-k3"


def test_update_definition_mismatch_rejected(client):
    derived_id = _create_derived(client).json()["evaluator_id"]
    judge_id = _create_judge(client).json()["evaluator_id"]
    # instructions payload against a derived evaluator
    res = client.put(f"/api/eval/evaluators/{derived_id}", json={
        "instructions": "Rate the tone of {context}",
    })
    assert res.status_code == 400
    assert res.json()["code"] == "evaluator.definition_mismatch"
    assert res.json()["detail"] == {"current": "derived", "payload": "judge"}
    # base_evaluator_id payload against an llmAsAJudge evaluator
    res = client.put(f"/api/eval/evaluators/{judge_id}", json={
        "base_evaluator_id": "Builtin.Helpfulness",
    })
    assert res.status_code == 400
    assert res.json()["code"] == "evaluator.definition_mismatch"
    assert _row(judge_id).definition == "judge" and _row(derived_id).definition == "derived"


def test_update_thirdparty_rejected(client):
    res = client.put("/api/eval/evaluators/ThirdParty.DeepEval.TaskCompletion", json={
        "instructions": "Rewrite the managed prompt with {context}",
    })
    assert res.status_code == 400
    assert res.json()["code"] == "evaluator.builtin_immutable"


def test_delete_managed_rejected(client):
    for evaluator_id in ("ThirdParty.AutoEval.Security", "Builtin.Correctness"):
        res = client.delete(f"/api/eval/evaluators/{evaluator_id}")
        assert res.status_code == 400
        assert res.json()["code"] == "evaluator.builtin_immutable"


def test_delete_removes_the_row(client):
    evaluator_id = _create_judge(client).json()["evaluator_id"]
    res = client.delete(f"/api/eval/evaluators/{evaluator_id}")
    assert res.status_code == 200 and res.json() == {"deleted": True}
    assert _row(evaluator_id) is None
    assert client.delete(f"/api/eval/evaluators/{evaluator_id}").status_code == 404


# ─── code-based (Lambda) evaluators ──────────────────────────────────────────
@pytest.fixture
def code_evaluators_on(monkeypatch):
    """The workspace opted in (Settings.eval_code_evaluators_enabled) — the Lambda
    definition's CRUD below is exercised with the flag on; the default (off) is refused
    (N12-obs41, test_code_definition_refused_while_the_flag_is_off)."""
    monkeypatch.setattr(get_settings(), "eval_code_evaluators_enabled", True)


def test_code_definition_refused_while_the_flag_is_off(client, monkeypatch):
    """N12-obs41: `eval_code_evaluators_enabled` defaults to false — creating or editing a
    Lambda code evaluator is refused before any row is written (403), the other two
    definitions are unaffected."""
    assert get_settings().eval_code_evaluators_enabled is False
    res = _create_code(client)
    assert res.status_code == 403
    body = res.json()
    assert body["code"] == "evaluation.code_evaluators_disabled"
    assert "eval_code_evaluators_enabled" in body["message"]
    assert body["detail"] == {"setting": "eval_code_evaluators_enabled"}
    assert _rows_named("se019_probe") == []
    # the update path is gated the same way: a row created while the flag was on cannot
    # be re-pointed once the workspace switched it off
    monkeypatch.setattr(get_settings(), "eval_code_evaluators_enabled", True)
    code_id = _create_code(client).json()["evaluator_id"]
    monkeypatch.setattr(get_settings(), "eval_code_evaluators_enabled", False)
    res = client.put(f"/api/eval/evaluators/{code_id}", json={"lambda_arn": LAMBDA_ARN + ":v2"})
    assert res.status_code == 403 and res.json()["code"] == "evaluation.code_evaluators_disabled"
    assert _row(code_id).lambda_arn == LAMBDA_ARN
    # judge / derived keep working
    assert _create_judge(client).status_code == 201
    assert _create_derived(client).status_code == 201


def _create_code(client, **overrides):
    body = {"name": "se019_probe", "lambda_arn": LAMBDA_ARN, "level": "SESSION",
            "description": "lambda scorer"}
    body.update(overrides)
    body = {k: v for k, v in body.items() if v is not None}
    return client.post("/api/eval/evaluators", json=body)


def test_code_create_default_timeout(client, code_evaluators_on):
    res = _create_code(client)
    assert res.status_code == 201
    row = _row(res.json()["evaluator_id"])
    assert row.definition == "code" and row.level == "SESSION"
    assert row.lambda_arn == LAMBDA_ARN and row.lambda_timeout_s == 60
    assert row.model_id is None and row.instructions is None


def test_code_create_explicit_timeout_and_qualified_arn(client, code_evaluators_on):
    res = _create_code(client, lambda_arn=LAMBDA_ARN + ":live", lambda_timeout_s=300,
                       level=None)
    assert res.status_code == 201
    row = _row(res.json()["evaluator_id"])
    assert row.level == "TRACE"  # JudgeCreate default, not resolved from anywhere
    assert row.lambda_arn == LAMBDA_ARN + ":live" and row.lambda_timeout_s == 300


def test_code_create_timeout_out_of_range_rejected(client, code_evaluators_on):
    for timeout in (0, 301):
        res = _create_code(client, lambda_timeout_s=timeout)
        assert res.status_code == 422, timeout
    assert _rows_named("se019_probe") == []


def test_code_create_malformed_arn_rejected(client, code_evaluators_on):
    for arn in (
        "arn:aws-cn:lambda:cn-northwest-1:111122223333:layer:not-a-function",
        "arn:aws-cn:iam::111122223333:role/x",
        "se019-eval-probe",
    ):
        res = client.post("/api/eval/evaluators", json={"name": "p", "lambda_arn": arn})
        assert res.status_code == 422, arn


def test_code_create_region_mismatch_rejected(client, code_evaluators_on):
    # the default workspace row is cn-northwest-1 (Settings.region)
    res = _create_code(
        client,
        lambda_arn="arn:aws-cn:lambda:cn-north-1:111122223333:function:se019-eval-probe",
    )
    assert res.status_code == 422
    body = res.json()
    assert body["code"] == "evaluator.lambda_region_mismatch"
    assert body["detail"] == {"lambda_region": "cn-north-1", "workspace_region": "cn-northwest-1"}


def test_code_create_with_instructions_ambiguous(client, code_evaluators_on):
    res = _create_code(client, instructions="Rate the tone of {context}")
    assert res.status_code == 400
    assert res.json()["code"] == "evaluator.definition_ambiguous"
    res = _create_code(client, base_evaluator_id="Builtin.Helpfulness")
    assert res.status_code == 400
    assert res.json()["code"] == "evaluator.definition_ambiguous"


def test_code_create_rating_scale_rejected(client, code_evaluators_on):
    res = _create_code(client, rating_scale=FIVE_POINT_SCALE)
    assert res.status_code == 400
    assert res.json()["code"] == "evaluator.rating_scale_not_allowed"


def test_get_code_output_mapping(client, code_evaluators_on):
    evaluator_id = _create_code(client, level="TRACE", lambda_timeout_s=45).json()["evaluator_id"]
    res = client.get(f"/api/eval/evaluators/{evaluator_id}")
    assert res.status_code == 200
    assert res.json() == {
        "id": evaluator_id,
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
        "provider": "Custom",
        "status": "ACTIVE",
        # P7.1 (additive): version 1 at create, its definition hash
        "version": 1, "definition_hash": res.json()["definition_hash"],
        "pass_threshold": 0.7,  # N17-obs425
        "requires_ground_truth": False, "ground_truth_fields": [],  # N17-obs437
        "created_by": None,
        "created_by_name": get_settings().auth_username,
    }


def test_list_marks_code_rows(client, code_evaluators_on):
    code_id = _create_code(client).json()["evaluator_id"]
    derived_id = _create_derived(client).json()["evaluator_id"]
    judge_id = _create_judge(client).json()["evaluator_id"]
    res = client.get("/api/eval/evaluators")
    assert res.status_code == 200
    by_id = {r["id"]: r for r in res.json()["evaluators"] if r["source"] != "builtin"}
    assert set(by_id) == {code_id, derived_id, judge_id}
    assert by_id[code_id]["definition"] == "code"
    assert by_id[code_id]["evaluator_type"] == "CustomCode"
    assert by_id[derived_id]["definition"] == "derived"
    assert by_id[derived_id]["evaluator_type"] == "CustomDerived"
    assert by_id[judge_id]["definition"] == "judge"
    assert by_id[judge_id]["evaluator_type"] == "Custom"
    assert all(r["source"] == "custom" and r["status"] == "ACTIVE" for r in by_id.values())
    assert by_id[judge_id]["name"] == "my_judge" and by_id[judge_id]["level"] == "SESSION"


def test_code_update_rewrites_the_row(client, code_evaluators_on):
    evaluator_id = _create_code(client).json()["evaluator_id"]
    res = client.put(f"/api/eval/evaluators/{evaluator_id}", json={
        "lambda_arn": LAMBDA_ARN,
        "lambda_timeout_s": 120,
        "level": "TRACE",
        "description": "lambda scorer v2",
    })
    assert res.status_code == 200
    assert res.json()["definition"] == "code"
    row = _row(evaluator_id)
    assert row.lambda_timeout_s == 120 and row.level == "TRACE"
    assert row.description == "lambda scorer v2"


def test_code_update_region_mismatch_rejected(client, code_evaluators_on):
    evaluator_id = _create_code(client).json()["evaluator_id"]
    res = client.put(f"/api/eval/evaluators/{evaluator_id}", json={
        "lambda_arn": "arn:aws:lambda:eu-west-1:111122223333:function:elsewhere",
    })
    assert res.status_code == 422
    assert res.json()["code"] == "evaluator.lambda_region_mismatch"
    assert _row(evaluator_id).lambda_arn == LAMBDA_ARN


def test_update_code_definition_mismatch_rejected(client, code_evaluators_on):
    code_id = _create_code(client).json()["evaluator_id"]
    judge_id = _create_judge(client).json()["evaluator_id"]
    derived_id = _create_derived(client).json()["evaluator_id"]
    # judge payload against a code-based evaluator — would convert it
    res = client.put(f"/api/eval/evaluators/{code_id}", json={
        "instructions": "Rate the tone of {context}",
    })
    assert res.status_code == 400
    assert res.json()["code"] == "evaluator.definition_mismatch"
    assert res.json()["detail"] == {"current": "code", "payload": "judge"}
    # derived payload against a code-based evaluator
    res = client.put(f"/api/eval/evaluators/{code_id}", json={
        "base_evaluator_id": "Builtin.Helpfulness",
    })
    assert res.status_code == 400
    assert res.json()["code"] == "evaluator.definition_mismatch"
    # code payload against a judge and against a derived evaluator
    for current in (judge_id, derived_id):
        res = client.put(f"/api/eval/evaluators/{current}", json={"lambda_arn": LAMBDA_ARN})
        assert res.status_code == 400
        assert res.json()["code"] == "evaluator.definition_mismatch"


# ─── the engine's view of the rows ───────────────────────────────────────────
def test_resolve_definitions_mixes_builtins_and_rows(client):
    from app.evaluation import evaluators_local
    from tests.conftest import ws_ctx

    judge_id = _create_judge(client).json()["evaluator_id"]
    db = SessionLocal()
    try:
        defs = evaluators_local.resolve_definitions(
            db, ws_ctx(id="default"), ["Builtin.Helpfulness", judge_id,
                                       "Builtin.TrajectoryInOrderMatch"])
    finally:
        db.close()
    assert defs[0] == {"id": "Builtin.Helpfulness"}
    assert defs[2] == {"id": "Builtin.TrajectoryInOrderMatch"}
    custom = defs[1]
    assert custom["id"] == judge_id and custom["definition"] == "judge"
    assert custom["instructions"] == "Rate the tone of {context}"
    assert custom["rating_scale"] == FIVE_POINT_SCALE and custom["level"] == "SESSION"
    assert custom["model_id"] == "global.zai.glm-5.3"


def test_resolve_definitions_refuses_unknown_ids(client):
    from app.core.errors import AppError
    from app.evaluation import evaluators_local
    from tests.conftest import ws_ctx

    db = SessionLocal()
    try:
        try:
            evaluators_local.resolve_definitions(db, ws_ctx(id="default"), ["cust_nope"])
        except AppError as exc:
            assert exc.code == "run.evaluator_not_found" and exc.status_code == 422
        else:
            raise AssertionError("unknown evaluator id must be refused")
    finally:
        db.close()


def test_run_preflight_refuses_an_unknown_custom_evaluator_before_any_replay(client):
    from app.models.ledger import Agent

    db = SessionLocal()
    try:
        db.add(Agent(id="agt1", workspace_id="default", name="demo", method="zip_runtime",
                     status="active", spec={}, resource_id="rid", arn="arn:x"))
        db.commit()
    finally:
        db.close()
    ds = client.post("/api/eval/datasets", json={"name": "d", "items": [{"prompt": "hi"}]})
    res = client.post("/api/eval/runs", json={
        "agent_id": "agt1", "dataset_id": ds.json()["id"], "evaluators": ["cust_missing"],
    })
    assert res.status_code == 422
    assert res.json()["code"] == "run.evaluator_not_found"
