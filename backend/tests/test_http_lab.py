"""Explicit demo HTTP transport preserves production authentication and one session."""

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import _assert_production_is_authenticated, create_app
from app.routers import auth
from app.services.local_exec import local_exec_enabled


@pytest.fixture
def lab(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_RUN_MODE", "prod")
    monkeypatch.setenv("LAUNCHPAD_CONSOLE_HTTP_LAB", "true")
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", "operator")
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", "s3cret-pass")
    get_settings.cache_clear()
    with TestClient(create_app(), base_url="http://lab.example:8088") as client:
        yield client
    get_settings.cache_clear()


def login(client):
    return client.post("/api/auth/login", json={"username": "operator", "password": "s3cret-pass"})


def test_explicit_lab_is_not_dev_or_an_auth_bypass():
    default = Settings(run_mode="prod")
    assert not default.console_http_lab
    assert auth.cookie_secure(default)
    assert auth.cookie_name(default) == auth.COOKIE_NAME
    lab = Settings(run_mode="prod", console_http_lab=True, auth_cookie_secure=True)
    assert not auth.cookie_secure(lab)
    assert not local_exec_enabled(lab)
    assert auth.cookie_name(lab) == auth.LAB_COOKIE_NAME
    with pytest.raises(RuntimeError):
        _assert_production_is_authenticated(
            Settings(
                run_mode="prod", console_http_lab=True, auth_password="", allow_open_console=False
            )
        )


def test_lab_requires_authentication_and_rejects_wrong_password(lab):
    assert lab.get("/api/agents").status_code == 401
    response = lab.post("/api/auth/login", json={"username": "operator", "password": "wrong"})
    assert response.status_code == 401
    assert not response.cookies
    assert lab.get("/api/auth/status").json()["auth_required"] is True


def test_one_cookie_on_both_transports_and_logout(lab):
    response = login(lab)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "launchpad_lab_session=" in cookie
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert "secure" not in cookie
    for url in ("http://lab.example:8088", "https://lab.example:8443"):
        response = lab.get(url + "/api/auth/status")
        assert response.json()["authenticated"] is True
        assert "strict-transport-security" not in response.headers
    lab.post("https://lab.example:8443/api/auth/logout")
    for url in ("http://lab.example:8088", "https://lab.example:8443"):
        assert lab.get(url + "/api/agents").status_code == 401


def test_old_secure_cookie_is_ignored_in_lab_mode(lab):
    # Real browser coverage also verifies the Secure cookie cannot be overwritten on HTTP.
    lab.cookies.set(auth.COOKIE_NAME, "old-https-session", domain="lab.example", path="/")
    assert lab.get("/api/agents").status_code == 401
    assert login(lab).status_code == 200
    assert lab.cookies.get(auth.COOKIE_NAME) == "old-https-session"
    assert lab.get("/api/auth/status").json()["authenticated"] is True
    lab.post("/api/auth/logout")
    assert lab.get("/api/agents").status_code == 401


def test_password_rotation_invalidates_same_session_on_both_transports(lab, monkeypatch):
    assert login(lab).status_code == 200
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", "rotated-password")
    get_settings.cache_clear()
    for url in ("http://lab.example:8088", "https://lab.example:8443"):
        assert lab.get(url + "/api/agents").status_code == 401


def test_forwarded_scheme_does_not_enable_http_mode(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_RUN_MODE", "prod")
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", "operator")
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", "s3cret-pass")
    monkeypatch.delenv("LAUNCHPAD_CONSOLE_HTTP_LAB", raising=False)
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/auth/login",
                json={"username": "operator", "password": "s3cret-pass"},
                headers={"X-Forwarded-Proto": "http"},
            )
            assert "Secure" in response.headers["set-cookie"]
            assert auth.COOKIE_NAME in response.cookies
            assert "strict-transport-security" in response.headers
    finally:
        get_settings.cache_clear()


def test_oauth_return_inherits_http_primary_origin():
    settings = Settings(
        run_mode="prod",
        console_http_lab=True,
        public_base_url="http://lab.example:8088",
        oauth_return_url="",
    )
    assert settings.resolved_oauth_return_url() == "http://lab.example:8088/auth/return"
