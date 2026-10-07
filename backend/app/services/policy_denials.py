"""Recognise an AgentCore Gateway Cedar denial in the text a tool call returns.

The Gateway answers a denied ``tools/call`` with JSON-RPC error ``-32002``

    "Tool Execution Denied: Tool call not allowed due to policy enforcement [<reason>]"

where ``<reason>`` is e.g. ``No policy applies to the request (denied by default).``
or ``Policy evaluation denied due to <policy-id>``. Policy Test sees the structured
error; a Managed Harness flattens it into its toolResult text
(``Tool execution failed: Tool Execution Denied: …``, captured live 2026-10-07), so
the text signature is the only signal on the Chat path.

Detection is deliberately conservative: anything that does not carry the signature
is not a denial, so a reworded message loses the card rather than inventing one.
"""

import re
from typing import Any

# JSON-RPC error code the Gateway returns for a Cedar denial.
POLICY_DENIED_RPC_CODE = -32002
DENIED_MARKER = "tool execution denied"
DETERMINING_POLICY_RE = re.compile(
    r"Policy evaluation denied due to ([A-Za-z0-9][A-Za-z0-9_-]*)",
    re.IGNORECASE,
)
_DENIAL_RE = re.compile(
    r"Tool Execution Denied:\s*Tool call not allowed due to policy enforcement"
    r"(?:\s*\[(?P<reason>[^\]]*)\])?",
    re.IGNORECASE,
)


def determining_policy_id(message: str) -> str | None:
    match = DETERMINING_POLICY_RE.search(message)
    return match.group(1) if match else None


def parse_tool_denial(text: Any) -> dict[str, Any] | None:
    """``{reason, policy_id}`` when ``text`` is a Gateway policy denial, else None."""
    if not isinstance(text, str):
        return None
    match = _DENIAL_RE.search(text)
    if match is None:
        return None
    reason = (match.group("reason") or "").strip()
    return {"reason": reason, "policy_id": determining_policy_id(reason)}
