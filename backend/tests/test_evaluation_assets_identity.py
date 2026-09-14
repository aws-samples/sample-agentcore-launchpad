"""SE-047 phase C — permanent / unknown resource identity and truthful cleanup, through
the REAL worker and cleanup with low-level cloud doubles and the temporary test DB.
Hermetic guards and fakes are the ones of ``tests/test_evaluation_assets.py``. Every
case asserts the outcome (status, retained dependencies, zero foreign deletes) — printed
observations are not acceptance."""

# ruff: noqa: F811 — the imported ``app_ready`` fixture is referenced by test parameters

import copy
import json

import pytest
from botocore.exceptions import ClientError

from app.assistant import evaluation_assets as assets
from app.core.db import SessionLocal
from app.models.assistant import EvaluationAssetOperation
from tests.conftest import ws_ctx
from tests.test_evaluation_assets import (  # noqa: F401 — fixtures by import
    ACCOUNT,
    RESOURCES,
    Fakes,
    _approve,
    _conversation,
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

CHAIN = ("lambda_function", "log_group", "lambda_role")


def _clean(op_id, fakes):
    with SessionLocal() as db:
        return assets.cleanup_operation(db, db.get(EvaluationAssetOperation, op_id),
                                        ws_ctx(RESOURCES), clients=fakes,
                                        sleeper=lambda s: None)


def _completed():
    cid, h = _conversation("local-operator")
    fakes = Fakes()
    op_id, *_ = _approve(cid, h, fakes=fakes)
    _run(op_id, fakes)
    op = _op(op_id)
    assert op.status == "succeeded", op.error
    return op_id, fakes, assets.function_name(op_id)


def _footprint(fakes, fn):
    return {"role": fn in fakes.iam.roles, "log_group": f"/aws/lambda/{fn}" in fakes.logs.groups,
            "function": fn in fakes.lam.functions,
            "grant": "launchpad-evalop-" in "".join(
                fakes.iam.roles["launchpad-agent-execution-role"]["policies"])}


# ---------------------------------------------------------------------------
# 1. lost create → copied replacement: unknown, never adopted, never deleted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["role", "logs", "function"])
def test_lost_create_then_copied_replacement_stays_unknown(app_ready, kind):
    cid, h = _conversation("local-operator")
    fakes = Fakes()
    op_id, *_ = _approve(cid, h, fakes=fakes)
    fn = assets.function_name(op_id)
    key = {"role": "lambda_role", "logs": "log_group", "function": "lambda_function"}[kind]
    {"role": fakes.iam, "logs": fakes.logs, "function": fakes.lam}[kind].lose_create_response = True
    _run(op_id, fakes)
    op = _op(op_id)
    res = _res(op, key)
    assert res["status"] == "unknown" and "cannot prove" in res["error"], res
    assert not res.get("owned") and not res.get("recovered")
    assert op.status == "partial" and key in op.error
    downstream = [r["key"] for r in op.resources if r["kind"] in assets.CODE_CHAIN
                  and r["key"] != key and r["status"] != "ready"]
    assert all(_res(op, k)["status"] == "blocked" for k in downstream), downstream
    assert not any(r["kind"] == "evaluator" and r["status"] == "ready" and r.get(
        "definition") == "code" for r in op.resources)
    # a replacement with copied PUBLIC content (tags / description / nonce-bearing package)
    # but a fresh service-issued identity appears before cleanup
    if kind == "role":
        replacement = copy.deepcopy(fakes.iam.roles[fn])
        replacement["RoleId"] = "AROAFOREIGNREPLACEMENT"
        fakes.iam.roles[fn] = replacement
    elif kind == "logs":
        replacement = copy.deepcopy(fakes.logs.groups[f"/aws/lambda/{fn}"])
        replacement["creationTime"] = 99999999999
        fakes.logs.groups[f"/aws/lambda/{fn}"] = replacement
    else:
        replacement = copy.deepcopy(fakes.lam.functions[fn])
        replacement["cfg"].update(Role=f"arn:aws:iam::{ACCOUNT}:role/foreign", Timeout=900,
                                  MemorySize=10240, RevisionId="foreign-new-revision")
        fakes.lam.functions[fn] = replacement
    replaced_identity = json.loads(json.dumps(replacement))
    # a worker retry re-evaluates without adopting: the same-name resource stays unknown
    _run(op_id, fakes)
    res = _res(_op(op_id), key)
    assert res["status"] == "unknown" and not res.get("owned"), res
    assert {"role": fakes.iam.roles.get(fn), "logs": fakes.logs.groups.get(f"/aws/lambda/{fn}"),
            "function": fakes.lam.functions.get(fn)}[kind] == replaced_identity
    op = _clean(op_id, fakes)
    res = _res(op, key)
    assert res["status"] == "unknown" and "cannot prove" in res["cleanup"]["note"], res
    # the possible replacement is never deleted or modified; owned UPSTREAM resources
    # (proven ours, nothing depends on them any more) may legitimately go
    live = {"role": fakes.iam.roles.get(fn), "logs": fakes.logs.groups.get(f"/aws/lambda/{fn}"),
            "function": fakes.lam.functions.get(fn)}[kind]
    assert live == replaced_identity
    if kind == "function":  # the log group and role depend on an unresolved function
        assert _footprint(fakes, fn) == {"role": True, "log_group": True, "function": True,
                                         "grant": False}
        assert _res(op, "log_group")["status"] == "retained"
        assert _res(op, "lambda_role")["status"] == "retained"
    assert op.status == "partial" and key in op.error


def test_preexisting_collision_and_unknown_create_are_both_unowned_but_differ(app_ready):
    """A collision established at creation (ConflictException, no lost response) is a
    foreign resource: not ours, nothing to clean → the operation can be 'cleaned'. A
    lost response is different: our own asset MAY exist → never 'cleaned'."""
    cid, h = _conversation("local-operator")
    fakes = Fakes()
    op_id, *_ = _approve(cid, h, fakes=fakes)
    fn = assets.function_name(op_id)
    fakes.iam.roles[fn] = {"RoleName": fn, "Arn": f"arn:aws:iam::{ACCOUNT}:role/{fn}",
                           "RoleId": "AROAFOREIGN", "policies": {}, "trust": "{}",
                           "Description": "foreign", "Tags": []}
    _run(op_id, fakes)
    role = _res(_op(op_id), "lambda_role")
    assert role["status"] == "conflict" and not role.get("owned")
    op = _clean(op_id, fakes)
    assert op.status == "cleaned" and fn in fakes.iam.roles


# ---------------------------------------------------------------------------
# 2./3. recorded function identity: RevisionId, $LATEST, missing version, disappearance
# ---------------------------------------------------------------------------


def test_recorded_revision_change_with_same_code_refuses_delete(app_ready):
    op_id, fakes, fn = _completed()
    recorded = _res(_op(op_id), "lambda_function")["result"]["readback"]["RevisionId"]
    assert recorded == fakes.lam.functions[fn]["versions"]["1"]["RevisionId"]
    fakes.lam.functions[fn]["versions"]["1"]["RevisionId"] = "replacement-revision"
    op = _clean(op_id, fakes)
    fnr = _res(op, "lambda_function")
    assert fnr["status"] == "conflict" and "RevisionId" in fnr["cleanup"]["note"]
    assert _res(op, "log_group")["status"] == "retained"
    assert _res(op, "lambda_role")["status"] == "retained"
    assert _footprint(fakes, fn) == {"role": True, "log_group": True, "function": True,
                                     "grant": False}
    assert op.status == "partial"


@pytest.mark.parametrize("field, value", [("RevisionId", "user-updated"),
                                          ("Timeout", 900), ("CodeSha256", "new-code")])
def test_changed_latest_while_version_one_same_refuses_whole_function_delete(
    app_ready, field, value
):
    op_id, fakes, fn = _completed()
    fakes.lam.functions[fn]["cfg"][field] = value  # $LATEST only; version 1 untouched
    op = _clean(op_id, fakes)
    fnr = _res(op, "lambda_function")
    assert fnr["status"] == "conflict" and "$LATEST" in fnr["cleanup"]["note"]
    assert field in fnr["cleanup"]["note"]
    assert fn in fakes.lam.functions and "1" in fakes.lam.functions[fn]["versions"]
    assert _res(op, "log_group")["status"] == "retained"
    assert _res(op, "lambda_role")["status"] == "retained"
    assert op.status == "partial"


@pytest.mark.parametrize("extra", ["version", "alias"])
def test_unowned_versions_or_aliases_block_whole_function_delete(app_ready, extra):
    op_id, fakes, fn = _completed()
    f = fakes.lam.functions[fn]
    if extra == "version":
        f["versions"]["2"] = {**f["cfg"], "Version": "2", "RevisionId": "rev-x",
                              "FunctionArn": f["cfg"]["FunctionArn"] + ":2"}
    else:
        fakes.lam.aliases[fn] = [{"Name": "live", "FunctionVersion": "1"}]
    op = _clean(op_id, fakes)
    fnr = _res(op, "lambda_function")
    assert fnr["status"] == "conflict" and "beyond the recorded" in fnr["cleanup"]["note"]
    assert fn in fakes.lam.functions and op.status == "partial"
    assert _res(op, "lambda_role")["status"] == "retained"


def test_missing_version_with_latest_present_is_not_function_absence(app_ready):
    op_id, fakes, fn = _completed()
    fakes.lam.functions[fn]["versions"].pop("1")  # $LATEST survives
    op = _clean(op_id, fakes)
    fnr = _res(op, "lambda_function")
    assert fnr["status"] == "conflict" and "still exists" in fnr["cleanup"]["note"]
    assert fn in fakes.lam.functions
    assert _res(op, "log_group")["status"] == "retained"
    assert _res(op, "lambda_role")["status"] == "retained"
    assert f"/aws/lambda/{fn}" in fakes.logs.groups and fn in fakes.iam.roles
    assert op.status == "partial"
    # the whole function really disappearing IS absence: dependencies follow, cleaned
    fakes.lam.functions.pop(fn)
    op = _clean(op_id, fakes)
    assert _res(op, "lambda_function")["status"] == "deleted"
    assert op.status == "cleaned", op.error
    assert fakes.logs.groups == {} and set(fakes.iam.roles) == {"launchpad-agent-execution-role"}


def test_delete_function_accepted_but_present_then_eventually_gone(app_ready):
    op_id, fakes, fn = _completed()
    real_delete = fakes.lam.delete_function
    calls = []

    def accepted_but_present(FunctionName, Qualifier=None):
        calls.append(FunctionName)
        fakes.lam.functions[FunctionName]["cfg"]["State"] = "Pending"
        return {}

    fakes.lam.delete_function = accepted_but_present
    op = _clean(op_id, fakes)
    fnr = _res(op, "lambda_function")
    assert calls == [fn] and fnr["status"] == "delete_pending", fnr
    assert fn in fakes.lam.functions
    assert _res(op, "log_group")["status"] == "retained"
    assert _res(op, "lambda_role")["status"] == "retained"
    assert f"/aws/lambda/{fn}" in fakes.logs.groups and fn in fakes.iam.roles
    assert op.status == "partial" and "lambda_function" in op.error
    # later the deletion completes: the pending delete is re-driven and confirmed gone
    fakes.lam.delete_function = real_delete
    op = _clean(op_id, fakes)
    assert _res(op, "lambda_function")["status"] == "deleted"
    assert op.status == "cleaned", op.error
    assert fakes.lam.functions == {} and fakes.logs.groups == {}
    assert set(fakes.iam.roles) == {"launchpad-agent-execution-role"}


def test_lost_delete_response_is_recorded_and_the_retry_completes(app_ready):
    op_id, fakes, fn = _completed()
    real_delete = fakes.lam.delete_function

    def lost(FunctionName, Qualifier=None):
        real_delete(FunctionName, Qualifier)
        raise ConnectionError("response lost")

    fakes.lam.delete_function = lost
    op = _clean(op_id, fakes)
    fnr = _res(op, "lambda_function")
    assert fnr["status"] == "delete_failed" and "ConnectionError" in fnr["cleanup"]["note"]
    assert _res(op, "log_group")["status"] == "retained"
    assert _res(op, "lambda_role")["status"] == "retained"
    assert op.status == "partial"
    fakes.lam.delete_function = real_delete
    op = _clean(op_id, fakes)
    assert _res(op, "lambda_function")["status"] == "deleted"
    assert op.status == "cleaned", op.error


# ---------------------------------------------------------------------------
# 4. lost CreateEvaluator + list absence: unknown, chain retained, token retry resolves
# ---------------------------------------------------------------------------


def test_lost_code_evaluator_create_invisible_in_list_stays_unknown(app_ready):
    cid, h = _conversation("local-operator")
    fakes = Fakes()
    op_id, *_ = _approve(cid, h, fakes=fakes)
    fn = assets.function_name(op_id)
    original = fakes.control.create_evaluator

    def create(**kw):
        if kw["evaluatorName"] == "kid_tools":
            fakes.control.lose_response_once = True
        return original(**kw)

    fakes.control.create_evaluator = create
    _run(op_id, fakes)
    tools = _res(_op(op_id), "evaluator:tools")
    assert tools["status"] == "failed" and tools["request"] and not tools.get("result")
    assert tools.get("create_outcome") is None  # a lost response is not a rejection
    fakes.control.list_evaluators = lambda **kw: {"evaluators": []}  # not yet visible
    op = _clean(op_id, fakes)
    tools = _res(op, "evaluator:tools")
    assert tools["status"] == "unknown", tools
    assert "does not prove" in tools["cleanup"]["note"]
    assert tools["request"]["clientToken"] in tools["cleanup"]["note"]
    for key in CHAIN + ("role_grant",):
        assert _res(op, key)["status"] == "retained", key
    assert _footprint(fakes, fn) == {"role": True, "log_group": True, "function": True,
                                     "grant": True}
    # the owned judge is legitimately gone; the possibly-created code evaluator is alive
    assert [e["evaluatorName"] for e in fakes.control.evaluators.values()] == ["kid_tools"]
    assert op.status == "partial" and "evaluator:tools" in op.error
    # cleanup never created anything; the WORKER retry replays the idempotency token and
    # recovers the very same evaluator under our ownership — then cleanup completes
    created_before = fakes.control.create_calls
    ids_before = set(fakes.control.evaluators)
    _run(op_id, fakes)
    op = _op(op_id)
    tools = _res(op, "evaluator:tools")
    assert tools["status"] == "ready" and tools["owned"], tools
    assert tools["result"]["evaluator_id"] in ids_before  # the SAME evaluator, no duplicate
    assert fakes.control.create_calls == created_before + 1  # one token replay, nothing new
    assert [e["evaluatorName"] for e in fakes.control.evaluators.values()] == ["kid_tools"]
    del fakes.control.list_evaluators
    op = _clean(op_id, fakes)
    assert _res(op, "evaluator:tools")["status"] == "deleted"
    assert op.status == "cleaned", op.error
    assert fakes.control.evaluators == {} and fakes.lam.functions == {}


def test_definite_create_rejection_is_not_unknown(app_ready):
    cid, h = _conversation("local-operator")
    fakes = Fakes()
    op_id, *_ = _approve(cid, h, fakes=fakes)

    def rejected(**kw):
        raise ClientError({"Error": {"Code": "ValidationException", "Message": "bad"},
                           "ResponseMetadata": {"HTTPStatusCode": 400}}, "CreateEvaluator")

    fakes.control.create_evaluator = rejected
    _run(op_id, fakes)
    op = _op(op_id)
    assert _res(op, "evaluator:pii")["status"] == "failed"
    assert _res(op, "evaluator:pii")["create_outcome"] == "rejected"
    op = _clean(op_id, fakes)
    assert _res(op, "evaluator:pii")["status"] == "deleted"
    assert "rejected" in _res(op, "evaluator:pii")["cleanup"]["note"]
    assert op.status == "cleaned", op.error


# ---------------------------------------------------------------------------
# 5. cleanup identity drift on role / log group / evaluator: safe conflict
# ---------------------------------------------------------------------------


DRIFTS = {
    "role_arn": ("lambda_role", lambda f, fn, op:
                 f.iam.roles[fn].__setitem__("Arn", f"arn:aws:iam::999900001111:role/{fn}")),
    "role_trust": ("lambda_role", lambda f, fn, op: f.iam.roles[fn].__setitem__(
        "trust", json.dumps({"Statement": [{"Effect": "Allow", "Principal": {
            "Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]}))),
    "role_policy": ("lambda_role", lambda f, fn, op: f.iam.roles[fn]["policies"].__setitem__(
        assets.LOGS_POLICY_NAME, {"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"}]})),
    "log_arn": ("log_group", lambda f, fn, op: f.logs.groups[f"/aws/lambda/{fn}"].__setitem__(
        "arn", "arn:aws:logs:eu-west-1:999900001111:log-group:foreign:*")),
    "log_retention": ("log_group", lambda f, fn, op:
                      f.logs.groups[f"/aws/lambda/{fn}"].__setitem__("retention", 90)),
    "log_recreated": ("log_group", lambda f, fn, op:
                      f.logs.groups[f"/aws/lambda/{fn}"].__setitem__("creationTime", 5)),
    "evaluator_arn": ("evaluator:pii", lambda f, fn, op: f.control.evaluators[
        _res(op, "evaluator:pii")["result"]["evaluator_id"]].__setitem__(
        "evaluatorArn", "arn:aws:bedrock-agentcore:eu-west-1:999900001111:evaluator/x")),
}


@pytest.mark.parametrize("case", sorted(DRIFTS))
def test_cleanup_identity_drift_is_a_safe_conflict(app_ready, case):
    op_id, fakes, fn = _completed()
    key, mutate = DRIFTS[case]
    mutate(fakes, fn, _op(op_id))
    op = _clean(op_id, fakes)
    res = _res(op, key)
    assert res["status"] == "conflict", (case, res)
    assert op.status == "partial" and key in op.error
    if key == "lambda_role":
        assert fn in fakes.iam.roles
        assert assets.LOGS_POLICY_NAME in fakes.iam.roles[fn]["policies"]
    elif key == "log_group":
        assert f"/aws/lambda/{fn}" in fakes.logs.groups
    else:
        assert len(fakes.control.evaluators) == 1  # the drifted judge stays
        for k in CHAIN:
            assert _res(op, k)["status"] == "retained", k


def test_incomplete_identity_snapshot_requires_review_not_delete(app_ready):
    op_id, fakes, fn = _completed()
    with SessionLocal() as db:
        op = db.get(EvaluationAssetOperation, op_id)
        resources = json.loads(json.dumps(op.resources))
        for r in resources:
            if r["key"] == "lambda_function":
                r["result"].pop("latest_readback")
            if r["key"] == "lambda_role":
                r["result"].pop("trust_document")
        op.resources = resources
        db.commit()
    op = _clean(op_id, fakes)
    assert _res(op, "lambda_function")["status"] == "conflict"
    assert "incomplete" in _res(op, "lambda_function")["cleanup"]["note"]
    assert _res(op, "lambda_role")["status"] == "retained"  # function still exists
    assert fn in fakes.lam.functions and fn in fakes.iam.roles and op.status == "partial"


# ---------------------------------------------------------------------------
# 6. positives retained: normal owned cleanup is complete and exact
# ---------------------------------------------------------------------------


def test_normal_owned_cleanup_positive_removes_exactly_the_footprint(app_ready):
    op_id, fakes, fn = _completed()
    fakes.control.evaluators["independent"] = {
        "evaluatorName": "independent", "status": "ACTIVE", "evaluatorArn": "arn:i",
        "evaluatorId": "independent"}
    fakes.iam.roles["someone-elses-role"] = {"RoleName": "someone-elses-role", "Arn": "a",
                                             "RoleId": "X", "policies": {}, "trust": "{}",
                                             "Description": "", "Tags": []}
    assert _footprint(fakes, fn) == {"role": True, "log_group": True, "function": True,
                                     "grant": True}
    op = _clean(op_id, fakes)
    assert op.status == "cleaned" and op.error is None, op.error
    assert {r["key"]: r["status"] for r in op.resources
            if r["kind"] not in ("dataset", "existing")} == {
        "lambda_role": "deleted", "log_group": "deleted", "lambda_function": "deleted",
        "lambda_permission": "deleted", "role_grant": "deleted",
        "evaluator:pii": "deleted", "evaluator:tools": "deleted"}
    assert _footprint(fakes, fn) == {"role": False, "log_group": False, "function": False,
                                     "grant": False}
    assert set(fakes.iam.roles) == {"launchpad-agent-execution-role", "someone-elses-role"}
    assert set(fakes.control.evaluators) == {"independent"}
    assert fakes.iam.roles["launchpad-agent-execution-role"]["policies"] == {
        "launchpad-agent-execution": {"Version": "2012-10-17"}}
