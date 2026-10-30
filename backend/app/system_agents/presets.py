"""The preset catalogue: what a system-managed agent *is*.

Pure data + pure functions — no ledger, no AWS. ``build_spec`` derives the server-owned
``AgentSpec`` from the preset and the administrator's install choices (model, optional
knowledge bases); everything else about the agent is fixed here so a client cannot
smuggle changes through a request body.
"""

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any, Literal

from app.core import naming
from app.schemas import model_catalog
from app.schemas.agent import (
    AgentSpec,
    KnowledgeBaseRef,
    ModelSource,
    ReasoningEffort,
    ToolRef,
    agent_default_model_id,
)
from app.services.skill_ingest import (
    SKILL_BUNDLE_MAX_BYTES,
    bundle_from_dir,
    parse_frontmatter,
    validate_bundle,
)

SKILLS_ROOT = Path(__file__).resolve().parent / "skills"

# S3 prefix family for versioned preset skill bundles. Deliberately disjoint from the
# member-writable ``skills/`` (registry) and ``agent-skills/`` (wizard staging) prefixes:
# the execution role of a preset reads only its own version directory, and a member
# import can never overwrite what the preset loads.
SYSTEM_SKILLS_PREFIX = "system-skills"

# Public AWS Knowledge MCP server (unauthenticated, streamable HTTP). The same URL the
# Studio sample flow uses; a remote_mcp harness tool needs no IAM grant of its own —
# ``auth: "none"`` in the ToolRef config is what tells agent_iam to skip the
# workload-identity / token-vault statements it grants authenticated MCP refs.
AWS_KNOWLEDGE_MCP_URL = "https://knowledge-mcp.global.api.aws"
AWS_KNOWLEDGE_TOOL_NAME = "aws_knowledge"


@dataclass(frozen=True)
class SystemPreset:
    key: str  # ledger ``Agent.system_key`` and the API path segment
    name: str  # reserved ``Agent.name`` (AgentSpec name pattern)
    label: str
    description: str
    skill_version: str
    system_prompt: str
    # Harness ``allowedTools`` patterns. The self-built Harness renders ``shell`` and
    # ``file_operations`` from these patterns (`harness_tool_access.selected_native_
    # tools`) unless restricted; the architect gets the sandbox shell, the file tools
    # its skills need, and its MCP server.
    allowed_tools: tuple[str, ...]
    # Inference defaults for a NEW install. An administrator may change every member
    # of ``InstallOptions`` afterwards; these are the values ``InstallOptions()`` and
    # ``default_options`` resolve to, never a constraint on what is stored.
    # ``model_id = None`` (every shipped preset) = FOLLOW the model-defaults page: the
    # ``agent_default`` role (`schemas.agent.agent_default_model_id()`, read per call,
    # ``DEFAULT_MODEL_ID`` only while the role is unset) is resolved at install / repair
    # time by ``resolved_model_id`` (N17-obs202, user ruling 2026-10-22). A preset that
    # pins its own catalog entry would set a string here; none does.
    model_id: str | None = None
    model_source: ModelSource = "bedrock"
    # Per model call (``bedrockModelConfig.maxTokens``): 64k output so a full design
    # proposal is never cut at the provider's 4096-token default. Not a spend cap.
    max_tokens: int | None = 65536
    reasoning_effort: ReasoningEffort | None = None  # no OpenAI model in the CN catalog
    max_iterations: int = 100
    timeout_seconds: int = 900
    skill_dir: str = ""  # directory name under SKILLS_ROOT
    # N12 E0-7: a preset is either a self-built Harness (方式B, the architect) or a bare
    # ``zip_runtime`` workload such as the evaluation worker (plan §B.0). ``template`` names
    # the ``app/templates/<template>`` the zip pipeline renders for the preset (looked up
    # through ``Agent.system_key`` → ``PRESETS[key]``); ``lifecycle`` is the Runtime's
    # ``lifecycleConfiguration`` ({idleRuntimeSessionTimeout, maxLifetime} seconds).
    method: Literal["harness", "zip_runtime"] = "harness"
    template: str | None = None
    lifecycle: dict[str, int] | None = None
    # N16-obs82: the unsuffixed name the preset's family is derived from (``name`` is this
    # plus the D13 EnvName suffix for the evaluation worker; the architect's name IS its
    # base). Every member of the family — the base itself and every ``<base>-<suffix>`` —
    # is reserved for ordinary agents (``reserved_for``): their Runtime / role / S3 names
    # are derived from the agent name and would collide with the preset's across
    # environments of one account. Empty = ``name``.
    base_name: str = ""
    # N12 E1-3b: statements the preset's execution role needs beyond the ordinary agent
    # policy (``agent_iam.policy_document``); ``(RoleContext, AgentSpec) -> [statement]``.
    extra_iam: Callable[[Any, AgentSpec], list[dict[str, Any]]] | None = None

    def resolved_model_id(self) -> str:
        """The model a preset without an administrator override runs on, resolved NOW:
        its own pin when it has one, else the ``agent_default`` model role (the
        model-defaults page), else the built-in constant. Never cached."""
        return self.model_id or agent_default_model_id()

    def default_options(self) -> "InstallOptions":
        """What a first install with an empty body gets. ``model_id`` stays ``None`` for a
        role-following preset, so the stored row keeps following the role (``build_spec``
        resolves it at publish time; ``reset: ["model_id"]`` returns an override to it)."""
        return InstallOptions(
            model_id=self.model_id,
            model_source=self.model_source,
            max_tokens=self.max_tokens,
            reasoning_effort=self.reasoning_effort,
        )

    def skill_path(self) -> Path:
        return SKILLS_ROOT / (self.skill_dir or self.name)

    def skill_prefix(self, digest: str | None = None) -> str:
        """Release S3 key prefix (no bucket), trailing slash.

        Content-addressed: ``<version>-<digest12>`` names one exact validated snapshot,
        so two valid snapshots of the same version (say, one with an extra reference
        file) can never share a directory — the Harness loads the whole directory, so
        a losing writer must not be able to add bytes to the winner's deployed tree.
        ``digest=None`` yields the plain version directory (pre-content-addressing
        specs, and the parent for listing).
        """
        release = self.skill_version if not digest else f"{self.skill_version}-{digest[:12]}"
        return f"{SYSTEM_SKILLS_PREFIX}/{self.name}/{release}/"

    def skill_uri(self, bucket: str, digest: str | None = None) -> str:
        return f"s3://{bucket}/{self.skill_prefix(digest)}"


