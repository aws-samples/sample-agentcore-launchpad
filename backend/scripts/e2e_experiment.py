#!/usr/bin/env python3
"""E2E: configuration A/B plus independent Runtime Canary against real AWS.

Drives both user workflows in order, exactly like the UI:
recommend → accept → bundles → gateway → abtest → traffic → verdict →
promote, then a separate canary record through
90/10 → 50/50 → 1/99 with traffic and verdict gates at every stage.

Async actions return 202; this waits on running_action/progress like the
page's poll loop does.

Run:  cd backend && uv run python scripts/e2e_experiment.py
"""

import json
import sys
import time

import httpx

AGENT = "eval-target"
CHALLENGER = "eval-target-v2"


def action(
    client,
    collection,
    row_id,
    response_key,
    name,
    timeout_s=1800,
    **fields,
):
    """POST one stage action, then poll the row until the runner is idle."""
    res = client.post(f"{collection}/{row_id}/action",
                      json={"action": name, **fields})
    res.raise_for_status()
    row = res.json()[response_key]
    if res.status_code != 202:  # sync action — done already
        return row
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        row = client.get(f"{collection}/{row_id}").json()
        line = row.get("progress") or row.get("stage")
        if line != last:
            print(f"  [{time.strftime('%H:%M:%S')}] {line}")
            last = line
        if not row.get("running_action"):
            if row.get("error"):
                raise RuntimeError(f"action {name} failed: {row['error']}")
            return row
        # Real-AWS action polling is intentionally paced and attempt-bounded.
        time.sleep(10)  # nosemgrep: arbitrary-sleep
    raise TimeoutError(f"action {name} timed out")


