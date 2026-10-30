"""N17-obs248 (user ruling 2026-10-22 "按方案改"): the registry review decision is an
administrator's.

`POST /api/registry/records/{id}/action` stays MEMBER in ROUTE_POLICY because ``submit`` is
a member operation; inside the handler ``approve`` / ``publish`` / ``reject`` / ``disable``
answer 403 ``registry.approval_admin_only {action}`` for a member — after the visibility
404, before any backend call or status change. Administrators (the config admin and an
admin account) are unchanged; the platform paths keep approving as the ``system`` actor; a
member's Skill Lab re-approval only re-submits; a member's content edit of an APPROVED
record still settles it to DRAFT (re-submit + admin approval). Real LocalRegistryBackend
on the hermetic SQLite ledger, no AWS.
"""

from __future__ import annotations

import pytest

from app.services import registry_backend, registry_console
from tests import test_visibility as tv
from tests.conftest import ws_ctx
from tests.test_mutation_ownership import _record
from tests.test_visibility import _job, _member

app, admin, ada, bob = tv.app, tv.admin, tv.ada, tv.bob

DECISIONS = ("approve", "publish", "reject", "disable")
_BACKEND_WRITES = (
    "set_record_status", "submit_record", "approve_record", "reject_record", "disable_record",
)


def _status(client, rid: str) -> str:
    return client.get(f"/api/registry/records/{rid}").json()["status"]


def _act(client, rid: str, action: str):
    return client.post(f"/api/registry/records/{rid}/action", json={"action": action})


@pytest.fixture
def spy(monkeypatch):
    """Counts every lifecycle write the registry backend receives."""
    backend = registry_backend.get_backend()
    calls: list[str] = []
    for name in _BACKEND_WRITES:
        original = getattr(backend, name)

        def wrapped(*a, _n=name, _o=original, **k):
            calls.append(_n)
            return _o(*a, **k)

        monkeypatch.setattr(backend, name, wrapped)
    return calls


def _walk(admin_client, rid: str, to: str) -> None:
    """Put a record into ``to`` as an administrator."""
    path = {
        "DRAFT": [],
        "PENDING_APPROVAL": ["submit"],
        "APPROVED": ["submit", "approve"],
        "REJECTED": ["submit", "reject"],
    }[to]
    for action in path:
        assert _act(admin_client, rid, action).status_code == 200


def test_member_submit_is_allowed(admin, ada, bob):
    ada_id, ada_client = ada
    _bob_id, bob_client = bob
    mine = _record(ada_id, name="ada-server")
    res = _act(ada_client, mine, "submit")
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "PENDING_APPROVAL"
    # submit keeps today's visibility rule: any member who sees the record may submit it
    theirs = _record(ada_id, name="ada-other")
    assert _act(bob_client, theirs, "submit").json()["status"] == "PENDING_APPROVAL"
    # a REJECTED record is re-submitted by the member
    rejected = _record(ada_id, name="ada-rejected")
    _walk(admin, rejected, "REJECTED")
    assert _act(ada_client, rejected, "submit").json()["status"] == "PENDING_APPROVAL"


@pytest.mark.parametrize("action", DECISIONS)
def test_member_decisions_are_refused_before_any_backend_call(admin, ada, spy, action):
    ada_id, ada_client = ada
    # the status the action would be valid from, so a pass-through would visibly move it
    start = "APPROVED" if action == "disable" else "PENDING_APPROVAL"
    rid = _record(ada_id, name=f"own-{action}")  # even the member's OWN record
    _walk(admin, rid, start)
    spy.clear()
    res = _act(ada_client, rid, action)
    assert res.status_code == 403, res.text
    body = res.json()
    assert body["code"] == "registry.approval_admin_only"
    assert body["detail"] == {"action": action}
    assert spy == [], "no lifecycle write may reach the backend"
    assert _status(admin, rid) == start


def test_member_decision_is_refused_before_the_backend_existence_read(ada):
    """The gate sits before the visibility check and every backend call, so an unknown id
    answers the role 403 for a member (submit still reaches the 404)."""
    _ada_id, ada_client = ada
    res = _act(ada_client, "rec-does-not-exist", "approve")
    assert res.status_code == 403 and res.json()["code"] == "registry.approval_admin_only"
    assert _act(ada_client, "rec-does-not-exist", "submit").status_code == 404


