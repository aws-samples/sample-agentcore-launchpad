"""The hr-approvals demo target (samples/hr_approvals_lambda): its tools behave as the demo
needs and its tool schema matches the manifest the engine tests use."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[3]
SAMPLE = REPO / "samples" / "hr_approvals_lambda"


def _handler():
    spec = importlib.util.spec_from_file_location("hr_approvals_handler", SAMPLE / "handler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ctx(tool: str):
    return SimpleNamespace(client_context=SimpleNamespace(
        custom={"bedrockAgentCoreToolName": f"hr-approvals___{tool}"}))


def test_verify_identity_and_approve_payout():
    h = _handler()
    good = h._demo_code("EMP-1024")
    assert h.lambda_handler({"employee_id": "EMP-1024", "code": good}, _ctx("verify_identity")) \
        == {"employee_id": "EMP-1024", "verified": True}
    assert not h.lambda_handler({"employee_id": "EMP-1024", "code": "nope"},
                                _ctx("verify_identity"))["verified"]
    out = h.lambda_handler({"employee_id": "EMP-1024", "amount": 420}, _ctx("approve_payout"))
    assert out["approved"] and out["approval_id"].startswith("APR-")
    assert not h.lambda_handler({"employee_id": "EMP-1", "amount": 1},
                                _ctx("approve_payout"))["approved"]


def test_tool_schema_matches_the_engine_demo_manifest():
    schema = {t["name"]: t for t in json.loads((SAMPLE / "tool_schema.json").read_text())}
    manifest = json.loads((REPO / "policy-engine" / "crates" / "lp-dogwood" / "assets"
                           / "demo-tools.json").read_text())["result"]["tools"]
    demo = {t["name"].split("___", 1)[1]: t for t in manifest
            if t["name"].startswith("hr-approvals___")}
    assert set(schema) == set(demo)
    for name, tool in schema.items():
        for part in ("inputSchema", "outputSchema"):
            assert tool[part]["properties"].keys() == demo[name][part]["properties"].keys()
