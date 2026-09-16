"""Independent, published code-evaluator packages and their complete resource lifecycles."""
# ruff: noqa: F811 — imported fixtures are requested by test parameters
import copy
import importlib.util
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.assistant import evaluation_assets as assets
from app.assistant import evaluation_plan as plan_contract
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.evaluation.models import EvalDataset
from app.models.assistant import AssistantEvaluationPlan, EvaluationAssetOperation
from tests.conftest import ws_ctx
from tests.test_evaluation_assets import (  # noqa: F401 — hermetic fixtures by import
    RESOURCES,
    FakeLambda,
    _approve,
    _conversation,
    _err,
    _event,
    _model_turn,
    _op,
    _res,
    _run,
    _valid_plan,
    app_ready,
    fast,
    no_aws_clients,
    no_deploy_no_eval,
    no_network,
)
from tests.test_evaluation_assets_revision_review import EVENT_ID, ReviewFakes, _lower


class PackageLambda(FakeLambda):
    def __init__(self):
        super().__init__()
        self.packages = {}
        self.published_packages = {}

    def create_function(self, **kw):
        answer = super().create_function(**kw)
        self.packages[kw["FunctionName"]] = kw["Code"]["ZipFile"]
        return answer

    def publish_version(self, **kw):
        answer = super().publish_version(**kw)
        self.published_packages[answer["FunctionArn"]] = self.packages[kw["FunctionName"]]
        return answer


def _multi_plan(cid, content_hash):
    plan = _valid_plan(cid, content_hash)
    plan["evaluators"][-1]["rules"] = {
        "version": 1, "checks": [{"id": "amber", "type": "output_exact", "text": "amber"}],
    }
    plan["evaluators"].append({
        "kind": "code", "key": "other", "name": "kid_other", "title": "Other output",
        "level": "SESSION", "lambda_timeout_s": 17, "golden_test_ids": [],
        "rules": {"version": 1, "checks": [
            {"id": "blue", "type": "output_exact", "text": "blue"},
        ]},
    })
    return plan


def _setup():
    cid, content_hash = _conversation("local-operator")
    fakes = ReviewFakes()
    fakes.lam = PackageLambda()
    op_id, *_ = _approve(cid, content_hash, plan=_multi_plan(cid, content_hash), fakes=fakes)
    return op_id, fakes


def _chain(op, kind, group):
    return next(r for r in op.resources if r["kind"] == kind and r.get("code_group") == group)


def _cleanup(op_id, fakes):
    with SessionLocal() as db:
        return assets.cleanup_operation(
            db, db.get(EvaluationAssetOperation, op_id), ws_ctx(RESOURCES), clients=fakes,
            sleeper=lambda _: None,
        )


