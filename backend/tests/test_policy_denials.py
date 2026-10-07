"""Gateway Cedar-denial recognition on the Chat path (text the Harness returns)."""

import pytest

from app.services.agentcore import harness as hc
from app.services.chat import _policy_denied
from app.services.policy_denials import parse_tool_denial
from tests.conftest import ws_ctx

# captured live from InvokeHarness on dev, 2026-10-07
DEFAULT_DENY = (
    "Tool execution failed: Tool Execution Denied: Tool call not allowed due to policy "
    "enforcement [No policy applies to the request (denied by default).]"
)
POLICY_DENY = (
    "Tool Execution Denied: Tool call not allowed due to policy enforcement "
    "[Policy evaluation denied due to launchpad_payout_admin_only-qtv_30w04g]"
)


def test_default_deny_has_reason_and_no_policy():
    assert parse_tool_denial(DEFAULT_DENY) == {
        "reason": "No policy applies to the request (denied by default).",
        "policy_id": None,
    }


def test_determining_policy_is_extracted():
    assert parse_tool_denial(POLICY_DENY) == {
        "reason": "Policy evaluation denied due to launchpad_payout_admin_only-qtv_30w04g",
        "policy_id": "launchpad_payout_admin_only-qtv_30w04g",
    }


@pytest.mark.parametrize("text", [
    '{"payout_id": "PAY-1", "status": "created"}',
    "Tool execution failed: Lambda timed out",
    "the policy says vacation requests need approval",
    "",
    None,
])
def test_anything_else_is_not_a_denial(text):
    assert parse_tool_denial(text) is None


def _result(status, text, tool_id="t-1"):
    return {"id": tool_id, "status": status, "chunks": [text]}


def test_user_gateway_tool_is_named_and_linked():
    names = {"t-1": f"{hc.USER_GATEWAY_ALIAS}_hr-database___create_payout"}
    workspace = ws_ctx({"gateway_id": "launchpad-gw-abc"})
    out = _policy_denied(_result("error", DEFAULT_DENY), names, workspace)
    assert out == {
        "tool": "hr-database___create_payout",
        "tool_use_id": "t-1",
        "reason": "No policy applies to the request (denied by default).",
        "policy_id": None,
        "gateway_id": "launchpad-gw-abc",
    }


def test_other_alias_keeps_its_name_unlinked():
    workspace = ws_ctx({"gateway_id": "launchpad-gw-abc"})
    out = _policy_denied(_result("error", POLICY_DENY), {"t-1": "launchpad_gw_x___y"}, workspace)
    assert out["tool"] == "launchpad_gw_x___y" and out["gateway_id"] is None


@pytest.mark.parametrize("result", [
    None,
    _result("success", DEFAULT_DENY),
    _result("error", "Tool execution failed: upstream 500"),
])
def test_non_denials_yield_nothing(result):
    assert _policy_denied(result, {"t-1": "x"}, ws_ctx()) is None
