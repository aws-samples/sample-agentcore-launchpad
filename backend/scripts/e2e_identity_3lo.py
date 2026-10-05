#!/usr/bin/env python3
"""E2E: as_user (3LO / USER_FEDERATION) outbound auth — consent, bind, call, revoke.

Hits REAL AWS. Every resource is named `lpid-e2e-*` and is deleted by `down`;
nothing existing is modified. The IdP is a NEW throwaway Cognito user pool (never
`launchpad-users`), so the whole OAuth dance — hosted-UI login included — runs
headless in Playwright:

    uv run python scripts/e2e_identity_3lo.py up            # pool, domain, client, user
    # a throwaway stack whose return URL is the vite port below; no registry /
    # gateway / memory in LAUNCHPAD_RESOURCES, so the deploy touches none of them
    LAUNCHPAD_DATABASE_URL=sqlite:////tmp/lpid-e2e-3lo.db \\
    LAUNCHPAD_PUBLIC_BASE_URL=http://localhost:5192 \\
    LAUNCHPAD_RESOURCES='{"execution_role_arn": "...", "artifacts_bucket": "..."}' \\
        uv run uvicorn app.main:app --port 8792
    (cd ../frontend && LAUNCHPAD_API=http://localhost:8792 npx vite --port 5192)
    uv run python scripts/e2e_identity_3lo.py run --base http://127.0.0.1:8792 \\
        --ui http://localhost:5192
    uv run python scripts/e2e_identity_3lo.py down --base http://127.0.0.1:8792

`run` creates a CustomOauth2 Connection on the pool, points the app client's
callback at the Connection's AgentCore callback URL, deploys a zip Runtime agent
with one as_user REST tool on the pool's `/oauth2/userInfo`, then proves:

1. a Chat turn answers `auth_required` (URL only — no session uri) and the
   grant is pending;
2. a headless Cognito login redirects to `/auth/return`, which binds the token
   (CompleteResourceTokenAuth) and reaches the `done` phase;
3. the grant is authorized and the next turn returns the user's own claims;
4. a different runtime user is not covered by that consent;
5. revoke (DELETE /api/identity/grants/{c}) forces the next call to re-auth;
6. consenting again clears the revocation and the tool works once more.

`down` deletes the agent (runtime + its service-linked workload identity), the
Connection, the zip artifact, the Cognito domain / pool, the state file and
proves each is gone. The execution role and artifacts bucket are only
referenced, never modified.
"""

import argparse
import asyncio
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

from _e2e_client import e2e_client

PREFIX = "lpid-e2e-"
POOL_NAME = f"{PREFIX}3lo-pool"
CLIENT_NAME = f"{PREFIX}3lo-client"
CONNECTION = f"{PREFIX}3lo"
AGENT_NAME = f"{PREFIX}3lo-agent"
TOOL = "userinfo"
USERNAME = f"{PREFIX}user"
SCOPES = ["openid", "profile", "email"]
STATE_FILE = Path("/tmp/lpid-e2e-3lo-state.json")
SHOTS = Path(os.environ.get("LPID_SHOTS", "/tmp/lpid-e2e-3lo-shots"))


def log(event: str, **data: Any) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(json.dumps({"ts": stamp, "event": event, **data}))
    sys.stdout.flush()


def _check(cond: bool, message: str, **data: Any) -> None:
    log("assert.ok" if cond else "assert.FAIL", message=message, **data)
    if not cond:
        raise AssertionError(message)


def _ws() -> Any:
    from app.services.workspace import default_workspace_context

    return default_workspace_context()


def _cognito() -> Any:
    return _ws().client("cognito-idp")


def _control() -> Any:
    from app.services.agentcore.client import control_client

    return control_client(_ws())


def _load_state() -> dict[str, Any]:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def _save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))
    STATE_FILE.chmod(0o600)  # holds the throwaway client secret + user password


# ── up: the throwaway IdP ────────────────────────────────────────────────────


