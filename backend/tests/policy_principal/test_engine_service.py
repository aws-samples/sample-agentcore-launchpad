"""The real engine service (policy-engine/, stream C) driven over its I4 / I5 contract with
the stage-0 Python codec — proves the cross-language HMAC, the JSON shapes and the demo
decisions end to end. Skipped unless the binary is built (``cargo build --release`` in
``policy-engine/``) or ``LP_ENGINE_BIN`` points at it."""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import jsonschema
import pytest

from app.policy import contracts
from app.policy.contracts import codec

REPO = Path(__file__).resolve().parents[3]
ENGINE = Path(os.environ.get(
    "LP_ENGINE_BIN", REPO / "policy-engine" / "target" / "release" / "launchpad-policy-engine",
))
ASSETS = REPO / "policy-engine" / "crates" / "lp-dogwood" / "assets"

pytestmark = pytest.mark.skipif(not ENGINE.exists(), reason="engine binary not built")

GW = 'AgentCore::Gateway::"arn:aws-cn:bedrock-agentcore:cn-northwest-1:123456789012:gateway/gw0"'
ROLE = 'AgentCore::IamEntity::"arn:aws-cn:iam::123456789012:role/launchpad-agent-hr-ab12cd34"'
AGENT = {"entity": ROLE, "agent_id": "ag_hrhelp01", "method": "zip_runtime", "system": False}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def engine(tmp_path):
    key = secrets.token_bytes(32)  # amendment 3: 32 raw bytes
    (tmp_path / "key").write_bytes(key)
    port = _free_port()
    control = tmp_path / "control.sock"
    proc = subprocess.Popen(
        [str(ENGINE), "--shard", "0", "--shards", "1", "--state-dir", str(tmp_path / "state"),
         "--listen", f"127.0.0.1:{port}", "--control", str(control),
         "--key-file", str(tmp_path / "key"), "--audit", str(tmp_path / "audit.jsonl"),
         "--workers", "2"],
        stderr=subprocess.PIPE,
    )
    deadline = time.time() + 15
    while time.time() < deadline and not control.exists():
        time.sleep(0.05)
    try:
        yield {"port": port, "key": key, "control": control,
               "audit": tmp_path / "audit.jsonl"}
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _control(engine, message: dict) -> dict:
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(str(engine["control"]))
        s.sendall((json.dumps(message) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            data += s.recv(65536)
    return json.loads(data)


def _post(engine, path: str, payload: dict, *, key: bytes | None = None) -> tuple[int, dict]:
    body = json.dumps(payload).encode()
    ts, nonce = str(int(time.time())), uuid.uuid4().hex
    sig = codec.engine_signature(key or engine["key"], "POST", path, ts, nonce, body)
    req = urllib.request.Request(
        f"http://127.0.0.1:{engine['port']}{path}", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-LP-Ts": ts, "X-LP-Nonce": nonce,
                 "X-LP-Sig": sig},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def _schema_text() -> str:
    # the committed golden the Rust generator test keeps in sync
    return (ASSETS / "demo.cedarschema").read_text(encoding="utf-8")


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


def _decide_body(principal: dict, tool: str, input_: dict) -> dict:
    psid = codec.psid_for("default", "s" * 40, 0)
    return {
        "ws": "default",
        "session": codec.session_key("default", psid, "user", principal["attrs"]["username"]),
        "record": False, "action": f'AgentCore::Action::"{tool}"', "resource": GW,
        "agent": AGENT, "principal": principal,
        "context": {"input": input_, "agent_id": "ag_hrhelp01", "channel": "chat"},
    }


def _user(name: str, role: str) -> dict:
    return {"entity": f'AgentCore::User::"{name}"', "parents": [f'AgentCore::Role::"{role}"'],
            "attrs": {"username": name, "role": role}}


def test_install_decide_and_hmac_over_the_wire(engine):
    install = {"op": "install", "ws": "default", "policy_set_version": "ps-1",
               "temporal_set_version": 0, "policies": POLICIES, "schema": _schema_text(),
               "event_schema": ""}
    jsonschema.validate(install, contracts.load_schema("engine_control_request"))
    out = _control(engine, install)
    assert out == {"ok": True, "errors": []}, out

    status, reply = _post(engine, "/v1/decide", _decide_body(
        _user("hr-intern", "member"), "hr-database___create_payout",
        {"employee_id": "EMP-1024", "amount": 420}))
    assert status == 200, reply
    jsonschema.validate(reply, contracts.load_schema("engine_decide_response"))
    assert (reply["decision"], reply["stage_a"], reply["stage_bt"]) == ("deny", "allow", "deny")
    assert reply["determining"][0]["policy_id"] == "pol_payoutadm"

    status, reply = _post(engine, "/v1/decide", _decide_body(
        _user("demo-admin", "admin"), "hr-database___create_payout",
        {"employee_id": "EMP-1024", "amount": 420}))
    assert (status, reply["decision"]) == (200, "allow"), reply

    # a wrong key is 401 with the contract's error shape
    status, reply = _post(engine, "/v1/decide", _decide_body(
        _user("demo-admin", "admin"), "hr-database___list_departments", {}), key=b"x" * 64)
    assert status == 401
    jsonschema.validate(reply, contracts.load_schema("engine_error"))

    # every recorded event wrote one engine audit line (I7, amendment 3: with ws)
    lines = [json.loads(line) for line in engine["audit"].read_text().splitlines() if line]
    for line in lines:
        jsonschema.validate(line, contracts.load_schema("audit_engine_line"))

    status_out = _control(engine, {"op": "status"})
    assert status_out["shards"][0]["sets"] == {"default": "ps-1"}


def _install_demo(engine) -> None:
    out = _control(engine, {"op": "install", "ws": "default", "policy_set_version": "ps-1",
                            "temporal_set_version": 0, "policies": POLICIES,
                            "schema": _schema_text(), "event_schema": ""})
    assert out["ok"], out


def test_an_idle_connection_is_closed_before_auth_and_others_are_served(engine):
    """Review nit d: a peer that connects and sends nothing (or half a request) cannot hold
    a worker; the read timeout fires before anything is parsed or authenticated."""
    _install_demo(engine)
    idle = [socket.create_connection(("127.0.0.1", engine["port"])) for _ in range(3)]
    idle[1].sendall(b"POST /v1/decide HTTP/1.1\r\nContent-Length: 10\r\n")  # never finished
    # both workers (--workers 2) are now held by idle peers; a real request still completes
    # once their read timeout (5 s) has fired
    started = time.monotonic()
    status, reply = _post(engine, "/v1/decide", _decide_body(
        _user("demo-admin", "admin"), "hr-database___list_departments", {}))
    assert (status, reply["decision"]) == (200, "allow"), reply
    assert time.monotonic() - started < 9
    for s in idle:
        s.settimeout(8)
        assert s.recv(65536) == b"", "idle peer not closed"
        s.close()


def test_request_limits_answer_before_auth(engine):
    with socket.create_connection(("127.0.0.1", engine["port"]), timeout=5) as s:
        s.sendall(b"POST /v1/decide HTTP/1.1\r\nContent-Length: 2000000\r\n\r\n")
        assert s.recv(65536).startswith(b"HTTP/1.1 413 ")
    with socket.create_connection(("127.0.0.1", engine["port"]), timeout=5) as s:
        s.sendall(b"POST /v1/decide HTTP/1.1\r\nX-Pad: " + b"a" * 20000 + b"\r\n\r\n")
        assert s.recv(65536).startswith(b"HTTP/1.1 431 ")
    with socket.create_connection(("127.0.0.1", engine["port"]), timeout=5) as s:
        s.sendall(b"POST /v1/decide HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n")
        assert s.recv(65536).startswith(b"HTTP/1.1 411 ")


def test_list_filter_bounds_actions(engine):
    _install_demo(engine)
    body = {"ws": "default", "agent": AGENT, "principal": _user("demo-admin", "admin"),
            "resource": GW, "actions": ["hr-database___list_departments"] * 1025}
    status, reply = _post(engine, "/v1/list-filter", body)
    assert status == 400, reply
    jsonschema.validate(reply, contracts.load_schema("engine_error"))
    body["actions"] = ['AgentCore::Action::"hr-database___list_departments"'] * 1024
    status, reply = _post(engine, "/v1/list-filter", body)
    assert status == 200, reply


def test_keep_alive_serves_several_requests_on_one_connection(engine):
    import http.client
    _install_demo(engine)
    conn = http.client.HTTPConnection("127.0.0.1", engine["port"], timeout=5)
    for _ in range(3):
        body = json.dumps(_decide_body(_user("demo-admin", "admin"),
                                       "hr-database___list_departments", {})).encode()
        ts, nonce = str(int(time.time())), uuid.uuid4().hex
        sig = codec.engine_signature(engine["key"], "POST", "/v1/decide", ts, nonce, body)
        conn.request("POST", "/v1/decide", body=body, headers={
            "Content-Type": "application/json", "X-LP-Ts": ts, "X-LP-Nonce": nonce,
            "X-LP-Sig": sig})
        resp = conn.getresponse()
        assert resp.status == 200
        assert json.loads(resp.read())["decision"] == "allow"
    conn.close()


def test_an_inherited_listen_fds_is_not_adopted(tmp_path):
    """LISTEN_FDS meant for another process (LISTEN_PID mismatch) must not make the engine
    serve on whatever fd 3 happens to be."""
    (tmp_path / "key").write_bytes(secrets.token_bytes(32))
    proc = subprocess.run(
        [str(ENGINE), "--shard", "0", "--shards", "1", "--state-dir", str(tmp_path / "state"),
         "--control", str(tmp_path / "c.sock"), "--key-file", str(tmp_path / "key"),
         "--audit", str(tmp_path / "audit.jsonl")],
        env={**os.environ, "LISTEN_FDS": "1", "LISTEN_PID": "1"},
        capture_output=True, timeout=30,
    )
    assert proc.returncode != 0
    assert b"LISTEN_PID" in proc.stderr


def _raw(engine, data: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", engine["port"]), timeout=8) as s:
        s.sendall(data)
        out = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                return out
            out += chunk


@pytest.mark.parametrize("request_bytes, status", [
    # ambiguous framing (request smuggling): refused, connection closed
    (b"POST /v1/decide HTTP/1.1\r\nContent-Length: 4\r\nContent-Length: 5\r\n\r\nabcde", b"400"),
    (b"POST /v1/decide HTTP/1.1\r\nContent-Length: 4\r\n"
     b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n", b"411"),
    (b"POST /v1/decide HTTP/1.1\r\nTransfer-Encoding: identity\r\n\r\n", b"411"),
    (b"POST /v1/decide HTTP/1.1\r\nContent-Length: +4\r\n\r\nabcd", b"400"),
    (b"POST /v1/decide HTTP/1.1\r\nContent-Length: 0x4\r\n\r\nabcd", b"400"),
    (b"POST /v1/decide HTTP/1.1\r\nContent-Length : 4\r\n\r\nabcd", b"400"),
    (b"POST /v1/decide HTTP/1.1\r\nX-A: b\r\n continued\r\n\r\n", b"400"),
    (b"POST /v1/decide HTTP/1.1 extra\r\n\r\n", b"400"),
    (b"POST /v1/decide HTTP/2.0\r\n\r\n", b"505"),
    (b"POST /v1/decide HTTP/1.1\r\nContent-Length: 99999999999999999999\r\n\r\n", b"413"),
])
def test_ambiguous_framing_is_refused_and_closes(engine, request_bytes, status):
    # a second, valid-looking request pipelined behind must never be served
    out = _raw(engine, request_bytes + b"GET /healthz HTTP/1.1\r\n\r\n")
    assert out.startswith(b"HTTP/1.1 " + status + b" "), out[:80]
    assert out.count(b"HTTP/1.1 ") == 1, out


def test_duplicated_auth_headers_fail_auth(engine):
    _install_demo(engine)
    body = json.dumps(_decide_body(_user("demo-admin", "admin"),
                                   "hr-database___list_departments", {})).encode()
    ts, nonce = str(int(time.time())), uuid.uuid4().hex
    sig = codec.engine_signature(engine["key"], "POST", "/v1/decide", ts, nonce, body)
    head = (f"POST /v1/decide HTTP/1.1\r\nContent-Length: {len(body)}\r\nX-LP-Ts: {ts}\r\n"
            f"X-LP-Nonce: {nonce}\r\nX-LP-Sig: {sig}\r\nX-LP-Sig: {sig}\r\n"
            "Connection: close\r\n\r\n").encode()
    assert _raw(engine, head + body).startswith(b"HTTP/1.1 401 ")


def _signed_decide_bytes(engine, *, close: bool) -> bytes:
    body = json.dumps(_decide_body(_user("demo-admin", "admin"),
                                   "hr-database___list_departments", {})).encode()
    ts, nonce = str(int(time.time())), uuid.uuid4().hex
    sig = codec.engine_signature(engine["key"], "POST", "/v1/decide", ts, nonce, body)
    head = (f"POST /v1/decide HTTP/1.1\r\nContent-Length: {len(body)}\r\nX-LP-Ts: {ts}\r\n"
            f"X-LP-Nonce: {nonce}\r\nX-LP-Sig: {sig}\r\n"
            + ("Connection: close\r\n" if close else "") + "\r\n").encode()
    return head + body


def test_pipelined_data_plane_requests_are_each_answered(engine):
    _install_demo(engine)
    out = _raw(engine, _signed_decide_bytes(engine, close=False)
               + _signed_decide_bytes(engine, close=True))
    assert out.count(b"HTTP/1.1 200 ") == 2, out
    assert out.count(b'"decision":"allow"') == 2, out


def test_healthz_answers_once_and_closes_even_when_pipelined(engine):
    out = _raw(engine, b"GET /healthz HTTP/1.1\r\n\r\nGET /healthz HTTP/1.1\r\n\r\n")
    assert out.count(b"HTTP/1.1 200 ") == 1, out
    assert b"Connection: close" in out


def test_four_slow_clients_do_not_stall_the_engine(engine):
    """Re-review item 1 (reproduced on 4a805d8c): 4 peers dribbling one header byte every 3 s
    held all workers, because the 5 s timeout was per read. Now each request has one deadline
    and slow peers never occupy a handler worker (the fixture runs --workers 2)."""
    import threading
    _install_demo(engine)
    stop = threading.Event()
    cut_after: list[float] = []

    def dribble():
        s = socket.create_connection(("127.0.0.1", engine["port"]))
        started = time.monotonic()
        try:
            for b in b"POST /v1/decide HTTP/1.1\r\nX-Slow: " + b"a" * 40:
                if stop.is_set():
                    return
                s.sendall(bytes([b]))
                time.sleep(3)
        except OSError:
            cut_after.append(time.monotonic() - started)
        finally:
            s.close()

    peers = [threading.Thread(target=dribble, daemon=True) for _ in range(4)]
    for p in peers:
        p.start()
    time.sleep(1)
    try:
        for _ in range(3):
            started = time.monotonic()
            status, reply = _post(engine, "/v1/decide", _decide_body(
                _user("demo-admin", "admin"), "hr-database___list_departments", {}))
            assert (status, reply["decision"]) == (200, "allow"), reply
            assert time.monotonic() - started < 2, "decide stalled behind slow peers"
        for p in peers:
            p.join(timeout=15)
        assert len(cut_after) == 4, cut_after
        assert all(t < 11 for t in cut_after), cut_after  # deadline 5 s + one 3 s send gap
    finally:
        stop.set()
