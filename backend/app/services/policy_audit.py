"""Policy decision audit sink (D20, plan §2.3(c)) — the CloudWatch mirror of
``policy_decisions``.

The ledger row ``policy_store.record_decision`` writes is the system of record; this
module ships the same decision as one structured JSON line to the CloudWatch Logs group
``/launchpad/policy/decisions[-<env>]`` (stream ``hub-<hostname>-<yyyymmdd>``) so an
operator has a retention store and a cross-check outside the host. Properties:

- **never blocks the request** — ``publish_row`` enqueues onto a bounded queue and a
  daemon thread batches ``put_log_events`` (≤ ``policy_audit_batch_size`` events or
  ``policy_audit_flush_s`` seconds, whichever first);
- **fail-soft** — a full queue drops the event (counted), a failed ``put_log_events``
  is a rate-limited WARN; nothing here ever raises into a decision;
- **no secrets** — the event carries only the fields in :data:`EVENT_FIELDS` (principal
  id/label, resource id, decision, policy ids/versions, latency, request id…); prompts,
  tool arguments, keys and bodies never enter a decision row, so they cannot leak here;
- **funnel only** — the ``logs`` client comes from the decision's workspace context
  (``context_for_workspace(ws).client("logs")``), so a spoke workspace ships into its
  own account's group (plan §4.1).

The bootstrap ``policy`` stage creates the group with ``Settings.log_group_retention_days``
(:func:`ensure_decision_log_group`); ``GET /api/policies/audit.runtime_channel`` reads
:func:`runtime_channel` — ``ready`` once the sink has shipped into the group, ``missing``
when CloudWatch answered that the group does not exist, ``unknown`` before the first ship.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import socket
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import get_settings
from app.core.naming import resource_name

log = logging.getLogger("launchpad.policy.audit")

LOG_GROUP_BASE = "/launchpad/policy/decisions"
EVENT_SCHEMA = "launchpad.policy.decision/1"
STREAM_PREFIX = "hub"
# The one place the record shape lives — pinned by tests/test_policy_audit.py so a new
# field is a deliberate contract change, never an accidental leak.
EVENT_FIELDS: frozenset[str] = frozenset({
    "schema", "decision_id", "request_id", "ts", "env", "workspace_id", "source", "channel",
    "principal", "action", "resource", "agent_id", "session_id", "decision", "mode",
    "policy_ids", "policy_versions", "bundle_version", "reason", "latency_ms",
})
REASON_MAX_CHARS = 2000
_WARN_EVERY_S = 60.0

ClientFactory = Callable[[str], Any]


def log_group_name(env_name: str | None = None) -> str:
    """``/launchpad/policy/decisions`` for the default environment, ``-<env>`` suffixed
    otherwise (``naming.resource_name`` — the same rule every fixed resource follows)."""
    return resource_name(LOG_GROUP_BASE, env_name)


def _default_logs_client(workspace_id: str) -> Any:
    """The decision's workspace ``logs`` client through the funnel. Module attribute on
    purpose: the hermetic suite replaces it (``tests/conftest.py``)."""
    from app.services.workspace import context_for_workspace

    return context_for_workspace(workspace_id).client("logs")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _epoch_ms(value: datetime | None) -> int:
    if value is None:
        return int(time.time() * 1000)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def event_from_row(row: Any, *, env: str | None = None) -> dict[str, Any]:
    """The structured line for one ``PolicyDecision`` row — exactly :data:`EVENT_FIELDS`."""
    reason = row.reason
    if isinstance(reason, str) and len(reason) > REASON_MAX_CHARS:
        reason = reason[:REASON_MAX_CHARS] + "…"
    return {
        "schema": EVENT_SCHEMA,
        "decision_id": row.id,
        "request_id": row.request_id,
        "ts": _iso(row.created_at) or datetime.now(UTC).isoformat(),
        "env": env if env is not None else get_settings().env_name,
        "workspace_id": row.workspace_id,
        "source": row.source,
        "channel": row.channel,
        "principal": {
            "kind": row.principal_kind, "id": row.principal_id, "label": row.principal_label,
        },
        "action": row.action,
        "resource": {"kind": row.resource_kind, "id": row.resource_id},
        "agent_id": row.agent_id,
        "session_id": row.session_id,
        "decision": row.decision,
        "mode": row.mode,
        "policy_ids": list(row.policy_ids or []),
        "policy_versions": dict(row.policy_versions or {}),
        "bundle_version": row.bundle_version,
        "reason": reason,
        "latency_ms": row.latency_ms,
    }


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code") or type(exc).__name__)
    return type(exc).__name__


class CloudWatchDecisionSink:
    """Batches decision events to one CloudWatch Logs group on a daemon thread.

    ``client_factory(workspace_id)`` returns the ``logs`` client for a decision's
    workspace (default: the funnel). ``autostart=False`` keeps the thread off so a test
    drains with :meth:`flush` deterministically.
    """

    def __init__(
        self,
        *,
        log_group: str | None = None,
        env: str | None = None,
        client_factory: ClientFactory | None = None,
        batch_size: int = 100,
        flush_s: float = 1.0,
        max_queue: int = 5000,
        hostname: str | None = None,
        autostart: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.env = env if env is not None else get_settings().env_name
        self.log_group = log_group or log_group_name(self.env)
        self._factory = client_factory
        self.batch_size = max(1, int(batch_size))
        self.flush_s = max(0.01, float(flush_s))
        self.hostname = hostname or socket.gethostname()
        self.autostart = autostart
        self._clock = clock
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max(1, int(max_queue)))
        self._ship_lock = threading.Lock()
        self._thread_lock = threading.Lock()
        self._pending = 0  # queued but not yet attempted (queue + the worker's local batch)
        self._pending_cv = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._streams: set[tuple[str, str]] = set()
        self._status: tuple[str, str] = ("unknown", "no decision shipped yet")
        self._last_warn: dict[str, float] = {}
        self.stats = {"queued": 0, "shipped": 0, "dropped": 0, "failed": 0}

    @classmethod
    def from_settings(cls, **overrides: Any) -> CloudWatchDecisionSink:
        s = get_settings()
        kwargs: dict[str, Any] = {
            "batch_size": s.policy_audit_batch_size,
            "flush_s": s.policy_audit_flush_s,
            "max_queue": s.policy_audit_queue_max,
        }
        kwargs.update(overrides)
        return cls(**kwargs)

    # -- public ---------------------------------------------------------------
    def publish(self, event: dict[str, Any]) -> bool:
        """Enqueue without blocking. ``False`` when the queue is full (event dropped)."""
        with self._pending_cv:
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                self.stats["dropped"] += 1
                self._warn("queue_full", f"policy audit queue full ({self._queue.maxsize}) — "
                                         f"decision {event.get('decision_id')} not mirrored")
                return False
            self._pending += 1
        self.stats["queued"] += 1
        if self.autostart:
            self._ensure_thread()
        return True

    def flush(self, timeout: float | None = None) -> int:
        """Ship everything queued right now from the calling thread, then wait until every
        event the background thread already holds has been attempted (tests, shutdown)."""
        shipped = 0
        while True:
            batch = self._drain(block=False)
            if not batch:
                break
            shipped += self._ship(batch)
        deadline = time.monotonic() + (timeout if timeout is not None else self.flush_s * 2 + 1)
        with self._pending_cv:
            while self._pending > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._pending_cv.wait(remaining):
                    break
        return shipped

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self.flush()

    def status(self) -> dict[str, Any]:
        state, reason = self._status
        return {"status": state, "reason": reason, "log_group": self.log_group, **self.stats}

    def mark_probed(self, logs: Any) -> str:
        """``describe_log_groups`` once (bootstrap / an explicit probe) and record
        ``ready`` / ``missing`` from the answer. Returns the state."""
        state = probe_log_group(logs, self.log_group)
        found = "found" if state == "ready" else "not found"
        self._status = (state, f"log group {self.log_group} {found}")
        return state

    # -- internals ------------------------------------------------------------
    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="policy-audit-sink", daemon=True,
            )
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            batch = self._drain(block=True)
            if batch:
                self._ship(batch)

    def _drain(self, *, block: bool) -> list[dict[str, Any]]:
        batch: list[dict[str, Any]] = []
        try:
            first = self._queue.get(block=block, timeout=0.2 if block else None)
        except queue.Empty:
            return batch
        batch.append(first)
        deadline = time.monotonic() + self.flush_s
        while len(batch) < self.batch_size:
            remaining = deadline - time.monotonic()
            try:
                if block and remaining > 0:
                    batch.append(self._queue.get(timeout=remaining))
                else:
                    batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch

    def _client(self, workspace_id: str) -> Any:
        factory = self._factory or _default_logs_client
        return factory(workspace_id)

    def _stream_name(self) -> str:
        day = datetime.fromtimestamp(self._clock(), UTC).strftime("%Y%m%d")
        return f"{STREAM_PREFIX}-{self.hostname}-{day}"

    def _ensure_stream(self, logs: Any, workspace_id: str, stream: str) -> None:
        key = (workspace_id, stream)
        if key in self._streams:
            return
        try:
            logs.create_log_stream(logGroupName=self.log_group, logStreamName=stream)
        except ClientError as exc:
            if _error_code(exc) != "ResourceAlreadyExistsException":
                raise
        self._streams.add(key)

    def _ship(self, batch: list[dict[str, Any]]) -> int:
        shipped = 0
        by_workspace: dict[str, list[dict[str, Any]]] = {}
        for event in batch:
            by_workspace.setdefault(str(event.get("workspace_id") or ""), []).append(event)
        try:
            with self._ship_lock:
                for workspace_id, events in by_workspace.items():
                    shipped += self._ship_workspace(workspace_id, events)
        finally:
            with self._pending_cv:
                self._pending -= len(batch)
                self._pending_cv.notify_all()
        return shipped

    def _ship_workspace(self, workspace_id: str, events: list[dict[str, Any]]) -> int:
        try:
            logs = self._client(workspace_id)
            stream = self._stream_name()
            self._ensure_stream(logs, workspace_id, stream)
            log_events = sorted(
                (
                    {
                        "timestamp": _ts_ms(event),
                        "message": json.dumps(
                            event, separators=(",", ":"), ensure_ascii=False, default=str,
                        ),
                    }
                    for event in events
                ),
                key=lambda e: e["timestamp"],
            )
            logs.put_log_events(
                logGroupName=self.log_group, logStreamName=stream, logEvents=log_events,
            )
        except (ClientError, BotoCoreError) as exc:
            code = _error_code(exc)
            if code == "ResourceNotFoundException":
                self._status = (
                    "missing",
                    f"log group {self.log_group} not found — run `make bootstrap` "
                    "(policy stage creates it)",
                )
            else:
                self._status = ("unknown", f"{code}: {exc}"[:300])
            self.stats["failed"] += len(events)
            self._warn(code, f"policy audit: {len(events)} decision(s) not mirrored to "
                             f"{self.log_group} ({code}: {exc})")
            return 0
        except Exception as exc:  # noqa: BLE001 — fail-soft by contract
            self._status = ("unknown", f"{type(exc).__name__}: {exc}"[:300])
            self.stats["failed"] += len(events)
            self._warn(type(exc).__name__, f"policy audit: unexpected {type(exc).__name__} "
                                           f"shipping {len(events)} decision(s): {exc}")
            return 0
        self._status = ("ready", f"shipping to {self.log_group}")
        self.stats["shipped"] += len(events)
        return len(events)

    def _warn(self, key: str, message: str) -> None:
        now = self._clock()
        last = self._last_warn.get(key, 0.0)
        if now - last >= _WARN_EVERY_S:
            self._last_warn[key] = now
            log.warning(message)
        else:
            log.debug(message)


def _ts_ms(event: dict[str, Any]) -> int:
    ts = event.get("ts")
    if isinstance(ts, str):
        try:
            return _epoch_ms(datetime.fromisoformat(ts.replace("Z", "+00:00")))
        except ValueError:
            pass
    return int(time.time() * 1000)


# ── module slot ────────────────────────────────────────────────────────────

_sink: CloudWatchDecisionSink | None = None


def install(sink: CloudWatchDecisionSink | None) -> None:
    """Register the process sink (``app/main.py`` with the ``policy`` feature); ``None``
    stops and removes the current one."""
    global _sink
    previous = _sink
    _sink = sink
    if previous is not None and previous is not sink:
        previous.stop(timeout=0.5)


def current() -> CloudWatchDecisionSink | None:
    return _sink


def publish_row(row: Any) -> bool:
    """Mirror one ``PolicyDecision`` row. Called by ``policy_store.record_decision`` after
    the row exists; never raises (the ledger row is already the record)."""
    sink = _sink
    if sink is None:
        return False
    try:
        return sink.publish(event_from_row(row, env=sink.env))
    except Exception:  # noqa: BLE001 — a mirror must never break a decision
        log.exception("policy audit: could not build the event for decision %s",
                      getattr(row, "id", "?"))
        return False


def runtime_channel() -> dict[str, Any]:
    """``GET /api/policies/audit.runtime_channel``: the CloudWatch decisions channel —
    ``ready`` | ``missing`` | ``unknown`` with a reason and the group name."""
    sink = _sink
    if sink is None:
        return {
            "status": "missing",
            "reason": (
                "audit sink not installed (policy feature off or policy_audit_enabled=false)"
            ),
            "log_group": log_group_name(),
        }
    state = sink.status()
    return {"status": state["status"], "reason": state["reason"], "log_group": sink.log_group}


# ── bootstrap helpers ──────────────────────────────────────────────────────


def probe_log_group(logs: Any, name: str) -> str:
    """``ready`` when the group exists, ``missing`` otherwise (raises on an API error)."""
    page = logs.describe_log_groups(logGroupNamePrefix=name, limit=50)
    for group in page.get("logGroups", []) or []:
        if group.get("logGroupName") == name:
            return "ready"
    return "missing"


def ensure_decision_log_group(
    logs: Any,
    *,
    retention_days: int,
    log: Callable[[str], None] = lambda _m: None,
    name: str | None = None,
) -> dict[str, Any]:
    """Create ``/launchpad/policy/decisions[-env]`` if missing and set its retention
    (``0`` leaves it unbounded). Idempotent; raises the botocore error for the stage to
    degrade on. Returns ``{log_group, created, retention_days}``."""
    name = name or log_group_name()
    try:
        logs.create_log_group(logGroupName=name)
        created = True
    except ClientError as exc:
        if _error_code(exc) != "ResourceAlreadyExistsException":
            raise
        created = False
    days = int(retention_days or 0)
    if days > 0:
        logs.put_retention_policy(logGroupName=name, retentionInDays=days)
    retention = f"retention {days} d" if days > 0 else "retention unbounded"
    log(f"policy decisions log group {name} {'created' if created else 'exists'} · {retention}")
    sink = _sink
    if sink is not None and sink.log_group == name:
        sink._status = ("ready", f"log group {name} ensured by bootstrap")
    return {"log_group": name, "created": created, "retention_days": days}


# ── gateway decision ingest (per-user policy stream B5; contract I7) ────────

GATEWAY_LINE_KEY = "launchpad.policy.decision"
INGEST_INTERVAL_S = 20.0  # §14.3 acceptance: rows within 60 s
INGEST_OVERLAP_MS = 5 * 60 * 1000  # re-read window: late-delivered events; ids dedupe
_DETERMINING_RE = re.compile(r"^(?P<id>.+):v(?P<version>\d+)$")


def gateway_log_group(env_name: str | None = None) -> str:
    """The interceptor Lambda's own group (Lambda delivers its stdout there)."""
    from app.services.policy_interceptor import FUNCTION_BASE

    return "/aws/lambda/" + resource_name(FUNCTION_BASE, env_name)