def up() -> int:
    cognito = _cognito()
    state = _load_state()
    if not state.get("pool_id"):
        pool = cognito.create_user_pool(
            PoolName=POOL_NAME,
            AdminCreateUserConfig={"AllowAdminCreateUserOnly": True},
            AutoVerifiedAttributes=[],
            UserPoolTags={"purpose": "launchpad-identity-v2-p2-e2e"},
        )["UserPool"]
        state["pool_id"] = pool["Id"]
        _save_state(state)
    pool_id = state["pool_id"]
    region = pool_id.split("_", 1)[0]
    if not state.get("domain"):
        domain = f"{PREFIX}3lo-{secrets.token_hex(3)}"
        cognito.create_user_pool_domain(Domain=domain, UserPoolId=pool_id)
        state["domain"] = domain
        _save_state(state)
    if not state.get("client_id"):
        client = cognito.create_user_pool_client(
            UserPoolId=pool_id,
            ClientName=CLIENT_NAME,
            GenerateSecret=True,
            AllowedOAuthFlows=["code"],
            AllowedOAuthScopes=SCOPES,
            AllowedOAuthFlowsUserPoolClient=True,
            SupportedIdentityProviders=["COGNITO"],
            # replaced by the Connection's AgentCore callback URL in `run`
            CallbackURLs=["https://example.com/lpid-e2e-placeholder"],
        )["UserPoolClient"]
        state["client_id"] = client["ClientId"]
        state["client_secret"] = client["ClientSecret"]
        _save_state(state)
    if not state.get("password"):
        password = f"Lpid-{secrets.token_urlsafe(12)}!9a"
        cognito.admin_create_user(
            UserPoolId=pool_id,
            Username=USERNAME,
            MessageAction="SUPPRESS",
            UserAttributes=[
                {"Name": "email", "Value": "lpid-e2e-user@example.com"},
                {"Name": "email_verified", "Value": "true"},
            ],
        )
        cognito.admin_set_user_password(
            UserPoolId=pool_id, Username=USERNAME, Password=password, Permanent=True
        )
        state["password"] = password
        _save_state(state)
    state["discovery_url"] = (
        f"https://cognito-idp.{region}.amazonaws.com/{pool_id}/.well-known/openid-configuration"
    )
    state["userinfo_url"] = f"https://{state['domain']}.auth.{region}.amazoncognito.com/oauth2/userInfo"
    _save_state(state)
    log(
        "idp.up",
        pool_id=pool_id,
        domain=state["domain"],
        client_id=state["client_id"],
        user=USERNAME,
        discovery_url=state["discovery_url"],
    )
    return 0


# ── run: the five steps (+ re-consent) ───────────────────────────────────────


def _chat(client: Any, agent_id: str, prompt: str, session: str | None) -> list[tuple[str, Any]]:
    body: dict[str, Any] = {"prompt": prompt}
    if session:
        body["session_id"] = session
    events: list[tuple[str, Any]] = []
    with client.stream("POST", f"/api/chat/{agent_id}", json=body, timeout=300) as res:
        res.raise_for_status()
        raw = "".join(res.iter_text())
    for frame in raw.split("\n\n"):
        name, data = None, ""
        for line in frame.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        if name and data:
            try:
                events.append((name, json.loads(data)))
            except ValueError:
                events.append((name, data))
    return events


def _ask(events: list[tuple[str, Any]]) -> dict[str, Any] | None:
    return next((d for n, d in events if n == "auth_required"), None)


def _text(events: list[tuple[str, Any]]) -> str:
    return "".join(d.get("text", "") for n, d in events if n == "delta" and isinstance(d, dict))


def _session(events: list[tuple[str, Any]]) -> str | None:
    return next(
        (d.get("session_id") for n, d in events if n == "meta" and isinstance(d, dict)), None
    )


