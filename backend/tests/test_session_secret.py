"""Session-cookie signing secret (r12-g, A-3): random, persisted 0600 on first start,
env override, rotation; never derived from the admin password."""

import os
import stat
import time

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import create_app
from app.routers import auth

ADMIN = {"username": "operator", "password": "s3cret-pass"}


@pytest.fixture(autouse=True)
def fresh_cache():
    auth.reset_session_secret_cache()
    yield
    auth.reset_session_secret_cache()


def test_first_use_generates_a_32_byte_hex_secret_at_0600(tmp_path):
    path = tmp_path / "nested" / "session_secret"
    settings = Settings(session_secret_file=str(path))
    value = auth.session_secret(settings)
    assert len(value) == 64 and int(value, 16) >= 0
    assert path.read_text().strip() == value
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    # cached and stable
    assert auth.session_secret(settings) == value
    auth.reset_session_secret_cache()
    assert auth.session_secret(settings) == value  # re-read from disk, same value


def test_an_existing_file_is_reused_and_an_empty_one_is_regenerated(tmp_path):
    path = tmp_path / "session_secret"
    path.write_text("operator-pinned-value\n")
    assert auth.session_secret(Settings(session_secret_file=str(path))) == "operator-pinned-value"
    auth.reset_session_secret_cache()
    path.write_text("")
    regenerated = auth.session_secret(Settings(session_secret_file=str(path)))
    assert len(regenerated) == 64
    assert path.read_text().strip() == regenerated


def test_env_override_wins_and_never_touches_the_file(tmp_path):
    path = tmp_path / "session_secret"
    settings = Settings(session_secret="pinned-by-operator", session_secret_file=str(path))
    assert auth.session_secret(settings) == "pinned-by-operator"
    assert not path.exists()


def test_signing_key_is_independent_of_the_admin_password(tmp_path):
    path = str(tmp_path / "session_secret")
    one = auth._signing_key(Settings(auth_password="a", session_secret_file=path))
    two = auth._signing_key(Settings(auth_password="b", session_secret_file=path))
    assert one == two
    other = auth._signing_key(Settings(auth_password="a", session_secret="different"))
    assert other != one


def test_cookie_survives_a_restart_with_the_same_file_and_dies_with_a_rotated_one(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN["password"])
    monkeypatch.setenv("LAUNCHPAD_SESSION_SECRET_FILE", str(tmp_path / "session_secret"))
    monkeypatch.delenv("LAUNCHPAD_SESSION_SECRET", raising=False)
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            assert client.post("/api/auth/login", json=ADMIN).status_code == 200
            cookie = client.cookies.get(auth.COOKIE_NAME)
        # "restart": a new process reads the same file → the cookie still verifies
        auth.reset_session_secret_cache()
        with TestClient(create_app()) as client:
            client.cookies.set(auth.COOKIE_NAME, cookie)
            assert client.get("/api/apikeys").status_code == 200
        # rotation on demand: delete the file and restart → every session signed out
        (tmp_path / "session_secret").unlink()
        auth.reset_session_secret_cache()
        with TestClient(create_app()) as client:
            client.cookies.set(auth.COOKIE_NAME, cookie)
            assert client.get("/api/apikeys").status_code == 401
            assert client.post("/api/auth/login", json=ADMIN).status_code == 200
    finally:
        get_settings.cache_clear()


def test_a_cookie_forged_with_the_admin_password_as_key_is_refused(monkeypatch, tmp_path):
    """The pre-r12-g derivation (sha256 of `…:{username}:{password}`) must not verify."""
    import hashlib
    import hmac

    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN["password"])
    monkeypatch.setenv("LAUNCHPAD_SESSION_SECRET_FILE", str(tmp_path / "session_secret"))
    monkeypatch.delenv("LAUNCHPAD_SESSION_SECRET", raising=False)
    get_settings.cache_clear()
    try:
        payload = auth._encode_payload(ADMIN["username"], int(time.time()) + 600)
        legacy_key = hashlib.sha256(
            f"agentcore-launchpad-session:{ADMIN['username']}:{ADMIN['password']}".encode()
        ).digest()
        forged = f"{payload}.{hmac.new(legacy_key, payload.encode(), hashlib.sha256).hexdigest()}"
        with TestClient(create_app()) as client:
            client.cookies.set(auth.COOKIE_NAME, forged)
            assert client.get("/api/apikeys").status_code == 401
    finally:
        get_settings.cache_clear()
