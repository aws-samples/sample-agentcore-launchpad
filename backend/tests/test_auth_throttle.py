"""Register/login throttling: client IP resolution, per-IP windows, login backoff,
and the pending-registration cap (services/auth_throttle.py, routers/auth.py)."""

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.core.config import Settings, get_settings
from app.main import create_app
from app.services import auth_throttle

ADMIN = {"username": "operator", "password": "s3cret-pass"}
CLOUDFRONT_EDGE = "52.46.1.10"


def _request(peer: str | None, *xff: str) -> Request:
    headers = [(b"x-forwarded-for", value.encode()) for value in xff]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/auth/login",
        "headers": headers,
        "client": (peer, 1234) if peer is not None else None,
    }
    return Request(scope)


def _settings(**overrides) -> Settings:
    return Settings(**overrides)


class TestClientIp:
    def test_header_is_ignored_without_trusted_hops(self):
        request = _request("127.0.0.1", "198.51.100.7")
        assert auth_throttle.client_ip(request, _settings()) == "127.0.0.1"

    def test_cloudfront_then_appending_nginx_is_two_hops(self):
        # CloudFront appends the viewer, nginx ($proxy_add_x_forwarded_for) the edge
        settings = _settings(auth_trusted_proxy_hops=2)
        request = _request("127.0.0.1", f"198.51.100.7, {CLOUDFRONT_EDGE}")
        assert auth_throttle.client_ip(request, settings) == "198.51.100.7"

    def test_spoofed_entries_left_of_the_trusted_ones_change_nothing(self):
        settings = _settings(auth_trusted_proxy_hops=2)
        request = _request(
            "127.0.0.1", f"1.1.1.1, 10.0.0.1, 198.51.100.7, {CLOUDFRONT_EDGE}"
        )
        assert auth_throttle.client_ip(request, settings) == "198.51.100.7"

    def test_repeated_headers_are_joined_in_order(self):
        settings = _settings(auth_trusted_proxy_hops=2)
        request = _request("127.0.0.1", "1.1.1.1, 198.51.100.7", CLOUDFRONT_EDGE)
        assert auth_throttle.client_ip(request, settings) == "198.51.100.7"

    def test_one_hop_reads_the_rightmost_entry(self):
        settings = _settings(auth_trusted_proxy_hops=1)
        request = _request("127.0.0.1", "1.1.1.1, 198.51.100.7")
        assert auth_throttle.client_ip(request, settings) == "198.51.100.7"

    def test_untrusted_peer_never_has_its_header_read(self):
        # someone reaching uvicorn directly cannot pick their own bucket
        settings = _settings(auth_trusted_proxy_hops=2)
        request = _request("203.0.113.50", "198.51.100.7, 52.46.1.10")
        assert auth_throttle.client_ip(request, settings) == "203.0.113.50"

    def test_configured_proxy_networks_are_trusted(self):
        settings = _settings(auth_trusted_proxy_hops=1, auth_trusted_proxies=["10.0.0.0/8"])
        request = _request("10.1.2.3", "198.51.100.7")
        assert auth_throttle.client_ip(request, settings) == "198.51.100.7"

    def test_fewer_entries_than_hops_takes_the_leftmost(self):
        settings = _settings(auth_trusted_proxy_hops=2)
        assert auth_throttle.client_ip(_request("127.0.0.1", "198.51.100.7"), settings) == (
            "198.51.100.7"
        )

    def test_missing_or_garbage_header_falls_back_to_the_peer(self):
        settings = _settings(auth_trusted_proxy_hops=2)
        assert auth_throttle.client_ip(_request("127.0.0.1"), settings) == "127.0.0.1"
        request = _request("127.0.0.1", "not-an-ip, 52.46.1.10")
        assert auth_throttle.client_ip(request, settings) == "127.0.0.1"

    def test_ipv6_and_port_forms(self):
        settings = _settings(auth_trusted_proxy_hops=2)
        request = _request("::1", f"2001:db8::7, {CLOUDFRONT_EDGE}")
        assert auth_throttle.client_ip(request, settings) == "2001:db8::7"
        request = _request("127.0.0.1", f"[2001:db8::7]:443, {CLOUDFRONT_EDGE}")
        assert auth_throttle.client_ip(request, settings) == "2001:db8::7"
        request = _request("127.0.0.1", f"198.51.100.7:5555, {CLOUDFRONT_EDGE}")
        assert auth_throttle.client_ip(request, settings) == "198.51.100.7"

    def test_cloudfront_viewer_address_wins_when_configured(self):
        settings = _settings(
            auth_client_ip_header="CloudFront-Viewer-Address", auth_trusted_proxy_hops=2
        )
        request = _request("127.0.0.1", f"9.9.9.9, {CLOUDFRONT_EDGE}")
        request.scope["headers"].append((b"cloudfront-viewer-address", b"54.189.180.95:46164"))
        assert auth_throttle.client_ip(request, settings) == "54.189.180.95"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("198.51.100.7:443", "198.51.100.7"),
            ("[2001:db8::7]:443", "2001:db8::7"),
            # CloudFront writes IPv6 unbracketed: the port is still the last field
            ("2001:db8::7:46164", "2001:db8::7"),
            ("2001:db8:0:0:0:0:0:7:46164", "2001:db8::7"),
        ],
    )
    def test_viewer_address_forms(self, value, expected):
        settings = _settings(auth_client_ip_header="CloudFront-Viewer-Address")
        request = _request("127.0.0.1")
        request.scope["headers"].append((b"cloudfront-viewer-address", value.encode()))
        assert auth_throttle.client_ip(request, settings) == expected

    def test_viewer_address_from_an_untrusted_peer_is_ignored(self):
        settings = _settings(auth_client_ip_header="CloudFront-Viewer-Address")
        request = _request("203.0.113.50")
        request.scope["headers"].append((b"cloudfront-viewer-address", b"198.51.100.7:1"))
        assert auth_throttle.client_ip(request, settings) == "203.0.113.50"

    def test_missing_viewer_address_falls_back_to_the_hops(self):
        settings = _settings(
            auth_client_ip_header="CloudFront-Viewer-Address", auth_trusted_proxy_hops=2
        )
        request = _request("127.0.0.1", f"198.51.100.7, {CLOUDFRONT_EDGE}")
        assert auth_throttle.client_ip(request, settings) == "198.51.100.7"
        request.scope["headers"].append((b"cloudfront-viewer-address", b"garbage"))
        assert auth_throttle.client_ip(request, settings) == "198.51.100.7"
        # header configured, no hops, nothing usable: the peer
        bare = _request("127.0.0.1", "198.51.100.7")
        no_hops = _settings(auth_client_ip_header="CloudFront-Viewer-Address")
        assert auth_throttle.client_ip(bare, no_hops) == "127.0.0.1"

    def test_no_peer(self):
        assert auth_throttle.client_ip(_request(None), _settings()) == "unknown"

    def test_invalid_proxy_network_fails_at_startup(self):
        with pytest.raises(ValueError):
            Settings(auth_trusted_proxies=["not-a-network"])


