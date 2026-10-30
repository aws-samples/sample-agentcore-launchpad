# ruff: noqa: F811 — pytest fixtures are imported explicitly from test_policy_deploy_narrowing
"""Stream A5 (part 1) — publish / retire compile the ACTIVE set and install it on every engine
shard before the ledger commits (design §15.1, contract I5 / I6): a refused or failed install
leaves the previous set live and is journalled; set versions advance as the contract says."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import types
from dataclasses import dataclass, field

import httpx
import jsonschema
import pytest

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.models.policy import Policy, PolicyChange, PolicyVersion
from app.policy import contracts
from app.services import policy_engine_control as control
from app.services import policy_engine_sync as sync
from app.services import policy_store, principal_mint
from tests.policy_principal.fakes import FakeEngine, FakeEngineDown, ephemeral_hmac_key
from tests.test_policy_deploy_narrowing import (  # noqa: F401
    PERMIT_HR,
    WS,
    engine,
    s3,
    workspace,
)

FORBID_PAYOUT = (
    'forbid(principal, action == Launchpad::Action::"call_tool", '
    'resource == Launchpad::Tool::"hr-database___create_payout");'
)


@dataclass
class FakeCompiled:
    ok: bool
    schema: str
    items: list
    errors: list
    warnings: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    temporal_count: int = 0
    temporal_referenced_actions: list = field(default_factory=list)


class FakeCompiler:
    """Stand-in for app.policy.dogwood_compiler (stream C3): one item per policy; a policy
    whose statement mentions `@temporal` is temporal; `@broken` fails the compile."""

    def __init__(self) -> None:
        self.calls: list[tuple[dict, list]] = []
        self.rewrites: list[str] = []

    def rewrite_d20(self, statement):
        self.rewrites.append(statement)
        return types.SimpleNamespace(statement=statement, warnings=[])

    def compile(self, tools_list, policies):
        self.calls.append((tools_list, policies))
        if any("@broken" in p["statement"] for p in policies):
            return FakeCompiled(False, "", [], [{"policy_id": policies[0]["id"],
                                                 "message": "does not validate"}])
        items = [{"id": p["id"], "version": p["version"], "mode": p["mode"], "stage": "bt",
                  "kind": "temporal" if "@temporal" in p["statement"] else "stateless",
                  "text": p["statement"]} for p in policies]
        temporal = sum(1 for i in items if i["kind"] == "temporal")
        # the REAL compiler's shape: BARE tool names (N17-obs488 — this fake used to answer the
        # entity form, which is why the hub's bare-name bundle was never caught here; the hub
        # converts in policy_engine_sync.gateway_actions, never the compiler)
        return FakeCompiled(True, "namespace AgentCore { entity Gateway; }", items, [],
                            actions=["hr-database___create_payout"],
                            temporal_count=temporal)


@pytest.fixture
def engines(monkeypatch):
    shards = [FakeEngine(key=ephemeral_hmac_key(), shard=i) for i in range(2)]
    seen: list[tuple[int, dict]] = []

    def call(shard, message, **_kw):
        seen.append((shard, message))
        try:
            return shards[shard].control(message)
        except FakeEngineDown as exc:
            raise control.EngineControlError("unavailable", str(exc)) from None

    monkeypatch.setattr(control, "call", call)
    return shards, seen


@pytest.fixture
def compiler(monkeypatch) -> FakeCompiler:
    fake = FakeCompiler()
    monkeypatch.setattr(sync, "compiler", lambda: fake)
    monkeypatch.setattr(sync, "tools_list", lambda _ws: {"tools": [{"name": "hr-database___x"}]})
    return fake


def _interceptor(monkeypatch, on: bool, *, publish: bool | None = None) -> None:
    """The platform switch (Global model: the workspace switches are gone; ``publish`` is
    accepted for the older call sites and ignored)."""
    monkeypatch.setattr(get_settings(), "policy_gateway_interceptor", on)


def _create(name: str, statement: str) -> str:
    db = SessionLocal()
    try:
        return policy_store.create_policy(db, WS, name=name, statement=statement,
                                          operator="demo-admin").id
    finally:
        db.close()


def _publish(workspace, pid: str):
    db = SessionLocal()
    try:
        return policy_store.publish_policy(db, workspace, pid, operator="demo-admin")
    finally:
        db.close()


def _row(model, **kw):
    db = SessionLocal()
    try:
        return db.query(model).filter_by(**kw).first()
    finally:
        db.close()


def _eid() -> str:
    """The workspace's default engine id — the Dogwood set key (Global model)."""
    db = SessionLocal()
    try:
        return policy_store.settings_row(db, WS).id
    finally:
        db.close()


