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
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
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
    assert [e["outcome"] for e in _res(op, "evaluator:pii")["create_history"]] == ["rejected"]
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


# ---------------------------------------------------------------------------
# 7. consolidated correction: intent history, preserved uncertainty, identity capture
#    before writes, complete snapshots, unqualified-only absence, recorded failures
# ---------------------------------------------------------------------------


class _Crash(BaseException):
    """A process death right after the remote create succeeded."""


def _crash_after(fakes, obj, method, *, only_name=None):
    original = getattr(obj, method)

    def crash(**kw):
        out = original(**kw)
        if only_name is None or kw.get("evaluatorName") == only_name:
            raise _Crash()
        return out

    setattr(obj, method, crash)
    return original


def _run_expect_crash(op_id, fakes):
    with pytest.raises(_Crash):
        _run(op_id, fakes)


def _fence_out_then_restore(op_id, fakes):
    """A restart fenced by a workspace pin change cannot resolve the pending intent."""
    with SessionLocal() as db:
        from app.models.ledger import Workspace

        op = db.get(EvaluationAssetOperation, op_id)
        region = op.pinned["region"]
        ws = db.get(Workspace, op.workspace_id)
        ws.region = "eu-west-1"
        db.commit()
    _run(op_id, fakes)
    with SessionLocal() as db:
        from app.models.ledger import Workspace

        ws = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        ws.region = region
        db.commit()


@pytest.mark.parametrize("kind", ["role", "logs", "function", "evaluator"])
def test_crash_after_remote_create_before_checkpoint_is_an_effect_that_may_exist(
    app_ready, kind
):
    cid, h = _conversation("local-operator")
    fakes = Fakes()
    op_id, *_ = _approve(cid, h, fakes=fakes)
    fn = assets.function_name(op_id)
    obj, method, key = {"role": (fakes.iam, "create_role", "lambda_role"),
                        "logs": (fakes.logs, "create_log_group", "log_group"),
                        "function": (fakes.lam, "create_function", "lambda_function"),
                        "evaluator": (fakes.control, "create_evaluator", "evaluator:tools")}[kind]
    original = _crash_after(fakes, obj, method,
                            only_name="kid_tools" if kind == "evaluator" else None)
    _run_expect_crash(op_id, fakes)
    res = _res(_op(op_id), key)
    assert res["status"] == "pending" and (res.get("intent") or res.get("request")), res
    assert assets._attempted(res)
    setattr(obj, method, original)
    _fence_out_then_restore(op_id, fakes)
    op = _clean(op_id, fakes)
    res = _res(op, key)
    assert op.status != "cleaned", op.error
    assert res["status"] in ("unknown", "retained", "conflict"), res
    if kind == "evaluator":
        assert res["status"] == "unknown" and "no recorded outcome" in res["cleanup"]["note"]
        assert [e["outcome"] for e in res["create_history"]] == ["dispatched"]
    if kind in ("function", "evaluator"):
        assert fn in fakes.iam.roles and f"/aws/lambda/{fn}" in fakes.logs.groups
        assert _res(op, "lambda_role")["status"] == "retained"
    live = {"role": fakes.iam.roles, "logs": fakes.logs.groups, "function": fakes.lam.functions,
            "evaluator": {e["evaluatorName"] for e in fakes.control.evaluators.values()}}[kind]
    assert ({"role": fn, "logs": f"/aws/lambda/{fn}", "function": fn,
             "evaluator": "kid_tools"}[kind]) in live  # the remote effect survives


@pytest.mark.parametrize("code, http", [("AccessDeniedException", 403),
                                        ("ConflictException", 409)])
