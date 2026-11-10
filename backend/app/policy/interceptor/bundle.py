"""The interceptor's view of the I3 gateway bundle and the engine HMAC key (stream B1;
contract docs/cn/interfaces/policy-principal-contracts.md §I3).

Layout in the artifacts bucket (contract amendment 3; written by the hub's bundle writer, B3):

- ``policy/gateway/<gateway_id>/v<n>.json`` — the immutable bundle;
- ``policy/gateway/<gateway_id>/current.json`` — the mutable pointer
  (``gateway_bundle_pointer.schema.json``: ``{schema, gateway_id, version, key, issued_at}``),
  so a publish swaps one small object and no Lambda update is needed;
- ``policy/gateway/<gateway_id>/engine-key`` (= ``engine.key_s3_key``) — the engine HMAC key,
  exactly 32 raw bytes, the same object the engine host installs.

``BundleSource`` caches the parsed bundle for ``ttl_s`` (default 30 s) and keeps the LAST
GOOD one when a refresh fails or yields an invalid bundle, so an S3 hiccup never turns into
a policy outage; only a cold Lambda that never loaded one has no bundle.

SELF-CONTAINED (stdlib + the vendored ``codec``): shipped in the interceptor Lambda zip.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import codec

# Global model (policy-engines contract, 2026-10-28; ruling 25): /3 carries ONE `attachment`
# per gateway ({engine_id, mode LOG_ONLY|ENFORCE, counts, actions} or null) and the Lambda
# STILL reads the /1 and /2 `workspaces` shapes (per-workspace blocks keyed by the caller's
# workspace, `published` required on /2) so the Lambda can be updated BEFORE the hub rewrites
# the live bundles — zero fail-closed window either way.
SCHEMA_V1 = "launchpad.policy.gateway/1"
SCHEMA_V2 = "launchpad.policy.gateway/2"
SCHEMA = "launchpad.policy.gateway/3"
SCHEMAS = (SCHEMA_V1, SCHEMA_V2, SCHEMA)
POINTER_SCHEMA = "launchpad.policy.gateway-pointer/1"
POINTER_NAME = "current.json"
DEFAULT_TTL_S = 30.0
# N17-obs483: the hard bound on serving a cached bundle while background refreshes fail — past
# it a request refreshes synchronously and, if that fails too, fails closed (bundle_stale)
DEFAULT_MAX_STALE_S = 120.0
HMAC_KEY_BYTES = 32
_ENDPOINT_RE = re.compile(r"[0-9.]+:[0-9]{2,5}\Z")
ATTACHMENT_MODES = {"LOG_ONLY": "log_only", "ENFORCE": "enforce"}  # bundle spelling → internal
MODES = ("log_only", "enforce")


class BundleError(ValueError):
    """The bundle (or its key) cannot be used; ``code`` goes into the audit reason."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def gateway_prefix(gateway_id: str) -> str:
    return f"policy/gateway/{gateway_id}/"


def pointer_key(gateway_id: str) -> str:
    return gateway_prefix(gateway_id) + POINTER_NAME


def bundle_key(gateway_id: str, version: int) -> str:
    return f"{gateway_prefix(gateway_id)}v{int(version)}.json"


def engine_key_key(gateway_id: str) -> str:
    return gateway_prefix(gateway_id) + "engine-key"


def pointer(gateway_id: str, version: int, issued_at: str) -> dict[str, Any]:
    """The ``current.json`` body (``gateway_bundle_pointer.schema.json``)."""
    return {"schema": POINTER_SCHEMA, "gateway_id": gateway_id, "version": int(version),
            "key": bundle_key(gateway_id, version), "issued_at": issued_at}


