#!/usr/bin/env python3
"""Golden-path E2E — the full customer journey on REAL AWS, then cleanup.

    1. bootstrap-verify   health + overview service map all green
    2. create agent       harness method, gateway HR tool, long-term memory
    3. chat ×2            same session — short-term memory continuity
    4. gateway tool       HR question answered via Cedar-enforced gateway
    5. registry           auto-created A2A record → approve → APPROVED
    6. mini eval          2-item dataset, 1 evaluator, real batch evaluation
    7. cleanup            agent + dataset deleted, record disabled

Run:  cd backend && uv run python scripts/e2e_golden_path.py [--keep] [--base URL]
"""

import argparse
import sys
import time
import uuid

import httpx

# unique per run — harness deletion is async, so a fixed name races re-runs
AGENT_NAME = f"golden-path-{uuid.uuid4().hex[:6]}"
SYSTEM_PROMPT = (
    "You are a concise HR assistant. Use the hr-database tool for employee "
    "questions. Answer in one short sentence."
)
RESULTS: list[tuple[str, str, str]] = []


def step(name: str, ok: bool, evidence: str) -> None:
    RESULTS.append((name, "PASS" if ok else "FAIL", evidence))
    print(f"── {name}: {'PASS' if ok else 'FAIL'} · {evidence}")
    if not ok:
        summary()
        sys.exit(1)


def summary() -> None:
    print("\n═══ GOLDEN PATH SUMMARY ═══")
    print(f"{'step':<18} {'result':<7} evidence")
    for name, result, evidence in RESULTS:
        print(f"{name:<18} {result:<7} {evidence[:90]}")


def wait_active(client: httpx.Client, agent_id: str, timeout: int = 300) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        agent = client.get(f"/api/agents/{agent_id}").json()
        if agent["status"] in ("active", "failed"):
            return agent
        time.sleep(5)
    return {"status": "timeout"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    client = httpx.Client(base_url=args.base, timeout=360)

    # 1 — bootstrap-verify
    health = client.get("/api/health").json()
    overview = client.get("/api/overview").json()
    services = overview["services"]
    down = [k for k, v in services.items() if not v and k != "evaluation"]
    step(
        "bootstrap-verify",
        health.get("status") == "ok" and not down,
        f"health ok · services {'all green' if not down else 'DOWN: ' + ','.join(down)}",
    )

    # 2 — create harness agent
    res = client.post(
        "/api/agents",
        json={
            "name": AGENT_NAME,
            "method": "harness",
            "system_prompt": SYSTEM_PROMPT,
            "tools": [{"type": "gateway", "name": "hr-database"}],
            "memory": {"short_term": True, "long_term": True},
        },
    )
    res.raise_for_status()
    agent_id = res.json()["agent"]["id"]
    agent = wait_active(client, agent_id)
    step(
        "create-agent",
        agent["status"] == "active",
        f"{AGENT_NAME} · {agent.get('arn', agent['status'])}",
    )

    try:
        # 3 — chat two turns, same session (short-term memory)
        turn1 = client.post(
            f"/api/agents/{agent_id}/invoke",
            json={"prompt": "Hi! My favourite colour is teal. Please remember that."},
        ).json()
        session_id = turn1["session_id"]
        turn2 = client.post(
            f"/api/agents/{agent_id}/invoke",
            json={
                "prompt": "What is my favourite colour? One word.",
                "session_id": session_id,
            },
        ).json()
        step(
            "chat-memory",
            "teal" in turn2["text"].lower(),
            f"turn2 → {turn2['text']!r} ({turn2['latency_ms']}ms, session {session_id[:12]}…)",
        )

        # 4 — gateway tool call (Cedar-enforced MCP gateway → HR lambda)
        hr = client.post(
            f"/api/agents/{agent_id}/invoke",
            json={"prompt": "How many vacation days does Maya Chen have left?"},
        ).json()
        step("gateway-tool", "9" in hr["text"], f"→ {hr['text']!r}")

        # 5 — registry approval chain on the auto-created A2A record
        record_id = client.get(f"/api/agents/{agent_id}").json()["registry_record_id"]
        assert record_id, "pipeline did not auto-register the agent"
        client.post(
            f"/api/registry/records/{record_id}/action", json={"action": "approve"}
        )
        record = client.get(f"/api/registry/records/{record_id}").json()
        step(
            "registry",
            record["status"] == "APPROVED",
            f"record {record_id} · {record['status']}",
        )

        # 6 — mini eval run: 2 items, 1 evaluator, real batch evaluation.
        # Batch evaluation targets runtime-backed agents (harness spans carry no
        # service name), so the journey deploys a small zip agent to evaluate.
        res = client.post(
            "/api/agents",
            json={
                "name": f"{AGENT_NAME}-eval",
                "method": "zip_runtime",
                "system_prompt": "You are a friendly, concise assistant.",
                "memory": {"short_term": False, "long_term": False},
            },
        )
        res.raise_for_status()
        eval_agent_id = res.json()["agent"]["id"]
        eval_agent = wait_active(client, eval_agent_id, timeout=600)
        assert eval_agent["status"] == "active", f"eval target: {eval_agent['status']}"

        dataset_res = client.post(
            "/api/eval/datasets",
            json={
                "name": "golden-path-mini",
                "items": [
                    {"prompt": "What is 21 * 2? Reply with just the number."},
                    {"prompt": "Say hello in one word."},
                ],
            },
        )
        dataset_res.raise_for_status()
        dataset = dataset_res.json()
        run_res = client.post(
            "/api/eval/runs",
            json={
                "agent_id": eval_agent_id,
                "dataset_id": dataset["id"],
                "evaluators": ["Builtin.Helpfulness"],
                "mode": "evaluators",
                "wait_seconds": 180,
            },
        )
        run_res.raise_for_status()
        run = run_res.json()
        deadline = time.time() + 900
        while time.time() < deadline:
            run = client.get(f"/api/eval/runs/{run['id']}").json()
            if run["status"] in ("completed", "failed"):
                break
            time.sleep(15)
        scores = ", ".join(
            f"{s['evaluatorId'].replace('Builtin.', '')}={s['score']:.2f}"
            for s in run.get("scores", [])
        )
        step(
            "mini-eval",
            run["status"] == "completed" and bool(run.get("scores")),
            f"run {run['id']} · {run['status']} · {scores or run.get('error', '')}",
        )

        # 7 — cleanup
        if args.keep:
            step("cleanup", True, "--keep set: resources left in place")
        else:
            client.delete(f"/api/eval/datasets/{dataset['id']}")
            eval_record = client.get(f"/api/agents/{eval_agent_id}").json().get(
                "registry_record_id"
            )
            for rid in (record_id, eval_record):
                if rid:
                    client.post(
                        f"/api/registry/records/{rid}/action", json={"action": "disable"}
                    )
            client.delete(f"/api/agents/{eval_agent_id}").raise_for_status()
            client.delete(f"/api/agents/{agent_id}").raise_for_status()
            final = client.get(f"/api/agents/{agent_id}").json()["status"]
            step(
                "cleanup",
                final == "deleted",
                f"agents {final} · dataset+record retired",
            )
    except Exception:
        if not args.keep:  # never leave demo agents behind on failures
            for aid in (agent_id, locals().get("eval_agent_id")):
                if aid:
                    client.delete(f"/api/agents/{aid}")
        raise

    summary()
    print("\nE2E GOLDEN PATH: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
