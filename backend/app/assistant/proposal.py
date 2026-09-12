"""The bounded, untrusted proposal contract.

A proposal is the only structured thing the model (or the member) can hand the
platform, and it is **inert**: it names an agent, a model, a prompt, and references
into the workspace catalog by *key*; it never carries URLs, ARNs, S3 prefixes, role
names, environment variables, code, requirements or any AgentSpec member outside
the allowlist below. ``to_agent_spec`` is the single place a proposal becomes an
``AgentSpec`` — every referenced resource is re-read from the catalog snapshot the
server fetched, so the model can pick a tool but cannot define one.

Extraction: after an ordinary model turn the reply is scanned for exactly one fenced
block tagged ``launchpad-proposal``. Anything else in the reply is conversation
text. A malformed block is kept as an *invalid* revision with its errors, never as
something an approval could execute.
"""

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.schemas.agent import (
    DEFAULT_MODEL_ID,
    AgentSpec,
    KnowledgeBaseRef,
    MemoryConfig,
    ToolRef,
)
from app.system_agents.presets import is_reserved_name

PROPOSAL_FENCE = "launchpad-proposal"
PROPOSAL_MAX_BYTES = 64_000
_FENCE_RE = re.compile(
    r"```" + PROPOSAL_FENCE + r"[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.DOTALL
)
_NAME_RE = r"^[a-z][a-z0-9-]{2,47}$"
_MODEL_ID_RE = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,120}$"
_KEY_RE = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$"
# Name prefixes the platform itself mints (kb-gateway targets, harness backing
# runtimes, per-agent roles); a business agent must not impersonate them.
_RESERVED_PREFIXES = ("launchpad-", "harness-", "system-")


class ProposalMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    short_term: bool = True
    long_term: bool = False


class GoldenTest(BaseModel):
    """Solution content only — never provisioned as an evaluator by this slice."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=64)
    input: str = Field(min_length=1, max_length=2000)
    expected_response: str = Field(default="", max_length=2000)
    expected_tools: list[str] = Field(default_factory=list, max_length=10)
    forbidden_behavior: str = Field(default="", max_length=1000)
    pass_criteria: str = Field(default="", max_length=1000)
    evaluator: str = Field(default="", max_length=200)
    source: Literal["customer_pain_point", "industry_assumption"] = "industry_assumption"


class ProposalContent(BaseModel):
    """Everything an approval may turn into a new managed Harness. ``extra="forbid"``
    is the allowlist: a member (or model) cannot smuggle ``env``, ``code``,
    ``requirements``, ``filesystem``, ``network``, ``allowed_tools``, ``protocol``
    or any other AgentSpec member through here."""

    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    name: str = Field(pattern=_NAME_RE)
    model_id: str = Field(default=DEFAULT_MODEL_ID, pattern=_MODEL_ID_RE)
    model_source: Literal["bedrock", "mantle"] = "bedrock"
    system_prompt: str = Field(min_length=1, max_length=20000)
    # Catalog keys (``gateway:<name>`` / ``mcp:<name>``), never URLs or ARNs.
    tools: list[str] = Field(default_factory=list, max_length=20)
    # Catalog skill names (registry AGENT_SKILLS records), never S3 paths.
    skills: list[str] = Field(default_factory=list, max_length=10)
    # Managed knowledge base ids present in the catalog.
    knowledge_bases: list[str] = Field(default_factory=list, max_length=10)
    memory: ProposalMemory = Field(default_factory=ProposalMemory)
    max_iterations: int = Field(default=10, ge=1, le=100)
    timeout_seconds: int = Field(default=300, ge=10, le=3600)
    # Solution content, shown for review and kept with the revision.
    summary: str = Field(default="", max_length=4000)
    requirements_baseline: list[str] = Field(default_factory=list, max_length=40)
    assumptions: list[str] = Field(default_factory=list, max_length=40)
    manual_tasks: list[str] = Field(default_factory=list, max_length=40)
    golden_tests: list[GoldenTest] = Field(default_factory=list, max_length=40)
    evaluator_recommendations: list[str] = Field(default_factory=list, max_length=40)


def _check_text_lists(content: ProposalContent) -> list[str]:
    errors: list[str] = []
    for field in ("requirements_baseline", "assumptions", "manual_tasks",
                  "evaluator_recommendations"):
        for index, item in enumerate(getattr(content, field)):
            if not isinstance(item, str) or not item.strip():
                errors.append(f"{field}[{index}] must be a non-empty string")
            elif len(item) > 1000:
                errors.append(f"{field}[{index}] exceeds 1000 characters")
    for field in ("tools", "skills", "knowledge_bases"):
        values = getattr(content, field)
        if len(values) != len(set(values)):
            errors.append(f"{field} must not repeat an entry")
        for index, item in enumerate(values):
            if not isinstance(item, str) or not re.match(_KEY_RE, item):
                errors.append(f"{field}[{index}] is not a catalog key")
    return errors


def canonical_hash(content: dict[str, Any]) -> str:
    """Stable identity of one revision's exact content (what an approval names)."""
    payload = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def revision_hash(content: dict[str, Any], bindings: dict[str, Any] | None) -> str:
    """What an approval names: the shown content AND the concrete bindings it
    resolved to. Either changing yields a different hash, so a member can never
    approve something other than exactly what was rendered."""
    return canonical_hash({"content": content, "bindings": bindings})


