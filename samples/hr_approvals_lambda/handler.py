"""hr-approvals sample Lambda — the demo target for session-aware (temporal) policies.

Exposed through AgentCore Gateway as ``hr-approvals___verify_identity`` and
``hr-approvals___approve_payout`` (plans/DESIGN-POLICY-USER-PRINCIPAL.md §15.6). It is a
NEW target so the shared ``hr-database`` sample stays untouched. Results are JSON objects
the Gateway returns as ``content[0].text``; the policy engine reads their fields
(``output.verified``, ``output.approved``) as declared in ``tool_schema.json``'s
``outputSchema``. Nothing here performs a real verification or payment.
"""

import hashlib
import time

# Demo one-time codes: deterministic per employee so a scripted demo can type them.
DEMO_CODE_SALT = "launchpad-demo"
KNOWN_EMPLOYEES = {"EMP-1024", "EMP-2048", "EMP-4096"}


def _demo_code(employee_id):
    return hashlib.sha256(f"{DEMO_CODE_SALT}:{employee_id}".encode()).hexdigest()[:6]


def verify_identity(args):
    employee_id = str(args.get("employee_id") or "")
    code = str(args.get("code") or "")
    verified = employee_id in KNOWN_EMPLOYEES and code == _demo_code(employee_id)
    return {"employee_id": employee_id, "verified": verified}


def approve_payout(args):
    employee_id = str(args.get("employee_id") or "")
    amount = args.get("amount")
    approved = employee_id in KNOWN_EMPLOYEES and isinstance(amount, (int, float)) and amount > 0
    return {
        "approved": approved,
        "approval_id": f"APR-{int(time.time())}" if approved else "",
        "employee_id": employee_id,
    }


TOOLS = {"verify_identity": verify_identity, "approve_payout": approve_payout}


def lambda_handler(event, context):
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    qualified = custom.get("bedrockAgentCoreToolName", "")
    tool_name = qualified.split("___")[-1] if qualified else event.get("__tool__", "")
    fn = TOOLS.get(tool_name)
    if fn is None:
        return {"error": "unknown_tool", "tool": tool_name}
    return fn(event or {})
