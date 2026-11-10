"""N-SCN-obs6: transient OpenSearch failures are retried inside the client funnel.

The 2026-10-12 host update caught one urllib3 `ReadTimeoutError` from an idempotent FGAC
security PUT on the shared t3.small domain and recorded the substrate unavailable. The
funnel now wraps its pool in `opensearch_retry.RetryingHttp`; these tests pin the policy
(urllib3 timeouts + HTTP 429/502/503/504 only, 1 s / 2 s backoff; 3 attempts for the
bootstrap stages, 2 for request-thread callers — the budget is part of the funnel's cache
key) and the retry WARNING the funnel wires in.
"""

import json
import logging
import pathlib
import re
from types import SimpleNamespace

import pytest
import urllib3
from botocore.credentials import Credentials

from app.services import aws_clients, opensearch_retry
from app.services import memory_bootstrap as mb
from app.services import opensearch_http as core
from app.services.opensearch_retry import RetryingHttp
from app.services.workspace import WorkspaceContext

CREDS = Credentials("AKIATEST", "secret", "token")
ROLE_PATH = f"/_plugins/_security/api/roles/{mb.security_role()}"


class _Scripted:
    """A urllib3-shaped pool answering one scripted outcome per call: an exception
    instance is raised, an int is an HTTP status (body `{}`), a ``(status, payload)``
    tuple answers that JSON body."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, str]] = []

    def request(self, method, url, body=None, headers=None):
        self.calls.append((method, url))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, tuple):
            status, payload = outcome
            return SimpleNamespace(status=status, data=json.dumps(payload).encode())
        return SimpleNamespace(status=outcome, data=b"{}")


def _timeout() -> urllib3.exceptions.ReadTimeoutError:
    return urllib3.exceptions.ReadTimeoutError(None, "/x", "Read timed out. (read timeout=30)")


def _client(pool, sleeps: list[float]) -> core.OpenSearchHttp:
    return core.OpenSearchHttp(
        "search-lp-memory.cn-northwest-1.es.example", "cn-northwest-1", lambda: CREDS,
        http=RetryingHttp(pool, sleeper=sleeps.append),
    )


def test_security_put_succeeds_on_the_second_attempt_after_a_read_timeout():
    """(a) the live defect: GET answers 404 (nothing provisioned), the PUT times out once,
    the retried PUT lands — `_ensure_security` reports the role created, one 1 s sleep."""
    pool = _Scripted(404, _timeout(), 200)
    sleeps: list[float] = []
    created: list[str] = []
    reused: list[str] = []
    mb._ensure_security(
        _client(pool, sleeps), "security role", ROLE_PATH, mb.security_role_body(),
        created, reused,
    )
    assert created == [f"security role {mb.security_role()}"] and reused == []
    assert [m for m, _u in pool.calls] == ["GET", "PUT", "PUT"]
    assert sleeps == [1.0]


def test_exhausted_retries_raise_the_last_transient_error():
    """(b) three read timeouts in a row: the third is re-raised unchanged (the bootstrap
    stage's `except urllib3.exceptions.HTTPError` still sees it), after 1 s + 2 s."""
    pool = _Scripted(_timeout(), _timeout(), _timeout())
    sleeps: list[float] = []
    with pytest.raises(urllib3.exceptions.ReadTimeoutError):
        _client(pool, sleeps).request("PUT", ROLE_PATH, mb.security_role_body())
    assert len(pool.calls) == 3 and sleeps == [1.0, 2.0]


@pytest.mark.parametrize("status", sorted(opensearch_retry.TRANSIENT_STATUSES))
def test_transient_http_statuses_are_retried(status):
    pool = _Scripted(status, 200)
    sleeps: list[float] = []
    assert _client(pool, sleeps).request("GET", "/_cluster/health") == {}
    assert len(pool.calls) == 2 and sleeps == [1.0]


def test_exhausted_transient_status_surfaces_as_opensearch_error():
    pool = _Scripted(503, 503, 503)
    sleeps: list[float] = []
    with pytest.raises(core.OpenSearchError) as exc:
        _client(pool, sleeps).request("GET", "/_cluster/health")
    assert exc.value.status == 503 and len(pool.calls) == 3


@pytest.mark.parametrize("status", [400, 403, 404, 409])
def test_non_transient_statuses_are_not_retried(status):
    """A real rejection (forbidden, not found, mapping conflict) must surface at once —
    retrying it would only slow every 404-driven `_security_get` probe down."""
    pool = _Scripted(status)
    sleeps: list[float] = []
    with pytest.raises(core.OpenSearchError) as exc:
        _client(pool, sleeps).request("GET", ROLE_PATH)
    assert exc.value.status == status and len(pool.calls) == 1 and sleeps == []


def test_connection_errors_count_as_transient_but_other_exceptions_do_not():
    sleeps: list[float] = []
    pool = _Scripted(urllib3.exceptions.ProtocolError("Connection aborted"), 200)
    assert _client(pool, sleeps).request("HEAD", "/launchpad-memory") == {"found": True}
    assert sleeps == [1.0]

    pool = _Scripted(ValueError("not a transport failure"))
    with pytest.raises(ValueError):
        _client(pool, sleeps).request("HEAD", "/launchpad-memory")
    assert sleeps == [1.0]  # no extra sleep


def test_backoff_schedule_is_exponential_from_one_second():
    assert opensearch_retry.backoff_schedule(3) == (1.0, 2.0)
    assert opensearch_retry.backoff_schedule(4) == (1.0, 2.0, 4.0)
    assert opensearch_retry.backoff_schedule(1) == ()
    with pytest.raises(ValueError):
        RetryingHttp(_Scripted(), attempts=0)


def test_retry_log_names_the_failure_without_the_body():
    notes: list[str] = []
    pool = _Scripted(_timeout(), 200)
    RetryingHttp(pool, sleeper=lambda _s: None, log=notes.append).request(
        "PUT", "https://h/x", body=json.dumps({"secret": "never"})
    )
    assert len(notes) == 1 and "ReadTimeoutError" in notes[0] and "retry 1/2 in 1s" in notes[0]
    assert "never" not in notes[0]


def _probe_answers(cluster: str) -> list:
    """Scripted pool answers for one full healthy probe: cluster health, the three
    index HEADs, the FGAC role GET and the role mapping GET."""
    return [
        (200, {"status": cluster}),
        200, 200, 200,
        (200, {mb.security_role(): mb.security_role_body()}),
        (200, {mb.security_role(): {"users": ["arn:aws-cn:iam::111:role/exec"]}}),
    ]


@pytest.mark.parametrize("cluster", ["green", "yellow"])
def test_stage_exhausts_its_retries_then_the_probe_passes_through_the_same_wrapper(cluster):
    """Review gap: the live sequence end to end through `RetryingHttp` over a scripted
    *pool* (not a fake client). The FGAC role GET of the stage times out three times
    (1 s + 2 s) and `_security_get` re-raises; the probe that follows on the same client
    is itself retried once (503 on `_cluster/health`) and then answers healthy — a
    `yellow` cluster (one-node t3.small domain, replicas unassigned) included."""
    pool = _Scripted(_timeout(), _timeout(), _timeout(), 503, *_probe_answers(cluster))
    sleeps: list[float] = []
    client = _client(pool, sleeps)

    with pytest.raises(urllib3.exceptions.ReadTimeoutError):
        mb._security_get(client, ROLE_PATH)
    assert sleeps == [1.0, 2.0] and len(pool.calls) == 3

    healthy, detail = mb.probe_memory_substrate(client)
    assert healthy, detail
    assert detail == (
        f"cluster {cluster}; 3 indices present; "
        f"security role and role mapping {mb.security_role()} present"
    )
    assert sleeps == [1.0, 2.0, 1.0]  # the probe's own retry
    assert [m for m, _u in pool.calls[3:]] == ["GET", "GET", "HEAD", "HEAD", "HEAD", "GET", "GET"]
    assert pool.calls[-2][1].endswith(ROLE_PATH)
    assert pool.calls[-1][1].endswith(f"/_plugins/_security/api/rolesmapping/{mb.security_role()}")
    assert pool.outcomes == []


def test_probe_through_the_wrapper_reports_an_absent_role_mapping():
    """Review follow-up: the mapping is provisioned by the stage, so the probe requires
    it — role present, mapping 404 ⇒ unhealthy, detail names the mapping."""
    pool = _Scripted(*_probe_answers("green")[:-1], 404)
    sleeps: list[float] = []
    assert mb.probe_memory_substrate(_client(pool, sleeps)) == (
        False, f"cluster green; role mapping {mb.security_role()} absent"
    )
    assert sleeps == []  # a 404 is not transient


def test_a_dead_domain_exhausts_the_probe_too_through_the_wrapper():
    """Stage and probe both run their full budget against a pool that only times out:
    the probe never raises, it reports the exception type after 1 s + 2 s of its own."""
    pool = _Scripted(*[_timeout() for _ in range(6)])
    sleeps: list[float] = []
    client = _client(pool, sleeps)
    with pytest.raises(urllib3.exceptions.ReadTimeoutError):
        client.index_exists(mb.naming.memory_index())
    assert mb.probe_memory_substrate(client) == (False, "probe failed (ReadTimeoutError)")
    assert sleeps == [1.0, 2.0, 1.0, 2.0] and len(pool.calls) == 6


@pytest.fixture
def funnel(monkeypatch):
    """A clean funnel cache with a stub session; yields a hub-shaped context."""
    aws_clients.reset_cache()
    monkeypatch.setattr(aws_clients, "get_session", lambda *a, **k: SimpleNamespace(
        get_credentials=lambda: SimpleNamespace(get_frozen_credentials=lambda: CREDS)))
    yield WorkspaceContext(account_id="111122223333", region="cn-northwest-1")
    aws_clients.reset_cache()


def test_funnel_wraps_a_pool_with_the_retry_policy_and_a_30s_read_timeout(funnel):
    """The funnel is the only construction site (tests/test_client_funnel.py): the pool
    it builds keeps the pre-existing `OpenSearchHttp` defaults (5 s connect / 30 s read,
    no urllib3-level retries — RetryingHttp owns them), the default 3-attempt policy,
    and logs retries through the `app.services.opensearch_retry` logger."""
    client = aws_clients.opensearch_http(funnel, "search-a.example")
    assert isinstance(client.http, RetryingHttp)
    assert client.http.attempts == opensearch_retry.DEFAULT_ATTEMPTS == 3
    assert client.http.log == logging.getLogger("app.services.opensearch_retry").warning
    pool = client.http.inner
    assert isinstance(pool, urllib3.PoolManager)
    retries = pool.connection_pool_kw["retries"]  # urllib3 normalizes False → Retry(total=False)
    assert retries is False or retries.total is False
    timeout = pool.connection_pool_kw["timeout"]
    assert (timeout.connect_timeout, timeout.read_timeout) == (5.0, 30.0)


def test_funnel_pool_keeps_eight_connections_per_host_without_blocking(funnel):
    """N17-obs238: urllib3's default pool KEEPS one connection per host; `block=False`
    never queues (a request without an idle connection opens its own) but every connection
    returning past `maxsize` is closed with "Connection pool is full, discarding connection
    … size: 1" and the next request pays a fresh TLS handshake against the busy domain —
    twice in the 2026-10-21 forensics while the console polled a KB concurrently. The
    interactive tier is ONE pool shared by the KB and memory consoles, so the funnel keeps
    eight connections and stays non-blocking; the tier's timeout and retry budget are
    unchanged. The Lambda's mirrored `OpenSearchHttp` default pool is not the funnel's."""
    assert aws_clients.OPENSEARCH_POOL_MAXSIZE == 8
    interactive = aws_clients.opensearch_http(
        funnel, "search-a.example", attempts=opensearch_retry.INTERACTIVE_ATTEMPTS,
        read_timeout=opensearch_retry.INTERACTIVE_READ_TIMEOUT_SECONDS,
    )
    for client in (aws_clients.opensearch_http(funnel, "search-a.example"), interactive):
        kw = client.http.inner.connection_pool_kw
        assert kw["maxsize"] == 8 and kw["block"] is False
    assert interactive.http.attempts == 2
    assert interactive.http.inner.connection_pool_kw["timeout"].read_timeout == 10.0
    # the Lambda's own default pool is untouched (urllib3 default: one connection)
    from app.services.opensearch_http import OpenSearchHttp

    bare = OpenSearchHttp("search-b.example", "cn-northwest-1", lambda: CREDS)
    assert "maxsize" not in bare.http.connection_pool_kw


def test_funnel_caches_one_client_per_retry_budget(funnel):
    """Review follow-up: `attempts` is part of the cache key — the bootstrap's 3-attempt
    client and a request thread's 2-attempt client for the same endpoint are two
    objects, each reused on its own budget."""
    bootstrap_client = aws_clients.opensearch_http(funnel, "search-a.example")
    interactive = aws_clients.opensearch_http(
        funnel, "search-a.example", attempts=opensearch_retry.INTERACTIVE_ATTEMPTS
    )
    assert bootstrap_client is not interactive
    assert bootstrap_client is aws_clients.opensearch_http(funnel, "search-a.example")
    assert interactive is aws_clients.opensearch_http(funnel, "search-a.example", attempts=2)
    assert (bootstrap_client.http.attempts, interactive.http.attempts) == (3, 2)
    assert opensearch_retry.INTERACTIVE_ATTEMPTS == 2


def test_funnel_caches_one_client_per_read_timeout(funnel):
    """N-SCN-obs11: `read_timeout` is part of the cache key too — the bootstrap's 30 s
    client and the console's 10 s client for the same endpoint and budget are two pools,
    each reused on its own timeout; the default stays 30 s."""
    default = aws_clients.opensearch_http(funnel, "search-a.example", attempts=2)
    interactive = aws_clients.opensearch_http(
        funnel, "search-a.example", attempts=2,
        read_timeout=opensearch_retry.INTERACTIVE_READ_TIMEOUT_SECONDS,
    )
    assert default is not interactive
    assert default is aws_clients.opensearch_http(funnel, "search-a.example", attempts=2)
    assert interactive is aws_clients.opensearch_http(
        funnel, "search-a.example", attempts=2, read_timeout=10
    )
    for client, read in ((default, 30.0), (interactive, 10.0)):
        timeout = client.http.inner.connection_pool_kw["timeout"]
        assert (timeout.connect_timeout, timeout.read_timeout) == (5.0, read)
    assert opensearch_retry.INTERACTIVE_READ_TIMEOUT_SECONDS == 10.0
    assert aws_clients.OPENSEARCH_READ_TIMEOUT_SECONDS == 30.0


def test_the_console_backends_ask_for_the_interactive_tier_and_bootstrap_does_not(
    funnel, monkeypatch,
):
    """The request-thread callers (`memory_selfhosted`, `knowledge_selfhosted`) pass
    both interactive constants; the bootstrap stages call the funnel with no overrides
    (3 attempts / 30 s) — asserted by reading their call sites, since running a bootstrap
    stage needs a whole substrate."""
    from app.services import knowledge_selfhosted, memory_selfhosted

    seen: list[dict] = []

    def _capture(ctx, endpoint, **kwargs):
        seen.append(kwargs)
        return object()

    monkeypatch.setattr(aws_clients, "opensearch_http", _capture)
    ws = WorkspaceContext(
        account_id="111122223333", region="cn-northwest-1",
        resources={"memory_os_endpoint": "search-mem.example", "memory_id": "m",
                   "memory_bucket": "b", "memory_prefix": "memory",
                   "memory_queue_url": "https://sqs.example/q", "artifacts_bucket": "b"},
    )
    memory_selfhosted.SelfHostedMemoryBackend()._os(ws)
    knowledge_selfhosted.SelfHostedKnowledgeBackend()._os(ws)
    assert seen == [{
        "attempts": opensearch_retry.INTERACTIVE_ATTEMPTS,
        "read_timeout": opensearch_retry.INTERACTIVE_READ_TIMEOUT_SECONDS,
    }] * 2
    app_dir = pathlib.Path(aws_clients.__file__).resolve().parents[1]
    for stage in ("services/bootstrap.py", "services/workspace_bootstrap.py"):
        text = (app_dir / stage).read_text(encoding="utf-8")
        calls = re.findall(r"aws_clients\.opensearch_http\(([^)]*)\)", text)
        assert calls, stage
        for args in calls:
            assert "attempts" not in args and "read_timeout" not in args, (stage, args)


def test_both_retry_budgets_are_observed_on_a_dead_domain(funnel, monkeypatch):
    """3 attempts (bootstrap) sleep 1 s + 2 s, 2 attempts (request thread) sleep 1 s —
    with the 30 s read timeout that is the ~93 s vs ~61 s worst case per request (and
    ~21 s for the console's 10 s tier)."""
    pools: list[_Scripted] = []

    def _dead_pool(*_read_timeout):
        pools.append(_Scripted(_timeout(), _timeout(), _timeout()))
        return pools[-1]

    monkeypatch.setattr(aws_clients, "_opensearch_pool", _dead_pool)
    for attempts, expected_sleeps in ((3, [1.0, 2.0]), (2, [1.0])):
        client = aws_clients.opensearch_http(funnel, "search-dead.example", attempts=attempts)
        sleeps: list[float] = []
        client.http.sleeper = sleeps.append
        with pytest.raises(urllib3.exceptions.ReadTimeoutError):
            client.request("GET", "/_cluster/health")
        assert sleeps == expected_sleeps
        assert len(pools[-1].calls) == attempts


def test_a_retry_through_the_funnel_emits_one_warning_record(funnel, monkeypatch, caplog):
    """Review follow-up: retries are not silent — the funnel wires `RetryingHttp.log` to
    the `app.services.opensearch_retry` logger at WARNING (one record per retry, naming
    the failure type and attempt, never a body)."""
    monkeypatch.setattr(
        aws_clients, "_opensearch_pool", lambda *_rt: _Scripted(_timeout(), 200)
    )
    client = aws_clients.opensearch_http(funnel, "search-flaky.example")
    client.http.sleeper = lambda _s: None
    with caplog.at_level(logging.WARNING, logger="app.services.opensearch_retry"):
        assert client.request("GET", "/_cluster/health") == {}
    records = [r for r in caplog.records if r.name == "app.services.opensearch_retry"]
    assert len(records) == 1 and records[0].levelno == logging.WARNING
    message = records[0].getMessage()
    assert "ReadTimeoutError" in message and "retry 1/2 in 1s" in message
    assert "GET https://search-flaky.example/_cluster/health" in message
