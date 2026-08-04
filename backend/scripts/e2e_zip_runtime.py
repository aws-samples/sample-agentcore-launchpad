#!/usr/bin/env python3
"""E2E smoke test for the zip runtime path — REAL AWS, cleaned up unless --keep.

Flow: POST /api/agents (zip_runtime) → poll stages (pip/zip/S3/create/poll READY)
      → invoke calculator prompt → assert → DELETE.

Run:  cd backend && uv run python scripts/e2e_zip_runtime.py [--keep]
"""

import argparse
import sys
import time

import httpx

AGENT_NAME = "e2e-zip-smoke"
SYSTEM_PROMPT = (
    "You are a concise math assistant. Use the calculator tool for arithmetic "
    "and answer with just the result."
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args()

    client = httpx.Client(base_url=args.base, timeout=180)

    for agent in client.get("/api/agents").json()["agents"]:
        if agent["name"] == AGENT_NAME:
            print(f"cleaning leftover agent {agent['id']}")
            client.delete(f"/api/agents/{agent['id']}")

    print("── creating zip_runtime agent…")
    res = client.post(
        "/api/agents",
        json={
            "name": AGENT_NAME,
            "method": "zip_runtime",
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
                print(
                    f"  [{time.strftime('%H:%M:%S')}] {stage['name']:<10} "
                    f"{stage['status']:<10} {stage['detail'][:90]}"
                )
        if status in ("active", "failed"):
            break
        # Real-AWS deployment polling is intentionally paced and deadline-bounded.
        time.sleep(10)  # nosemgrep: arbitrary-sleep

    if status != "active":
        job = client.get(f"/api/jobs/{job_id}").json()
        print(f"FAILED: agent status={status} error={job.get('error')}")
        return 1

    arn = client.get(f"/api/agents/{agent_id}").json()["arn"]
    print(f"── runtime READY · arn {arn}")

    print("── invoking: what is 17*23?")
    inv = client.post(
        f"/api/agents/{agent_id}/invoke",
        json={"prompt": "What is 17*23? Use the calculator tool and reply with just the number."},
    )
    inv.raise_for_status()
    answer = inv.json()
    print(f"answer: {answer['text']!r} · {answer['latency_ms']}ms")
    assert "391" in answer["text"], f"expected '391' in answer, got: {answer['text']!r}"
    print("── invoke assertion PASSED")

    if args.keep:
        print("--keep set: leaving runtime deployed")
        return 0

    print("── deleting agent…")
    client.delete(f"/api/agents/{agent_id}").raise_for_status()
    print(f"ledger status after delete: {client.get(f'/api/agents/{agent_id}').json()['status']}")
    print("E2E ZIP RUNTIME: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
