"""Shared register stage — every deploy method lands its A2A record here; a
system-managed preset additionally registers its published Skill release (SE-043)."""

from typing import Any

from app.deployer.pipeline import StageContext, StageResult
from app.models.ledger import Agent
from app.services import registry_backend
from app.services.registry_backend import RegistryUnavailableError
from app.services.registry_local import RegistryConflictError
from app.services.workspace import WorkspaceContext


def register_agent_record(
    agent: Agent, workspace: WorkspaceContext, auto_submit: bool = True
) -> dict[str, Any]:
    """Create the A2A record for a deployed agent (auto-submit), or refresh it when
    the card changed.

    Raises ``RegistryUnavailableError`` when no registry backend is registered (the
    stage reports ``register skipped``)."""
    backend = registry_backend.backend_or_none()
    if backend is None:
        raise RegistryUnavailableError("no registry backend in this build")
    spec = agent.spec or {}
    # A2A-protocol runtimes serve real JSON-RPC at the data-plane URL — their
    # card is directly consumable. HTTP runtime agents get the same URL as an
    # informative endpoint (platform-invoke transport).
    is_a2a = spec.get("protocol") == "a2a"
    arn = agent.arn or ""
    card = registry_backend.build_a2a_card(
        name=agent.name,
        description=(spec.get("system_prompt") or "")[:180] or f"Launchpad agent {agent.name}",
        arn=arn,
        # Card version is the platform constant shared with the A2A runtime
        # template, not the AgentCore runtime version (which the card cannot
        # carry — it is assigned after the template is rendered).
        version=registry_backend.A2A_CARD_VERSION,
        method=agent.method,
        skills=registry_backend.derive_card_skills(spec) or None,
        transport="a2a-jsonrpc" if is_a2a else "agentcore-http",
        # the partition-aware data-plane URL, derived only for runtime ARNs
        workspace=workspace if ":runtime/" in arn else None,
    )
    description = f"Launchpad agent · method {agent.method}"
    descriptors = registry_backend.build_a2a_descriptors(card)
    record, created = backend.upsert_record(
        workspace,
        name=agent.name,
        description=description,
        descriptor_type="A2A",
        descriptors=descriptors,
    )
    record_id = record["recordId"]
    changed = created
    if created:
        if auto_submit:
            backend.wait_record_settled(workspace, record_id)
            backend.submit_record(workspace, record_id)
    elif (
        registry_backend.descriptor_fingerprint(record.get("descriptors"))
        != registry_backend.descriptor_fingerprint(descriptors)
        or (record.get("description") or "") != description
    ):
        # The card changed (new ARN, skills, prompt): refresh it. The backend resets the
        # record to DRAFT — re-approval is a human step (ruling R3). An identical redeploy
        # leaves an APPROVED record approved.
        backend.update_record(
            workspace, record_id, description=description, descriptors=descriptors
        )
        changed = True
    return {"record_id": record_id, "created": created, "changed": changed}


AGENT_DELETED_DETAIL = "agent deleted during the deploy — no registry record created"


def register_stage(ctx: StageContext, agent: Agent) -> StageResult:
    db = ctx.session()
    try:
        row = db.get(Agent, agent.id)
        if row is None or getattr(row, "status", None) == "deleted":
            # N16-obs83 (live, t1): the register stage created an A2A record for an agent
            # the member had deleted a minute earlier. The stage re-reads the row in its
            # own session and skips — the pipeline's loop then cancels the job.
            ctx.log(AGENT_DELETED_DETAIL)
            return StageResult(skipped=True, detail=AGENT_DELETED_DETAIL)
        try:
            result = register_agent_record(row, ctx.workspace)
        except RegistryUnavailableError as exc:
            detail = f"registry unavailable · register skipped · {exc.message}"
            ctx.log(detail)
            return StageResult(skipped=True, detail=detail)
        except RegistryConflictError as exc:
            # A redeploy whose card changed hits update_record on a DEPRECATED
            # record (the only conflict register_agent_record's own call sites can
            # raise — it never transitions status). The runtime already deployed
            # successfully; failing the whole job over a disabled registry record
            # would be worse than just skipping registration.
            if exc.code == "registry.record_deprecated":
                reason = "a2a record is DEPRECATED — delete it to re-register"
            else:
                reason = "registry conflict"
            detail = f"{reason} · register skipped · {exc.message}"
            ctx.log(detail)
            return StageResult(skipped=True, detail=detail)
        row.registry_record_id = result["record_id"]
        db.commit()
        # Only NEW records are auto-submitted; a refresh re-enters review (N4 plan, R3).
        if result["created"]:
            verb, state = "created", "auto-submitted"
        elif result.get("changed"):
            verb, state = "refreshed", "DRAFT — needs re-approval"
        else:
            verb, state = "unchanged", "approval untouched"
        ctx.log(f"a2a record {verb} · {result['record_id']} · {state}")
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
