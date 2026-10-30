"""Memory feature boundary.

The AWS AgentCore Memory implementation was removed for the CN build (spec C5). N6
registers a self-hosted backend with ``set_backend()``; until then every router
that needs one answers 503 ``feature.unavailable``. The pure helpers at the bottom
(actor scoping, record decoding) have no backend and are used by chat/agents.
"""

import json
from typing import Any, Protocol

from app.core.features import feature_unavailable
from app.services.workspace import WorkspaceContext


class MemoryBackend(Protocol):
    """What a memory backend must provide. One method per console/runtime read or
    write; every method takes the workspace it targets first."""

    # ── console reads (routers/memory.py) ──────────────────────────────────
    def memory_overview(self, workspace: WorkspaceContext) -> dict[str, Any]:
        """``GET /api/memory/overview`` — landing-view summary of the memory store."""
        ...

    def get_strategies(self, workspace: WorkspaceContext) -> list[dict[str, Any]]:
        """Long-term strategies the store runs (used by ``resolve_namespaces``)."""
        ...

    def list_actors(
        self, workspace: WorkspaceContext, next_token: str | None = None,
        max_results: int | None = None,
    ) -> dict[str, Any]:
        """``GET /api/memory/actors`` — page of ``{items, next_token}``; each item carries
        the ``decode_actor()`` fields (``actor_id``, ``agent_id``, ``human_actor``, ``scoped``)."""
        ...

    def list_sessions(
        self, workspace: WorkspaceContext, actor_id: str, next_token: str | None = None,
        max_results: int | None = None,
    ) -> dict[str, Any]:
        """``GET /api/memory/sessions`` — page of ``{items: [{session_id, …}], next_token}``."""
        ...

    def list_events(
        self, workspace: WorkspaceContext, actor_id: str, session_id: str,
        include_payloads: bool = True, next_token: str | None = None,
        max_results: int | None = None,
    ) -> dict[str, Any]:
        """``GET /api/memory/events`` — page of short-term events (``{items, next_token}``);
        also read by ``services/observability.session_transcript``."""
        ...

    def resolve_namespaces(
        self, workspace: WorkspaceContext, actor_id: str,
        strategies: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /api/memory/namespaces`` — ``[{strategy_id, namespace, resolvable}, …]``
        for one actor; also used by ``/records`` and ``/records/search`` to derive a
        namespace from ``(actor_id, strategy_id)``."""
        ...

    def list_records(
        self, workspace: WorkspaceContext, namespace: str, strategy_id: str | None = None,
        next_token: str | None = None, max_results: int | None = None,
        include_superseded: bool = False,
    ) -> dict[str, Any]:
        """``GET /api/memory/records`` — page of long-term records in one namespace. Records
        superseded / soft-deleted / expired by the merge strategy (r14-a) are hidden unless
        ``include_superseded`` is set; each item carries the op-history fields."""
        ...

    def search_records(
        self, workspace: WorkspaceContext, namespace: str, query: str, top_k: int = 5,
        strategy_id: str | None = None,
    ) -> dict[str, Any]:
        """``POST /api/memory/records/search`` — semantic retrieval over one namespace."""
        ...

    def delete_record(
        self, workspace: WorkspaceContext, record_id: str, actor_id: str,
    ) -> dict[str, Any]:
        """``DELETE /api/memory/records/{record_id}?actor_id=…`` (N10v2-bug6) — remove one
        long-term record. Scoped twice before anything is deleted: the record must belong
        to this workspace's store AND to ``actor_id``; anything else is 404
        ``memory.not_found`` (a record id is never confirmed across actors). Returns
        ``{record_id, actor_id, namespace, strategy_id, deleted: True}``."""
        ...

    def list_extraction_jobs(
        self, workspace: WorkspaceContext, actor_id: str | None = None,
        session_id: str | None = None, strategy_id: str | None = None,
        status: str | None = None, next_token: str | None = None,
        max_results: int | None = None,
    ) -> dict[str, Any]:
        """``GET /api/memory/extraction-jobs`` — page of long-term extraction jobs."""
        ...

    # ── runtime/data plane (chat, invoke) ──────────────────────────────────
    def create_turn_event(
        self, workspace: WorkspaceContext, actor_id: str, session_id: str,
        prompt: str, answer: str,
    ) -> dict[str, Any]:
        """Record one USER/ASSISTANT turn as a short-term event (chat/invoke write path)."""
        ...

    def retrieve_records(
        self, workspace: WorkspaceContext, namespace: str, query: str, top_k: int = 3,
    ) -> list[dict]:
        """Long-term records relevant to ``query`` (runtime prompt enrichment)."""
        ...

    def session_memory_summary(
        self, workspace: WorkspaceContext, actor_id: str, session_id: str,
        memory_id: str | None = None,
    ) -> dict[str, Any]:
        """``GET /api/chat/{agent_id}/memory`` — the chat rail's short-/long-term summary
        for one (scoped actor, session); ``memory_id`` is the spec-pinned store, if any."""
        ...

    # ── resource lifecycle (routers/memory_resources.py) ───────────────────
    def list_memory_resources(self, workspace: WorkspaceContext) -> dict[str, Any]:
        """``GET /api/memory/resources`` — ``{items: [{id, …}]}`` of memory stores."""
        ...

    def get_memory_resource(self, workspace: WorkspaceContext, memory_id: str) -> dict[str, Any]:
        """``GET /api/memory/resources/{memory_id}`` (and the admin ``/adopt`` probe)."""
        ...

    def create_memory_resource(
        self, workspace: WorkspaceContext, name: str, description: str,
        event_expiry_days: int, strategies: list[str],
        namespace_keys: list[dict[str, Any]] | None = None,
        merge: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``POST /api/memory/resources`` — returns the created store detail (``id`` set);
        ``merge`` = the merge strategy v2 knobs the caller set (r14-a)."""
        ...

    def update_memory_resource(
        self, workspace: WorkspaceContext, memory_id: str, *,
        description: str | None = None, event_expiry_days: int | None = None,
        merge: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``PUT /api/memory/resources/{memory_id}`` — returns the refreshed detail; ``merge``
        is a partial patch of the merge strategy v2 knobs (r14-a)."""
        ...

    def delete_memory_resource(self, workspace: WorkspaceContext, memory_id: str) -> dict[str, Any]:
        """``DELETE /api/memory/resources/{memory_id}``."""
        ...


_backend: MemoryBackend | None = None


def set_backend(backend: MemoryBackend | None) -> None:
    global _backend
    _backend = backend


def backend_or_none() -> MemoryBackend | None:
    return _backend


def get_backend() -> MemoryBackend:
    if _backend is None:
        raise feature_unavailable("memory")
    return _backend


# ── pure helpers (no backend) ───────────────────────────────────────────────
SCOPE_SEP = "__"


def scoped_actor(agent_id: str, base_actor: str = "river") -> str:
    """`<agent>__<human>`: the actor id both short- and long-term memory are keyed on."""
    return f"{agent_id}{SCOPE_SEP}{base_actor}"


def spec_memory_id(agent_spec: dict | None) -> str | None:
    memory = (agent_spec or {}).get("memory") or {}
    value = memory.get("memory_id")
    return str(value) if value else None


def memory_id_or_none(workspace: WorkspaceContext) -> str | None:
    value = (workspace.resources or {}).get("memory_id")
    return str(value) if value else None


def decode_actor(actor_id: str) -> dict[str, Any]:
    """Split a platform-scoped actor id back into (agent id, human actor).

    ``scoped_actor()`` builds ``<agent_id>__<human>``. The human part may itself
    contain the separator (actor ids are free-form), so split on the *first*
    occurrence only. An actor id written by something other than this platform
    has no separator — report it as unscoped rather than inventing an agent id.
    """
    agent_id, sep, human = actor_id.partition(SCOPE_SEP)
    if not sep:
        return {
            "actor_id": actor_id,
            "agent_id": None,
            "human_actor": actor_id,
            "scoped": False,
        }
    return {
        "actor_id": actor_id,
        "agent_id": agent_id,
        "human_actor": human,
        "scoped": True,
    }


# Strategies do not agree on a record payload shape: SEMANTIC stores prose in
# content.text, while USER_PREFERENCE (and SUMMARIZATION) store a structured
# JSON object there. Showing that object verbatim is unreadable, so pick the
# first present display field — in decreasing specificity — and hand the parsed
# object back so callers can render the rest.
_RECORD_DISPLAY_KEYS = (
    "preference",
    "summary",
    "fact",
    "context",
    "text",
)


def decode_record_text(raw: str) -> tuple[str, dict | None]:
    """Return (display text, parsed object or None) for one memory record."""
    text = (raw or "").strip()
    if not text.startswith("{"):
        return raw, None
    try:
        parsed = json.loads(text)
    except ValueError:
        return raw, None
    if not isinstance(parsed, dict):
        return raw, None
    for key in _RECORD_DISPLAY_KEYS:
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            return value, parsed
    # a structured record with no known display field: keep the raw JSON rather
    # than inventing a summary, but still expose the parsed fields
    return raw, parsed