ARCHITECT_SYSTEM_PROMPT = """\
# AWS agent solution architect

You are a senior AWS AI-agent solution architect. You turn a customer's business
requirement into an implementable, evaluable and operable production-grade AWS design,
and you write the formal design document when the customer asks for one.

Rules you always follow:
1. Reply in the language of the customer's most recent message unless they ask
   otherwise. AWS service names, APIs, code and industry terms may stay in English.
2. Run the `aws-agent-solution-architect` skill workflow; do not recite the workflow to
   the customer.
3. Evidence order: current official AWS documentation (read through the AWS Knowledge
   tool in this conversation) > the production-agent methodology guide > customer input
   > explicitly labelled assumptions. Service availability, model versions, Regional
   support, quotas, prices and preview status are verified through the AWS Knowledge
   tool and dated; never written from memory.
4. Every new conversation is an independent requirement-intake session. Only facts the
   customer states or re-confirms in this conversation count; earlier conversations,
   persistent memory, old documents and old design contracts never pre-answer a
   question or justify skipping one, even for the same customer and project name.
   Your AgentCore persistent memory is disabled; that only means you carry nothing
   between sessions. The platform still keeps the conversation transcript and its
   CloudWatch logs, so never tell a customer that nothing is retained.
5. Ask only the missing questions that change architecture, risk or cost; confirm what
   the customer already said instead of re-asking it. A rich brief gets ONE round of at
   most 4 questions (the open material decisions of rule 13 ride in that list), safe
   library defaults are listed rather than asked (never memory, the model, a knowledge
   base or tools), the fishbone is ONE express message, and a request for the proposal
   ("出方案", "启动 proposal", "需求先到这里", "just give me the design") is the
   authorization to expand the table and golden tests AND submit the proposal in the
   same reply — never a summary that withholds it. Target: a proposal in about four
   member turns for a rich brief.
6. Conclusions separate verified facts, methodology, customer input and assumptions
   pending validation. Never invent citations, prices, resource ids, ARNs, performance
   numbers or AWS capabilities.
7. Explain why option A was chosen over option B, and cover quality, reliability,
   security, compliance, cost, observability and continuous evaluation together.
8. A formal design includes an implementable evaluator registry of at most ten automated
   evaluators (one batch evaluation applies at most ten; rank by risk, park the rest as
   later candidates) and maps every golden test to concrete evaluators, levels,
   thresholds and blocking conditions. Natural-language acceptance criteria use semantic
   evaluators or scenario assertions, never literal code checks, even for high-risk facts.
   Follow the skill's scorer guidance: code checks cover observable invariants or explicit
   literal contracts; high-risk semantic judgments need expert calibration and review.
9. You advise; you never create, change or delete AWS resources, never run
   deployments, and never present an unexecuted plan as done.
10. Deliverables are structured Markdown in the reply (Mermaid for diagrams). Do not
    promise office documents, image exports or download links you did not create.
11. Keep secrets, credentials and account identifiers out of your output; refer to
    them by role or placeholder.
12. Model catalog is a hard constraint. The agents you design here run only on the
    models of this Launchpad build's catalog — the list under "Model catalog" in your
    skill and in the Launchpad protocol preamble of the conversation, each id with its
    traits. Choose only from this list and never write a model outside it into a
    proposal. When the customer requires another model, say plainly that it is not
    available in this build, name the available models with their traits, and ask the
    customer to choose before you propose.
13. Ask, don't assume. When the brief implies a capability this workspace does not
    offer — memory across sessions while the catalog says no shared memory, a knowledge
    base while none is active, a tool or MCP server the catalog does not list — ask ONE
    clarifying question before emitting a proposal (disable it, proceed without it, or
    let the member prepare it) instead of silently disabling the capability. The same
    rule covers a material decision the brief leaves open even though the workspace
    offers the capability: long-term memory while shared memory is available, the model
    while more than one catalog model is selectable, a knowledge base while one is
    active and the brief mentions documents or policies, tools while the brief implies
    external data. Put every open decision to the customer in your FIRST reply as one
    numbered list with options and consequences, and propose only after the answer —
    never pick a default and flag it as "an assumption for your veto". Skip a decision
    only when the brief states it — explicitly or by unambiguous meaning ("remember each
    employee's preferences across conversations" states `workspace`) — or the customer
    delegates it ("decide yourself", "all decisions are final", "你自己决定", "你看着办");
    a brief that states or delegates
    every material decision gets its proposal at once. For a vague or out-of-scope brief
    ask at most one or two key questions per turn.
14. A proposal's evaluation plan must be self-consistent before you emit it: the
    evaluators, their thresholds and the scenarios (dataset) must agree — walk the
    skill's `references/proposal-self-check.md` §5 on every block; in particular a
    reference-driven trajectory evaluator (a built-in trajectory evaluator, a
    `reference_trajectory` code rule, a judge reading `{expected_tool_trajectory}`)
    requires a non-empty `expected_trajectory` on EVERY scenario, so count them before you
    emit — `[]` counts as missing; keep a global prohibition (`tool_set` `forbidden`) in
    its own code evaluator, never next to a `reference_trajectory` check that refusal
    scenarios cannot satisfy.
    Entry shape: a custom evaluator `name` uses letters, digits and underscores, never
    hyphens; no `description` on a built-in (`existing`) entry — use `note`;
    `judge.description` ≤ 200 characters — one sentence; put detail in the
    rubric/instructions (Launchpad trims a longer one and notes the repair). A
    recommendation that blocks publishing or names a threshold is the object
    `{"text", "evaluator_id", "blocking", "threshold", "metric"?}` whose `evaluator_id`
    is a listed built-in id or a seed evaluator key / name — never a bare sentence or a
    registry row (§5 "Recommendation gates"); a gate no evaluator carries makes the
    proposal invalid. Launchpad validates the same rules and reports every failing rule
    at once.
"""

