"""Custom evaluators as ledger rows (N12 E1-1; contract amendment CA-3).

The Global console stored custom judges / derived / code evaluators in AgentCore
Evaluations (the managed evaluator CRUD). That service does not exist in
aws-cn, so the ``eval_evaluators`` row *is* the evaluator: the routers write it, the
engine (``strands_eval.evaluators.build``) consumes :func:`definition_of`, and
``GET /evaluators`` renders the builtin catalog plus the workspace's rows.

Pure ledger + catalog helpers — no AWS client anywhere in this module.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.evaluation.models import EvalEvaluator
from app.schemas import model_catalog
from app.schemas.agent import (
    DEFAULT_MODEL_ID,
    assert_model_available,
    assert_model_visible,
    hub_model_refusal_code,
)
from app.services import creators
from app.services.strands_eval.evaluators import BUILTIN_LEVELS, LEVELS, TRAJECTORY_LEVELS
from app.services.strands_eval.judge_placeholders import GROUND_TRUTH_PLACEHOLDERS

# A batch applies at most this many evaluators (and insights) — kept from the baseline so
# a run never replays every scenario only to be refused at evaluation time.
MAX_BATCH_EVALUATORS = 10

# LLM-judge model default (custom judges and derived evaluators) — CN catalog (C3/CA-1c).
# The OpenAPI contract pins this constant as the request default (`ALLOWED_DELTA` CA-1c);
# the live value a request WITHOUT `model_id` gets is the `judge` model role
# (`schemas.agent.judge_default_model_id()`, N17-obs252). The former
# `JUDGE_FALLBACK_MODEL_ID` literal had no consumer and was deleted (delete-not-flag).
JUDGE_DEFAULT_MODEL_ID = DEFAULT_MODEL_ID

# Default built-in evaluators used across batch + online evaluation.
BUILTIN_EVALUATORS = [
    "Builtin.GoalSuccessRate",
    "Builtin.Helpfulness",
    "Builtin.Correctness",
]
# The contract catalog: 16 prompt-template evaluators + 3 trajectory matchers.
ALL_BUILTIN_EVALUATORS: dict[str, str] = dict(BUILTIN_LEVELS)
TRAJECTORY_EVALUATORS: dict[str, str] = dict(TRAJECTORY_LEVELS)
KNOWN_BUILTINS: dict[str, str] = dict(LEVELS)

# Insight kinds an ``insights`` run / online config may select (the baseline ids; the CN
# engine maps them onto strands ``detectors`` + a summariser — plan §B.4).
INSIGHT_TYPES = [
    "Builtin.Insight.FailureAnalysis",
    "Builtin.Insight.UserIntent",
    "Builtin.Insight.ExecutionSummary",
]

# Evaluators whose score is a *penalty*: Builtin.Refusal scores 1.0 when the agent
# refused, so the lower-mean arm is the better arm.
LOWER_IS_BETTER_EVALUATORS = frozenset({"Builtin.Refusal"})

# N17-obs425: the normalized score (penalty evaluators inverted) a row must reach to count as
# PASSED in the console and the run summaries. 0.7 by default (a 5- / 7-point scale's
# upper labels: Generally Yes 0.75, Very helpful 0.833); a two-label evaluator passes only on
# its good label (Yes / Not Harmful / a trajectory match), and SkillInstructionFollowing's
# five labels (Fully 1.0 / Mostly 0.75 / Partially 0.5 / Minimally 0.25 / Not 0) pass only
# on Fully Followed — the accepted bar, stricter than the library's own ``test_pass``
# (≥ 0.75). Correctness passes only on Perfectly Correct. Helpfulness keeps the default: on
# its 7-point scale 0.7 means Very helpful (0.833) or better. Custom evaluators keep the
# default. Each bar is one value here (``lib/evaluators.ts`` mirrors the table).
DEFAULT_PASS_THRESHOLD = 0.7
PASS_THRESHOLDS: dict[str, float] = {
    # user decision 2026-10-25: pass = Completely Yes only (hard calibration tier
    # c76c90a4a420b75b-hard: at 0.75 κ 0.49–0.52 with 50–53 % false passes; at 1.0 κ 1.0)
    "Builtin.Faithfulness": 1.0,
    # lead decision 2026-10-25, the Faithfulness rule: pass = Completely Yes only (calibration
    # c76c90a4a420b75b: κ 1.0 at 1.0, 0/9 control false fails; at 0.7 the easy tier passed 5/12
    # self-contradictions; real Generally Yes turns 63 % real flaws, coherence-real-turns-20261025)
    "Builtin.Coherence": 1.0,
    "Builtin.Correctness": 1.0,
    "Builtin.GoalSuccessRate": 1.0,
    "Builtin.InstructionFollowing": 1.0,
    "Builtin.Refusal": 1.0,
    "Builtin.Harmfulness": 1.0,
    "Builtin.Stereotyping": 1.0,
    "Builtin.ToolSelectionAccuracy": 1.0,
    "Builtin.ToolParameterAccuracy": 1.0,
    "Builtin.SkillSelectionAccuracy": 1.0,
    "Builtin.SkillInstructionFollowing": 1.0,
    "Builtin.TrajectoryExactOrderMatch": 1.0,
    "Builtin.TrajectoryInOrderMatch": 1.0,
    "Builtin.TrajectoryAnyOrderMatch": 1.0,
}

# ``GROUND_TRUTH_PLACEHOLDERS`` (imported above, ``strands_eval.judge_placeholders``; read with
# their aliases by ``ground_truth_placeholders``, N17-obs437): the judge placeholders fed from
# ground truth rather than from the trace itself — a live session carries none of them
# (online evaluation / SCORE NOW refuse such judges).

_EVALUATOR_TYPE = {"judge": "Custom", "derived": "CustomDerived", "code": "CustomCode"}


def evaluator_polarity(evaluator: str) -> int:
    """+1 when a higher mean is the better arm, -1 when a lower mean is. Accepts a bare
    id or an evaluator ARN (``…:evaluator/<id>``); custom judges are +1."""
    return -1 if (evaluator or "").rsplit("/", 1)[-1] in LOWER_IS_BETTER_EVALUATORS else 1


def pass_threshold(evaluator: str) -> float:
    """N17-obs425: the pass bar of an evaluator (bare id or ARN); see ``PASS_THRESHOLDS``."""
    return PASS_THRESHOLDS.get((evaluator or "").rsplit("/", 1)[-1], DEFAULT_PASS_THRESHOLD)


def passes(score: float, evaluator: str) -> bool:
    """A scored row passes when its polarity-oriented score reaches the evaluator's bar
    (a tiny slack absorbs float noise such as 0.833 stored as 0.8329999)."""
    oriented = 1.0 - score if evaluator_polarity(evaluator) < 0 else score
    return oriented >= pass_threshold(evaluator) - 1e-9


# The renderer's aliases of two ground-truth placeholders (``judge_placeholders.RENDERERS``;
# N17-obs437: a SESSION judge may write ``{expected_trajectory}`` — it reads ground truth
# just the same). Hub-side only: the worker zip's renderer is unchanged.
GROUND_TRUTH_ALIASES = {"expected_output": "expected_response",
                        "expected_trajectory": "expected_tool_trajectory"}


def ground_truth_placeholders(instructions: str | None) -> list[str]:
    """Ground-truth fields a custom judge's instructions reference, canonical names in
    ``GROUND_TRUTH_PLACEHOLDERS`` order — an alias counts as its field (N17-obs437)."""
    text = instructions or ""
    used = {p for p in GROUND_TRUTH_PLACEHOLDERS if f"{{{p}}}" in text}
    used |= {canon for alias, canon in GROUND_TRUTH_ALIASES.items() if f"{{{alias}}}" in text}
    return [p for p in GROUND_TRUTH_PLACEHOLDERS if p in used]


def is_builtin(evaluator_id: str) -> bool:
    return evaluator_id in KNOWN_BUILTINS


def is_managed_id(evaluator_id: str) -> bool:
    """Builtin / third-party ids are catalog entries, never rows."""
    return evaluator_id.startswith(("Builtin.", "ThirdParty."))


def _require_model(model_id: str, resources: Mapping[str, Any] | None = None,
                   principal: model_catalog.Principal | None = None) -> str:
    # N17-obs227: another member's personal provider's model is refused FIRST (422
    # `models.model_not_visible`) — the hub would otherwise spend THAT member's key on every
    # judge call of this evaluator; `principal` None (workers, internal callers) skips it.
    # Then catalog AND a configured provider in the workspace (N-R6-obs1:
    # `model.provider_unconfigured`; `resources` = the workspace's map, hub env otherwise)
    try:
        assert_model_visible(model_id, principal, where="judge model_id")
        assert_model_available(model_id, where="judge model_id", resources=resources)
    except ValueError as exc:
        raise AppError(hub_model_refusal_code(exc), str(exc), {"model_id": model_id},
                       status_code=422) from exc
    return model_id


def base_level(base_evaluator_id: str) -> str:
    """Level of a derived evaluator = its base's; only builtin judges may be a base
    (trajectory matchers take no model, third-party evaluators do not exist in CN)."""
    level = BUILTIN_LEVELS.get(base_evaluator_id)
    if level is None:
        raise AppError(
            "evaluator.base_not_found",
            f"base evaluator {base_evaluator_id} not found",
            status_code=400,
        )
    return level


def _require_name_free(db: Session, workspace_id: str | None, name: str,
                       *, exclude_id: str | None = None) -> None:
    query = db.query(EvalEvaluator).filter(
        EvalEvaluator.workspace_id == workspace_id, EvalEvaluator.name == name
    )
    if exclude_id:
        query = query.filter(EvalEvaluator.id != exclude_id)
    if query.first() is not None:
        raise AppError(
            "evaluator.name_taken",
            f"an evaluator named '{name}' already exists in this workspace",
            {"name": name}, status_code=409,
        )


# ─── rows ────────────────────────────────────────────────────────────────────
def list_rows(db: Session, workspace_id: str | None) -> list[EvalEvaluator]:
    return (
        db.query(EvalEvaluator)
        .filter(EvalEvaluator.workspace_id == workspace_id)
        .order_by(EvalEvaluator.created_at.asc(), EvalEvaluator.id.asc())
        .all()
    )


def find(db: Session, workspace_id: str | None, evaluator_id: str) -> EvalEvaluator | None:
    row = db.get(EvalEvaluator, evaluator_id)
    if row is None or row.workspace_id != workspace_id:
        return None
    return row


def get(db: Session, workspace_id: str | None, evaluator_id: str) -> EvalEvaluator:
    """The workspace's row, or 404 ``evaluator.not_found`` (another workspace's row reads
    as absent, like every other foreign id)."""
    row = find(db, workspace_id, evaluator_id)
    if row is None:
        raise NotFoundError("evaluator.not_found", f"evaluator {evaluator_id} not found")
    return row


# P7.1: the fields that change what an evaluator scores (a description edit does not)
SCORING_FIELDS = ("definition", "level", "instructions", "rating_scale", "model_id",
                  "base_evaluator_id", "lambda_arn", "lambda_timeout_s")


def definition_hash(row: EvalEvaluator) -> str:
    """sha256 prefix of the row's scoring fields (canonical JSON)."""
    payload = {f: getattr(row, f, None) for f in SCORING_FIELDS}
    payload["rating_scale"] = list(payload["rating_scale"] or [])
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def stamp_version(row: EvalEvaluator) -> None:
    """Version 1 at create; +1 when the scoring definition changed (P7.1)."""
    digest = definition_hash(row)
    if row.definition_hash != digest:
        row.version = 1 if row.definition_hash is None and not row.version else \
            int(row.version or 1) + 1
        row.definition_hash = digest


