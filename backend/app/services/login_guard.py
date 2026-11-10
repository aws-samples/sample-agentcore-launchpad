"""In-process login protection for the console sign-in and the `/v1` key check.

The console may sit behind the open NLB (user decision, N12-obs52), which makes
`POST /api/auth/login` the only gate. Two controls, both in-memory (one uvicorn
worker, SQLite ledger, no Redis — a restart clears them, which is documented):

* **Per-account lockout (PRIMARY).** Keyed by the *submitted* normalized username
  whether or not an account exists, so a lock never discloses account existence.
  After `threshold` consecutive failures the name is locked for `base` seconds,
  doubling with every further failure up to `max`; attempts during a lock are
  refused with 429 `auth.locked` and do not count. A successful sign-in or an
  admin unlock (`PATCH /api/users/{id} {"unlock": true}`) clears it. The built-in
  admin is covered too (its lock is capped the same way; a restart also clears it).
* **Per-source-IP sliding window (SECONDARY, coarse).** Failed sign-ins per transport
  peer in a window; once tripped, further FAILURES from that peer answer 429
  `auth.rate_limited` — a correct password or a valid key is never refused by it, and
  it is consulted only after a failed check. The transport peer is what uvicorn
  reports: with `--proxy-headers --forwarded-allow-ips=127.0.0.1` (the systemd unit)
  nginx's `X-Forwarded-For` is honoured, so on the EIP path it is the real client;
  behind the NLB (client-IP preservation OFF) it is the NLB's private IP shared by every
  user — hence coarse and secondary. The built-in admin is exempt from the per-name
  lock (break-glass) and covered by this window only.

The same window class rate-limits invalid `X-Api-Key` attempts on `/v1`.

Each lock and each window trip is written to the journal (logger `launchpad.auth`,
WARNING) with the account key, counters and peer — never any password material.
Memory is bounded: stale entries are pruned on every write and the maps are capped.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass

from fastapi import Request

from app.core.config import Settings, get_settings
from app.core.errors import AppError

logger = logging.getLogger("launchpad.auth")

CODE_LOCKED = "auth.locked"
CODE_RATE_LIMITED = "auth.rate_limited"

# Hard caps on tracked keys so a flood of random usernames / spoofed peers cannot
# grow the maps without bound; the oldest entries are evicted first.
_MAX_TRACKED = 10_000
# Consecutive failures keep accumulating while they land within this many lockout
# maxima of each other; after that silence the counter is forgotten. Twice the cap
# so a lock that just expired does not hand out a fresh batch of free attempts.
_STALE_FACTOR = 2


def peer_address(request: Request) -> str:
    """The transport peer, or a stable placeholder for peer-less transports."""
    client = request.client
    if client is None or not client.host:
        return "-"
    return str(client.host)


def _retry_after_header(seconds: int) -> dict[str, str]:
    return {"Retry-After": str(max(1, int(seconds)))}


def locked_error(retry_after: int) -> AppError:
    seconds = max(1, int(retry_after))
    return AppError(
        CODE_LOCKED,
        f"Too many failed sign-in attempts for this account. Try again in {seconds} seconds.",
        {"retry_after": seconds},
        status_code=429,
        headers=_retry_after_header(seconds),
    )


def rate_limited_error(retry_after: int, *, what: str = "sign-in") -> AppError:
    seconds = max(1, int(retry_after))
    return AppError(
        CODE_RATE_LIMITED,
        f"Too many failed {what} attempts from this address. Try again in {seconds} seconds.",
        {"retry_after": seconds},
        status_code=429,
        headers=_retry_after_header(seconds),
    )


class SlidingWindow:
    """Failed-attempt timestamps per key; `retry_after` is 0 while under the limit."""

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _trim(self, key: str, window: float, now: float) -> deque[float]:
        events = self._events.setdefault(key, deque())
        cutoff = now - window
        while events and events[0] <= cutoff:
            events.popleft()
        return events

    def _prune(self, window: float, now: float) -> None:
        cutoff = now - window
        for key in [k for k, ev in self._events.items() if not ev or ev[-1] <= cutoff]:
            del self._events[key]
        while len(self._events) > _MAX_TRACKED:
            oldest = min(self._events, key=lambda k: self._events[k][-1])
            del self._events[oldest]

    def retry_after(self, key: str, limit: int, window: float, now: float | None = None) -> int:
        """Seconds until the oldest counted attempt leaves the window (0 = allowed)."""
        if limit <= 0:
            return 0
        moment = time.time() if now is None else now
        with self._lock:
            events = self._trim(key, window, moment)
            if len(events) < limit:
                if not events:
                    self._events.pop(key, None)
                return 0
            # the attempt that must age out before another one is admitted
            oldest = events[len(events) - limit]
            return max(1, math.ceil(oldest + window - moment))

    def record(self, key: str, limit: int, window: float, now: float | None = None) -> int:
        """Count one failure; returns the count now inside the window."""
        if limit <= 0:
            return 0
        moment = time.time() if now is None else now
        with self._lock:
            events = self._trim(key, window, moment)
            events.append(moment)
            count = len(events)
            if len(self._events) > _MAX_TRACKED:
                self._prune(window, moment)
            return count

    def count(self, key: str, window: float, now: float | None = None) -> int:
        moment = time.time() if now is None else now
        with self._lock:
            return len(self._trim(key, window, moment))

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


@dataclass
class AccountState:
    failures: int = 0
    locked_until: float = 0.0
    last_failure: float = 0.0


class LoginGuard:
    """Per-account lockout + per-peer window for the console sign-in."""

    def __init__(self) -> None:
        self._accounts: dict[str, AccountState] = {}
        self._lock = threading.Lock()
        self.peers = SlidingWindow()

    # --- per-account -------------------------------------------------------

    @staticmethod
    def _stale_after(settings: Settings) -> float:
        return float(settings.auth_login_lockout_max_seconds) * _STALE_FACTOR

    def _prune_accounts(self, settings: Settings, now: float) -> None:
        stale = self._stale_after(settings)
        for key in [
            k
            for k, st in self._accounts.items()
            if st.locked_until <= now and now - st.last_failure > stale
        ]:
            del self._accounts[key]
        while len(self._accounts) > _MAX_TRACKED:
            oldest = min(self._accounts, key=lambda k: self._accounts[k].last_failure)
            del self._accounts[oldest]

    def locked_for(
        self, account_key: str, settings: Settings | None = None, now: float | None = None
    ) -> int:
        """Seconds the submitted name stays locked (0 = not locked)."""
        current = settings or get_settings()
        if not current.auth_login_lockout_enabled or not account_key:
            return 0
        moment = time.time() if now is None else now
        with self._lock:
            state = self._accounts.get(account_key)
            if state is None or state.locked_until <= moment:
                return 0
            return max(1, math.ceil(state.locked_until - moment))

    def record_failure(
        self,
        account_key: str,
        peer: str,
        settings: Settings | None = None,
        now: float | None = None,
    ) -> int:
        """Count one wrong password; returns the lock just applied in seconds (0 = none)."""
        current = settings or get_settings()
        if not current.auth_login_lockout_enabled or not account_key:
            return 0
        moment = time.time() if now is None else now
        with self._lock:
            state = self._accounts.get(account_key)
            if state is None or (
                state.locked_until <= moment
                and moment - state.last_failure > self._stale_after(current)
            ):
                state = AccountState()
                self._accounts[account_key] = state
            state.failures += 1
            state.last_failure = moment
            over = state.failures - current.auth_login_lockout_threshold
            lock_seconds = 0
            if over >= 0:
                lock_seconds = min(
                    current.auth_login_lockout_base_seconds * (2 ** min(over, 30)),
                    current.auth_login_lockout_max_seconds,
                )
                state.locked_until = moment + lock_seconds
            failures = state.failures
            if len(self._accounts) > _MAX_TRACKED:
                self._prune_accounts(current, moment)
        if lock_seconds:
            # journal (journald under systemd): who/where, never the password
            logger.warning(
                "login lockout: account=%s consecutive_failures=%d locked_for=%ds peer=%s",
                account_key,
                failures,
                lock_seconds,
                peer,
            )
        return lock_seconds

    def record_success(self, account_key: str) -> None:
        with self._lock:
            self._accounts.pop(account_key, None)

    def unlock(self, account_key: str) -> bool:
        """Admin unlock: forget the name's counter and lock. True when there was one."""
        with self._lock:
            return self._accounts.pop(account_key, None) is not None

    def state(self, account_key: str, now: float | None = None) -> tuple[int, float | None]:
        """`(consecutive_failures, locked_until_epoch | None)` for the Users console."""
        moment = time.time() if now is None else now
        with self._lock:
            state = self._accounts.get(account_key)
            if state is None:
                return 0, None
            locked = state.locked_until if state.locked_until > moment else None
            return state.failures, locked

    # --- per-peer ----------------------------------------------------------

    def peer_retry_after(
        self, peer: str, settings: Settings | None = None, now: float | None = None
    ) -> int:
        current = settings or get_settings()
        return self.peers.retry_after(
            peer, current.auth_login_ip_limit, current.auth_login_ip_window_seconds, now
        )

    def record_peer_failure(
        self, peer: str, settings: Settings | None = None, now: float | None = None
    ) -> None:
        current = settings or get_settings()
        count = self.peers.record(
            peer, current.auth_login_ip_limit, current.auth_login_ip_window_seconds, now
        )
        if count == current.auth_login_ip_limit:
            logger.warning(
                "login rate limit: peer=%s failed_attempts=%d window=%ds",
                peer,
                count,
                current.auth_login_ip_window_seconds,
            )

    # --- entry points used by the login route ------------------------------

    def check(self, account_key: str, peer: str, settings: Settings | None = None) -> None:
        """Raise 429 `auth.locked` before any credential work while the name is locked.

        The peer window is deliberately NOT consulted here (rev-r11): behind nginx or
        the NLB every caller shares one peer address, so a pre-check would let twenty
        wrong passwords from anyone lock the whole console out. It is applied to
        failures only — see `failed()`.
        """
        current = settings or get_settings()
        locked = self.locked_for(account_key, current)
        if locked:
            raise locked_error(locked)

    def failed(
        self,
        account_key: str,
        peer: str,
        settings: Settings | None = None,
        *,
        count_account: bool = True,
    ) -> int:
        """Record one wrong password; returns the peer window's `retry_after` as it stood
        BEFORE this failure (0 = the caller answers its ordinary 401, >0 = answer 429).
        `count_account=False` keeps the name out of the lockout (the built-in admin)."""
        current = settings or get_settings()
        waiting = self.peer_retry_after(peer, current)
        if count_account:
            self.record_failure(account_key, peer, current)
        self.record_peer_failure(peer, current)
        return waiting

    def reset(self) -> None:
        with self._lock:
            self._accounts.clear()
        self.peers.reset()


