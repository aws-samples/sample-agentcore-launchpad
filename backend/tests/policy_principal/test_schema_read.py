"""Schema reads fail closed, with bounded retries and no upstream content in errors."""

from types import SimpleNamespace

import httpx
import pytest

from app.core.errors import AppError
from app.services import mcp_client
from app.services import policy_engine_sync as sync
from tests.test_mcp_client import _stub_credentials, _workspace


@pytest.mark.parametrize(("status", "attempts"), [(429, 3), (500, 3), (503, 3),
                                                 (400, 1), (401, 1), (403, 1)])
def test_schema_http_errors_are_bounded_and_sanitized(monkeypatch, status, attempts):
    _stub_credentials(monkeypatch)
    calls, sleeps = [], []
    monkeypatch.setattr(sync.time, "sleep", sleeps.append)

    def post(url, **kwargs):
        calls.append(kwargs["timeout"])
        return httpx.Response(status, text="secret upstream body",
                              request=httpx.Request("POST", url + "?token=secret"))

    monkeypatch.setattr(mcp_client.httpx, "post", post)
    with pytest.raises(AppError) as error:
        sync._read_gateway_schema(_workspace())
    assert calls == [20] * attempts
    assert sleeps == ([0.25, 0.5] if attempts == 3 else [])
    assert error.value.detail["upstream_status"] == status
    assert error.value.detail["attempts"] == attempts
    assert "secret" not in str(error.value) + str(error.value.detail)


@pytest.mark.parametrize("error", [
    httpx.ReadTimeout("secret"), httpx.ConnectError("secret"),
    AppError("gateway.rpc_error", "secret", {"body": "secret"}),
    ValueError("secret"),
])
def test_schema_transport_retries_but_semantic_errors_do_not(monkeypatch, error):
    calls = []
    monkeypatch.setattr(sync.time, "sleep", lambda _: None)

    def fail(*args, **kwargs):
        calls.append(1)
        raise error

    monkeypatch.setattr(mcp_client, "tools_list", fail)
    with pytest.raises(AppError) as result:
        sync._read_gateway_schema(SimpleNamespace())
    assert len(calls) == (3 if isinstance(error, httpx.TransportError) else 1)
    assert "secret" not in str(result.value) + str(result.value.detail)


@pytest.mark.parametrize("payload", [{}, {"result": {}}, {"result": {"tools": None}},
                                     {"result": {"tools": [None]}}, {"result": []}])
def test_missing_or_invalid_tools_are_not_an_empty_schema(monkeypatch, payload):
    _stub_credentials(monkeypatch)
    monkeypatch.setattr(mcp_client.httpx, "post", lambda url, **_: httpx.Response(
        200, json=payload, request=httpx.Request("POST", url),
    ))
    with pytest.raises(AppError) as error:
        sync._read_gateway_schema(_workspace())
    assert error.value.detail["reason"] == "gateway.bad_response"
    assert error.value.detail["attempts"] == 1


def test_html_response_is_typed_and_tools_call_is_never_retried(monkeypatch):
    _stub_credentials(monkeypatch)
    calls = []

    def post(url, **kwargs):
        calls.append(1)
        return httpx.Response(200, text="<html>secret</html>",
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(mcp_client.httpx, "post", post)
    with pytest.raises(AppError) as error:
        sync._read_gateway_schema(_workspace())
    assert error.value.detail["attempts"] == 1
    assert "secret" not in str(error.value)
    calls.clear()

    def failed_post(url, **kwargs):
        calls.append(1)
        return httpx.Response(500, request=httpx.Request("POST", url))

    monkeypatch.setattr(mcp_client.httpx, "post", failed_post)
    with pytest.raises(httpx.HTTPStatusError):
        mcp_client.tools_call(_workspace(), "mutate", {})
    assert len(calls) == 1