def row_from_gateway_line(line: dict[str, Any]) -> Any | None:
    """One ``PolicyDecision`` from an I7 interceptor line, or ``None`` when it is not one.
    The id IS the line's ``decision_id`` (a gateway retry carries the same id), so a
    re-read is a no-op; an unknown caller (``ws: unknown``) lands in the default workspace."""
    from app.core.db import DEFAULT_WORKSPACE_ID
    from app.models.policy import PolicyDecision

    body = line.get(GATEWAY_LINE_KEY) if isinstance(line, dict) else None
    if not isinstance(body, dict) or body.get("source") != "gateway":
        return None
    decision_id = str(body.get("decision_id") or "")
    if len(decision_id) != 32:
        return None
    principal = body.get("principal") if isinstance(body.get("principal"), dict) else {}
    versions: dict[str, int] = {}
    for item in body.get("determining") or []:
        match = _DETERMINING_RE.match(str(item))
        if match:
            versions[match["id"]] = int(match["version"])
    try:
        created = datetime.fromisoformat(str(body.get("ts") or "").replace("Z", "+00:00"))
    except ValueError:
        created = datetime.now(UTC)
    ws = str(body.get("ws") or "")
    reason = body.get("reason")
    if isinstance(reason, str) and len(reason) > REASON_MAX_CHARS:
        reason = reason[:REASON_MAX_CHARS] + "…"
    return PolicyDecision(
        id=decision_id, workspace_id=DEFAULT_WORKSPACE_ID if ws in ("", "unknown") else ws,
        request_id=None, source="gateway", channel="gateway",
        principal_kind=str(principal.get("kind") or "unknown")[:16],
        principal_id=str(principal.get("id") or "unknown")[:64], principal_label=None,
        action="call_tool", resource_kind="tool", resource_id=str(body.get("action") or "")[:160],
        agent_id=body.get("agent_id"), session_id=None,
        decision=str(body.get("decision") or "error")[:16], mode=str(body.get("mode") or "")[:16],
        policy_ids=[str(p) for p in body.get("policy_ids") or []], policy_versions=versions,
        bundle_version=None, reason=reason, latency_ms=body.get("latency_ms"),
        stage=str(body.get("stage") or "final")[:8], session_psid=body.get("session_psid"),
        event_seq=body.get("event_seq") if isinstance(body.get("event_seq"), int) else None,
        store_epoch=body.get("store_epoch"),
        input_keys=[str(k) for k in body.get("input_keys") or []],
        # Global model: the deciding gateway and its attached engine (the I7 line carries both
        # since bundle /3; older lines leave them NULL — the migration back-fills the gateway)
        gateway_id=(str(body["gateway_id"])[:128] if body.get("gateway_id") else None),
        engine_id=(str(body["engine_id"])[:32] if body.get("engine_id") else None),
        created_at=created,
    )


