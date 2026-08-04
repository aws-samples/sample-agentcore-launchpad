#!/usr/bin/env python3
"""E2E: registry records for all three types + approval workflow + search.

Flow: sync-defaults (MCP ×2 + AGENT_SKILLS) → deploy harness agent (A2A
auto-registered by pipeline) → status transitions on the skill record
(DRAFT→PENDING_APPROVAL→APPROVED) → SearchRegistryRecords → disable one.

Run:  cd backend && uv run python scripts/e2e_registry.py [--keep]
"""

import argparse
import json
import sys
import time

import httpx

AGENT_NAME = "e2e-registry-agent"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    client = httpx.Client(base_url=args.base, timeout=300)

    print("── sync-defaults (MCP + AGENT_SKILLS records)…")
    res = client.post("/api/registry/sync-defaults")
    res.raise_for_status()
    for row in res.json()["results"]:
        print(f"  {row['type']:<12} {row['name']:<24} {row['record_id']} "
              f"({'created' if row['created'] else 'refreshed'})")

    print("── deploying harness agent (A2A auto-register)…")
    for agent in client.get("/api/agents").json()["agents"]:
        if agent["name"] == AGENT_NAME:
            client.delete(f"/api/agents/{agent['id']}")
            # Registry/runtime deletion is eventually consistent in real AWS.
            time.sleep(60)  # nosemgrep: arbitrary-sleep
    res = client.post(
        "/api/agents",
        json={"name": AGENT_NAME, "method": "harness",
              "system_prompt": "You are a registry e2e agent."},
    )
    res.raise_for_status()
    agent_id, job_id = res.json()["agent"]["id"], res.json()["job_id"]
    status = "deploying"
    for _ in range(60):
        agent = client.get(f"/api/agents/{agent_id}").json()
        status = agent["status"]
        if status in ("active", "failed"):
            break
        # Real-AWS deployment polling is intentionally paced and attempt-bounded.
        time.sleep(5)  # nosemgrep: arbitrary-sleep
    if status != "active":
        print(f"deploy failed: {client.get(f'/api/jobs/{job_id}').json().get('error')}")
        return 1
    job = client.get(f"/api/jobs/{job_id}").json()
    register_events = [e for e in job["events"] if e["stage"] == "register"]
    for ev in register_events:
        print(f"  register: {ev['msg']}")
    a2a_record_id = agent["registry_record_id"]
    print(f"  ledger registry_record_id: {a2a_record_id}")

    print("── three records (GetRegistryRecord evidence):")
    records = client.get("/api/registry/records").json()["records"]
    by_type = {}
    for rec in records:
        by_type.setdefault(rec["type"], rec)
    for typ in ("A2A", "MCP", "AGENT_SKILLS"):
        rec = by_type.get(typ)
        assert rec, f"missing {typ} record"
        detail = client.get(f"/api/registry/records/{rec['record_id']}").json()
        print(f"  {typ:<12} {detail['name']:<24} {detail['record_id']} status={detail['status']}")

    print("── approval workflow on the skill record:")
    skill = next(r for r in records if r["type"] == "AGENT_SKILLS")
    rid = skill["record_id"]
    print(f"  initial: {skill['status']}")
    if skill["status"] == "DRAFT":
        step = client.post(f"/api/registry/records/{rid}/action", json={"action": "submit"}).json()
        print(f"  after submit: {step['status']}")
    step = client.post(f"/api/registry/records/{rid}/action", json={"action": "approve"}).json()
    print(f"  after approve (published): {step['status']}")
    assert step["status"] == "APPROVED"

    print("── SearchRegistryRecords('expense'):")
    found = client.get("/api/registry/records/search", params={"q": "expense"}).json()["records"]
    print(json.dumps([{k: r[k] for k in ("name", "type", "status")} for r in found], indent=1))
    assert any(r["name"] == "expense-report-writer" for r in found), "search must find the skill"

    print("── disable office-facts record:")
    facts = next(r for r in records if r["name"] == "office-facts")
    step = client.post(
        f"/api/registry/records/{facts['record_id']}/action", json={"action": "disable"}
    ).json()
    print(f"  after disable: {step['status']}")
    assert step["status"] == "DEPRECATED"

    if not args.keep:
        print("── cleaning e2e agent…")
        client.delete(f"/api/agents/{agent_id}")
    print("E2E REGISTRY: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
