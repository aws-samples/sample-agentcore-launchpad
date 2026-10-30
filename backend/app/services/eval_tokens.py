"""Eval / canary principal token (per-user policy D5; contract I9 v2 — amendment 10 in
docs/cn/interfaces/policy-principal-contracts.md, user ruling 2026-10-27: evaluation and system
agents are exempt from policy).

Eval and canary turns are not started by a browser, and the Gateway cannot tell an evaluation
run's call from a chat turn of the same agent (same execution role) — the only unforgeable
carrier of "this is eval traffic" is a hub signature. So the hub hands the evaluation worker
ONE hub-signed system token per job through the job's S3 prefix:
``s3://<artifacts>/evaluations/<job_id>/tokens.json`` = ``{schema, job_id, written_at, token}``.

* ``prepare`` (at submission, before the job is dispatched): mints the job's single token —
  ``p`` = ``System::"eval"`` (dataset / online eval runs) or ``System::"canary"`` (canary
  traffic), ``ch`` = ``eval`` | ``canary``, ``sid`` = the job id (one policy session per job,
  ``psid`` = ``codec.psid_for(ws, job_id, tsv)``), audience = the target agent + its declared
  A2A peers — writes the file, records an ``eval_token_files`` row (the claim, never a token)
  and marks the job spec ``principal_tokens`` so the worker reads it. Never the launching user:
  eval traffic carries no user identity (the interceptor passes a verified system principal
  without evaluation). A no-op — the spec untouched — while the Gateway interceptor is off for
  the workspace or the agent is an exempt preset.
* ``tick`` (every poll of the job's watcher): re-signs the token (``jti`` / ``iat`` / ``exp``
  only — same sid / psid / tsv / p) once the file is ``refresh_s`` old, and only while the job
  runs; a finished job's file is deleted.
* ``discard`` / ``discard_jobs`` (terminal state, the watcher's ``finally``) and
  ``sweep_on_startup`` (a terminal or un-resumable owner whose file outlived the watcher).

The token is bearer material: never logged (WARN lines name the job and a code), never copied
into results / artefacts / spans; the bucket's ``EvalTokensReadersOnly`` Deny keeps every
reader but the eval worker and the hub role out. Minting goes through the hub signer behind
the eval path's failure circuit (``principal_mint.PATH_EVAL``, N17-obs493: a KMS failure here
never makes an interactive turn token-less). A job whose token could not be minted runs
token-less — the Gateway then decides as for any token-less caller (a system preset passes, an
ordinary agent is ``principal_missing`` and that is visible in Decisions).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from app.core.db import SessionLocal
from app.models.ledger import Agent
from app.models.policy import EvalTokenFile
from app.policy import contracts
from app.services import principal_mint, principal_token
from app.services.policy_entities import Principal

logger = logging.getLogger("launchpad.policy")

SCHEMA = "launchpad.policy.eval-tokens/2"
SPEC_FLAG = "principal_tokens"
OWNER_EVAL_RUN = "eval_run"
OWNER_CANARY = "canary"
# the two system principals eval traffic acts as (I9 v2); anything else is coerced to them
SYSTEM_EVAL = "eval"
SYSTEM_CANARY = "canary"


def tokens_key(job_id: str) -> str:
    return contracts.EVAL_TOKENS_KEY.format(job_id=job_id)


def refresh_s(method: str | None) -> int:
    """How old a file may get before the watcher rewrites it: I9's 300 s, and at most half a
    token's lifetime (a BYOC target's tokens live 300 s, D4)."""
    return min(int(contracts.EVAL_TOKENS_REFRESH_S), principal_token.ttl_for(method) // 2)


def system_principal(principal: Principal | None, channel: str) -> principal_token.TokenPrincipal:
    """The ``p`` claim of an eval / canary job: ``System::"canary"`` for canary traffic, else
    ``System::"eval"`` — a user principal handed in by an older caller is never minted for
    (amendment 10: eval traffic carries no user identity)."""
    if principal is not None and principal.kind == "system" and principal.id in (
            SYSTEM_EVAL, SYSTEM_CANARY):
        return principal_token.TokenPrincipal.system(principal.id)
    return principal_token.TokenPrincipal.system(
        SYSTEM_CANARY if channel == SYSTEM_CANARY else SYSTEM_EVAL)


def _bucket(ws: Any) -> str:
    return str((ws.resources or {}).get("artifacts_bucket") or "")


def _audience(db: Any, agent_id: str) -> list[str]:
    """The run's agents: the target and its declared A2A peers (``aud``, like a turn token)."""
    agent = db.get(Agent, agent_id)
    if agent is None:
        return []
    try:
        return principal_mint._peer_ids(db, agent)
    except Exception:  # noqa: BLE001 — an unreadable peer list mints the agent alone
        return []


