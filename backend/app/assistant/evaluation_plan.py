"""The reviewed **evaluation-assets plan** of an architect-assistant proposal (SE-047).

A proposal's ``golden_tests`` / ``evaluator_recommendations`` are inert solution
content. This module adds the separate, versioned, typed plan that says what those
recommendations become **if** an administrator later materializes it:

* ``scenarios``   — the Launchpad Dataset items (one per golden test, standard
  predefined shape, optionally carrying the SE-046 ``launchpad_execution`` procedure);
* ``evaluators``  — every recommendation classified into exactly one kind:
  ``existing`` (a Builtin/ThirdParty/custom evaluator id that already exists),
  ``judge`` / ``derived`` / ``code`` (AgentCore evaluators the operation creates —
  the code kind is *declarative rules only*, executed by the reviewed static Lambda
  in ``app/assistant/lambda_runtime``), and the four non-automatable kinds
  ``orchestration`` (cross-session predicates the local runner computes),
  ``manual_review`` (human / expert judgement), ``metric_baseline`` and
  ``external_control`` — which are shown with their obligation and are NEVER turned
  into evaluator records;
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
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.evaluation import execution
from app.evaluation.agentcore_eval import ALL_BUILTIN_EVALUATORS

PLAN_VERSION = 1
PLAN_MAX_BYTES = 160_000
MAX_CLOUD_EVALUATORS = 10  # judge + derived + code per plan (AWS records the operation creates)
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
    """One predefined dataset item. ``execution`` is the SE-046 procedure object
    (validated by ``execution.parse_plan`` on the assembled item)."""

    model_config = ConfigDict(extra="forbid")
    scenario_id: str = Field(pattern=_SCENARIO_ID_RE)
    golden_test_id: str = Field(min_length=1, max_length=64)
    turns: list[ScenarioTurn] = Field(min_length=1, max_length=20)
    expected_trajectory: list[Short] = Field(default_factory=list, max_length=10)
    assertions: list[Line] = Field(default_factory=list, max_length=10)
    execution: dict[str, Any] | None = None
    note: Text = ""


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
        if c.type == "tool_sequence" and not c.tools:
            errors.append(f"rules.{c.id}: tool_sequence needs tools[]")
        if c.type == "tool_sequence" and c.mode == "superset":
            errors.append(f"rules.{c.id}: tool_sequence mode must be exact or subsequence")
        if c.type == "tool_set" and not (c.allowed or c.forbidden):
            errors.append(f"rules.{c.id}: tool_set needs allowed[] and/or forbidden[]")
        if c.type in ("output_contains", "output_not_contains", "output_exact") and not c.text:
            errors.append(f"rules.{c.id}: {c.type} needs text")
        if c.type == "reference_trajectory" and c.mode == "subsequence":
            errors.append(f"rules.{c.id}: reference_trajectory mode must be superset or exact")
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


class NonAutomated(_Entry):
    """A recommendation that is NOT an evaluator record: who does what instead."""

    kind: Literal["orchestration", "manual_review", "metric_baseline", "external_control"]
    reason: str = Field(min_length=1, max_length=1000)
    obligation: str = Field(default="", max_length=1000)


Evaluator = Annotated[
    ExistingEvaluator | JudgeEvaluator | DerivedEvaluator | CodeEvaluator | NonAutomated,
    Field(discriminator="kind"),
]
CLOUD_KINDS = ("judge", "derived", "code")
NON_AUTOMATED_KINDS = ("orchestration", "manual_review", "metric_baseline", "external_control")


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


def dataset_item(scenario: Scenario, plan_provenance: dict[str, Any]) -> dict[str, Any]:
    """The standard predefined item for one scenario (what the Dataset stores)."""
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
    metadata: dict[str, Any] = {
        "launchpad_assets": {**plan_provenance, "golden_test_id": scenario.golden_test_id},
    }
    if scenario.execution is not None:
        metadata[execution.EXECUTION_KEY] = scenario.execution
    item["metadata"] = metadata
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
    evaluators; every assembled dataset item passes the Dataset/execution gates.
    """
    if serialized_bytes(raw) > PLAN_MAX_BYTES:
        return None, [f"plan exceeds {PLAN_MAX_BYTES} bytes"]
    if not isinstance(raw, dict):
        return None, ["plan must be a JSON object"]
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
    for e in plan.evaluators:
        for gt in e.golden_test_ids:
            if gt not in gts:
                errors.append(f"evaluators.{e.key}: golden test '{gt}' does not exist")
        if isinstance(e, JudgeEvaluator):
            errors += _judge_errors(e)
        if isinstance(e, CodeEvaluator):
            errors += [f"evaluators.{e.key}: {m}" for m in _check_rules(e.rules)]
        if isinstance(e, ExistingEvaluator) and e.evaluator_id.startswith("Builtin.") \
                and e.evaluator_id not in ALL_BUILTIN_EVALUATORS:
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
    if not errors and plan.scenarios:
        items = [dataset_item(s, {"plan": "validation"}) for s in plan.scenarios]
        try:
            execution.validate_items(items)
        except Exception as exc:  # AppError from the dataset gate → plain plan error
            errors.append(f"scenarios: {getattr(exc, 'message', None) or exc}")
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


