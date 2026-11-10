"""N16-obs35 (round 16, r16-d; rediscovered by the round-4 strip oracle; fix cn/r17-j):
`DELETE /api/registry/records/{id}` on an AGENT_SKILLS record deleted the row and left the
whole `skills/<name>/` prefix in the artifacts bucket. RULING: the route sweeps the prefix
after the row delete — the current objects (a versioned bucket keeps noncurrent versions for
its lifecycle rules) — but ONLY when no other record or live agent references the prefix;
`system-skills/<name>/<version>/` (presets) is never a target; the sweep is best-effort and
refuses nothing new. Contract: registry-backend.md, api.md Console Registry API."""
from __future__ import annotations

import uuid

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent
from app.services import aws_clients, registry_console
from tests.conftest import set_default_resources, ws_ctx
from tests.test_registry_manage import FakeS3 as RegistryFakeS3


def _skill(client, name: str) -> str:
    res = client.post("/api/registry/records", json={
        "type": "AGENT_SKILLS", "name": name, "skill_md": f"---\nname: {name}\n---\n# {name}",
    })
    assert res.status_code == 201, res.text
    return res.json()["record_id"]


def _fake(monkeypatch) -> RegistryFakeS3:
    set_default_resources({"artifacts_bucket": "bkt", "registry_id": "local-default"})
    fake = RegistryFakeS3()
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: fake)
    return fake


def test_delete_sweeps_the_prefix_when_nothing_else_references_it(client, monkeypatch):
    fake = _fake(monkeypatch)
    name = f"sweep-{uuid.uuid4().hex[:6]}"
    rid = _skill(client, name)
    assert fake.uploaded == [f"skills/{name}/SKILL.md"]

    res = client.delete(f"/api/registry/records/{rid}")

    assert res.status_code == 200, res.text
    assert res.json()["objects"] == {"objects_deleted": True, "prefix": f"skills/{name}/"}
    assert fake.deleted == [f"skills/{name}/SKILL.md"]
    assert client.get(f"/api/registry/records/{rid}").status_code == 404


def test_delete_keeps_the_prefix_while_a_live_agent_mounts_it(client, monkeypatch):
    """`spec.skills` lists the prefix as an s3 URI (the deployer's shape) — the objects stay
    and the response names the holder; the row is still deleted."""
    fake = _fake(monkeypatch)
    name = f"held-{uuid.uuid4().hex[:6]}"
    rid = _skill(client, name)
    db = SessionLocal()
    try:
        agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=f"reader-{name}",
                      method="zip_runtime", status="active",
                      spec={"name": f"reader-{name}", "skills": [f"s3://bkt/skills/{name}/"]})
        db.add(agent)
        db.commit()
        agent_id = agent.id
    finally:
        db.close()

    res = client.delete(f"/api/registry/records/{rid}")

    assert res.status_code == 200, res.text
    body = res.json()["objects"]
    assert body["objects_deleted"] is False and body["prefix"] == f"skills/{name}/"
    assert body["kept_for"] == [{"kind": "agent", "id": agent_id, "name": f"reader-{name}"}]
    assert fake.deleted == []
    assert client.get(f"/api/registry/records/{rid}").status_code == 404


def test_every_uri_shape_the_deployer_accepts_holds_the_prefix(client, monkeypatch):
    """rev-r17 on cn/r17-j: `s3://bkt/skills/a` (no trailing slash), `skills/a` and `skills/a/`
    all reference the prefix — the holder match normalises both sides once."""
    for shape in ("s3://bkt/skills/{n}", "skills/{n}", "skills/{n}/", "s3://bkt/skills/{n}/"):
        fake = _fake(monkeypatch)
        name = f"shape-{uuid.uuid4().hex[:6]}"
        rid = _skill(client, name)
        db = SessionLocal()
        try:
            db.add(Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=f"holder-{name}",
                         method="zip_runtime", status="active",
                         spec={"skills": [shape.format(n=name)]}))
            db.commit()
        finally:
            db.close()
        res = client.delete(f"/api/registry/records/{rid}")
        assert res.status_code == 200, res.text
        body = res.json()["objects"]
        assert body["objects_deleted"] is False, (shape, body)
        assert body["kept_for"][0]["name"] == f"holder-{name}", (shape, body)
        assert fake.deleted == [], shape
    # and a reference to a DIFFERENT skill never holds it
    fake = _fake(monkeypatch)
    name = f"other-{uuid.uuid4().hex[:6]}"
    rid = _skill(client, name)
    db = SessionLocal()
    try:
        db.add(Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=f"nothold-{name}",
                     method="zip_runtime", status="active",
                     spec={"skills": [f"s3://bkt/skills/{name}-2"]}))
        db.commit()
    finally:
        db.close()
    out = client.delete(f"/api/registry/records/{rid}").json()["objects"]
    assert out["objects_deleted"] is True
    assert fake.deleted == [f"skills/{name}/SKILL.md"]


