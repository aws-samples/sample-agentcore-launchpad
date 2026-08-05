#!/usr/bin/env python3
"""E2E: a harness agent answers an HR question by calling a gateway MCP tool.

Proof of tool usage: the answer must contain seeded facts (9 remaining days /
Maya Chen) that the model cannot know without calling hr-database___get_employee.

Run:  cd backend && uv run python scripts/e2e_gateway_tool.py [--keep]
"""

import argparse
import sys
import time

from _e2e_client import e2e_client

AGENT_NAME = "e2e-gw-hr-assistant"
SYSTEM_PROMPT = (
    "You are an HR assistant for Octank Inc. Always verify employee data with the "
    "hr-database tools before answering; never fabricate records. Answer concisely."
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    client = e2e_client(args.base, timeout=300)

    for agent in client.get("/api/agents").json()["agents"]:
        if agent["name"] == AGENT_NAME:
            client.delete(f"/api/agents/{agent['id']}")

    print("── creating harness agent with gateway tool…")
    res = client.post(
        "/api/agents",
        json={
            "name": AGENT_NAME,
            "method": "harness",
            "system_prompt": SYSTEM_PROMPT,
            "tools": [{"type": "gateway", "name": "hr-database"}],
            "memory": {"short_term": True, "long_term": False},
        },
    )
    res.raise_for_status()
    agent_id = res.json()["agent"]["id"]

    status = "deploying"
    for _ in range(60):
        agent = client.get(f"/api/agents/{agent_id}").json()
        status = agent["status"]
        if status in ("active", "failed"):
            break
        # Real-AWS deployment polling is intentionally paced and attempt-bounded.
        time.sleep(5)  # nosemgrep: arbitrary-sleep
    if status != "active":
        job = client.get(f"/api/jobs/{res.json()['job_id']}").json()
        print(f"FAILED to deploy: {job.get('error')}")
        return 1
    print(f"harness active · {agent['arn']}")

    question = (
        "How many vacation days does employee EMP-1024 have left this year, "
        "and what is their name?"
    )
    print(f"── asking: {question}")
    inv = client.post(f"/api/agents/{agent_id}/invoke", json={"prompt": question})
    inv.raise_for_status()
    answer = inv.json()["text"]
    print(f"answer: {answer!r}")
    assert "9" in answer, f"expected seeded '9 days' in answer: {answer!r}"
    assert "Maya" in answer or "Chen" in answer, f"expected seeded employee name: {answer!r}"
    print("── tool-usage assertions PASSED (seeded data reflected in answer)")

    if args.keep:
        print("--keep set: leaving agent deployed")
        return 0
    client.delete(f"/api/agents/{agent_id}").raise_for_status()
    print("E2E GATEWAY TOOL: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
