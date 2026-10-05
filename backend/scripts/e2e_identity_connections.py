#!/usr/bin/env python3
"""E2E: Connections (credential providers) + a Gateway target bound as_agent.

Hits REAL AWS. Every resource it creates is named `lpid-e2e-*` and is deleted by
`down` / the `run` finally-block; nothing existing is modified. The target lives
on a throwaway `lpid-e2e-gw` gateway (never the shared `launchpad-gw`), so the
backend under test must be started with that gateway as its workspace gateway:

    uv run python scripts/e2e_identity_connections.py gateway-up --role-arn <arn>
    LAUNCHPAD_DATABASE_URL=sqlite:////tmp/x.db \\
    LAUNCHPAD_RESOURCES='{"gateway_id": "<printed id>"}' \\
        uv run uvicorn app.main:app --port 8791
    uv run python scripts/e2e_identity_connections.py run --base http://127.0.0.1:8791
    uv run python scripts/e2e_identity_connections.py gateway-down --gateway-id <id>

`run` creates an API-key and an OAuth2 Connection, one OpenAPI target bound to
each (as_agent), checks the read-back binding and the 409s, then deletes them all
and proves each is gone (`--keep` skips the teardown so screenshots can be taken;
`cleanup` finishes it later). The role passed to `gateway-up` is only referenced,
never modified.
"""

import argparse
import json
import sys
import time
from typing import Any

from _e2e_client import e2e_client

PREFIX = "lpid-e2e-"
GATEWAY_NAME = f"{PREFIX}gw"
API_KEY_CONN = f"{PREFIX}apikey"
OAUTH_CONN = f"{PREFIX}oauth"
TARGET_OAUTH = f"{PREFIX}crm-oauth"
TARGET_KEY = f"{PREFIX}facts-key"

