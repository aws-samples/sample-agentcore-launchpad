"""The embedding reindex guard (D21 M1-d; plan §6, interface (4)).

Registered into `services/model_registry.set_embedding_guard` from `app/main.py`; the
registry calls it before flipping an embedding row to default and before disabling an
embedding row. Vectors written by one model are not comparable with another's (even at
the same dimension), and an index mapping cannot change its ``dimension`` in place, so the
rule is: the active embedding may change only while EVERY vector index this hub serves is
empty. Otherwise 409 ``embedding.reindex_required`` naming the indices and their counts —
the reindex job that lifts it is the §12.1-2 follow-up.

Counting: for every workspace with the self-hosted substrate (shared domain, one count per
endpoint) the guard lists ``_cat/indices`` for `naming.index_patterns()` — this
environment's ``launchpad[-env]-memory``, ``-kb-docs`` and every ``-kb-<id>`` — and skips
the jobs index (no vectors). No AWS call happens in tests: the OpenSearch client is an
injectable factory.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.core import naming
from app.core.errors import AppError
from app.models.ledger import Workspace
from app.schemas.agent import active_embedding
from app.services import aws_clients, model_registry, opensearch_retry
from app.services import opensearch_http as core
from app.services.memory_selfhosted import has_substrate, os_ctx, substrate_for
from app.services.workspace import WorkspaceContext, workspace_context

logger = logging.getLogger("launchpad.embeddings")

REINDEX_REQUIRED = "embedding.reindex_required"

OpenSearchFactory = Callable[[WorkspaceContext, str], Any]


def _default_factory(ctx: WorkspaceContext, endpoint: str) -> Any:
    return aws_clients.opensearch_http(
        os_ctx(ctx), endpoint,
        attempts=opensearch_retry.INTERACTIVE_ATTEMPTS,
        read_timeout=opensearch_retry.INTERACTIVE_READ_TIMEOUT_SECONDS,
    )


def _is_vector_index(name: str) -> bool:
    """Everything `naming.index_name` produces except the jobs index."""
    return name != naming.jobs_index() and name.startswith("launchpad-")


def index_doc_counts(db: Session, *, factory: OpenSearchFactory | None = None
                     ) -> list[dict[str, Any]]:
    """``[{name, docs}]`` for every vector index of every substrate this hub serves."""
    factory = factory or _default_factory
    seen_endpoints: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in db.query(Workspace).order_by(Workspace.id).all():
        ctx = workspace_context(row)
        if not has_substrate(ctx):
            continue
        endpoint = substrate_for(ctx).endpoint
        if endpoint in seen_endpoints:
            continue
        seen_endpoints.add(endpoint)
        http = factory(ctx, endpoint)
        for pattern in naming.index_patterns():
            try:
                rows = http.request("GET", f"/_cat/indices/{pattern}",
                                    params={"format": "json", "h": "index,docs.count"})
            except core.OpenSearchError as exc:
                if exc.status == 404:  # no index matches the pattern yet
                    continue
                raise
            for entry in rows if isinstance(rows, list) else []:
                name = str(entry.get("index") or "")
                if not _is_vector_index(name):
                    continue
                docs = int(entry.get("docs.count") or 0)
                out.append({"name": name, "docs": docs})
    return out


def guard(db: Session, row: Any, *, factory: OpenSearchFactory | None = None) -> None:
    """Raise 409 `embedding.reindex_required` unless every vector index is empty."""
    counts = index_doc_counts(db, factory=factory)
    non_empty = [c for c in counts if c["docs"] > 0]
    if not non_empty:
        return
    active = active_embedding()
    raise AppError(
        REINDEX_REQUIRED,
        "the embedding model cannot change while vector indices hold documents — "
        + ", ".join(f"{c['name']} ({c['docs']})" for c in non_empty)
        + "; delete the memory records / KB documents (or run a reindex) first",
        {
            "indices": non_empty,
            "dimension_from": active.embedding_dimension if active else None,
            "dimension_to": getattr(row, "embedding_dimension", None),
            "model_from": active.model_id if active else None,
            "model_to": getattr(row, "model_id", None),
        },
        status_code=409,
    )


def install() -> None:
    """Plug the guard into the registry (called once from `app/main.py`)."""
    model_registry.set_embedding_guard(guard)
