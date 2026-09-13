"""SE-043 — the architect preset's published Skill as its own, platform-owned Registry
record. Hermetic: the ledger is the conftest temp DB, S3 and the Registry control
plane are fakes with the real semantics the code relies on (conditional reads, token
idempotency, name+version uniqueness, DRAFT-on-update), any socket connect fails."""

import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

import app.routers.registry as registry_router
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.main import create_app
from app.models.ledger import Agent, Job, SystemSkillRecord, Workspace
from app.services import aws_clients
from app.services import registry_console as console
from app.services import users as users_service
from app.services.agentcore import registry as reg
from app.system_agents import presets, skill_registry
from app.system_agents.presets import ARCHITECT, InstallOptions
from app.system_agents.service import MANIFEST_KEY
from tests.conftest import ws_ctx

KEY = ARCHITECT.key
BUCKET = "launchpad-artifacts-test"
REGISTRY_ID = "launchpad-registry-test"
ROUTE = f"/api/system-agents/{KEY}/skill-registration"
READY_RESOURCES = {
    "artifacts_bucket": BUCKET,
    "execution_role_arn": "arn:aws:iam::111122223333:role/launchpad-agent-role",
    "registry_id": REGISTRY_ID,
    "per_agent_execution_roles": True,
}
ADMIN_CREDS = {"username": "admin", "password": "correct horse battery staple"}
MEMBER_CREDS = {"username": "member", "email": "member@example.com",
                "password": "another long member passphrase"}


# ---------------------------------------------------------------------------
# hermetic guards
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket

    def refuse(self, *args, **kwargs):
        raise AssertionError(f"network connect attempted during a hermetic test: {args}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


@pytest.fixture(autouse=True)
def no_real_deploy(monkeypatch):
    import app.routers.agents as agents_router
    import app.routers.system_agents as system_router

    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: None)
    monkeypatch.setattr(system_router, "start_deploy_async", lambda jid: None)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


def _aws_error(code: str, op: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


class FakeS3:
    """Read-only view of a bucket. Any write is a test failure: registration must
    never upload or delete."""

    def __init__(self, objects: dict[str, bytes]):
        self.objects = dict(objects)
        self.reads: list[str] = []

    def get_object(self, Bucket, Key):  # noqa: N803 (boto3 kwarg names)
        self.reads.append(Key)
        if Key not in self.objects:
            raise _aws_error("NoSuchKey", "GetObject")
        body = self.objects[Key]
        return {"Body": io.BytesIO(body), "ETag": '"' + sha256(body).hexdigest()[:16] + '"'}

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):  # noqa: N803
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page, rest = keys[start:start + 2], keys[start + 2:]
        out = {"Contents": [{"Key": k} for k in page], "IsTruncated": bool(rest)}
        if rest:
            out["NextContinuationToken"] = str(start + 2)
        return out

    def put_object(self, **kwargs):
        raise AssertionError(f"registration wrote to S3: {kwargs.get('Key')}")

    def upload_file(self, *args, **kwargs):
        raise AssertionError(f"registration uploaded to S3: {args}")

    def delete_object(self, **kwargs):
        raise AssertionError(f"registration deleted from S3: {kwargs.get('Key')}")


class FakeRegistry:
    """agent-registry-control with the semantics the code depends on:
    - CreateRegistryRecord is idempotent on clientToken (same token → same ARN, no
      second record) and unique on (name, recordVersion) → ConflictException;
    - GetRegistryRecord returns GA descriptors; UpdateRegistryRecord resets DRAFT;
    - submit → PENDING_APPROVAL; status updates; ResourceNotFound on unknown ids.
    ``crash_after_create`` raises once AFTER the record is stored (the request was
    accepted, the response never arrived)."""

    def __init__(self):
        self.records: dict[str, dict] = {}
        self.tokens: dict[str, str] = {}
        self.creates: list[dict] = []
        self.updates: list[dict] = []
        self.status_changes: list[tuple[str, str]] = []
        self.deletes: list[str] = []
        self.crash_after_create = False
        self.crash_after_update = False  # accepted update, response lost (once)
        self.fail_get = False  # GetRegistryRecord denied (IAM) for SKILL records
        self.hide_records = False  # list eventual-consistency lag: nothing visible
        self._lock = threading.Lock()
        self._seq = 0

    def _arn(self, record_id: str) -> str:
        return (
            "arn:aws:agent-registry:us-west-2:111122223333:registry/"
            f"{REGISTRY_ID}/record/{record_id}"
        )

    # -- control plane -----------------------------------------------------------
    def create_registry_record(self, **kw):
        with self._lock:
            self.creates.append(kw)
            token = kw.get("clientToken")
            if token and token in self.tokens:
                rid, original = self.tokens[token]
                if original != kw:  # the service compares the whole request, not the token
                    raise _aws_error("IdempotentParameterMismatchException", "CreateRegistryRecord")
                return {"recordArn": self._arn(rid), "status": self.records[rid]["status"]}
            for rec in self.records.values():
                if rec["name"] == kw["name"] and rec["recordVersion"] == kw["recordVersion"]:
                    raise _aws_error("ConflictException", "CreateRegistryRecord")
            self._seq += 1
            rid = f"rec-{self._seq:04d}"
            self.records[rid] = {
                "recordId": rid, "recordArn": self._arn(rid), "name": kw["name"],
                "displayName": kw.get("displayName"), "description": kw.get("description", ""),
                "recordType": kw["recordType"], "recordVersion": kw["recordVersion"],
                "descriptors": kw["descriptors"], "status": "DRAFT", "tags": kw.get("tags", {}),
            }
            if token:
                self.tokens[token] = (rid, json.loads(json.dumps(kw)))
            if self.crash_after_create:
                self.crash_after_create = False
                raise ConnectionError("socket closed after the request was accepted")
            return {"recordArn": self._arn(rid), "status": "DRAFT"}

    def get_registry_record(self, registryId, recordId):  # noqa: N803
        rec = self.records.get(recordId)
        if rec is None:
            raise _aws_error("ResourceNotFoundException", "GetRegistryRecord")
        if self.fail_get and rec["recordType"] == "SKILL":
            raise _aws_error("AccessDeniedException", "GetRegistryRecord")
        return dict(rec)

    def list_registry_records(self, **kw):
        rows = [] if self.hide_records else list(self.records.values())
        for flt in kw.get("filters") or []:
            rows = [r for r in rows if r.get(flt["name"]) in flt["values"]]
        return {"registryRecords": [dict(r) for r in rows]}

    def update_registry_record(self, **kw):
        with self._lock:
            self.updates.append(kw)
            rec = self.records.get(kw["recordId"])
            if rec is None:
                raise _aws_error("ResourceNotFoundException", "UpdateRegistryRecord")
            primary, value = next(iter(kw["descriptors"]["optionalValue"].items()))
            rec["descriptors"] = {primary: _unwrap(value["optionalValue"])}
            if "recordVersion" in kw:
                rec["recordVersion"] = kw["recordVersion"]
            if "description" in kw:
                rec["description"] = kw["description"]["optionalValue"]
            rec["status"] = "DRAFT"  # verified live: any update re-enters review
            if self.crash_after_update:
                self.crash_after_update = False
                raise ConnectionError("socket closed after the update was accepted")
            return dict(rec)

    def submit_registry_record_for_approval(self, registryId, recordId):  # noqa: N803
        self.records[recordId]["status"] = "PENDING_APPROVAL"
        return {"status": "PENDING_APPROVAL"}

    def update_registry_record_status(self, registryId, recordId, status, statusReason):  # noqa: N803
        self.status_changes.append((recordId, status))
        self.records[recordId]["status"] = status
        return {"status": status}

    def search_discoverable_registry_records(self, registryIds, searchQuery, maxResults):  # noqa: N803
        return {"registryRecords": [dict(r) for r in self.records.values()]}

    def delete_registry_record(self, registryId, recordId):  # noqa: N803
        self.deletes.append(recordId)
        self.records.pop(recordId, None)


