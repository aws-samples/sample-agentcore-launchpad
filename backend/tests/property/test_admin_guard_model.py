"""E18 — last-admin / self-delete guards as a model (round 16, channel A; §9).

Random sequences of role / status / expiry patches and deletes across 2–4 administrator
rows (plus a member), through the real ``PATCH`` / ``DELETE /api/users/{id}`` routes as
the config admin and through the service as an administrator row (the self-delete rule).
A reference model keeps every row's (role, status, expires_at); after every step:

* the ledger never holds zero active, non-expired administrator rows (the invariant §9
  states — the config admin does not count);
* a step is refused (409 ``users.last_admin`` / ``users.self_delete``) exactly when the
  model says it would break the invariant — no false refusals, no false acceptances;
* a refused step changes nothing.

The sequential model holds on the current code. The one way to reach zero admins was two
administrators acting at the same instant (N16-obs21, fixed: a serialised guard section) —
pinned below with a barrier inside `active_admin_ids`.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck
from hypothesis import settings as h_settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.main import create_app
from app.services import users as users_service

pytestmark = pytest.mark.property

CONFIG_ADMIN = {"username": "operator", "password": "s3cret-pass"}
PAST = datetime(2020, 1, 1, tzinfo=UTC)
FUTURE = datetime(2099, 1, 1, tzinfo=UTC)


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", CONFIG_ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", CONFIG_ADMIN["password"])
    monkeypatch.setattr(users_service, "_ITERATIONS", 1000)
    get_settings.cache_clear()
    app = create_app()
    client = TestClient(app, client=("10.9.9.9", 1))
    assert client.post("/api/auth/login", json=CONFIG_ADMIN).status_code == 200
    yield SimpleNamespace(app=app, admin=client)
    client.close()
    get_settings.cache_clear()


def _row(role: str) -> str:
    name = f"adm-{uuid.uuid4().hex[:10]}"
    db = SessionLocal()
    try:
        user = users_service.register_user(
            db, name, f"{name}@acme-corp.com", "Correct-Horse-Battery-0000",
            created_by="operator", role=role)
        user.status = users_service.STATUS_ACTIVE
        user.expires_at = None
        db.commit()
        return user.id
    finally:
        db.close()


def _wipe_accounts() -> None:
    from app.models.ledger import User, UserWorkspace

    db = SessionLocal()
    try:
        db.query(UserWorkspace).delete()
        db.query(User).delete()
        db.commit()
    finally:
        db.close()


def _ledger_active_admins() -> set[str]:
    db = SessionLocal()
    try:
        return users_service.active_admin_ids(db)
    finally:
        db.close()


class Row:
    def __init__(self, role: str) -> None:
        self.role, self.status, self.expires_at = role, "active", None
        self.deleted = False

    def active_admin(self) -> bool:
        # §9 "Last-admin / self-delete guards": active_admin_ids = role=admin, status=active,
        # not expired; the config-driven admin does not count
        return (not self.deleted and self.role == "admin" and self.status == "active"
                and (self.expires_at is None or self.expires_at > datetime.now(UTC)))

    def after(self, patch: dict) -> Row:
        other = Row(self.role)
        other.status, other.expires_at = self.status, self.expires_at
        if "role" in patch:
            other.role = patch["role"]
        if "status" in patch:
            other.status = patch["status"]
        if "expires_at" in patch:
            other.expires_at = patch["expires_at"]
        return other


PATCHES = st.sampled_from((
    {"role": "member"}, {"role": "admin"},
    {"status": "disabled"}, {"status": "pending"}, {"status": "active"},
    {"expires_at": PAST}, {"expires_at": FUTURE}, {"expires_at": None},
    {"role": "member", "status": "disabled"}, {"status": "active", "role": "admin"},
    {"extend_days": 30},
))


class AdminGuardMachine(RuleBasedStateMachine):
    def __init__(self, world, admins: int) -> None:
        super().__init__()
        self.admin = world.admin
        _wipe_accounts()  # every example starts from an empty users table
        self.rows: dict[str, Row] = {}
        for _ in range(admins):
            self.rows[_row("admin")] = Row("admin")
        self.rows[_row("member")] = Row("member")
        self.ids = list(self.rows)

    # ── model ────────────────────────────────────────────────────────────────

    def _active(self) -> set[str]:
        return {uid for uid, row in self.rows.items() if row.active_admin()}

    def _would_break(self, uid: str, after: Row | None) -> bool:
        # §9: refuse "a patch that would take an active admin row out of that set — role=member,
        # status ∈ {pending, disabled}, a past expires_at — when no OTHER active admin row
        # remains"; delete: "the last active administrator row is never deleted"
        before = self.rows[uid]
        if not before.active_admin():
            return False
        remaining = self._active() - {uid}
        if after is not None and after.active_admin():
            return False
        return not remaining

    # ── rules ────────────────────────────────────────────────────────────────

    def _pick(self, data) -> str:
        return data.draw(st.sampled_from(self.ids))

    @rule(data=st.data(), patch=PATCHES)
    def patch(self, data, patch: dict) -> None:
        uid = self._pick(data)
        row = self.rows[uid]
        if row.deleted:
            res = self.admin.patch(f"/api/users/{uid}", json=_json(patch))
            assert res.status_code == 404 and res.json()["code"] == "users.not_found"
            return
        after = row.after(patch) if {"role", "status", "expires_at"} & set(patch) else row
        refuse = self._would_break(uid, after)
        res = self.admin.patch(f"/api/users/{uid}", json=_json(patch))
        if refuse:
            assert res.status_code == 409, (patch, res.text)
            assert res.json()["code"] == "users.last_admin"
            assert res.json()["detail"]["user_id"] == uid
            return
        assert res.status_code == 200, (patch, res.text)
        if "extend_days" in patch:
            # extends from max(expiry, now): never shortens, never makes a row expired
            if row.expires_at is None or row.expires_at <= datetime.now(UTC):
                row.expires_at = datetime.now(UTC) + timedelta(days=30)
            else:
                row.expires_at = row.expires_at + timedelta(days=30)
        else:
            self.rows[uid] = after

    @rule(data=st.data())
    def delete_as_config_admin(self, data) -> None:
        uid = self._pick(data)
        row = self.rows[uid]
        if row.deleted:
            res = self.admin.delete(f"/api/users/{uid}")
            assert res.status_code == 404
            return
        refuse = self._would_break(uid, None)
        res = self.admin.delete(f"/api/users/{uid}")
        if refuse:
            assert res.status_code == 409 and res.json()["code"] == "users.last_admin", res.text
            return
        assert res.status_code == 200, res.text
        row.deleted = True

    @precondition(lambda self: any(not r.deleted for r in self.rows.values()))
    @rule(data=st.data())
    def delete_as_an_admin_row(self, data) -> None:
        """The service path an administrator ROW takes: the self-delete rule first."""
        live = [uid for uid, r in self.rows.items() if not r.deleted]
        actor = data.draw(st.sampled_from(live))
        target = self._pick(data)
        row = self.rows[target]
        db = SessionLocal()
        try:
            if row.deleted:
                with pytest.raises(AppError) as exc:
                    users_service.get_user(db, target)
                assert exc.value.code == "users.not_found"
                return
            user = users_service.get_user(db, target)
            expected = None
            if actor == target:
                expected = "users.self_delete"  # §9: "refuses the caller's own row" first
            elif self._would_break(target, None):
                expected = "users.last_admin"
            try:
                users_service.delete_user(db, user, actor_user_id=actor)
            except AppError as exc:
                assert exc.code == expected, (exc.code, expected)
                db.rollback()
                return
            assert expected is None, f"expected {expected}, the delete went through"
            row.deleted = True
        finally:
            db.close()

    # ── invariants ───────────────────────────────────────────────────────────

    @invariant()
    def never_zero_active_admin_rows(self) -> None:
        ledger = _ledger_active_admins()
        assert ledger, "the ledger holds no active administrator row"
        # the model and the ledger agree on who administers
        assert ledger == self._active()

    @invariant()
    def refused_rows_are_unchanged(self) -> None:
        page = self.admin.get("/api/users", params={"page_size": 100}).json()
        shown = {row["id"]: row for row in page["items"]}
        for uid, row in self.rows.items():
            if row.deleted:
                assert uid not in shown
                continue
            assert shown[uid]["role"] == row.role and shown[uid]["status"] == row.status


def _json(patch: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in patch.items()}


@pytest.mark.parametrize("admins", [2, 3, 4])
def test_admin_guard_model(world, admins):
    run_state_machine_as_test(
        lambda: AdminGuardMachine(world, admins),
        settings=h_settings(max_examples=12, stateful_step_count=25, deadline=None,
                            suppress_health_check=[HealthCheck.too_slow]))


# ── the concurrent corner the sequential model cannot reach ──────────────────


def test_two_simultaneous_demotions_cannot_remove_both_admins(world, monkeypatch):
    alice, bob = _row("admin"), _row("admin")
    barrier = threading.Barrier(2, timeout=2)
    passed = threading.Event()
    original = users_service.active_admin_ids

    def paused(db, now=None):
        ids = original(db, now)
        if not passed.is_set():
            try:
                barrier.wait()  # both requests have read "the other admin is still active"
            except threading.BrokenBarrierError:
                pass
            passed.set()
        return ids

    monkeypatch.setattr(users_service, "active_admin_ids", paused)
    results: dict[str, int] = {}

    def demote(name: str, uid: str) -> None:
        client = TestClient(world.app, client=("10.9.9.9", 1), cookies=world.admin.cookies)
        results[name] = client.patch(f"/api/users/{uid}", json={"role": "member"}).status_code

    threads = [threading.Thread(target=demote, args=("a", alice)),
               threading.Thread(target=demote, args=("b", bob))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    # N16-obs21 (fixed): the guard + commit run in one serialised section, so the second
    # demotion reads the first one's commit and is refused; before the fix both were 200
    assert _ledger_active_admins(), f"zero active administrator rows after two demotions {results}"
    assert sorted(results.values()) == [200, 409], results
