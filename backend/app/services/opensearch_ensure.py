"""Race-safe index ensure for the hub's two index creators (WB-007).

`opensearch_http.OpenSearchHttp.ensure_index` is a non-atomic ``HEAD /<index>`` → ``PUT
/<index>``. When two creators run at once (or a HEAD answers from a stale view of the
cluster), the loser's PUT fails with ``400 resource_already_exists_exception`` — and until
this module the knowledge-base create path wrote the KB ``FAILED`` for an index that does
exist and is perfectly usable.

The core module is mirrored byte-for-byte into the extractor Lambda and the agent runtime
template (ruling R15), and neither of those ever *creates* an index: the hub's
`knowledge_selfhosted.create_kb` and `memory_bootstrap.ensure_memory_substrate` are the
only two callers. So, exactly like `opensearch_retry.RetryingHttp`, the fix lives hub-side
and the core stays untouched: `ensure_index` here delegates to the core's `ensure_index`
and only on the already-exists answer reads the live mapping/settings back and decides
whether the index that won the race is the index the caller asked for.

**Compatible** (→ ``False``, "ensured, not created") is defined conservatively:

- every property the requested mapping names exists live with the same ``type``
  (a missing field would be dynamically mapped on first write — a ``keyword`` filter
  field coming back as ``text`` silently breaks term filters, so "missing" is a conflict);
- a ``knn_vector`` field matches ``dimension`` and every ``method.*`` key the request
  carries (``engine``, ``space_type``, ``name``) — a different dimension rejects every
  bulk, a different space/engine changes what ``_score`` and the recall cutoffs mean;
- an ``enabled`` flag the request carries (``metadata: {type: object, enabled: false}``)
  matches live;
- a requested ``index.knn: true`` is live ``true``.

Extra live fields are allowed (dynamic mapping adds them), and ``number_of_shards`` /
``number_of_replicas`` are capacity, not semantics, so they are not compared. Anything
else raises `IndexIncompatible` — an `OpenSearchError` subclass, so the existing
``except core.OpenSearchError`` in both callers keeps catching it (the KB create path
writes it as the ``FAILED`` reason; the bootstrap stage fails with the problem list).
"""

from typing import Any

from app.services import opensearch_http as core

ALREADY_EXISTS = "resource_already_exists_exception"
# `method.*` keys a `knn_vector` request pins; compared only when the request carries them
_KNN_METHOD_KEYS = ("engine", "space_type", "name")


class IndexIncompatible(core.OpenSearchError):
    """The index already exists but its live mapping/settings conflict with the body the
    caller asked for. ``.name`` is the index, ``.problems`` the human-readable conflicts."""

    def __init__(self, name: str, problems: list[str]) -> None:
        self.name = name
        self.problems = list(problems)
        super().__init__(
            409, f"index {name!r} exists with an incompatible mapping: " + "; ".join(problems)
        )


def is_already_exists(exc: BaseException) -> bool:
    """The core raises ``OpenSearchError(400, <body>)`` for the loser of a create race; the
    body's first error type is ``resource_already_exists_exception``."""
    return (
        isinstance(exc, core.OpenSearchError)
        and exc.status == 400
        and ALREADY_EXISTS in str(exc)
    )


def _norm(value: Any) -> str:
    return str(value).strip().lower()


def _live_index_settings(live: dict[str, Any]) -> dict[str, Any]:
    """``settings.index`` as OpenSearch returns it (nested) — tolerating the dotted form."""
    settings = live.get("settings") or {}
    nested = dict(settings.get("index") or {})
    for key, value in settings.items():
        if isinstance(key, str) and key.startswith("index."):
            nested.setdefault(key[len("index."):], value)
    return nested


def _requested_knn(settings: dict[str, Any]) -> Any:
    if "index.knn" in settings:
        return settings["index.knn"]
    return (settings.get("index") or {}).get("knn")