def ingest_gateway_events(db: Any, events: list[dict[str, Any]], *,
                          now: datetime | None = None) -> int:
    """Insert the decision lines among FilterLogEvents ``events`` that are not in the ledger
    yet; returns how many were new. Lines that are not I7 (START/END/REPORT, diagnostics) are
    skipped. Every new row is stamped ``ingested_at = now`` (UTC, the hub's clock — N17-obs482):
    ``created_at`` stays the interceptor's own ``ts``, so the ingest lag is the difference. The
    line never supplies the stamp (``row_from_gateway_line`` reads no such key)."""
    from app.models.policy import PolicyDecision

    stamp = now or datetime.now(UTC)
    rows = {}
    for event in events:
        message = str(event.get("message") or "").strip()
        if GATEWAY_LINE_KEY not in message:
            continue
        try:
            row = row_from_gateway_line(json.loads(message))
        except ValueError:
            continue
        if row is not None:
            rows[row.id] = row
    if not rows:
        return 0
    existing = {r for (r,) in db.query(PolicyDecision.id).filter(
        PolicyDecision.id.in_(list(rows))).all()}
    fresh = [row for key, row in rows.items() if key not in existing]
    for row in fresh:
        row.ingested_at = stamp
    db.add_all(fresh)
    db.commit()
    # N17-obs492 (re-review F3): the Lambda's `temporal_set_mismatch` denials mean a shard is
    # not on the bundle's set — the same signal the hub's own decide path reacts to; one
    # background reconcile per policy engine per minute, best effort
    for engine_id in sorted({row.engine_id for row in fresh
                             if row.source == "gateway" and row.engine_id
                             and row.reason == "temporal_set_mismatch"}):
        try:
            from app.services import policy_engine_decide

            policy_engine_decide.schedule_reconcile(engine_id)
        except Exception:  # noqa: BLE001 — the rows are already stored
            log.exception("policy: reconcile after temporal_set_mismatch rows failed (%s)",
                           engine_id)
    return len(fresh)