_BINDING_FIELDS = ("name", "method", "model_id", "model_source", "tools", "skills",
                   "knowledge_bases", "memory", "max_iterations", "timeout_seconds")


def bindings_view(bindings: dict[str, Any] | None) -> dict[str, Any] | None:
    """The reviewer-facing subset of the bound spec (no prompt duplicate)."""
    if not bindings:
        return None
    return {k: bindings.get(k) for k in _BINDING_FIELDS}


def binding_diff(before: dict[str, Any] | None, after: dict[str, Any]) -> list[str]:
    before = before or {}
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


def extract_block(text: str) -> tuple[str | None, list[str]]:
    """The single fenced proposal block of a reply, or why there is none usable.

    ``(None, [])`` = the reply carries no proposal (ordinary discussion).
    ``(None, [errors])`` = a block exists but is unusable (two blocks, oversize).
    """
    matches = _FENCE_RE.findall(text or "")
    if not matches:
        return None, []
    if len(matches) > 1:
        return None, [f"reply carries {len(matches)} proposal blocks; exactly one is allowed"]
    block = matches[0]
    if len(block.encode("utf-8")) > PROPOSAL_MAX_BYTES:
        return None, [f"proposal block exceeds {PROPOSAL_MAX_BYTES} bytes"]
    return block, []


def parse_content(raw: Any) -> tuple[ProposalContent | None, list[str]]:
    """Shape validation only (no catalog). ``raw`` may be a JSON string or a dict."""
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except ValueError as exc:
            return None, [f"proposal block is not valid JSON: {exc}"]
    if not isinstance(data, dict):
        return None, ["proposal must be a JSON object"]
    try:
        content = ProposalContent.model_validate(data)
    except ValidationError as exc:
        return None, [
            f"{'.'.join(str(p) for p in err['loc']) or 'proposal'}: {err['msg']}"
            for err in exc.errors()
        ]
    errors = _check_text_lists(content)
    return (content if not errors else None), errors


# ---------------------------------------------------------------------------
# catalog references
# ---------------------------------------------------------------------------


