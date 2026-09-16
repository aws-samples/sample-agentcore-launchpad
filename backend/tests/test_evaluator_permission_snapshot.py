"""Permission writes may advance a published Lambda revision, never its ownership."""
# ruff: noqa: F811 — imported fixtures are requested by test parameters

import copy
import json

import pytest

from app.assistant import evaluation_assets as assets
from app.core.db import SessionLocal
from app.models.assistant import EvaluationAssetOperation
from tests.conftest import ws_ctx
from tests.test_evaluation_assets import (  # noqa: F401 — hermetic fixtures by import
    ACCOUNT,
    RESOURCES,
    FakeLambda,
    Fakes,
    _approve,
    _conversation,
    _err,
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


class PermissionLambda(FakeLambda):
    """Advance only the qualified configuration revision on a successful AddPermission."""

    def __init__(self):
        super().__init__()
        self.add_calls = []
        self.policy_revisions = {}
        self.snapshots = {}
        self.before_permission = None
        self.before_add = None
        self.after_add = None

    def get_function(self, **kw):
        return copy.deepcopy(super().get_function(**kw))

    def get_policy(self, FunctionName, Qualifier=None):
        response = super().get_policy(FunctionName, Qualifier)
        response["RevisionId"] = self.policy_revisions.get(FunctionName)
        return response

    def add_permission(self, **kw):
        self.add_calls.append(dict(kw))
        name = kw["FunctionName"]
        if self.before_add:
            self.before_add(name)
        if "RevisionId" in kw and kw["RevisionId"] != self.policy_revisions.get(name):
            raise _err("PreconditionFailedException", "AddPermission")
        super().add_permission(**kw)
        revision = self._revision()
        self.functions[name]["versions"][kw["Qualifier"]]["RevisionId"] = revision
        self.policy_revisions[name] = revision
        if self.after_add:
            self.after_add(name)
        return {"Statement": json.dumps(self.policies[name][-1]),
                "ResponseMetadata": {"RequestId": f"add-{name}"}}


@pytest.fixture(autouse=True)
def record_permission_boundary(monkeypatch):
    original = assets._Runner._step_lambda_permission

    def step(self, db, op, resources, res):
        lam = self._client("lambda")
        fn = assets._resource(resources, "lambda_function", code_group=res.get("code_group"))
        lam.snapshots.setdefault(fn["name"], copy.deepcopy(fn["result"]))
        if lam.before_permission:
            lam.before_permission(fn)
        return original(self, db, op, resources, res)

    monkeypatch.setattr(assets._Runner, "_step_lambda_permission", step)


def _setup(multi=False):
    cid, content_hash = _conversation("local-operator")
    fakes = Fakes()
    fakes.lam = PermissionLambda()
    plan = _valid_plan(cid, content_hash)
    if multi:
        second = copy.deepcopy(plan["evaluators"][-1])
        second.update(key="other", name="other_rules")
        plan["evaluators"].append(second)
    op_id, *_ = _approve(cid, content_hash, plan=plan, fakes=fakes)
    return op_id, fakes


def _chain(op, kind, group=None):
    return assets._resource(op.resources, kind, code_group=group)


def _clean(op_id, fakes):
    with SessionLocal() as db:
        return assets.cleanup_operation(db, db.get(EvaluationAssetOperation, op_id),
                                        ws_ctx(RESOURCES), clients=fakes, sleeper=lambda s: None)


def _retry_permission(op_id):
    with SessionLocal() as db:
        op = db.get(EvaluationAssetOperation, op_id)
        resources = copy.deepcopy(op.resources)
        for resource in resources:
            if resource["kind"] == "lambda_permission":
                resource["status"] = "failed"
        op.resources, op.status, op.worker_token = resources, "partial", None
        db.commit()


def _assert_unsettled(op_id, lam, status="conflict"):
    op = _op(op_id)
    fn = _chain(op, "lambda_function")
    assert _chain(op, "lambda_permission")["status"] == status
    assert fn["result"] == lam.snapshots[fn["name"]]
    assert _res(op, "evaluator:tools")["status"] == "blocked"
    return fn


def _foreign_statement(fn):
    return {"Sid": "existing", "Effect": "Allow", "Action": "lambda:InvokeFunction",
            "Principal": {"AWS": f"arn:aws:iam::{ACCOUNT}:root"},
            "Resource": fn["result"]["version_arn"]}


@pytest.mark.parametrize("multi", [False, True])
def test_own_permission_revision_is_checkpointed_and_cleanup_succeeds(app_ready, multi):
    op_id, fakes = _setup(multi)
    _run(op_id, fakes)
    op = _op(op_id)
    assert op.status == "succeeded", op.error
    functions = [r for r in op.resources if r["kind"] == "lambda_function"]
    assert len(functions) == (2 if multi else 1)
    for fn in functions:
        stored, before = fn["result"], fakes.lam.snapshots[fn["name"]]
        published = fakes.lam.get_function(FunctionName=fn["name"], Qualifier="1")["Configuration"]
        assert published["RevisionId"] != before["readback"]["RevisionId"]
        assert stored["readback"]["RevisionId"] == published["RevisionId"]
        expected = copy.deepcopy(before)
        expected["readback"]["RevisionId"] = published["RevisionId"]
        audit = stored["revision_history"][-1]
        expected.setdefault("revision_history", []).append(audit)
        assert stored == expected  # creation, $LATEST, inventories and all other fields unchanged
        assert audit == {
            "at": audit["at"], "reason": "lambda_permission_added",
            "from": before["readback"]["RevisionId"], "to": published["RevisionId"],
            "qualifier": "1", "statement_id": assets.PERMISSION_SID,
            "policy_revision_before": None, "policy_revision_after": published["RevisionId"],
            "request_id": f"add-{fn['name']}",
        }
    assert all("RevisionId" not in call for call in fakes.lam.add_calls)
    assert _clean(op_id, fakes).status == "cleaned"
    assert not fakes.lam.functions and not fakes.lam.policies


def test_existing_policy_uses_policy_revision_cas_and_preserves_entire_baseline(app_ready):
    op_id, fakes = _setup()

    def existing(fn):
        fakes.lam.policies[fn["name"]] = [_foreign_statement(fn)]
        fakes.lam.policy_revisions[fn["name"]] = "policy-cas-token"

    fakes.lam.before_permission = existing
    _run(op_id, fakes)
    op = _op(op_id)
    assert op.status == "succeeded", op.error
    fn = _chain(op, "lambda_function")
    assert fakes.lam.add_calls[0]["RevisionId"] == "policy-cas-token"
    assert fn["result"]["revision_history"][-1]["policy_revision_before"] == "policy-cas-token"
    assert fakes.lam.snapshots[fn["name"]]["readback"]["RevisionId"] != "policy-cas-token"
    assert fakes.lam.policies[fn["name"]][0] == _foreign_statement(fn)


@pytest.mark.parametrize("target", ["published", "latest"])
@pytest.mark.parametrize("field", assets.FUNCTION_IDENTITY_FIELDS)
def test_every_recorded_identity_field_is_checked_before_write(app_ready, target, field):
    op_id, fakes = _setup()

    def drift(fn):
        live = fakes.lam.functions[fn["name"]]
        cfg = live["versions"]["1"] if target == "published" else live["cfg"]
        cfg[field] = "externally-changed"

    fakes.lam.before_permission = drift
    _run(op_id, fakes)
    _assert_unsettled(op_id, fakes.lam)
    assert not fakes.lam.add_calls


@pytest.mark.parametrize("field", assets.FUNCTION_IDENTITY_FIELDS)
def test_missing_recorded_identity_never_authorizes_write(app_ready, field):
    op_id, fakes = _setup()

    def incomplete(fn):
        fn["result"]["readback"].pop(field)
        fakes.lam.snapshots[fn["name"]] = copy.deepcopy(fn["result"])

    fakes.lam.before_permission = incomplete
    _run(op_id, fakes)
    _assert_unsettled(op_id, fakes.lam)
    assert not fakes.lam.add_calls


@pytest.mark.parametrize(("target", "field", "value"), [
    ("published", "Timeout", 299),
    ("published", "Environment", {"Variables": {"INJECTED": "yes"}}),
    ("published", "LastModified", "externally-changed"),
    ("published", "RevisionId", None),
    ("latest", "RevisionId", "externally-changed"),
    ("latest", "Timeout", 299),
    ("latest", "Environment", {"Variables": {"INJECTED": "yes"}}),
])
def test_post_write_configuration_changes_never_rebase(app_ready, target, field, value):
    op_id, fakes = _setup()

    def drift(name):
        live = fakes.lam.functions[name]
        cfg = live["versions"]["1"] if target == "published" else live["cfg"]
        cfg[field] = value

    fakes.lam.after_add = drift
    _run(op_id, fakes)
    _assert_unsettled(op_id, fakes.lam)
    assert len(fakes.lam.add_calls) == 1


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("change", ["concurrency", "version", "alias"])
def test_concurrency_and_inventory_changes_refused(app_ready, when, change):
    op_id, fakes = _setup()

    def drift(name):
        live = fakes.lam.functions[name]
        if change == "concurrency":
            live["reserved"] = 99
        elif change == "version":
            live["versions"]["2"] = {**live["versions"]["1"], "Version": "2"}
        else:
            fakes.lam.aliases[name] = [{"Name": "external", "FunctionVersion": "1"}]

    if when == "before":
        fakes.lam.before_permission = lambda fn: drift(fn["name"])
    else:
        fakes.lam.after_add = drift
    _run(op_id, fakes)
    _assert_unsettled(op_id, fakes.lam)
    assert len(fakes.lam.add_calls) == (0 if when == "before" else 1)


@pytest.mark.parametrize("change", ["extra", "baseline", "ours", "duplicate", "envelope"])
def test_entire_policy_must_equal_baseline_plus_exact_statement(app_ready, monkeypatch, change):
    op_id, fakes = _setup()
    fakes.lam.before_permission = lambda fn: (
        fakes.lam.policies.update({fn["name"]: [_foreign_statement(fn)]}),
        fakes.lam.policy_revisions.update({fn["name"]: "policy-cas-token"}),
    )

    def drift(name):
        statements = fakes.lam.policies[name]
        if change == "extra":
            statements.append({**statements[0], "Sid": "injected"})
        elif change == "baseline":
            statements[0]["Action"] = "lambda:*"
        elif change == "ours":
            statements[-1]["NotResource"] = "unexpected"
        elif change == "duplicate":
            statements.append(dict(statements[-1]))

    fakes.lam.after_add = drift
    original = fakes.lam.get_policy

    def policy(**kw):
        response = original(**kw)
        if change == "envelope" and fakes.lam.add_calls:
            document = json.loads(response["Policy"])
            document["Id"] = "changed-during-write"
            response["Policy"] = json.dumps(document)
        return response

    monkeypatch.setattr(fakes.lam, "get_policy", policy)
    _run(op_id, fakes)
    _assert_unsettled(op_id, fakes.lam)


@pytest.mark.parametrize("missing", [False, True])
def test_policy_cas_failure_or_missing_revision_refuses_without_rebase(app_ready, missing):
    op_id, fakes = _setup()

    def existing(fn):
        fakes.lam.policies[fn["name"]] = [_foreign_statement(fn)]
        if not missing:
            fakes.lam.policy_revisions[fn["name"]] = "before"

    fakes.lam.before_permission = existing
    fakes.lam.before_add = lambda name: fakes.lam.policy_revisions.update({name: "external"})
    _run(op_id, fakes)
    fn = _assert_unsettled(op_id, fakes.lam)
    assert len(fakes.lam.add_calls) == (0 if missing else 1)
    assert not any(s["Sid"] == assets.PERMISSION_SID for s in fakes.lam.policies[fn["name"]])


def test_matching_sid_without_write_accepts_only_unchanged_identity(app_ready):
    op_id, fakes = _setup()

    def existing(fn):
        fakes.lam.policies[fn["name"]] = [{
            "Sid": assets.PERMISSION_SID, "Effect": "Allow", "Action": "lambda:InvokeFunction",
            "Principal": {"Service": assets.AGENTCORE_PRINCIPAL},
            "Resource": fn["result"]["version_arn"],
            "Condition": {"StringEquals": {"AWS:SourceAccount": ACCOUNT}},
        }]
        fakes.lam.policy_revisions[fn["name"]] = "existing-policy"

    fakes.lam.before_permission = existing
    _run(op_id, fakes)
    op = _op(op_id)
    assert op.status == "succeeded", op.error
    fn = _chain(op, "lambda_function")
    assert fn["result"] == fakes.lam.snapshots[fn["name"]]
    assert not fakes.lam.add_calls


def test_successful_checkpoint_retry_does_not_rewrite_or_add_audit(app_ready):
    op_id, fakes = _setup()
    _run(op_id, fakes)
    before = copy.deepcopy(_chain(_op(op_id), "lambda_function")["result"])
    _retry_permission(op_id)
    _run(op_id, fakes)
    op = _op(op_id)
    assert op.status == "succeeded", op.error
    assert _chain(op, "lambda_function")["result"] == before
    assert len(fakes.lam.add_calls) == 1


@pytest.mark.parametrize("change", ["revision", "configuration", "policy", "policy_revision"])
def test_existing_sid_requires_unchanged_complete_baseline(app_ready, monkeypatch, change):
    op_id, fakes = _setup()
    _run(op_id, fakes)
    fn = _chain(_op(op_id), "lambda_function")
    before = copy.deepcopy(fn["result"])
    _retry_permission(op_id)
    original = fakes.lam.get_policy
    reads = 0

    def concurrent_change(**kw):
        nonlocal reads
        response = original(**kw)
        reads += 1
        if reads == 1:
            name = kw["FunctionName"]
            cfg = fakes.lam.functions[name]["versions"]["1"]
            if change == "revision":
                cfg["RevisionId"] = "external"
            elif change == "configuration":
                cfg["Environment"] = {"Variables": {"INJECTED": "yes"}}
            elif change == "policy":
                fakes.lam.policies[name].append(_foreign_statement(fn))
            else:
                fakes.lam.policy_revisions[name] = "external"
        return response

    monkeypatch.setattr(fakes.lam, "get_policy", concurrent_change)
    _run(op_id, fakes)
    op = _op(op_id)
    assert _chain(op, "lambda_permission")["status"] == "conflict"
    assert _chain(op, "lambda_function")["result"] == before
    assert len(fakes.lam.add_calls) == 1


@pytest.mark.parametrize("failure", ["response_lost", "readback_failed", "checkpoint_lost"])
def test_uncheckpointed_write_is_not_adopted_on_retry(app_ready, monkeypatch, failure):
    op_id, fakes = _setup()
    if failure == "response_lost":
        def lose_response(name):
            raise ConnectionError("AddPermission answer lost")
        fakes.lam.after_add = lose_response
    elif failure == "readback_failed":
        original = fakes.lam.get_policy

        def fail_readback(**kw):
            if fakes.lam.add_calls:
                raise ConnectionError("GetPolicy unavailable")
            return original(**kw)
        monkeypatch.setattr(fakes.lam, "get_policy", fail_readback)
    else:
        original_save = assets._Fence.save

        def lose_checkpoint(self, db, op, resources, event, **fields):
            if event == "lambda_permission:ready":
                raise assets._LeaseLost("crash before checkpoint")
            return original_save(self, db, op, resources, event, **fields)
        monkeypatch.setattr(assets._Fence, "save", lose_checkpoint)
    _run(op_id, fakes)
    fn = _chain(_op(op_id), "lambda_function")
    assert fn["result"] == fakes.lam.snapshots[fn["name"]]
    _retry_permission(op_id)
    _run(op_id, fakes)
    _assert_unsettled(op_id, fakes.lam)
    assert len(fakes.lam.add_calls) == 1
    cleaned = _clean(op_id, fakes)
    assert cleaned.status == "partial"
    assert _chain(cleaned, "lambda_function")["status"] == "conflict"
    assert _chain(cleaned, "log_group")["status"] == "retained"


@pytest.mark.parametrize("historical", [False, True])
def test_cleanup_still_refuses_external_or_historical_revision_drift(app_ready, historical):
    op_id, fakes = _setup()
    _run(op_id, fakes)
    fn = _chain(_op(op_id), "lambda_function")
    if historical:
        with SessionLocal() as db:
            op = db.get(EvaluationAssetOperation, op_id)
            resources = copy.deepcopy(op.resources)
            saved = assets._resource(resources, "lambda_function")
            saved["result"] = copy.deepcopy(fakes.lam.snapshots[fn["name"]])
            op.resources = resources
            db.commit()
    else:
        fakes.lam.functions[fn["name"]]["versions"]["1"]["RevisionId"] = "external-revision"
    op = _clean(op_id, fakes)
    assert op.status == "partial"
    assert _chain(op, "lambda_function")["status"] == "conflict"
    assert "RevisionId" in _chain(op, "lambda_function")["cleanup"]["note"]
    assert _chain(op, "log_group")["status"] == "retained"
    assert _chain(op, "lambda_role")["status"] == "retained"


def test_one_permission_conflict_does_not_rebase_or_block_another_group(app_ready):
    op_id, fakes = _setup(multi=True)
    bad_name = assets.function_name(op_id, "tools")

    def drift(name):
        if name == bad_name:
            fakes.lam.functions[name]["versions"]["1"]["Timeout"] = 299

    fakes.lam.after_add = drift
    _run(op_id, fakes)
    op = _op(op_id)
    assert _chain(op, "lambda_permission", "tools")["status"] == "conflict"
    assert _chain(op, "lambda_permission", "other")["status"] == "ready"
    assert _chain(op, "lambda_function", "tools")["result"] == fakes.lam.snapshots[bad_name]
    good = _chain(op, "lambda_function", "other")
    assert good["result"]["readback"]["RevisionId"] != fakes.lam.snapshots[
        good["name"]]["readback"]["RevisionId"]
    op = _clean(op_id, fakes)
    assert _chain(op, "lambda_function", "tools")["status"] == "conflict"
    assert _chain(op, "lambda_role", "tools")["status"] == "retained"
    assert all(_chain(op, kind, "other")["status"] == "deleted" for kind in assets.CODE_CHAIN)
