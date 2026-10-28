"""Criteria sets (判据表): versions, templates, validation, publish and sign.

A criteria set is a lineage of versions. Version N is either a draft (editable) or
published (frozen). Templates (`kind="template"`) hold the criteria shared by every
agent of one scenario; an agent set may inherit one template version and then only
*overrides* (same key, changed) or *adds* criteria, or drops a template criterion with a
written reason. Every version is materialized: its `criteria` rows are the full,
effective table, so a run or gate report needs only `(lineage, version)`.

Rules mirror the Agent-DLC handbook (§1.10.3 `validate`):

* a red line must be decided by code, a trajectory matcher or a metric — never a judge;
* cost and performance criteria are metrics, not evaluator scores;
* a judge criterion only gates once calibrated (`effective_tier`);
* the subject of a criterion is the agent and its predicate is observable;
* every dimension has a criterion (or a written "not applicable"), and at least one
  red line exists, before a set can be published.
"""

from __future__ import annotations

import copy
import re
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.actor import same_actor, user_id_for
from app.core.errors import AppError, NotFoundError, next_step
from app.evaluation import stats
from app.models.dlc import (
    DENOMINATORS,
    DIMENSIONS,
    EXPECTED_TYPES,
    LEVELS,
    METRICS,
    TIERS,
    CriteriaSet,
    Criterion,
)

# evaluator id → "code" | "trajectory" | "judge"; injected by the routes (it may call
# GetEvaluator) and stubbed by tests
EvaluatorKindOf = Callable[[str], str]

# fields a template row and an agent row are compared on to tell inherited from override
_CONTENT_FIELDS = (
    "text", "dimension", "tier", "threshold", "metric_rule", "level", "executor",
    "denominator", "expected_type", "pass_k", "attribution_layer",
)
_SUBJECT_SMELLS = re.compile(
    r"(让客户|客户觉得|用户觉得|使客户|customers? feel|users? feel|make (the )?customers?)",
    re.IGNORECASE,
)
_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,31}$")
NOT_APPLICABLE_PREFIX = "n/a:"


def _now() -> datetime:
    return datetime.now(UTC)


def static_evaluator_kind(evaluator_id: str) -> str | None:
    """The kind known without an AWS call, or None for a custom evaluator."""
    if evaluator_id.startswith("Builtin.Trajectory"):
        return "trajectory"
    if evaluator_id.startswith(("Builtin.", "ThirdParty.")):
        return "judge"
    return None


# ── reading ────────────────────────────────────────────────────────────────────


def criteria_of(db: Session, set_id: str) -> list[Criterion]:
    return list(
        db.scalars(
            select(Criterion).where(Criterion.set_id == set_id).order_by(Criterion.position)
        ).all()
    )


def get_version(
    db: Session, workspace_id: str, lineage_id: str, version: int | None = None
) -> CriteriaSet:
    """A version of a lineage; `None` ⇒ the newest (draft if one exists)."""
    query = select(CriteriaSet).where(
        CriteriaSet.workspace_id == workspace_id, CriteriaSet.lineage_id == lineage_id
    )
    if version is not None:
        query = query.where(CriteriaSet.version == version)
    row = db.scalars(query.order_by(CriteriaSet.version.desc())).first()
    if row is None:
        raise NotFoundError("criteria.not_found", "criteria set not found")
    return row


def latest_published(db: Session, workspace_id: str, lineage_id: str) -> CriteriaSet | None:
    return db.scalars(
        select(CriteriaSet)
        .where(
            CriteriaSet.workspace_id == workspace_id,
            CriteriaSet.lineage_id == lineage_id,
            CriteriaSet.status == "published",
        )
        .order_by(CriteriaSet.version.desc())
    ).first()


def agent_set(db: Session, workspace_id: str, agent_id: str) -> CriteriaSet | None:
    """The agent's latest *published* criteria set version, if any."""
    return db.scalars(
        select(CriteriaSet)
        .where(
            CriteriaSet.workspace_id == workspace_id,
            CriteriaSet.agent_id == agent_id,
            CriteriaSet.kind == "agent",
            CriteriaSet.status == "published",
        )
        .order_by(CriteriaSet.version.desc())
    ).first()


