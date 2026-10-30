"""The Protocol-backed registry routes against an in-memory stub ``RegistryBackend``
(the real one is ``LocalRegistryBackend``, exercised in ``test_registry_local.py``).

These are the only live (non-skeleton) routes in ``routers/registry.py`` until N4:
list / search / discoverable / get, the lifecycle ``/action`` map, delete, and the
``_record_out`` projection. The ledger guard for system-managed Skills must answer
before the backend is even looked up (403 before 503).
"""

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import SystemSkillRecord
from app.routers import registry as registry_router
from app.services import registry_backend
from tests.conftest import set_default_resources

RECORD = {
    "recordId": "rec-1",
    "name": "reports",
    "description": "MCP reports",
    "descriptorType": "MCP",
    "status": "DRAFT",
    "statusReason": None,
    "recordVersion": "1.0.0-mcp",
    "descriptors": {"mcp": {"server": {"inlineContent": "{}"}}},
    "createdAt": "2026-10-08T00:00:00",
    "updatedAt": "2026-10-08T00:00:00",
}


class StubRegistry:
    """Just enough of ``RegistryBackend`` for the live routes; records every call."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []
        self.records = {RECORD["recordId"]: dict(RECORD)}

    def _note(self, op, *args):
        self.calls.append((op, args))

    def list_records(self, workspace, descriptor_type=None, status=None):
        self._note("list_records", descriptor_type, status)
        return list(self.records.values())

    def list_discoverable_records(self, workspace, descriptor_type=None):
        self._note("list_discoverable_records", descriptor_type)
        if descriptor_type not in (None, "MCP", "A2A", "AGENT_SKILLS"):
            raise ValueError(f"unknown registry record type: {descriptor_type!r}")
        return [{**r, "displayName": r["name"], "descriptorTypes": [r["descriptorType"]]}
                for r in self.records.values() if r["status"] == "APPROVED"]

    def get_record(self, workspace, record_id):
        self._note("get_record", record_id)
        return self.records[record_id]

    def search_records(self, workspace, query, max_results=20):
        self._note("search_records", query)
        return [r for r in self.records.values() if query in r["name"]]

    def _status(self, record_id, status):
        self.records[record_id] = {**self.records[record_id], "status": status}
        return self.records[record_id]

    def submit_record(self, workspace, record_id):
        self._note("submit_record", record_id)
        return self._status(record_id, "PENDING_APPROVAL")

    def approve_record(self, workspace, record_id):
        self._note("approve_record", record_id)
        return self._status(record_id, "APPROVED")

    def reject_record(self, workspace, record_id):
        self._note("reject_record", record_id)
        return self._status(record_id, "REJECTED")

    def disable_record(self, workspace, record_id):
        self._note("disable_record", record_id)
        return self._status(record_id, "DEPRECATED")

    def delete_record(self, workspace, record_id):
        self._note("delete_record", record_id)
        self.records.pop(record_id)


@pytest.fixture
def stub(client):
    backend = StubRegistry()
    registry_backend.set_backend(backend)
    registry_router._attachables_cache.clear()
    return backend


def test_list_search_discoverable_and_get_project_records(client, stub):
    listed = client.get("/api/registry/records", params={"type": "MCP", "status": "DRAFT"})
    assert listed.status_code == 200
    (row,) = listed.json()["records"]
    assert row == {
        "system": None, "record_id": "rec-1", "name": "reports", "description": "MCP reports",
        "type": "MCP", "status": "DRAFT", "status_reason": None, "version": "1.0.0-mcp",
        "descriptors": RECORD["descriptors"],
        "created_at": "2026-10-08T08:00:00+08:00", "updated_at": "2026-10-08T08:00:00+08:00",
        # N15-obs7: the creator rides on every projection; a stub record has none
        "created_by": None, "created_by_name": None,
    }
    assert ("list_records", ("MCP", "DRAFT")) in stub.calls

    assert client.get("/api/registry/records/search", params={"q": "rep"}).json()["records"]
    assert client.get("/api/registry/records/search", params={"q": "zzz"}).json() == {
        "records": []
    }
    assert client.get("/api/registry/records/rec-1").json()["record_id"] == "rec-1"

    # discoverable = APPROVED only, summaries without descriptors
    assert client.get("/api/registry/records/discoverable").json() == {"records": [], "count": 0}
    stub.approve_record(None, "rec-1")
    (summary,) = client.get("/api/registry/records/discoverable").json()["records"]
    assert "descriptors" not in summary
    assert summary["display_name"] == "reports" and summary["descriptor_types"] == ["MCP"]


def test_discoverable_unknown_type_is_422(client, stub):
    res = client.get("/api/registry/records/discoverable", params={"type": "bogus"})
    assert res.status_code == 422 and res.json()["code"] == "registry.bad_type"


@pytest.mark.parametrize(("action", "method", "status"), [
    ("submit", "submit_record", "PENDING_APPROVAL"),
    ("approve", "approve_record", "APPROVED"),
    ("publish", "approve_record", "APPROVED"),
    ("reject", "reject_record", "REJECTED"),
    ("disable", "disable_record", "DEPRECATED"),
])
def test_record_action_maps_onto_the_backend(client, stub, action, method, status):
    res = client.post("/api/registry/records/rec-1/action", json={"action": action})
    assert res.status_code == 200, res.text
    assert res.json()["status"] == status
    assert (method, ("rec-1",)) in stub.calls


def test_unknown_action_is_400_without_a_backend_write(client, stub):
    res = client.post("/api/registry/records/rec-1/action", json={"action": "explode"})
    assert res.status_code == 400 and res.json()["code"] == "registry.unknown_action"
    assert all(op in ("get_record",) or op.startswith("list") for op, _ in stub.calls)


def test_delete_calls_the_backend(client, stub):
    res = client.delete("/api/registry/records/rec-1")
    assert res.json() == {"deleted": True, "record_id": "rec-1"}
    assert ("delete_record", ("rec-1",)) in stub.calls
    assert stub.records == {}


def _protect(record_id: str) -> None:
    """A system-managed Skill mapping for ``record_id`` in the default workspace."""
    set_default_resources({"registry_id": "local-default"})
    db = SessionLocal()
    db.add(SystemSkillRecord(
        workspace_id=DEFAULT_WORKSPACE_ID, preset_key="aws-agent-solution-architect",
        skill_name="architect", registry_id="local-default", record_id=record_id,
        client_token="t", status="registered",
    ))
    db.commit()
    db.close()


def test_protected_delete_is_refused_before_the_backend_lookup(client):
    """No backend registered: the ledger guard still answers first (403, not 503)."""
    registry_backend.set_backend(None)
    _protect("sys-1")
    assert registry_backend.backend_or_none() is None
    res = client.delete("/api/registry/records/sys-1")
    assert res.status_code == 403, res.text
    assert res.json()["code"] == "registry.system_skill_protected"


def test_protected_lifecycle_action_by_a_member_is_refused_before_the_backend(
    client, monkeypatch
):
    """Lifecycle actions on a system Skill are administrator-only; the hermetic client
    is the built-in admin, so run the route as a member to exercise the refusal."""
    from app.routers.auth import Identity

    registry_backend.set_backend(None)
    monkeypatch.setattr(
        registry_router, "require_identity",
        lambda _request: Identity(username="member", role="member"),
    )
    _protect("sys-1")
    assert registry_backend.backend_or_none() is None
    res = client.post("/api/registry/records/sys-1/action", json={"action": "submit"})
    assert res.status_code == 403, res.text
    assert res.json()["code"] == "registry.system_skill_protected"


def test_protected_lifecycle_action_by_an_admin_reaches_the_backend_lookup(client):
    """The admin passes the guard; with no backend that is the honest 503."""
    registry_backend.set_backend(None)
    _protect("sys-1")
    res = client.post("/api/registry/records/sys-1/action", json={"action": "submit"})
    assert res.status_code == 503 and res.json()["code"] == "feature.unavailable"
