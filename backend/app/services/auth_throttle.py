"""Throttling of the two anonymous console auth endpoints: register and login.

Three guards, all process-local (the console runs one uvicorn process; a restart
forgets them, which is acceptable for a brake whose windows are minutes long):

* **per-IP rate limits** — a sliding window of attempts per client IP for
  ``POST /api/auth/register`` and for ``POST /api/auth/login``, counted before the
  request is processed, whatever its outcome;
* **login backoff** — after ``auth_login_lockout_threshold`` consecutive wrong
  passwords for one username *from one client IP*, that pair is refused for a
  window that doubles per further failure. Keyed on the pair on purpose: a
  per-username lockout would let anyone lock a real user (or the built-in admin)
  out from anywhere. A correct password clears the pair;
* the pending-registration cap lives with registration itself
  (``users.register_user``'s caller in ``routers/auth``).

The client IP (:func:`client_ip`) is the transport peer unless the request came
through a trusted proxy — the peer is in ``auth_trusted_proxies``, or uvicorn's own
proxy-headers middleware already rewrote the peer (its default: it trusts
127.0.0.1, so behind nginx the app sees the CloudFront edge with port 0; see
:func:`_rewritten_by_uvicorn`). Only then are forwarding headers read. First the
optional ``auth_client_ip_header`` (CloudFront-Viewer-Address: written by the edge
itself, ``address:port``), else the ``X-Forwarded-For`` entry
``auth_trusted_proxy_hops`` from the right — the one the outermost trusted proxy
(CloudFront) appended. Every entry left of it is whatever the caller sent, so
prepending fake addresses changes nothing.

None of this stops a distributed flood (many IPs, few attempts each); that is the
edge's job (AWS WAF rate-based rules / Bot Control in front of CloudFront).
"""

from __future__ import annotations

import ipaddress
import logging
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache

from fastapi import Request

from app.core.config import Settings, get_settings
from app.core.errors import AppError

logger = logging.getLogger(__name__)

# Bound on tracked keys per table, so an attacker rotating addresses cannot grow
# memory without limit; the least recently touched key is dropped first.
MAX_KEYS = 50_000


_Network = ipaddress.IPv4Network | ipaddress.IPv6Network


