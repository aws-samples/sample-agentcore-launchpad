#!/usr/bin/env python3
"""E2E: inbound JWT switch in place, bearer vs SigV4, and on-behalf-of (identity P3).

Hits REAL AWS. Every resource is named `lpid-e2e-p3-*` and is deleted by `down`;
nothing existing is modified. The IdP is a NEW throwaway Cognito user pool (never
`launchpad-users`); the OBO gateway is a NEW throwaway CUSTOM_JWT gateway that only
*references* the existing `launchpad-gateway-role`:

    uv run python scripts/e2e_identity_inbound.py up --gateway-role-arn ARN
    LAUNCHPAD_DATABASE_URL=sqlite:////tmp/lpid-e2e-p3.db \\
    LAUNCHPAD_RESOURCES='{"execution_role_arn": "...", "artifacts_bucket": "...",
                          "gateway_id": "<lpid-e2e-p3-gw id printed by up>"}' \\
        uv run uvicorn app.main:app --port 8793
    LAUNCHPAD_DATABASE_URL=sqlite:////tmp/lpid-e2e-p3.db LAUNCHPAD_RESOURCES='…same…' \\
        uv run python scripts/e2e_identity_inbound.py run --base http://127.0.0.1:8793
    uv run python scripts/e2e_identity_inbound.py down --base http://127.0.0.1:8793
    uv run python scripts/e2e_identity_inbound.py trail   # CloudTrail, ~15 min later

`run` proves, on ONE zip Runtime:

1. deployed IAM → SigV4 InvokeAgentRuntime answers;
2. switched to JWT in place (POST /api/agents/{id}/inbound-auth): same runtime id,
   a higher version, the live authorizer is the pinned customJWTAuthorizer;
3. a user JWT and an M2M JWT from the pool invoke over HTTPS bearer; SigV4 is
   refused (403); the backend's own invoke chain (the Chat "invoke as me" path)
   succeeds with the user JWT and the scoped memory actor;
4. a re-publish of the JWT agent keeps the authorizer (acceptance ②, live);
5. switched back to IAM in place: same runtime, higher version, no authorizer,
   SigV4 answers again and a bearer is refused;
6. OBO: the pool's /oauth2/token rejects the RFC 8693 grant; a Cognito Connection
   with `obo` is refused 422; a CustomOauth2 Connection with an OBO config is
   accepted by AWS and echoed; an obo target on it is created on the CUSTOM_JWT
   gateway (grantType TOKEN_EXCHANGE) and reads back as mode obo; an obo target on
   a Connection without OBO is refused 422.
"""

import argparse
import base64
import json
import os
import secrets
import sys
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

import httpx
from _e2e_client import e2e_client

PREFIX = "lpid-e2e-p3-"
POOL_NAME = f"{PREFIX}pool"
M2M_CLIENT = f"{PREFIX}m2m"
USER_CLIENT = f"{PREFIX}user-client"
RESOURCE_SERVER = "lpid-e2e-p3"
SCOPE = f"{RESOURCE_SERVER}/invoke"
USERNAME = f"{PREFIX}user"
AGENT_NAME = f"{PREFIX}agent"
GATEWAY_NAME = f"{PREFIX}gw"
COGNITO_CONN = f"{PREFIX}cognito"
OBO_CONN = f"{PREFIX}obo"
OBO_TARGET = f"{PREFIX}obo-target"
REFUSED_TARGET = f"{PREFIX}refused-target"
STATE_FILE = Path("/tmp/lpid-e2e-p3-state.json")
EXCHANGE_URN = "urn:ietf:params:oauth:grant-type:token-exchange"
OPENAPI = json.dumps({
    "openapi": "3.0.1",
    "info": {"title": "lpid e2e obo", "version": "1"},
    "servers": [{"url": "https://lpid-e2e-p3-api.example.com"}],
    "paths": {"/me": {"get": {"operationId": "whoAmI", "summary": "the caller",
                              "responses": {"200": {"description": "ok"}}}}},
})


def log(event: str, **data: Any) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(json.dumps({"ts": stamp, "event": event, **data}, default=str))
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


