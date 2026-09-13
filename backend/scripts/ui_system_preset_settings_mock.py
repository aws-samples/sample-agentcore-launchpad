#!/usr/bin/env python3
"""Mocked browser scenario for the System presets SETTINGS dialog (SE-040).

Runs against a *frontend-only* dev server: every ``/api/**`` request is intercepted
in the browser and answered from an in-memory fixture, so no backend, ledger or AWS
is touched (unlike the ``e2e_*_browser.py`` scripts). Exercises, for the architect
preset:

* administrator: CONFIGURE opens the editor prefilled with the stored settings;
  cancel posts nothing; a client-side validation problem disables SAVE; a save
  posts exactly the changed members (+ ``clear`` for a dropped reasoning effort);
  the 202 outcome swaps the row to DEPLOYING and the poll carries it to ACTIVE with
  the new values; a 409 (deploy in progress) and a 422 (invalid options) render
  inline and leave the dialog open;
* member: VIEW SETTINGS shows the same fields read-only, no SAVE, and no POST;
* workspace switch: an open editor closes and a late response never lands.

    uv run python scripts/ui_system_preset_settings_mock.py --base-url http://127.0.0.1:5199
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Route, sync_playwright

KEY = "aws-agent-solution-architect"
WS_A = {"id": "ws000000000000000000000000000a", "name": "default (us-west-2)",
        "account_id": "111122223333", "region": "us-west-2", "cross_account": False,
        "bootstrap_status": "ready", "is_default": True,
        "created_at": "2026-09-01T00:00:00Z", "updated_at": "2026-09-01T00:00:00Z"}
WS_B = {**WS_A, "id": "ws000000000000000000000000000b", "name": "lab (us-east-2)",
        "region": "us-east-2", "is_default": False}
PROMPT = "# AWS agent solution architect\n\nYou are a senior AWS AI-agent solution architect."
DEFAULTS = {
    "model_id": "us.openai.gpt-5.6-sol", "model_source": "bedrock", "max_tokens": 65536,
    "reasoning_effort": "high", "system_prompt": PROMPT, "max_iterations": 30,
    "timeout_seconds": 900, "knowledge_bases": [],
}


def auth(role: str) -> dict:
    return {
        "auth_required": True, "authenticated": True, "registration_enabled": False,
        "registration_requires_approval": False,
        "username": "operator" if role == "admin" else "member-user", "role": role,
        "email": None, "account_expires_at": None,
        "permissions": ["agents.deploy", "agents.import", "agents.delete", "agents.convert",
                        "evaluation.run"],
    }


class Fixture:
    """One preset row per workspace + the install route's state machine."""

    def __init__(self, role: str) -> None:
        self.role = role
        self.posts: list[dict] = []
        self.next_post: str = "accept"  # accept | conflict | invalid | slow
        self.polls = 0
        self.rows = {WS_A["id"]: self._row(dict(DEFAULTS)), WS_B["id"]: self._row(None)}

    def _row(self, settings: dict | None) -> dict:
        installed = settings is not None
        admin = self.role == "admin"
        return {
            "key": KEY, "name": KEY, "label": "AWS Agent Solution Architect",
            "description": "Platform-managed advisory Harness.", "method": "harness",
            "skill_version": "1.0.0", "installed_skill_version": "1.0.0" if installed else None,
            "update_available": False, "status": "active" if installed else "not_installed",
            "requirements": [], "name_collision": None,
            "agent_id": "a" * 32 if installed else None,
            "agent_status": "active" if installed else None, "error": None,
            "job_id": "j" * 32 if installed else None,
            "deployment_id": "d" * 32 if installed else None,
            "deployment_status": "succeeded" if installed else None,
            "model_id": settings["model_id"] if installed else None,
            "model_source": settings["model_source"] if installed else None,
            "knowledge_bases": settings["knowledge_bases"] if installed else [],
            "allowed_tools": ["file_*", "@aws_knowledge"], "memory": "disabled",
            "settings": settings or {}, "defaults": dict(DEFAULTS),
            "editable_fields": list(DEFAULTS), "operation": None,
            "can_install": admin and not installed, "can_repair": admin and installed,
            "can_configure": admin and installed, "can_uninstall": admin and installed,
            "updated_at": "2026-09-13T00:00:00Z",
        }

    def status(self, ws: str) -> dict:
        row = self.rows[ws]
        # a deploying row settles on the second poll after the save
        if row["status"] == "deploying":
            self.polls += 1
            if self.polls >= 2:
                row["status"] = "active"
                row["agent_status"] = "active"
                row["can_configure"] = row["can_repair"] = self.role == "admin"
        return {"workspace_id": ws, "presets": [copy.deepcopy(row)]}

    def install(self, ws: str, body: dict) -> tuple[int, dict]:
        self.posts.append({"workspace": ws, "body": body})
        if self.role != "admin":
            return 403, {"code": "auth.forbidden", "message": "administrator only",
                         "detail": None}
        row = self.rows[ws]
        mode, self.next_post = self.next_post, "accept"
        if mode == "conflict":
            return 409, {"code": "system_agent.deploy_in_progress",
                         "message": "being deployed", "detail": {"job_id": "j" * 32}}
        if mode == "invalid":
            return 422, {"code": "system_agent.invalid_options",
                         "message": "the requested preset settings are not valid",
                         "detail": {"errors": [{"loc": [], "msg": "Value error, "
                                                "reasoning_effort applies to OpenAI "
                                                "GPT-5.x models only"}]}}
        settings = dict(row["settings"])
        for name in body.get("reset", []):
            settings[name] = copy.deepcopy(DEFAULTS[name])
        for name in body.get("clear", []):
            settings[name] = None
        for name in DEFAULTS:
            if body.get(name) is not None:
                settings[name] = body[name]
        row["settings"] = settings
        row["model_id"] = settings["model_id"]
        row["knowledge_bases"] = settings["knowledge_bases"]
        row["status"] = row["agent_status"] = "deploying"
        row["can_configure"] = row["can_repair"] = False
        self.polls = 0
        return 202, {"agent": {"id": row["agent_id"]}, "job_id": "k" * 32,
                     "deployment_id": "e" * 32, "created": False, "changed": True,
                     "preset": copy.deepcopy(row)}


