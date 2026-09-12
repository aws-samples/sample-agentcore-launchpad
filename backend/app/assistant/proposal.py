"""The bounded, untrusted proposal contract.

A proposal is the only structured thing the model (or the member) can hand the
platform, and it is **inert**: it names an agent, a model, a prompt, and references
into the workspace catalog by *key*; it never carries URLs, ARNs, S3 prefixes, role
names, environment variables, code, requirements or any AgentSpec member outside
the allowlist below. ``to_agent_spec`` is the single place a proposal becomes an
``AgentSpec`` — every referenced resource is re-read from the catalog snapshot the
server fetched, so the model can pick a tool but cannot define one.

``resource_bindings`` is what a member actually reviews and what an approval must
still resolve to: the spec **plus** the deployment-relevant identity of every
referenced resource (gateway ARN and outbound-auth identity, skill record + S3 path
+ content digest, KB gateway prerequisites, the memory ARN). It pins only the
configuration this one operation depends on; it does not claim AWS-wide immutability.

Extraction: after an ordinary model turn the reply is scanned for exactly one fenced
block tagged ``launchpad-proposal``. Anything else in the reply is conversation
text. A malformed block is kept as an *invalid* revision with its errors, never as
something an approval could execute. One serialized-UTF-8 byte cap applies to the
model block and to a member edit alike, before validation and before storage.
"""

import hashlib
import json
import re
from typing import Annotated, Any, Literal

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

Key = Annotated[str, Field(min_length=1, max_length=200, pattern=_KEY_RE)]
Line = Annotated[str, Field(min_length=1, max_length=1000)]
ShortText = Annotated[str, Field(max_length=2000)]

# The only memory choices the Harness API can actually enforce: no memory at all
# (``{"disabled": {}}``), or the workspace's existing shared AgentCore Memory with
# every strategy it is configured with. "Short-term only" is not expressible.
MemoryMode = Literal["disabled", "workspace"]