def test_uncertain_evaluator_create_survives_a_later_rejection(app_ready, code, http):
    cid, h = _conversation("local-operator")
    fakes = Fakes()
    op_id, *_ = _approve(cid, h, fakes=fakes)
    fn = assets.function_name(op_id)
    original = fakes.control.create_evaluator

    def lost(**kw):
        if kw["evaluatorName"] == "kid_tools":
            fakes.control.lose_response_once = True
        return original(**kw)

    fakes.control.create_evaluator = lost
    _run(op_id, fakes)
    ids = set(fakes.control.evaluators)
    op = _clean(op_id, fakes)
    assert _res(op, "evaluator:tools")["status"] == "unknown"

    def reject(**kw):
        raise ClientError({"Error": {"Code": code, "Message": "synthetic"},
                           "ResponseMetadata": {"HTTPStatusCode": http}}, "CreateEvaluator")

    fakes.control.create_evaluator = reject
    _run(op_id, fakes)
    tools = _res(_op(op_id), "evaluator:tools")
    outcomes = [e["outcome"] for e in tools["create_history"]]
    assert outcomes[0] == "lost" and outcomes[-1] == (
        "conflict" if code == "ConflictException" else "rejected"), outcomes
    assert tools["status"] == ("unknown" if code == "ConflictException" else "failed"), tools
    assert assets._uncertain_create(tools)
    op = _clean(op_id, fakes)
    assert op.status != "cleaned", op.error
    assert _res(op, "evaluator:tools")["status"] == "unknown"
    assert set(fakes.control.evaluators) & ids  # the possibly-ours evaluator is alive
    assert fn in fakes.lam.functions and fn in fakes.iam.roles
    for k in CHAIN:
        assert _res(op, k)["status"] == "retained", k


def test_created_function_identity_is_captured_before_any_readback_or_write(app_ready):
    cid, h = _conversation("local-operator")
    fakes = Fakes()
    op_id, *_ = _approve(cid, h, fakes=fakes)
    fn = assets.function_name(op_id)
    original = fakes.lam.get_function_configuration

    def lost(**kw):
        raise ConnectionError("first readback lost")

    fakes.lam.get_function_configuration = lost
    _run(op_id, fakes)
    res = _res(_op(op_id), "lambda_function")
    assert res["status"] == "failed" and res["result"]["revision_id"] == \
        fakes.lam.functions[fn]["cfg"]["RevisionId"]
    assert res["result"]["created_identity"]["FunctionArn"].endswith(f":function:{fn}")
    assert fakes.lam.publish_calls == 0
    # the function is replaced by one with the same downloadable code but a new RevisionId
    # (a re-created function also carries its own LastModified — which is what tells a
    # replacement apart from Lambda's own Pending → Active transition, see SE-049)
    replacement = copy.deepcopy(fakes.lam.functions[fn])
    replacement["cfg"]["RevisionId"] = "replacement-service-identity"
    replacement["cfg"]["LastModified"] = "2026-09-14T09:00:00.000+0000"
    fakes.lam.functions[fn] = replacement
    fakes.lam.get_function_configuration = original
    _run(op_id, fakes)
    res = _res(_op(op_id), "lambda_function")
    assert res["status"] == "conflict" and "RevisionId" in res["error"], res
    assert fakes.lam.publish_calls == 0 and replacement["versions"] == {}
    assert fakes.lam.functions[fn].get("reserved") is None
    assert fakes.lam.policies.get(fn) is None  # no permission written either
    op = _clean(op_id, fakes)
    assert op.status == "partial" and fn in fakes.lam.functions
    assert _res(op, "lambda_function")["status"] == "conflict"
    assert "incomplete" in _res(op, "lambda_function")["cleanup"]["note"]
    assert _res(op, "log_group")["status"] == "retained"


def test_log_group_identity_is_read_before_retention_and_a_recreated_group_is_refused(
    app_ready
):
    cid, h = _conversation("local-operator")
    fakes = Fakes()
    op_id, *_ = _approve(cid, h, fakes=fakes)
    name = f"/aws/lambda/{assets.function_name(op_id)}"
    original = fakes.logs.put_retention_policy

    def failing(**kw):
        raise ConnectionError("retention failed before first identity read")

    fakes.logs.put_retention_policy = failing
    _run(op_id, fakes)
    res = _res(_op(op_id), "log_group")
    assert res["status"] == "failed" and res["result"]["creation_time"] == \
        fakes.logs.groups[name]["creationTime"]
    replacement = copy.deepcopy(fakes.logs.groups[name])
    replacement["creationTime"] = 777777777
    fakes.logs.groups[name] = replacement
    fakes.logs.put_retention_policy = original
    _run(op_id, fakes)
    res = _res(_op(op_id), "log_group")
    assert res["status"] == "conflict" and "re-created" in res["error"], res
    assert "retention" not in fakes.logs.groups[name]  # our retention was never written
    op = _clean(op_id, fakes)
    assert op.status == "partial" and name in fakes.logs.groups
    assert _res(op, "log_group")["status"] == "conflict"