def _status(client: Any, agent_id: str) -> dict[str, Any]:
    res = client.get(f"/api/identity/grants/{CONNECTION}/status", params={"agent_id": agent_id})
    res.raise_for_status()
    return res.json()


async def _consent(url: str, state: dict[str, Any], shot: str) -> dict[str, Any]:
    """Log in at the throwaway pool's hosted UI; the IdP → AgentCore → /auth/return
    redirects follow, and the return page performs the binding leg."""
    from playwright.async_api import async_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        # LPID_CHROMIUM: a locally installed browser when the venv's playwright
        # expects a newer build than the one downloaded on this box
        browser = await p.chromium.launch(executable_path=os.environ.get("LPID_CHROMIUM") or None)
        page = await browser.new_page(viewport={"width": 1280, "height": 860}, locale="zh-CN")
        await page.goto(url, wait_until="domcontentloaded")
        # the hosted UI renders duplicate desktop/mobile forms; only the last is visible
        user = page.locator("#signInFormUsername").last
        await user.wait_for(state="visible", timeout=30000)
        await user.fill(USERNAME)
        await page.locator("#signInFormPassword").last.fill(state["password"])
        await page.locator('input[name="signInSubmitButton"]').last.click()
        await page.wait_for_url("**/auth/return**", timeout=60000)
        landed = page.url
        card = page.get_by_test_id("auth-return")
        await page.wait_for_function(
            "() => ['done','failed'].includes("
            "document.querySelector('[data-testid=auth-return]')?.dataset.phase)",
            timeout=60000,
        )
        phase = await card.get_attribute("data-phase")
        body = await page.get_by_test_id("auth-return-body").inner_text()
        await page.wait_for_timeout(500)
        await page.screenshot(path=str(SHOTS / shot), full_page=True)
        after = page.url
        back_enabled = await page.get_by_test_id("auth-return-back").is_enabled()
        await browser.close()
    return {
        "landed": landed.split("?")[0],
        "landed_has_session_id": "session_id=" in landed,
        "landed_has_state": "state=" in landed,
        "phase": phase,
        "body": body,
        "address_bar_after": after,
        "back_enabled": back_enabled,
    }


def _wait_active(client: Any, agent_id: str, timeout: int) -> dict[str, Any]:
    seen: set[tuple[str, str]] = set()
    deadline = time.time() + timeout
    agent: dict[str, Any] = {}
    while time.time() < deadline:
        agent = client.get(f"/api/agents/{agent_id}").json()
        for stage in agent["deployments"][0]["stages"]:
            key = (stage["name"], stage["status"])
            if key not in seen and stage["status"] != "pending":
                seen.add(key)
                log("deploy.stage", name=stage["name"], status=stage["status"],
                    detail=stage["detail"][:160])
        if agent["status"] in ("active", "failed"):
            break
        time.sleep(10)  # nosemgrep: arbitrary-sleep — paced real-AWS polling
    return agent


def _find_agent(client: Any) -> dict[str, Any] | None:
    agents = client.get("/api/agents").json()["agents"]
    return next((a for a in agents if a["name"] == AGENT_NAME and a["status"] != "deleted"), None)