def _unwrap(wrapped: dict) -> dict:
    out = {}
    for field, value in wrapped.items():
        inner = value["optionalValue"]
        if field == "additionalData":
            out[field] = {k: _unwrap(v["optionalValue"]) for k, v in inner.items()}
        else:
            out[field] = inner
    return out


# ---------------------------------------------------------------------------
# fixtures: a published release in S3 + an active preset in the ledger
# ---------------------------------------------------------------------------


def _snapshot():
    return presets.snapshot_bundle(ARCHITECT)


def _release_prefix(snapshot) -> str:
    return f"system-skills/{ARCHITECT.name}/{snapshot.version}-{snapshot.digest[:12]}/"


def _published_objects(snapshot=None) -> dict[str, bytes]:
    snapshot = snapshot or _snapshot()
    prefix = _release_prefix(snapshot)
    objects = {f"{prefix}{rel}": body for rel, body in snapshot.files.items()}
    objects[f"{prefix}{MANIFEST_KEY}"] = json.dumps(
        {"name": ARCHITECT.name, **snapshot.release(), "published_at": "2026-09-13T00:00:00Z"}
    ).encode()
    return objects


@pytest.fixture
def clouds(monkeypatch):
    """Route every client the app asks for to the fakes (funnel-compatible)."""
    s3 = FakeS3(_published_objects())
    registry = FakeRegistry()

    def client(service, ws, **kw):
        if service == "s3":
            return s3
        if service in ("agent-registry-control", "agent-registry"):
            return registry  # the data-plane search reads the same records
        raise AssertionError(f"unexpected AWS client during a hermetic test: {service}")

    monkeypatch.setattr(aws_clients, "client", client)
    monkeypatch.setattr(aws_clients, "get_session", lambda *a, **k: pytest.fail("session"))
    monkeypatch.setattr(reg, "wait_record_settled",
                        lambda c, r, rid, timeout_s=60, sleeper=None: reg.get_record(c, r, rid))
    return s3, registry


def _mark_ready(workspace_id: str = DEFAULT_WORKSPACE_ID) -> None:
    db = SessionLocal()
    try:
        row = db.get(Workspace, workspace_id)
        row.bootstrap_status = "ready"
        row.resources = dict(READY_RESOURCES)
        db.commit()
    finally:
        db.close()


def _install_active(
    workspace_id: str = DEFAULT_WORKSPACE_ID, *, digest=None, status="active"
) -> str:
    """An installed preset whose spec pins the published release (what a finished
    install leaves behind), without running a deploy job."""
    digest = digest or _snapshot().digest
    db = SessionLocal()
    try:
        agent = Agent(
            workspace_id=workspace_id, name=ARCHITECT.name, method="harness", status=status,
            system_key=KEY, resource_id="h1",
            arn="arn:aws:bedrock-agentcore:us-west-2:1:harness/h1",
            spec=presets.build_spec(
                ARCHITECT, BUCKET, InstallOptions(), digest=digest
            ).model_dump(),
        )
        db.add(agent)
        db.commit()
        return agent.id
    finally:
        db.close()


def _mapping() -> SystemSkillRecord | None:
    db = SessionLocal()
    try:
        rows = db.query(SystemSkillRecord).all()
        assert len(rows) <= 1, "one mapping per workspace and preset"
        return rows[0] if rows else None
    finally:
        db.close()


def _definition(record: dict) -> dict:
    return json.loads(record["descriptors"]["agentSkillsDefinition"]["data"])


def _skill_md(record: dict) -> str:
    return record["descriptors"]["agentSkillsDefinition"]["additionalData"]["skillMd"]["data"]


# ---------------------------------------------------------------------------
# first registration: exact descriptor, honest review state
# ---------------------------------------------------------------------------


def test_first_registration_describes_the_published_release_exactly(client, clouds):
    s3, registry = clouds
    _mark_ready()
    _install_active()
    snapshot = _snapshot()

    res = client.post(ROUTE)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["created"] is True and body["changed"] is True and body["submitted"] is True
    assert body["record"]["type"] == "AGENT_SKILLS"
    assert body["record"]["status"] == "PENDING_APPROVAL"  # submitted, never approved
    assert body["record"]["system"]["managed"] is True
    assert body["record"]["system"]["preset_key"] == KEY
    assert body["skill"]["files"] == sorted(snapshot.files) and len(body["skill"]["files"]) == 5
    assert body["skill"]["path"] == ARCHITECT.skill_uri(BUCKET, snapshot.digest)
    assert body["skill"]["digest"] == snapshot.digest[:12]
    assert body["preset"]["skill_registration"]["record_id"] == body["record"]["record_id"]

    # the AWS record: GA shape, exact metadata, the real SKILL.md, provenance
    assert len(registry.records) == 1
    stored = next(iter(registry.records.values()))
    assert stored["recordType"] == "SKILL" and stored["name"] == ARCHITECT.name
    assert stored["recordVersion"] == "1.0.0-skill"
    assert stored["tags"] == {"launchpad:system-preset": KEY, "launchpad:workspace": "default"}
    definition = _definition(stored)
    assert definition["name"] == ARCHITECT.name
    assert definition["version"] == ARCHITECT.skill_version == "1.0.0"
    assert definition["path"] == ARCHITECT.skill_uri(BUCKET, snapshot.digest)
    assert definition["files"] == sorted(snapshot.files)
    assert definition["source"]["kind"] == "system"
    assert definition["source"]["preset_key"] == KEY
    assert definition["source"]["release_digest"] == snapshot.digest
    assert _skill_md(stored) == snapshot.files["SKILL.md"].decode()
    assert registry.status_changes == []  # no approve/disable of any kind
    create = registry.creates[0]
    token = create["clientToken"]
    assert 33 <= len(token) <= 256 and all(c.isalnum() or c == "-" for c in token)

    # S3 was read (manifest, listing, every object) and never written
    assert f"{ARCHITECT.skill_prefix(snapshot.digest)}{MANIFEST_KEY}" in s3.reads
    prefix = ARCHITECT.skill_prefix(snapshot.digest)
    assert all(f"{prefix}{rel}" in s3.reads for rel in snapshot.files)

    # the ledger holds identifiers + the registered release, and the same token
    row = _mapping()
    assert row is not None and row.status == "registered"
    assert row.record_id == stored["recordId"] and row.client_token == token
    assert row.release_digest == snapshot.digest and row.registry_id == REGISTRY_ID


