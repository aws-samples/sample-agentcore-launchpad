"""Shared register stage — every deploy method lands its A2A record here."""

from app.deployer.pipeline import StageContext, StageResult
from app.models.ledger import Agent
from app.services.registry_console import RegistryUnavailableError, register_agent_record


def register_stage(ctx: StageContext, agent: Agent) -> StageResult:
    db = ctx.session()
    try:
        row = db.get(Agent, agent.id)
        try:
            result = register_agent_record(row)
        except RegistryUnavailableError as exc:
            detail = f"registry unavailable · register skipped · {exc.message}"
            ctx.log(detail)
            return StageResult(skipped=True, detail=detail)
        row.registry_record_id = result["record_id"]
        db.commit()
        verb = "created" if result["created"] else "refreshed"
        # Only NEW records are auto-submitted; UpdateRegistryRecord resets an
        # existing record to DRAFT and re-entering the approval flow is a human
        # decision (scripts/refresh_a2a_cards.py restores prior approvals). Saying
        # "auto-submitted" on the refresh path sent a later reader hunting a
        # broken state machine when the DRAFT was expected.
        submitted = (
            "auto-submitted" if result["created"] else "DRAFT — needs re-approval"
        )
        ctx.log(f"a2a record {verb} · {result['record_id']} · {submitted}")
        return StageResult(detail=f"registry (A2A) {verb} · {result['record_id']}")
    finally:
        db.close()