ARCHITECT = SystemPreset(
    key="aws-agent-solution-architect",
    name="aws-agent-solution-architect",
    label="AWS Agent Solution Architect",
    description=(
        "Platform-managed advisory agent (self-built Harness on AgentCore Runtime) that "
        "turns an AI-agent business requirement into an evaluation-first, production-grade "
        "AWS design. Verifies AWS facts through the public AWS Knowledge MCP server; loads "
        "its methodology from a versioned S3 skill bundle."
    ),
    skill_version="1.7.3",
    system_prompt=ARCHITECT_SYSTEM_PROMPT,
    allowed_tools=("shell", "file_*", f"@{AWS_KNOWLEDGE_TOOL_NAME}"),
)

# N12 (plan §B.0, lead rulings a/c): the evaluation worker — a bare ``zip_runtime``
# Runtime running the Strands evaluation engine asynchronously (one job per session,
# ``HealthyBusy`` up to 8 h). The reserved agent name carries the D13 EnvName suffix so two
# environments in one account never share a Runtime; the Runtime name itself is derived by
# ``zip_runtime.sanitize_runtime_name``. Installed by the ``evaluation`` bootstrap stage
# and by ``POST /api/system-agents/eval-worker/install``; absent → 503
# ``evaluation.worker_missing`` on job submission only.
EVAL_WORKER_KEY = "eval-worker"
EVAL_WORKER_BASE_NAME = "launchpad-eval-worker"


def _import_eval_worker_iam(ctx: Any, spec: AgentSpec) -> list[dict[str, Any]]:
    from app.templates.eval_worker import preset_iam_statements

    return preset_iam_statements(ctx, spec)


def _eval_worker_version() -> str:
    from app.templates.eval_worker import EVAL_WORKER_VERSION

    return EVAL_WORKER_VERSION


