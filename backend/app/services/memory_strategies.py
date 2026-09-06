"""The platform's long-term memory strategy catalog — one source for bootstrap
(``services/bootstrap.ensure_memory``) and the console's create-memory form
(``services/memory_admin``).

Agents write under a scoped actor and the console reads ``/facts/...`` and
``/preferences/...`` namespaces, so any memory the platform provisions must
expose the same layout for extraction and the chat rail to work unchanged.
Summaries and episodes are additive: their namespaces keep the platform's flat
``/label/{actorId}`` style rather than the docs' ``/strategy/{memoryStrategyId}/...``
example, so the console's namespace resolution treats every strategy the same way.
"""

from typing import Any

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
    # One consolidated summary per session — the record lives under the session
    # so a long conversation can be resumed from its digest.
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
            "reflectionConfiguration": {"namespaceTemplates": ["/episodes/{actorId}"]},
        }
    },
}

# CreateMemory input key → the `type` GetMemory reports for that strategy.
STRATEGY_TYPES: dict[str, str] = {
    "semanticMemoryStrategy": "SEMANTIC",
    "userPreferenceMemoryStrategy": "USER_PREFERENCE",
    "summaryMemoryStrategy": "SUMMARIZATION",
    "episodicMemoryStrategy": "EPISODIC",
}

# Every built-in strategy, in catalog order — what the bootstrap memory carries.
DEFAULT_STRATEGIES: list[dict[str, Any]] = list(STRATEGIES.values())


def strategy_kind(strategy: dict[str, Any]) -> str:
    """The single CreateMemory key of one strategy entry (e.g. ``semanticMemoryStrategy``)."""
    return next(iter(strategy))


def strategy_name(strategy: dict[str, Any]) -> str:
    return str(strategy[strategy_kind(strategy)]["name"])


def missing_strategies(existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Catalog entries a memory lacks, judged by GetMemory's ``strategies``.

    A strategy counts as present when the memory already has one of the same
    ``type`` (whatever its name) or the same ``name`` (CreateMemory rejects a
    duplicate name, so adding it would fail anyway).
    """
    have_types = {str(s.get("type")) for s in existing}
    have_names = {str(s.get("name")) for s in existing}
    return [
        entry
        for entry in DEFAULT_STRATEGIES
        if STRATEGY_TYPES[strategy_kind(entry)] not in have_types
        and strategy_name(entry) not in have_names
    ]
