#!/usr/bin/env python3
"""E2E for the Claude SDK container path — REAL AWS (CodeBuild+ECR+Runtime).

Flow: [local docker smoke] → POST /api/agents (container) → poll stages
      (codebuild phases stream into job log) → invoke → assert → DELETE.

Run:  cd backend && uv run python scripts/e2e_claude_sdk.py [--keep] [--skip-local]
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

from _e2e_client import e2e_client

AGENT_NAME = "e2e-claude-sdk"
SYSTEM_PROMPT = (
    "You are a terse assistant. Answer with just the result."
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--skip-local", action="store_true")
    parser.add_argument("--timeout", type=int, default=1500)
    args = parser.parse_args()

    if not args.skip_local:
        print("── local docker smoke (pre-CodeBuild gate)…")
        repo_root = Path(__file__).resolve().parents[2]
        smoke = subprocess.run(
            ["bash", str(repo_root / "scripts" / "local_container_smoke.sh")],
            capture_output=True,
            text=True,
        )
        print(smoke.stdout[-600:])
        if smoke.returncode != 0:
            print(f"local smoke failed:\n{smoke.stderr[-1500:]}")
            return 1

    client = e2e_client(args.base, timeout=180)
    for agent in client.get("/api/agents").json()["agents"]:
        if agent["name"] == AGENT_NAME:
            print(f"cleaning leftover agent {agent['id']}")
            client.delete(f"/api/agents/{agent['id']}")

    print("── creating container agent…")
    res = client.post(
        "/api/agents",
        json={"name": AGENT_NAME, "method": "container", "system_prompt": SYSTEM_PROMPT},
    )
    res.raise_for_status()
    body = res.json()
    agent_id, job_id = body["agent"]["id"], body["job_id"]
    print(f"agent {agent_id} · job {job_id}")

    seen: set[str] = set()
    deadline = time.time() + args.timeout
    status = "deploying"
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        for ev in job["events"]:
            key = f"{ev['ts']}{ev['msg'][:40]}"
            if key not in seen:
                seen.add(key)
                print(f"  [{ev['ts'][11:19]}] {ev['stage']:<9} {ev['msg'][:100]}")
        status = client.get(f"/api/agents/{agent_id}").json()["status"]
        if status in ("active", "failed"):
            break
        # Real-AWS deployment polling is intentionally paced and attempt-bounded.
        time.sleep(10)  # nosemgrep: arbitrary-sleep

    if status != "active":
        print(f"FAILED: agent status={status} error={job.get('error')}")
        return 1

    arn = client.get(f"/api/agents/{agent_id}").json()["arn"]
    print(f"── runtime READY · arn {arn}")

    print("── invoking: what is 6*7?")
    inv = client.post(
        f"/api/agents/{agent_id}/invoke",
        json={"prompt": "What is 6*7? Reply with just the number."},
    )
    inv.raise_for_status()
    answer = inv.json()
    print(f"answer: {answer['text']!r} · {answer['latency_ms']}ms")
    assert "42" in answer["text"], f"expected '42', got: {answer['text']!r}"
    print("── invoke assertion PASSED")

    if args.keep:
        print("--keep set: leaving agent deployed")
        return 0
    print("── deleting agent…")
    client.delete(f"/api/agents/{agent_id}").raise_for_status()
    print(f"ledger status after delete: {client.get(f'/api/agents/{agent_id}').json()['status']}")
    print("E2E CLAUDE SDK: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
