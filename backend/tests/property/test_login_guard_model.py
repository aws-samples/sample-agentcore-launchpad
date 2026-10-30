"""E18 — login protection as a model (round 16, channel A; users-and-ownership.md §9).

A Hypothesis ``RuleBasedStateMachine`` drives the REAL ``POST /api/auth/login`` route with
an INJECTED clock (``services.login_guard.time`` is replaced — no sleeps; every lock / window
timestamp comes from the model's clock) over the actions {wrong password, right password,
config-admin wrong / right, the same from another peer, time advance, admin password change,
admin unlock, backend restart (fresh guard)}; a second machine adds clock steps backwards
and large jumps forwards. A reference model written from the CONTRACT TEXT predicts every
answer. Every threshold below cites its §9 line; none is copied from ``login_guard.py``.

Invariants (the two that matter most first):

* §9 "Per-source-IP windows … a correct password is never refused by it" — the peer window
  is consulted only after a failed credential check;
* §9 "Built-in admin exemption … never entered into the per-name lockout" — the config
  admin is never per-name locked (its wrong passwords still count against the peer window);
* §9 "After `auth_login_lockout_threshold` (5) consecutive failures the name is locked for
  `auth_login_lockout_base_seconds` (60) … every further failure after the lock lapsed
  doubles it (60 → 120 → 240 → 480 → 960 → 1800) up to `auth_login_lockout_max_seconds`
  (1800)" — monotone within one streak, capped; "Attempts during the lock answer 429
  `auth.locked` with `Retry-After` BEFORE any credential work and do not count";
* §9 "Consecutive failures keep accumulating while they land within `2 × max` (1 h) of each
  other";
* §9 "Envelope. Every 429: `{code, message, detail: {retry_after}}` plus the `Retry-After`
  response header";
* §9 "Session epoch … A password change MUST sign that account's other sessions out … the
  v3 cookie payload … `resolve_identity` refuses a cookie whose epoch is not the row's";
* §9 "A successful sign-in forgets the name; an administrator forgets it with `PATCH
  /api/users/{id} {"unlock": true}`"; "a backend restart clears them".

Where §9 is silent the model states the behaviour as a DIFFERENTIAL against the observed
product and the register row N16-obs25 names the missing contract line: a password change
does not clear the lock counter; a clock step backwards lengthens a running lock by the
step (the lock is a wall-clock instant); the peer window keeps a failure until `window`
seconds after its recorded timestamp — and FORGETS it at the first read after it aged out
(the product's `SlidingWindow._trim` is a destructive head trim of a deque: a later backward
clock step does not revive it, and because the deque is appended in arrival order a step
back leaves it un-sorted, so the trim stops at the first still-young head and an older entry
behind it survives). N17-obs160: the model used to keep every failure forever and filter
by timestamp, so after `advance → step back` it predicted a 429 the product no longer gives.

The PBKDF2 iteration count is lowered for the test accounts only, so a step costs
milliseconds; nothing about the guard depends on it. Fake credentials only; no cookie value
is ever printed (assertion messages carry status codes and bodies, never headers).
"""

from __future__ import annotations

import math
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck
from hypothesis import settings as h_settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
    run_state_machine_as_test,
)

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import create_app
from app.routers.auth import COOKIE_NAME
from app.services import login_guard
from app.services import users as users_service

pytestmark = pytest.mark.property

ADMIN = {"username": "operator", "password": "s3cret-pass"}
WRONG = "definitely-not-the-password"
PEER_A, PEER_B, PEER_ADMIN = ("10.1.1.1", 4000), ("10.2.2.2", 4000), ("10.9.9.9", 4000)
HOSTS = {"a": PEER_A[0], "b": PEER_B[0]}


@dataclass(frozen=True)
class Limits:
    """The §9 knobs. `CONTRACT` are the documented defaults; `REACHABLE` lowers the two
    counters through the documented settings so a 30-step example reaches every state."""

    threshold: int  # §9: auth_login_lockout_threshold (5)
    base: int  # §9: auth_login_lockout_base_seconds (60)
    max: int  # §9: auth_login_lockout_max_seconds (1800)
    peer_limit: int  # §9: auth_login_ip_limit (20)
    peer_window: int  # §9: auth_login_ip_window_seconds (300)

    @property
    def streak_window(self) -> int:
        return 2 * self.max  # §9: "within 2 × max (1 h) of each other"


