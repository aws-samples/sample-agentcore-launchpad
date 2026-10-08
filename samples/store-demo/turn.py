"""Send one architect-assistant turn and print the reply + event summary.

  python3 turn.py <conversation_id> <prompt-file> [base_url]
"""
import json
import os
import pathlib
import sys
import urllib.request

cid, prompt_file = sys.argv[1], sys.argv[2]
base = sys.argv[3] if len(sys.argv) > 3 else "http://localhost:8000"
prompt = pathlib.Path(prompt_file).read_text(encoding="utf-8").strip()
req = urllib.request.Request(
    f"{base}/api/assistant/architect/conversations/{cid}/turns",
    data=json.dumps({"prompt": prompt}).encode(), method="POST",
    headers={**({"Cookie": os.environ["LP_COOKIE"]} if os.environ.get("LP_COOKIE") else {}),
                             "Content-Type": "application/json", "Accept": "text/event-stream"})
counts: dict[str, int] = {}
text: list[str] = []
event = None
with urllib.request.urlopen(req, timeout=1800) as resp:
    for raw in resp:
        line = raw.decode("utf-8").rstrip("\n")
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: ") and event:
            data = json.loads(line[6:])
            counts[event] = counts.get(event, 0) + 1
            if event in ("delta", "text", "token") and isinstance(data, dict):
                text.append(str(data.get("text") or data.get("delta") or ""))
            elif event not in ("delta", "text", "token", "tool", "tool_use", "tool_result"):
                print(f"[{event}]", json.dumps(data, ensure_ascii=False)[:600])
print("events", counts)
if text:
    out = "".join(text)
    pathlib.Path(prompt_file).with_suffix(".reply.md").write_text(out, encoding="utf-8")
    print(f"reply {len(out)} chars → {pathlib.Path(prompt_file).with_suffix('.reply.md')}")