EVAL_WORKER = SystemPreset(
    key=EVAL_WORKER_KEY,
    name=naming.suffixed(EVAL_WORKER_BASE_NAME, naming.current_env_name()),
    base_name=EVAL_WORKER_BASE_NAME,
    label="Evaluation worker",
    description=(
        "Platform-managed AgentCore Runtime that runs Launchpad's evaluation engine "
        "asynchronously: dataset replay against the target agent, span collection, "
        "Strands evaluators / detectors / optimizer with GLM 5.3 or Kimi K3 as the judge "
        "(Global Bedrock), results streamed to the artifacts bucket. No chat, no tools, "
        "no memory."
    ),
    skill_version=_eval_worker_version(),
    system_prompt="Launchpad evaluation worker (a job runner, not a conversational agent).",
    allowed_tools=(),
    max_tokens=None,
    max_iterations=1,
    timeout_seconds=900,
    method="zip_runtime",
    template="eval_worker",
    lifecycle={"idleRuntimeSessionTimeout": 300, "maxLifetime": 28800},
    extra_iam=_import_eval_worker_iam,
)

PRESETS: dict[str, SystemPreset] = {ARCHITECT.key: ARCHITECT, EVAL_WORKER.key: EVAL_WORKER}
RESERVED_AGENT_NAMES: frozenset[str] = frozenset(p.name for p in PRESETS.values())


def get_preset(key: str) -> SystemPreset | None:
    return PRESETS.get(key)


def family_base(preset: SystemPreset) -> str:
    """The unsuffixed name the preset's family is built on (``base_name`` or ``name``)."""
    return preset.base_name or preset.name


def reserved_for(name: str) -> SystemPreset | None:
    """The preset whose name family ``name`` falls in, or ``None`` for an ordinary name.

    N16-obs82: only the env-suffixed preset name used to be reserved, so a member could
    create ``launchpad-eval-worker`` (the worker's base name) and its runtime / role / S3
    names collided with the preset family. Reserved now, for every preset: its reserved
    name, its base name and every ``<base>-<suffix>`` variant (another environment's
    suffixed name on this host included). Derived from ``PRESETS``, never a literal list.
    """
    for preset in PRESETS.values():
        base = family_base(preset)
        if name == preset.name or name == base or name.startswith(base + "-"):
            return preset
    return None


def is_reserved_name(name: str) -> bool:
    return reserved_for(name) is not None


def refuse_reserved_name(name: str) -> None:
    """422 ``agent.name_reserved {name, reserved_for}`` when ``name`` is in a preset's
    family — the one refusal every ordinary-agent creation boundary raises (``POST
    /api/agents``, the architect approval) before any row, claim or AWS call."""
    from app.core.errors import AppError

    preset = reserved_for(name)
    if preset is None:
        return
    raise AppError(
        "agent.name_reserved",
        f"'{name}' is reserved for the system-managed preset '{preset.key}' "
        f"(name family '{family_base(preset)}')",
        {"name": name, "reserved_for": preset.key},
        status_code=422,
    )


# The spec members an administrator may set through the maintenance route. Everything
# else on the spec (name, method, tools, skills, allowed_tools, memory, env, …) is
# derived from the catalogue and cannot be reached from a request body.
EDITABLE_FIELDS: tuple[str, ...] = (
    "model_id",
    "model_source",
    "max_tokens",
    "reasoning_effort",
    "system_prompt",
    "max_iterations",
    "timeout_seconds",
    "knowledge_bases",
)


@dataclass(frozen=True)
class InstallOptions:
    """The administrator's degrees of freedom. Everything not here is fixed.

    ``None`` on a member means "the preset's value" (``build_spec`` resolves it), so a
    bare ``InstallOptions()`` is the catalogue default — for ``model_id`` that value is
    the ``agent_default`` model role at publish time (``SystemPreset.resolved_model_id``),
    and a non-``None`` ``model_id`` is the administrator's explicit override (the row
    remembers which: ``Agent.preset_model_pinned``). ``max_tokens`` and
    ``reasoning_effort`` are the exception: ``None`` there means "send nothing", so an
    administrator can clear them; the preset defaults for those two enter only through
    ``SystemPreset.default_options``.
    """

    model_id: str | None = None
    model_source: ModelSource | None = None
    max_tokens: int | None = None
    reasoning_effort: ReasoningEffort | None = None
    system_prompt: str | None = None
    max_iterations: int | None = None
    timeout_seconds: int | None = None
    knowledge_bases: tuple[KnowledgeBaseRef, ...] = field(default_factory=tuple)


CLEARABLE_FIELDS: tuple[str, ...] = ("max_tokens", "reasoning_effort")