OPENAPI = json.dumps(
    {
        "openapi": "3.0.0",
        "info": {"title": "lpid e2e", "version": "1.0.0"},
        "servers": [{"url": "https://example.com"}],
        "paths": {
            "/ping": {
                "get": {
                    "operationId": "ping",
                    "summary": "Ping",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
)


def log(event: str, **data: Any) -> None:
    print(
        json.dumps(
            {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **data}
        )
    )
    sys.stdout.flush()


def _control() -> Any:
    from app.services.agentcore.client import control_client
    from app.services.workspace import default_workspace_context

    return control_client(default_workspace_context())


def gateway_up(role_arn: str) -> int:
    control = _control()
    existing = [g for g in control.list_gateways().get("items", []) if g["name"] == GATEWAY_NAME]
    if existing:
        gateway_id = existing[0]["gatewayId"]
    else:
        created = control.create_gateway(
            name=GATEWAY_NAME,
            description="Launchpad identity P1 e2e (temporary)",
            roleArn=role_arn,
            protocolType="MCP",
            authorizerType="AWS_IAM",
        )
        gateway_id = created["gatewayId"]
    for _ in range(60):
        status = control.get_gateway(gatewayIdentifier=gateway_id)["status"]
        if status in ("READY", "FAILED"):
            break
        time.sleep(5)
    log("gateway.up", gateway_id=gateway_id, status=status)
    return 0 if status == "READY" else 1


def gateway_down(gateway_id: str) -> int:
    control = _control()
    detail = control.get_gateway(gatewayIdentifier=gateway_id)
    if detail["name"] != GATEWAY_NAME:  # never delete anything this script did not create
        raise SystemExit(f"refusing: {gateway_id} is {detail['name']!r}, not {GATEWAY_NAME!r}")
    for target in control.list_gateway_targets(gatewayIdentifier=gateway_id).get("items", []):
        control.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=target["targetId"])
        log("gateway.target.deleted", target_id=target["targetId"], name=target["name"])
    for _ in range(60):
        if not control.list_gateway_targets(gatewayIdentifier=gateway_id).get("items"):
            break
        time.sleep(5)
    control.delete_gateway(gatewayIdentifier=gateway_id)
    for _ in range(60):
        try:
            control.get_gateway(gatewayIdentifier=gateway_id)
        except control.exceptions.ResourceNotFoundException:
            log("gateway.gone", gateway_id=gateway_id)
            return 0
        time.sleep(5)
    log("gateway.still_present", gateway_id=gateway_id)
    return 1


def _check(cond: bool, message: str, **data: Any) -> None:
    log("assert.ok" if cond else "assert.FAIL", message=message, **data)
    if not cond:
        raise AssertionError(message)


def _connections(client: Any) -> dict[str, dict[str, Any]]:
    res = client.get("/api/identity/connections")
    res.raise_for_status()
    return {f"{c['kind']}:{c['name']}": c for c in res.json()["connections"]}


def _targets(client: Any) -> dict[str, dict[str, Any]]:
    res = client.get("/api/identity/gateway-targets")
    res.raise_for_status()
    return {t["name"]: t for t in res.json()["targets"]}


def _wait_target(client: Any, name: str) -> dict[str, Any]:
    for _ in range(40):
        target = _targets(client).get(name)
        if target and target["status"] not in ("CREATING", "UPDATING"):
            return target
        time.sleep(3)
    raise AssertionError(f"target {name} never settled")


def create_all(client: Any, discovery_url: str) -> None:
    res = client.post(
        "/api/identity/connections/api-key",
        json={
            "name": API_KEY_CONN,
            "description": "e2e API key",
            "api_key": "lpid-e2e-not-a-real-key",
        },
    )
    _check(res.status_code == 201, "api-key Connection created", status=res.status_code)
    body = res.json()
    log("connection.created", kind="api_key", name=body["name"], arn=body["arn"])
    _check(
        "api_key" not in body and "lpid-e2e-not-a-real-key" not in res.text, "secret never echoed"
    )

    dup = client.post(
        "/api/identity/connections/api-key", json={"name": API_KEY_CONN, "api_key": "x"}
    )
    _check(
        dup.status_code == 409 and dup.json().get("code") == "identity.connection_exists",
        "duplicate name answers 409 identity.connection_exists",
        status=dup.status_code,
        code=dup.json().get("code"),
    )

    res = client.post(
        "/api/identity/connections/oauth2",
        json={
            "name": OAUTH_CONN,
            "vendor": "CustomOauth2",
            "template": "cognito",
            "description": "e2e OAuth2 (client credentials)",
            "client_id": "lpid-e2e-placeholder-client",
            "client_secret": "lpid-e2e-placeholder-secret",
            "discovery_url": discovery_url,
            "scopes": ["lpid-e2e/read"],
        },
    )
    _check(
        res.status_code == 201,
        "OAuth2 Connection created",
        status=res.status_code,
        body=res.text[:300],
    )
    body = res.json()
    log(
        "connection.created",
        kind="oauth2",
        name=body["name"],
        arn=body["arn"],
        callback_url=body["callback_url"],
    )
    _check(bool(body["callback_url"]), "OAuth2 Connection returns a callback URL")
    _check("placeholder-secret" not in res.text, "client secret never echoed")

    listed = _connections(client)
    for key in (f"api_key:{API_KEY_CONN}", f"oauth2:{OAUTH_CONN}"):
        _check(
            listed.get(key, {}).get("source") == "launchpad",
            f"{key} listed as launchpad/ready",
            status=listed.get(key, {}).get("status"),
        )

    res = client.post(
        "/api/identity/gateway-targets",
        json={
            "name": TARGET_OAUTH,
            "source": "openapi",
            "openapi_schema": OPENAPI,
            "connection": OAUTH_CONN,
            "kind": "oauth2",
            "mode": "as_agent",
            "scopes": ["lpid-e2e/read"],
        },
    )
    _check(
        res.status_code == 201,
        "OpenAPI target bound to the OAuth2 Connection",
        status=res.status_code,
        body=res.text[:400],
    )
    log(
        "target.created",
        **{k: res.json().get(k) for k in ("target_id", "name", "connection", "mode", "auth")},
    )

    res = client.post(
        "/api/identity/gateway-targets",
        json={
            "name": TARGET_KEY,
            "source": "openapi",
            "openapi_schema": OPENAPI,
            "connection": API_KEY_CONN,
            "kind": "api_key",
            "mode": "as_agent",
            "api_key": {"location": "HEADER", "parameter_name": "X-Api-Key"},
        },
    )
    _check(
        res.status_code == 201,
        "OpenAPI target bound to the API-key Connection",
        status=res.status_code,
        body=res.text[:400],
    )
    log(
        "target.created",
        **{k: res.json().get(k) for k in ("target_id", "name", "connection", "mode", "auth")},
    )

    for name, conn, auth in (
        (TARGET_OAUTH, OAUTH_CONN, "oauth2"),
        (TARGET_KEY, API_KEY_CONN, "api_key"),
    ):
        target = _wait_target(client, name)
        log("target.readback", **target)
        _check(
            target["connection"] == conn
            and target["mode"] == "as_agent"
            and target["auth"] == auth,
            f"{name} reads back bound to {conn} as_agent",
        )

    listed = _connections(client)
    refs = [r["name"] for r in listed[f"oauth2:{OAUTH_CONN}"]["referenced_by"]]
    _check(TARGET_OAUTH in refs, "OAuth2 Connection lists the target in referenced_by", refs=refs)
    res = client.delete(f"/api/identity/connections/oauth2/{OAUTH_CONN}")
    _check(
        res.status_code == 409 and res.json().get("code") == "identity.connection_referenced",
        "deleting a referenced Connection answers 409 identity.connection_referenced",
        status=res.status_code,
    )


def cleanup(client: Any) -> bool:
    ok = True
    targets = _targets(client)
    for name in (TARGET_OAUTH, TARGET_KEY):
        if name in targets:
            res = client.delete(f"/api/identity/gateway-targets/{targets[name]['target_id']}")
            log("target.delete", name=name, status=res.status_code)
    for _ in range(40):
        if not {TARGET_OAUTH, TARGET_KEY} & set(_targets(client)):
            break
        time.sleep(3)
    remaining = sorted({TARGET_OAUTH, TARGET_KEY} & set(_targets(client)))
    log("targets.remaining", names=remaining)
    ok &= not remaining
    for kind, name in (("oauth2", OAUTH_CONN), ("api_key", API_KEY_CONN)):
        if f"{kind}:{name}" in _connections(client):
            res = client.delete(f"/api/identity/connections/{kind}/{name}")
            log(
                "connection.delete",
                kind=kind,
                name=name,
                status=res.status_code,
                body=res.text[:200],
            )
        res = client.get(f"/api/identity/connections/{kind}/{name}")
        log(
            "connection.readback_after_delete",
            kind=kind,
            name=name,
            status=res.status_code,
            code=res.json().get("code"),
        )
        ok &= res.status_code == 404
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("gateway-up")
    up.add_argument("--role-arn", required=True)
    down = sub.add_parser("gateway-down")
    down.add_argument("--gateway-id", required=True)
    for name in ("run", "cleanup"):
        p = sub.add_parser(name)
        p.add_argument("--base", default="http://127.0.0.1:8791")
    sub.choices["run"].add_argument("--discovery-url", required=True)
    sub.choices["run"].add_argument("--keep", action="store_true")
    args = parser.parse_args()

    if args.cmd == "gateway-up":
        return gateway_up(args.role_arn)
    if args.cmd == "gateway-down":
        return gateway_down(args.gateway_id)
    client = e2e_client(args.base, timeout=120)
    if args.cmd == "cleanup":
        return 0 if cleanup(client) else 1
    try:
        create_all(client, args.discovery_url)
    except Exception as exc:  # teardown still runs below
        log("run.failed", error=f"{type(exc).__name__}: {exc}")
        args.keep = False
        cleanup(client)
        return 1
    if args.keep:
        log("run.kept", note="run `cleanup` to delete")
        return 0
    return 0 if cleanup(client) else 1


if __name__ == "__main__":
    sys.exit(main())
