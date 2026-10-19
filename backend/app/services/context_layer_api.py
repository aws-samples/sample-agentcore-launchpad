"""COA REST client — the backend's reads of a member's COA context, as that member.

One API Gateway REST API fronts COA's control plane and data layer (``ApiEndpoint``
of ``<prefix>-<env>-api``). Every request carries the member's own COA access token
(``context_layer_tokens``), so COA's grants decide what comes back. Redirects are
never followed (a redirect would re-send the bearer elsewhere), and an error names
the HTTP status only — never the token or the response body.

``call_as_member`` owns the error contract (parent design §3): no consent ⇒
``context_layer.authorization_required`` with the URL; COA 401 ⇒ the broker drops
its token and the next fetch forces consent (``context_layer.reauthorize``); COA 403
⇒ ``context_layer.forbidden`` (no grant — do not retry); COA 404 ⇒
``context_layer.coa_not_found``; 429, 5xx or timeout ⇒ ``context_layer.upstream``.
An error may carry COA's own message (``detail.coa_message``, at most 200
characters) — never the response body beyond it.

Reads come back as the allowlisted projections in ``schemas/context_layer.py``.
"""

import logging
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError, aws_error_code
from app.schemas.context_layer import (
    MetricOut,
    NamespaceOut,
    OntologyClassOut,
    OntologyOut,
    SourceOut,
    text,
)
from app.services import context_layer
from app.services import context_layer_tokens as tokens
from app.services.context_layer import ContextLayer
from app.services.workspace import WorkspaceContext

logger = logging.getLogger("launchpad.context_layer.api")

# COA's REST data-layer Lambda caps at 29 s; the control plane answers far sooner.
TIMEOUT_S = 30.0
MAX_PAGES = 20
PAGE_SIZE = 100
# COA's own error message, as echoed in an error's detail
COA_MESSAGE_MAX = 200
# The ontology tab: overviews read per namespace (concurrently) and classes per ontology.
MAX_OVERVIEWS = 8
CLASSES_PER_ONTOLOGY = 200
OVERVIEW_WORKERS = 4
# COA namespace ids are UUIDs (``ContextLayerRef``); nothing else is put in a path.
NAMESPACE_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class CoaUnauthorized(Exception):
    """COA answered 401: the token is no longer accepted."""


def _transport() -> httpx.BaseTransport | None:
    """Seam for tests (an ``httpx.MockTransport``); None = the real network."""
    return None


def coa_message(response: httpx.Response) -> str | None:
    """COA's error message (``message`` / ``error`` of a JSON body), bounded."""
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    return text(body.get("message") or body.get("error"), COA_MESSAGE_MAX)


def _get(layer: ContextLayer, token: str, path: str, params: dict[str, Any] | None = None) -> Any:
    base = layer.api_endpoint.rstrip("/")
    parsed = urlparse(base)
    # discovery only stores an https stack output; re-checked here because this is
    # where the member's bearer leaves the process
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        raise AppError(
            "context_layer.upstream", "the connected COA deployment has no https REST endpoint",
            status_code=502,
        )
    url = f"{base}/{path.lstrip('/')}"
    try:
        with httpx.Client(
            timeout=TIMEOUT_S, follow_redirects=False, transport=_transport()
        ) as client:
            response = client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
    except httpx.TimeoutException as exc:
        raise AppError(
            "context_layer.upstream", "COA did not answer in time", {"status": None},
            status_code=504,
        ) from exc
    except httpx.HTTPError as exc:
        raise AppError(
            "context_layer.upstream", f"COA is unreachable ({type(exc).__name__})",
            {"status": None}, status_code=502,
        ) from exc
    status = response.status_code
    if status == 401:
        raise CoaUnauthorized()
    if status == 403:
        raise AppError(
            "context_layer.forbidden",
            "COA refused this read: you have no grant for it in COA",
            {"status": 403, "coa_message": coa_message(response)},
            status_code=403,
        )
    if status == 404:
        raise NotFoundError(
            "context_layer.coa_not_found",
            "COA has no such item, or does not show it to you",
            {"status": 404, "coa_message": coa_message(response)},
        )
    if status >= 300:
        logger.warning("COA GET %s answered HTTP %s", path, status)
        raise AppError(
            "context_layer.upstream",
            f"COA answered HTTP {status}",
            {"status": status, "coa_message": coa_message(response)},
            status_code=502,
        )
    try:
        return response.json()
    except ValueError as exc:
        raise AppError(
            "context_layer.upstream", "COA answered with a body that is not JSON",
            {"status": status}, status_code=502,
        ) from exc