def test_registration_is_ledger_read_on_get_and_projects_onto_registry_records(client, clouds):
    _, registry = clouds
    _mark_ready()
    _install_active()
    assert client.get("/api/system-agents").json()["presets"][0]["skill_registration"] is None
    assert registry.creates == []  # the read registered nothing
    rid = client.post(ROUTE).json()["record"]["record_id"]

    listed = client.get("/api/registry/records?type=AGENT_SKILLS").json()["records"]
    assert [r["record_id"] for r in listed] == [rid]
    assert listed[0]["system"]["managed"] is True
    assert listed[0]["system"]["skill_version"] == "1.0.0"
    assert listed[0]["system"]["path"] == ARCHITECT.skill_uri(BUCKET, _snapshot().digest)
    assert "edit" in listed[0]["system"]["protected_actions"]
    detail = client.get(f"/api/registry/records/{rid}").json()
    assert detail["system"]["preset_key"] == KEY
    status = client.get("/api/system-agents").json()["presets"][0]
    assert status["skill_registration"]["record_id"] == rid
    assert status["can_register_skill"] is True


def test_ordinary_records_carry_no_system_member(client, clouds):
    _, registry = clouds
    registry.create_registry_record(
        registryId=REGISTRY_ID, name="member-skill", displayName="member-skill", description="d",
        recordType="SKILL", recordVersion="1.0.0-skill",
        descriptors=reg.to_ga_descriptors("AGENT_SKILLS", reg.build_skills_descriptors(
            skill_md="---\nname: member-skill\n---\n", definition={"name": "member-skill"})),
    )
    _mark_ready()
    listed = client.get("/api/registry/records").json()["records"]
    assert listed[0]["system"] is None


# ---------------------------------------------------------------------------
# idempotency: same content no-op, newer release reviewed again
# ---------------------------------------------------------------------------


def test_repeat_registration_of_the_same_release_is_a_no_op_that_keeps_approval(client, clouds):
    _, registry = clouds
    _mark_ready()
    _install_active()
    first = client.post(ROUTE).json()
    rid = first["record"]["record_id"]
    # the administrator approves in the Registry (explicit, not automatic)
    approved = client.post(f"/api/registry/records/{rid}/action", json={"action": "approve"})
    assert approved.status_code == 200 and approved.json()["status"] == "APPROVED"

    again = client.post(ROUTE)
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["created"] is False and body["changed"] is False
    assert body["record"]["record_id"] == rid
    assert body["record"]["status"] == "APPROVED"  # untouched
    assert len(registry.creates) == 1 and registry.updates == []
    assert _mapping().record_id == rid


def test_a_newer_release_updates_the_descriptor_and_needs_review_again(client, clouds, monkeypatch):
    s3, registry = clouds
    _mark_ready()
    agent_id = _install_active()
    rid = client.post(ROUTE).json()["record"]["record_id"]
    client.post(f"/api/registry/records/{rid}/action", json={"action": "approve"})

    # a new build ships v1.1.0 and the preset was re-published with it
    newer = _snapshot_with_version("1.1.0", monkeypatch)
    s3.objects.update(_published_objects(newer))
    _repoint_spec(agent_id, newer)

    res = client.post(ROUTE)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["created"] is False and body["changed"] is True and body["submitted"] is False
    assert body["record"]["record_id"] == rid  # same record, rolled forward
    assert body["record"]["status"] == "DRAFT"  # review again — never auto-approved
    assert body["record"]["version"] == "1.1.0-skill"
    stored = registry.records[rid]
    assert _definition(stored)["version"] == "1.1.0"
    assert _definition(stored)["path"] == f"s3://{BUCKET}/{_release_prefix(newer)}"
    assert len(registry.creates) == 1 and len(registry.updates) == 1
    assert registry.status_changes == [(rid, "APPROVED")]  # only the human's approval
    row = _mapping()
    assert row.release_version == "1.1.0" and row.release_digest == newer.digest


def _snapshot_with_version(version: str, monkeypatch):
    """This build's bundle at another version (SKILL.md frontmatter + catalogue)."""
    base = _snapshot()
    files = dict(base.files)
    files["SKILL.md"] = files["SKILL.md"].replace(
        f"version: {ARCHITECT.skill_version}".encode(), f"version: {version}".encode(), 1
    )
    assert files["SKILL.md"] != base.files["SKILL.md"]
    snapshot = presets.BundleSnapshot(
        version=version, digest=presets._digest_of(files), files=dict(sorted(files.items()))
    )
    # the catalogue of that build: same preset, bumped skill_version
    monkeypatch.setitem(presets.PRESETS, KEY, replace(ARCHITECT, skill_version=version))
    monkeypatch.setattr(presets, "snapshot_bundle", lambda preset: snapshot)
    return snapshot


def _repoint_spec(agent_id: str, snapshot) -> None:
    """What a finished re-publish leaves on the stored spec."""
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        spec = dict(agent.spec)
        spec["skills"] = [f"s3://{BUCKET}/{_release_prefix(snapshot)}"]
        agent.spec = spec
        db.commit()
    finally:
        db.close()


def test_a_stale_registration_cannot_downgrade_the_registered_release(client, clouds):
    _, registry = clouds
    _mark_ready()
    _install_active()
    client.post(ROUTE)
    db = SessionLocal()
    try:  # the record already describes a newer release than this build/install
        row = db.query(SystemSkillRecord).one()
        row.release_version = "1.2.0"
        row.content_digest = "x"
        db.commit()
    finally:
        db.close()
    res = client.post(ROUTE)
    assert res.status_code == 409, res.text
    assert res.json()["code"] == "system_skill.stale_release"
    assert registry.updates == [] and len(registry.creates) == 1


# ---------------------------------------------------------------------------
# preconditions: release proof, published bytes, foreign records
# ---------------------------------------------------------------------------


def test_checkout_that_is_not_the_installed_release_is_refused_before_any_aws_call(
    client, clouds, monkeypatch
):
    s3, registry = clouds
    _mark_ready()
    _install_active()  # spec pins the published 1.0.0 release …
    _snapshot_with_version("1.1.0", monkeypatch)  # … but this checkout is an unpublished 1.1.0
    res = client.post(ROUTE)
    assert res.status_code == 409, res.text
    assert res.json()["code"] == "system_skill.release_mismatch"
    assert registry.creates == [] and s3.reads == [] and _mapping() is None


