"""The console-side MCP client SigV4-signs every call to the shared gateway."""

from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from app.core.errors import AppError
from app.services import mcp_client
from app.services.workspace import WorkspaceContext


def _workspace() -> WorkspaceContext:
    ws = WorkspaceContext(account_id="123456789012", region="cn-northwest-1")
    ws.resources["gateway_url"] = "https://gw-1.gateway.bedrock-agentcore.cn-northwest-1.amazonaws.com.cn/mcp"
    return ws


def _stub_credentials(monkeypatch) -> None:
    # WorkspaceContext is a frozen dataclass, so an instance-level
    # `monkeypatch.setattr(ws, "credentials", ...)` raises FrozenInstanceError;
    # patch the class method instead.
    monkeypatch.setattr(
        WorkspaceContext,
        "credentials",
        lambda self: SimpleNamespace(access_key="AK", secret_key="SK", token=None),
    )


def test_tools_list_signs_the_request_with_sigv4(monkeypatch):
    ws = _workspace()
    _stub_credentials(monkeypatch)
    captured = {}

    def fake_post(url, *, headers, content, timeout):
        captured["headers"] = headers
        response = httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "result": {"tools": []}},
            request=httpx.Request("POST", url),
        )
        return response

    with patch("httpx.post", side_effect=fake_post):
        result = mcp_client.tools_list(ws)

    assert result == []
    assert "Authorization" in captured["headers"]
    assert captured["headers"]["Authorization"].startswith("AWS4-HMAC-SHA256")


def test_tools_call_signs_and_posts_the_rpc_body(monkeypatch):
    ws = _workspace()
    _stub_credentials(monkeypatch)
    with patch("httpx.post") as post:
        post.return_value = httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "result": {"ok": True}},
            request=httpx.Request("POST", mcp_client._gateway_url(ws)),
        )
        result = mcp_client.tools_call(ws, "hr-database___list_employees", {"dept": "eng"})
    assert result == {"ok": True}
    sent = post.call_args
    assert sent.kwargs["headers"]["Authorization"].startswith("AWS4-HMAC-SHA256")


def test_unsigned_401_still_raises_the_named_error(monkeypatch):
    ws = _workspace()
    _stub_credentials(monkeypatch)
    with patch("httpx.post") as post:
        post.return_value = httpx.Response(403, text="Forbidden")
        with pytest.raises(AppError) as exc:
            mcp_client.tools_list(ws)
    assert exc.value.code == "gateway.unauthorized"