def test_publish_precondition_uses_the_recorded_revision(app_ready):
    cid, h = _conversation("local-operator")
    fakes = Fakes()
    op_id, *_ = _approve(cid, h, fakes=fakes)
    fn = assets.function_name(op_id)
    original = fakes.lam.publish_version
    seen = {}

    def capture(FunctionName, CodeSha256=None, RevisionId=None):
        seen["RevisionId"] = RevisionId
        fakes.lam.functions[FunctionName]["cfg"]["RevisionId"] = "changed-in-window"
        return original(FunctionName, CodeSha256, RevisionId)

    fakes.lam.publish_version = capture
    _run(op_id, fakes)
    res = _res(_op(op_id), "lambda_function")
    assert seen["RevisionId"] and res["status"] == "conflict"
    assert "precondition" in res["error"].lower(), res
    assert fakes.lam.functions[fn]["versions"] == {}


@pytest.mark.parametrize("field, value", [
    ("Timeout", 900),
    ("FunctionArn", "arn:aws:lambda:eu-west-1:999900001111:function:foreign"),
    ("MemorySize", 10240), ("Handler", "other.handler"),
])
def test_first_latest_snapshot_validates_every_approved_field(app_ready, field, value):
    cid, h = _conversation("local-operator")
    fakes = Fakes()
    op_id, *_ = _approve(cid, h, fakes=fakes)
    fn = assets.function_name(op_id)
    original = fakes.lam.get_function

    def drift(FunctionName, Qualifier=None):
        if not Qualifier and fakes.lam.functions[FunctionName]["versions"]:
            fakes.lam.functions[FunctionName]["cfg"][field] = value
        return original(FunctionName, Qualifier)

    fakes.lam.get_function = drift
    _run(op_id, fakes)
    res = _res(_op(op_id), "lambda_function")
    assert res["status"] == "conflict" and field in res["error"], res
    assert "latest_readback" not in res["result"]
    op = _clean(op_id, fakes)
    assert op.status == "partial" and fn in fakes.lam.functions
    assert _res(op, "lambda_function")["status"] == "conflict"
    assert _res(op, "log_group")["status"] == "retained"
    assert _res(op, "lambda_role")["status"] == "retained"


def test_missing_service_revision_never_compares_equal(app_ready):
    cid, h = _conversation("local-operator")
    fakes = Fakes()
    op_id, *_ = _approve(cid, h, fakes=fakes)
    fn = assets.function_name(op_id)
    original = fakes.lam.get_function

    def strip(FunctionName, Qualifier=None):
        d = original(FunctionName, Qualifier)
        d["Configuration"].pop("RevisionId", None)
        return d

    fakes.lam.get_function = strip
    _run(op_id, fakes)
    res = _res(_op(op_id), "lambda_function")
    assert res["status"] == "conflict" and "RevisionId" in res["error"]
    op = _clean(op_id, fakes)
    assert op.status == "partial" and fn in fakes.lam.functions
    assert _res(op, "lambda_function")["status"] == "conflict"
    assert _res(op, "log_group")["status"] == "retained"


@pytest.mark.parametrize("field", ["aliases", "versions"])
def test_missing_recorded_inventory_refuses_delete(app_ready, field):
    op_id, fakes, fn = _completed()
    with SessionLocal() as db:
        op = db.get(EvaluationAssetOperation, op_id)
        resources = json.loads(json.dumps(op.resources))
        next(r for r in resources if r["key"] == "lambda_function")["result"].pop(field)
        op.resources = resources
        db.commit()
    op = _clean(op_id, fakes)
    fnr = _res(op, "lambda_function")
    assert fnr["status"] == "conflict" and "inventory" in fnr["cleanup"]["note"]
    assert fn in fakes.lam.functions and op.status == "partial"
    assert _res(op, "log_group")["status"] == "retained"


def test_reserved_concurrency_drift_retains(app_ready):
    op_id, fakes, fn = _completed()
    fakes.lam.functions[fn]["reserved"] = 100
    op = _clean(op_id, fakes)
    fnr = _res(op, "lambda_function")
    assert fnr["status"] == "conflict" and "concurrency" in fnr["cleanup"]["note"]
    assert fn in fakes.lam.functions and op.status == "partial"


