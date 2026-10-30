import argparse
import json
import time

from lab import LEARNERS, ROOT, STAFF, api, evidence, load, session
from lab import save as save_manifest

p = argparse.ArgumentParser()
p.add_argument("user", choices=LEARNERS + STAFF)
p.add_argument("stage", type=int, choices=[1, 2, 3])
a = p.parse_args()


def save(state):
    import fcntl

    with (ROOT / "manifest.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        merged = load()
        merged["agents"][a.user] = state["agents"][a.user]
        save_manifest(merged)


m = load()
c = session(a.user)
prompts = json.loads((ROOT / "lab-assets/prompts.json").read_text())
staff = a.user in STAFF
# The first staff account keeps the handbook's agent name; further staff get their own.
if a.user == STAFF[0]:
    name = "itdesk-console-cn"
elif staff:
    name = a.user + "-console-cn"
else:
    name = a.user + "-itdesk-cn"
prompt = (
    prompts["staff"].replace("{{staff_account}}", a.user)
    if staff
    else "\n\n".join(prompts["step" + str(s)] for s in range(1, a.stage + 1))
)
spec = {
    "name": name,
    "method": "harness",
    "model_id": m["model_id"],
    "model_source": "bedrock",
    "system_prompt": prompt,
    "native_tools": [],
    "memory": {"short_term": True, "long_term": True},
    "tools": [],
    "skills": [],
    "knowledge_bases": [],
}
if a.stage >= 2:
    spec["knowledge_bases"] = [{"kb_id": m["kb"]["kb_id"], "name": m["kb"]["name"]}]
    spec["skills"] = [
        r["path"] for n, r in m["skills"].items() if staff or n != "kb-article-from-ticket"
    ]
if a.stage >= 3:
    names = ["servicenow", "servicenow-knowledge"] if staff else ["servicenow"]
    spec["tools"] = [
        {
            "type": "gateway",
            "name": n,
            "config": {
                "gateway_id": m["gateway"]["id"],
                "record_id": m["gateway"]["targets"][n]["record_id"],
            },
        }
        for n in names
    ]
row = m["agents"].get(a.user)
if row and row.get("stage") == a.stage and row.get("ready"):
    print("Agent already ready", row["id"])
    raise SystemExit()
if row:
    response = api(c, "POST", f"/api/agents/{row['id']}/redeploy", spec)
else:
    response = api(c, "POST", "/api/agents", spec)
agent = response.get("agent", response)
aid = agent.get("id") or row["id"]
m["agents"][a.user] = {
    "id": aid,
    "name": name,
    "stage": a.stage,
    "ready": False,
    "spec": spec,
    "job_id": response.get("job_id"),
}
save(m)
evidence(f"agent-{a.user}-stage{a.stage}-deploy.json", response)
print("Deployment submitted", a.user, aid, flush=True)
start = time.monotonic()
for _ in range(160):
    detail = api(c, "GET", "/api/agents/" + aid)
    item = detail.get("agent", detail)
    if item.get("status") in ("active", "running"):
        m = load()
        m["agents"][a.user].update(
            ready=True, seconds=round(time.monotonic() - start), detail=detail
        )
        save(m)
        evidence(f"agent-{a.user}-stage{a.stage}-ready.json", detail)
        print("Agent ready", a.user, m["agents"][a.user]["seconds"], flush=True)
        break
    if item.get("status") in ("failed", "error"):
        evidence(f"agent-{a.user}-stage{a.stage}-failed.json", detail)
        raise RuntimeError(str(item)[:500])
    time.sleep(5)
else:
    raise RuntimeError("Deployment did not finish")
