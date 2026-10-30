"""Recognise a policy denial in the text a tool call returns (Global signature, kept).

CN (D20 P2-a): the self-hosted runtime guard (``templates/policy_guard.py.tmpl``) cancels a
denied Strands tool call with exactly this text, so the hub, the console
(``frontend/src/lib/policy-deny.ts``) and any toolResult that still flows as text all read
one signature. Restored verbatim from the Global baseline ``4e27b1f4``; the paragraphs
below describe where the text came from.

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


# N17-obs507: the interceptor names EVERY determining policy — "Policy evaluation denied due
# to pol_a, pol_b" — and a reason CODE when no policy decided ("[unknown_caller]",
# "[engine_unavailable]", "[No policy applies to the request (denied by default).]").
_POLICY_IDS_RE = re.compile(
    r"Policy evaluation denied due to\s*(?P<ids>[A-Za-z0-9][A-Za-z0-9_:,\s-]*)", re.IGNORECASE,
)
# the hub bounds a tool-result summary (256 chars): the closing bracket may be cut off
_DENIAL_LOOSE_RE = re.compile(
    r"Tool Execution Denied:\s*Tool call not allowed due to policy enforcement"
    r"(?:\s*\[(?P<reason>[^\]]*)\]?)?",
    re.IGNORECASE,
)


def policy_ids_in_reason(reason: str) -> list[str]:
    """Every policy id the Gateway's reason names (``pol_…:v2`` annotations stripped), in
    order, deduplicated; empty for a default deny or a reason code."""
    match = _POLICY_IDS_RE.search(reason or "")
    if match is None:
        return []
    out: list[str] = []
    for raw in match.group("ids").split(","):
        pid = raw.strip().split(":", 1)[0]
        if pid and pid not in out:
            out.append(pid)
    return out


def gateway_denial_from_tool_result(
    name: Any, status: Any, summary: Any, *, tool_use_id: Any = None,
) -> dict[str, Any] | None:
    """N17-obs507: the Gateway interceptor's denial as it reaches the hub — flattened into the
    agent's TOOL RESULT text ("Tool execution failed: Tool Execution Denied: Tool call not
    allowed due to policy enforcement [Policy evaluation denied due to pol_a, pol_b]"). Under
    Global parity nothing else in the stream says a tool call was denied (the runtime guard
    that used to emit ``policy_denied`` is gone), so the hub derives the ``policy_denied``
    frame from this text. Conservative: no signature ⇒ None (a reworded message loses the
    card rather than inventing one). Returns the frame body — ``tool`` (catalog name),
    ``reason`` (the raw English text, bounded as received), ``policy_id`` (the first id) and
    ``policy_ids``, ``source: "gateway"``, ``stage`` / ``decision_id`` None (the gateway audit
    row is ingested separately), ``decision: "deny"``."""
    if status not in (None, "", "error"):
        return None
    parsed = parse_tool_denial(summary)  # the Global signature parser (one id, bracket intact)
    if parsed is None:
        return None
    reason = parsed["reason"]
    if not reason:
        # the hub's 256-char cut can take the closing bracket with it: read the rest loosely
        match = _DENIAL_LOOSE_RE.search(summary)
        reason = ((match.group("reason") if match else "") or "").strip()
    ids = policy_ids_in_reason(reason)
    if parsed["policy_id"] and parsed["policy_id"] not in ids:
        ids.insert(0, parsed["policy_id"])
    return {
        "tool": str(name or ""), "reason": summary.strip(), "policy_id": ids[0] if ids else None,
        "policy_ids": ids, "tool_use_id": str(tool_use_id or ""), "mode": "",
        "decision": "deny", "bundle_version": None, "decision_id": None,
        "source": "gateway", "stage": None,
    }