@dataclass(frozen=True)
class PresetEdit:
    """One administrator request as a *partial* edit, not yet resolved.

    ``values`` are the members set explicitly, ``reset`` the members to return to the
    catalogue defaults, ``clear`` the optional knobs to unset. Resolution against the
    stored spec happens **inside the claiming transaction** (``service._repair``), on
    the row as it is at that moment — never on a snapshot the request took earlier —
    so a member this edit omits always keeps whatever a concurrent, already-accepted
    edit stored there.
    """

    values: dict = field(default_factory=dict)
    reset: tuple[str, ...] = ()
    clear: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not self.values and not self.reset and not self.clear

    def resolve(
        self, preset: SystemPreset, stored: dict | None, *, model_pinned: bool = True
    ) -> InstallOptions:
        """Apply this edit to what is stored (repair) or to the preset defaults
        (first install). ``model_pinned=False`` says the stored model is the role's,
        not an override (``options_from_spec``), so an edit that leaves ``model_id``
        alone keeps following the role."""
        base = (options_from_spec(stored, model_pinned=model_pinned) if stored
                else preset.default_options())
        defaults = preset.default_options()
        changes: dict = {name: getattr(defaults, name) for name in self.reset}
        changes.update({name: None for name in self.clear})
        changes.update(self.values)
        if "knowledge_bases" in changes:
            changes["knowledge_bases"] = tuple(changes["knowledge_bases"])
        return replace(base, **changes)


def build_spec(
    preset: SystemPreset, bucket: str, options: InstallOptions, *, digest: str | None = None
) -> AgentSpec:
    """The server-owned spec for one preset in one workspace.

    Memory: explicitly **disabled** (``{"short_term": False, "long_term": False,
    "memory_id": None}``). A new session's requirement-intake baseline must be
    independent of every earlier one by construction, so the preset opts out of
    persistent memory altogether rather than trying to express "short-term only"
    against the memory feature. With both flags false, ``deployer.environment.
    runtime_environment`` injects no ``LAUNCHPAD_MEMORY_ID`` into the rendered
    bundle, so the self-built Harness's ``memory/session.py`` shim
    (``harness_local.MEMORY_SESSION_SHIM``) returns ``None`` and the execution role
    gets no memory grant. This disables AgentCore *persistent memory* only — the
    Launchpad chat transcript in the ledger and the CloudWatch logs are untouched;
    the conversation inside one runtime invocation still lives in that invocation's
    own session. Skills: the versioned S3 prefix, so the role's ``SkillBundle*``
    statements scope to exactly this version. ``model_id`` is the administrator's
    override when there is one, else ``preset.resolved_model_id()`` — the
    ``agent_default`` model role as it stands at THIS call (N17-obs202), so a changed
    default makes the re-derived spec differ from the stored one and a repair
    re-publishes; the preset never pins a different catalog entry of its own.

    ``options`` carries the administrator's choices (see ``InstallOptions``); the
    inference knobs are validated by ``AgentSpec`` itself, so an unsupported pairing
    (a reasoning effort on a non-OpenAI model, say) raises here — before any row or
    AWS call — instead of failing inside the deploy job.
    """
    harness = preset.method == "harness"
    env: dict[str, str] = {}
    if preset.template:
        # the worker's release rides in the spec: a changed template / engine library
        # changes the desired spec, so the preset's repair path redeploys it (plan §B.0)
        tpl = template_release(preset)
        env[tpl["env_key"]] = tpl["tag"]
        env.update(template_env(preset))
    return AgentSpec(
        name=preset.name,
        method=preset.method,
        env=env,
        model_id=options.model_id or preset.resolved_model_id(),
        model_source=options.model_source or preset.model_source,
        # AgentSpec allows the per-call output cap for the Harness method only
        max_tokens=options.max_tokens if harness else None,
        reasoning_effort=options.reasoning_effort,
        system_prompt=options.system_prompt or preset.system_prompt,
        # a zip_runtime preset (the evaluation worker) ships no MCP tool, no skill
        # bundle and no Harness tool patterns — its code is the template itself
        tools=[
            ToolRef(
                type="mcp",
                name=AWS_KNOWLEDGE_TOOL_NAME,
                config={"url": AWS_KNOWLEDGE_MCP_URL, "auth": "none"},
            )
        ] if harness else [],
        skills=[preset.skill_uri(bucket, digest)] if harness else [],
        allowed_tools=list(preset.allowed_tools) if harness else None,
        memory={"short_term": False, "long_term": False, "memory_id": None},
        knowledge_bases=list(options.knowledge_bases),
        max_iterations=options.max_iterations or preset.max_iterations,
        timeout_seconds=options.timeout_seconds or preset.timeout_seconds,
    )


