"""N17-obs483: the interceptor's bundle source serves stale-while-revalidate with BOUNDED
staleness — no request blocks on a TTL refresh; past ``max_stale_s`` a request refreshes
synchronously and fails closed if that fails; a forced refresh (engine set mismatch) is
synchronous; one refresh in flight at a time; a failing refresh keeps the cache."""

from __future__ import annotations

import json
import threading
import time

import pytest

from app.policy.interceptor import bundle as ib
from tests.policy_principal.conftest import load_fixture
from tests.policy_principal.fakes import EphemeralSigner

GW = "launchpad-gw-exampleid0"
BUCKET = "bkt"


class Store:
    """S3 stand-in: the pointer / bundles / key, with switches to fail or block reads."""

    def __init__(self) -> None:
        signer = EphemeralSigner()
        self.raw = load_fixture("gateway_bundle.json")
        self.raw["pubkeys"] = {signer.kid: signer.public_spki_b64}
        self.objects: dict[str, bytes] = {}
        self.fail = False
        self.gate: threading.Event | None = None  # set → reads block until released
        self.reads = 0
        self.publish(self.raw["version"])
        self.objects[self.raw["engine"]["key_s3_key"]] = b"k" * 32

    def publish(self, version: int) -> None:
        raw = dict(self.raw, version=version)
        self.objects[ib.bundle_key(GW, version)] = json.dumps(raw).encode()
        self.objects[ib.pointer_key(GW)] = json.dumps(ib.pointer(GW, version, "t")).encode()

    def fetch(self, _bucket: str, key: str) -> bytes:
        self.reads += 1
        if self.gate is not None:
            assert self.gate.wait(5), "test gate never released"
        if self.fail:
            raise ConnectionError("s3 down")
        return self.objects[key]


def source(store: Store, clock: list[float], **kw) -> ib.BundleSource:
    return ib.BundleSource(bucket=BUCKET, gateway_id=GW, fetch=store.fetch,
                           clock=lambda: clock[0], **kw)


def test_a_fresh_bundle_is_served_without_any_read():
    store, clock = Store(), [0.0]
    src = source(store, clock)
    assert src.get()[0].version == 7
    reads = store.reads
    clock[0] = 29.0
    assert src.get()[0].version == 7 and store.reads == reads


def test_no_request_blocks_on_a_ttl_refresh():
    """The refresh past the TTL runs on a real background thread while S3 is slow; the
    request returns the cached bundle at once."""
    store, clock = Store(), [0.0]
    src = source(store, clock)
    src.get()
    store.publish(8)
    store.gate = threading.Event()  # every read now blocks until released
    clock[0] = 31.0
    started = time.perf_counter()
    bundle, _key = src.get()
    assert time.perf_counter() - started < 0.5, "the request waited for the refresh"
    assert bundle.version == 7
    store.gate.set()
    deadline = time.monotonic() + 5
    while src.get()[0].version != 8 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert src.get()[0].version == 8


