"""Stream B against the REAL engine (stream C's ``launchpad-policy-engine``): the interceptor
Lambda code, over its own stdlib HTTP transport and the vendored codec, drives the Rust
service end to end — HMAC, the amendment-1 entities, the amendment-3 set handshake.

Runs against this worktree's release binary (``policy-engine/target/release/…``, built by the
gate's policy-engine step before the backend pytest step) and the crate's assets; skipped,
never failed, while the binary is absent. ``LP_ENGINE_BIN`` / ``LP_ENGINE_ASSETS`` override
(``tests/policy_principal/conftest.py``):

    cd policy-engine && cargo build --locked --release
    cd backend && uv run pytest tests/policy_principal/test_interceptor_real_engine.py -q

The set is installed under the /3 fixture's ``attachment.engine_id`` — the Global model keys
one Dogwood set per ENGINE, and the Lambda asks the engine for ``ws = engine_id``
(``core.py``); installing under the legacy workspace key answered every call with
``temporal_set_mismatch`` (N17-obs516).
"""

from __future__ import annotations

import json
import secrets
import socket
import subprocess
import time
from types import SimpleNamespace

import pytest

from app.policy import contracts
from app.policy.interceptor import bundle as bundle_mod
from app.policy.interceptor import core, handler
from app.policy.interceptor.bundle import BundleSource, bundle_key, pointer_key
from app.policy.interceptor.engine_client import EngineClient, http_transport
from tests.policy_principal.conftest import (
    ENGINE_ASSETS,
    ENGINE_BIN,
    load_fixture,
    needs_real_engine,
)
from tests.policy_principal.fakes import EphemeralSigner

pytestmark = needs_real_engine