def _wait_gateway(control: Any, gateway_id: str) -> str:
    status = ""
    for _ in range(60):
        status = control.get_gateway(gatewayIdentifier=gateway_id)["status"]
        if status in ("READY", "FAILED"):
            break
        time.sleep(5)  # nosemgrep: arbitrary-sleep — paced real-AWS polling
    return status


# ── up: throwaway pool + CUSTOM_JWT gateway ──────────────────────────────────


def up(gateway_role_arn: str) -> int:
    cognito = _cognito()
    state = _load_state()
    if not state.get("pool_id"):
        pool = cognito.create_user_pool(
            PoolName=POOL_NAME,
            AdminCreateUserConfig={"AllowAdminCreateUserOnly": True},
            AutoVerifiedAttributes=[],
            UserPoolTags={"purpose": "launchpad-identity-v2-p3-e2e"},
        )["UserPool"]
        state["pool_id"] = pool["Id"]
        _save_state(state)
    pool_id = state["pool_id"]
    region = pool_id.split("_", 1)[0]
    if not state.get("domain"):
        domain = f"{PREFIX}{secrets.token_hex(3)}"
        cognito.create_user_pool_domain(Domain=domain, UserPoolId=pool_id)
        state["domain"] = domain
        _save_state(state)
    if not state.get("resource_server"):
        cognito.create_resource_server(
            UserPoolId=pool_id, Identifier=RESOURCE_SERVER, Name=RESOURCE_SERVER,
            Scopes=[{"ScopeName": "invoke", "ScopeDescription": "invoke the e2e runtime"}],
        )
        state["resource_server"] = RESOURCE_SERVER
        _save_state(state)
    if not state.get("m2m_client_id"):
        client = cognito.create_user_pool_client(
            UserPoolId=pool_id, ClientName=M2M_CLIENT, GenerateSecret=True,
            AllowedOAuthFlows=["client_credentials"], AllowedOAuthScopes=[SCOPE],
            AllowedOAuthFlowsUserPoolClient=True, SupportedIdentityProviders=["COGNITO"],
        )["UserPoolClient"]
        state["m2m_client_id"] = client["ClientId"]
        state["m2m_client_secret"] = client["ClientSecret"]
        _save_state(state)
    if not state.get("user_client_id"):
        client = cognito.create_user_pool_client(
            UserPoolId=pool_id, ClientName=USER_CLIENT, GenerateSecret=False,
            ExplicitAuthFlows=["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
        )["UserPoolClient"]
        state["user_client_id"] = client["ClientId"]
        _save_state(state)
    if not state.get("password"):
        password = f"Lpid-{secrets.token_urlsafe(12)}!9a"
        cognito.admin_create_user(
            UserPoolId=pool_id, Username=USERNAME, MessageAction="SUPPRESS",
            UserAttributes=[{"Name": "email", "Value": "lpid-e2e-p3@example.com"},
                            {"Name": "email_verified", "Value": "true"}],
        )
        cognito.admin_set_user_password(
            UserPoolId=pool_id, Username=USERNAME, Password=password, Permanent=True
        )
        state["password"] = password
        _save_state(state)
    state["discovery_url"] = (
        f"https://cognito-idp.{region}.amazonaws.com/{pool_id}/.well-known/openid-configuration"
    )
    state["token_url"] = f"https://{state['domain']}.auth.{region}.amazoncognito.com/oauth2/token"
    _save_state(state)
    log("idp.up", pool_id=pool_id, domain=state["domain"], m2m_client=state["m2m_client_id"],
        user_client=state["user_client_id"], user=USERNAME)

    control = _control()
    if not state.get("gateway_id"):
        existing = [g for g in control.list_gateways().get("items", [])
                    if g["name"] == GATEWAY_NAME]
        if existing:
            state["gateway_id"] = existing[0]["gatewayId"]
        else:
            created = control.create_gateway(
                name=GATEWAY_NAME,
                description="Launchpad identity P3 OBO e2e (temporary)",
                roleArn=gateway_role_arn,
                protocolType="MCP",
                authorizerType="CUSTOM_JWT",
                authorizerConfiguration={"customJWTAuthorizer": {
                    "discoveryUrl": state["discovery_url"],
                    "allowedClients": [state["m2m_client_id"], state["user_client_id"]],
                }},
            )
            state["gateway_id"] = created["gatewayId"]
        _save_state(state)
    status = _wait_gateway(control, state["gateway_id"])
    log("gateway.up", gateway_id=state["gateway_id"], status=status,
        authorizer=control.get_gateway(gatewayIdentifier=state["gateway_id"])["authorizerType"])
    return 0 if status == "READY" else 1


# ── tokens + direct callers ──────────────────────────────────────────────────


def _m2m_token(state: dict[str, Any]) -> str:
    basic = base64.b64encode(
        f"{state['m2m_client_id']}:{state['m2m_client_secret']}".encode()).decode()
    res = httpx.post(state["token_url"], data={"grant_type": "client_credentials",
                                               "scope": SCOPE},
                     headers={"Authorization": f"Basic {basic}"}, timeout=20)
    res.raise_for_status()
    return res.json()["access_token"]


def _user_token(state: dict[str, Any]) -> str:
    result = _cognito().initiate_auth(
        ClientId=state["user_client_id"], AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": USERNAME, "PASSWORD": state["password"]},
    )["AuthenticationResult"]
    return result["AccessToken"]


def _claims(token: str) -> dict[str, Any]:
    body = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


def _bearer(arn: str, token: str, prompt: str) -> tuple[int, str]:
    region = arn.split(":")[3]
    url = (f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
           f"{urllib.parse.quote(arn, safe='')}/invocations?qualifier=DEFAULT")
    res = httpx.post(url, headers={
        "Authorization": f"Bearer {token}",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": uuid.uuid4().hex + uuid.uuid4().hex,
    }, json={"prompt": prompt, "actor_id": "lpid-e2e-direct"}, timeout=300)
    return res.status_code, res.text[:300]


def _sigv4(arn: str, prompt: str) -> tuple[str, str]:
    data = _ws().client("bedrock-agentcore")
    try:
        res = data.invoke_agent_runtime(
            agentRuntimeArn=arn, qualifier="DEFAULT",
            runtimeSessionId=uuid.uuid4().hex + uuid.uuid4().hex,
            payload=json.dumps({"prompt": prompt, "actor_id": "lpid-e2e-direct"}).encode(),
        )
        return "ok", res["response"].read()[:200].decode(errors="replace")
    except Exception as exc:  # noqa: BLE001 — the refusal IS the evidence
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__)
        status = getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
        return f"{code} ({status})", str(exc)[:300]


