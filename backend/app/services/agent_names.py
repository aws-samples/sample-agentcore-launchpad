"""Atomic live-name reservation shared by every agent creation path.

``agents.name`` uniqueness among live rows is enforced by the API layer (a
deleted agent's name is reusable), so two concurrent creators can both pass the
holder query. ``claim_agent_name`` turns the reservation into a single INSERT on
``agent_name_claims`` (composite primary key) inside the caller's transaction:
exactly one writer wins; the loser gets the same ``409 agent.name_exists`` the
holder query gives (the claim is the unique ``claim_key`` column). ``release_agent_name``
frees the row when the agent is deleted.
Legacy agents predate the table and stay protected by the holder query alone.
"""

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.assistant import AgentNameClaim
from app.models.ledger import Agent


def live_holder(db: Session, workspace_id: str, name: str) -> Agent | None:
    return (
        db.query(Agent)
        .filter(Agent.workspace_id == workspace_id, Agent.name == name,
                Agent.status != "deleted")
        .first()
    )


def name_exists_error(name: str, agent_id: str | None) -> AppError:
    return AppError(
        "agent.name_exists",
        f"an agent named '{name}' already exists",
        {"agent_id": agent_id},
        status_code=409,
    )


def claim_agent_name(db: Session, workspace_id: str, name: str, agent_id: str) -> None:
    """Reserve ``name`` for ``agent_id`` in the caller's open transaction.

    Raises ``409 agent.name_exists`` after rolling the session back when another
    live agent (legacy holder query) or a concurrent creation (claim row) owns it.
    The caller must treat the raise as the end of its transaction.
    """
    holder = live_holder(db, workspace_id, name)
    if holder is not None and holder.id != agent_id:
        db.rollback()
        raise name_exists_error(name, holder.id)
    db.add(AgentNameClaim(workspace_id=workspace_id, name=name, agent_id=agent_id,
                          claim_key=AgentNameClaim.key_for(workspace_id, name)))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        current = live_holder(db, workspace_id, name)
        raise name_exists_error(name, current.id if current else None) from None


def release_agent_name(db: Session, workspace_id: str | None, name: str, agent_id: str) -> None:
    """Free the claim when its agent is deleted (no-op for legacy rows)."""
    db.execute(
        delete(AgentNameClaim).where(
            AgentNameClaim.workspace_id == (workspace_id or ""),
            AgentNameClaim.name == name,
            AgentNameClaim.agent_id == agent_id,
        )
    )
