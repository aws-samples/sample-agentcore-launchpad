"""Live verification of the KB gateway chain (step 3 gate).

ensure launchpad-kb-gw → per-KB Retrieve target → per-agent agentic target →
MCP tools/list + tools/call through the gateway with a Cognito user token →
cleanup of the probe agentic target.
Run: cd backend && PYTHONPATH=. python scripts/e2e_kb_gateway.py <KB_ID>
"""

import argparse
import json
import sys
import time

from app.services import kb_gateway
from app.services.agentcore.client import control_client
from app.services.mcp_client import _rpc, get_cognito_token


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main(kb_id: str) -> int:
    control = control_client()
    gw = kb_gateway.ensure_kb_gateway_persisted(control)
    log(f"kb gateway: {gw['id']} · {gw['url']}")

    target_id = kb_gateway.ensure_retrieve_target(
        control, gw["id"], kb_id, "aurora-deck-docs",
        "Aurora Deck product documentation and support runbook",
    )
    log(f"retrieve target READY: {target_id}")

    agentic_id = kb_gateway.sync_agentic_target(
        control, gw["id"], "kbgw-probe-agent",
        [{"kb_id": kb_id, "description": "Aurora Deck docs"}],
    )
    log(f"agentic target READY: {agentic_id}")

    token = get_cognito_token("river")
    tools = _rpc(gw["url"], token, "tools/list").get("tools", [])
    names = [t["name"] for t in tools]
    log(f"tools/list → {names}")
    retrieve_tool = next((n for n in names if n.endswith("___Retrieve")), None)
    agentic_tool = next(
        (n for n in names if n.startswith("agentic-kbgw-probe-agent")), None
    )
    if not retrieve_tool or not agentic_tool:
        log("FAIL: expected tools missing")
        return 1

    result = _rpc(
        gw["url"], token, "tools/call",
        {"name": retrieve_tool,
         "arguments": {"retrievalQuery": {"text": "What is known issue AD-4411?"}}},
    )
    text = "".join(
        c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"
    )
    ok = "AD-4411" in text or "250 slides" in text
    log(f"tools/call {retrieve_tool} → isError={result.get('isError')} · hit={ok}")
    log(f"  payload head: {text[:220]!r}")

    kb_gateway.delete_agentic_target(control, gw["id"], "kbgw-probe-agent")
    log("probe agentic target cleaned up")

    # update-path check: re-sync creates it again, then update with same set
    tid1 = kb_gateway.sync_agentic_target(
        control, gw["id"], "kbgw-probe-agent",
        [{"kb_id": kb_id, "description": "Aurora Deck docs"}],
    )
    tid2 = kb_gateway.sync_agentic_target(
        control, gw["id"], "kbgw-probe-agent",
        [{"kb_id": kb_id, "description": "Aurora Deck docs v2"}],
    )
    same = tid1 == tid2
    log(f"agentic re-sync update path: create={tid1} update={tid2} same={same}")
    kb_gateway.delete_agentic_target(control, gw["id"], "kbgw-probe-agent")
    log("probe target cleaned up again")

    if not (ok and same):
        log("FAIL")
        return 1
    log("PASS")
    print(json.dumps({"gateway": gw, "retrieve_tool": retrieve_tool}))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "kb_id", nargs="?", default="BL6ZKAVWFB",
        help="managed KB id to verify the gateway chain against",
    )
    sys.exit(main(parser.parse_args().kb_id))
