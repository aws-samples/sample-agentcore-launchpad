#!/usr/bin/env python3
"""E2E: chat with session continuity + short/long-term AgentCore Memory.

Flow (against the persistent `hr-assistant` harness agent):
  1. session A turn 1: identify as EMP-1024, ask vacation days → expects "9"
  2. session A turn 2: "what department am I in?" → continuity proves turn-1 context
  3. ListEvents(session A) shows conversation events (short-term)
  4. state a preference → poll for the extracted long-term record (async).
     NB: memory is agent-scoped — events and records live under the compound
     actor `<agent_id>__<actor>` (see memory.scoped_actor), so this flow reads
     via the /memory endpoint, which re-scopes internally, rather than hitting
     /preferences/<actor> directly.
  5. session B (NEW): ask about preferences → long-term influence evidence

Run:  cd backend && uv run python scripts/e2e_chat_memory.py [--actor river]
"""

import argparse
import json
import sys
import time

import httpx
from _e2e_client import e2e_client

AGENT_NAME = "hr-assistant"


def sse_collect(client: httpx.Client, agent_id: str, prompt: str, session_id, actor: str):
    text, sid, chunks = "", session_id, 0
    with client.stream(
        "POST",
        f"/api/chat/{agent_id}",
        json={"prompt": prompt, "session_id": session_id, "actor_id": actor},
    ) as res:
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
                    chunks += 1
                elif event == "error":
                    raise RuntimeError(data["message"])
    return text, sid, chunks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--actor", default="river")
    parser.add_argument("--extract-timeout", type=int, default=420)
    args = parser.parse_args()
    client = e2e_client(args.base, timeout=300)

    agents = client.get("/api/agents").json()["agents"]
    agent = next((a for a in agents if a["name"] == AGENT_NAME and a["status"] == "active"), None)
    if not agent:
        print(f"agent '{AGENT_NAME}' not active — deploy it first")
        return 1
    agent_id = agent["id"]
    print(f"agent: {AGENT_NAME} · {agent['arn'][-40:]}")

    print("\n── session A · turn 1")
    q1 = "Hi! My employee id is EMP-1024. How many vacation days do I have left this year?"
    a1, session_a, chunks1 = sse_collect(client, agent_id, q1, None, args.actor)
    print(f"  Q: {q1}\n  A: {a1[:300]}  ({chunks1} chunks)")
    assert "9" in a1, f"expected 9 vacation days in answer: {a1!r}"

    print("\n── session A · turn 2 (continuity)")
    q2 = "And which department am I in? Answer from what you already know about me."
    a2, _, _ = sse_collect(client, agent_id, q2, session_a, args.actor)
    print(f"  Q: {q2}\n  A: {a2[:300]}")
    assert "HR" in a2 or "Operations" in a2, f"turn-2 must use turn-1 context: {a2!r}"
    print("  ✓ turn 2 used turn-1 context (EMP-1024 → HR Operations)")

    print("\n── short-term: ListEvents(session A)")
    mem = client.get(
        f"/api/chat/{agent_id}/memory",
        params={"session_id": session_a, "actor_id": args.actor},
    ).json()
    print(f"  events in session: {mem['event_count']}")
    for ev in mem["events"][:3]:
        for p in ev["payload"][:2]:
            print(f"    [{p['role']}] {p['text'][:70]}")
    assert mem["event_count"] >= 1, "expected conversation events in short-term memory"

    print("\n── stating a long-term preference")
    q3 = (
        "Please remember this about me permanently: I always prefer answers formatted "
        "as exactly one short sentence, no bullet points."
    )
    a3, _, _ = sse_collect(client, agent_id, q3, session_a, args.actor)
    print(f"  A: {a3[:200]}")

    print("── waiting for async long-term extraction…")
    deadline = time.time() + args.extract_timeout
    records = []
    while time.time() < deadline:
        mem = client.get(
            f"/api/chat/{agent_id}/memory",
            params={"session_id": session_a, "actor_id": args.actor},
        ).json()
        records = [r for r in mem["records"] if "sentence" in r["text"].lower()
                   or "prefer" in r["text"].lower()]
        if records:
            break
        # Real-AWS memory extraction is eventually consistent and deadline-bounded.
        time.sleep(20)  # nosemgrep: arbitrary-sleep
    if not records:
        print(f"  no preference record after {args.extract_timeout}s "
              f"(records: {json.dumps(mem['records'])[:400]})")
        return 1
    print(f"  ✓ long-term record: {json.dumps(records[0], ensure_ascii=False)[:220]}")

    print("\n── session B (NEW) · long-term influence")
    q4 = "What do you know about my answer-format preferences?"
    a4, session_b, _ = sse_collect(client, agent_id, q4, None, args.actor)
    print(f"  Q: {q4}\n  A: {a4[:300]}")
    assert session_b != session_a
    assert "sentence" in a4.lower() or "concise" in a4.lower() or "short" in a4.lower(), (
        f"new session must reflect the stored preference: {a4!r}"
    )
    print("  ✓ new session reflected the long-term preference")
    print("\nE2E CHAT MEMORY: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
