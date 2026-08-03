#!/usr/bin/env python
"""Report which agents are still on the shared execution role (T3).

    cd backend && uv run python scripts/migrate_agent_roles.py

Migration is a **re-publish**, not a hand-rolled `UpdateAgentRuntime`. Two reasons:

* `UpdateAgentRuntime` **resets fields that are omitted** from the request, so a
  partial update would silently clear `filesystemConfigurations`,
  `protocolConfiguration` or the environment. The deploy pipeline already builds the
  full, correct payload for every method.
* the provision stage is where per-agent roles are created, so re-publishing gets the
  role, its policies, and the runtime update in one already-tested path.

So this script reports, and prints the re-publish call to make. It deliberately does
not mutate anything: a re-publish rebuilds the artifact (pip / CodeBuild), which is
an operator's decision to schedule, not a script's to trigger silently.

**Order matters.** Re-publishing also re-validates the spec, so an agent whose
`requirements` are unpinned will be refused by the T10 rules. Run
`scripts/migrate_pin_requirements.py --apply` first.
"""

import sys

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.ledger import Agent
from app.services import agent_iam
from app.services.agentcore.client import control_client
from app.services.runtime_discovery import DISCOVERED_METHOD


def _live_role(agent: Agent) -> str | None:
    """The role the runtime is actually using, or None when it cannot be read."""
    if not agent.resource_id:
        return None
    try:
        from app.services.agentcore import runtime as rt

        if agent.method == "harness":
            from app.services.agentcore import harness as hc

            return hc.get_harness(control_client(), agent.resource_id).get(
                "executionRoleArn"
            )
        return rt.get_runtime(control_client(), agent.resource_id).get("roleArn")
    except Exception:  # noqa: BLE001 — reporting must not hard-fail on one agent
        return None


def main() -> int:
    settings = get_settings()
    shared = agent_iam.shared_role_arn(settings)
    if not settings.per_agent_execution_roles:
        print("per_agent_execution_roles is off — nothing to migrate to.")
        return 0

    db = SessionLocal()
    try:
        agents = (
            db.query(Agent)
            .filter(Agent.status != "deleted", Agent.method != DISCOVERED_METHOD)
            .order_by(Agent.created_at)
            .all()
        )
    finally:
        db.close()

    on_shared: list[tuple[Agent, str]] = []
    unknown: list[Agent] = []
    migrated: list[Agent] = []
    for agent in agents:
        role = _live_role(agent)
        if role is None:
            unknown.append(agent)
        elif shared and role == shared:
            on_shared.append((agent, role))
        else:
            migrated.append(agent)

    print(f"shared role: {shared or '(not configured)'}\n")
    print(f"already on a per-agent role: {len(migrated)}")
    for agent in migrated:
        print(f"  {agent.name} ({agent.method})")

    print(f"\nstill on the shared role: {len(on_shared)}")
    for agent, _role in on_shared:
        target = agent_iam.role_name_for(agent.name, agent.id)
        print(f"  {agent.name} ({agent.method}) → will become {target}")

    if unknown:
        print(f"\ncould not read the live role: {len(unknown)}")
        for agent in unknown:
            print(f"  {agent.name} ({agent.method}) — no resource_id, or AWS read failed")

    if on_shared:
        print(
            "\nTo migrate, re-publish each one from the console (Agents → re-publish)\n"
            "or POST /api/agents/{id}/redeploy with its current spec. The provision\n"
            "stage creates the per-agent role and the deploy stage moves the runtime\n"
            "onto it, using the full update payload.\n\n"
            "Run scripts/migrate_pin_requirements.py --apply FIRST — a re-publish\n"
            "re-validates the spec, and an unpinned requirement is now refused."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