def _draft_rubric(content: dict[str, Any]) -> str | None:
    """One clearly labelled semantic rubric drafted from the golden tests' pass
    criteria / forbidden behaviour (trace level, uses {context} + {assistant_turn})."""
    lines: list[str] = []
    for g in content.get("golden_tests") or []:
        crit = str(g.get("pass_criteria") or "").strip()
        forb = str(g.get("forbidden_behavior") or "").strip()
        if crit:
            lines.append(f"- [{g.get('id')}] must: {crit}")
        if forb:
            lines.append(f"- [{g.get('id')}] must not: {forb}")
    if not lines:
        return None
    head = (
        "DRAFT rubric generated from the proposal's golden tests — calibrate with domain "
        "experts before relying on it. Judge ONLY the assistant's reply below against the "
        "requirements that apply to this turn; ignore requirements about other turns.\n\n"
        "Conversation so far:\n{context}\n\nAssistant reply under evaluation:\n"
        "{assistant_turn}\n\nRequirements:\n"
    )
    body = "\n".join(lines)
    text = head + body
    if len(text) > 3900:
        text = text[:3880] + "\n- …(truncated)"
    return text


def draft_plan(
    content: dict[str, Any], *, revision: int, content_hash: str, agent_name: str
) -> dict[str, Any]:
    """The editable first draft. Uses the proposal's optional structured
    ``evaluation_plan`` seed when present (validated separately by the proposal
    contract), otherwise classifies what it can identify exactly and leaves the rest
    ``unresolved``. Never marks a prose recommendation as implemented."""
    gts = content.get("golden_tests") or []
    scenarios: list[dict[str, Any]] = []
    for g in gts:
        assertions = []
        if g.get("pass_criteria"):
            assertions.append(str(g["pass_criteria"])[:1000])
        if g.get("forbidden_behavior"):
            assertions.append(("Must not: " + str(g["forbidden_behavior"]))[:1000])
        scenarios.append({
            "scenario_id": _scenario_id(str(g["id"])),
            "golden_test_id": str(g["id"]),
            "turns": [{"input": str(g.get("input") or "")[:8000],
                       "expected_response": str(g.get("expected_response") or "")[:2000]}],
            "expected_trajectory": [str(t)[:200] for t in (g.get("expected_tools") or [])][:10],
            "assertions": assertions,
            "execution": None,
            "note": "",
        })
    evaluators: list[dict[str, Any]] = []
    recommendations: list[dict[str, Any]] = []
    seed = content.get("evaluation_plan") if isinstance(content.get("evaluation_plan"), dict) \
        else None
    seeded_keys: set[str] = set()
    if seed:
        for e in seed.get("evaluators") or []:
            if isinstance(e, dict) and isinstance(e.get("key"), str):
                evaluators.append(dict(e))
                seeded_keys.add(e["key"])
    used_keys = set(seeded_keys)
    for i, text in enumerate(content.get("evaluator_recommendations") or []):
        text = str(text)
        ids = [m for m in _KNOWN_ID_RE.findall(text) if m in ALL_BUILTIN_EVALUATORS
               or m.startswith("ThirdParty.")]
        mapped: list[str] = []
        for evaluator_id in dict.fromkeys(ids):
            key = _slug(evaluator_id.replace(".", "_"), 32)
            if key not in used_keys:
                used_keys.add(key)
                evaluators.append({
                    "kind": "existing", "key": key, "title": evaluator_id,
                    "evaluator_id": evaluator_id, "golden_test_ids": [],
                    "blocking": False, "threshold": None,
                    "note": f"named in recommendation #{i + 1}",
                })
            mapped.append(key)
        seeded = [str(k) for k in ((seed or {}).get("recommendation_keys") or {}).get(str(i), [])
                  if str(k) in seeded_keys] if seed else []
        if seeded:  # an explicit structured mapping wins over id spotting in prose
            for key in mapped:
                if key not in seeded and key not in seeded_keys:
                    evaluators[:] = [e for e in evaluators if e.get("key") != key]
                    used_keys.discard(key)
            mapped = list(dict.fromkeys(seeded))
        else:
            mapped = list(dict.fromkeys(mapped))
        recommendations.append({
            "index": i, "text": text, "mapped_to": mapped,
            "status": "mapped" if mapped else "unresolved",
            "note": "" if mapped else "no exact evaluator identified — classify or decline",
        })
    rubric = _draft_rubric(content)
    if rubric and "draft_rubric" not in used_keys:
        evaluators.append({
            "kind": "judge", "key": "draft_rubric",
            "title": "Draft semantic rubric (needs calibration)",
            "name": _slug(f"{agent_name}_gt_rubric", 48),
            "instructions": rubric,
            "rating_scale": [r.model_dump() for r in DEFAULT_RATING_SCALE],
            "model_id": DEFAULT_JUDGE_MODEL, "level": "TRACE",
            "description": f"Drafted from golden tests of proposal revision {revision}",
            "golden_test_ids": [str(g["id"]) for g in gts
                                if g.get("pass_criteria") or g.get("forbidden_behavior")],
            "blocking": False, "threshold": None, "draft": True,
            "note": "platform draft — review wording, model and level before creating",
        })
    if any(g.get("expected_tools") for g in gts) and "expected_tools" not in used_keys:
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
            "golden_test_ids": [str(g["id"]) for g in gts if g.get("expected_tools")],
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
        "blocked_golden_tests": [],
        "grant_workspace_execution_role": True,
        "summary": "Draft prepared by the platform from the proposal's golden tests and "
                   "evaluator recommendations. Unresolved recommendations are NOT implemented; "
                   "classify each as an existing/new evaluator, a runner check, a human "
                   "review, a metric baseline or an external control before creating assets.",
    }


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
