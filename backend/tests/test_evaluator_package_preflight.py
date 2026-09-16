"""Review counts distinguish new isolated packages from recorded shared operations."""

from types import SimpleNamespace

import pytest

from app.assistant import evaluation_assets
from app.assistant.evaluation_plan import CodeEvaluator, CodeRules, plan_summary
from app.core.errors import AppError
from app.evaluation.routers import _assert_target_references


@pytest.mark.parametrize("count", [0, 1, 3])
@pytest.mark.parametrize("grant", [False, True])
def test_review_counts_one_resource_chain_per_code_evaluator(count, grant):
    plan = {"evaluators": [{"kind": "code", "key": f"rule{i}"} for i in range(count)]
            + [{"kind": "judge"}, {"kind": "existing"}],
            "grant_workspace_execution_role": grant}
    summary = plan_summary(plan)
    assert summary["lambda_functions"] == summary["iam_roles"] == count
    assert summary["role_grants"] == (count if grant else 0)


def test_historical_shared_operation_displays_its_recorded_resources():
    plan = {"evaluators": [{"kind": "code"}, {"kind": "code"}]}
    resources = [
        {"kind": "lambda_function", "status": "ready"},
        {"kind": "lambda_role", "status": "ready"},
        {"kind": "role_grant", "status": "skipped"},
        {"kind": "evaluator", "status": "ready"},
        {"kind": "evaluator", "status": "ready"},
    ]
    summary = plan_summary(plan, resources=resources)
    assert summary["lambda_functions"] == summary["iam_roles"] == 1
    assert summary["role_grants"] == 0
    assert plan_summary(plan, resources=[])["lambda_functions"] == 0


@pytest.mark.parametrize("package_count", [0, 2, 4])
def test_run_preflight_rejects_ambiguous_managed_package_before_cloud_calls(
    monkeypatch, package_count,
):
    owner = {
        "operation_id": "historical-operation", "definition": "code", "level": "TRACE",
        "evaluator_config": {"codeBased": {"lambdaConfig": {
            "lambdaArn": "arn:aws:lambda:us-west-2:111122223333:function:old:1",
        }}},
    }
    monkeypatch.setattr(evaluation_assets, "managed_evaluator", lambda *args: owner)
    monkeypatch.setattr(evaluation_assets, "managed_rules",
                        lambda *args: [{"id": "ref", "type": "reference_response"}])
    monkeypatch.setattr(evaluation_assets, "managed_package_evaluator_count",
                        lambda *args: package_count)

    def no_cloud(*args):
        raise AssertionError("preflight must not call AWS")

    monkeypatch.setattr("app.evaluation.routers.control_client", no_cloud)
    with pytest.raises(AppError) as error:
        _assert_target_references(
            None, SimpleNamespace(id="default"), ["reference_response-old"], [], True,
            agent_spec={"knowledge_bases": [{"kb_id": "financial-kb"}]},
        )
    assert error.value.code == "run.evaluator_package_ambiguous"
    assert error.value.status_code == 422
    assert error.value.detail["packaged_evaluators"] == package_count


@pytest.mark.parametrize("package_count", [1, None])
def test_unambiguous_or_unknown_package_is_not_rejected_by_name(monkeypatch, package_count):
    owner = {
        "operation_id": "operation", "definition": "code", "level": "SESSION",
        "evaluator_config": {"codeBased": {"lambdaConfig": {
            "lambdaArn": "arn:aws:lambda:us-west-2:111122223333:function:one:1",
        }}},
    }
    monkeypatch.setattr(evaluation_assets, "managed_evaluator", lambda *args: owner)
    monkeypatch.setattr(evaluation_assets, "managed_rules",
                        lambda *args: [{"id": "literal", "type": "output_not_contains",
                                        "text": "sent successfully"}])
    monkeypatch.setattr(evaluation_assets, "managed_package_evaluator_count",
                        lambda *args: package_count)
    _assert_target_references(
        None, SimpleNamespace(id="default"), ["legacy-named-evaluator"], [], False,
        agent_spec={"skills": ["s3://test/skill/"]},
    )


def test_single_rule_package_cannot_be_swapped_to_another_approved_evaluator():
    entries = [
        CodeEvaluator(kind="code", key=color, title=color, name=f"only_{color}", level="SESSION",
                      rules=CodeRules(checks=[
                          {"id": "color", "type": "output_exact", "text": color},
                      ]))
        for color in ("amber", "blue")
    ]
    plan = SimpleNamespace(
        evaluators=entries, dataset=SimpleNamespace(name="colors"),
        grant_workspace_execution_role=False,
    )
    resources = evaluation_assets.compose_intents(plan, "a" * 32)
    evaluation_assets._require_isolated_packages(resources, plan)
    functions = [r for r in resources if r["kind"] == "lambda_function"]
    functions[0]["rules"] = functions[1]["rules"]
    # Even a coherent ZIP digest for the swapped single-rule package cannot change
    # which rule this approved evaluator owns.
    _, functions[0]["digest"] = evaluation_assets.build_package(
        functions[0]["rules"], functions[0]["nonce"],
    )
    with pytest.raises(evaluation_assets._Stop, match="approved evaluator amber"):
        evaluation_assets._require_isolated_packages(resources, plan)
