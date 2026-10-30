"""Registry feature boundary (N4 implements `RegistryBackend` on SQLite + FTS5).

The AWS Agent Registry wrappers were removed (spec C5). The descriptor builders
below are pure and shared by every backend; the Protocol mirrors the former
`agentcore/registry.py` client-bound operations minus the `client`/`registry_id`
arguments (a backend knows its own store).

Status model (API enum has no PUBLISHED): the platform maps
    submit  → PENDING_APPROVAL   (UI chip: SUBMITTED)
    approve → APPROVED           (UI chip: PUBLISHED — live/consumable)
    disable → DEPRECATED         (UI chip: DISABLED)
"""

import json
import re
import time
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote

from app.core.errors import AppError
from app.core.features import feature_unavailable
from app.services.workspace import WorkspaceContext

if TYPE_CHECKING:
    from app.services.creators import Creator


class RegistryUnavailableError(AppError):
    """Registry is an optional capability and no backend is registered."""

    def __init__(self, reason: str | None = None):
        super().__init__(
            "registry.unavailable",
            reason or "Agent Registry is not configured",
            {"reason": reason},
            status_code=503,
        )


class RegistryBackend(Protocol):
    """Records are returned in the platform contract (``recordId``, ``name``,
    ``description``, ``descriptorType`` ∈ A2A/MCP/AGENT_SKILLS/CUSTOM, ``status``,
    ``statusReason``, ``recordVersion``, ``descriptors``, ``createdAt``, ``updatedAt``) —
    the shape ``LocalRegistryBackend`` produces and ``routers/registry._record_out`` reads."""

    def find_record(
        self, workspace: WorkspaceContext, name: str, descriptor_type: str | None = None,
    ) -> dict[str, Any] | None:
        """Exact-name lookup (deployer register stage, system skill registration)."""
        ...

    def upsert_record(
        self, workspace: WorkspaceContext, *, name: str, description: str,
        descriptor_type: str, descriptors: dict[str, Any], record_version: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Create or refresh by name → ``(record, created)``. Deployer register stage,
        ``POST /api/registry/records``, skill import/reimport."""
        ...

    def get_record(self, workspace: WorkspaceContext, record_id: str) -> dict[str, Any]:
        """``GET /api/registry/records/{record_id}`` — full record incl. ``descriptors``."""
        ...

    def wait_record_settled(
        self, workspace: WorkspaceContext, record_id: str, timeout_s: int = 60,
        sleeper: Any = time.sleep,
    ) -> dict[str, Any]:
        """Block until the record leaves CREATING/UPDATING (deployer register stage)."""
        ...

    def list_records(
        self, workspace: WorkspaceContext, descriptor_type: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /api/registry/records`` (publisher view, every status); Overview tiles;
        ``GET /api/registry/attachables`` and the zip deployer read ``APPROVED`` only."""
        ...

    def list_discoverable_records(
        self, workspace: WorkspaceContext, descriptor_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /api/registry/records/discoverable`` — consumer view (APPROVED only,
        summaries without ``descriptors``). ``ValueError`` on an unknown type."""
        ...

    def update_record(
        self, workspace: WorkspaceContext, record_id: str, *, description: str | None = None,
        descriptors: dict[str, Any] | None = None, record_version: str | None = None,
    ) -> dict[str, Any]:
        """``PUT /api/registry/records/{record_id}``; system skill roll-forward."""
        ...

    def submit_record(self, workspace: WorkspaceContext, record_id: str) -> dict[str, Any]:
        """``POST …/action {submit}`` → PENDING_APPROVAL; also deployer auto-submit."""
        ...

    def set_record_status(
        self, workspace: WorkspaceContext, record_id: str, status: str, reason: str,
    ) -> dict[str, Any]:
        """Primitive behind approve/disable/reject."""
        ...

    def approve_record(self, workspace: WorkspaceContext, record_id: str) -> dict[str, Any]:
        """``POST …/action {approve|publish}`` → APPROVED."""
        ...

    def disable_record(self, workspace: WorkspaceContext, record_id: str) -> dict[str, Any]:
        """``POST …/action {disable}`` → DEPRECATED (terminal; only delete remains)."""
        ...

    def reject_record(self, workspace: WorkspaceContext, record_id: str) -> dict[str, Any]:
        """``POST …/action {reject}`` → REJECTED (recoverable)."""
        ...

    def delete_record(self, workspace: WorkspaceContext, record_id: str) -> None:
        """``DELETE /api/registry/records/{record_id}``."""
        ...

    def search_records(
        self, workspace: WorkspaceContext, query: str, max_results: int = 20,
    ) -> list[dict[str, Any]]:
        """``GET /api/registry/records/search?q=`` — full-text search over records."""
        ...


_backend: RegistryBackend | None = None


def set_backend(backend: RegistryBackend | None) -> None:
    global _backend
    _backend = backend


def backend_or_none() -> RegistryBackend | None:
    return _backend


def get_backend() -> RegistryBackend:
    if _backend is None:
        raise feature_unavailable("registry")
    return _backend


# The Protocol's lifecycle methods carry no actor; the `/action` route sets this around
# its backend call so a local backend can stamp `decidedBy` (N4 plan, ruling R9).
current_actor: ContextVar[str | None] = ContextVar("registry_actor", default=None)

# `upsert_record` carries no creator either (N15-obs7): the console create routes
# (`POST /api/registry/records`, `POST /api/registry/skills/import`) set the caller's
# `Creator` here around the service call so a ledger-backed backend stamps `created_by` /
# `created_by_name` on the NEW row (`app.services.creators`). Unset — deploy-created A2A
# records, gateway imports, platform defaults, workers — leaves NULL: an administrator's.
current_creator: "ContextVar[Creator | None]" = ContextVar("registry_creator", default=None)


def data_plane_invocations_url(arn: str, workspace: WorkspaceContext) -> str:
    """Runtime data-plane URL for an A2A card (partition-aware; ARN percent-encoded as
    the InvokeAgentRuntime path requires — matches the frontend's bearerInvokeUrl)."""
    return (
        f"https://bedrock-agentcore.{workspace.region}.{workspace.dns_suffix}/runtimes/"
        f"{quote(arn, safe='')}/invocations?qualifier=DEFAULT"
    )


# ---------- descriptor builders (pure; moved verbatim from agentcore/registry.py) ----------

A2A_SCHEMA_VERSION = "0.3.0"
# Platform-owned A2A AgentCard ``version``. The runtime template (Strands
# ``A2AServer(version=...)``) and the registry record (``build_a2a_card``) both
# read this one constant, so the live card and the stored card agree by
# construction. It is NOT the AgentCore runtime version: that is assigned by
# Create/UpdateAgentRuntime only after the template has been rendered, and
# keeps living in the ledger (``Agent.version``) and the VERSIONS & ENDPOINTS panel.
A2A_CARD_VERSION = "1.0.0"
MCP_SERVER_SCHEMA_VERSION = "2025-07-09"  # MCP registry server.json schema date
MCP_PROTOCOL_VERSION = "2025-06-18"
SKILL_SCHEMA_VERSION = "0.1.0"

_PLATFORM_TO_AWS_TYPE = {
    "A2A": "AGENT",
    "MCP": "MCP",
    "AGENT_SKILLS": "SKILL",
    "CUSTOM": "CUSTOM",
}
_AWS_TO_PLATFORM_TYPE = {value: key for key, value in _PLATFORM_TO_AWS_TYPE.items()}


def ga_record_type(descriptor_type: str) -> str:
    """The GA ``recordType`` filter value for a platform (``A2A``/``MCP``/``AGENT_SKILLS``)
    or GA (``agent``/``mcp``/``skill``, any case) type name. ``ValueError`` on anything else."""
    key = descriptor_type.strip().upper()
    if key in _PLATFORM_TO_AWS_TYPE:
        return _PLATFORM_TO_AWS_TYPE[key]
    if key in _AWS_TO_PLATFORM_TYPE:
        return key
    raise ValueError(f"unknown registry record type: {descriptor_type!r}")


def platform_record_type(descriptor_type: str) -> str:
    """The platform type name (``A2A``/``MCP``/``AGENT_SKILLS``/``CUSTOM``) for a platform
    or GA (``agent``/``mcp``/``skill``/``custom``, any case) type name. ``ValueError``
    on anything else — routers map it to 422 ``registry.bad_type``."""
    return _AWS_TO_PLATFORM_TYPE[ga_record_type(descriptor_type)]


def bump_minor_version(version: str) -> str:
    """Bump the minor component while preserving a type suffix: ``1.0.0`` → ``1.1.0``,
    ``1.0.0-skill`` → ``1.1.0-skill``. A version whose major/minor do not parse resets
    to a clean bumped baseline rather than failing the caller."""
    parts = (version or "").split(".")
    try:
        major = int(parts[0])
    except (ValueError, IndexError):
        return "1.1.0"
    try:
        minor = int(parts[1])
    except (ValueError, IndexError):
        minor = 0
    patch = parts[2] if len(parts) > 2 else ""
    suffix = f"-{patch.split('-', 1)[1]}" if "-" in patch else ""
    return f"{major}.{minor + 1}.0{suffix}"


def descriptor_fingerprint(value: Any) -> Any:
    """Normalize embedded descriptor JSON (``inlineContent`` strings) so two descriptor
    trees compare by meaning, not by key order or whitespace."""
    if isinstance(value, dict):
        return {key: descriptor_fingerprint(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [descriptor_fingerprint(item) for item in value]
    if isinstance(value, str):
        try:
            return descriptor_fingerprint(json.loads(value))
        except (TypeError, ValueError):
            return value
    return value


INITIAL_VERSION_BY_TYPE = {
    "A2A": "1.0.0-a2a",
    "MCP": "1.0.0-mcp",
    "AGENT_SKILLS": "1.0.0-skill",
    "CUSTOM": "1.0.0-custom",
}


# ---------- payload builders (pure) ----------

_SKILL_ID_STRIP_RE = re.compile(r"[^a-z0-9_-]+")

# the zip template bakes these two tools into every generated agent — they are
# guaranteed capabilities even though spec.tools is empty for template agents
ZIP_TEMPLATE_SKILLS: list[dict[str, Any]] = [
    {"id": "calculator", "name": "calculator",
     "description": "Evaluate a basic arithmetic expression", "tags": ["math"]},
    {"id": "current-time", "name": "current time",
     "description": "Report the current UTC date and time", "tags": ["time"]},
]


def _skill_id(name: str) -> str:
    slug = _SKILL_ID_STRIP_RE.sub("-", name.lower()).strip("-")
    slug = re.sub(r"^[^a-z]+", "", slug)[:64]
    return slug or "skill"


def derive_card_skills(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """AgentCard ``skills`` for a deployed agent — the routing surface other
    agents (and the front-desk demo) match on.

    Explicit ``a2a_skills`` win; otherwise a best-effort derivation from what
    the spec declares: tools, knowledge bases (name+description carry real
    routing signal), attached agent skills, plus the zip template's baked-in
    tools for template-generated agents.
    """
    explicit = spec.get("a2a_skills") or []
    if explicit:
        return [dict(s) for s in explicit]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(name: str, description: str, tag: str) -> None:
        if not name:
            return
        base = _skill_id(name)
        sid, n = base, 2
        while sid in seen:
            sid, n = f"{base[:60]}-{n}", n + 1
        seen.add(sid)
        out.append({"id": sid, "name": name, "description": description,
                    "tags": [tag] if tag else []})

    for tool in spec.get("tools") or []:
        ttype = str(tool.get("type") or "tool")
        add(str(tool.get("name") or ""), f"{ttype} tool", ttype)
    for kb in spec.get("knowledge_bases") or []:
        add(str(kb.get("name") or kb.get("kb_id") or ""),
            str(kb.get("description") or "knowledge base retrieval"), "knowledge")
    for path in spec.get("skills") or []:
        add(str(path).rstrip("/").rsplit("/", 1)[-1], "agent skill", "skill")
    if (spec.get("method") == "zip_runtime"
            and not spec.get("code") and not spec.get("code_bundle")):
        for skill in ZIP_TEMPLATE_SKILLS:
            if skill["id"] not in seen:
                seen.add(skill["id"])
                out.append(dict(skill))
    return out


def build_a2a_card(
    *,
    name: str,
    description: str,
    arn: str,
    method: str,
    version: str | None = None,
    url: str | None = None,
    skills: list[dict[str, Any]] | None = None,
    transport: str = "agentcore-http",
    workspace: WorkspaceContext | None = None,
) -> dict[str, Any]:
    """A2A AgentCard for the registry record.

    ``transport`` tells consumers whether ``url`` speaks real A2A JSON-RPC
    (`a2a-jsonrpc` — serverProtocol=A2A runtimes) or the AgentCore HTTP
    invocations contract (`agentcore-http` — call via the platform API).
    ``version`` defaults to ``A2A_CARD_VERSION`` — the same constant the A2A
    runtime template serves — so record and live card match on that field.
    ``workspace`` derives the partition-aware data-plane ``url`` when none is given.
    """
    if url is None and workspace is not None:
        url = data_plane_invocations_url(arn, workspace)
    return {
        "protocolVersion": A2A_SCHEMA_VERSION,
        "name": name,
        "description": description,
        "url": url or arn,
        "preferredTransport": "JSONRPC",
        "version": version or A2A_CARD_VERSION,
        "capabilities": {"streaming": True},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": list(skills or []),
        "metadata": {
            "launchpad.method": method,
            "launchpad.transport": transport,
            "launchpad.invoke": (
                "standard A2A JSON-RPC (InvokeAgentRuntime passthrough)"
                if transport == "a2a-jsonrpc" else "platform /v1 API"
            ),
        },
    }


def build_a2a_descriptors(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "a2a": {
            "agentCard": {
                "schemaVersion": A2A_SCHEMA_VERSION,
                "inlineContent": json.dumps(card),
            }
        }
    }


def stored_a2a_card(record: dict[str, Any]) -> dict[str, Any]:
    """The AgentCard a registry record carries (``descriptors.a2a.agentCard``
    ``inlineContent``, parsed); ``{}`` when absent or unparseable."""
    descriptors = record.get("descriptors") or {}
    card = ((descriptors.get("a2a") or {}).get("agentCard") or {}).get("inlineContent")
    if isinstance(card, dict):
        return card
    if not isinstance(card, str) or not card.strip():
        return {}
    try:
        parsed = json.loads(card)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


CARD_DIFF_FIELDS = ("name", "url", "version", "protocolVersion")


def _skill_ids(card: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for skill in card.get("skills") or []:
        if isinstance(skill, dict) and skill.get("id") is not None:
            out.append(str(skill["id"]))
    return out


def agent_card_diff(record_card: dict[str, Any], live_card: dict[str, Any]) -> dict[str, Any]:
    """Record card vs. the card the runtime serves — identity fields + skill ids.

    Only the fields a consumer keys on are compared (``name``/``url``/``version``/
    ``protocolVersion``) plus the skill id sets; description text, capabilities
    and the platform's own ``metadata`` block are deliberately left out, so an
    edited description never reads as "drifted".
    """
    fields = [
        {"field": f, "record": record_card.get(f), "live": live_card.get(f)}
        for f in CARD_DIFF_FIELDS
        if record_card.get(f) != live_card.get(f)
    ]
    record_ids, live_ids = set(_skill_ids(record_card)), set(_skill_ids(live_card))
    only_live = sorted(live_ids - record_ids)
    only_record = sorted(record_ids - live_ids)
    return {
        "identical": not fields and not only_live and not only_record,
        "fields": fields,
        "skills_only_in_live": only_live,
        "skills_only_in_record": only_record,
    }


def build_mcp_descriptors(
    *,
    target: str,
    description: str,
    gateway_url: str,
    tools: list[dict[str, Any]] | None,
    gateway: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """MCP record descriptors. ``gateway_url`` is any streamable-http MCP
    endpoint (the shared gateway or an external remote server); ``tools=None``
    omits the tool listing (unknown for externally registered servers).

    ``gateway`` (WB-005, additive) is the AgentCore Gateway the record was imported
    from — ``{gateway_id, gateway_arn, auth, runtime_tools[, auth_note]}`` — stored
    verbatim as ``descriptors.mcp.gateway`` so a record remembers which Gateway and
    which outbound auth (``aws_iam`` | ``none`` | ``oauth`` | ``unmapped``) it binds
    to, without touching the ``server`` document consumers parse."""
    server_json = {
        "name": f"io.launchpad/{target}",
        "description": description or f"launchpad gateway target {target}",
        "version": "1.0.0",
        "remotes": [{"type": "streamable-http", "url": gateway_url}],
    }
    out: dict[str, Any] = {
        "mcp": {
            "server": {
                "schemaVersion": MCP_SERVER_SCHEMA_VERSION,
                "inlineContent": json.dumps(server_json),
            },
        }
    }
    if tools is not None:
        out["mcp"]["tools"] = {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "inlineContent": json.dumps({"tools": tools}),
        }
    if gateway is not None:
        out["mcp"]["gateway"] = dict(gateway)
    return out


def build_skills_descriptors(
    *, skill_md: str, definition: dict[str, Any]
) -> dict[str, Any]:
    return {
        "agentSkills": {
            "skillMd": {"inlineContent": skill_md},
            "skillDefinition": {
                "schemaVersion": SKILL_SCHEMA_VERSION,
                "inlineContent": json.dumps(definition),
            },
        }
    }


