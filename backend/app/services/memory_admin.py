"""AgentCore Memory resource administration — the mutating counterpart to
``memory_console``.

``memory_console`` (and ``routers/memory``) is read-only **by construction** and
a structural test pins the absence of any mutating call there. Everything that
manages the lifecycle of the memory resources themselves — list the account's
memories, create one, delete one — lives here instead, behind its own router
(``routers/memory_resources``).

The workspace's bootstrap memory stays the default: agents whose spec does not
pick a memory land on it, so it can never be deleted from here.
"""

from datetime import datetime
from typing import Any

from app.core.errors import AppError
from app.services.agentcore.client import control_client
from app.services.memory import memory_id_or_none
from app.services.workspace import WorkspaceContext

PAGE_MAX = 100

# The strategy shapes mirror the bootstrap memory exactly (services/bootstrap.py):
# agents write under a scoped actor and the console reads /facts/... and
# /preferences/... namespaces, so a custom memory must expose the same layout for
# extraction and the chat rail to work unchanged. Summaries and episodes are
# additive: their namespaces keep the platform's flat `/label/{actorId}` style
# rather than the docs' `/strategy/{memoryStrategyId}/...` example, so the
# console's namespace resolution treats every strategy the same way.
STRATEGIES: dict[str, dict[str, Any]] = {
    "semantic": {
        "semanticMemoryStrategy": {
            "name": "semantic_facts",
            "namespaces": ["/facts/{actorId}"],
        }
    },
    "user_preference": {
        "userPreferenceMemoryStrategy": {
            "name": "user_preferences",
            "namespaces": ["/preferences/{actorId}"],
        }
    },
    "summarization": {
        "summaryMemoryStrategy": {
            "name": "session_summaries",
            "namespaces": ["/summaries/{actorId}/{sessionId}"],
        }
    },
    # Episodes capture whole interactions (scenario/intent/actions/outcome);
    # reflections aggregate insights across them. The live API requires the
    # reflection namespace to be "the same as or a hierarchical prefix of" the
    # episode namespace, so it is the per-actor prefix of the per-session one.
    "episodic": {
        "episodicMemoryStrategy": {
            "name": "episodes",
            "namespaces": ["/episodes/{actorId}/{sessionId}"],
            "reflectionConfiguration": {
                "namespaceTemplates": ["/episodes/{actorId}"]
            },
        }
    },
}


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else None


def _name_from_id(memory_id: str | None) -> str | None:
    """Memory ids are ``{name}-{suffix}``; ListMemories carries no name field."""
    if not memory_id:
        return memory_id
    name, sep, _ = memory_id.rpartition("-")
    return name if sep else memory_id


def _summary(raw: dict[str, Any], default_id: str | None) -> dict[str, Any]:
    ident = raw.get("id") or raw.get("memoryId")
    return {
        "id": ident,
        "arn": raw.get("arn"),
        "name": _name_from_id(ident),
        "status": raw.get("status"),
        "created_at": _iso(raw.get("createdAt")),
        "updated_at": _iso(raw.get("updatedAt")),
        "is_default": bool(ident) and ident == default_id,
    }


def _detail(raw: dict[str, Any], default_id: str | None) -> dict[str, Any]:
    """GetMemory → the console's resource shape (same keys as the overview)."""
    ident = raw.get("id")
    return {
        "id": ident,
        "arn": raw.get("arn"),
        "name": raw.get("name") or _name_from_id(ident),
        "description": raw.get("description"),
        "status": raw.get("status"),
        "failure_reason": raw.get("failureReason"),
        "event_expiry_days": raw.get("eventExpiryDuration"),
        "execution_role_arn": raw.get("memoryExecutionRoleArn"),
        "created_at": _iso(raw.get("createdAt")),
        "updated_at": _iso(raw.get("updatedAt")),
        "is_default": bool(ident) and ident == default_id,
        "strategies": [
            {
                "strategy_id": s.get("strategyId"),
                "name": s.get("name"),
                "type": s.get("type"),
                "status": s.get("status"),
                "namespaces": list(s.get("namespaces") or []),
            }
            for s in raw.get("strategies") or []
        ],
        # flexible namespace variables defined on the resource (CreateMemory
        # namespaceKeys) — validation rules echoed in the console's shape
        "namespace_keys": [
            {
                "key": k.get("key"),
                "allowed_values": list((k.get("validation") or {}).get("allowedValues") or [])
                or None,
                "regex_pattern": (k.get("validation") or {}).get("regexPattern"),
            }
            for k in raw.get("namespaceKeys") or []
        ],
    }


