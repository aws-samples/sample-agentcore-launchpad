"""E18 acceptance gap (`plans/acceptance/E18-users.md` §5: NC-E18-09 … 12) — the admin
`PATCH /api/users/{id}` 4xx matrix, hermetically, with the exact code per body:

* 422 `validation.invalid_request` — what the `UserPatch` schema refuses before the service
  runs: wrong types, out-of-range `extend_days` (pydantic `ge/le`, so the plan's
  `users.invalid_extension` is never reached over HTTP), a non-strict boolean in
  `permissions`, both `workspaces` and `workspace_ids`, an empty body, and — N13-obs9 —
  **unknown keys** (`extra="forbid"`: a mistyped `expire_at` used to be silently ignored).
* 400 `users.invalid_status` / `invalid_role` / `invalid_permissions` / `invalid_workspaces`,
  `auth.weak_password` — what `services.users.apply_patch` refuses on a well-typed body.
* 404 `users.not_found` — PATCH / DELETE of an unknown id.

A refused patch changes nothing: the row re-read afterwards equals the row before.

The last-admin / self-delete guards (E18 NC-14 / 15, Q2a `cn/r12-g`) are NOT covered here:
`cnfork/cn/r12-g` was not pushed when this file was written (lead brief: skip and say so).

Fixtures are `tests/test_users_api.py`'s (`app`, `anon`, `admin`; imported — registration).
"""

from __future__ import annotations

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID
from tests.test_users_api import (  # noqa: F401 — fixtures registered here
    MEMBER,
    admin,
    anon,
    app,
    register,
    stored,
)


@pytest.fixture
def admin_client(request):
    """`tests.test_users_api.admin` by name (the import above is the registration only)."""
    return request.getfixturevalue("admin")


@pytest.fixture
def member_id(request) -> str:
    """A pending registered member to patch (registered through the public route)."""
    anon_client = request.getfixturevalue("anon")
    res = register(anon_client)
    assert res.status_code == 201, res.text
    return stored().id


def _snapshot(username: str = MEMBER["username"]) -> dict:
    user = stored(username)
    return {
        "status": user.status, "role": user.role, "expires_at": user.expires_at,
        "password_hash": user.password_hash, "permissions": user.permissions,
    }


# ─── 422: the schema refuses the body ─────────────────────────────────────────────────
@pytest.mark.parametrize(("body", "loc_tail"), [
    pytest.param({"status": "active", "bogus": 1}, ("bogus",), id="unknown-key"),
    pytest.param({"expire_at": "2030-01-01T00:00:00Z"}, ("expire_at",), id="mistyped-key"),
    pytest.param({"role": 5}, ("role",), id="role-not-a-string"),
    pytest.param({"status": ["active"]}, ("status",), id="status-not-a-string"),
    pytest.param({"expires_at": "not-a-date"}, ("expires_at",), id="expires_at-garbage"),
    pytest.param({"expires_at": "2026-13-01T00:00:00Z"}, ("expires_at",), id="expires_at-month"),
    pytest.param({"extend_days": 0}, ("extend_days",), id="extend_days-zero"),
    pytest.param({"extend_days": 3651}, ("extend_days",), id="extend_days-over-max"),
    pytest.param({"permissions": {"agents.deploy": "no"}}, ("permissions", "agents.deploy"),
                 id="permissions-non-strict-bool"),
    pytest.param({"permissions": {"agents.deploy": 0}}, ("permissions", "agents.deploy"),
                 id="permissions-int-not-bool"),
    pytest.param({"permissions": ["agents.deploy"]}, ("permissions",), id="permissions-list"),
    pytest.param({"workspace_ids": "default"}, ("workspace_ids",), id="workspace_ids-string"),
    pytest.param({"workspace_ids": [1]}, ("workspace_ids", 0), id="workspace_ids-int-item"),
    pytest.param({"workspaces": ["default"], "workspace_ids": ["default"]}, (),
                 id="both-grant-shapes"),
    pytest.param({}, (), id="empty-body"),
    pytest.param({"password": "x" * 257}, ("password",), id="password-over-256"),
])
def test_bodies_the_schema_refuses_are_422_and_change_nothing(admin_client, member_id, body,
                                                               loc_tail):
    before = _snapshot()
    res = admin_client.patch(f"/api/users/{member_id}", json=body)
    assert res.status_code == 422, res.text
    payload = res.json()
    assert payload["code"] == "validation.invalid_request"
    assert payload["message"] == "Request validation failed"
    assert payload["detail"], payload  # pydantic's per-field errors ride in `detail`
    locs = [tuple(e["loc"]) for e in payload["detail"]]
    assert any(loc[-len(loc_tail):] == loc_tail if loc_tail else loc == ("body",)
               for loc in locs), locs
    assert _snapshot() == before


def test_an_unknown_key_next_to_a_valid_change_is_refused_whole(admin_client, member_id):
    """N13-obs9: before `extra="forbid"` this was a 200 that approved the account while the
    mistyped expiry was dropped on the floor."""
    res = admin_client.patch(f"/api/users/{member_id}",
                             json={"status": "active", "expire_at": "2030-01-01T00:00:00Z"})
    assert res.status_code == 422
    [error] = [e for e in res.json()["detail"] if e["type"] == "extra_forbidden"]
    assert error["loc"] == ["body", "expire_at"]
    assert stored().status == "pending"  # the valid half was not applied either


