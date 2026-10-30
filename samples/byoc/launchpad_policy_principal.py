# <launchpad-principal-carrier:v1>  # --- CN per-user principal (stream A3) ---
# The hub mints a short-lived principal token per turn (who the agent acts for) and sends it
# as payload["launchpad"]["principal"]. This block keeps it for the turn and puts it into
# params._meta["launchpad/principal"] of every Gateway MCP tools/call and tools/list the
# SigV4 transport sends (contract I2), plus Global's policy-session header. The token is a
# bearer credential: it is never logged, never shown to the model, never put into memory.
# One AgentCore Runtime session runs in its own microVM and the MCP client signs requests on
# its own background thread, so the token is held per process (not in a context variable) —
# and only for the TURN: set when the turn starts, cleared when it ends (a `finally`), so a
# later turn never inherits it; a turn without a token clears the previous one at its start.
#
# BYOC: this file ships verbatim as samples/byoc/launchpad_policy_principal.py. Run every
# invocation inside `with launchpad_principal_scope(payload):` (or wrap an async-generator
# entrypoint's events in `launchpad_principal_turn(payload, events)`), then either pass the
# outgoing JSON-RPC body of each Gateway call through `launchpad_principal_body(body)` before
# signing it, or — with an httpx-based SigV4 auth flow — sign `_launchpad_with_principal(request)`.
import base64 as _lp_principal_b64
import contextlib as _lp_principal_contextlib
import json as _lp_principal_json
import threading as _lp_principal_threading

LAUNCHPAD_PRINCIPAL_META_KEY = "launchpad/principal"
LAUNCHPAD_POLICY_SESSION_HEADER = "x-amzn-bedrock-agentcore-policy-session-id"
_LAUNCHPAD_PRINCIPAL_METHODS = ("tools/call", "tools/list")
_launchpad_principal_lock = _lp_principal_threading.Lock()
_launchpad_principal_state = {"token": None, "psid": None}


def _launchpad_principal_psid(token):
    """The token's psid claim (read without trust — the Gateway interceptor verifies)."""
    try:
        segment = token.split(".")[1]
        claims = _lp_principal_json.loads(
            _lp_principal_b64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
        psid = claims.get("psid") if isinstance(claims, dict) else None
        return psid if isinstance(psid, str) else None
    except Exception:
        return None


def launchpad_set_principal(payload):
    """The turn starts: this turn's token from the request payload, or none (which clears a
    previous turn's). Returns the token set — hand it to ``launchpad_clear_principal``."""
    token = None
    if isinstance(payload, dict) and isinstance(payload.get("launchpad"), dict):
        value = payload["launchpad"].get("principal")
        token = value if isinstance(value, str) and value else None
    with _launchpad_principal_lock:
        _launchpad_principal_state["token"] = token
        _launchpad_principal_state["psid"] = _launchpad_principal_psid(token) if token else None
    return token


def launchpad_clear_principal(token=None):
    """The turn ends: forget its token — unless another turn has set its own since."""
    with _launchpad_principal_lock:
        if token is None or _launchpad_principal_state["token"] == token:
            _launchpad_principal_state["token"] = None
            _launchpad_principal_state["psid"] = None


@_lp_principal_contextlib.contextmanager
def launchpad_principal_scope(payload):
    """``with launchpad_principal_scope(payload):`` around one invocation's work."""
    token = launchpad_set_principal(payload)
    try:
        yield token
    finally:
        launchpad_clear_principal(token)


async def launchpad_principal_turn(payload, events):
    """An async-generator entrypoint's events with the turn's token set while they run."""
    token = launchpad_set_principal(payload)
    try:
        async for event in events:
            yield event
    finally:
        launchpad_clear_principal(token)


def _launchpad_principal_current():
    with _launchpad_principal_lock:
        return _launchpad_principal_state["token"], _launchpad_principal_state["psid"]


def _launchpad_principal_message(message, token):
    if isinstance(message, dict) and message.get("method") in _LAUNCHPAD_PRINCIPAL_METHODS:
        params = message.get("params")
        if params is None:
            params = message["params"] = {}
        if isinstance(params, dict):
            meta = params.get("_meta")
            if not isinstance(meta, dict):
                meta = params["_meta"] = {}
            meta[LAUNCHPAD_PRINCIPAL_META_KEY] = token
            return True
    return False


def launchpad_principal_body(body):
    """``body`` (a JSON-RPC message or batch, bytes) with the token in ``_meta`` of every
    tools/call / tools/list — or ``body`` unchanged (no token, other methods, not JSON)."""
    token, _psid = _launchpad_principal_current()
    if not token or not body:
        return body
    try:
        data = _lp_principal_json.loads(body)
    except Exception:
        return body
    changed = False
    for message in data if isinstance(data, list) else [data]:
        changed = _launchpad_principal_message(message, token) or changed
    if not changed:
        return body
    return _lp_principal_json.dumps(data, separators=(",", ":")).encode("utf-8")


def _launchpad_with_principal(request):
    """The httpx request the SigV4 auth flow signs: the carrier body + the psid header — both
    on tools/call and tools/list only; any other request is signed unchanged."""
    import httpx  # the MCP client's own transport library (a dependency of `mcp`)

    token, psid = _launchpad_principal_current()
    if not token:
        return request
    body = launchpad_principal_body(request.content)
    if body is request.content:  # no tools/call or tools/list in it
        return request
    headers = {k: v for k, v in request.headers.items() if k.lower() != "content-length"}
    if psid:
        headers[LAUNCHPAD_POLICY_SESSION_HEADER] = psid
    return httpx.Request(request.method, request.url, headers=headers, content=body,
                         extensions=request.extensions)
# </launchpad-principal-carrier:v1>