def list_memory_resources(workspace: WorkspaceContext) -> dict[str, Any]:
    """Every memory in this workspace's account/region, default first."""
    control = control_client(workspace)
    default_id = memory_id_or_none(workspace)
    items: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"maxResults": PAGE_MAX}
        if token:
            kwargs["nextToken"] = token
        page = control.list_memories(**kwargs)
        items += [_summary(m, default_id) for m in page.get("memories", [])]
        token = page.get("nextToken")
        if not token:
            break
    items.sort(key=lambda m: (not m["is_default"], m["name"] or "", m["id"] or ""))
    return {"items": items, "default_id": default_id}


def get_memory_resource(workspace: WorkspaceContext, memory_id: str) -> dict[str, Any]:
    raw = control_client(workspace).get_memory(memoryId=memory_id).get("memory", {})
    return _detail(raw, memory_id_or_none(workspace))


def create_memory_resource(
    workspace: WorkspaceContext,
    name: str,
    description: str,
    event_expiry_days: int,
    strategies: list[str],
    namespace_keys: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """CreateMemory with the platform's strategy layout; returns CREATING state.

    Creation takes minutes to reach ACTIVE — deliberately no wait here: the list
    view surfaces the live status and an agent can only be pointed at a memory
    the user selects, so a not-yet-ACTIVE id is visible for what it is.

    ``namespace_keys`` pre-registers flexible namespace variables (up to 5) on
    the resource — CreateMemory's ``namespaceKeys``. The platform's canned
    strategy templates deliberately do NOT reference them: the console's invoke
    path never supplies ``extractionConfig.namespaceVariables`` on CreateEvent,
    and a template variable left unresolved silently skips long-term extraction
    for that strategy. External consumers of the memory can reference the keys
    in their own strategies/templates and supply values at event time.
    """
    unknown = sorted(set(strategies) - set(STRATEGIES))
    if unknown:
        raise AppError(
            "memory.invalid_strategy",
            f"unknown memory strategies: {', '.join(unknown)}",
            {"supported": sorted(STRATEGIES)},
            status_code=400,
        )
    control = control_client(workspace)
    params: dict[str, Any] = {
        "name": name,
        "eventExpiryDuration": event_expiry_days,
    }
    if description:
        params["description"] = description
    if strategies:
        params["memoryStrategies"] = [STRATEGIES[key] for key in strategies]
        role_arn = _platform_execution_role(control, workspace)
        if role_arn:
            params["memoryExecutionRoleArn"] = role_arn
    if namespace_keys:
        params["namespaceKeys"] = [_namespace_key_entry(k) for k in namespace_keys]
    created = control.create_memory(**params).get("memory", {})
    return _detail(created, memory_id_or_none(workspace))


def _namespace_key_entry(key: dict[str, Any]) -> dict[str, Any]:
    """Console shape → CreateMemory ``NamespaceKeyEntry`` (validation only if set)."""
    entry: dict[str, Any] = {"key": key["key"]}
    validation: dict[str, Any] = {}
    if key.get("allowed_values"):
        validation["allowedValues"] = list(key["allowed_values"])
    if key.get("regex_pattern"):
        validation["regexPattern"] = key["regex_pattern"]
    if validation:
        entry["validation"] = validation
    return entry


def _platform_execution_role(control: Any, workspace: WorkspaceContext) -> str | None:
    """Reuse the bootstrap memory's extraction role for new memories.

    Long-term strategies invoke extraction models under this role. Best-effort:
    a memory without one still works for short-term events, so a failed lookup
    must not block creation.
    """
    default_id = memory_id_or_none(workspace)
    if not default_id:
        return None
    try:
        raw = control.get_memory(memoryId=default_id).get("memory", {})
    except Exception:
        return None
    return raw.get("memoryExecutionRoleArn") or None


def delete_memory_resource(workspace: WorkspaceContext, memory_id: str) -> dict[str, Any]:
    if memory_id == memory_id_or_none(workspace):
        raise AppError(
            "memory.platform_protected",
            "the workspace's bootstrap memory is the platform default and "
            "cannot be deleted from the console",
            status_code=409,
        )
    control_client(workspace).delete_memory(memoryId=memory_id)
    return {"deleted": True, "id": memory_id}
