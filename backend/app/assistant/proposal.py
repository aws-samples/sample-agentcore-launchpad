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

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.assistant.recommendation_intent import (
    RECOMMENDATION_SHAPE_RULE,
    dump_entry,
    normalise_entries,
)
from app.schemas.agent import (
    DEFAULT_MODEL_ID,
    AgentSpec,
    KnowledgeBaseRef,
    MemoryConfig,
    ToolRef,
    agent_default_model_id,
    allowed_model_ids,
    model_available,
    model_enabled,
    model_provider,
    model_visible,
)
from app.services import memory_bootstrap
from app.system_agents.presets import is_reserved_name

PROPOSAL_FENCE = "launchpad-proposal"
# A revision of the latest stored proposal as RFC 6902 operations: a correction costs
# the model the changed members only, never a second copy of the whole proposal.
PATCH_FENCE = "launchpad-proposal-patch"
PATCH_MAX_OPERATIONS = 200

# The model a proposed agent gets unless the architect names another — the Create
# Agent wizard's default (``MODEL_CATALOG.bedrock[0]`` in frontend/src/lib/models.ts,
# which the proposal editor already falls back to). Safe for every proposal: an
# approval always creates a self-built Harness (native Bedrock, Converse). The CN
# catalog (spec C3) has exactly two models, so this now IS ``AgentSpec``'s
# DEFAULT_MODEL_ID — kept as its own name because a stored spec without a model_id
# meaning "the schema default" and a proposal without one meaning "the wizard
# default" are different claims that happen to resolve to the same id today.
# N17-obs252: this constant is only the import-time render baseline of the protocol
# preamble; the LIVE default — `ProposalContent.model_id`'s default_factory and the id
# `assistant.service.protocol_preamble()` states to the architect each turn — is the
# `agent_default` model role (`agent_default_model_id()`), never this literal.
PROPOSAL_DEFAULT_MODEL_ID = DEFAULT_MODEL_ID
PROPOSAL_MAX_BYTES = 64_000
# Every fenced block of a reply: ``` or ~~~ (3+, the closing fence repeats the opening
# one), with its info string. Tag recognition happens on the info string afterwards, so
# ```json launchpad-proposal, ```launchpad-proposal json, ~~~LAUNCHPAD-PROPOSAL and the
# plain ```launchpad-proposal all name the same block (N10v3-bug4: GLM 5.3 prefixed the
# tag with the language hint on three turns and a complete proposal was read as prose).
# The closing fence may be LONGER than the opening one (CommonMark; review #7).
_FENCED_RE = re.compile(
    r"(?P<fence>`{3,}|~{3,})(?P<info>[^\n`~]*)\r?\n(?P<body>.*?)\r?\n[ \t]*(?P=fence)[`~]*"
    r"[ \t]*(?=\r?\n|$)",
    re.DOTALL,
)
# The heading the protocol preamble puts over the stored proposal's JSON. A reply that
# carries it is quoting / discussing that JSON, so the UNFENCED fallback must not read
# the quoted object as a new proposal (review #6); tagged blocks are unaffected.
STORED_PROPOSAL_MARKER = "Current stored proposal"


def _tag_re(tag: str) -> re.Pattern[str]:
    # the tag as a whole word of the info string: `launchpad-proposal` must not match
    # inside `launchpad-proposal-patch` (or `launchpad-proposals`), hence the explicit
    # word boundaries that also treat `-` and `_` as word characters
    return re.compile(r"(?<![\w-])" + re.escape(tag) + r"(?![\w-])", re.IGNORECASE)


_PROPOSAL_TAG_RE = _tag_re(PROPOSAL_FENCE)
_PATCH_TAG_RE = _tag_re(PATCH_FENCE)
# An info string that names ANY platform contract (proposal, patch, preparation, …): a
# block so tagged is never a fallback candidate for a different contract.
_PLATFORM_TAG_RE = re.compile(r"(?<![\w-])launchpad-[\w-]+", re.IGNORECASE)
# Where an unfenced JSON object may start: a brace followed by a quoted key.
_OBJECT_START_RE = re.compile(r"\{\s*\"")
_DECODER = json.JSONDecoder()
_NAME_RE = r"^[a-z][a-z0-9-]{2,47}$"
_MODEL_ID_RE = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,120}$"
_KEY_RE = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$"
# Name prefixes the platform itself mints (kb-gateway targets, harness backing
# runtimes, per-agent roles); a business agent must not impersonate them.
_RESERVED_PREFIXES = ("launchpad-", "harness-", "system-")

Key = Annotated[str, Field(min_length=1, max_length=200, pattern=_KEY_RE)]
Line = Annotated[str, Field(min_length=1, max_length=1000)]
ToolName = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")]
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
    # A red-team test: the input deliberately pushes the agent across a red line —
    # embedded instructions / role overrides, use of insider or nonpublic information,
    # pressure for prohibited advice. Still evaluated; Launchpad keeps the session out of
    # the evidence the prompt optimizer reads (``evaluation/recommendations.py::
    # adversarial_sessions``), so AI recommendations never learn from such traces.
    adversarial: bool = False


