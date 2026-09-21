"""Bind attachment capability to the artifact actually packaged for this job."""

from sqlalchemy.orm import Session

from app.deployer.pipeline import StageContext
from app.models.ledger import Agent, Job
from app.templates.attachment_support import ATTACHMENT_CONTRACT, has_attachment_contract


def source_contract(source: str) -> str | None:
    return ATTACHMENT_CONTRACT if has_attachment_contract(source) else None


def record_input_contract(ctx: StageContext, source: str) -> None:
    """Persist at package success, so deploy resume does not infer from new code."""
    with ctx.session() as db:
        job = db.get(Job, ctx.job_id)
        if job:
            job.payload = {**job.payload, "attachment_contract": source_contract(source)}
            db.commit()


def stamp_input_contract(ctx: StageContext, db: Session, agent: Agent) -> None:
    """Called only after AWS reports READY; never trust AgentSpec for capability."""
    job = db.get(Job, ctx.job_id)
    contract = job.payload.get("attachment_contract") if job else None
    agent.attachment_version = agent.version if contract == "v1" else None