@pytest.mark.parametrize("failure", ["aliases-notfound", "versions-notfound",
                                     "qualified-transport", "versions-page-transport"])
def test_ancillary_failures_never_establish_whole_function_absence(app_ready, failure):
    op_id, fakes, fn = _completed()
    if failure == "aliases-notfound":
        fakes.lam.list_aliases = lambda **kw: (_ for _ in ()).throw(
            ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "ListAliases"))
    elif failure == "versions-notfound":
        fakes.lam.list_versions_by_function = lambda **kw: (_ for _ in ()).throw(
            ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "ListVersions"))
    elif failure == "qualified-transport":
        original = fakes.lam.get_function

        def get(FunctionName, Qualifier=None):
            if Qualifier:
                raise ConnectionError("qualified readback lost")
            return original(FunctionName, Qualifier)

        fakes.lam.get_function = get
    else:
        original = fakes.lam.list_versions_by_function

        def pages(FunctionName, Marker=None):
            if Marker:
                raise ConnectionError("second page lost")
            return {**original(FunctionName=FunctionName), "NextMarker": "second"}

        fakes.lam.list_versions_by_function = pages
    op = _clean(op_id, fakes)
    fnr = _res(op, "lambda_function")
    assert fnr["status"] == "delete_failed", fnr
    assert fn in fakes.lam.functions and fn in fakes.iam.roles
    assert f"/aws/lambda/{fn}" in fakes.logs.groups
    assert _res(op, "log_group")["status"] == "retained"
    assert _res(op, "lambda_role")["status"] == "retained"
    assert op.status == "partial"


@pytest.mark.parametrize("code", ["AccessDeniedException", "ThrottlingException", "transport"])
def test_readback_error_after_delete_function_keeps_dependencies(app_ready, code):
    op_id, fakes, fn = _completed()
    original = fakes.lam.get_function
    pending = []

    def delete(FunctionName, Qualifier=None):
        pending.append(FunctionName)
        return {}

    def get(FunctionName, Qualifier=None):
        if pending:
            if code == "transport":
                raise ConnectionError("lost readback")
            raise ClientError({"Error": {"Code": code}}, "GetFunction")
        return original(FunctionName, Qualifier)

    fakes.lam.delete_function = delete
    fakes.lam.get_function = get
    op = _clean(op_id, fakes)
    assert _res(op, "lambda_function")["status"] == "delete_failed"
    assert _res(op, "log_group")["status"] == "retained"
    assert _res(op, "lambda_role")["status"] == "retained"
    assert fn in fakes.lam.functions and fn in fakes.iam.roles and fakes.logs.groups
    assert op.status == "partial"


@pytest.mark.parametrize("where", ["delete", "readback", "poll"])
def test_evaluator_cleanup_transport_failures_are_recorded_and_retryable(app_ready, where):
    op_id, fakes, fn = _completed()
    original_delete, original_get = fakes.control.delete_evaluator, fakes.control.get_evaluator
    armed = {"on": True}

    def lost_delete(**kw):
        original_delete(**kw)
        raise ConnectionError("delete accepted, reply lost")

    def lost_readback(evaluatorId):
        if armed["on"]:
            raise ConnectionError("readback lost")
        return original_get(evaluatorId)

    def lost_poll(evaluatorId):
        if evaluatorId in fakes.control.deleting and armed["on"]:
            raise ConnectionError("poll lost")
        return original_get(evaluatorId)

    if where == "delete":
        fakes.control.delete_evaluator = lost_delete
    elif where == "readback":
        fakes.control.get_evaluator = lost_readback
    else:
        fakes.control.get_evaluator = lost_poll
    op = _clean(op_id, fakes)  # no exception escapes; the row is not left 'cleaning'
    assert op.status == "partial" and op.worker_token is None, (op.status, op.error)
    statuses = {_res(op, k)["status"] for k in ("evaluator:pii", "evaluator:tools")}
    assert statuses <= {"delete_pending", "delete_failed"}, statuses
    for k in CHAIN + ("role_grant",):
        assert _res(op, k)["status"] == "retained", k
    assert fn in fakes.lam.functions and fn in fakes.iam.roles
    # the transport recovers: the explicit retry finishes without creating anything
    armed["on"] = False
    fakes.control.delete_evaluator = original_delete
    fakes.control.get_evaluator = original_get
    created = fakes.control.create_calls
    op = _clean(op_id, fakes)
    assert op.status == "cleaned", op.error
    assert fakes.control.create_calls == created
    assert fakes.control.evaluators == {} and fakes.lam.functions == {}


