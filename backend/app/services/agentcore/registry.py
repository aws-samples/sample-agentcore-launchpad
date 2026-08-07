"""AgentCore Registry wrappers — records CRUD, approval workflow, search.

Status model (API enum has no PUBLISHED): the platform maps
    submit  → PENDING_APPROVAL   (UI chip: SUBMITTED)
    approve → APPROVED           (UI chip: PUBLISHED — live/consumable)
    disable → DEPRECATED         (UI chip: DISABLED)
Explicit-client style; payload builders are pure for unit testing.
"""

import json
import re
import time
from typing import Any
from urllib.parse import quote

A2A_SCHEMA_VERSION = "0.3.0"
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
_INITIAL_VERSION_BY_TYPE = {
    "A2A": "1.0.0-a2a",
    "MCP": "1.0.0-mcp",
    "AGENT_SKILLS": "1.0.0-skill",
    "CUSTOM": "1.0.0-custom",
}


# ---------- payload builders (pure) ----------

def data_plane_invocations_url(arn: str, region: str) -> str:
    """The runtime's HTTP data-plane base URL — for serverProtocol=A2A runtimes
    this is a real A2A endpoint (well-known agent card + JSON-RPC root)."""
    return (
        f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
        f"{quote(arn, safe='')}/invocations/"
    )


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
    version: str,
    method: str,
    url: str | None = None,
    skills: list[dict[str, Any]] | None = None,
    transport: str = "agentcore-http",
) -> dict[str, Any]:
    """A2A AgentCard for the registry record.

    ``transport`` tells consumers whether ``url`` speaks real A2A JSON-RPC
    (`a2a-jsonrpc` — serverProtocol=A2A runtimes) or the AgentCore HTTP
    invocations contract (`agentcore-http` — call via the platform API).
    """
    return {
        "protocolVersion": A2A_SCHEMA_VERSION,
        "name": name,
        "description": description,
        "url": url or arn,
        "preferredTransport": "JSONRPC",
        "version": version or "1",
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


def build_mcp_descriptors(
    *,
    target: str,
    description: str,
    gateway_url: str,
    tools: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """MCP record descriptors. ``gateway_url`` is any streamable-http MCP
    endpoint (the shared gateway or an external remote server); ``tools=None``
    omits the tool listing (unknown for externally registered servers)."""
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


# ---------- GA schema adapter ----------

def _required_descriptor(
    value: dict[str, Any], field: str, descriptor_type: str
) -> dict[str, Any]:
    descriptor = value.get(field)
    if not isinstance(descriptor, dict):
        raise ValueError(f"{descriptor_type} descriptor is missing {field}")
    return descriptor


def _ga_leaf(value: dict[str, Any], *, version_field: str = "schemaVersion") -> dict[str, Any]:
    out = {"data": value.get("inlineContent", "")}
    version = value.get(version_field)
    if version:
        out["dataSchemaVersion"] = version
    return out


def to_ga_descriptors(
    descriptor_type: str, descriptors: dict[str, Any]
) -> dict[str, Any]:
    """Translate Launchpad's stable descriptor contract to the GA AWS shape."""
    if descriptor_type == "A2A":
        container = _required_descriptor(descriptors, "a2a", descriptor_type)
        card = _required_descriptor(container, "agentCard", descriptor_type)
        return {"a2aAgentCard": _ga_leaf(card)}
    if descriptor_type == "MCP":
        container = _required_descriptor(descriptors, "mcp", descriptor_type)
        server = _ga_leaf(_required_descriptor(container, "server", descriptor_type))
        tools = container.get("tools")
        if isinstance(tools, dict):
            server["additionalData"] = {
                "tools": _ga_leaf(tools, version_field="protocolVersion")
            }
        return {"mcpServer": server}
    if descriptor_type == "AGENT_SKILLS":
        container = _required_descriptor(descriptors, "agentSkills", descriptor_type)
        definition = _ga_leaf(
            _required_descriptor(container, "skillDefinition", descriptor_type)
        )
        skill_md = container.get("skillMd")
        if isinstance(skill_md, dict):
            definition["additionalData"] = {"skillMd": _ga_leaf(skill_md)}
        return {"agentSkillsDefinition": definition}
    if descriptor_type == "CUSTOM":
        custom = _required_descriptor(descriptors, "custom", descriptor_type)
        return {"custom": {"data": custom.get("inlineContent", "")}}
    raise ValueError(f"unsupported Registry descriptor type: {descriptor_type}")


def _platform_leaf(
    value: dict[str, Any], *, version_field: str = "schemaVersion"
) -> dict[str, Any]:
    out = {"inlineContent": value.get("data", "")}
    version = value.get("dataSchemaVersion")
    if version:
        out[version_field] = version
    return out


def from_ga_descriptors(
    record_type: str, descriptors: dict[str, Any] | None
) -> dict[str, Any]:
    """Translate a GA AWS descriptor tree to Launchpad's stable contract."""
    descriptors = descriptors or {}
    if record_type == "AGENT":
        return {
            "a2a": {
                "agentCard": _platform_leaf(
                    _required_descriptor(descriptors, "a2aAgentCard", record_type)
                )
            }
        }
    if record_type == "MCP":
        server = _required_descriptor(descriptors, "mcpServer", record_type)
        legacy: dict[str, Any] = {"server": _platform_leaf(server)}
        tools = (server.get("additionalData") or {}).get("tools")
        if isinstance(tools, dict):
            legacy["tools"] = _platform_leaf(tools, version_field="protocolVersion")
        return {"mcp": legacy}
    if record_type == "SKILL":
        definition = _required_descriptor(
            descriptors, "agentSkillsDefinition", record_type
        )
        legacy = {"skillDefinition": _platform_leaf(definition)}
        skill_md = (definition.get("additionalData") or {}).get("skillMd")
        if isinstance(skill_md, dict):
            legacy["skillMd"] = _platform_leaf(skill_md)
        return {"agentSkills": legacy}
    if record_type == "CUSTOM":
        custom = _required_descriptor(descriptors, "custom", record_type)
        return {"custom": {"inlineContent": custom.get("data", "")}}
    raise ValueError(f"unsupported Registry record type: {record_type}")


def normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    """Return a copy using the preview-era Launchpad contract at the app boundary."""
    normalized = dict(record)
    record_type = str(record.get("recordType") or "")
    normalized["descriptorType"] = _AWS_TO_PLATFORM_TYPE.get(record_type, record_type)
    if "descriptors" in record:
        normalized["descriptors"] = from_ga_descriptors(
            record_type, record.get("descriptors")
        )
    return normalized


# ---------- record operations ----------

def find_record(
    client: Any, registry_id: str, name: str, descriptor_type: str | None = None
) -> dict[str, Any] | None:
    filters = [{"name": "name", "values": [name]}]
    if descriptor_type:
        filters.append(
            {
                "name": "recordType",
                "values": [_PLATFORM_TO_AWS_TYPE[descriptor_type]],
            }
        )
    kwargs: dict[str, Any] = {
        "registryId": registry_id,
        "filters": filters,
        "maxResults": 20,
    }
    for record in client.list_registry_records(**kwargs).get("registryRecords", []):
        if record.get("name") == name:
            return normalize_record(record)
    return None


def upsert_record(
    client: Any,
    registry_id: str,
    *,
    name: str,
    description: str,
    descriptor_type: str,
    descriptors: dict[str, Any],
    record_version: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Create the record or refresh its descriptors. Returns (record, created).

    ``record_version`` is only meaningful on the update branch (reimport bumps it
    to a fresh revision, e.g. ``1.1.0``); create always starts at ``1.0.0``.
    """
    existing = find_record(client, registry_id, name, descriptor_type)
    aws_record_type = _PLATFORM_TO_AWS_TYPE[descriptor_type]
    ga_descriptors = to_ga_descriptors(descriptor_type, descriptors)
    if existing is None:
        created = client.create_registry_record(
            registryId=registry_id,
            name=name,
            displayName=name,
            description=description,
            recordType=aws_record_type,
            descriptors=ga_descriptors,
            # GA uniqueness is (name, recordVersion), while preview allowed the
            # same name for A2A/MCP/Skill records. Type-qualified initial
            # versions preserve that platform contract.
            recordVersion=_INITIAL_VERSION_BY_TYPE[descriptor_type],
        )
        # CreateRegistryRecord returns only {recordArn, status}
        record_id = created["recordArn"].split("/")[-1]
        return {**created, "recordId": record_id}, True
    kwargs: dict[str, Any] = {
        "registryId": registry_id,
        "recordId": existing["recordId"],
        "name": name,
        "displayName": {"optionalValue": name},
        "description": {"optionalValue": description},
        "recordType": aws_record_type,
        "descriptors": wrap_descriptors_for_update(
            descriptors, descriptor_type=descriptor_type
        ),
    }
    if record_version is not None:
        kwargs["recordVersion"] = record_version
    updated = client.update_registry_record(**kwargs)
    return normalize_record(updated), False


def _wrap_ga_descriptor(value: dict[str, Any]) -> dict[str, Any]:
    wrapped: dict[str, Any] = {}
    for field in ("data", "dataSchemaVersion", "source"):
        if field in value:
            wrapped[field] = {"optionalValue": value[field]}
    additional = value.get("additionalData")
    if isinstance(additional, dict):
        wrapped["additionalData"] = {
            "optionalValue": {
                key: {"optionalValue": _wrap_ga_descriptor(child)}
                for key, child in additional.items()
            }
        }
    return wrapped


def wrap_descriptors_for_update(
    descriptors: dict[str, Any], *, descriptor_type: str | None = None
) -> dict[str, Any]:
    """Translate create-style Launchpad descriptors to GA PATCH wrappers."""
    if descriptor_type is None:
        key = next(iter(descriptors), "")
        descriptor_type = {
            "a2a": "A2A",
            "mcp": "MCP",
            "agentSkills": "AGENT_SKILLS",
            "custom": "CUSTOM",
        }.get(key)
    if descriptor_type is None:
        raise ValueError("cannot infer Registry descriptor type")
    ga_descriptors = to_ga_descriptors(descriptor_type, descriptors)
    primary, value = next(iter(ga_descriptors.items()))
    return {
        "optionalValue": {
            primary: {"optionalValue": _wrap_ga_descriptor(value)}
        }
    }


def get_record(client: Any, registry_id: str, record_id: str) -> dict[str, Any]:
    return normalize_record(
        client.get_registry_record(registryId=registry_id, recordId=record_id)
    )


def wait_record_settled(
    client: Any,
    registry_id: str,
    record_id: str,
    timeout_s: int = 60,
    sleeper: Any = time.sleep,
) -> dict[str, Any]:
    """Records transition CREATING/UPDATING → DRAFT asynchronously; wait it out."""
    deadline = time.monotonic() + timeout_s
    while True:
        record = get_record(client, registry_id, record_id)
        if record["status"] not in ("CREATING", "UPDATING"):
            return record
        if time.monotonic() > deadline:
            raise TimeoutError(f"record {record_id} still {record['status']}")
        sleeper(2)


def list_records(
    client: Any,
    registry_id: str,
    descriptor_type: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    kwargs: dict[str, Any] = {"registryId": registry_id, "maxResults": 100}
    filters: list[dict[str, Any]] = []
    if descriptor_type:
        filters.append(
            {
                "name": "recordType",
                "values": [_PLATFORM_TO_AWS_TYPE[descriptor_type]],
            }
        )
    if status:
        filters.append({"name": "status", "values": [status]})
    if filters:
        kwargs["filters"] = filters
    records: list[dict[str, Any]] = []
    while True:
        page = client.list_registry_records(**kwargs)
        records.extend(normalize_record(record) for record in page.get("registryRecords", []))
        token = page.get("nextToken")
        if not token:
            break
        kwargs["nextToken"] = token
    return records


def submit_record(client: Any, registry_id: str, record_id: str) -> dict[str, Any]:
    return client.submit_registry_record_for_approval(
        registryId=registry_id, recordId=record_id
    )


def set_record_status(
    client: Any, registry_id: str, record_id: str, status: str, reason: str
) -> dict[str, Any]:
    return client.update_registry_record_status(
        registryId=registry_id, recordId=record_id, status=status, statusReason=reason
    )


def approve_record(client: Any, registry_id: str, record_id: str) -> dict[str, Any]:
    return set_record_status(
        client, registry_id, record_id, "APPROVED", "approved via launchpad console"
    )


def disable_record(client: Any, registry_id: str, record_id: str) -> dict[str, Any]:
    # NB: DEPRECATED is terminal — the service refuses any further status
    # change (verified live); the only remaining operation is delete.
    return set_record_status(
        client, registry_id, record_id, "DEPRECATED", "disabled via launchpad console"
    )


def reject_record(client: Any, registry_id: str, record_id: str) -> dict[str, Any]:
    # REJECTED is recoverable: a later APPROVED status change is accepted.
    return set_record_status(
        client, registry_id, record_id, "REJECTED", "rejected via launchpad console"
    )


def delete_record(client: Any, registry_id: str, record_id: str) -> None:
    client.delete_registry_record(registryId=registry_id, recordId=record_id)


def search_records(
    data_client: Any, registry_ids: list[str], query: str, max_results: int = 20
) -> list[dict[str, Any]]:
    records = data_client.search_discoverable_registry_records(
        registryIds=registry_ids, searchQuery=query, maxResults=max_results
    ).get("registryRecords", [])
    return [normalize_record(record) for record in records]
