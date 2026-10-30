"""`/v1` invalid-key rate limiting (r12-g, N12-obs57): missing / unknown / disabled
`X-Api-Key` attempts count per source IP; over the window the answer is 429
`auth.rate_limited` with `Retry-After`; valid keys never count; `last_used_at` keeps
being stamped as before."""

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.main import create_app
from app.models.ledger import ApiKey
from app.routers.apikeys import hash_key
from app.services import login_guard

RAW = "lp_live_r12g_valid_key_0001"


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_API_KEY_IP_LIMIT", "4")
    monkeypatch.setenv("LAUNCHPAD_AUTH_API_KEY_IP_WINDOW_SECONDS", "300")
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


def _mint() -> str:
    db = SessionLocal()
    try:
        key = ApiKey(
            workspace_id=DEFAULT_WORKSPACE_ID, name="r12g", prefix="lp_live_r12g…",
            key_hash=hash_key(RAW),
        )
        db.add(key)
        db.commit()
        return key.id
    finally:
        db.close()


def _get(client: TestClient, raw: str | None):
    headers = {"X-Api-Key": raw} if raw is not None else {}
    return client.get("/v1/agents", headers=headers)


def test_invalid_attempts_from_one_peer_are_limited_then_429_with_retry_after(app):
    with TestClient(app, client=("203.0.113.9", 40000)) as client:
        codes = [_get(client, f"lp_live_bogus_{i}").status_code for i in range(3)]
        codes.append(_get(client, None).status_code)  # a missing header counts too
        assert codes == [401] * 4
        limited = _get(client, "lp_live_bogus_x")
        assert limited.status_code == 429
        body = limited.json()
        assert body["code"] == "auth.rate_limited"
        assert 1 <= body["detail"]["retry_after"] <= 300
        assert limited.headers["Retry-After"] == str(body["detail"]["retry_after"])
        # rev-r11: the window only governs FAILED checks — a valid key from the same peer
        # (behind nginx / the NLB that is everyone) is never refused by it
        _mint()
        assert _get(client, RAW).status_code == 200
        assert _get(client, "lp_live_bogus_y").status_code == 429


def test_the_window_is_per_source_ip(app):
    with TestClient(app, client=("203.0.113.9", 40000)) as noisy:
        for i in range(4):
            assert _get(noisy, f"lp_live_bogus_{i}").status_code == 401
        assert _get(noisy, "lp_live_bogus_x").status_code == 429
    with TestClient(app, client=("198.51.100.7", 40000)) as other:
        assert _get(other, "lp_live_bogus_y").status_code == 401


def test_valid_keys_never_count_and_last_used_is_still_stamped(app):
    key_id = _mint()
    with TestClient(app, client=("203.0.113.9", 40000)) as client:
        for _ in range(8):
            assert _get(client, RAW).status_code == 200
        assert login_guard.api_key_peers.count("203.0.113.9", 300.0) == 0
    db = SessionLocal()
    try:
        assert db.get(ApiKey, key_id).last_used_at is not None
    finally:
        db.close()


def test_limit_zero_disables_the_window(monkeypatch, app):
    monkeypatch.setenv("LAUNCHPAD_AUTH_API_KEY_IP_LIMIT", "0")
    get_settings.cache_clear()
    with TestClient(app, client=("203.0.113.9", 40000)) as client:
        assert [_get(client, f"lp_live_bogus_{i}").status_code for i in range(10)] == [401] * 10
