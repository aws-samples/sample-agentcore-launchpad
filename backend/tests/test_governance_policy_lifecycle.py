from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.db import SessionLocal
from app.core.errors import AppError
from app.models.ledger import PolicyChange
from app.schemas.governance import (
    EngineRequest,
    GatewayModeRequest,
    PolicyCreateRequest,
    PolicyDeleteRequest,
    PolicyTransitionRequest,
    PolicyUpdateRequest,
)
from app.services import governance
from app.services.agentcore import policy as policy_api


class FakeControl:
    exceptions = SimpleNamespace(ResourceNotFoundException=KeyError)

    def __init__(self):
        self.counter = 0
        self.gateway = {
            "gatewayId": "gw-1",
            "gatewayArn": "arn:aws:bedrock-agentcore:us-west-2:123:gateway/gw-1",
            "gatewayUrl": "https://gw-1.example.test/mcp",
            "name": "payments-gw",
            "description": "Payments",
            "roleArn": "arn:aws:iam::123:role/gateway",
            "protocolType": "MCP",
            "authorizerType": "AWS_IAM",
            "status": "READY",
            "statusReasons": [],
            "createdAt": self._tick(),
            "updatedAt": self._tick(),
        }
        self.tags = dict(policy_api.MANAGED_TAGS)
        self.engines = {}
        self.policies = {}
        self.update_calls = []
        self.fail_log_only_policy_id = None

    def _tick(self):
        self.counter += 1
        return datetime(2026, 7, 16, tzinfo=UTC) + timedelta(seconds=self.counter)

    def list_gateways(self, **_):
        return {
            "items": [
                {
                    "gatewayId": self.gateway["gatewayId"],
                    "name": self.gateway["name"],
                    "protocolType": "MCP",
                }
            ]
        }

    def get_gateway(self, **_):
        return dict(self.gateway)

    def list_tags_for_resource(self, **_):
        return {"tags": dict(self.tags)}

    def tag_resource(self, **kwargs):
        self.tags.update(kwargs["tags"])

    def untag_resource(self, **kwargs):
        for key in kwargs["tagKeys"]:
            self.tags.pop(key, None)

    def list_gateway_targets(self, **_):
        return {"items": []}

    def create_policy_engine(self, **kwargs):
        engine_id = f"pe-{len(self.engines) + 1}"
        engine = {
            "policyEngineId": engine_id,
            "policyEngineArn": (
                "arn:aws:bedrock-agentcore:us-west-2:123:"
                f"policy-engine/{engine_id}"
            ),
            "name": kwargs["name"],
            "status": "ACTIVE",
            "statusReasons": [],
            "createdAt": self._tick(),
            "updatedAt": self._tick(),
        }
        self.engines[engine_id] = engine
        return dict(engine)

    def get_policy_engine(self, *, policyEngineId):
        return dict(self.engines[policyEngineId])

    def update_gateway(self, **kwargs):
        self.gateway["policyEngineConfiguration"] = dict(
            kwargs["policyEngineConfiguration"]
        )
        self.gateway["updatedAt"] = self._tick()
        self.update_calls.append(
            ("gateway", kwargs["policyEngineConfiguration"]["mode"])
        )
        return dict(self.gateway)

    def list_policies(self, *, policyEngineId, **_):
        return {
            "policies": [
                dict(policy)
                for policy in self.policies.values()
                if policy["policyEngineId"] == policyEngineId
            ]
        }

    def create_policy(self, **kwargs):
        policy_id = f"p-{len(self.policies) + 1}"
        detail = {
            "policyId": policy_id,
            "policyArn": (
                "arn:aws:bedrock-agentcore:us-west-2:123:"
                f"policy/{policy_id}"
            ),
            "policyEngineId": kwargs["policyEngineId"],
            "name": kwargs["name"],
            "description": kwargs.get("description", ""),
            "definition": kwargs["definition"],
            "enforcementMode": kwargs["enforcementMode"],
            "status": "ACTIVE",
            "statusReasons": [],
            "createdAt": self._tick(),
            "updatedAt": self._tick(),
        }
        self.policies[policy_id] = detail
        return dict(detail)

    def get_policy(self, *, policyEngineId, policyId):
        detail = self.policies[policyId]
        assert detail["policyEngineId"] == policyEngineId
        return dict(detail)

    def delete_policy(self, *, policyEngineId, policyId):
        detail = self.policies.pop(policyId)
        assert detail["policyEngineId"] == policyEngineId
        return dict(detail)

    def update_policy(self, **kwargs):
        policy_id = kwargs["policyId"]
        mode = kwargs.get("enforcementMode")
        if mode == "LOG_ONLY" and policy_id == self.fail_log_only_policy_id:
            raise RuntimeError("injected original downgrade failure")
        detail = self.policies[policy_id]
        if "definition" in kwargs:
            detail["definition"] = kwargs["definition"]
        if mode is not None:
            detail["enforcementMode"] = mode
        description = kwargs.get("description")
        if description is not None:
            detail["description"] = description.get("optionalValue", "")
        detail["status"] = "ACTIVE"
        detail["updatedAt"] = self._tick()
        self.update_calls.append((policy_id, mode))
        return dict(detail)