def _settings():
    """``(policy_set_seq, temporal_set_version)`` of the workspace's default engine."""
    db = SessionLocal()
    try:
        row = policy_store.settings_row(db, WS)
        return row.policy_set_seq, row.temporal_set_version
    finally:
        db.close()


def _journal(operation: str) -> list[PolicyChange]:
    db = SessionLocal()
    try:
        return db.query(PolicyChange).filter_by(workspace_id=WS, operation=operation) \
            .order_by(PolicyChange.created_at).all()
    finally:
        db.close()


def _statement(text: str, marker: str) -> str:
    # a valid D20 statement with a marker the fake compiler reads (a Cedar comment)
    return f"// {marker}\n{text}"


def test_switch_off_publishes_exactly_as_before(engine, s3, workspace, engines, compiler,
                                                monkeypatch):
    _interceptor(monkeypatch, False)
    pid = _create("permit_hr", PERMIT_HR)
    _publish(workspace, pid)
    assert compiler.calls == [] and engines[1] == []
    assert _journal("engine.install") == []
    assert _row(Policy, id=pid).status == "active"


def test_attached_gateway_http_failure_preserves_the_published_set(
    engine, s3, workspace, engines, compiler, monkeypatch, client,
):
    """r29: a successful validation did not prevent a later tools/list HTTP 500."""
    from app.models.policy import PolicyEngineAttachment
    from app.services import mcp_client
    from app.services.agentcore import client as agentcore_client
    from app.services.agentcore import gateway

    _interceptor(monkeypatch, True)
    active = _create("existing", PERMIT_HR)
    _publish(workspace, active)
    with SessionLocal() as db:
        row = policy_store.settings_row(db, WS)
        candidate = policy_store.create_policy(
            db, row, name="itsm_resolve_servicedesk_only", dialect="dogwood",
            statement='forbid(principal, action == AgentCore::Action::'
                      '"servicenow___resolve_ticket", resource) unless {'
                      'principal == AgentCore::User::"itdesk01" };',
            operator="demo-admin",
        ).id
        db.add(PolicyEngineAttachment(engine_id=row.id, gateway_id="workshop-gateway"))
        db.commit()
    monkeypatch.setattr(agentcore_client, "control_client", lambda _: object())
    monkeypatch.setattr(gateway, "get_gateway", lambda *_: {
        "gatewayUrl": "https://gateway.example.test/mcp", "gatewayArn": "test-gateway",
    })

    calls = []
    monkeypatch.setattr(sync.time, "sleep", lambda _: None)

    def unavailable(_, **kwargs):
        calls.append(kwargs)
        request = httpx.Request("POST", "https://gateway.example.test/mcp?secret=hidden")
        httpx.Response(500, request=request, text="private upstream body").raise_for_status()

    monkeypatch.setattr(mcp_client, "tools_list", unavailable)
    before = _settings()
    seen = len(engines[1])
    route = f"/api/policy-engines/{_eid()}"
    response = client.post(f"{route}/policies/{candidate}/publish", json={})
    assert response.status_code == 409
    error = response.json()
    assert error["code"] == "policy.engine_install_failed"
    assert error["detail"] == {
        "source": "tools_list", "reason": "HTTPStatusError",
        "upstream_status": 500, "attempts": 3,
    }
    assert calls == [{"timeout": 20}] * 3
    assert "hidden" not in response.text and "private upstream" not in response.text
    assert _settings() == before
    assert len(engines[1]) == seen
    assert _row(Policy, id=active).active_version == 1
    assert _row(Policy, id=candidate).active_version is None
    assert _row(Policy, id=candidate).draft_version == 1
    assert _journal("engine.install")[-1].status == "failed"
    assert _journal("policy.publish")[-1].status == "failed"
    assert all(shard.sets[_eid()] == f"ps-{before[0]}" for shard in engines[0])
    version = _row(PolicyVersion, policy_id=candidate, version=1)
    assert version.published_at is None and version.s3_key is None
    for dialect in ("cedar", "dogwood"):
        response = client.post(f"{route}/validate", json={
            "statement": PERMIT_HR if dialect == "cedar" else version.statement,
        })
        assert response.status_code == 409
        assert response.json()["code"] == "policy.compiler_unavailable"
        assert response.json()["detail"] == error["detail"]
        assert _settings() == before and len(engines[1]) == seen

    # An explicit later publish reads a fresh schema, tolerates one transient read,
    # and installs exactly once on each shard. The publish itself is never retried.
    attempts = []

    def recovered(_, **kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            return unavailable(_, **kwargs)
        return [{"name": "servicenow___resolve_ticket"}]

    monkeypatch.setattr(mcp_client, "tools_list", recovered)
    response = client.post(f"{route}/policies/{candidate}/publish", json={})
    assert response.status_code == 200, response.text
    assert len(attempts) == 2
    assert _settings() == (before[0] + 1, before[1])
    installs = [msg for _, msg in engines[1][seen:] if msg["op"] == "install"]
    assert len(installs) == 2
    assert _row(Policy, id=candidate).active_version == 1


def test_publish_installs_on_every_shard_before_it_commits(engine, s3, workspace, engines,
                                                           compiler, monkeypatch):
    _interceptor(monkeypatch, True)
    pid = _create("permit_hr", PERMIT_HR)
    _publish(workspace, pid)
    shards, seen = engines
    installs = [(s, m) for s, m in seen if m["op"] == "install"]
    assert [s for s, _ in installs] == [0, 1]
    validator = jsonschema.Draft202012Validator(contracts.load_schema("engine_control_request"))
    for _shard, message in installs:
        validator.validate(message)
        assert message["ws"] == _eid() and message["policy_set_version"] == "ps-1"
        assert [p["id"] for p in message["policies"]] == [pid]
    assert all(e.sets[_eid()] == "ps-1" for e in shards)
    assert _settings() == (1, 0)
    assert _row(Policy, id=pid).status == "active"
    assert _row(PolicyVersion, policy_id=pid, version=1).kind == "stateless"
    ok = _journal("engine.install")[-1]
    assert ok.status == "succeeded" and ok.after["policy_set_version"] == "ps-1"
    # the compiler saw the gateway's tools and the would-be ACTIVE set
    tools, policies = compiler.calls[-1]
    assert tools == {"tools": [{"name": "hr-database___x"}]}
    # item 5 (Global parity): a new policy is installed log_only until promoted
    assert policies == [{"id": pid, "version": 1, "mode": "log_only", "statement": PERMIT_HR}]


def test_the_temporal_set_version_moves_only_with_the_temporal_items(
        engine, s3, workspace, engines, compiler, monkeypatch):
    _interceptor(monkeypatch, True)
    first = _create("permit_hr", PERMIT_HR)
    _publish(workspace, first)
    assert _settings() == (1, 0)
    temporal = _create("payout_cap", _statement(FORBID_PAYOUT, "@temporal"))
    _publish(workspace, temporal)
    assert _settings() == (2, 1)
    assert _row(PolicyVersion, policy_id=temporal, version=1).kind == "temporal"
    another = _create("permit_more", PERMIT_HR.replace("hr-database", "office-facts"))
    _publish(workspace, another)
    assert _settings() == (3, 1)  # a stateless change keeps open temporal sessions
    db = SessionLocal()
    try:
        policy_store.retire_policy(db, WS, temporal, operator="demo-admin")
    finally:
        db.close()
    assert _settings() == (4, 2)  # the temporal set changed again
    db = SessionLocal()
    try:
        # the psid of new tokens follows the engine ATTACHED to the workspace gateway
        from app.models.ledger import Workspace
        from app.models.policy import PolicyEngineAttachment

        assert principal_mint.temporal_set_version(db, WS) == 0  # nothing attached yet
        db.get(Workspace, WS).resources = {"gateway_id": "gw-sync"}
        db.add(PolicyEngineAttachment(gateway_id="gw-sync", engine_id=_eid(), mode="ENFORCE"))
        db.commit()
        assert principal_mint.temporal_set_version(db, WS) == 2
    finally:
        db.close()


def test_a_set_that_does_not_compile_is_refused_and_nothing_goes_live(
        engine, s3, workspace, engines, compiler, monkeypatch):
    # Legacy draft created before Gateway preflight existed; publish must still recheck it.
    _interceptor(monkeypatch, False)
    pid = _create("broken_one", _statement(PERMIT_HR, "@broken"))
    _interceptor(monkeypatch, True)
    with pytest.raises(AppError) as err:
        _publish(workspace, pid)
    assert err.value.code == "policy.invalid_statement" and err.value.status_code == 422
    assert _row(Policy, id=pid).status == "draft"
    assert engines[1] == [] and _settings() == (0, 0)
    assert _journal("engine.install")[-1].status == "failed"
    assert _journal("policy.publish")[-1].status == "failed"


def test_failed_draft_install_keeps_previous_version_and_all_shards(
        engine, s3, workspace, engines, compiler, monkeypatch):
    _interceptor(monkeypatch, True)
    pid = _create("replacement", PERMIT_HR)
    _publish(workspace, pid)
    with SessionLocal() as db:
        policy_store.update_policy(db, WS, pid, operator="demo-admin",
                                   statement=FORBID_PAYOUT)
    previous_sets = [dict(shard.sets) for shard in engines[0]]
    previous_settings = _settings()
    previous_installs = len(engines[1])
    # Schema/compiler failure AFTER successful draft validation.
    monkeypatch.setattr(compiler, "compile", lambda *_: FakeCompiled(
        False, "", [], [{"policy_id": pid, "message": "attached tool schema changed"}]))
    with pytest.raises(AppError) as err:
        _publish(workspace, pid)
    assert err.value.detail["errors"][0]["message"] == "attached tool schema changed"
    row = _row(Policy, id=pid)
    assert (row.status, row.active_version, row.draft_version) == ("active", 1, 2)
    assert _row(PolicyVersion, policy_id=pid, version=2).published_at is None
    assert _settings() == previous_settings
    assert [dict(shard.sets) for shard in engines[0]] == previous_sets
    assert len(engines[1]) == previous_installs  # no shard install was attempted
    assert _journal("engine.install")[-1].status == "failed"
    assert "attached tool schema changed" in _journal("engine.install")[-1].error


@pytest.mark.parametrize("fault", ["refuse_shard_1", "down"])
def test_an_engine_that_refuses_or_is_down_leaves_the_previous_set_live(
        engine, s3, workspace, engines, compiler, monkeypatch, fault):
    _interceptor(monkeypatch, True)
    live = _create("permit_hr", PERMIT_HR)
    _publish(workspace, live)
    shards, _seen = engines
    if fault == "down":
        shards[0].down = True
    else:
        original = shards[1].control
        shards[1].control = lambda m: ({"ok": False, "errors": ["set_mismatch"]}
                                       if m["op"] == "install" else original(m))
    pid = _create("forbid_payout", FORBID_PAYOUT)
    with pytest.raises(AppError) as err:
        _publish(workspace, pid)
    assert err.value.code == "policy.engine_install_failed" and err.value.status_code == 409
    assert _row(Policy, id=pid).status == "draft"  # rolled back
    assert _row(Policy, id=live).status == "active"  # the previous set is live
    assert _settings() == (1, 0)
    failed = _journal("engine.install")[-1]
    assert failed.status == "failed" and failed.requested == {"trigger": "policy.publish"}
    # N17-obs492 (re-review F3): the shard that accepted ps-2 is rolled back to ps-1 (forced,
    # past the engine's version CAS) so the shards never stay on different sets
    if fault != "down":
        assert shards[0].sets[_eid()] == "ps-1", "shard 0 was left on the refused publish's set"
    assert all(s.sets.get(WS) in (None, "ps-1") for s in shards)


def test_a_partial_install_the_hub_could_not_roll_back_is_resynced_with_force(
        engine, s3, workspace, engines, compiler, monkeypatch):
    """The hub restarted between the partial install and the next reconcile (no previous set
    known here): the shard ahead on ps-2 refuses ps-2 again (CAS), so the reconcile's
    shard-disagreement path installs with ``force`` and both shards agree afterwards."""
    _interceptor(monkeypatch, True)
    _publish(workspace, _create("permit_hr", PERMIT_HR))
    shards, seen = engines
    assert all(s.sets[_eid()] == "ps-1" for s in shards)
    shards[0].sets[_eid()] = "ps-2"  # the leftover of a partial install nobody rolled back
    monkeypatch.setattr(control, "_LAST_INSTALLED", {})
    db = SessionLocal()
    try:
        assert sync.reconcile_workspace(db, WS) is not None
        db.commit()
    finally:
        db.close()
    installs = [m for _, m in seen if m.get("op") == "install" and m.get("force")]
    assert installs and installs[-1]["policy_set_version"] == "ps-2"
    assert all(s.sets[_eid()] == "ps-2" for s in shards)
    assert _settings()[0] == 2


def test_a_missing_engine_binary_is_an_install_failure_not_a_crash(
        engine, s3, workspace, engines, monkeypatch):
    from app.policy import dogwood_compiler

    _interceptor(monkeypatch, False)
    pid = _create("permit_hr", PERMIT_HR)
    _interceptor(monkeypatch, True)
    monkeypatch.setattr(sync, "tools_list", lambda _ws: {"tools": []})

    def missing():
        raise dogwood_compiler.EngineUnavailable("launchpad-policy-engine binary not found")

    monkeypatch.setattr(dogwood_compiler, "engine_bin", missing)
    with pytest.raises(AppError) as err:
        _publish(workspace, pid)  # the real compiler module, its binary gone
    assert err.value.code == "policy.engine_install_failed"
    assert err.value.detail["reason"] == "EngineUnavailable"
    assert _row(Policy, id=pid).status == "draft"


def test_stored_d20_statements_are_rewritten_for_the_engine_only(
        engine, s3, workspace, engines, compiler, monkeypatch):
    _interceptor(monkeypatch, True)
    pid = _create("permit_hr", PERMIT_HR)
    _publish(workspace, pid)
    assert compiler.rewrites == [PERMIT_HR, PERMIT_HR]  # preflight and install use the same rewrite
    assert _row(PolicyVersion, policy_id=pid, version=1).statement == PERMIT_HR  # stored as is
    db = SessionLocal()
    try:
        assert sync.engine_statement("permit(principal, action, resource);", "dogwood") == (
            "permit(principal, action, resource);", [])
    finally:
        db.close()


def test_a_retire_the_engine_refuses_keeps_the_policy_active(engine, s3, workspace, engines,
                                                             compiler, monkeypatch):
    _interceptor(monkeypatch, True)
    pid = _create("permit_hr", PERMIT_HR)
    _publish(workspace, pid)
    engines[0][0].down = True
    db = SessionLocal()
    try:
        with pytest.raises(AppError) as err:
            policy_store.retire_policy(db, WS, pid, operator="demo-admin")
    finally:
        db.close()
    assert err.value.code == "policy.engine_install_failed"
    assert _row(Policy, id=pid).status == "active"


# ── the control client over a real Unix socket ─────────────────────────────


@pytest.fixture
def socket_dir(monkeypatch):
    # AF_UNIX paths are short: a fresh dir under /tmp, not pytest's long tmp_path
    path = tempfile.mkdtemp(prefix="lpctl")
    monkeypatch.setattr(contracts, "ENGINE_CONTROL_SOCKET", os.path.join(path, "c-{shard}.sock"))
    yield path


def _serve(path: str, answer) -> threading.Thread:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)

    def run():
        conn, _ = server.accept()
        with conn:
            data = b""
            while b"\n" not in data:
                data += conn.recv(4096)
            conn.sendall(answer(json.loads(data.split(b"\n")[0])))
        server.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_install_all_speaks_json_lines_to_every_shard(socket_dir):
    got: list[dict] = []

    def ok(message):
        got.append(message)
        return b'{"ok": true, "errors": []}\n'

    threads = [_serve(control.socket_path(i), ok) for i in range(control.shard_count())]
    message = {"op": "install", "ws": "default", "policy_set_version": "ps-1",
               "temporal_set_version": 0, "policies": [], "schema": "x", "event_schema": ""}
    answers = control.install_all(message)
    for t in threads:
        t.join(2)
    assert answers == [{"ok": True, "errors": []}] * control.shard_count()
    assert got == [message] * control.shard_count()


