"""Tool catalog (gateway MCP tools + builtins) and builtin-tool demos."""

import threading
import time
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.errors import AppError
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import mcp_client
from app.services.agentcore.client import control_client
from app.services.workspace import WorkspaceContext

router = APIRouter(prefix="/api", tags=["tools"])

BUILTIN_TOOLS = [
    {
        "name": "code-interpreter",
        "source": "builtin",
        "description": "AgentCore managed sandbox that executes code (aws.codeinterpreter.v1).",
        "inputSchema": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "Code to execute"}},
            "required": ["code"],
        },
        "auth": "IAM",
    },
    {
        "name": "browser",
        "source": "builtin",
        "description": "AgentCore managed cloud browser for web automation (aws.browser.v1).",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "Page to open"}},
            "required": ["url"],
        },
        "auth": "IAM",
    },
]

# Per workspace: the gateway tool catalog belongs to one environment's gateway.
_cache: dict[str, dict[str, Any]] = {}

BROWSER_DEMO_SESSION_SECONDS = 300
BROWSER_DEMO_VIEWPORT = {"width": 1280, "height": 720}
BROWSER_DEMO_DEFAULT_IDENTIFIER = "aws.browser.v1"
_browser_demo_lock = threading.Lock()
_browser_demo_timers: dict[str, threading.Timer] = {}


@dataclass(slots=True)
class _BrowserDemoSession:
    client: Any
    browser_identifier: str
    profile_identifier: str | None
    save_profile: bool
    # The environment that started it: the browser lives in that account/region,
    # so only that workspace may stop it (the dict itself is process-global).
    workspace_id: str


_browser_demo_sessions: dict[str, _BrowserDemoSession] = {}