def test_a_deleted_agents_reference_does_not_hold_the_prefix(client, monkeypatch):
    fake = _fake(monkeypatch)
    name = f"freed-{uuid.uuid4().hex[:6]}"
    rid = _skill(client, name)
    db = SessionLocal()
    try:
        db.add(Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=f"gone-{name}", method="zip_runtime",
                     status="deleted", spec={"skills": [f"skills/{name}/"]}))
        db.commit()
    finally:
        db.close()
    res = client.delete(f"/api/registry/records/{rid}")
    assert res.status_code == 200 and res.json()["objects"]["objects_deleted"] is True
    assert fake.deleted == [f"skills/{name}/SKILL.md"]


def test_an_mcp_record_delete_sweeps_nothing(client, monkeypatch):
    fake = _fake(monkeypatch)
    res = client.post("/api/registry/records", json={
        "type": "MCP", "name": f"mcp-{uuid.uuid4().hex[:6]}",
        "endpoint": "https://mcp.example.internal/mcp",
    })
    if res.status_code != 201:  # the MCP create needs more than this test stages: unit-level
        rec = {"descriptorType": "MCP", "name": "x", "recordId": "r"}
        assert registry_console.skill_prefix(rec) is None
        assert registry_console.delete_skill_objects(ws_ctx(), rec) == {
            "objects_deleted": False, "prefix": None}
        return
    rid = res.json()["record_id"]
    out = client.delete(f"/api/registry/records/{rid}")
    assert out.status_code == 200 and "objects" not in out.json()  # non-skill: as before
    assert fake.deleted == []


def test_system_skill_prefixes_are_never_a_target():
    """Presets live under `system-skills/<name>/<version>/` — disjoint from `skills/` by
    design; a definition path pointing there (or anywhere outside `skills/`) yields no
    prefix, so the sweep cannot touch it."""
    import json

    for path in ("s3://bkt/system-skills/hr/1.0.0-abc/", "s3://bkt/agents/x/", "s3://bkt/skills/"):
        rec = {"descriptorType": "AGENT_SKILLS", "name": "hr", "recordId": "r",
               "descriptors": {"agentSkills": {"skillDefinition": {
                   "inlineContent": json.dumps({"path": path})}}}}
        assert registry_console.skill_prefix(rec) is None, path
    rec = {"descriptorType": "AGENT_SKILLS", "name": "writer", "recordId": "r",
           "descriptors": {"agentSkills": {"skillDefinition": {
               "inlineContent": json.dumps({"path": "s3://bkt/skills/writer/"})}}}}
    assert registry_console.skill_prefix(rec) == "skills/writer/"
    assert registry_console.skill_prefix({"descriptorType": "AGENT_SKILLS", "name": "n"}) \
        == "skills/n/"


def test_an_s3_failure_is_reported_not_raised(client, monkeypatch):
    fake = _fake(monkeypatch)
    name = f"flaky-{uuid.uuid4().hex[:6]}"
    rid = _skill(client, name)

    def boom(*a, **k):
        raise RuntimeError("s3 down")

    monkeypatch.setattr(registry_console, "_delete_prefix", boom)
    res = client.delete(f"/api/registry/records/{rid}")
    assert res.status_code == 200, res.text
    body = res.json()["objects"]
    assert body["objects_deleted"] is False and body["error"].startswith("RuntimeError")
    assert client.get(f"/api/registry/records/{rid}").status_code == 404
    assert fake.deleted == []