def test_unexpected_cleanup_failure_is_recorded_partial_with_released_token(app_ready):
    """A failure outside the per-resource boundaries (here: the client factory itself)
    must not leave the row 'cleaning' with a lease token; it is recorded and retryable."""
    op_id, fakes, fn = _completed()

    def broken_factory(workspace, service):
        if service == "bedrock-agentcore-control":
            raise RuntimeError("synthetic client construction failure")
        return fakes(workspace, service)

    with SessionLocal() as db, pytest.raises(assets.AppError) as excinfo:
        assets.cleanup_operation(db, db.get(EvaluationAssetOperation, op_id), ws_ctx(RESOURCES),
                                 clients=broken_factory, sleeper=lambda s: None)
    assert excinfo.value.code == "assistant.evaluation_assets_cleanup_failed"
    op = _op(op_id)
    assert op.status == "partial" and op.worker_token is None
    assert "cleanup failed" in op.error
    assert fn in fakes.lam.functions and len(fakes.control.evaluators) == 2  # nothing touched
    op = _clean(op_id, fakes)  # retryable once the fault is gone
    assert op.status == "cleaned", op.error


# ---------------------------------------------------------------------------
# 8. final two: demonstrably complete inventories; legacy uncertainty migrated first
# ---------------------------------------------------------------------------


def _pages(key, first, hidden, *, last_page, cycle=False, malformed=None):
    calls = []

    def fetch(FunctionName, Marker=None):
        page = int(Marker or 1)
        calls.append(page)
        if malformed is not None and page == malformed[0]:
            return malformed[1]
        if page == 1:
            return {key: copy.deepcopy(first), "NextMarker": "2"}
        if cycle:
            return {key: [], "NextMarker": "2"}
        if page < last_page:
            return {key: [], "NextMarker": str(page + 1)}
        return {key: copy.deepcopy(hidden)}

    return fetch, calls


@pytest.mark.parametrize("kind", ["versions", "aliases"])
def test_foreign_entry_beyond_the_page_budget_is_an_incomplete_inventory(app_ready, kind):
    op_id, fakes, fn = _completed()
    f = fakes.lam.functions[fn]
    if kind == "versions":
        hidden = [{**f["versions"]["1"], "Version": "2", "FunctionArn": f["cfg"]["FunctionArn"]
                   + ":2", "RevisionId": "foreign"}]
        fetch, calls = _pages("Versions", [f["cfg"], f["versions"]["1"]], hidden, last_page=51)
        fakes.lam.list_versions_by_function = fetch
    else:
        fetch, calls = _pages("Aliases", [], [{"Name": "external", "FunctionVersion": "1"}],
                              last_page=51)
        fakes.lam.list_aliases = fetch
    op = _clean(op_id, fakes)
    fnr = _res(op, "lambda_function")
    assert fnr["status"] == "delete_failed" and "not complete" in fnr["cleanup"]["note"], fnr
    assert len(calls) == assets.INVENTORY_PAGE_BUDGET and fn in fakes.lam.functions
    assert _res(op, "log_group")["status"] == "retained"
    assert _res(op, "lambda_role")["status"] == "retained"
    assert op.status == "partial"


@pytest.mark.parametrize("fault", ["cycle", "missing-collection", "wrong-type", "bad-entry",
                                   "bad-marker", "not-object"])
def test_malformed_or_cyclic_inventory_pages_never_authorize_delete(app_ready, fault):
    op_id, fakes, fn = _completed()
    f = fakes.lam.functions[fn]
    first = [f["cfg"], f["versions"]["1"]]
    if fault == "cycle":
        fetch, _ = _pages("Versions", first, [], last_page=3, cycle=True)
    elif fault == "missing-collection":
        fetch, _ = _pages("Versions", first, [], last_page=2, malformed=(2, {}))
    elif fault == "wrong-type":
        fetch, _ = _pages("Versions", first, [], last_page=2, malformed=(2, {"Versions": None}))
    elif fault == "bad-entry":
        fetch, _ = _pages("Versions", first, [], last_page=2,
                          malformed=(2, {"Versions": [{"FunctionArn": "x"}]}))
    elif fault == "bad-marker":
        fetch, _ = _pages("Versions", first, [], last_page=2,
                          malformed=(1, {"Versions": first, "NextMarker": 7}))
    else:
        fetch, _ = _pages("Versions", first, [], last_page=2, malformed=(2, ["not", "a", "page"]))
    fakes.lam.list_versions_by_function = fetch
    op = _clean(op_id, fakes)
    fnr = _res(op, "lambda_function")
    assert fnr["status"] == "delete_failed" and "_IncompleteInventory" in fnr["cleanup"]["note"]
    assert fn in fakes.lam.functions and op.status == "partial"
    assert _res(op, "log_group")["status"] == "retained"


