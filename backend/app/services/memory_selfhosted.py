"""Self-hosted Memory backend (N6): S3 snapshots · OpenSearch k-NN · SQS extraction.

AgentCore Memory does not exist in `cn-northwest-1` (spec C5), so this module
implements the frozen `MemoryBackend` Protocol (`app/services/memory_backend.py`) on
top of plain AWS primitives: short-term session snapshots live in S3 (the exact
layout `docs/cn/interfaces/memory-runtime.md` §3 pins — the runtime library writes
there, this backend only reads/writes the same keys), long-term records live in the
shared `launchpad-memory` OpenSearch index (`app/services/opensearch_http.py`,
k-NN over Titan v2 embeddings), and extraction is handed off to SQS for the Lambda
extractor (a later task) to run asynchronously.

A `MemoryStore` ledger row is the console-facing identity of a store; the
workspace's own bootstrap store (`store_id_for(ws)`, normally recorded as
`resources["memory_id"]`) is *synthesized* on read until the console edits it for
the first time — no row, no AWS resource, just a well-known default (ruling: the
default store exists implicitly). Every console-created store beyond that one is a
real row from the moment it is created.
"""

import json
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.core import naming
from app.core.db import SessionLocal
from app.core.errors import AppError, NotFoundError, redacted_error
from app.models.ledger import MemoryStore
from app.services import aws_clients, embedding_backend, opensearch_retry, s3_sweep
from app.services import opensearch_http as core
from app.services.memory_backend import SCOPE_SEP, decode_actor, decode_record_text
from app.services.workspace import WorkspaceContext

logger = logging.getLogger("launchpad.memory")

DEFAULT_STORE_NAME = "launchpad_memory"
DEFAULT_EVENT_EXPIRY_DAYS = 30
DEFAULT_STRATEGIES = ["semantic", "user_preference", "summarization"]
ACTOR_PAGE = 100
RECORD_PAGE = 20
RAIL_RECORDS_PER_NAMESPACE = 5
_VALID_STRATEGY_IDS = {s["strategy_id"] for s in core.STRATEGIES}
# --- CN memory/KB (N6) fix round 2 ---
# `list_sessions` probes every session's event range (oldest/newest) to compute
# `created_at` and apply the store's expiry — bounded to one S3 list call per
# session (see `_session_times`) rather than paginating every events/ page.
_SESSION_PROBE_MAX_KEYS = 1000
# --- end CN memory/KB (N6) fix round 2 ---


def store_id_for(ws: WorkspaceContext) -> str:
    """The workspace's own bootstrap store id — what the bootstrap stage records
    into `resources["memory_id"]`. Independent of whatever `substrate_for` reads
    back from that resource map (a legacy workspace could have a different value)."""
    return f"{DEFAULT_STORE_NAME}-{ws.id}"


@dataclass(frozen=True)
class MemorySubstrate:
    bucket: str
    prefix: str
    endpoint: str
    queue_url: str
    store_id: str
    admin_role_arn: str


def has_substrate(ws: WorkspaceContext) -> bool:
    r = ws.resources or {}
    return bool(r.get("memory_os_endpoint") and r.get("memory_id"))


def substrate_for(ws: WorkspaceContext) -> MemorySubstrate:
    """The resolved substrate for `ws`, or 503 `memory.not_provisioned` (ruling R16)
    when the workspace has not run the memory bootstrap stage — every method that
    touches AWS calls this first so a workspace without the substrate degrades to a
    clear, catchable error rather than a `KeyError` deep in a boto3 call."""
    if not has_substrate(ws):
        raise AppError(
            "memory.not_provisioned",
            "self-hosted memory is not provisioned for this workspace — deploy the "
            "launchpad-memory stack and run its bootstrap",
            {"workspace": ws.id},
            status_code=503,
        )
    r = ws.resources
    return MemorySubstrate(
        bucket=r.get("memory_bucket") or r["artifacts_bucket"],
        prefix=r.get("memory_prefix") or "memory",
        endpoint=r["memory_os_endpoint"],
        queue_url=r.get("memory_queue_url", ""),
        store_id=str(r["memory_id"]),
        admin_role_arn=r.get("memory_admin_role_arn", ""),
    )


