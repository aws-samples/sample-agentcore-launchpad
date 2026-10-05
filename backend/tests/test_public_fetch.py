"""SSRF guard of the user-supplied OIDC discovery fetch (`services.public_fetch`):
https only, every resolved address public, pinned connect, no redirects, and a
generic 422 by category from both callers (inbound JWT probe, OBO Connection)."""

import json
import socket

import httpx
import pytest

from app.core.errors import AppError
from app.services import identity_providers as ip
from app.services import inbound_auth as svc
from app.services import public_fetch as pf

URL = "https://idp.example.com/realm/.well-known/openid-configuration"
DOC = {"issuer": "https://idp.example.com/realm", "jwks_uri": "https://idp.example.com/keys"}


def resolver(*addresses: str):
    calls: list[str] = []

    def resolve(host, port, proto=0):
        calls.append(host)
        return [
            (socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, proto, "",
             (a, port))
            for a in addresses
        ]

    resolve.calls = calls
    return resolve


def transport(handler):
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    mock = httpx.MockTransport(handle)
    mock.seen = seen
    return mock


def _fetch(url=URL, *, addresses=("93.184.215.14",), handler=None, **kw):
    t = transport(handler or (lambda r: httpx.Response(200, json=DOC)))
    result = pf.fetch_public_json(url, timeout=1.0, resolver=resolver(*addresses),
                                  transport=t, **kw)
    return result, t


def test_a_public_host_is_fetched_pinned_to_the_validated_address():
    result, t = _fetch()
    assert result == DOC
    (request,) = t.seen
    assert request.url.host == "93.184.215.14" and request.url.port in (None, 443)
    assert request.url.path == "/realm/.well-known/openid-configuration"
    assert request.headers["host"] == "idp.example.com"
    assert request.extensions["sni_hostname"] == "idp.example.com"


def test_ipv6_and_explicit_port_are_pinned_correctly():
    _, t = _fetch("https://idp.example.com:8443/.well-known/openid-configuration",
                  addresses=("2606:4700::6810:84e5",))
    (request,) = t.seen
    assert request.url.host == "2606:4700::6810:84e5" and request.url.port == 8443
    assert request.headers["host"] == "idp.example.com:8443"


@pytest.mark.parametrize("url", [
    "http://idp.example.com/.well-known/openid-configuration",
    "ftp://idp.example.com/.well-known/openid-configuration",
    "https:///.well-known/openid-configuration",
])
def test_anything_but_https_is_refused_before_resolving(url):
    resolve = resolver("93.184.215.14")
    with pytest.raises(pf.PublicFetchError) as err:
        pf.fetch_public_json(url, timeout=1.0, resolver=resolve)
    assert err.value.reason == "not_https"
    assert resolve.calls == []


@pytest.mark.parametrize("addresses", [
    ("127.0.0.1",),
    ("10.1.2.3",),
    ("172.16.0.9",),
    ("192.168.1.1",),
    ("169.254.169.254",),  # instance metadata
    ("100.64.0.1",),  # CGNAT
    ("0.0.0.0",),
    ("224.0.0.1",),
    ("::1",),
    ("fe80::1",),
    ("fd00::1",),
    ("::",),
    ("::ffff:127.0.0.1",),  # IPv4-mapped loopback
    ("::ffff:169.254.169.254",),
    ("93.184.215.14", "10.0.0.5"),  # ONE private answer is enough
])
def test_non_public_addresses_are_refused_without_connecting(addresses):
    t = transport(lambda r: httpx.Response(200, json=DOC))
    with pytest.raises(pf.PublicFetchError) as err:
        pf.fetch_public_json(URL, timeout=1.0, resolver=resolver(*addresses), transport=t)
    assert err.value.reason == "non_public_address"
    assert err.value.label == "non-public address"
    assert t.seen == []


def test_a_literal_private_ip_is_refused_through_the_real_resolver():
    with pytest.raises(pf.PublicFetchError) as err:
        pf.fetch_public_json(
            "https://169.254.169.254/.well-known/openid-configuration", timeout=1.0)
    assert err.value.reason == "non_public_address"


def test_unresolvable_host():
    def fail(*_a, **_k):
        raise socket.gaierror("Name or service not known")

    with pytest.raises(pf.PublicFetchError) as err:
        pf.fetch_public_json(URL, timeout=1.0, resolver=fail)
    assert err.value.reason == "unresolvable"


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirects_are_refused_not_followed(status):
    t = transport(lambda r: httpx.Response(
        status, headers={"Location": "http://169.254.169.254/latest/meta-data/"}))
    with pytest.raises(pf.PublicFetchError) as err:
        pf.fetch_public_json(URL, timeout=1.0, resolver=resolver("93.184.215.14"),
                             transport=t)
    assert err.value.reason == "redirect_refused"
    assert len(t.seen) == 1  # the Location was never requested


@pytest.mark.parametrize(("handler", "reason", "label"), [
    (lambda r: httpx.Response(404), "http_error", "HTTP 404"),
    (lambda r: httpx.Response(200, json=["not", "an", "object"]),
     "not_json_object", "not a JSON object"),
    (lambda r: httpx.Response(200, text="<html>"), "not_json_object", "not a JSON object"),
    (lambda r: httpx.Response(200, content=b"x" * 64), "too_large", "response too large"),
])
def test_bad_responses_fail_by_category(handler, reason, label):
    with pytest.raises(pf.PublicFetchError) as err:
        _fetch(handler=handler, max_bytes=32)
    assert (err.value.reason, err.value.label) == (reason, label)


def test_transport_errors_read_as_unreachable():
    def boom(request):
        raise httpx.ConnectError("[Errno 111] Connection refused to 93.184.215.14")

    with pytest.raises(pf.PublicFetchError) as err:
        _fetch(handler=boom)
    assert err.value.reason == "unreachable"


def test_the_default_probe_uses_the_guard():
    # an http URL never leaves the process, and the 422 carries only the category
    with pytest.raises(AppError) as err:
        svc.probe_discovery("http://10.0.0.5/.well-known/openid-configuration")
    assert err.value.code == "identity.discovery_unreachable"
    assert err.value.message.endswith("(not https)")
    assert err.value.detail == {"reason": "not_https"}


def test_the_obo_connection_check_shares_the_guard_and_the_generic_message():
    def internal(url):
        raise httpx.ConnectError("connect to 10.9.8.7:443 failed: internal-vault.corp")

    with pytest.raises(AppError) as err:
        ip.check_obo_idp(
            "TOKEN_EXCHANGE", discovery_url=URL, token_endpoint=None, probe=internal)
    assert err.value.code == "identity.discovery_unreachable"
    assert err.value.status_code == 422
    assert "10.9.8.7" not in err.value.message and "internal-vault" not in err.value.message
    assert "internal" not in json.dumps(err.value.detail)