def call_as_member[T](
    db: Session,
    workspace: WorkspaceContext,
    username: str,
    call: Callable[[ContextLayer, str], T],
) -> T:
    """Run ``call(layer, token)`` with the member's COA token (see module docstring)."""
    grant = tokens.user_token(db, workspace, username)
    if isinstance(grant, tokens.AuthNeeded):
        raise AppError(
            "context_layer.authorization_required",
            "authorize Launchpad to read COA as you first",
            {"authorization_url": grant.url, "connection": grant.layer.connection},
            status_code=409,
        )
    try:
        return call(grant.layer, grant.value)
    except CoaUnauthorized:
        tokens.invalidate(workspace.id, username)
        # 409, not 401: the console treats any 401 as "your Launchpad session ended"
        raise AppError(
            "context_layer.reauthorize",
            "COA no longer accepts your authorization — authorize again",
            {"connection": grant.layer.connection},
            status_code=409,
        ) from None


def namespace_path(namespace_id: str, *rest: str) -> str:
    """``namespaces/<id>/…``. Refuses anything but a UUID, so no caller input can
    reshape the COA path."""
    if not NAMESPACE_ID_RE.fullmatch(namespace_id or ""):
        raise AppError(
            "context_layer.invalid_namespace",
            "a COA namespace id is a UUID",
            {"namespace_id": (namespace_id or "")[:64]},
            status_code=422,
        )
    return "/".join(["namespaces", quote(namespace_id, safe=""), *rest])