# The one guard behind `/api/auth/login` ...
guard = LoginGuard()
# ... the window behind `/v1` key checks (invalid / missing keys only) ...
api_key_peers = SlidingWindow()
# ... and the window behind self-registration (every validated attempt, A-2).
register_peers = SlidingWindow()


def register_retry_after(peer: str, settings: Settings | None = None) -> int:
    current = settings or get_settings()
    return register_peers.retry_after(
        peer, current.auth_register_ip_limit, current.auth_register_ip_window_seconds
    )


def record_register_attempt(peer: str, settings: Settings | None = None) -> None:
    current = settings or get_settings()
    count = register_peers.record(
        peer, current.auth_register_ip_limit, current.auth_register_ip_window_seconds
    )
    if count == current.auth_register_ip_limit:
        logger.warning(
            "registration rate limit: peer=%s attempts=%d window=%ds",
            peer,
            count,
            current.auth_register_ip_window_seconds,
        )


def api_key_retry_after(peer: str, settings: Settings | None = None) -> int:
    current = settings or get_settings()
    return api_key_peers.retry_after(
        peer, current.auth_api_key_ip_limit, current.auth_api_key_ip_window_seconds
    )


def record_api_key_failure(peer: str, settings: Settings | None = None) -> None:
    current = settings or get_settings()
    count = api_key_peers.record(
        peer, current.auth_api_key_ip_limit, current.auth_api_key_ip_window_seconds
    )
    if count == current.auth_api_key_ip_limit:
        logger.warning(
            "/v1 api-key rate limit: peer=%s invalid_attempts=%d window=%ds",
            peer,
            count,
            current.auth_api_key_ip_window_seconds,
        )


def reset() -> None:
    """Forget every counter (tests; the hermetic suite calls this between tests)."""
    guard.reset()
    api_key_peers.reset()
    register_peers.reset()
