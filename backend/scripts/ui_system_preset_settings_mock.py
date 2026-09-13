#!/usr/bin/env python3
"""Mocked browser scenario for the System presets CONFIGURE flow (SE-040 → SE-044).

Runs against a *frontend-only* dev server: every ``/api/**`` request is intercepted
in the browser and answered from an in-memory fixture, so no backend, ledger or AWS
is touched (unlike the ``e2e_*_browser.py`` scripts). Since SE-044 a preset is
configured on the SAME configure page an existing agent's EDIT uses — there is no
preset settings dialog, no REPAIR/UPDATE and no Skill-record controls on the card.
Exercises, for the architect preset:

* card: no Skill record row / Registry link / REGISTER-VERIFY SKILL / REPAIR button;
  CONFIGURE (admin) and the agent table's EDIT open the shared configure page;
* administrator: the page is prefilled with the stored settings (Sol / 65536 / high /
  30 / 900); BACK posts nothing; a client-side validation problem disables the button;
  a save posts exactly the changed members (+ ``clear`` for a dropped reasoning
  effort) pinned to the panel's workspace, the 202 lands on the launch view whose
  poll is pinned too, and the panel shows the new values afterwards; a 409 and a 422
  render inline and keep the draft; nothing changed ⇒ RE-PUBLISH sends ``force``;
  a FAILED preset opens the same editor and is retried the same way; USE PRESET
  DEFAULTS; a custom id survives a re-click on the selected source; a failing KB
  catalog is an error with RETRY; a slow save locks the page and its late 202 lands;
* workspace: another TAB switching the shared selection never redirects this tab's
  reads, save or poll (pinned ``X-Workspace``); a same-tab switch drops the draft and
  posts nothing;
* ordinary agent: EDIT still posts to ``/api/agents/{id}/redeploy`` and carries the
  stored ``max_tokens`` back;
* member: VIEW SETTINGS is the same page read-only (no save, disabled inputs), the
  table's EDIT is disabled, and no POST is ever made;
* async races (host review 1, genuinely held responses): the wizard's generic
  mount-time KB catalog fetch landing AFTER the pinned preset read must not replace
  the catalog (a foreign workspace's KB never becomes selectable in the preset
  editor), a pinned read from a closed editor landing after a re-opened one is
  dropped (out-of-order loads settle on the newest), and a table EDIT on the system
  row whose preset read lands after the user opened an ordinary edit (or the
  details view) must not reset that draft;
* zh-CN screenshots of the card and the configure page.

    uv run python scripts/ui_system_preset_settings_mock.py --base-url http://127.0.0.1:5199
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

from playwright.sync_api import Route, sync_playwright

KEY = "aws-agent-solution-architect"
AGENT_ID = "a" * 32
ORDINARY_ID = "b" * 32
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
CAPS = {
    "experiment_capability": {"eligible": False, "system_prompt": False,
                              "tool_descriptions": False, "reason": None, "reason_code": None},
    "canary_capability": {"eligible": False, "reason": None, "reason_code": None},
    "invoke_capability": {"eligible": True, "reason": None, "reason_code": None},
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


def deployment(job_id: str, status: str = "succeeded") -> dict:
    stage = "succeeded" if status == "succeeded" else "running"
    return {"id": "d" * 32, "agent_id": AGENT_ID, "job_id": job_id, "status": status,
            "stages": [{"name": n, "status": stage, "detail": ""}
                       for n in ("generate", "package", "provision", "deploy", "register")],
            "started_at": "2026-09-13T00:00:00Z", "ended_at": None}


class Fixture:
    """One preset row per workspace + the install route's state machine + agents."""

    def __init__(self, role: str) -> None:
        self.role = role
        self.posts: list[dict] = []          # system-agents install POSTs
        self.redeploys: list[dict] = []      # ordinary /redeploy POSTs
        self.next_post: str = "accept"       # accept | conflict | invalid | slow
        self.kb_mode: str = "ok"             # ok | fail
        self.held: tuple[Route, int, dict] | None = None
        # held reads for the race scenarios: "first"/"next" holds the next KB catalog
        # request; hold_next_status holds the next GET /api/system-agents
        self.kb_hold: str | None = None
        self.held_kb: list[Route] = []
        self.hold_next_status = False
        self.held_status: tuple[Route, str] | None = None
        self.kb_reads: list[str | None] = []
        self.agent_reads: list[str | None] = []   # X-Workspace of GET /api/agents/{preset}
        self.job_reads: list[str | None] = []
        self.polls = 0
        self.job_id = "j" * 32
        self.rows = {WS_A["id"]: self._row(dict(DEFAULTS)), WS_B["id"]: self._row(None)}

    def release_held(self) -> None:
        assert self.held is not None, "no held response"
        route, status, payload = self.held
        self.held = None
        route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))

    def release_kb(self, items: list[dict]) -> None:
        """Answer the OLDEST held KB catalog request with `items`."""
        route = self.held_kb.pop(0)
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"items": items}))

    def release_status(self) -> None:
        assert self.held_status is not None, "no held status read"
        route, ws = self.held_status
        self.held_status = None
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(self.status(ws)))

    def _row(self, settings: dict | None) -> dict:
        installed = settings is not None
        admin = self.role == "admin"
        return {
            "key": KEY, "name": KEY, "label": "AWS Agent Solution Architect",
            "description": "Platform-managed advisory Harness.", "method": "harness",
            "skill_version": "1.0.0", "installed_skill_version": "1.0.0" if installed else None,
            "update_available": False, "status": "active" if installed else "not_installed",
            "requirements": [], "name_collision": None,
            "agent_id": AGENT_ID if installed else None,
            "agent_status": "active" if installed else None, "error": None,
            "job_id": self.job_id if installed else None,
            "deployment_id": "d" * 32 if installed else None,
            "deployment_status": "succeeded" if installed else None,
            "model_id": settings["model_id"] if installed else None,
            "model_source": settings["model_source"] if installed else None,
            "knowledge_bases": settings["knowledge_bases"] if installed else [],
            "allowed_tools": ["file_*", "@aws_knowledge"], "memory": "disabled",
            "settings": settings or {}, "defaults": dict(DEFAULTS),
            "editable_fields": list(DEFAULTS), "operation": None,
            "skill_registration": {"record_id": "r" * 32, "release_version": "1.0.0"}
            if installed else None,
            "can_register_skill": admin and installed,
            "can_install": admin and not installed, "can_repair": admin and installed,
            "can_configure": admin and installed, "can_uninstall": admin and installed,
            "updated_at": "2026-09-13T00:00:00Z",
        }

    # ── agents (the Existing agents table + the launch view poll) ──────────────
    def system_agent(self, ws: str) -> dict:
        row = self.rows[ws]
        s = row["settings"]
        return {
            "id": AGENT_ID, "name": KEY, "method": "harness", "status": row["agent_status"],
            "arn": "arn:aws:bedrock-agentcore:us-west-2:111:harness/xyz", "resource_id": "xyz",
            "version": "4", "owner": "system", "error": row["error"],
            "spec": {"name": KEY, "method": "harness", **{k: s.get(k) for k in DEFAULTS},
                     "allowed_tools": row["allowed_tools"], "memory": {"short_term": False,
                                                                       "long_term": False}},
            "system": {"managed": True, "key": KEY, "label": row["label"],
                       "skill_version": "1.0.0",
                       "protected_actions": ["redeploy", "delete", "convert"]},
            **CAPS, "created_at": "2026-09-01T00:00:00Z", "updated_at": row["updated_at"],
            "deployment": deployment(row["job_id"], row["deployment_status"] or "succeeded"),
            "deployments": [deployment(row["job_id"], row["deployment_status"] or "succeeded")],
            "revision": 4,
        }

    def ordinary_agent(self) -> dict:
        return {
            "id": ORDINARY_ID, "name": "hr-assistant", "method": "harness", "status": "active",
            "arn": "arn:aws:bedrock-agentcore:us-west-2:111:harness/hr", "resource_id": "hr",
            "version": "2", "owner": "operator", "error": None,
            "spec": {"name": "hr-assistant", "method": "harness",
                     "model_id": "global.anthropic.claude-sonnet-5", "model_source": "bedrock",
                     "max_tokens": 4096, "reasoning_effort": None, "max_iterations": 12,
                     "timeout_seconds": 400, "system_prompt": "You help with HR.",
                     "tools": [], "skills": [], "knowledge_bases": [],
                     "memory": {"short_term": True, "long_term": True}},
            "system": None, **CAPS,
            "created_at": "2026-09-01T00:00:00Z", "updated_at": "2026-09-10T00:00:00Z",
            "deployment": {**deployment("h" * 32), "agent_id": ORDINARY_ID},
            "deployments": [{**deployment("h" * 32), "agent_id": ORDINARY_ID}], "revision": 2,
        }

    def agents(self, ws: str) -> dict:
        rows = [self.ordinary_agent()]
        if self.rows[ws]["agent_id"]:
            rows.insert(0, self.system_agent(ws))
        return {"agents": rows, "count": len(rows)}

    # ── system-agents ──────────────────────────────────────────────────────────
    def status(self, ws: str) -> dict:
        row = self.rows[ws]
        # a deploying row settles on the second poll after the save
        if row["status"] == "deploying":
            self.polls += 1
            if self.polls >= 2:
                row["status"] = row["agent_status"] = "active"
                row["deployment_status"] = "succeeded"
                row["can_configure"] = row["can_repair"] = self.role == "admin"
        return {"workspace_id": ws, "presets": [copy.deepcopy(row)]}

    def fail_preset(self, ws: str, error: str) -> None:
        row = self.rows[ws]
        row["status"] = row["agent_status"] = "failed"
        row["deployment_status"] = "failed"
        row["error"] = error
        row["can_configure"] = row["can_repair"] = self.role == "admin"

    def install(self, ws: str | None, body: dict) -> tuple[int, dict]:
        self.posts.append({"workspace": ws, "body": body})
        assert ws in self.rows, f"POST without a pinned/known X-Workspace header: {ws!r}"
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
        row["deployment_status"] = "running"
        row["error"] = None
        row["can_configure"] = row["can_repair"] = False
        self.polls = 0
        self.job_id = row["job_id"] = f"k{len(self.posts):031d}"
        return 202, {"agent": self.system_agent(ws), "job_id": row["job_id"],
                     "deployment_id": "e" * 32, "created": False, "changed": True,
                     "preset": copy.deepcopy(row)}