@dataclass(frozen=True)
class Attachment:
    """What decides a call on this gateway: the engine's set (``engine_id`` = the Dogwood set key
    — on a /2 bundle the workspace id), the gateway mode (``log_only`` records / ``enforce``
    denies — internal spelling of /3's ``LOG_ONLY`` / ``ENFORCE``) and the installed set's
    facts. ``published`` is a /2 fact (False = the engine does not hold the hub's current set:
    fail closed ``engine_unpublished``); /3 has no such flag (a set the engine does not hold
    answers ``set_mismatch``). A /2 block that is switched off / mode ``off`` is not built
    (``block_for`` answers None — pass, as before)."""

    engine_id: str
    mode: str
    active_count: int
    temporal_count: int
    policy_set_version: str
    temporal_set_version: int
    governed_actions: frozenset[str]
    temporal_referenced_actions: frozenset[str]
    published: bool = True


@dataclass(frozen=True)
class Caller:
    role: str
    agent_id: str
    ws: str
    method: str
    system: bool
    byoc: bool
    # user ruling (12) / GLOBAL-DEVIATIONS §0b-12: the caller is exempt from policy — passed
    # with a `system_exempt` row and no engine call. Additive bundle field `role_map[*].exempt`;
    # absent ⇒ `system` (the hub writes it from `Agent.system_key`).
    exempt: bool = False


@dataclass
class Bundle:
    raw: dict[str, Any]
    gateway_id: str
    gateway_arn: str
    version: int
    env: str
    empty_set_decision: str
    hub_role: str
    role_map: dict[str, Caller]
    attachment: Attachment | None  # /3: the gateway's one attachment
    workspaces: dict[str, Attachment]  # /1 + /2: one block per (enabled) workspace
    schema: str
    endpoints: list[str]
    shards: int
    key_s3_key: str
    _pubkeys: dict[str, str]
    _loaded_keys: dict[str, Any] = field(default_factory=dict)

    def pubkeys(self) -> dict[str, Any]:
        """``kid`` → loaded P-256 public key (parsed once per bundle)."""
        if len(self._loaded_keys) != len(self._pubkeys):
            for kid, spki in self._pubkeys.items():
                if kid not in self._loaded_keys:
                    self._loaded_keys[kid] = codec.load_public_key(spki)
        return self._loaded_keys

    @property
    def account(self) -> str:
        """The gateway's account (``arn:<partition>:bedrock-agentcore:<region>:<account>:…``)
        — every caller role must be in it."""
        parts = self.gateway_arn.split(":")
        return parts[4] if len(parts) > 5 else ""

    @property
    def blocks(self) -> list[Attachment]:
        """Every block that decides something on this gateway (/3: the attachment; /2: the
        enabled workspace blocks)."""
        if self.attachment is not None:
            return [self.attachment]
        return [w for w in self.workspaces.values() if w.mode != "off"]

    def block_for(self, ws: str) -> Attachment | None:
        """The block a KNOWN caller of workspace ``ws`` is decided by: /3 — the gateway's
        attachment (whatever the caller's workspace); /2 — the workspace's block, or an
        enforcing unpublished stand-in when the bundle does not describe the workspace
        (review F1, N17-obs490: a bundle older than the workspace fails closed)."""
        if self.schema == SCHEMA:
            return self.attachment
        block = self.workspaces.get(ws)
        if block is not None:
            return None if block.mode == "off" else block
        return _unpublished_block(ws)

    def any_enforcing(self, action: str) -> tuple[bool, bool]:
        """(some block enforces ``action``, some block logs it) — what an UNKNOWN caller (not in
        ``role_map``) is judged by."""
        enforce = log_only = False
        for w in self.blocks:
            if self.schema == SCHEMA or (w.active_count and action in w.governed_actions):
                enforce |= w.mode == "enforce"
                log_only |= w.mode == "log_only"
        return enforce, log_only


def _unpublished_block(ws: str) -> Attachment:
    """A /2 workspace the bundle does not describe: enabled, enforcing, unpublished."""
    return Attachment(engine_id=ws, mode="enforce", active_count=0, temporal_count=0,
                      policy_set_version="unpublished", temporal_set_version=0,
                      governed_actions=frozenset(), temporal_referenced_actions=frozenset(),
                      published=False)


ACTION_ENTITY_PREFIX = 'AgentCore::Action::"'