# ── run ──────────────────────────────────────────────────────────────────────


def _wait_active(client: Any, agent_id: str, timeout: int) -> dict[str, Any]:
    seen: set[tuple[str, str, str]] = set()
    deadline = time.time() + timeout
    agent: dict[str, Any] = {}
    time.sleep(5)  # nosemgrep: arbitrary-sleep — let the job flip the row to deploying
    while time.time() < deadline:
        agent = client.get(f"/api/agents/{agent_id}").json()
        dep = agent["deployments"][0]
        for stage in dep["stages"]:
            key = (dep["id"], stage["name"], stage["status"])
            if key not in seen and stage["status"] != "pending":
                seen.add(key)
                log("deploy.stage", name=stage["name"], status=stage["status"],
                    detail=stage["detail"][:160])
        if agent["status"] in ("active", "failed"):
            break
        time.sleep(10)  # nosemgrep: arbitrary-sleep — paced real-AWS polling
    return agent


def _live(runtime_id: str) -> dict[str, Any]:
    rt = _control().get_agent_runtime(agentRuntimeId=runtime_id)
    return {"version": rt["agentRuntimeVersion"], "status": rt["status"],
            "authorizer": rt.get("authorizerConfiguration")}


def _find_agent(client: Any) -> dict[str, Any] | None:
    agents = client.get("/api/agents").json()["agents"]
    return next((a for a in agents if a["name"] == AGENT_NAME and a["status"] != "deleted"), None)


def _spec(inbound: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": AGENT_NAME,
        "method": "zip_runtime",
        "system_prompt": "You are a terse test agent. Answer in at most ten words.",
        "memory": {"short_term": False, "long_term": False},
        "inbound_auth": inbound,
    }


