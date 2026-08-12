"""Bounded-concurrency contract of send_gateway_traffic.

The serial loop these tests replaced would fail every concurrency assertion here
(peak in-flight is 1 when prompts go out one at a time), which is the point: a
test that only counted poster calls would pass against either implementation.

Concurrency is proven with a `threading.Barrier` rather than sleeps — every
prompt in a group must be in flight simultaneously for the barrier to trip, so
"at least N concurrent" is a hard synchronization fact, not a timing guess. The
peak counter supplies the other half ("at most N"). Barrier tests always use a
prompt count that is a multiple of the expected width so the cyclic barrier ends
each round cleanly.
"""

import json
import threading

import pytest

from app.core.config import get_settings
from app.optimization import service as svc


class _Poster:
    """Fake gateway that records overlap, per-request bodies, and status codes.

    ``parties`` is the concurrency the caller expects: each request waits for
    that many peers before returning, so an under-parallel implementation trips
    the barrier timeout instead of quietly passing.
    """

    def __init__(self, parties, statuses=None, raise_on=None, timeout=10):
        self._lock = threading.Lock()
        self._barrier = threading.Barrier(parties, timeout=timeout)
        self._statuses = statuses or {}
        self._raise_on = raise_on
        self.in_flight = 0
        self.peak = 0
        self.broken = False
        self.bodies = []          # (session_id, prompt) in completion order
        self.threads = set()

    def __call__(self, url, content, headers):
        body = json.loads(content)
        with self._lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            self.bodies.append((body["sessionId"], body["prompt"]))
            self.threads.add(threading.get_ident())
        try:
            if self._raise_on is not None and body["prompt"] == self._raise_on:
                raise RuntimeError("expired credentials")
            try:
                self._barrier.wait()
            except threading.BrokenBarrierError:
                self.broken = True
            status = self._statuses.get(body["prompt"], 200)
            return type("Resp", (), {"status_code": status})()
        finally:
            with self._lock:
                self.in_flight -= 1

    def prompt_of(self, session_id):
        return dict(self.bodies)[session_id]


def _send(prompts, poster, **kwargs):
    return svc.send_gateway_traffic(
        "https://gw.example", "expv1", prompts,
        poster=poster, signer=lambda creds, region, req: None, **kwargs
    )


@pytest.fixture(autouse=True)
def _stub_credentials(monkeypatch):
    """No AWS: sigv4_post still resolves credentials before calling the poster."""
    from app.services import aws_clients

    monkeypatch.setattr(
        aws_clients, "get_session",
        lambda *a, **k: type(
            "S", (), {"get_credentials": lambda self: type(
                "C", (), {"get_frozen_credentials": lambda self: "frozen"})()}
        )(),
    )


def test_sends_reach_the_hard_cap_concurrently():
    # 20 prompts over the default width of 10 → two clean barrier rounds
    poster = _Poster(parties=svc.TRAFFIC_MAX_CONCURRENCY)
    result = _send([f"p{i}" for i in range(20)], poster)

    assert not poster.broken, "fewer than 10 requests were ever in flight together"
    assert poster.peak == svc.TRAFFIC_MAX_CONCURRENCY
    assert result["sent"] == 20 and result["failed"] == 0
    assert result["status_counts"] == {"200": 20}
    assert len(poster.threads) > 1  # work really left the calling thread


def test_requested_concurrency_below_the_cap_is_honoured():
    poster = _Poster(parties=3)
    result = _send([f"p{i}" for i in range(9)], poster, concurrency=3)

    assert not poster.broken
    assert poster.peak == 3
    assert result["sent"] == 9


def test_requested_concurrency_above_the_cap_is_clamped():
    poster = _Poster(parties=svc.TRAFFIC_MAX_CONCURRENCY)
    _send([f"p{i}" for i in range(20)], poster, concurrency=99)

    assert not poster.broken
    assert poster.peak == svc.TRAFFIC_MAX_CONCURRENCY  # 99 did not get through


def test_concurrency_never_exceeds_the_prompt_count():
    poster = _Poster(parties=2)
    _send(["only", "two"], poster, concurrency=svc.TRAFFIC_MAX_CONCURRENCY)

    assert not poster.broken
    assert poster.peak == 2  # no idle workers spun up for a 2-prompt dataset


def test_settings_value_bounds_concurrency(monkeypatch):
    """An operator hitting throttling dials this down without a code change."""
    monkeypatch.setenv("LAUNCHPAD_TRAFFIC_CONCURRENCY", "3")
    get_settings.cache_clear()
    try:
        assert get_settings().traffic_concurrency == 3
        poster = _Poster(parties=3)
        _send([f"p{i}" for i in range(9)], poster)
        assert not poster.broken
        assert poster.peak == 3
    finally:
        get_settings.cache_clear()