def catalog_index(catalog: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    return {
        "tools": {t["key"]: t for t in catalog.get("tools") or [] if t.get("key")},
        "skills": {s["key"]: s for s in catalog.get("skills") or [] if s.get("key")},
        "knowledge_bases": {
            k["kb_id"]: k for k in catalog.get("knowledge_bases") or [] if k.get("kb_id")
        },
    }


def reference_errors(content: ProposalContent, catalog: dict[str, Any]) -> list[str]:
    """Every reference must resolve to an entry of the given catalog snapshot."""
    index = catalog_index(catalog)
    errors: list[str] = []
    for key in content.tools:
        entry = index["tools"].get(key)
        if entry is None:
            errors.append(f"tools: '{key}' is not an available tool in this workspace")
        elif entry.get("attachable") is False:
            errors.append(f"tools: '{key}' is not attachable ({entry.get('reason') or 'n/a'})")
    for key in content.skills:
        if key not in index["skills"]:
            errors.append(f"skills: '{key}' is not an available skill in this workspace")
    for kb_id in content.knowledge_bases:
        if kb_id not in index["knowledge_bases"]:
            errors.append(
                f"knowledge_bases: '{kb_id}' is not an active managed knowledge base here"
            )
    if is_reserved_name(content.name):
        errors.append(f"name: '{content.name}' is reserved for a system-managed preset")
    if content.name.startswith(_RESERVED_PREFIXES):
        errors.append(f"name: '{content.name}' uses a platform-reserved prefix")
    return errors


def to_agent_spec(content: ProposalContent, catalog: dict[str, Any]) -> AgentSpec:
    """The ONLY proposal → AgentSpec mapping. Resource details come from the catalog
    entry the key names, never from the proposal. Raises ``KeyError`` on a dangling
    reference (callers validate with ``reference_errors`` first)."""
    index = catalog_index(catalog)
    tools: list[ToolRef] = []
    for key in content.tools:
        entry = index["tools"][key]
        if entry["kind"] == "gateway":
            tools.append(
                ToolRef(
                    type="gateway",
                    name=entry["name"],
                    config={"record_id": entry["record_id"], "gateway_id": entry["gateway_id"]},
                )
            )
        else:
            tools.append(ToolRef(type="mcp", name=entry["name"], config={"url": entry["url"]}))
    skills = [index["skills"][key]["path"] for key in content.skills]
    kbs = [
        KnowledgeBaseRef(
            kb_id=kb_id,
            name=index["knowledge_bases"][kb_id].get("name") or "",
            description=index["knowledge_bases"][kb_id].get("description") or "",
        )
        for kb_id in content.knowledge_bases
    ]
    return AgentSpec(
        name=content.name,
        method="harness",
        model_id=content.model_id,
        model_source=content.model_source,
        system_prompt=content.system_prompt,
        tools=tools,
        skills=skills,
        knowledge_bases=kbs,
        memory=MemoryConfig(
            short_term=content.memory.short_term,
            long_term=content.memory.long_term,
            memory_id=None,
        ),
        max_iterations=content.max_iterations,
        timeout_seconds=content.timeout_seconds,
    )


def validate(
    raw: Any, catalog: dict[str, Any]
) -> tuple[ProposalContent | None, dict[str, Any], list[str]]:
    """Shape + reference validation in one call.

    Returns ``(content | None, display_dict, errors)``. ``display_dict`` is the
    normalized content when the shape parsed (so an invalid-reference revision can
    still be shown and edited) or the raw object otherwise (bounded).
    """
    content, errors = parse_content(raw)
    if content is None:
        shown = raw if isinstance(raw, dict) else {}
        return None, _bounded(shown), errors
    errors = reference_errors(content, catalog)
    display = content.model_dump()
    if not errors:
        try:
            to_agent_spec(content, catalog)
        except (ValueError, KeyError) as exc:  # pragma: no cover — defensive
            errors = [f"proposal cannot be turned into an agent spec: {exc}"]
    return (content if not errors else None), display, errors


def _bounded(data: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(data, ensure_ascii=False)
    if len(text) > PROPOSAL_MAX_BYTES:
        return {"_truncated": True}
    return data