GW = "launchpad-gw-exampleid0"
BUCKET = "bkt"
ROLE_ARN = "arn:aws-cn:iam::123456789012:role/launchpad-agent-hr-helpdesk-ab12cd34"
POLICIES = [
    {"id": "pol_agent", "version": 1, "mode": "active", "stage": "a", "kind": "stateless",
     "text": 'permit (principal in AgentCore::Agent::"ag_hrhelp01", '
             'action in AgentCore::Action::"CallTool", resource);'},
    {"id": "pol_payoutadm", "version": 2, "mode": "active", "stage": "bt", "kind": "stateless",
     "text": 'forbid (principal, action == AgentCore::Action::"hr-database___create_payout", '
             'resource) unless { principal in AgentCore::Role::"admin" };'},
    {"id": "pol_users", "version": 1, "mode": "active", "stage": "bt", "kind": "stateless",
     "text": 'permit (principal, action in AgentCore::Action::"CallTool", resource);'},
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def engine(tmp_path):
    key = secrets.token_bytes(contracts.ENGINE_KEY_BYTES)
    (tmp_path / "key").write_bytes(key)
    port, control = _free_port(), tmp_path / "control.sock"
    proc = subprocess.Popen(
        [str(ENGINE_BIN), "--shard", "0", "--shards", "1",
         "--state-dir", str(tmp_path / "state"),
         "--listen", f"127.0.0.1:{port}", "--control", str(control),
         "--key-file", str(tmp_path / "key"), "--audit", str(tmp_path / "audit.jsonl"),
         "--workers", "2"],
        stderr=subprocess.PIPE)
    deadline = time.time() + 15
    while time.time() < deadline and not control.exists():
        time.sleep(0.05)
    # the set key is the ENGINE id the bundle's attachment names — what the Lambda sends as
    # ``ws`` on every decide (amendment 11: one set per engine, not per workspace)
    engine_id = load_fixture("gateway_bundle.json")["attachment"]["engine_id"]
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(str(control))
        s.sendall((json.dumps({
            "op": "install", "ws": engine_id, "policy_set_version": "ps-1",
            "temporal_set_version": 0, "policies": POLICIES,
            "schema": (ENGINE_ASSETS / "demo.cedarschema").read_text(encoding="utf-8"),
            "event_schema": ""}) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            data += s.recv(65536)
    assert json.loads(data) == {"ok": True, "errors": []}, data
    try:
        yield SimpleNamespace(port=port, key=key, engine_id=engine_id)
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _deps(engine, signer, *, policy_set_version="ps-1"):
    raw = load_fixture("gateway_bundle.json")
    raw["pubkeys"] = {signer.kid: signer.public_spki_b64}
    raw["engine"]["endpoints"] = [f"127.0.0.1:{engine.port}"]
    raw["engine"]["shards"] = 1
    attachment = raw["attachment"]
    assert attachment["engine_id"] == engine.engine_id  # the installed set IS this bundle's
    attachment.update(policy_set_version=policy_set_version, temporal_set_version=0,
                      temporal_count=0, temporal_referenced_actions=[])
    objects = {
        bundle_key(GW, raw["version"]): json.dumps(raw).encode(),
        pointer_key(GW): json.dumps(bundle_mod.pointer(GW, raw["version"], "t")).encode(),
        raw["engine"]["key_s3_key"]: engine.key,
    }
    source = BundleSource(bucket=BUCKET, gateway_id=GW, fetch=lambda _b, k: objects[k])
    lines: list[dict] = []
    deps = core.Deps(
        source=source, audit=lines.append,
        engine=lambda b, k: EngineClient(endpoints=b.endpoints, shards=b.shards, key=k,
                                         transport=http_transport),
        clock=time.time)
    deps.lines = lines  # the decision lines the handler emitted, in order
    return deps, raw


def _event(token: str, tool: str = "hr-database___create_payout"):
    event = load_fixture("interceptor_events/request_tools_call_role.json")["event"]
    params = event["mcp"]["gatewayRequest"]["body"]["params"]
    params["name"] = tool
    params["arguments"] = {"employee_id": "EMP-1024", "amount": 420}
    params["_meta"]["launchpad/principal"] = token
    event["mcp"]["gatewayRequest"]["context"]["identity"]["awsPrincipalArn"] = ROLE_ARN
    return event


def _token(signer, username: str, role: str) -> str:
    now = int(time.time())
    claims = dict(load_fixture("claims_valid.json")["cases"][0]["claims"])
    claims.update(iat=now - 10, exp=now + 600, jti=f"jti-{username}-{now}",
                  p={"kind": "user", "id": username, "label": username, "role": role})
    return signer.token(claims)


def _run(deps, raw, event):
    context = SimpleNamespace(aws_request_id="r", client_context=SimpleNamespace(
        custom={"GATEWAY_ARN": raw["gateway_arn"], "REQUEST_ID": "gw-1"}))
    return handler.handle(event, context, deps)


def test_member_payout_is_denied_by_the_policy_and_an_admin_passes(engine):
    signer = EphemeralSigner()
    deps, raw = _deps(engine, signer)
    out = _run(deps, raw, _event(_token(signer, "hr-intern", "member")))
    error = out["mcp"]["transformedGatewayResponse"]["body"]["error"]
    assert error["message"] == contracts.deny_message(
        "Policy evaluation denied due to pol_payoutadm"), error
    assert error["data"]["stage"] == "bt"
    # the decision line names the engine whose set decided (the Global-model row keys)
    (line,) = deps.lines
    row = line["launchpad.policy.decision"]
    assert row["decision"] == "deny" and row["stage_bt"] == "deny", row
    assert row["engine_id"] == engine.engine_id and row["gateway_id"] == GW, row
    assert row["policy_ids"] == ["pol_payoutadm"], row
    out = _run(deps, raw, _event(_token(signer, "demo-admin", "admin")))
    assert "transformedGatewayRequest" in out["mcp"], out
    out = _run(deps, raw, _event(_token(signer, "hr-intern", "member"),
                                 tool="hr-database___get_employee"))
    assert "transformedGatewayRequest" in out["mcp"], out
    assert [ln["launchpad.policy.decision"]["decision"] for ln in deps.lines] == [
        "deny", "allow", "allow"], deps.lines


def test_a_bundle_on_another_set_version_is_refused_by_the_handshake(engine):
    signer = EphemeralSigner()
    deps, raw = _deps(engine, signer, policy_set_version="ps-0")
    out = _run(deps, raw, _event(_token(signer, "demo-admin", "admin")))
    error = out["mcp"]["transformedGatewayResponse"]["body"]["error"]
    assert error["data"]["reason_code"] == "temporal_set_mismatch", error


def test_a_wrong_hmac_key_is_unauthorized(engine):
    signer = EphemeralSigner()
    deps, raw = _deps(engine, signer)
    deps.engine = lambda b, _k: EngineClient(endpoints=b.endpoints, shards=b.shards,
                                             key=b"x" * 32, transport=http_transport)
    out = _run(deps, raw, _event(_token(signer, "demo-admin", "admin")))
    error = out["mcp"]["transformedGatewayResponse"]["body"]["error"]
    assert error["data"]["reason_code"] == "engine_unauthorized", error