class FakeIam:
    def simulate_principal_policy(self, **kwargs):
        return {
            "EvaluationResults": [
                {"EvalActionName": action, "EvalDecision": "allowed"}
                for action in kwargs["ActionNames"]
            ]
        }


def _refresh(db, operation_id):
    db.expire_all()
    return db.get(PolicyChange, operation_id)


def test_candidate_cutover_partial_retry_and_inverse_rollback():
    control = FakeControl()
    iam = FakeIam()
    db = SessionLocal()
    try:
        attach = governance.queue_engine_attach(
            db,
            control,
            "gw-1",
            EngineRequest(
                expected_gateway_updated_at=control.gateway["updatedAt"],
                authorization_model="allowlist",
            ),
        )
        governance.run_policy_change(attach["id"], control=control, iam=iam)
        assert _refresh(db, attach["id"]).status == "succeeded"
        assert control.gateway["policyEngineConfiguration"]["mode"] == "LOG_ONLY"

        create = governance.queue_policy_create(
            db,
            control,
            "gw-1",
            PolicyCreateRequest(
                expected_gateway_updated_at=control.gateway["updatedAt"],
                name="allow_payments",
                statement="permit(principal, action, resource);",
                authorization_model="allowlist",
            ),
        )
        governance.run_policy_change(create["id"], control=control, iam=iam)
        created_change = _refresh(db, create["id"])
        assert created_change.status == "succeeded"
        original_id = created_change.after["policy"]["id"]
        assert control.policies[original_id]["enforcementMode"] == "LOG_ONLY"

        update = governance.queue_policy_update(
            db,
            control,
            "gw-1",
            original_id,
            PolicyUpdateRequest(
                expected_gateway_updated_at=control.gateway["updatedAt"],
                expected_policy_updated_at=control.policies[original_id]["updatedAt"],
                statement="permit(principal, action, resource) when { true };",
            ),
        )
        governance.run_policy_change(update["id"], control=control, iam=iam)
        assert _refresh(db, update["id"]).status == "succeeded"
        assert len(control.policies) == 1

        governance.queue_policy_transition(
            db,
            control,
            "gw-1",
            original_id,
            PolicyTransitionRequest(
                expected_gateway_updated_at=control.gateway["updatedAt"],
                expected_policy_updated_at=control.policies[original_id]["updatedAt"],
                confirmation_name="payments-gw",
                override_reason="approved low-traffic rollout",
            ),
            rollback=False,
            evidence_count=0,
        )
        pending = (
            db.query(PolicyChange)
            .filter(PolicyChange.operation == "policy_promote")
            .order_by(PolicyChange.created_at.desc())
            .first()
        )
        governance.run_policy_change(pending.id, control=control, iam=iam)
        assert _refresh(db, pending.id).status == "succeeded"
        assert control.policies[original_id]["enforcementMode"] == "ACTIVE"

        candidate_change = governance.queue_policy_update(
            db,
            control,
            "gw-1",
            original_id,
            PolicyUpdateRequest(
                expected_gateway_updated_at=control.gateway["updatedAt"],
                expected_policy_updated_at=control.policies[original_id]["updatedAt"],
                statement="permit(principal, action, resource) when { false };",
            ),
        )
        governance.run_policy_change(candidate_change["id"], control=control, iam=iam)
        candidate_row = _refresh(db, candidate_change["id"])
        candidate_id = candidate_row.candidate_policy_id
        assert candidate_id
        assert control.policies[original_id]["enforcementMode"] == "ACTIVE"
        assert control.policies[candidate_id]["enforcementMode"] == "LOG_ONLY"

        promote_req = PolicyTransitionRequest(
            expected_gateway_updated_at=control.gateway["updatedAt"],
            expected_policy_updated_at=control.policies[candidate_id]["updatedAt"],
            confirmation_name="payments-gw",
            override_reason="approved candidate cutover",
        )
        promote = governance.queue_policy_transition(
            db,
            control,
            "gw-1",
            candidate_id,
            promote_req,
            rollback=False,
            evidence_count=0,
        )
        control.fail_log_only_policy_id = original_id
        governance.run_policy_change(promote["id"], control=control, iam=iam)
        partial = _refresh(db, promote["id"])
        assert partial.status == "partial"
        assert control.policies[original_id]["enforcementMode"] == "ACTIVE"
        assert control.policies[candidate_id]["enforcementMode"] == "ACTIVE"

        control.fail_log_only_policy_id = None
        retry = governance.queue_policy_transition(
            db,
            control,
            "gw-1",
            candidate_id,
            PolicyTransitionRequest(
                expected_gateway_updated_at=control.gateway["updatedAt"],
                expected_policy_updated_at=control.policies[candidate_id]["updatedAt"],
                confirmation_name="payments-gw",
                override_reason="retry conservative cutover",
            ),
            rollback=False,
            evidence_count=0,
        )
        governance.run_policy_change(retry["id"], control=control, iam=iam)
        assert _refresh(db, retry["id"]).status == "succeeded"
        assert control.policies[original_id]["enforcementMode"] == "LOG_ONLY"
        assert control.policies[candidate_id]["enforcementMode"] == "ACTIVE"

        rollback = governance.queue_policy_transition(
            db,
            control,
            "gw-1",
            candidate_id,
            PolicyTransitionRequest(
                expected_gateway_updated_at=control.gateway["updatedAt"],
                expected_policy_updated_at=control.policies[candidate_id]["updatedAt"],
            ),
            rollback=True,
            evidence_count=0,
        )
        governance.run_policy_change(rollback["id"], control=control, iam=iam)
        assert _refresh(db, rollback["id"]).status == "succeeded"
        assert control.update_calls[-2:] == [
            (original_id, "ACTIVE"),
            (candidate_id, "LOG_ONLY"),
        ]
    finally:
        db.close()


