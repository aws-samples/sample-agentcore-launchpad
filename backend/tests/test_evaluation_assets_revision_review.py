"""SE-049 — the Lambda first-initialization RevisionId transition and its reviewed
recovery, through the REAL worker / service / route with low-level doubles (Lambda,
IAM, Logs, control plane, CloudTrail LookupEvents) and the temporary test DB. The
CloudTrail record is a SYNTHETIC rendering of the real CreateFunction event shape
(lower-camel members, ``responseElements.state = Pending``) built from the fake's own
state — no private identifiers. Hermetic guards are the ones of
``tests/test_evaluation_assets.py``. Every case asserts the outcome: zero cloud writes on
refusal, the settled RevisionId + CodeSha256 as PublishVersion preconditions on
acceptance, unchanged Dataset / evaluators / versions on replay."""
# ruff: noqa: F811 — the imported fixtures are referenced by test parameters
import fcntl
import json
import os
import threading
from datetime import UTC, datetime
from functools import partial

import pytest
from botocore.exceptions import ClientError

from app.assistant import evaluation_assets as assets
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.evaluation.models import EvalDataset
from app.models.assistant import EvaluationAssetOperation
from app.models.ledger import User, Workspace
from tests.test_evaluation_assets import (  # noqa: F401 — fixtures by import
    ACCOUNT,
    BASE,
    REGION,
    Fakes,
    _approve,
    _conversation,
    _op,
    _res,
    _run,
    app_ready,
    fast,
    gated,
    no_aws_clients,
    no_deploy_no_eval,
    no_network,
)

EVENT_ID = "11111111-2222-4333-8444-555555555555"
REASON = "verified: CreateFunction event matches; only the Pending→Active RevisionId moved"


# ---------------------------------------------------------------------------
# doubles
# ---------------------------------------------------------------------------


class FakeCloudTrail:
    """LookupEvents by EventId: pages of ``Events`` whose ``CloudTrailEvent`` is the JSON
    record. ``raw`` overrides the JSON string (malformed evidence)."""

    def __init__(self):
        self.records: dict[str, list[dict]] = {}
        self.raw: dict[str, str] = {}
        self.calls = 0
        self.never_terminates = False

    def lookup_events(self, LookupAttributes, MaxResults=50, NextToken=None):
        self.calls += 1
        (attr,) = LookupAttributes
        assert attr["AttributeKey"] == "EventId"
        if self.never_terminates:
            return {"Events": [], "NextToken": "more"}
        events = []
        for rec in self.records.get(attr["AttributeValue"], []):
            events.append({"EventId": rec["eventID"], "EventName": rec["eventName"],
                           "EventSource": rec["eventSource"], "ReadOnly": "false",
                           "EventTime": datetime(2026, 9, 14, 6, 21, 38, tzinfo=UTC),
                           "Username": "synthetic", "Resources": [],
                           "CloudTrailEvent": self.raw.get(rec["eventID"], json.dumps(rec))})
        return {"Events": events}


class ReviewFakes(Fakes):
    def __init__(self):
        super().__init__()
        self.trail = FakeCloudTrail()

    def __call__(self, workspace, service_name):
        if service_name == "cloudtrail":
            self.requested.append(service_name)
            return self.trail
        return super().__call__(workspace, service_name)


