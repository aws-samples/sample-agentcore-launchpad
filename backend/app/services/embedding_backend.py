"""Embeddings for the self-hosted Memory / Knowledge-Base substrate (D21 M1-d; plan §6).

The ACTIVE embedding row of the model registry (`schemas.agent.active_embedding`: Titan
Text Embeddings V2 for the built-in table) decides three things the hub does:

* which embedder a request-thread query uses (`build_embedder`) — `TitanEmbedder` from the
  mirrored core for a Bedrock row, `OpenAIEmbedder` (``POST {base_url}/embeddings``) for an
  ``openai_compatible`` row, the hub resolving that provider's key through
  `aws_clients.provider_api_key` (M1-b) — never a key in the ledger or a log line;
* the k-NN ``dimension`` a NEW vector index is created with (`vector_index_body`): the
  mirrored core keeps Titan's 1024 as its constant, this module rewrites the mapping from
  the row at index creation. Existing indices keep their mapping — which is why the
  registry refuses an embedding change while any vector index holds documents
  (`services/embedding_guard.py`, 409 `embedding.reindex_required`);
* the ``embedding`` descriptor every SQS message to the extractor Lambda carries
  (`message_block`), so the Lambda embeds with the SAME row the hub queries with and never
  needs a redeploy per model change (`lambdas/memory_extractor/handler.py::embedder_for`).

`app/services/opensearch_http.py` is the byte-identical core shared with the Lambda and the
runtime template (R15) and is deliberately NOT touched: everything registry-aware lives here.
Generated runtimes' own memory / KB retrieval (the third mirror) still embeds with Titan —
the guard keeps that consistent by allowing a row change only on an empty substrate.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

import httpx

from app.core.config import get_settings
from app.schemas.agent import active_embedding, catalog_version, model_row
from app.schemas.model_catalog import (
    DEFAULT_MODEL_CREDENTIAL,
    PROVIDER_OPENAI_COMPATIBLE,
    ModelRow,
)
from app.services import aws_clients
from app.services import opensearch_http as core

logger = logging.getLogger("launchpad.embeddings")

KIND_OPENAI_COMPATIBLE = PROVIDER_OPENAI_COMPATIBLE
DEFAULT_TIMEOUT_S = 30.0


class EmbeddingDimensionMismatch(RuntimeError):
    """The endpoint returned a vector of another length than the catalog row declares —
    writing it would corrupt the index (every vector in it must share one dimension)."""


def provider_api_key(provider_name: str) -> str:
    """The hub's own copy of ``provider_name``'s key: M1-b's cached SSM-or-env reader when
    present, else the pre-D21 environment-only reader. Never logged, never stored."""
    reader = getattr(aws_clients, "provider_api_key", None)
    if reader is not None:
        return str(reader(provider_name))
    return str(aws_clients.require_provider_api_key(provider_name))


class OpenAIEmbedder:
    """``POST {base_url}/embeddings`` on an OpenAI-compatible endpoint (SiliconFlow …).

    Same ``embed(text) -> list[float]`` contract as the core's `TitanEmbedder`; the key is
    a client argument only. ``dimension`` (the catalog row's) is enforced on every vector:
    a mismatch raises `EmbeddingDimensionMismatch` rather than writing a vector the index
    mapping cannot hold. ``transport`` is the httpx test seam (``MockTransport``).
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        api_model_name: str,
        dimension: int | None = None,
        max_chars: int | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        transport: Any = None,
    ) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.model_id = api_model_name
        self.api_model_name = api_model_name
        self.dimensions = int(dimension) if dimension else None
        self.max_chars = int(max_chars) if max_chars else core.EMBED_MAX_CHARS
        self._client = httpx.Client(
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            transport=transport,
        )

    def embed(self, text: str) -> list[float]:
        response = self._client.post(
            f"{self.base_url}/embeddings",
            json={"model": self.api_model_name, "input": text[: self.max_chars]},
        )
        if response.status_code >= 300:
            # the vendor body may echo the request, never the Authorization header; keep it
            # short and never include our own headers
            raise RuntimeError(
                f"embeddings endpoint {self.base_url} answered {response.status_code}: "
                f"{response.text[:300]}"
            )
        data = response.json()
        try:
            vector = [float(x) for x in data["data"][0]["embedding"]]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"embeddings endpoint {self.base_url} returned no data[0].embedding"
            ) from exc
        if self.dimensions and len(vector) != self.dimensions:
            raise EmbeddingDimensionMismatch(
                f"{self.api_model_name} returned a {len(vector)}-dimensional vector; the "
                f"catalog row declares {self.dimensions} — fix the row's embedding_dimension "
                "(probe it) before any document is written"
            )
        return vector


# ── the active row as data ────────────────────────────────────────────────────────────
def active_row() -> ModelRow | None:
    return active_embedding()


def embedding_dimension(row: ModelRow | None = None) -> int:
    """The k-NN dimension new indices are created with: the active row's, else the core's
    Titan constant (an empty registry, or a row saved without a probed dimension)."""
    row = row if row is not None else active_row()
    if row is not None and row.embedding_dimension:
        return int(row.embedding_dimension)
    return core.EMBED_DIMENSIONS