def os_ctx(ws: WorkspaceContext) -> WorkspaceContext:
    """The workspace context OpenSearch calls sign with — same account/region/id/
    resources, but assuming the FGAC master role when the workspace recorded one
    (ruling R13); never assumes a role for S3/SQS, which use the workspace's own
    credentials."""
    r = ws.resources or {}
    return WorkspaceContext(
        account_id=ws.account_id,
        region=ws.region,
        resources=ws.resources,
        role_arn=r.get("memory_admin_role_arn") or None,
        id=ws.id,
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def merge_settings_for_row(row: MemoryStore | None) -> dict[str, Any]:
    """The store's merge strategy v2 settings (r14-a) with defaults filled — a pre-v2 row
    (NULL column) and the synthesized default store both read as the defaults."""
    return core.normalize_merge_settings(getattr(row, "merge_settings", None) or {})


def _merge_patch(
    current: dict[str, Any] | None, patch: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Stored `merge_settings` after applying `patch`: only known keys, `None` resets a key
    to its default (dropped from the row), the result is `None` when nothing is set."""
    out = {k: v for k, v in (current or {}).items() if k in core.MERGE_SETTINGS_DEFAULTS}
    for key, value in (patch or {}).items():
        if key not in core.MERGE_SETTINGS_DEFAULTS:
            continue
        if value is None:
            out.pop(key, None)
        else:
            out[key] = int(value)
    return out or None


# Env keys the deployer writes for the rendered runtime library's recall ranking; the
# library falls back to the core defaults when either is absent.
ENV_MEMORY_HALF_LIFE_DAYS = "LAUNCHPAD_MEMORY_HALF_LIFE_DAYS"
ENV_MEMORY_TTL_DAYS = "LAUNCHPAD_MEMORY_TTL_DAYS"


def recall_env_for(memory_id: str) -> dict[str, str]:
    """`deployer/environment.py` hook (r14-a): the recall knobs of store `memory_id` as env
    vars, **only when they differ from the defaults** (today's environment stays byte for
    byte otherwise). Fail-soft: no row, no table, or a ledger error → `{}`. The store id
    alone keys the lookup — the default store id embeds the workspace id and console-created
    ids carry a random suffix."""
    try:
        db = SessionLocal()
        try:
            row = db.query(MemoryStore).filter(MemoryStore.memory_id == memory_id).first()
        finally:
            db.close()
    except Exception:  # noqa: BLE001 - a deploy never fails on a recall knob lookup
        return {}
    settings = merge_settings_for_row(row)
    out: dict[str, str] = {}
    if settings["recall_half_life_days"] != core.RECALL_HALF_LIFE_DAYS:
        out[ENV_MEMORY_HALF_LIFE_DAYS] = str(settings["recall_half_life_days"])
    if settings["long_term_ttl_days"]:
        out[ENV_MEMORY_TTL_DAYS] = str(settings["long_term_ttl_days"])
    return out


def purge_agent_records(
    ws: WorkspaceContext, agent_id: str, memory_id: str | None = None,
) -> int:
    """N11-obs19: delete every long-term record of one agent — the teardown counterpart of
    `scoped_actor`. Per-agent memory folds the agent id in front of the human actor
    (`<agent_id>__<human>`), so one `delete_by_query` on the environment's memory index
    (`naming.memory_index()`, D17 prefix-aware) filtered on the store the spec pins (or the
    workspace default) AND the actor prefix `<agent_id>__` removes all of its actors' facts,
    preferences and summaries while leaving every other agent — and every other store that
    shares the index — untouched. Returns the number of documents deleted. Raises
    `memory.not_provisioned` (503) before building any client when the workspace has no
    substrate; the caller (`deployer/teardown.py`) treats that as a skipped step.
    """
    sub = substrate_for(ws)
    client = aws_clients.opensearch_http(
        os_ctx(ws), sub.endpoint, attempts=opensearch_retry.INTERACTIVE_ATTEMPTS
    )
    return int(client.delete_by_query(naming.memory_index(), {"bool": {"filter": [
        {"term": {"store_id": memory_id or sub.store_id}},
        {"prefix": {"actor_id": f"{agent_id}{SCOPE_SEP}"}},
    ]}}))


class SelfHostedMemoryBackend:
    """`MemoryBackend` over S3 + OpenSearch + SQS. The four injection points
    (`s3`, `opensearch`, `sqs`, `embedder`) each accept either a ready client/
    instance or a callable that builds one from the workspace being served —
    `None` falls back to the real funnel (`WorkspaceContext.client`,
    `aws_clients.opensearch_http`, `aws_clients.global_bedrock_runtime_client`).
    Tests inject the hermetic fakes from `tests/memory_fakes.py` directly."""

    def __init__(
        self,
        *,
        s3: Any = None,
        opensearch: Any = None,
        sqs: Any = None,
        embedder: Any = None,
        clock: Any = None,
    ) -> None:
        self._s3_init = s3
        self._os_init = opensearch
        self._sqs_init = sqs
        self._embedder_init = embedder
        self._clock = clock or (lambda: datetime.now(UTC))

    # ── client resolvers ────────────────────────────────────────────────────
    def _now(self) -> datetime:
        return self._clock()

    def _s3(self, ws: WorkspaceContext) -> Any:
        if self._s3_init is None:
            return ws.client("s3")
        if callable(self._s3_init):
            return self._s3_init(ws)
        return self._s3_init

    def _os(self, ws: WorkspaceContext) -> Any:
        if self._os_init is None:
            endpoint = substrate_for(ws).endpoint
            # Request thread: 2 attempts × 10 s read timeout (~21 s worst case on a dead
            # or busy domain), not the bootstrap's 3 × 30 s (~93 s) — N-SCN-obs11.
            return aws_clients.opensearch_http(
                os_ctx(ws), endpoint,
                attempts=opensearch_retry.INTERACTIVE_ATTEMPTS,
                read_timeout=opensearch_retry.INTERACTIVE_READ_TIMEOUT_SECONDS,
            )
        if callable(self._os_init):
            return self._os_init(ws)
        return self._os_init

    def _sqs(self, ws: WorkspaceContext) -> Any:
        if self._sqs_init is None:
            return ws.client("sqs")
        if callable(self._sqs_init):
            return self._sqs_init(ws)
        return self._sqs_init

    def _embedder(self) -> Any:
        if self._embedder_init is None:
            # D21: the registry's active embedding row decides the class (Titan / OpenAI-
            # compatible) and the key the hub resolves — never spelled here.
            return embedding_backend.build_embedder()
        if callable(self._embedder_init):
            return self._embedder_init()
        return self._embedder_init

    # ── stores (ledger) ──────────────────────────────────────────────────────
    def _row(self, ws: WorkspaceContext, memory_id: str) -> MemoryStore | None:
        db = SessionLocal()
        try:
            return (
                db.query(MemoryStore)
                .filter(MemoryStore.workspace_id == ws.id, MemoryStore.memory_id == memory_id)
                .first()
            )
        finally:
            db.close()

    def _row_to_dict(self, row: MemoryStore, is_default: bool) -> dict[str, Any]:
        stored = list(row.strategies or [])
        strategies = [s for s in stored if s in _VALID_STRATEGY_IDS]
        if len(strategies) != len(stored):
            # A store created before R13 may still list the retired `episodic`
            # strategy (N13-obs4): keep loading it, just without that entry.
            logger.info(
                "memory store %s: ignoring retired strategies %s",
                row.memory_id, sorted(set(stored) - set(strategies)),
            )
        return {
            "id": row.memory_id,
            "name": row.name,
            "description": row.description,
            "status": row.status,
            "event_expiry_days": row.event_expiry_days,
            "strategies": strategies,
            "namespace_keys": list(row.namespace_keys or []),
            "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at),
            "is_default": is_default,
            "merge": merge_settings_for_row(row),
        }

    def _synthesized_default(self, sub: MemorySubstrate) -> dict[str, Any]:
        return {
            "id": sub.store_id,
            "name": DEFAULT_STORE_NAME,
            "description": "",
            "status": "ACTIVE",
            "event_expiry_days": DEFAULT_EVENT_EXPIRY_DAYS,
            "strategies": list(DEFAULT_STRATEGIES),
            "namespace_keys": [],
            "created_at": None,
            "updated_at": None,
            "is_default": True,
            "merge": merge_settings_for_row(None),
        }

    def _merge_settings(self, ws: WorkspaceContext, store_id: str) -> dict[str, Any]:
        try:
            return dict(self._store(ws, store_id)["merge"])
        except NotFoundError:
            return merge_settings_for_row(None)

    def _store(self, ws: WorkspaceContext, memory_id: str) -> dict[str, Any]:
        sub = substrate_for(ws)
        row = self._row(ws, memory_id)
        if row is not None:
            return self._row_to_dict(row, row.memory_id == sub.store_id)
        if memory_id == sub.store_id:
            return self._synthesized_default(sub)
        raise NotFoundError(
            "memory.not_found", f"memory resource {memory_id!r} not found",
            {"memory_id": memory_id},
        )

    def _expiry_days(self, ws: WorkspaceContext, store_id: str) -> int:
        try:
            return int(self._store(ws, store_id)["event_expiry_days"])
        except NotFoundError:
            return DEFAULT_EVENT_EXPIRY_DAYS

    def _detail(self, store: dict[str, Any]) -> dict[str, Any]:
        strategies = [
            {
                "strategy_id": s["strategy_id"],
                "name": s["name"],
                "type": s["type"],
                "status": "ACTIVE",
                "namespaces": [s["template"]],
            }
            for s in core.STRATEGIES
            if s["strategy_id"] in store["strategies"]
        ]
        return {
            "id": store["id"],
            "arn": None,
            "name": store["name"],
            "description": store["description"],
            "status": store["status"],
            "failure_reason": None,
            "event_expiry_days": store["event_expiry_days"],
            "execution_role_arn": None,
            "created_at": store["created_at"],
            "updated_at": store["updated_at"],
            "is_default": store["is_default"],
            "strategies": strategies,
            "namespace_keys": store["namespace_keys"],
            # r14-a: merge strategy v2 knobs, defaults filled
            "merge": dict(store.get("merge") or merge_settings_for_row(None)),
        }

    def _row_item(
        self, row: MemoryStore | None, memory_id: str, is_default: bool
    ) -> dict[str, Any]:
        if row is None:
            return {
                "id": memory_id, "arn": None, "name": DEFAULT_STORE_NAME, "status": "ACTIVE",
                "created_at": None, "updated_at": None, "is_default": is_default, "agents": [],
            }
        return {
            "id": row.memory_id, "arn": None, "name": row.name, "status": row.status,
            "created_at": _iso(row.created_at), "updated_at": _iso(row.updated_at),
            "is_default": is_default, "agents": [],
        }

    def list_memory_resources(self, ws: WorkspaceContext) -> dict[str, Any]:
        sub = substrate_for(ws)
        db = SessionLocal()
        try:
            rows = (
                db.query(MemoryStore)
                .filter(MemoryStore.workspace_id == ws.id)
                .order_by(MemoryStore.created_at)
                .all()
            )
        finally:
            db.close()
        default_row = next((r for r in rows if r.memory_id == sub.store_id), None)
        items = [self._row_item(default_row, sub.store_id, True)]
        for row in rows:
            if row.memory_id == sub.store_id:
                continue
            items.append(self._row_item(row, row.memory_id, False))
        return {"items": items, "default_id": sub.store_id}

    def get_memory_resource(self, ws: WorkspaceContext, memory_id: str) -> dict[str, Any]:
        return self._detail(self._store(ws, memory_id))

    def create_memory_resource(
        self, ws: WorkspaceContext, name: str, description: str,
        event_expiry_days: int, strategies: list[str],
        namespace_keys: list[dict[str, Any]] | None = None,
        merge: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        substrate_for(ws)  # 503 before any ledger write if not provisioned
        unknown = [s for s in strategies if s not in _VALID_STRATEGY_IDS]
        if unknown:
            raise AppError(
                "memory.unknown_strategy", f"unknown strategy id(s): {', '.join(unknown)}",
                {"strategies": unknown}, status_code=422,
            )
        db = SessionLocal()
        try:
            existing = (
                db.query(MemoryStore)
                .filter(MemoryStore.workspace_id == ws.id, MemoryStore.name == name)
                .first()
            )
            if existing is not None:
                raise AppError(
                    "memory.name_exists", f"a memory resource named {name!r} already exists",
                    {"name": name}, status_code=409,
                )
            memory_id = f"{name}-{secrets.token_hex(4)}"
            row = MemoryStore(
                workspace_id=ws.id, memory_id=memory_id, name=name,
                description=description or "", event_expiry_days=event_expiry_days,
                strategies=list(strategies), namespace_keys=list(namespace_keys or []),
                # only the knobs the caller set are stored; the rest read as defaults
                merge_settings=_merge_patch(None, merge),
            )
            db.add(row)
            try:
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                raise AppError(
                    "memory.name_exists", f"a memory resource named {name!r} already exists",
                    {"name": name}, status_code=409,
                ) from exc
            return self._detail(self._row_to_dict(row, False))
        finally:
            db.close()

    def update_memory_resource(
        self, ws: WorkspaceContext, memory_id: str, *,
        description: str | None = None, event_expiry_days: int | None = None,
        merge: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        sub = substrate_for(ws)
        db = SessionLocal()
        try:
            row = (
                db.query(MemoryStore)
                .filter(MemoryStore.workspace_id == ws.id, MemoryStore.memory_id == memory_id)
                .first()
            )
            if row is None:
                if memory_id != sub.store_id:
                    raise NotFoundError(
                        "memory.not_found", f"memory resource {memory_id!r} not found",
                        {"memory_id": memory_id},
                    )
                # First edit of the default store materializes its row.
                row = MemoryStore(
                    workspace_id=ws.id, memory_id=memory_id, name=DEFAULT_STORE_NAME,
                    description="", event_expiry_days=DEFAULT_EVENT_EXPIRY_DAYS,
                    strategies=list(DEFAULT_STRATEGIES), namespace_keys=[],
                )
                db.add(row)
            if description is not None:
                row.description = description
            if event_expiry_days is not None:
                row.event_expiry_days = event_expiry_days
            if merge:
                # r14-a: a partial patch over the stored knobs (a key set to null resets it)
                row.merge_settings = _merge_patch(row.merge_settings, merge)
            row.updated_at = self._now()
            db.commit()
            return self._detail(self._row_to_dict(row, row.memory_id == sub.store_id))
        finally:
            db.close()

    def _purge_s3_prefix(self, ws: WorkspaceContext, sub: MemorySubstrate, memory_id: str) -> None:
        """The store's S3 prefix through the shared reporting deleter (N17-obs119): a key
        S3 refused inside a 200 `DeleteObjects` answer (`Errors[]`) raises `S3SweepResidue`, so
        `delete_memory_resource`'s step guard reports it `skipped` with a redacted warning
        instead of calling the purge complete with the keys still there."""
        s3_sweep.delete_prefix(
            self._s3(ws), sub.bucket, f"{sub.prefix}/{memory_id}/"
        ).raise_for_residue()

    def delete_memory_resource(self, ws: WorkspaceContext, memory_id: str) -> dict[str, Any]:
        sub = substrate_for(ws)
        if memory_id == sub.store_id:
            raise AppError(
                "memory.default_protected",
                "the workspace's default memory cannot be deleted",
                {"memory_id": memory_id}, status_code=409,
            )
        db = SessionLocal()
        try:
            row = (
                db.query(MemoryStore)
                .filter(MemoryStore.workspace_id == ws.id, MemoryStore.memory_id == memory_id)
                .first()
            )
            if row is None:
                raise NotFoundError(
                    "memory.not_found", f"memory resource {memory_id!r} not found",
                    {"memory_id": memory_id},
                )
            db.delete(row)
            db.commit()
        finally:
            db.close()
        # The delete is complete once the ledger row is gone; the two storage purges are
        # best-effort and EVERY step is guarded (N14-obs4 for the OpenSearch purge, N17-obs93
        # for the S3 one — an unguarded `list_objects_v2` failure used to answer 502 for a
        # store that was already gone and skip the index purge after it). A failed step is
        # reported `skipped` with a redacted warning (type + status / error code, never a
        # body) and one WARNING line per step so an operator can re-run the purge by hand.
        cleanup: dict[str, str] = {}
        warnings: list[str] = []
        steps: tuple[tuple[str, Callable[[], Any]], ...] = (
            ("s3", lambda: self._purge_s3_prefix(ws, sub, memory_id)),
            ("index", lambda: self._os(ws).delete_by_query(
                naming.memory_index(), {"term": {"store_id": memory_id}})),
        )
        for step, run in steps:
            try:
                run()
                cleanup[step] = "ok"
            except Exception as exc:  # noqa: BLE001 — best-effort by contract (see above)
                cleanup[step] = "skipped"
                warnings.append(f"{step}: {redacted_error(exc)}")
                logger.warning(
                    "memory_selfhosted: %s purge skipped for deleted store %r (%s)",
                    step, memory_id, redacted_error(exc),
                )
        return {"id": memory_id, "deleted": True, "cleanup": cleanup,
                "cleanup_warnings": warnings}

    # ── strategies / namespaces ──────────────────────────────────────────────
    def get_strategies(self, ws: WorkspaceContext) -> list[dict[str, Any]]:
        sub = substrate_for(ws)
        enabled = self._store(ws, sub.store_id)["strategies"]
        return [
            {
                "strategy_id": s["strategy_id"], "name": s["name"], "description": None,
                "type": s["type"], "status": "ACTIVE", "namespaces": [s["template"]],
                "namespace_templates": [s["template"]], "created_at": None, "updated_at": None,
            }
            for s in core.STRATEGIES
            if s["strategy_id"] in enabled
        ]

    def resolve_namespaces(
        self, ws: WorkspaceContext, actor_id: str,
        strategies: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        if strategies is None:
            strategies = self.get_strategies(ws)
        out = []
        for s in strategies:
            template = s.get("template") or s["namespace_templates"][0]
            namespace, resolvable, prefix = core.resolve_namespace(template, actor_id)
            out.append({
                "strategy_id": s["strategy_id"], "strategy_name": s["name"],
                "strategy_type": s["type"], "template": template, "namespace": namespace,
                "resolvable": resolvable, "prefix": prefix,
            })
        return out

    # ── S3 snapshots ──────────────────────────────────────────────────────────
    def list_actors(
        self, ws: WorkspaceContext, next_token: str | None = None,
        max_results: int | None = None,
    ) -> dict[str, Any]:
        sub = substrate_for(ws)
        s3 = self._s3(ws)
        prefix = f"{sub.prefix}/{sub.store_id}/"
        kwargs: dict[str, Any] = {
            "Bucket": sub.bucket, "Prefix": prefix, "Delimiter": "/",
            "MaxKeys": max_results or ACTOR_PAGE,
        }
        if next_token:
            kwargs["ContinuationToken"] = next_token
        page = s3.list_objects_v2(**kwargs)
        items = []
        for cp in page.get("CommonPrefixes", []):
            segment = cp["Prefix"][len(prefix):].rstrip("/")
            items.append(decode_actor(segment))
        return {"items": items, "next_token": page.get("NextContinuationToken")}

    def _session_times(
        self, ws: WorkspaceContext, sub: MemorySubstrate, actor_id: str, session_id: str,
    ) -> tuple[datetime | None, datetime | None]:
        """(oldest, newest) event timestamps for one session, from a single
        bounded `list_objects_v2` call (fix round 2 — the previous version
        paginated every `events/` page per session, which was O(sessions × pages)
        S3 round trips for one `list_sessions` request: 100 sessions each with a
        few pages of events easily reached hundreds of calls).

        Event keys sort chronologically (ruling R5's lexicographic timestamp
        prefix), so the first/last `Contents` entries of one page are the true
        oldest/newest *as long as the session holds at most
        `_SESSION_PROBE_MAX_KEYS` events*. A session past that bound reports an
        approximate `newest` — the last key of this single page, not the
        session's true last event — rather than paying for further round trips;
        this is an accepted, documented approximation (no runtime-written
        session manifest exists to read instead per `memory-runtime.md`)."""
        s3 = self._s3(ws)
        prefix = f"{sub.prefix}/{sub.store_id}/{actor_id}/session_{session_id}/events/"
        page = s3.list_objects_v2(
            Bucket=sub.bucket, Prefix=prefix, MaxKeys=_SESSION_PROBE_MAX_KEYS,
        )
        contents = page.get("Contents", [])
        if not contents:
            return None, None
        oldest = core.parse_event_key(contents[0]["Key"][len(prefix):])
        newest = core.parse_event_key(contents[-1]["Key"][len(prefix):])
        return oldest, newest

    def list_sessions(
        self, ws: WorkspaceContext, actor_id: str, next_token: str | None = None,
        max_results: int | None = None,
    ) -> dict[str, Any]:
        sub = substrate_for(ws)
        s3 = self._s3(ws)
        prefix = f"{sub.prefix}/{sub.store_id}/{actor_id}/"
        kwargs: dict[str, Any] = {
            "Bucket": sub.bucket, "Prefix": prefix, "Delimiter": "/",
            "MaxKeys": max_results or ACTOR_PAGE,
        }
        if next_token:
            kwargs["ContinuationToken"] = next_token
        page = s3.list_objects_v2(**kwargs)
        cutoff = self._now() - timedelta(days=self._expiry_days(ws, sub.store_id))
        rows = []
        for cp in page.get("CommonPrefixes", []):
            segment = cp["Prefix"][len(prefix):].rstrip("/")
            if not segment.startswith("session_"):
                continue
            session_id = segment[len("session_"):]
            created_at, newest = self._session_times(ws, sub, actor_id, session_id)
            if newest is not None and newest < cutoff:
                continue
            rows.append((
                {
                    "session_id": session_id, "actor_id": actor_id,
                    "created_at": _iso(created_at), "ledger": None,
                },
                newest,
            ))
        rows.sort(key=lambda t: t[1] or datetime.min.replace(tzinfo=UTC), reverse=True)
        return {"items": [r for r, _ in rows], "next_token": page.get("NextContinuationToken")}

    def _event_item(self, body: dict[str, Any], include_payloads: bool) -> dict[str, Any]:
        payload: list[dict[str, Any]] = []
        if include_payloads:
            for part in body.get("payload", []):
                conv = part.get("conversational")
                if conv:
                    payload.append({
                        "kind": "conversational", "role": conv.get("role"),
                        "text": (conv.get("content") or {}).get("text"),
                        "parts": [], "blob_bytes": None, "conversational": conv,
                    })
        return {
            **body,
            "event_id": body.get("eventId"),
            "at": body.get("eventTimestamp"),
            "branch": None,
            "metadata": body.get("metadata", {}),
            "payload": payload,
        }

    def list_events(
        self, ws: WorkspaceContext, actor_id: str, session_id: str,
        include_payloads: bool = True, next_token: str | None = None,
        max_results: int | None = None,
    ) -> dict[str, Any]:
        sub = substrate_for(ws)
        cutoff = self._now() - timedelta(days=self._expiry_days(ws, sub.store_id))
        s3 = self._s3(ws)
        prefix = f"{sub.prefix}/{sub.store_id}/{actor_id}/session_{session_id}/events/"
        kwargs: dict[str, Any] = {
            "Bucket": sub.bucket, "Prefix": prefix, "MaxKeys": max_results or ACTOR_PAGE,
        }
        if next_token:
            kwargs["ContinuationToken"] = next_token
        page = s3.list_objects_v2(**kwargs)
        items = []
        for c in page.get("Contents", []):
            key = c["Key"]
            ts = core.parse_event_key(key[len(prefix):])
            if ts is not None and ts < cutoff:
                continue
            body = json.loads(s3.get_object(Bucket=sub.bucket, Key=key)["Body"].read())
            items.append(self._event_item(body, include_payloads))
        return {"items": items, "next_token": page.get("NextContinuationToken")}

    def create_turn_event(
        self, ws: WorkspaceContext, actor_id: str, session_id: str, prompt: str, answer: str,
    ) -> dict[str, Any]:
        sub = substrate_for(ws)
        ts = self._now()
        ev = core.turn_event(actor_id, session_id, prompt, answer, ts)
        key = (
            f"{sub.prefix}/{sub.store_id}/{actor_id}/session_{session_id}/events/"
            f"{core.event_key(ts, ev['eventId'][:8])}"
        )
        s3 = self._s3(ws)
        s3.put_object(
            Bucket=sub.bucket, Key=key, Body=json.dumps(ev).encode(),
            ContentType="application/json",
        )
        queued = bool(sub.queue_url)
        if queued:
            message: dict[str, Any] = {
                "kind": "memory_extract", "store_id": sub.store_id, "workspace_id": ws.id,
                "actor_id": actor_id, "session_id": session_id, "event_key": key,
                "long_term": True,
                # D21: the embedding row the Lambda must use (= the one the hub queries)
                "embedding": embedding_backend.message_block(),
                # r14-a: the store's merge strategy knobs (caps, TTL, summary interval)
                "merge": self._merge_settings(ws, sub.store_id),
            }
            # N17-obs252: the extraction model = the `memory_extract` role's catalog row;
            # omitted (Lambda env fallback) while the role has no row
            extract_model = embedding_backend.extract_model_block()
            if extract_model:
                message["extract_model"] = extract_model
            self._sqs(ws).send_message(QueueUrl=sub.queue_url, MessageBody=json.dumps(message))
        return {"event_id": ev["eventId"], "event_key": key, "queued": queued}

    # ── OpenSearch records ───────────────────────────────────────────────────
    def _ns_filter(self, namespace: str) -> list[dict[str, Any]]:
        if namespace.endswith("/"):
            return [{"prefix": {"namespace": namespace}}]
        return [{"term": {"namespace": namespace}}]

    def _record(self, hit: dict[str, Any], scored: bool = False) -> dict[str, Any]:
        raw = hit.get("text") or ""
        text, structured = decode_record_text(raw)
        return {
            "record_id": hit.get("_id"),
            "text": text,
            "structured": structured,
            "raw_text": raw,
            "strategy_id": hit.get("strategy"),
            "namespaces": [hit.get("namespace")],
            "created_at": hit.get("created_at"),
            # r14-a: a search hit's `score` is the v2 recall score (similarity × recency ×
            # confidence); `similarity` is the raw cosine. A listing has neither.
            "score": (hit.get("recall_score", hit.get("_score"))) if scored else None,
            "similarity": hit.get("similarity") if scored else None,
            "metadata": {"store_id": hit.get("store_id"), "session_id": hit.get("session_id")},
            # r14-a: op-history fields — absent on a pre-v2 document (read as active, 1.0)
            "updated_at": hit.get("updated_at"),
            "confidence": core.record_confidence(hit),
            "superseded_by": hit.get("superseded_by"),
            "superseded_at": hit.get("superseded_at"),
            "supersedes": list(hit.get("supersedes") or []),
            "deleted_at": hit.get("deleted_at"),
            "expired_at": hit.get("expired_at"),
            "last_op": hit.get("last_op"),
            "active": core.record_is_active(hit),
        }

    def _visibility_filters(
        self, ws: WorkspaceContext, store_id: str, include_superseded: bool = False,
    ) -> list[dict[str, Any]]:
        """Filter clauses hiding superseded / deleted / expired records and, when the store
        has a TTL, records older than it (soft TTL on read — no reindex, no write)."""
        if include_superseded:
            return []
        out = [core.active_record_filter()]
        ttl_days = self._merge_settings(ws, store_id)["long_term_ttl_days"]
        ttl = core.ttl_cutoff_filter(self._now(), ttl_days)
        if ttl is not None:
            out.append(ttl)
        return out

    def list_records(
        self, ws: WorkspaceContext, namespace: str, strategy_id: str | None = None,
        next_token: str | None = None, max_results: int | None = None,
        include_superseded: bool = False,
    ) -> dict[str, Any]:
        sub = substrate_for(ws)
        filters = [{"term": {"store_id": sub.store_id}}, *self._ns_filter(namespace)]
        if strategy_id:
            filters.append({"term": {"strategy": strategy_id}})
        # r14-a: superseded / deleted / expired records are hidden unless asked for
        filters.extend(self._visibility_filters(ws, sub.store_id, include_superseded))
        from_ = int(next_token or 0)
        size = max_results or RECORD_PAGE
        body = {
            "query": {"bool": {"filter": filters}},
            "sort": [{"created_at": {"order": "desc"}}],
            "from": from_, "size": size,
            "_source": {"excludes": ["embedding"]},
        }
        result = self._os(ws).search(naming.memory_index(), body)
        hits = result["hits"]["hits"]
        total = result["hits"]["total"]["value"]
        items = [self._record({"_id": h["_id"], **h["_source"]}) for h in hits]
        next_tok = str(from_ + size) if from_ + size < total else None
        return {"items": items, "next_token": next_tok, "namespace": namespace}

    def search_records(
        self, ws: WorkspaceContext, namespace: str, query: str, top_k: int = 5,
        strategy_id: str | None = None,
    ) -> dict[str, Any]:
        sub = substrate_for(ws)
        vector = self._embedder().embed(query)
        filters = [{"term": {"store_id": sub.store_id}}, *self._ns_filter(namespace)]
        if strategy_id:
            filters.append({"term": {"strategy": strategy_id}})
        merge = self._merge_settings(ws, sub.store_id)
        filters.extend(self._visibility_filters(ws, sub.store_id))
        # r14-a recall ranking: over-fetch active candidates, re-rank by similarity ×
        # recency decay (store half-life) × confidence floor, keep top_k (ruling R3: the
        # cosine is computed here, never read off the engine's `_score`).
        candidates = max(
            min(top_k * core.RECALL_CANDIDATES_PER_K, core.RECALL_MAX_CANDIDATES), top_k
        )
        hits = self._os(ws).knn_search(
            naming.memory_index(), vector, candidates, filters, include_embedding=True
        )
        ranked = core.rank_records(
            hits, vector, self._now(), top_k, half_life_days=merge["recall_half_life_days"]
        )
        items = [self._record(h, scored=True) for h in ranked]
        return {"items": items, "next_token": None, "namespace": namespace, "query": query}

    def retrieve_records(
        self, ws: WorkspaceContext, namespace: str, query: str, top_k: int = 3,
    ) -> list[dict]:
        return self.search_records(ws, namespace, query, top_k)["items"]

    def delete_record(
        self, ws: WorkspaceContext, record_id: str, actor_id: str,
    ) -> dict[str, Any]:
        """N10v2-bug6: delete one long-term record of ``actor_id`` in this workspace's store.

        The document is read first so both scopes are checked on the stored fields — a
        record of another store (another workspace) or another actor answers the same
        404 as an unknown id, and nothing is deleted. There is no per-record S3 object in
        the self-hosted layout (S3 holds session snapshots and turn events only; the
        extractor writes records to OpenSearch alone), so the index delete — plus the
        unlinking of the merge-v2 history neighbours (N16-obs111's sibling N16-obs114,
        ``_unlink_history``) — is the whole operation.
        """
        sub = substrate_for(ws)
        os_client = self._os(ws)
        index = naming.memory_index()
        doc = os_client.get_doc(index, record_id)
        if (
            doc is None
            or doc.get("store_id") != sub.store_id
            or doc.get("actor_id") != actor_id
        ):
            raise NotFoundError(
                "memory.not_found", f"memory record {record_id!r} not found for this actor",
                {"record_id": record_id, "actor_id": actor_id},
            )
        # neighbours first, then the document: a retry after a failed delete re-runs the
        # (idempotent) unlink and still finds the record; the reverse order could leave the
        # dangling link the fix exists to prevent. N17-obs91: when the delete itself fails
        # the unlink is undone — for a chain head it had already revived the newest
        # predecessor next to the head that retired it — so a failed call changes nothing.
        undo = self._unlink_history(os_client, index, record_id, doc)
        try:
            os_client.delete_doc(index, record_id)
        except Exception:
            self._relink_history(os_client, index, record_id, undo)
            raise
        return {
            "record_id": record_id,
            "actor_id": actor_id,
            "namespace": doc.get("namespace"),
            "strategy_id": doc.get("strategy"),
            "deleted": True,
        }

    @staticmethod
    def _unlink_history(
        os_client: Any, index: str, record_id: str, doc: dict[str, Any],
    ) -> list[tuple[str, dict[str, Any]]]:
        """N16-obs114: a console delete of a record inside a merge-v2 chain used to leave its
        neighbours pointing at a document that no longer exists (`B.supersedes` naming the
        gone A; `A.superseded_by` naming a gone B) — `include_superseded=true` listings and
        the index scan saw the dangling link. The neighbours are unlinked in the same
        operation, nothing else about them changes (history is kept):

        * the **successor** (`doc.superseded_by`) drops this id from its `supersedes`;
        * each **predecessor** (`doc.supersedes[]`) is re-pointed at the successor when there
          is one — `superseded_by = successor`, and the successor's `supersedes` gains it —
          so the chain stays connected and the predecessor stays retired;
        * a record with no successor (the chain's head) frees exactly ONE predecessor — the
          newest by `record_timestamp` (`updated_at`, else `created_at`; id as tie-break):
          its `superseded_by` / `superseded_at` are cleared and it reads active again (the
          fact that retired it is gone; nothing is re-extracted) — and the other
          predecessors (a consolidate's members, near-duplicates by construction) are
          re-pointed at that one, so the merge-v2 invariant "no two ACTIVE records at
          cosine ≥ the dedupe threshold" holds after the delete (rev-r16 on cn/r16-q).

        A neighbour id that no longer resolves is skipped (the delete is retry-safe).
        Returns the undo list — ``(neighbour id, its whole document as it was read)`` per
        write, in order — that `_relink_history` re-indexes when the delete that follows fails
        (N17-obs91). Contract: memory-backend.md "Merge strategy (v2)" › the record delete
        unlinks."""
        undo: list[tuple[str, dict[str, Any]]] = []

        def relink(doc_id: str, before: dict[str, Any], fields: dict[str, Any]) -> None:
            undo.append((doc_id, dict(before)))
            os_client.update_doc(index, doc_id, fields)

        successor_id = doc.get("superseded_by") or None
        predecessors = [m for m in (doc.get("supersedes") or []) if m and m != record_id]
        successor = os_client.get_doc(index, successor_id) if successor_id else None
        if successor_id and successor is None:
            successor_id = None  # already dangling: nothing to re-point at
        members: dict[str, dict[str, Any]] = {}
        for member in predecessors:
            target = os_client.get_doc(index, member)
            if target is not None and member != successor_id:
                members[member] = target
        if successor is not None:
            kept = [m for m in (successor.get("supersedes") or []) if m != record_id]
            kept.extend(m for m in members if m not in kept)
            relink(successor_id, successor, {"supersedes": kept})
            for member in members:
                relink(member, members[member], {"superseded_by": successor_id})
            return undo
        if not members:
            return undo
        newest = max(members, key=lambda m: (core.record_timestamp(members[m]), m))
        others = [m for m in members if m != newest]
        if others:
            kept = [m for m in (members[newest].get("supersedes") or []) if m != record_id]
            kept.extend(m for m in others if m not in kept)
            relink(newest, members[newest], {
                "superseded_by": None, "superseded_at": None, "supersedes": kept})
            for member in others:
                relink(member, members[member], {"superseded_by": newest})
        else:
            relink(newest, members[newest], {"superseded_by": None, "superseded_at": None})
        return undo

    @staticmethod
    def _relink_history(
        os_client: Any, index: str, record_id: str, undo: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """N17-obs91: the record delete failed AFTER `_unlink_history` had already changed its
        neighbours — for a chain head, the newest predecessor was reading active again next to
        the head that retired it. Re-index every neighbour exactly as `_unlink_history` read it
        (`get_doc` returns the whole source, embedding included; a partial update could not
        drop a link field the unlink added), newest write first, so the failed call leaves the
        index as it found it and the member's retry re-runs the idempotent unlink. Best-effort:
        a neighbour the re-link cannot reach is logged (id + exception type) — the retry heals
        it, since the record is still there."""
        for doc_id, before in reversed(undo):
            try:
                os_client.put_doc(index, doc_id, before)
            except Exception as exc:  # noqa: BLE001 — the original failure is what propagates
                logger.warning(
                    "memory_selfhosted: delete of %r failed and neighbour %r could not be "
                    "re-linked (%s) — a retried delete re-runs the unlink",
                    record_id, doc_id, type(exc).__name__,
                )

    def list_extraction_jobs(
        self, ws: WorkspaceContext, actor_id: str | None = None, session_id: str | None = None,
        strategy_id: str | None = None, status: str | None = None,
        next_token: str | None = None, max_results: int | None = None,
    ) -> dict[str, Any]:
        sub = substrate_for(ws)
        filters: list[dict[str, Any]] = [
            {"term": {"kind": "memory_extract"}}, {"term": {"store_id": sub.store_id}},
        ]
        if actor_id:
            filters.append({"term": {"actor_id": actor_id}})
        if session_id:
            filters.append({"term": {"session_id": session_id}})
        if strategy_id:
            filters.append({"term": {"strategy_ids": strategy_id}})
        if status:
            filters.append({"term": {"status": status}})
        from_ = int(next_token or 0)
        size = max_results or RECORD_PAGE
        body = {
            "query": {"bool": {"filter": filters}},
            "sort": [{"created_at": {"order": "desc"}}],
            "from": from_, "size": size,
        }
        result = self._os(ws).search(naming.jobs_index(), body)
        hits = result["hits"]["hits"]
        total = result["hits"]["total"]["value"]
        items = [dict(h["_source"]) for h in hits]
        next_tok = str(from_ + size) if from_ + size < total else None
        return {"items": items, "next_token": next_tok}

    def session_memory_summary(
        self, ws: WorkspaceContext, actor_id: str, session_id: str,
        memory_id: str | None = None,
    ) -> dict[str, Any]:
        sub = substrate_for(ws)
        store_id = memory_id or sub.store_id
        if store_id != sub.store_id:
            self._store(ws, store_id)  # 404 memory.not_found when unknown to this workspace
        s3 = self._s3(ws)
        prefix = f"{sub.prefix}/{store_id}/{actor_id}/session_{session_id}/events/"
        cutoff = self._now() - timedelta(days=self._expiry_days(ws, store_id))
        event_count = 0
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": sub.bucket, "Prefix": prefix, "MaxKeys": 1000}
            if token:
                kwargs["ContinuationToken"] = token
            page = s3.list_objects_v2(**kwargs)
            for c in page.get("Contents", []):
                ts = core.parse_event_key(c["Key"][len(prefix):])
                if ts is None or ts >= cutoff:
                    event_count += 1
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")

        namespaces = [
            f"/facts/{actor_id}", f"/preferences/{actor_id}", f"/summaries/{actor_id}/{session_id}",
        ]
        os_client = self._os(ws)
        records = []
        visibility = self._visibility_filters(ws, store_id)  # r14-a: the rail hides superseded
        for namespace in namespaces:
            body = {
                "query": {"bool": {"filter": [
                    {"term": {"store_id": store_id}}, {"term": {"namespace": namespace}},
                    *visibility,
                ]}},
                "sort": [{"created_at": {"order": "desc"}}],
                "size": RAIL_RECORDS_PER_NAMESPACE,
            }
            result = os_client.search(naming.memory_index(), body)
            for h in result["hits"]["hits"]:
                text, _ = decode_record_text(h["_source"].get("text") or "")
                records.append({"namespace": namespace, "text": text})
        return {"event_count": event_count, "records": records}

    def memory_overview(self, ws: WorkspaceContext) -> dict[str, Any]:
        if not has_substrate(ws):
            return {
                "configured": False, "memory": None, "strategies": [], "actor_count": 0,
                "actor_count_truncated": False, "other_memories": [],
            }
        sub = substrate_for(ws)
        store = self._store(ws, sub.store_id)
        strategies = self.get_strategies(ws)
        actors_page = self.list_actors(ws, max_results=ACTOR_PAGE)
        db = SessionLocal()
        try:
            rows = (
                db.query(MemoryStore)
                .filter(MemoryStore.workspace_id == ws.id)
                .order_by(MemoryStore.created_at)
                .all()
            )
        finally:
            db.close()
        other = [{
            "id": sub.store_id, "arn": None, "status": "ACTIVE", "created_at": None,
            "updated_at": None, "is_platform": True,
        }]
        for row in rows:
            if row.memory_id == sub.store_id:
                continue
            other.append({
                "id": row.memory_id, "arn": None, "status": row.status,
                "created_at": _iso(row.created_at), "updated_at": _iso(row.updated_at),
                "is_platform": False,
            })
        return {
            "configured": True,
            "memory": {
                "id": sub.store_id, "arn": None, "name": store["name"],
                "description": store["description"], "status": store["status"],
                "failure_reason": None, "event_expiry_days": store["event_expiry_days"],
                "encryption_key_arn": None, "execution_role_arn": None,
                "created_at": store["created_at"], "updated_at": store["updated_at"],
            },
            "strategies": strategies,
            "actor_count": len(actors_page["items"]),
            "actor_count_truncated": bool(actors_page["next_token"]),
            "other_memories": other,
        }
