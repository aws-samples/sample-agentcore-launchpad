"""Allowlisted projections of COA REST responses (`/api/context-layer/*`).

Each model names exactly what the console shows; nothing COA returns passes through
unnamed, and no projection carries a token. Every field is optional and an unknown
status is kept verbatim, because COA's API is a preview contract: fields come and go
between releases, so a missing one must degrade to ``None``, never to an error.

The ``from_coa`` builders accept both COA's camelCase wire keys and the snake_case
some of its services emit (the ontology engine answers ``ontology_id``), and bound
every free-text field so one oversized COA value cannot bloat a console response.
"""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

# Free-text bounds (characters): descriptions, SQL expressions, list members.
TEXT_MAX = 2000
EXPRESSION_MAX = 4000
SHORT_MAX = 256
LIST_MAX = 50


def pick(item: dict[str, Any], *keys: str) -> Any:
    """The first present, non-empty value among ``keys`` (camelCase first)."""
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def text(value: Any, limit: int = SHORT_MAX) -> str | None:
    """A bounded string, or None. Numbers are stringified; containers are dropped."""
    if value is None or isinstance(value, dict | list | tuple | set | bool):
        return None
    out = str(value).strip()
    if not out:
        return None
    return out if len(out) <= limit else out[:limit] + "…"


def integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def timestamp(value: Any) -> str | None:
    """An RFC 3339 string. COA's sources service answers epoch seconds (Smithy
    ``Timestamp``), other services ISO strings; both read back as ISO with a zone."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        seconds = value / 1000 if value > 1e12 else value
        try:
            return datetime.fromtimestamp(seconds, UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return text(value, 64)


def strings(value: Any, limit: int = SHORT_MAX) -> list[str]:
    if not isinstance(value, list):
        return []
    return [s for s in (text(v, limit) for v in value[:LIST_MAX]) if s]


class NamespaceOut(BaseModel):
    namespace_id: str
    name: str = ""
    display_name: str | None = None
    description: str | None = None
    owner: str | None = None
    status: str | None = None
    source_count: int | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_coa(cls, item: dict[str, Any]) -> "NamespaceOut":
        return cls(
            namespace_id=text(pick(item, "namespaceId", "namespace_id")) or "",
            name=text(item.get("name")) or "",
            display_name=text(pick(item, "displayName", "display_name")),
            description=text(item.get("description"), TEXT_MAX),
            owner=text(item.get("owner")),
            status=text(item.get("status"), 64),
            source_count=integer(pick(item, "sourceCount", "source_count")),
            created_at=timestamp(pick(item, "createdAt", "created_at")),
            updated_at=timestamp(pick(item, "updatedAt", "updated_at")),
        )


class SourceOut(BaseModel):
    source_id: str
    name: str = ""
    source_type: str | None = None
    source_subtype: str | None = None
    status: str | None = None
    tables_discovered: int | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_coa(cls, item: dict[str, Any]) -> "SourceOut":
        return cls(
            source_id=text(pick(item, "sourceId", "source_id")) or "",
            name=text(item.get("name")) or "",
            source_type=text(pick(item, "sourceType", "source_type"), 64),
            source_subtype=text(pick(item, "sourceSubType", "sourceSubtype", "source_subtype"), 64),
            status=text(item.get("status"), 64),
            tables_discovered=integer(pick(item, "tablesDiscovered", "tables_discovered")),
            created_at=timestamp(pick(item, "createdAt", "created_at")),
            updated_at=timestamp(pick(item, "updatedAt", "updated_at")),
        )


class MetricDialectOut(BaseModel):
    dialect: str
    # what COA returns to this member, bounded — never anything COA did not send
    expression: str = ""


class MetricOut(BaseModel):
    name: str
    description: str = ""
    unit: str | None = None
    return_type: str | None = None
    default_time_grain: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    dialects: list[MetricDialectOut] = Field(default_factory=list)
    source_table: str | None = None
    data_source_id: str | None = None

    @classmethod
    def from_coa(cls, item: dict[str, Any]) -> "MetricOut":
        expression = item.get("expression")
        raw_dialects = expression.get("dialects") if isinstance(expression, dict) else None
        dialects = [
            MetricDialectOut(
                dialect=text(d.get("dialect"), 64) or "",
                expression=text(d.get("expression"), EXPRESSION_MAX) or "",
            )
            for d in (raw_dialects if isinstance(raw_dialects, list) else [])[:LIST_MAX]
            if isinstance(d, dict) and d.get("dialect")
        ]
        ai = pick(item, "aiContext", "ai_context")
        return cls(
            name=text(item.get("name")) or "",
            description=text(item.get("description"), TEXT_MAX) or "",
            unit=text(item.get("unit"), 64),
            return_type=text(pick(item, "returnType", "return_type"), 64),
            default_time_grain=text(pick(item, "defaultTimeGrain", "default_time_grain"), 64),
            synonyms=strings(ai.get("synonyms")) if isinstance(ai, dict) else [],
            dialects=dialects,
            source_table=text(pick(item, "sourceTable", "source_table")),
            data_source_id=text(pick(item, "dataSourceId", "data_source_id")),
        )


class OntologyOut(BaseModel):
    ontology_id: str
    title: str = ""
    description: str | None = None
    ontology_type: str | None = None
    # the registry row's lifecycle (``deleting``) when set, else the ingest marker
    status: str | None = None
    class_count: int | None = None
    property_count: int | None = None
    updated_at: str | None = None

    @classmethod
    def from_coa(cls, item: dict[str, Any]) -> "OntologyOut":
        ontology_id = text(pick(item, "ontologyId", "ontology_id"), TEXT_MAX) or ""
        return cls(
            ontology_id=ontology_id,
            title=text(item.get("title")) or text(item.get("uri"), TEXT_MAX) or ontology_id,
            description=text(item.get("description"), TEXT_MAX),
            ontology_type=text(pick(item, "ontologyType", "ontology_type"), 64),
            status=text(pick(item, "status", "parseStatus", "parse_status"), 64),
            class_count=integer(pick(item, "classCount", "class_count")),
            property_count=integer(pick(item, "propertyCount", "property_count")),
            updated_at=timestamp(pick(item, "updatedAt", "updated_at")),
        )


class OntologyClassOut(BaseModel):
    uri: str
    label: str = ""
    description: str | None = None
    ontology_id: str
    # the owning ontology's title
    ontology: str = ""

    @classmethod
    def from_coa(cls, item: dict[str, Any], ontology: OntologyOut) -> "OntologyClassOut":
        uri = text(item.get("uri"), TEXT_MAX) or ""
        return cls(
            uri=uri,
            label=text(item.get("label")) or uri.rsplit("#", 1)[-1].rsplit("/", 1)[-1],
            description=text(pick(item, "comment", "description"), TEXT_MAX),
            ontology_id=ontology.ontology_id,
            ontology=ontology.title,
        )