def list_lineages(
    db: Session, workspace_id: str, *, kind: str | None = None, agent_id: str | None = None
) -> list[CriteriaSet]:
    """The newest version of every lineage (drafts included)."""
    query = select(CriteriaSet).where(CriteriaSet.workspace_id == workspace_id)
    if kind:
        query = query.where(CriteriaSet.kind == kind)
    if agent_id:
        query = query.where(CriteriaSet.agent_id == agent_id)
    newest: dict[str, CriteriaSet] = {}
    for row in db.scalars(query.order_by(CriteriaSet.version)).all():
        newest[row.lineage_id] = row
    return sorted(newest.values(), key=lambda r: r.updated_at or r.created_at, reverse=True)


def versions_of(db: Session, workspace_id: str, lineage_id: str) -> list[CriteriaSet]:
    return list(
        db.scalars(
            select(CriteriaSet)
            .where(
                CriteriaSet.workspace_id == workspace_id, CriteriaSet.lineage_id == lineage_id
            )
            .order_by(CriteriaSet.version.desc())
        ).all()
    )


# ── projection ─────────────────────────────────────────────────────────────────


def criterion_out(row: Criterion, *, calibrated: bool | None = None) -> dict[str, Any]:
    out = {
        "key": row.key,
        "position": row.position,
        "text": row.text,
        "dimension": row.dimension,
        "tier": row.tier,
        "threshold": row.threshold,
        "metric_rule": row.metric_rule,
        "level": row.level,
        "executor": row.executor or {},
        "denominator": row.denominator,
        "expected_type": row.expected_type,
        "pass_k": row.pass_k,
        "attribution_layer": row.attribution_layer,
        "owner": row.owner,
        "examples": row.examples or [],
        "notes": row.notes,
        "origin": row.origin,
        "online": online_capability(row),
        "is_judge": is_judge(row),
    }
    out["effective_tier"] = effective_tier(row, calibrated=bool(calibrated))
    out["calibrated"] = calibrated if is_judge(row) else None
    return out