def create_judge(
    db: Session, workspace_id: str | None, *, name: str, instructions: str,
    rating_scale: list[dict[str, Any]], model_id: str, level: str, description: str = "",
    resources: Mapping[str, Any] | None = None,
    creator: creators.Creator | None = None,
    principal: model_catalog.Principal | None = None,
) -> EvalEvaluator:
    _require_name_free(db, workspace_id, name)
    row = EvalEvaluator(
        workspace_id=workspace_id, name=name, level=level, description=description,
        definition="judge", instructions=instructions, rating_scale=list(rating_scale),
        model_id=_require_model(model_id, resources, principal),
    )
    creators.stamp(row, creator)
    stamp_version(row)
    db.add(row)
    db.commit()
    return row


def create_derived(
    db: Session, workspace_id: str | None, *, name: str, base_evaluator_id: str,
    model_id: str, description: str = "",
    resources: Mapping[str, Any] | None = None,
    creator: creators.Creator | None = None,
    principal: model_catalog.Principal | None = None,
) -> EvalEvaluator:
    level = base_level(base_evaluator_id)
    _require_name_free(db, workspace_id, name)
    row = EvalEvaluator(
        workspace_id=workspace_id, name=name, level=level, description=description,
        definition="derived", base_evaluator_id=base_evaluator_id,
        model_id=_require_model(model_id, resources, principal), rating_scale=[],
    )
    creators.stamp(row, creator)
    stamp_version(row)
    db.add(row)
    db.commit()
    return row