def test_an_unreachable_or_garbled_shard_is_unavailable(socket_dir):
    with pytest.raises(control.EngineControlError) as err:
        control._call_socket(0, {"op": "status"})
    assert err.value.code == "unavailable"
    thread = _serve(control.socket_path(1), lambda _m: b"not json\n")
    with pytest.raises(control.EngineControlError) as err:
        control._call_socket(1, {"op": "status"})
    thread.join(2)
    assert err.value.code == "unavailable"


# ── the real Dogwood compiler (stream C), when its binary is built ──────────


def _engine_built() -> bool:
    from app.policy import dogwood_compiler

    try:
        dogwood_compiler.engine_bin()
        return True
    except dogwood_compiler.EngineUnavailable:
        return False


@pytest.mark.skipif(not _engine_built(), reason="engine binary not built")
def test_a_stored_d20_policy_publishes_through_the_real_compiler(
        engine, s3, workspace, engines, monkeypatch):
    from pathlib import Path

    repo = Path(__file__).resolve().parents[3]
    tools = json.loads((repo / "policy-engine" / "crates" / "lp-dogwood" / "assets"
                        / "demo-tools.json").read_text())
    tools = tools.get("result", tools)
    template = (repo / "backend" / "app" / "policy" / "templates"
                / "forbid_tool_for_members.cedar").read_text()
    statement = template.replace("__TOOL_ID__", "hr-database___create_payout")
    monkeypatch.setattr(sync, "tools_list", lambda _ws: tools)
    _interceptor(monkeypatch, True)
    pid = _create("payout_admin_only", statement)
    _publish(workspace, pid)
    shards, seen = engines
    installs = [m for _s, m in seen if m["op"] == "install"]
    assert len(installs) == 2
    validator = jsonschema.Draft202012Validator(contracts.load_schema("engine_control_request"))
    validator.validate(installs[0])
    items = installs[0]["policies"]
    ours = [i for i in items if i["id"] != "closure"]  # + the compiler's D7 closure permit
    assert ours and all(i["id"].split("#")[0] == pid and i["version"] == 1 for i in ours)
    assert {i["id"] for i in items} - {i["id"] for i in ours} <= {"closure"}
    assert all("Launchpad::" not in i["text"] for i in items)  # the engine sees AgentCore
    assert {i["stage"] for i in items} <= {"a", "bt"}
    assert _row(Policy, id=pid).status == "active"
    assert _row(PolicyVersion, policy_id=pid, version=1).kind == "stateless"
    assert _row(PolicyVersion, policy_id=pid, version=1).statement == statement  # stored as is