def install_routes(page, fx: Fixture, unhandled: list[str]) -> None:
    def handle(route: Route) -> None:
        req = route.request
        url = req.url.split("?", 1)[0]
        path = "/" + url.split("/", 3)[3] if url.count("/") >= 3 else url
        ws = req.headers.get("x-workspace")  # exactly what the console sent

        def reply(status: int, body) -> None:
            route.fulfill(status=status, content_type="application/json",
                          body=json.dumps(body))

        if path == "/api/auth/status":
            return reply(200, auth(fx.role))
        if path == "/api/workspaces" and req.method == "GET":
            return reply(200, {"workspaces": [WS_A, WS_B], "all_workspaces": fx.role == "admin"})
        if path == "/api/system-agents" and req.method == "GET":
            assert ws in fx.rows, f"status read without a known X-Workspace: {ws!r}"
            if fx.hold_next_status:
                fx.hold_next_status = False
                fx.held_status = (route, ws)
                return None
            return reply(200, fx.status(ws))
        if path == f"/api/system-agents/{KEY}/install" and req.method == "POST":
            body = req.post_data_json or {}
            slow = fx.next_post == "slow"
            if slow:
                fx.next_post = "accept"
            status, payload = fx.install(ws, body)
            if slow:  # accepted server-side, response delivered when the test says so
                fx.held = (route, status, payload)
                return None
            return reply(status, payload)
        if path == "/api/knowledge-bases":
            fx.kb_reads.append(ws)
            if fx.kb_hold in ("first", "next"):
                fx.kb_hold = None
                fx.held_kb.append(route)
                return None
            if fx.kb_mode == "fail":
                return reply(502, {"code": "http.502", "message": "kb catalog upstream down",
                                   "detail": None})
            return reply(200, {"items": [
                {"kb_id": "KB123ABC", "name": "aws-whitepapers", "description": "guides",
                 "status": "ACTIVE", "type": "MANAGED"},
                {"kb_id": "KB999ZZZ", "name": "creating", "status": "CREATING",
                 "type": "MANAGED"},
            ]})
        if path == "/api/agents" and req.method == "GET":
            return reply(200, fx.agents(ws if ws in fx.rows else WS_A["id"]))
        if path == f"/api/agents/{AGENT_ID}" and req.method == "GET":
            fx.agent_reads.append(ws)
            assert ws in fx.rows, f"agent poll without a known X-Workspace: {ws!r}"
            return reply(200, fx.system_agent(ws))
        if path == f"/api/agents/{ORDINARY_ID}" and req.method == "GET":
            return reply(200, fx.ordinary_agent())
        if path == f"/api/agents/{ORDINARY_ID}/redeploy" and req.method == "POST":
            fx.redeploys.append({"workspace": ws, "body": req.post_data_json})
            return reply(202, {"agent": fx.ordinary_agent(), "job_id": "h" * 32,
                               "deployment_id": "d" * 32})
        if path == f"/api/agents/{AGENT_ID}/redeploy":
            fx.redeploys.append({"workspace": ws, "body": req.post_data_json, "system": True})
            return reply(403, {"code": "agent.system_managed", "message": "protected",
                               "detail": None})
        if path.endswith("/versions") and path.startswith("/api/agents/"):
            return reply(200, {"kind": "harness", "resource_id": "xyz", "versions": [],
                               "endpoints": [], "latest_version": "4", "ledger_version": "4",
                               "canary_endpoints": []})
        if path.startswith("/api/jobs/") and req.method == "GET":
            fx.job_reads.append(ws)
            job_id = path.rsplit("/", 1)[1]
            return reply(200, {"id": job_id, "type": "deploy_agent", "status": "succeeded",
                               "payload": {}, "log": "", "error": None,
                               "created_at": "2026-09-13T00:00:00Z",
                               "updated_at": "2026-09-13T00:00:10Z"})
        if path == "/api/registry/attachables":
            return reply(200, {"mcp_servers": [], "skills": []})
        if path == "/api/memory/resources":
            return reply(200, {"items": []})
        unhandled.append(f"{req.method} {path}")
        return reply(200, {"items": [], "records": [], "skills": [], "count": 0})

    page.route("**/api/**", handle)