def create_code(
    db: Session, workspace_id: str | None, *, name: str, lambda_arn: str,
    lambda_timeout_s: int, level: str, description: str = "",
    creator: creators.Creator | None = None,
) -> EvalEvaluator:
    _require_name_free(db, workspace_id, name)
    row = EvalEvaluator(
        workspace_id=workspace_id, name=name, level=level, description=description,
        definition="code", lambda_arn=lambda_arn, lambda_timeout_s=int(lambda_timeout_s),
        rating_scale=[],
    )
    creators.stamp(row, creator)
    stamp_version(row)
    db.add(row)
    db.commit()
    return row


def update_judge(
    db: Session, row: EvalEvaluator, *, instructions: str, rating_scale: list[dict[str, Any]],
    model_id: str, level: str, description: str,
    resources: Mapping[str, Any] | None = None,
    principal: model_catalog.Principal | None = None,
) -> EvalEvaluator:
    row.instructions = instructions
    row.rating_scale = list(rating_scale)
    row.model_id = _require_model(model_id, resources, principal)
    row.level = level
    row.description = description
    stamp_version(row)
    db.commit()
    return row


def update_derived(
    db: Session, row: EvalEvaluator, *, base_evaluator_id: str, model_id: str,
    description: str,
    resources: Mapping[str, Any] | None = None,
    principal: model_catalog.Principal | None = None,
) -> EvalEvaluator:
    row.level = base_level(base_evaluator_id)
    row.base_evaluator_id = base_evaluator_id
    row.model_id = _require_model(model_id, resources, principal)
    row.description = description
    stamp_version(row)
    db.commit()
    return row