def index_compatibility_problems(requested: dict[str, Any], live: dict[str, Any]) -> list[str]:
    """The conflicts between a requested index body and one index's live ``GET /<index>``
    entry (the value under the index name). Empty means compatible."""
    problems: list[str] = []
    want_props: dict[str, Any] = (requested.get("mappings") or {}).get("properties") or {}
    live_props: dict[str, Any] = (live.get("mappings") or {}).get("properties") or {}

    for field, want in want_props.items():
        have = live_props.get(field)
        if have is None:
            problems.append(f"field {field!r} missing")
            continue
        want_type = want.get("type", "object")
        have_type = have.get("type", "object")
        if _norm(want_type) != _norm(have_type):
            problems.append(f"field {field!r} type {have_type!r} != {want_type!r}")
            continue
        if _norm(want_type) == "knn_vector":
            if "dimension" in want and _norm(want["dimension"]) != _norm(have.get("dimension")):
                problems.append(
                    f"field {field!r} dimension {have.get('dimension')!r} != {want['dimension']!r}"
                )
            want_method = want.get("method") or {}
            have_method = have.get("method") or {}
            for key in _KNN_METHOD_KEYS:
                if key in want_method and _norm(want_method[key]) != _norm(have_method.get(key)):
                    problems.append(
                        f"field {field!r} method.{key} {have_method.get(key)!r} "
                        f"!= {want_method[key]!r}"
                    )
        if "enabled" in want and _norm(want["enabled"]) != _norm(have.get("enabled", True)):
            problems.append(
                f"field {field!r} enabled {have.get('enabled', True)!r} != {want['enabled']!r}"
            )

    want_knn = _requested_knn(requested.get("settings") or {})
    if want_knn is not None and _norm(want_knn) == "true":
        have_knn = _live_index_settings(live).get("knn")
        if _norm(have_knn) != "true":
            problems.append(f"settings index.knn {have_knn!r} != True")
    return problems


def _live_entry(name: str, response: dict[str, Any]) -> dict[str, Any]:
    """``GET /<index>`` keys the body by the concrete index name; when `name` was an alias
    the single entry is the backing index."""
    if name in response:
        return dict(response[name] or {})
    if len(response) == 1:
        (only,) = response.values()
        return dict(only or {})
    return {}


def ensure_mapping(http: Any, name: str, properties: dict[str, dict[str, Any]]) -> list[str]:
    """Additive mapping update (r14-a, N14-obs1): PUT onto `name` every property of
    `properties` its live mapping lacks and return their names — `[]` when nothing was
    missing, so a second bootstrap pass issues no write. Never changes an existing field
    (OpenSearch would refuse that anyway) and never reindexes: documents written before the
    field existed simply lack it, which every v2 reader treats as "active, confidence 1.0".
    Raises the core's `OpenSearchError` unchanged when the index does not exist."""
    live = _live_entry(name, http.request("GET", f"/{name}/_mapping"))
    have = (live.get("mappings") or {}).get("properties") or {}
    missing = {field: spec for field, spec in properties.items() if field not in have}
    if not missing:
        return []
    http.request("PUT", f"/{name}/_mapping", {"properties": missing})
    return sorted(missing)


def ensure_index(http: Any, name: str, body: dict[str, Any]) -> bool:
    """`OpenSearchHttp.ensure_index` with the create race closed: ``True`` when this call
    created the index, ``False`` when it already existed — including when it came into
    existence between this call's HEAD and PUT and its live mapping is compatible with
    `body`. Raises `IndexIncompatible` when the existing index conflicts, and re-raises
    every other `OpenSearchError` unchanged."""
    try:
        return bool(http.ensure_index(name, body))
    except core.OpenSearchError as exc:
        if not is_already_exists(exc):
            raise
    live = _live_entry(name, http.request("GET", f"/{name}"))
    problems = index_compatibility_problems(body, live)
    if problems:
        raise IndexIncompatible(name, problems)
    return False
