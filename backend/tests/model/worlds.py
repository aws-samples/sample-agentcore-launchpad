"""Per-example world resets for the round-16 state machines.

Hypothesis runs every example of a ``RuleBasedStateMachine`` inside ONE pytest test, so
the autouse ``clean_tables`` wipe never runs between examples. Each machine starts from a
clean slice of the ledger (the rows it creates — never users, workspaces or the installed
presets), so a previous example's queued job or stuck canary flag cannot leak into the
next one's expectations.
"""

from __future__ import annotations

import shutil

from sqlalchemy import delete

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation.models import EvalDataset, OnlineEvalConfig
from app.models.assistant import (
    AgentNameClaim,
    AssistantConversation,
    AssistantEvaluationPlan,
    AssistantMessage,
    AssistantProposal,
    AssistantTurnEvent,
)
from app.models.ledger import Agent, Deployment, Job, KnowledgeBase, SpecSnapshot
from app.optimization.models import ExperimentAssignment, RuntimeCanary
from app.skill_lab import artifacts
from app.skill_lab.models import SkillLabJob


def reset_assistant_world() -> None:
    """Conversations and everything an approval / a canary left behind; presets stay. The
    stubbed catalog's knowledge base (``KB123ABC``) gets its ledger row, so an approved
    Agent's KB pin resolves for the shared ledger scan exactly as a bootstrapped KB would."""
    db = SessionLocal()
    try:
        if db.get(KnowledgeBase, "KB123ABC") is None:
            db.add(KnowledgeBase(kb_id="KB123ABC", workspace_id=DEFAULT_WORKSPACE_ID,
                                 name="hr-policies", status="ACTIVE", data_sources=[]))
        for model in (AssistantEvaluationPlan, AssistantProposal, AssistantMessage,
                      AssistantTurnEvent, AssistantConversation, ExperimentAssignment,
                      OnlineEvalConfig, RuntimeCanary, Job, Deployment, SpecSnapshot,
                      AgentNameClaim):
            db.execute(delete(model))
        db.execute(delete(Agent).where(Agent.system_key.is_(None)))
        db.commit()
    finally:
        db.close()


def reset_canary_world(*, keep_datasets: bool = True) -> None:
    db = SessionLocal()
    try:
        for model in (AssistantEvaluationPlan, AssistantProposal, AssistantMessage,
                      AssistantTurnEvent, AssistantConversation, ExperimentAssignment,
                      OnlineEvalConfig, RuntimeCanary, Job, Deployment, SpecSnapshot):
            db.execute(delete(model))
        if not keep_datasets:
            db.execute(delete(EvalDataset))
        db.execute(delete(Agent).where(Agent.system_key.is_(None)))
        db.commit()
    finally:
        db.close()


def reset_skill_lab_world() -> None:
    """Job rows and their artifact directories (task sets stay — they are inputs)."""
    db = SessionLocal()
    try:
        db.execute(delete(SkillLabJob))
        db.commit()
    finally:
        db.close()
    if artifacts.JOBS_DIR.is_dir():
        for path in artifacts.JOBS_DIR.iterdir():
            shutil.rmtree(path, ignore_errors=True)
