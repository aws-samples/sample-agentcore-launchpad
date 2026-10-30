# ruff: noqa: F811 — pytest fixtures are imported explicitly from their modules
"""The per-gateway bundle under the Global model (``docs/cn/interfaces/policy-engines.md``
§Bundle): after a successful engine install the hub composes the ``launchpad.policy.gateway/3``
bundle of EVERY gateway attached to that engine — ``attachment: {engine_id, mode}`` + the
installed set's facts — and writes it with the forward-only CAS writer; a detach writes
``attachment: null``. A bundle-write failure never undoes the install: it is journalled, the
engine stays ``bundle_pending`` and the reconciler writes it. Off unless the bundle settings
are on."""

from __future__ import annotations

import hashlib
import io
import json

import pytest
from botocore.exceptions import ClientError

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.ledger import Workspace
from app.models.policy import Policy, PolicyChange, PolicyEngine, PolicyEngineAttachment
from app.policy.interceptor import bundle as interceptor_bundle
from app.services import aws_clients, policy_engines, policy_gateway_publish, policy_store
from tests.conftest import ws_ctx
from tests.policy_principal.test_engine_sync import (  # noqa: F401
    FORBID_PAYOUT,
    _interceptor,
    _journal,
    compiler,
    engines,
)
from tests.policy_principal.test_mint_entrances import kms  # noqa: F401
from tests.policy_principal.test_principal_token import KEY_ID
from tests.test_policy_deploy_narrowing import PERMIT_HR, WS, engine, s3  # noqa: F401

BUCKET = "launchpad-artifacts-test"
GW = "launchpad-gw-shared01"
GW2 = "launchpad-gw-second02"
GW_ARN = f"arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:gateway/{GW}"
RES = {"artifacts_bucket": BUCKET, "gateway_id": GW, "gateway_arn": GW_ARN,
       "gateway_url": "https://gw.example.test/mcp",
       "policy_interceptor_function_arn":
           "arn:aws-cn:lambda:cn-northwest-1:111122223333:function:launchpad-policy-interceptor",
       "execution_role_arn": "arn:aws-cn:iam::111122223333:role/shared"}
HUB_ROLE = "arn:aws-cn:iam::111122223333:role/launchpad-cn-ops-cn-northwest-1"