def test_a_compiler_whose_action_format_changed_fails_the_install_as_a_409(
        engine, s3, workspace, engines, compiler, monkeypatch):
    """N17-obs488 (review r17-bl): `gateway_actions` stays strict — a compiler build that emits
    entity-formatted ids (or anything but bare tool names) is a producer regression that must fail
    loudly — but it fails as the install's 409 `policy.engine_install_failed` with reason
    `compiler_output_format`, BEFORE any shard receives the set, never as a 500."""
    _interceptor(monkeypatch, True)
    live = _create("permit_hr", PERMIT_HR)
    _publish(workspace, live)
    shards, seen = engines
    original = compiler.compile

    def entity_form(tools_list, policies):
        out = original(tools_list, policies)
        out.actions = ['AgentCore::Action::"hr-database___create_payout"']
        return out

    monkeypatch.setattr(compiler, "compile", entity_form)
    installs_before = sum(1 for _, m in seen if m.get("op") == "install")
    pid = _create("forbid_payout", FORBID_PAYOUT)
    with pytest.raises(AppError) as err:
        _publish(workspace, pid)
    assert (err.value.code, err.value.status_code) == ("policy.engine_install_failed", 409)
    assert err.value.detail["reason"] == "compiler_output_format"
    assert sum(1 for _, m in seen if m.get("op") == "install") == installs_before, \
        "no shard may receive a set whose action ids could not be composed"
    assert _row(Policy, id=pid).status == "draft" and _row(Policy, id=live).status == "active"
    assert all(s.sets[_eid()] == "ps-1" for s in shards)
    assert _journal("engine.install")[-1].status == "failed"