class GatewayDecisionIngester:
    """Polls the interceptor's log group with FilterLogEvents and writes ``policy_decisions``
    rows (``source=gateway``). Fail-soft like the sink: an AWS error is a rate-limited WARN
    and the next poll re-reads from the last cursor minus the overlap window."""

    def __init__(self, logs: Any, session_factory: Callable[[], Any], *,
                 log_group: str | None = None, interval_s: float = INGEST_INTERVAL_S,
                 clock: Callable[[], float] = time.time) -> None:
        self.logs = logs
        self.session_factory = session_factory
        self.log_group = log_group or gateway_log_group()
        self.interval_s = interval_s
        self.clock = clock
        self.cursor_ms = int(clock() * 1000) - INGEST_OVERLAP_MS
        self.ingested = 0
        self.last_error: str | None = None
        self._last_warn = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def poll_once(self) -> int:
        start = max(0, self.cursor_ms - INGEST_OVERLAP_MS)
        newest = self.cursor_ms
        events: list[dict[str, Any]] = []
        token = None
        try:
            while True:
                kwargs = {"logGroupName": self.log_group, "startTime": start,
                          "filterPattern": f'"{GATEWAY_LINE_KEY}"'}
                if token:
                    kwargs["nextToken"] = token
                page = self.logs.filter_log_events(**kwargs)
                events.extend(page.get("events") or [])
                token = page.get("nextToken")
                if not token:
                    break
        except (ClientError, BotoCoreError) as exc:
            code = _error_code(exc)
            self.last_error = code
            if code != "ResourceNotFoundException" and \
                    self.clock() - self._last_warn >= _WARN_EVERY_S:
                self._last_warn = self.clock()
                log.warning("policy gateway ingest: FilterLogEvents on %s failed (%s)",
                            self.log_group, code)
            return 0
        for event in events:
            newest = max(newest, int(event.get("timestamp") or 0))
        db = self.session_factory()
        try:
            added = ingest_gateway_events(db, events)
        finally:
            db.close()
        self.cursor_ms = newest
        self.ingested += added
        self.last_error = None
        return added

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 — a poller must never die on one bad batch
                log.exception("policy gateway ingest: poll failed")

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="policy-gateway-ingest")
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None


_ingester: GatewayDecisionIngester | None = None


def start_gateway_ingest(settings: Any) -> GatewayDecisionIngester | None:
    """``app/main.py``: only with ``Settings.policy_gateway_interceptor`` (kill switch) on."""
    global _ingester
    if not getattr(settings, "policy_gateway_interceptor", False):
        return None
    if _ingester is None:
        from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal

        # the governed gateway and its interceptor are the hub's (default workspace's)
        _ingester = GatewayDecisionIngester(_default_logs_client(DEFAULT_WORKSPACE_ID),
                                            SessionLocal)
    _ingester.start()
    return _ingester


def stop_gateway_ingest() -> None:
    global _ingester
    if _ingester is not None:
        _ingester.stop()
    _ingester = None