def test_missing_live_aliases_payload_is_not_an_empty_inventory(app_ready):
    op_id, fakes, fn = _completed()
    fakes.lam.aliases[fn] = [{"Name": "external", "FunctionVersion": "1"}]
    fakes.lam.list_aliases = lambda **kw: {}
    op = _clean(op_id, fakes)
    fnr = _res(op, "lambda_function")
    assert fnr["status"] == "delete_failed" and "no Aliases list" in fnr["cleanup"]["note"]
    assert fn in fakes.lam.functions and op.status == "partial"


def test_complete_multipage_inventories_still_provision_and_clean(app_ready):
    cid, h = _conversation("local-operator")
    fakes = Fakes()
    op_id, *_ = _approve(cid, h, fakes=fakes)
    orig_versions, orig_aliases = fakes.lam.list_versions_by_function, fakes.lam.list_aliases
    reads = {"versions": [], "aliases": []}

    def versions(FunctionName, Marker=None):
        reads["versions"].append(Marker)
        all_v = orig_versions(FunctionName)["Versions"]
        if Marker is None:
            return {"Versions": all_v[:1], "NextMarker": "p2"}
        return {"Versions": all_v[1:]}  # terminal page, no NextMarker

    def aliases(FunctionName, Marker=None):
        reads["aliases"].append(Marker)
        if Marker is None:
            return {"Aliases": [], "NextMarker": "p2"}
        return {"Aliases": orig_aliases(FunctionName)["Aliases"]}  # fully read empty list

    fakes.lam.list_versions_by_function, fakes.lam.list_aliases = versions, aliases
    _run(op_id, fakes)
    op = _op(op_id)
    assert op.status == "succeeded", op.error
    fnr = _res(op, "lambda_function")["result"]
    assert fnr["versions"] == ["$LATEST", "1"] and fnr["aliases"] == []
    assert reads["versions"][:2] == [None, "p2"] and reads["aliases"][:2] == [None, "p2"]
    op = _clean(op_id, fakes)
    assert op.status == "cleaned", op.error
    assert fakes.lam.functions == {} and fakes.logs.groups == {}


def test_incomplete_inventory_at_provisioning_never_marks_ready(app_ready):
    cid, h = _conversation("local-operator")
    fakes = Fakes()
    op_id, *_ = _approve(cid, h, fakes=fakes)
    fn = assets.function_name(op_id)
    fakes.lam.list_aliases = lambda **kw: {  # never reaches a terminal page
        "Aliases": [], "NextMarker": str(int(kw.get("Marker") or 0) + 1)}
    _run(op_id, fakes)
    op = _op(op_id)
    fnr = _res(op, "lambda_function")
    assert fnr["status"] == "failed" and "not complete" in fnr["error"], fnr
    assert "latest_readback" not in fnr["result"] and op.status == "partial"
    assert _res(op, "lambda_permission")["status"] == "blocked"
    assert fn in fakes.lam.functions


def _legacy_lost_create(fakes, op_id):
    """Produce the fc8bdd2-era ledger shape: request persisted, no id, no history, no
    recorded rejection — by running the worker with a lost response and stripping the
    history field this version writes (the old worker wrote none)."""
    original = fakes.control.create_evaluator

    def lost(**kw):
        if kw["evaluatorName"] == "kid_tools":
            fakes.control.lose_response_once = True
        return original(**kw)

    fakes.control.create_evaluator = lost
    _run(op_id, fakes)
    fakes.control.create_evaluator = original
    with SessionLocal() as db:
        op = db.get(EvaluationAssetOperation, op_id)
        resources = json.loads(json.dumps(op.resources))
        r = next(r for r in resources if r["key"] == "evaluator:tools")
        assert r["request"] and not r.get("result")
        r.pop("create_history", None)
        op.resources = resources
        db.commit()
    return set(fakes.control.evaluators)