def message_block(row: ModelRow | None = None) -> dict[str, Any]:
    """The ``embedding`` descriptor an SQS message carries to the extractor Lambda (plan §6):
    ``{model_id, kind, base_url, api_model_name, credential_provider, dimension, max_chars}``
    plus the additive ``provider``, ``model_region`` and ``catalog_version``. Built from
    the snapshot — the Lambda never imports the hub package."""
    row = row if row is not None else active_row()
    if row is None:
        return {
            "model_id": core.EMBED_MODEL_ID, "kind": "bedrock", "base_url": None,
            "api_model_name": core.EMBED_MODEL_ID,
            "credential_provider": DEFAULT_MODEL_CREDENTIAL,
            "dimension": core.EMBED_DIMENSIONS, "max_chars": core.EMBED_MAX_CHARS,
            "provider": "bedrock", "model_region": get_settings().model_region,
            "catalog_version": int(catalog_version()),
        }
    return {
        "model_id": row.model_id,
        "kind": row.provider_kind,
        "base_url": row.base_url,
        "api_model_name": row.api_model_name,
        "credential_provider": row.credential_provider,
        "dimension": embedding_dimension(row),
        "max_chars": int(row.embedding_max_chars or core.EMBED_MAX_CHARS),
        "provider": row.provider,
        "model_region": get_settings().model_region,
        "catalog_version": int(catalog_version()),
    }


def extract_model_block() -> dict[str, Any] | None:
    """N17-obs252 (user ruling 2026-10-22 「需要都读模型角色来确定模型」): the ``extract_model``
    descriptor a `memory_extract` message carries to the extractor Lambda — the catalog row of
    the `memory_extract` model role (`Settings.memory_extract_model_id`, r17-am), in the
    `message_block` shape minus the embedding fields: ``{model_id, kind, base_url,
    api_model_name, credential_provider, provider, model_region, catalog_version,
    temperature}``. ``None`` — the block is omitted — when the role field is absent (a build
    before r17-am's Settings field) or names no catalog row; the Lambda then falls back to its
    own `LAUNCHPAD_EXTRACT_MODEL_ID`, so messages in flight across the update keep working.

    ``temperature`` (F-LOAD-03, N17-obs534, 2026-10-30) is the row's capability
    (`ModelRow.temperature`: Kimi K3 False, GLM 5.3 True; an OpenAI-compatible row its trait,
    True when the trait is absent) — the Lambda sends `temperature: 0` only when it is True,
    so a role pointed at Kimi K3 no longer fails every extraction with "This model doesn't
    support the temperature field". The same block is baked into every memory-enabled
    Runtime's environment at deploy (`LAUNCHPAD_MEMORY_EXTRACT_MODEL`, F-LOAD-02) so the
    Runtime's own `memory_extract` messages carry it too."""
    settings = get_settings()
    model_id = getattr(settings, "memory_extract_model_id", None)
    if not model_id:
        return None
    row = model_row(str(model_id))
    if row is None:
        return None
    return {
        "model_id": row.model_id,
        "kind": row.provider_kind,
        "base_url": row.base_url,
        "api_model_name": row.api_model_name,
        "credential_provider": row.credential_provider,
        "provider": row.provider,
        "model_region": settings.model_region,
        "catalog_version": int(catalog_version()),
        "temperature": bool(row.temperature),
    }


def vector_index_body(body: dict[str, Any], dimension: int | None = None) -> dict[str, Any]:
    """``body`` (one of the core's ``*_index_body()`` dicts) with every ``knn_vector`` field's
    ``dimension`` set from the active row — the core keeps Titan's 1024, this is applied at
    INDEX CREATION only. A body without a vector field is returned as an equal copy."""
    dimension = int(dimension or embedding_dimension())
    out = copy.deepcopy(body)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "knn_vector" and "dimension" in node:
                node["dimension"] = dimension
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(out)
    return out


def build_embedder(
    descriptor: dict[str, Any] | None = None,
    *,
    bedrock_runtime: Any = None,
    api_key: str | None = None,
    transport: Any = None,
) -> Any:
    """The hub-side embedder for ``descriptor`` (default: the active row's `message_block`):
    `OpenAIEmbedder` for an ``openai_compatible`` row — key from `provider_api_key` unless
    injected — else the core's `TitanEmbedder` on the Global Bedrock bearer client."""
    descriptor = descriptor or message_block()
    if descriptor.get("kind") == KIND_OPENAI_COMPATIBLE:
        return OpenAIEmbedder(
            base_url=str(descriptor["base_url"]),
            api_key=api_key or provider_api_key(str(descriptor.get("provider") or "")),
            api_model_name=str(descriptor["api_model_name"]),
            dimension=descriptor.get("dimension"),
            max_chars=descriptor.get("max_chars"),
            transport=transport,
        )
    client = bedrock_runtime or aws_clients.global_bedrock_runtime_client(
        get_settings().model_region
    )
    return core.TitanEmbedder(
        client,
        str(descriptor.get("model_id") or core.EMBED_MODEL_ID),
        int(descriptor.get("dimension") or core.EMBED_DIMENSIONS),
    )