def test_oversized_settings_value_clamps_instead_of_raising(monkeypatch):
    """A too-large yaml/env value must not keep the app from booting."""
    monkeypatch.setenv("LAUNCHPAD_TRAFFIC_CONCURRENCY", "500")
    get_settings.cache_clear()
    try:
        assert get_settings().traffic_concurrency == 500  # accepted...
        poster = _Poster(parties=svc.TRAFFIC_MAX_CONCURRENCY)
        _send([f"p{i}" for i in range(20)], poster)
        assert poster.peak == svc.TRAFFIC_MAX_CONCURRENCY  # ...but clamped here
    finally:
        get_settings.cache_clear()


def test_session_ids_follow_input_order_not_completion_order():
    prompts = [f"p{i}" for i in range(6)]
    # every request blocks until all 6 are in flight, then they finish in
    # whatever order the scheduler picks — order must still come out by input
    poster = _Poster(parties=6)
    result = _send(prompts, poster, concurrency=6)

    assert not poster.broken
    assert [poster.prompt_of(sid) for sid in result["session_ids"]] == prompts


def test_progress_only_runs_on_the_calling_thread():
    caller = threading.get_ident()
    idents, lines = [], []

    def progress(msg):
        idents.append(threading.get_ident())
        lines.append(msg)

    poster = _Poster(parties=4)
    _send([f"p{i}" for i in range(8)], poster, concurrency=4, progress=progress)

    # progress() writes the experiment row; keeping it off the workers is what
    # keeps 10 threads from contending for the SQLite writer lock
    assert set(idents) == {caller}
    assert lines[-1] == "sent 8/8 (0 failed)"
    assert [int(line.split()[1].split("/")[0]) for line in lines] == list(range(1, 9))


def test_non_200_counts_as_failed_and_lands_in_status_counts():
    prompts = ["ok1", "throttled", "boom", "ok2"]
    poster = _Poster(parties=4,
                     statuses={"throttled": 429, "boom": 500})
    result = _send(prompts, poster, concurrency=4)

    assert result["sent"] == 2 and result["failed"] == 2
    assert result["status_counts"] == {"200": 2, "429": 1, "500": 1}
    assert [poster.prompt_of(sid) for sid in result["session_ids"]] == ["ok1", "ok2"]


def test_transport_exception_propagates_and_leaves_no_threads():
    # a fatal error (expired credentials) must fail the stage, not be laundered
    # into the failed count the way a 429 is
    poster = _Poster(parties=1, raise_on="p1")
    with pytest.raises(RuntimeError, match="expired credentials"):
        _send(["p0", "p1", "p2"], poster, concurrency=1)

    # every worker is joined before the raise, so none outlives the call
    assert [t for t in threading.enumerate() if t.name.startswith("exp-traffic")] == []
    # the workers check the abort flag themselves, so the prompt after the fatal
    # one is never sent — no queue of doomed requests drains behind the failure
    assert [prompt for _, prompt in poster.bodies] == ["p0", "p1"]


def test_surfaced_exception_follows_input_order_not_failure_order():
    """Two failures, the later prompt failing first — the caller sees the first."""
    p2_failed = threading.Event()

    def poster(url, content, headers):
        prompt = json.loads(content)["prompt"]
        if prompt == "p2":
            p2_failed.set()
            raise RuntimeError("boom p2")
        assert p2_failed.wait(timeout=10), "p2 never failed; test would be moot"
        raise RuntimeError("boom p1")

    with pytest.raises(RuntimeError, match="boom p1"):
        _send(["p1", "p2"], poster, concurrency=2)


def test_replay_posts_use_the_longer_traffic_timeout(monkeypatch):
    """The poster seam never sees the timeout, so pin it at the call site.

    sigv4_post's own 120s default guards the interactive canary route in
    services.invoke; a background replay is allowed to wait longer.
    """
    seen = []

    def fake_sigv4_post(url, body, **kwargs):
        seen.append(kwargs.get("timeout"))
        return type("Resp", (), {"status_code": 200})()

    monkeypatch.setattr(svc, "sigv4_post", fake_sigv4_post)
    svc.send_gateway_traffic("https://gw.example", "expv1", ["p1", "p2"])

    assert seen == [svc.TRAFFIC_REQUEST_TIMEOUT_S] * 2
    assert svc.TRAFFIC_REQUEST_TIMEOUT_S == 180.0


def test_empty_prompt_list_short_circuits():
    poster = _Poster(parties=1)
    result = _send([], poster)

    assert result == {"session_ids": [], "sent": 0, "failed": 0, "status_counts": {}}
    assert poster.bodies == []