def install_routes(page, fx: Fixture, unhandled: list[str]) -> None:
    def handle(route: Route) -> None:
        req = route.request
        url = req.url.split("?", 1)[0]
        path = "/" + url.split("/", 3)[3] if url.count("/") >= 3 else url
        ws = req.headers.get("x-workspace") or WS_A["id"]

        def reply(status: int, body) -> None:
            route.fulfill(status=status, content_type="application/json",
                          body=json.dumps(body))

        if path == "/api/auth/status":
            return reply(200, auth(fx.role))
        if path == "/api/workspaces" and req.method == "GET":
            return reply(200, {"workspaces": [WS_A, WS_B], "all_workspaces": fx.role == "admin"})
        if path == "/api/system-agents" and req.method == "GET":
            return reply(200, fx.status(ws))
        if path == f"/api/system-agents/{KEY}/install" and req.method == "POST":
            body = req.post_data_json or {}
            if fx.next_post == "slow":
                fx.next_post = "accept"
                time.sleep(1.5)  # a response that arrives after a workspace switch
            status, payload = fx.install(ws, body)
            return reply(status, payload)
        if path == "/api/knowledge-bases":
            return reply(200, {"items": [
                {"kb_id": "KB123ABC", "name": "aws-whitepapers", "description": "guides",
                 "status": "ACTIVE", "type": "MANAGED"},
                {"kb_id": "KB999ZZZ", "name": "creating", "status": "CREATING",
                 "type": "MANAGED"},
            ]})
        if path == "/api/agents":
            return reply(200, {"agents": [], "count": 0})
        unhandled.append(f"{req.method} {path}")
        return reply(200, {"items": [], "records": [], "skills": [], "count": 0})

    page.route("**/api/**", handle)


def shot(page, evidence: Path, name: str) -> None:
    page.screenshot(path=str(evidence / f"{name}.png"), full_page=False)


def wait_status(page, status: str, timeout_ms: int = 15000) -> None:
    page.locator(f'[data-testid="system-preset-{KEY}"][data-status="{status}"]').wait_for(
        timeout=timeout_ms
    )


