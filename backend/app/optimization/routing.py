"""Canary routing — the backend is the routing authority (N12 E3-1; plan §B.7,
§B.8, lead ruling CA-2/CA-4).

What the Global build delegated to the AgentCore A/B service (an A/B test on an
experiment Gateway, variant weights, ``GetABTest`` statistics) now lives here:

* the A/B definition is the owner row's ``artifacts.abtest``
  ``{ab_test_id: "local_<id>", variants: [...], execution_status: RUNNING|PAUSED|STOPPED}``
  — endpoint variants (``variantConfiguration.target.name``) for Runtime canaries. The
  configuration-bundle experiment (bundle variants) retired with its routes (N13-obs2):
  its legacy ``experiments`` rows never route traffic again, whatever their A/B row says;
  the ``launchpad.bundle`` baggage pointer stays for the optimizer's rollouts
  (``strands_eval/jobs_optimize.py``);
* ``experiment_assignments`` is the sticky session → arm table, written **before** a
  replay job is dispatched (``assign_batch``) and by the live Chat / ``/v1`` chain
  (``route_for``); the worker only executes what it is told and
  ``reconcile`` drops the sessions a job never reached;
* ``ab_results`` builds the ``GetABTest``-shaped statistics from ``eval_results`` ×
  assignments with a local Welch test, so the baseline ``normalize_ab_results`` /
  ``compute_verdict`` read it unchanged.

A canary whose setup is still partial (stable endpoint provisioned, no A/B row) routes
every session to the stable endpoint: ``UpdateAgentRuntime`` auto-rolls DEFAULT to the
untested candidate, so this is what keeps production on ``v_current`` (the baseline's
"provisioning form" of ``active_canary_route``).
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, TypedDict
from urllib.parse import quote, unquote

from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.evaluation.models import EvalResult
from app.optimization import stats
from app.optimization.models import ExperimentAssignment, RuntimeCanary

ARM_CONTROL = "C"
ARM_TREATMENT = "T1"
ARMS: tuple[str, ...] = (ARM_CONTROL, ARM_TREATMENT)
RUNNING, PAUSED, STOPPED = "RUNNING", "PAUSED", "STOPPED"
SIGNIFICANCE_ALPHA = 0.05

# W3C ``baggage`` keys (CA-2). The two ``aws.agentcore.gateway.*`` keys are the SDK's own
# experiment keys — ``BaggageSpanProcessor`` stamps them on every span of the session
# — so per-arm span attribution comes for free when the data plane passes baggage
# through (R-1, N9 probe). ``launchpad.bundle`` is the config-bundle pointer.
BAGGAGE_BUNDLE_KEY = "launchpad.bundle"
BAGGAGE_EXPERIMENT_KEY = "aws.agentcore.gateway.routing_experiment_arn"
BAGGAGE_VARIANT_KEY = "aws.agentcore.gateway.routing_experiment_variant_name"

_SQL_IN_CHUNK = 500


# ─── evaluator polarity + pure variant builders (relocated from the Global wrappers) ─
# Builtin.Refusal scores the share of turns the agent refused, so the lower-mean arm
# is the better arm; DeepEval Bias and Toxicity score the share of biased / toxic
# opinions. Builtin.Harmfulness / Stereotyping are NOT penalties: their scale is
# "Not Harmful" = 1.0, so they stay higher-is-better like every other evaluator.
LOWER_IS_BETTER_EVALUATORS = frozenset(
    {"Builtin.Refusal", "ThirdParty.DeepEval.Bias", "ThirdParty.DeepEval.Toxicity"}
)


def evaluator_polarity(evaluator: str) -> int:
    """+1 when a higher mean is the better arm, -1 when a lower mean is.

    Accepts a bare id (``Builtin.Refusal``) or an evaluator ARN. Custom judges are +1:
    the launchpad's default scale is pass=1.0 / fail=0.0.
    """
    return -1 if (evaluator or "").rsplit("/", 1)[-1] in LOWER_IS_BETTER_EVALUATORS else 1


def config_bundle_variants(
    control_arn: str,
    control_version: str,
    treatment_arn: str,
    treatment_version: str,
    control_weight: int = 50,
    treatment_weight: int = 50,
    *,
    control_ref: str | None = None,
    treatment_ref: str | None = None,
) -> list[dict[str, Any]]:
    """The two config-bundle variants (baseline shape; ``bundleRef`` = ``<id>:<v>`` is the
    CN pointer the invoke chain puts into the request baggage)."""
    def variant(name: str, weight: int, arn: str, version: str, ref: str | None) -> dict:
        bundle: dict[str, Any] = {"bundleArn": arn, "bundleVersion": str(version)}
        if ref:
            bundle["bundleRef"] = ref
        return {"name": name, "weight": weight,
                "variantConfiguration": {"configurationBundle": bundle}}

    return [
        variant(ARM_CONTROL, control_weight, control_arn, control_version, control_ref),
        variant(ARM_TREATMENT, treatment_weight, treatment_arn, treatment_version, treatment_ref),
    ]


def target_variants(
    target_v1: str,
    target_v2: str,
    control_weight: int = 90,
    treatment_weight: int = 10,
) -> list[dict[str, Any]]:
    """The two endpoint variants of a Runtime canary (``target.name`` = endpoint name)."""
    return [
        {"name": ARM_CONTROL, "weight": control_weight,
         "variantConfiguration": {"target": {"name": target_v1}}},
        {"name": ARM_TREATMENT, "weight": treatment_weight,
         "variantConfiguration": {"target": {"name": target_v2}}},
    ]


def weights_of(variants: Sequence[dict[str, Any]]) -> dict[str, int]:
    return {str(v.get("name")): int(v.get("weight") or 0) for v in variants}


def reweight(
    variants: Sequence[dict[str, Any]], *, control_weight: int, treatment_weight: int,
) -> list[dict[str, Any]]:
    """A copy of ``variants`` at new weights (the A/B service's UpdateABTest)."""
    out: list[dict[str, Any]] = []
    for v in variants:
        weight = control_weight if v.get("name") == ARM_CONTROL else treatment_weight
        out.append({**v, "weight": weight})
    return out


def normalize_ab_results(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Map a ``GetABTest``-shaped result into the frontend ABComparisonChart shape:
    ``[{evaluatorId, label, polarity, control:{name,mean,sampleSize}, variants:[…]}]``.

    ``polarity`` travels with the metric so every consumer (verdict maths, the console,
    a stored artifact read back later) reads the same direction.
    """
    metrics_out: list[dict[str, Any]] = []
    results = result.get("results", {}) or {}
    for m in results.get("evaluatorMetrics", []):
        arn = m.get("evaluatorArn", "")
        label = arn.split("/")[-1] if arn else m.get("evaluatorId", "evaluator")
        cs = m.get("controlStats", {}) or {}
        variants = [
            {
                "name": vr.get("name", ARM_TREATMENT),
                "mean": vr.get("mean"),
                "sampleSize": vr.get("sampleSize"),
                "pValue": vr.get("pValue"),
                "percentChange": vr.get("percentChange"),
                "isSignificant": vr.get("isSignificant"),
            }
            for vr in m.get("variantResults", [])
        ]
        metrics_out.append({
            "evaluatorId": arn or label,
            "label": label,
            "polarity": evaluator_polarity(arn or label),
            "control": {
                "name": cs.get("name", ARM_CONTROL),
                "mean": cs.get("mean"),
                "sampleSize": cs.get("sampleSize"),
            },
            "variants": variants,
            # P3.2: the judges behind the metric; a mixed one never decides a verdict
            "judges": list(m.get("judges") or []),
            "judgeMismatch": bool(m.get("judgeMismatch")),
        })
    return metrics_out


# ─── owners, arms, routes ───────────────────────────────────────────────────────
@dataclass(frozen=True)
class ArmConfig:
    name: str
    weight: int
    qualifier: str | None  # canary arm → named endpoint
    bundle_ref: str | None  # config-bundle pointer ``<id>:<v>`` (optimizer rollouts)


@dataclass(frozen=True)
class OwnerRef:
    """The one canary that currently routes an agent's traffic."""

    kind: str  # canary (``experiment`` only on legacy assignment rows)
    id: str
    workspace_id: str | None
    agent_id: str
    arms: tuple[ArmConfig, ...]
    execution_status: str  # RUNNING | PAUSED | STOPPED
    ab_test_id: str | None = None
    resource_id: str | None = None

    def arm(self, name: str) -> ArmConfig:
        for arm in self.arms:
            if arm.name == name:
                return arm
        raise KeyError(name)

    @property
    def weights(self) -> dict[str, int]:
        return {arm.name: arm.weight for arm in self.arms}


@dataclass(frozen=True)
class Route:
    owner_kind: str
    owner_id: str
    arm: str
    qualifier: str | None
    bundle_ref: str | None
    baggage: str

    @property
    def launchpad_meta(self) -> dict[str, Any]:
        """``payload["launchpad"]`` — the guaranteed pointer channel (CA-2)."""
        meta: dict[str, Any] = {"experiment": self.owner_id, "variant": self.arm}
        if self.bundle_ref:
            meta = {"bundle": self.bundle_ref, **meta}
        return meta


class Assignment(TypedDict):
    arm: str
    qualifier: str | None
    baggage: str
    launchpad_meta: dict[str, Any]


def baggage_for(bundle_ref: str | None, owner_id: str, arm: str) -> str:
    """W3C ``baggage``: comma-separated ``key=value`` pairs, values percent-encoded."""
    pairs: list[tuple[str, str]] = []
    if bundle_ref:
        pairs.append((BAGGAGE_BUNDLE_KEY, bundle_ref))
    pairs.append((BAGGAGE_EXPERIMENT_KEY, owner_id))
    pairs.append((BAGGAGE_VARIANT_KEY, arm))
    return ",".join(f"{key}={quote(value, safe='')}" for key, value in pairs)


def bundle_ref_from_baggage(baggage: str) -> str | None:
    for member in (baggage or "").split(","):
        key, sep, value = member.strip().partition("=")
        if sep and key.strip() == BAGGAGE_BUNDLE_KEY:
            return unquote(value.strip().split(";", 1)[0]) or None
    return None


def hash_unit(owner_id: str, session_id: str) -> float:
    """Sticky, uniform ``[0, 1)`` from ``sha256(owner:session)`` (first 32 bits)."""
    digest = hashlib.sha256(f"{owner_id}:{session_id}".encode()).hexdigest()
    return int(digest[:8], 16) / 2**32


def choose_arm(owner: OwnerRef, session_id: str) -> str:
    """Hash × weights; anything but RUNNING sends new sessions to control."""
    if owner.execution_status != RUNNING:
        return ARM_CONTROL
    treatment = owner.weights.get(ARM_TREATMENT, 0)
    total = sum(owner.weights.values()) or 100
    return ARM_TREATMENT if hash_unit(owner.id, session_id) < treatment / total else ARM_CONTROL


def _route(owner: OwnerRef, arm_name: str) -> Route:
    arm = owner.arm(arm_name)
    return Route(
        owner_kind=owner.kind, owner_id=owner.id, arm=arm.name,
        qualifier=arm.qualifier, bundle_ref=arm.bundle_ref,
        baggage=baggage_for(arm.bundle_ref, owner.id, arm.name),
    )


def _assignment(route: Route) -> Assignment:
    return {"arm": route.arm, "qualifier": route.qualifier, "baggage": route.baggage,
            "launchpad_meta": route.launchpad_meta}


# ─── owner projection from the ledger rows ──────────────────────────────────────
def owner_from_canary(row: RuntimeCanary) -> OwnerRef | None:
    if row.status != "running":
        return None
    artifacts = row.artifacts or {}
    setup = artifacts.get("setup") or {}
    stable = setup.get("stable_endpoint")
    if not stable:
        return None  # nothing provisioned yet — DEFAULT is still v_current
    abtest = artifacts.get("abtest") or {}
    variants = abtest.get("variants") or []
    status = str(abtest.get("execution_status") or "")
    meta = artifacts.get("agent_meta") or {}
    resource_id = setup.get("runtime_id") or meta.get("resource_id")
    if variants and status in (RUNNING, PAUSED):
        arms = tuple(
            ArmConfig(name=str(v.get("name")), weight=int(v.get("weight") or 0),
                      qualifier=str(((v.get("variantConfiguration") or {}).get("target") or {})
                                    .get("name") or stable),
                      bundle_ref=None)
            for v in variants
        )
        return OwnerRef(kind="canary", id=row.id, workspace_id=row.workspace_id,
                        agent_id=row.champion_agent_id, arms=arms, execution_status=status,
                        ab_test_id=abtest.get("ab_test_id"), resource_id=resource_id)
    if status == STOPPED:
        # complete / rollback stopped the split; the action's own ledger write (version
        # flip / re-mint) decides what DEFAULT serves — nothing left to route here.
        return None
    # provisioning form: everything → stable endpoint (v_current)
    arms = (ArmConfig(ARM_CONTROL, 100, str(stable), None),
            ArmConfig(ARM_TREATMENT, 0, str(setup.get("treatment_endpoint") or stable), None))
    return OwnerRef(kind="canary", id=row.id, workspace_id=row.workspace_id,
                    agent_id=row.champion_agent_id, arms=arms, execution_status=PAUSED,
                    ab_test_id=None, resource_id=resource_id)


def active_owner(db: Session, agent_id: str) -> OwnerRef | None:
    """The one owner routing ``agent_id`` — the newest running canary with something
    provisioned (its stable-endpoint safety must hold while a candidate is minted)."""
    canaries = (
        db.query(RuntimeCanary)
        .filter(RuntimeCanary.champion_agent_id == agent_id, RuntimeCanary.status == "running")
        .order_by(RuntimeCanary.created_at.desc())
        .all()
    )
    for row in canaries:
        owner = owner_from_canary(row)
        if owner is not None:
            return owner
    return None


def owner_for_row(row: RuntimeCanary) -> OwnerRef | None:
    return owner_from_canary(row)


# ─── assignments ─────────────────────────────────────────────────────────────────
def _with_db(db: Session | None) -> tuple[Session, bool]:
    """``(session, owned)`` — open a short-lived session when the caller has none."""
    return (db, False) if db is not None else (SessionLocal(), True)


def route_for(db: Session, agent_id: str, session_id: str) -> Route | None:
    """Sticky routing for one live session; writes the assignment row on first sight."""
    owner = active_owner(db, agent_id)
    if owner is None:
        return None
    existing = (
        db.query(ExperimentAssignment)
        .filter(ExperimentAssignment.owner_id == owner.id,
                ExperimentAssignment.session_id == session_id)
        .first()
    )
    if existing is not None:
        return _route(owner, existing.arm)
    arm = choose_arm(owner, session_id)
    db.add(ExperimentAssignment(workspace_id=owner.workspace_id, owner_kind=owner.kind,
                                owner_id=owner.id, session_id=session_id, arm=arm))
    db.commit()
    return _route(owner, arm)


def assign_batch(
    owner: OwnerRef, session_ids: Iterable[str], db: Session | None = None,
) -> dict[str, Assignment]:
    """Pre-assign every replay session **before** the job is dispatched; existing rows
    keep their arm (idempotent re-plan)."""
    ids = list(dict.fromkeys(session_ids))
    session, owned = _with_db(db)
    try:
        existing: dict[str, str] = {}
        for start in range(0, len(ids), _SQL_IN_CHUNK):
            chunk = ids[start:start + _SQL_IN_CHUNK]
            for row in session.query(ExperimentAssignment).filter(
                ExperimentAssignment.owner_id == owner.id,
                ExperimentAssignment.session_id.in_(chunk),
            ):
                existing[row.session_id] = row.arm
        out: dict[str, Assignment] = {}
        for sid in ids:
            arm = existing.get(sid)
            if arm is None:
                arm = choose_arm(owner, sid)
                session.add(ExperimentAssignment(
                    workspace_id=owner.workspace_id, owner_kind=owner.kind,
                    owner_id=owner.id, session_id=sid, arm=arm))
            out[sid] = _assignment(_route(owner, arm))
        session.commit()
        return out
    finally:
        if owned:
            session.close()


def reconcile(
    owner: OwnerRef,
    invocations: Iterable[dict[str, Any]],
    *,
    planned: Iterable[str] | None = None,
    db: Session | None = None,
) -> list[str]:
    """Drop the assignment rows of ``planned`` sessions the worker never invoked
    (``invocations.jsonl`` is what actually happened). Without a ``planned`` set there
    is nothing safe to drop: live Chat assignments are never part of a job."""
    if planned is None:
        return []
    invoked = {str(rec.get("session_id")) for rec in invocations if rec.get("session_id")}
    dropped = [sid for sid in planned if sid not in invoked]
    if not dropped:
        return []
    session, owned = _with_db(db)
    try:
        for start in range(0, len(dropped), _SQL_IN_CHUNK):
            chunk = dropped[start:start + _SQL_IN_CHUNK]
            session.query(ExperimentAssignment).filter(
                ExperimentAssignment.owner_id == owner.id,
                ExperimentAssignment.session_id.in_(chunk),
            ).delete(synchronize_session=False)
        session.commit()
        return dropped
    finally:
        if owned:
            session.close()


def delete_assignments(owner_id: str, db: Session | None = None) -> int:
    session, owned = _with_db(db)
    try:
        count = session.query(ExperimentAssignment).filter(
            ExperimentAssignment.owner_id == owner_id).delete(synchronize_session=False)
        session.commit()
        return int(count)
    finally:
        if owned:
            session.close()


def assigned_sessions(db: Session, owner_id: str) -> dict[str, str]:
    return {
        row.session_id: row.arm
        for row in db.query(ExperimentAssignment).filter(ExperimentAssignment.owner_id == owner_id)
    }


# ─── GetABTest-shaped statistics ─────────────────────────────────────────────────
def ab_results(db: Session, owner: OwnerRef,
               sessions: set[str] | None = None) -> dict[str, Any]:
    """``{abTestId, executionStatus, results:{evaluatorMetrics:[{evaluatorId,
    controlStats{name,mean,sampleSize}, variantResults[{name,mean,sampleSize,pValue,
    percentChange,isSignificant}]}]}}`` from ``eval_results`` × assignments.

    Every assigned session scored by an evaluator is ONE observation (N17-obs387): the
    replay job (source ``run``) and the arm's online config (``online``) both score a
    replayed session, and per-turn TRACE / per-call TOOL_CALL rows are several rows of one
    session — counting rows doubled ``sampleSize`` (the 10-per-arm bar met with 5 sessions)
    and fed Welch pseudo-replicated observations. Rows are first deduped per
    ``(session, evaluator, unit)`` (unit = trace / span), the replay job's preferred over
    other sources; the session's observation is the mean of its units, so ``sampleSize`` is
    the number of sessions.
    """
    arms = assigned_sessions(db, owner.id)
    if sessions is not None:
        # R3-c: the same statistics over a subset (the held-out scenarios' sessions)
        arms = {sid: arm for sid, arm in arms.items() if sid in sessions}
    scores: dict[str, dict[str, list[float]]] = {}
    # P3.2: the judges behind each evaluator's rows (an unstamped legacy row is unknown,
    # not another judge) — a metric scored by more than one is flagged, never compared
    judges: dict[str, set[str]] = {}
    if arms:
        sids = list(arms)
        units: dict[tuple[str, str], dict[tuple[Any, Any], tuple[int, float, Any]]] = {}
        for start in range(0, len(sids), _SQL_IN_CHUNK):
            chunk = sids[start:start + _SQL_IN_CHUNK]
            rows = (
                db.query(EvalResult.session_id, EvalResult.evaluator_id, EvalResult.score,
                         EvalResult.source, EvalResult.trace_id, EvalResult.span_id,
                         EvalResult.judge_model_id)
                .filter(EvalResult.session_id.in_(chunk), EvalResult.score.isnot(None))
                .all()
            )
            for session_id, evaluator_id, score, source, trace_id, span_id, judge in rows:
                if arms.get(session_id) is None:
                    continue
                rank = 0 if source == "run" else 1  # the replay job's rows win
                per_unit = units.setdefault((session_id, evaluator_id), {})
                known = per_unit.get((trace_id, span_id))
                if known is None or rank < known[0]:
                    per_unit[(trace_id, span_id)] = (rank, float(score), judge)
        for (session_id, evaluator_id), per_unit in units.items():
            best = min(entry[0] for entry in per_unit.values())
            kept = [entry for entry in per_unit.values() if entry[0] == best]
            values = [entry[1] for entry in kept]
            bucket = scores.setdefault(evaluator_id, {a: [] for a in ARMS})
            bucket.setdefault(arms[session_id], []).append(math.fsum(values) / len(values))
            # P3.2: the judges behind the observations that count
            for _rank, _score, judge in kept:
                if judge:
                    judges.setdefault(evaluator_id, set()).add(str(judge))
    metrics: list[dict[str, Any]] = []
    for evaluator_id in sorted(scores):
        control = scores[evaluator_id].get(ARM_CONTROL, [])
        treatment = scores[evaluator_id].get(ARM_TREATMENT, [])
        c_mean, t_mean = stats.mean(control), stats.mean(treatment)
        test = stats.welch_two_sided(control, treatment)
        p_value = test[2] if test is not None else None
        percent = (
            (t_mean - c_mean) / abs(c_mean) * 100.0
            if c_mean not in (None, 0.0) and t_mean is not None else None
        )
        metrics.append({
            "evaluatorId": evaluator_id,
            "controlStats": {"name": ARM_CONTROL, "mean": c_mean, "sampleSize": len(control)},
            "variantResults": [{
                "name": ARM_TREATMENT,
                "mean": t_mean,
                "sampleSize": len(treatment),
                "pValue": p_value,
                "percentChange": percent,
                "isSignificant": bool(p_value is not None and p_value < SIGNIFICANCE_ALPHA),
            }],
            # CN additive (P3.2)
            "judges": sorted(judges.get(evaluator_id, set())),
            "judgeMismatch": len(judges.get(evaluator_id, set())) > 1,
        })
    return {
        "abTestId": owner.ab_test_id or f"local_{owner.id}",
        "executionStatus": owner.execution_status,
        "results": {"evaluatorMetrics": metrics},
    }