def _handler(payload, path):
    path.mkdir()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        rules = json.loads(archive.read("rules.json"))
        archive.extractall(path)
    spec = importlib.util.spec_from_file_location(path.name, path / "handler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return rules, module.lambda_handler


def test_each_published_zip_and_evaluator_binding_has_one_identityless_rule(app_ready, tmp_path):
    op_id, fakes = _setup()
    _run(op_id, fakes)
    op = _op(op_id)
    assert op.status == "succeeded", op.error
    assert len(fakes.lam.published_packages) == 2
    assert [r["code_group"] for r in assets.operation_out(op)["resources"]
            if r["kind"] == "lambda_function"] == ["tools", "other"]
    event = _event("SESSION", _model_turn("t1", "m1", "amber"))
    del event["evaluatorName"], event["evaluatorId"]
    for group, name, label, timeout in (
        ("tools", "kid_tools", "PASS", 60), ("other", "kid_other", "FAIL", 17),
    ):
        fn = _chain(op, "lambda_function", group)
        arn = fn["result"]["version_arn"]
        evaluator = _res(op, f"evaluator:{group}")
        assert evaluator["request"]["evaluatorConfig"]["codeBased"]["lambdaConfig"] == {
            "lambdaArn": arn, "lambdaTimeoutInSeconds": timeout,
        }
        rules, handler = _handler(fakes.lam.published_packages[arn], tmp_path / group)
        assert list(rules["evaluators"]) == [name]
        assert rules == fn["rules"]
        assert handler(event, None)["label"] == label
        assert "errorCode" not in handler(event, None)
        assert handler({**event, "evaluatorName": "unknown"}, None)["errorCode"] \
            == "UNKNOWN_EVALUATOR"
        assert len(fn["name"]) <= 64
        assert fn["name"] == assets.function_name(op_id, group)
        assert fn["timeout_s"] == timeout
        role = _chain(op, "lambda_role", group)
        log_arn = _chain(op, "log_group", group)["result"]["arn"].removesuffix(":*")
        assert role["result"]["policy_document"]["Statement"][0]["Resource"] \
            == [log_arn, log_arn + ":*"]
        grant = _chain(op, "role_grant", group)
        assert grant["result"]["policy_document"]["Statement"][0]["Resource"] == [arn]
        assert fakes.lam.policies[fn["name"]][0]["Resource"] == arn
        assert fn["result"]["readback"]["Role"] == role["result"]["role_arn"]
        with SessionLocal() as db:
            owner = assets.managed_evaluator(
                db, DEFAULT_WORKSPACE_ID, evaluator["result"]["evaluator_id"])
            assert owner["code_group"] == group
            assert assets.managed_package_evaluator_count(db, owner) == 1
            assert assets.managed_package_evaluator_count(
                db, {**owner, "evaluator_config": None}) == 1  # explicit group fallback
            assert assets.managed_package_evaluator_count(
                db, {**owner, "evaluator_config": {"codeBased": {"lambdaConfig": {
                    "lambdaArn": "unrecorded:version"}}}}) is None  # no guess by group
    assert len({r["key"] for r in op.resources}) == len(op.resources)
    calls = fakes.lam.create_calls, fakes.lam.publish_calls, fakes.control.create_calls
    assert not _run(op_id, fakes)
    assert calls == (fakes.lam.create_calls, fakes.lam.publish_calls, fakes.control.create_calls)
    assert _cleanup(op_id, fakes).status == "cleaned"
    assert not fakes.lam.functions and not fakes.logs.groups and not fakes.control.evaluators
    assert set(fakes.iam.roles) == {"launchpad-agent-execution-role"}
    assert set(fakes.iam.roles["launchpad-agent-execution-role"]["policies"]) \
        == {"launchpad-agent-execution"}
    with SessionLocal() as db:
        assert db.get(EvalDataset, op.dataset_id) is not None


@pytest.mark.parametrize("kind,service,method,name_field", [
    ("lambda_role", "iam", "create_role", "RoleName"),
    ("log_group", "logs", "create_log_group", "logGroupName"),
    ("lambda_function", "lam", "create_function", "FunctionName"),
    ("lambda_permission", "lam", "add_permission", "FunctionName"),
    ("role_grant", "iam", "put_role_policy", "PolicyName"),
])
def test_failure_and_retry_touch_only_the_owning_chain(
    app_ready, monkeypatch, kind, service, method, name_field,
):
    op_id, fakes = _setup()
    resource = _chain(_op(op_id), kind, "tools")
    name = (assets.function_name(op_id, "tools")
            if kind == "lambda_permission" else resource["name"])
    client = getattr(fakes, service)
    original = getattr(client, method)
    failed = False

    def fail_once(**kw):
        nonlocal failed
        if kw[name_field] == name and not failed:
            failed = True
            raise _err("ServiceFailure", method)
        return original(**kw)

    monkeypatch.setattr(client, method, fail_once)
    _run(op_id, fakes)
    op = _op(op_id)
    assert _chain(op, kind, "tools")["status"] == "failed"
    assert all(r["status"] == "ready" for r in op.resources if r.get("code_group") == "other")
    other = copy.deepcopy([r for r in op.resources if r.get("code_group") == "other"])
    other_cloud = copy.deepcopy(fakes.lam.functions[assets.function_name(op_id, "other")])
    assert _run(op_id, fakes)
    op = _op(op_id)
    assert op.status == "succeeded", op.error
    assert other == [r for r in op.resources if r.get("code_group") == "other"]
    assert other_cloud == fakes.lam.functions[assets.function_name(op_id, "other")]
    assert fakes.lam.publish_calls == 2


def test_persisted_conflict_does_not_block_or_recreate_another_chain(app_ready, monkeypatch):
    op_id, fakes = _setup()
    target = assets.function_name(op_id, "tools")
    original = fakes.lam.add_permission

    def foreign_permission(**kw):
        if kw["FunctionName"] == target:
            fakes.lam.policies[target] = [{
                "Sid": kw["StatementId"], "Effect": "Allow", "Action": "lambda:*",
                "Principal": {"Service": kw["Principal"]}, "Resource": "*",
            }]
        return original(**kw)

    monkeypatch.setattr(fakes.lam, "add_permission", foreign_permission)
    _run(op_id, fakes)
    _run(op_id, fakes)
    op = _op(op_id)
    assert _chain(op, "lambda_permission", "tools")["status"] == "conflict"
    assert _res(op, "evaluator:tools")["status"] == "blocked"
    assert _res(op, "evaluator:other")["status"] == "ready"
    assert _chain(op, "role_grant", "tools")["status"] == "blocked"
    assert fakes.lam.create_calls == fakes.lam.publish_calls == 2


@pytest.mark.parametrize("kind,client_name", [
    ("lambda_role", "iam"), ("log_group", "logs"), ("lambda_function", "lam"),
])
@pytest.mark.parametrize("action", ["retry", "cleanup"])
def test_lost_create_response_keeps_ownership_unknown_in_only_its_chain(
    app_ready, kind, client_name, action,
):
    op_id, fakes = _setup()
    client = getattr(fakes, client_name)
    client.lose_create_response = True
    _run(op_id, fakes)
    op = _op(op_id)
    unknown = _chain(op, kind, "tools")
    assert unknown["status"] == "unknown" and not unknown.get("owned")
    other = copy.deepcopy([r for r in op.resources if r.get("code_group") == "other"])
    assert all(r["status"] == "ready" for r in other)
    name = unknown["name"]
    inventory = {"iam": fakes.iam.roles, "logs": fakes.logs.groups,
                 "lam": fakes.lam.functions}[client_name]
    if action == "cleanup":
        op = _cleanup(op_id, fakes)
        assert op.status == "partial" and name in inventory
        assert _chain(op, kind, "tools")["status"] == "unknown"
        assert all(_chain(op, k, "other")["status"] == "deleted" for k in assets.CODE_CHAIN)
    else:
        # An operator removes the unproven resource. Retry creates it under recorded
        # ownership instead of adopting it, while the complete sibling remains intact.
        inventory.pop(name)
        _run(op_id, fakes)
        op = _op(op_id)
        assert op.status == "succeeded", op.error
        assert _chain(op, kind, "tools")["owned"]
        assert other == [r for r in op.resources if r.get("code_group") == "other"]


def test_exhausted_dependency_blocks_only_its_chain(app_ready):
    op_id, fakes = _setup()
    with SessionLocal() as db:
        op = db.get(EvaluationAssetOperation, op_id)
        resources = copy.deepcopy(op.resources)
        role = next(r for r in resources if r["key"] == "lambda_role:tools")
        role.update(status="failed", attempts=assets.MAX_ATTEMPTS, error="exhausted")
        op.resources = resources
        db.commit()
    _run(op_id, fakes)
    op = _op(op_id)
    assert _res(op, "evaluator:tools")["status"] == "blocked"
    assert _res(op, "evaluator:other")["status"] == "ready"
    assert assets.function_name(op_id, "tools") not in fakes.lam.functions


def test_cleanup_waits_only_for_evaluators_using_that_chain(app_ready):
    op_id, fakes = _setup()
    _run(op_id, fakes)
    op = _op(op_id)
    eid = _res(op, "evaluator:tools")["result"]["evaluator_id"]
    fakes.control.evaluators[eid]["locked"] = True
    op = _cleanup(op_id, fakes)
    assert op.status == "partial"
    assert _res(op, "evaluator:tools")["status"] == "delete_failed"
    assert _res(op, "evaluator:other")["status"] == "deleted"
    for kind in assets.CODE_CHAIN:
        assert _chain(op, kind, "tools")["status"] == "retained"
        assert _chain(op, kind, "other")["status"] == "deleted"
    assert set(fakes.lam.functions) == {assets.function_name(op_id, "tools")}
    fakes.control.evaluators[eid]["locked"] = False
    assert _cleanup(op_id, fakes).status == "cleaned"
    assert not fakes.lam.functions and not fakes.logs.groups


@pytest.mark.parametrize("drift", ["extra_version", "alias", "role", "log_group", "grant"])
def test_cleanup_preserves_drift_and_finishes_the_independent_chain(app_ready, drift):
    op_id, fakes = _setup()
    _run(op_id, fakes)
    op = _op(op_id)
    fn = _chain(op, "lambda_function", "tools")
    name = fn["name"]
    if drift == "extra_version":
        live = fakes.lam.functions[name]
        live["versions"]["2"] = {**live["versions"]["1"], "Version": "2"}
    elif drift == "alias":
        fakes.lam.aliases[name] = [{"Name": "foreign", "FunctionVersion": "1"}]
    elif drift == "role":
        fakes.iam.roles[name]["policies"][assets.LOGS_POLICY_NAME] = {"foreign": True}
    elif drift == "log_group":
        fakes.logs.groups[f"/aws/lambda/{name}"]["retention"] = 99
    else:
        grant_name = _chain(op, "role_grant", "tools")["name"]
        fakes.iam.roles["launchpad-agent-execution-role"]["policies"][grant_name] = {
            "foreign": True,
        }
    op = _cleanup(op_id, fakes)
    assert op.status == "partial"
    for kind in assets.CODE_CHAIN:
        assert _chain(op, kind, "other")["status"] == "deleted"
    if drift in ("extra_version", "alias"):
        assert name in fakes.lam.functions
        assert _chain(op, "lambda_function", "tools")["status"] == "conflict"
        assert _chain(op, "lambda_role", "tools")["status"] == "retained"
        assert _chain(op, "log_group", "tools")["status"] == "retained"
    elif drift == "role":
        assert fakes.iam.roles[name]["policies"][assets.LOGS_POLICY_NAME] == {"foreign": True}
    elif drift == "log_group":
        assert fakes.logs.groups[f"/aws/lambda/{name}"]["retention"] == 99
    else:
        assert fakes.iam.roles["launchpad-agent-execution-role"]["policies"][grant_name] == {
            "foreign": True,
        }


def _save_legacy_shared_intents(op_id):
    """Represent the old producer's approved shared package, before any cloud writes."""
    with SessionLocal() as db:
        op = db.get(EvaluationAssetOperation, op_id)
        plan = plan_contract.EvaluationPlan.model_validate(
            db.get(AssistantEvaluationPlan, op.plan_id).content)
        resources = copy.deepcopy(op.resources)
        resources = [r for r in resources
                     if r["kind"] not in assets.CODE_CHAIN or r.get("code_group") == "tools"]
        name = assets.function_name(op_id)
        for r in resources:
            r.pop("code_group", None)
            if r["kind"] in assets.CODE_CHAIN:
                r["key"] = r["kind"]
            if r["kind"] in ("lambda_role", "lambda_function"):
                r["name"] = name
            elif r["kind"] == "log_group":
                r["name"] = f"/aws/lambda/{name}"
            elif r["kind"] == "role_grant":
                r["name"] = f"launchpad-evalop-{op_id}"
            if r["kind"] == "lambda_function":
                r["rules"] = assets.canonical_rules(plan)
                _, r["digest"] = assets.build_package(r["rules"], r["nonce"])
                _, r["rules_digest"] = assets.build_package(r["rules"])
        op.resources = resources
        db.commit()


def test_legacy_pending_shared_package_refuses_every_new_write(app_ready):
    op_id, fakes = _setup()
    _save_legacy_shared_intents(op_id)
    before = _op(op_id).resources
    assert _run(op_id, fakes)
    op = _op(op_id)
    assert op.status == "failed" and "new evaluation-plan revision" in op.error
    assert op.resources == before and op.dataset_id is None
    assert not fakes.lam.functions and not fakes.logs.groups and not fakes.control.evaluators
    assert not fakes.iam.calls


def test_legacy_retry_requires_replacement_without_consuming_an_attempt(app_ready, monkeypatch):
    op_id, fakes = _setup()
    _save_legacy_shared_intents(op_id)
    _run(op_id, fakes)
    before = _op(op_id)
    assert assets.operation_out(before)["requires_new_plan"] is True

    def no_worker(*args):
        pytest.fail("a legacy-package retry must not dispatch a worker")

    monkeypatch.setattr(assets, "start_async", no_worker)
    with SessionLocal() as db:
        with pytest.raises(AppError) as error:
            assets.retry_operation(db, db.get(EvaluationAssetOperation, op_id))
    assert error.value.code == "assistant.evaluation_assets_new_plan_required"
    assert error.value.status_code == 409
    after = _op(op_id)
    assert (after.status, after.attempts, after.resources, after.log) == (
        before.status, before.attempts, before.resources, before.log,
    )
    client = TestClient(app_ready)
    path = (f"/api/assistant/architect/conversations/{before.conversation_id}"
            f"/evaluation-plan/operations/{op_id}")
    response = client.get(path)
    assert response.status_code == 200
    assert response.json()["operation"]["requires_new_plan"] is True
    response = client.post(f"{path}/retry")
    assert response.status_code == 409
    assert response.json()["code"] == "assistant.evaluation_assets_new_plan_required"
    assert _op(op_id).attempts == before.attempts


def test_replacement_draft_preserves_saved_plan_and_materializes_isolated_packages(app_ready):
    from app.models.assistant import AssistantConversation
    from app.models.ledger import Workspace

    op_id, fakes = _setup()
    _save_legacy_shared_intents(op_id)
    _run(op_id, fakes)
    before = _op(op_id)
    with SessionLocal() as db:
        original = db.get(AssistantEvaluationPlan, before.plan_id)
        content = copy.deepcopy(original.content)
        conversation = db.get(AssistantConversation, before.conversation_id)
        draft = assets.edit_plan(db, conversation, content, created_by="operator")
        assert draft.status == "draft"
        assert draft.revision == original.revision + 1
        assert draft.content == content
        assert draft.source_revision == original.source_revision
        assert draft.source_content_hash == original.source_content_hash
        assert not fakes.lam.functions and not fakes.control.evaluators
        replacement_id = draft.id
    # The ordinary approval still binds the exact saved revision/hash.
    with SessionLocal() as db:
        draft = db.get(AssistantEvaluationPlan, replacement_id)
        conversation = db.get(AssistantConversation, before.conversation_id)
        outcome = assets.approve_plan(
            db, conversation, db.get(Workspace, DEFAULT_WORKSPACE_ID),
            plan_revision=draft.revision, plan_hash=draft.content_hash,
            approved_by="admin", approver_user_id=None, clients=fakes,
        )
        replacement_op_id = outcome.operation.id
    _run(replacement_op_id, fakes)
    result = _op(replacement_op_id)
    assert result.status == "succeeded", result.error
    assert assets.operation_out(result)["requires_new_plan"] is False
    assert len(fakes.lam.functions) == 2
    assert all(len(r["rules"]["evaluators"]) == 1 for r in result.resources
               if r["kind"] == "lambda_function")
    assert _op(op_id).resources == before.resources


def test_historical_shared_packages_are_inspected_refused_for_reuse_and_cleanable(
    app_ready, monkeypatch, tmp_path,
):
    op_id, fakes = _setup()
    _save_legacy_shared_intents(op_id)
    # Seed the historical artifact using the former producer's saved intents. Only this
    # setup bypasses the new write refusal; the real ownership and cleanup code still run.
    with monkeypatch.context() as patch:
        patch.setattr(assets, "_require_isolated_packages", lambda resources, plan: None)
        patch.setattr(assets, "_approved_package_rules", lambda plan, res: res["rules"])
        _run(op_id, fakes)
    original = _op(op_id)
    assert original.status == "succeeded", original.error
    fn = _res(original, "lambda_function")
    _, handler = _handler(fakes.lam.published_packages[fn["result"]["version_arn"]],
                          tmp_path / "legacy")
    event = _event("SESSION", _model_turn("t1", "m1", "amber"))
    del event["evaluatorName"], event["evaluatorId"]
    assert handler(event, None)["errorCode"] == "UNKNOWN_EVALUATOR"
    eid = _res(original, "evaluator:tools")["result"]["evaluator_id"]
    with SessionLocal() as db:
        owner = assets.managed_evaluator(db, DEFAULT_WORKSPACE_ID, eid)
        assert assets.managed_package_evaluator_count(db, owner) == 2
        assert assets.managed_package_evaluator_count(db, None) is None
    cid, content_hash = _conversation("local-operator")
    plan = _valid_plan(cid, content_hash, with_code=False)
    plan["evaluators"][1]["name"] = "new_judge"
    plan["evaluators"].append({
        "kind": "existing", "key": "old_code", "title": "Historical",
        "evaluator_id": eid, "golden_test_ids": [],
    })
    replacement, *_ = _approve(cid, content_hash, plan=plan, fakes=fakes)
    _run(replacement, fakes)
    reference = _res(_op(replacement), "existing:old_code")
    assert reference["status"] == "conflict"
    assert "containing 2 evaluators" in reference["error"]
    assert _op(op_id).resources == original.resources
    assert _cleanup(op_id, fakes).status == "cleaned"
    assert not fakes.lam.functions


def test_legacy_single_code_keys_resume_and_cleanup_unchanged(app_ready):
    cid, content_hash = _conversation("local-operator")
    fakes = ReviewFakes()
    op_id, *_ = _approve(cid, content_hash, fakes=fakes)
    with SessionLocal() as db:
        op = db.get(EvaluationAssetOperation, op_id)
        resources = copy.deepcopy(op.resources)
        for resource in resources:
            resource.pop("code_group", None)
        op.resources = resources
        db.commit()
    _run(op_id, fakes)
    op = _op(op_id)
    assert op.status == "succeeded", op.error
    assert _res(op, "lambda_function")["name"] == assets.function_name(op_id)
    with SessionLocal() as db:
        eid = _res(op, "evaluator:tools")["result"]["evaluator_id"]
        assert assets.managed_package_evaluator_count(
            db, assets.managed_evaluator(db, DEFAULT_WORKSPACE_ID, eid)) == 1
    assert _cleanup(op_id, fakes).status == "cleaned"


def test_each_function_records_its_own_automatic_initialization_transition(app_ready, monkeypatch):
    op_id, fakes = _setup()
    original = fakes.lam.create_function

    def create(**kw):
        fakes.lam.activation_revision = "active-" + kw["FunctionName"]
        return original(**kw)

    monkeypatch.setattr(fakes.lam, "create_function", create)
    _run(op_id, fakes)
    op = _op(op_id)
    assert op.status == "succeeded", op.error
    for group in ("tools", "other"):
        fn = _chain(op, "lambda_function", group)
        transition = fn["result"]["revision_history"][0]
        assert transition["reason"] == "initial_activation_settled"
        assert transition["from"] == fn["result"]["initial_revision_id"]
        assert transition["to"] == "active-" + fn["name"]
        assert not fn.get("reviews")
    assert fakes.lam.publish_calls == 2


def _record_review_event(op, resource, fakes):
    record = {
        "eventID": EVENT_ID, "eventSource": "lambda.amazonaws.com",
        "eventName": "CreateFunction20150331", "awsRegion": op.region,
        "recipientAccountId": op.account_id, "readOnly": False,
        "requestID": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        "requestParameters": _lower({k: v for k, v in resource["request"].items()
                                     if k != "CodeSha256"}),
        "responseElements": _lower(resource["result"]["create_response"]),
    }
    fakes.trail.records[EVENT_ID] = [record]


@pytest.mark.parametrize("dependency_drift", [False, True])
def test_initialization_review_selects_the_second_function_and_only_its_dependencies(
    app_ready, monkeypatch, dependency_drift,
):
    op_id, fakes = _setup()
    target = assets.function_name(op_id, "other")
    original = fakes.lam.create_function

    def create(**kw):
        if kw["FunctionName"] == target:
            fakes.lam.activation_revision = "rev-second-active"
        return original(**kw)

    monkeypatch.setattr(fakes.lam, "create_function", create)
    with monkeypatch.context() as patch:
        patch.setattr(assets, "initialization_transition_evidence", lambda stored, cfg: None)
        _run(op_id, fakes)
    op = _op(op_id)
    fn = _chain(op, "lambda_function", "other")
    assert fn["status"] == "conflict"
    first = copy.deepcopy([r for r in op.resources if r.get("code_group") == "tools"])
    assert all(r["status"] == "ready" for r in first)
    _record_review_event(op, fn, fakes)
    monkeypatch.setattr(assets, "start_async", lambda *args, **kwargs: None)
    kwargs = dict(
        plan_hash=op.plan_hash, expected_created=fn["result"]["initial_revision_id"],
        expected_current="rev-second-active", event_id=EVENT_ID,
        reason="Verified second function initialization", reviewer="admin", reviewer_user_id=None,
        clients=fakes,
    )
    with SessionLocal() as db:
        outcome = assets.review_lambda_initial_revision(
            db, db.get(EvaluationAssetOperation, op_id), **kwargs)
        assert outcome.operation.status == "queued"
    reviewed = _op(op_id)
    assert first == [r for r in reviewed.resources if r.get("code_group") == "tools"]
    assert _chain(reviewed, "lambda_function", "other")["status"] == "pending"
    assert _res(reviewed, "evaluator:other")["status"] == "pending"
    if dependency_drift:
        fakes.iam.roles[target]["policies"][assets.LOGS_POLICY_NAME] = {"changed": True}
    _run(op_id, fakes)
    op = _op(op_id)
    if dependency_drift:
        assert _chain(op, "lambda_function", "other")["status"] == "conflict"
        assert "dependencies differ" in _chain(op, "lambda_function", "other")["error"]
    else:
        assert op.status == "succeeded", op.error
    assert first == [r for r in op.resources if r.get("code_group") == "tools"]
    with SessionLocal() as db:
        replay = assets.review_lambda_initial_revision(
            db, db.get(EvaluationAssetOperation, op_id), **kwargs)
    assert not replay.started and fakes.trail.calls == 1
    assert fakes.lam.create_calls == 2
    assert fakes.lam.publish_calls == (1 if dependency_drift else 2)


def test_initialization_review_still_refuses_unrelated_conflicts(app_ready, monkeypatch):
    op_id, fakes = _setup()
    fakes.lam.activation_revision = "rev-first-active"
    with monkeypatch.context() as patch:
        patch.setattr(assets, "initialization_transition_evidence", lambda stored, cfg: None)
        _run(op_id, fakes)
    op = _op(op_id)
    _chain(op, "lambda_role", "other")["status"] = "conflict"
    fn = _chain(op, "lambda_function", "tools")
    with pytest.raises(AppError, match="unrelated open outcome"):
        assets._review_target(op, op.plan_hash, fn["result"]["initial_revision_id"])
