"""Materialization of a reviewed evaluation-assets plan (SE-047).

One approved plan revision → one durable ``EvaluationAssetOperation`` → these owned
resources, created in this order, each from a persisted **intent** (stable client
token / unique name / exact request) so a lost response, a crash between a cloud
success and the ledger write, a restart or a concurrent click resumes the SAME
resources instead of creating new ones or adopting foreign ones by name:

1. ``dataset``        — the local Launchpad Dataset (ledger only; never synced to AWS
                        here; edits made afterwards in the Evaluation console are the
                        member's and are not overwritten by a retry);
2. ``lambda_role``    — a dedicated Lambda execution role with ONLY log rights on its
                        own log group (only when the plan has code evaluators);
3. ``log_group``      — ``/aws/lambda/<function>`` with bounded retention;
4. ``lambda_function``— the reviewed static handler + canonical ``rules.json`` as a
                        deterministic ZIP (sorted names, fixed timestamps), one
                        immutable published version whose ``CodeSha256`` must equal
                        the persisted digest; bounded timeout/memory/reserved
                        concurrency, no provisioned concurrency;
5. ``lambda_permission`` — resource policy for ``bedrock-agentcore.amazonaws.com``
                        scoped by ``SourceAccount`` (no invented SourceArn pattern);
6. ``role_grant``     — optional additive inline policy on the workspace execution
                        role (resolved by ARN + persisted RoleId, trusted ownership
                        required) granting Invoke/GetFunction on the exact owned
                        function version only; existing trust/policies untouched;
7. ``evaluator:<key>``— every judge / derived / code evaluator of the plan, created
                        with a stable ``clientToken`` and read back until ACTIVE with
                        a configuration equal to the request.

Nothing here deploys an agent, starts an evaluation, syncs a dataset to AWS, enables
online evaluation or invokes a model. Status reads are ledger-only. Every AWS client
comes from the workspace funnel (``WorkspaceContext.client``).
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import secrets
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.assistant import evaluation_plan as plan_contract
from app.core.db import SessionLocal
from app.core.errors import AppError, NotFoundError
from app.evaluation.agentcore_eval import ALL_BUILTIN_EVALUATORS
from app.evaluation.models import EvalDataset
from app.models.assistant import (
    AssistantConversation,
    AssistantEvaluationPlan,
    AssistantProposal,
    EvaluationAssetOperation,
)
from app.models.ledger import User, Workspace
from app.models.ledger import _id as _new_id
from app.routers.auth import ROLE_ADMIN
from app.services import users as users_service
from app.services.workspace import WorkspaceContext, workspace_context

logger = logging.getLogger(__name__)

HANDLER_PATH = Path(__file__).resolve().parent / "lambda_runtime" / "handler.py"
LAMBDA_RUNTIME = "python3.12"
LAMBDA_HANDLER = "handler.lambda_handler"
LAMBDA_MEMORY_MB = 256
LAMBDA_RESERVED_CONCURRENCY = 5
LAMBDA_TIMEOUT_CAP_S = 300
LOG_RETENTION_DAYS = 14
PERMISSION_SID = "launchpad-agentcore-evaluations"
AGENTCORE_PRINCIPAL = "bedrock-agentcore.amazonaws.com"
TAG_OPERATION = "launchpad:eval-operation"
TAG_MANAGED = "launchpad:managed"
LEASE_TTL = timedelta(minutes=10)
READBACK_ATTEMPTS = 30
READBACK_DELAY_S = 2.0
MAX_ATTEMPTS = 5  # bounded side-effect retries per operation (explicit, never silent)

CLOUD_STATUSES = ("pending", "accepted", "ready", "failed", "conflict", "skipped",
                  "deleted", "delete_failed")
_CONFLICT_CODES = ("ConflictException", "ResourceConflictException", "AlreadyExistsException",
                   "EntityAlreadyExists", "ResourceAlreadyExistsException")
_NOT_FOUND_CODES = ("ResourceNotFoundException", "NotFoundException", "NoSuchEntity",
                    "NoSuchEntityException")
_LIVE: dict[str, threading.Thread] = {}
_LIVE_LOCK = threading.Lock()

ClientFactory = Callable[[WorkspaceContext, str], Any]


def _default_clients(workspace: WorkspaceContext, service: str) -> Any:
    return workspace.client(service)


def _code(exc: BaseException) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code") or "")
    return type(exc).__name__


def _safe_error(exc: BaseException) -> str:
    return f"{_code(exc)}: {str(exc)[:400]}"


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# deterministic Lambda package
# ---------------------------------------------------------------------------


def canonical_rules(plan: plan_contract.EvaluationPlan) -> dict[str, Any]:
    """``rules.json``: evaluator NAME → declarative rules (frozen mapping; the handler
    refuses names it does not know)."""
    return {
        "version": 1,
        "evaluators": {
            e.name: e.rules.model_dump(exclude_none=True)
            for e in plan.evaluators
            if isinstance(e, plan_contract.CodeEvaluator)
        },
    }


def build_package(rules: dict[str, Any]) -> tuple[bytes, str]:
    """(zip bytes, sha256 hex) — byte-identical for identical rules: fixed entry
    order, fixed timestamp, fixed permissions, canonical JSON."""
    handler = HANDLER_PATH.read_bytes()
    rules_bytes = json.dumps(rules, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in sorted({"handler.py": handler, "rules.json": rules_bytes}.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
    payload = buf.getvalue()
    return payload, hashlib.sha256(payload).hexdigest()


def code_sha256_b64(digest_hex: str) -> str:
    """Lambda reports/expects CodeSha256 as base64 of the raw sha256."""
    return base64.b64encode(bytes.fromhex(digest_hex)).decode("ascii")


# ---------------------------------------------------------------------------
# intents
# ---------------------------------------------------------------------------


def function_name(op_id: str) -> str:
    return f"launchpad-evalfn-{op_id}"


def _token(op_id: str, key: str) -> str:
    # CreateEvaluator clientToken: [A-Za-z0-9-], stable per intent
    return f"lp-evalop-{op_id}-{key}-{secrets.token_hex(8)}".replace("_", "-")


def compose_intents(plan: plan_contract.EvaluationPlan, op_id: str) -> list[dict[str, Any]]:
    fn = function_name(op_id)
    intents: list[dict[str, Any]] = [
        {"kind": "dataset", "key": "dataset", "name": plan.dataset.name, "status": "pending"},
    ]
    code = [e for e in plan.evaluators if isinstance(e, plan_contract.CodeEvaluator)]
    if code:
        rules = canonical_rules(plan)
        _, digest = build_package(rules)
        intents += [
            {"kind": "lambda_role", "key": "lambda_role", "name": fn, "status": "pending"},
            {"kind": "log_group", "key": "log_group", "name": f"/aws/lambda/{fn}",
             "status": "pending"},
            {"kind": "lambda_function", "key": "lambda_function", "name": fn,
             "status": "pending", "digest": digest, "rules": rules,
             "timeout_s": min(max(e.lambda_timeout_s for e in code), LAMBDA_TIMEOUT_CAP_S)},
            {"kind": "lambda_permission", "key": "lambda_permission", "name": PERMISSION_SID,
             "status": "pending"},
            {"kind": "role_grant", "key": "role_grant", "name": f"launchpad-evalop-{op_id}",
             "status": "pending" if plan.grant_workspace_execution_role else "skipped",
             "error": None if plan.grant_workspace_execution_role
             else "not requested by the plan"},
        ]
    for e in plan.evaluators:
        if e.kind in plan_contract.CLOUD_KINDS:
            intents.append({
                "kind": "evaluator", "key": f"evaluator:{e.key}", "plan_key": e.key,
                "name": getattr(e, "name", ""), "definition": e.kind, "status": "pending",
                "client_token": _token(op_id, e.key),
                "reference_dependent": plan_contract.reference_dependent(e),
            })
        elif e.kind == "existing":
            intents.append({
                "kind": "existing", "key": f"existing:{e.key}", "plan_key": e.key,
                "name": e.evaluator_id, "status": "pending",
            })
    return intents


# ---------------------------------------------------------------------------
# projections
# ---------------------------------------------------------------------------


def plan_out(row: AssistantEvaluationPlan, op: EvaluationAssetOperation | None) -> dict[str, Any]:
    return {
        "id": row.id,
        "conversation_id": row.conversation_id,
        "proposal_id": row.proposal_id,
        "source_revision": row.source_revision,
        "source_content_hash": row.source_content_hash,
        "revision": row.revision,
        "source": row.source,
        "status": row.status,
        "content": row.content,
        "content_hash": row.content_hash,
        "validation_errors": row.validation_errors or [],
        "summary": plan_contract.plan_summary(row.content) if not row.validation_errors else None,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "operation_id": op.id if op else None,
    }


_PUBLIC_RESOURCE_KEYS = ("kind", "key", "plan_key", "name", "status", "definition", "error",
                         "digest", "reference_dependent", "attempts", "result", "cleanup")


def operation_out(op: EvaluationAssetOperation) -> dict[str, Any]:
    resources = []
    for r in op.resources or []:
        out = {k: r.get(k) for k in _PUBLIC_RESOURCE_KEYS if k in r}
        if r.get("kind") == "dataset" and r.get("result"):
            out["link"] = f"/evaluation?view=datasets&ds={r['result'].get('dataset_id')}"
        if r.get("kind") in ("evaluator", "existing") and (r.get("result") or {}).get(
                "evaluator_id"):
            out["link"] = f"/evaluation?view=evaluators&ev={r['result']['evaluator_id']}"
        resources.append(out)
    return {
        "id": op.id,
        "conversation_id": op.conversation_id,
        "plan_id": op.plan_id,
        "plan_revision": op.plan_revision,
        "plan_hash": op.plan_hash,
        "proposal_revision": op.proposal_revision,
        "approved_by": op.approved_by,
        "account_id": op.account_id,
        "region": op.region,
        "status": op.status,
        "attempts": op.attempts,
        "max_attempts": MAX_ATTEMPTS,
        "dataset_id": op.dataset_id,
        "error": op.error,
        "resources": resources,
        "created_at": op.created_at.isoformat() if op.created_at else None,
        "updated_at": op.updated_at.isoformat() if op.updated_at else None,
        "running": live_worker(op.id) is not None,
    }


def live_worker(op_id: str) -> threading.Thread | None:
    with _LIVE_LOCK:
        t = _LIVE.get(op_id)
        return t if t is not None and t.is_alive() else None


# ---------------------------------------------------------------------------
# plan revisions (ledger only)
# ---------------------------------------------------------------------------


def plans_of(db: Session, conversation_id: str) -> list[AssistantEvaluationPlan]:
    return (
        db.query(AssistantEvaluationPlan)
        .filter(AssistantEvaluationPlan.conversation_id == conversation_id)
        .order_by(AssistantEvaluationPlan.revision.asc())
        .all()
    )


def operations_of(db: Session, conversation_id: str) -> list[EvaluationAssetOperation]:
    return (
        db.query(EvaluationAssetOperation)
        .filter(EvaluationAssetOperation.conversation_id == conversation_id)
        .order_by(EvaluationAssetOperation.created_at.asc())
        .all()
    )


def operation_for_plan(db: Session, plan_id: str) -> EvaluationAssetOperation | None:
    return (
        db.query(EvaluationAssetOperation)
        .filter(EvaluationAssetOperation.plan_id == plan_id).first()
    )


def _proposal(db: Session, conversation_id: str, revision: int) -> AssistantProposal:
    row = (
        db.query(AssistantProposal)
        .filter(AssistantProposal.conversation_id == conversation_id,
                AssistantProposal.revision == revision)
        .first()
    )
    if row is None:
        raise AppError("assistant.proposal_stale", "that proposal revision does not exist",
                       {"revision": revision}, status_code=409)
    if row.status == "invalid" or not isinstance(row.content, dict) or not row.content.get(
            "name"):
        raise AppError("assistant.evaluation_plan_source_invalid",
                       "an evaluation plan needs a shape-valid proposal revision",
                       {"revision": revision}, status_code=409)
    return row


def _store_plan(
    db: Session, conversation: AssistantConversation, proposal: AssistantProposal,
    raw: Any, *, source: str, created_by: str,
) -> AssistantEvaluationPlan:
    """Validate and append one plan revision (draft or invalid); older drafts of the
    conversation become superseded. Unique (conversation, revision) — a loser of a
    concurrent write re-allocates the number."""
    plan, errors = plan_contract.validate_plan(
        raw, proposal.content, revision=proposal.revision, content_hash=proposal.content_hash
    )
    content = plan.model_dump() if plan else (raw if isinstance(raw, dict) else {})
    for _attempt in range(5):
        db.rollback()
        db.execute(
            update(AssistantEvaluationPlan)
            .where(AssistantEvaluationPlan.conversation_id == conversation.id,
                   AssistantEvaluationPlan.status.in_(("draft", "invalid")))
            .values(status="superseded")
        )
        latest = (
            db.query(AssistantEvaluationPlan.revision)
            .filter(AssistantEvaluationPlan.conversation_id == conversation.id)
            .order_by(AssistantEvaluationPlan.revision.desc())
            .first()
        )
        row = AssistantEvaluationPlan(
            workspace_id=conversation.workspace_id,
            conversation_id=conversation.id,
            proposal_id=proposal.id,
            source_revision=proposal.revision,
            source_content_hash=proposal.content_hash,
            revision=(latest[0] if latest else 0) + 1,
            source=source,
            content=content,
            content_hash=plan_contract.canonical_hash(content),
            validation_errors=errors,
            status="draft" if plan else "invalid",
            created_by=created_by,
        )
        db.add(row)
        try:
            db.commit()
            return row
        except IntegrityError:
            db.rollback()
    raise AppError("assistant.evaluation_plan_conflict",
                   "could not allocate a plan revision; retry", status_code=409)


def prepare_plan(
    db: Session, conversation: AssistantConversation, *, revision: int, created_by: str
) -> AssistantEvaluationPlan:
    """The platform draft for one proposal revision (no side effects beyond the row)."""
    proposal = _proposal(db, conversation.id, revision)
    draft = plan_contract.draft_plan(
        proposal.content, revision=proposal.revision, content_hash=proposal.content_hash,
        agent_name=str(proposal.content.get("name") or "agent"),
    )
    return _store_plan(db, conversation, proposal, draft, source="platform",
                       created_by=created_by)


def edit_plan(
    db: Session, conversation: AssistantConversation, raw: Any, *, created_by: str
) -> AssistantEvaluationPlan:
    if plan_contract.serialized_bytes(raw) > plan_contract.PLAN_MAX_BYTES:
        raise AppError("assistant.evaluation_plan_too_large",
                       f"the plan exceeds {plan_contract.PLAN_MAX_BYTES} bytes",
                       {"max_bytes": plan_contract.PLAN_MAX_BYTES}, status_code=413)
    revision = raw.get("source_revision") if isinstance(raw, dict) else None
    if not isinstance(revision, int):
        raise AppError("assistant.evaluation_plan_invalid",
                       "plan.source_revision must name the proposal revision", status_code=422)
    proposal = _proposal(db, conversation.id, revision)
    return _store_plan(db, conversation, proposal, raw, source="member", created_by=created_by)


# ---------------------------------------------------------------------------
# approval → operation (the only path that may create an operation)
# ---------------------------------------------------------------------------


@dataclass
class Materialization:
    operation: EvaluationAssetOperation
    started: bool  # this call created the operation (caller launches the worker)


def approver_authorized(db: Session, op: EvaluationAssetOperation) -> str | None:
    """Fresh check that the approver is still an active administrator. ``None`` when
    fine, else the reason. The config admin has no row and stays authorized."""
    if op.approver_user_id is None:
        return None
    user = db.get(User, op.approver_user_id)
    if user is None:
        return "the approving account no longer exists"
    if user.status != "active" or users_service.is_expired(user):
        return "the approving account is disabled or expired"
    if user.role != ROLE_ADMIN:
        return "the approving account is no longer an administrator"
    return None


def approve_plan(
    db: Session,
    conversation: AssistantConversation,
    row: Workspace,
    *,
    plan_revision: int,
    plan_hash: str,
    approved_by: str,
    approver_user_id: str | None,
) -> Materialization:
    """Claim exactly one plan revision for materialization. Repeated / concurrent
    calls for the same plan return the recorded operation (200); a fresh claim
    inserts the operation with all intents in ONE commit (202)."""
    plan_row = (
        db.query(AssistantEvaluationPlan)
        .filter(AssistantEvaluationPlan.conversation_id == conversation.id,
                AssistantEvaluationPlan.revision == plan_revision)
        .first()
    )
    if plan_row is None or plan_row.content_hash != plan_hash:
        raise AppError("assistant.evaluation_plan_stale",
                       "that plan revision/hash is not what is stored",
                       {"revision": plan_revision}, status_code=409)
    existing = operation_for_plan(db, plan_row.id)
    if existing is not None:
        return Materialization(existing, False)
    if plan_row.status not in ("draft", "approved"):
        raise AppError("assistant.evaluation_plan_not_approvable",
                       f"plan revision {plan_revision} is {plan_row.status}",
                       {"revision": plan_revision, "errors": plan_row.validation_errors},
                       status_code=409)
    plan, errors = plan_contract.validate_plan(
        plan_row.content, _proposal(db, conversation.id, plan_row.source_revision).content,
        revision=plan_row.source_revision, content_hash=plan_row.source_content_hash,
    )
    if plan is None:
        raise AppError("assistant.evaluation_plan_invalid",
                       "the plan no longer validates against its proposal revision",
                       {"errors": errors}, status_code=409)
    if row.bootstrap_status != "ready" or not (row.resources or {}).get("execution_role_arn"):
        raise AppError("assistant.workspace_not_ready",
                       "this workspace is not bootstrapped (no execution role)",
                       status_code=409)
    op = EvaluationAssetOperation(
        id=_new_id(),  # assigned NOW: every intent name/token embeds it
        workspace_id=conversation.workspace_id,
        conversation_id=conversation.id,
        plan_id=plan_row.id,
        plan_revision=plan_row.revision,
        plan_hash=plan_row.content_hash,
        proposal_revision=plan_row.source_revision,
        owner_principal=str(conversation.owner_principal),
        approved_by=approved_by,
        approver_user_id=approver_user_id,
        account_id=row.account_id,
        region=row.region,
        status="queued",
    )
    op.resources = compose_intents(plan, op.id)
    db.add(op)
    db.execute(update(AssistantEvaluationPlan).where(AssistantEvaluationPlan.id == plan_row.id)
               .values(status="approved"))
    try:
        db.commit()
    except IntegrityError:  # a concurrent approval won the unique(plan_id)
        db.rollback()
        winner = operation_for_plan(db, plan_row.id)
        if winner is None:  # pragma: no cover
            raise
        return Materialization(winner, False)
    return Materialization(op, True)


# ---------------------------------------------------------------------------
# the worker
# ---------------------------------------------------------------------------


class _LeaseLost(RuntimeError):
    pass


class _Stop(RuntimeError):
    """Stop the operation with a recorded reason (authorization revoked)."""


class _Runner:
    def __init__(self, op_id: str, clients: ClientFactory, sleeper: Callable[[float], None],
                 token: str) -> None:
        self.op_id = op_id
        self.clients = clients
        self.sleep = sleeper
        self.token = token

    # -- ledger helpers -----------------------------------------------------

    def _load(self, db: Session) -> EvaluationAssetOperation:
        db.expire_all()
        op = db.get(EvaluationAssetOperation, self.op_id)
        if op is None or op.worker_token != self.token or op.status != "running":
            raise _LeaseLost(f"operation {self.op_id} lease lost")
        return op

    def _guard(self, db: Session) -> EvaluationAssetOperation:
        """Fresh scalar ownership + authorization check before EVERY mutation."""
        op = self._load(db)
        reason = approver_authorized(db, op)
        if reason:
            raise _Stop(reason)
        db.execute(update(EvaluationAssetOperation)
                   .where(EvaluationAssetOperation.id == op.id,
                          EvaluationAssetOperation.worker_token == self.token)
                   .values(heartbeat_at=_now()))
        db.commit()
        return op

    def _save(self, db: Session, op: EvaluationAssetOperation, resources: list[dict[str, Any]],
              event: str, **fields: Any) -> None:
        line = json.dumps({"at": _now().isoformat(), "event": event, **{
            k: v for k, v in fields.items() if k in ("status", "error", "dataset_id")}},
            ensure_ascii=False)
        rows = db.execute(
            update(EvaluationAssetOperation)
            .where(EvaluationAssetOperation.id == op.id,
                   EvaluationAssetOperation.worker_token == self.token)
            .values(resources=resources, log=(op.log or "") + line + "\n", **fields)
        ).rowcount
        db.commit()
        if rows != 1:
            raise _LeaseLost("lease lost while saving")
        db.expire_all()

    def _resource(self, op: EvaluationAssetOperation, key: str) -> tuple[list[dict], dict]:
        resources = json.loads(json.dumps(op.resources or []))
        for r in resources:
            if r.get("key") == key:
                return resources, r
        raise KeyError(key)

    # -- steps ----------------------------------------------------------------

    def run(self) -> None:
        db = SessionLocal()
        try:
            op = self._guard(db)
            plan_row = db.get(AssistantEvaluationPlan, op.plan_id)
            proposal = _proposal(db, op.conversation_id, op.proposal_revision)
            plan, errors = plan_contract.validate_plan(
                plan_row.content, proposal.content, revision=plan_row.source_revision,
                content_hash=plan_row.source_content_hash,
            )
            if plan is None:
                raise _Stop("plan no longer validates: " + "; ".join(errors[:3]))
            workspace = workspace_context(db.get(Workspace, op.workspace_id))
            for key in [r["key"] for r in op.resources or []]:
                op = self._guard(db)
                resources, res = self._resource(op, key)
                if res.get("status") in ("ready", "skipped", "conflict"):
                    continue
                if res.get("status") == "failed" and int(res.get("attempts") or 0) >= MAX_ATTEMPTS:
                    continue
                res["attempts"] = int(res.get("attempts") or 0) + 1
                try:
                    getattr(self, f"_step_{res['kind']}")(db, op, plan, workspace, resources, res)
                    res["status"] = "ready"
                    res["error"] = None
                    self._save(db, op, resources, f"{key}:ready")
                except (_LeaseLost, _Stop):
                    raise
                except _Conflict as exc:
                    res["status"] = "conflict"
                    res["error"] = str(exc)
                    self._save(db, op, resources, f"{key}:conflict")
                except Exception as exc:  # noqa: BLE001 — recorded per resource
                    logger.warning("evaluation assets %s step %s failed: %s", op.id, key, exc)
                    res["status"] = "failed"
                    res["error"] = _safe_error(exc)
                    self._save(db, op, resources, f"{key}:failed")
                    if res["kind"] in ("lambda_role", "log_group", "lambda_function",
                                       "lambda_permission"):
                        # the code evaluators depend on the function: stop here, honestly
                        break
            op = self._load(db)
            statuses = [r.get("status") for r in op.resources or []]
            if all(s in ("ready", "skipped") for s in statuses):
                final, error = "succeeded", None
            elif any(s in ("ready",) for s in statuses):
                final = "partial"
                error = "; ".join(f"{r['key']}: {r.get('error')}" for r in op.resources or []
                                  if r.get("status") in ("failed", "conflict"))[:2000]
            else:
                final = "failed"
                error = "; ".join(f"{r['key']}: {r.get('error')}" for r in op.resources or []
                                  if r.get("status") in ("failed", "conflict"))[:2000]
            self._save(db, op, op.resources or [], "finished", status=final, error=error,
                       worker_token=None, dataset_id=op.dataset_id)
        except _LeaseLost:
            logger.info("evaluation assets %s: lease lost, another worker owns it", self.op_id)
        except _Stop as exc:
            db.rollback()
            db.execute(update(EvaluationAssetOperation)
                       .where(EvaluationAssetOperation.id == self.op_id,
                              EvaluationAssetOperation.worker_token == self.token)
                       .values(status="failed", error=f"stopped: {exc}", worker_token=None))
            db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.exception("evaluation assets %s crashed", self.op_id)
            db.rollback()
            db.execute(update(EvaluationAssetOperation)
                       .where(EvaluationAssetOperation.id == self.op_id,
                              EvaluationAssetOperation.worker_token == self.token)
                       .values(status="failed", error=_safe_error(exc), worker_token=None))
            db.commit()
        finally:
            db.close()

    # dataset (ledger only) ---------------------------------------------------

    def _step_dataset(self, db, op, plan, workspace, resources, res) -> None:
        if (res.get("result") or {}).get("dataset_id"):
            if db.get(EvalDataset, res["result"]["dataset_id"]) is not None:
                return  # already created; member edits afterwards are theirs
        provenance = {
            "conversation_id": op.conversation_id,
            "proposal_revision": op.proposal_revision,
            "plan_revision": op.plan_revision,
            "plan_hash": op.plan_hash,
            "operation_id": op.id,
            # plan-local keys → resolved ids live on the operation (mapping table)
            "evaluators": {
                e.key: {"kind": e.kind, "golden_test_ids": list(e.golden_test_ids),
                        "blocking": e.blocking, "threshold": e.threshold}
                for e in plan.evaluators
            },
        }
        items = [plan_contract.dataset_item(s, provenance) for s in plan.scenarios]
        from app.evaluation.routers import _validate_items  # dataset ingress gate

        _validate_items(items)
        from app.evaluation.execution import validate_items

        validate_items(items)
        dataset = EvalDataset(
            workspace_id=op.workspace_id, name=plan.dataset.name, locale=plan.dataset.locale,
            description=plan.dataset.description, items=items, kind="predefined",
        )
        db.add(dataset)
        db.flush()
        res["result"] = {"dataset_id": dataset.id, "item_count": len(items)}
        op.dataset_id = dataset.id
        # same transaction as the resource status (saved by the caller) — flush now so
        # the id is stable; _save commits
        self._save(db, op, resources, "dataset:created", dataset_id=dataset.id)

    # IAM role for the function -----------------------------------------------

    def _step_lambda_role(self, db, op, plan, workspace, resources, res) -> None:
        iam = self.clients(workspace, "iam")
        name = res["name"]
        fn = function_name(op.id)
        trust = {"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {"StringEquals": {"aws:SourceAccount": op.account_id}},
        }]}
        log_arn = f"arn:aws:logs:{op.region}:{op.account_id}:log-group:/aws/lambda/{fn}"
        policy = {"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow",
            "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
            "Resource": [log_arn, f"{log_arn}:*"],
        }]}
        stored = res.get("result") or {}
        if stored.get("role_id"):
            current = iam.get_role(RoleName=name)["Role"]
            if current["RoleId"] != stored["role_id"]:
                raise _Conflict(f"role {name} was replaced (RoleId differs) — not ours")
        else:
            try:
                created = iam.create_role(
                    RoleName=name, AssumeRolePolicyDocument=json.dumps(trust),
                    Description=f"Launchpad code-evaluator Lambda role (operation {op.id})",
                    Tags=[{"Key": TAG_OPERATION, "Value": op.id},
                          {"Key": TAG_MANAGED, "Value": "true"}],
                )["Role"]
            except ClientError as exc:
                if _code(exc) in _CONFLICT_CODES:
                    raise _Conflict(f"an IAM role named {name} already exists and was not "
                                    "created by this operation") from exc
                raise
            res["result"] = {"role_arn": created["Arn"], "role_id": created["RoleId"]}
            self._save(db, op, resources, "lambda_role:accepted")
        iam.put_role_policy(RoleName=name, PolicyName="launchpad-evalfn-logs",
                            PolicyDocument=json.dumps(policy))
        res["result"]["policy"] = "launchpad-evalfn-logs"

    # log group ---------------------------------------------------------------

    def _step_log_group(self, db, op, plan, workspace, resources, res) -> None:
        logs = self.clients(workspace, "logs")
        name = res["name"]
        if not (res.get("result") or {}).get("created"):
            try:
                logs.create_log_group(logGroupName=name,
                                      tags={TAG_OPERATION: op.id, TAG_MANAGED: "true"})
            except ClientError as exc:
                if _code(exc) in _CONFLICT_CODES:
                    raise _Conflict(f"log group {name} already exists and was not created by "
                                    "this operation") from exc
                raise
            res["result"] = {"created": True}
            self._save(db, op, resources, "log_group:accepted")
        logs.put_retention_policy(logGroupName=name, retentionInDays=LOG_RETENTION_DAYS)
        res["result"]["retention_days"] = LOG_RETENTION_DAYS

    # the function -------------------------------------------------------------

    def _step_lambda_function(self, db, op, plan, workspace, resources, res) -> None:
        lam = self.clients(workspace, "lambda")
        _, role = self._resource(op, "lambda_role")
        role_arn = (role.get("result") or {}).get("role_arn")
        if not role_arn:
            raise RuntimeError("lambda role not ready")
        rules = canonical_rules(plan)
        payload, digest = build_package(rules)
        if digest != res.get("digest"):
            raise RuntimeError("package digest differs from the persisted intent")
        name = res["name"]
        stored = res.get("result") or {}
        if not stored.get("function_arn"):
            request = {
                "FunctionName": name, "Runtime": LAMBDA_RUNTIME, "Role": role_arn,
                "Handler": LAMBDA_HANDLER,
                "Description": f"Launchpad reviewed code evaluator (operation {op.id})",
                "Timeout": int(res["timeout_s"]), "MemorySize": LAMBDA_MEMORY_MB,
                "Publish": False,
                "Tags": {TAG_OPERATION: op.id, TAG_MANAGED: "true"},
            }
            res["request"] = {**request, "CodeSha256": code_sha256_b64(digest)}
            self._save(db, op, resources, "lambda_function:intent")
            created = None
            for attempt in range(6):
                try:
                    created = lam.create_function(**request, Code={"ZipFile": payload})
                    break
                except ClientError as exc:
                    code = _code(exc)
                    if code in _CONFLICT_CODES:
                        # lost response replay: accept ONLY our exact function
                        current = lam.get_function(FunctionName=name)
                        cfg = current.get("Configuration") or {}
                        tags = current.get("Tags") or {}
                        if (cfg.get("CodeSha256") == code_sha256_b64(digest)
                                and cfg.get("Role") == role_arn
                                and tags.get(TAG_OPERATION) == op.id):
                            created = cfg
                            break
                        raise _Conflict(f"a Lambda function named {name} exists that this "
                                        "operation cannot prove it created") from exc
                    if code == "InvalidParameterValueException" and "assumed" in str(exc) \
                            and attempt < 5:
                        self.sleep(5)  # IAM propagation of the brand-new role
                        continue
                    raise
            res["result"] = {"function_arn": created["FunctionArn"], "function_name": name}
            self._save(db, op, resources, "lambda_function:accepted")
        # wait Active, then publish the immutable version pinned to our digest
        cfg = self._wait_function_active(lam, name)
        if cfg.get("CodeSha256") != code_sha256_b64(digest):
            raise _Conflict("function code differs from the reviewed package")
        stored = res["result"]
        if not stored.get("version"):
            published = lam.publish_version(FunctionName=name,
                                            CodeSha256=code_sha256_b64(digest))
            stored["version"] = published["Version"]
            stored["version_arn"] = published["FunctionArn"]
            self._save(db, op, resources, "lambda_function:published")
        lam.put_function_concurrency(FunctionName=name,
                                     ReservedConcurrentExecutions=LAMBDA_RESERVED_CONCURRENCY)
        back = lam.get_function(FunctionName=name, Qualifier=stored["version"])
        cfg = back.get("Configuration") or {}
        mismatch = [k for k, want in (("CodeSha256", code_sha256_b64(digest)),
                                      ("Runtime", LAMBDA_RUNTIME), ("Handler", LAMBDA_HANDLER),
                                      ("Role", role_arn), ("Version", stored["version"]))
                    if cfg.get(k) != want]
        if mismatch:
            raise _Conflict(f"published version readback differs on {mismatch}")
        stored["readback"] = {k: cfg.get(k) for k in ("CodeSha256", "Runtime", "Handler",
                                                        "Role", "Version", "FunctionArn",
                                                        "State", "Timeout", "MemorySize")}

    def _wait_function_active(self, lam: Any, name: str) -> dict[str, Any]:
        for _ in range(READBACK_ATTEMPTS):
            cfg = lam.get_function_configuration(FunctionName=name)
            state = cfg.get("State")
            if state == "Active":
                return cfg
            if state == "Failed":
                raise RuntimeError(f"function state Failed: {cfg.get('StateReason')}")
            self.sleep(READBACK_DELAY_S)
        raise RuntimeError("function did not become Active in time")

    # resource policy -----------------------------------------------------------

    def _step_lambda_permission(self, db, op, plan, workspace, resources, res) -> None:
        lam = self.clients(workspace, "lambda")
        _, fn = self._resource(op, "lambda_function")
        stored = fn.get("result") or {}
        if not stored.get("version"):
            raise RuntimeError("function version not published")
        try:
            lam.add_permission(
                FunctionName=stored["function_name"], StatementId=PERMISSION_SID,
                Action="lambda:InvokeFunction", Principal=AGENTCORE_PRINCIPAL,
                SourceAccount=op.account_id, Qualifier=stored["version"],
            )
        except ClientError as exc:
            if _code(exc) not in _CONFLICT_CODES:
                raise
            # replay: the statement must be exactly ours
            policy = json.loads(lam.get_policy(FunctionName=stored["function_name"],
                                               Qualifier=stored["version"])["Policy"])
            ours = [s for s in policy.get("Statement", []) if s.get("Sid") == PERMISSION_SID]
            if not ours or ours[0].get("Principal", {}).get("Service") != AGENTCORE_PRINCIPAL:
                raise _Conflict("resource policy statement exists but is not ours") from exc
        res["result"] = {"principal": AGENTCORE_PRINCIPAL, "source_account": op.account_id,
                         "qualifier": stored["version"]}

    # additive grant on the workspace execution role ------------------------------

    def _step_role_grant(self, db, op, plan, workspace, resources, res) -> None:
        iam = self.clients(workspace, "iam")
        _, fn = self._resource(op, "lambda_function")
        stored = fn.get("result") or {}
        if not stored.get("version_arn"):
            raise RuntimeError("function version not published")
        role_arn = str((workspace.resources or {}).get("execution_role_arn") or "")
        role_name = role_arn.rsplit("/", 1)[-1]
        if not role_arn or not role_name:
            raise RuntimeError("workspace has no execution role ARN")
        current = iam.get_role(RoleName=role_name)["Role"]
        tags = {t["Key"]: t["Value"] for t in (current.get("Tags") or [])}
        trusted = current["Arn"] == role_arn and (
            tags.get(TAG_MANAGED) == "true" or role_name.startswith("launchpad-"))
        if not trusted:
            raise _Conflict(f"execution role {role_name} is not a platform-managed role — the "
                            "grant was not written; attach Invoke/GetFunction on "
                            f"{stored['version_arn']} manually if wanted")
        previous = (res.get("result") or {}).get("role_id")
        if previous and previous != current["RoleId"]:
            raise _Conflict("execution role was replaced since the first attempt (RoleId "
                            "differs) — grant refused")
        res["result"] = {"role_arn": current["Arn"], "role_id": current["RoleId"],
                         "policy_name": res["name"]}
        self._save(db, op, resources, "role_grant:intent")
        document = {"Version": "2012-10-17", "Statement": [{
            "Sid": "LaunchpadEvalOpInvoke", "Effect": "Allow",
            "Action": ["lambda:InvokeFunction", "lambda:GetFunction"],
            "Resource": [stored["version_arn"], stored["function_arn"]],
        }]}
        iam.put_role_policy(RoleName=role_name, PolicyName=res["name"],
                            PolicyDocument=json.dumps(document))
        res["result"]["resources"] = document["Statement"][0]["Resource"]

    # evaluators ------------------------------------------------------------------

    def _step_existing(self, db, op, plan, workspace, resources, res) -> None:
        evaluator_id = res["name"]
        if evaluator_id in ALL_BUILTIN_EVALUATORS:
            res["result"] = {"evaluator_id": evaluator_id, "level":
                             ALL_BUILTIN_EVALUATORS[evaluator_id], "source": "builtin"}
            return
        control = self.clients(workspace, "bedrock-agentcore-control")
        try:
            detail = control.get_evaluator(evaluatorId=evaluator_id)
        except ClientError as exc:
            if _code(exc) in _NOT_FOUND_CODES:
                raise RuntimeError(f"evaluator {evaluator_id} does not exist in this "
                                   "workspace") from exc
            raise
        res["result"] = {"evaluator_id": detail.get("evaluatorId") or evaluator_id,
                         "level": detail.get("level"), "status": detail.get("status"),
                         "source": "existing"}

    def _step_evaluator(self, db, op, plan, workspace, resources, res) -> None:
        control = self.clients(workspace, "bedrock-agentcore-control")
        entry = next(e for e in plan.evaluators if e.key == res["plan_key"])
        request = self._evaluator_request(op, entry, res)
        stored = res.get("result") or {}
        if res.get("request") is None:
            res["request"] = request
            self._save(db, op, resources, f"{res['key']}:intent")
        elif res["request"] != request:
            raise _Conflict("the persisted create request differs from the plan — refused")
        if not stored.get("evaluator_id"):
            try:
                created = control.create_evaluator(**request)
            except ClientError as exc:
                if _code(exc) in _CONFLICT_CODES:
                    raise _Conflict(f"an evaluator named {entry.name} exists that this "
                                    "operation cannot prove it created (token replay refused)"
                                    ) from exc
                raise
            res["result"] = {"evaluator_id": created["evaluatorId"],
                             "evaluator_arn": created.get("evaluatorArn")}
            self._save(db, op, resources, f"{res['key']}:accepted")
            stored = res["result"]
        detail = None
        for _ in range(READBACK_ATTEMPTS):
            detail = control.get_evaluator(evaluatorId=stored["evaluator_id"])
            if detail.get("status") in ("ACTIVE", "READY"):
                break
            if detail.get("status") in ("FAILED", "DELETING"):
                raise RuntimeError(f"evaluator status {detail.get('status')}")
            self.sleep(READBACK_DELAY_S)
        if detail is None or detail.get("status") not in ("ACTIVE", "READY"):
            raise RuntimeError("evaluator did not become ACTIVE in time")
        if detail.get("evaluatorConfig") != request["evaluatorConfig"] or \
                detail.get("level") != request["level"]:
            raise _Conflict("evaluator readback configuration differs from the request")
        stored["status"] = detail.get("status")
        stored["level"] = detail.get("level")
        stored["name"] = detail.get("evaluatorName")

    def _evaluator_request(self, op, entry, res) -> dict[str, Any]:
        base = {"evaluatorName": entry.name, "description": entry.description or entry.title,
                "clientToken": res["client_token"]}
        if isinstance(entry, plan_contract.JudgeEvaluator):
            return {**base, "level": entry.level, "evaluatorConfig": {"llmAsAJudge": {
                "instructions": entry.instructions,
                "ratingScale": {"numerical": [r.model_dump() for r in entry.rating_scale]},
                "modelConfig": {"bedrockEvaluatorModelConfig": {"modelId": entry.model_id}},
            }}}
        if isinstance(entry, plan_contract.DerivedEvaluator):
            level = ALL_BUILTIN_EVALUATORS.get(entry.base_evaluator_id)
            if level is None:
                level = str(self.clients(self._workspace_of(op), "bedrock-agentcore-control")
                            .get_evaluator(evaluatorId=entry.base_evaluator_id)["level"])
            return {**base, "level": level, "evaluatorConfig": {"derived": {
                "baseEvaluatorId": entry.base_evaluator_id,
                "modelConfig": {"bedrockEvaluatorModelConfig": {"modelId": entry.model_id}},
            }}}
        _, fn = self._resource(op, "lambda_function")
        version_arn = (fn.get("result") or {}).get("version_arn")
        if not version_arn:
            raise RuntimeError("code evaluator needs the published Lambda version")
        return {**base, "level": entry.level, "evaluatorConfig": {"codeBased": {"lambdaConfig": {
            "lambdaArn": version_arn, "lambdaTimeoutInSeconds": entry.lambda_timeout_s}}}}

    def _workspace_of(self, op) -> WorkspaceContext:
        db = SessionLocal()
        try:
            return workspace_context(db.get(Workspace, op.workspace_id))
        finally:
            db.close()


class _Conflict(RuntimeError):
    """A resource this operation cannot prove it owns — recorded, never adopted."""


# ---------------------------------------------------------------------------
# lease + dispatch
# ---------------------------------------------------------------------------


def claim_lease(db: Session, op_id: str) -> str | None:
    """Conditional UPDATE: one writer per operation. Returns the lease token when this
    caller now owns the run, ``None`` when another live worker does or the operation is
    finished/exhausted."""
    token = secrets.token_hex(16)
    stale = _now() - LEASE_TTL
    rows = db.execute(
        update(EvaluationAssetOperation)
        .where(
            EvaluationAssetOperation.id == op_id,
            EvaluationAssetOperation.status.in_(("queued", "running", "partial", "failed")),
            EvaluationAssetOperation.attempts < MAX_ATTEMPTS,
            (EvaluationAssetOperation.worker_token.is_(None))
            | (EvaluationAssetOperation.heartbeat_at.is_(None))
            | (EvaluationAssetOperation.heartbeat_at < stale),
        )
        .values(status="running", worker_token=token, heartbeat_at=_now(),
                attempts=EvaluationAssetOperation.attempts + 1, error=None)
    ).rowcount
    db.commit()
    return token if rows == 1 else None


def run_operation(
    op_id: str, *, clients: ClientFactory = _default_clients,
    sleeper: Callable[[float], None] = time.sleep,
) -> bool:
    """Run (or resume) one operation synchronously. False when no lease was won."""
    db = SessionLocal()
    try:
        token = claim_lease(db, op_id)
    finally:
        db.close()
    if token is None:
        return False
    _Runner(op_id, clients, sleeper, token).run()
    return True


def start_async(op_id: str, **kwargs: Any) -> threading.Thread | None:
    with _LIVE_LOCK:
        current = _LIVE.get(op_id)
        if current is not None and current.is_alive():
            return None
        thread = threading.Thread(target=run_operation, args=(op_id,), kwargs=kwargs,
                                  name=f"eval-assets-{op_id}", daemon=True)
        _LIVE[op_id] = thread
        thread.start()
        return thread


def resume_operations() -> list[str]:
    """Startup: re-wake ONLY operations an administrator explicitly approved and that
    were interrupted (running/queued with no live worker). Finished, partial and failed
    operations are never re-run without a new explicit retry."""
    db = SessionLocal()
    try:
        ids = [
            r[0] for r in db.query(EvaluationAssetOperation.id)
            .filter(EvaluationAssetOperation.status.in_(("queued", "running")))
            .all()
        ]
    finally:
        db.close()
    resumed = [op_id for op_id in ids if start_async(op_id) is not None]
    return resumed


def retry_operation(db: Session, op: EvaluationAssetOperation) -> bool:
    """Explicit retry of a partial/failed operation (bounded by MAX_ATTEMPTS)."""
    if op.status in ("succeeded", "cleaned", "cleaning"):
        return False
    if op.attempts >= MAX_ATTEMPTS:
        raise AppError("assistant.evaluation_assets_exhausted",
                       f"the operation reached its {MAX_ATTEMPTS} attempts; clean up the owned "
                       "resources and prepare a new plan revision", status_code=409)
    if live_worker(op.id) is not None:
        return False
    db.execute(update(EvaluationAssetOperation).where(EvaluationAssetOperation.id == op.id)
               .values(status="queued", worker_token=None))
    db.commit()
    return start_async(op.id) is not None


# ---------------------------------------------------------------------------
# cleanup of owned cloud artifacts (never the dataset, never foreign resources)
# ---------------------------------------------------------------------------


def cleanup_operation(
    db: Session, op: EvaluationAssetOperation, workspace: WorkspaceContext, *,
    clients: ClientFactory = _default_clients,
) -> EvaluationAssetOperation:
    """Delete exactly the cloud resources this operation recorded as its own (ids from
    its own create responses), in reverse order. Leaves the local Dataset (a member
    asset, removable in the Evaluation console), never touches the shared execution
    role beyond removing the operation's own inline policy, and records every
    limitation (e.g. an evaluator locked by an online configuration) instead of hiding
    it. Idempotent."""
    if live_worker(op.id) is not None:
        raise AppError("assistant.evaluation_assets_running",
                       "the operation is still running; wait for it to finish", status_code=409)
    resources = json.loads(json.dumps(op.resources or []))
    by_key = {r["key"]: r for r in resources}

    def mark(r: dict[str, Any], ok: bool, note: str | None = None) -> None:
        r["cleanup"] = {"at": _now().isoformat(), "ok": ok, "note": note}
        r["status"] = "deleted" if ok else "delete_failed"

    def gone(exc: ClientError) -> bool:
        return _code(exc) in _NOT_FOUND_CODES

    for r in [x for x in resources if x["kind"] == "evaluator"]:
        eid = (r.get("result") or {}).get("evaluator_id")
        if not eid or r.get("status") in ("deleted", "conflict"):
            if r.get("status") not in ("deleted", "conflict"):
                mark(r, True, "nothing created")
            continue
        try:
            clients(workspace, "bedrock-agentcore-control").delete_evaluator(evaluatorId=eid)
            mark(r, True)
        except ClientError as exc:
            mark(r, gone(exc), None if gone(exc) else _safe_error(exc))
    grant = by_key.get("role_grant")
    if grant and (grant.get("result") or {}).get("policy_name") and grant.get("status") not in (
            "deleted", "skipped", "conflict"):
        try:
            clients(workspace, "iam").delete_role_policy(
                RoleName=str(grant["result"]["role_arn"]).rsplit("/", 1)[-1],
                PolicyName=grant["result"]["policy_name"])
            mark(grant, True)
        except ClientError as exc:
            mark(grant, gone(exc), None if gone(exc) else _safe_error(exc))
    fn = by_key.get("lambda_function")
    if fn and (fn.get("result") or {}).get("function_arn") and fn.get("status") not in (
            "deleted", "conflict"):
        try:
            clients(workspace, "lambda").delete_function(FunctionName=fn["result"]["function_name"])
            mark(fn, True)
            perm = by_key.get("lambda_permission")
            if perm:
                mark(perm, True, "deleted with the function")
        except ClientError as exc:
            mark(fn, gone(exc), None if gone(exc) else _safe_error(exc))
    lg = by_key.get("log_group")
    if lg and (lg.get("result") or {}).get("created") and lg.get("status") not in (
            "deleted", "conflict"):
        try:
            clients(workspace, "logs").delete_log_group(logGroupName=lg["name"])
            mark(lg, True)
        except ClientError as exc:
            mark(lg, gone(exc), None if gone(exc) else _safe_error(exc))
    role = by_key.get("lambda_role")
    if role and (role.get("result") or {}).get("role_id") and role.get("status") not in (
            "deleted", "conflict"):
        iam = clients(workspace, "iam")
        try:
            current = iam.get_role(RoleName=role["name"])["Role"]
            if current["RoleId"] != role["result"]["role_id"]:
                mark(role, False, "RoleId differs — not deleting a role we did not create")
            else:
                try:
                    iam.delete_role_policy(RoleName=role["name"],
                                           PolicyName="launchpad-evalfn-logs")
                except ClientError as exc:
                    if not gone(exc):
                        raise
                iam.delete_role(RoleName=role["name"])
                mark(role, True)
        except ClientError as exc:
            mark(role, gone(exc), None if gone(exc) else _safe_error(exc))
    remaining = [r["key"] for r in resources if r.get("status") == "delete_failed"]
    op.resources = resources
    op.status = "cleaned" if not remaining else "partial"
    op.error = None if not remaining else "cleanup incomplete: " + ", ".join(remaining)
    op.log = (op.log or "") + json.dumps({"at": _now().isoformat(), "event": "cleanup",
                                          "remaining": remaining}) + "\n"
    db.commit()
    db.refresh(op)
    return op


# ---------------------------------------------------------------------------
# managed-association projection for the ordinary Evaluation CRUD
# ---------------------------------------------------------------------------


def managed_evaluator(db: Session, workspace_id: str, evaluator_id: str) -> dict[str, Any] | None:
    """The operation that owns ``evaluator_id`` (created it and has not cleaned it), so
    the ordinary evaluator DELETE can refuse and point at the managed cleanup path
    instead of leaving an undeclared Lambda/IAM orphan behind."""
    rows = (
        db.query(EvaluationAssetOperation)
        .filter(EvaluationAssetOperation.workspace_id == workspace_id,
                EvaluationAssetOperation.status.notin_(("cleaned",)))
        .all()
    )
    for op in rows:
        for r in op.resources or []:
            if r.get("kind") == "evaluator" and (r.get("result") or {}).get(
                    "evaluator_id") == evaluator_id and r.get("status") != "deleted":
                return {"operation_id": op.id, "conversation_id": op.conversation_id,
                        "plan_revision": op.plan_revision}
    return None


def owned_operation(
    db: Session, conversation: AssistantConversation, op_id: str
) -> EvaluationAssetOperation:
    op = db.get(EvaluationAssetOperation, op_id)
    if op is None or op.conversation_id != conversation.id \
            or op.workspace_id != conversation.workspace_id:
        raise NotFoundError("assistant.operation_not_found", "operation not found")
    return op