def ensure_agent(client, name, prompt):
    agents = client.get("/api/agents").json()["agents"]
    agent = next((a for a in agents if a["name"] == name and a["status"] == "active"), None)
    if agent:
        return agent
    print(f"── deploying {name}…")
    res = client.post("/api/agents", json={
        "name": name, "method": "zip_runtime", "system_prompt": prompt,
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
    assert agent["status"] == "active"
    return agent


def main() -> int:
    client = httpx.Client(base_url="http://localhost:8000", timeout=300)
    agent = ensure_agent(
        client, AGENT,
        "You are a precise math assistant. Use the calculator tool for arithmetic "
        "and answer with just the result.",
    )
    challenger = ensure_agent(
        client, CHALLENGER,
        "You are a rigorous math assistant v2. ALWAYS verify with the calculator "
        "tool and reply with ONLY the final number.",
    )

    # resume an interrupted run instead of tripping the one-running guard —
    # completed actions are skipped by artifact presence, like the UI does
    running = [e for e in client.get("/api/experiments").json()["experiments"]
               if e["status"] == "running"]
    if running:
        exp = running[0]
        exp_id = exp["id"]
        print(f"── resuming running experiment {exp_id} "
              f"(artifacts: {sorted(exp['artifacts'])})…")
    else:
        print("── create experiment (no stage work yet)…")
        res = client.post("/api/experiments", json={"agent_id": agent["id"]})
        res.raise_for_status()
        exp = res.json()
        exp_id = exp["id"]
        assert exp["stage"] == "recommend" and set(exp["artifacts"]) == {"agent_meta"}, \
            "create must defer all stage work"

    def ensure(name, done, **fields):
        nonlocal exp
        if done(exp["artifacts"]):
            print(f"── {name}: already done, skipping")
            return exp
        print(f"── {name}…")
        exp = action(
            client,
            "/api/experiments",
            exp_id,
            "experiment",
            name,
            **fields,
        )
        return exp

    ensure("recommend", lambda a: "recommend" in a)
    rec = exp["artifacts"]["recommend"]
    print("  prompt suggestion:", rec["recommended_prompt"][:220].replace("\n", " "))
    print("  tool suggestions:", json.dumps(rec["tool_descriptions"])[:220])

    ensure("accept", lambda a: a["recommend"].get("accepted_prompt"),
           accepted_prompt=rec["recommended_prompt"])

    ensure("bundles", lambda a: "bundles" in a)
    art = exp["artifacts"]
    print("  control:  ", art["bundles"]["control"]["arn"])
    print("  treatment:", art["bundles"]["treatment"]["arn"])

    ensure("gateway", lambda a: "gateway" in a)
    art = exp["artifacts"]
    print("  gateway:", art["gateway"]["gateway_id"], "· target",
          art["gateway"]["target_v1"])

    ensure("abtest", lambda a: "abtest" in a)
    art = exp["artifacts"]
    print("  abtest:", art["abtest"]["ab_test_id"], "· weights",
          [v["weight"] for v in art["abtest"]["variants"]])

    datasets = client.get("/api/eval/datasets").json()["datasets"]
    usable = next((d for d in datasets if d["kind"] in ("legacy", "predefined")
                   and d["item_count"] > 0), None)
    if usable is None:
        raise RuntimeError("traffic requires a non-empty legacy or predefined dataset")
    traffic_fields = {"dataset_id": usable["id"]}
    print(f"── traffic dataset: {usable['name']} ({usable['item_count']} items)")
    ensure("traffic", lambda a: "traffic" in a, **traffic_fields)
    print("  traffic:", {k: v for k, v in exp["artifacts"]["traffic"].items()
                         if k != "session_ids"})

    print("── verdict (monitoring)…")
    exp = action(
        client, "/api/experiments", exp_id, "experiment", "verdict"
    )
    verdict = exp["artifacts"]["verdict"]
    print("  verdict:", json.dumps({k: v for k, v in verdict.items()
                                    if k != "metrics"}))
    print("  metrics:", json.dumps(verdict["metrics"])[:400])

    ensure("promote", lambda a: "promote" in a)
    promote = exp["artifacts"]["promote"]
    print("  deployed version:", promote["agent_version"],
          "· A/B test:", promote["ab_test_status"])

    running_canaries = [
        row
        for row in client.get("/api/runtime-canaries").json()["canaries"]
        if row["status"] == "running"
        and row["champion_agent_id"] == agent["id"]
        and row["challenger_agent_id"] == challenger["id"]
    ]
    if running_canaries:
        canary = running_canaries[0]
        canary_id = canary["id"]
        print(f"── resuming Runtime Canary {canary_id}…")
    else:
        print("── create independent Runtime Canary…")
        res = client.post(
            "/api/runtime-canaries",
            json={
                "champion_agent_id": agent["id"],
                "challenger_agent_id": challenger["id"],
                "source_experiment_id": exp_id,
            },
        )
        res.raise_for_status()
        canary = res.json()
        canary_id = canary["id"]

    def canary_action(name, **fields):
        nonlocal canary
        print(f"── canary {name}…")
        canary = action(
            client,
            "/api/runtime-canaries",
            canary_id,
            "canary",
            name,
            **fields,
        )
        return canary

    if "setup" not in canary["artifacts"]:
        canary_action("setup")
    setup = canary["artifacts"]["setup"]
    print("  target A/B:", setup["ab_test_id"], "· weights:", setup["weights"])

    for ramp_stage in range(3):
        canary = client.get(f"/api/runtime-canaries/{canary_id}").json()
        rounds = canary["artifacts"].get("rounds", [])
        current = next(
            (row for row in rounds if row["ramp_stage"] == ramp_stage),
            None,
        )
        if current is None or not current.get("traffic_attempts"):
            canary_action("traffic", **traffic_fields)
            rounds = canary["artifacts"].get("rounds", [])
            current = next(
                row for row in rounds if row["ramp_stage"] == ramp_stage
            )
        if not current.get("verdict"):
            canary_action("verdict")
            current = next(
                row
                for row in canary["artifacts"]["rounds"]
                if row["ramp_stage"] == ramp_stage
            )

        verdict = current["verdict"]
        summary = {
            key: value for key, value in verdict.items() if key != "metrics"
        }
        print(f"  stage {ramp_stage} verdict:", json.dumps(summary))
        outcome = verdict["verdict"]
        if outcome in ("control-wins", "insufficient-data", "insufficient-n"):
            raise RuntimeError(
                f"canary stage {ramp_stage} is blocked by {outcome}; "
                "send more traffic or roll back"
            )
        allow_override = outcome == "tie" or verdict.get("significant") is False
        if ramp_stage < 2:
            if canary["artifacts"]["setup"]["ramp_stage"] == ramp_stage:
                canary_action(
                    "advance",
                    allow_non_significant=allow_override,
                )
        elif canary["status"] == "running":
            canary_action(
                "complete",
                allow_non_significant=allow_override,
            )

    print("── canary online eval config state:")
    import boto3
    control = boto3.client("bedrock-agentcore-control", region_name="us-west-2")
    setup = canary["artifacts"]["setup"]
    for role in ("champion", "challenger"):
        oe = control.get_online_evaluation_config(
            onlineEvaluationConfigId=setup[role]["online_eval_id"]
        )
        print(f"  {oe.get('onlineEvaluationConfigName')} · status={oe.get('status')} "
              f"· execution={oe.get('executionStatus')}")

    print("── canary cleanup…")
    canary_action("cleanup")
    canary_cleanup = canary["artifacts"]["cleanup"]
    for row in canary_cleanup:
        print(f"  {row['status']:<8} {row['category']:<28} {row['detail'][:60]}")

    print("── configuration experiment cleanup…")
    exp = action(
        client, "/api/experiments", exp_id, "experiment", "cleanup"
    )
    experiment_cleanup = exp["artifacts"]["cleanup"]
    for row in experiment_cleanup:
        print(f"  {row['status']:<8} {row['category']:<28} {row['detail'][:60]}")

    print("── shared gateway untouched check:")
    gw = control.get_gateway(gatewayIdentifier=setup["gateway_id"])
    print(f"  {gw['name']} status: {gw['status']}")

    failed = [
        row
        for row in canary_cleanup + experiment_cleanup
        if row["status"] != "deleted"
    ]
    if failed:
        print(f"cleanup had {len(failed)} skipped categories")
    print("E2E EXPERIMENT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