# ─── 400: the service refuses a well-typed body ───────────────────────────────────────
@pytest.mark.parametrize(("body", "code", "needle"), [
    pytest.param({"status": "sleepy"}, "users.invalid_status", "pending, active, disabled",
                 id="status-unknown"),
    pytest.param({"status": None}, "users.invalid_status", "Status must be one of",
                 id="status-null"),
    pytest.param({"role": "owner"}, "users.invalid_role", "admin or member", id="role-owner"),
    pytest.param({"role": "Admin"}, "users.invalid_role", "admin or member",
                 id="role-wrong-case"),
    pytest.param({"role": None}, "users.invalid_role", "admin or member", id="role-null"),
    pytest.param({"permissions": {"nope.manage": True}}, "users.invalid_permissions",
                 "Unknown permission key(s): nope.manage", id="permissions-unknown-key"),
    pytest.param({"workspace_ids": ["nope"]}, "users.invalid_workspaces",
                 "Unknown workspace id(s): nope", id="workspace_ids-unknown"),
    pytest.param({"workspaces": ["nope", DEFAULT_WORKSPACE_ID]}, "users.invalid_workspaces",
                 "Unknown workspace id(s): nope", id="workspaces-one-unknown"),
    pytest.param({"password": "short"}, "auth.weak_password", "", id="password-weak"),
])
def test_bodies_the_service_refuses_are_400_with_their_code_and_change_nothing(
        admin_client, member_id, body, code, needle):
    before = _snapshot()
    res = admin_client.patch(f"/api/users/{member_id}", json=body)
    assert res.status_code == 400, res.text
    assert res.json()["code"] == code
    assert needle in res.json()["message"]
    assert _snapshot() == before


def test_a_refused_workspace_grant_keeps_the_existing_grants(admin_client, member_id):
    ok = admin_client.patch(f"/api/users/{member_id}",
                            json={"status": "active", "workspace_ids": [DEFAULT_WORKSPACE_ID]})
    assert ok.status_code == 200 and ok.json()["workspaces"] == [DEFAULT_WORKSPACE_ID]
    res = admin_client.patch(f"/api/users/{member_id}", json={"workspace_ids": ["nope"]})
    assert res.status_code == 400 and res.json()["code"] == "users.invalid_workspaces"
    listed = admin_client.get("/api/users?q=qa-user").json()["items"]
    [row] = [u for u in listed if u["id"] == member_id]
    assert row["workspaces"] == [DEFAULT_WORKSPACE_ID] and row["status"] == "active"


# ─── the accepted edges the matrix brackets ───────────────────────────────────────────
def test_the_valid_edges_of_the_same_fields_are_accepted(admin_client, member_id):
    assert admin_client.patch(f"/api/users/{member_id}",
                              json={"extend_days": 1}).status_code == 200
    assert admin_client.patch(f"/api/users/{member_id}",
                              json={"extend_days": 3650}).status_code == 200
    # pydantic's lax mode coerces a numeric string: accepted, not a 422
    assert admin_client.patch(f"/api/users/{member_id}",
                              json={"extend_days": "30"}).status_code == 200
    res = admin_client.patch(f"/api/users/{member_id}",
                             json={"permissions": {"agents.deploy": False}})
    assert res.status_code == 200
    perms = res.json()["permissions"]  # the full map: explicit denial, everything else granted
    assert perms["agents.deploy"] is False
    assert all(flag is True for key, flag in perms.items() if key != "agents.deploy")
    res = admin_client.patch(f"/api/users/{member_id}", json={"permissions": None})
    assert res.status_code == 200 and all(res.json()["permissions"].values())  # reset
    assert admin_client.patch(f"/api/users/{member_id}",
                              json={"expires_at": "2030-01-01T00:00:00Z"}).status_code == 200
    assert admin_client.patch(f"/api/users/{member_id}",
                              json={"expires_at": None}).json()["expires_at"] is None
    assert admin_client.patch(f"/api/users/{member_id}",
                              json={"role": "admin"}).json()["role"] == "admin"
    assert admin_client.patch(f"/api/users/{member_id}",
                              json={"workspaces": None}).json()["workspaces"] == []


# ─── 404 ──────────────────────────────────────────────────────────────────────────────
def test_patch_and_delete_of_an_unknown_id_are_404(admin_client):
    res = admin_client.patch("/api/users/does-not-exist", json={"role": "member"})
    assert res.status_code == 404 and res.json()["code"] == "users.not_found"
    res = admin_client.delete("/api/users/does-not-exist")
    assert res.status_code == 404 and res.json()["code"] == "users.not_found"
    # the SERVICE checks run after the lookup: a well-typed but invalid role on an unknown id
    # is the 404, while a body the schema refuses is 422 before any lookup
    res = admin_client.patch("/api/users/does-not-exist", json={"role": "owner"})
    assert res.status_code == 404
    res = admin_client.patch("/api/users/does-not-exist", json={"role": 5})
    assert res.status_code == 422
