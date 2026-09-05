"""SE-012 UI probe: drive the built console against a fully stubbed /api so no AWS
is touched. Asserts: EDIT opens the inline form pre-filled from GET detail; SAVE is
disabled with a reason until a field changes and is valid; the confirm dialog names
the referencing agents + the expiry impact; PUT carries only the changed field; the
row refreshes from the response without a list reload."""
import json, sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:4199"
DEFAULT = "launchpad_memory-ABC123"
TEAM = "team_notes-XYZ789"
puts = []
list_calls = []
unhandled = []

def row(id_, name, default, agents, status="ACTIVE"):
    return {"id": id_, "arn": f"arn:mem:{id_}", "name": name, "status": status,
            "created_at": "2026-08-30T12:00:00+00:00", "updated_at": None,
            "is_default": default, "agents": agents}

def detail(id_, name, desc, days, status="ACTIVE", updated=None):
    return {"id": id_, "arn": f"arn:mem:{id_}", "name": name, "description": desc,
            "status": status, "failure_reason": None, "event_expiry_days": days,
            "execution_role_arn": None, "created_at": "2026-08-30T12:00:00+00:00",
            "updated_at": updated, "is_default": id_ == DEFAULT, "strategies": [],
            "namespace_keys": []}

def handle(route, request):
    url = request.url.split("?")[0]
    path = url[url.index("/api/"):]
    m = request.method
    body = None
    if path == "/api/auth/status":
        body = {"auth_required": False, "authenticated": True, "registration_enabled": False,
                "registration_requires_approval": False, "username": "admin", "role": "admin",
                "email": None, "account_expires_at": None, "permissions": []}
    elif path == "/api/workspaces" and m == "GET":
        body = {"workspaces": [{"id": "default", "name": "default", "account_id": "123456789012",
                "region": "us-west-2", "cross_account": False, "bootstrap_status": "ready",
                "is_default": True, "created_at": None, "updated_at": None}], "all_workspaces": True}
    elif path == "/api/memory/overview":
        body = {"configured": True, "memory": detail(DEFAULT, "launchpad_memory", None, 30),
                "strategies": [], "actor_count": 0, "actor_count_truncated": False,
                "other_memories": []}
    elif path == "/api/memory/resources" and m == "GET":
        list_calls.append(1)
        body = {"items": [row(DEFAULT, "launchpad_memory", True, []),
                          row(TEAM, "team_notes", False,
                              [{"id": "a1", "name": "hr-assistant"}, {"id": "a2", "name": "faq-bot"}])],
                "default_id": DEFAULT}
    elif path == f"/api/memory/resources/{TEAM}" and m == "GET":
        body = detail(TEAM, "team_notes", "per-team memory", 30)
    elif path == f"/api/memory/resources/{TEAM}" and m == "PUT":
        puts.append(json.loads(request.post_data))
        body = detail(TEAM, "team_notes", "per-team memory", 14, status="UPDATING",
                      updated="2026-09-05T09:30:00+00:00")
    else:
        unhandled.append((m, path))
        body = {}
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        raise SystemExit(1)

with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    page = b.new_page(viewport={"width": 1280, "height": 900})
    page.route("**/api/**", handle)
    page.goto(f"{BASE}/memory?view=resources")
    edits = page.get_by_test_id("memory-resource-edit")
    edits.nth(1).wait_for()
    check(edits.count() == 2, "EDIT action on every row (incl. the platform default)")
    edits.nth(1).click()
    er = page.get_by_test_id("memory-resource-edit-row")
    er.wait_for()
    desc = page.locator("#mem-res-edit-desc")
    exp = page.locator("#mem-res-edit-expiry")
    page.wait_for_function("document.querySelector('#mem-res-edit-desc').value === 'per-team memory'")
    check(exp.input_value() == "30", "inline form pre-filled from GET detail (desc + expiry 30)")
    save = page.get_by_test_id("memory-resource-save")
    hint = er.get_by_test_id("btn-hint")
    check(save.is_disabled() and hint.inner_text() == "Nothing changed yet.",
          f"SAVE disabled with reason before any change: {hint.inner_text()!r}")
    exp.fill("6")
    check(save.is_disabled() and "between 7 and 365" in hint.inner_text(),
          f"invalid expiry 6 keeps SAVE disabled with reason: {hint.inner_text()!r}")
    exp.fill("14")
    page.wait_for_function("!document.querySelector('[data-testid=memory-resource-save]').disabled")
    check(hint.count() == 0, "valid change enables SAVE and drops the hint")
    save.click()
    dlg = page.get_by_role("alertdialog")
    dlg.wait_for()
    text = dlg.inner_text()
    check("hr-assistant, faq-bot" in text and "(2)" in text, "confirm dialog lists the referencing agents")
    check("14 days" in text, "confirm dialog states the expiry impact on every agent")
    print("DIALOG:", text.replace("\n", " | "))
    page.screenshot(path=sys.argv[1] + "/confirm.png")
    dlg.locator(".confirm-actions button", has_text="SAVE").click()
    page.wait_for_function("!document.querySelector('[data-testid=memory-resource-edit-row]')")
    check(puts == [{"event_expiry_days": 14}], f"PUT body carries only the changed field: {puts}")
    rows = page.locator("tbody tr")
    check("UPDATING" in rows.nth(1).inner_text(), "row refreshed from the PUT response (status UPDATING)")
    check(len(list_calls) == 1, f"no list reload after save (list GETs = {len(list_calls)})")
    check(page.locator("text=Memory team_notes-XYZ789 updated.").count() >= 1, "success toast shown")
    page.screenshot(path=sys.argv[1] + "/after-save.png")
    # zh-CN copy renders too
    page.evaluate("localStorage.setItem('i18nextLng','zh-CN')")
    page.goto(f"{BASE}/memory?view=resources")
    page.get_by_test_id("memory-resource-edit").nth(0).wait_for()
    check(page.get_by_test_id("memory-resource-edit").nth(0).inner_text().strip() == "编辑",
          "zh-CN EDIT label")
    print("unhandled stub paths:", unhandled)
    b.close()
print("ALL PASS")