def run(base: str, ui: str, timeout: int) -> int:
    state = _load_state()
    if not state.get("client_id"):
        raise SystemExit("run `up` first")
    client = e2e_client(base, timeout=300)
    cognito = _cognito()

    # the Connection on the throwaway pool
    conns = client.get("/api/identity/connections").json()["connections"]
    conn = next((c for c in conns if c["name"] == CONNECTION), None)
    if conn is None:
        res = client.post(
            "/api/identity/connections/oauth2",
            json={
                "name": CONNECTION,
                "vendor": "CustomOauth2",
                "template": "cognito",
                "description": "lpid e2e 3LO (temporary)",
                "client_id": state["client_id"],
                "client_secret": state["client_secret"],
                "discovery_url": state["discovery_url"],
                "scopes": SCOPES,
            },
        )
        _check(res.status_code == 201, "OAuth2 Connection created", status=res.status_code,
               body=res.text[:300])
        conn = res.json()
    log("connection", name=conn["name"], arn=conn.get("arn"), callback_url=conn["callback_url"])
    _check(state["client_secret"] not in json.dumps(conn), "client secret never echoed")

    # the IdP must redirect to AgentCore's per-provider callback
    cognito.update_user_pool_client(
        UserPoolId=state["pool_id"],
        ClientId=state["client_id"],
        ClientName=CLIENT_NAME,
        AllowedOAuthFlows=["code"],
        AllowedOAuthScopes=SCOPES,
        AllowedOAuthFlowsUserPoolClient=True,
        SupportedIdentityProviders=["COGNITO"],
        CallbackURLs=[conn["callback_url"]],
    )
    echoed = cognito.describe_user_pool_client(
        UserPoolId=state["pool_id"], ClientId=state["client_id"]
    )["UserPoolClient"]["CallbackURLs"]
    _check(echoed == [conn["callback_url"]], "app client callback = Connection callback",
           callback_urls=echoed)

    agent = _find_agent(client)
    if agent is None:
        res = client.post(
            "/api/agents",
            json={
                "name": AGENT_NAME,
                "method": "zip_runtime",
                "system_prompt": (
                    "You answer questions about the signed-in user. Always call the "
                    f"{TOOL} tool (no arguments) and report the exact claims it returns."
                ),
                "memory": {"short_term": False, "long_term": False},
                "tools": [
                    {
                        "type": "rest",
                        "name": TOOL,
                        "config": {
                            "url": state["userinfo_url"],
                            "method": "GET",
                            "description": "The signed-in user's OIDC claims (Cognito userInfo).",
                        },
                        "auth": {
                            "connection": CONNECTION,
                            "kind": "oauth2",
                            "mode": "as_user",
                            "scopes": SCOPES,
                        },
                    }
                ],
            },
        )
        _check(res.status_code == 202, "as_user agent accepted", status=res.status_code,
               body=res.text[:300])
        agent = res.json()["agent"]
    agent_id = agent["id"]
    agent = _wait_active(client, agent_id, timeout)
    _check(agent["status"] == "active", "zip Runtime agent active", status=agent["status"])
    runtime_id = agent["arn"].rsplit("/", 1)[-1]
    state.update(agent_id=agent_id, runtime_id=runtime_id)
    _save_state(state)
    wi = _control().get_workload_identity(name=runtime_id)
    return_url = f"{ui.rstrip('/')}/auth/return"
    _check(
        return_url in wi.get("allowedResourceOauth2ReturnUrls", []),
        "deployer reconciled the return URL onto the runtime's workload identity",
        workload_identity=wi["name"],
        allowed=wi.get("allowedResourceOauth2ReturnUrls"),
    )

    # (1) first turn → auth_required
    log("step", n=1, what="Chat turn → auth_required, grant pending")
    events = _chat(client, agent_id, "Who am I? Use the userinfo tool.", None)
    ask = _ask(events)
    _check(ask is not None, "auth_required event emitted", events=[n for n, _ in events])
    assert ask is not None
    _check("session_uri" not in ask, "session uri never reaches the browser", keys=sorted(ask))
    _check(ask["provider"] == CONNECTION and ask["tool"] == TOOL, "ask names Connection + tool",
           provider=ask["provider"], tool=ask["tool"], scopes=ask.get("scopes"))
    log("ask.url", host=ask["url"].split("?")[0])
    session = _session(events)
    status = _status(client, agent_id)
    _check(status["status"] == "pending", "grant pending before consent", grant=status)

    # (2) headless consent → /auth/return binds
    log("step", n=2, what="headless Cognito consent → /auth/return binding leg")
    consent = asyncio.run(_consent(ask["url"], state, "e2e-auth-return-done-zh.png"))
    log("consent", **consent)
    _check(consent["landed"] == return_url, "IdP → AgentCore → our /auth/return",
           landed=consent["landed"])
    _check(consent["landed_has_session_id"] and consent["landed_has_state"],
           "return URL carries session_id + state")
    _check(consent["phase"] == "done", "return page reached done", body=consent["body"])
    _check("session_id=" not in consent["address_bar_after"],
           "spent session id removed from the address bar")
    _check(consent["back_enabled"], "back-to-chat CTA enabled once bound")

    # (3) authorized → the tool runs with the user's own token
    log("step", n=3, what="grant authorized, next turn returns the user's claims")
    status = _status(client, agent_id)
    _check(status["status"] == "authorized" and not status["force_reauth"],
           "grant authorized", grant=status)
    events = _chat(client, agent_id, "Who am I? Use the userinfo tool and report the claims.",
                   session)
    answer = _text(events)
    log("answer", text=answer[:600])
    _check(_ask(events) is None, "no second consent ask")
    _check(USERNAME in answer or "lpid-e2e-user@example.com" in answer,
           "answer carries the consenting user's claims")

    # (4) another runtime user is not covered
    log("step", n=4, what="different runtime user → not authorized")
    other = client.post(
        f"/api/agents/{agent_id}/invoke",
        json={"prompt": "Who am I? Use the userinfo tool.", "actor_id": "lpid-e2e-someone-else"},
    )
    other.raise_for_status()
    other_text = other.json()["text"]
    log("other_user", text=other_text[:400])
    _check(USERNAME not in other_text and "example.com" not in other_text,
           "the first user's token did not serve another user")

    # (5) revoke → re-auth on the next call
    log("step", n=5, what="revoke → next call asks for consent again")
    rev = client.delete(f"/api/identity/grants/{CONNECTION}")
    _check(rev.status_code == 200, "revoke accepted", body=rev.json())
    status = _status(client, agent_id)
    _check(status["force_reauth"], "revocation in force", grant=status)
    events = _chat(client, agent_id, "Who am I? Use the userinfo tool.", session)
    ask = _ask(events)
    _check(ask is not None, "revoked grant → auth_required again")
    assert ask is not None
    _check(USERNAME not in _text(events), "no claims served while revoked")

    # (6) consent again → revocation cleared, tool works
    log("step", n=6, what="re-consent clears the revocation")
    consent = asyncio.run(_consent(ask["url"], state, "e2e-auth-return-reconsent-zh.png"))
    log("consent", **consent)
    _check(consent["phase"] == "done", "re-consent bound", body=consent["body"])
    status = _status(client, agent_id)
    _check(status["status"] == "authorized" and not status["force_reauth"],
           "authorized again, revocation cleared", grant=status)
    events = _chat(client, agent_id, "Who am I? Use the userinfo tool and report the claims.",
                   session)
    _check(_ask(events) is None and USERNAME in _text(events), "tool works after re-consent")
    grants = client.get("/api/identity/grants").json()["grants"]
    log("my_grants", grants=grants)

    # Consent Portal (as_user Gateway targets): read-only probe. Creating one needs
    # an execution role (no IAM creation allowed here) and a JWT gateway.
    from app.services.agentcore.consent_portal import list_portals

    log("consent_portal.probe", portals=[p.get("name") for p in list_portals(_control())])
    log("RUN PASSED")
    return 0


