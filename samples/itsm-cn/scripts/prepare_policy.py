import fcntl
import json

from lab import CONFIG, ROOT, STAFF, api, evidence, load, session
from lab import save as save_manifest

m = load()
c = session()
gid = m["gateway"]["id"]


def save():
    with (ROOT / "manifest.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        merged = load()
        for key in ("gateway", "engine", "policies"):
            if key in m:
                merged[key] = m[key]
        save_manifest(merged)


if not all("record_id" in t for t in m["gateway"]["targets"].values()):
    result = api(
        c,
        "POST",
        "/api/registry/import-gateway",
        {"gateway_id": gid, "target_ids": [t["id"] for t in m["gateway"]["targets"].values()]},
    )
    evidence("gateway-import.json", result)
    for row in result["results"]:
        rid = row["record_id"]
        if row.get("status") != "APPROVED":
            api(c, "POST", f"/api/registry/records/{rid}/action", {"action": "approve"})
        m["gateway"]["targets"][row["name"]]["record_id"] = rid
    save()
    print("Gateway targets registered and approved.", flush=True)
if not m.get("engine"):
    m["engine"] = api(
        c,
        "POST",
        "/api/policy-engines",
        {
            "name": "itsm_cn_lab_engine",
            "description": "中国区IT服务台实验专用：本人访问与服务台知识发布",
        },
    )
    save()
eid = m["engine"]["id"]
# Bind LOG_ONLY first so the engine compiles against THIS Gateway's concrete schemas.
if not m.get("policies"):
    api(
        c,
        "PUT",
        f"/api/governance/gateways/{gid}/policy-engine",
        {"engine_id": eid, "mode": "LOG_ONLY"},
    )

targets = json.loads((ROOT / "lab-assets/tools.json").read_text())
actions = ",\n    ".join(
    'AgentCore::Action::"servicenow___' + t["name"] + '"' for t in targets["servicenow"]
)
# Cedar disjunction over the configured service-desk accounts (one account = no `||`).
staff_clause = " || ".join("principal == AgentCore::User::" + json.dumps(u) for u in STAFF)
policy_texts = {
    "itsm_agents_call_tools": f'permit (principal in AgentCore::Workspace::{json.dumps(CONFIG["workspace"])}, action in AgentCore::Action::"CallTool", resource);',
    "itsm_users_call_tools": """permit (principal in AgentCore::Role::"member", action in AgentCore::Action::"CallTool", resource);
permit (principal in AgentCore::Role::"admin", action in AgentCore::Action::"CallTool", resource);
permit (principal is AgentCore::System, action in AgentCore::Action::"CallTool", resource);
permit (principal is AgentCore::ApiKey, action in AgentCore::Action::"CallTool", resource);""",
    "itsm_requester_is_self": f"""forbid (
  principal is AgentCore::User,
  action in [
    {actions}
  ],
  resource
) when {{
  context.input.requester != principal.username
}} unless {{
  {staff_clause}
}};""",
    "itsm_kb_publish_servicedesk_only": f"""forbid (
  principal,
  action in AgentCore::Action::"target:servicenow-knowledge",
  resource
) unless {{
  {staff_clause}
}};""",
    "itsm_resolve_servicedesk_only": f"""forbid (
  principal,
  action == AgentCore::Action::"servicenow___resolve_ticket",
  resource
) unless {{ {staff_clause} }};""",
}
m.setdefault("policies", {})
for name, text in policy_texts.items():
    (ROOT / "policies" / (name + ".cedar")).write_text(text + "\n")
    if name in m["policies"]:
        pid = m["policies"][name]["id"]
        current = api(c, "GET", f"/api/policy-engines/{eid}/policies/{pid}")
        active = next(
            (v for v in current.get("versions", []) if v["version"] == current.get("active_version")),
            None,
        )
        if (active or {}).get("statement", "").strip() != text.strip():
            # The generated text changed (e.g. the staff account list): new draft, then publish.
            validated = api(
                c, "POST", f"/api/policy-engines/{eid}/validate",
                {"statement": text, "policy_id": pid}, schema_read_attempts=13,
            )
            if not validated.get("ok"):
                evidence("policy-validation-failed.json", validated)
                raise RuntimeError(str(validated)[:1500])
            api(c, "PUT", f"/api/policy-engines/{eid}/policies/{pid}", {"statement": text})
            current = api(
                c, "POST", f"/api/policy-engines/{eid}/policies/{pid}/publish",
                {"reason": "Workshop: regenerated Cedar for the configured staff accounts"},
            )
            print("Policy updated", name, "active version", current.get("active_version"), flush=True)
        elif not current.get("active_version"):
            current = api(c, "POST", f"/api/policy-engines/{eid}/policies/{pid}/publish", {})
        current.pop("versions", None)
        m["policies"][name] = current
        save()
        continue
    validated = api(
        c, "POST", f"/api/policy-engines/{eid}/validate", {"statement": text},
        schema_read_attempts=13,
    )
    if not validated.get("ok"):
        evidence("policy-validation-failed.json", validated)
        raise RuntimeError(str(validated)[:1500])
    mode = "log_only" if name == "itsm_requester_is_self" else "active"
    policy = api(
        c,
        "POST",
        f"/api/policy-engines/{eid}/policies",
        {"name": name, "statement": text, "enforcement_mode": mode},
    )
    m["policies"][name] = policy
    save()
    api(c, "POST", f"/api/policy-engines/{eid}/policies/{policy['id']}/publish", {})
    print("Policy published", name, mode, flush=True)
api(
    c, "PUT", f"/api/governance/gateways/{gid}/policy-engine", {"engine_id": eid, "mode": "ENFORCE"}
)
evidence("policy-prepared.json", api(c, "GET", f"/api/policy-engines/{eid}/policies"))
print(
    "Lab engine attached. Requester rule LOG_ONLY for chapter-3 baseline; remaining rules ACTIVE.",
    flush=True,
)