def _mint(*, job_id: str, agent_id: str, workspace_id: str, method: str | None, channel: str,
          who: principal_token.TokenPrincipal, tsv: int, audience: list[str]) -> str | None:
    """The job's token, or ``None`` with one WARN naming the job and a code. Behind the eval
    path's failure circuit for this workspace (N17-obs493): an open circuit mints nothing
    without a KMS call, a Sign failure here opens only that circuit."""
    try:
        claims = principal_token.build_claims(
            workspace_id=workspace_id, agent_id=agent_id, session_id=job_id, tsv=tsv,
            channel=channel, principal=who, method=method, audience=audience)
        return principal_mint._sign(claims, path=principal_mint.PATH_EVAL)
    except principal_token.PrincipalTokenUnavailable as exc:
        code = exc.code
    except Exception as exc:  # noqa: BLE001 — the job runs token-less, never fails on the token
        code = type(exc).__name__
    logger.warning("policy: eval job %s minted no principal token (%s) — the gateway decides",
                   job_id, code)
    return None


def _write(ws: Any, job_id: str, token: str | None, now: datetime) -> bool:
    if not token:
        return False
    body = {"schema": SCHEMA, "job_id": job_id,
            "written_at": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "token": token}
    # one PutObject replaces the object atomically; the bucket default encrypts it (SSE-S3)
    ws.client("s3").put_object(
        Bucket=_bucket(ws), Key=tokens_key(job_id), ContentType="application/json",
        Body=json.dumps(body, separators=(",", ":")).encode("utf-8"))
    return True


def _delete_object(ws: Any, job_id: str) -> None:
    """Delete the file AND its noncurrent versions (the artifacts bucket is versioned: a plain
    DELETE would leave every earlier tokens.json behind a delete marker)."""
    s3 = ws.client("s3")
    bucket, key = _bucket(ws), tokens_key(job_id)
    lister = getattr(s3, "list_object_versions", None)
    if lister is None:
        s3.delete_object(Bucket=bucket, Key=key)
        return
    page = lister(Bucket=bucket, Prefix=key)
    versions = [v for kind in ("Versions", "DeleteMarkers") for v in page.get(kind) or []
                if v.get("Key") == key]
    for version in versions:
        s3.delete_object(Bucket=bucket, Key=key, VersionId=version["VersionId"])
    if not versions:
        s3.delete_object(Bucket=bucket, Key=key)


def prepare(ws: Any, agent: Agent, job: Any, *, channel: str, owner_kind: str, owner_id: str,
            principal: Principal | None = None) -> bool:
    """Mint + write the job's single system token and mark its spec; ``False`` (nothing
    changed) while the carrier is off for the agent or the job has no sessions. Never raises on
    a mint failure (the job runs token-less and the Gateway decides); an S3 write failure
    propagates (the job must not run blind)."""
    if not principal_mint.carrier_on(agent) or not _bucket(ws):
        return False
    if not any(s.get("session_id") for s in (job.spec or {}).get("sessions") or []):
        return False
    who = system_principal(principal, channel)
    db = SessionLocal()
    try:
        tsv = principal_mint.temporal_set_version(db, agent.workspace_id)
        audience = _audience(db, agent.id)
    finally:
        db.close()
    token = _mint(job_id=job.job_id, agent_id=agent.id, workspace_id=agent.workspace_id,
                  method=agent.method, channel=channel, who=who, tsv=tsv, audience=audience)
    now = datetime.now(UTC)
    _write(ws, job.job_id, token, now)
    job.spec[SPEC_FLAG] = {"key": tokens_key(job.job_id)}
    db = SessionLocal()
    try:
        row = db.get(EvalTokenFile, job.job_id) or EvalTokenFile(job_id=job.job_id)
        row.workspace_id, row.owner_kind, row.owner_id = agent.workspace_id, owner_kind, owner_id
        row.agent_id, row.method, row.channel = agent.id, agent.method, channel
        row.principal, row.tsv, row.session_ids = who.claim(), tsv, []  # one token per job
        row.written_at = now
        db.add(row)
        db.commit()
    finally:
        db.close()
    return True