def test_member_decision_on_a_hidden_record_is_the_same_403(admin, ada, bob):
    """Isolation hides the A2A record of another member's agent. rev-r23: the role gate
    runs BEFORE the visibility 404, so a member's decision answers the same 403 for a
    hidden, a visible and a missing record — no exists-but-hidden oracle. Submit, the
    member operation, still meets the visibility 404."""
    from app.models.ledger import Agent

    ada_id, _ = ada
    _bob_id, bob_client = bob
    tv._add(Agent(
        name="ada-agent", method="zip_runtime", status="active", created_by=ada_id,
        created_by_name="ada", registry_record_id="rec-hidden-a2a",
        spec={"name": "ada-agent", "method": "zip_runtime", "system_prompt": "p"},
    ))
    tv._isolate()
    for action in DECISIONS:
        res = _act(bob_client, "rec-hidden-a2a", action)
        assert res.status_code == 403, res.text
        assert res.json()["code"] == "registry.approval_admin_only"
    res = _act(bob_client, "rec-hidden-a2a", "submit")
    assert res.status_code == 404 and res.json()["code"] == "registry.record_not_found"


def test_unknown_action_stays_a_400_for_a_member(ada):
    ada_id, ada_client = ada
    rid = _record(ada_id, name="unknown-action")
    res = _act(ada_client, rid, "frobnicate")
    assert res.status_code == 400 and res.json()["code"] == "registry.unknown_action"


def test_administrators_decide(admin, ada, app):
    ada_id, ada_client = ada
    _carol_id, carol = _member(app, "carol", role="admin")
    for who, name in ((admin, "by-root"), (carol, "by-carol")):
        rid = _record(ada_id, name=name)
        assert _act(ada_client, rid, "submit").status_code == 200
        res = _act(who, rid, "approve")
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "APPROVED"
        assert _act(who, rid, "disable").json()["status"] == "DEPRECATED"
    rid = _record(ada_id, name="to-reject")
    assert _act(ada_client, rid, "submit").status_code == 200
    assert _act(carol, rid, "reject").json()["status"] == "REJECTED"
    assert _act(carol, rid, "publish").json()["status"] == "APPROVED"


def test_platform_approval_by_the_system_actor_is_unaffected(ada):
    """The platform paths (gateway-target import, sync-defaults, the sample-skill refresh)
    promote through `_approve_platform_target` as `system`, not through the route."""
    ada_id, _ = ada
    rid = _record(None, name="platform-target")
    backend = registry_backend.get_backend()
    out = registry_console._approve_platform_target(backend, ws_ctx(), rid)
    assert out["status"] == "APPROVED"
    assert out["decidedBy"] == registry_console.PLATFORM_TARGET_ACTOR


def test_member_content_edit_of_an_approved_record_settles_to_draft(admin, ada):
    """Unchanged by design: the creator's content edit drops the APPROVED record back to
    DRAFT — it needs a fresh submit and an administrator's approval to publish again."""
    ada_id, ada_client = ada
    rid = _record(ada_id, name="edit-after-approve")
    assert _act(ada_client, rid, "submit").status_code == 200
    _act(admin, rid, "approve")
    assert _status(admin, rid) == "APPROVED"
    res = ada_client.put(f"/api/registry/records/{rid}", json={"url": "https://m.example/v2"})
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "DRAFT"
    assert _act(ada_client, rid, "submit").json()["status"] == "PENDING_APPROVAL"
    assert _act(ada_client, rid, "approve").status_code == 403
    assert _act(admin, rid, "approve").json()["status"] == "APPROVED"


@pytest.mark.parametrize("who,expected", [("ada", False), ("admin", True)])
def test_skill_lab_publish_forwards_the_callers_role(admin, ada, monkeypatch, who, expected):
    """The Skill Lab publish route hands the caller's role to `publish_job`, so only an
    administrator's publish re-approves (a member's re-submits — `reapprove_record`)."""
    from app.skill_lab import jobs as jobs_svc

    ada_id, ada_client = ada
    seen: dict = {}
    monkeypatch.setattr(jobs_svc, "publish_job", lambda *a, **k: seen.update(k) or {})
    job = _job(ada_id)
    client = ada_client if who == "ada" else admin
    res = client.post(f"/api/skill-lab/jobs/{job}/publish", json={"reapprove": True})
    assert res.status_code == 200, res.text
    assert seen["is_admin"] is expected and seen["reapprove"] is True