def test_unverified_published_bytes_refuse_without_writing(client, clouds):
    s3, registry = clouds
    _mark_ready()
    _install_active()
    prefix = ARCHITECT.skill_prefix(_snapshot().digest)
    s3.objects[f"{prefix}references/intake-options.md"] = b"tampered"
    res = client.post(ROUTE)
    assert res.status_code == 409, res.text
    assert res.json()["code"] == "system_skill.bundle_unverified"
    assert registry.creates == []
    s3.objects[f"{prefix}stray.txt"] = b"foreign"
    del s3.objects[f"{prefix}references/intake-options.md"]
    assert client.post(ROUTE).json()["code"] == "system_skill.bundle_unverified"
    assert registry.creates == []
    row = _mapping()  # the durable intent exists; it is not a registration
    assert row is not None and row.status == "creating" and row.record_id is None


def test_a_foreign_record_with_the_reserved_name_is_refused_never_adopted(client, clouds):
    _, registry = clouds
    registry.create_registry_record(
        registryId=REGISTRY_ID, name=ARCHITECT.name, displayName=ARCHITECT.name, description="mine",
        recordType="SKILL", recordVersion="1.0.0-skill",
        descriptors=reg.to_ga_descriptors("AGENT_SKILLS", reg.build_skills_descriptors(
            skill_md="---\nname: aws-agent-solution-architect\n---\n# not the platform's",
            definition={"name": ARCHITECT.name, "path": f"s3://{BUCKET}/skills/aws-agent-solution-architect/",
                        "source": {"kind": "inline"}})),
    )
    foreign_id = next(iter(registry.records))
    _mark_ready()
    _install_active()
    res = client.post(ROUTE)
    assert res.status_code == 409, res.text
    assert res.json()["code"] == "system_skill.foreign_record"
    assert res.json()["detail"]["record_id"] == foreign_id
    assert len(registry.creates) == 1 and registry.updates == []  # the seed only
    assert _definition(registry.records[foreign_id])["source"] == {"kind": "inline"}
    assert _mapping().record_id is None
    # the foreign record is NOT system-protected: no ledger row names it
    assert client.get(f"/api/registry/records/{foreign_id}").json()["system"] is None


def test_registration_needs_an_active_installed_preset(client, clouds):
    _, registry = clouds
    _mark_ready()
    assert client.post(ROUTE).status_code == 404
    assert client.post(ROUTE).json()["code"] == "system_agent.not_installed"
    _install_active(status="deploying")
    res = client.post(ROUTE)
    assert res.status_code == 409 and res.json()["code"] == "system_skill.preset_not_active"
    assert client.post("/api/system-agents/no-such-preset/skill-registration").status_code == 404
    assert registry.creates == [] and _mapping() is None


def test_registry_unavailable_is_reported_not_faked(client, clouds):
    _mark_ready()
    db = SessionLocal()
    try:
        row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        row.resources = {**READY_RESOURCES, "registry_id": None,
                         "registry_unavailable_reason": "disabled at bootstrap"}
        db.commit()
    finally:
        db.close()
    _install_active()
    res = client.post(ROUTE)
    assert res.status_code == 503 and res.json()["code"] == "registry.unavailable"


# ---------------------------------------------------------------------------
# protection at the service boundary — before any AWS client or S3 object
# ---------------------------------------------------------------------------


def _no_client(*args, **kwargs):
    # AssertionError, not pytest.fail: a BaseException inside a request handler would
    # take the TestClient portal down with it and hide the real failure
    raise AssertionError(f"AWS client built for a refused mutation: {args}")


