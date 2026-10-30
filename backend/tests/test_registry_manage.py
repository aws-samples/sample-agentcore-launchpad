"""Registry registration + lifecycle management endpoints, end-to-end on the
real local backend (ledger-backed state machine, no AWS Agent Registry — spec
C5). The attachables catalog (Task 5) is covered in test_registry_attachables.py."""

import json

import pytest

from app.services import aws_clients
from app.services import registry_backend as reg
from tests.conftest import set_default_resources, ws_ctx


class FakePaginator:
    def __init__(self, keys):
        self._keys = keys

    def paginate(self, Bucket, Prefix):  # noqa: N803 (boto3 kwarg names)
        yield {"Contents": [{"Key": k} for k in self._keys if k.startswith(Prefix)]}


class FakeS3:
    def __init__(self, existing=None):
        self.existing = list(existing or [])
        self.uploaded: list[str] = []
        self.deleted: list[str] = []
        self.put: list[str] = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        # a listing reflects the bucket: pre-existing + uploaded + put, minus deleted (N16-obs35's
        # sweep lists what the registration uploaded)
        live = [k for k in [*self.existing, *self.uploaded, *self.put] if k not in self.deleted]
        return FakePaginator(live)

    def upload_file(self, src, bucket, key):
        self.uploaded.append(key)

    def put_object(self, Bucket, Key, Body):  # noqa: N803
        self.put.append(Key)

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.deleted.append(Key)

    def delete_objects(self, Bucket, Delete, **kw):  # noqa: N803 — boto kwargs
        for obj in Delete.get("Objects", []):
            self.delete_object(Bucket=Bucket, Key=obj["Key"])
        return {"Deleted": [{"Key": o["Key"]} for o in Delete.get("Objects", [])]}


def test_register_mcp_record(client):
    res = client.post("/api/registry/records", json={
        "type": "MCP", "name": "external-search",
        "description": "team search service", "url": "https://mcp.internal/sse",
    })
    assert res.status_code == 201
    rid = res.json()["record_id"]
    records = reg.get_backend().list_records(ws_ctx(), "MCP")
    assert any(r["recordId"] == rid and r["status"] == "DRAFT" for r in records)


def test_register_mcp_requires_url(client):
    res = client.post("/api/registry/records",
                      json={"type": "MCP", "name": "no-url-server"})
    assert res.status_code == 400
    assert res.json()["code"] == "registry.invalid_url"


def test_register_skill_requires_md(client):
    res = client.post("/api/registry/records",
                      json={"type": "AGENT_SKILLS", "name": "empty-skill"})
    assert res.status_code == 400
    assert res.json()["code"] == "registry.skill_md_required"


def test_register_rejects_bad_name(client):
    res = client.post("/api/registry/records",
                      json={"type": "MCP", "name": "Bad Name!", "url": "https://x"})
    assert res.status_code == 422  # pydantic pattern → validation envelope


def test_register_skill_routes_to_console(client, monkeypatch):
    set_default_resources({"artifacts_bucket": "bkt", "registry_id": "local-default"})
    fake = FakeS3()
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: fake)
    res = client.post("/api/registry/records", json={
        "type": "AGENT_SKILLS", "name": "report-writer",
        "skill_md": "---\nname: report-writer\n---\n# Report writer",
    })
    assert res.status_code == 201
    body = res.json()
    assert body["name"] == "report-writer"
    assert fake.uploaded == ["skills/report-writer/SKILL.md"]
    assert "# Report writer" in body["descriptors"]["agentSkills"]["skillMd"]["inlineContent"]


def test_delete_record_endpoint(client):
    backend = reg.get_backend()
    ws = ws_ctx({"registry_id": "local-default"})
    record, _ = backend.upsert_record(
        ws, name="to-delete", description="d", descriptor_type="MCP",
        descriptors=reg.build_mcp_descriptors(
            target="to-delete", description="d", gateway_url="https://x.example/mcp",
            tools=None,
        ),
    )
    rid = record["recordId"]
    res = client.delete(f"/api/registry/records/{rid}")
    assert res.status_code == 200 and res.json()["deleted"] is True
    assert res.json()["record_id"] == rid


def test_reject_action_routes(client):
    backend = reg.get_backend()
    ws = ws_ctx({"registry_id": "local-default"})
    record, _ = backend.upsert_record(
        ws, name="to-reject", description="d", descriptor_type="MCP",
        descriptors=reg.build_mcp_descriptors(
            target="to-reject", description="d", gateway_url="https://x.example/mcp",
            tools=None,
        ),
    )
    rid = record["recordId"]
    backend.submit_record(ws, rid)  # reject needs PENDING_APPROVAL first
    res = client.post(f"/api/registry/records/{rid}/action", json={"action": "reject"})
    assert res.status_code == 200
    assert res.json()["status"] == "REJECTED"


def test_mcp_descriptors_without_tools():
    """External servers register without a tool listing — the tools descriptor
    must be omitted entirely, not sent as an empty list."""
    desc = reg.build_mcp_descriptors(
        target="external-search", description="d",
        gateway_url="https://mcp.internal/sse", tools=None,
    )
    assert "tools" not in desc["mcp"]
    server = json.loads(desc["mcp"]["server"]["inlineContent"])
    assert server["remotes"][0]["url"] == "https://mcp.internal/sse"


@pytest.fixture
def backend_ready(client, monkeypatch):
    set_default_resources({"artifacts_bucket": "bkt", "registry_id": "local-default"})
    fake = FakeS3()
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: fake)
    return fake


def test_register_same_mcp_name_twice_is_409(client, backend_ready):
    body = {"type": "MCP", "name": "external-search", "description": "d",
            "url": "https://mcp.internal/sse"}
    assert client.post("/api/registry/records", json=body).status_code == 201
    dup = client.post("/api/registry/records", json=body)
    assert dup.status_code == 409 and dup.json()["code"] == "registry.name_exists"


def test_invalid_lifecycle_transition_is_409(client, backend_ready):
    rid = client.post("/api/registry/records", json={
        "type": "MCP", "name": "ext", "url": "https://mcp.internal/sse"}).json()["record_id"]
    res = client.post(f"/api/registry/records/{rid}/action", json={"action": "approve"})
    assert res.status_code == 409 and res.json()["code"] == "registry.invalid_transition"
    assert res.json()["detail"] == {
        "record_id": rid, "from_status": "DRAFT", "to_status": "APPROVED",
    }


def test_action_route_stamps_the_admin_as_decider(client, backend_ready):
    rid = client.post("/api/registry/records", json={
        "type": "MCP", "name": "ext", "url": "https://mcp.internal/sse"}).json()["record_id"]
    client.post(f"/api/registry/records/{rid}/action", json={"action": "submit"})
    client.post(f"/api/registry/records/{rid}/action", json={"action": "approve"})
    record = reg.get_backend().get_record(ws_ctx(), rid)
    assert record["decidedBy"]  # the hermetic client is the built-in admin identity


def test_list_route_rejects_an_unknown_type_with_422(client, backend_ready):
    res = client.get("/api/registry/records", params={"type": "gadget"})
    assert res.status_code == 422 and res.json()["code"] == "registry.bad_type"