def admin_scenario(browser, base: str, evidence: Path) -> dict:
    fx = Fixture("admin")
    unhandled: list[str] = []
    ctx = browser.new_context(viewport={"width": 1440, "height": 1100})
    ctx.add_init_script(f"window.localStorage.setItem('launchpad_workspace', '{WS_A['id']}')")
    page = ctx.new_page()
    install_routes(page, fx, unhandled)
    page.goto(f"{base}/create", wait_until="networkidle")
    wait_status(page, "active")
    row = page.get_by_test_id(f"system-preset-{KEY}")
    assert "max output/call: 65536 tok" in row.get_by_test_id("preset-inference").inner_text()
    assert "reasoning effort: high" in row.get_by_test_id("preset-inference").inner_text()
    button = page.get_by_test_id(f"settings-{KEY}")
    assert button.inner_text().strip() == "CONFIGURE", button.inner_text()
    shot(page, evidence, "01-admin-panel")

    # --- open: prefilled from the stored settings, nothing posted
    button.click()
    dialog = page.get_by_test_id("preset-settings-dialog")
    dialog.wait_for()
    assert page.get_by_test_id("preset-settings-model").input_value() == "us.openai.gpt-5.6-sol"
    assert page.get_by_test_id("preset-settings-max-tokens").input_value() == "65536"
    assert page.get_by_test_id("preset-settings-effort").input_value() == "high"
    assert page.get_by_test_id("preset-settings-max-iterations").input_value() == "30"
    assert page.get_by_test_id("preset-settings-timeout").input_value() == "900"
    assert page.get_by_test_id("preset-settings-prompt").input_value() == PROMPT
    assert page.get_by_test_id("preset-settings-save").is_disabled()  # no changes yet
    assert page.get_by_test_id("preset-settings-readonly").count() == 0
    shot(page, evidence, "02-admin-editor-prefilled")

    # --- cancel: no effect
    page.get_by_test_id("preset-settings-max-tokens").fill("4096")
    page.get_by_test_id("preset-settings-cancel").click()
    assert page.get_by_test_id("preset-settings-dialog").count() == 0
    assert fx.posts == []
    button.click()
    dialog.wait_for()
    assert page.get_by_test_id("preset-settings-max-tokens").input_value() == "65536"

    # --- client-side validation blocks SAVE
    page.get_by_test_id("preset-settings-max-tokens").fill("0")
    page.get_by_test_id("preset-settings-problems").wait_for()
    assert page.get_by_test_id("preset-settings-save").is_disabled()
    shot(page, evidence, "03-admin-validation")
    page.get_by_test_id("preset-settings-max-tokens").fill("65536")
    assert page.get_by_test_id("preset-settings-problems").count() == 0

    # --- switching to Claude disables the effort select; save posts model + clear only
    page.get_by_test_id("preset-settings-model").select_option("global.anthropic.claude-opus-5")
    assert page.get_by_test_id("preset-settings-effort").is_disabled()
    assert "Only OpenAI" in page.get_by_test_id("preset-settings-effort-hint").inner_text()
    page.get_by_test_id("preset-settings-max-iterations").fill("40")
    page.get_by_test_id("preset-settings-kb-KB123ABC").click()  # mount an ACTIVE KB
    assert page.get_by_test_id("preset-settings-kb-KB999ZZZ").count() == 0  # CREATING hidden

    # 409 first: the dialog stays open with the reason
    fx.next_post = "conflict"
    page.get_by_test_id("preset-settings-save").click()
    page.get_by_role("alertdialog").wait_for()
    page.get_by_role("alertdialog").get_by_role("button", name="SAVE & RE-PUBLISH").click()
    page.get_by_test_id("preset-settings-error").wait_for()
    assert "being deployed" in page.get_by_test_id("preset-settings-error").inner_text()
    assert page.get_by_test_id("preset-settings-dialog").count() == 1
    shot(page, evidence, "04-admin-409")
    # 422 next: server-side detail rows render
    fx.next_post = "invalid"
    page.get_by_test_id("preset-settings-save").click()
    page.get_by_role("alertdialog").get_by_role("button", name="SAVE & RE-PUBLISH").click()
    page.get_by_test_id("preset-settings-error").wait_for()
    assert "OpenAI GPT-5.x" in page.get_by_test_id("preset-settings-error").inner_text()
    shot(page, evidence, "05-admin-422")
    # accepted: partial body, row → deploying → active with the new values
    page.get_by_test_id("preset-settings-save").click()
    page.get_by_role("alertdialog").get_by_role("button", name="SAVE & RE-PUBLISH").click()
    page.get_by_test_id("preset-settings-dialog").wait_for(state="detached")
    wait_status(page, "deploying")
    shot(page, evidence, "06-admin-deploying")
    wait_status(page, "active", timeout_ms=20000)
    accepted = fx.posts[-1]["body"]
    assert accepted == {
        "model_id": "global.anthropic.claude-opus-5",
        "max_iterations": 40,
        "knowledge_bases": [{"kb_id": "KB123ABC", "name": "aws-whitepapers",
                             "description": "guides"}],
        "clear": ["reasoning_effort"],
    }, accepted
    assert all(p["workspace"] == WS_A["id"] for p in fx.posts)
    summary = row.get_by_test_id("preset-inference").inner_text()
    assert "reasoning effort: none" in summary and "40 iterations" in summary, summary
    assert "global.anthropic.claude-opus-5" in row.inner_text()
    shot(page, evidence, "07-admin-after-save")

    # --- reset to defaults from the editor: prompt/knobs come back, KBs kept
    page.get_by_test_id(f"settings-{KEY}").click()
    dialog.wait_for()
    assert page.get_by_test_id("differs-model_id").count() == 1
    page.get_by_test_id("preset-settings-defaults").click()
    assert page.get_by_test_id("preset-settings-model").input_value() == "us.openai.gpt-5.6-sol"
    assert page.get_by_test_id("preset-settings-effort").input_value() == "high"
    assert page.get_by_test_id("preset-settings-kb-KB123ABC").count() == 1  # untouched
    shot(page, evidence, "08-admin-use-defaults")

    # --- workspace switch closes the editor; a late response never lands
    fx.next_post = "slow"
    page.get_by_test_id("preset-settings-save").click()
    page.get_by_role("alertdialog").get_by_role("button", name="SAVE & RE-PUBLISH").click()
    page.get_by_test_id("workspace-switcher-btn").click()
    page.get_by_test_id(f"workspace-option-{WS_B['id']}").click()
    page.get_by_test_id("preset-settings-dialog").wait_for(state="detached")
    wait_status(page, "not_installed")
    page.wait_for_timeout(2500)  # the slow response has arrived by now
    assert page.get_by_test_id(f"system-preset-{KEY}").get_attribute("data-status") == (
        "not_installed"
    )
    assert page.get_by_test_id("preset-settings-dialog").count() == 0
    assert page.get_by_test_id(f"settings-{KEY}").count() == 0  # nothing installed here
    shot(page, evidence, "09-admin-workspace-switch")
    posted_ws = {p["workspace"] for p in fx.posts}
    assert posted_ws == {WS_A["id"]}, posted_ws  # the late save was scoped to ws-a only
    ctx.close()
    return {"posts": fx.posts, "unhandled": sorted(set(unhandled))}