def _registered(client) -> str:
    _mark_ready()
    _install_active()
    res = client.post(ROUTE)
    assert res.status_code == 200, res.text
    return res.json()["record"]["record_id"]


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN_CREDS["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN_CREDS["password"])
    get_settings.cache_clear()
    app = create_app()
    with (
        TestClient(app, client=("127.0.0.1", 4321)) as admin,
        TestClient(app, client=("127.0.0.1", 4321)) as member,
    ):
        assert admin.post("/api/auth/login", json=ADMIN_CREDS).status_code == 200
        assert member.post("/api/auth/register", json=MEMBER_CREDS).status_code == 201
        db = SessionLocal()
        try:
            user = users_service.find_by_username(db, MEMBER_CREDS["username"])
            user.status = users_service.STATUS_ACTIVE
            user.expires_at = datetime.now(UTC) + timedelta(days=7)
            users_service.set_workspace_grants(db, user, [DEFAULT_WORKSPACE_ID])
            db.commit()
        finally:
            db.close()
        login = member.post("/api/auth/login", json={
            "username": MEMBER_CREDS["username"], "password": MEMBER_CREDS["password"]})
        assert login.status_code == 200 and login.json()["role"] == "member"
        yield admin, member
    get_settings.cache_clear()


def test_member_cannot_register_or_operate_the_system_skill_but_may_view_it(gated, clouds):
    admin, member = gated
    _, registry = clouds
    _mark_ready()
    _install_active()
    assert member.post(ROUTE).status_code == 403
    assert member.post(ROUTE).json()["code"] == "auth.forbidden"
    assert registry.creates == []
    status = member.get("/api/system-agents").json()["presets"][0]
    assert status["can_register_skill"] is False
    rid = admin.post(ROUTE).json()["record"]["record_id"]
    assert admin.get("/api/system-agents").json()["presets"][0]["can_register_skill"] is True

    # view: yes
    seen = member.get(f"/api/registry/records/{rid}").json()
    assert seen["system"]["managed"] is True and seen["status"] == "PENDING_APPROVAL"
    # lifecycle: no (whatever perm:* the member holds)
    for action in ("approve", "reject", "disable", "submit"):
        res = member.post(f"/api/registry/records/{rid}/action", json={"action": action})
        assert res.status_code == 403, (action, res.text)
        assert res.json()["code"] == "registry.system_skill_protected"
    assert registry.status_changes == []
    # the administrator's explicit approval is the only way it becomes mountable
    res = admin.post(f"/api/registry/records/{rid}/action", json={"action": "approve"})
    assert res.status_code == 200 and res.json()["status"] == "APPROVED"
    assert registry.status_changes == [(rid, "APPROVED")]


def test_content_mutations_are_refused_for_everyone_before_any_aws_call(
    gated, clouds, monkeypatch
):
    admin, member = gated
    s3, registry = clouds
    rid = _registered(admin)
    # from here on, ANY AWS client construction is a test failure
    monkeypatch.setattr(aws_clients, "client", _no_client)
    for who in (admin, member):
        res = who.put(f"/api/registry/records/{rid}", json={"description": "renamed"})
        assert res.status_code == 403 and res.json()["code"] == "registry.system_skill_protected"
        res = who.put(f"/api/registry/records/{rid}", json={"skill_md": "---\nname: x\n---\n"})
        assert res.status_code == 403 and res.json()["code"] == "registry.system_skill_protected"
        res = who.post(f"/api/registry/records/{rid}/reimport")
        assert res.status_code == 403 and res.json()["code"] == "registry.system_skill_protected"
        res = who.delete(f"/api/registry/records/{rid}")
        assert res.status_code == 403 and res.json()["code"] == "registry.system_skill_protected"
        assert "System presets" in res.json()["message"]
        assert res.json()["detail"]["maintenance_route"] == ROUTE
    assert registry.updates == [] and registry.deletes == [] and rid in registry.records


def test_skill_lab_publish_path_and_internal_callers_are_guarded(client, clouds, monkeypatch):
    """`update_record` is what Skill Lab's publish uses; `console_action` without an
    identity is what a background job would call. Both refuse a system Skill."""
    rid = _registered(client)
    _, registry = clouds
    monkeypatch.setattr(aws_clients, "client", _no_client)
    ws = ws_ctx(READY_RESOURCES)
    from app.core.errors import AppError
    from app.services.skill_ingest import bundle_from_inline

    with pytest.raises(AppError) as exc:
        console.update_record(rid, ws, skill_md="---\nname: x\n---\n# trained")
    assert exc.value.code == "registry.system_skill_protected"
    bundle = bundle_from_inline("---\nname: aws-agent-solution-architect\n---\n# x")
    try:
        with pytest.raises(AppError) as exc:
            console.update_record(rid, ws, bundle=bundle)
        assert exc.value.detail["action"] == "replace"
    finally:
        bundle.close()
    with pytest.raises(AppError) as exc:  # identity omitted ⇒ never an admin
        console.console_action(ws, rid, "approve")
    assert exc.value.code == "registry.system_skill_protected"
    with pytest.raises(AppError):
        console.console_delete(ws, rid)
    with pytest.raises(AppError):
        console.reimport_skill(ws, rid)
    assert registry.updates == [] and registry.status_changes == []


def test_reserved_skill_name_is_refused_for_ordinary_register_and_import_before_s3(
    client, clouds, monkeypatch
):
    s3, registry = clouds
    _mark_ready()
    res = client.post("/api/registry/records", json={
        "type": "AGENT_SKILLS", "name": ARCHITECT.name,
        "skill_md": "---\nname: aws-agent-solution-architect\ndescription: d\n---\n# mine",
    })
    assert res.status_code == 409, res.text
    assert res.json()["code"] == "registry.name_reserved"
    # rename-on-import to the reserved name is refused the same way
    from app.core.errors import AppError
    from app.services.skill_ingest import bundle_from_inline

    bundle = bundle_from_inline("---\nname: harmless-skill\ndescription: d\n---\n# ok")
    try:
        with pytest.raises(AppError) as exc:
            console.register_skill_bundle(
                bundle, ws_ctx(READY_RESOURCES), name_override=ARCHITECT.name
            )
        assert exc.value.code == "registry.name_reserved"
    finally:
        bundle.close()
    assert registry.creates == [] and s3.reads == []
    # an MCP record may still use the name — only the Skill name is reserved
    seen = {}
    monkeypatch.setattr(
        registry_router.console, "register_mcp_server",
        lambda _ws, name, description, url: seen.update(name=name) or {
            "recordId": "m1", "name": name, "descriptorType": "MCP", "status": "DRAFT"},
    )
    res = client.post("/api/registry/records", json={
        "type": "MCP", "name": ARCHITECT.name, "url": "https://mcp.example/mcp"})
    assert res.status_code == 201 and seen == {"name": ARCHITECT.name}


def test_ordinary_skill_records_keep_their_member_lifecycle_and_edits(gated, clouds, monkeypatch):
    """Parity: nothing above changes an ordinary record."""
    admin, member = gated
    _, registry = clouds
    _mark_ready()
    registry.create_registry_record(
        registryId=REGISTRY_ID, name="member-skill", displayName="member-skill", description="d",
        recordType="SKILL", recordVersion="1.0.0-skill",
        descriptors=reg.to_ga_descriptors("AGENT_SKILLS", reg.build_skills_descriptors(
            skill_md="---\nname: member-skill\n---\n# m",
            definition={"name": "member-skill", "path": f"s3://{BUCKET}/skills/member-skill/",
                        "files": ["SKILL.md"], "source": {"kind": "inline"}})),
    )
    rid = next(iter(registry.records))
    res = member.post(f"/api/registry/records/{rid}/action", json={"action": "submit"})
    assert res.status_code == 200 and res.json()["status"] == "PENDING_APPROVAL"
    res = member.put(f"/api/registry/records/{rid}", json={"description": "edited by a member"})
    assert res.status_code == 200, res.text
    assert registry.records[rid]["description"] == "edited by a member"
    res = member.delete(f"/api/registry/records/{rid}")
    assert res.status_code == 200 and registry.deletes == [rid]


# ---------------------------------------------------------------------------
# concurrency, crash recovery, retention
# ---------------------------------------------------------------------------


def test_concurrent_registrations_converge_on_one_record(clouds):
    _, registry = clouds
    # the app first: init_db re-seeds the default workspace row from settings
    app = create_app()
    _mark_ready()
    _install_active()
    # ONE TestClient context: FastAPI's routing cache races across two portals
    with TestClient(app) as client:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: client.post(ROUTE), range(4)))
    assert [r.status_code for r in results] == [200] * 4, [r.text for r in results]
    ids = {r.json()["record"]["record_id"] for r in results}
    assert len(ids) == 1 and len(registry.records) == 1
    assert sum(1 for r in results if r.json()["created"]) == 1
    assert len({c["clientToken"] for c in registry.creates}) == 1  # one durable intent
    assert _mapping().record_id == ids.pop()


def test_crash_after_create_before_the_ledger_commit_recovers_the_same_record(client, clouds):
    _, registry = clouds
    _mark_ready()
    _install_active()
    registry.crash_after_create = True
    with pytest.raises(ConnectionError):  # the request died after AWS accepted the create
        client.post(ROUTE)
    row = _mapping()
    assert row is not None and row.status == "creating" and row.record_id is None
    token = row.client_token
    assert len(registry.records) == 1  # AWS did create it

    second = client.post(ROUTE)
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["record"]["record_id"] == next(iter(registry.records))
    assert len(registry.records) == 1  # the platform's own record, no twin
    # recovery = replaying the persisted request: the service answering the same id
    # for the same token is the proof of ownership (metadata is never consulted)
    assert [c["clientToken"] for c in registry.creates] == [token, token]
    assert registry.creates[0] == registry.creates[1]
    assert _mapping().status == "registered" and _mapping().client_token == token