@router.get("/tools")
def list_tools(
    refresh: bool = False,
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    slot = _cache.setdefault(ws.id, {"tools": None, "at": 0.0})
    gateway_tools: list[dict[str, Any]] = []
    gateway_error: str | None = None
    if not refresh and slot["tools"] is not None and time.time() - slot["at"] < 60:
        gateway_tools = slot["tools"]
    else:
        try:
            raw = mcp_client.tools_list(ws.context)
            gateway_tools = [
                {
                    "name": t["name"],
                    "source": "gateway",
                    "target": t["name"].split("___")[0] if "___" in t["name"] else "",
                    "description": t.get("description", ""),
                    "inputSchema": t.get("inputSchema", {}),
                    "auth": "Cognito JWT · via launchpad-gw",
                }
                for t in raw
            ]
            slot["tools"], slot["at"] = gateway_tools, time.time()
        except AppError as exc:
            gateway_error = exc.code
    return {
        "gateway_url": ws.context.resources.get("gateway_url"),
        "gateway_error": gateway_error,
        "tools": gateway_tools + BUILTIN_TOOLS,
    }


class ToolCallRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


@router.post("/tools/call")
def call_tool(
    req: ToolCallRequest,
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return mcp_client.tools_call(ws.context, req.name, req.arguments)


class CodeDemoRequest(BaseModel):
    code: str = Field(
        default="import math\nprint('sqrt(1764) =', math.isqrt(1764))",
        max_length=4000,
    )


@router.post("/demos/code-interpreter")
def demo_code_interpreter(
    req: CodeDemoRequest,
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    from bedrock_agentcore.tools import CodeInterpreter

    # `session=` is what keeps this in the workspace's account: the SDK builds its
    # own control/data-plane clients, and without a session it builds them off
    # ambient credentials — a spoke workspace's demo would then run in the hub.
    interpreter = CodeInterpreter(
        region=ws.context.region, session=ws.context.sdk_session()
    )
    started = time.monotonic()
    try:
        interpreter.start(session_timeout_seconds=300)
        response = interpreter.invoke("executeCode", {"language": "python", "code": req.code})
        chunks: list[str] = []
        for event in response.get("stream", []):
            result = event.get("result", {})
            for item in result.get("content", []):
                if item.get("type") == "text":
                    chunks.append(item.get("text", ""))
        return {
            "stdout": "\n".join(chunks),
            "session_id": interpreter.session_id,
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    finally:
        try:
            interpreter.stop()
        except Exception:
            pass


class BrowserDemoRequest(BaseModel):
    url: str = Field(default="https://example.com", max_length=2000)
    web_bot_auth: bool = False
    browser_identifier: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    profile_identifier: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    save_profile: bool = False


_BLOCKED_HOSTS = ("localhost", "metadata.google.internal")


def _validate_demo_url(url: str) -> None:
    """The demo browser runs in AgentCore's cloud sandbox (not our VPC), but
    still refuse non-web schemes and internal/metadata targets — resolve the
    host and judge the ACTUAL IPs so decimal/hex/octal/IPv6 encodings and DNS
    names for private ranges can't slip through (defense in depth)."""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    blocked = (
        parsed.scheme not in ("http", "https")
        or not host
        or host in _BLOCKED_HOSTS
        or host.endswith(".internal")
    )
    if not blocked:
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
            addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
        except (OSError, ValueError):
            addresses = []  # unresolvable — let the sandbox browser fail it
        blocked = any(
            ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified
            for ip in addresses
        )
    if blocked:
        raise AppError("tools.url_blocked", f"url not allowed for the browser demo: {url}")


def _list_control_resources(
    workspace: WorkspaceContext, operation: str, result_key: str
) -> list[dict[str, Any]]:
    client = control_client(workspace)
    paginator = client.get_paginator(operation)
    return [
        item
        for page in paginator.paginate()
        for item in page.get(result_key, [])
    ]


@router.get("/demos/browser/options")
def browser_demo_options(
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    try:
        custom_browsers = []
        client = control_client(ws.context)
        for summary in _list_control_resources(
            ws.context, "list_browsers", "browserSummaries"
        ):
            detail = client.get_browser(browserId=summary["browserId"])
            custom_browsers.append(
                {
                    "identifier": summary["browserId"],
                    "name": summary.get("name") or summary["browserId"],
                    "description": summary.get("description", ""),
                    "status": detail.get("status", summary.get("status", "UNKNOWN")),
                    "web_bot_auth": bool(
                        detail.get("browserSigning", {}).get("enabled")
                    ),
                }
            )
        profiles = [
            {
                "identifier": summary["profileId"],
                "name": summary.get("name") or summary["profileId"],
                "description": summary.get("description", ""),
                "status": summary.get("status", "UNKNOWN"),
                "last_saved_at": (
                    summary["lastSavedAt"].isoformat()
                    if hasattr(summary.get("lastSavedAt"), "isoformat")
                    else summary.get("lastSavedAt")
                ),
                "last_saved_browser_identifier": summary.get("lastSavedBrowserId"),
            }
            for summary in _list_control_resources(
                ws.context,
                "list_browser_profiles",
                "profileSummaries",
            )
        ]
    except Exception as exc:
        raise AppError(
            "tools.browser_options_failed",
            "failed to list browser demo resources",
            status_code=502,
        ) from exc
    return {
        "browsers": sorted(custom_browsers, key=lambda item: item["name"].lower()),
        "profiles": sorted(profiles, key=lambda item: item["name"].lower()),
    }


def _assert_browser_demo_available(workspace: WorkspaceContext) -> None:
    """Refuse the browser demo in a cross-account workspace.

    `BrowserClient` takes a region and nothing else: it builds its boto3 clients
    off ambient credentials with no way to pass a session (unlike
    `CodeInterpreter`, which accepts one). In a spoke workspace it would start a
    browser session in the HUB account while the console said otherwise, so this
    fails loudly instead. `tests/test_client_funnel.py` asserts the SDK still has
    no `session` parameter — the day it grows one, that test fails and this gate
    comes out.
    """
    if workspace.role_arn is None:
        return
    raise AppError(
        "workspace.cross_account_tool_unavailable",
        "the browser demo can only run under the hub's own credentials, so it "
        "cannot target a workspace reached through an assumed role",
        {"workspace_id": workspace.id},
        status_code=409,
    )


def _resolve_browser_demo_configuration(
    req: BrowserDemoRequest,
    workspace: WorkspaceContext,
) -> tuple[str, str | None]:
    if req.save_profile and not req.profile_identifier:
        raise AppError(
            "tools.browser_profile_required",
            "select a browser profile before enabling profile save",
            status_code=422,
        )

    browser_identifier = BROWSER_DEMO_DEFAULT_IDENTIFIER
    try:
        client = control_client(workspace)
        if req.web_bot_auth:
            if not req.browser_identifier:
                raise AppError(
                    "tools.browser_required",
                    "select a Web Bot Auth browser",
                    status_code=422,
                )
            browser = client.get_browser(browserId=req.browser_identifier)
            if (
                browser.get("status") != "READY"
                or not browser.get("browserSigning", {}).get("enabled")
            ):
                raise AppError(
                    "tools.browser_web_bot_auth_unavailable",
                    "the selected browser is not READY with Web Bot Auth enabled",
                    status_code=422,
                )
            browser_identifier = req.browser_identifier

        if req.profile_identifier:
            profile = client.get_browser_profile(profileId=req.profile_identifier)
            if profile.get("status") != "READY":
                raise AppError(
                    "tools.browser_profile_unavailable",
                    "the selected browser profile is not READY",
                    status_code=422,
                )
    except AppError:
        raise
    except Exception as exc:
        raise AppError(
            "tools.browser_configuration_failed",
            "failed to validate browser demo resources",
            status_code=502,
        ) from exc
    return browser_identifier, req.profile_identifier


def _stop_browser_demo_session(
    session_id: str, workspace_id: str | None = None
) -> dict[str, bool | None]:
    """Stop and forget one demo session.

    `workspace_id` is the caller's environment; `None` is the expiry timer, which
    owns every session. A session another workspace started is left running and
    answers exactly like an expired one — the console stops a possibly-expired
    previous session before starting the next, so "already gone" has to remain a
    success, and a 404 here would be the one place a foreign id is
    distinguishable from a missing one.
    """
    with _browser_demo_lock:
        session = _browser_demo_sessions.get(session_id)
        if session is not None and workspace_id not in (None, session.workspace_id):
            return {"stopped": False, "profile_saved": None}
        _browser_demo_sessions.pop(session_id, None)
        timer = _browser_demo_timers.pop(session_id, None)
    if timer is not None and timer is not threading.current_thread():
        timer.cancel()
    if session is None:
        return {"stopped": False, "profile_saved": None}

    profile_saved: bool | None = None
    if session.save_profile and session.profile_identifier:
        try:
            session.client.data_plane_client.save_browser_session_profile(
                browserIdentifier=session.browser_identifier,
                sessionId=session_id,
                profileIdentifier=session.profile_identifier,
            )
            profile_saved = True
        except Exception:
            profile_saved = False
    session.client.stop()
    return {"stopped": True, "profile_saved": profile_saved}


def _expire_browser_demo_session(session_id: str) -> None:
    try:
        _stop_browser_demo_session(session_id)
    except Exception:
        pass


def _retain_browser_demo_session(
    client: Any,
    *,
    browser_identifier: str,
    profile_identifier: str | None,
    save_profile: bool,
    workspace_id: str,
) -> None:
    session_id = client.session_id
    if not session_id:
        raise RuntimeError("browser session did not return an id")
    timer = threading.Timer(
        BROWSER_DEMO_SESSION_SECONDS,
        _expire_browser_demo_session,
        args=(session_id,),
    )
    timer.daemon = True
    with _browser_demo_lock:
        _browser_demo_sessions[session_id] = _BrowserDemoSession(
            client=client,
            browser_identifier=browser_identifier,
            profile_identifier=profile_identifier,
            save_profile=save_profile,
            workspace_id=workspace_id,
        )
        _browser_demo_timers[session_id] = timer
    timer.start()


@router.post("/demos/browser")
def demo_browser(
    req: BrowserDemoRequest,
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _assert_browser_demo_available(ws.context)

    from bedrock_agentcore.tools import BrowserClient
    from playwright.sync_api import sync_playwright

    _validate_demo_url(req.url)
    browser_identifier, profile_identifier = _resolve_browser_demo_configuration(
        req, ws.context
    )
    client = BrowserClient(region=ws.context.region)
    started = time.monotonic()
    retained = False
    try:
        start_kwargs: dict[str, Any] = {
            "identifier": browser_identifier,
            "session_timeout_seconds": BROWSER_DEMO_SESSION_SECONDS,
            "viewport": BROWSER_DEMO_VIEWPORT,
        }
        if profile_identifier:
            start_kwargs["profile_configuration"] = {
                "profileIdentifier": profile_identifier
            }
        client.start(**start_kwargs)
        ws_url, headers = client.generate_ws_headers()
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(ws_url, headers=headers)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(req.url, wait_until="domcontentloaded", timeout=30000)
            title = page.title()
            # Exiting Playwright disconnects automation while the AgentCore session
            # remains alive for the DCV Live View client.
        live_view_url = client.generate_live_view_url(
            expires=BROWSER_DEMO_SESSION_SECONDS
        )
        _retain_browser_demo_session(
            client,
            browser_identifier=browser_identifier,
            profile_identifier=profile_identifier,
            save_profile=req.save_profile,
            workspace_id=ws.id,
        )
        retained = True
        return {
            "url": req.url,
            "title": title,
            "session_id": client.session_id,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "live_view_url": live_view_url,
            "live_view_expires_in": BROWSER_DEMO_SESSION_SECONDS,
            "viewport": BROWSER_DEMO_VIEWPORT,
            "browser_identifier": browser_identifier,
            "web_bot_auth": req.web_bot_auth,
            "profile_identifier": profile_identifier,
            "save_profile": req.save_profile,
        }
    finally:
        if not retained:
            try:
                client.stop()
            except Exception:
                pass


@router.delete("/demos/browser/{session_id}")
def stop_demo_browser(
    session_id: str,
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    try:
        result = _stop_browser_demo_session(session_id, ws.id)
    except Exception as exc:
        raise AppError(
            "tools.browser_stop_failed",
            "failed to stop the browser demo session",
            status_code=502,
        ) from exc
    return {"session_id": session_id, **result}