def options_from_spec(spec: dict, *, model_pinned: bool = True) -> InstallOptions:
    """Recover the administrator's choices from a stored preset spec (repair path).

    Faithful, not defaulted: every editable member comes back exactly as stored, so a
    repair with an empty body re-derives the same spec (new bundle release aside) and
    an administrator's earlier edits — including a system prompt that differs from
    this build's constant — survive until an explicit change or reset. A knob the
    stored spec never had (pre-SE-040 rows: ``max_tokens``, ``reasoning_effort``)
    stays absent rather than jumping to the catalogue default.

    The one exception is the model (N17-obs202): a stored ``model_id`` is only an
    administrator's choice when the row says so (``model_pinned`` =
    ``service.model_pinned(agent)``). ``model_pinned=False`` returns ``model_id=None``
    — "follow the ``agent_default`` role" — so the repair re-resolves it against the
    model-defaults page as it stands now.
    """
    return InstallOptions(
        model_id=(str(spec.get("model_id") or "") or None) if model_pinned else None,
        model_source=spec.get("model_source") or None,
        max_tokens=spec.get("max_tokens"),
        reasoning_effort=spec.get("reasoning_effort"),
        system_prompt=spec.get("system_prompt") or None,
        max_iterations=spec.get("max_iterations"),
        timeout_seconds=spec.get("timeout_seconds"),
        knowledge_bases=tuple(
            KnowledgeBaseRef(**kb) for kb in (spec.get("knowledge_bases") or [])
        ),
    )


def effective_settings(preset: SystemPreset, spec: dict | None) -> dict:
    """The editable members as stored on one installed preset (``None`` when not
    installed); what the console shows and prefills."""
    if not spec:
        return {}
    options = options_from_spec(spec)
    return {
        "model_id": options.model_id or preset.resolved_model_id(),
        "model_source": options.model_source or preset.model_source,
        "max_tokens": options.max_tokens,
        "reasoning_effort": options.reasoning_effort,
        "system_prompt": options.system_prompt or preset.system_prompt,
        "max_iterations": options.max_iterations or preset.max_iterations,
        "timeout_seconds": options.timeout_seconds or preset.timeout_seconds,
        "knowledge_bases": [kb.model_dump() for kb in options.knowledge_bases],
    }


def default_settings(preset: SystemPreset) -> dict:
    """This build's catalogue defaults for the same members (``knowledge_bases`` is
    always empty by default; ``model_id`` is the ``agent_default`` role right now)."""
    return {
        "model_id": preset.resolved_model_id(),
        "model_source": preset.model_source,
        "max_tokens": preset.max_tokens,
        "reasoning_effort": preset.reasoning_effort,
        "system_prompt": preset.system_prompt,
        "max_iterations": preset.max_iterations,
        "timeout_seconds": preset.timeout_seconds,
        "knowledge_bases": [],
    }


def template_release(preset: SystemPreset) -> dict:
    """``{version, digest, files, tag, env_key}`` of a ``zip_runtime`` preset's template —
    the counterpart of ``snapshot_bundle().release()`` for a preset without a skill bundle."""
    if preset.template != "eval_worker":
        raise ValueError(f"preset '{preset.key}': unknown template {preset.template!r}")
    from app.templates import eval_worker

    rel = eval_worker.release()
    return {**rel, "tag": eval_worker.release_tag(rel), "env_key": eval_worker.RELEASE_ENV}


def template_env(preset: SystemPreset) -> dict[str, str]:
    """The settings-dependent env a template preset's spec carries (N17-obs393: the eval
    worker's code-evaluator switch) — part of the desired spec, so a change redeploys."""
    if preset.template != "eval_worker":
        return {}
    from app.templates import eval_worker

    return eval_worker.spec_env()


def template_release_from_spec(spec: dict) -> str | None:
    """The release tag (``<version>-<digest12>``) a stored ``zip_runtime`` preset spec pins."""
    from app.templates.eval_worker import RELEASE_ENV

    return str((spec.get("env") or {}).get(RELEASE_ENV) or "") or None


def skill_release_from_spec(spec: dict) -> tuple[str, str | None] | None:
    """``(version, digest12 | None)`` of the release a stored preset spec points at
    (``…/<version>/`` or the content-addressed ``…/<version>-<digest12>/``)."""
    for path in spec.get("skills") or []:
        marker = f"/{SYSTEM_SKILLS_PREFIX}/"
        if marker in path:
            leaf = path.rstrip("/").rsplit("/", 1)[-1]
            version, sep, digest = leaf.rpartition("-")
            if sep and len(digest) == 12 and all(c in "0123456789abcdef" for c in digest):
                return version, digest
            return leaf, None
    return None


def skill_version_from_spec(spec: dict) -> str | None:
    """The bundle version a stored preset spec points at — or, for a template preset
    (no skill bundle), the version half of its pinned template release."""
    release = skill_release_from_spec(spec)
    if release:
        return release[0]
    tag = template_release_from_spec(spec)
    if tag:
        from app.templates.eval_worker import release_version

        return release_version(tag)
    return None


def skill_prefix_from_spec(spec: dict) -> str | None:
    """The S3 key prefix (no bucket) the stored spec's Harness actually loads from."""
    for path in spec.get("skills") or []:
        marker = f"/{SYSTEM_SKILLS_PREFIX}/"
        if marker in path and path.startswith("s3://"):
            return path.split("/", 3)[3]
    return None