def test_uninstall_keeps_the_record_and_reinstall_reuses_the_mapping(client, clouds, monkeypatch):
    _, registry = clouds
    _mark_ready()
    agent_id = _install_active()
    rid = client.post(ROUTE).json()["record"]["record_id"]
    client.post(f"/api/registry/records/{rid}/action", json={"action": "approve"})

    # the preset is uninstalled (verified teardown marks the row deleted); the Skill
    # record, its S3 release and the mapping stay for other consumers
    db = SessionLocal()
    try:
        db.get(Agent, agent_id).status = "deleted"
        db.commit()
    finally:
        db.close()
    assert rid in registry.records and registry.records[rid]["status"] == "APPROVED"
    assert client.get(f"/api/registry/records/{rid}").json()["system"]["managed"] is True
    status = client.get("/api/system-agents").json()["presets"][0]
    assert status["skill_registration"]["record_id"] == rid
    catalog = console.attachable_records(ws_ctx(READY_RESOURCES), gateways=[])
    assert [s["record_id"] for s in catalog["skills"]] == [rid]
    assert catalog["skills"][0]["path"] == ARCHITECT.skill_uri(BUCKET, _snapshot().digest)

    # reinstall: the deploy job's register stage lands on the SAME record, no create,
    # no update — the approval survives
    new_agent_id = _install_active(status="deploying")
    from app.deployer.pipeline import StageContext
    from app.deployer.registration import register_stage

    monkeypatch.setattr("app.deployer.registration.register_agent_record",
                        lambda row, ws: {"record_id": "a2a-1", "created": True})
    logs: list[str] = []
    ctx = StageContext(agent_id=new_agent_id, deployment_id="d", job_id="j",
                       workspace=ws_ctx(READY_RESOURCES), log=logs.append)
    db = SessionLocal()
    try:
        result = register_stage(ctx, db.get(Agent, new_agent_id))
    finally:
        db.close()
    assert "system skill record verified · no-op" in result.detail
    assert "registry (A2A) created · a2a-1" in result.detail
    assert len(registry.records) == 1 and registry.updates == []
    assert registry.records[rid]["status"] == "APPROVED"
    assert _mapping().record_id == rid
    db = SessionLocal()
    try:
        assert db.get(Agent, new_agent_id).registry_record_id == "a2a-1"  # the agent's own A2A id
    finally:
        db.close()


def test_register_stage_registers_the_skill_for_a_fresh_install_and_fails_honestly(
    clouds, monkeypatch
):
    s3, registry = clouds
    _mark_ready()
    agent_id = _install_active(status="deploying")
    from app.deployer.pipeline import StageContext
    from app.deployer.registration import register_stage

    monkeypatch.setattr("app.deployer.registration.register_agent_record",
                        lambda row, ws: {"record_id": "a2a-1", "created": True})
    logs: list[str] = []
    ctx = StageContext(agent_id=agent_id, deployment_id="d", job_id="j",
                       workspace=ws_ctx(READY_RESOURCES), log=logs.append)
    db = SessionLocal()
    try:
        result = register_stage(ctx, db.get(Agent, agent_id))
    finally:
        db.close()
    assert "system skill record created" in result.detail
    assert "submitted for approval" in result.detail
    assert any("system skill record created" in line for line in logs)
    assert len(registry.records) == 1 and _mapping().status == "registered"

    # a second preset job whose published bytes no longer verify fails the stage with
    # the reason — no "registered" claim without AWS + S3 agreement
    prefix = ARCHITECT.skill_prefix(_snapshot().digest)
    s3.objects[f"{prefix}SKILL.md"] = b"tampered"
    from app.core.errors import AppError

    db = SessionLocal()
    try:
        with pytest.raises(AppError) as exc:
            register_stage(ctx, db.get(Agent, agent_id))
    finally:
        db.close()
    assert exc.value.code == "system_skill.bundle_unverified"
    assert registry.updates == []


def test_registration_and_approval_invalidate_the_attachables_cache(client, clouds):
    _mark_ready()
    _install_active()
    registry_router._attachables_cache["default"] = {"data": {"skills": [], "mcp_servers": []},
                                                    "at": __import__("time").time()}
    rid = client.post(ROUTE).json()["record"]["record_id"]
    assert "default" not in registry_router._attachables_cache
    registry_router._attachables_cache["default"] = {"data": {"skills": [], "mcp_servers": []},
                                                    "at": __import__("time").time()}
    client.post(f"/api/registry/records/{rid}/action", json={"action": "approve"})
    assert "default" not in registry_router._attachables_cache


def test_mapping_is_workspace_scoped(client, clouds):
    """A record id is protected only in the workspace whose ledger names it."""
    rid = _registered(client)
    db = SessionLocal()
    try:
        assert skill_registry.protected_record(db, "default", REGISTRY_ID, rid) is not None
        assert skill_registry.protected_record(db, "other-workspace", REGISTRY_ID, rid) is None
        other = ws_ctx(READY_RESOURCES, id="other-workspace")
        assert skill_registry.projections_for(other, [rid]) == {}
    finally:
        db.close()


def test_deprecated_record_fails_clearly_instead_of_being_rewritten(client, clouds, monkeypatch):
    s3, registry = clouds
    _mark_ready()
    agent_id = _install_active()
    rid = client.post(ROUTE).json()["record"]["record_id"]
    registry.records[rid]["status"] = "DEPRECATED"
    newer = _snapshot_with_version("1.1.0", monkeypatch)
    s3.objects.update(_published_objects(newer))
    _repoint_spec(agent_id, newer)
    res = client.post(ROUTE)
    assert res.status_code == 409 and res.json()["code"] == "system_skill.record_deprecated"
    assert registry.updates == [] and registry.status_changes == []


def test_new_table_is_workspace_scoped_and_created_fresh():
    from app.core.db import WORKSPACE_SCOPED_TABLES, engine, schema_drift

    assert "system_skill_records" in WORKSPACE_SCOPED_TABLES
    assert schema_drift(engine) == {}
    db = SessionLocal()
    try:
        assert db.query(Job).count() == 0  # nothing here queues a job
    finally:
        db.close()


# ---------------------------------------------------------------------------
# correction pass: ownership proof, replay bytes, verification state, high-water,
# registry identity, Skill Lab publish
# ---------------------------------------------------------------------------


def _seed_foreign(registry, *, skill_md: str, version: str = "9.0.0-skill") -> str:
    """A same-name Skill record somebody else created — with the platform's own
    descriptor metadata copied verbatim (source.kind=system, preset key, S3 path)."""
    snapshot = _snapshot()
    registration = skill_registry.build_registration(
        ARCHITECT, snapshot, BUCKET, ARCHITECT.skill_prefix(snapshot.digest)
    )
    descriptors = registration.descriptors()
    descriptors["agentSkills"]["skillMd"]["inlineContent"] = skill_md
    registry.create_registry_record(
        registryId=REGISTRY_ID, name=ARCHITECT.name, displayName=ARCHITECT.name,
        description="theirs", recordType="SKILL", recordVersion=version,
        descriptors=reg.to_ga_descriptors("AGENT_SKILLS", descriptors),
    )
    return next(iter(registry.records))