def test_gateway_enforce_requires_evidence_or_audited_override():
    control = FakeControl()
    iam = FakeIam()
    engine = control.create_policy_engine(name="existing")
    control.gateway["policyEngineConfiguration"] = {
        "arn": engine["policyEngineArn"],
        "mode": "LOG_ONLY",
    }
    db = SessionLocal()
    try:
        with pytest.raises(Exception) as caught:
            governance.queue_gateway_mode(
                db,
                control,
                iam,
                "gw-1",
                GatewayModeRequest(
                    expected_gateway_updated_at=control.gateway["updatedAt"],
                    mode="ENFORCE",
                    confirmation_name="payments-gw",
                ),
                evidence_count=0,
            )
        assert getattr(caught.value, "code", None) == "governance.evidence_required"

        queued = governance.queue_gateway_mode(
            db,
            control,
            iam,
            "gw-1",
            GatewayModeRequest(
                expected_gateway_updated_at=control.gateway["updatedAt"],
                mode="ENFORCE",
                confirmation_name="payments-gw",
                override_reason="emergency rollout approved by operator",
            ),
            evidence_count=0,
        )
        governance.run_policy_change(queued["id"], control=control, iam=iam)
        change = _refresh(db, queued["id"])
        assert change.status == "succeeded"
        assert change.override_reason == "emergency rollout approved by operator"
        assert change.requested["evidence_count"] == 0
        assert control.gateway["policyEngineConfiguration"]["mode"] == "ENFORCE"
    finally:
        db.close()


