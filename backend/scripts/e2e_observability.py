#!/usr/bin/env python3
"""E2E: observability endpoints against live aws/spans + AgentCore Memory.

Chats with hr-assistant once (fresh spans), then exercises all five
/api/observability endpoints, prints live dashboard numbers, one real trace
tree, session list + detail with the memory transcript, and proves the 60s
cache (2nd identical call <300ms).

Run:  cd backend && uv run python scripts/e2e_observability.py
"""

import json
import sys
import time

import httpx

AGENT_NAME = "hr-assistant"
RANGE = "24h"


def timed_get(client: httpx.Client, url: str) -> tuple[dict, float]:
    started = time.monotonic()
    res = client.get(url)
    elapsed_ms = (time.monotonic() - started) * 1000
    res.raise_for_status()
    return res.json(), elapsed_ms


def print_tree(node: dict, indent: int = 0) -> int:
    print(f"    {'  ' * indent}[{node['category']:<7}] {node['name'][:58]:<60} "
          f"+{node['start_offset_ms']}ms · {node['duration_ms']}ms · {node['width_pct']}%")
    depth = indent
    for child in node["children"]:
        depth = max(depth, print_tree(child, indent + 1))
    return depth


def main() -> int:
    client = httpx.Client(base_url="http://localhost:8000", timeout=300)

    print("── fresh spans: one chat turn with hr-assistant…")
    agents = client.get("/api/agents").json()["agents"]
    agent = next(
        (a for a in agents if a["name"] == AGENT_NAME and a["status"] == "active"), None
    )
    if not agent:
        print("hr-assistant not active")
        return 1
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
    print(f"  chat session: {session_id}")

    print("── waiting for spans to land in aws/spans (fresh trace for this session)…")
    deadline = time.time() + 300
    session_traces: list[dict] = []
    while time.time() < deadline:
        body, _ = timed_get(
            client, f"/api/observability/traces?range=1h&session={session_id}&force=true"
        )
        session_traces = body["traces"]
        if session_traces and session_traces[0]["llm_count"] >= 1:
            break
        # CloudWatch trace polling is intentionally paced and attempt-bounded.
        time.sleep(15)  # nosemgrep: arbitrary-sleep
    assert session_traces, "expected the fresh chat trace to appear in aws/spans"

    print(f"\n── GET /api/observability/dashboard?range={RANGE}")
    dashboard, ms1 = timed_get(client, f"/api/observability/dashboard?range={RANGE}")
    tiles = dashboard["tiles"]
    print(f"  TRACES   {tiles['traces']['total']} ({tiles['traces']['ok']} ok · "
          f"{tiles['traces']['error']} error)")
    print(f"  SESSIONS {tiles['sessions']['total']} · {tiles['sessions']['agents']} agents")
    print(f"  ERROR%   {tiles['error_rate'] * 100:.1f}%")
    print(f"  LATENCY  p50 {tiles['latency']['p50_ms']}ms · p95 {tiles['latency']['p95_ms']}ms")
    print(f"  TOKENS   {tiles['tokens']['total']} ({tiles['tokens']['input']} in · "
          f"{tiles['tokens']['output']} out) · ≈${tiles['tokens']['est_cost_usd']}")
    print(f"  series buckets: {len(dashboard['series'])}")
    for row in dashboard["tokens_by_model"]:
        print(f"  model {row['model']}: {row['total']} tok · ≈${row['est_cost_usd']}")
    for row in dashboard["top_tools"][:5]:
        print(f"  tool  {row['tool']}: {row['calls']} calls · {row['success_rate']}% ok")
    assert tiles["traces"]["total"] > 0, "expected live traces in dashboard"
    assert dashboard["tokens_by_model"], "expected token metrics by model"

    print(f"\n── GET /api/observability/traces?range={RANGE} (first 5 rows)")
    traces, _ = timed_get(client, f"/api/observability/traces?range={RANGE}")
    for row in traces["traces"][:5]:
        print(f"  {row['time']} {row['root_operation'][:24]:<26} {row['agent'][:24]:<26} "
              f"{(row['session_id'] or '—')[:12]:<14} {row['duration_ms']}ms · "
              f"{row['span_count']} spans · {row['llm_count']} llm · "
              f"{row['tokens']['total']} tok · ≈${row['est_cost_usd']} · {row['status']}")
    assert traces["count"] > 0, "expected non-empty traces list"

    trace_id = session_traces[0]["trace_id"]
    print(f"\n── GET /api/observability/traces/{trace_id} (waterfall tree)")
    detail, _ = timed_get(client, f"/api/observability/traces/{trace_id}?range={RANGE}")
    meta = detail["meta"]
    print(f"  agent={meta['agent']} session={str(meta['session_id'])[:16]}… "
          f"{meta['duration_ms']}ms · {meta['span_count']} spans · {meta['llm_count']} llm · "
          f"{meta['tokens']['total']} tok · ≈${meta['est_cost_usd']} · {meta['status']}")
    max_depth = 0
    for root in detail["tree"]:
        max_depth = max(max_depth, print_tree(root))
    assert max_depth >= 1, "expected a span tree with >=2 levels"
    assert detail["spans"][0]["attributes"] is not None

    print(f"\n── GET /api/observability/sessions?range={RANGE}")
    sessions, _ = timed_get(client, f"/api/observability/sessions?range={RANGE}")
    for row in sessions["sessions"][:5]:
        print(f"  {row['session_id'][:20]:<22} {row['agent'][:24]:<26} "
              f"{row['traces']} traces · {row['llm_calls']} llm · "
              f"{row['tokens']['total']} tok · ≈${row['est_cost_usd']} · "
              f"platform={row['platform']}")
    assert sessions["count"] > 0, "expected sessions"

    print(f"\n── GET /api/observability/sessions/{session_id} (detail + transcript)")
    sdetail, _ = timed_get(client, f"/api/observability/sessions/{session_id}?range={RANGE}")
    summary = sdetail["summary"]
    print(f"  summary: agent={summary['agent']} traces={summary['traces']} "
          f"llm={summary['llm_calls']} tok={summary['tokens']['total']} "
          f"≈${summary['est_cost_usd']}")
    transcript = sdetail["transcript"]
    print(f"  transcript available={transcript['available']} "
          f"turns={len(transcript.get('turns', []))} "
          f"long_term_records={transcript.get('long_term_records')}")
    for turn in transcript.get("turns", [])[:4]:
        print(f"    {turn['role']:<10} {turn['text'][:70]}")
    assert transcript["available"] is True, "platform session must have a transcript"
    assert transcript["turns"], "expected ordered turns from AgentCore Memory"

    print("\n── external/unknown session id → transcript degrades, no error")
    ext, _ = timed_get(
        client, f"/api/observability/sessions/{'f' * 63}0?range={RANGE}"
    )
    assert ext["transcript"] == {"available": False, "reason": "not_platform_session"}
    print(f"  transcript: {ext['transcript']}")

    print("\n── cache proof: identical dashboard call again")
    dashboard2, ms2 = timed_get(client, f"/api/observability/dashboard?range={RANGE}")
    print(f"  1st call {ms1:.0f}ms (hit={dashboard['cache']['hit']}) → "
          f"2nd call {ms2:.0f}ms (hit={dashboard2['cache']['hit']}, "
          f"age={dashboard2['cache']['age_seconds']}s)")
    assert dashboard2["cache"]["hit"] is True, "second identical call must be a cache hit"
    assert ms2 < 300, f"cache hit should respond <300ms, took {ms2:.0f}ms"

    print("\nE2E OBSERVABILITY: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
