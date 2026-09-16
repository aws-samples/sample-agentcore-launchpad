"""The reviewed **evaluation-assets plan** of an architect-assistant proposal (SE-047).

A proposal's ``golden_tests`` / ``evaluator_recommendations`` are inert solution
content. This module adds the separate, versioned, typed plan that says what those
recommendations become **if** an administrator later materializes it:

* ``scenarios``   — the Launchpad Dataset items (one per golden test, standard
  predefined shape; every scenario is ONE runtime session — no multi-actor /
  multi-session procedure, no locally computed check);
* ``evaluators``  — every recommendation classified into exactly one kind, all of
  them **AgentCore evaluators**: ``existing`` (a Builtin/ThirdParty/custom evaluator
  id that already exists) or ``judge`` / ``derived`` / ``code`` (AgentCore evaluators
  the operation creates — the code kind is *declarative rules only*, executed by the
  reviewed static Lambda in ``app/assistant/lambda_runtime``). Anything an AgentCore
  evaluator cannot compute (cross-session predicates, human review, metric baselines,
  external controls) is NOT a plan entry: the golden test is blocked with a reason and
  the obligation lives in the proposal's ``manual_tasks``;
* ``recommendations`` — every prose recommendation of the source revision, with the
  plan keys it maps to or an explicit ``unresolved`` / ``declined`` status;
* ``blocked_golden_tests`` — golden tests deliberately not turned into a scenario.

The plan is bound to one proposal revision (``source_revision`` +
``source_content_hash``) and hashed canonically; materialization names the exact
plan revision + hash. Nothing here touches AWS. ``draft_plan`` builds the editable
first draft a member reviews — it never claims a prose recommendation was
implemented: only ids it can identify exactly are mapped, the rest stay
``unresolved``, and the single drafted semantic rubric is labelled as a draft.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.evaluation.agentcore_eval import (
    ALL_BUILTIN_EVALUATORS,
    MAX_BATCH_EVALUATORS,
    TRAJECTORY_EVALUATORS,
)

KNOWN_BUILTINS: dict[str, str] = {**ALL_BUILTIN_EVALUATORS, **TRAJECTORY_EVALUATORS}

PLAN_VERSION = 1
PLAN_MAX_BYTES = 160_000
MAX_CLOUD_EVALUATORS = 10  # judge + derived + code per plan (AWS records the operation creates)
# Every evaluator of a plan — existing AND created — is applied to the dataset run in ONE
# StartBatchEvaluation, which accepts at most MAX_BATCH_EVALUATORS; a plan listing more
# can never run and is refused at proposal time.
MAX_RUN_EVALUATORS = MAX_BATCH_EVALUATORS


def _run_size_error(count: int) -> str:
    return (f"the plan applies {count} evaluators to every dataset run, but one batch "
            f"evaluation accepts at most {MAX_RUN_EVALUATORS} — drop the ones that overlap "
            "(one quality judge, one safety judge, the assertions judge and the exact "
            "invariants usually suffice) or block golden tests the rest would cover")
MAX_SCENARIOS = 40
MAX_CODE_CHECKS = 20
DEFAULT_JUDGE_MODEL = "global.anthropic.claude-sonnet-5"  # the platform's judge default
DEFAULT_LAMBDA_TIMEOUT_S = 60

_KEY_RE = r"^[A-Za-z][A-Za-z0-9_-]{0,31}$"
_EVALUATOR_NAME_RE = r"^[a-zA-Z][a-zA-Z0-9_]{0,47}$"
_EXISTING_ID_RE = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$"
_MODEL_ID_RE = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,120}$"
_SCENARIO_ID_RE = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_KNOWN_ID_RE = re.compile(r"\b((?:Builtin|ThirdParty)\.[A-Za-z0-9_.]+)\b")

# Documented custom-judge placeholders per level (ground-truth-evaluations devguide).
JUDGE_PLACEHOLDERS: dict[str, frozenset[str]] = {
    "SESSION": frozenset({"context", "available_tools", "actual_tool_trajectory",
                          "expected_tool_trajectory", "assertions"}),
    "TRACE": frozenset({"context", "assistant_turn", "expected_response"}),
    "TOOL_CALL": frozenset({"context", "assistant_turn", "expected_response"}),
}
# Placeholders the service can only fill from reference inputs → such a judge must
# not be offered for online (no-ground-truth) scoring.
REFERENCE_PLACEHOLDERS = frozenset({"expected_response", "expected_tool_trajectory",
                                    "assertions"})

Key = Annotated[str, Field(pattern=_KEY_RE)]
Short = Annotated[str, Field(max_length=200)]
Line = Annotated[str, Field(min_length=1, max_length=1000)]
Text = Annotated[str, Field(max_length=2000)]


# ---------------------------------------------------------------------------
# dataset side
# ---------------------------------------------------------------------------


class ScenarioTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: str = Field(min_length=1, max_length=8000)
    expected_response: Text = ""


class Scenario(BaseModel):
    """One predefined dataset item: ONE runtime session whose turns replay in order.
    Multi-actor / multi-session procedures with locally computed checks are not
    representable here — every check the plan produces must be an AgentCore
    evaluator."""

    model_config = ConfigDict(extra="forbid")
    scenario_id: str = Field(pattern=_SCENARIO_ID_RE)
    golden_test_id: str = Field(min_length=1, max_length=64)
    turns: list[ScenarioTurn] = Field(min_length=1, max_length=20)
    expected_trajectory: list[Short] = Field(default_factory=list, max_length=10)
    assertions: list[Line] = Field(default_factory=list, max_length=10)
    note: Text = ""
    # A legacy golden test whose text was NOT supplied as a typed scenario by the
    # proposal (no structured seed) is drafted as a single-turn scenario but marked
    # here; the member must confirm it (``false``) or block the golden test before
    # assets can be created. Nothing infers turns from prose.
    review_required: bool = False


class DatasetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    locale: str = Field(default="en", max_length=8)
    description: str = Field(default="", max_length=1000)


# ---------------------------------------------------------------------------
# declarative code rules (executed only by the static Lambda handler)
# ---------------------------------------------------------------------------

CODE_CHECK_TYPES = (
    "tool_count",            # observed calls of ``tool`` (or all tools) within [min, max]
    "tool_sequence",         # observed tool-name sequence equals / contains ``tools``
    "tool_set",              # every ``allowed``-only / no ``forbidden`` tool observed
    "output_contains",       # final assistant output contains literal ``text``
    "output_not_contains",   # final assistant output does not contain literal ``text``
    "output_exact",          # final assistant output equals literal ``text``
    "reference_trajectory",  # observed tools ⊇ / == reference expectedTrajectory.toolNames
    "reference_response",    # final assistant output contains reference expectedResponse.text
)


class CodeCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Key
    type: Literal[
        "tool_count", "tool_sequence", "tool_set", "output_contains",
        "output_not_contains", "output_exact", "reference_trajectory", "reference_response",
    ]
    tool: Short | None = None
    min: int | None = Field(default=None, ge=0, le=1000)
    max: int | None = Field(default=None, ge=0, le=1000)
    tools: list[Short] = Field(default_factory=list, max_length=20)
    mode: Literal["exact", "subsequence", "superset"] = "superset"
    allowed: list[Short] = Field(default_factory=list, max_length=50)
    forbidden: list[Short] = Field(default_factory=list, max_length=50)
    text: Text = ""
    case_sensitive: bool = False


class CodeRules(BaseModel):
    """Canonical rules data. Fails closed: every check must pass; missing evidence
    is an error, never a pass (see the handler)."""

    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    checks: list[CodeCheck] = Field(min_length=1, max_length=MAX_CODE_CHECKS)


def _check_rules(rules: CodeRules) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for c in rules.checks:
        if c.id in seen:
            errors.append(f"rules: duplicate check id '{c.id}'")
        seen.add(c.id)
        if c.type == "tool_count" and c.min is None and c.max is None:
            errors.append(f"rules.{c.id}: tool_count needs min and/or max")
        if c.type == "tool_count" and c.min is not None and c.max is not None and c.min > c.max:
            errors.append(f"rules.{c.id}: min exceeds max")
        if c.type == "tool_sequence" and not c.tools and c.mode != "exact":
            errors.append(f"rules.{c.id}: tool_sequence needs tools[] unless mode is exact")
        if c.type == "tool_sequence" and c.mode == "superset":
            errors.append(f"rules.{c.id}: tool_sequence mode must be exact or subsequence")
        if c.type == "tool_set" and not (c.allowed or c.forbidden):
            errors.append(f"rules.{c.id}: tool_set needs allowed[] and/or forbidden[]")
        if c.type in ("output_contains", "output_not_contains", "output_exact") and not c.text:
            errors.append(f"rules.{c.id}: {c.type} needs text")
        if c.type == "reference_trajectory" and c.mode == "subsequence":
            errors.append(f"rules.{c.id}: reference_trajectory mode must be superset or exact")
    return errors


def _code_level_errors(entry: CodeEvaluator) -> list[str]:
    """Reference rules must match the level their reference input is scoped to
    (expectedResponse is trace-scoped, expectedTrajectory session-scoped)."""
    errors: list[str] = []
    for c in entry.rules.checks:
        if c.type == "reference_response" and entry.level != "TRACE":
            errors.append(f"rules.{c.id}: reference_response needs level TRACE "
                          "(expectedResponse is trace-scoped)")
        if c.type == "reference_trajectory" and entry.level != "SESSION":
            errors.append(f"rules.{c.id}: reference_trajectory needs level SESSION "
                          "(expectedTrajectory is session-scoped)")
    return errors


# ---------------------------------------------------------------------------
# evaluator entries
# ---------------------------------------------------------------------------


class _Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: Key
    title: str = Field(min_length=1, max_length=200)
    golden_test_ids: list[Annotated[str, Field(max_length=64)]] = Field(
        default_factory=list, max_length=40
    )
    # a blocking entry must pass before the agent is considered acceptable (display
    # semantics for the plan table; the platform never enforces gates here)
    blocking: bool = False
    # minimum acceptable score (0..1) for display next to the evaluator
    threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    note: Text = ""


class ExistingEvaluator(_Entry):
    kind: Literal["existing"]
    evaluator_id: str = Field(pattern=_EXISTING_ID_RE)


class RatingScaleItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: float
    label: str = Field(min_length=1, max_length=64)
    definition: str = Field(min_length=1, max_length=1000)


DEFAULT_RATING_SCALE = [
    RatingScaleItem(value=1.0, label="pass", definition="meets the rubric"),
    RatingScaleItem(value=0.0, label="fail", definition="does not meet the rubric"),
]


class JudgeEvaluator(_Entry):
    kind: Literal["judge"]
    name: str = Field(pattern=_EVALUATOR_NAME_RE)
    instructions: str = Field(min_length=10, max_length=4000)
    rating_scale: list[RatingScaleItem] = Field(default_factory=lambda: list(DEFAULT_RATING_SCALE),
                                                min_length=2, max_length=10)
    model_id: str = Field(default=DEFAULT_JUDGE_MODEL, pattern=_MODEL_ID_RE)
    level: Literal["TRACE", "SESSION", "TOOL_CALL"] = "TRACE"
    description: str = Field(default="", max_length=1000)
    # a member-visible flag: the rubric was drafted by the platform from golden-test
    # text and needs calibration before anyone relies on it
    draft: bool = False


class DerivedEvaluator(_Entry):
    kind: Literal["derived"]
    name: str = Field(pattern=_EVALUATOR_NAME_RE)
    base_evaluator_id: str = Field(pattern=r"^(Builtin|ThirdParty)\.[A-Za-z0-9_.]+$")
    model_id: str = Field(default=DEFAULT_JUDGE_MODEL, pattern=_MODEL_ID_RE)
    description: str = Field(default="", max_length=1000)


class CodeEvaluator(_Entry):
    kind: Literal["code"]
    name: str = Field(pattern=_EVALUATOR_NAME_RE)
    level: Literal["TRACE", "SESSION"] = "TRACE"  # TOOL_CALL targets are not supported
    rules: CodeRules
    lambda_timeout_s: int = Field(default=DEFAULT_LAMBDA_TIMEOUT_S, ge=1, le=300)
    description: str = Field(default="", max_length=1000)


Evaluator = Annotated[
    ExistingEvaluator | JudgeEvaluator | DerivedEvaluator | CodeEvaluator,
    Field(discriminator="kind"),
]
CLOUD_KINDS = ("judge", "derived", "code")


def capability_conflict_errors(
    evaluators: Iterable[Evaluator],
    proposal_content: dict[str, Any],
    *,
    scenarios: Iterable[Scenario] = (),
) -> list[str]:
    """Validate each declared code evaluator against its source capabilities."""
    scenarios = tuple(scenarios)
    return [
        error
        for evaluator in evaluators
        if isinstance(evaluator, CodeEvaluator)
        for error in code_rule_capability_conflict_errors(
            evaluator.rules.checks, proposal_content, evaluator_key=evaluator.key,
            scenarios=scenarios,
        )
    ]


def code_rule_capability_conflict_errors(
    checks: Iterable[CodeCheck],
    proposal_content: dict[str, Any],
    *,
    evaluator_key: str,
    scenarios: Iterable[Scenario] = (),
) -> list[str]:
    """Reject global zero-call rules against mounted or explicitly expected tools.

    KB retrieval and Skill loading mount tools independently of ``tools``. Empty
    golden/scenario trajectories are unspecified, never proof of tool-free execution.
    Every plan evaluator applies globally; golden_test_ids cannot narrow a rule.
    This check is pure and never rewrites historical content or hashes.
    """
    conflicts = [
        f"{field}={proposal_content[field]!r}"
        for field in ("knowledge_bases", "skills", "tools")
        if proposal_content.get(field)
    ]
    # Typed scenarios say which golden tests actually run (blocked tests do not).
    golden_tools = {
        str(g.get("id")): g["expected_tools"]
        for g in proposal_content.get("golden_tests") or []
        if g.get("expected_tools")
    }
    for scenario in scenarios:
        if scenario.expected_trajectory:
            conflicts.append(
                f"scenarios.{scenario.scenario_id}.expected_trajectory="
                f"{scenario.expected_trajectory!r}"
            )
        elif scenario.golden_test_id in golden_tools:
            conflicts.append(
                f"golden_tests.{scenario.golden_test_id}.expected_tools="
                f"{golden_tools[scenario.golden_test_id]!r}"
            )
    if not conflicts:
        return []
    errors: list[str] = []
    for check in checks:
        if check.type == "tool_count" and not check.tool and check.max == 0:
            rule = "unqualified tool_count max=0"
        elif check.type == "tool_sequence" and check.mode == "exact" and not check.tools:
            rule = "exact empty tool_sequence"
        else:
            continue
        errors.append(
            f"evaluators.{evaluator_key}.rules.{check.id}: {rule} forbids all tool calls "
            f"globally and conflicts with {'; '.join(conflicts)}. Read-only does not mean "
            "zero tool calls: retrieval and Skill-loading calls are allowed. Forbid named "
            "business-write tools (tool_count with tool, max=0 or tool_set.forbidden), "
            "or use scenario assertions for refusals and false execution claims."
        )
    return errors


# Kinds an earlier contract accepted as "obligations" (runner-computed cross-session
# predicates, human review, metric baselines, external controls). They are no plan
# entries any more: a golden test that needs one is blocked with a reason and the
# obligation is stated in the proposal's ``manual_tasks``. Kept only to turn legacy
# input into an actionable message instead of a bare "unknown kind".
LEGACY_NON_AUTOMATED_KINDS = ("orchestration", "manual_review", "metric_baseline",
                              "external_control")


def _legacy_errors(raw: dict[str, Any], prefix: str = "") -> list[str]:
    """Actionable messages for the two members the contract no longer has: a
    ``scenarios[].execution`` procedure and a non-AgentCore evaluator kind."""
    errors: list[str] = []
    for sc in raw.get("scenarios") or []:
        if isinstance(sc, dict) and sc.get("execution") is not None:
            sid = sc.get("golden_test_id") or sc.get("scenario_id") or "?"
            errors.append(
                f"{prefix}scenarios.{sid}: 'execution' procedures (multi-actor / multi-session "
                "steps with local checks) are not supported — every scenario is one runtime "
                "session scored by AgentCore evaluators; rewrite it as single-session turns "
                "or block the golden test and describe the test under manual_tasks")
    for e in raw.get("evaluators") or []:
        if isinstance(e, dict) and e.get("kind") in LEGACY_NON_AUTOMATED_KINDS:
            errors.append(
                f"{prefix}evaluators.{e.get('key') or '?'}: kind '{e['kind']}' is not an "
                "AgentCore evaluator — only existing / judge / derived / code entries are "
                "allowed; drop it, block the golden tests it covered (with a reason) and "
                "list the obligation under manual_tasks")
    return errors


class Recommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int = Field(ge=0, le=39)
    text: str = Field(min_length=1, max_length=1000)
    mapped_to: list[Key] = Field(default_factory=list, max_length=10)
    status: Literal["mapped", "unresolved", "declined"] = "unresolved"
    note: Text = ""


class BlockedGoldenTest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    golden_test_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=1000)


class EvaluationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    source_revision: int = Field(ge=1)
    source_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset: DatasetSpec
    scenarios: list[Scenario] = Field(default_factory=list, max_length=MAX_SCENARIOS)
    evaluators: list[Evaluator] = Field(default_factory=list, max_length=60)
    recommendations: list[Recommendation] = Field(default_factory=list, max_length=40)
    blocked_golden_tests: list[BlockedGoldenTest] = Field(default_factory=list, max_length=40)
    # additive Invoke/GetFunction policy on the workspace execution role for the owned
    # Lambda version(s) — only meaningful with code evaluators
    grant_workspace_execution_role: bool = True
    summary: str = Field(default="", max_length=4000)


# ---------------------------------------------------------------------------
# hashing / bytes
# ---------------------------------------------------------------------------


def canonical_hash(content: dict[str, Any]) -> str:
    payload = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def serialized_bytes(raw: Any) -> int:
    try:
        return len(json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        return PLAN_MAX_BYTES + 1


# ---------------------------------------------------------------------------
# validation against the source proposal
# ---------------------------------------------------------------------------


def golden_test_snapshot(gt: dict[str, Any] | None) -> dict[str, Any] | None:
    """The reviewed golden-test facts kept with a Dataset item (never the transcript)."""
    if not isinstance(gt, dict):
        return None
    return {k: gt.get(k) for k in ("id", "input", "expected_response", "expected_tools",
                                   "forbidden_behavior", "pass_criteria", "evaluator", "source")
            if gt.get(k) not in (None, "", [])}


def dataset_item(
    scenario: Scenario, plan_provenance: dict[str, Any],
    golden_test: dict[str, Any] | None = None, applies: list[str] | None = None,
) -> dict[str, Any]:
    """The standard predefined item for one scenario (what the Dataset stores).
    ``applies`` lists the plan keys of the evaluators mapped to this golden test."""
    item: dict[str, Any] = {
        "scenario_id": scenario.scenario_id,
        "turns": [
            {"input": t.input, **({"expected_response": t.expected_response}
                                   if t.expected_response else {})}
            for t in scenario.turns
        ],
    }
    if scenario.expected_trajectory:
        item["expected_trajectory"] = list(scenario.expected_trajectory)
    if scenario.assertions:
        item["assertions"] = list(scenario.assertions)
    assets: dict[str, Any] = {**plan_provenance, "golden_test_id": scenario.golden_test_id}
    snapshot = golden_test_snapshot(golden_test)
    if snapshot:
        assets["golden_test"] = snapshot
    if applies is not None:
        assets["applies"] = list(applies)
    if scenario.note:
        assets["note"] = scenario.note
    item["metadata"] = {"launchpad_assets": assets}
    return item


def _judge_errors(entry: JudgeEvaluator) -> list[str]:
    names = set(_PLACEHOLDER_RE.findall(entry.instructions))
    if not names:
        return [f"evaluators.{entry.key}: instructions need at least one {{placeholder}}"]
    unknown = sorted(names - JUDGE_PLACEHOLDERS[entry.level])
    if unknown:
        return [f"evaluators.{entry.key}: placeholders {unknown} are not available at level "
                f"{entry.level} (allowed: {sorted(JUDGE_PLACEHOLDERS[entry.level])})"]
    return []


def references_needed(entry: Any) -> set[str]:
    """Ground-truth fields an evaluator reads: ``expected_response`` / ``assertions`` /
    ``expected_trajectory`` (judge placeholders or code reference rules)."""
    if isinstance(entry, JudgeEvaluator):
        found = set(_PLACEHOLDER_RE.findall(entry.instructions)) & REFERENCE_PLACEHOLDERS
        return {"expected_trajectory" if p == "expected_tool_trajectory" else p for p in found}
    if isinstance(entry, CodeEvaluator):
        out: set[str] = set()
        for c in entry.rules.checks:
            if c.type == "reference_trajectory":
                out.add("expected_trajectory")
            if c.type == "reference_response":
                out.add("expected_response")
        return out
    return set()


def _scenario_references(s: Scenario) -> set[str]:
    out: set[str] = set()
    if any(t.expected_response for t in s.turns):
        out.add("expected_response")
    if s.assertions:
        out.add("assertions")
    if s.expected_trajectory:
        out.add("expected_trajectory")
    return out


def _routing_errors(plan: EvaluationPlan) -> list[str]:
    """Only GLOBAL evaluator selection is real at execution time.

    The shared dataset runner applies one evaluator list to every session, and the
    reference envelope does not select evaluators. So an evaluator mapped to a proper
    subset of golden tests is refused before creation (actionable: map it to all
    golden tests or block the tests it cannot cover). A global reference-driven
    evaluator is valid only when EVERY scenario — and, at TRACE, every turn — carries
    the reference it reads."""
    errors: list[str] = []
    all_gts = {s.golden_test_id for s in plan.scenarios}
    for e in plan.evaluators:
        needs = references_needed(e)
        if e.kind == "existing" and e.evaluator_id in TRAJECTORY_EVALUATORS:
            needs = {"expected_trajectory"}
        mapped = set(e.golden_test_ids)
        if mapped and mapped != all_gts:
            errors.append(f"evaluators.{e.key}: targets only {sorted(mapped)} but a dataset run "
                          "applies every evaluator to every session — the platform cannot "
                          "route an AWS evaluator per golden test; map it to all golden tests "
                          "(golden_test_ids: []) or block the golden tests it cannot cover")
        if not needs:
            continue
        level = getattr(e, "level", "SESSION")
        for s in plan.scenarios:
            have = _scenario_references(s)
            missing = sorted(needs - have)
            if missing:
                errors.append(f"evaluators.{e.key}: scenario '{s.golden_test_id}' has no "
                              f"{'/'.join(missing)} — a reference-driven evaluator is applied "
                              "to every session, so every scenario must carry it")
            if level == "TRACE" and "expected_response" in needs and not all(
                    t.expected_response for t in s.turns):
                errors.append(f"evaluators.{e.key}: scenario '{s.golden_test_id}' has a turn "
                              "without expected_response — a TRACE reference evaluator scores "
                              "every turn")
    return errors


def reference_dependent(entry: Any) -> bool:
    """Whether an evaluator needs ground truth (so it must not score live traffic)."""
    if isinstance(entry, JudgeEvaluator):
        return bool(set(_PLACEHOLDER_RE.findall(entry.instructions)) & REFERENCE_PLACEHOLDERS)
    if isinstance(entry, CodeEvaluator):
        return any(c.type.startswith("reference_") for c in entry.rules.checks)
    return False


def validate_plan(
    raw: Any, proposal_content: dict[str, Any], *, revision: int, content_hash: str
) -> tuple[EvaluationPlan | None, list[str]]:
    """Byte cap → shape → cross-field rules → binding to the source revision.

    Every golden test of the proposal is either a scenario or explicitly blocked;
    every prose recommendation appears exactly once; mapped keys exist; cloud
    evaluator names and keys are unique; code rules are coherent; ≤ 10 cloud
    evaluators; every assembled dataset item passes the Dataset ingress gate.
    """
    if serialized_bytes(raw) > PLAN_MAX_BYTES:
        return None, [f"plan exceeds {PLAN_MAX_BYTES} bytes"]
    if not isinstance(raw, dict):
        return None, ["plan must be a JSON object"]
    legacy = _legacy_errors(raw)
    if legacy:
        return None, legacy
    try:
        plan = EvaluationPlan.model_validate(raw)
    except ValidationError as exc:
        return None, [
            f"{'.'.join(str(p) for p in err['loc']) or 'plan'}: {err['msg']}"
            for err in exc.errors()
        ][:40]
    errors: list[str] = []
    if plan.source_revision != revision or plan.source_content_hash != content_hash:
        errors.append("plan is bound to another proposal revision/hash than the one selected")
    gts = {str(g.get("id")) for g in proposal_content.get("golden_tests") or [] if g.get("id")}
    covered = {s.golden_test_id for s in plan.scenarios} | {
        b.golden_test_id for b in plan.blocked_golden_tests
    }
    for missing in sorted(gts - covered):
        errors.append(f"golden test '{missing}' is neither a scenario nor explicitly blocked")
    for extra in sorted(covered - gts):
        errors.append(f"golden test '{extra}' does not exist on revision {revision}")
    sids = [s.scenario_id for s in plan.scenarios]
    if len(set(sids)) != len(sids):
        errors.append("scenario ids must be unique")
    keys = [e.key for e in plan.evaluators]
    if len(set(keys)) != len(keys):
        errors.append("evaluator keys must be unique")
    names = [str(getattr(e, "name", "")) for e in plan.evaluators if e.kind in CLOUD_KINDS]
    if len(set(names)) != len(names):
        errors.append("cloud evaluator names must be unique within the plan")
    if len(names) > MAX_CLOUD_EVALUATORS:
        errors.append(f"the plan creates {len(names)} AWS evaluators (max {MAX_CLOUD_EVALUATORS})"
                      " — split it or mark some entries manual/existing")
    if len(plan.evaluators) > MAX_RUN_EVALUATORS:
        errors.append(_run_size_error(len(plan.evaluators)))
    for e in plan.evaluators:
        for gt in e.golden_test_ids:
            if gt not in gts:
                errors.append(f"evaluators.{e.key}: golden test '{gt}' does not exist")
        if isinstance(e, JudgeEvaluator):
            errors += _judge_errors(e)
        if isinstance(e, CodeEvaluator):
            errors += [f"evaluators.{e.key}: {m}" for m in _check_rules(e.rules)]
            errors += [f"evaluators.{e.key}: {m}" for m in _code_level_errors(e)]
        if isinstance(e, ExistingEvaluator) and e.evaluator_id.startswith("Builtin.") \
                and e.evaluator_id not in KNOWN_BUILTINS:
            errors.append(f"evaluators.{e.key}: unknown builtin evaluator '{e.evaluator_id}'")
    prose = [str(x) for x in proposal_content.get("evaluator_recommendations") or []]
    indexes = sorted(r.index for r in plan.recommendations)
    if indexes != list(range(len(prose))):
        errors.append("recommendations must list every evaluator_recommendations entry of the "
                      f"revision exactly once (expected indexes 0..{len(prose) - 1})")
    for r in plan.recommendations:
        if 0 <= r.index < len(prose) and r.text != prose[r.index]:
            errors.append(f"recommendations[{r.index}]: text differs from the revision")
        for k in r.mapped_to:
            if k not in keys:
                errors.append(f"recommendations[{r.index}]: mapped key '{k}' is not an evaluator")
        if r.status == "mapped" and not r.mapped_to:
            errors.append(f"recommendations[{r.index}]: mapped without any evaluator key")
        if r.status != "mapped" and r.mapped_to:
            errors.append(f"recommendations[{r.index}]: carries keys but is not 'mapped'")
    pending = [s.golden_test_id for s in plan.scenarios if s.review_required]
    if pending:
        errors.append("scenarios need review before assets can be created: "
                      + ", ".join(pending) + " (confirm each as typed turns — "
                      "review_required: false — or block the golden test)")
    errors += _routing_errors(plan)
    errors += capability_conflict_errors(
        plan.evaluators, proposal_content, scenarios=plan.scenarios
    )
    return (plan if not errors else None), errors


# ---------------------------------------------------------------------------
# the first draft a member reviews
# ---------------------------------------------------------------------------


def _slug(value: str, limit: int = 40) -> str:
    out = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")
    if not out or not out[0].isalpha():
        out = "gt_" + out
    return out[:limit]


def _scenario_id(gt_id: str) -> str:
    sid = re.sub(r"[^A-Za-z0-9_.-]+", "-", gt_id).strip("-.")
    return (sid or "gt")[:64]


def _unique_key(base: str, used: set[str]) -> str:
    key = base
    n = 2
    while key in used:
        key = f"{base[:28]}_{n}"
        n += 1
    used.add(key)
    return key


def _seed_scenarios(seed: dict[str, Any], gts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Structured scenarios a proposal seed supplies, keyed by golden test id (shape
    already validated by the proposal contract)."""
    out: dict[str, dict[str, Any]] = {}
    ids = {str(g.get("id")) for g in gts}
    for sc in seed.get("scenarios") or []:
        if isinstance(sc, dict) and str(sc.get("golden_test_id")) in ids:
            out[str(sc["golden_test_id"])] = {**sc, "review_required": False}
    return out