def run(base: str, timeout: int) -> int:
    state = _load_state()
    if not state.get("gateway_id"):
        raise SystemExit("run `up` first")
    client = e2e_client(base, timeout=300)
    jwt_pin = {"mode": "jwt", "jwt": {
        "discovery_url": state["discovery_url"],
        "allowed_clients": [state["m2m_client_id"], state["user_client_id"]],
    }}

    # (1) IAM deploy
    log("step", n=1, what="deploy zip Runtime with inbound IAM")
    agent = _find_agent(client)
    if agent is None:
        res = client.post("/api/agents", json=_spec({"mode": "iam"}))
        _check(res.status_code == 202, "IAM agent accepted", status=res.status_code,
               body=res.text[:300])
        agent = res.json()["agent"]
    agent_id = agent["id"]
    agent = _wait_active(client, agent_id, timeout)
    _check(agent["status"] == "active", "IAM agent active", status=agent["status"])
    arn = agent["arn"]
    runtime_id = arn.rsplit("/", 1)[-1]
    state.update(agent_id=agent_id, runtime_id=runtime_id, arn=arn)
    _save_state(state)
    v_iam = _live(runtime_id)
    log("runtime.iam", runtime_id=runtime_id, **v_iam)
    _check(not v_iam["authorizer"], "no authorizer while IAM")
    outcome, body = _sigv4(arn, "Say ok.")
    _check(outcome == "ok", "SigV4 answers an IAM runtime", body=body)

    # (2) switch to JWT in place
    log("step", n=2, what="one-click switch IAM → JWT, same runtime")
    res = client.post(f"/api/agents/{agent_id}/inbound-auth", json={"inbound_auth": jwt_pin})
    _check(res.status_code == 202, "switch to JWT accepted", status=res.status_code,
           body=res.text[:300])
    agent = _wait_active(client, agent_id, timeout)
    _check(agent["status"] == "active" and agent["arn"] == arn,
           "same ARN after the switch", arn=agent["arn"])
    v_jwt = _live(runtime_id)
    log("runtime.jwt", **v_jwt)
    _check(int(v_jwt["version"]) > int(v_iam["version"]), "version increased on the switch",
           before=v_iam["version"], after=v_jwt["version"])
    live_jwt = (v_jwt["authorizer"] or {}).get("customJWTAuthorizer") or {}
    _check(live_jwt.get("discoveryUrl") == state["discovery_url"]
           and sorted(live_jwt.get("allowedClients", []))
           == sorted(jwt_pin["jwt"]["allowed_clients"]),
           "live authorizer = the pinned JWT config", live=live_jwt)
    ident = client.get(f"/api/agents/{agent_id}/identity").json().get("inbound", {})
    log("identity.inbound", mode=ident.get("mode"), pinned=ident.get("pinned"),
        invoke_url=ident.get("invoke_url"))

    # (3) bearer vs SigV4
    log("step", n=3, what="user + M2M bearer succeed, SigV4 refused, backend invoke chain")
    user_tok, m2m_tok = _user_token(state), _m2m_token(state)
    uc, mc = _claims(user_tok), _claims(m2m_tok)
    log("tokens", user={"client_id": uc.get("client_id"), "username": uc.get("username"),
                        "token_use": uc.get("token_use")},
        m2m={"client_id": mc.get("client_id"), "scope": mc.get("scope")})
    code, body = _bearer(arn, user_tok, "Say ok.")
    _check(code == 200, "user JWT bearer → 200", status=code, body=body[:160])
    code, body = _bearer(arn, m2m_tok, "Say ok.")
    _check(code == 200, "M2M JWT bearer → 200", status=code, body=body[:160])
    outcome, body = _sigv4(arn, "Say ok.")
    _check(outcome != "ok" and "403" in outcome, "SigV4 refused by a JWT runtime",
           outcome=outcome, body=body)
    code, body = _bearer(arn, "not-a-jwt", "Say ok.")
    _check(code in (401, 403), "a garbage bearer is refused", status=code, body=body[:200])

    from app.core.db import SessionLocal
    from app.models.ledger import Agent
    from app.services.invoke import invoke_agent_text
    from app.services.memory import scoped_actor

    db = SessionLocal()
    try:
        row = db.get(Agent, agent_id)
        actor = scoped_actor(agent_id, USERNAME)
        result = invoke_agent_text(row, "Say ok.", actor_id=actor, runtime_user_id=USERNAME,
                                   bearer_token=user_tok)
        _check(bool(result.get("text")), "backend invoke chain answers with the user JWT",
               actor_id=actor, text=result.get("text", "")[:120])
    finally:
        db.close()

    # (4) a re-publish of the JWT agent keeps the authorizer
    log("step", n=4, what="re-publish the JWT agent → authorizer kept")
    res = client.post(f"/api/agents/{agent_id}/redeploy", json=_spec(jwt_pin))
    _check(res.status_code == 202, "re-publish accepted", status=res.status_code,
           body=res.text[:300])
    agent = _wait_active(client, agent_id, timeout)
    v_re = _live(runtime_id)
    log("runtime.republished", **v_re)
    _check(int(v_re["version"]) > int(v_jwt["version"]), "version increased on re-publish",
           before=v_jwt["version"], after=v_re["version"])
    _check(v_re["authorizer"] == v_jwt["authorizer"], "authorizer survived the re-publish")

    # (5) back to IAM in place
    log("step", n=5, what="switch JWT → IAM, same runtime")
    res = client.post(f"/api/agents/{agent_id}/inbound-auth",
                      json={"inbound_auth": {"mode": "iam"}})
    _check(res.status_code == 202, "switch to IAM accepted", status=res.status_code)
    agent = _wait_active(client, agent_id, timeout)
    v_back = _live(runtime_id)
    log("runtime.iam_again", **v_back)
    _check(agent["arn"] == arn and int(v_back["version"]) > int(v_re["version"]),
           "same runtime, version increased again", after=v_back["version"])
    _check(not v_back["authorizer"], "authorizer removed")
    outcome, body = _sigv4(arn, "Say ok.")
    _check(outcome == "ok", "SigV4 answers again", body=body)
    code, body = _bearer(arn, _user_token(state), "Say ok.")
    _check(code in (401, 403), "bearer refused by an IAM runtime", status=code, body=body[:200])

    # (6) OBO
    log("step", n=6, what="on-behalf-of: Cognito refusal + AWS-accepted OBO shapes")
    basic = base64.b64encode(
        f"{state['m2m_client_id']}:{state['m2m_client_secret']}".encode()).decode()
    exchange = httpx.post(state["token_url"], data={
        "grant_type": EXCHANGE_URN, "subject_token": _user_token(state),
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
    }, headers={"Authorization": f"Basic {basic}"}, timeout=20)
    log("cognito.token_exchange", status=exchange.status_code, body=exchange.text[:200])
    _check(exchange.status_code == 400 and "unsupported_grant_type" in exchange.text,
           "Cognito /oauth2/token rejects the RFC 8693 grant")
    discovery = httpx.get(state["discovery_url"], timeout=20).json()
    log("cognito.discovery", grant_types_supported=discovery.get("grant_types_supported"))

    res = client.post("/api/identity/connections/oauth2", json={
        "name": COGNITO_CONN, "vendor": "CustomOauth2", "template": "cognito",
        "client_id": state["m2m_client_id"], "client_secret": state["m2m_client_secret"],
        "discovery_url": state["discovery_url"], "scopes": [SCOPE],
        "obo": {"grant_type": "TOKEN_EXCHANGE"},
    })
    _check(res.status_code == 422 and res.json().get("code") == "identity.obo_unsupported",
           "Cognito Connection with obo → 422 identity.obo_unsupported",
           status=res.status_code, body=res.text[:300])

    # the same Connection without obo (as_agent) — the refused-target fixture
    res = client.post("/api/identity/connections/oauth2", json={
        "name": COGNITO_CONN, "vendor": "CustomOauth2", "template": "cognito",
        "client_id": state["m2m_client_id"], "client_secret": state["m2m_client_secret"],
        "discovery_url": state["discovery_url"], "scopes": [SCOPE],
    })
    _check(res.status_code in (201, 409), "plain Cognito Connection created",
           status=res.status_code)

    res = client.post("/api/identity/connections/oauth2", json={
        "name": OBO_CONN, "vendor": "CustomOauth2", "template": "custom_endpoints",
        "description": "lpid e2e OBO shape (temporary; placeholder IdP)",
        "client_id": "lpid-e2e-obo-client", "client_secret": secrets.token_urlsafe(16),
        "issuer": "https://lpid-e2e-p3-idp.example.com",
        "authorization_endpoint": "https://lpid-e2e-p3-idp.example.com/oauth2/authorize",
        "token_endpoint": "https://lpid-e2e-p3-idp.example.com/oauth2/token",
        "scopes": ["api/read"],
        "obo": {"grant_type": "TOKEN_EXCHANGE", "actor_token_content": "NONE"},
    })
    _check(res.status_code in (201, 409), "CustomOauth2 Connection with OBO accepted by AWS",
           status=res.status_code, body=res.text[:300])
    got = _control().get_oauth2_credential_provider(name=OBO_CONN)
    live_obo = (got.get("oauth2ProviderConfigOutput", {}).get("customOauth2ProviderConfig", {})
                .get("onBehalfOfTokenExchangeConfig"))
    _check(bool(live_obo) and live_obo.get("grantType") == "TOKEN_EXCHANGE",
           "GetOauth2CredentialProvider echoes onBehalfOfTokenExchangeConfig", live=live_obo)

    res = client.post("/api/identity/gateway-targets", json={
        "name": REFUSED_TARGET, "source": "openapi", "openapi_schema": OPENAPI,
        "connection": COGNITO_CONN, "kind": "oauth2", "mode": "obo", "scopes": [SCOPE],
    })
    _check(res.status_code == 422 and res.json().get("code") == "identity.obo_unsupported",
           "obo target on a Connection without OBO → 422", status=res.status_code,
           body=res.text[:300])

    res = client.post("/api/identity/gateway-targets", json={
        "name": OBO_TARGET, "source": "openapi", "openapi_schema": OPENAPI,
        "connection": OBO_CONN, "kind": "oauth2", "mode": "obo", "scopes": ["api/read"],
    })
    _check(res.status_code in (201, 409), "obo target created on the CUSTOM_JWT gateway",
           status=res.status_code, body=res.text[:300])
    control = _control()
    target = next(t for t in control.list_gateway_targets(
        gatewayIdentifier=state["gateway_id"]).get("items", []) if t["name"] == OBO_TARGET)
    detail = control.get_gateway_target(gatewayIdentifier=state["gateway_id"],
                                        targetId=target["targetId"])
    oauth = detail["credentialProviderConfigurations"][0]["credentialProvider"][
        "oauthCredentialProvider"]
    log("gateway_target.obo", status=detail.get("status"), grant_type=oauth.get("grantType"),
        provider=oauth.get("providerArn"))
    _check(oauth.get("grantType") == "TOKEN_EXCHANGE", "live target grantType TOKEN_EXCHANGE")
    listed = client.get("/api/identity/gateway-targets").json()["targets"]
    mine = next(t for t in listed if t["name"] == OBO_TARGET)
    _check(mine["mode"] == "obo" and mine["connection"] == OBO_CONN,
           "console lists the target as mode obo", row=mine)
    log("RUN PASSED")
    return 0


