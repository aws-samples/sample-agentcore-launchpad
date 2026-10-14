"""SSRF guard of the alert webhook (`services.webhook_safety`).

The URL is typed by an administrator and POSTed to unattended from inside the hub's
network, so it is checked on save AND on every send: https only, no credentials, every
resolved address public (IPv4-mapped IPv6 judged as its IPv4), pinned connect, and a
redirect is never followed.
"""

import json
import socket

import httpx
import pytest

from app.core.errors import AppError
from app.services import webhook_safety as ws

PUBLIC = "93.184.216.34"


def _dns(monkeypatch, *addresses: str, fail: bool = False) -> list[str]:
    """Stub `socket.getaddrinfo`; returns the list of hosts it was asked for."""
    asked: list[str] = []

    def resolve(host, port, *args, **kwargs):
        asked.append(host)
        if fail:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (address, port, 0, 0) if ":" in address else (address, port),
            )
            for address in addresses
        ]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    return asked


def _code(url: str) -> str:
    with pytest.raises(AppError) as exc:
        ws.check_webhook_url(url)
    assert exc.value.status_code == 400
    return exc.value.code


# ── on save ─────────────────────────────────────────────────────────────────────


def test_a_public_https_url_is_accepted(monkeypatch):
    asked = _dns(monkeypatch, PUBLIC)
    assert ws.check_webhook_url(" https://hooks.slack.com/services/x ") == (
        "https://hooks.slack.com/services/x"
    )
    assert asked == ["hooks.slack.com"]


@pytest.mark.parametrize(
    "url",
    ["http://hook.example/x", "ftp://hook.example/x", "hook.example/x", "//hook.example/x"],
)
def test_anything_but_https_is_refused(monkeypatch, url):
    asked = _dns(monkeypatch, PUBLIC)
    assert _code(url) == "alert.webhook_not_https"
    assert asked == []  # refused before any DNS lookup


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@hook.example/x",
        "https://user@hook.example/x",
        "https://:pass@hook.example/x",
        "https://@hook.example/x",
    ],
)
def test_credentials_in_the_url_are_refused(monkeypatch, url):
    _dns(monkeypatch, PUBLIC)
    assert _code(url) == "alert.webhook_credentials"


def test_a_url_without_a_host_or_with_a_bad_port_is_refused(monkeypatch):
    _dns(monkeypatch, PUBLIC)
    assert _code("https:///path") == "alert.webhook_invalid"
    assert _code("https://hook.example:99999/x") == "alert.webhook_invalid"


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.5",  # private
        "172.16.3.4",
        "192.168.1.1",
        "127.0.0.1",  # loopback
        "169.254.169.254",  # link-local: the instance metadata service
        "0.0.0.0",  # unspecified
        "100.64.0.1",  # CGNAT
        "224.0.0.1",  # multicast
        "240.0.0.1",  # reserved
        "::1",  # IPv6 loopback
        "fe80::1",  # IPv6 link-local
        "fc00::1",  # IPv6 unique-local
        "::",  # IPv6 unspecified
        "::ffff:10.0.0.5",  # IPv4-mapped private
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "::ffff:169.254.169.254",  # IPv4-mapped metadata
    ],
)
def test_a_non_public_address_is_refused(monkeypatch, address):
    _dns(monkeypatch, address)
    assert _code("https://hook.example/x") == "alert.webhook_non_public"


def test_one_private_answer_among_public_ones_is_enough_to_refuse(monkeypatch):
    _dns(monkeypatch, PUBLIC, "10.0.0.5")
    assert _code("https://hook.example/x") == "alert.webhook_non_public"


def test_an_ip_literal_is_judged_directly(monkeypatch):
    # getaddrinfo of a literal returns the literal; the stub does the same
    _dns(monkeypatch, "127.0.0.1")
    assert _code("https://127.0.0.1/x") == "alert.webhook_non_public"


def test_an_unresolvable_host_is_refused(monkeypatch):
    _dns(monkeypatch, fail=True)
    assert _code("https://nowhere.invalid/x") == "alert.webhook_unresolvable"


# ── on send ─────────────────────────────────────────────────────────────────────


class _Recorder:
    def __init__(self, status: int = 200, headers: dict | None = None):
        self.status = status
        self.headers = headers or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, headers=self.headers)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def test_a_send_posts_json_to_the_validated_address(monkeypatch):
    _dns(monkeypatch, PUBLIC)
    recorder = _Recorder(200)
    ok, outcome = ws.post_json(
        "https://hooks.slack.com/services/x?y=1", {"text": "hi"}, transport=recorder.transport
    )
    assert (ok, outcome) == (True, "HTTP 200")
    [request] = recorder.requests
    assert request.method == "POST"
    # pinned: the connection goes to the IP that was checked, with the real Host
    assert request.url.host == PUBLIC and request.url.port in (None, 443)
    assert request.url.path == "/services/x" and request.url.query == b"y=1"
    assert request.headers["host"] == "hooks.slack.com"
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {"text": "hi"}


def test_a_send_re_checks_the_url(monkeypatch):
    """DNS that turned private since the rule was saved: nothing is sent."""
    _dns(monkeypatch, "10.0.0.5")
    recorder = _Recorder(200)
    ok, outcome = ws.post_json("https://hook.example/x", {"text": "hi"},
                               transport=recorder.transport)
    assert ok is False and outcome.startswith("refused")
    assert recorder.requests == []

    _dns(monkeypatch, "::ffff:127.0.0.1")
    ok, _ = ws.post_json("https://hook.example/x", {"text": "hi"}, transport=recorder.transport)
    assert ok is False and recorder.requests == []

    ok, _ = ws.post_json("http://hook.example/x", {"text": "hi"}, transport=recorder.transport)
    assert ok is False and recorder.requests == []


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_a_redirect_is_not_followed(monkeypatch, status):
    _dns(monkeypatch, PUBLIC)
    recorder = _Recorder(status, {"Location": "https://169.254.169.254/latest/meta-data"})
    ok, outcome = ws.post_json("https://hook.example/x", {"text": "hi"},
                               transport=recorder.transport)
    assert ok is False
    assert outcome == f"redirect refused (HTTP {status})"
    assert len(recorder.requests) == 1  # the Location was never requested


def test_an_http_error_and_a_transport_failure_never_raise(monkeypatch):
    _dns(monkeypatch, PUBLIC)
    assert ws.post_json("https://hook.example/x", {}, transport=_Recorder(500).transport) == (
        False, "HTTP 500",
    )

    def down(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    ok, outcome = ws.post_json("https://hook.example/x", {},
                               transport=httpx.MockTransport(down))
    assert (ok, outcome) == (False, "ConnectTimeout")


def test_an_oversized_body_is_not_sent(monkeypatch):
    _dns(monkeypatch, PUBLIC)
    recorder = _Recorder(200)
    ok, outcome = ws.post_json(
        "https://hook.example/x", {"text": "x" * (ws.MAX_BODY_BYTES + 1)},
        transport=recorder.transport,
    )
    assert (ok, outcome) == (False, "body too large")
    assert recorder.requests == []


def test_a_non_default_port_keeps_it_in_the_host_header(monkeypatch):
    _dns(monkeypatch, PUBLIC)
    recorder = _Recorder(204)
    assert ws.post_json("https://hook.example:8443/x", {}, transport=recorder.transport) == (
        True, "HTTP 204",
    )
    [request] = recorder.requests
    assert request.url.port == 8443 and request.headers["host"] == "hook.example:8443"