def shot(page, evidence: Path, name: str) -> None:
    page.screenshot(path=str(evidence / f"{name}.png"), full_page=False)


def wait_status(page, status: str, timeout_ms: int = 15000) -> None:
    page.locator(f'[data-testid="system-preset-{KEY}"][data-status="{status}"]').wait_for(
        timeout=timeout_ms
    )


def editor(page, mode: str = "edit"):
    loc = page.locator(f'[data-testid="configure-step"][data-system-edit="{mode}"]')
    loc.wait_for()
    return loc


def submit_and_confirm(page, label: str) -> None:
    page.get_by_test_id("launch-submit").click()
    page.get_by_role("alertdialog").wait_for()
    page.get_by_role("alertdialog").get_by_role("button", name=label).click()


def assert_card_simplified(page) -> None:
    row = page.get_by_test_id(f"system-preset-{KEY}")
    assert row.get_by_test_id("preset-skill-registration").count() == 0
    assert page.get_by_test_id(f"skill-record-link-{KEY}").count() == 0
    assert page.get_by_test_id(f"register-skill-{KEY}").count() == 0
    assert page.get_by_test_id(f"repair-{KEY}").count() == 0
    assert page.get_by_test_id("preset-settings-dialog").count() == 0
    text = row.inner_text()
    assert "Skill record:" not in text and "OPEN IN REGISTRY" not in text, text
    assert page.get_by_test_id(f"details-{KEY}").count() == 1
    assert page.get_by_test_id(f"uninstall-{KEY}").count() == 1
    assert page.get_by_test_id("preset-open-assistant").count() == 1