def _actions(entry: dict[str, Any], ws: Any, key: str) -> frozenset[str]:  # ws = block name
    """The block's action ids — entity-formatted (``AgentCore::Action::"<tool>"``, what
    ``core.action_entity`` compares) or the bundle is invalid: a bare tool name there governs
    nothing, so a bundle carrying one would pass every call (N17-obs488 FAIL-OPEN, contract
    amendment 6). The hub builds every bundle through this parser, so compose fails closed too."""
    items = entry[key]
    if not isinstance(items, list):
        raise BundleError("bundle_invalid", f"{ws}.{key}")
    out = set()
    for item in items:
        text = str(item)
        if not (text.startswith(ACTION_ENTITY_PREFIX) and text.endswith('"')
                and len(text) > len(ACTION_ENTITY_PREFIX) + 1):
            raise BundleError("bundle_invalid",
                              f"{ws}.{key}: action ids must be entity-formatted")
        out.add(text)
    return frozenset(out)


def _block(entry: dict[str, Any], *, name: str, engine_id: str, mode: str,
           published: bool) -> Attachment:
    return Attachment(
        engine_id=engine_id, mode=mode, published=published,
        active_count=_count(entry, "active_count"),
        temporal_count=_count(entry, "temporal_count"),
        policy_set_version=_str(entry, "policy_set_version"),
        temporal_set_version=_count(entry, "temporal_set_version"),
        governed_actions=_actions(entry, name, "governed_actions"),
        temporal_referenced_actions=_actions(entry, name, "temporal_referenced_actions"),
    )


def parse_bundle(raw: Any, *, expect_gateway_id: str | None = None) -> Bundle:
    """Strict structural check of I3 (the hub validates against the JSON Schema before it
    writes; this re-checks what the Lambda relies on, without a jsonschema dependency). /3 and
    /2 (and the M1 /1) are read — ruling 25."""
    if not isinstance(raw, dict) or raw.get("schema") not in SCHEMAS:
        raise BundleError("bundle_invalid", "schema")
    schema = str(raw["schema"])
    try:
        gateway_id = _str(raw, "gateway_id")
        if expect_gateway_id and gateway_id != expect_gateway_id:
            raise BundleError("bundle_invalid", "gateway_id")
        version = raw["version"]
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise BundleError("bundle_invalid", "version")
        empty = raw["empty_set_decision"]
        if empty not in (("deny",) if schema == SCHEMA else ("allow", "deny")):
            raise BundleError("bundle_invalid", "empty_set_decision")
        pubkeys = raw["pubkeys"]
        if not isinstance(pubkeys, dict) or not pubkeys or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in pubkeys.items()
        ):
            raise BundleError("bundle_invalid", "pubkeys")
        role_map: dict[str, Caller] = {}
        for role, entry in (raw["role_map"] or {}).items():
            # /3: `exempt` is REQUIRED (ruling 10); /1 and /2: absent = the value of `system`
            exempt = entry["exempt"] if schema == SCHEMA else entry.get("exempt", entry["system"])
            if not isinstance(exempt, bool):
                raise BundleError("bundle_invalid", f"role_map.{role}.exempt")
            role_map[str(role)] = Caller(
                role=str(role), agent_id=_str(entry, "agent_id"), ws=_str(entry, "ws"),
                method=str(entry.get("method") or ""), system=entry["system"] is True,
                byoc=entry["byoc"] is True, exempt=exempt,
            )
        attachment: Attachment | None = None
        workspaces: dict[str, Attachment] = {}
        if schema == SCHEMA:
            entry = raw["attachment"]
            if entry is not None:
                if not isinstance(entry, dict):
                    raise BundleError("bundle_invalid", "attachment")
                mode = ATTACHMENT_MODES.get(entry["mode"])
                if mode is None:
                    raise BundleError("bundle_invalid", "attachment.mode")
                attachment = _block(entry, name="attachment", engine_id=_str(entry, "engine_id"),
                                    mode=mode, published=True)
        else:
            for ws, entry in (raw["workspaces"] or {}).items():
                mode = entry["mode"]
                if mode not in ("off", "log_only", "enforce"):
                    raise BundleError("bundle_invalid", f"workspaces.{ws}.mode")
                if schema == SCHEMA_V2 and "published" not in entry:
                    raise BundleError("bundle_invalid", f"workspaces.{ws}.published")
                published = entry.get("published", True)
                if not isinstance(published, bool):
                    raise BundleError("bundle_invalid", f"workspaces.{ws}.published")
                if entry["gateway_interceptor"] is not True:
                    mode = "off"
                block = _block(entry, name=f"workspaces.{ws}", engine_id=str(ws), mode=mode,
                               published=published)
                workspaces[str(ws)] = block
        engine = raw["engine"]
        endpoints = [str(e) for e in engine["endpoints"]]
        if not endpoints or not all(_ENDPOINT_RE.match(e) for e in endpoints):
            raise BundleError("bundle_invalid", "engine.endpoints")
        shards = engine["shards"]
        if not isinstance(shards, int) or isinstance(shards, bool) or not 1 <= shards <= 16:
            raise BundleError("bundle_invalid", "engine.shards")
        return Bundle(
            raw=raw, gateway_id=gateway_id, gateway_arn=_str(raw, "gateway_arn"),
            version=version, env=str(raw["env"]),
            empty_set_decision=empty, hub_role=str(raw["hub_role"]), role_map=role_map,
            attachment=attachment, workspaces=workspaces, schema=schema, endpoints=endpoints,
            shards=shards, key_s3_key=_str(engine, "key_s3_key"), _pubkeys=dict(pubkeys),
        )
    except BundleError:
        raise
    except (KeyError, TypeError, AttributeError) as exc:
        raise BundleError("bundle_invalid", type(exc).__name__) from None


