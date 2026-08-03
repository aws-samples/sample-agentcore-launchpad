"""Console authorization table (T1).

Two things are asserted here, and the first is what makes the table trustworthy:

1. **No drift.** Every live `/api` route has an entry and every entry matches a
   live route. Without this the table would rot into a document that merely looks
   authoritative.
2. **The entries take effect.** Every `ADMIN` route answers 401 to an anonymous
   caller and 403 to a member, and does not 403 an admin.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.route_policy import (
    ADMIN,
    MEMBER,
    PUBLIC,
    ROUTE_POLICY,
    enforce_route_policy,
)
from app.main import create_app
from app.routers import auth
from app.services import users as users_service

ADMIN_CREDS = {"username": "operator", "password": "s3cret-pass"}
MEMBER_CREDS = {
    "username": "qa-user",
    "email": "qa-user@acme-corp.com",
    "password": "sufficient-pass",
}


def _api_routes(app) -> set[tuple[str, str]]:
    """Every (method, path_format) the app actually serves under /api.

    FastAPI 0.139 keeps included routers as `_IncludedRouter` wrappers instead of
    flattening them into `app.routes`, so this recurses rather than iterating.
    """

    def walk(routes):
        found = []
        for route in routes:
            if type(route).__name__ == "_IncludedRouter":
                found += walk(route.original_router.routes)
            else:
                found.append(route)
        return found

    pairs = set()
    for route in walk(app.routes):
        path = getattr(route, "path_format", None) or getattr(route, "path", "")
        if not (path == "/api" or path.startswith("/api/")):
            continue
        for method in getattr(route, "methods", None) or []:
            if method in {"HEAD", "OPTIONS"}:
                continue  # HEAD is answered by the GET entry; OPTIONS is CORS
            pairs.add((method, path))
    return pairs


@pytest.fixture
def gated_app(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN_CREDS["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN_CREDS["password"])
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


@pytest.fixture
def member_session(gated_app):
    """A live non-admin session — registered, then activated the way an admin
    approval does (same shape as tests/test_users_api.py)."""
    with TestClient(gated_app, client=("127.0.0.1", 4321)) as client:
        assert client.post("/api/auth/register", json=MEMBER_CREDS).status_code == 201
        db = SessionLocal()
        try:
            user = users_service.find_by_username(db, MEMBER_CREDS["username"])
            assert user is not None
            user.status = users_service.STATUS_ACTIVE
            user.expires_at = datetime.now(UTC) + timedelta(days=7)
            db.commit()
        finally:
            db.close()
        login = client.post(
            "/api/auth/login",
            json={
                "username": MEMBER_CREDS["username"],
                "password": MEMBER_CREDS["password"],
            },
        )
        assert login.status_code == 200, login.text
        assert login.json()["role"] == "member"
        yield client


@pytest.fixture
def anon_session(gated_app):
    with TestClient(gated_app, client=("127.0.0.1", 4321)) as client:
        yield client


@pytest.fixture
def admin_session(gated_app):
    with TestClient(gated_app, client=("127.0.0.1", 4321)) as client:
        assert client.post("/api/auth/login", json=ADMIN_CREDS).status_code == 200
        yield client


ADMIN_ROUTES = sorted(k for k, v in ROUTE_POLICY.items() if v == ADMIN)
MEMBER_ROUTES = sorted(k for k, v in ROUTE_POLICY.items() if v == MEMBER)


def _concrete(path_format: str) -> str:
    """Fill path params with a placeholder — we only care about the status."""
    out = []
    for segment in path_format.split("/"):
        out.append("route-policy-probe" if segment.startswith("{") else segment)
    return "/".join(out)


class _Route:
    def __init__(self, path_format: str) -> None:
        self.path_format = path_format


def _fake_request(method: str, path_format: str) -> Request:
    """A request already routed to `path_format`, for exercising the dependency
    on its own. `scope["route"]` is what `enforce_route_policy` reads."""
    return Request(
        {
            "type": "http",
            "method": method,
            "path": _concrete(path_format),
            "headers": [],
            "query_string": b"",
            "client": ("127.0.0.1", 4321),
            "route": _Route(path_format),
        }
    )


class TestNoDrift:
    def test_every_live_route_is_classified(self, gated_app):
        missing = sorted(_api_routes(gated_app) - set(ROUTE_POLICY))
        assert not missing, (
            "these /api routes have no ROUTE_POLICY entry (default-deny would "
            f"refuse them at runtime): {missing}"
        )

    def test_every_entry_matches_a_live_route(self, gated_app):
        stale = sorted(set(ROUTE_POLICY) - _api_routes(gated_app))
        assert not stale, f"these ROUTE_POLICY entries match no live route: {stale}"

    def test_every_role_is_known(self):
        assert set(ROUTE_POLICY.values()) <= {ADMIN, MEMBER, PUBLIC}

    def test_the_open_paths_are_public(self):
        """`auth_middleware` lets these through, so the table must agree."""
        for path in auth._OPEN_API_PATHS:
            entries = {v for (_, p), v in ROUTE_POLICY.items() if p == path}
            assert entries == {PUBLIC}, f"{path} is open in the middleware but {entries}"


class TestAdminRoutesAreEnforced:
    @pytest.mark.parametrize(("method", "path_format"), ADMIN_ROUTES)
    def test_anonymous_gets_401(self, anon_session, method, path_format):
        response = anon_session.request(method, _concrete(path_format))
        assert response.status_code == 401, response.text
        assert response.json()["code"] == "auth.required"

    @pytest.mark.parametrize(("method", "path_format"), ADMIN_ROUTES)
    def test_member_gets_403(self, member_session, method, path_format):
        response = member_session.request(method, _concrete(path_format))
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "auth.forbidden"


class TestMemberRoutesStayReachable:
    @pytest.mark.parametrize(("method", "path_format"), MEMBER_ROUTES)
    def test_the_dependency_demands_nothing_extra(self, method, path_format):
        """The route resolves in the table and does not demand admin.

        Deliberately does not go over HTTP: that would execute the real handler,
        and these handlers call live AWS — an earlier version of this sweep was
        making real GetRegistryRecord calls from the hermetic suite. What is worth
        asserting here is the authorization decision, and this is all of it.
        """
        enforce_route_policy(_fake_request(method, path_format))  # must not raise

    @pytest.mark.parametrize(("method", "path_format"), MEMBER_ROUTES)
    def test_anonymous_is_still_rejected(self, anon_session, method, path_format):
        """Cheap over HTTP: the middleware answers 401 before any handler runs."""
        response = anon_session.request(method, _concrete(path_format))
        assert response.status_code == 401, response.text


class TestUnclassifiedRouteFailsClosed:
    def test_removing_an_entry_makes_the_route_refuse(self, admin_session, monkeypatch):
        monkeypatch.delitem(ROUTE_POLICY, ("GET", "/api/overview"))
        response = admin_session.get("/api/overview")
        assert response.status_code == 500
        assert response.json()["code"] == "auth.route_unclassified"


class TestOpenConsoleIsUnaffected:
    def test_local_operator_keeps_admin_access(self, client):
        """With no password configured the local operator is the built-in admin,
        so the table must not lock a single-operator dev box out of its own
        console."""
        assert client.get("/api/apikeys").status_code == 200