def assert_prefilled(page, tokens: str = "65536", effort: str = "high") -> None:
    assert page.get_by_test_id("model-select").input_value() == "us.openai.gpt-5.6-sol"
    assert page.get_by_test_id("agent-max-tokens").input_value() == tokens
    assert page.get_by_test_id("agent-effort").input_value() == effort
    assert page.get_by_test_id("agent-max-iterations").input_value() == "30"
    assert page.get_by_test_id("agent-timeout").input_value() == "900"
    assert page.get_by_test_id("agent-prompt").input_value() == PROMPT


def back_to_list(page) -> None:
    page.get_by_test_id("configure-back").click()
    page.get_by_test_id(f"system-preset-{KEY}").wait_for()


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
    assert_card_simplified(page)
    button = page.get_by_test_id(f"settings-{KEY}")
    assert button.inner_text().strip() == "CONFIGURE", button.inner_text()
    shot(page, evidence, "01-admin-card")

    # --- CONFIGURE opens the shared configure page (no dialog), prefilled, nothing posted
    button.click()
    editor(page, "edit")
    assert page.get_by_test_id("preset-settings-dialog").count() == 0
    assert page.get_by_test_id("system-edit-note").count() == 1
    assert page.get_by_test_id("preset-settings-readonly").count() == 0
    assert_prefilled(page)
    assert page.get_by_test_id("preset-protected").count() == 1  # tools/skill/memory read-only
    assert page.get_by_test_id("memory-select").count() == 0     # no ordinary memory editor
    assert page.locator('input[type="file"]').count() == 0        # no skill upload for a preset
    assert page.get_by_test_id("kb-picker").count() == 1
    assert "RE-PUBLISH" in page.get_by_test_id("launch-submit").inner_text()
    assert "SAVE" not in page.get_by_test_id("launch-submit").inner_text()  # nothing changed yet
    assert fx.posts == []
    shot(page, evidence, "02-admin-editor-prefilled")

    # --- BACK: no write, the draft is gone
    page.get_by_test_id("agent-max-tokens").fill("4096")
    back_to_list(page)
    assert fx.posts == []
    page.get_by_test_id(f"settings-{KEY}").click()
    editor(page, "edit")
    assert_prefilled(page)

    # --- client-side validation blocks the button
    page.get_by_test_id("agent-max-tokens").fill("0")
    page.get_by_test_id("preset-settings-problems").wait_for()
    assert page.get_by_test_id("launch-submit").is_disabled()
    shot(page, evidence, "03-admin-validation")
    page.get_by_test_id("agent-max-tokens").fill("65536")
    assert page.get_by_test_id("preset-settings-problems").count() == 0

    # --- Claude disables the effort select; the save posts model + clear (+ the rest)
    page.get_by_test_id("model-select").select_option("global.anthropic.claude-opus-5")
    assert page.get_by_test_id("agent-effort").is_disabled()
    assert "Only OpenAI" in page.get_by_test_id("agent-effort-hint").inner_text()
    page.get_by_test_id("agent-max-iterations").fill("40")
    page.get_by_test_id("kb-KB123ABC").click()  # mount an ACTIVE KB
    assert page.get_by_test_id("kb-KB999ZZZ").count() == 0  # CREATING is hidden
    assert "SAVE & RE-PUBLISH" in page.get_by_test_id("launch-submit").inner_text()
    assert "change(s) pending" in page.get_by_test_id("system-edit-pending").inner_text()

    # 409 first: the page stays with the reason and the draft
    fx.next_post = "conflict"
    submit_and_confirm(page, "SAVE & RE-PUBLISH")
    page.get_by_test_id("submit-error").wait_for()
    assert "being deployed" in page.get_by_test_id("submit-error").inner_text()
    editor(page, "edit")
    assert page.get_by_test_id("agent-max-iterations").input_value() == "40"  # draft kept
    shot(page, evidence, "04-admin-409")
    # 422 next: server-side detail rows render
    fx.next_post = "invalid"
    submit_and_confirm(page, "SAVE & RE-PUBLISH")
    page.get_by_test_id("submit-error").wait_for()
    assert "OpenAI GPT-5.x" in page.get_by_test_id("submit-error").inner_text()
    shot(page, evidence, "05-admin-422")
    # accepted: partial body, launch view with a pinned poll, then the card converges
    submit_and_confirm(page, "SAVE & RE-PUBLISH")
    page.get_by_test_id("job-log").wait_for()
    accepted = fx.posts[-1]["body"]
    assert accepted == {
        "model_id": "global.anthropic.claude-opus-5",
        "max_iterations": 40,
        "knowledge_bases": [{"kb_id": "KB123ABC", "name": "aws-whitepapers",
                             "description": "guides"}],
        "clear": ["reasoning_effort"],
    }, accepted
    assert all(p["workspace"] == WS_A["id"] for p in fx.posts)
    assert fx.redeploys == []  # never the ordinary redeploy for a preset
    page.wait_for_timeout(2500)  # a couple of poll ticks
    assert fx.agent_reads and all(w == WS_A["id"] for w in fx.agent_reads), fx.agent_reads
    assert fx.job_reads and all(w == WS_A["id"] for w in fx.job_reads), fx.job_reads
    shot(page, evidence, "06-admin-launch-view")
    page.goto(f"{base}/create", wait_until="networkidle")
    wait_status(page, "active", timeout_ms=20000)
    summary = row.get_by_test_id("preset-inference").inner_text()
    assert "reasoning effort: none" in summary and "40 iterations" in summary, summary
    assert "global.anthropic.claude-opus-5" in row.inner_text()
    shot(page, evidence, "07-admin-after-save")

    # --- the agent table's EDIT on the system row opens the SAME page
    page.get_by_test_id(f"edit-{KEY}").click()
    editor(page, "edit")
    assert page.get_by_test_id("model-select").input_value() == "global.anthropic.claude-opus-5"
    assert page.get_by_test_id("agent-max-iterations").input_value() == "40"
    assert page.get_by_test_id("differs-model_id").count() == 1
    shot(page, evidence, "08-admin-table-edit")
    # USE PRESET DEFAULTS: prompt/knobs come back, KBs kept
    page.get_by_test_id("preset-settings-defaults").click()
    assert page.get_by_test_id("model-select").input_value() == "us.openai.gpt-5.6-sol"
    assert page.get_by_test_id("agent-effort").input_value() == "high"
    assert page.get_by_test_id("agent-max-iterations").input_value() == "30"
    assert page.get_by_test_id("kb-KB123ABC").count() == 1  # untouched
    shot(page, evidence, "08a-admin-use-defaults")

    # --- a custom model id survives a re-click on the selected source
    page.get_by_test_id("model-select").select_option("__custom__")
    page.get_by_test_id("model-custom").fill("us.anthropic.claude-custom-v9")
    page.get_by_test_id("model-source-bedrock").click()  # already selected
    assert page.get_by_test_id("model-custom").input_value() == "us.anthropic.claude-custom-v9"
    assert page.get_by_test_id("model-select").input_value() == "__custom__"
    page.get_by_test_id("model-source-mantle").click()  # a real switch does reset
    assert page.get_by_test_id("model-select").input_value() == "openai.gpt-5.6-terra"
    back_to_list(page)

    # --- a failing KB catalog is an error with RETRY, never a silent empty catalog
    fx.kb_mode = "fail"
    page.get_by_test_id(f"settings-{KEY}").click()
    editor(page, "edit")
    page.get_by_test_id("preset-settings-kb-error").wait_for()
    kb_error = page.get_by_test_id("preset-settings-kb-error").inner_text()
    assert "kb catalog upstream down" in kb_error, kb_error
    assert page.get_by_test_id("kb-KB123ABC").count() == 1  # the stored chip is kept
    shot(page, evidence, "08b-admin-kb-error")
    fx.kb_mode = "ok"
    page.get_by_test_id("preset-settings-kb-retry").click()
    page.get_by_test_id("preset-settings-kb-error").wait_for(state="detached")
    assert page.get_by_test_id("kb-KB123ABC").count() == 1
    assert all(w == WS_A["id"] for w in fx.kb_reads), fx.kb_reads  # pinned reads

    # --- nothing changed ⇒ RE-PUBLISH is a forced repair (the same stored settings)
    assert "SAVE" not in page.get_by_test_id("launch-submit").inner_text()
    assert "no changes" in page.get_by_test_id("system-edit-pending").inner_text()
    before = len(fx.posts)
    page.get_by_test_id("launch-submit").click()
    dialog = page.get_by_role("alertdialog")
    dialog.wait_for()
    assert "Nothing changed" in dialog.inner_text()
    shot(page, evidence, "08c-admin-force-confirm")
    dialog.get_by_role("button", name="RE-PUBLISH").click()
    page.get_by_test_id("job-log").wait_for()
    assert fx.posts[-1] == {"workspace": WS_A["id"], "body": {"force": True}}, fx.posts[-1]
    assert len(fx.posts) == before + 1

    # --- a FAILED preset opens the same editor and is retried the same way
    fx.fail_preset(WS_A["id"], "deploy stage: UpdateHarness ValidationException")
    page.goto(f"{base}/create", wait_until="networkidle")
    wait_status(page, "failed")
    assert "ValidationException" in page.get_by_test_id("preset-error").inner_text()
    assert page.get_by_test_id(f"repair-{KEY}").count() == 0
    shot(page, evidence, "08d-admin-failed-card")
    page.get_by_test_id(f"settings-{KEY}").click()
    editor(page, "edit")
    submit_and_confirm(page, "RE-PUBLISH")
    page.get_by_test_id("job-log").wait_for()
    assert fx.posts[-1]["body"] == {"force": True}
    page.goto(f"{base}/create", wait_until="networkidle")
    wait_status(page, "active", timeout_ms=20000)

    # --- a slow save locks the page (single-flight) and its late 202 lands
    fx.next_post = "slow"
    page.get_by_test_id(f"settings-{KEY}").click()
    editor(page, "edit")
    page.get_by_test_id("agent-max-tokens").fill("1234")
    submit_and_confirm(page, "SAVE & RE-PUBLISH")
    page.get_by_test_id("launch-submit").filter(has_text="SAVING").wait_for()
    assert page.get_by_test_id("launch-submit").is_disabled()
    assert page.get_by_test_id("configure-back").is_disabled()
    page.get_by_test_id("launch-submit").click(force=True)
    page.wait_for_timeout(300)
    assert fx.held is not None  # the 202 has not been delivered yet
    assert len([p for p in fx.posts if p["body"] == {"max_tokens": 1234}]) == 1  # posted once
    shot(page, evidence, "08e-admin-saving-locked")
    fx.release_held()
    page.get_by_test_id("job-log").wait_for(timeout=10000)
    page.goto(f"{base}/create", wait_until="networkidle")
    wait_status(page, "active", timeout_ms=20000)
    assert "max output/call: 1234 tok" in row.get_by_test_id("preset-inference").inner_text()

    # --- another TAB switches the shared selection to workspace B while this tab
    # still displays A: A's reads, save and poll stay pinned to A
    tab_b = ctx.new_page()
    install_routes(tab_b, fx, unhandled)
    tab_b.goto(f"{base}/create", wait_until="networkidle")
    tab_b.get_by_test_id("workspace-switcher-btn").click()
    tab_b.get_by_test_id(f"workspace-option-{WS_B['id']}").click()
    tab_b.locator(f'[data-testid="system-preset-{KEY}"][data-status="not_installed"]').wait_for()
    assert page.evaluate("localStorage.getItem('launchpad_workspace')") == WS_B["id"]  # shared
    page.get_by_test_id(f"settings-{KEY}").click()  # tab A still shows workspace A
    editor(page, "edit")
    assert page.get_by_test_id("agent-max-tokens").input_value() == "1234"
    page.get_by_test_id("agent-max-tokens").fill("4321")
    before = len(fx.posts)
    fx.agent_reads.clear()
    fx.job_reads.clear()
    submit_and_confirm(page, "SAVE & RE-PUBLISH")
    page.get_by_test_id("job-log").wait_for()
    page.wait_for_timeout(2500)
    assert len(fx.posts) == before + 1
    assert fx.posts[-1] == {"workspace": WS_A["id"], "body": {"max_tokens": 4321}}, fx.posts[-1]
    assert fx.agent_reads and all(w == WS_A["id"] for w in fx.agent_reads), fx.agent_reads
    assert fx.job_reads and all(w == WS_A["id"] for w in fx.job_reads), fx.job_reads
    assert fx.rows[WS_B["id"]]["status"] == "not_installed"  # B never touched
    assert fx.rows[WS_A["id"]]["settings"]["max_tokens"] == 4321
    tab_b.get_by_test_id("system-presets-reload").click()  # B re-reads: still nothing
    tab_b.locator(f'[data-testid="system-preset-{KEY}"][data-status="not_installed"]').wait_for()
    shot(page, evidence, "09-admin-tab-a-pinned")
    shot(tab_b, evidence, "09b-admin-tab-b-untouched")
    tab_b.close()

    # --- same-tab switch from an open draft: the page remounts on B, nothing posted
    page.evaluate(f"localStorage.setItem('launchpad_workspace', '{WS_A['id']}')")
    page.goto(f"{base}/create", wait_until="networkidle")
    wait_status(page, "active", timeout_ms=20000)
    page.get_by_test_id(f"settings-{KEY}").click()
    editor(page, "edit")
    page.get_by_test_id("agent-max-tokens").fill("999")
    before = len(fx.posts)
    page.get_by_test_id("workspace-switcher-btn").click()
    page.get_by_test_id(f"workspace-option-{WS_B['id']}").click()
    wait_status(page, "not_installed")
    assert page.get_by_test_id("configure-step").count() == 0  # the draft is gone
    assert page.get_by_test_id(f"settings-{KEY}").count() == 0  # nothing installed here
    assert len(fx.posts) == before
    shot(page, evidence, "09c-admin-same-tab-switch")
    posted_ws = {p["workspace"] for p in fx.posts}
    assert posted_ws == {WS_A["id"]}, posted_ws

    # --- an ORDINARY agent's EDIT is unchanged: ordinary redeploy, stored cap carried
    page.evaluate(f"localStorage.setItem('launchpad_workspace', '{WS_A['id']}')")
    page.goto(f"{base}/create", wait_until="networkidle")
    wait_status(page, "active", timeout_ms=20000)
    page.get_by_test_id("edit-hr-assistant").click()
    page.locator('[data-testid="configure-step"]:not([data-system-edit])').wait_for()
    assert page.get_by_test_id("agent-max-tokens").input_value() == "4096"
    assert page.get_by_test_id("agent-max-iterations").input_value() == "12"
    assert page.get_by_test_id("agent-timeout").input_value() == "400"
    assert page.get_by_test_id("memory-select").count() == 1  # the ordinary sections stay
    assert page.get_by_test_id("preset-protected").count() == 0
    page.get_by_test_id("agent-prompt").fill("You help with HR. Be brief.")
    shot(page, evidence, "10-admin-ordinary-edit")
    submit_and_confirm(page, "RE-PUBLISH")
    page.get_by_test_id("job-log").wait_for()
    assert len(fx.redeploys) == 1 and not fx.redeploys[0].get("system"), fx.redeploys
    sent = fx.redeploys[0]["body"]
    assert sent["max_tokens"] == 4096 and "reasoning_effort" not in sent, sent
    assert (sent["max_iterations"], sent["timeout_seconds"]) == (12, 400), sent
    assert sent["system_prompt"] == "You help with HR. Be brief."
    assert sent["memory"] == {"short_term": True, "long_term": True}, sent
    ctx.close()
    return {"posts": fx.posts, "redeploys": fx.redeploys, "unhandled": sorted(set(unhandled))}