def test_standalone_policy_promote_can_rollback_from_audit_snapshot():
    control = FakeControl()
    iam = FakeIam()
    db = SessionLocal()
    try:
        attach = governance.queue_engine_attach(
            db,
            control,
            "gw-1",
            EngineRequest(
                expected_gateway_updated_at=control.gateway["updatedAt"],
                authorization_model="allowlist",
            ),
        )
        governance.run_policy_change(attach["id"], control=control, iam=iam)

        create = governance.queue_policy_create(
            db,
            control,
            "gw-1",
            PolicyCreateRequest(
                expected_gateway_updated_at=control.gateway["updatedAt"],
                name="allow_payments",
                statement="permit(principal, action, resource);",
                authorization_model="allowlist",
            ),
        )
        governance.run_policy_change(create["id"], control=control, iam=iam)
        policy_id = _refresh(db, create["id"]).after["policy"]["id"]

        promote = governance.queue_policy_transition(
            db,
            control,
            "gw-1",
            policy_id,
            PolicyTransitionRequest(
                expected_gateway_updated_at=control.gateway["updatedAt"],
                expected_policy_updated_at=control.policies[policy_id]["updatedAt"],
                confirmation_name="payments-gw",
                override_reason="approved low-traffic rollout",
            ),
            rollback=False,
            evidence_count=0,
        )
        governance.run_policy_change(promote["id"], control=control, iam=iam)
        assert control.policies[policy_id]["enforcementMode"] == "ACTIVE"

        rollback = governance.queue_policy_transition(
            db,
            control,
            "gw-1",
            policy_id,
            PolicyTransitionRequest(
                expected_gateway_updated_at=control.gateway["updatedAt"],
                expected_policy_updated_at=control.policies[policy_id]["updatedAt"],
                audit_id=promote["id"],
            ),
            rollback=True,
            evidence_count=0,
        )
        governance.run_policy_change(rollback["id"], control=control, iam=iam)

        row = _refresh(db, rollback["id"])
        assert row.status == "succeeded"
        assert row.requested["snapshot_audit_id"] == promote["id"]
        assert control.policies[policy_id]["enforcementMode"] == "LOG_ONLY"
    finally:
        db.close()