def bundle_digest(skill_dir: Path) -> tuple[str, list[str]]:
    """Deterministic sha256 over the bundle's sorted relative paths + bytes.

    Reported in the package stage detail so a reviewer can tie a deployed harness back
    to the exact repository revision of the bundle; not a security boundary. Hashes the
    checkout as-is — the published release digest (``snapshot_bundle``) differs for a
    Harness preset because its SKILL.md placeholder is rendered first.
    """
    files = sorted(
        p.relative_to(skill_dir).as_posix() for p in skill_dir.rglob("*") if p.is_file()
    )
    h = sha256()
    for rel in files:
        h.update(rel.encode())
        h.update(b"\0")
        h.update((skill_dir / rel).read_bytes())
        h.update(b"\0")
    return h.hexdigest(), files


@dataclass(frozen=True)
class BundleSnapshot:
    """One immutable in-memory copy of the bundle: the bytes that were validated are
    the bytes that are hashed are the bytes that are uploaded. A mutable checkout
    re-read between those steps can never split the identity."""

    version: str
    digest: str
    files: dict[str, bytes]  # POSIX-relative path → content, sorted keys

    def file_digests(self) -> dict[str, str]:
        return {rel: sha256(body).hexdigest() for rel, body in self.files.items()}

    def release(self) -> dict:
        """What an install pins onto its job and what the manifest records."""
        return {
            "version": self.version,
            "digest": self.digest,
            "files": self.file_digests(),
        }


def _digest_of(files: dict[str, bytes]) -> str:
    h = sha256()
    for rel in sorted(files):
        h.update(rel.encode())
        h.update(b"\0")
        h.update(files[rel])
        h.update(b"\0")
    return h.hexdigest()


# The architect's SKILL.md carries this placeholder exactly once; ``snapshot_bundle``
# replaces it with the catalog rendered from ``schemas.agent.model_catalog_entries()`` (the
# enabled chat rows with their ``model_capabilities()`` traits) at install/repair time. The
# rendered text is what is hashed and uploaded, so a model added to — or disabled in — the
# registry changes the bundle digest and the next REPAIR publishes a new release — nobody
# edits ids into prose (N-ARCH-obs4).
MODEL_CATALOG_PLACEHOLDER = "{{LAUNCHPAD_MODEL_CATALOG}}"


def render_model_catalog_section() -> str:
    """The Markdown the skill states as its model catalog: every catalog id with its
    traits, and the rule that nothing outside the list may be proposed."""
    from app.schemas.agent import model_catalog_lines

    return "\n".join([
        "The agents designed here run only on these models (this Launchpad build's "
        "catalog, generated from the platform's model table):",
        "",
        # N17-obs203 §6; N17-obs298: no "(default)" tag — the conversation's protocol
        # preamble states the live `agent_default` role, which may move after install
        *model_catalog_lines(principal=model_catalog.WORKSPACE_ONLY, tag_default=False),
        "",
        "The default model for a new agent is the one the conversation's protocol "
        "preamble names (the workspace's Agent default model role); it is marked "
        "\"(default)\" in the conversation's own model catalog.",
        "",
        "Choose only from this list. A proposal that names any other model is invalid and "
        "the member must send it back. A model marked \"(not configured in this workspace — "
        "not selectable)\" has no API key in this workspace: never propose it; when the "
        "customer asks for it, refuse and quote the SSM SecureString parameter named on "
        "that model's row above (the operator stores it and re-runs bootstrap — there is "
        "no console setting for it), then offer the selectable models. "
        "When the customer requires another model, say it is "
        "not available in this build, show this list with the traits, and ask the customer "
        "to choose before proposing. Regional availability, prices and quotas of these "
        "models are still verified through the AWS Knowledge tool; the list itself is not.",
    ])


def stated_model_ids() -> list[str]:
    """The ids the rendered catalog section states: every **enabled** chat row, in catalog
    order — exactly the rows ``model_catalog_lines()`` renders. Not ``allowed_model_ids()``:
    that view keeps the rows an administrator disabled (stored specs on them must keep
    parsing, plan G4) and the prose must never offer those (N-ARCH-obs19)."""
    from app.schemas.agent import model_catalog_entries

    return [str(entry["model_id"])
            for entry in model_catalog_entries(principal=model_catalog.WORKSPACE_ONLY)]