class CasS3:
    """S3 with the conditional writes the writer relies on (IfNoneMatch="*", IfMatch=<etag>)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_prefix: str | None = None

    @staticmethod
    def _etag(body: bytes) -> str:
        return '"' + hashlib.md5(body).hexdigest() + '"'  # noqa: S324 — S3's own ETag shape

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch=None, IfMatch=None, **_kw):
        self.calls.append(("put", Key))
        if self.fail_prefix and Key.startswith(self.fail_prefix):
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "PutObject")
        body = bytes(Body)
        if IfNoneMatch == "*" and Key in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed", "Message": "x"}},
                              "PutObject")
        if IfMatch is not None and self._etag(self.objects.get(Key, b"")) != IfMatch:
            raise ClientError({"Error": {"Code": "PreconditionFailed", "Message": "x"}},
                              "PutObject")
        self.objects[Key] = body
        return {"ETag": self._etag(body)}

    def get_object(self, *, Bucket, Key, **_kw):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject")
        body = self.objects[Key]
        return {"Body": io.BytesIO(body), "ETag": self._etag(body)}

    def pointer(self, gateway_id: str = GW) -> dict | None:
        raw = self.objects.get(interceptor_bundle.pointer_key(gateway_id))
        return json.loads(raw) if raw else None

    def bundle(self, version: int, gateway_id: str = GW) -> dict:
        return json.loads(self.objects[interceptor_bundle.bundle_key(gateway_id, version)])


@pytest.fixture
def cas(monkeypatch, s3) -> CasS3:
    fake = CasS3()
    monkeypatch.setattr(aws_clients, "client", lambda service, ctx, **kw: fake)
    return fake


@pytest.fixture
def gateway(monkeypatch, kms):
    """The workspace gateway in the resource map, the bundle settings on, the interceptor
    attach hook a recorder."""
    db = SessionLocal()
    try:
        db.get(Workspace, WS).resources = dict(RES)
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(get_settings(), "policy_engine_endpoints",
                        ["10.44.0.10:7443", "10.44.0.10:7444"])
    monkeypatch.setattr(get_settings(), "policy_hub_role_arn", HUB_ROLE)
    hooked: list[tuple[str, bool]] = []
    attached: set[str] = set()
    def hook(db, facts, attach):
        from app.services import policy_gateway_state

        hooked.append((facts["gateway_id"], attach))
        if attach:
            attached.add(facts["gateway_id"])
            policy_gateway_state.record_attach(
                db, facts["gateway_id"], facts["interceptor_function_arn"])
        else:
            attached.discard(facts["gateway_id"])
            policy_gateway_state.record_detach(db, facts["gateway_id"])
        return {"interceptor": "updated"}

    monkeypatch.setattr(policy_engines, "interceptor_hook", hook)
    monkeypatch.setattr(policy_engines, "gateway_snapshot", lambda facts: {
        "gatewayArn": facts["gateway_arn"], "gatewayId": facts["gateway_id"],
        "gatewayUrl": "https://gw.example.test/mcp",
        "roleArn": facts["context"].arn("iam", "role/gateway", region=""),
        "name": facts["gateway_id"],
        "interceptorConfigurations": [{
            "interceptor": {"lambda": {"arn": facts["interceptor_function_arn"]}}
        }] if facts["gateway_id"] in attached else [],
    })
    from app.services import policy_engine_sync

    monkeypatch.setattr(policy_engine_sync, "gateway_tools", lambda ws, gid: [
        {"name": "hr-database___create_payout"}])
    return hooked


def _engine(name="hr") -> str:
    db = SessionLocal()
    try:
        return policy_engines.create_engine(db, WS, name=name, operator="demo-admin").id
    finally:
        db.close()


def _create_in(engine_id: str, name: str, statement: str) -> str:
    db = SessionLocal()
    try:
        return policy_store.create_policy(db, db.get(PolicyEngine, engine_id), name=name,
                                          statement=statement, operator="demo-admin").id
    finally:
        db.close()


def _publish_in(engine_id: str, pid: str):
    db = SessionLocal()
    try:
        return policy_store.publish_policy(db, ws_ctx(RES, id=WS), db.get(PolicyEngine, engine_id),
                                           pid, operator="demo-admin")
    finally:
        db.close()


def _attach(gateway_id: str, engine_id: str, mode: str) -> None:
    db = SessionLocal()
    try:
        policy_engines.attach(db, WS, gateway_id, engine_id=engine_id, mode=mode,
                              operator="demo-admin")
    finally:
        db.close()


def _detach(gateway_id: str) -> None:
    db = SessionLocal()
    try:
        policy_engines.detach(db, WS, gateway_id, operator="demo-admin")
    finally:
        db.close()


def _state(engine_id: str) -> dict | None:
    db = SessionLocal()
    try:
        row = db.get(PolicyEngine, engine_id)
        return dict(row.engine_state) if row and row.engine_state else None
    finally:
        db.close()


def _bundle_journal() -> list[PolicyChange]:
    db = SessionLocal()
    try:
        return db.query(PolicyChange).filter_by(workspace_id=WS, operation="gateway.bundle") \
            .order_by(PolicyChange.created_at).all()
    finally:
        db.close()


def test_attach_writes_the_gateway_bundle_and_a_publish_rewrites_it(engine, cas, gateway,
                                                                     engines, compiler,
                                                                     monkeypatch):
    _interceptor(monkeypatch, True)
    eid = _engine()
    _publish_in(eid, _create_in(eid, "permit_hr", PERMIT_HR))
    assert cas.pointer() is None, "no gateway attached yet: nothing to write"
    assert _state(eid)["bundle_pending"] is True

    _attach(GW, eid, "LOG_ONLY")
    assert gateway == [(GW, True)]
    assert cas.pointer()["version"] == 1
    first = cas.bundle(1)
    parsed = interceptor_bundle.parse_bundle(first, expect_gateway_id=GW)  # what the Lambda reads
    assert first["schema"] == "launchpad.policy.gateway/3" and "workspaces" not in first
    assert first["attachment"] == {
        "engine_id": eid, "mode": "LOG_ONLY", "active_count": 1,
        "temporal_count": 0, "policy_set_version": "ps-2", "temporal_set_version": 0,
        "governed_actions": ['AgentCore::Action::"hr-database___create_payout"'],
        "temporal_referenced_actions": []}
    assert parsed.attachment.mode == "log_only"
    assert first["hub_role"] == "launchpad-cn-ops-cn-northwest-1"
    assert set(first["pubkeys"]) == {KEY_ID}
    assert first["engine"] == {"endpoints": ["10.44.0.10:7443", "10.44.0.10:7444"],
                               "shards": 2, "key_s3_key": f"policy/gateway/{GW}/engine-key"}
    assert _state(eid)["bundle_pending"] is False and _state(eid)["bundle_version"] == 1

    _publish_in(eid, _create_in(eid, "forbid_payout", FORBID_PAYOUT))
    assert cas.pointer()["version"] == 2
    second = cas.bundle(2)
    assert second["attachment"]["policy_set_version"] == "ps-3"
    assert second["attachment"]["active_count"] == 2
    ok = _bundle_journal()[-1]
    assert ok.status == "succeeded" and ok.after["gateway_id"] == GW
    assert ok.after["attachment"]["engine_id"] == eid and ok.engine_id == eid


def test_two_gateways_on_one_engine_get_two_bundles_with_their_own_mode(engine, cas, gateway,
                                                                         engines, compiler,
                                                                         monkeypatch):
    _interceptor(monkeypatch, True)
    eid = _engine()
    _publish_in(eid, _create_in(eid, "permit_hr", PERMIT_HR))
    _attach(GW, eid, "ENFORCE")
    _attach(GW2, eid, "LOG_ONLY")
    assert gateway == [(GW, True), (GW2, True)]
    one, two = cas.bundle(cas.pointer(GW)["version"], GW), cas.bundle(1, GW2)
    assert one["attachment"]["mode"] == "ENFORCE" and two["attachment"]["mode"] == "LOG_ONLY"
    assert one["attachment"]["policy_set_version"] == two["attachment"]["policy_set_version"]
    assert two["gateway_arn"].endswith(f"gateway/{GW2}")
    assert two["engine"]["key_s3_key"] == f"policy/gateway/{GW2}/engine-key"
    # a publish rewrites BOTH; a mode change rewrites ONE
    _publish_in(eid, _create_in(eid, "forbid_payout", FORBID_PAYOUT))
    assert cas.pointer(GW)["version"] == 3 and cas.pointer(GW2)["version"] == 2
    _attach(GW2, eid, "ENFORCE")
    assert cas.pointer(GW)["version"] == 3 and cas.pointer(GW2)["version"] == 3
    assert cas.bundle(3, GW2)["attachment"]["mode"] == "ENFORCE"
    assert gateway == [(GW, True), (GW2, True)], "a mode change never touches the interceptor"
    # a detach retracts that gateway's attachment and leaves the other one alone
    _detach(GW2)
    assert gateway[-1] == (GW2, False)
    assert cas.bundle(4, GW2)["attachment"] is None and cas.pointer(GW)["version"] == 3
    parsed = interceptor_bundle.parse_bundle(cas.bundle(4, GW2), expect_gateway_id=GW2)
    assert parsed.attachment is None


def test_an_unpublished_engine_is_attached_fail_closed(engine, cas, gateway, engines, compiler,
                                                      monkeypatch):
    """The engine's set is not on the Dogwood engine (the platform switch is off while the
    attach happens): the bundle names no installed set — the Lambda denies, never passes."""
    _interceptor(monkeypatch, False)
    eid = _engine()
    _publish_in(eid, _create_in(eid, "permit_hr", PERMIT_HR))
    _attach(GW, eid, "ENFORCE")
    block = cas.bundle(1)["attachment"]
    assert "published" not in block and block["policy_set_version"] == "unpublished"
    assert block["active_count"] == 0  # nothing installed: the bundle governs nothing yet


def test_a_taken_version_and_a_newer_pointer_are_respected(engine, cas, gateway, engines,
                                                           compiler, monkeypatch):
    """The CAS writer: an existing v<n> moves the write to the next free version; a pointer
    already at a newer version is never moved back."""
    _interceptor(monkeypatch, True)
    eid = _engine()
    cas.objects[interceptor_bundle.bundle_key(GW, 1)] = b"{}"  # another writer's v1
    _publish_in(eid, _create_in(eid, "permit_hr", PERMIT_HR))
    _attach(GW, eid, "LOG_ONLY")
    assert interceptor_bundle.bundle_key(GW, 2) in cas.objects
    assert cas.pointer()["version"] == 2
    real_get = cas.get_object
    state = {"raced": False}

    def racing_get(*, Bucket, Key, **kw):
        out = real_get(Bucket=Bucket, Key=Key, **kw)
        if Key == interceptor_bundle.pointer_key(GW) and not state["raced"]:
            state["raced"] = True
            cas.objects[Key] = json.dumps(interceptor_bundle.pointer(GW, 9, "now")).encode()
        return out

    cas.get_object = racing_get
    _publish_in(eid, _create_in(eid, "forbid_payout", FORBID_PAYOUT))
    assert cas.pointer()["version"] == 9  # forward only


def test_a_bundle_write_failure_keeps_the_install_and_is_retried(engine, cas, gateway, engines,
                                                                 compiler, monkeypatch):
    _interceptor(monkeypatch, True)
    eid = _engine()
    _attach(GW, eid, "LOG_ONLY")
    cas.fail_prefix = f"policy/gateway/{GW}/"
    pid = _create_in(eid, "permit_hr", PERMIT_HR)
    _publish_in(eid, pid)
    db = SessionLocal()
    try:
        assert db.get(Policy, pid).status == "active"  # the install stands
    finally:
        db.close()
    assert _journal("engine.install")[-1].status == "succeeded"
    failed = _bundle_journal()[-1]
    assert failed.status == "failed" and failed.error == "policy.bundle_write_failed"
    assert _state(eid)["bundle_pending"] is True and cas.pointer()["version"] == 1
    cas.fail_prefix = None
    db = SessionLocal()
    try:
        assert policy_gateway_publish.reconcile(db) == [GW]
    finally:
        db.close()
    assert cas.pointer()["version"] == 2 and _state(eid)["bundle_pending"] is False


def test_nothing_is_written_without_the_bundle_settings(engine, cas, gateway, engines, compiler,
                                                        monkeypatch):
    _interceptor(monkeypatch, True)
    monkeypatch.setattr(get_settings(), "policy_engine_endpoints", [])
    eid = _engine()
    _publish_in(eid, _create_in(eid, "permit_hr", PERMIT_HR))
    _attach(GW, eid, "ENFORCE")
    assert cas.pointer() is None and _bundle_journal() == []
    assert _state(eid)["bundle_pending"] is True  # written once the settings are on


def test_the_hub_role_is_named_by_its_arn():
    assert policy_gateway_publish.hub_role_name(HUB_ROLE) == "launchpad-cn-ops-cn-northwest-1"
    from app.core.errors import AppError

    with pytest.raises(AppError):
        policy_gateway_publish.hub_role_name("arn:aws-cn:iam::1:user/bob")


def test_the_reconciler_retracts_a_detached_attachment_the_hub_missed(engine, cas, gateway,
                                                                     engines, compiler,
                                                                     monkeypatch):
    """A live bundle that still carries an attachment the ledger no longer has (the detach's
    write failed, a hand-written bundle) is rewritten by the reconciler — drift, one per pass."""
    _interceptor(monkeypatch, True)
    eid = _engine()
    _publish_in(eid, _create_in(eid, "permit_hr", PERMIT_HR))
    _attach(GW, eid, "ENFORCE")
    db = SessionLocal()
    try:
        db.delete(db.get(PolicyEngineAttachment, GW))  # the row goes, the bundle stays
        db.commit()
        assert cas.bundle(cas.pointer()["version"])["attachment"] is not None
        assert policy_gateway_publish.reconcile(db) == [GW]
        assert cas.bundle(cas.pointer()["version"])["attachment"] is None
        assert policy_gateway_publish.reconcile(db) == [], "in sync: nothing to do"
    finally:
        db.close()


def test_the_periodic_reconciler_writes_a_pending_bundle(engine, cas, gateway, engines,
                                                         compiler, monkeypatch):
    import threading

    _interceptor(monkeypatch, True)
    eid = _engine()
    _attach(GW, eid, "LOG_ONLY")
    cas.fail_prefix = f"policy/gateway/{GW}/"
    _publish_in(eid, _create_in(eid, "permit_hr", PERMIT_HR))
    assert _state(eid)["bundle_pending"] is True
    cas.fail_prefix = None
    waits: list[float] = []

    def wait(seconds: float) -> bool:  # two passes, then stop
        waits.append(seconds)
        return len(waits) > 2

    policy_gateway_publish.run_reconciler(threading.Event(), wait=wait)
    assert len(waits) == 3
    assert all(policy_gateway_publish.RECONCILE_EVERY_S <= w
               <= policy_gateway_publish.RECONCILE_EVERY_S
               + policy_gateway_publish.RECONCILE_JITTER_S for w in waits)
    assert cas.pointer()["version"] == 2 and _state(eid)["bundle_pending"] is False


def test_the_reconciler_does_nothing_without_the_bundle_settings(monkeypatch):
    monkeypatch.setattr(get_settings(), "policy_engine_endpoints", [])
    monkeypatch.setattr(get_settings(), "policy_gateway_interceptor", False)
    assert policy_gateway_publish.reconcile_once() == []
    assert policy_gateway_publish.start_reconciler() is False


def test_bundle_writes_hold_the_publish_lock(engine, cas, gateway, engines, compiler,
                                             monkeypatch):
    _interceptor(monkeypatch, True)
    eid = _engine()
    held: list[bool] = []
    real = policy_gateway_publish.compose

    def compose(*a, **k):
        held.append(policy_gateway_publish._PUBLISH_LOCK.locked())
        return real(*a, **k)

    monkeypatch.setattr(policy_gateway_publish, "compose", compose)
    _attach(GW, eid, "ENFORCE")
    assert held == [True]


def test_a_migrated_engine_state_is_reinstalled_under_the_engine_key_and_rewritten(
        engine, cas, gateway, engines, compiler, monkeypatch):
    """The migration carries the workspace-era set facts as a MIGRATED state (the Dogwood store
    still holds the set under the workspace key): the reconciler re-installs the engine's set
    under the engine key (``force``: past the CAS) and writes the bundle in the same pass."""
    _interceptor(monkeypatch, True)
    eid = _engine()
    _publish_in(eid, _create_in(eid, "permit_hr", PERMIT_HR))
    _attach(GW, eid, "ENFORCE")
    db = SessionLocal()
    try:
        row = db.get(PolicyEngine, eid)
        row.engine_state = {**row.engine_state, "migrated_from_workspace": WS,
                            "bundle_pending": True}
        db.commit()
        installs_before = len(engines[1])
        assert policy_gateway_publish.reconcile(db) == [GW]
        db.refresh(row)
        assert "migrated_from_workspace" not in row.engine_state
        assert row.engine_state["policy_set_version"] == "ps-3"
        forced = [m for _shard, m in engines[1][installs_before:] if m.get("op") == "install"]
        assert forced and all(m.get("force") is True and m["ws"] == eid for m in forced)
    finally:
        db.close()
    assert cas.bundle(cas.pointer()["version"])["attachment"]["policy_set_version"] == "ps-3"
