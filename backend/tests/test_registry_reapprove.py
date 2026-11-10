"""``registry_console.reapprove_record`` — N10v4-bug3 (live, round-4 re-test case 28).

A Skill Lab publish bumps the record version through ``update_record``, which settles
the record into DRAFT; the publish then re-approved with a bare "approve", and the local
lifecycle (DRAFT → PENDING_APPROVAL → APPROVED) answered 409
``registry.invalid_transition``. The helper walks the lifecycle instead and is a no-op on
an already-APPROVED record. Real ``LocalRegistryBackend`` (ledger-backed), no AWS.
"""

import pytest

from app.core.errors import AppError
from app.services import registry_backend as reg
from app.services import registry_console
from app.services.registry_local import LocalRegistryBackend
from tests.conftest import ws_ctx

WS = ws_ctx()


@pytest.fixture
def backend(client):
    be = LocalRegistryBackend()
    reg.set_backend(be)
    return be


def _record(backend, name: str = "sample-logtriage") -> str:
    card = reg.build_a2a_card(name=name, description="d", arn="", method="harness",
                              workspace=WS)
    record, _ = backend.upsert_record(
        WS, name=name, description="skill", descriptor_type="A2A",
        descriptors=reg.build_a2a_descriptors(card),
    )
    assert record["status"] == "DRAFT"
    return record["recordId"]


def test_draft_walks_submit_then_approve(backend):
    rid = _record(backend)
    token = reg.current_actor.set("river")
    try:
        out = registry_console.reapprove_record(WS, rid, is_admin=True)
    finally:
        reg.current_actor.reset(token)
    assert out["status"] == "APPROVED"
    after = backend.get_record(WS, rid)
    assert after["status"] == "APPROVED"
    assert after["statusReason"] == registry_console.REAPPROVE_REASON
    assert after["decidedBy"] == "river"
    assert after["submittedAt"] is not None  # it went THROUGH pending, not around it


def test_pending_and_rejected_approve_directly(backend):
    pending = _record(backend, "pending-skill")
    backend.submit_record(WS, pending)
    assert registry_console.reapprove_record(WS, pending, is_admin=True)["status"] == "APPROVED"

    rejected = _record(backend, "rejected-skill")
    backend.submit_record(WS, rejected)
    backend.reject_record(WS, rejected)
    assert backend.get_record(WS, rejected)["status"] == "REJECTED"
    assert registry_console.reapprove_record(
        WS, rejected, is_admin=True
    )["status"] == "APPROVED"


def test_already_approved_is_a_no_op(backend):
    rid = _record(backend)
    backend.submit_record(WS, rid)
    backend.approve_record(WS, rid)
    before = backend.get_record(WS, rid)  # same read path as `after` (stamp formatting)
    assert before["status"] == "APPROVED"

    out = registry_console.reapprove_record(WS, rid, is_admin=True)

    assert out["status"] == "APPROVED"
    after = backend.get_record(WS, rid)
    # no transition happened: the original decision (reason, stamp, actor) is intact
    assert after["statusReason"] == before["statusReason"] != registry_console.REAPPROVE_REASON
    assert after["decidedAt"] == before["decidedAt"]
    assert after["updatedAt"] == before["updatedAt"]


def test_deprecated_surfaces_the_backends_409(backend):
    rid = _record(backend)
    backend.submit_record(WS, rid)
    backend.approve_record(WS, rid)
    backend.disable_record(WS, rid)
    with pytest.raises(AppError) as exc:
        registry_console.reapprove_record(WS, rid)
    assert exc.value.status_code == 409
    assert exc.value.code == "registry.invalid_transition"


def test_member_reapprove_only_submits(backend):
    """N17-obs248: a member's re-approval request stops at PENDING_APPROVAL — the approval
    is an administrator's. DRAFT and REJECTED are submitted; PENDING stays untouched; an
    APPROVED record is still a no-op; DEPRECATED still answers the backend's 409."""
    draft = _record(backend, "member-draft")
    token = reg.current_actor.set("ada")
    try:
        out = registry_console.reapprove_record(WS, draft)  # default: not an administrator
    finally:
        reg.current_actor.reset(token)
    assert out["status"] == "PENDING_APPROVAL"
    after = backend.get_record(WS, draft)
    assert after["status"] == "PENDING_APPROVAL" and after["decidedBy"] is None

    pending_before = backend.get_record(WS, draft)
    again = registry_console.reapprove_record(WS, draft, is_admin=False)
    assert again["status"] == "PENDING_APPROVAL"
    assert backend.get_record(WS, draft)["updatedAt"] == pending_before["updatedAt"]

    rejected = _record(backend, "member-rejected")
    backend.submit_record(WS, rejected)
    backend.reject_record(WS, rejected)
    assert registry_console.reapprove_record(WS, rejected)["status"] == "PENDING_APPROVAL"

    approved = _record(backend, "member-approved")
    backend.submit_record(WS, approved)
    backend.approve_record(WS, approved)
    assert registry_console.reapprove_record(WS, approved)["status"] == "APPROVED"

    backend.disable_record(WS, approved)
    with pytest.raises(AppError) as exc:
        registry_console.reapprove_record(WS, approved)
    assert exc.value.status_code == 409


def test_admin_reapprove_approves_the_draft(backend):
    rid = _record(backend, "admin-draft")
    assert registry_console.reapprove_record(WS, rid, is_admin=True)["status"] == "APPROVED"


def test_platform_target_helpers_share_the_lifecycle_walk(backend):
    """The platform-record promotion (bootstrap / sync-defaults) rides the same helper."""
    rid = _record(backend, "platform-target")
    out = registry_console._approve_platform_target(backend, WS, rid)
    assert out["status"] == "APPROVED"
    assert out["decidedBy"] == registry_console.PLATFORM_TARGET_ACTOR