def _principal_of(claim: dict[str, Any]) -> principal_token.TokenPrincipal:
    return principal_token.TokenPrincipal(
        kind=claim["kind"], id=claim["id"], label=claim.get("label") or claim["id"],
        role=claim.get("role"), user_id=claim.get("user_id"))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def refresh(ws: Any, job_id: str, *, now: datetime | None = None) -> bool:
    """Re-sign the job's token (``jti`` / ``iat`` / ``exp`` — sid / psid / tsv / p unchanged:
    the run stays one policy session) and rewrite the file. ``True`` when a fresh token was
    written; a mint failure leaves the old file (its token expires on its own) and the next
    poll tries again."""
    db = SessionLocal()
    try:
        row = db.get(EvalTokenFile, job_id)
        if row is None:
            return False
        now = now or datetime.now(UTC)
        token = _mint(job_id=job_id, agent_id=row.agent_id, workspace_id=row.workspace_id,
                      method=row.method, channel=row.channel,
                      who=_principal_of(dict(row.principal)), tsv=int(row.tsv),
                      audience=_audience(db, row.agent_id))
        if not _write(ws, job_id, token, now):
            return False
        row.written_at = now
        db.commit()
        return True
    finally:
        db.close()


def discard(ws: Any, job_id: str) -> None:
    """Delete the job's file and row (terminal state). Idempotent; a failed delete keeps the
    row, so the next sweep tries again."""
    db = SessionLocal()
    try:
        row = db.get(EvalTokenFile, job_id)
        if row is None:
            return
        try:
            _delete_object(ws, job_id)
        except Exception as exc:  # noqa: BLE001 — the token expires within 900 s anyway
            logger.warning("policy: eval token of job %s not deleted (%s)", job_id,
                           type(exc).__name__)
            return
        db.delete(row)
        db.commit()
    finally:
        db.close()


def discard_jobs(ws: Any, job_ids: list[str]) -> None:
    db = SessionLocal()
    try:
        known = [r[0] for r in db.query(EvalTokenFile.job_id)
                 .filter(EvalTokenFile.job_id.in_(job_ids)).all()] if job_ids else []
    finally:
        db.close()
    for job_id in known:
        discard(ws, job_id)


def tick(ws: Any, jobs: list[tuple[str, bool]], *, now: datetime | None = None) -> None:
    """One watcher poll: ``jobs`` = ``[(job_id, finished)]``. A finished job's file is
    deleted; a running one's is re-signed once it is ``refresh_s`` old. Never raises — a
    failed refresh is retried on the next poll, and the worker sees the old token until it
    expires."""
    if not jobs:
        return
    db = SessionLocal()
    try:
        rows = {r.job_id: (r.written_at, r.method) for r in db.query(EvalTokenFile)
                .filter(EvalTokenFile.job_id.in_([j for j, _ in jobs])).all()}
    finally:
        db.close()
    if not rows:
        return
    now = now or datetime.now(UTC)
    for job_id, finished in jobs:
        if job_id not in rows:
            continue
        try:
            if finished:
                discard(ws, job_id)
                continue
            written_at, method = rows[job_id]
            if (now - _aware(written_at)).total_seconds() >= refresh_s(method):
                refresh(ws, job_id, now=now)
        except Exception as exc:  # noqa: BLE001
            logger.warning("policy: eval token of job %s not refreshed (%s)", job_id,
                           type(exc).__name__)


def sweep_on_startup(*, owner_terminal: Any) -> list[str]:
    """At hub start: delete every file whose owner will not be watched again —
    ``owner_terminal(owner_kind, owner_id) -> bool`` (a canary's traffic is never resumed;
    an eval run is resumed by ``reconcile_run`` unless it is terminal). Returns the job ids
    deleted."""
    from app.services.workspace import context_for_workspace

    db = SessionLocal()
    try:
        rows = [(r.job_id, r.workspace_id, r.owner_kind, r.owner_id)
                for r in db.query(EvalTokenFile).all()]
    finally:
        db.close()
    swept: list[str] = []
    for job_id, workspace_id, owner_kind, owner_id in rows:
        try:
            if not owner_terminal(owner_kind, owner_id):
                continue
            discard(context_for_workspace(workspace_id), job_id)
            swept.append(job_id)
        except Exception as exc:  # noqa: BLE001 — a vanished workspace keeps its row
            logger.warning("policy: eval token of job %s not swept (%s)", job_id,
                           type(exc).__name__)
    return swept
