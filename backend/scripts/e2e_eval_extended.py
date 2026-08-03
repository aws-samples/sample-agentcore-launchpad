#!/usr/bin/env python3
"""E2E: REAL AWS verification of the evaluation extensions.

Flow (all through the launchpad backend API — the platform chain, not raw
boto3): custom-evaluator CRUD (create → get → update → get) → time-window
evaluators run scored by the custom judge → insights run with a type subset →
scenario dataset with ground truth (edit → sync to an AWS Dataset resource) →
ground-truth dataset run (Trajectory + Correctness) → cleanup.

The account allows ONE batch evaluation at a time — every run is polled to a
terminal state before the next starts.

Run:  cd backend && uv run python scripts/e2e_eval_extended.py
"""

import json
import sys
import time
import uuid

import httpx

AGENT_NAME = "eval-target"
LOOKBACK_HOURS = 24

GT_SCENARIOS = [
    {
        "scenario_id": "add_two_numbers",
        "turns": [{"input": "What is 17 + 25? Use the calculator tool and answer "
                            "with just the number.",
                   "expected_response": "42"}],
        "assertions": ["The agent answers with the exact sum 42"],
        "expected_trajectory": ["calculator"],
    },
    {
        "scenario_id": "multiply_numbers",
        "turns": [{"input": "What is 6 * 7? Use the calculator tool and answer "
                            "with just the number.",
                   "expected_response": "42"}],
        "assertions": ["The agent answers with the exact product 42"],
        "expected_trajectory": ["calculator"],
    },
    {
        "scenario_id": "divide_numbers",
        "turns": [{"input": "What is 144 / 12? Use the calculator tool and answer "
                            "with just the number.",
                   "expected_response": "12"}],
        "assertions": ["The agent answers with the exact quotient 12"],
        "expected_trajectory": ["calculator"],
    },
]


def wait_run(client: httpx.Client, run_id: str, timeout_s: int = 2400) -> dict:
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        run = client.get(f"/api/eval/runs/{run_id}").json()
        state = f"{run['status']} (queue={run['queue_position']})"
        if state != last:
            print(f"  [{time.strftime('%H:%M:%S')}] {state}", flush=True)
            last = state
        if run["status"] in ("completed", "failed"):
            return run
        time.sleep(15)
    raise TimeoutError(f"run {run_id} did not finish in {timeout_s}s")


