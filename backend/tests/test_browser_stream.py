"""N15-obs17: the hub signs the AgentCore Browser stream / live-view URLs from the client's
resolved data-plane endpoint (`services/agentcore/browser_stream.py`), with the same recipe
the Harness builtin carries in `harness_builtin_browser.py.tmpl` — the template renders
standalone (it cannot import the hub), so this file is what keeps the two from drifting.
Hermetic: fake client, static fake credentials, no socket.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import types
from urllib.parse import parse_qs, urlparse

import pytest
from botocore.credentials import Credentials

from app.deployer.harness_local import BROWSER_TOOL_TEMPLATE
from app.services.agentcore import browser_stream

CN_DATA_PLANE = "https://bedrock-agentcore.cn-northwest-1.amazonaws.com.cn"
CREDS = Credentials("AKIAFAKEFAKEFAKEFAKE", "fake-secret-not-real", "fake-token")


def _client(endpoint_url: str = CN_DATA_PLANE, region: str = "cn-northwest-1"):
    return types.SimpleNamespace(
        region=region, identifier="aws.browser.v1", session_id="01KXNH955ZJWTEVR5PFHGE827F",
        data_plane_client=types.SimpleNamespace(
            meta=types.SimpleNamespace(endpoint_url=endpoint_url)
        ),
    )


@pytest.fixture
def template_module(monkeypatch):
    """The vendored browser tool loaded as the bundle ships it; the SDK and websocket imports
    are stubbed and the ambient credentials are the same static fake keys."""
    fake_sdk = types.ModuleType("bedrock_agentcore.tools.browser_client")
    fake_sdk.BrowserClient = object
    monkeypatch.setitem(sys.modules, "bedrock_agentcore.tools.browser_client", fake_sdk)
    monkeypatch.setitem(sys.modules, "websockets.sync.client", types.ModuleType("w"))
    monkeypatch.setenv("AWS_REGION", "cn-northwest-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", CREDS.access_key)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", CREDS.secret_key)
    monkeypatch.setenv("AWS_SESSION_TOKEN", CREDS.token)
    spec = importlib.util.spec_from_loader("launchpad_browser_tool_parity", loader=None)
    module = importlib.util.module_from_spec(spec)
    exec(compile(BROWSER_TOOL_TEMPLATE.read_text(encoding="utf-8"),
                 str(BROWSER_TOOL_TEMPLATE), "exec"), module.__dict__)
    return module


def _scope(authorization: str) -> tuple[str, str]:
    """(credential scope, signed headers) of one SigV4 Authorization header — the parts
    that do not depend on the signing second."""
    credential = re.search(r"Credential=([^,]+)", authorization).group(1)
    signed = re.search(r"SignedHeaders=([^,]+)", authorization).group(1)
    return credential.split("/", 2)[2], signed  # drop key id + date


def test_hub_and_harness_template_resolve_the_same_aws_cn_stream_endpoint(template_module):
    client = _client()
    hub_url, hub_headers = browser_stream.stream_endpoint(client, CREDS)
    tmpl_url, tmpl_headers = template_module._stream_endpoint(client)

    assert hub_url == tmpl_url == (
        "wss://bedrock-agentcore.cn-northwest-1.amazonaws.com.cn/browser-streams/aws.browser.v1"
        "/sessions/01KXNH955ZJWTEVR5PFHGE827F/automation"
    )
    assert set(hub_headers) == set(tmpl_headers) == {
        "X-Amz-Date", "Authorization", "X-Amz-Security-Token",
    }
    assert _scope(hub_headers["Authorization"]) == _scope(tmpl_headers["Authorization"]) == (
        "cn-northwest-1/bedrock-agentcore/aws4_request",
        "host;x-amz-date;x-amz-security-token",
    )
    assert hub_headers["X-Amz-Security-Token"] == "fake-token"
    assert tmpl_headers["X-Amz-Security-Token"] == "fake-token"
    for headers in (hub_headers, tmpl_headers):
        assert "fake-secret-not-real" not in str(headers)
        assert headers["Authorization"].startswith(
            "AWS4-HMAC-SHA256 Credential=AKIAFAKEFAKEFAKEFAKE/"
        )


def test_the_host_follows_the_resolved_endpoint_whatever_it_is(template_module):
    """No partition table and no literal on either side: the resolver's answer is the host
    (port and trailing path of the endpoint URL included / dropped the same way)."""
    client = _client("https://dp.example.test:8443/", region="us-west-2")
    hub_url, hub_headers = browser_stream.stream_endpoint(client, CREDS)
    tmpl_url, tmpl_headers = template_module._stream_endpoint(client)
    assert hub_url == tmpl_url
    assert hub_url.startswith("wss://dp.example.test:8443/browser-streams/")
    assert _scope(hub_headers["Authorization"])[0] == _scope(tmpl_headers["Authorization"])[0]
    assert "us-west-2/bedrock-agentcore/aws4_request" in hub_headers["Authorization"]


def test_live_view_url_is_presigned_on_the_same_host():
    url = browser_stream.live_view_url(_client(), CREDS, expires=300)
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "bedrock-agentcore.cn-northwest-1.amazonaws.com.cn"
    assert parsed.path == (
        "/browser-streams/aws.browser.v1/sessions/01KXNH955ZJWTEVR5PFHGE827F/live-view"
    )
    query = parse_qs(parsed.query)
    assert query["X-Amz-Expires"] == ["300"]
    assert query["X-Amz-Security-Token"] == ["fake-token"]
    assert query["X-Amz-Credential"][0].endswith("/cn-northwest-1/bedrock-agentcore/aws4_request")
    assert "X-Amz-Signature" in query
    assert "fake-secret-not-real" not in url


def test_signing_refuses_an_unstarted_session_and_missing_credentials():
    unstarted = _client()
    unstarted.session_id = None
    with pytest.raises(RuntimeError, match="not been started"):
        browser_stream.stream_endpoint(unstarted, CREDS)
    with pytest.raises(RuntimeError, match="no AWS credentials"):
        browser_stream.stream_endpoint(_client(), None)
    with pytest.raises(RuntimeError, match="no resolved data-plane endpoint"):
        browser_stream.stream_endpoint(_client(endpoint_url=""), CREDS)


def test_the_module_carries_no_partition_literal():
    text = browser_stream.__file__ and open(browser_stream.__file__, encoding="utf-8").read()
    # cn-lint: allow — literals asserted ABSENT from the module
    for literal in (".amazonaws.com", "arn:aws:", "boto3."):
        assert literal not in text, literal
