"""Shared register stage — every deploy method lands its A2A record here; a
system-managed preset additionally registers its published Skill release (SE-043)."""

from app.deployer.pipeline import StageContext, StageResult
from app.models.ledger import Agent
from app.services.registry_console import RegistryUnavailableError, register_agent_record


def register_stage(ctx: StageContext, agent: Agent) -> StageResult:
    db = ctx.session()
    try:
        row = db.get(Agent, agent.id)
        try:
            result = register_agent_record(row, ctx.workspace)
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
        detail = f"registry (A2A) {verb} · {result['record_id']}"
        if row.system_key:
            # The preset's Skill gets its own record, after the package stage has
            # verified the published release. A failure here fails the stage with the
            # exact reason — the job never reports a registration AWS did not confirm.
            detail = f"{detail} · {_register_system_skill(ctx, db, row)}"
        return StageResult(detail=detail)
    finally:
        db.close()


def _register_system_skill(ctx: StageContext, db, row: Agent) -> str:
    from app.system_agents import presets as catalogue
    from app.system_agents import skill_registry

    preset = catalogue.get_preset(row.system_key or "")
    if preset is None:
        raise RuntimeError(f"unknown system preset key {row.system_key!r} on agent {row.id}")
    outcome = skill_registry.register_system_skill(db, ctx.workspace, preset, row)
    ctx.log(outcome.summary() + (f" · {outcome.note}" if outcome.note else ""))
    return outcome.summary()