# ── down: delete everything and prove it ─────────────────────────────────────


def _gone(fn: Any, exc: type[BaseException], tries: int = 60, pause: float = 5) -> bool:
    for _ in range(tries):
        try:
            fn()
        except exc:
            return True
        time.sleep(pause)  # nosemgrep: arbitrary-sleep — paced real-AWS polling
    return False


def down(base: str | None) -> int:
    state = _load_state()
    ok = True
    control = _control()
    if base:
        client = e2e_client(base, timeout=300)
        agent = _find_agent(client)
        if agent is not None:
            client.delete(f"/api/agents/{agent['id']}").raise_for_status()
            log("agent.deleted", agent_id=agent["id"])
        res = client.delete(f"/api/identity/connections/oauth2/{CONNECTION}")
        log("connection.delete", status=res.status_code)
    runtime_id = state.get("runtime_id")
    if runtime_id:
        gone = _gone(lambda: control.get_agent_runtime(agentRuntimeId=runtime_id),
                     control.exceptions.ResourceNotFoundException)
        log("runtime.gone" if gone else "runtime.STILL_PRESENT", runtime_id=runtime_id)
        ok &= gone
        try:
            control.delete_workload_identity(name=runtime_id)
            log("workload_identity.deleted", name=runtime_id)
        except control.exceptions.ResourceNotFoundException:
            log("workload_identity.gone", name=runtime_id)
        except Exception as exc:  # noqa: BLE001 — service-linked: goes with the runtime
            log("workload_identity.delete_refused", name=runtime_id, error=str(exc)[:200])
    # direct fallback in case the backend is gone (a no-op once already deleted)
    try:
        control.delete_oauth2_credential_provider(name=CONNECTION)
    except control.exceptions.ResourceNotFoundException:
        pass
    ok &= _gone(lambda: control.get_oauth2_credential_provider(name=CONNECTION),
                control.exceptions.ResourceNotFoundException, tries=12)
    log("credential_provider.gone", name=CONNECTION)

    s3 = _ws().client("s3")
    bucket = (_ws().resources or {}).get("artifacts_bucket") or os.environ.get("LPID_BUCKET")
    if bucket:
        prefix = f"agents/{AGENT_NAME}/"
        listed = s3.list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents", [])
        for key in [o["Key"] for o in listed]:
            s3.delete_object(Bucket=bucket, Key=key)
            log("artifact.deleted", key=f"s3://{bucket}/{key}")
        left = s3.list_objects_v2(Bucket=bucket, Prefix=prefix).get("KeyCount", 0)
        log("artifact.gone" if not left else "artifact.STILL_PRESENT", prefix=prefix, left=left)
        ok &= not left

    cognito = _cognito()
    pool_id = state.get("pool_id")
    if pool_id:
        detail = cognito.describe_user_pool(UserPoolId=pool_id)["UserPool"]
        if detail["Name"] != POOL_NAME:  # never delete anything this script did not create
            raise SystemExit(f"refusing: {pool_id} is {detail['Name']!r}")
        if state.get("domain"):
            cognito.delete_user_pool_domain(Domain=state["domain"], UserPoolId=pool_id)
            log("cognito.domain.deleted", domain=state["domain"])
        cognito.delete_user_pool(UserPoolId=pool_id)
        gone = _gone(lambda: cognito.describe_user_pool(UserPoolId=pool_id),
                     cognito.exceptions.ResourceNotFoundException, tries=12)
        log("cognito.pool.gone" if gone else "cognito.pool.STILL_PRESENT", pool_id=pool_id)
        ok &= gone
    pools = [p["Name"] for p in cognito.list_user_pools(MaxResults=60)["UserPools"]]
    left_pools = [p for p in pools if p.startswith(PREFIX)]
    log("cognito.pools.lpid_left", pools=left_pools)
    ok &= not left_pools
    if STATE_FILE.exists():
        STATE_FILE.unlink()
        log("state_file.deleted", path=str(STATE_FILE))
    log("DOWN OK" if ok else "DOWN INCOMPLETE")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("up")
    run_p = sub.add_parser("run")
    run_p.add_argument("--base", required=True)
    run_p.add_argument("--ui", required=True)
    run_p.add_argument("--timeout", type=int, default=1500)
    down_p = sub.add_parser("down")
    down_p.add_argument("--base")
    args = parser.parse_args()
    if args.cmd == "up":
        return up()
    if args.cmd == "run":
        return run(args.base, args.ui, args.timeout)
    return down(args.base)


if __name__ == "__main__":
    sys.exit(main())
