"""Shared HTTP client for the `e2e_*` scripts — authenticates when the target asks.

The scripts hit a real deployment. A local dev backend usually runs with the login
gate off, but the remote prod box runs `LAUNCHPAD_RUN_MODE=prod` with a password
set, where every `/api/*` call except health/status/login answers
`{"code": "auth.required"}`. This module is the one place that knows how to get a
session, so a script keeps its single `client = e2e_client(...)` line and works
against either.

Two traps this deliberately handles:

* **Credentials are checked before any work.** A 401 discovered halfway through a
  deploy leaves a half-built agent on real AWS, so a missing password aborts the
  run before the first mutating call.
* **The session cookie is `Secure` on prod** — `cookie_secure(settings)` is
  `auth_cookie_secure or run_mode == "prod"` (see `.trellis/spec/launchpad/console-auth.md`
  §3.4). httpx's RFC 6265 jar never attaches a `Secure` cookie to an `http://`
  request, so trusting the jar means login succeeds and every following call 401s
  whenever the base is plain HTTP — which is what these scripts use on the box
  (`http://127.0.0.1:8000`), and the only base the scripts with a hardcoded
  localhost URL can use at all. The token is therefore re-pinned as a plain
  `Cookie` header. That is a test-client relaxation only: `Secure` is a hint to
  the client, the server neither sees nor cares, and the product's cookie posture
  is untouched.
"""

import os
from typing import Any, NoReturn

import httpx

SESSION_COOKIE = "launchpad_session"
# checked in order, so a runner can keep its own creds separate from the ones the
# deployment itself is configured with
USERNAME_VARS = ("LAUNCHPAD_E2E_USERNAME", "LAUNCHPAD_AUTH_USERNAME")
PASSWORD_VARS = ("LAUNCHPAD_E2E_PASSWORD", "LAUNCHPAD_AUTH_PASSWORD")


def _first_env(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _fail(message: str) -> NoReturn:
    raise SystemExit(f"e2e auth: {message}")


def _auth_status(client: httpx.Client) -> dict[str, Any]:
    try:
        response = client.get("/api/auth/status")
    except httpx.HTTPError as exc:
        _fail(f"cannot reach {client.base_url} — {type(exc).__name__}: {exc}")
    if response.status_code != 200:
        _fail(f"GET /api/auth/status returned HTTP {response.status_code}")
    return response.json()


def e2e_client(base: str, timeout: float = 300) -> httpx.Client:
    """Return an `httpx.Client` for `base`, logged in if the deployment requires it.

    Aborts with `SystemExit` — the failure mode the scripts already use — when the
    target needs auth and no usable credentials are available, so nothing is
    created before the problem surfaces.
    """
    client = httpx.Client(base_url=base, timeout=timeout)

    if not _auth_status(client).get("auth_required"):
        return client  # dev deployment: behaves exactly as before this helper existed

    username = _first_env(USERNAME_VARS)
    password = _first_env(PASSWORD_VARS)
    if not (username and password):
        _fail(
            f"{base} requires authentication. Export credentials before running: "
            f"{USERNAME_VARS[0]} / {PASSWORD_VARS[0]} "
            f"(or {USERNAME_VARS[1]} / {PASSWORD_VARS[1]})."
        )

    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    if response.status_code != 200:
        # the body carries the error code; the password never gets echoed
        code = ""
        try:
            code = f" ({response.json().get('code')})"
        except ValueError:
            pass
        _fail(f"login as {username!r} failed with HTTP {response.status_code}{code}")

    token = client.cookies.get(SESSION_COOKIE)
    if not token:
        _fail(
            f"login succeeded but no {SESSION_COOKIE} cookie came back — "
            "the auth contract changed"
        )
    # drop the jar copy (it may be flagged Secure and would then be skipped on an
    # http:// base) and carry the session as an explicit header instead
    client.cookies.clear()
    client.headers["Cookie"] = f"{SESSION_COOKIE}={token}"

    status = _auth_status(client)
    if not status.get("authenticated"):
        _fail(
            "session did not survive the login round-trip "
            f"(auth_required={status.get('auth_required')}, "
            f"authenticated={status.get('authenticated')}) — the session cookie is "
            "not reaching the backend"
        )
    return client