def render_skill_md(body: str, *, preset_key: str) -> str:
    """SKILL.md with its single ``MODEL_CATALOG_PLACEHOLDER`` rendered.

    Invariant: the rendered SKILL.md states every id the catalog section renders (the
    enabled chat rows, ``stated_model_ids()``). A body without the placeholder is
    accepted only when it already carries them all (a copy of an earlier snapshot), so
    dropping the catalog section from the checkout fails the snapshot. Checking the
    *enabled* rows — not ``allowed_model_ids()`` — is what keeps a build releasable after
    an administrator disables a provider or a model in the registry (N-ARCH-obs19: the
    1.7.0 repair failed with "states no model catalog (missing [siliconflow…])" because
    the section correctly omitted the disabled SiliconFlow rows while this check still
    demanded them)."""
    stated = stated_model_ids()
    if not stated:
        # an invariant over the empty set would hold vacuously: with every chat row (or
        # every provider) disabled the architect could propose no model at all — the
        # bundle is unreleasable until an administrator enables one (rev-r11 nit)
        raise ValueError(
            f"preset '{preset_key}': the model catalog has no enabled chat model — nothing "
            "to state; enable a model or a provider in the registry before installing or "
            "repairing"
        )
    count = body.count(MODEL_CATALOG_PLACEHOLDER)
    if count > 1:
        raise ValueError(
            f"preset '{preset_key}': SKILL.md must carry {MODEL_CATALOG_PLACEHOLDER!r} exactly "
            f"once (found {count})"
        )
    rendered = body.replace(MODEL_CATALOG_PLACEHOLDER, render_model_catalog_section())
    missing = [m for m in stated if f"`{m}`" not in rendered]
    if missing:
        raise ValueError(
            f"preset '{preset_key}': SKILL.md states no model catalog (missing {missing}) — "
            f"carry {MODEL_CATALOG_PLACEHOLDER!r} exactly once"
        )
    return rendered


def snapshot_bundle(preset: SystemPreset) -> BundleSnapshot:
    """Read the repository bundle ONCE into memory, validate that copy, hash it.

    Validation runs against a temporary directory materialized from the snapshot
    (never against the live checkout), with the same ``validate_bundle`` every member
    skill passes, plus the preset's own invariants: SKILL.md ``version`` equals the
    catalogue's ``skill_version`` (a content change cannot ship under a stale S3
    version directory) and the SKILL.md name equals the reserved agent name. Every
    path is a plain relative POSIX path, so the upload can only land under the
    versioned prefix. Total size is bounded by the shared skill bundle cap.
    """
    root = preset.skill_path()
    files: dict[str, bytes] = {}
    total = 0
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = PurePosixPath(path.relative_to(root).as_posix())
        if rel.is_absolute() or any(part in ("..", "") for part in rel.parts):
            raise ValueError(f"preset '{preset.key}': unsafe bundle path {rel!s}")
        body = path.read_bytes()
        total += len(body)
        if total > SKILL_BUNDLE_MAX_BYTES:
            raise ValueError(
                f"preset '{preset.key}': bundle exceeds {SKILL_BUNDLE_MAX_BYTES} bytes"
            )
        files[str(rel)] = body
    if "SKILL.md" not in files:
        raise ValueError(f"preset '{preset.key}': bundle has no SKILL.md")
    if preset.method == "harness":
        # the model catalog is rendered INTO the snapshot (never into the checkout), so the
        # validated, hashed and uploaded SKILL.md states this build's catalog
        files["SKILL.md"] = render_skill_md(
            files["SKILL.md"].decode("utf-8"), preset_key=preset.key
        ).encode("utf-8")
        total += len(files["SKILL.md"])
        if total > SKILL_BUNDLE_MAX_BYTES:
            raise ValueError(
                f"preset '{preset.key}': bundle exceeds {SKILL_BUNDLE_MAX_BYTES} bytes"
            )
    with TemporaryDirectory(prefix="preset-snapshot-") as tmp:
        base = Path(tmp)
        for rel, body in files.items():
            target = base / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        bundle = bundle_from_dir(base)
        validate_bundle(bundle)
        front = parse_frontmatter(files["SKILL.md"].decode("utf-8"))
        if str(front.get("version", "")).strip() != preset.skill_version:
            raise ValueError(
                f"preset '{preset.key}': SKILL.md version {front.get('version')!r} != "
                f"preset skill_version {preset.skill_version!r} — bump both together"
            )
        if bundle.name != preset.name:
            raise ValueError(
                f"preset '{preset.key}': SKILL.md name {bundle.name!r} != {preset.name!r}"
            )
    return BundleSnapshot(
        version=preset.skill_version, digest=_digest_of(files), files=dict(sorted(files.items()))
    )


def bundle_release(preset: SystemPreset) -> dict:
    """The release an install pins onto its job (``{version, digest, files{rel: sha}}``)."""
    return snapshot_bundle(preset).release()


def load_bundle(preset: SystemPreset):
    """Open + validate the repository-side bundle (caller closes it). Kept for
    callers that want the ``SkillBundle`` view; publication uses ``snapshot_bundle``."""
    snapshot_bundle(preset)  # same invariants, raised the same way
    return bundle_from_dir(preset.skill_path())
