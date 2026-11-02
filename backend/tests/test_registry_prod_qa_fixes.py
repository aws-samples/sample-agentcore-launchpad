"""Registry fixes from the 2026-11-02 prod QA (QA-3, R3/R4/R7). AWS + S3 are faked.

- R4: deleting an id that names no record answers 404 (`DeleteRegistryRecord` itself
  raised nothing for one, so the console said `{"deleted": true}`).
- R3: an edit sends a PENDING_APPROVAL / APPROVED record back to DRAFT (AWS Agent
  Registry lifecycle), so `republish` submits and approves it again in one call.
- R7: a Skill record's delete removes the `skills/<name>/` objects the platform wrote,
  unless a live agent still mounts them or another same-name record shares the prefix.
"""


import pytest

import app.routers.registry as registry_router
import app.services.registry_console as console_mod
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.models.ledger import Agent
from app.services import aws_clients
from app.services.agentcore import registry as reg

from .conftest import client_error, ws_ctx

WS = ws_ctx({"artifacts_bucket": "bkt"})
RID = "AbCdEf123456"  # the service's 12-character record id shape


def _skill(rid=RID, name="probe-skill", status="APPROVED", path=None):
    definition = {"name": name, "path": path or f"s3://bkt/skills/{name}/", "files": ["SKILL.md"]}
    return {
        "recordId": rid, "recordArn": f"arn:aws:agent-registry:us-west-2:1:registry/r/record/{rid}",
        "name": name, "status": status, "recordType": "SKILL", "recordVersion": "1.0.0-skill",
        # GetRegistryRecord answers the GA descriptor shape
        "descriptors": reg.to_ga_descriptors("AGENT_SKILLS", reg.build_skills_descriptors(
            skill_md="---\nname: x\n---\n# x", definition=definition,
        )),
    }


class FakeRegistry:
    """agent-registry-control: records by id, a status log, deletes."""

    def __init__(self, *records, auto_approve=False, submit_lag=0):
        self.records = {r["recordId"]: dict(r) for r in records}
        self.deleted: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self.auto_approve = auto_approve
        self.submit_lag = submit_lag  # reads that still answer DRAFT after a submit

    def get_registry_record(self, registryId, recordId):  # noqa: N803
        if recordId not in self.records:
            # the service answers a malformed id with a validation error
            code = "ResourceNotFoundException" if len(recordId) == 12 else "ValidationException"
            raise client_error(code)
        record = self.records[recordId]
        if record.get("_lag"):
            record["_lag"] -= 1
            return {**record, "status": "DRAFT"}
        return dict(record)

    def delete_registry_record(self, registryId, recordId):  # noqa: N803
        self.deleted.append(recordId)
        self.records.pop(recordId, None)

    def list_registry_records(self, **kw):
        name = next(f["values"][0] for f in kw["filters"] if f["name"] == "name")
        return {"registryRecords": [r for r in self.records.values() if r["name"] == name]}

    def submit_registry_record_for_approval(self, registryId, recordId):  # noqa: N803
        self.calls.append(("submit", recordId))
        record = self.records[recordId]
        record["status"] = "APPROVED" if self.auto_approve else "PENDING_APPROVAL"
        record["_lag"] = self.submit_lag

    def update_registry_record_status(self, registryId, recordId, status, statusReason):  # noqa: N803
        self.calls.append((status, recordId))
        self.records[recordId]["status"] = status


class FakeS3:
    def __init__(self, keys):
        self.keys = list(keys)
        self.deleted: list[str] = []

    def get_paginator(self, name):
        keys = self.keys

        class _P:
            def paginate(self, Bucket, Prefix):  # noqa: N803
                yield {"Contents": [{"Key": k} for k in keys if k.startswith(Prefix)]}

        return _P()

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.deleted.append(Key)


@pytest.fixture
def cloud(monkeypatch):
    def install(registry, s3=None):
        monkeypatch.setattr(console_mod, "registry_control_client", lambda _ws: registry)
        monkeypatch.setattr(console_mod, "_registry_id", lambda _ws: "reg")
        monkeypatch.setattr(aws_clients, "client", lambda *a, **k: s3)
        return registry, s3

    return install


# ── R4: a delete of an unknown id is a 404 ───────────────────────────────────


@pytest.mark.parametrize("record_id", ["ZZZZZZZZZZZZ", "not-a-record-id", "x"])
def test_delete_of_an_unknown_record_is_not_found_and_calls_no_delete(cloud, record_id):
    registry, _ = cloud(FakeRegistry(_skill()))
    with pytest.raises(AppError) as exc:
        console_mod.console_delete(WS, record_id)
    assert exc.value.code == "registry.record_not_found" and exc.value.status_code == 404
    assert registry.deleted == []


def test_delete_route_answers_404_for_an_unknown_record(client, cloud):
    cloud(FakeRegistry())
    res = client.delete("/api/registry/records/ZZZZZZZZZZZZ")
    assert res.status_code == 404
    assert res.json()["code"] == "registry.record_not_found"


def test_find_record_by_id_reads_a_malformed_id_as_no_record():
    class Strict:
        def get_registry_record(self, **kw):
            raise client_error("ValidationException", "recordId failed to satisfy constraint")

    assert reg.find_record_by_id(Strict(), "reg", "bad id!") is None


def test_find_record_by_id_surfaces_other_errors():
    class Denied:
        def get_registry_record(self, **kw):
            raise client_error("AccessDeniedException")

    with pytest.raises(Exception, match="AccessDenied"):
        reg.find_record_by_id(Denied(), "reg", RID)


# ── R7: a Skill's platform-written files go with its record ──────────────────