def _str(entry: Any, name: str) -> str:
    value = entry[name]
    if not isinstance(value, str) or not value:
        raise BundleError("bundle_invalid", name)
    return value


def _count(entry: Any, name: str) -> int:
    value = entry[name]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise BundleError("bundle_invalid", name)
    return value


# (bucket, key) -> object body bytes; raises on any failure
Fetch = Callable[[str, str], bytes]


Spawn = Callable[[Callable[[], None]], None]


def _spawn_thread(fn: Callable[[], None]) -> threading.Thread:
    thread = threading.Thread(target=fn, name="bundle-refresh", daemon=True)
    thread.start()
    return thread


# the RESPONSE invocation finishes a pending refresh within this budget (N17-obs498); a Lambda
# has 5 s (`policy_interceptor.TIMEOUT_S`) and the engine `respond` call still follows
SETTLE_BUDGET_S = 2.0

# the `bundle_refresh` diagnostic line (obs498 follow-up): which path ran the refresh and how it
# ended — fixed vocabulary, numbers and the bundle version only (never a key or a body)
REFRESH_MODES = ("sync", "background", "settle")
REFRESH_OUTCOMES = ("loaded", "unchanged", "behind", "failed")


@dataclass
class BundleSource:
    """Pointer → bundle → HMAC key with stale-while-revalidate and BOUNDED staleness
    (N17-obs483 — the synchronous 30 s refresh sat in the request path and set the p99).

    - fresh (confirmed less than ``ttl_s`` ago): served, nothing else happens;
    - older than ``ttl_s``: served from the cache while ONE background refresh runs
      (``spawn``). N17-obs498: a Lambda FREEZES the container the moment the handler returns,
      so a thread started at the end of a REQUEST makes no progress between invocations and
      a 2 ms invocation cannot finish an S3 round trip — under spaced traffic the background
      refresh never completed, the age reached ``max_stale_s`` and the REQUEST path paid the
      synchronous fetch (the 77 / 117 ms outliers of M2 9c). ``settle`` therefore finishes
      the pending refresh during the RESPONSE invocation that follows every REQUEST (off the
      decision path, within ``SETTLE_BUDGET_S``), and an in-flight attempt older than
      ``ttl_s`` is treated as frozen so a fresh one may start;
    - older than ``max_stale_s`` (refreshes kept failing): the request refreshes
      synchronously; if the bundle is still that old afterwards it fails closed
      (``bundle_stale``);
    - ``refresh(force=True)`` (after an engine set mismatch) is always synchronous;
    - no bundle yet (cold start): synchronous.
    A failed refresh keeps the last good pair. A policy change is therefore served within
    ``ttl_s`` + one refresh normally and ``max_stale_s`` at worst."""

    bucket: str
    gateway_id: str
    fetch: Fetch
    ttl_s: float = DEFAULT_TTL_S
    max_stale_s: float = DEFAULT_MAX_STALE_S
    clock: Callable[[], float] = time.monotonic
    spawn: Spawn = _spawn_thread
    _bundle: Bundle | None = None
    _key: bytes | None = None
    _confirmed_at: float = float("-inf")  # last refresh that reached S3 and kept/loaded a pair
    _attempted_at: float = float("-inf")  # last background attempt (one per ttl, even failing)
    _in_flight: bool = False
    _in_flight_since: float = float("-inf")
    _thread: Any = None  # the background refresh thread when `spawn` returned one
    _lock: threading.Lock = field(default_factory=threading.Lock)
    last_error: str | None = None
    settled: int = 0  # refreshes completed by `settle` (observability / tests)
    # the diagnostic sink for `bundle_refresh` lines (the Lambda's audit stdout); never raises
    on_refresh: Callable[[dict[str, Any]], None] | None = None

    def _start_background(self, now: float, mode: str = "background") -> None:
        """Mark one refresh in flight and hand it to ``spawn`` (a thread in the Lambda)."""
        with self._lock:
            self._in_flight, self._in_flight_since, self._attempted_at = True, now, now
        thread = self.spawn(lambda: self._background_refresh(mode))
        if isinstance(thread, threading.Thread):
            with self._lock:
                self._thread = thread

    def _emit(self, mode: str, outcome: str, started: float, version: int | None) -> None:
        if self.on_refresh is None:
            return
        try:
            self.on_refresh({"launchpad.policy.interceptor": {
                "event": "bundle_refresh", "mode": mode, "outcome": outcome,
                "ms": round(max(0.0, (time.perf_counter() - started) * 1000), 3),
                "version": version, "error": self.last_error if outcome == "failed" else None}})
        except Exception:  # noqa: BLE001 — a diagnostic must never break a refresh
            pass

    def _due(self, now: float, age: float) -> bool:
        """A background refresh may start: past the TTL, one per TTL, and none in flight —
        an attempt in flight for longer than a TTL counts as frozen (N17-obs498)."""
        frozen = self._in_flight and now - self._in_flight_since >= self.ttl_s
        return (self._bundle is not None and age >= self.ttl_s
                and (not self._in_flight or frozen) and now - self._attempted_at >= self.ttl_s)

    def settle(self, budget_s: float = SETTLE_BUDGET_S) -> bool:
        """Finish the pending refresh NOW, off the decision path (the RESPONSE invocation,
        N17-obs498): join the in-flight thread for up to ``budget_s``; when nothing is in
        flight and the bundle is past its TTL, run one refresh — on a thread, so a stalled S3
        read never holds this invocation past the budget either (the thread resumes in a
        later invocation and completes harmlessly). Never raises. Returns whether the bundle
        is fresh (younger than ``ttl_s``) afterwards."""
        now = self.clock()
        with self._lock:
            thread, in_flight = self._thread, self._in_flight
            age = now - self._confirmed_at
            start = self._bundle is not None and age >= self.ttl_s and not in_flight
        if start:
            self._start_background(now, mode="settle")
            with self._lock:
                thread = self._thread
        if isinstance(thread, threading.Thread) and thread.is_alive():
            thread.join(max(0.0, budget_s))
        with self._lock:
            fresh = self._bundle is not None and self.clock() - self._confirmed_at < self.ttl_s
            if fresh and (start or in_flight):
                self.settled += 1
            return fresh

    def get(self) -> tuple[Bundle, bytes]:
        now = self.clock()
        with self._lock:
            bundle, key, age = self._bundle, self._key, now - self._confirmed_at
            due = self._due(now, age)
        if bundle is None or key is None or age >= self.max_stale_s:
            self.refresh(mode="sync")
            with self._lock:
                bundle, key = self._bundle, self._key
                age = self.clock() - self._confirmed_at
            if bundle is None or key is None:
                raise BundleError("bundle_unavailable", self.last_error or "never loaded")
            if age >= self.max_stale_s:
                raise BundleError("bundle_stale", self.last_error or "refresh failed")
            return bundle, key
        if due:
            self._start_background(now)
        return bundle, key

    def _background_refresh(self, mode: str = "background") -> None:
        try:
            self.refresh(mode=mode)
        finally:
            with self._lock:
                self._in_flight = False
                if self._thread is threading.current_thread():
                    self._thread = None

    def refresh(self, *, force: bool = False, mode: str = "sync") -> None:
        """Synchronous: read the pointer (and, when it moved forward or ``force``, the bundle
        and key), swap atomically, record the outcome. Never raises. ``mode`` names the path
        that ran it for the ``bundle_refresh`` diagnostic line (obs498 follow-up)."""
        started = time.perf_counter()
        outcome, version_out = "failed", None
        try:
            current = json.loads(self.fetch(self.bucket, pointer_key(self.gateway_id)))
            if not isinstance(current, dict) or current.get("schema") != POINTER_SCHEMA or \
                    current.get("gateway_id") != self.gateway_id:
                raise BundleError("bundle_invalid", "pointer")
            version, key = current.get("version"), current.get("key")
            if not isinstance(version, int) or isinstance(version, bool) or \
                    key != bundle_key(self.gateway_id, version):
                raise BundleError("bundle_invalid", "pointer")
            with self._lock:
                loaded = self._bundle
            if not force and loaded is not None and version <= loaded.version:
                # same version: nothing new; LOWER: a pointer moved backwards (a lost race,
                # a restore) is never followed unless forced (after an engine set mismatch)
                with self._lock:
                    self._confirmed_at = self.clock()
                    self.last_error = None if version == loaded.version else \
                        "pointer_behind_loaded"
                outcome = "unchanged" if version == loaded.version else "behind"
                version_out = loaded.version
                return
            with self._lock:
                hmac_key, old = self._key, self._bundle
            # N17-obs498: on a cold load (or a forced one) the key is needed too — fetch it in
            # parallel with the bundle (one S3 round trip instead of two on the first request)
            prefetch: list[Any] = []
            key_thread = None
            if hmac_key is None or old is None or force:
                expected_key = engine_key_key(self.gateway_id)

                def fetch_key() -> None:
                    try:
                        prefetch.append(self.fetch(self.bucket, expected_key))
                    except Exception as exc:  # noqa: BLE001 — surfaced below if still needed
                        prefetch.append(exc)

                key_thread = threading.Thread(target=fetch_key, name="bundle-key", daemon=True)
                key_thread.start()
            bundle = parse_bundle(json.loads(self.fetch(self.bucket, key)),
                                  expect_gateway_id=self.gateway_id)
            if bundle.version != version:
                raise BundleError("bundle_invalid", "version differs from the pointer")
            if hmac_key is None or old is None or old.key_s3_key != bundle.key_s3_key or force:
                fetched: Any = None
                if key_thread is not None:
                    key_thread.join()
                    fetched = prefetch[0] if prefetch else None
                if (isinstance(fetched, bytes)
                        and bundle.key_s3_key == engine_key_key(self.gateway_id)):
                    hmac_key = fetched
                else:
                    hmac_key = self.fetch(self.bucket, bundle.key_s3_key)
                if len(hmac_key) != HMAC_KEY_BYTES:
                    raise BundleError("engine_key_invalid", "not 32 bytes")
            with self._lock:
                self._bundle, self._key = bundle, hmac_key
                self._confirmed_at = self.clock()
                self.last_error = None
            outcome, version_out = "loaded", bundle.version
        except BundleError as exc:
            self.last_error = exc.code
        except Exception as exc:  # S3 / JSON failure: keep the last good bundle
            self.last_error = f"bundle_fetch_failed ({type(exc).__name__})"
        finally:
            self._emit(mode, outcome, started, version_out)