class TestBackoffSchedule:
    def test_lockout_doubles_up_to_the_cap(self):
        now = [0.0]
        throttle = auth_throttle.AuthThrottle(clock=lambda: now[0])
        settings = _settings(
            auth_login_lockout_threshold=3, auth_login_lockout_base_s=60,
            auth_login_lockout_max_s=200,
        )
        assert [throttle.failed("u", "ip", settings) for _ in range(6)] == [
            0.0, 0.0, 60.0, 120.0, 200.0, 200.0,
        ]
        assert throttle.locked_for("u", "ip") == 200.0
        now[0] = 150.0
        assert throttle.locked_for("u", "ip") == 50.0
        assert throttle.locked_for("u", "other-ip") == 0.0
        throttle.succeeded("u", "ip")
        assert throttle.locked_for("u", "ip") == 0.0

    def test_sliding_window_reopens(self):
        now = [0.0]
        throttle = auth_throttle.AuthThrottle(clock=lambda: now[0])
        assert throttle.hit("login", "ip", 2, 10) == 0.0
        now[0] = 4.0
        assert throttle.hit("login", "ip", 2, 10) == 0.0
        assert throttle.hit("login", "ip", 2, 10) == 10.0 - 4.0
        assert throttle.hit("login", "other", 2, 10) == 0.0
        now[0] = 10.0
        assert throttle.hit("login", "ip", 2, 10) == 0.0
        assert throttle.hit("login", "ip", 0, 10) == 0.0  # 0 = disabled

    def test_tracked_keys_are_bounded(self, monkeypatch):
        monkeypatch.setattr(auth_throttle, "MAX_KEYS", 3)
        throttle = auth_throttle.AuthThrottle()
        for n in range(10):
            throttle.hit("register", f"ip{n}", 5, 60)
        assert list(throttle._windows["register"]) == ["ip7", "ip8", "ip9"]


@pytest.fixture
def env(monkeypatch):
    def apply(**values):
        for key, value in values.items():
            monkeypatch.setenv(f"LAUNCHPAD_{key.upper()}", str(value))
        get_settings.cache_clear()

    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN["password"])
    get_settings.cache_clear()
    yield apply
    get_settings.cache_clear()


@pytest.fixture
def client(env):
    # a loopback peer, like nginx in front of uvicorn
    with TestClient(create_app(), client=("127.0.0.1", 40000)) as test_client:
        yield test_client


def _via(viewer: str) -> dict[str, str]:
    return {"X-Forwarded-For": f"{viewer}, {CLOUDFRONT_EDGE}"}


def _login(client: TestClient, password: str, viewer: str = "198.51.100.7", user=None):
    return client.post(
        "/api/auth/login",
        json={"username": user or ADMIN["username"], "password": password},
        headers=_via(viewer),
    )


def _register(client: TestClient, n: int, viewer: str = "198.51.100.7"):
    return client.post(
        "/api/auth/register",
        json={
            "username": f"newcomer{n}",
            "email": f"newcomer{n}@acme-corp.com",
            "password": "sufficient-pass",
        },
        headers=_via(viewer),
    )