class EvaluatorRecommendation(BaseModel):
    """One structured ``evaluator_recommendations`` entry (WB-006 / WB-009). The sentence
    is ``text``; the optional members state what the prose used to carry only in words:
    the evaluator it names (a listed ``Builtin.*`` id or a seed evaluator key / name),
    whether it **blocks publishing**, the **threshold** (0–1) and the **metric**. A plain
    string stays valid — the plan then parses the same facts out of the prose
    (``recommendation_intent.parse_intent``). ``note`` is the platform's repair note when
    an object of another shape was normalised into this one."""

    model_config = ConfigDict(extra="forbid")
    text: Line
    evaluator_id: Annotated[str, Field(min_length=1, max_length=96,
                                       pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")] | None = None
    blocking: bool | None = None
    threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    metric: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    note: Annotated[str, Field(max_length=1000)] | None = None


class EvaluationPlanSeed(BaseModel):
    """The optional structured evaluation recommendation of a proposal: typed
    evaluators (kinds existing / judge / derived / code — every one an AgentCore
    evaluator), typed single-session scenarios (turns, references),
    ``recommendation_keys`` mapping a prose recommendation index to entry keys, and
    ``blocked_golden_tests`` for tests no AgentCore evaluator can compute (kept with
    their reason; the obligation is described under ``manual_tasks``). Validated by the
    plan contract's ``seed_errors`` — shape first, then rules and references — so a
    malformed seed is an *invalid* revision, never a 500. Nothing here is executed;
    the plan draft merely starts from it."""

    model_config = ConfigDict(extra="forbid")
    evaluators: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    scenarios: list[dict[str, Any]] = Field(default_factory=list, max_length=40)
    recommendation_keys: dict[str, list[str]] = Field(default_factory=dict)
    blocked_golden_tests: list[dict[str, Any]] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def _typed(self) -> "EvaluationPlanSeed":
        from app.assistant.evaluation_plan import seed_errors

        errors = seed_errors(self.model_dump())
        if errors:
            raise ValueError(errors[0])
        return self


# ── Agent-DLC five-dimension fishbone (launch barriers the customer confirmed) ─────

FISHBONE_DIMENSIONS: tuple[str, ...] = (
    "cognition", "quality", "responsibility", "cost", "performance", "other",
)
FISHBONE_SLOTS_PER_DIMENSION = 3
FishboneCoverage = Literal["confirmed", "explored_empty", "unresolved"]


class FishboneBarrier(BaseModel):
    """One sticky note: a barrier the customer stated, in the customer's terms — or,
    with ``confirmed: false``, a barrier the architect suggested for a dimension the
    customer has not confirmed yet."""

    model_config = ConfigDict(extra="forbid")
    sticky_text: Annotated[str, Field(min_length=1, max_length=120)]
    evidence: Annotated[str, Field(max_length=1000)] = ""
    customer_quote: Annotated[str, Field(max_length=1000)] = ""
    confirmed: bool = False
    selected: bool = False


class FishboneParkingLotItem(BaseModel):
    """A solution the customer proposed during discovery, kept apart from the barriers."""

    model_config = ConfigDict(extra="forbid")
    original: Annotated[str, Field(min_length=1, max_length=1000)]
    converted_to: Annotated[str, Field(max_length=200)] = ""


class Fishbone(BaseModel):
    """The structured fishbone the console renders (mirrors the Agent-DLC skill's
    ``fishbone-data.json``): metadata, per-dimension coverage, barriers and the parking
    lot. Validated cross-field by ``fishbone_errors`` — a violation makes the proposal
    an *invalid* revision, never a 500. Inert: nothing is executed from it."""

    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    customer: Annotated[str, Field(min_length=1, max_length=200)]
    date: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
    use_case: Annotated[str, Field(min_length=1, max_length=500)]
    service_target: Literal["internal", "b2b", "b2c"]
    coverage: dict[str, FishboneCoverage]
    barriers: dict[str, list[FishboneBarrier]]
    parking_lot: list[FishboneParkingLotItem] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def _typed(self) -> "Fishbone":
        errors = fishbone_errors(self)
        if errors:
            raise ValueError(errors[0])
        return self


def fishbone_errors(fb: Fishbone) -> list[str]:
    """Cross-field rules of the methodology: exactly the six dimensions, at most three
    selected notes per dimension, a selected note is confirmed, and coverage
    ``confirmed`` needs a confirmed barrier while an empty/unresolved dimension carries
    none, an ``unresolved`` dimension is never blank (it carries the suggestion still
    awaiting the customer's yes) and the whole fishbone holds at least one confirmed
    barrier. Pure — every problem is a message."""
    errors: list[str] = []
    dims = set(FISHBONE_DIMENSIONS)
    unknown = sorted((set(fb.coverage) | set(fb.barriers)) - dims)
    if unknown:
        errors.append(f"fishbone: unknown dimensions {unknown}")
    missing = sorted(dims - set(fb.coverage))
    if missing:
        errors.append(f"fishbone.coverage: missing dimensions {missing}")
    for dim in FISHBONE_DIMENSIONS:
        notes = fb.barriers.get(dim, [])
        if len(notes) > 20:
            errors.append(f"fishbone.barriers.{dim}: more than 20 barriers")
        selected = [b for b in notes if b.selected]
        if len(selected) > FISHBONE_SLOTS_PER_DIMENSION:
            errors.append(f"fishbone.barriers.{dim}: {len(selected)} selected barriers "
                          f"(max {FISHBONE_SLOTS_PER_DIMENSION})")
        for b in selected:
            if not b.confirmed:
                errors.append(f"fishbone.barriers.{dim}: selected barrier is not confirmed: "
                              f"{b.sticky_text!r}")
        confirmed = any(b.confirmed for b in notes)
        state = fb.coverage.get(dim)
        if state == "confirmed" and not confirmed:
            errors.append(f"fishbone.coverage.{dim}: 'confirmed' without a confirmed barrier")
        if state in ("explored_empty", "unresolved") and confirmed:
            errors.append(f"fishbone.coverage.{dim}: '{state}' but a barrier is confirmed")
        if state == "unresolved" and not notes:
            errors.append(f"fishbone.barriers.{dim}: 'unresolved' dimension is blank — ask "
                          "the customer about it, or carry the barrier you suggested for "
                          "this scenario as a note with confirmed: false")
    if not any(b.confirmed for notes in fb.barriers.values() for b in notes):
        errors.append("fishbone: no confirmed barrier — omit the member until the customer "
                      "confirmed at least one")
    return errors


class ProposalContent(BaseModel):
    """Everything an approval may turn into a new self-built Harness. ``extra="forbid"``
    is the allowlist: a member (or model) cannot smuggle ``env``, ``code``,
    ``requirements``, ``filesystem``, ``network``, ``allowed_tools``, ``protocol``
    or any other AgentSpec member through here."""

    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    name: str = Field(pattern=_NAME_RE)
    # r15-e: a proposal without a model gets the live `agent_default` role (an approval
    # creates a Harness spec, whose default this is); the constant above is what that
    # resolves to when no administrator override / env / yaml names another model.
    model_id: str = Field(default_factory=lambda: agent_default_model_id(), pattern=_MODEL_ID_RE)
    model_source: Literal["bedrock"] = "bedrock"
    system_prompt: str = Field(min_length=1, max_length=20000)
    # Catalog keys (``gateway:<name>`` / ``mcp:<name>``), never URLs or ARNs.
    tools: list[Key] = Field(default_factory=list, max_length=20)
    # Optional narrowing of selected Gateway attachments to some of their runtime
    # callable names (``{"gateway:hr-database": ["hr-database___get_employee"]}``). A
    # selected Gateway without an entry exposes exactly its declared tools. Omitted
    # from the stored content when absent (hash stability of older revisions).
    tool_functions: dict[Key, Annotated[list[ToolName], Field(min_length=1, max_length=50)]] \
        | None = Field(default=None, max_length=20)
    # Native command/filesystem tools (not attachments): a proposal starts with both,
    # like the Create Agent wizard; an explicit list (including []) is kept as given.
    native_tools: list[Literal["shell", "file_operations"]] = Field(
        default_factory=lambda: ["shell", "file_operations"], max_length=2,
    )
    # AgentCore built-in tools mounted on the Harness (ToolRef type "builtin").
    builtin_tools: list[Literal["code-interpreter"]] = Field(default_factory=list, max_length=1)
    # Catalog skill names (registry AGENT_SKILLS records), never S3 paths.
    skills: list[Key] = Field(default_factory=list, max_length=10)
    # Managed knowledge base ids present in the catalog.
    knowledge_bases: list[Key] = Field(default_factory=list, max_length=10)
    memory: MemoryMode = "disabled"
    max_iterations: int = Field(default=100, ge=1, le=100)
    timeout_seconds: int = Field(default=600, ge=10, le=3600)
    # Solution content, shown for review and kept with the revision.
    summary: Annotated[str, Field(max_length=4000)] = ""
    requirements_baseline: list[Line] = Field(default_factory=list, max_length=40)
    assumptions: list[Line] = Field(default_factory=list, max_length=40)
    manual_tasks: list[Line] = Field(default_factory=list, max_length=40)
    golden_tests: list[GoldenTest] = Field(default_factory=list, max_length=40)
    # A sentence, or a structured entry carrying the evaluator / blocking / threshold /
    # metric the sentence states (additive: every stored string revision is unchanged).
    evaluator_recommendations: list[Line | EvaluatorRecommendation] = Field(
        default_factory=list, max_length=40
    )
    # Optional structured seed for the SEPARATE evaluation-assets plan (SE-047): a
    # bounded object the member later reviews/edits; inert here, validated by
    # ``EvaluationPlanSeed`` below, omitted from the stored content when absent so
    # every pre-existing revision hashes exactly as before.
    evaluation_plan: EvaluationPlanSeed | None = None
    # Optional Agent-DLC launch-barrier fishbone the customer confirmed during intake:
    # rendered by the console in the proposal panel, never executed; omitted from the
    # stored content when absent (hash-stability rule: a pre-existing revision without
    # one hashes exactly as before).
    fishbone: Fishbone | None = None


def content_dump(content: ProposalContent) -> dict[str, Any]:
    """New revisions include explicit selection defaults. Optional seed/fishbone
    members are omitted when absent; historical rows are returned as stored."""
    data = content.model_dump()
    for optional in ("evaluation_plan", "fishbone", "tool_functions"):
        if data.get(optional) is None:
            data.pop(optional, None)
    # a structured recommendation entry is stored without its unset members; strings
    # are untouched (hash stability of every pre-existing revision)
    data["evaluator_recommendations"] = [
        dump_entry(e) for e in data.get("evaluator_recommendations") or []
    ]
    return data


def serialized_bytes(raw: Any) -> int:
    """UTF-8 size of the canonical JSON of ``raw`` (what the cap is measured on)."""
    try:
        return len(json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        return PROPOSAL_MAX_BYTES + 1


def _check_lists(content: ProposalContent) -> list[str]:
    errors: list[str] = []
    for field in ("tools", "skills", "knowledge_bases", "native_tools", "builtin_tools"):
        values = getattr(content, field)
        if len(values) != len(set(values)):
            errors.append(f"{field} must not repeat an entry")
    for key, names in (content.tool_functions or {}).items():
        if len(names) != len(set(names)):
            errors.append(f"tool_functions.{key} must not repeat a tool name")
    if len({g.id for g in content.golden_tests}) != len(content.golden_tests):
        errors.append("golden_tests ids must be unique")
    seed = content.evaluation_plan
    if seed is not None:
        from app.assistant.evaluation_plan import seed_errors

        errors += seed_errors(seed.model_dump(), proposal_content=content.model_dump())
        errors += _seed_cross_errors(seed.model_dump(), content.model_dump())
    if not errors:
        from app.assistant.evaluation_plan import draft_size_errors

        errors += draft_size_errors(content_dump(content))
    return errors


def _seed_cross_errors(seed: dict[str, Any], content: dict[str, Any]) -> list[str]:
    """The seed ↔ proposal agreements (``recommendation_keys`` indexes name a prose
    recommendation; every golden test a scenario / block / evaluator names exists). On
    plain dicts, so they are reported even when the seed's own shape validator already
    refused the proposal (N-ARCH-obs5: the member sees every disagreement at once)."""
    errors: list[str] = []
    recs = content.get("evaluator_recommendations") or []
    gt_ids = {str(g.get("id")) for g in content.get("golden_tests") or [] if isinstance(g, dict)}
    rec_keys = seed.get("recommendation_keys")
    for idx in (rec_keys if isinstance(rec_keys, dict) else {}):
        if isinstance(idx, str) and idx.isdigit() and int(idx) >= len(recs):
            errors.append(f"evaluation_plan.recommendation_keys: index {idx} has no "
                          f"evaluator_recommendations entry (the proposal lists {len(recs)})")
    for sc in seed.get("scenarios") or []:
        if isinstance(sc, dict) and str(sc.get("golden_test_id")) not in gt_ids:
            errors.append(f"evaluation_plan.scenarios: golden test "
                          f"'{sc.get('golden_test_id')}' does not exist")
    for b in seed.get("blocked_golden_tests") or []:
        if isinstance(b, dict) and str(b.get("golden_test_id")) not in gt_ids:
            errors.append(f"evaluation_plan.blocked_golden_tests: golden test "
                          f"'{b.get('golden_test_id')}' does not exist")
    for e in seed.get("evaluators") or []:
        if not isinstance(e, dict):
            continue
        for gt in e.get("golden_test_ids") or []:
            if gt not in gt_ids:
                errors.append(f"evaluation_plan.evaluators.{e.get('key')}: golden test "
                              f"'{gt}' does not exist")
    return errors


def canonical_hash(content: dict[str, Any]) -> str:
    payload = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def revision_hash(content: dict[str, Any], bindings: dict[str, Any] | None) -> str:
    """What an approval names: the shown content AND the concrete bindings it
    resolved to. Either changing yields a different hash, so a member can never
    approve something other than exactly what was rendered."""
    return canonical_hash({"content": content, "bindings": bindings})


class FoundBlock:
    """One recognised contract block of a reply: its kind, raw body and text span."""

    __slots__ = ("kind", "body", "start", "end", "tagged")

    def __init__(self, kind: str, body: str, start: int, end: int, *, tagged: bool) -> None:
        self.kind = kind  # "proposal" | "patch"
        self.body = body
        self.start = start
        self.end = end
        self.tagged = tagged  # named by its fence tag, or recognised by the contract shape


def looks_like_proposal(data: Any) -> bool:
    """The contract's discriminator: a JSON object that says ``version: 1`` and carries
    the two members no proposal is without. Used ONLY to recognise an untagged block the
    model dropped the tag from — validation stays ``parse_content``'s."""
    return (
        isinstance(data, dict)
        and data.get("version") == 1
        and isinstance(data.get("name"), str)
        and isinstance(data.get("system_prompt"), str)
    )


def _untagged_candidate(body: str) -> bool:
    if len(body.encode("utf-8")) > PROPOSAL_MAX_BYTES:
        return False
    try:
        return looks_like_proposal(json.loads(body))
    except ValueError:
        return False


def find_blocks(text: str) -> list[FoundBlock]:
    """Every contract block of ``text`` in order of appearance.

    Tagged fences first: a fence whose info string names ``launchpad-proposal`` (as a
    whole word, anywhere, any case) is a proposal, ``launchpad-proposal-patch`` a patch.
    Only when NO tagged block exists does the fallback look for an untagged proposal —
    a fence with a non-platform info string (```` ```json ````, ```` ``` ````) whose body
    is a JSON object with the contract's discriminator (``looks_like_proposal``), else
    an unfenced object with the same shape — unless the reply quotes the stored proposal
    (``STORED_PROPOSAL_MARKER``), in which case no unfenced object is read. A plain
    ```` ```json ```` example without the discriminator stays prose. The patch contract
    has no fallback: operations are a bare list, indistinguishable from any other JSON
    array.
    """
    text = text or ""
    found: list[FoundBlock] = []
    fenced = list(_FENCED_RE.finditer(text))
    for m in fenced:
        info = m.group("info")
        if _PATCH_TAG_RE.search(info):
            found.append(FoundBlock("patch", m.group("body"), m.start(), m.end(), tagged=True))
        elif _PROPOSAL_TAG_RE.search(info):
            found.append(FoundBlock("proposal", m.group("body"), m.start(), m.end(),
                                    tagged=True))
    if found:
        return found
    for m in fenced:
        if _PLATFORM_TAG_RE.search(m.group("info")):
            continue  # another platform contract (e.g. launchpad-preparation)
        body = m.group("body").strip()
        if _untagged_candidate(body):
            found.append(FoundBlock("proposal", body, m.start(), m.end(), tagged=False))
    if found:
        return found
    # unfenced: a bare object in the prose. Inside-fence positions are skipped so a
    # non-proposal ```json example is not re-read as prose; a reply quoting the stored
    # proposal under the preamble's heading is discussion, never a new proposal.
    if STORED_PROPOSAL_MARKER.lower() in text.lower():
        return found
    spans = [(m.start(), m.end()) for m in fenced]
    pos = 0
    while True:
        start = _OBJECT_START_RE.search(text, pos)
        if start is None:
            break
        at = start.start()
        pos = at + 1
        if any(a <= at < b for a, b in spans):
            continue
        try:
            data, end = _DECODER.raw_decode(text, at)
        except ValueError:
            continue
        if looks_like_proposal(data):
            body = text[at:end]
            if len(body.encode("utf-8")) <= PROPOSAL_MAX_BYTES:
                found.append(FoundBlock("proposal", body, at, end, tagged=False))
            pos = end
    return found


def _single(blocks: list[FoundBlock], what: str) -> tuple[str | None, list[str]]:
    if not blocks:
        return None, []
    if len(blocks) > 1:
        return None, [f"reply carries {len(blocks)} {what} blocks; exactly one is allowed"]
    block = blocks[0].body
    if len(block.encode("utf-8")) > PROPOSAL_MAX_BYTES:
        return None, [f"{what} block exceeds {PROPOSAL_MAX_BYTES} bytes"]
    return block, []


def extract_block(text: str) -> tuple[str | None, list[str]]:
    """The single proposal block of a reply, or why there is none usable.

    ``(None, [])`` = the reply carries no proposal (ordinary discussion).
    ``(None, [errors])`` = a block exists but is unusable (two blocks, oversize).
    See ``find_blocks`` for what counts as a proposal block.
    """
    return _single([b for b in find_blocks(text) if b.kind == "proposal"], "proposal")


def extract_patch(text: str) -> tuple[str | None, list[str]]:
    """The single revision block of a reply — ``extract_block``'s contract for
    ``launchpad-proposal-patch``. A reply carries a full block OR a patch, never both
    (the caller refuses the pair)."""
    return _single([b for b in find_blocks(text) if b.kind == "patch"], "proposal revision")


def strip_proposal_blocks(text: str, marker: str) -> str:
    """``text`` with every recognised proposal / patch block replaced by ``marker`` —
    the replay view of an assistant turn (its stored result travels in the preamble
    instead). Uses the same recognition as extraction, so a block the platform read as
    a proposal is never replayed to the model as prose."""
    out = text or ""
    for block in sorted(find_blocks(out), key=lambda b: b.start, reverse=True):
        out = out[:block.start] + marker + out[block.end:]
    return out


def is_patch_base(content: Any) -> bool:
    """A stored revision a patch can apply to: a real proposal object, not the marker
    of a block that was too large or unparseable to keep."""
    return isinstance(content, dict) and bool(content) and "_rejected" not in content


def _pointer(path: Any) -> list[str] | None:
    if not isinstance(path, str) or not path.startswith("/"):
        return None
    return [t.replace("~1", "/").replace("~0", "~") for t in path[1:].split("/")]


def _index(container: list[Any], token: str, *, insert: bool) -> int | None:
    if insert and token == "-":
        return len(container)
    if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
        return None
    i = int(token)
    return i if i < len(container) + (1 if insert else 0) else None


def apply_patch(base: dict[str, Any], raw: Any, *, base_revision: int) -> tuple[
    dict[str, Any] | None, list[str]
]:
    """Apply ``{"base_revision": N, "operations": [...]}`` to a deep copy of ``base``.

    RFC 6902 ``add`` / ``remove`` / ``replace`` / ``test`` with JSON Pointer paths
    (``-`` appends to an array). All or nothing: the first failing operation rejects the
    whole revision, and the result still goes through the ordinary ``validate``.
    ``base_revision`` must name the revision the platform offered as the base, so a
    patch written against an older view can never land on newer content. The messages
    speak of "the change", not of patches: the member sees them.
    """
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except ValueError as exc:
            return None, [f"the proposal change is not valid JSON: {exc}"]
    if not isinstance(data, dict) or set(data) - {"base_revision", "operations"}:
        return None, ["the proposal change must be an object with base_revision and "
                      "operations only"]
    if data.get("base_revision") != base_revision:
        return None, [f"the proposal change targets revision {data.get('base_revision')!r}, "
                      f"but the current stored proposal is revision {base_revision}"]
    ops = data.get("operations")
    if not isinstance(ops, list) or not ops:
        return None, ["the proposal change lists no operations"]
    if len(ops) > PATCH_MAX_OPERATIONS:
        return None, [f"the proposal change has {len(ops)} operations "
                      f"(max {PATCH_MAX_OPERATIONS})"]
    doc: Any = json.loads(json.dumps(base))
    for n, op in enumerate(ops, start=1):
        where = f"change {n}"
        if not isinstance(op, dict) or op.get("op") not in ("add", "remove", "replace", "test"):
            return None, [f"{where}: op must be add, remove, replace or test"]
        kind = op["op"]
        allowed = {"op", "path"} | ({"value"} if kind != "remove" else set())
        if set(op) - allowed or (kind != "remove" and "value" not in op):
            return None, [f"{where} ({kind}): takes exactly {sorted(allowed)}"]
        tokens = _pointer(op.get("path"))
        if not tokens or tokens == [""]:
            return None, [f"{where}: path must point inside the proposal, e.g. /system_prompt"]
        where = f"{where} ({kind} {op['path']})"
        parent: Any = doc
        for token in tokens[:-1]:
            if isinstance(parent, dict) and token in parent:
                parent = parent[token]
            elif isinstance(parent, list) and _index(parent, token, insert=False) is not None:
                parent = parent[int(token)]
            else:
                return None, [f"{where}: path does not exist"]
        last = tokens[-1]
        if isinstance(parent, dict):
            exists = last in parent
            if kind in ("remove", "replace", "test") and not exists:
                return None, [f"{where}: path does not exist"]
            if kind == "test":
                if parent[last] != op["value"]:
                    return None, [f"{where}: current value differs from the tested value"]
            elif kind == "remove":
                del parent[last]
            else:
                parent[last] = op["value"]
        elif isinstance(parent, list):
            i = _index(parent, last, insert=kind == "add")
            if i is None:
                return None, [f"{where}: array index out of range"]
            if kind == "test":
                if parent[i] != op["value"]:
                    return None, [f"{where}: current value differs from the tested value"]
            elif kind == "remove":
                parent.pop(i)
            elif kind == "replace":
                parent[i] = op["value"]
            else:
                parent.insert(i, op["value"])
        else:
            return None, [f"{where}: path does not exist"]
    return doc, []


MAX_ERRORS = 40
# Allowed values the operator-readable messages quote for an enumerated member.
_MEMBER_CHOICES: dict[str, str] = {
    "memory": "`disabled` or `workspace`",
    "model_source": "`bedrock`",
    "native_tools": "`shell` and/or `file_operations`",
    "builtin_tools": "`code-interpreter`",
}


def _dedupe(errors: list[str]) -> list[str]:
    return list(dict.fromkeys(e for e in errors if e))[:MAX_ERRORS]


def _readable(err: dict[str, Any], *, prefix: str = "") -> str:
    """One pydantic error as an operator-readable sentence: the member path, what is
    wrong, and the allowed values when known — never the raw dump (``input_value``,
    the pydantic URL) the default ``str(ValidationError)`` carries (N-ARCH-obs3)."""
    loc = [str(part) for part in err.get("loc", ())]
    path = ".".join(loc) or "proposal"
    if prefix and loc:
        path = f"{prefix}.{path}"
    kind = str(err.get("type", ""))
    msg = str(err.get("msg", "")).strip()
    ctx = err.get("ctx") or {}
    leaf = loc[-1] if loc else ""
    if kind == "extra_forbidden":
        return f"{path}: is not a proposal member — remove it"
    if kind == "missing":
        return f"{path}: is required"
    if kind in ("literal_error", "enum") and leaf in _MEMBER_CHOICES:
        return f"{path}: must be {_MEMBER_CHOICES[leaf]}"
    if kind == "literal_error" and len(loc) >= 2 and loc[-2] in _MEMBER_CHOICES:
        return f"{path}: must be {_MEMBER_CHOICES[loc[-2]]}"
    if kind == "string_pattern_mismatch" and leaf == "name":
        return (f"{path}: must be a lowercase slug of 3–48 characters "
                "(letters, digits, hyphens; starts with a letter)")
    if kind == "string_pattern_mismatch" and leaf == "model_id":
        return f"{path}: must be one of {', '.join(f'`{m}`' for m in allowed_model_ids())}"
    if kind in ("string_too_long", "too_long") and "max_length" in ctx:
        return f"{path}: too long (at most {ctx['max_length']})"
    if kind in ("string_too_short", "too_short") and "min_length" in ctx:
        return f"{path}: too short (at least {ctx['min_length']})"
    if kind == "value_error":
        # the message of the ValueError the validator raised, without pydantic's prefix
        body = msg.removeprefix("Value error, ").strip()
        if body.startswith(f"{path}:") or body.startswith(f"{path}."):
            return body
        return f"{path}: {body}"
    if msg:
        return f"{path}: {msg}"
    return f"{path}: invalid"  # pragma: no cover — pydantic always sets msg


def _recommendation_error(err: dict[str, Any], *, prefix: str, model_indexes: set[str]
                          ) -> str | None:
    """The ``str | EvaluatorRecommendation`` union reports one error per branch for a
    bad entry; the member reads ONE line (WB-009): the object branch's member error when
    the entry was an object, else the shape rule verbatim. ``None`` = drop this error."""
    loc = [str(part) for part in err.get("loc", ())]
    if len(loc) < 3 or loc[0] != "evaluator_recommendations":
        return ""
    head = f"{prefix}." if prefix else ""
    idx = loc[1]
    if loc[2] == "EvaluatorRecommendation":
        if len(loc) >= 4:
            member = loc[3]
            kind = str(err.get("type", ""))
            if kind == "extra_forbidden":
                return (f"{head}evaluator_recommendations[{idx}].{member}: is not a member of a "
                        "recommendation entry — accepted: text, evaluator_id, blocking, "
                        "threshold, metric")
            return f"{head}evaluator_recommendations[{idx}].{member}: {err.get('msg') or 'invalid'}"
        return f"{head}evaluator_recommendations[{idx}]: {RECOMMENDATION_SHAPE_RULE}"
    if idx in model_indexes:
        return None  # the object branch already says what is wrong with this entry
    return f"{head}evaluator_recommendations[{idx}]: {RECOMMENDATION_SHAPE_RULE}"


def format_validation_errors(
    exc: ValidationError, *, prefix: str = "", reported_recommendations: set[str] | None = None,
) -> list[str]:
    """Every error of ``exc`` as operator-readable sentences (see ``_readable``).
    ``reported_recommendations`` are the ``evaluator_recommendations`` indexes the
    normaliser already reported in one line — pydantic's per-branch errors for them are
    dropped."""
    errs = list(exc.errors())
    reported = reported_recommendations or set()
    model_indexes = {
        str(e["loc"][1]) for e in errs
        if len(e.get("loc", ())) >= 3 and e["loc"][0] == "evaluator_recommendations"
        and e["loc"][2] == "EvaluatorRecommendation"
    }
    lines: list[str] = []
    for err in errs:
        loc = err.get("loc", ())
        if len(loc) >= 2 and loc[0] == "evaluator_recommendations" and str(loc[1]) in reported:
            continue
        special = _recommendation_error(err, prefix=prefix, model_indexes=model_indexes)
        if special is None:
            continue
        lines.append(special or _readable(err, prefix=prefix))
    return _dedupe(lines)


def model_catalog_errors(model_id: Any, resources: dict[str, Any] | None = None,
                         principal: Any = None) -> list[str]:
    """The allowlist check of ``model_id`` against this build's catalog, worded for the
    member and listing the models the member can actually pick here — the catalog
    (``allowed_model_ids()``) minus the rows whose provider has no API key in this
    workspace, in EVERY branch (N-ARCH-obs9: a hint that names an unconfigured id only
    earns the member a second error). A proposal with a model outside the catalog can
    never become an agent (``AgentSpec`` refuses it), so the member learns it here, next
    to every other error, not after fixing the rest (N-ARCH-obs2)."""
    if not isinstance(model_id, str):
        return []
    # `resources` = the workspace's map, carried by the catalog; hub env otherwise.
    # N17-obs203 §6: `principal` = the conversation owner — another member's personal
    # provider's models are neither selectable nor listed
    allowed = [m for m in allowed_model_ids() if model_visible(m, principal)]
    selectable = [m for m in allowed if model_available(m, resources)]
    if model_id not in allowed:
        return [f"model_id: '{model_id}' is not available in this build — choose one of: "
                + ", ".join(f"`{m}`" for m in selectable)]
    if not model_enabled(model_id):
        # D21: an administrator disabled the row (or its provider) in the model registry
        return [f"model_id: '{model_id}' is disabled in the model registry (an administrator "
                "can re-enable it under Settings → Model Providers) — choose one of: "
                + ", ".join(f"`{m}`" for m in selectable)]
    if not model_available(model_id, resources):
        # N-R6-obs1: a catalog model whose provider has no API key in this workspace
        row = model_provider(model_id)
        return [f"model_id: '{model_id}' runs on {row['label']}, which is not configured in "
                f"this workspace (store the API key as SSM SecureString parameter "
                f"{row['ssm_parameter']} and re-run bootstrap) — choose one of: "
                + ", ".join(f"`{m}`" for m in selectable)]
    return []


def _parse(raw: Any, resources: dict[str, Any] | None = None, principal: Any = None
           ) -> tuple[ProposalContent | None, Any, list[str]]:
    """Byte cap → shape → list rules → model catalog, reporting every error found.

    Returns ``(content | None, data, errors)``: ``content`` whenever the SHAPE parsed
    (even with list/seed/model errors, so later stages can still run), ``data`` the
    decoded object (``None`` when unparseable / not an object)."""
    data = raw
    if isinstance(raw, str):
        if len(raw.encode("utf-8")) > PROPOSAL_MAX_BYTES:
            return None, None, [f"proposal exceeds {PROPOSAL_MAX_BYTES} bytes"]
        try:
            data = json.loads(raw)
        except ValueError as exc:
            return None, None, [f"proposal block is not valid JSON: {exc}"]
    if not isinstance(data, dict):
        return None, None, ["proposal must be a JSON object"]
    if serialized_bytes(data) > PROPOSAL_MAX_BYTES:
        return None, data, [f"proposal exceeds {PROPOSAL_MAX_BYTES} bytes"]
    # WB-009: the architect's object-shaped recommendation entries are normalised into
    # the structured shape (with a repair note) before the schema sees them; an entry
    # nothing can be read from stays and is reported in one readable line
    recs, rec_errors, changed = normalise_entries(data.get("evaluator_recommendations"))
    if changed:
        data = {**data, "evaluator_recommendations": recs}
    # N-ARCH-obs14: over-long display text on a seed evaluator entry (a 201-character judge
    # `description`) is trimmed with a repair note on the entry instead of failing the
    # proposal; identifiers, judge instructions and scenario members are never shortened
    from app.assistant.evaluation_plan import trim_seed_free_text

    seed_data, seed_changed = trim_seed_free_text(data.get("evaluation_plan"))
    if seed_changed:
        data = {**data, "evaluation_plan": seed_data}
    try:
        content = ProposalContent.model_validate(data)
    except ValidationError as exc:
        reported = {m.group(1) for m in
                    (re.match(r"evaluator_recommendations\[(\d+)\]", e) for e in rec_errors) if m}
        errors = _dedupe(rec_errors + format_validation_errors(
            exc, reported_recommendations=reported))
        seed = data.get("evaluation_plan")
        if isinstance(seed, dict):
            # the seed's own validator stops at its first rule; list them all here
            from app.assistant.evaluation_plan import seed_errors

            all_seed = seed_errors(seed) + _seed_cross_errors(seed, data)
            errors = [e for e in errors if e not in all_seed] + all_seed
        return None, data, _dedupe(errors + model_catalog_errors(data.get("model_id"),
                                                                 resources, principal))
    errors = _check_lists(content) + model_catalog_errors(content.model_id, resources, principal)
    return content, data, _dedupe(errors)


def parse_content(raw: Any, resources: dict[str, Any] | None = None
                  ) -> tuple[ProposalContent | None, list[str]]:
    """Shape validation only (no catalog). ``raw`` may be a JSON string or a dict.
    The byte cap is checked first, so an oversized object is never validated or
    kept. Every shape, list-rule, seed and model-catalog error is reported together."""
    content, _data, errors = _parse(raw, resources)
    return (content if not errors else None), errors


# ---------------------------------------------------------------------------
# catalog references
# ---------------------------------------------------------------------------


def _catalog_resources(catalog: dict[str, Any]) -> dict[str, Any] | None:
    """The workspace resource map the catalog summary carries (N-R6-obs1: which model
    providers are configured there); ``None`` → the hub's own environment."""
    resources = catalog.get("resources") if isinstance(catalog, dict) else None
    return resources if isinstance(resources, dict) else None


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
            entry.get("gateway_arn") and entry.get("auth_type")
        ):
            errors.append(f"tools: '{key}' has no resolvable gateway ARN / auth type")
    for key, names in (content.tool_functions or {}).items():
        entry = index["tools"].get(key)
        declared = (entry or {}).get("runtime_tools")
        if key not in content.tools:
            errors.append(f"tool_functions: '{key}' is not one of the selected tools")
        elif (entry or {}).get("kind") != "gateway":
            errors.append(f"tool_functions: '{key}' is not a Gateway attachment; only "
                          "Gateway tools can be narrowed to some of their functions")
        elif not isinstance(declared, list):
            errors.append(f"tool_functions: the runtime tool catalog of '{key}' is "
                          "unavailable; refresh the catalog, never guess tool names")
        else:
            unknown = sorted(set(names) - set(declared))
            if unknown:
                errors.append(f"tool_functions.{key}: {unknown} are not runtime callable "
                              f"names of '{key}' (declared: {sorted(declared)})")
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
    if content.memory == "workspace" and not memory_bootstrap.workspace_has_shared_memory(
        resources
    ):
        errors.append("memory: this workspace has no shared memory to bind (neither the "
                      "self-hosted substrate nor an AgentCore Memory is configured)")
    known = {e.get("evaluator_id") for e in catalog.get("evaluators") or []}
    if known and content.evaluation_plan is not None:  # older snapshots carry no list
        for e in content.evaluation_plan.evaluators:
            if e.get("kind") == "existing" and e.get("evaluator_id") not in known:
                errors.append(f"evaluation_plan.evaluators.{e.get('key')}: "
                              f"'{e.get('evaluator_id')}' is not a built-in or third-party "
                              "evaluator available in this workspace")
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
            config = {"record_id": entry["record_id"], "gateway_id": entry["gateway_id"]}
            resources = catalog.get("resources") or {}
            url = entry.get("url")
            if entry["gateway_id"] == resources.get("gateway_id"):
                url = resources.get("gateway_url") or url
            if url:
                config["gateway_url"] = url
            # the exact callables the Harness may use: the reviewed narrowing, else the
            # record's declared tools; unknown → the deployer's ``<name>___*`` target scope
            narrowed = (content.tool_functions or {}).get(key)
            declared = entry.get("runtime_tools")
            if narrowed:
                config["tools"] = list(narrowed)
            elif isinstance(declared, list) and declared:
                config["tools"] = list(declared)
            tools.append(ToolRef(type="gateway", name=entry["name"], config=config))
        else:
            tools.append(ToolRef(type="mcp", name=entry["name"], config={"url": entry["url"]}))
    tools += [ToolRef(type="builtin", name=name) for name in content.builtin_tools]
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
        native_tools=content.native_tools,
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
                # identity of an outbound auth (provider ARN, grant type, scopes) when
                # the catalog carries one — never a credential value; None for the
                # SigV4 gateways the CN build attaches
                "outbound_auth": entry.get("outbound_auth"),
            }
        else:
            remote_mcp[entry["name"]] = {"url": entry["url"], "record_id": entry.get("record_id")}
    skills = {
        key: {
            "record_id": index["skills"][key].get("record_id"),
            "path": index["skills"][key]["path"],
            # the EXACT directory the Harness loads (a legacy `…/SKILL.md` path is
            # normalized to its parent) and the sha256 of every object's real bytes
            "source_prefix": index["skills"][key].get("source_prefix"),
            "content_digest": index["skills"][key].get("content_digest"),
            "object_count": index["skills"][key].get("object_count"),
            "total_bytes": index["skills"][key].get("total_bytes"),
        }
        for key in content.skills
    }
    memory: dict[str, Any] = {
        "mode": content.memory,
        "arn": resources.get("memory_arn") if content.memory == "workspace" else None,
    }
    # N-ARCH-bug1: on the CN substrate the reviewed identity is the store id, not an ARN.
    # Added only for that case so every already-reviewed `disabled` / AgentCore binding
    # keeps its exact shape (approval compares bindings by equality).
    if (
        content.memory == "workspace"
        and memory_bootstrap.shared_memory_kind(resources)
        == memory_bootstrap.MEMORY_KIND_SELFHOSTED
    ):
        memory["memory_id"] = resources.get("memory_id")
    return {
        **spec,
        "resources": {
            "gateways": gateways,
            "remote_mcp": remote_mcp,
            "skills": skills,
            "memory": memory,
            "execution_role_arn": resources.get("execution_role_arn"),
            "tool_access_policy": "selected-v1",
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
                "native_tools", "allowed_tools", "knowledge_bases", "memory",
                "max_iterations", "timeout_seconds", "resources")


def bindings_view(bindings: dict[str, Any] | None) -> dict[str, Any] | None:
    """The reviewer-facing subset of the bound spec (no prompt duplicate)."""
    if not bindings:
        return None
    return {k: bindings.get(k) for k in _VIEW_FIELDS}


def _bindings_errors(content: ProposalContent, catalog: dict[str, Any]) -> list[str]:
    """The last stage, defensive: the proposal → ``AgentSpec`` mapping itself refusing
    (a schema rule no earlier stage mirrors). Worded for the member, never the pydantic
    dump."""
    try:
        resource_bindings(content, catalog)
    except ValidationError as exc:
        return [f"proposal cannot be turned into an agent spec — {e}"
                for e in format_validation_errors(exc)]
    except (ValueError, KeyError) as exc:
        return [f"proposal cannot be turned into an agent spec: {exc}"]
    return []


def validate(
    raw: Any, catalog: dict[str, Any], principal: Any = None
) -> tuple[ProposalContent | None, dict[str, Any], list[str]]:
    """Byte cap → shape → model catalog → references → drafted evaluation plan →
    bindings, in one call — and **every stage that fails is reported**, not only the
    first (N-ARCH-obs2: a foreign model used to hide behind an evaluation-plan error
    until the member had fixed the plan). The list is deduplicated in stage order, so
    ``errors[0]`` is the headline the console shows first.

    Returns ``(content | None, display_dict, errors)``. ``display_dict`` is the
    normalized content when the shape parsed (so an invalid-reference revision can
    still be shown and edited), the raw object when only the shape failed and it is
    small enough to keep, or a marker when the raw object is oversized.
    """
    # ``principal`` (N17-obs203 §6) = the conversation owner / the approver: the model list
    # offered and accepted is theirs (shared providers + their own personal ones); ``None``
    # (the deploy-job re-validation) keeps every row
    content, data, errors = _parse(raw, _catalog_resources(catalog), principal)
    if content is None:
        if isinstance(data, dict) and serialized_bytes(data) <= PROPOSAL_MAX_BYTES:
            return None, data, errors
        return None, {"_rejected": "oversized or unparseable proposal was not stored"}, errors
    display = content_dump(content)
    errors = errors + reference_errors(content, catalog)
    from app.assistant.evaluation_plan import draft_plan_errors

    try:
        errors += draft_plan_errors(display, catalog)
    except (ValueError, KeyError, TypeError) as exc:  # pragma: no cover — defensive
        errors.append(f"evaluation_plan (as drafted for review): cannot be drafted: {exc}")
    if not errors:
        errors = _bindings_errors(content, catalog)
    errors = _dedupe(errors)
    return (content if not errors else None), display, errors
