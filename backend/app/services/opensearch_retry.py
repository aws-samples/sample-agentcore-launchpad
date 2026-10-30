"""Transient-failure retry for the hand-signed OpenSearch REST client (N-SCN-obs6).

`opensearch_http.OpenSearchHttp` is mirrored byte-for-byte into the extractor Lambda and
the agent runtime template (ruling R15), so the hub-side resilience lives here instead:
`RetryingHttp` wraps the `urllib3.PoolManager` the funnel (`aws_clients.opensearch_http`)
constructs and re-issues a request when the previous attempt failed *transiently* —
a urllib3 timeout / connection / protocol error, or an HTTP 429, 502, 503 or 504 —
with a short exponential backoff (``attempts=3`` → sleeps of 1 s and 2 s, i.e. the
1 s/2 s/4 s schedule truncated to the two gaps three attempts have). The pool itself
keeps the pre-existing `OpenSearchHttp.__init__` default — 5 s connect / 30 s read
timeout, ``retries=False`` — unchanged; the fix adds this wrapper and the bootstrap
probe, nothing else. Every call the hub makes against the substrate (index HEAD/PUT,
the FGAC security GET/PUT, document PUT/GET/DELETE by id, search, delete-by-query) is
idempotent, so a repeated request is safe. (Only the extractor Lambda bulks, and it
does not go through this wrapper.)

Two tiers: `DEFAULT_ATTEMPTS` (3) with the funnel's default 30 s read timeout for the
bootstrap stages, where waiting ~93 s on a dead domain is acceptable, and
`INTERACTIVE_ATTEMPTS` (2) × `INTERACTIVE_READ_TIMEOUT_SECONDS` (10 s) for the console's
request-thread callers (`/api/memory`, `/api/knowledge-bases`), capping a dead or busy
domain at ~21 s (2 × 10 s + 1 s backoff) per request. `purge_agent_records` (teardown)
keeps the 30 s read timeout with the 2-attempt budget: a `_delete_by_query` over an
agent's records is not a poll and may legitimately run longer than a search.

This module constructs nothing: the pool is injected, so the `aws_clients.py`
construction funnel (`tests/test_client_funnel.py`) stays the only builder.
"""

import time
from collections.abc import Callable
from typing import Any

import urllib3

# urllib3 failures that mean "the request may not have reached/finished on the server":
# read/connect timeouts (`ReadTimeoutError` is what the 2026-10-12 host update caught),
# refused/reset connections and truncated responses.
TRANSIENT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    urllib3.exceptions.TimeoutError,
    urllib3.exceptions.NewConnectionError,
    urllib3.exceptions.ProtocolError,
)
# Statuses the OpenSearch data plane answers while throttling or momentarily unavailable.
TRANSIENT_STATUSES: frozenset[int] = frozenset({429, 502, 503, 504})
# Everything the pool can raise *through* `RetryingHttp` once its attempts are spent — the
# transient family above (re-raised on the last attempt) plus any other urllib3 failure
# (`HTTPError` is their common base: SSL, pool, decode, …) and a bare socket `OSError`.
# A best-effort caller that must not fail once its own ledger write is done catches this
# (N14-obs4: a `ReadTimeoutError` from the store purge turned a finished delete into a 502).
TRANSPORT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    urllib3.exceptions.HTTPError,
    OSError,
)
DEFAULT_ATTEMPTS = 3
# Request-thread callers: one retry only, so a dead domain blocks a request for at most
# ~21 s (2 × 10 s interactive read timeout + 1 s backoff) instead of ~93 s with the
# default budget and the default 30 s read timeout.
INTERACTIVE_ATTEMPTS = 2
# Read timeout (seconds) for the same request-thread callers (N-SCN-obs11). A console
# poll — the KB detail's ingestion-job lookup, a memory record page — normally answers in
# < 0.5 s; one that is still unanswered after 10 s means the single t3.small node is
# busy (bulk HNSW indexing + `_refresh` during an ingestion), and waiting the full 30 s
# just holds the request thread. The bootstrap stages keep the funnel default
# (`aws_clients.OPENSEARCH_READ_TIMEOUT_SECONDS`, 30 s): the FGAC security API on a
# t3.small domain can genuinely take that long, and nobody is polling them.
INTERACTIVE_READ_TIMEOUT_SECONDS = 10.0
DEFAULT_BACKOFF_SECONDS = 1.0


def backoff_schedule(attempts: int = DEFAULT_ATTEMPTS, base: float = DEFAULT_BACKOFF_SECONDS
                     ) -> tuple[float, ...]:
    """Sleep before retry ``n`` (1-based): ``base * 2**(n-1)`` — 1 s, 2 s, 4 s, …"""
    return tuple(base * (2**i) for i in range(max(attempts - 1, 0)))


class RetryingHttp:
    """Duck-types the one method `OpenSearchHttp` uses on its pool — ``request(method,
    url, body=, headers=)`` — and retries transient failures of the wrapped pool."""

    def __init__(
        self,
        inner: Any,
        *,
        attempts: int = DEFAULT_ATTEMPTS,
        backoff: float = DEFAULT_BACKOFF_SECONDS,
        sleeper: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] | None = None,
    ) -> None:
        if attempts < 1:
            raise ValueError("attempts must be >= 1")
        self.inner = inner
        self.attempts = attempts
        self.backoff = backoff
        self.sleeper = sleeper
        self.log = log

    def _note(self, message: str) -> None:
        if self.log is not None:
            self.log(message)

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        delays = backoff_schedule(self.attempts, self.backoff)
        for attempt in range(1, self.attempts + 1):
            last = attempt == self.attempts
            try:
                resp = self.inner.request(method, url, **kwargs)
            except TRANSIENT_EXCEPTIONS as exc:
                if last:
                    raise
                self._note(
                    f"opensearch {method} {url}: {type(exc).__name__} — retry "
                    f"{attempt}/{self.attempts - 1} in {delays[attempt - 1]:g}s"
                )
                self.sleeper(delays[attempt - 1])
                continue
            if resp.status in TRANSIENT_STATUSES and not last:
                self._note(
                    f"opensearch {method} {url}: HTTP {resp.status} — retry "
                    f"{attempt}/{self.attempts - 1} in {delays[attempt - 1]:g}s"
                )
                self.sleeper(delays[attempt - 1])
                continue
            return resp
        raise AssertionError("unreachable")  # pragma: no cover — loop returns or raises
