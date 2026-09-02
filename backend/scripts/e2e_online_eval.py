#!/usr/bin/env python3
"""E2E: per-agent online evaluation config — create → traffic → results → patch → delete.

Against REAL AWS (needs `make bootstrap` + credentials + a running backend).

Flow (default agent: the first active zip/studio/container/harness agent, or --agent):
  1. POST /api/eval/online with sampling 100 %, session timeout 1 min, two evaluators,
     NO filters (live 2026-09-02: a `session.id NotContains …` filter silently excluded
     every session — filter keys must be span attributes AWS actually matches on)
  2. wait until the config is ACTIVE / ENABLED
  3. drive two chat sessions through /api/chat/{id} (real invocations → spans)
  4. poll GET …/results?range=1h until records show up (judging starts once the
     sessions have been idle for the timeout; observed lag ≈ 9–10 min after the
     session; give up after --results-timeout)
  5. pause → resume → PATCH a filter in → PATCH sampling only → assert the filter
     and the timeout survived (AWS replaces `rule` wholesale; the backend must
     re-send the complete rule)
  6. DELETE → config gone from the list; results log group still exists

Run:  cd backend && uv run python scripts/e2e_online_eval.py [--agent <name>] [--keep]
"""

import argparse
import json
import sys
import time

import httpx
from _e2e_client import e2e_client

EVALUATORS = ["Builtin.Helpfulness", "Builtin.Refusal"]
# Added AFTER results are seen (step 5) — never at create: an unmatched filter key
# drops every session silently. Key from the AWS CDK docs' filter example.
FILTER = {"key": "user.region", "operator": "Equals", "value": {"stringValue": "us-west-2"}}
PROMPTS = [
    "Give me one sentence on what you can help with.",
    "What is 17 * 23? Answer with the number only.",
]


def chat(client: httpx.Client, agent_id: str, prompt: str) -> str:
    text, sid = "", None
    with client.stream("POST", f"/api/chat/{agent_id}", json={"prompt": prompt}) as res:
        res.raise_for_status()
        event = None
        for line in res.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:") and event:
                data = json.loads(line[5:])
                if event == "meta":
                    sid = data["session_id"]
                elif event == "delta":
                    text += data["text"]
                elif event == "error":
                    raise RuntimeError(data["message"])
    print(f"    session {sid} · {len(text)} chars")
    return sid or ""


def wait_status(client: httpx.Client, config_id: str, *, want: str, timeout: int = 120) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        cfg = client.get(f"/api/eval/online/{config_id}").json()
        if cfg["status"] == want:
            return cfg
        if cfg["status"] in ("CREATE_FAILED", "UPDATE_FAILED", "ERROR"):
            raise RuntimeError(f"config {cfg['status']}: {cfg.get('failure_reason')}")
        time.sleep(3)
    raise TimeoutError(f"config never reached {want}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--agent", default=None, help="agent name (default: first eligible)")
    parser.add_argument("--results-timeout", type=int, default=1200)
    parser.add_argument("--keep", action="store_true", help="skip the delete step")
    args = parser.parse_args()
    client = e2e_client(args.base, timeout=300)

    agents = client.get("/api/agents").json()["agents"]
    eligible = [
        a for a in agents
        if a["status"] == "active"
        and a["method"] in ("zip_runtime", "studio", "container", "harness")
        and (args.agent is None or a["name"] == args.agent)
    ]
    if not eligible:
        print("no eligible active agent — deploy one first (or check --agent)")
        return 1
    agent = eligible[0]
    print(f"agent: {agent['name']} ({agent['method']}) · {agent['id']}")

    print("\n── 1. create")
    res = client.post("/api/eval/online", json={
        "agent_id": agent["id"],
        "evaluators": EVALUATORS,
        "sampling_percentage": 100,
        "session_timeout_minutes": 1,
        "description": "e2e_online_eval probe",
        "enable_on_create": True,
    })
    if res.status_code != 201:
        print(f"create failed {res.status_code}: {res.text}")
        return 1
    cfg = res.json()
    config_id = cfg["config_id"]
    print(f"    {cfg['name']} · {config_id} · {cfg['status']}/{cfg['execution_status']}")
    assert cfg["owner"] == "agent" and cfg["filter_count"] == 0

    print("\n── 2. wait ACTIVE")
    cfg = wait_status(client, config_id, want="ACTIVE")
    assert cfg["execution_status"] == "ENABLED", cfg
    rows = client.get("/api/eval/online").json()["configs"]
    mine = next(r for r in rows if r["config_id"] == config_id)
    assert mine["owner"] == "agent" and mine["agent_id"] == agent["id"]
    print(f"    ACTIVE · listed as owner={mine['owner']} · evaluators={mine['evaluators']}")

    print("\n── 3. traffic (two sessions)")
    for prompt in PROMPTS:
        chat(client, agent["id"], prompt)

    print("\n── 4. results (judging starts after the 1-minute idle timeout)")
    deadline = time.time() + args.results_timeout
    body = None
    while time.time() < deadline:
        body = client.get(f"/api/eval/online/{config_id}/results?range=1h").json()
        if body["evaluators"]:
            break
        time.sleep(15)
        print("    … waiting")
    if not body or not body["evaluators"]:
        group = body and body["log_group"]
        print(f"    no results within {args.results_timeout}s — inspect {group}")
        return 1
    for ev in body["evaluators"]:
        print(f"    {ev['evaluator_id']}: mean={ev['mean']} n={ev['count']} labels={ev['labels']}")
    print(f"    recent={len(body['recent'])} · errors={body['errors']}")
    assert any(r["explanation"] for r in body["recent"]), "expected judge explanations"

    print("\n── 5. pause / resume / patch")
    paused = client.post(f"/api/eval/online/{config_id}/pause").json()
    assert paused["execution_status"] == "DISABLED", paused
    resumed = client.post(f"/api/eval/online/{config_id}/resume").json()
    assert resumed["execution_status"] == "ENABLED", resumed
    res = client.patch(f"/api/eval/online/{config_id}", json={"filters": [FILTER]})
    assert res.status_code == 200, res.text
    wait_status(client, config_id, want="ACTIVE")
    res = client.patch(f"/api/eval/online/{config_id}", json={"sampling_percentage": 42.5})
    assert res.status_code == 200, res.text
    cfg = wait_status(client, config_id, want="ACTIVE")
    assert cfg["sampling_percentage"] == 42.5, cfg
    assert cfg["session_timeout_minutes"] == 1, "timeout dropped — rule was not sent complete"
    assert cfg["filters"] == [FILTER], "filters dropped — rule was not sent complete"
    print("    sampling 42.5 · timeout + filter retained ✓")

    if args.keep:
        print(f"\n--keep: leaving {config_id} in place")
        return 0

    print("\n── 6. delete")
    res = client.delete(f"/api/eval/online/{config_id}")
    assert res.status_code == 200, res.text
    print(f"    results log group kept: {res.json()['results_log_group']}")
    for _ in range(20):
        ids = {r["config_id"] for r in client.get("/api/eval/online").json()["configs"]}
        if config_id not in ids:
            break
        time.sleep(3)
    else:
        print("    config still listed after 60s")
        return 1
    print("    gone from the list ✓")
    print("\nALL GOOD")
    return 0


if __name__ == "__main__":
    sys.exit(main())
