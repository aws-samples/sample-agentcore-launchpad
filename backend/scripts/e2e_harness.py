#!/usr/bin/env python3
"""E2E smoke test for the harness deploy path — REAL AWS, small, cleaned up.

Flow: POST /api/agents → poll job stages → invoke "2+2?" → assert answer
      → DELETE (unless --keep).

Run:  cd backend && uv run python scripts/e2e_harness.py [--keep] [--base URL]
"""

import argparse
import sys
import time

import httpx

AGENT_NAME = "e2e-harness-smoke"
SYSTEM_PROMPT = "You are a concise math assistant. Answer with just the result."


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--keep", action="store_true", help="leave the agent deployed")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    client = httpx.Client(base_url=args.base, timeout=120)

    # clean any leftover from a previous run
    for agent in client.get("/api/agents").json()["agents"]:
        if agent["name"] == AGENT_NAME:
            print(f"cleaning leftover agent {agent['id']}")
            client.delete(f"/api/agents/{agent['id']}")

    print("── creating harness agent…")
    res = client.post(
        "/api/agents",
        json={
            "name": AGENT_NAME,
            "method": "harness",
            "system_prompt": SYSTEM_PROMPT,
            "memory": {"short_term": True, "long_term": False},
        },
    )
    res.raise_for_status()
    body = res.json()
    agent_id, job_id = body["agent"]["id"], body["job_id"]
    print(f"agent {agent_id} · job {job_id}")

    seen: set[tuple[str, str]] = set()
    deadline = time.time() + args.timeout
    status = "deploying"
    while time.time() < deadline:
        agent = client.get(f"/api/agents/{agent_id}").json()
        status = agent["status"]
        for stage in agent["deployments"][0]["stages"]:
            key = (stage["name"], stage["status"])
            if key not in seen and stage["status"] != "pending":
                seen.add(key)
                print(f"  stage {stage['name']:<10} {stage['status']:<10} {stage['detail']}")
        if status in ("active", "failed"):
            break
        # Real-AWS deployment polling is intentionally paced and attempt-bounded.
        time.sleep(5)  # nosemgrep: arbitrary-sleep

    if status != "active":
        job = client.get(f"/api/jobs/{job_id}").json()
        print(f"FAILED: agent status={status} error={job.get('error')}")
        return 1

    arn = client.get(f"/api/agents/{agent_id}").json()["arn"]
    print(f"── harness READY · arn {arn}")

    print("── invoking: what is 2+2?")
    inv = client.post(
        f"/api/agents/{agent_id}/invoke",
        json={"prompt": "What is 2+2? Reply with just the number."},
    )
    inv.raise_for_status()
    answer = inv.json()
    print(
        f"answer: {answer['text']!r} · {answer['latency_ms']}ms "
        f"· session {answer['session_id'][:12]}…"
    )
    assert "4" in answer["text"], f"expected '4' in answer, got: {answer['text']!r}"
    print("── invoke assertion PASSED")

    if args.keep:
        print("--keep set: leaving agent deployed")
        return 0

    print("── deleting agent…")
    client.delete(f"/api/agents/{agent_id}").raise_for_status()
    final = client.get(f"/api/agents/{agent_id}").json()
    print(f"ledger status after delete: {final['status']}")
    print("E2E HARNESS: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
