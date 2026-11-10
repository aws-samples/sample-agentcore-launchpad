"""Preserve legacy agent scope as immutable policy text during the engine migration."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.ledger import Agent
from app.models.policy import Policy, PolicyChange, PolicyEngine, PolicyVersion


def _statement_ends(text: str) -> list[int]:
    ends: list[int] = []
    depth, i = 0, 0
    while i < len(text):
        ch = text[i]
        if ch == '"':
            i += 1
            while i < len(text) and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif text[i:i + 2] == "//":
            while i < len(text) and text[i] != "\n":
                i += 1
        elif ch in "({[":
            depth += 1
        elif ch in ")}]":
            depth -= 1
        elif ch == ";" and depth == 0:
            ends.append(i)
        i += 1
    return ends


def bake_scope(db: Session, policy: Policy) -> None:
    """Copy scoped candidates and active versions; never mutate historical statements."""
    from app.services import policy_store
    from app.services.policy_engine_sync import engine_statement
    from app.services.workspace import context_for_workspace

    if policy.scope != "agents":
        return
    agents = set(policy.agent_ids or [])
    for agent in db.query(Agent).filter(
            Agent.workspace_id == policy.workspace_id, Agent.status != "deleted").all():
        if policy.id in ((agent.spec or {}).get("policy_ids") or []):
            agents.add(agent.id)
    clause = (" when { [" + ", ".join(json.dumps(a) for a in sorted(agents))
              + "].contains(context.launchpad.agent_id) }")
    next_version = int(db.scalar(select(func.max(PolicyVersion.version)).where(
        PolicyVersion.policy_id == policy.id)) or 0)
    replacements: dict[int, int] = {}
    for old_number in sorted({v for v in (policy.active_version, policy.draft_version) if v}):
        old = db.scalars(select(PolicyVersion).where(
            PolicyVersion.policy_id == policy.id, PolicyVersion.version == old_number)).one()
        statement, _ = engine_statement(old.statement, old.dialect)
        parts: list[str] = []
        start = 0
        for end in _statement_ends(statement):
            parts.append(statement[start:end].rstrip() + clause)
            start = end
        parts.append(statement[start:])
        statement = "".join(parts)
        report, kind = policy_store._require_valid(
            statement, dialect="dogwood", db=db, engine=db.get(PolicyEngine, policy.engine_id),
            policy_id=policy.id)
        next_version += 1
        is_active = old_number == policy.active_version
        key = policy_store._copy_to_s3(
            context_for_workspace(policy.workspace_id), policy.id, next_version, statement
        ) if is_active else None
        db.add(PolicyVersion(
            workspace_id=policy.workspace_id, policy_id=policy.id, version=next_version,
            statement=statement, kind=kind, dialect="dogwood",
            schema_digest=old.schema_digest, validation=report.as_dict(), s3_key=key,
            authored_by="migration", published_by="migration" if is_active else None,
            published_at=datetime.now(UTC) if is_active else None))
        replacements[old_number] = next_version
    before = {"scope": policy.scope, "agent_ids": sorted(agents),
              "active_version": policy.active_version}
    policy.active_version = replacements.get(policy.active_version, policy.active_version)
    policy.draft_version = replacements.get(policy.draft_version, policy.draft_version)
    policy.scope, policy.agent_ids = "workspace", []
    db.add(PolicyChange(
        workspace_id=policy.workspace_id, engine_id=policy.engine_id, policy_id=policy.id,
        operation="policy.scope_baked", operator="migration", before=before,
        after={"active_version": policy.active_version, "scope": "workspace"}))
