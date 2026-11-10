"""N17-obs267 (user request 2026-10-22 "可不可以把待审批和已经审批的分一下类"):
``GET /api/registry/records/counts`` → ``{pending, approved, draft, rejected, deprecated,
total}`` for the registry list's status tabs and the sidebar's pending badge.

MEMBER in ROUTE_POLICY, workspace-scoped, the same visibility as ``GET /records``: under
isolation a member's counts leave out the A2A record of an agent hidden from them, an
administrator's count everything. Real LocalRegistryBackend on the hermetic SQLite ledger.
"""

from __future__ import annotations

from app.core.route_policy import MEMBER, ROUTE_POLICY
from app.models.ledger import Agent
from tests import test_visibility as tv
from tests.test_mutation_ownership import _record

app, admin, ada, bob = tv.app, tv.admin, tv.ada, tv.bob

URL = "/api/registry/records/counts"


def _act(client, rid: str, action: str) -> None:
    res = client.post(f"/api/registry/records/{rid}/action", json={"action": action})
    assert res.status_code == 200, res.text


def _counts(client) -> dict:
    res = client.get(URL)
    assert res.status_code == 200, res.text
    return res.json()


def _list_buckets(client) -> dict:
    """The counts the list route implies — the two must never disagree."""
    rows = client.get("/api/registry/records").json()["records"]
    by = {"PENDING_APPROVAL": "pending", "APPROVED": "approved", "DRAFT": "draft",
          "REJECTED": "rejected", "DEPRECATED": "deprecated"}
    out = dict.fromkeys(by.values(), 0)
    for r in rows:
        out[by[r["status"]]] += 1
    out["total"] = len(rows)
    return out


def test_route_policy_is_member():
    assert ROUTE_POLICY[("GET", URL)] is MEMBER


def test_counts_per_status_agree_with_the_list(admin, ada):
    ada_id, ada_client = ada
    _record(ada_id, name="still-draft")
    for name in ("wait-one", "wait-two"):
        _act(ada_client, _record(ada_id, name=name), "submit")
    published = _record(ada_id, name="published")
    _act(ada_client, published, "submit")
    _act(admin, published, "approve")
    rejected = _record(None, name="rejected")
    _act(admin, rejected, "submit")
    _act(admin, rejected, "reject")
    gone = _record(None, name="disabled")
    for action in ("submit", "approve", "disable"):
        _act(admin, gone, action)
    expected = {"pending": 2, "approved": 1, "draft": 1, "rejected": 1, "deprecated": 1,
                "total": 6}
    assert _counts(admin) == expected
    assert _counts(ada_client) == expected  # shared mode: a member sees every record
    assert _list_buckets(admin) == expected


def test_an_approval_moves_the_record_out_of_pending(admin, ada):
    ada_id, ada_client = ada
    rid = _record(ada_id, name="to-approve")
    _act(ada_client, rid, "submit")
    assert _counts(admin)["pending"] == 1
    _act(admin, rid, "approve")
    after = _counts(admin)
    assert (after["pending"], after["approved"]) == (0, 1)


def test_a_hidden_agents_a2a_record_is_not_counted_for_a_member(admin, ada, bob):
    ada_id, _ada_client = ada
    _bob_id, bob_client = bob
    hidden = _record(ada_id, name="ada-agent", record_type="A2A")
    tv._add(Agent(
        name="ada-agent", method="zip_runtime", status="active", created_by=ada_id,
        created_by_name="ada", registry_record_id=hidden,
        spec={"name": "ada-agent", "method": "zip_runtime", "system_prompt": "p"},
    ))
    _act(admin, hidden, "submit")
    _act(bob_client, _record(None, name="shared-tool"), "submit")
    # shared: the A2A record is everyone's to see
    assert _counts(bob_client)["pending"] == 2
    tv._isolate()
    member = _counts(bob_client)
    assert (member["pending"], member["total"]) == (1, 1)
    assert member == _list_buckets(bob_client)
    everyone = _counts(admin)
    assert (everyone["pending"], everyone["total"]) == (2, 2)


def test_counts_are_zero_when_nothing_is_registered(admin):
    assert _counts(admin) == {"pending": 0, "approved": 0, "draft": 0, "rejected": 0,
                              "deprecated": 0, "total": 0}
