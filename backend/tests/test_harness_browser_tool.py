"""N15-obs16 part 2: the rendered `browser` builtin against a fake BrowserClient + CDP stream.

The template module is loaded from `harness_builtin_browser.py.tmpl` exactly as the bundle
ships it; `bedrock_agentcore.tools.browser_client` and `websockets.sync.client` are replaced
in `sys.modules` so no session, socket or credential is touched.
"""

import importlib.util
import json
import sys
import types

import pytest

from app.deployer.harness_local import BROWSER_TOOL_TEMPLATE

PAGE_TEXT = "Welcome to the HR portal. " * 1200  # ≈ 31 k chars, above the 20 k bound


class FakeBrowserClient:
    instances: list["FakeBrowserClient"] = []

    def __init__(self, region):
        self.region = region
        self.calls: list[tuple] = []
        self.identifier = None
        self.session_id = None
        self.data_plane_client = types.SimpleNamespace(
            meta=types.SimpleNamespace(endpoint_url="https://dp.example.test")
        )
        FakeBrowserClient.instances.append(self)

    def start(self, **kwargs):
        self.calls.append(("start", kwargs))
        self.identifier, self.session_id = kwargs["identifier"], "sess-1"
        return self.session_id

    def stop(self):
        self.calls.append(("stop",))
        return True


class FakeWs:
    """Scripted CDP peer: answers every call, fires loadEventFired after Page.navigate."""

    def __init__(self, *, navigate_error: str | None = None, text: str = PAGE_TEXT,
                 title: str = "HR portal", url: str = "https://hr.example.test/home"):
        self.sent: list[dict] = []
        self.queue: list[str] = []
        self.navigate_error = navigate_error
        self.text, self.title, self.url = text, title, url

    def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        method, mid, sid = msg["method"], msg["id"], msg.get("sessionId")
        if method == "Target.createTarget":
            result = {"targetId": "T1"}
        elif method == "Target.attachToTarget":
            result = {"sessionId": "S1"}
        elif method == "Page.navigate":
            result = {"frameId": "F1"}
            if self.navigate_error:
                result["errorText"] = self.navigate_error
            else:
                self.queue.append(json.dumps(
                    {"method": "Page.loadEventFired", "sessionId": sid, "params": {}}))
        elif method == "Runtime.evaluate":
            result = {"result": {"type": "object", "value": {
                "title": self.title, "url": self.url, "text": self.text}}}
        else:
            result = {}
        self.queue.append(json.dumps({"id": mid, "result": result}))

    def recv(self, timeout=None):
        if not self.queue:
            raise TimeoutError("no frame")
        return self.queue.pop(0)


@pytest.fixture
def browser_tool(monkeypatch):
    """The template module with fakes installed; returns (module, FakeWs factory slot)."""
    FakeBrowserClient.instances.clear()
    slot: dict = {"ws": FakeWs(), "connect_error": None}
    fake_sdk = types.ModuleType("bedrock_agentcore.tools.browser_client")
    fake_sdk.BrowserClient = FakeBrowserClient
    fake_ws_mod = types.ModuleType("websockets.sync.client")

    class _Conn:
        def __init__(self, uri, **kwargs):
            slot["uri"], slot["kwargs"] = uri, kwargs
            if slot["connect_error"]:
                raise slot["connect_error"]

        def __enter__(self):
            return slot["ws"]

        def __exit__(self, *exc):
            slot["closed"] = True
            return False

    fake_ws_mod.connect = _Conn
    monkeypatch.setitem(sys.modules, "bedrock_agentcore.tools.browser_client", fake_sdk)
    monkeypatch.setitem(sys.modules, "websockets.sync.client", fake_ws_mod)
    monkeypatch.setenv("AWS_REGION", "cn-northwest-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFAKEFAKEFAKEFAKE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake-secret-not-real")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "fake-token")
    spec = importlib.util.spec_from_loader("launchpad_browser_tool", loader=None)
    module = importlib.util.module_from_spec(spec)
    exec(compile(BROWSER_TOOL_TEMPLATE.read_text(encoding="utf-8"),
                 str(BROWSER_TOOL_TEMPLATE), "exec"), module.__dict__)
    return module, slot