def member_scenario(browser, base: str, evidence: Path) -> dict:
    fx = Fixture("member")
    unhandled: list[str] = []
    ctx = browser.new_context(viewport={"width": 1440, "height": 1100})
    ctx.add_init_script(f"window.localStorage.setItem('launchpad_workspace', '{WS_A['id']}')")
    page = ctx.new_page()
    install_routes(page, fx, unhandled)
    page.goto(f"{base}/create", wait_until="networkidle")
    wait_status(page, "active")
    assert_card_simplified(page)
    button = page.get_by_test_id(f"settings-{KEY}")
    assert button.inner_text().strip() == "VIEW SETTINGS", button.inner_text()
    assert page.get_by_test_id(f"edit-{KEY}").is_disabled()   # the table's EDIT stays off
    assert not page.get_by_test_id("edit-hr-assistant").is_disabled()  # ordinary rows unchanged
    button.click()
    editor(page, "review")
    page.get_by_test_id("preset-settings-readonly").wait_for()
    assert page.get_by_test_id("launch-submit").count() == 0
    assert page.get_by_test_id("preset-settings-defaults").count() == 0
    assert page.get_by_test_id("preset-settings-prompt-default").count() == 0
    assert_prefilled(page)
    for testid in ("agent-max-tokens", "agent-prompt", "model-select", "agent-max-iterations"):
        assert page.get_by_test_id(testid).is_disabled(), testid
    assert page.get_by_test_id("kb-KB123ABC").is_disabled()  # the shared picker, locked
    assert all(w == WS_A["id"] for w in fx.kb_reads), fx.kb_reads  # never another workspace
    shot(page, evidence, "11-member-readonly")
    assert "close" in page.get_by_test_id("configure-back").inner_text().lower()
    back_to_list(page)
    assert fx.posts == [] and fx.redeploys == []
    ctx.close()
    return {"posts": fx.posts, "unhandled": sorted(set(unhandled))}