class GoldenTest(BaseModel):
    """Solution content only — never provisioned as an evaluator by this slice."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=64)
    input: str = Field(min_length=1, max_length=2000)
    expected_response: ShortText = ""
    expected_tools: list[Annotated[str, Field(max_length=200)]] = Field(
        default_factory=list, max_length=10
    )
    forbidden_behavior: Annotated[str, Field(max_length=1000)] = ""
    pass_criteria: Annotated[str, Field(max_length=1000)] = ""
    evaluator: Annotated[str, Field(max_length=200)] = ""
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
    tools: list[Key] = Field(default_factory=list, max_length=20)
    # Catalog skill names (registry AGENT_SKILLS records), never S3 paths.
    skills: list[Key] = Field(default_factory=list, max_length=10)
    # Managed knowledge base ids present in the catalog.
    knowledge_bases: list[Key] = Field(default_factory=list, max_length=10)
    memory: MemoryMode = "disabled"
    max_iterations: int = Field(default=10, ge=1, le=100)
    timeout_seconds: int = Field(default=300, ge=10, le=3600)
    # Solution content, shown for review and kept with the revision.
    summary: Annotated[str, Field(max_length=4000)] = ""
    requirements_baseline: list[Line] = Field(default_factory=list, max_length=40)
    assumptions: list[Line] = Field(default_factory=list, max_length=40)
    manual_tasks: list[Line] = Field(default_factory=list, max_length=40)
    golden_tests: list[GoldenTest] = Field(default_factory=list, max_length=40)
    evaluator_recommendations: list[Line] = Field(default_factory=list, max_length=40)


def serialized_bytes(raw: Any) -> int:
    """UTF-8 size of the canonical JSON of ``raw`` (what the cap is measured on)."""
    try:
        return len(json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        return PROPOSAL_MAX_BYTES + 1


def _check_lists(content: ProposalContent) -> list[str]:
    errors: list[str] = []
    for field in ("tools", "skills", "knowledge_bases"):
        values = getattr(content, field)
        if len(values) != len(set(values)):
            errors.append(f"{field} must not repeat an entry")
    if len({g.id for g in content.golden_tests}) != len(content.golden_tests):
        errors.append("golden_tests ids must be unique")
    return errors


def canonical_hash(content: dict[str, Any]) -> str:
    payload = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def revision_hash(content: dict[str, Any], bindings: dict[str, Any] | None) -> str:
    """What an approval names: the shown content AND the concrete bindings it
    resolved to. Either changing yields a different hash, so a member can never
    approve something other than exactly what was rendered."""
    return canonical_hash({"content": content, "bindings": bindings})


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
    """Shape validation only (no catalog). ``raw`` may be a JSON string or a dict.
    The byte cap is checked first, so an oversized object is never validated or
    kept."""
    data = raw
    if isinstance(raw, str):
        if len(raw.encode("utf-8")) > PROPOSAL_MAX_BYTES:
            return None, [f"proposal exceeds {PROPOSAL_MAX_BYTES} bytes"]
        try:
            data = json.loads(raw)
        except ValueError as exc:
            return None, [f"proposal block is not valid JSON: {exc}"]
    if not isinstance(data, dict):
        return None, ["proposal must be a JSON object"]
    if serialized_bytes(data) > PROPOSAL_MAX_BYTES:
        return None, [f"proposal exceeds {PROPOSAL_MAX_BYTES} bytes"]
    try:
        content = ProposalContent.model_validate(data)
    except ValidationError as exc:
        return None, [
            f"{'.'.join(str(p) for p in err['loc']) or 'proposal'}: {err['msg']}"
            for err in exc.errors()
        ][:40]
    errors = _check_lists(content)
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
    """Every reference must resolve to an entry of the given catalog snapshot, and
    every prerequisite of the chosen resources must exist in the workspace."""
    index = catalog_index(catalog)
    resources = catalog.get("resources") or {}
    errors: list[str] = []
    for key in content.tools:
        entry = index["tools"].get(key)
        if entry is None:
            errors.append(f"tools: '{key}' is not an available tool in this workspace")
        elif entry.get("attachable") is False:
            errors.append(f"tools: '{key}' is not attachable ({entry.get('reason') or 'n/a'})")
        elif entry.get("kind") == "gateway" and not (
            entry.get("gateway_arn") and entry.get("outbound_auth")
        ):
            errors.append(f"tools: '{key}' has no resolvable gateway ARN / outbound auth")
    for key in content.skills:
        entry = index["skills"].get(key)
        if entry is None:
            errors.append(f"skills: '{key}' is not an available skill in this workspace")
        elif not entry.get("content_digest"):
            errors.append(f"skills: '{key}' has no readable bundle content to pin")
    for kb_id in content.knowledge_bases:
        if kb_id not in index["knowledge_bases"]:
            errors.append(
                f"knowledge_bases: '{kb_id}' is not an active managed knowledge base here"
            )
    if content.knowledge_bases and not (
        resources.get("kb_gateway_id") and resources.get("kb_gateway_arn")
        and resources.get("oauth_provider_arn")
    ):
        errors.append(
            "knowledge_bases: this workspace has no ready knowledge-base gateway "
            "(launchpad-kb-gw + OAuth provider); mounting a KB here is manual work, the "
            "assistant never creates a gateway"
        )
    if content.memory == "workspace" and not resources.get("memory_arn"):
        errors.append("memory: this workspace has no shared AgentCore Memory to bind")
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
    shared = content.memory == "workspace"
    return AgentSpec(
        name=content.name,
        method="harness",
        model_id=content.model_id,
        model_source=content.model_source,
        system_prompt=content.system_prompt,
        tools=tools,
        skills=skills,
        knowledge_bases=kbs,
        # ``workspace`` = the shared memory with every strategy it carries; the
        # deployer attaches that ARN when either flag is set. ``disabled`` sends the
        # explicit ``{"disabled": {}}`` opt-out (an omitted member is NOT "no memory").
        memory=MemoryConfig(short_term=shared, long_term=shared, memory_id=None),
        max_iterations=content.max_iterations,
        timeout_seconds=content.timeout_seconds,
    )


def resource_bindings(content: ProposalContent, catalog: dict[str, Any]) -> dict[str, Any]:
    """The reviewed deployment identity: the spec plus every referenced resource's
    deployment-relevant identity (no secret values)."""
    index = catalog_index(catalog)
    resources = catalog.get("resources") or {}
    spec = to_agent_spec(content, catalog).model_dump()
    gateways: dict[str, Any] = {}
    remote_mcp: dict[str, Any] = {}
    for key in content.tools:
        entry = index["tools"][key]
        if entry["kind"] == "gateway":
            gateways[entry["gateway_id"]] = {
                "gateway_arn": entry.get("gateway_arn"),
                "gateway_name": entry.get("gateway_name") or entry["name"],
                "record_id": entry["record_id"],
                "auth_type": entry.get("auth_type"),
                # identity of the outbound auth (provider ARN, grant type, scopes) —
                # never a credential value
                "outbound_auth": entry.get("outbound_auth"),
            }
        else:
            remote_mcp[entry["name"]] = {"url": entry["url"], "record_id": entry.get("record_id")}
    skills = {
        key: {
            "record_id": index["skills"][key].get("record_id"),
            "path": index["skills"][key]["path"],
            "content_digest": index["skills"][key].get("content_digest"),
            "object_count": index["skills"][key].get("object_count"),
        }
        for key in content.skills
    }
    kb_gateway = (
        {
            "gateway_id": resources.get("kb_gateway_id"),
            "gateway_arn": resources.get("kb_gateway_arn"),
            "oauth_provider_arn": resources.get("oauth_provider_arn"),
        }
        if content.knowledge_bases
        else None
    )
    memory = {"mode": content.memory,
              "arn": resources.get("memory_arn") if content.memory == "workspace" else None}
    return {
        **spec,
        "resources": {
            "gateways": gateways,
            "remote_mcp": remote_mcp,
            "skills": skills,
            "kb_gateway": kb_gateway,
            "memory": memory,
            "execution_role_arn": resources.get("execution_role_arn"),
        },
    }


def binding_diff(before: dict[str, Any] | None, after: dict[str, Any]) -> list[str]:
    before = before or {}
    changed = sorted(k for k in set(before) | set(after) if k != "resources"
                     and before.get(k) != after.get(k))
    b_res, a_res = before.get("resources") or {}, after.get("resources") or {}
    changed += sorted(f"resources.{k}" for k in set(b_res) | set(a_res)
                      if b_res.get(k) != a_res.get(k))
    return changed


_VIEW_FIELDS = ("name", "method", "model_id", "model_source", "tools", "skills",
                "knowledge_bases", "memory", "max_iterations", "timeout_seconds", "resources")


def bindings_view(bindings: dict[str, Any] | None) -> dict[str, Any] | None:
    """The reviewer-facing subset of the bound spec (no prompt duplicate)."""
    if not bindings:
        return None
    return {k: bindings.get(k) for k in _VIEW_FIELDS}


def validate(
    raw: Any, catalog: dict[str, Any]
) -> tuple[ProposalContent | None, dict[str, Any], list[str]]:
    """Byte cap → shape → references → bindings, in one call.

    Returns ``(content | None, display_dict, errors)``. ``display_dict`` is the
    normalized content when the shape parsed (so an invalid-reference revision can
    still be shown and edited), the raw object when only the shape failed and it is
    small enough to keep, or a marker when the raw object is oversized.
    """
    content, errors = parse_content(raw)
    if content is None:
        if isinstance(raw, dict) and serialized_bytes(raw) <= PROPOSAL_MAX_BYTES:
            return None, raw, errors
        return None, {"_rejected": "oversized or unparseable proposal was not stored"}, errors
    errors = reference_errors(content, catalog)
    display = content.model_dump()
    if not errors:
        try:
            resource_bindings(content, catalog)
        except (ValueError, KeyError) as exc:  # pragma: no cover — defensive
            errors = [f"proposal cannot be turned into an agent spec: {exc}"]
    return (content if not errors else None), display, errors