def test_startup_reconciliation_classifies_interrupted_operations():
    """A restart never replays AWS mutations — it classifies live state."""
    control = FakeControl()
    iam = FakeIam()
    db = SessionLocal()
    try:
        attach = governance.queue_engine_attach(
            db,
            control,
            "gw-1",
            EngineRequest(
                expected_gateway_updated_at=control.gateway["updatedAt"],
                authorization_model="allowlist",
            ),
        )
        governance.run_policy_change(attach["id"], control=control, iam=iam)

        # requested state already holds live → succeeded, no AWS mutation
        already_applied = governance.queue_gateway_mode(
            db,
            control,
            iam,
            "gw-1",
            GatewayModeRequest(
                expected_gateway_updated_at=control.gateway["updatedAt"],
                mode="LOG_ONLY",
            ),
            evidence_count=0,
        )
        assert governance.reconcile_policy_changes(control) == [already_applied["id"]]
        assert _refresh(db, already_applied["id"]).status == "succeeded"
        assert control.update_calls == [("gateway", "LOG_ONLY")]

        # nothing observable happened → interrupted, requiring explicit retry
        never_ran = governance.queue_policy_create(
            db,
            control,
            "gw-1",
            PolicyCreateRequest(
                expected_gateway_updated_at=control.gateway["updatedAt"],
                name="allow_payments",
                statement="permit(principal, action, resource);",
                authorization_model="allowlist",
            ),
        )
        assert governance.reconcile_policy_changes(control) == [never_ran["id"]]
        assert _refresh(db, never_ran["id"]).status == "interrupted"
        assert control.policies == {}

        engine_id = _refresh(db, never_ran["id"]).engine_id
        original = control.create_policy(
            policyEngineId=engine_id,
            name="original",
            definition={"cedar": {"statement": "permit(principal, action, resource);"}},
            enforcementMode="ACTIVE",
        )
        candidate = control.create_policy(
            policyEngineId=engine_id,
            name="candidate",
            definition={"cedar": {"statement": "forbid(principal, action, resource);"}},
            enforcementMode="ACTIVE",
        )
        interrupted_cutover = governance.queue_policy_transition(
            db,
            control,
            "gw-1",
            original["policyId"],
            PolicyTransitionRequest(
                expected_gateway_updated_at=control.gateway["updatedAt"],
                expected_policy_updated_at=original["updatedAt"],
                confirmation_name="payments-gw",
                override_reason="approved cutover",
            ),
            rollback=False,
            evidence_count=0,
        )
        row = db.get(PolicyChange, interrupted_cutover["id"])
        row.candidate_policy_id = candidate["policyId"]
        db.commit()

        # both policies ACTIVE → conservative partial state, idempotent retry
        assert governance.reconcile_policy_changes(control) == [
            interrupted_cutover["id"]
        ]
        assert _refresh(db, interrupted_cutover["id"]).status == "partial"
        assert governance.reconcile_policy_changes(control) == []
    finally:
        db.close()


def test_operation_mutex_conflict_and_audit_snapshot_immutability():
    control = FakeControl()
    db = SessionLocal()
    try:
        first = governance.queue_engine_attach(
            db,
            control,
            "gw-1",
            EngineRequest(
                expected_gateway_updated_at=control.gateway["updatedAt"],
                authorization_model="allowlist",
            ),
        )
        with pytest.raises(Exception) as caught:
            governance.queue_engine_attach(
                db,
                control,
                "gw-1",
                EngineRequest(
                    expected_gateway_updated_at=control.gateway["updatedAt"],
                    authorization_model="allowlist",
                ),
            )
        assert getattr(caught.value, "code", None) == "governance.operation_in_flight"

        row = db.get(PolicyChange, first["id"])
        row.before = {"tampered": True}
        with pytest.raises(ValueError, match="immutable policy audit fields"):
            db.commit()
        db.rollback()

        row = db.get(PolicyChange, first["id"])
        row.status = "interrupted"
        db.commit()
        assert db.get(PolicyChange, first["id"]).status == "interrupted"
    finally:
        db.close()


def test_draft_paths_record_the_override_reason_and_need_no_evidence():
    """A LOG_ONLY draft never gated on evidence — but the justification the
    editor collects must still reach the audit column (it used to be dropped by
    queue_policy_create/update, so the audit entry read "OVERRIDE REASON -")."""
    control = FakeControl()
    iam = FakeIam()
    db = SessionLocal()
    try:
        attach = governance.queue_engine_attach(
            db,
            control,
            "gw-1",
            EngineRequest(
                expected_gateway_updated_at=control.gateway["updatedAt"],
                authorization_model="allowlist",
                override_reason="engine attached during the maintenance window",
            ),
        )
        assert _refresh(db, attach["id"]).override_reason == (
            "engine attached during the maintenance window"
        )
        governance.run_policy_change(attach["id"], control=control, iam=iam)

        # create: no evidence anywhere, no confirmation name — still accepted
        create = governance.queue_policy_create(
            db,
            control,
            "gw-1",
            PolicyCreateRequest(
                expected_gateway_updated_at=control.gateway["updatedAt"],
                name="allow_payments",
                statement="permit(principal, action, resource);",
                override_reason="zero traffic yet, drafting from the allowlist",
            ),
        )
        created = _refresh(db, create["id"])
        assert created.override_reason == (
            "zero traffic yet, drafting from the allowlist"
        )
        governance.run_policy_change(create["id"], control=control, iam=iam)
        policy_id = _refresh(db, create["id"]).after["policy"]["id"]

        update = governance.queue_policy_update(
            db,
            control,
            "gw-1",
            policy_id,
            PolicyUpdateRequest(
                expected_gateway_updated_at=control.gateway["updatedAt"],
                expected_policy_updated_at=control.policies[policy_id]["updatedAt"],
                statement="permit(principal, action, resource) when { true };",
                override_reason="tightened after review",
            ),
        )
        assert _refresh(db, update["id"]).override_reason == "tightened after review"
    finally:
        db.close()