def _lower(value):
    """CloudTrail's rendering: the first character of every member name lowered."""
    if isinstance(value, dict):
        return {k[:1].lower() + k[1:]: _lower(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_lower(v) for v in value]
    return value


def _set(record: dict, path: str, value) -> None:
    node = record
    parts = path.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    if value is _DEL:
        node.pop(parts[-1], None)
    else:
        node[parts[-1]] = value


_DEL = object()


def _create_event(op_id: str, fakes: ReviewFakes, event_id: str = EVENT_ID, **overrides) -> dict:
    """The synthetic CreateFunction20150331 record for this operation's function, as
    CloudTrail renders it (request + Pending/Creating response with the INITIAL
    RevisionId), registered on the fake trail."""
    res = _res(_op(op_id), "lambda_function")
    request, stored = res["request"], res["result"]
    cfg = fakes.lam.functions[res["name"]]["cfg"]
    response = {k: v for k, v in cfg.items() if k not in ("polls", "LastUpdateStatus")}
    response.update({"RevisionId": stored["created_identity"]["RevisionId"], "State": "Pending",
                     "StateReason": "The function is being created.",
                     "StateReasonCode": "Creating", "Environment": {}})
    record = {
        "eventVersion": "1.11",
        "userIdentity": {"type": "AssumedRole", "principalId": "SYNTHETIC:console",
                         "arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/synthetic/console",
                         "accountId": ACCOUNT},
        "eventTime": "2026-09-14T06:21:38Z", "eventSource": "lambda.amazonaws.com",
        "eventName": "CreateFunction20150331", "awsRegion": REGION,
        "sourceIPAddress": "203.0.113.10", "userAgent": "Boto3/1.43",
        "requestParameters": _lower({k: v for k, v in request.items() if k != "CodeSha256"}),
        "responseElements": _lower(response),
        "requestID": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "eventID": event_id,
        "readOnly": False, "eventType": "AwsApiCall", "managementEvent": True,
        "recipientAccountId": ACCOUNT, "eventCategory": "Management",
    }
    for path, value in overrides.items():
        _set(record, path, value)
    fakes.trail.records.setdefault(event_id, []).append(record)
    return record


# ---------------------------------------------------------------------------
# scenario helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def no_threads(monkeypatch):
    """The review re-queues and launches the worker only after its commit; tests run
    that worker synchronously and deterministically."""
    launched: list[str] = []
    monkeypatch.setattr(assets, "start_async", lambda op_id, **kw: launched.append(op_id) or
                        threading.current_thread())
    return launched


def _blocked(principal="local-operator", **approve_kw):
    """An operation whose function's RevisionId moved Pending → Active: review required."""
    cid, h = _conversation(principal, **{k: v for k, v in approve_kw.items() if k == "owner"})
    fakes = ReviewFakes()
    fakes.lam.activation_revision = "rev-active-0001"
    op_id, *_ = _approve(cid, h, fakes=fakes,
                         **{k: v for k, v in approve_kw.items() if k != "owner"})
    assert _run(op_id, fakes)
    op = _op(op_id)
    assert op.status == "partial", op.error
    res = _res(op, "lambda_function")
    assert res["status"] == "conflict" and "['RevisionId']" in res["error"]
    assert res["review"]["kind"] == "initial_revision_changed"
    assert res["review"]["observed_revision_id"] == "rev-active-0001"
    assert fakes.lam.publish_calls == 0
    return cid, h, op_id, fakes


def _review(op_id, fakes, **overrides):
    op = _op(op_id)
    res = _res(op, "lambda_function")
    stored = res.get("result") or {}
    kwargs = dict(plan_hash=op.plan_hash,
                  expected_created=(stored.get("created_identity") or {}).get("RevisionId"),
                  expected_current=fakes.lam.functions[res["name"]]["cfg"]["RevisionId"],
                  event_id=EVENT_ID, reason=REASON, reviewer="admin", reviewer_user_id=None)
    kwargs.update(overrides)
    with SessionLocal() as db:
        return assets.review_lambda_initial_revision(
            db, db.get(EvaluationAssetOperation, op_id), clients=fakes, **kwargs)


def _refused(op_id, fakes, code, **overrides) -> AppError:
    before = _footprint(op_id, fakes)
    with pytest.raises(AppError) as info:
        _review(op_id, fakes, **overrides)
    assert info.value.code == code, (info.value.code, str(info.value))
    op = _op(op_id)
    res = _res(op, "lambda_function")
    assert res["status"] == "conflict" and not res.get("reviews")  # nothing recorded
    assert op.status == "partial" and _footprint(op_id, fakes) == before  # zero cloud writes
    return info.value


def _footprint(op_id, fakes):
    op = _op(op_id)
    fn = assets.function_name(op_id)
    f = fakes.lam.functions.get(fn) or {}
    with SessionLocal() as db:
        datasets = sorted(d.id for d in db.query(EvalDataset).all())
    return {"publish": fakes.lam.publish_calls, "versions": sorted(f.get("versions") or {}),
            "reserved": f.get("reserved"), "policy": fakes.lam.policies.get(fn),
            "evaluators": sorted(fakes.control.evaluators), "datasets": datasets,
            "dataset_id": op.dataset_id, "create_calls": fakes.lam.create_calls}


def _edit(op_id, mutate) -> None:
    """Simulate a persisted ledger state (legacy rows, unrelated outcomes) — a TEST
    fixture, never a production repair path."""
    with SessionLocal() as db:
        op = db.get(EvaluationAssetOperation, op_id)
        resources = json.loads(json.dumps(op.resources))
        mutate(op, resources)
        op.resources = resources
        db.commit()


# ===========================================================================
# 1. worker: lifecycle snapshot, Active + Successful wait, review-required conflict
# ===========================================================================


def test_unchanged_initial_revision_records_lifecycle_snapshot_and_settles(app_ready):
    cid, h = _conversation("local-operator")
    fakes = ReviewFakes()
    op_id, *_ = _approve(cid, h, fakes=fakes)
    assert _run(op_id, fakes)
    op = _op(op_id)
    assert op.status == "succeeded", op.error
    stored = _res(op, "lambda_function")["result"]
    snap = stored["create_response"]
    assert snap["State"] == "Pending" and snap["StateReasonCode"] == "Creating"
    assert snap["LastModified"] and snap["RevisionId"] == stored["initial_revision_id"]
    assert "LastUpdateStatus" not in snap  # the raw create answer carries none
    assert stored["settled_revision_id"] == stored["initial_revision_id"]
    # the baseline moved with the platform's OWN writes (publish / concurrency) as before,
    # while the create answer stayed untouched
    assert stored["revision_id"] == fakes.lam.functions[assets.function_name(op_id)]["cfg"][
        "RevisionId"]
    assert stored["created_identity"]["RevisionId"] == stored["initial_revision_id"]


@pytest.mark.parametrize("status, outcome", [("InProgress", "failed"), ("Failed", "failed"),
                                             (None, "failed")])
def test_publish_needs_active_and_successful_last_update(app_ready, status, outcome):
    cid, h = _conversation("local-operator")
    fakes = ReviewFakes()
    op_id, *_ = _approve(cid, h, fakes=fakes)
    fn = assets.function_name(op_id)
    original = fakes.lam.get_function_configuration

    def stuck(FunctionName):
        cfg = original(FunctionName)
        if cfg["State"] == "Active":
            cfg["LastUpdateStatus"] = status
            if status is None:
                cfg.pop("LastUpdateStatus")
        return cfg

    fakes.lam.get_function_configuration = stuck
    _run(op_id, fakes)
    res = _res(_op(op_id), "lambda_function")
    assert res["status"] == outcome and "Successful" in res["error"] or status == "Failed"
    assert fakes.lam.publish_calls == 0 and fakes.lam.functions[fn]["versions"] == {}
    assert res.get("review") is None  # an unknown / failed update is NOT the reviewable case


def test_initial_revision_change_is_a_review_required_conflict_not_a_rebase(app_ready):
    cid, h, op_id, fakes = _blocked()
    op = _op(op_id)
    res = _res(op, "lambda_function")
    assert res["owned"] and res["result"]["revision_id"] == res["result"]["initial_revision_id"]
    assert res["result"].get("settled_revision_id") is None
    assert "lambda-revision-review" in res["error"]
    assert {r["key"]: r["status"] for r in op.resources if r["status"] == "blocked"} == {
        "lambda_permission": "blocked", "role_grant": "blocked", "evaluator:tools": "blocked"}
    assert _res(op, "evaluator:pii")["status"] == "ready"  # judges still proceeded
    # an ordinary retry re-evaluates nothing here: the conflict is durable
    with SessionLocal() as db:
        assert assets.retry_operation(db, db.get(EvaluationAssetOperation, op_id)) is not None
    _run(op_id, fakes)
    assert _res(_op(op_id), "lambda_function")["status"] == "conflict"
    assert fakes.lam.publish_calls == 0
    out = assets.operation_out(_op(op_id))
    fn_out = next(r for r in out["resources"] if r["key"] == "lambda_function")
    assert fn_out["review"]["kind"] == "initial_revision_changed"


# ===========================================================================
# 2. the reviewed recovery
# ===========================================================================


def test_verified_review_requeues_and_worker_publishes_with_settled_revision(app_ready,
                                                                             no_threads):
    cid, h, op_id, fakes = _blocked()
    fn = assets.function_name(op_id)
    before = _footprint(op_id, fakes)
    event = _create_event(op_id, fakes)
    initial = _res(_op(op_id), "lambda_function")["result"]["initial_revision_id"]
    outcome = _review(op_id, fakes)
    assert outcome.started and no_threads == [op_id]
    op = _op(op_id)
    assert op.status == "queued" and op.error is None
    res = _res(op, "lambda_function")
    assert res["status"] == "pending" and res["error"] is None
    stored = res["result"]
    assert stored["revision_id"] == "rev-active-0001" == stored["settled_revision_id"]
    assert stored["initial_revision_id"] == initial  # create evidence never overwritten
    assert stored["created_identity"]["RevisionId"] == initial
    assert stored["revision_history"] == [{
        "at": stored["revision_history"][0]["at"], "from": initial, "to": "rev-active-0001",
        "reason": "initial_activation_reviewed", "review_id": outcome.review["id"]}]
    (review,) = res["reviews"]
    assert review == outcome.review
    assert review["reviewer"] == "admin" and review["reason"] == REASON
    assert review["event_id"] == EVENT_ID and review["request_id"] == event["requestID"]
    assert review["plan_hash"] == op.plan_hash and review["operation_id"] == op_id
    assert review["old"]["revision_id"] == initial and review["old"]["status"] == "conflict"
    assert review["new"]["revision_id"] == "rev-active-0001"
    assert review["verified"]["create_state"] == "Pending"
    assert res["review"]["resolved_by"] == review["id"]
    assert "userIdentity" not in json.dumps(assets.operation_out(op))  # no actor leaks
    assert {r["key"] for r in op.resources if r["status"] == "pending"} == {
        "lambda_function", "lambda_permission", "role_grant", "evaluator:tools"}
    assert _footprint(op_id, fakes) == {**before, "publish": 0}  # the review wrote nothing
    assert fakes.trail.calls == 1
    # the ordinary worker resumes; PublishVersion carries the settled RevisionId + digest
    seen = {}
    original = fakes.lam.publish_version

    def capture(FunctionName, CodeSha256=None, RevisionId=None):
        seen.update(RevisionId=RevisionId, CodeSha256=CodeSha256)
        return original(FunctionName, CodeSha256, RevisionId)

    fakes.lam.publish_version = capture
    assert _run(op_id, fakes)
    op = _op(op_id)
    assert op.status == "succeeded", op.error
    assert seen == {"RevisionId": "rev-active-0001",
                    "CodeSha256": _res(op, "lambda_function")["request"]["CodeSha256"]}
    assert all(r["status"] in ("ready", "skipped") for r in op.resources)
    assert list(fakes.lam.functions[fn]["versions"]) == ["1"]
    after = _footprint(op_id, fakes)
    assert after["datasets"] == before["datasets"] and after["dataset_id"] == before["dataset_id"]
    # exactly ONE new evaluator (the released code evaluator); the judges are untouched
    assert set(before["evaluators"]) <= set(after["evaluators"])
    assert len(after["evaluators"]) == len(before["evaluators"]) + 1
    assert after["create_calls"] == before["create_calls"]
    # exact replay: the recorded review, no cloud read, no cloud write
    again = _review(op_id, fakes)
    assert again.review == review and again.started is False
    assert fakes.trail.calls == 1 and _footprint(op_id, fakes) == after
    assert len(_res(_op(op_id), "lambda_function")["reviews"]) == 1


def _mismatch_cases():
    def event(**over):
        return lambda op_id, fakes: _create_event(op_id, fakes, **over)

    def cfg(**fields):
        def apply(op_id, fakes):
            _create_event(op_id, fakes)
            fakes.lam.functions[assets.function_name(op_id)]["cfg"].update(fields)
        return apply

    def extra_version(op_id, fakes):
        _create_event(op_id, fakes)
        f = fakes.lam.functions[assets.function_name(op_id)]
        f["versions"]["1"] = {**f["cfg"], "Version": "1", "RevisionId": "foreign",
                              "FunctionArn": f["cfg"]["FunctionArn"] + ":1"}

    def alias(op_id, fakes):
        _create_event(op_id, fakes)
        fakes.lam.aliases[assets.function_name(op_id)] = [{"Name": "live"}]

    def policy(op_id, fakes):
        _create_event(op_id, fakes)
        fakes.lam.policies[assets.function_name(op_id)] = [{"Sid": "someone"}]

    def reserved(op_id, fakes):
        _create_event(op_id, fakes)
        fakes.lam.functions[assets.function_name(op_id)]["reserved"] = 1

    def role_changed(op_id, fakes):
        _create_event(op_id, fakes)
        fakes.iam.roles[assets.function_name(op_id)]["RoleId"] = "AROAREPLACED"

    def log_recreated(op_id, fakes):
        _create_event(op_id, fakes)
        fakes.logs.groups[f"/aws/lambda/{assets.function_name(op_id)}"]["creationTime"] = 1

    def two_events(op_id, fakes):
        _create_event(op_id, fakes)
        _create_event(op_id, fakes)

    def malformed(op_id, fakes):
        _create_event(op_id, fakes)
        fakes.trail.raw[EVENT_ID] = "{not json"

    def other_function(op_id, fakes):
        rec = _create_event(op_id, fakes)
        rec["requestParameters"]["functionName"] = "launchpad-evalfn-other"
        rec["responseElements"]["functionName"] = "launchpad-evalfn-other"
        rec["responseElements"]["functionArn"] = rec["responseElements"]["functionArn"].replace(
            assets.function_name(op_id), "launchpad-evalfn-other")

    U = "assistant.lambda_revision_review_unverified"
    return {
        "no_event": (lambda op_id, fakes: None, U),
        "two_events": (two_events, U),
        "malformed_record": (malformed, U),
        "unrelated_event_name": (event(eventName="UpdateFunctionConfiguration20150331v2"), U),
        "other_source": (event(eventSource="iam.amazonaws.com"), U),
        "failed_call": (event(errorCode="AccessDenied"), U),
        "read_only": (event(readOnly=True), U),
        "other_account": (event(recipientAccountId="999988887777"), U),
        "other_region": (event(awsRegion="eu-west-1"), U),
        "other_function": (other_function, U),
        "other_arn": (event(**{"responseElements.functionArn":
                               f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:other"}), U),
        "other_created_revision_in_response": (event(**{"responseElements.revisionId": "x"}), U),
        "other_code_sha": (event(**{"responseElements.codeSha256": "AAAA"}), U),
        "response_not_pending": (event(**{"responseElements.state": "Active",
                                          "responseElements.stateReasonCode": _DEL}), U),
        "no_last_modified": (event(**{"responseElements.lastModified": _DEL}), U),
        "last_modified_moved": (cfg(LastModified="2026-09-14T09:00:00.000+0000"), U),
        "other_role_in_request": (event(**{"requestParameters.role": "arn:aws:iam::1:role/x"}), U),
        "published_in_request": (event(**{"requestParameters.publish": True}), U),
        "other_tags": (event(**{"requestParameters.tags": {"launchpad:managed": "true"}}), U),
        "environment_added": (cfg(Environment={"Variables": {"DEBUG": "1"}}), U),
        "layers_added": (cfg(Layers=[{"Arn": "arn:aws:lambda:x:1:layer:l:1"}]), U),
        "vpc_added": (cfg(VpcConfig={"SubnetIds": ["subnet-1"], "SecurityGroupIds": ["sg-1"]}),
                      U),
        "kms_added": (cfg(KMSKeyArn=f"arn:aws:kms:{REGION}:{ACCOUNT}:key/k"), U),
        "dead_letter_added": (cfg(DeadLetterConfig={"TargetArn": "arn:aws:sqs:x:1:q"}), U),
        "architecture_changed": (cfg(Architectures=["arm64"]), U),
        "tracing_changed": (cfg(TracingConfig={"Mode": "Active"}), U),
        "timeout_changed": (cfg(Timeout=299), U),
        "role_changed_on_function": (cfg(Role=f"arn:aws:iam::{ACCOUNT}:role/other"), U),
        "update_in_progress": (cfg(LastUpdateStatus="InProgress"), U),
        "update_failed": (cfg(LastUpdateStatus="Failed"), U),
        "not_active": (cfg(State="Inactive"), U),
        "extra_version": (extra_version, U),
        "extra_alias": (alias, U),
        "resource_policy_present": (policy, U),
        "reserved_concurrency_present": (reserved, U),
        "role_identity_changed": (role_changed, U),
        "log_group_recreated": (log_recreated, U),
    }


@pytest.mark.parametrize("case", sorted(_mismatch_cases()))
def test_review_fails_closed_on_every_mismatch(app_ready, no_threads, case):
    arrange, code = _mismatch_cases()[case]
    cid, h, op_id, fakes = _blocked()
    arrange(op_id, fakes)
    _refused(op_id, fakes, code)
    assert no_threads == []


def test_review_refuses_wrong_bindings_before_any_cloud_read(app_ready, no_threads):
    cid, h, op_id, fakes = _blocked()
    _create_event(op_id, fakes)
    current = fakes.lam.functions[assets.function_name(op_id)]["cfg"]["RevisionId"]
    _refused(op_id, fakes, "assistant.evaluation_plan_stale", plan_hash="f" * 64)
    _refused(op_id, fakes, "assistant.lambda_revision_review_stale", expected_created="rev-zzz")
    _refused(op_id, fakes, "assistant.lambda_revision_review_stale",
             expected_created=current)  # created == current: no transition
    err = _refused(op_id, fakes, "assistant.lambda_revision_review_reason_required", reason="  ")
    assert err.status_code == 422
    assert fakes.trail.calls == 0
    _refused(op_id, fakes, "assistant.lambda_revision_review_unverified",
             expected_current="rev-not-the-current-one")
    assert fakes.trail.calls == 1 and no_threads == []


def test_review_is_not_applicable_to_lost_creates_ordinary_drift_or_unrelated_conflicts(
    app_ready, no_threads
):
    # (a) a lost CreateFunction response: unknown, no recorded identity → never reviewable
    cid, h = _conversation("local-operator")
    fakes = ReviewFakes()
    fakes.lam.lose_create_response = True
    op_id, *_ = _approve(cid, h, fakes=fakes)
    _run(op_id, fakes)
    res = _res(_op(op_id), "lambda_function")
    assert res["status"] == "unknown" and not res.get("result")
    with pytest.raises(AppError) as info:
        _review(op_id, fakes, expected_created="rev-0001", expected_current="rev-0002")
    assert info.value.code == "assistant.lambda_revision_review_not_applicable"
    # (b) ordinary drift after the platform re-pinned the baseline (a version exists)
    cid, h, op_id, fakes = _blocked()
    _create_event(op_id, fakes)

    def repinned(op, resources):
        fn = next(r for r in resources if r["key"] == "lambda_function")
        fn["result"]["revision_id"] = "rev-repinned-after-publish"
        fn["result"]["version"] = "1"
    _edit(op_id, repinned)
    _refused(op_id, fakes, "assistant.lambda_revision_review_not_applicable")
    # (c) a publish intent was already recorded: not the pre-publish initialization case
    cid, h, op_id, fakes = _blocked()
    _create_event(op_id, fakes)
    _edit(op_id, lambda op, rs: next(r for r in rs if r["key"] == "lambda_function")["result"]
          .__setitem__("publish_requested_at", "2026-09-14T06:30:00+00:00"))
    _refused(op_id, fakes, "assistant.lambda_revision_review_not_applicable")
    # (d) an unrelated open outcome (a judge in conflict) must be resolved first
    cid, h, op_id, fakes = _blocked()
    _create_event(op_id, fakes)
    _edit(op_id, lambda op, rs: next(r for r in rs if r["key"] == "evaluator:pii")
          .update(status="conflict", error="name taken by a foreign evaluator"))
    _refused(op_id, fakes, "assistant.lambda_revision_review_not_applicable")
    # (e) a different recorded conflict text (not the pre-publish RevisionId drift)
    cid, h, op_id, fakes = _blocked()
    _create_event(op_id, fakes)
    _edit(op_id, lambda op, rs: next(r for r in rs if r["key"] == "lambda_function")
          .update(review=None, error="$LATEST differs from the approved identity before "
                                     "concurrency on ['RevisionId'] — refusing"))
    _refused(op_id, fakes, "assistant.lambda_revision_review_not_applicable")
    assert no_threads == []


def test_review_refuses_live_worker_foreign_lock_revoked_approver_and_drifted_workspace(
    app_ready, no_threads
):
    cid, h, op_id, fakes = _blocked()
    _create_event(op_id, fakes)
    # a live in-process worker
    alive = threading.Thread(target=lambda: threading.Event().wait(0.5))
    alive.start()
    assets._LIVE[op_id] = alive
    try:
        _refused(op_id, fakes, "assistant.evaluation_assets_running")
    finally:
        alive.join()
        assets._LIVE.pop(op_id, None)
    # the host lock held by another process/thread
    fd = os.open(assets._lock_path(op_id), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        _refused(op_id, fakes, "assistant.evaluation_assets_running")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    # exhausted attempts
    with SessionLocal() as db:
        db.get(EvaluationAssetOperation, op_id).attempts = assets.MAX_ATTEMPTS
        db.commit()
    _refused(op_id, fakes, "assistant.evaluation_assets_exhausted")
    with SessionLocal() as db:
        db.get(EvaluationAssetOperation, op_id).attempts = 1
        db.commit()
    # workspace identity drift (pinned region)
    with SessionLocal() as db:
        db.get(Workspace, DEFAULT_WORKSPACE_ID).region = "eu-central-1"
        db.commit()
    _refused(op_id, fakes, "assistant.evaluation_assets_stopped")
    with SessionLocal() as db:
        db.get(Workspace, DEFAULT_WORKSPACE_ID).region = REGION
        db.commit()
    # the recheck callback (caller re-resolved inside the write) can veto
    with pytest.raises(AppError) as info:
        _review(op_id, fakes, recheck=lambda db: (_ for _ in ()).throw(
            AppError("auth.admin_required", "demoted", status_code=403)))
    assert info.value.status_code == 403
    assert _res(_op(op_id), "lambda_function")["status"] == "conflict"
    assert fakes.lam.publish_calls == 0 and no_threads == []
    # a registered approver that is disabled meanwhile
    with SessionLocal() as db:
        user = User(username="approver", username_key="approver", email="a@example.com",
                    password_hash="x", role="admin", status="active")
        db.add(user)
        db.commit()
        uid = user.id
    cid2, h2 = _conversation("local-operator")
    fakes2 = ReviewFakes()
    fakes2.lam.activation_revision = "rev-active-0002"
    op2, *_ = _approve(cid2, h2, fakes=fakes2, approver_user_id=uid)
    _run(op2, fakes2)
    assert _res(_op(op2), "lambda_function")["status"] == "conflict"
    _create_event(op2, fakes2)
    with SessionLocal() as db:
        db.get(User, uid).status = "disabled"
        db.commit()
    _refused(op2, fakes2, "assistant.evaluation_assets_stopped")


def test_second_different_review_is_refused_and_later_drift_fails_publish_precondition(
    app_ready, no_threads
):
    cid, h, op_id, fakes = _blocked()
    fn = assets.function_name(op_id)
    _create_event(op_id, fakes)
    first = _review(op_id, fakes)
    # $LATEST moves AGAIN between the review and the worker's publish: precondition
    original = fakes.lam.publish_version

    def race(FunctionName, CodeSha256=None, RevisionId=None):
        fakes.lam.functions[FunctionName]["cfg"]["RevisionId"] = "rev-after-review"
        return original(FunctionName, CodeSha256, RevisionId)

    fakes.lam.publish_version = race
    _run(op_id, fakes)
    res = _res(_op(op_id), "lambda_function")
    assert res["status"] == "conflict" and "PreconditionFailed" in res["error"] \
        or "RevisionId precondition" in res["error"]
    assert fakes.lam.functions[fn]["versions"] == {} and fakes.lam.functions[fn].get(
        "reserved") is None
    assert res["result"]["initial_revision_id"] != res["result"]["revision_id"]
    # the moved function is NOT reviewable again: neither with the old event + new revision
    # (a different review) nor as a fresh initialization (baseline already reviewed)
    fakes.lam.publish_version = original
    with pytest.raises(AppError) as info:
        _review(op_id, fakes, expected_current="rev-after-review")
    assert info.value.code == "assistant.lambda_revision_review_stale"
    with pytest.raises(AppError) as info:
        _review(op_id, fakes, expected_created="rev-active-0001",
                expected_current="rev-after-review")
    assert info.value.code == "assistant.lambda_revision_review_stale"
    assert _res(_op(op_id), "lambda_function")["reviews"] == [first.review]
    assert fakes.lam.publish_calls == 1


def test_post_review_drift_before_the_worker_reads_back_stays_a_conflict(app_ready,
                                                                        no_threads):
    cid, h, op_id, fakes = _blocked()
    _create_event(op_id, fakes)
    _review(op_id, fakes)
    fakes.lam.functions[assets.function_name(op_id)]["cfg"]["RevisionId"] = "rev-moved-later"
    _run(op_id, fakes)
    res = _res(_op(op_id), "lambda_function")
    assert res["status"] == "conflict" and "RevisionId" in res["error"]
    assert res["result"]["revision_id"] == "rev-active-0001"  # the reviewed baseline stays
    assert res["review"]["resolved_by"]  # the ORIGINAL marker is not re-armed
    assert fakes.lam.publish_calls == 0
    with pytest.raises(AppError) as info:
        _review(op_id, fakes, expected_current="rev-moved-later")
    assert info.value.code == "assistant.lambda_revision_review_stale"


def test_crash_after_the_review_commit_is_resumed_by_startup_or_retry(app_ready, no_threads):
    cid, h, op_id, fakes = _blocked()
    _create_event(op_id, fakes)
    _review(op_id, fakes)  # the worker launch is stubbed: as if the host died right here
    assert _op(op_id).status == "queued"
    no_threads.clear()
    assert assets.resume_operations() == [op_id]
    assert _run(op_id, fakes)
    assert _op(op_id).status == "succeeded"


def test_legacy_record_without_lifecycle_snapshot_needs_the_positive_event(app_ready,
                                                                           no_threads):
    """An operation accepted before SE-049 stored only the identity (RevisionId, no
    State / LastModified / request id, no review marker) and the pre-publish error text."""
    cid, h, op_id, fakes = _blocked()

    def legacy(op, resources):
        fn = next(r for r in resources if r["key"] == "lambda_function")
        for key in ("initial_revision_id", "create_response", "request_id"):
            fn["result"].pop(key, None)
        fn.pop("review", None)
        fn["error"] = ("$LATEST differs from the approved identity before publish on "
                       "['RevisionId'] — the function was changed or replaced; refusing to "
                       "continue")
    _edit(op_id, legacy)
    # the current RevisionId alone is no proof: without the event the review fails closed
    _refused(op_id, fakes, "assistant.lambda_revision_review_unverified")
    # an event whose response names another initial RevisionId is not ours
    _create_event(op_id, fakes, event_id="other-event", **{"responseElements.revisionId": "zz"})
    _refused(op_id, fakes, "assistant.lambda_revision_review_unverified", event_id="other-event")
    _create_event(op_id, fakes)
    outcome = _review(op_id, fakes)
    assert outcome.started
    res = _res(_op(op_id), "lambda_function")
    assert "create_response" not in res["result"]  # nothing invented for the legacy row
    assert res["result"]["revision_id"] == "rev-active-0001"
    assert _run(op_id, fakes) and _op(op_id).status == "succeeded"


def test_existing_cleanup_and_unknown_guards_unchanged_for_the_reviewable_conflict(app_ready):
    cid, h, op_id, fakes = _blocked()
    fn = assets.function_name(op_id)
    with SessionLocal() as db:
        from tests.conftest import ws_ctx
        from tests.test_evaluation_assets import RESOURCES
        op = assets.cleanup_operation(db, db.get(EvaluationAssetOperation, op_id),
                                      ws_ctx(RESOURCES), clients=fakes, sleeper=lambda s: None)
    assert op.status == "partial" and fn in fakes.lam.functions
    res = _res(op, "lambda_function")
    assert res["status"] == "conflict" and "incomplete" in res["cleanup"]["note"]
    assert _res(op, "log_group")["status"] == "retained"
    assert res["review"]["kind"] == "initial_revision_changed"  # still reviewable after


# ===========================================================================
# 3. the route
# ===========================================================================


def _route(cid, op_id):
    return f"{BASE}/conversations/{cid}/evaluation-plan/operations/{op_id}/lambda-revision-review"


def test_route_admin_owner_reviews_member_and_foreign_are_refused(gated, monkeypatch,
                                                                  no_threads):
    admin, member, member_id = gated
    cid, h, op_id, fakes = _blocked(principal="config-admin", owner="admin")
    _create_event(op_id, fakes)
    monkeypatch.setattr(assets, "review_lambda_initial_revision",
                        partial(assets.review_lambda_initial_revision, clients=fakes))
    op = _op(op_id)
    res = _res(op, "lambda_function")
    body = {"plan_hash": op.plan_hash,
            "expected_created_revision_id": res["result"]["initial_revision_id"],
            "expected_current_revision_id": "rev-active-0001",
            "cloudtrail_event_id": EVENT_ID, "reason": REASON}
    assert member.post(_route(cid, op_id), json=body).status_code == 403
    assert admin.post(_route(cid, op_id), json={**body, "event": {"x": 1}}).status_code == 422
    assert admin.post(_route(cid, op_id), json={**body, "reason": ""}).status_code == 422
    r = admin.post(_route(cid, op_id), json={**body, "plan_hash": "f" * 64})
    assert r.status_code == 409 and r.json()["code"] == "assistant.evaluation_plan_stale"
    r = admin.post(_route(cid, op_id), json=body)
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["started"] is True and payload["review"]["reviewer"] == "admin"
    assert payload["operation"]["status"] == "queued"
    fn_out = next(x for x in payload["operation"]["resources"] if x["key"] == "lambda_function")
    assert fn_out["reviews"][0]["event_id"] == EVENT_ID and "userIdentity" not in r.text
    r = admin.post(_route(cid, op_id), json=body)  # exact replay
    assert r.status_code == 200 and r.json()["started"] is False
    assert r.json()["review"] == payload["review"] and fakes.trail.calls == 1
    assert _run(op_id, fakes) and _op(op_id).status == "succeeded"
    # a member-owned conversation is invisible to the administrator (404, no review)
    cid2, h2 = _conversation(f"user:{member_id}", owner="member")
    fakes2 = ReviewFakes()
    fakes2.lam.activation_revision = "rev-active-0009"
    op2, *_ = _approve(cid2, h2, fakes=fakes2)
    _run(op2, fakes2)
    assert admin.post(_route(cid2, op2), json=body).status_code == 404
    assert member.post(_route(cid2, op2), json=body).status_code == 403
    assert _res(_op(op2), "lambda_function")["status"] == "conflict"


def test_route_policy_names_the_review_route_admin():
    from app.core.route_policy import ADMIN, ROUTE_POLICY
    key = ("POST", "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/"
                   "operations/{operation_id}/lambda-revision-review")
    assert ROUTE_POLICY[key] == ADMIN


def test_cloudtrail_casing_is_rebuilt_from_the_model_and_data_maps_stay_lossless():
    shape = assets._lambda_model().shape_for("FunctionConfiguration")
    trail = {"kMSKeyArn": "k", "tracingConfig": {"mode": "PassThrough"},
             "environment": {"variables": {"Token": "x", "FLAG": ""}},
             "loggingConfig": {"logFormat": "Text"}, "durableConfig": {"executionTimeout": 6},
             "someFutureMember": {"x": 1}}
    sdk = assets._from_cloudtrail(trail, shape)
    assert sdk["KMSKeyArn"] == "k" and sdk["TracingConfig"] == {"Mode": "PassThrough"}
    assert sdk["Environment"] == {"Variables": {"Token": "x", "FLAG": ""}}  # keys/values verbatim
    assert sdk["LoggingConfig"] == {"LogFormat": "Text"}
    assert sdk["DurableConfig"] == {"ExecutionTimeout": 6}  # a modelled member folds …
    assert sdk["someFutureMember"] == {"x": 1}  # … an unmodelled one is kept verbatim (refused)
    # data-map case and empty values are content: never equal to a different rendering
    fold = assets._fold_envelopes
    other = assets._from_cloudtrail({"environment": {"variables": {"token": "x"}}}, shape)
    assert fold(sdk)["Environment"] != fold(other)["Environment"]
    absent = assets._from_cloudtrail({"environment": {"variables": {"Token": "x"}}}, shape)
    assert fold(sdk)["Environment"] != fold(absent)["Environment"]
    # the ONLY envelope equivalence: an empty Environment / Layers / VpcConfig is absent
    assert assets._fold_envelopes({"Environment": {}, "Layers": [], "VpcConfig": {
        "SubnetIds": [], "SecurityGroupIds": [], "VpcId": ""}}) == {}
    assert "Environment" in assets._fold_envelopes({"Environment": {"Variables": {"A": ""}}})


# ===========================================================================
# 4. correction pass 2 — host acceptance findings as regressions
# ===========================================================================


def _route_case(gated, monkeypatch, **blocked_kw):
    """Actual route + service + worker; only the low-level AWS client returns are faked."""
    from app.services import aws_clients
    admin, member, member_id = gated
    cid, h, op_id, fakes = _blocked(principal="config-admin", owner="admin", **blocked_kw)
    event = _create_event(op_id, fakes)
    monkeypatch.setattr(aws_clients, "client", lambda service, ctx, **kw: fakes(ctx, service))
    op = _op(op_id)
    body = {"plan_hash": op.plan_hash,
            "expected_created_revision_id": _res(op, "lambda_function")["result"][
                "initial_revision_id"],
            "expected_current_revision_id": "rev-active-0001",
            "cloudtrail_event_id": EVENT_ID, "reason": REASON}
    return admin, member, cid, op_id, fakes, event, body


def _closed(client, cid, op_id, fakes, body, code=None):
    r = client.post(_route(cid, op_id), json=body)
    assert r.status_code in (401, 403, 404, 409, 422), r.text[:300]
    if code:
        assert r.json()["code"] == code, r.text[:300]
    assert not _res(_op(op_id), "lambda_function").get("reviews")
    assert _op(op_id).status == "partial" and fakes.lam.publish_calls == 0
    return r


@pytest.mark.parametrize("field, value", [
    ("DurableConfig", {"ExecutionTimeout": 600}),
    ("TenancyConfig", {"TenantIsolationMode": "PER_TENANT"}),
    ("CapacityProviderConfig", {"LambdaManagedInstancesCapacityProviderConfig": {
        "CapacityProviderArn": f"arn:aws:lambda:{REGION}:{ACCOUNT}:capacity-provider:t"}}),
    ("MasterArn", f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:foreign"),
    ("SomeFutureMember", {"x": 1}),
])
def test_route_refuses_any_extra_current_member_even_when_unknown_to_the_platform(
    gated, monkeypatch, no_threads, field, value
):
    admin, member, cid, op_id, fakes, event, body = _route_case(gated, monkeypatch)
    fakes.lam.functions[assets.function_name(op_id)]["cfg"][field] = value
    r = _closed(admin, cid, op_id, fakes, body, "assistant.lambda_revision_review_unverified")
    assert field in r.json()["detail"]["fields"]


@pytest.mark.parametrize("which", ["request_extra_environment", "response_and_current_env",
                                   "description_not_approved", "snapshot_architecture",
                                   "env_key_case", "env_empty_value", "code_size"])
def test_route_compares_request_snapshot_event_and_current_losslessly(gated, monkeypatch,
                                                                       no_threads, which):
    admin, member, cid, op_id, fakes, event, body = _route_case(gated, monkeypatch)
    cfg = fakes.lam.functions[assets.function_name(op_id)]["cfg"]
    if which == "request_extra_environment":
        event["requestParameters"]["environment"] = {"variables": {"EXTRA": "1"}}
    if which == "response_and_current_env":  # equal on both sides, absent from the snapshot
        event["responseElements"]["environment"] = {"variables": {"EXTRA": "1"}}
        cfg["Environment"] = {"Variables": {"EXTRA": "1"}}
    if which == "description_not_approved":
        event["responseElements"]["description"] = "not what the plan approved"
        cfg["Description"] = "not what the plan approved"
    if which == "snapshot_architecture":
        event["responseElements"]["architectures"] = ["arm64"]
        cfg["Architectures"] = ["arm64"]
    if which == "env_key_case":  # a data-map key differs only by case: never equal
        event["responseElements"]["environment"] = {"variables": {"Token": "x"}}
        cfg["Environment"] = {"Variables": {"token": "x"}}
    if which == "env_empty_value":  # an empty variable is content, not absence
        event["responseElements"]["environment"] = {"variables": {"FLAG": ""}}
        cfg["Environment"] = {"Variables": {}}
    if which == "code_size":
        cfg["CodeSize"] = cfg["CodeSize"] + 1
    _closed(admin, cid, op_id, fakes, body, "assistant.lambda_revision_review_unverified")


def test_legacy_record_without_snapshot_still_refuses_unapproved_members(gated, monkeypatch,
                                                                         no_threads):
    """A pre-SE-049 row has no ``create_response``: defaults come from the reviewed request
    and the documented service defaults, never from the current function."""
    admin, member, cid, op_id, fakes, event, body = _route_case(gated, monkeypatch)
    _edit(op_id, lambda op, rs: [next(r for r in rs if r["key"] == "lambda_function")["result"]
                                 .pop(k, None) for k in ("initial_revision_id", "create_response",
                                                         "request_id")])
    cfg = fakes.lam.functions[assets.function_name(op_id)]["cfg"]
    event["responseElements"]["architectures"] = ["arm64"]
    cfg["Architectures"] = ["arm64"]
    _closed(admin, cid, op_id, fakes, body, "assistant.lambda_revision_review_unverified")
    event["responseElements"]["architectures"] = ["x86_64"]
    cfg["Architectures"] = ["x86_64"]
    assert admin.post(_route(cid, op_id), json=body).status_code == 200


@pytest.mark.parametrize("which", ["trust", "inline_policy", "role_tags", "retention",
                                   "log_tags", "malformed_policy", "empty_policy_document"])
def test_route_recompares_dependency_configuration_and_proves_policy_absence(
    gated, monkeypatch, no_threads, which
):
    admin, member, cid, op_id, fakes, event, body = _route_case(gated, monkeypatch)
    name = assets.function_name(op_id)
    if which == "trust":
        fakes.iam.roles[name]["trust"] = json.dumps({"Statement": [{
            "Effect": "Allow", "Principal": "*", "Action": "sts:AssumeRole"}]})
    if which == "inline_policy":
        fakes.iam.roles[name]["policies"][assets.LOGS_POLICY_NAME] = {
            "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}
    if which == "role_tags":
        fakes.iam.roles[name]["Tags"] = [{"Key": "launchpad:managed", "Value": "true"}]
    if which == "retention":
        fakes.logs.groups[f"/aws/lambda/{name}"]["retention"] = 3653
    if which == "log_tags":
        fakes.logs.groups[f"/aws/lambda/{name}"]["tags"] = {}
    if which == "malformed_policy":
        fakes.lam.get_policy = lambda **kw: {"Policy": "not-json"}
    if which == "empty_policy_document":  # only NotFound proves absence
        fakes.lam.get_policy = lambda **kw: {"Policy": json.dumps({"Statement": []})}
    _closed(admin, cid, op_id, fakes, body, "assistant.lambda_revision_review_unverified")


@pytest.mark.parametrize("which", ["owner", "operation_owner", "plan_hash", "plan_content",
                                   "plan_revision_superseded", "workspace"])
def test_route_binding_changed_during_the_cloud_read_is_refused(gated, monkeypatch, no_threads,
                                                                which):
    from app.models.assistant import AssistantConversation, AssistantEvaluationPlan
    admin, member, cid, op_id, fakes, event, body = _route_case(gated, monkeypatch)
    original = fakes.trail.lookup_events

    def race(**kw):
        with SessionLocal() as db:
            op = db.get(EvaluationAssetOperation, op_id)
            plan = db.get(AssistantEvaluationPlan, op.plan_id)
            if which == "owner":
                db.get(AssistantConversation, cid).owner_principal = "user:different"
            if which == "operation_owner":
                op.owner_principal = "user:different"
            if which == "plan_hash":
                plan.content_hash = "e" * 64
            if which == "plan_content":
                content = json.loads(json.dumps(plan.content))
                content["dataset"]["description"] = "changed after approval"
                plan.content = content
            if which == "plan_revision_superseded":
                plan.status = "superseded"
            if which == "workspace":
                db.get(Workspace, DEFAULT_WORKSPACE_ID).region = "eu-central-1"
            db.commit()
        return original(**kw)

    fakes.trail.lookup_events = race
    try:
        r = admin.post(_route(cid, op_id), json=body)
        assert r.status_code in (404, 409), r.text[:300]
        assert not _res(_op(op_id), "lambda_function").get("reviews")
        assert _op(op_id).status == "partial" and fakes.lam.publish_calls == 0
    finally:
        with SessionLocal() as db:
            db.get(Workspace, DEFAULT_WORKSPACE_ID).region = REGION
            db.commit()


@pytest.mark.parametrize("which", ["owner", "reviewer_expired", "plan_status", "workspace"])
def test_conditional_update_carries_owner_admin_plan_and_workspace_predicates(
    gated, monkeypatch, no_threads, which
):
    """The change lands between the in-lock reads and the UPDATE itself: only a predicate
    of that statement can refuse it."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import event as sa_event

    from app.core.db import engine
    from app.models.assistant import AssistantConversation, AssistantEvaluationPlan
    admin, member, member_id = gated
    registered = which == "reviewer_expired"
    if registered:
        with SessionLocal() as db:
            db.get(User, member_id).role = "admin"
            db.commit()
    cid, h, op_id, fakes = _blocked(
        principal=f"user:{member_id}" if registered else "config-admin",
        owner="member" if registered else "admin",
        **({"approver_user_id": member_id, "approved_by": "member"} if registered else {}))
    _create_event(op_id, fakes)
    from app.services import aws_clients
    monkeypatch.setattr(aws_clients, "client", lambda service, ctx, **kw: fakes(ctx, service))
    op = _op(op_id)
    body = {"plan_hash": op.plan_hash,
            "expected_created_revision_id": _res(op, "lambda_function")["result"][
                "initial_revision_id"],
            "expected_current_revision_id": "rev-active-0001",
            "cloudtrail_event_id": EVENT_ID, "reason": REASON}
    fired = []

    def race(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE evaluation_asset_operations SET") and not fired:
            fired.append(True)
            with SessionLocal() as db:
                if which == "owner":
                    db.get(AssistantConversation, cid).owner_principal = "user:other"
                if which == "reviewer_expired":
                    db.get(User, member_id).expires_at = datetime.now(UTC) - timedelta(seconds=1)
                if which == "plan_status":
                    db.get(AssistantEvaluationPlan, op.plan_id).status = "superseded"
                if which == "workspace":
                    db.get(Workspace, DEFAULT_WORKSPACE_ID).account_id = "999988887777"
                db.commit()

    sa_event.listen(engine, "before_cursor_execute", race)
    try:
        r = (member if registered else admin).post(_route(cid, op_id), json=body)
    finally:
        sa_event.remove(engine, "before_cursor_execute", race)
        with SessionLocal() as db:
            db.get(Workspace, DEFAULT_WORKSPACE_ID).account_id = ACCOUNT
            db.commit()
    assert fired and r.status_code == 409, r.text[:300]
    assert r.json()["code"] == "assistant.evaluation_assets_stopped"
    assert not _res(_op(op_id), "lambda_function").get("reviews")
    assert _op(op_id).status == "partial" and no_threads == []


@pytest.mark.parametrize("which", ["external_same_code_version", "concurrency", "alias",
                                   "policy", "config", "role_trust", "log_retention"])
def test_worker_revalidates_the_reviewed_baseline_before_its_first_mutation(
    gated, monkeypatch, no_threads, which
):
    admin, member, cid, op_id, fakes, event, body = _route_case(gated, monkeypatch)
    name = assets.function_name(op_id)
    assert admin.post(_route(cid, op_id), json=body).status_code == 200
    f = fakes.lam.functions[name]
    if which == "external_same_code_version":
        fakes.lam.publish_version(FunctionName=name)  # someone published our exact code
        fakes.lam.unchanged_publish = "conflict"
    if which == "concurrency":
        fakes.lam.put_function_concurrency(FunctionName=name, ReservedConcurrentExecutions=17)
    if which == "alias":
        fakes.lam.aliases[name] = [{"Name": "foreign", "FunctionVersion": "9"}]
    if which == "policy":
        fakes.lam.policies[name] = [{"Sid": "foreign"}]
    if which == "config":
        f["cfg"]["Environment"] = {"Variables": {"ADDED": "1"}}
    if which == "role_trust":
        fakes.iam.roles[name]["trust"] = json.dumps({"Statement": "replaced"})
    if which == "log_retention":
        fakes.logs.groups[f"/aws/lambda/{name}"]["retention"] = 1
    before = fakes.lam.publish_calls
    assert _run(op_id, fakes)
    op = _op(op_id)
    res = _res(op, "lambda_function")
    assert res["status"] == "conflict", res
    assert fakes.lam.publish_calls == before and res["result"].get("version") is None
    assert f.get("reserved") in (None, 17)  # never overwritten
    assert list(f["versions"]) == (["1"] if which == "external_same_code_version" else [])
    # the reviewed baseline is kept, the conflict is not reviewable again
    assert res["result"]["reviewed_baseline"]["versions"] == ["$LATEST"]
    assert admin.post(_route(cid, op_id), json=body).status_code == 200  # exact replay only
    r = admin.post(_route(cid, op_id), json={
        **body, "expected_created_revision_id": "rev-active-0001",
        "expected_current_revision_id": f["cfg"]["RevisionId"]})
    assert r.status_code in (409, 422) and r.json()["code"].startswith("assistant.lambda_revision")
    assert fakes.lam.publish_calls == before


def test_first_publish_refusal_never_adopts_a_same_digest_version(app_ready):
    """Ordinary path: PublishVersion is refused on the FIRST dispatch while a version with
    our digest exists → conflict, not adoption; a lost answer of OUR OWN dispatch still
    reconciles to exactly one version."""
    cid, h = _conversation("local-operator")
    fakes = ReviewFakes()
    op_id, *_ = _approve(cid, h, fakes=fakes)
    name = assets.function_name(op_id)
    original = fakes.lam.publish_version

    def foreign_then_conflict(FunctionName, CodeSha256=None, RevisionId=None):
        original(FunctionName)  # a foreign publisher wins the window with the same code
        fakes.lam.publish_version = original
        raise ClientError({"Error": {"Code": "ResourceConflictException", "Message": "x"}},
                          "PublishVersion")

    fakes.lam.publish_version = foreign_then_conflict
    _run(op_id, fakes)
    res = _res(_op(op_id), "lambda_function")
    assert res["status"] == "conflict" and "first" in res["error"]
    assert res["result"].get("version") is None and res["result"]["publish_dispatches"] == 1
    # our own lost answer (second dispatch) reconciles the single version we created
    cid2, h2 = _conversation("local-operator")
    fakes2 = ReviewFakes()
    fakes2.lam.lose_publish_response = True
    op2, *_ = _approve(cid2, h2, fakes=fakes2)
    _run(op2, fakes2)
    assert _res(_op(op2), "lambda_function")["status"] == "failed"
    assert _run(op2, fakes2)  # the lease re-admits a failed operation (explicit retry path)
    op = _op(op2)
    assert op.status == "succeeded", op.error
    assert list(fakes2.lam.functions[assets.function_name(op2)]["versions"]) == ["1"]
    assert name  # noqa: S101 — first function untouched


def test_reviewed_recovery_keeps_dataset_items_and_judges_exact(gated, monkeypatch,
                                                                no_threads):
    admin, member, cid, op_id, fakes, event, body = _route_case(gated, monkeypatch)
    with SessionLocal() as db:
        ds = db.get(EvalDataset, _op(op_id).dataset_id)
        items_before = json.loads(json.dumps(ds.items))
    judges_before = dict(fakes.control.evaluators)
    assert admin.post(_route(cid, op_id), json=body).status_code == 200
    assert _run(op_id, fakes) and _op(op_id).status == "succeeded"
    with SessionLocal() as db:
        ds = db.get(EvalDataset, _op(op_id).dataset_id)
        items_after = json.loads(json.dumps(ds.items))
    # the ONLY permitted change: the resolved evaluator-id provenance mapping of the newly
    # created code evaluator; scenario inputs / references / procedures stay exact
    for item in items_before + items_after:
        item["metadata"]["launchpad_assets"].pop("evaluators", None)
    assert items_after == items_before
    assert all(fakes.control.evaluators[k] == v for k, v in judges_before.items())
    assert len(fakes.control.evaluators) == len(judges_before) + 1