def set_out(row: CriteriaSet) -> dict[str, Any]:
    return {
        "id": row.id,
        "lineage_id": row.lineage_id,
        "kind": row.kind,
        "agent_id": row.agent_id,
        "template_id": row.template_id,
        "template_version": row.template_version,
        "name": row.name,
        "description": row.description,
        "scenario": row.scenario,
        "version": row.version,
        "status": row.status,
        "parent_version": row.parent_version,
        "source": row.source,
        "signed_by": row.signed_by,
        "signed_at": row.signed_at.isoformat() if row.signed_at else None,
        "sign_note": row.sign_note,
        "removals": row.removals or [],
        "published_at": row.published_at.isoformat() if row.published_at else None,
        "created_by": row.created_by,
        "updated_by": row.updated_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


# ── classification helpers ─────────────────────────────────────────────────────


def is_judge(row: Criterion | dict[str, Any]) -> bool:
    executor = (row.executor if isinstance(row, Criterion) else row.get("executor")) or {}
    return executor.get("kind") == "evaluator" and executor.get("evaluator_kind") == "judge"


def effective_tier(row: Criterion | dict[str, Any], *, calibrated: bool) -> str:
    """A judge criterion only blocks once calibrated; until then it is observed."""
    tier = row.tier if isinstance(row, Criterion) else row.get("tier")
    if tier in ("redline", "gate") and is_judge(row) and not calibrated:
        return "observe"
    return tier


def online_capability(row: Criterion) -> str:
    """Ground-truth criteria cannot be scored on live traffic (AWS refuses the config)."""
    executor = row.executor or {}
    if executor.get("kind") == "metric":
        return "online_ok"
    if executor.get("kind") == "human":
        return "offline_only"
    if executor.get("evaluator_kind") == "trajectory" or executor.get("needs_ground_truth"):
        return "offline_only"
    return "online_ok"


# ── validation ─────────────────────────────────────────────────────────────────


def _finding(level: str, code: str, message: str, key: str | None = None) -> dict[str, Any]:
    return {"level": level, "code": code, "message": message, "key": key}


def normalize_criterion(raw: dict[str, Any], kind_of: EvaluatorKindOf) -> dict[str, Any]:
    """Shape one incoming row; resolve the evaluator kind once, at save time."""
    executor = dict(raw.get("executor") or {})
    kind = executor.get("kind") or "evaluator"
    if kind == "manual":
        kind = "human"  # the editors sent "manual" for 人工 until 2026-10; old bundles still may
    executor["kind"] = kind
    if kind == "evaluator" and executor.get("evaluator_id"):
        executor["evaluator_kind"] = (
            static_evaluator_kind(executor["evaluator_id"])
            or kind_of(executor["evaluator_id"])
        )
    else:
        executor.pop("evaluator_kind", None)
    out = {
        "key": str(raw.get("key") or "").strip(),
        "text": str(raw.get("text") or "").strip(),
        "dimension": raw.get("dimension"),
        "tier": raw.get("tier"),
        "threshold": raw.get("threshold"),
        "metric_rule": raw.get("metric_rule"),
        "level": raw.get("level") or "session",
        "executor": executor,
        "denominator": raw.get("denominator") or "sessions",
        "expected_type": raw.get("expected_type") or "deterministic",
        "pass_k": raw.get("pass_k"),
        "attribution_layer": raw.get("attribution_layer"),
        "owner": str(raw.get("owner") or ""),
        "examples": list(raw.get("examples") or []),
        "notes": str(raw.get("notes") or ""),
    }
    if out["tier"] == "redline":
        out["threshold"] = None  # a red line is always 0 violations
    return out


def validate(
    rows: Iterable[dict[str, Any]],
    *,
    removals: Iterable[dict[str, Any]] = (),
    template_rows: Iterable[dict[str, Any]] = (),
    for_publish: bool = False,
) -> list[dict[str, Any]]:
    """Errors block publishing; warnings are shown in the editor."""
    rows = list(rows)
    findings: list[dict[str, Any]] = []
    keys: set[str] = set()
    for row in rows:
        key = row.get("key") or ""
        if not _KEY_RE.match(key):
            findings.append(_finding("error", "criteria.bad_key",
                                     "a key is 1–32 letters, digits, '.', '_' or '-'", key))
        elif key in keys:
            findings.append(_finding("error", "criteria.duplicate_key", "duplicate key", key))
        keys.add(key)
        if not row.get("text"):
            findings.append(
                _finding("error", "criteria.empty_text", "criterion text is empty", key)
            )
        elif _SUBJECT_SMELLS.search(row["text"]):
            findings.append(_finding(
                "warning", "criteria.subject_not_agent",
                "write the criterion with the agent as subject and an observable predicate",
                key,
            ))
        if row.get("dimension") not in DIMENSIONS:
            findings.append(_finding("error", "criteria.bad_dimension",
                                     f"dimension must be one of {', '.join(DIMENSIONS)}", key))
        if row.get("tier") not in TIERS:
            findings.append(_finding("error", "criteria.bad_tier",
                                     f"tier must be one of {', '.join(TIERS)}", key))
        if row.get("level") not in LEVELS:
            findings.append(_finding("error", "criteria.bad_level",
                                     f"level must be one of {', '.join(LEVELS)}", key))
        if row.get("denominator") not in DENOMINATORS:
            findings.append(_finding("error", "criteria.bad_denominator",
                                     f"denominator must be one of {', '.join(DENOMINATORS)}", key))
        if row.get("expected_type") not in EXPECTED_TYPES:
            findings.append(_finding("error", "criteria.bad_expected_type",
                                     "unknown expected type", key))
        executor = row.get("executor") or {}
        kind = executor.get("kind")
        if kind not in ("evaluator", "metric", "human"):
            findings.append(_finding("error", "criteria.bad_executor",
                                     "executor kind must be evaluator, metric or human", key))
        if kind == "evaluator" and not executor.get("evaluator_id"):
            findings.append(_finding("error", "criteria.no_evaluator",
                                     "pick the evaluator that decides this criterion", key))
        if row.get("tier") == "redline" and executor.get("evaluator_kind") == "judge":
            findings.append(_finding(
                "error", "criteria.redline_judge",
                "a red line cannot be decided by an LLM judge — use a code assertion, a "
                "trajectory matcher or a metric",
                key,
            ))
        if row.get("dimension") in ("cost", "performance"):
            if kind != "metric":
                findings.append(_finding(
                    "error", "criteria.metric_dimension",
                    "cost and performance criteria are measured metrics, not evaluator scores",
                    key,
                ))
        if kind == "metric":
            rule = row.get("metric_rule") or {}
            if rule.get("metric") not in METRICS or rule.get("op") not in ("<=", ">="):
                findings.append(_finding(
                    "error", "criteria.bad_metric_rule",
                    f"a metric rule needs metric ({', '.join(METRICS)}), op (<= or >=) and "
                    "value",
                    key,
                ))
            elif not isinstance(rule.get("value"), (int, float)):
                findings.append(_finding("error", "criteria.bad_metric_rule",
                                         "metric value must be a number", key))
        elif row.get("tier") == "gate":
            threshold = row.get("threshold")
            if not isinstance(threshold, (int, float)) or not 0 < threshold <= 1:
                findings.append(_finding("error", "criteria.bad_threshold",
                                         "a gate needs a pass-rate threshold in (0, 1]", key))
        pass_k = row.get("pass_k")
        if pass_k:
            k = pass_k.get("k")
            if not isinstance(k, int) or not 2 <= k <= 10 or pass_k.get("mode", "all") not in (
                "all", "majority",
            ):
                findings.append(_finding("error", "criteria.bad_pass_k",
                                         "pass^k needs k in 2..10 and mode all|majority", key))
        if executor.get("evaluator_kind") == "judge" and row.get("tier") != "observe":
            findings.append(_finding(
                "warning", "criteria.judge_uncalibrated_until_calibrated",
                "a judge criterion is observed (does not block) until it is calibrated",
                key,
            ))
        polarities = {e.get("polarity") for e in row.get("examples") or []}
        if executor.get("evaluator_kind") == "judge" and not {"positive", "negative"} <= polarities:
            findings.append(_finding(
                "warning", "criteria.judge_needs_examples",
                "add at least one positive and one negative example — they are how the judge "
                "is calibrated",
                key,
            ))
    # template rules (agent sets)
    template = {r["key"]: r for r in template_rows}
    removed = {r.get("key"): r for r in removals}
    for key, reason in removed.items():
        if key not in template:
            findings.append(_finding("error", "criteria.removal_not_template",
                                     "only template criteria can be removed", key))
        elif not str(reason.get("reason") or "").strip():
            findings.append(_finding("error", "criteria.removal_needs_reason",
                                     "removing a template criterion needs a written reason", key))
    for row in rows:
        base = template.get(row.get("key"))
        if base and base.get("tier") == "redline" and row.get("tier") != "redline":
            findings.append(_finding(
                "error", "criteria.redline_demoted",
                "a template red line can be tightened, not demoted", row.get("key"),
            ))
    if for_publish:
        present = {row.get("dimension") for row in rows}
        notes = " ".join(row.get("notes") or "" for row in rows).lower()
        for dimension in DIMENSIONS:
            if dimension not in present and f"{NOT_APPLICABLE_PREFIX}{dimension}" not in notes:
                findings.append(_finding(
                    "error", "criteria.dimension_missing",
                    f"no '{dimension}' criterion — add one or write "
                    f"'{NOT_APPLICABLE_PREFIX}{dimension} <reason>' in a criterion note",
                ))
        if not any(row.get("tier") == "redline" for row in rows):
            findings.append(_finding("error", "criteria.no_redline",
                                     "a criteria set needs at least one red line"))
        if not rows:
            findings.append(_finding("error", "criteria.empty", "the set has no criteria"))
    return findings


def summary(rows: list[dict[str, Any]], calibrated_keys: set[str]) -> dict[str, Any]:
    """Tier distribution, judge share, effective gates and the compound pass rate."""
    tiers = {tier: 0 for tier in TIERS}
    judges = 0
    effective_gates = 0
    thresholds: list[float] = []
    for row in rows:
        tiers[row.get("tier")] = tiers.get(row.get("tier"), 0) + 1
        judge = is_judge(row)
        judges += 1 if judge else 0
        tier = effective_tier(row, calibrated=row.get("key") in calibrated_keys)
        if tier == "gate":
            effective_gates += 1
            metric = (row.get("executor") or {}).get("kind") == "metric"
            if not metric and isinstance(row.get("threshold"), (int, float)):
                thresholds.append(float(row["threshold"]))
    share = judges / len(rows) if rows else 0.0
    if share < 0.2:
        cadence = "full_every_commit"
    elif share < 0.5:
        cadence = "split_quick_full"
    else:
        cadence = "split_criteria"
    return {
        "count": len(rows),
        "tiers": tiers,
        "judge_count": judges,
        "judge_share": round(share, 4),
        "run_cadence": cadence,
        "declared_gates": tiers.get("gate", 0),
        "effective_gates": effective_gates,
        "compound_gate_rate": round(stats.compound_pass_rate(thresholds), 4)
        if thresholds else None,
    }


# ── writing ────────────────────────────────────────────────────────────────────


def _template_rows(db: Session, workspace_id: str, row: CriteriaSet) -> list[Criterion]:
    if row.kind != "agent" or not row.template_id or row.template_version is None:
        return []
    template = get_version(db, workspace_id, row.template_id, row.template_version)
    if template.status not in ("published", "superseded"):
        raise AppError("criteria.template_not_published",
                       "an agent set can only inherit a published template version",
                       status_code=409)
    return criteria_of(db, template.id)


def _content(row: Criterion | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, Criterion):
        return {f: copy.deepcopy(getattr(row, f)) for f in _CONTENT_FIELDS}
    return {f: copy.deepcopy(row.get(f)) for f in _CONTENT_FIELDS}


def _write_rows(
    db: Session,
    row: CriteriaSet,
    rows: list[dict[str, Any]],
    template_rows: list[Criterion],
) -> None:
    template = {t.key: _content(t) for t in template_rows}
    for existing in criteria_of(db, row.id):
        db.delete(existing)
    db.flush()
    columns = {c.key for c in Criterion.__table__.columns}
    for position, data in enumerate(rows):
        base = template.get(data["key"])
        if base is None:
            origin = "added" if row.kind == "agent" and template_rows else "own"
        elif _content(data) == base:
            origin = "template"
        else:
            origin = "override"
        db.add(
            Criterion(
                workspace_id=row.workspace_id,
                set_id=row.id,
                position=position,
                origin=origin,
                **{k: v for k, v in data.items() if k in columns},
            )
        )
    db.flush()


def create_set(
    db: Session,
    workspace_id: str,
    *,
    kind: str,
    name: str,
    actor: str,
    agent_id: str | None = None,
    description: str = "",
    scenario: str = "",
    template_id: str | None = None,
    template_version: int | None = None,
    source: str = "manual",
    rows: list[dict[str, Any]] | None = None,
    actor_user_id: str | None = None,
) -> CriteriaSet:
    """A new lineage at draft v1; inheriting a template copies its criteria in."""
    if kind not in ("template", "agent"):
        raise AppError("criteria.bad_kind", "kind must be template or agent")
    if kind == "agent" and not agent_id:
        raise AppError("criteria.agent_required", "an agent criteria set names its agent",
                       next=[next_step("agent.create")])
    if kind == "template" and (agent_id or template_id):
        raise AppError("criteria.bad_template", "a template belongs to no agent or template")
    if kind == "agent":
        existing = db.scalar(
            select(func.count()).select_from(CriteriaSet).where(
                CriteriaSet.workspace_id == workspace_id,
                CriteriaSet.agent_id == agent_id,
                CriteriaSet.kind == "agent",
            )
        )
        if existing:
            raise AppError("criteria.agent_has_set",
                           "this agent already has a criteria set — create a new version",
                           status_code=409,
                           next=[next_step("criteria.open", agent_id=agent_id)])
    row = CriteriaSet(
        workspace_id=workspace_id,
        lineage_id=uuid.uuid4().hex[:16],
        kind=kind,
        agent_id=agent_id,
        template_id=template_id,
        template_version=template_version,
        name=name.strip()[:96] or "criteria",
        description=description,
        scenario=scenario[:96],
        version=1,
        status="draft",
        source=source,
        created_by=actor,
        updated_by=actor,
        created_by_user_id=user_id_for(actor, actor_user_id),
        updated_by_user_id=user_id_for(actor, actor_user_id),
    )
    db.add(row)
    db.flush()
    template_rows = _template_rows(db, workspace_id, row)
    initial = rows
    if initial is None and template_rows:
        initial = [{"key": t.key, **_content(t), "owner": t.owner, "examples": t.examples,
                    "notes": t.notes} for t in template_rows]
    _write_rows(db, row, initial or [], template_rows)
    return row


def save_draft(
    db: Session,
    row: CriteriaSet,
    rows: list[dict[str, Any]],
    *,
    actor: str,
    kind_of: EvaluatorKindOf,
    removals: list[dict[str, Any]] | None = None,
    name: str | None = None,
    description: str | None = None,
    scenario: str | None = None,
    actor_user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Replace a draft's criteria; returns the validation findings (warnings allowed)."""
    if row.status != "draft":
        raise AppError("criteria.not_draft",
                       "a published version is frozen — create a new version to edit",
                       status_code=409)
    normalized = [normalize_criterion(r, kind_of) for r in rows]
    template_rows = _template_rows(db, row.workspace_id, row)
    template_dicts = [{"key": t.key, **_content(t)} for t in template_rows]
    removals = removals if removals is not None else list(row.removals or [])
    findings = validate(normalized, removals=removals, template_rows=template_dicts)
    errors = [f for f in findings if f["level"] == "error"]
    if errors:
        raise AppError("criteria.invalid", "the criteria table has errors",
                       {"findings": findings}, status_code=422)
    _write_rows(db, row, normalized, template_rows)
    row.removals = removals
    if name is not None:
        row.name = name.strip()[:96] or row.name
    if description is not None:
        row.description = description
    if scenario is not None:
        row.scenario = scenario[:96]
    row.updated_by = actor
    row.updated_by_user_id = user_id_for(actor, actor_user_id)
    row.updated_at = _now()
    db.flush()
    return findings


def _rows_as_dicts(rows: list[Criterion]) -> list[dict[str, Any]]:
    return [{"key": r.key, **_content(r), "owner": r.owner, "examples": r.examples,
             "notes": r.notes} for r in rows]


def publish(db: Session, row: CriteriaSet, *, actor: str,
            actor_user_id: str | None = None) -> CriteriaSet:
    if row.status != "draft":
        raise AppError("criteria.not_draft", "only a draft can be published", status_code=409)
    rows = _rows_as_dicts(criteria_of(db, row.id))
    template_rows = _template_rows(db, row.workspace_id, row)
    findings = validate(
        rows,
        removals=row.removals or [],
        template_rows=[{"key": t.key, **_content(t)} for t in template_rows],
        for_publish=row.kind == "agent",
    )
    errors = [f for f in findings if f["level"] == "error"]
    if errors:
        raise AppError("criteria.invalid", "the criteria set cannot be published",
                       {"findings": findings}, status_code=422)
    for older in db.scalars(
        select(CriteriaSet).where(
            CriteriaSet.lineage_id == row.lineage_id, CriteriaSet.status == "published"
        )
    ).all():
        older.status = "superseded"
    row.status = "published"
    row.published_at = _now()
    row.updated_by = actor
    row.updated_by_user_id = user_id_for(actor, actor_user_id)
    db.flush()
    return row


def sign(db: Session, row: CriteriaSet, *, actor: str, note: str, is_admin: bool,
         actor_user_id: str | None = None) -> CriteriaSet:
    """The business owner's signature on a published version (signer ≠ last editor).

    "The same person" is by account id when both sides carry one (design §3.6), else
    by username."""
    if row.status not in ("published", "superseded"):
        raise AppError("criteria.sign_unpublished", "publish the version before signing it",
                       status_code=409,
                       next=[next_step("criteria.publish", agent_id=row.agent_id,
                                       lineage_id=row.lineage_id)])
    if row.signed_by:
        raise AppError("criteria.already_signed", "this version is already signed",
                       status_code=409)
    actor_user_id = user_id_for(actor, actor_user_id)
    if not is_admin and (
        same_actor(row.updated_by, row.updated_by_user_id, actor, actor_user_id)
        or same_actor(row.created_by, row.created_by_user_id, actor, actor_user_id)
    ):
        raise AppError("criteria.self_sign",
                       "the person who edited the criteria cannot also sign them",
                       status_code=409, needs="perm:criteria.sign")
    row.signed_by = actor
    row.signed_by_user_id = actor_user_id
    row.signed_at = _now()
    row.sign_note = note[:2000]
    db.flush()
    return row


def new_version(db: Session, workspace_id: str, lineage_id: str, *, actor: str,
                actor_user_id: str | None = None) -> CriteriaSet:
    """A draft vN+1 copied from the newest version (refused while a draft exists)."""
    newest = get_version(db, workspace_id, lineage_id)
    if newest.status == "draft":
        raise AppError("criteria.draft_exists", "a draft of this set already exists",
                       status_code=409,
                       next=[next_step("criteria.open_draft", agent_id=newest.agent_id,
                                       lineage_id=lineage_id)])
    draft = CriteriaSet(
        workspace_id=workspace_id,
        lineage_id=lineage_id,
        kind=newest.kind,
        agent_id=newest.agent_id,
        template_id=newest.template_id,
        template_version=newest.template_version,
        name=newest.name,
        description=newest.description,
        scenario=newest.scenario,
        version=newest.version + 1,
        status="draft",
        parent_version=newest.version,
        source=newest.source,
        removals=copy.deepcopy(newest.removals or []),
        created_by=actor,
        updated_by=actor,
        created_by_user_id=user_id_for(actor, actor_user_id),
        updated_by_user_id=user_id_for(actor, actor_user_id),
    )
    db.add(draft)
    db.flush()
    _write_rows(db, draft, _rows_as_dicts(criteria_of(db, newest.id)),
                _template_rows(db, workspace_id, draft))
    return draft


def adopt_template(
    db: Session, workspace_id: str, lineage_id: str, *, template_version: int, actor: str,
    actor_user_id: str | None = None,
) -> CriteriaSet:
    """A new agent-set draft on a newer template version, keeping overrides and additions."""
    newest = get_version(db, workspace_id, lineage_id)
    if newest.kind != "agent" or not newest.template_id:
        raise AppError("criteria.no_template", "this set does not inherit a template")
    if newest.status == "draft":
        raise AppError("criteria.draft_exists", "finish or discard the current draft first",
                       status_code=409,
                       next=[next_step("criteria.open_draft", agent_id=newest.agent_id,
                                       lineage_id=lineage_id)])
    target = get_version(db, workspace_id, newest.template_id, template_version)
    if target.status != "published" and target.status != "superseded":
        raise AppError("criteria.template_not_published",
                       "adopt a published template version", status_code=409)
    own = criteria_of(db, newest.id)
    kept = {r.key: r for r in own if r.origin in ("override", "added")}
    removed = {r.get("key") for r in newest.removals or []}
    rows: list[dict[str, Any]] = []
    for t in criteria_of(db, target.id):
        if t.key in removed:
            continue
        source = kept.pop(t.key, None) or t
        rows.append({"key": source.key, **_content(source), "owner": source.owner,
                     "examples": source.examples, "notes": source.notes})
    for extra in kept.values():
        rows.append({"key": extra.key, **_content(extra), "owner": extra.owner,
                     "examples": extra.examples, "notes": extra.notes})
    draft = new_version(db, workspace_id, lineage_id, actor=actor, actor_user_id=actor_user_id)
    draft.template_version = template_version
    draft.signed_by = None
    draft.signed_by_user_id = None
    db.flush()
    _write_rows(db, draft, rows, criteria_of(db, target.id))
    return draft


def discard_draft(db: Session, row: CriteriaSet) -> None:
    if row.status != "draft":
        raise AppError("criteria.not_draft", "only a draft can be discarded", status_code=409)
    for c in criteria_of(db, row.id):
        db.delete(c)
    db.delete(row)
    db.flush()


def diff(a: list[Criterion], b: list[Criterion]) -> list[dict[str, Any]]:
    """Per-key changes from version a to version b."""
    left = {r.key: _content(r) for r in a}
    right = {r.key: _content(r) for r in b}
    out: list[dict[str, Any]] = []
    for key in sorted(set(left) | set(right)):
        if key not in left:
            out.append({"key": key, "change": "added", "after": right[key]})
        elif key not in right:
            out.append({"key": key, "change": "removed", "before": left[key]})
        else:
            fields = [f for f in _CONTENT_FIELDS if left[key].get(f) != right[key].get(f)]
            if fields:
                out.append({
                    "key": key, "change": "changed", "fields": fields,
                    "before": {f: left[key].get(f) for f in fields},
                    "after": {f: right[key].get(f) for f in fields},
                })
    return out


def newer_template_version(db: Session, row: CriteriaSet) -> int | None:
    """The newest published template version above the one this set inherits."""
    if row.kind != "agent" or not row.template_id:
        return None
    newest = latest_published(db, row.workspace_id, row.template_id)
    if newest and row.template_version is not None and newest.version > row.template_version:
        return newest.version
    return None


# ── import from the architect evaluation plan ──────────────────────────────────


def rows_from_evaluation_plan(content: dict[str, Any]) -> list[dict[str, Any]]:
    """Map an architect evaluation plan's evaluator entries onto draft criteria.

    `blocking` evaluators become gates (or red lines when they are deterministic code
    checks guarding forbidden behaviour), informational ones are observed; the plan's
    `threshold` carries over. Dimensions come from the evaluator's nature: trajectory
    and tool checks are cognition, judges default to quality — the editor flags what
    the business owner still has to confirm.
    """
    rows: list[dict[str, Any]] = []
    for index, entry in enumerate(content.get("evaluators") or [], start=1):
        kind = entry.get("kind")
        evaluator_id = entry.get("evaluator_id") or entry.get("created_evaluator_id") or ""
        title = str(entry.get("title") or entry.get("key") or f"criterion {index}")
        blocking = bool(entry.get("blocking"))
        if kind == "code":
            checks = ((entry.get("rules") or {}).get("checks") or [])
            negative = any(c.get("type") in ("output_not_contains",) for c in checks)
            dimension = "responsibility" if negative else "cognition"
            tier = "redline" if (blocking and negative) else ("gate" if blocking else "observe")
            expected = "redline" if tier == "redline" else "deterministic"
        elif evaluator_id.startswith("Builtin.Trajectory"):
            dimension, tier, expected = "cognition", ("gate" if blocking else "observe"), (
                "trajectory"
            )
        else:
            dimension = "quality"
            tier = "gate" if blocking else "observe"
            expected = "soft"
        threshold = entry.get("threshold")
        if tier == "gate" and not isinstance(threshold, (int, float)):
            threshold = 0.9
        rows.append({
            "key": f"C-{index:03d}",
            "text": title,
            "dimension": dimension,
            "tier": tier,
            "threshold": threshold if tier == "gate" else None,
            "level": str(entry.get("level") or "session").lower(),
            "executor": {"kind": "evaluator", "evaluator_id": evaluator_id,
                         "plan_key": entry.get("key")},
            "denominator": "sessions",
            "expected_type": expected,
            "notes": str(entry.get("note") or ""),
            "examples": [],
        })
    return rows