@lru_cache(maxsize=16)
def _networks(entries: tuple[str, ...]) -> tuple[_Network, ...]:
    return tuple(ipaddress.ip_network(entry, strict=False) for entry in entries)


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    text = value.strip()
    if text.startswith("[") and "]" in text:  # "[v6]:port"
        text = text[1 : text.index("]")]
    elif text.count(":") == 1:  # "v4:port"
        text = text.split(":", 1)[0]
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def _parse_ip_port(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """An ``address:port`` header value (CloudFront-Viewer-Address): ``1.2.3.4:5``,
    ``[2001:db8::1]:5`` or CloudFront's unbracketed ``2001:db8::1:5`` — the port is
    always the last ``:`` field, so it is cut before parsing (an unbracketed IPv6
    with its port still attached would parse as a different, valid address)."""
    text = value.strip()
    if text.startswith("[") and "]" in text:
        text = text[1 : text.index("]")]
    elif ":" in text:
        text = text.rsplit(":", 1)[0]
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def _forwarded_for(request: Request) -> list[str]:
    return [
        part.strip()
        for raw in request.headers.getlist("x-forwarded-for")
        for part in raw.split(",")
        if part.strip()
    ]


def _rewritten_by_uvicorn(host: str, port: int | None, entries: list[str]) -> bool:
    """Whether uvicorn's ProxyHeadersMiddleware already replaced the peer.

    uvicorn runs with ``proxy_headers`` on by default and trusts 127.0.0.1
    (``FORWARDED_ALLOW_IPS``): a request from nginx on loopback reaches the app with
    ``scope["client"]`` overwritten by the rightmost X-Forwarded-For entry uvicorn
    does not trust — behind CloudFront → nginx, the CloudFront edge — and port 0
    (no X-Forwarded-For writer here adds one). The original peer is gone by then,
    so the rewrite itself is the evidence: a TCP peer never has port 0, and uvicorn
    only rewrites when the real peer was one it trusts. The X-Forwarded-For header
    is left untouched, so the hop rule below still reads the right entry.
    """
    if port != 0 or not host:
        return False
    peer = _parse_ip(host)
    return peer is not None and any(_parse_ip(entry) == peer for entry in entries)


_warned_untrusted: set[str] = set()


def _warn_untrusted_once(peer: str) -> None:
    if peer in _warned_untrusted or len(_warned_untrusted) > 32:
        return
    _warned_untrusted.add(peer)
    logger.warning(
        "auth throttle: forwarding headers are configured but a request came from "
        "%s, which is neither in auth_trusted_proxies nor rewritten by uvicorn's "
        "proxy headers; keying on that address (if it is your proxy, add it to "
        "auth_trusted_proxies)",
        peer,
    )


def client_ip(request: Request, settings: Settings | None = None) -> str:
    """The address the throttles key on (see the module docstring)."""
    current = settings or get_settings()
    client = request.client
    peer = client.host if client and client.host else ""
    hops = current.auth_trusted_proxy_hops
    header = current.auth_client_ip_header.strip()
    if hops <= 0 and not header:
        return peer or "unknown"
    entries = _forwarded_for(request)
    peer_ip = _parse_ip(peer)
    trusted = _networks(tuple(current.auth_trusted_proxies))
    via_proxy = (peer_ip is not None and any(peer_ip in net for net in trusted)) or (
        _rewritten_by_uvicorn(peer, client.port if client else None, entries)
    )
    if not via_proxy:
        # reached the backend without going through the proxy: the headers are the
        # caller's own claim, so they are never read
        if peer:
            _warn_untrusted_once(peer)
        return peer or "unknown"
    if header:
        # an edge-generated "address:port" header (CloudFront-Viewer-Address) wins —
        # CloudFront writes it over anything the viewer sent (the runbook's probe
        # forges one to prove that). More than one value means something in the
        # chain passed a caller's copy along: trust none of them, use the hop rule.
        values = request.headers.getlist(header)
        viewer = _parse_ip_port(values[0]) if len(values) == 1 else None
        if viewer is not None:
            return str(viewer)
    if hops <= 0 or not entries:
        return peer or "unknown"
    # fewer entries than hops: the leftmost one was still written by a trusted proxy
    chosen = _parse_ip(entries[max(0, len(entries) - hops)])
    return str(chosen) if chosen is not None else peer


@dataclass
class _Failures:
    count: int = 0
    locked_until: float = 0.0


@dataclass
class AuthThrottle:
    clock: Callable[[], float] = time.monotonic
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _windows: dict[str, OrderedDict[str, deque[float]]] = field(
        default_factory=lambda: {"register": OrderedDict(), "login": OrderedDict()}
    )
    _failures: OrderedDict[tuple[str, str], _Failures] = field(default_factory=OrderedDict)

    # -- per-IP sliding windows -------------------------------------------------

    def hit(self, scope: str, ip: str, limit: int, window_s: int) -> float:
        """Record one attempt. Returns 0.0 when admitted, else seconds to wait."""
        if limit <= 0:
            return 0.0
        with self._lock:
            now = self.clock()
            table = self._windows[scope]
            stamps = table.get(ip)
            if stamps is None:
                stamps = table[ip] = deque()
                _bound(table)
            else:
                table.move_to_end(ip)
            while stamps and now - stamps[0] >= window_s:
                stamps.popleft()
            if len(stamps) >= limit:
                return max(0.0, window_s - (now - stamps[0]))
            stamps.append(now)
            return 0.0

    # -- per (username, IP) login backoff ---------------------------------------

    def locked_for(self, username: str, ip: str) -> float:
        with self._lock:
            state = self._failures.get((username, ip))
            if state is None:
                return 0.0
            return max(0.0, state.locked_until - self.clock())

    def failed(self, username: str, ip: str, settings: Settings) -> float:
        """Count one wrong password; returns the lockout it started (0 if none)."""
        threshold = settings.auth_login_lockout_threshold
        if threshold <= 0:
            return 0.0
        with self._lock:
            key = (username, ip)
            state = self._failures.get(key)
            if state is None:
                state = self._failures[key] = _Failures()
                _bound(self._failures)
            else:
                self._failures.move_to_end(key)
            state.count += 1
            if state.count < threshold:
                return 0.0
            exponent = min(state.count - threshold, 16)
            seconds = min(
                settings.auth_login_lockout_max_s,
                settings.auth_login_lockout_base_s * (2**exponent),
            )
            state.locked_until = self.clock() + seconds
            return float(seconds)

    def succeeded(self, username: str, ip: str) -> None:
        with self._lock:
            self._failures.pop((username, ip), None)

    def reset(self) -> None:
        with self._lock:
            for table in self._windows.values():
                table.clear()
            self._failures.clear()


def _bound(table: OrderedDict) -> None:
    while len(table) > MAX_KEYS:
        table.popitem(last=False)


throttle = AuthThrottle()


def reset_for_tests() -> None:
    throttle.reset()
    _warned_untrusted.clear()


def _retry_after(seconds: float) -> int:
    return max(1, int(seconds) + (0 if float(seconds).is_integer() else 1))


def _rate_limited(scope: str, ip: str, wait: float, limit: int, window_s: int) -> AppError:
    retry = _retry_after(wait)
    logger.warning(
        "auth throttle: %s rate limit (%d per %ds) hit by ip=%s; retry in %ds",
        scope, limit, window_s, ip, retry,
    )
    return AppError(
        "auth.too_many_attempts",
        "Too many attempts from this address. Try again later.",
        {"retry_after_seconds": retry},
        status_code=429,
        headers={"Retry-After": str(retry)},
    )


def check_register(ip: str, settings: Settings) -> None:
    limit, window = settings.auth_register_rate_limit, settings.auth_register_rate_window_s
    wait = throttle.hit("register", ip, limit, window)
    if wait:
        raise _rate_limited("register", ip, wait, limit, window)


def _user_key(username: str) -> str:
    return username.strip().lower()[:128]


def check_login(ip: str, username: str, settings: Settings) -> None:
    """Refuse before any password check: the IP's window, then the pair's lockout.
    A locked pair is refused even with the right password — else guessing goes on."""
    limit, window = settings.auth_login_rate_limit, settings.auth_login_rate_window_s
    wait = throttle.hit("login", ip, limit, window)
    if wait:
        raise _rate_limited("login", ip, wait, limit, window)
    locked = throttle.locked_for(_user_key(username), ip)
    if locked:
        retry = _retry_after(locked)
        raise AppError(
            "auth.login_locked",
            "Too many failed sign-ins. Try again later.",
            {"retry_after_seconds": retry},
            status_code=429,
            headers={"Retry-After": str(retry)},
        )


def login_failed(ip: str, username: str, settings: Settings) -> None:
    seconds = throttle.failed(_user_key(username), ip, settings)
    if seconds:
        logger.warning(
            "auth throttle: sign-in locked for %ds after repeated wrong passwords "
            "(user=%r ip=%s)",
            int(seconds), _user_key(username), ip,
        )


def login_succeeded(ip: str, username: str) -> None:
    throttle.succeeded(_user_key(username), ip)
