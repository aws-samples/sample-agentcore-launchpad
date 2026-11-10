#!/usr/bin/env python3
"""E2E: per-agent online evaluation config — create → traffic → results → patch → delete.

Against REAL AWS (needs `make bootstrap` + credentials + a running backend with the
evaluation worker installed — N12: the config is **local truth**, scoring happens on the
hub scheduler → `online_eval` job on the worker → `eval_results` rows, never an AWS
OnlineEvaluationConfig). Preconditions the script checks before creating anything:

  * `LAUNCHPAD_ONLINE_EVAL_ENABLED` is on for the running backend and
    `LAUNCHPAD_EVAL_EXECUTOR=runtime` with the worker preset installed (operator-checked —
    the API does not expose the flag; with it off nothing is ever sampled and step 4 times
    out, the config stays ACTIVE with zero results)
  * the chosen agent has telemetry (`/aws/bedrock-agentcore/runtimes/<resource>-DEFAULT`
    exists — the create route answers 409 `online_eval.no_telemetry` otherwise)

Flow (default agent: the first active zip/studio/container/harness agent, or --agent):
  1. POST /api/eval/online with sampling 100 %, session timeout 1 min, two evaluators,
     NO filters (filter keys must be span attributes the Logs Insights scan matches on)
  2. the config is ACTIVE / ENABLED at once (no AWS resource to wait for)
  3. drive two chat sessions through /api/chat/{id} (real invocations → spans)
  4. poll GET …/results?range=1h until records show up: the scheduler samples a session
     once it has been idle for the timeout, dispatches one worker job per tick, and the
     job's `results.jsonl` is ingested on the next tick (observed lag ≈ tick + judge time,
     2–5 min; give up after --results-timeout). Also asserts the sampled sessions show
     the scores in GET /api/observability/sessions/{id} (`online_scores`) and that
     GET /api/overview/online-quality counts them.
  5. pause → resume → PATCH a filter in → PATCH sampling only → assert the filter and
     the timeout survived (a PATCH is a row update; nothing is replaced wholesale)
  6. DELETE → config gone from the list; the results rows are kept (the response still
     names the EMF mirror log group for operators who enabled the mirror)

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
# drops every session silently.
FILTER = {"key": "user.region", "operator": "Equals", "value": {"stringValue": "cn-northwest-1"}}
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--agent", default=None, help="agent name (default: first eligible)")
    parser.add_argument("--results-timeout", type=int, default=900)
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

    print("\n── 2. ACTIVE at once (local truth)")
    cfg = client.get(f"/api/eval/online/{config_id}").json()
    assert cfg["status"] == "ACTIVE" and cfg["execution_status"] == "ENABLED", cfg
    rows = client.get("/api/eval/online").json()["configs"]
    mine = next(r for r in rows if r["config_id"] == config_id)
    assert mine["owner"] == "agent" and mine["agent_id"] == agent["id"]
    print(f"    ACTIVE · listed as owner={mine['owner']} · evaluators={mine['evaluators']}")

    print("\n── 3. traffic (two sessions)")
    sessions = [chat(client, agent["id"], prompt) for prompt in PROMPTS]

    print("\n── 4. results (sampled after the 1-minute idle timeout, judged on the worker)")
    deadline = time.time() + args.results_timeout
    body = None
    while time.time() < deadline:
        cfg = client.get(f"/api/eval/online/{config_id}").json()
        if cfg["status"] == "FAILED":
            print(f"    config FAILED: {cfg.get('failure_reason')}")
            return 1
        body = client.get(f"/api/eval/online/{config_id}/results?range=1h").json()
        if body["evaluators"]:
            break
        time.sleep(15)
        print("    … waiting")
    if not body or not body["evaluators"]:
        print(f"    no results within {args.results_timeout}s — check the scheduler log and "
              f"the worker's status.json under evaluations/oe-{config_id}-*/")
        return 1
    for ev in body["evaluators"]:
        print(f"    {ev['evaluator_id']}: mean={ev['mean']} n={ev['count']} labels={ev['labels']}")
    print(f"    recent={len(body['recent'])} · errors={body['errors']}")
    assert any(r["explanation"] for r in body["recent"]), "expected judge explanations"
    judged = {r["session_id"] for r in body["recent"]}
    assert judged & set(sessions), f"judged {judged} ∌ our sessions {sessions}"
    sid = next(s for s in sessions if s in judged)
    detail = client.get(f"/api/observability/sessions/{sid}?range=1h").json()
    scores = detail.get("online_scores") or []
    assert any(s["config_id"] == config_id for s in scores), detail.get("online_scores")
    print(f"    session {sid} carries online_scores from this config ✓")
    quality = client.get("/api/overview/online-quality").json()
    assert quality["scores"] >= 1 and quality["configs"] >= 1, quality
    print(f"    overview online-quality: mean={quality['mean']} scores={quality['scores']} ✓")

    print("\n── 5. pause / resume / patch")
    paused = client.post(f"/api/eval/online/{config_id}/pause").json()
    assert paused["execution_status"] == "DISABLED", paused
    resumed = client.post(f"/api/eval/online/{config_id}/resume").json()
    assert resumed["execution_status"] == "ENABLED", resumed
    res = client.patch(f"/api/eval/online/{config_id}", json={"filters": [FILTER]})
    assert res.status_code == 200, res.text
    res = client.patch(f"/api/eval/online/{config_id}", json={"sampling_percentage": 42.5})
    assert res.status_code == 200, res.text
    cfg = client.get(f"/api/eval/online/{config_id}").json()
    assert cfg["status"] == "ACTIVE" and cfg["sampling_percentage"] == 42.5, cfg
    assert cfg["session_timeout_minutes"] == 1, "timeout dropped by a partial PATCH"
    assert cfg["filters"] == [FILTER], "filters dropped by a partial PATCH"
    print("    sampling 42.5 · timeout + filter retained ✓")

    if args.keep:
        print(f"\n--keep: leaving {config_id} in place")
        return 0

    print("\n── 6. delete")
    res = client.delete(f"/api/eval/online/{config_id}")
    assert res.status_code == 200, res.text
    assert res.json()["deleted"] is True
    print(f"    EMF mirror log group named: {res.json()['results_log_group']}")
    ids = {r["config_id"] for r in client.get("/api/eval/online").json()["configs"]}
    if config_id in ids:
        print("    config still listed after delete")
        return 1
    detail = client.get(f"/api/observability/sessions/{sid}?range=1h").json()
    assert any(s["config_id"] == config_id for s in detail.get("online_scores") or []), (
        "results of a deleted config must stay on the session")
    print("    gone from the list · results kept on the session ✓")
    print("\nALL GOOD")
    return 0


if __name__ == "__main__":
    sys.exit(main())