def member_scenario(browser, base: str, evidence: Path) -> dict:
    fx = Fixture("member")
    unhandled: list[str] = []
    ctx = browser.new_context(viewport={"width": 1440, "height": 1100})
    ctx.add_init_script(f"window.localStorage.setItem('launchpad_workspace', '{WS_A['id']}')")
    page = ctx.new_page()
    install_routes(page, fx, unhandled)
    page.goto(f"{base}/create", wait_until="networkidle")
    wait_status(page, "active")
    button = page.get_by_test_id(f"settings-{KEY}")
    assert button.inner_text().strip() == "VIEW SETTINGS", button.inner_text()
    assert page.get_by_test_id(f"repair-{KEY}").is_disabled()
    button.click()
    page.get_by_test_id("preset-settings-dialog").wait_for()
    page.get_by_test_id("preset-settings-readonly").wait_for()
    assert page.get_by_test_id("preset-settings-save").count() == 0
    assert page.get_by_test_id("preset-settings-defaults").count() == 0
    assert page.get_by_test_id("preset-settings-max-tokens").input_value() == "65536"
    assert page.get_by_test_id("preset-settings-max-tokens").is_disabled()
    assert page.get_by_test_id("preset-settings-prompt").is_disabled()
    shot(page, evidence, "10-member-readonly")
    page.get_by_test_id("preset-settings-cancel").click()
    assert page.get_by_test_id("preset-settings-dialog").count() == 0
    assert fx.posts == []
    ctx.close()
    return {"posts": fx.posts, "unhandled": sorted(set(unhandled))}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:5173")
    parser.add_argument("--evidence-dir", default="/tmp/system-preset-settings-mock")
    parser.add_argument("--browser", default=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"))
    args = parser.parse_args()
    evidence = Path(args.evidence_dir)
    evidence.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        launch: dict[str, Any] = {"headless": True}
        if args.browser:
            launch["executable_path"] = args.browser
        browser = playwright.chromium.launch(**launch)
        try:
            results = {
                "admin": admin_scenario(browser, args.base_url, evidence),
                "member": member_scenario(browser, args.base_url, evidence),
            }
        finally:
            browser.close()
    (evidence / "results.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
