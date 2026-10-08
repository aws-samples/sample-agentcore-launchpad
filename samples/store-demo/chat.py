"""Chat with a deployed agent through the console API; prints tool calls + the reply.

  python3 chat.py <agent_id> <case_id> "<prompt>" [session_id] [base_url]
"""
import json
import os
import sys
import urllib.request

agent, case, prompt = sys.argv[1], sys.argv[2], sys.argv[3]
session = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] != "-" else None
base = sys.argv[5] if len(sys.argv) > 5 else "http://localhost:8000"
body = {"prompt": prompt}
if session:
    body["session_id"] = session
req = urllib.request.Request(f"{base}/api/chat/{agent}", data=json.dumps(body).encode(),
                             method="POST", headers={**({"Cookie": os.environ["LP_COOKIE"]} if os.environ.get("LP_COOKIE") else {}),
                             "Content-Type": "application/json",
                                                     "Accept": "text/event-stream"})
text, tools, event, meta = [], [], None, {}
with urllib.request.urlopen(req, timeout=900) as resp:
    for raw in resp:
        line = raw.decode("utf-8").rstrip("\n")
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: ") and event:
            data = json.loads(line[6:])
            if event == "delta":
                text.append(data.get("text", "") if isinstance(data, dict) else str(data))
            elif event == "tool":
                name = data.get("name") or data.get("tool") or "?"
                tools.append(name.split("___")[-1] + (
                    "(" + json.dumps(data.get("input"), ensure_ascii=False)[:120] + ")"
                    if data.get("input") else ""))
            elif event == "meta":
                meta = data
            elif event in ("error", "policy_denied"):
                tools.append(f"!{event}:{json.dumps(data, ensure_ascii=False)[:200]}")
print(f"===== {case}  session={meta.get('session_id')}")
print("tools:", " → ".join(tools) or "(none)")
print("".join(text).strip())
