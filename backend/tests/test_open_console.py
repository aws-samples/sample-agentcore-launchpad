"""An unauthenticated console must refuse non-loopback callers (T1).

The guard lives in `auth_middleware` rather than at startup because that is the
only place the caller's address is known — `create_app()` cannot see uvicorn's
`--host`, so a startup-only check is bypassed by launching uvicorn directly.

`TestClient` presents the literal peer "testclient", which is not a loopback IP,
so it exercises the refusal path by default; `conftest.py` sets
LAUNCHPAD_ALLOW_OPEN_CONSOLE for the rest of the suite and these tests clear it.
"""

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import _assert_production_is_authenticated, create_app


@pytest.fixture
def open_console(monkeypatch):
    """No password configured and no open-console override."""
    monkeypatch.delenv("LAUNCHPAD_ALLOW_OPEN_CONSOLE", raising=False)
    monkeypatch.delenv("LAUNCHPAD_AUTH_PASSWORD", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def remote_client(open_console) -> TestClient:
    """A client whose transport peer is a routable address."""
    with TestClient(create_app(), client=("203.0.113.5", 51234)) as test_client:
        yield test_client


@pytest.fixture
def loopback_client(open_console) -> TestClient:
    with TestClient(create_app(), client=("127.0.0.1", 51234)) as test_client:
        yield test_client


class TestNonLoopbackIsRefused:
    def test_api_route_is_refused(self, remote_client):
        response = remote_client.get("/api/apikeys")
        assert response.status_code == 403
        assert response.json()["code"] == "auth.open_console_refused"

    def test_the_message_names_both_remedies(self, remote_client):
        message = remote_client.get("/api/apikeys").json()["message"]
        assert "LAUNCHPAD_AUTH_PASSWORD" in message
        assert "LAUNCHPAD_ALLOW_OPEN_CONSOLE" in message

    def test_health_stays_reachable(self, remote_client):
        # start.py polls /api/health to decide the service came up.
        assert remote_client.get("/api/health").status_code == 200

    def test_auth_status_and_login_stay_reachable(self, remote_client):
        # A locked-out operator still needs to see the gate and log in.
        assert remote_client.get("/api/auth/status").status_code == 200
        assert (
            remote_client.post(
                "/api/auth/login", json={"username": "a", "password": "b"}
            ).status_code
            == 200
        )

    def test_public_v1_surface_is_untouched(self, remote_client):
        # /v1 has its own X-Api-Key auth; this guard must not shadow it.
        assert remote_client.get("/v1/agents").status_code != 403


class TestLoopbackIsAllowed:
    def test_local_operator_keeps_full_access(self, loopback_client):
        assert loopback_client.get("/api/apikeys").status_code == 200


class TestOverride:
    def test_allow_open_console_restores_access(self, monkeypatch):
        monkeypatch.delenv("LAUNCHPAD_AUTH_PASSWORD", raising=False)
        monkeypatch.setenv("LAUNCHPAD_ALLOW_OPEN_CONSOLE", "true")
        get_settings.cache_clear()
        try:
            with TestClient(create_app(), client=("203.0.113.5", 51234)) as client:
                assert client.get("/api/apikeys").status_code == 200
        finally:
            get_settings.cache_clear()


class TestAuthenticatedConsoleIsUnaffected:
    def test_a_password_makes_the_peer_irrelevant(self, monkeypatch):
        """With the gate on, remote callers get the normal 401 — not the refusal."""
        monkeypatch.delenv("LAUNCHPAD_ALLOW_OPEN_CONSOLE", raising=False)
        monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", "s3cret-pass")
        get_settings.cache_clear()
        try:
            with TestClient(create_app(), client=("203.0.113.5", 51234)) as client:
                response = client.get("/api/apikeys")
                assert response.status_code == 401
                assert response.json()["code"] == "auth.required"
        finally:
            get_settings.cache_clear()


class TestProductionStartupAssertion:
    def test_prod_without_auth_refuses_to_build(self, monkeypatch):
        monkeypatch.delenv("LAUNCHPAD_ALLOW_OPEN_CONSOLE", raising=False)
        monkeypatch.delenv("LAUNCHPAD_AUTH_PASSWORD", raising=False)
        monkeypatch.setenv("LAUNCHPAD_RUN_MODE", "prod")
        get_settings.cache_clear()
        try:
            with pytest.raises(RuntimeError, match="production mode"):
                create_app()
        finally:
            get_settings.cache_clear()

    def test_prod_with_auth_builds(self, monkeypatch):
        monkeypatch.setenv("LAUNCHPAD_RUN_MODE", "prod")
        monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", "s3cret-pass")
        get_settings.cache_clear()
        try:
            _assert_production_is_authenticated(get_settings())
        finally:
            get_settings.cache_clear()

    def test_dev_without_auth_builds(self, open_console):
        _assert_production_is_authenticated(get_settings())


class TestTransportSecurity:
    """Secure cookies and HSTS follow run_mode (T9)."""

    def _client(self, monkeypatch, mode: str) -> TestClient:
        monkeypatch.setenv("LAUNCHPAD_RUN_MODE", mode)
        monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", "s3cret-pass")
        monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", "operator")
        get_settings.cache_clear()
        return TestClient(create_app(), client=("127.0.0.1", 4321))

    def test_prod_marks_the_session_cookie_secure(self, monkeypatch):
        try:
            with self._client(monkeypatch, "prod") as client:
                response = client.post(
                    "/api/auth/login",
                    json={"username": "operator", "password": "s3cret-pass"},
                )
                assert response.status_code == 200
                assert "Secure" in response.headers["set-cookie"]
        finally:
            get_settings.cache_clear()

    def test_dev_does_not(self, monkeypatch):
        """A Secure cookie over a plain-HTTP dev origin is never sent back, which
        would silently break local sign-in."""
        try:
            with self._client(monkeypatch, "dev") as client:
                response = client.post(
                    "/api/auth/login",
                    json={"username": "operator", "password": "s3cret-pass"},
                )
                assert response.status_code == 200
                assert "Secure" not in response.headers["set-cookie"]
        finally:
            get_settings.cache_clear()

    def test_auth_cookie_secure_still_forces_it_on_in_dev(self, monkeypatch):
        monkeypatch.setenv("LAUNCHPAD_AUTH_COOKIE_SECURE", "true")
        try:
            with self._client(monkeypatch, "dev") as client:
                response = client.post(
                    "/api/auth/login",
                    json={"username": "operator", "password": "s3cret-pass"},
                )
                assert "Secure" in response.headers["set-cookie"]
        finally:
            get_settings.cache_clear()

    def test_hsts_is_emitted_in_prod_only(self, monkeypatch):
        try:
            with self._client(monkeypatch, "prod") as client:
                assert "Strict-Transport-Security" in client.get("/api/health").headers
        finally:
            get_settings.cache_clear()
        try:
            with self._client(monkeypatch, "dev") as client:
                assert "Strict-Transport-Security" not in client.get("/api/health").headers
        finally:
            get_settings.cache_clear()
