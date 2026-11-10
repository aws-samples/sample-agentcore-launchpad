"""Engine service resilience (stream C5, design §13.5): kill -9 under load loses no
acknowledged event and recovers fast; a systemd socket-activated engine serves; latency is
inside the budget. Skipped unless the engine binary is built."""

from __future__ import annotations

import json
import os
import shutil
import statistics
import time

import jsonschema
import pytest

from app.policy import contracts
from app.policy.contracts import codec
from tests.policy_principal.engine_harness import (
    ENGINE,
    Engine,
    decide_body,
    demo_install,
    respond_body,
    user,
)

pytestmark = pytest.mark.skipif(not ENGINE.exists(), reason="engine binary not built")

# Wall-clock budgets are only meaningful on a quiet host: in `make verify` the suite runs
# `-n auto` beside everything else, so the timing assertions run under LAUNCHPAD_SOAK=1
# (the correctness assertions always run).
SOAK = os.environ.get("LAUNCHPAD_SOAK") == "1"

POLICIES = [
    {"id": "pol_users", "version": 1, "mode": "active",
     "statement": 'permit (principal is AgentCore::User, '
                  'action in AgentCore::Action::"CallTool", resource);'},
    {"id": "pol_agent", "version": 1, "mode": "active",
     "statement": 'permit (principal in AgentCore::Agent::"ag_hrhelp01", '
                  'action in AgentCore::Action::"CallTool", resource);'},
    {"id": "pol_approval", "version": 1, "mode": "active",
     "statement": 'forbid (principal, action == AgentCore::Action::"hr-database___create_payout",'
                  ' resource) unless temporal { formerly within 1h '
                  'AgentCore::Action::"hr-approvals___approve_payout"::response{ '
                  'input.employee_id: context.input.employee_id, output.approved: true } };'},
]
ADMIN = user("demo-admin", "admin")


def _approve(engine: Engine, psid: str, employee: str) -> int:
    payout = {"employee_id": employee, "amount": 10}
    status, out = engine.post("/v1/decide", decide_body(
        ADMIN, "hr-approvals___approve_payout", payout, psid=psid, record=True))
    assert status == 200, out
    assert out["decision"] == "allow", out
    status, out = engine.post("/v1/respond", respond_body(
        ADMIN, "hr-approvals___approve_payout", payout,
        {"approved": True, "approval_id": f"apr-{employee}"}, psid=psid))
    assert status == 200, out
    return out["event_seq"]


def _payout(engine: Engine, psid: str, employee: str) -> str:
    status, out = engine.post("/v1/decide", decide_body(
        ADMIN, "hr-database___create_payout", {"employee_id": employee, "amount": 10},
        psid=psid, record=True))
    assert status == 200, out
    return out["decision"]


def test_kill9_loses_no_acknowledged_event(tmp_path):
    engine = Engine(tmp_path)
    engine.start()
    try:
        demo_install(engine, POLICIES)
        psid = codec.psid_for("default", "s" * 40, 1)
        acknowledged = [f"EMP-{i:04d}" for i in range(40)]
        for employee in acknowledged:
            _approve(engine, psid, employee)  # each acknowledged before the next
        engine.kill9()
        recovery_s = engine.start()
        # every approval acknowledged before the crash still counts after it
        assert all(_payout(engine, psid, e) == "allow" for e in acknowledged)
        assert _payout(engine, psid, "EMP-9999") == "deny"
        assert recovery_s < (2.0 if SOAK else 15.0), f"recovery took {recovery_s:.2f}s"
        # one engine audit line per recorded event, each a valid I7 line with ws
        lines = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines() if x]
        assert len(lines) >= 2 * len(acknowledged)
        schema = contracts.load_schema("audit_engine_line")
        for line in lines:
            jsonschema.validate(line, schema)
            assert line["launchpad.policy.engine"]["ws"] == "default"
    finally:
        engine.stop()


@pytest.mark.skipif(shutil.which("systemd-socket-activate") is None,
                    reason="systemd-socket-activate not available")
def test_socket_activated_engine_serves(tmp_path):
    engine = Engine(tmp_path, socket_activated=True)
    engine.start()
    try:
        demo_install(engine, POLICIES)
        psid = codec.psid_for("default", "t" * 40, 1)
        assert _payout(engine, psid, "EMP-1") == "deny"
        _approve(engine, psid, "EMP-1")
        assert _payout(engine, psid, "EMP-1") == "allow"
    finally:
        engine.stop()


@pytest.mark.skipif(not SOAK, reason="timing budget — LAUNCHPAD_SOAK=1 on a quiet host")
def test_decide_latency_budget(tmp_path):
    """§13.5 latency budget, measured end to end over localhost HTTP (a fresh TCP
    connection per call, recorded calls with fsync). The design budget is decide p99 <= 10 ms
    engine-side and <= 40 ms for the whole REQUEST interceptor; this gate allows p50 <= 10 ms
    and p99 <= 40 ms because CI hosts are shared and loaded (the engine-internal figures are
    `cargo test --release --test bench_decide -- --nocapture`; M1 measures on the hub host)."""
    engine = Engine(tmp_path)
    engine.start()
    try:
        demo_install(engine, POLICIES)
        psid = codec.psid_for("default", "u" * 40, 1)
        samples = []
        for i in range(300):
            started = time.perf_counter()
            _payout(engine, psid, f"EMP-{i % 7}")
            samples.append((time.perf_counter() - started) * 1000)
        samples.sort()
        p50, p99 = statistics.median(samples), samples[int(len(samples) * 0.99) - 1]
        print(f"decide latency p50={p50:.2f}ms p99={p99:.2f}ms")
        assert p50 <= 10.0 and p99 <= 40.0, f"p50 {p50:.2f} ms, p99 {p99:.2f} ms"
    finally:
        engine.stop()