def main() -> int:
    client = httpx.Client(base_url="http://localhost:8000", timeout=300)
    suffix = uuid.uuid4().hex[:6]

    agents = client.get("/api/agents").json()["agents"]
    agent = next(a for a in agents if a["name"] == AGENT_NAME and a["status"] == "active")
    print(f"agent: {AGENT_NAME} · {agent['arn'][-45:]}", flush=True)

    # ── 1. custom evaluator CRUD ─────────────────────────────────────────────
    judge_name = f"e2e_brevity_{suffix}"
    print(f"── [1] create custom evaluator {judge_name} …", flush=True)
    created = client.post("/api/eval/evaluators", json={
        "name": judge_name,
        "level": "TRACE",
        "description": "Scores replies for brevity",
        "instructions": "Rate how brief the assistant reply is while still "
                        "answering the question.\n\nContext: {context}\n"
                        "Assistant reply: {assistant_turn}",
        "rating_scale": [
            {"value": 1.0, "label": "terse", "definition": "minimal words, complete answer"},
            {"value": 0.5, "label": "adequate", "definition": "some filler, answer intact"},
            {"value": 0.0, "label": "verbose", "definition": "rambling or padded reply"},
        ],
    })
    created.raise_for_status()
    judge_id = created.json()["evaluator_id"]
    print(f"  evaluatorId: {judge_id}", flush=True)

    detail = client.get(f"/api/eval/evaluators/{judge_id}").json()
    assert detail["description"] == "Scores replies for brevity", detail
    assert len(detail["rating_scale"]) == 3
    print(f"  GET ok · description: {detail['description']!r} · "
          f"scale: {len(detail['rating_scale'])} points", flush=True)

    print("── [1] update (description + 2-point scale) …", flush=True)
    updated = client.put(f"/api/eval/evaluators/{judge_id}", json={
        "level": "TRACE",
        "description": "Scores replies for brevity — v2 tightened",
        "instructions": detail["instructions"],
        "rating_scale": [
            {"value": 1.0, "label": "brief", "definition": "no filler at all"},
            {"value": 0.0, "label": "wordy", "definition": "any filler present"},
        ],
    })
    updated.raise_for_status()
    after = client.get(f"/api/eval/evaluators/{judge_id}").json()
    assert after["description"] == "Scores replies for brevity — v2 tightened", after
    assert len(after["rating_scale"]) == 2, after
    assert {p["label"] for p in after["rating_scale"]} == {"brief", "wordy"}
    print(f"  PUT→GET ok · description: {after['description']!r} · "
          f"scale labels: {[p['label'] for p in after['rating_scale']]}", flush=True)

    # ── 2. traffic + time-window evaluators run (with the custom judge) ─────
    print("── [2] seeding window traffic: 3 invokes …", flush=True)
    for i, scenario in enumerate(GT_SCENARIOS, 1):
        res = client.post(f"/api/agents/{agent['id']}/invoke",
                          json={"prompt": scenario["turns"][0]["input"]})
        res.raise_for_status()
        print(f"  invoke {i}: {res.json()['text'][:40]!r}", flush=True)
    print("  waiting 120s for traces to land in CloudWatch …", flush=True)
    time.sleep(120)

    print(f"── [2] window run: lookback {LOOKBACK_HOURS}h · "
          f"[Builtin.Helpfulness, {judge_id}] …", flush=True)
    run = client.post("/api/eval/runs", json={
        "agent_id": agent["id"], "mode": "evaluators",
        "lookback_hours": LOOKBACK_HOURS,
        "evaluators": ["Builtin.Helpfulness", judge_id],
    })
    run.raise_for_status()
    window_run = wait_run(client, run.json()["id"])
    assert window_run["status"] == "completed", window_run.get("error")
    print(f"  batch: {window_run['batch_eval_id']}", flush=True)
    print(f"  dataset column: {window_run['dataset_name']}", flush=True)
    print(f"  scores: {json.dumps(window_run['scores'])}", flush=True)
    scored = {s["evaluatorId"]: s["score"] for s in window_run["scores"]}
    assert "Builtin.Helpfulness" in scored, scored
    assert judge_id in scored and scored[judge_id] is not None, scored
    print(f"  custom judge scored: {judge_id} = {scored[judge_id]}", flush=True)

    # ── 3. insights run with a 2-type subset ─────────────────────────────────
    print("── [3] insights run: lookback 24h · [UserIntent, ExecutionSummary] …",
          flush=True)
    run = client.post("/api/eval/runs", json={
        "agent_id": agent["id"], "mode": "insights",
        "lookback_hours": LOOKBACK_HOURS,
        "insights": ["Builtin.Insight.UserIntent", "Builtin.Insight.ExecutionSummary"],
    })
    run.raise_for_status()
    insights_run = wait_run(client, run.json()["id"])
    assert insights_run["status"] == "completed", insights_run.get("error")
    trees = insights_run["insights"]
    print(f"  batch: {insights_run['batch_eval_id']}", flush=True)
    print(f"  trees: {sorted(trees.keys())} · "
          f"sizes: {({k: len(v) for k, v in trees.items()})}", flush=True)
    assert "failures" not in trees, trees.keys()
    assert set(trees.keys()) <= {"userIntents", "executionSummaries"}, trees.keys()
    assert any(trees.values()), "expected at least one non-empty insight tree"
    excerpt = json.dumps(trees, ensure_ascii=False)[:400]
    print(f"  excerpt: {excerpt}", flush=True)

    # ── 4. ground-truth dataset: create → edit → sync to AWS ────────────────
    ds_name = f"e2e_gt_math_{suffix}"
    print(f"── [4] create dataset {ds_name} (3 GT scenarios) …", flush=True)
    dataset = client.post("/api/eval/datasets", json={
        "name": ds_name, "description": "e2e ground-truth math set",
        "items": GT_SCENARIOS,
    })
    dataset.raise_for_status()
    ds = dataset.json()
    assert ds["kind"] == "predefined" and ds["has_ground_truth"] is True, ds
    print(f"  local id: {ds['id']} · kind: {ds['kind']} · GT: {ds['has_ground_truth']}",
          flush=True)

    edited = client.put(f"/api/eval/datasets/{ds['id']}", json={
        "description": "e2e ground-truth math set — edited",
    })
    edited.raise_for_status()
    assert edited.json()["description"].endswith("edited")
    print("  PUT edit ok", flush=True)

    print("  sync-to-aws …", flush=True)
    synced = client.post(f"/api/eval/datasets/{ds['id']}/sync-to-aws")
    if synced.status_code != 200:
        print(f"SYNC FAILED: {synced.text}")
        return 1
    cloud = synced.json()["cloud"]
    assert cloud["status"] == "ACTIVE", cloud
    print(f"  cloud datasetId: {cloud['dataset_id']} · status: {cloud['status']}",
          flush=True)
    listing = client.get("/api/eval/datasets/cloud").json()["datasets"]
    assert any(c["datasetId"] == cloud["dataset_id"] for c in listing), listing
    print(f"  visible in GET /datasets/cloud ({len(listing)} total)", flush=True)

    # ── 5. ground-truth dataset run (Trajectory + Correctness + GSR) ────────
    print("── [5] dataset run with ground-truth evaluators …", flush=True)
    run = client.post("/api/eval/runs", json={
        "agent_id": agent["id"], "dataset_id": ds["id"],
        "evaluators": ["Builtin.Correctness", "Builtin.GoalSuccessRate",
                       "Builtin.TrajectoryInOrderMatch"],
        "wait_seconds": 180,
    })
    run.raise_for_status()
    gt_run = wait_run(client, run.json()["id"])
    assert gt_run["status"] == "completed", gt_run.get("error")
    print(f"  batch: {gt_run['batch_eval_id']}", flush=True)
    print(f"  sessions: {len(gt_run['session_ids'])}", flush=True)
    print(f"  scores: {json.dumps(gt_run['scores'])}", flush=True)
    gt_scored = {s["evaluatorId"]: s["score"] for s in gt_run["scores"]}
    for ev in ("Builtin.Correctness", "Builtin.GoalSuccessRate",
               "Builtin.TrajectoryInOrderMatch"):
        assert ev in gt_scored and gt_scored[ev] is not None, (ev, gt_scored)
    print(f"  TrajectoryInOrderMatch = {gt_scored['Builtin.TrajectoryInOrderMatch']} "
          "(ground truth injected OK)", flush=True)

    # ── 6. cleanup ───────────────────────────────────────────────────────────
    print("── [6] cleanup …", flush=True)
    res = client.delete(f"/api/eval/evaluators/{judge_id}")
    print(f"  evaluator {judge_id}: {'deleted' if res.status_code == 200 else res.text}",
          flush=True)
    res = client.delete(f"/api/eval/datasets/cloud/{cloud['dataset_id']}")
    print(f"  cloud dataset {cloud['dataset_id']}: "
          f"{'deleted' if res.status_code == 200 else res.text}", flush=True)
    print(f"  local dataset {ds['id']} kept as demo material", flush=True)

    print("E2E EVAL EXTENDED: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