# ── down ─────────────────────────────────────────────────────────────────────


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
    role_name = None
    if base:
        client = e2e_client(base, timeout=300)
        agent = _find_agent(client)
        if agent is not None:
            from app.services.agent_iam import role_name_for

            role_name = role_name_for(AGENT_NAME, agent["id"])
            client.delete(f"/api/agents/{agent['id']}").raise_for_status()
            log("agent.deleted", agent_id=agent["id"])
        for t in client.get("/api/identity/gateway-targets").json().get("targets", []):
            if t["name"].startswith(PREFIX):
                res = client.delete(f"/api/identity/gateway-targets/{t['target_id']}")
                log("gateway_target.delete", name=t["name"], status=res.status_code)
        for name in (COGNITO_CONN, OBO_CONN):
            res = client.delete(f"/api/identity/connections/oauth2/{name}")
            log("connection.delete", name=name, status=res.status_code)
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

    gateway_id = state.get("gateway_id")
    if gateway_id:
        try:
            detail = control.get_gateway(gatewayIdentifier=gateway_id)
        except control.exceptions.ResourceNotFoundException:
            detail = None
        if detail is not None:
            if detail["name"] != GATEWAY_NAME:  # never delete what this script did not create
                raise SystemExit(f"refusing: {gateway_id} is {detail['name']!r}")
            for t in control.list_gateway_targets(gatewayIdentifier=gateway_id).get("items", []):
                control.delete_gateway_target(gatewayIdentifier=gateway_id,
                                              targetId=t["targetId"])
                log("gateway.target.deleted", target_id=t["targetId"], name=t["name"])
            for _ in range(60):
                if not control.list_gateway_targets(
                        gatewayIdentifier=gateway_id).get("items"):
                    break
                time.sleep(5)  # nosemgrep: arbitrary-sleep — paced real-AWS polling
            control.delete_gateway(gatewayIdentifier=gateway_id)
            log("gateway.deleted", gateway_id=gateway_id)
        gone = _gone(lambda: control.get_gateway(gatewayIdentifier=gateway_id),
                     control.exceptions.ResourceNotFoundException)
        log("gateway.gone" if gone else "gateway.STILL_PRESENT", gateway_id=gateway_id)
        ok &= gone

    for name in (COGNITO_CONN, OBO_CONN):  # direct fallback; a no-op once deleted
        try:
            control.delete_oauth2_credential_provider(name=name)
        except control.exceptions.ResourceNotFoundException:
            pass
        gone = _gone(lambda n=name: control.get_oauth2_credential_provider(name=n),
                     control.exceptions.ResourceNotFoundException, tries=12)
        log("credential_provider.gone" if gone else "credential_provider.STILL_PRESENT",
            name=name)
        ok &= gone

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

    iam = _ws().client("iam")
    roles = [r["RoleName"] for page in iam.get_paginator("list_roles").paginate()
             for r in page["Roles"] if "lpid-e2e-p3" in r["RoleName"]]
    log("iam.lpid_p3_roles_left", roles=roles, agent_role=role_name)
    ok &= not roles

    cognito = _cognito()
    pool_id = state.get("pool_id")
    if pool_id:
        detail = cognito.describe_user_pool(UserPoolId=pool_id)["UserPool"]
        if detail["Name"] != POOL_NAME:
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
    left_pools = [p for p in pools if p.startswith("lpid-e2e")]
    log("cognito.pools.lpid_left", pools=left_pools)
    ok &= not left_pools
    if STATE_FILE.exists():
        # keep the non-secret ids for `trail`
        keep = {k: state[k] for k in ("agent_id", "runtime_id") if k in state}
        STATE_FILE.unlink()
        Path("/tmp/lpid-e2e-p3-ids.json").write_text(json.dumps(keep))
        log("state_file.deleted", path=str(STATE_FILE))
    log("DOWN OK" if ok else "DOWN INCOMPLETE")
    return 0 if ok else 1