@pytest.mark.parametrize("code, http", [("AccessDeniedException", 403),
                                        ("ConflictException", 409)])
def test_legacy_uncertain_request_is_migrated_before_the_first_new_dispatch(
    app_ready, code, http
):
    cid, h = _conversation("local-operator")
    fakes = Fakes()
    op_id, *_ = _approve(cid, h, fakes=fakes)
    fn = assets.function_name(op_id)
    ids = _legacy_lost_create(fakes, op_id)
    tokens = []

    def rejected(**kw):
        tokens.append(kw["clientToken"])
        raise ClientError({"Error": {"Code": code, "Message": "reject"},
                           "ResponseMetadata": {"HTTPStatusCode": http}}, "CreateEvaluator")

    fakes.control.create_evaluator = rejected
    _run(op_id, fakes)
    tools = _res(_op(op_id), "evaluator:tools")
    outcomes = [e["outcome"] for e in tools["create_history"]]
    assert outcomes == ["legacy-uncertain", "conflict" if http == 409 else "rejected"], outcomes
    assert len(tokens) == 1 and assets._uncertain_create(tools)
    assert tools["status"] == ("unknown" if http == 409 else "failed")
    for _ in range(2):  # any number of later rejections / cleanups keep the uncertainty
        op = _clean(op_id, fakes)
        assert op.status == "partial" and _res(op, "evaluator:tools")["status"] == "unknown"
        assert ids & set(fakes.control.evaluators) and fn in fakes.lam.functions
        for k in CHAIN:
            assert _res(op, k)["status"] == "retained", k
        _run(op_id, fakes)
    assert fakes.control.create_calls == 2  # the legacy create + nothing new was created
    # the exact token replay answering with a verified identity resolves it
    def replay(**kw):
        return fakes.control.__class__.create_evaluator(fakes.control, **kw)

    fakes.control.create_evaluator = replay
    _run(op_id, fakes)
    tools = _res(_op(op_id), "evaluator:tools")
    assert tools["status"] == "ready" and tools["result"]["evaluator_id"] in ids
    assert op.status != "cleaned"
    op = _clean(op_id, fakes)
    assert op.status == "cleaned", op.error


def test_legacy_migration_survives_a_crash_before_the_response(app_ready):
    cid, h = _conversation("local-operator")
    fakes = Fakes()
    op_id, *_ = _approve(cid, h, fakes=fakes)
    fn = assets.function_name(op_id)
    ids = _legacy_lost_create(fakes, op_id)

    def crash(**kw):
        raise _Crash()

    fakes.control.create_evaluator = crash
    _run_expect_crash(op_id, fakes)
    tools = _res(_op(op_id), "evaluator:tools")
    assert [e["outcome"] for e in tools["create_history"]] == ["legacy-uncertain", "dispatched"]
    _fence_out_then_restore(op_id, fakes)
    op = _clean(op_id, fakes)
    assert op.status == "partial" and _res(op, "evaluator:tools")["status"] == "unknown"
    assert ids & set(fakes.control.evaluators) and fn in fakes.lam.functions
    assert _res(op, "lambda_role")["status"] == "retained"


def test_fresh_first_request_is_not_uncertain(app_ready):
    cid, h = _conversation("local-operator")
    fakes = Fakes()
    op_id, *_ = _approve(cid, h, fakes=fakes)
    original = fakes.control.create_evaluator

    def rejected(**kw):
        if kw["evaluatorName"] != "kid_tools":
            return original(**kw)
        raise ClientError({"Error": {"Code": "ConflictException", "Message": "taken"},
                           "ResponseMetadata": {"HTTPStatusCode": 409}}, "CreateEvaluator")

    fakes.control.create_evaluator = rejected
    _run(op_id, fakes)
    tools = _res(_op(op_id), "evaluator:tools")
    assert [e["outcome"] for e in tools["create_history"]] == ["conflict"]
    assert tools["status"] == "conflict" and not assets._uncertain_create(tools)
    op = _clean(op_id, fakes)
    assert op.status == "cleaned", op.error
