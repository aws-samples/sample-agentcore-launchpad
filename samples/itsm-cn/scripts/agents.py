import argparse
import json
import time

import lab
from lab import LEARNERS, ROOT, STAFF, api, evidence, load, session
from lab import save as save_manifest
from student_kit_lib import check_model, staged_agent_spec, sync_model_id

p = argparse.ArgumentParser()
p.add_argument("user", choices=LEARNERS + STAFF)
p.add_argument("stage", type=int, choices=[1, 2, 3])
p.add_argument("--model", metavar="MODEL_ID",
               help="deploy with this model for THIS run only (the lab config and the manifest"
                    " keep their model_id; default: the lab config's model_id)")
a = p.parse_args()
lab.sync_assets()  # N17-obs558: prompts.json follows the package, not the first install


def save(state):
    import fcntl

    with (ROOT / "manifest.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        merged = load()
        merged["agents"][a.user] = state["agents"][a.user]
        save_manifest(merged)


m = load()
# N17-obs557: the lab config is the single source of truth for the model (``--model`` is a
# one-run override). An unknown / unconfigured / judge-only model is refused before anything
# is written; then the manifest copy (what the card prints) is brought in line.
model_id = a.model or str(lab.CONFIG["model_id"])
c = session(a.user)
check_model(lab, c, model_id)
if not a.model:
    sync_model_id(lab, m)
prompts = json.loads((ROOT / "lab-assets/prompts.json").read_text())
staff = a.user in STAFF
# The first staff account keeps the handbook's agent name; further staff get their own.
if a.user == STAFF[0]:
    name = "itdesk-console-cn"
elif staff:
    name = a.user + "-console-cn"
else:
    name = a.user + "-itdesk-cn"
# One spec builder with the kit / reset_student (student_kit_lib): a learner's Agent keeps
# long-term memory OFF at every stage — the manual turns it on in 4.2.1 (N17-obs550).
spec = staged_agent_spec(
    name, a.stage, model_id=model_id, prompts=prompts, staff=a.user if staff else None,
    kb=m.get("kb"), skills=m.get("skills"), gateway=m.get("gateway"),
)
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