def trail() -> int:
    """CloudTrail proof of the per-agent role's lifecycle (events lag ~5–15 min)."""
    ct = _ws().client("cloudtrail")
    ids = json.loads(Path("/tmp/lpid-e2e-p3-ids.json").read_text())
    from app.services.agent_iam import role_name_for

    role = role_name_for(AGENT_NAME, ids["agent_id"])
    events = ct.lookup_events(
        LookupAttributes=[{"AttributeKey": "ResourceName", "AttributeValue": role}],
        MaxResults=50,
    )["Events"]
    for e in sorted(events, key=lambda e: e["EventTime"]):
        log("cloudtrail", role=role, event=e["EventName"], time=e["EventTime"].isoformat())
    names = {e["EventName"] for e in events}
    return 0 if {"CreateRole", "DeleteRole"} <= names else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    up_p = sub.add_parser("up")
    up_p.add_argument("--gateway-role-arn", required=True)
    run_p = sub.add_parser("run")
    run_p.add_argument("--base", required=True)
    run_p.add_argument("--timeout", type=int, default=1500)
    down_p = sub.add_parser("down")
    down_p.add_argument("--base")
    sub.add_parser("trail")
    args = parser.parse_args()
    if args.cmd == "up":
        return up(args.gateway_role_arn)
    if args.cmd == "run":
        return run(args.base, args.timeout)
    if args.cmd == "down":
        return down(args.base)
    return trail()


if __name__ == "__main__":
    sys.exit(main())