def race_scenario(browser, base: str, evidence: Path) -> dict:
    """Host review 1: the two async editor regressions, with genuinely held responses."""
    fx = Fixture("admin")
    unhandled: list[str] = []
    ctx = browser.new_context(viewport={"width": 1440, "height": 1100})
    ctx.add_init_script(f"window.localStorage.setItem('launchpad_workspace', '{WS_A['id']}')")
    page = ctx.new_page()
    install_routes(page, fx, unhandled)
    foreign = [{"kb_id": "KBFOREIGN", "name": "lab-only", "description": "workspace B",
                "status": "ACTIVE", "type": "MANAGED"}]
    stale = [{"kb_id": "KBSTALE", "name": "stale-read", "status": "ACTIVE", "type": "MANAGED"}]

    # --- (1) the generic mount-time catalog fetch is held; the pinned preset read lands
    # first; the late generic response (another tab moved the shared selection to B,
    # so it carries B's catalog) must NOT replace the preset editor's catalog
    fx.kb_hold = "first"
    page.goto(f"{base}/create", wait_until="domcontentloaded")
    wait_status(page, "active")
    assert len(fx.held_kb) == 1, fx.kb_reads  # exactly the mount-time generic fetch
    page.get_by_test_id(f"settings-{KEY}").click()
    editor(page, "edit")
    page.get_by_test_id("kb-KB123ABC").wait_for()  # the pinned read landed
    fx.release_kb(foreign)                          # the generic one lands late
    page.wait_for_timeout(400)
    assert page.get_by_test_id("kb-KBFOREIGN").count() == 0, "foreign KB became selectable"
    assert page.get_by_test_id("kb-KB123ABC").count() == 1
    shot(page, evidence, "14-race-late-generic-kb")

    # --- (1b) close / reopen with a pinned read from the CLOSED editor landing after the
    # re-opened editor's own read: out-of-order loads settle on the newest
    back_to_list(page)
    fx.kb_hold = "next"
    page.get_by_test_id(f"settings-{KEY}").click()
    editor(page, "edit")
    page.get_by_test_id("preset-settings-kb-loading").wait_for()
    assert len(fx.held_kb) == 1
    back_to_list(page)                               # closed while its read is in flight
    page.get_by_test_id(f"settings-{KEY}").click()  # reopened: a fresh pinned read
    editor(page, "edit")
    page.get_by_test_id("kb-KB123ABC").wait_for()
    fx.release_kb(stale)                             # the closed editor's read lands now
    page.wait_for_timeout(400)
    assert page.get_by_test_id("kb-KBSTALE").count() == 0, "stale pinned read applied"
    assert page.get_by_test_id("kb-KB123ABC").count() == 1
    assert page.get_by_test_id("preset-settings-kb-loading").count() == 0
    # the error path still shows + retries, and the stored refs survive a failure
    back_to_list(page)
    fx.kb_mode = "fail"
    page.get_by_test_id(f"settings-{KEY}").click()
    editor(page, "edit")
    page.get_by_test_id("preset-settings-kb-error").wait_for()
    fx.kb_mode = "ok"
    page.get_by_test_id("preset-settings-kb-retry").click()
    page.get_by_test_id("kb-KB123ABC").wait_for()
    assert page.get_by_test_id("preset-settings-kb-error").count() == 0
    back_to_list(page)
    assert all(w == WS_A["id"] for w in fx.kb_reads), fx.kb_reads

    # --- (2) table EDIT on the system row: its preset read is held; the user opens an
    # ORDINARY edit and types; the late system read must not reset that draft
    fx.hold_next_status = True
    page.get_by_test_id(f"edit-{KEY}").click()
    page.wait_for_timeout(200)
    assert fx.held_status is not None
    page.get_by_test_id("edit-hr-assistant").click()
    page.locator('[data-testid="configure-step"]:not([data-system-edit])').wait_for()
    page.get_by_test_id("agent-prompt").fill("UNSAVED ordinary draft")
    fx.release_status()
    page.wait_for_timeout(400)
    page.locator('[data-testid="configure-step"]:not([data-system-edit])').wait_for()
    assert page.get_by_test_id("system-edit-note").count() == 0
    assert page.get_by_test_id("agent-prompt").input_value() == "UNSAVED ordinary draft"
    assert page.get_by_test_id("agent-max-tokens").input_value() == "4096"
    shot(page, evidence, "15-race-stale-system-edit")
    back_to_list(page)
    # (2b) the same with the DETAILS view opened meanwhile: the launch view stays
    fx.hold_next_status = True
    page.get_by_test_id(f"edit-{KEY}").click()
    page.wait_for_timeout(200)
    assert fx.held_status is not None
    page.get_by_test_id(f"details-{KEY}").click()
    page.get_by_test_id("job-log").wait_for()
    fx.release_status()
    page.wait_for_timeout(400)
    assert page.get_by_test_id("configure-step").count() == 0
    assert page.get_by_test_id("job-log").count() == 1
    # (2c) with nothing newer, the held read still opens the editor (no lost click)
    page.goto(f"{base}/create", wait_until="networkidle")
    wait_status(page, "active")
    fx.hold_next_status = True
    page.get_by_test_id(f"edit-{KEY}").click()
    page.wait_for_timeout(200)
    fx.release_status()
    editor(page, "edit")
    assert_prefilled(page)
    assert fx.posts == [] and fx.redeploys == []
    ctx.close()
    return {"kb_reads": fx.kb_reads, "unhandled": sorted(set(unhandled))}


def zh_screenshots(browser, base: str, evidence: Path) -> None:
    fx = Fixture("admin")
    unhandled: list[str] = []
    ctx = browser.new_context(viewport={"width": 1440, "height": 1100})
    ctx.add_init_script(
        f"window.localStorage.setItem('launchpad_workspace', '{WS_A['id']}');"
        "window.localStorage.setItem('i18nextLng', 'zh-CN')"
    )
    page = ctx.new_page()
    install_routes(page, fx, unhandled)
    page.goto(f"{base}/create", wait_until="networkidle")
    wait_status(page, "active")
    assert_card_simplified(page)
    shot(page, evidence, "12-zh-admin-card")
    page.get_by_test_id(f"settings-{KEY}").click()
    editor(page, "edit")
    assert_prefilled(page)
    shot(page, evidence, "13-zh-admin-editor")
    assert fx.posts == []
    ctx.close()


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
                "races": race_scenario(browser, args.base_url, evidence),
            }
            zh_screenshots(browser, args.base_url, evidence)
        finally:
            browser.close()
    (evidence / "results.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