def test_only_one_refresh_is_in_flight_under_concurrent_invocations():
    store, clock, spawned = Store(), [0.0], []
    src = source(store, clock, spawn=spawned.append)
    src.get()
    clock[0] = 40.0
    threads = [threading.Thread(target=src.get) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(spawned) == 1


def test_a_failing_refresh_keeps_the_cache_until_max_stale_then_fails_closed():
    store, clock, spawned = Store(), [0.0], []
    src = source(store, clock, spawn=spawned.append)
    src.get()
    store.fail = True
    clock[0] = 31.0
    assert src.get()[0].version == 7
    spawned.pop()()  # the background refresh fails
    assert src.last_error.startswith("bundle_fetch_failed")
    clock[0] = 45.0
    assert src.get()[0].version == 7 and spawned == [], "one attempt per TTL, not per request"
    clock[0] = 62.0
    assert src.get()[0].version == 7 and len(spawned) == 1
    spawned.pop()()
    clock[0] = 121.0  # older than max_stale_s: a synchronous refresh, which fails
    with pytest.raises(ib.BundleError) as err:
        src.get()
    assert err.value.code == "bundle_stale"
    assert spawned == [], "past max_stale the refresh is synchronous, never spawned"


def test_past_max_stale_a_successful_sync_refresh_serves_the_new_bundle():
    store, clock, spawned = Store(), [0.0], []
    src = source(store, clock, spawn=lambda fn: None)  # background refreshes never run
    src.get()
    store.publish(9)
    clock[0] = 31.0
    assert src.get()[0].version == 7
    clock[0] = 125.0
    assert src.get()[0].version == 9, "the synchronous refresh at max_stale loaded v9"
    assert spawned == []


def test_a_forced_refresh_is_synchronous():
    store, clock = Store(), [0.0]
    src = source(store, clock, spawn=lambda fn: pytest.fail("a forced refresh never spawns"))
    src.get()
    store.publish(8)
    src.refresh(force=True)
    assert src.get()[0].version == 8


def test_a_cold_source_loads_synchronously_and_fails_closed_without_s3():
    store, clock = Store(), [0.0]
    store.fail = True
    src = source(store, clock, spawn=lambda fn: pytest.fail("a cold start never spawns"))
    with pytest.raises(ib.BundleError) as err:
        src.get()
    assert err.value.code == "bundle_unavailable"


def test_the_interceptor_denies_bundle_stale_past_max_stale():
    """End to end through the handler: refreshes keep failing past max_stale → the call is
    denied interceptor_error with reason_code bundle_stale (fail closed)."""
    from types import SimpleNamespace

    from app.policy.interceptor import core, handler
    from app.policy.interceptor.engine_client import EngineClient

    store, clock = Store(), [0.0]
    src = source(store, clock, spawn=lambda fn: None)
    src.get()
    store.fail = True
    clock[0] = 130.0
    deps = core.Deps(source=src, audit=lambda _l: None,
                     engine=lambda b, k: EngineClient(endpoints=b.endpoints, shards=b.shards,
                                                      key=k, transport=None))
    event = load_fixture("interceptor_events/request_tools_call_role.json")["event"]
    out = handler.handle(event, SimpleNamespace(aws_request_id="r", client_context=None), deps)
    error = out["mcp"]["transformedGatewayResponse"]["body"]["error"]
    assert error["data"]["reason_code"] == "bundle_stale"


def test_the_s3_client_times_out_inside_the_lambda_budget():
    """Review F5 (N17-obs494): a stalled S3 read becomes a refresh failure in ~2 s, well inside
    the 5 s Lambda timeout, so the SWR path (serve the cache, then fail closed) decides — never
    the gateway's own timeout."""
    from app.policy.interceptor import handler
    from app.services.policy_interceptor import TIMEOUT_S

    config = handler.s3_client_config()
    assert config.connect_timeout == handler.S3_CONNECT_TIMEOUT_S == 0.5
    assert config.read_timeout == handler.S3_READ_TIMEOUT_S == 1.5
    assert config.retries == {"max_attempts": 1}
    assert config.connect_timeout + config.read_timeout < TIMEOUT_S / 2


def test_a_stalled_read_keeps_the_cache_then_fails_closed():
    """The timeout surfaces as botocore's ReadTimeoutError: the same failure path as any other
    fetch error — the last bundle is served until max_stale, then `bundle_stale`."""
    from botocore.exceptions import ReadTimeoutError

    store = Store()
    clock = [0.0]
    src = source(store, clock, spawn=lambda fn: fn())
    src.get()

    def stalled(_bucket: str, _key: str) -> bytes:
        raise ReadTimeoutError(endpoint_url="https://s3.cn-northwest-1.amazonaws.com.cn")

    src.fetch = stalled
    clock[0] = ib.DEFAULT_TTL_S + 1
    bundle, _key = src.get()  # stale-while-revalidate: the cache is served
    assert bundle.version == store.raw["version"]
    clock[0] = ib.DEFAULT_MAX_STALE_S + 1
    with pytest.raises(ib.BundleError) as err:
        src.get()
    assert err.value.code == "bundle_stale"


# ── N17-obs498: a Lambda freezes between invocations — the refresh completes in the RESPONSE ──


def test_a_frozen_background_refresh_is_finished_by_settle_not_by_the_request_path():
    """M2 9c: the REQUEST past the TTL spawns a refresh, the container is frozen the moment
    the handler returns (modelled by the gated read), so the refresh never completed and the
    next REQUEST past max_stale paid the synchronous S3 round trip. Now the RESPONSE
    invocation `settle`s it: the request path never fetches while responses flow."""
    store, clock = Store(), [0.0]
    src = source(store, clock)
    src.get()
    store.publish(8)
    store.gate = threading.Event()  # frozen: the thread cannot progress
    clock[0] = 31.0
    assert src.get()[0].version == 7  # served from the cache, refresh spawned
    assert src._in_flight is True
    # the RESPONSE invocation: the container runs again, settle finishes the refresh
    clock[0] = 31.1
    store.gate.set()
    assert src.settle(budget_s=5.0) is True
    assert src.settled == 1 and src._in_flight is False and src.get()[0].version == 8
    # 94 s later (past the OLD max_stale, not the new one): no synchronous fetch on the request
    store.publish(9)
    store.gate = threading.Event()
    clock[0] = 125.0
    started = time.perf_counter()
    bundle, _key = src.get()
    assert time.perf_counter() - started < 0.5, "the request paid the fetch"
    assert bundle.version == 8
    store.gate.set()
    assert src.settle(budget_s=5.0) is True and src.get()[0].version == 9


def test_settle_runs_a_due_refresh_when_nothing_is_in_flight():
    store, clock = Store(), [0.0]
    src = source(store, clock)
    src.get()
    store.publish(8)
    clock[0] = 45.0
    assert src.settle(budget_s=5.0) is True  # nothing spawned yet: settle refreshes itself
    assert src.settled == 1 and src.get()[0].version == 8
    assert src.settle(budget_s=5.0) is True and src.settled == 1  # fresh: nothing to do


def test_settle_respects_its_budget_on_a_stalled_read_and_never_raises():
    store, clock = Store(), [0.0]
    src = source(store, clock)
    src.get()
    store.publish(8)
    store.gate = threading.Event()  # S3 stalls
    clock[0] = 40.0
    started = time.perf_counter()
    assert src.settle(budget_s=0.2) is False
    assert time.perf_counter() - started < 1.0, "settle outlived its budget"
    assert src.get()[0].version == 7  # the cache is still served
    store.gate.set()
    assert src.settle(budget_s=5.0) is True and src.get()[0].version == 8


def test_a_frozen_in_flight_attempt_is_superseded_after_a_ttl():
    """A refresh that has been "in flight" for a whole TTL is a frozen thread: a new attempt
    may start instead of pinning `_in_flight` forever (re-review F5 secondary note)."""
    store, clock, spawned = Store(), [0.0], []
    src = source(store, clock, spawn=spawned.append)  # never runs: frozen for good
    src.get()
    clock[0] = 31.0
    src.get()
    assert len(spawned) == 1 and src._in_flight is True
    clock[0] = 50.0
    src.get()
    assert len(spawned) == 1  # younger than a TTL: still waiting for it
    clock[0] = 62.0
    src.get()
    assert len(spawned) == 2  # frozen: superseded


def test_a_cold_load_fetches_the_bundle_and_the_key_in_parallel():
    store, clock = Store(), [0.0]
    order: list[str] = []
    real = store.fetch

    def fetch(bucket: str, key: str) -> bytes:
        order.append(key.rsplit("/", 1)[-1])
        return real(bucket, key)

    src = ib.BundleSource(bucket=BUCKET, gateway_id=GW, fetch=fetch, clock=lambda: clock[0])
    src.get()
    assert order[0] == "current.json" and set(order[1:]) == {"v7.json", "engine-key"}
    assert len(order) == 3  # the key read exactly once, from the parallel prefetch


def test_preload_builds_the_dependencies_in_the_init_phase(monkeypatch):
    from app.policy.interceptor import handler

    monkeypatch.setattr(handler, "_DEPS", {})
    monkeypatch.delenv("LAUNCHPAD_POLICY_BUCKET", raising=False)
    assert handler.preload() is False and handler._DEPS == {}  # not a Lambda: no-op
    store = Store()
    monkeypatch.setenv("LAUNCHPAD_POLICY_BUCKET", BUCKET)
    monkeypatch.setenv("LAUNCHPAD_POLICY_GATEWAY_ID", GW)
    monkeypatch.setattr(handler, "_s3_fetch", lambda: store.fetch)
    assert handler.preload() is True
    assert handler._DEPS and handler.deps().source._bundle.version == 7
    store.fail = True  # a failing S3 at init: never raises, the request path reports it
    monkeypatch.setattr(handler, "_DEPS", {})
    assert handler.preload() is False
    monkeypatch.setattr(handler, "_DEPS", {})


# ── obs498 follow-up: the `bundle_refresh` diagnostic line ──────────────────────────────────


def _refresh_lines(events: list[dict]) -> list[dict]:
    return [e["launchpad.policy.interceptor"] for e in events
            if e.get("launchpad.policy.interceptor", {}).get("event") == "bundle_refresh"]


def test_bundle_refresh_lines_name_the_path_and_the_outcome():
    """One line per refresh attempt, fixed vocabulary: sync (cold load / past max_stale),
    background (the thread a REQUEST spawned), settle (started by the RESPONSE); outcome
    loaded / unchanged / behind / failed with the error code; never a key or a body."""
    store, clock, events = Store(), [0.0], []
    src = source(store, clock, spawn=lambda fn: fn(), on_refresh=events.append)
    src.get()  # cold load: sync
    store.publish(8)
    clock[0] = 31.0
    src.get()  # past the TTL: the background refresh (run inline here) loads v8
    clock[0] = 70.0
    assert src.settle(budget_s=5.0) is True  # due again, nothing in flight: settle refreshes
    store.publish(5)  # a pointer behind the loaded version
    clock[0] = 110.0
    src.settle(budget_s=5.0)
    store.fail = True
    clock[0] = 240.0  # past max_stale from the last confirmation: sync, which fails
    with pytest.raises(ib.BundleError):
        src.get()
    lines = _refresh_lines(events)
    assert [(x["mode"], x["outcome"], x["version"]) for x in lines] == [
        ("sync", "loaded", 7), ("background", "loaded", 8), ("settle", "unchanged", 8),
        ("settle", "behind", 8), ("sync", "failed", None)]
    assert lines[-1]["error"].startswith("bundle_fetch_failed")
    for line in lines:
        assert set(line) == {"event", "mode", "outcome", "ms", "version", "error"}
        assert line["mode"] in ib.REFRESH_MODES and line["outcome"] in ib.REFRESH_OUTCOMES
        assert isinstance(line["ms"], float) and line["ms"] >= 0.0
        assert line["error"] is None or line["outcome"] == "failed"
    assert "k" * 32 not in json.dumps(events) and "eyJ" not in json.dumps(events)


def test_a_failing_refresh_sink_never_breaks_a_refresh():
    def boom(_line: dict) -> None:
        raise RuntimeError("sink down")

    store, clock = Store(), [0.0]
    src = source(store, clock, on_refresh=boom)
    assert src.get()[0].version == 7
    store.publish(8)
    clock[0] = 45.0
    assert src.settle(budget_s=5.0) is True and src.get()[0].version == 8


def test_the_lambda_wires_bundle_refresh_lines_to_its_audit_sink(monkeypatch):
    from app.policy.interceptor import handler

    monkeypatch.setattr(handler, "_DEPS", {})
    monkeypatch.setenv("LAUNCHPAD_POLICY_BUCKET", BUCKET)
    monkeypatch.setenv("LAUNCHPAD_POLICY_GATEWAY_ID", GW)
    store, lines = Store(), []
    monkeypatch.setattr(handler, "_s3_fetch", lambda: store.fetch)
    monkeypatch.setattr(handler, "_audit", lines.append)
    assert handler.preload() is True
    assert handler.deps().source.on_refresh is handler._audit
    [line] = _refresh_lines(lines)
    assert (line["mode"], line["outcome"], line["version"]) == ("sync", "loaded", 7)
    monkeypatch.setattr(handler, "_DEPS", {})