def _register_via_stage(agent_id: str, monkeypatch) -> tuple:
    from app.deployer.pipeline import StageContext
    from app.deployer.registration import register_stage

    monkeypatch.setattr("app.deployer.registration.register_agent_record",
                        lambda row, ws: {"record_id": "a2a-1", "created": True})
    ctx = StageContext(agent_id=agent_id, deployment_id="d", job_id="j",
                       workspace=ws_ctx(READY_RESOURCES), log=lambda _m: None)
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        try:
            return register_stage(ctx, agent), None
        except Exception as exc:  # the pipeline lands this as the failed stage's detail
            return None, exc
    finally:
        db.close()


@pytest.mark.parametrize("exact_copy", [False, True])
def test_copied_system_metadata_never_creates_ownership(client, clouds, monkeypatch, exact_copy):
    """Descriptor metadata is copyable; only our own create's response is proof. A
    foreign same-name record — unrelated bytes, or an exact copy of our descriptor —
    is refused by the route AND by the deploy register stage, never bound to the
    ledger, never read back into ownership, never overwritten by a later repair."""
    _, registry = clouds
    snapshot = _snapshot()
    own_md = snapshot.files["SKILL.md"].decode()
    foreign_id = _seed_foreign(registry, skill_md=own_md if exact_copy else "# theirs")
    before = json.loads(json.dumps(registry.records[foreign_id]))
    _mark_ready()
    agent_id = _install_active()
    for _ in range(2):  # a repeat after the refusal must not adopt either
        res = client.post(ROUTE)
        assert res.status_code == 409, res.text
        assert res.json()["code"] == "system_skill.foreign_record"
        assert res.json()["detail"]["record_id"] == foreign_id
    result, exc = _register_via_stage(agent_id, monkeypatch)
    assert result is None and getattr(exc, "code", "") == "system_skill.foreign_record"
    row = _mapping()
    assert row.record_id is None and row.pending_record_id is None
    assert row.status == "creating" and row.create_request is None
    assert registry.records[foreign_id] == before  # untouched
    assert len(registry.creates) == 1 and registry.updates == []  # the seed only
    assert client.get(f"/api/registry/records/{foreign_id}").json()["system"] is None
    assert client.get("/api/system-agents").json()["presets"][0]["skill_registration"] == {
        "record_id": None, "pending_record_id": None, "status": "creating",
        "release_version": None, "release_digest": None, "path": None,
        "updated_at": _mapping().updated_at.isoformat(),
    }


def test_lost_create_response_replays_the_identical_request_after_time_passes(
    client, clouds, monkeypatch
):
    """The persisted create request — token, descriptors, imported_at, tags — is what
    the retry sends, byte for byte, even after the clock moved and the list API
    still shows nothing; the fake rejects a same-token request with other bytes."""
    _, registry = clouds
    _mark_ready()
    _install_active()
    registry.crash_after_create = True
    with pytest.raises(ConnectionError):
        client.post(ROUTE)
    row = _mapping()
    assert row.status == "creating" and row.record_id is None and row.pending_record_id is None
    assert row.create_request["clientToken"] == row.client_token
    original = registry.creates[0]
    monkeypatch.setattr(skill_registry, "_utcnow_iso", lambda: "2031-01-01T00:00:00Z")
    registry.hide_records = True  # eventual consistency: find-by-name sees nothing
    res = client.post(ROUTE)
    assert res.status_code == 200, res.text
    assert res.json()["record"]["record_id"] == next(iter(registry.records))
    assert len(registry.records) == 1
    assert registry.creates[1] == original  # whole request equal, not just the token
    assert registry.creates[1]["clientToken"] == row.client_token
    row = _mapping()
    assert row.status == "registered" and row.record_id == next(iter(registry.records))


def test_replay_that_the_service_cannot_honour_binds_nothing(client, clouds):
    """Idempotency window closed: the replay conflicts with the record our lost create
    made. Without the service confirming the id, the record is unprovable — refused,
    nothing bound, nothing overwritten (an administrator resolves it in the Registry)."""
    _, registry = clouds
    _mark_ready()
    _install_active()
    registry.crash_after_create = True
    with pytest.raises(ConnectionError):
        client.post(ROUTE)
    rid = next(iter(registry.records))
    registry.tokens.clear()  # the service forgot the token
    before = json.loads(json.dumps(registry.records[rid]))
    res = client.post(ROUTE)
    assert res.status_code == 409 and res.json()["code"] == "system_skill.foreign_record"
    row = _mapping()
    assert row.record_id is None and row.pending_record_id is None and row.status == "creating"
    assert registry.records[rid] == before and registry.updates == []
    assert client.get(f"/api/registry/records/{rid}").json()["system"] is None


def test_accepted_create_with_denied_readback_is_not_registered_until_verified(gated, clouds):
    admin, member = gated
    _, registry = clouds
    _mark_ready()
    _install_active()
    registry.fail_get = True
    res = admin.post(ROUTE)
    assert res.status_code == 403, res.text  # the AWS AccessDenied envelope, not a success
    assert res.json()["code"] == "aws.access_denied"
    rid = next(iter(registry.records))
    row = _mapping()
    assert row.status == "accepted" and row.pending_record_id == rid and row.record_id is None
    assert row.release_version is None and row.content_digest is None
    projection = admin.get("/api/system-agents").json()["presets"][0]["skill_registration"]
    assert projection["status"] == "accepted" and projection["record_id"] is None
    assert projection["pending_record_id"] == rid
    # ours (our create returned the id) ⇒ protected while unverified …
    for who in (admin, member):
        res = who.put(f"/api/registry/records/{rid}", json={"description": "x"})
        assert res.status_code == 403 and res.json()["code"] == "registry.system_skill_protected"
        assert who.delete(f"/api/registry/records/{rid}").status_code == 403
    assert member.post(f"/api/registry/records/{rid}/action", json={"action": "approve"}
                       ).status_code == 403
    # … and verified on the next attempt without a second create
    registry.fail_get = False
    res = admin.post(ROUTE)
    assert res.status_code == 200, res.text
    assert res.json()["created"] is False and res.json()["changed"] is False
    assert "verified" in res.json()["note"]
    assert len(registry.creates) == 1 and len(registry.records) == 1
    row = _mapping()
    assert row.status == "registered" and row.record_id == rid and row.pending_record_id is None
    assert admin.get("/api/system-agents").json()["presets"][0]["skill_registration"]["status"] \
        == "registered"


