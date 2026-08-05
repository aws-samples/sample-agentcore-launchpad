#!/usr/bin/env python3
"""E2E: REAL batch evaluation + insights for a platform-deployed zip agent.

Flow: ensure eval-target agent (zip_runtime) → upload 3-item dataset →
      evaluators run (Correctness+Helpfulness) → scores → insights run over
      the same sessions → failure/intent excerpt.

Run:  cd backend && uv run python scripts/e2e_eval_run.py [--keep]
"""

import argparse
import json
import sys
import time

import httpx
from _e2e_client import e2e_client

AGENT_NAME = "eval-target"
DATASET = [
    {"prompt": "What is 12*9? Use the calculator tool and answer with just the number.",
     "expected": "108"},
    {"prompt": "What is 45+55? Use the calculator tool and answer with just the number.",
     "expected": "100"},
    {"prompt": "What is 144/12? Use the calculator tool and answer with just the number.",
     "expected": "12"},
]


def wait_run(client: httpx.Client, run_id: str, timeout_s: int = 1800) -> dict:
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        run = client.get(f"/api/eval/runs/{run_id}").json()
        state = f"{run['status']} (queue={run['queue_position']})"
        if state != last:
            print(f"  [{time.strftime('%H:%M:%S')}] {state}")
            last = state
        if run["status"] in ("completed", "failed"):
            return run
        # Real-AWS evaluation polling is intentionally paced and attempt-bounded.
        time.sleep(15)  # nosemgrep: arbitrary-sleep
    raise TimeoutError("run did not finish")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    client = e2e_client(args.base, timeout=300)

    agents = client.get("/api/agents").json()["agents"]
    agent = next((a for a in agents if a["name"] == AGENT_NAME and a["status"] == "active"), None)
    if agent is None:
        print("── deploying eval-target (zip_runtime)…")
        res = client.post("/api/agents", json={
            "name": AGENT_NAME, "method": "zip_runtime",
            "system_prompt": "You are a precise math assistant. Use the calculator tool "
                             "for arithmetic and answer with just the result.",
            "memory": {"short_term": False, "long_term": False},
        })
        res.raise_for_status()
        agent_id = res.json()["agent"]["id"]
        for _ in range(90):
            agent = client.get(f"/api/agents/{agent_id}").json()
            if agent["status"] in ("active", "failed"):
                break
            # Real-AWS deployment polling is intentionally paced and attempt-bounded.
            time.sleep(10)  # nosemgrep: arbitrary-sleep
        assert agent["status"] == "active", "eval-target failed to deploy"
    print(f"agent: {AGENT_NAME} · {agent['arn'][-45:]}")

    print("── uploading 3-item dataset…")
    dataset = client.post("/api/eval/datasets", json={
        "name": "e2e-math-mini", "items": DATASET,
    }).json()

    print("── evaluators listing:")
    evaluators = client.get("/api/eval/evaluators").json()
    print(f"  builtin: {evaluators['builtin_count']}")

    print("── starting evaluators run (Correctness + Helpfulness)…")
    run = client.post("/api/eval/runs", json={
        "agent_id": agent["id"], "dataset_id": dataset["id"],
        "evaluators": ["Builtin.Correctness", "Builtin.Helpfulness"],
        "wait_seconds": 180,
    }).json()
    result = wait_run(client, run["id"])
    if result["status"] != "completed":
        print(f"RUN FAILED: {result['error']}")
        return 1
    print(f"  batch: {result['batch_eval_id']}")
    print(f"  scores: {json.dumps(result['scores'], indent=1)}")
    assert len(result["scores"]) >= 1, "expected numeric scores"

    print("── starting insights run over the same sessions…")
    insights_run = client.post("/api/eval/runs", json={
        "agent_id": agent["id"], "mode": "insights",
        "session_ids": result["session_ids"], "wait_seconds": 0,
    }).json()
    insights_result = wait_run(client, insights_run["id"])
    if insights_result["status"] != "completed":
        print(f"INSIGHTS FAILED: {insights_result['error']}")
        return 1
    excerpt = json.dumps(insights_result["insights"], ensure_ascii=False)[:900]
    print(f"  insights excerpt: {excerpt}")

    if not args.keep:
        client.delete(f"/api/eval/datasets/{dataset['id']}")
    print("E2E EVAL RUN: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