def update_code(
    db: Session, row: EvalEvaluator, *, lambda_arn: str, lambda_timeout_s: int, level: str,
    description: str,
) -> EvalEvaluator:
    row.lambda_arn = lambda_arn
    row.lambda_timeout_s = int(lambda_timeout_s)
    row.level = level
    row.description = description
    stamp_version(row)
    db.commit()
    return row


def delete(db: Session, row: EvalEvaluator) -> None:
    db.delete(row)
    db.commit()


# ─── projections ─────────────────────────────────────────────────────────────
def definition_of(row: EvalEvaluator) -> dict[str, Any]:
    """The engine's evaluator definition (``strands_eval.evaluators.build`` input)."""
    return {
        "id": row.id,
        "name": row.name,
        "definition": row.definition,
        "level": row.level,
        "instructions": row.instructions,
        "rating_scale": list(row.rating_scale or []),
        "model_id": row.model_id,
        "base_evaluator_id": row.base_evaluator_id,
        "lambda_arn": row.lambda_arn,
        "lambda_timeout_s": row.lambda_timeout_s,
        # P7.1: which version of the definition this is
        "version": row.version,
        "definition_hash": row.definition_hash,
    }


def provider_of(row: EvalEvaluator) -> str:
    """Derived evaluators keep their base's provider (every base is an AWS-named builtin
    of the catalog); judges and code evaluators are the operator's own."""
    return "AWS" if row.definition == "derived" else "Custom"


def evaluator_out(row: EvalEvaluator) -> dict[str, Any]:
    """The frozen ``GET /evaluators/{id}`` key-set (plan §A.1) from a row."""
    return {
        "id": row.id,
        "name": row.name,
        "level": row.level,
        "description": row.description or "",
        "definition": row.definition,
        "instructions": row.instructions or "" if row.definition == "judge" else "",
        "rating_scale": list(row.rating_scale or []) if row.definition == "judge" else [],
        "model_id": row.model_id if row.definition != "code" else None,
        "base_evaluator_id": row.base_evaluator_id,
        "lambda_arn": row.lambda_arn,
        "lambda_timeout_s": row.lambda_timeout_s,
        "evaluator_type": _EVALUATOR_TYPE.get(row.definition, "Custom"),
        "provider": provider_of(row),
        "status": row.status,
        # P7.1 (additive): the scoring definition's version and hash
        "version": row.version,
        "definition_hash": row.definition_hash,
        "pass_threshold": DEFAULT_PASS_THRESHOLD,  # N17-obs425
        **creators.fields(row),
    }