def test_reads_the_page_bounded_and_always_stops_the_session(browser_tool):
    module, slot = browser_tool
    out = module.browser("https://hr.example.test/")
    assert out.startswith("title: HR portal\nurl: https://hr.example.test/home\n\n")
    body = out.split("\n\n", 1)[1]
    assert body.endswith(f"[truncated to {module.BROWSER_MAX_CHARS} characters]")
    assert len(body) <= module.BROWSER_MAX_CHARS + 60
    (client,) = FakeBrowserClient.instances
    assert client.region == "cn-northwest-1"
    assert client.calls[0] == ("start", {"identifier": "aws.browser.v1",
                                         "session_timeout_seconds": 300})
    assert client.calls[-1] == ("stop",) and slot["closed"] is True
    # the stream is signed against the boto3 data-plane host, never a Global literal
    assert slot["uri"] == "wss://dp.example.test/browser-streams/aws.browser.v1/sessions/sess-1/automation"
    headers = slot["kwargs"]["additional_headers"]
    assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIAFAKEFAKEFAKEFAKE/")
    assert "cn-northwest-1/bedrock-agentcore/aws4_request" in headers["Authorization"]
    assert headers["X-Amz-Security-Token"] == "fake-token" and "X-Amz-Date" in headers
    assert "fake-secret-not-real" not in json.dumps(headers)
    methods = [m["method"] for m in slot["ws"].sent]
    assert methods == ["Target.createTarget", "Target.attachToTarget", "Page.enable",
                       "Page.navigate", "Runtime.evaluate", "Target.closeTarget"]
    assert slot["ws"].sent[3]["params"] == {"url": "https://hr.example.test/"}
    # the tool the model sees is the `browser` callable harness_tool_access names
    assert module.browser.tool_spec["name"] == "browser"
    assert module.browser.tool_spec["inputSchema"]["json"]["required"] == ["url"]


def test_a_navigation_failure_is_one_error_line_and_the_session_still_stops(browser_tool):
    module, slot = browser_tool
    slot["ws"] = FakeWs(navigate_error="net::ERR_NAME_NOT_RESOLVED")
    out = module.browser("https://nope.invalid/")
    assert out == "browser error: RuntimeError: navigation failed: net::ERR_NAME_NOT_RESOLVED"
    assert "Traceback" not in out
    (client,) = FakeBrowserClient.instances
    assert client.calls[-1] == ("stop",)
    assert [m["method"] for m in slot["ws"].sent][-1] == "Target.closeTarget"


def test_a_stream_connect_failure_is_mapped_and_the_session_stops(browser_tool):
    module, slot = browser_tool
    slot["connect_error"] = OSError("handshake refused " + "x" * 500)
    out = module.browser("https://hr.example.test/")
    assert out.startswith("browser error: OSError: handshake refused")
    assert len(out) <= len("browser error: ") + module._ERROR_CHARS
    assert FakeBrowserClient.instances[0].calls[-1] == ("stop",)


def test_title_and_url_are_bounded_too_even_when_the_page_ignores_the_js_slice(browser_tool):
    """rev-r16: a page controls all three fields — a 500 k-char document.title must not
    become a 500 k-char tool result. Bounded in the JS expression AND again in Python."""
    module, slot = browser_tool
    slot["ws"] = FakeWs(title="T" * 500_000, url="https://hr.example.test/" + "q" * 10_000,
                        text="short")
    out = module.browser("https://hr.example.test/")
    title_line, url_line, body = out.split("\n", 2)
    assert title_line == "title: " + "T" * module.BROWSER_TITLE_CHARS
    long_url = "https://hr.example.test/" + "q" * 10_000
    assert url_line == "url: " + long_url[: module.BROWSER_URL_CHARS]
    assert body == "\nshort"
    assert len(out) < 3000
    js = module._PAGE_READ_JS
    assert f".slice(0, {module.BROWSER_TITLE_CHARS})" in js
    assert f".slice(0, {module.BROWSER_URL_CHARS})" in js
    assert f".slice(0, {module.BROWSER_MAX_CHARS})" in js


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/",  # loopback (literal)
    "http://localhost:8000/",  # loopback (name)
    "http://[::1]/",  # loopback (IPv6)
    "http://169.254.169.254/latest/meta-data/",  # link-local / instance metadata
    "http://10.0.0.8/",  # RFC1918
    "http://192.168.1.1/",  # RFC1918
    "http://[fd00::1]/",  # unique-local
    "http://8.8.8.8/",  # any literal IP
    "http://db.internal/",  # local-only name
])
def test_private_link_local_loopback_and_literal_ip_hosts_are_refused_pre_session(
    browser_tool, url
):
    """rev-r16 ruling (defence in depth): refused with one line, no session started."""
    module, _slot = browser_tool
    assert module.browser(url) == "browser error: host not allowed"
    assert FakeBrowserClient.instances == []


def test_a_non_http_url_is_refused_without_opening_a_session(browser_tool):
    module, _slot = browser_tool
    for bad in ("file:///etc/passwd", "ftp://x.example", "example.com", ""):
        assert module.browser(bad) == "browser error: url must be an absolute http(s) URL"
    assert FakeBrowserClient.instances == []


def test_the_template_carries_no_partition_literal_and_no_disk_writes():
    text = BROWSER_TOOL_TEMPLATE.read_text(encoding="utf-8")
    # cn-lint: allow — literals asserted ABSENT from the vendored module
    for literal in (".amazonaws.com", "arn:aws:", "captureScreenshot", "take_screenshot",
                    "open(", "write_text", "client.generate_ws_headers"):
        assert literal not in text, literal
