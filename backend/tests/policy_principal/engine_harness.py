"""Start / stop / kill the real engine binary for service-level tests (stream C)."""

from __future__ import annotations

import atexit
import json
import os
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.policy.contracts import codec

REPO = Path(__file__).resolve().parents[3]
ENGINE = Path(os.environ.get(
    "LP_ENGINE_BIN", REPO / "policy-engine" / "target" / "release" / "launchpad-policy-engine",
))
ASSETS = REPO / "policy-engine" / "crates" / "lp-dogwood" / "assets"
GW = 'AgentCore::Gateway::"arn:aws-cn:bedrock-agentcore:cn-northwest-1:123456789012:gateway/gw0"'
ROLE = 'AgentCore::IamEntity::"arn:aws-cn:iam::123456789012:role/launchpad-agent-hr-ab12cd34"'
AGENT = {"entity": ROLE, "agent_id": "ag_hrhelp01", "method": "zip_runtime", "system": False}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def user(name: str, role: str) -> dict:
    return {"entity": f'AgentCore::User::"{name}"', "parents": [f'AgentCore::Role::"{role}"'],
            "attrs": {"username": name, "role": role}}


# every engine process a test started; any still alive at interpreter exit is terminated (a
# worker killed mid-test, a fixture whose teardown never ran must not leak an engine)
_STARTED: set[subprocess.Popen] = set()


def _terminate_all() -> None:
    for proc in list(_STARTED):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


atexit.register(_terminate_all)


@dataclass
class Engine:
    root: Path
    port: int = field(default_factory=free_port)
    key: bytes = field(default_factory=lambda: secrets.token_bytes(32))  # raw (amendment 3)
    proc: subprocess.Popen | None = None
    socket_activated: bool = False

    @property
    def control(self) -> Path:
        return self.root / "control.sock"

    def start(self) -> float:
        """Start (or restart on the same state); returns seconds until /healthz is ready."""
        (self.root / "key").write_bytes(self.key)
        args = [str(ENGINE), "--shard", "0", "--shards", "1",
                "--state-dir", str(self.root / "state"), "--control", str(self.control),
                "--key-file", str(self.root / "key"), "--audit", str(self.root / "audit.jsonl"),
                "--workers", "4"]
        if self.socket_activated:
            args = ["systemd-socket-activate", "-l", f"127.0.0.1:{self.port}", *args]
        else:
            args += ["--listen", f"127.0.0.1:{self.port}"]
        if self.proc is not None and self.proc.poll() is None:
            self.stop()  # a restart never leaves the previous process running
        if self.control.exists():
            self.control.unlink()
        started = time.monotonic()
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _STARTED.add(self.proc)
        deadline = started + 20
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/healthz", timeout=1,
                ) as resp:
                    if json.loads(resp.read()).get("ready"):
                        return time.monotonic() - started
            except (OSError, urllib.error.URLError):
                time.sleep(0.02)
        raise TimeoutError("engine did not become ready")

    def kill9(self) -> None:
        assert self.proc is not None
        self.proc.kill()
        self.proc.wait(timeout=10)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        if self.proc is not None:
            _STARTED.discard(self.proc)

    def control_call(self, message: dict) -> dict:
        deadline = time.monotonic() + 10
        while not self.control.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(str(self.control))
            s.sendall((json.dumps(message) + "\n").encode())
            data = b""
            while not data.endswith(b"\n"):
                data += s.recv(65536)
        return json.loads(data)

    def post(self, path: str, payload: dict) -> tuple[int, dict]:
        body = json.dumps(payload).encode()
        ts, nonce = str(int(time.time())), uuid.uuid4().hex
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=body, method="POST",
            headers={"Content-Type": "application/json", "X-LP-Ts": ts, "X-LP-Nonce": nonce,
                     "X-LP-Sig": codec.engine_signature(self.key, "POST", path, ts, nonce, body)},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())


def demo_install(engine: Engine, policies: list[dict], version: str = "ps-1", tsv: int = 1,
                 *, ws: str = "default"):
    from app.policy import dogwood_compiler as dc

    tools = json.loads((ASSETS / "demo-tools.json").read_text())
    compiled = dc.compile(tools, policies)
    assert compiled.ok, compiled.errors
    out = engine.control_call({
        "op": "install", "ws": ws, "policy_set_version": version,
        "temporal_set_version": tsv, "policies": compiled.items, "schema": compiled.schema,
        "event_schema": "",
    })
    assert out["ok"], out
    return compiled


def decide_body(principal: dict, tool: str, input_: dict, *, psid: str, record: bool,
                tsv: int = 1) -> dict:
    return {
        "ws": "default", "record": record,
        "session": codec.session_key("default", psid, "user", principal["attrs"]["username"]),
        "action": f'AgentCore::Action::"{tool}"', "resource": GW, "agent": AGENT,
        "principal": principal, "tsv": tsv, "session_started_at": int(time.time()) - 5,
        "context": {"input": input_, "agent_id": AGENT["agent_id"], "channel": "chat"},
    }


def respond_body(principal: dict, tool: str, input_: dict, output: dict, *, psid: str) -> dict:
    return {
        "ws": "default", "kind": "response",
        "session": codec.session_key("default", psid, "user", principal["attrs"]["username"]),
        "action": f'AgentCore::Action::"{tool}"', "input": input_, "output": output,
        "principal": principal, "agent": AGENT, "resource": GW,
    }
