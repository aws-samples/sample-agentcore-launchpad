"""Console authorization table (T1).

Two things are asserted here, and the first is what makes the table trustworthy:

1. **No drift.** Every live `/api` route has an entry and every entry matches a
   live route. Without this the table would rot into a document that merely looks
   authoritative.
2. **The entries take effect.** Every `ADMIN` route answers 401 to an anonymous
   caller and 403 to a member, and does not 403 an admin.
"""

import functools
import inspect
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app.core import route_policy
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.core.route_policy import (
    ADMIN,
    MEMBER,
    PUBLIC,
    ROUTE_POLICY,
    WORKSPACE_EXEMPT,
    enforce_route_policy,
    is_hub_global,
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


def _walk(routes) -> list:
    """FastAPI 0.139 keeps included routers as `_IncludedRouter` wrappers instead of
    flattening them into `app.routes`, so this recurses rather than iterating."""
    found = []
    for route in routes:
        if type(route).__name__ == "_IncludedRouter":
            found += _walk(route.original_router.routes)
        else:
            found.append(route)
    return found


def _api_routes(app) -> set[tuple[str, str]]:
    """Every (method, path_format) the app actually serves under /api."""
    pairs = set()
    for route in _walk(app.routes):
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
PERMISSION_ROUTES = sorted(
    k for k, v in ROUTE_POLICY.items() if v.startswith("perm:")
)


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
        permission_roles = {
            f"perm:{key}" for key in users_service.AGENT_PERMISSIONS
        }
        assert set(ROUTE_POLICY.values()) <= {ADMIN, MEMBER, PUBLIC} | permission_roles

    def test_the_open_paths_are_public(self):
        """`auth_middleware` lets these through, so the table must agree."""
        for path in auth._OPEN_API_PATHS:
            entries = {v for (_, p), v in ROUTE_POLICY.items() if p == path}
            assert entries == {PUBLIC}, f"{path} is open in the middleware but {entries}"

    def test_the_workspace_dimension_matches_the_hub_global_rule(self):
        """The second dimension drifts in both directions too: a hub-global route
        cannot be left workspace-scoped, and a workspace-scoped one cannot be
        quietly exempted (absence from the set is the classification)."""
        derived = {
            (method, path)
            for (method, path), role in ROUTE_POLICY.items()
            if role == PUBLIC or is_hub_global(path)
        }
        assert WORKSPACE_EXEMPT == derived

    def test_the_hub_global_rule_matches_whole_segments(self):
        """A raw prefix match would exempt a future `/api/userspace` by accident,
        and an accidental exemption is the fail-open direction."""
        assert is_hub_global("/api/users") and is_hub_global("/api/users/{user_id}")
        assert not is_hub_global("/api/userspace")
        assert not is_hub_global("/api/authorizers")
        # D21: the model registry is hub-global (its per-workspace flags name the workspace
        # in the path); a look-alike prefix is not
        assert is_hub_global("/api/settings/model-providers")
        assert is_hub_global("/api/settings/model-providers/{provider_id}/models/{model_ref}")
        assert not is_hub_global("/api/settingsx")

    def test_model_registry_routes_are_member_crud_plus_two_admin_entries(self):
        """N17-obs203 (user decision 2026-10-21) supersedes the D21 / plan §9.1 ruling: model
        providers are owner-bound — every signed-in member may register, read, change and
        delete their OWN providers (the handler adds the visibility 404 and the creator-or-admin
        403), so the CRUD is MEMBER; the journal and the per-workspace flag matrix stay ADMIN.
        Still no member-grantable `models.manage` permission — ownership is the rule."""
        registry_routes = {k: v for k, v in ROUTE_POLICY.items()
                           if k[1].startswith("/api/settings/model-providers")}
        assert len(registry_routes) == 13
        admin_only = {("GET", "/api/settings/model-providers/changes"),
                      ("PUT", "/api/settings/model-providers/workspaces/{workspace_id}/models")}
        assert {k for k, v in registry_routes.items() if v == ADMIN} == admin_only
        assert {v for k, v in registry_routes.items() if k not in admin_only} == {MEMBER}
        assert set(registry_routes) <= WORKSPACE_EXEMPT
        assert "models.manage" not in users_service.AGENT_PERMISSIONS

    def test_public_routes_are_never_workspace_scoped(self):
        """A PUBLIC route has no identity, so there is nobody to resolve a
        workspace for."""
        public = {key for key, role in ROUTE_POLICY.items() if role == PUBLIC}
        assert public <= WORKSPACE_EXEMPT

    def test_the_console_surface_is_workspace_scoped(self):
        for key in [
            ("GET", "/api/agents"),
            ("POST", "/api/agents"),
            ("GET", "/api/jobs/{job_id}"),
            ("GET", "/api/apikeys"),
            ("POST", "/api/chat/{agent_id}"),
            ("GET", "/api/eval/runs"),
            ("GET", "/api/observability/sessions"),
        ]:
            assert key not in WORKSPACE_EXEMPT


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


def _admin_api_routes(app) -> list:
    """The served route objects (the routers' own `APIRoute`s) whose table entry is ADMIN."""
    return [route for route in _walk(app.routes)
            if any(ROUTE_POLICY.get((m, getattr(route, "path_format", ""))) == ADMIN
                   for m in getattr(route, "methods", None) or [])]


def _dependency_calls(dependant) -> list:
    out = []
    for sub in dependant.dependencies:
        out.append(sub.call)
        out += _dependency_calls(sub)
    return out


class TestAdminRoutesAreTableOnly:
    """N17-obs284 (r17-s2, design review C6): the table is the only route-level admin check.
    Up to cn/integration e1259c0e, 24 ADMIN handlers repeated it ("belt and braces"):
    `require_admin(request)` in system_agents ×4, `Depends(require_admin)` in users ×4,
    workspaces ×12, model_providers ×2 and model_roles ×2. They were deleted because the
    app-level `enforce_route_policy` dependency already refuses a member first; these tests
    prove that per route and keep the duplicates from coming back."""

    def test_every_admin_route_is_in_the_sweep(self, gated_app):
        swept = {(m, r.path_format) for r in _admin_api_routes(gated_app)
                 for m in r.methods if ROUTE_POLICY.get((m, r.path_format)) == ADMIN}
        assert swept == set(ADMIN_ROUTES)

    def test_no_admin_handler_repeats_the_table(self, gated_app):
        repeated = []
        for route in _admin_api_routes(gated_app):
            if auth.require_admin in _dependency_calls(route.dependant):
                repeated.append(f"{route.path_format}: Depends(require_admin)")
            if "require_admin(" in inspect.getsource(route.endpoint):
                repeated.append(f"{route.path_format}: require_admin(request)")
        assert not repeated, (
            f"ADMIN routes re-check what ROUTE_POLICY already enforces: {repeated} — the table "
            "is the only route-level admin check (core/route_policy.py docstring)"
        )

    @pytest.mark.parametrize(("method", "path_format"), ADMIN_ROUTES)
    def test_the_table_refuses_a_member_before_the_route_runs(
        self, gated_app, member_session, monkeypatch, method, path_format
    ):
        """Every route dependency and the handler are swapped for recorders; the table's
        `require_admin` raises a sentinel code. A member must get exactly that sentinel and
        nothing of the route may have run — the table is first and sufficient."""
        ran: list[str] = []
        for route in _admin_api_routes(gated_app):
            original = route.endpoint

            @functools.wraps(original)
            def recorder(*_args, _name=route.path_format, **_kwargs):
                ran.append(_name)
                return {}

            monkeypatch.setattr(route, "endpoint", recorder)

        def table_refuses(request):
            raise AppError("route_policy.sentinel", "refused by the table", status_code=403)

        monkeypatch.setattr(route_policy, "require_admin", table_refuses)
        response = member_session.request(method, _concrete(path_format))
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "route_policy.sentinel"
        assert ran == []

    def test_the_recorder_swap_reaches_the_handler(self, gated_app, admin_session, monkeypatch):
        """Guards the sweep above: the endpoint swap really takes effect, so an empty
        `ran` there means the handler did not run, not that the swap was ignored."""
        ran: list[str] = []
        route = next(r for r in _admin_api_routes(gated_app) if r.path_format == "/api/users")
        original = route.endpoint

        @functools.wraps(original)
        def recorder(*_args, **_kwargs):
            ran.append(route.path_format)
            return {"items": []}

        monkeypatch.setattr(route, "endpoint", recorder)
        assert admin_session.get("/api/users").status_code == 200
        assert ran == ["/api/users"]


class TestRootPathDoesNotBypassTheGuards:
    """N17-obs284 rev-r26 N1: with the per-route duplicates gone, `enforce_route_policy` is
    the single route-level guard. Its /api test used to read `request.url.path`, which keeps
    an ASGI `root_path` that routing strips, so a configured prefix would have skipped it for
    every route (and the auth middleware's 401 the same way)."""

    def test_the_app_sets_no_root_path_today(self):
        assert create_app().root_path == ""

    @pytest.fixture
    def prefixed(self, gated_app):
        gated_app.root_path = "/console"
        with TestClient(gated_app, client=("127.0.0.1", 4321)) as client:
            yield client

    def test_a_member_is_still_refused_on_an_admin_route(self, prefixed):
        prefixed.post("/console/api/auth/register", json=MEMBER_CREDS)
        db = SessionLocal()
        try:
            user = users_service.find_by_username(db, MEMBER_CREDS["username"])
            assert user is not None
            user.status = users_service.STATUS_ACTIVE
            user.expires_at = datetime.now(UTC) + timedelta(days=7)
            db.commit()
        finally:
            db.close()
        login = prefixed.post("/console/api/auth/login", json={
            "username": MEMBER_CREDS["username"], "password": MEMBER_CREDS["password"]})
        assert login.status_code == 200, login.text
        for method, path_format in ADMIN_ROUTES:
            response = prefixed.request(method, "/console" + _concrete(path_format))
            assert response.status_code == 403, (method, path_format, response.text)
            assert response.json()["code"] == "auth.forbidden"

    def test_an_anonymous_caller_is_still_401(self, prefixed):
        response = prefixed.get("/console/api/users")
        assert response.status_code == 401, response.text
        assert response.json()["code"] == "auth.required"


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


class TestPermissionRoutesAreEnforced:
    """`perm:agents.*` routes: anonymous 401; a fully-revoked member 403 with
    the dedicated code. The granted-member (default) path is covered end-to-end
    in tests/test_member_permissions.py."""

    @pytest.mark.parametrize(("method", "path_format"), PERMISSION_ROUTES)
    def test_anonymous_gets_401(self, anon_session, method, path_format):
        response = anon_session.request(method, _concrete(path_format))
        assert response.status_code == 401, response.text
        assert response.json()["code"] == "auth.required"

    @pytest.mark.parametrize(("method", "path_format"), PERMISSION_ROUTES)
    def test_fully_revoked_member_gets_403(self, member_session, method, path_format):
        db = SessionLocal()
        try:
            user = users_service.find_by_username(db, MEMBER_CREDS["username"])
            assert user is not None
            user.permissions = dict.fromkeys(users_service.AGENT_PERMISSIONS, False)
            db.commit()
        finally:
            db.close()
        response = member_session.request(method, _concrete(path_format))
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "auth.permission_required"


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