CONTRACT = Limits(threshold=5, base=60, max=1800, peer_limit=20, peer_window=300)
REACHABLE = Limits(threshold=3, base=60, max=1800, peer_limit=4, peer_window=300)


class Clock:
    def __init__(self) -> None:
        self.now = float(int(time.time()))

    def time(self) -> float:  # the `time.time()` the guard sees
        return self.now


def _world(monkeypatch, limits: Limits):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN["password"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_LOGIN_LOCKOUT_THRESHOLD", str(limits.threshold))
    monkeypatch.setenv("LAUNCHPAD_AUTH_LOGIN_LOCKOUT_BASE_SECONDS", str(limits.base))
    monkeypatch.setenv("LAUNCHPAD_AUTH_LOGIN_LOCKOUT_MAX_SECONDS", str(limits.max))
    monkeypatch.setenv("LAUNCHPAD_AUTH_LOGIN_IP_LIMIT", str(limits.peer_limit))
    monkeypatch.setenv("LAUNCHPAD_AUTH_LOGIN_IP_WINDOW_SECONDS", str(limits.peer_window))
    monkeypatch.setattr(users_service, "_ITERATIONS", 1000)  # cheap KDF for the test rows
    clock = Clock()
    monkeypatch.setattr(login_guard, "time", clock)
    get_settings.cache_clear()
    return SimpleNamespace(app=create_app(), clock=clock, limits=limits)


@pytest.fixture
def world(monkeypatch):
    original_guard = login_guard.guard
    yield _world(monkeypatch, REACHABLE)
    login_guard.guard = original_guard
    get_settings.cache_clear()


@pytest.fixture
def contract_world(monkeypatch):
    """The documented defaults themselves (the Settings defaults are not relied on: every
    knob is set to the §9 number explicitly)."""
    original_guard = login_guard.guard
    yield _world(monkeypatch, CONTRACT)
    login_guard.guard = original_guard
    get_settings.cache_clear()


# ── the reference model (contract §9) ────────────────────────────────────────


class AccountModel:
    def __init__(self, limits: Limits) -> None:
        self.limits = limits
        self.failures = 0
        self.last_failure = 0.0
        self.locked_until = 0.0
        self.locks: list[int] = []  # every lock applied in the current streak

    def locked_for(self, now: float) -> int:
        # §9: "Attempts during the lock answer 429 auth.locked with Retry-After" — the lock
        # is a wall-clock instant; the seconds left are rounded up, never below 1
        return 0 if self.locked_until <= now else max(1, math.ceil(self.locked_until - now))

    def fail(self, now: float) -> None:
        # §9: "Consecutive failures keep accumulating while they land within 2 × max of
        # each other" — a longer silence (after the lock lapsed) starts a fresh streak
        if self.locked_until <= now and now - self.last_failure > self.limits.streak_window:
            self.failures, self.locks = 0, []
        self.failures += 1
        self.last_failure = now
        # §9: "After threshold consecutive failures the name is locked for base seconds;
        # every further failure after the lock lapsed doubles it … up to max"
        over = self.failures - self.limits.threshold
        if over >= 0:
            lock = min(self.limits.base * 2**over, self.limits.max)
            self.locked_until = now + lock
            self.locks.append(lock)

    def forget(self) -> None:
        # §9: "A successful sign-in forgets the name; an administrator forgets it with unlock"
        self.failures, self.last_failure, self.locked_until, self.locks = 0, 0.0, 0.0, []


class PeerModel:
    def __init__(self, limits: Limits) -> None:
        self.limits = limits
        self.events: dict[str, deque[float]] = {}

    def _trim(self, peer: str, now: float) -> deque[float]:
        # Differential (§9 silent; N17-obs160): the product forgets an aged-out failure at
        # the first READ after it aged out — a destructive head trim of an arrival-ordered
        # deque — so a later backward clock step cannot revive it, and after a step back the
        # trim stops at the first still-young head. Mirrored exactly: the model is read at
        # the same moments the product is (every failed credential check: `retry_after`
        # then `fail`; a locked name never reaches the window).
        events = self.events.setdefault(peer, deque())
        cutoff = now - self.limits.peer_window
        while events and events[0] <= cutoff:
            events.popleft()
        return events

    def retry_after(self, peer: str, now: float) -> int:
        # §9: "auth_login_ip_limit (20) per auth_login_ip_window_seconds (300)" — a failure
        # counts until `window` seconds after its recorded timestamp (differential: §9 does
        # not say so in words; the product's SlidingWindow does exactly this)
        live = self._trim(peer, now)
        if len(live) < self.limits.peer_limit:
            return 0
        return max(1, math.ceil(live[-self.limits.peer_limit] + self.limits.peer_window - now))

    def fail(self, peer: str, now: float) -> None:
        self._trim(peer, now).append(now)


class LoginMachine(RuleBasedStateMachine):
    def __init__(self, world) -> None:
        super().__init__()
        self.app, self.clock, self.limits = world.app, world.clock, world.limits
        login_guard.guard = login_guard.LoginGuard()  # a fresh process for every example
        login_guard.reset()
        self.clock.now = max(self.clock.now, float(int(time.time())))
        self.username = f"qa-{uuid.uuid4().hex[:10]}"
        self.key = self.username.lower()
        self.password = "Correct-Horse-Battery-0000"
        self.generation = 0
        db = SessionLocal()
        try:
            user = users_service.register_user(
                db, self.username, f"{self.username}@acme-corp.com", self.password,
                created_by="operator",
            )
            user.status = users_service.STATUS_ACTIVE
            user.expires_at = datetime.now(UTC) + timedelta(days=7)
            db.commit()
            self.user_id = user.id
        finally:
            db.close()
        self.clients = {
            "a": TestClient(self.app, client=PEER_A),
            "b": TestClient(self.app, client=PEER_B),
        }
        self.admin = TestClient(self.app, client=PEER_ADMIN)
        assert self.admin.post("/api/auth/login", json=ADMIN).status_code == 200
        self.probe = TestClient(self.app, client=("10.7.7.7", 1))
        self.account = AccountModel(self.limits)
        self.peers = PeerModel(self.limits)
        self.live_cookies: list[str] = []
        self.stale_cookies: list[str] = []

    # ── helpers ──────────────────────────────────────────────────────────────

    @property
    def now(self) -> float:
        return self.clock.now

    def _login(self, peer: str, username: str, password: str):
        return self.clients[peer].post(
            "/api/auth/login", json={"username": username, "password": password})

    def _assert_429(self, res, code: str, retry_after: int) -> None:
        # §9 envelope: code + detail.retry_after in the body AND the Retry-After header
        assert res.status_code == 429, (res.status_code, res.text)
        body = res.json()
        assert body["code"] == code, body
        assert body["detail"] == {"retry_after": retry_after}, (body, retry_after)
        assert res.headers.get("Retry-After") == str(retry_after), (
            res.headers.get("Retry-After"), retry_after)

    def _member_attempt(self, peer: str, correct: bool) -> None:
        host = HOSTS[peer]
        lock = self.account.locked_for(self.now)
        res = self._login(peer, self.username, self.password if correct else WRONG)
        if lock:
            # §9: refused BEFORE any credential work; "do not count" — for the name and,
            # since no credential check failed, for the peer window either
            self._assert_429(res, "auth.locked", lock)
            return
        if correct:
            # §9: "a correct password is never refused by [the peer window]"
            assert res.status_code == 200, (res.status_code, res.text)
            self.account.forget()
            cookie = res.cookies.get(COOKIE_NAME)
            assert cookie, "a sign-in must set the session cookie"
            self.live_cookies = (self.live_cookies + [cookie])[-3:]
            return
        # §9: "LoginGuard.failed() returns the peer's retry_after as it stood BEFORE the
        # failure" — the window is read after the failed check, then the failure counts
        waiting = self.peers.retry_after(host, self.now)
        self.account.fail(self.now)
        self.peers.fail(host, self.now)
        if waiting:
            self._assert_429(res, "auth.rate_limited", waiting)
        else:
            assert res.status_code == 401 and res.json()["code"] == "auth.invalid_credentials"

    def _admin_attempt(self, peer: str, correct: bool) -> None:
        host = HOSTS[peer]
        res = self._login(peer, ADMIN["username"], ADMIN["password"] if correct else WRONG)
        if correct:
            # §9: never per-name locked; never refused by the window on a correct password
            assert res.status_code == 200, (res.status_code, res.text)
            return
        # §9: "Its wrong passwords still count against the peer window"
        waiting = self.peers.retry_after(host, self.now)
        self.peers.fail(host, self.now)
        if waiting:
            self._assert_429(res, "auth.rate_limited", waiting)
        else:
            assert res.status_code == 401, (res.status_code, res.text)
        assert res.json()["code"] != "auth.locked"

    # ── rules ────────────────────────────────────────────────────────────────

    @initialize()
    def start(self) -> None:
        pass

    @rule(peer=st.sampled_from(("a", "b")))
    def wrong_password(self, peer: str) -> None:
        self._member_attempt(peer, correct=False)

    @rule(peer=st.sampled_from(("a", "b")))
    def right_password(self, peer: str) -> None:
        self._member_attempt(peer, correct=True)

    @rule(peer=st.sampled_from(("a", "b")))
    def config_admin_wrong(self, peer: str) -> None:
        self._admin_attempt(peer, correct=False)

    @rule(peer=st.sampled_from(("a", "b")))
    def config_admin_right(self, peer: str) -> None:
        self._admin_attempt(peer, correct=True)

    @rule(seconds=st.sampled_from((1, 30, 59, 60, 61, 119, 121, 239, 241, 299, 301, 479, 481,
                                   959, 961, 1799, 1801, 3599, 3601)))
    def advance(self, seconds: int) -> None:
        self.clock.now += seconds

    @rule()
    def change_password(self) -> None:
        self.generation += 1
        new = f"Correct-Horse-Battery-{self.generation:04d}"
        res = self.admin.patch(f"/api/users/{self.user_id}", json={"password": new})
        assert res.status_code == 200, (res.status_code, res.text)
        self.password = new
        # §9 session epoch: the account's OTHER sessions end — every cookie issued so far
        # is stale. Differential (§9 silent): the lock counter is NOT cleared by a change.
        self.stale_cookies = (self.stale_cookies + self.live_cookies)[-4:]
        self.live_cookies = []

    @rule()
    def admin_unlock(self) -> None:
        res = self.admin.patch(f"/api/users/{self.user_id}", json={"unlock": True})
        assert res.status_code == 200, (res.status_code, res.text)
        assert res.json()["failed_logins"] == 0 and res.json()["locked_until"] is None
        self.account.forget()

    @rule()
    def backend_restart(self) -> None:
        # §9: "a backend restart clears them" — a fresh guard instance, every counter gone
        login_guard.guard = login_guard.LoginGuard()
        self.account.forget()
        self.peers = PeerModel(self.limits)

    # ── invariants ───────────────────────────────────────────────────────────

    @invariant()
    def product_lock_state_matches_the_model(self) -> None:
        settings = get_settings()
        guard = login_guard.guard
        assert guard.locked_for(self.key, settings, self.now) == self.account.locked_for(self.now)
        failures, locked_until = guard.state(self.key, self.now)
        if locked_until is not None:
            assert math.isclose(locked_until, self.account.locked_until), (
                locked_until, self.account.locked_until)
        # §9: "serialize() adds failed_logins / locked_until for the Users consoles"
        page = self.admin.get("/api/users", params={"q": self.username}).json()
        shown = next(row for row in page["items"] if row["id"] == self.user_id)
        assert shown["failed_logins"] == failures
        assert (shown["locked_until"] is None) == (locked_until is None)

    @invariant()
    def lock_durations_are_monotone_and_capped(self) -> None:
        # §9: 60 → 120 → 240 → 480 → 960 → 1800, capped
        locks = self.account.locks
        assert all(a <= b for a, b in zip(locks, locks[1:], strict=False)), locks
        assert all(self.limits.base <= lock <= self.limits.max for lock in locks), locks
        if locks:
            assert locks[0] == self.limits.base

    @invariant()
    def config_admin_is_never_per_name_locked(self) -> None:
        # §9: "settings.auth_username is never entered into the per-name lockout"
        assert login_guard.guard.state(ADMIN["username"].lower(), self.now) == (0, None)

    @invariant()
    def cookies_follow_the_session_epoch(self) -> None:
        for cookie in self.stale_cookies:
            res = self.probe.get("/api/auth/status", headers={"Cookie": f"{COOKIE_NAME}={cookie}"})
            assert res.status_code == 200 and res.json()["authenticated"] is False, res.text
        for cookie in self.live_cookies:
            res = self.probe.get("/api/auth/status", headers={"Cookie": f"{COOKIE_NAME}={cookie}"})
            assert res.json()["authenticated"] is True and res.json()["username"] == self.username

    def teardown(self) -> None:
        for client in (*self.clients.values(), self.admin, self.probe):
            client.close()


class LoginMachineWithClockSteps(LoginMachine):
    """The same model under a clock that also steps BACKWARDS (an NTP correction) and jumps
    far forwards. §9 is silent on clock steps (N16-obs25); the model treats every lock as a
    wall-clock instant and every window entry as its recorded timestamp, so the differential
    pins what the product does today."""

    @rule(seconds=st.sampled_from((1, 5, 30, 59, 61, 299, 301)))
    def clock_step_back(self, seconds: int) -> None:
        self.clock.now -= seconds

    @rule(seconds=st.sampled_from((3600, 7200, 86400)))
    def clock_jump_forward(self, seconds: int) -> None:
        self.clock.now += seconds


MACHINE_SETTINGS = h_settings(
    max_examples=25, stateful_step_count=30, deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])