def _paged(
    layer: ContextLayer, token: str, path: str, key: str, params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Every ``key`` item of a ``nextToken``-paged COA list (bounded by MAX_PAGES)."""
    items: list[dict[str, Any]] = []
    next_token: str | None = None
    for _ in range(MAX_PAGES):
        query: dict[str, Any] = {"maxResults": PAGE_SIZE, **(params or {})}
        if next_token:
            query["nextToken"] = next_token
        body = _get(layer, token, path, query) or {}
        if not isinstance(body, dict):
            break
        items.extend(item for item in body.get(key) or [] if isinstance(item, dict))
        next_token = body.get("nextToken") or None
        if not next_token:
            break
    return items


def list_namespaces(layer: ContextLayer, token: str) -> list[dict[str, Any]]:
    """``GET /namespaces`` across pages: the namespaces COA lets this member see."""
    items = (NamespaceOut.from_coa(ns) for ns in _paged(layer, token, "namespaces", "namespaces"))
    return [ns.model_dump() for ns in items if ns.namespace_id]


def get_namespace(layer: ContextLayer, token: str, namespace_id: str) -> dict[str, Any]:
    """``GET /namespaces/{id}`` → ``{namespace: NamespaceDetail}``."""
    body = _get(layer, token, namespace_path(namespace_id)) or {}
    item = body.get("namespace") if isinstance(body, dict) else None
    if not isinstance(item, dict):
        item = body if isinstance(body, dict) else {}
    out = NamespaceOut.from_coa(item)
    if not out.namespace_id:
        out.namespace_id = namespace_id
    return out.model_dump()


def list_sources(layer: ContextLayer, token: str, namespace_id: str) -> list[dict[str, Any]]:
    """``GET /namespaces/{id}/sources`` (``items``, paged): every source type."""
    rows = _paged(layer, token, namespace_path(namespace_id, "sources"), "items")
    return [SourceOut.from_coa(row).model_dump() for row in rows]


def list_metrics(layer: ContextLayer, token: str, namespace_id: str) -> list[dict[str, Any]]:
    """``GET /namespaces/{id}/metrics`` (``metrics``, paged): the governed metrics."""
    rows = _paged(layer, token, namespace_path(namespace_id, "metrics"), "metrics")
    return [m.model_dump() for m in (MetricOut.from_coa(row) for row in rows) if m.name]


def list_ontologies(layer: ContextLayer, token: str, namespace_id: str) -> list[OntologyOut]:
    """``GET /namespaces/{id}/ontologies`` → ``{ontologies: [...]}`` (not paged)."""
    body = _get(layer, token, namespace_path(namespace_id, "ontologies")) or {}
    rows = body.get("ontologies") if isinstance(body, dict) else body
    items = [OntologyOut.from_coa(row) for row in rows or [] if isinstance(row, dict)]
    return [item for item in items if item.ontology_id]


def ontology_overview(
    layer: ContextLayer, token: str, namespace_id: str, ontology: OntologyOut,
) -> tuple[list[OntologyClassOut], int | None]:
    """``GET …/graph/ontology-overview?ontology_id=`` → (classes, total classes).

    The ontology id is an IRI (it carries ``#``), so it rides the query string."""
    body = _get(
        layer, token, namespace_path(namespace_id, "graph", "ontology-overview"),
        {"ontology_id": ontology.ontology_id, "limit": CLASSES_PER_ONTOLOGY},
    ) or {}
    if not isinstance(body, dict):
        return [], None
    classes = [
        OntologyClassOut.from_coa(item, ontology)
        for item in body.get("classes") or []
        if isinstance(item, dict) and item.get("uri")
    ]
    total = body.get("totalClasses", body.get("total_classes"))
    return classes, total if isinstance(total, int) and not isinstance(total, bool) else None


# induced and authored ontologies first: they describe this namespace's own data
_TYPE_ORDER = {"induced": 0, "user_created": 1, "foundational": 2}


def ontology(layer: ContextLayer, token: str, namespace_id: str) -> dict[str, Any]:
    """The Ontology tab: the namespace's ontologies and their classes.

    One overview per ontology (at most ``MAX_OVERVIEWS``, read concurrently, each
    capped at ``CLASSES_PER_ONTOLOGY`` classes). An overview COA could not answer
    (5xx, timeout) is listed in ``failed`` and the rest still show; a 401 or 403
    fails the whole read, as any other read does."""
    ontologies = sorted(
        list_ontologies(layer, token, namespace_id),
        key=lambda o: (_TYPE_ORDER.get((o.ontology_type or "").lower(), 3), o.title.lower()),
    )
    read = ontologies[:MAX_OVERVIEWS]

    def overview(item: OntologyOut) -> tuple[OntologyOut, Any]:
        try:
            return item, ontology_overview(layer, token, namespace_id, item)
        except AppError as exc:
            if exc.code in ("context_layer.upstream", "context_layer.coa_not_found"):
                return item, exc
            raise

    with ThreadPoolExecutor(max_workers=OVERVIEW_WORKERS) as pool:
        results = list(pool.map(overview, read))
    classes: list[dict[str, Any]] = []
    failed: list[str] = []
    truncated = len(ontologies) > len(read)
    for item, result in results:
        if isinstance(result, AppError):
            failed.append(item.ontology_id)
            continue
        found, total = result
        classes.extend(c.model_dump() for c in found)
        if (total is not None and total > len(found)) or len(found) >= CLASSES_PER_ONTOLOGY:
            truncated = True
    return {
        "ontologies": [o.model_dump() for o in ontologies],
        "classes": classes,
        "failed": failed,
        "truncated": truncated,
    }


# COA ``GET /system-health`` component keys → the console's names
_HEALTH_KEYS = {"dynamodb": "dynamodb", "neptuneKg": "neptune_kg", "opensearch": "opensearch"}
_INDUCTION_KEYS = {
    "graphStore": "graph_store",
    "vectorStore": "vector_store",
    "bedrockEmbeddings": "bedrock_embeddings",
    "descriptionLlm": "description_llm",
}


def system_health(layer: ContextLayer, token: str) -> dict[str, Any]:
    """COA ``GET /system-health`` (``ok`` / ``degraded`` / ``error: …`` per
    dependency), projected to named components; unknown keys are dropped."""
    body = _get(layer, token, "system-health") or {}
    if not isinstance(body, dict):
        body = {}
    components = [
        {"key": name, "status": text(body.get(key), COA_MESSAGE_MAX)}
        for key, name in _HEALTH_KEYS.items()
        if key in body
    ]
    induction = body.get("ontologyInduction")
    if isinstance(induction, dict):
        components.extend(
            {"key": name, "status": text(induction.get(key), COA_MESSAGE_MAX)}
            for key, name in _INDUCTION_KEYS.items()
            if key in induction
        )
    return {"status": text(body.get("status"), 64), "components": components}


def health(db: Session, workspace: WorkspaceContext, username: str) -> dict[str, Any]:
    """``GET /api/context-layer/health``: both runtimes' ``GetAgentRuntime`` status
    (IAM, COA region) plus COA's own ``/system-health`` read as the member.

    Without the member's consent COA is not called (``authorization_required``); a
    COA refusal or outage reads as ``coa_error {code, status}`` next to the runtime
    status, never as a failed request."""
    layer = context_layer.require(db, workspace.id)
    ctx = context_layer.coa_context(workspace, layer.coa_region)
    probe_errors: list[dict[str, str]] = []
    runtimes: dict[str, str | None] = {}
    for kind, key in context_layer.RUNTIME_KEYS.items():
        try:
            runtimes[kind] = context_layer.runtime_status(ctx, layer.resources[key])
        except (ClientError, BotoCoreError, AppError) as exc:
            code = (
                aws_error_code(exc) if isinstance(exc, ClientError)
                else getattr(exc, "code", None) or type(exc).__name__
            )
            probe_errors.append({"probe": f"{kind}_runtime", "code": str(code)})
            runtimes[kind] = None
    out: dict[str, Any] = {
        "runtimes": runtimes,
        "ready": all(value == "READY" for value in runtimes.values()),
        "authorization_required": False,
        "coa": None,
        "coa_error": None,
        "probe_errors": probe_errors,
    }
    mine = context_layer.my_authorization(db, workspace.id, username, layer.connection)
    if mine["status"] != "authorized":
        out["authorization_required"] = True
        return out
    try:
        out["coa"] = call_as_member(db, workspace, username, system_health)
    except AppError as exc:
        if exc.code == "context_layer.authorization_required":
            out["authorization_required"] = True
        else:
            status = exc.detail.get("status") if isinstance(exc.detail, dict) else None
            out["coa_error"] = {"code": exc.code, "status": status}
    return out