def test_skill_delete_removes_the_platform_prefix(cloud):
    s3 = FakeS3(["skills/probe-skill/SKILL.md", "skills/probe-skill/ref.md",
                 "skills/probe-skill-2/SKILL.md", "system-skills/probe-skill/1/SKILL.md"])
    registry, _ = cloud(FakeRegistry(_skill()), s3)
    out = console_mod.console_delete(WS, RID)
    assert registry.deleted == [RID]
    assert out["files"] == {"prefix": "s3://bkt/skills/probe-skill/", "status": "deleted"}
    # exactly this skill's prefix — not a longer name sharing it, never system-skills/
    assert sorted(s3.deleted) == ["skills/probe-skill/SKILL.md", "skills/probe-skill/ref.md"]


def test_skill_files_stay_while_a_live_agent_mounts_them(cloud):
    s3 = FakeS3(["skills/probe-skill/SKILL.md"])
    cloud(FakeRegistry(_skill()), s3)
    db = SessionLocal()
    db.add_all([
        Agent(workspace_id=WS.id, name="mounts-it", method="harness", status="active",
              spec={"skills": ["s3://bkt/skills/probe-skill"]}),
        Agent(workspace_id=WS.id, name="gone", method="harness", status="deleted",
              spec={"skills": ["s3://bkt/skills/probe-skill/"]}),
    ])
    db.commit()
    db.close()
    out = console_mod.console_delete(WS, RID)
    assert out["files"]["status"] == "kept_in_use"
    assert out["files"]["agents"] == ["mounts-it"]
    assert s3.deleted == []


def test_skill_files_stay_when_another_record_shares_the_name(cloud):
    s3 = FakeS3(["skills/probe-skill/SKILL.md"])
    old = _skill(status="DEPRECATED")
    new = _skill(rid="NewRecord001", status="APPROVED")
    cloud(FakeRegistry(old, new), s3)
    out = console_mod.console_delete(WS, RID)
    assert out["files"]["status"] == "kept_shared"
    assert s3.deleted == []


def test_a_bundle_the_platform_did_not_write_is_left_alone(cloud):
    s3 = FakeS3(["skills/probe-skill/SKILL.md"])
    cloud(FakeRegistry(_skill(path="s3://someone-else/skills/probe-skill/")), s3)
    out = console_mod.console_delete(WS, RID)
    assert out["files"] == {"status": "not_platform"}
    assert s3.deleted == []


def test_non_skill_records_report_no_file_cleanup(cloud):
    mcp = {**_skill(), "recordType": "MCP", "descriptors": reg.to_ga_descriptors(
        "MCP", reg.build_mcp_descriptors(target="t", description="d",
                                         gateway_url="https://x.example/mcp", tools=None))}
    registry, _ = cloud(FakeRegistry(mcp), FakeS3([]))
    assert console_mod.console_delete(WS, RID) == {"files": None}
    assert registry.deleted == [RID]


def test_delete_route_returns_the_file_outcome(client, monkeypatch):
    monkeypatch.setattr(registry_router.console, "console_delete",
                        lambda _ws, rid: {"files": {"status": "deleted", "prefix": "p"}})
    res = client.delete(f"/api/registry/records/{RID}")
    assert res.status_code == 200
    body = res.json()
    assert body["deleted"] is True and body["files"]["status"] == "deleted"


# ── R3: publish again after an edit sent the record back to DRAFT ────────────


def test_republish_submits_then_approves_a_draft():
    registry = FakeRegistry(_skill(status="DRAFT"), submit_lag=2)
    out = reg.republish_record(registry, "reg", RID, sleeper=lambda s: None)
    assert registry.calls == [("submit", RID), ("APPROVED", RID)]
    assert out["status"] == "APPROVED"


def test_republish_does_not_approve_twice_when_the_registry_auto_approves():
    registry = FakeRegistry(_skill(status="DRAFT"), auto_approve=True)
    out = reg.republish_record(registry, "reg", RID, sleeper=lambda s: None)
    assert registry.calls == [("submit", RID)]
    assert out["status"] == "APPROVED"


def test_republish_of_a_pending_record_only_approves():
    registry = FakeRegistry(_skill(status="PENDING_APPROVAL"))
    reg.republish_record(registry, "reg", RID, sleeper=lambda s: None)
    assert registry.calls == [("APPROVED", RID)]


@pytest.mark.parametrize("status", ["APPROVED", "DEPRECATED", "REJECTED"])
def test_republish_refuses_other_statuses(status, cloud):
    cloud(FakeRegistry(_skill(status=status)))
    with pytest.raises(AppError) as exc:
        console_mod.console_action(WS, RID, "republish")
    assert exc.value.code == "registry.not_republishable" and exc.value.status_code == 409


def test_republish_action_route(client, cloud):
    registry, _ = cloud(FakeRegistry(_skill(status="DRAFT")))
    res = client.post(f"/api/registry/records/{RID}/action", json={"action": "republish"})
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "APPROVED"
    assert [c[0] for c in registry.calls] == ["submit", "APPROVED"]


def test_republish_of_an_mcp_record_goes_through_the_approval_guard(client, cloud, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(registry_router, "_require_mcp_record_approvable",
                        lambda request, ws, rid: seen.append(rid))
    cloud(FakeRegistry(_skill(status="DRAFT")))
    client.post(f"/api/registry/records/{RID}/action", json={"action": "republish"})
    assert seen == [RID]


def test_a_file_cleanup_failure_never_fails_the_delete(cloud):
    def broken_listing(*a, **k):
        raise client_error("AccessDenied")

    s3 = FakeS3([])
    s3.get_paginator = lambda name: type("P", (), {"paginate": broken_listing})()
    registry, _ = cloud(FakeRegistry(_skill()), s3)
    out = console_mod.console_delete(WS, RID)
    assert registry.deleted == [RID]
    assert out["files"]["status"] == "failed"
