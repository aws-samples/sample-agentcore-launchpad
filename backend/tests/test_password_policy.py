"""Password policy (r12-g, N12-obs57): minimum length 12 (configurable), never equal
to the username / email / email local part, never a top-common password; applied on
registration and on an admin reset, never to stored hashes.

Relaxed mode (r14-c, N14-obs3, user decision 2026-10-19): `auth_password_policy:
relaxed` keeps only the length rule, and the floor of `auth_password_min_length` is 6.
"""

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.main import create_app
from app.services import users as users_service

ADMIN = {"username": "operator", "password": "s3cret-pass"}
BASE = {"username": "qa-user", "email": "qa.user@acme-corp.com"}


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN["password"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_REGISTRATION_REQUIRE_APPROVAL", "false")
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


@pytest.fixture
def anon(app):
    with TestClient(app) as client:
        yield client


@pytest.fixture
def admin(app):
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json=ADMIN).status_code == 200
        yield client


def _register(client, password: str, **overrides):
    return client.post("/api/auth/register", json={**BASE, "password": password, **overrides})


@pytest.mark.parametrize(
    ("password", "reason"),
    [
        ("short-pass1", "too_short"),  # 11 chars
        ("qa-user", "too_short"),
        ("qa.user@acme-corp.com", "matches_identity"),
        ("QA.USER@ACME-CORP.COM", "matches_identity"),
        ("password1234", "common"),
        ("Password-1234!", "common"),  # separators / case do not rescue a common one
        ("administrator", "common"),
        ("launchpad2026", "common"),
    ],
)
def test_registration_refuses_weak_passwords_with_a_reason(anon, password, reason):
    response = _register(anon, password)
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["code"] == "auth.weak_password"
    assert body["detail"] == {"reason": reason, "min_length": 12}


def test_username_equal_to_password_is_refused_once_long_enough(anon):
    response = _register(anon, "long-user-name", username="long-user-name")
    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "matches_identity"


def test_a_twelve_character_password_passes(anon):
    assert _register(anon, "Tr0ub4dor&3xy").status_code == 201


def test_min_length_is_configurable_and_reported(monkeypatch, app):
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD_MIN_LENGTH", "16")
    get_settings.cache_clear()
    with TestClient(app) as client:
        response = _register(client, "Tr0ub4dor&3xy")
        assert response.status_code == 400
        assert response.json()["detail"] == {"reason": "too_short", "min_length": 16}
        assert "16" in response.json()["message"]


def test_admin_reset_applies_the_same_policy_and_generated_passwords_pass(admin, anon):
    assert _register(anon, "Tr0ub4dor&3xy").status_code == 201
    db = SessionLocal()
    try:
        user_id = users_service.find_by_username(db, BASE["username"]).id
    finally:
        db.close()
    weak = admin.patch(f"/api/users/{user_id}", json={"password": "password1234"})
    assert weak.status_code == 400
    assert weak.json()["detail"]["reason"] == "common"
    identity = admin.patch(f"/api/users/{user_id}", json={"password": BASE["email"]})
    assert identity.json()["detail"]["reason"] == "matches_identity"
    generated = admin.patch(f"/api/users/{user_id}", json={"password": None})
    assert generated.status_code == 200
    assert len(generated.json()["generated_password"]) >= 12


def test_existing_accounts_keep_signing_in_with_a_pre_policy_password(anon):
    """A hash stored under the old 8-character rule is never re-validated."""
    db = SessionLocal()
    try:
        user = users_service.register_user(
            db, "legacy-user", "legacy@acme-corp.com", "Tr0ub4dor&3xy",
            Settings(auth_username="operator", auth_password="s3cret-pass",
                     auth_registration_require_approval=False),
        )
        user.password_hash = users_service.hash_password("eightchr")  # pre-r12-g
        db.commit()
    finally:
        db.close()
    response = anon.post(
        "/api/auth/login", json={"username": "legacy-user", "password": "eightchr"}
    )
    assert response.status_code == 200


def test_generate_password_never_undercuts_the_configured_minimum():
    strict = Settings(auth_password_min_length=20)
    assert len(users_service.generate_password(settings=strict)) == 20
    assert len(users_service.generate_password(settings=Settings())) == 14


def test_validate_password_direct_messages_never_echo_the_password():
    with pytest.raises(AppError) as exc:
        users_service.validate_password("password1234", settings=Settings())
    assert "password1234" not in exc.value.message


# --- relaxed mode (r14-c, N14-obs3) -------------------------------------------


def _relaxed(**overrides) -> Settings:
    return Settings(auth_password_policy="relaxed", **overrides)


def test_strict_is_the_default_and_the_status_payload_says_so(anon):
    assert Settings().auth_password_policy == "strict"
    assert anon.get("/api/auth/status").json()["password_policy"] == {
        "mode": "strict",
        "min_length": 12,
    }


def test_relaxed_accepts_a_common_password_that_strict_refuses():
    users_service.validate_password("password", settings=_relaxed(auth_password_min_length=8))
    with pytest.raises(AppError) as exc:
        users_service.validate_password(
            "password", settings=Settings(auth_password_min_length=8)
        )
    assert exc.value.detail == {"reason": "common", "min_length": 8}


def test_relaxed_accepts_an_identity_match():
    settings = _relaxed(auth_password_min_length=8)
    assert (
        users_service.validate_password(
            "qa.user@acme-corp.com", username="qa-user", email=BASE["email"], settings=settings
        )
        == "qa.user@acme-corp.com"
    )
    assert users_service.validate_password(
        "long-user-name", username="long-user-name", settings=settings
    )


def test_relaxed_floor_six_accepts_six_and_refuses_five():
    settings = _relaxed(auth_password_min_length=6)
    assert users_service.validate_password("abc123", settings=settings) == "abc123"
    with pytest.raises(AppError) as exc:
        users_service.validate_password("abc12", settings=settings)
    assert exc.value.code == "auth.weak_password"
    assert exc.value.detail == {"reason": "too_short", "min_length": 6}


def test_relaxed_only_ever_reports_too_short(monkeypatch, app):
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD_POLICY", "relaxed")
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD_MIN_LENGTH", "8")
    get_settings.cache_clear()
    with TestClient(app) as client:
        assert client.get("/api/auth/status").json()["password_policy"] == {
            "mode": "relaxed",
            "min_length": 8,
        }
        refused = _register(client, "short")
        assert refused.status_code == 400
        assert refused.json()["detail"] == {"reason": "too_short", "min_length": 8}
        # strict's `common` and `matches_identity` refusals do not exist in relaxed mode
        assert _register(client, "password").status_code == 201
        assert _register(
            client, "qa.user@acme-corp.com", username="qa-user2", email="qa.user2@acme-corp.com"
        ).status_code == 201


def test_relaxed_admin_reset_generates_at_least_the_configured_minimum():
    short = _relaxed(auth_password_min_length=6)
    long = _relaxed(auth_password_min_length=20)
    assert len(users_service.generate_password(settings=short)) == 14
    assert len(users_service.generate_password(settings=long)) == 20


def test_password_policy_helper_reports_the_effective_minimum():
    assert users_service.password_policy(_relaxed(auth_password_min_length=6)) == {
        "mode": "relaxed",
        "min_length": 6,
    }
    assert users_service.password_policy(Settings()) == {"mode": "strict", "min_length": 12}
