"""Optional local username/password session authentication."""

import time

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.routers import auth


@pytest.fixture
def auth_env(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", "operator")
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", "s3cret-pass")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def auth_client(auth_env) -> TestClient:
    with TestClient(create_app()) as test_client:
        yield test_client


class TestAuthDisabled:
    def test_console_stays_open_without_password(self, client):
        assert client.get("/api/apikeys").status_code == 200
        assert client.get("/api/auth/status").json() == {
            "auth_required": False,
            "authenticated": True,
            "registration_enabled": False,
            "username": None,
            "role": None,
            "email": None,
            "account_expires_at": None,
        }

    def test_registration_is_refused_while_the_gate_is_open(self, client):
        response = client.post(
            "/api/auth/register",
            json={
                "username": "newcomer",
                "email": "newcomer@acme-corp.com",
                "password": "sufficient-pass",
            },
        )
        assert response.status_code == 400
        assert response.json()["code"] == "auth.registration_disabled"

    def test_login_is_noop_without_password(self, client):
        response = client.post(
            "/api/auth/login",
            json={"username": "anything", "password": "anything"},
        )
        assert response.status_code == 200
        assert response.json()["auth_required"] is False
        assert auth.COOKIE_NAME not in response.cookies


class TestAuthEnabled:
    def test_console_and_docs_are_blocked_without_session(self, auth_client):
        for path in ("/api/apikeys", "/api/docs", "/api/openapi.json"):
            response = auth_client.get(path)
            assert response.status_code == 401
            assert response.json() == {
                "code": "auth.required",
                "message": "Authentication required",
                "detail": None,
            }

    def test_public_paths_and_v1_contract_stay_open(self, auth_client):
        assert auth_client.get("/api/health").status_code == 200
        assert auth_client.get("/api/auth/status").json() == {
            "auth_required": True,
            "authenticated": False,
            "registration_enabled": True,
            # the configured identity is never disclosed before authentication
            "username": None,
            "role": None,
            "email": None,
            "account_expires_at": None,
        }
        response = auth_client.post(
            "/api/auth/login",
            json={"username": "operator", "password": "wrong"},
        )
        assert response.status_code == 401
        public = auth_client.post(
            "/v1/agents/missing/invoke",
            json={"input": "hello"},
        )
        assert public.status_code == 401
        assert public.json()["code"] == "auth.missing_api_key"

    def test_cors_preflight_is_not_blocked(self, auth_client):
        response = auth_client.options(
            "/api/apikeys",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "http://localhost:5173"

    def test_wrong_credentials_do_not_create_session(self, auth_client):
        for credentials in (
            {"username": "operator", "password": "wrong"},
            {"username": "wrong", "password": "s3cret-pass"},
        ):
            response = auth_client.post("/api/auth/login", json=credentials)
            assert response.status_code == 401
            assert response.json()["code"] == "auth.invalid_credentials"
            assert auth.COOKIE_NAME not in response.cookies
            assert auth_client.get("/api/apikeys").status_code == 401

    def test_login_grants_session_and_logout_clears_it(self, auth_client):
        response = auth_client.post(
            "/api/auth/login",
            json={"username": "operator", "password": "s3cret-pass"},
        )
        assert response.status_code == 200
        assert response.json()["username"] == "operator"
        assert response.json()["role"] == "admin"
        assert auth.COOKIE_NAME in response.cookies
        set_cookie = response.headers["set-cookie"].lower()
        assert "httponly" in set_cookie
        assert "samesite=lax" in set_cookie
        assert auth_client.get("/api/apikeys").status_code == 200
        assert auth_client.get("/api/auth/status").json()["username"] == "operator"

        logout = auth_client.post("/api/auth/logout")
        assert logout.status_code == 200
        assert auth_client.get("/api/apikeys").status_code == 401

    def test_tampered_and_expired_cookies_are_rejected(self, auth_client):
        auth_client.cookies.set(auth.COOKIE_NAME, "9999999999.deadbeef")
        assert auth_client.get("/api/apikeys").status_code == 401

        expired = auth._issue("operator", int(time.time()) - 10)
        auth_client.cookies.set(auth.COOKIE_NAME, expired)
        assert auth_client.get("/api/apikeys").status_code == 401

        # authentic signature, but the subject is nobody the platform knows
        stranger = auth._issue("stranger", int(time.time()) + 600)
        auth_client.cookies.set(auth.COOKIE_NAME, stranger)
        assert auth_client.get("/api/apikeys").status_code == 401

    def test_password_rotation_invalidates_session(
        self,
        auth_client,
        monkeypatch,
    ):
        auth_client.post(
            "/api/auth/login",
            json={"username": "operator", "password": "s3cret-pass"},
        )
        assert auth_client.get("/api/apikeys").status_code == 200

        monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", "rotated-password")
        get_settings.cache_clear()
        assert auth_client.get("/api/apikeys").status_code == 401

    def test_secure_cookie_setting(self, monkeypatch):
        monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", "operator")
        monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", "s3cret-pass")
        monkeypatch.setenv("LAUNCHPAD_AUTH_COOKIE_SECURE", "true")
        get_settings.cache_clear()
        try:
            with TestClient(create_app()) as client:
                response = client.post(
                    "/api/auth/login",
                    json={"username": "operator", "password": "s3cret-pass"},
                )
            assert "secure" in response.headers["set-cookie"].lower()
        finally:
            get_settings.cache_clear()