def test_login_guard_model(world):
    run_state_machine_as_test(lambda: LoginMachine(world), settings=MACHINE_SETTINGS)


def test_login_guard_model_under_clock_steps(world):
    run_state_machine_as_test(lambda: LoginMachineWithClockSteps(world),
                              settings=MACHINE_SETTINGS)


# ── the contract numbers themselves (5 → 60 … 1800; 20 / 300) through the route ──────


def _member(app) -> tuple[str, str]:
    name = f"qa-{uuid.uuid4().hex[:10]}"
    db = SessionLocal()
    try:
        user = users_service.register_user(
            db, name, f"{name}@acme-corp.com", "Correct-Horse-Battery-0000", created_by="op")
        user.status = users_service.STATUS_ACTIVE
        user.expires_at = datetime.now(UTC) + timedelta(days=7)
        db.commit()
        return name, user.id
    finally:
        db.close()


def test_the_full_doubling_ladder_through_the_route(contract_world):
    """§9: 5 failures → 60 s; then each failure after the lock lapsed doubles: 120, 240,
    480, 960, 1800, 1800 — as 429 `auth.locked` with the exact Retry-After, through the
    real route; an hour of silence after the last lock starts a fresh streak."""
    world = contract_world
    name, _ = _member(world.app)
    client = TestClient(world.app, client=PEER_A)
    login_guard.guard = login_guard.LoginGuard()

    def wrong():
        return client.post("/api/auth/login", json={"username": name, "password": WRONG})

    for _ in range(CONTRACT.threshold):
        assert wrong().status_code == 401
    ladder = []
    for expected in (60, 120, 240, 480, 960, 1800, 1800):
        probe = client.post("/api/auth/login",
                            json={"username": name, "password": "Correct-Horse-Battery-0000"})
        assert probe.status_code == 429 and probe.json()["code"] == "auth.locked"
        ladder.append(probe.json()["detail"]["retry_after"])
        assert probe.headers["Retry-After"] == str(expected)
        # past the lock AND past the peer window (so the secondary control stays out of it)
        world.clock.now += max(expected, CONTRACT.peer_window) + 1
        assert wrong().status_code == 401  # the lock lapsed; this failure doubles it
    assert ladder == [60, 120, 240, 480, 960, 1800, 1800]
    world.clock.now += CONTRACT.streak_window + 1
    for _ in range(CONTRACT.threshold):
        assert wrong().status_code == 401
    assert login_guard.guard.locked_for(
        name.lower(), get_settings(), world.clock.now) == CONTRACT.base


def test_a_tripped_peer_window_never_refuses_a_correct_password(contract_world):
    """§9: 20 failed sign-ins per 300 s from one peer → further FAILURES answer 429
    `auth.rate_limited`; a correct password (member or admin) is never refused by it."""
    world = contract_world
    name, _ = _member(world.app)
    client = TestClient(world.app, client=PEER_A)
    login_guard.guard = login_guard.LoginGuard()
    # trip the peer window with the admin's name (never per-name locked)
    for _ in range(CONTRACT.peer_limit):
        assert client.post("/api/auth/login", json={
            "username": ADMIN["username"], "password": WRONG}).status_code == 401
    tripped = client.post("/api/auth/login",
                          json={"username": ADMIN["username"], "password": WRONG})
    assert tripped.status_code == 429 and tripped.json()["code"] == "auth.rate_limited"
    assert int(tripped.headers["Retry-After"]) == CONTRACT.peer_window
    assert login_guard.guard.state(ADMIN["username"].lower()) == (0, None)
    ok = client.post("/api/auth/login", json={"username": name,
                                              "password": "Correct-Horse-Battery-0000"})
    assert ok.status_code == 200, (ok.status_code, ok.text)
    assert client.post("/api/auth/login", json=ADMIN).status_code == 200
