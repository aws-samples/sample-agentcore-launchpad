#!/usr/bin/env python3
"""E2E: chat once, then poll aws/spans until the session's trace appears.

Asserts the normalized span tree contains model + tool activity.

Run:  cd backend && uv run python scripts/e2e_traces.py
"""

import argparse
import json
import sys
import time

from _e2e_client import e2e_client

AGENT_NAME = "hr-assistant"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--base", default="http://localhost:8000")
    args = parser.parse_args()

    client = e2e_client(args.base, timeout=300)
    agents = client.get("/api/agents").json()["agents"]
    agent = next((a for a in agents if a["name"] == AGENT_NAME and a["status"] == "active"), None)
    if not agent:
        print("hr-assistant not active")
        return 1

    print("── chat turn (tool + model activity)…")
    session_id = None
    with client.stream(
        "POST",
        f"/api/chat/{agent['id']}",
        json={"prompt": "How many vacation days does EMP-4096 have left? Use the database."},
    ) as res:
        event = None
        for line in res.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:") and event == "meta":
                session_id = json.loads(line[5:])["session_id"]
    print(f"  session: {session_id}")

    print("── polling aws/spans for the session trace…")
    deadline = time.time() + 300
    trace = {"span_count": 0}
    while time.time() < deadline:
        trace = client.get(f"/api/traces/{session_id}").json()
        if trace["span_count"] >= 3:
            break
        # CloudWatch trace polling is intentionally paced and attempt-bounded.
        time.sleep(15)  # nosemgrep: arbitrary-sleep

    print(f"  spans found: {trace['span_count']} in {trace['log_group']}")
    categories = {}
    for span in trace["spans"][:20]:
        categories.setdefault(span["category"], 0)
        categories[span["category"]] += 1
        print(f"    [{span['category']:<7}] {span['name'][:60]:<62} "
              f"+{span['start_ms']}ms · {span['duration_ms']}ms")
    print(f"  categories: {categories}")

    assert trace["span_count"] >= 3, "expected spans in aws/spans for the session"
    assert "model" in categories or "runtime" in categories, "expected model/runtime spans"
    assert "tool" in categories or any(
        "hr" in s["name"].lower() for s in trace["spans"]
    ), "expected gateway tool span"
    print("E2E TRACES: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
