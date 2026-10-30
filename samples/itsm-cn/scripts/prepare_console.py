import secrets
import time

from lab import BASE, CONFIG, LEARNERS, PREFIX, ROOT, SSM, STAFF, api, evidence, load, save, session

c = session()
m = load()
status = api(c, "GET", "/api/auth/status")
evidence("auth-status.json", status)
print("Auth status", status, flush=True)
existing = api(c, "GET", "/api/users?status=all&limit=200")["items"]
by_name = {u["username"]: u for u in existing}
for username in LEARNERS + STAFF:
    if username in m["users"]:
        continue
    if username in by_name:
        raise RuntimeError(f"Unowned existing username: {username}; do not overwrite")
    key = PREFIX + "users/" + username
    try:
        password = SSM.get_parameter(Name=key, WithDecryption=True)["Parameter"]["Value"]
    except SSM.exceptions.ParameterNotFound:
        password = "It9!" + secrets.token_urlsafe(24)
        SSM.put_parameter(
            Name=key,
            Type="SecureString",
            Value=password,
            Overwrite=False,
            Description="ITSM CN workshop learner credential; scenario-owned",
        )
    r = c.post(
        BASE + "/api/auth/register",
        json={
            "username": username,
            "email": username + "@itsm-lab.example.org",
            "password": password,
        },
        timeout=60,
    )
    if r.status_code == 429:
        print("Registration rate limit; retry in 60 seconds", flush=True)
        time.sleep(60)
        r = c.post(
            BASE + "/api/auth/register",
            json={
                "username": username,
                "email": username + "@itsm-lab.example.org",
                "password": password,
            },
            timeout=60,
        )
    if not r.ok:
        # Validation errors may echo the submitted password; never print the body.
        raise RuntimeError(f"Register {username}: HTTP {r.status_code}")
    users = api(c, "GET", "/api/users?status=all&limit=200")["items"]
    user = next(u for u in users if u["username"] == username)
    permissions = {
        "agents.deploy": True,
        "agents.delete": True,
        "agents.import": False,
        "agents.convert": False,
        "eval.run": True,
        "identity.manage": False,
        "identity.grant": False,
        "memory.manage": True,
        "policy.manage": username in STAFF,
    }
    api(
        c,
        "PATCH",
        "/api/users/" + user["id"],
        {
            "status": "active",
            "role": "member",
            "workspace_ids": [CONFIG["workspace"]],
            "permissions": permissions,
        },
    )
    m["users"][username] = {
        "id": user["id"],
        "role": "member",
        "credential_parameter": key,
        "ownership": "created",
    }
    save(m)
    print("Account ready", username, flush=True)
if not m["kb"]:
    result = api(
        c,
        "POST",
        "/api/knowledge-bases",
        {
            "name": "anypharma-it-helpdesk-cn",
            "description": "中国区IT服务台实验专用合成资料",
            "source": {"mode": "upload"},
        },
    )
    m["kb"] = result
    save(m)
    print("KB created", result.get("kb_id"), flush=True)
kb_id = m["kb"]["kb_id"]
for _ in range(60):
    kb = api(c, "GET", "/api/knowledge-bases/" + kb_id)
    if kb.get("status") == "ACTIVE" and kb.get("data_sources"):
        break
    time.sleep(3)
else:
    raise RuntimeError("KB not ready")
m["kb"] = kb
save(m)
if not m.get("knowledge_uploaded"):
    files = [(p.name, p.read_bytes()) for p in (ROOT / "lab-assets/knowledge").glob("*.md")]
    r = c.post(
        BASE + "/api/knowledge-bases/" + kb_id + "/files",
        files=[("files", (n, b, "text/markdown")) for n, b in files],
        timeout=180,
    )
    r.raise_for_status()
    evidence("kb-upload.json", r.json())
    m["knowledge_uploaded"] = True
    save(m)
    print("Knowledge uploaded", flush=True)
for z in sorted((ROOT / "lab-assets").glob("*.zip")):
    name = z.stem
    if name in m["skills"]:
        continue
    r = c.post(
        BASE + "/api/registry/skills/inspect",
        files={"file": (z.name, z.read_bytes(), "application/zip")},
        timeout=180,
    )
    r.raise_for_status()
    inspection = r.json()
    if inspection["skills"][0].get("errors"):
        raise RuntimeError(str(inspection))
    imported = api(
        c,
        "POST",
        "/api/registry/skills/import",
        {"staging_id": inspection["staging_id"], "selections": [{"index": 0}]},
    )
    item = imported["records"][0]
    if not item.get("ok"):
        raise RuntimeError(str(imported))
    rid = item["record"]["record_id"]
    api(c, "POST", f"/api/registry/records/{rid}/action", {"action": "submit"})
    api(c, "POST", f"/api/registry/records/{rid}/action", {"action": "approve"})
    m["skills"][name] = {"record_id": rid}
    save(m)
    print("Skill approved", name, flush=True)
attachables = api(c, "GET", "/api/registry/attachables")
for row in attachables.get("skills", []):
    if row["name"] in m["skills"]:
        m["skills"][row["name"]].update(row)
save(m)
for name in ("itsm-exam-8-cn", "itsm-held-out-cn"):
    if name not in m["datasets"]:
        import json

        data = json.loads((ROOT / "lab-assets" / (name + ".json")).read_text())
        m["datasets"][name] = api(c, "POST", "/api/eval/datasets", data)
        save(m)
        print("Dataset created", name, flush=True)
evidence(
    "console-prepared.json",
    {
        "users": list(m["users"]),
        "kb": m["kb"],
        "skills": m["skills"],
        "datasets": list(m["datasets"]),
    },
)
print("Console material preparation complete.", flush=True)