def test_policy_delete_guard_execution_and_idempotent_resume():
    control = FakeControl()
    iam = FakeIam()
    db = SessionLocal()
    try:
        attach = governance.queue_engine_attach(
            db,
            control,
            "gw-1",
            EngineRequest(
                expected_gateway_updated_at=control.gateway["updatedAt"],
                authorization_model="allowlist",
            ),
        )
        governance.run_policy_change(attach["id"], control=control, iam=iam)
        create = governance.queue_policy_create(
            db,
            control,
            "gw-1",
            PolicyCreateRequest(
                expected_gateway_updated_at=control.gateway["updatedAt"],
                name="allow_payments",
                statement="permit(principal, action, resource);",
                authorization_model="allowlist",
            ),
        )
        governance.run_policy_change(create["id"], control=control, iam=iam)
        policy_id = _refresh(db, create["id"]).after["policy"]["id"]

        control.policies[policy_id]["enforcementMode"] = "ACTIVE"
        control.gateway["policyEngineConfiguration"]["mode"] = "ENFORCE"
        with pytest.raises(AppError) as excinfo:
            governance.queue_policy_delete(
                db,
                control,
                "gw-1",
                policy_id,
                PolicyDeleteRequest(
                    expected_gateway_updated_at=control.gateway["updatedAt"],
                    expected_policy_updated_at=control.policies[policy_id]["updatedAt"],
                ),
            )
        assert excinfo.value.code == "governance.policy_delete_enforced"

        control.policies[policy_id]["enforcementMode"] = "LOG_ONLY"
        delete = governance.queue_policy_delete(
            db,
            control,
            "gw-1",
            policy_id,
            PolicyDeleteRequest(
                expected_gateway_updated_at=control.gateway["updatedAt"],
                expected_policy_updated_at=control.policies[policy_id]["updatedAt"],
            ),
        )
        governance.run_policy_change(delete["id"], control=control, iam=iam)
        change = _refresh(db, delete["id"])
        assert change.status == "succeeded"
        assert change.after["deleted"] is True
        assert change.after["policy"]["id"] == policy_id
        assert policy_id not in control.policies

        # resume after the policy is already gone stays idempotent
        recreate = governance.queue_policy_create(
            db,
            control,
            "gw-1",
            PolicyCreateRequest(
                expected_gateway_updated_at=control.gateway["updatedAt"],
                name="allow_refunds",
                statement="permit(principal, action, resource);",
                authorization_model="allowlist",
            ),
        )
        governance.run_policy_change(recreate["id"], control=control, iam=iam)
        second_id = _refresh(db, recreate["id"]).after["policy"]["id"]
        pending = governance.queue_policy_delete(
            db,
            control,
            "gw-1",
            second_id,
            PolicyDeleteRequest(
                expected_gateway_updated_at=control.gateway["updatedAt"],
                expected_policy_updated_at=control.policies[second_id]["updatedAt"],
            ),
        )
        control.policies.pop(second_id)
        governance.run_policy_change(pending["id"], control=control, iam=iam)
        resumed = _refresh(db, pending["id"])
        assert resumed.status == "succeeded"
        assert resumed.after["deleted"] is True
        assert resumed.after["policy"]["id"] == second_id
    finally:
        db.close()