# Builtin ids Global AgentCore Evaluations does not have (N17-obs403)
LAUNCHPAD_ONLY_BUILTINS = frozenset({"Builtin.ContextRelevance"})


def library_version() -> str:
    """A built-in's version: the evaluation library release (``strands-agents-evals 1.5.0``)
    — what a run's evaluator snapshot and the evaluator detail both name (P7.1, N17-obs434)."""
    try:
        from importlib.metadata import version

        return f"strands-agents-evals {version('strands-agents-evals')}"
    except Exception:  # noqa: BLE001
        return "strands-agents-evals"


def builtin_out(evaluator_id: str) -> dict[str, Any]:
    """A catalog builtin in the same key-set (what the baseline's ``GetEvaluator`` on a
    ``Builtin.*`` id rendered)."""
    launchpad_only = evaluator_id in LAUNCHPAD_ONLY_BUILTINS
    return {
        "id": evaluator_id,
        "name": evaluator_id.split(".", 1)[-1],
        "level": KNOWN_BUILTINS[evaluator_id],
        # audit 3.2 / N17-obs403: Global has no such evaluator — a Launchpad-only judge with
        # a fixed scale, said so wherever it is listed
        "description": ("Launchpad approximation — Global AgentCore Evaluations has no "
                        "ContextRelevance evaluator; Launchpad-only judge, labels Fully / "
                        "Partially / Not Relevant" if launchpad_only else ""),
        "definition": "judge",
        "instructions": "",
        "rating_scale": [],
        "model_id": None,
        "base_evaluator_id": None,
        "lambda_arn": None,
        "lambda_timeout_s": None,
        "evaluator_type": "Builtin",
        "provider": "Launchpad" if launchpad_only else "AWS",
        "status": "ACTIVE",
        "version": library_version(),  # N17-obs434: the library release, as runs record it
        "definition_hash": None,
        "pass_threshold": pass_threshold(evaluator_id),  # N17-obs425
        # N17-obs437: the trajectory matchers score against the dataset's expected trajectory
        "requires_ground_truth": evaluator_id in TRAJECTORY_EVALUATORS,
        "ground_truth_fields": (["expected_tool_trajectory"]
                                if evaluator_id in TRAJECTORY_EVALUATORS else []),
        **creators.empty(),
    }


def catalog_entry(row: EvalEvaluator) -> dict[str, Any]:
    """One ``GET /evaluators`` list entry for a custom row."""
    return {
        "id": row.id,
        "name": row.name,
        "level": row.level,
        "status": row.status,
        "source": "custom",
        "evaluator_type": _EVALUATOR_TYPE.get(row.definition, "Custom"),
        "provider": provider_of(row),
        "definition": row.definition,
        "pass_threshold": DEFAULT_PASS_THRESHOLD,  # N17-obs425
        # N17-obs428 (additive): what the console's zh label display maps a custom row
        # through — a derived row's base built-in, and the labels of the row's own scale
        "base_evaluator_id": row.base_evaluator_id,
        "scale_labels": [str(p.get("label") or "") for p in row.rating_scale or []
                         if isinstance(p, dict)],
        # N17-obs434 (additive): the scoring definition's version, shown in the list
        "version": row.version,
        **creators.fields(row),
    }


def resolve_definitions(
    db: Session, workspace: Any, evaluator_ids: list[str]
) -> list[dict[str, Any]]:
    """Builtin ids → ``{"id"}``; custom ids → their row definition; anything else is
    refused with 422 ``run.evaluator_not_found`` before any replay or judge call."""
    workspace_id = getattr(workspace, "id", None)
    out: list[dict[str, Any]] = []
    for evaluator_id in dict.fromkeys(evaluator_ids):
        if evaluator_id in KNOWN_BUILTINS:
            out.append({"id": evaluator_id})
            continue
        row = None if is_managed_id(evaluator_id) else find(db, workspace_id, evaluator_id)
        if row is None:
            raise AppError(
                "run.evaluator_not_found",
                f"evaluator {evaluator_id} does not exist in this workspace — drop it or pick "
                "one from the catalog",
                {"evaluator": evaluator_id}, status_code=422,
            )
        out.append(definition_of(row))
    return out