class TestLoginEndpoint:
    def test_wrong_passwords_lock_the_pair_even_against_the_right_one(self, env, client):
        env(auth_trusted_proxy_hops=2, auth_login_lockout_threshold=3)
        for _ in range(2):
            assert _login(client, "wrong").json()["code"] == "auth.invalid_credentials"
        # the third failure starts the lockout, and still answers as a wrong password
        assert _login(client, "wrong").status_code == 401
        locked = _login(client, ADMIN["password"])
        assert locked.status_code == 429
        assert locked.json()["code"] == "auth.login_locked"
        assert locked.json()["detail"]["retry_after_seconds"] == 60
        assert locked.headers["Retry-After"] == "60"
        # the same username from another address is untouched: no remote lock-out
        assert _login(client, ADMIN["password"], viewer="198.51.100.8").status_code == 200
        # ...and the lock follows the username case-insensitively
        assert _login(client, ADMIN["password"], user="OPERATOR ").status_code == 429

    def test_spoofed_forwarded_for_cannot_escape_the_lockout(self, env, client):
        env(auth_trusted_proxy_hops=2, auth_login_lockout_threshold=2)
        for _ in range(2):
            _login(client, "wrong")
        spoofed = client.post(
            "/api/auth/login",
            json=ADMIN,
            headers={"X-Forwarded-For": f"9.9.9.9, 198.51.100.7, {CLOUDFRONT_EDGE}"},
        )
        assert spoofed.json()["code"] == "auth.login_locked"

    def test_a_correct_password_clears_the_count(self, env, client):
        env(auth_trusted_proxy_hops=2, auth_login_lockout_threshold=3)
        for _ in range(2):
            _login(client, "wrong")
        assert _login(client, ADMIN["password"]).status_code == 200
        for _ in range(2):
            _login(client, "wrong")
        assert _login(client, ADMIN["password"]).status_code == 200

    def test_registered_members_are_locked_the_same_way(self, env, client):
        env(
            auth_trusted_proxy_hops=2, auth_login_lockout_threshold=2,
            auth_registration_require_approval="false",
        )
        assert _register(client, 1).status_code == 201
        for _ in range(2):
            assert _login(client, "wrong", user="newcomer1").status_code == 401
        locked = _login(client, "sufficient-pass", user="newcomer1")
        assert locked.json()["code"] == "auth.login_locked"

    def test_per_ip_window_counts_every_attempt(self, env, client):
        env(
            auth_trusted_proxy_hops=2, auth_login_rate_limit=3,
            auth_login_lockout_threshold=0,
        )
        assert _login(client, ADMIN["password"]).status_code == 200
        assert _login(client, "wrong", user="a").status_code == 401
        assert _login(client, "wrong", user="b").status_code == 401
        refused = _login(client, ADMIN["password"])
        assert refused.status_code == 429
        assert refused.json()["code"] == "auth.too_many_attempts"
        assert int(refused.headers["Retry-After"]) > 0
        assert _login(client, ADMIN["password"], viewer="198.51.100.8").status_code == 200

    def test_without_trusted_hops_every_caller_shares_the_peer_bucket(self, env, client):
        env(auth_login_rate_limit=2, auth_login_lockout_threshold=0)
        _login(client, ADMIN["password"], viewer="198.51.100.1")
        _login(client, ADMIN["password"], viewer="198.51.100.2")
        assert _login(client, ADMIN["password"], viewer="198.51.100.3").status_code == 429

    def test_an_open_console_is_not_throttled(self, monkeypatch):
        monkeypatch.setenv("LAUNCHPAD_AUTH_LOGIN_RATE_LIMIT", "1")
        get_settings.cache_clear()
        with TestClient(create_app()) as open_client:
            for _ in range(3):
                assert _login(open_client, "anything").status_code == 200
        get_settings.cache_clear()


class TestRegisterEndpoint:
    def test_per_ip_window(self, env, client):
        env(auth_trusted_proxy_hops=2, auth_register_rate_limit=2)
        assert _register(client, 1).status_code == 201
        assert _register(client, 2).status_code == 201
        refused = _register(client, 3)
        assert refused.status_code == 429
        assert refused.json()["code"] == "auth.too_many_attempts"
        assert _register(client, 3, viewer="198.51.100.8").status_code == 201

    def test_disabled_registration_answers_before_the_window(self, env, client):
        env(auth_registration_enabled="false", auth_register_rate_limit=1)
        for n in range(3):
            assert _register(client, n).json()["code"] == "auth.registration_disabled"

    def test_pending_queue_cap(self, env, client):
        env(auth_registration_max_pending=2, auth_register_rate_limit=0)
        assert _register(client, 1).status_code == 201
        assert _register(client, 2).status_code == 201
        full = _register(client, 3)
        assert full.status_code == 503
        assert full.json()["code"] == "auth.registration_queue_full"

    def test_pending_cap_does_not_apply_to_instant_accounts(self, env, client):
        env(
            auth_registration_max_pending=1, auth_register_rate_limit=0,
            auth_registration_require_approval="false",
        )
        for n in range(3):
            assert _register(client, n).status_code == 201