def _detached_copy(agent_id: str):
    from types import SimpleNamespace

    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        return SimpleNamespace(id=agent.id, system_key=agent.system_key,
                               spec=json.loads(json.dumps(agent.spec)))
    finally:
        db.close()


def test_accepted_update_with_lost_response_cannot_be_downgraded_by_a_stale_worker(
    client, clouds
):
    s3, registry = clouds
    _mark_ready()
    agent_id = _install_active()
    rid = client.post(ROUTE).json()["record"]["record_id"]
    client.post(f"/api/registry/records/{rid}/action", json={"action": "approve"})
    stale_agent = _detached_copy(agent_id)  # what an old worker still holds

    with pytest.MonkeyPatch.context() as mp:  # the new build publishes v1.1.0
        newer = _snapshot_with_version("1.1.0", mp)
        s3.objects.update(_published_objects(newer))
        _repoint_spec(agent_id, newer)
        registry.crash_after_update = True
        with pytest.raises(ConnectionError):
            client.post(ROUTE)
    remote = registry.records[rid]
    assert _definition(remote)["version"] == "1.1.0"  # AWS accepted the update
    row = _mapping()
    assert row.release_version == "1.0.0"  # the response never arrived …
    assert row.intent_version == "1.1.0"  # … but the intent was committed first

    # the old build's worker, with its stale agent object: refused, remote untouched
    from app.core.errors import AppError

    db = SessionLocal()
    try:
        with pytest.raises(AppError) as exc:
            skill_registry.register_system_skill(
                db, ws_ctx(READY_RESOURCES), ARCHITECT, stale_agent
            )
    finally:
        db.close()
    assert exc.value.code == "system_skill.stale_release"
    assert _definition(registry.records[rid])["version"] == "1.1.0"
    assert len(registry.updates) == 1
    # the high-water mark alone also refuses a v1.0 registration
    snapshot = _snapshot()
    stale_registration = skill_registry.build_registration(
        ARCHITECT, snapshot, BUCKET, ARCHITECT.skill_prefix(snapshot.digest)
    )
    db = SessionLocal()
    try:
        current = db.get(Agent, agent_id)
        with pytest.raises(AppError) as exc:
            skill_registry._refuse_stale(db.query(SystemSkillRecord).one(), stale_registration,
                                         current, current)
        assert exc.value.code == "system_skill.stale_release"
    finally:
        db.close()

    with pytest.MonkeyPatch.context() as mp:  # the new build's retry reconciles
        newer = _snapshot_with_version("1.1.0", mp)
        s3.objects.update(_published_objects(newer))
        res = client.post(ROUTE)
    assert res.status_code == 200, res.text
    assert res.json()["created"] is False and res.json()["changed"] is False
    assert len(registry.updates) == 1  # the accepted update was not repeated
    row = _mapping()
    assert row.release_version == "1.1.0" and row.status == "registered"
    assert registry.records[rid]["status"] == "DRAFT"  # a new release still needs review


def test_same_version_different_digest_is_not_identical(client, clouds):
    """A stale object pinning the same version but another digest is refused: content
    identity is (version, digest), never the version string alone."""
    _, registry = clouds
    _mark_ready()
    agent_id = _install_active()
    client.post(ROUTE)
    stale_agent = _detached_copy(agent_id)
    stale_agent.spec["skills"] = [ARCHITECT.skill_uri(BUCKET, "f" * 64)]
    from app.core.errors import AppError

    db = SessionLocal()
    try:
        with pytest.raises(AppError) as exc:
            skill_registry.register_system_skill(
                db, ws_ctx(READY_RESOURCES), ARCHITECT, stale_agent
            )
    finally:
        db.close()
    assert exc.value.code in ("system_skill.release_mismatch", "system_skill.stale_release")
    assert registry.updates == []


def test_registry_replacement_makes_a_same_id_record_ordinary(client, clouds):
    """The mapping is scoped to the registry it was made in: after the workspace moves
    to a replacement registry, a record there with the same id is ordinary — no
    SYSTEM projection, no refusal, no administrative re-registration required."""
    _, registry = clouds
    rid = _registered(client)
    db = SessionLocal()
    try:
        row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        row.resources = {**READY_RESOURCES, "registry_id": "replacement-reg"}
        db.commit()
    finally:
        db.close()
    registry.records[rid]["name"] = "replacement-ordinary"
    listed = client.get("/api/registry/records").json()["records"]
    assert [r["system"] for r in listed] == [None]
    assert client.get(f"/api/registry/records/{rid}").json()["system"] is None
    assert client.get("/api/registry/records/search?q=x").json()["records"][0]["system"] is None
    assert client.get("/api/system-agents").json()["presets"][0]["skill_registration"] is None
    res = client.put(f"/api/registry/records/{rid}", json={"description": "edited"})
    assert res.status_code == 200, res.text
    assert client.delete(f"/api/registry/records/{rid}").status_code == 200
    assert registry.deletes == [rid]
    # the ledger row is inert until a registration in the new registry restarts it
    assert _mapping().registry_id == REGISTRY_ID and _mapping().record_id == rid


def test_skill_lab_multi_file_publish_is_refused_before_any_bundle_preparation(
    client, clouds, monkeypatch, tmp_path
):
    from app.skill_lab import artifacts, runner
    from app.skill_lab.models import SkillLabJob

    rid = _registered(client)
    _, registry = clouds
    monkeypatch.setattr(artifacts, "JOBS_DIR", tmp_path / "jobs")
    job_id = "job_sysskill_pub"
    directory = artifacts.job_dir(job_id)
    (directory / "out" / "skills").mkdir(parents=True)
    (directory / "out" / "skills" / "skill_v0000.md").write_text("seed")
    (directory / "out" / "best_skill.md").write_text("trained")
    (directory / "skills" / ARCHITECT.name).mkdir(parents=True)
    (directory / "skills" / ARCHITECT.name / "SKILL.md").write_text("# s")
    (directory / "publish_skill").mkdir()
    (directory / "publish_skill" / "prior.txt").write_text("previous publish output")
    db = SessionLocal()
    try:
        db.add(SkillLabJob(
            id=job_id, workspace_id=DEFAULT_WORKSPACE_ID, type="train", status="succeeded",
            taskset_id="ts_sysskill",
            skill_source={"kind": "registry", "record_id": rid, "name": ARCHITECT.name},
            params={"trainable_files": ["SKILL.md", "references/intake-options.md"]},
        ))
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(runner, "split_trained_bundle",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("splitter launched")))
    monkeypatch.setattr(aws_clients, "client", _no_client)
    res = client.post(f"/api/skill-lab/jobs/{job_id}/publish", json={"reapprove": True})
    assert res.status_code == 403, res.text
    assert res.json()["code"] == "registry.system_skill_protected"
    assert (directory / "publish_skill" / "prior.txt").read_text() == "previous publish output"
    assert not (directory / "log.txt").exists()
    assert registry.updates == [] and registry.status_changes == []