def _seed_blocked(seed: dict[str, Any], gts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Golden tests a proposal seed deliberately blocks (with the reason) — the way a
    test that no AgentCore evaluator can compute stays visible without becoming a
    scenario. Shape already validated by the proposal contract."""
    ids = {str(g.get("id")) for g in gts}
    out: list[dict[str, Any]] = []
    for b in seed.get("blocked_golden_tests") or []:
        if isinstance(b, dict) and str(b.get("golden_test_id")) in ids and b.get("reason"):
            out.append({"golden_test_id": str(b["golden_test_id"]),
                        "reason": str(b["reason"])[:1000]})
    return out


def draft_plan(
    content: dict[str, Any], *, revision: int, content_hash: str, agent_name: str
) -> dict[str, Any]:
    """The editable first draft.

    * Scenarios: a proposal seed may carry typed scenarios (turns / references) — those
      are used as-is — and may block golden tests it cannot express as one session
      scored by AgentCore evaluators (the reason is kept). Every other golden test
      becomes a single-turn scenario marked ``review_required``; the member confirms or
      blocks it before anything is created.
    * Evaluators: seeded entries first (keys reserved). Prose recommendations are
      mapped only to ids identified exactly (``Builtin.*`` / ``ThirdParty.*``) or to
      the seed's explicit ``recommendation_keys``; a seed mapping never removes an
      entry another recommendation references, and keys are collision-safe.
    * One clearly labelled SESSION-level draft judge scores each scenario against ITS
      OWN ``assertions`` (pass criteria / forbidden behaviour) through the
      ``{assertions}`` reference — no global rubric mixing golden tests.
    """
    gts = [g for g in content.get("golden_tests") or [] if isinstance(g, dict) and g.get("id")]
    seed = content.get("evaluation_plan") if isinstance(content.get("evaluation_plan"), dict) \
        else {}
    seeded_scenarios = _seed_scenarios(seed, gts)
    blocked = _seed_blocked(seed, gts)
    blocked_ids = {b["golden_test_id"] for b in blocked}
    scenarios: list[dict[str, Any]] = []
    for g in gts:
        gid = str(g["id"])
        if gid in blocked_ids:
            continue
        if gid in seeded_scenarios:
            scenarios.append(seeded_scenarios[gid])
            continue
        assertions = []
        if g.get("pass_criteria"):
            assertions.append(str(g["pass_criteria"])[:1000])
        if g.get("forbidden_behavior"):
            assertions.append(("Must not: " + str(g["forbidden_behavior"]))[:1000])
        scenarios.append({
            "scenario_id": _scenario_id(gid),
            "golden_test_id": gid,
            "turns": [{"input": str(g.get("input") or "")[:8000],
                       "expected_response": str(g.get("expected_response") or "")[:2000]}],
            "expected_trajectory": [str(t)[:200] for t in (g.get("expected_tools") or [])][:10],
            "assertions": assertions,
            "note": "legacy golden test drafted as ONE user turn — confirm, rewrite the turns "
                    "or block it",
            "review_required": True,
        })
    evaluators: list[dict[str, Any]] = []
    used_keys: set[str] = set()
    seeded_keys: set[str] = set()
    for e in seed.get("evaluators") or []:
        if isinstance(e, dict) and isinstance(e.get("key"), str) and e["key"] not in used_keys:
            evaluators.append(dict(e))
            used_keys.add(e["key"])
            seeded_keys.add(e["key"])
    existing_keys: dict[str, str] = {
        e["evaluator_id"]: e["key"] for e in evaluators
        if e.get("kind") == "existing" and isinstance(e.get("evaluator_id"), str)
    }
    recommendations: list[dict[str, Any]] = []
    rec_keys = (seed.get("recommendation_keys") or {}) if seed else {}
    for i, text in enumerate(content.get("evaluator_recommendations") or []):
        text = str(text)
        mapped: list[str] = []
        for evaluator_id in dict.fromkeys(m for m in _KNOWN_ID_RE.findall(text)
                                          if m in KNOWN_BUILTINS
                                          or m.startswith("ThirdParty.")):
            key = existing_keys.get(evaluator_id)
            if key is None:
                key = _unique_key(_slug(evaluator_id.replace(".", "_"), 28), used_keys)
                existing_keys[evaluator_id] = key
                evaluators.append({
                    "kind": "existing", "key": key, "title": evaluator_id,
                    "evaluator_id": evaluator_id, "golden_test_ids": [],
                    "blocking": False, "threshold": None,
                    "note": f"named in recommendation #{i + 1}",
                })
            mapped.append(key)
        seeded = [str(k) for k in (rec_keys.get(str(i)) or []) if str(k) in seeded_keys]
        mapped = list(dict.fromkeys(mapped + seeded))
        recommendations.append({
            "index": i, "text": text, "mapped_to": mapped,
            "status": "mapped" if mapped else "unresolved",
            "note": "" if mapped else "no exact evaluator identified — classify or decline",
        })
    all_assertions = bool(scenarios) and all(sc.get("assertions") for sc in scenarios)
    if all_assertions and "draft_rubric" not in used_keys:
        used_keys.add("draft_rubric")
        evaluators.append({
            "kind": "judge", "key": "draft_rubric",
            "title": "Draft per-scenario judge (scores each scenario's own assertions)",
            "name": _slug(f"{agent_name}_gt_assertions", 48),
            "instructions": DRAFT_ASSERTION_RUBRIC,
            "rating_scale": [r.model_dump() for r in DEFAULT_RATING_SCALE],
            "model_id": DEFAULT_JUDGE_MODEL, "level": "SESSION",
            "description": f"Drafted for proposal revision {revision}: judges a session "
                           "against the assertions of its own scenario (reference input)",
            "golden_test_ids": [],
            "blocking": False, "threshold": None, "draft": True,
            "note": "platform draft — reference-driven ({assertions}); calibrate wording, "
                    "model and scale with domain experts before relying on it",
        })
    all_trajectory = bool(scenarios) and all(sc.get("expected_trajectory") for sc in scenarios)
    if all_trajectory and "expected_tools" not in used_keys:
        used_keys.add("expected_tools")
        evaluators.append({
            "kind": "code", "key": "expected_tools",
            "title": "Expected tools observed (deterministic, per session)",
            "name": _slug(f"{agent_name}_expected_tools", 48),
            "level": "SESSION",
            "rules": {"version": 1, "checks": [
                {"id": "trajectory", "type": "reference_trajectory", "mode": "superset"},
            ]},
            "lambda_timeout_s": DEFAULT_LAMBDA_TIMEOUT_S,
            "description": "Observed tool calls must include every tool of the scenario's "
                           "expected_trajectory (reference input); missing evidence is an error",
            "golden_test_ids": [],
            "blocking": False, "threshold": None,
            "note": "deterministic rule — not a semantic or safety judgement",
        })
    return {
        "version": PLAN_VERSION,
        "source_revision": revision,
        "source_content_hash": content_hash,
        "dataset": {
            "name": _slug(f"{agent_name}-golden-r{revision}".replace("-", "_"), 64)
            .replace("_", "-"),
            "locale": "en",
            "description": f"Golden tests of assistant proposal revision {revision} "
                           f"({agent_name}); created from the reviewed evaluation plan",
        },
        "scenarios": scenarios,
        "evaluators": evaluators,
        "recommendations": recommendations,
        "blocked_golden_tests": blocked,
        "grant_workspace_execution_role": True,
        "summary": "Draft prepared by the platform. Legacy golden tests are single-turn "
                   "scenarios that REQUIRE REVIEW (confirm or block each). Every evaluator is "
                   "an AgentCore evaluator (existing, LLM judge, derived or declarative code "
                   "rules); a test no AgentCore evaluator can compute is blocked with its "
                   "reason and stays a manual task. Unresolved recommendations are NOT "
                   "implemented; classify each as an existing/new AgentCore evaluator or "
                   "decline it.",
    }


DRAFT_ASSERTION_RUBRIC = (
    "DRAFT rubric — calibrate with domain experts before relying on it.\n"
    "You are given one agent session and the assertions that must hold for THIS scenario.\n"
    "Session:\n{context}\n\nAssertions for this scenario:\n{assertions}\n\n"
    "Judge only against these assertions. Return pass only if every assertion holds; an "
    "assertion starting with 'Must not:' fails when the behaviour occurs anywhere in the "
    "session. If the session evidence is incomplete, fail."
)


def seed_errors(
    seed: dict[str, Any], *, proposal_content: dict[str, Any] | None = None
) -> list[str]:
    """Shape + cross-field validation of a proposal's optional ``evaluation_plan`` seed
    (evaluators / scenarios / recommendation_keys / blocked_golden_tests). Pure; never
    raises on malformed input — every problem is a message, so the proposal becomes an
    *invalid* revision rather than a server error."""
    if not isinstance(seed, dict):
        return ["evaluation_plan must be an object"]
    unknown = sorted(set(seed) - {"evaluators", "scenarios", "recommendation_keys",
                                  "blocked_golden_tests"})
    if unknown:
        return [f"evaluation_plan: unknown members {unknown}"]
    evaluators = seed.get("evaluators", [])
    scenarios = seed.get("scenarios", [])
    rec_keys = seed.get("recommendation_keys", {})
    blocked = seed.get("blocked_golden_tests", [])
    if not isinstance(evaluators, list) or not isinstance(scenarios, list) \
            or not isinstance(rec_keys, dict) or not isinstance(blocked, list):
        return ["evaluation_plan: evaluators/scenarios/blocked_golden_tests must be lists and "
                "recommendation_keys an object"]
    if len(evaluators) > 20 or len(scenarios) > MAX_SCENARIOS or len(blocked) > MAX_SCENARIOS:
        return ["evaluation_plan: too many evaluators, scenarios or blocked golden tests"]
    legacy = _legacy_errors({"evaluators": evaluators, "scenarios": scenarios},
                            prefix="evaluation_plan.")
    if legacy:
        return legacy
    probe = {"version": 1, "source_revision": 1, "source_content_hash": "0" * 64,
             "dataset": {"name": "seed"}, "evaluators": evaluators, "scenarios": scenarios,
             "blocked_golden_tests": blocked}
    try:
        plan = EvaluationPlan.model_validate(probe)
    except ValidationError as exc:
        return [f"evaluation_plan.{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
                for err in exc.errors()][:20]
    errors: list[str] = []
    blocked_ids = [b.golden_test_id for b in plan.blocked_golden_tests]
    if len(set(blocked_ids)) != len(blocked_ids):
        errors.append("evaluation_plan: a golden test is blocked twice")
    both = sorted(set(blocked_ids) & {sc.golden_test_id for sc in plan.scenarios})
    if both:
        errors.append(f"evaluation_plan: golden tests {both} are both a scenario and blocked")
    keys = [e.key for e in plan.evaluators]
    if len(set(keys)) != len(keys):
        errors.append("evaluation_plan: evaluator keys must be unique")
    if len(plan.evaluators) > MAX_RUN_EVALUATORS:
        errors.append("evaluation_plan: " + _run_size_error(len(plan.evaluators)))
    names = [str(getattr(e, "name", "")) for e in plan.evaluators if e.kind in CLOUD_KINDS]
    if len(set(names)) != len(names):
        errors.append("evaluation_plan: cloud evaluator names must be unique")
    gts = [sc.golden_test_id for sc in plan.scenarios]
    if len(set(gts)) != len(gts):
        errors.append("evaluation_plan: one scenario per golden test")
    for e in plan.evaluators:
        if isinstance(e, JudgeEvaluator):
            errors += [f"evaluation_plan.{m}" for m in _judge_errors(e)]
        if isinstance(e, CodeEvaluator):
            errors += [f"evaluation_plan.evaluators.{e.key}: {m}"
                       for m in _check_rules(e.rules) + _code_level_errors(e)]
    # the execution-time routing rules apply to the seed too: a proposal whose
    # evaluators target a subset of the golden tests used to pass here and fail only
    # when an administrator tried to create the assets, after the Agent was deployed
    errors += [f"evaluation_plan.{m}" for m in _routing_errors(plan)]
    if proposal_content is not None:
        errors += [
            f"evaluation_plan.{m}"
            for m in capability_conflict_errors(
                plan.evaluators, proposal_content, scenarios=plan.scenarios
            )
        ]
    for idx, mapped in rec_keys.items():
        if not (isinstance(idx, str) and idx.isdigit() and int(idx) < 40):
            errors.append(f"evaluation_plan.recommendation_keys: '{idx}' is not a "
                          "recommendation index")
            continue
        if not isinstance(mapped, list) or not all(isinstance(k, str) for k in mapped):
            errors.append(f"evaluation_plan.recommendation_keys[{idx}]: must list keys")
            continue
        for k in mapped:
            if k not in keys:
                errors.append(f"evaluation_plan.recommendation_keys[{idx}]: unknown key '{k}'")
    return errors


def plan_summary(plan: dict[str, Any]) -> dict[str, Any]:
    """Counts for the review table / disclosure (works on any validated plan dict)."""
    evaluators = plan.get("evaluators") or []
    by_kind: dict[str, int] = {}
    for e in evaluators:
        by_kind[str(e.get("kind"))] = by_kind.get(str(e.get("kind")), 0) + 1
    code = [e for e in evaluators if e.get("kind") == "code"]
    return {
        "scenarios": len(plan.get("scenarios") or []),
        "blocked_golden_tests": len(plan.get("blocked_golden_tests") or []),
        "evaluators_by_kind": by_kind,
        "cloud_evaluators": sum(by_kind.get(k, 0) for k in CLOUD_KINDS),
        "lambda_functions": 1 if code else 0,
        "iam_roles": 1 if code else 0,
        "role_grants": 1 if code and plan.get("grant_workspace_execution_role", True) else 0,
        "unresolved_recommendations": sum(
            1 for r in plan.get("recommendations") or [] if r.get("status") == "unresolved"
        ),
    }
