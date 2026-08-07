"""One-shot A2A card refresh — re-register every active agent's registry
record so existing cards pick up the enriched shape (resolvable url, derived
skills, transport metadata) without a redeploy.

Usage: .venv/bin/python scripts/refresh_a2a_cards.py [--dry-run]

Approval is preserved: if UpdateRegistryRecord knocks an APPROVED record back
to an earlier status, the script re-approves it (these records were already
through the governance gate; a metadata refresh must not demote them).
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.db import SessionLocal  # noqa: E402
from app.models.ledger import Agent  # noqa: E402
from app.services.agentcore import registry as reg  # noqa: E402
from app.services.agentcore.client import registry_control_client  # noqa: E402
from app.services.registry_console import _registry_id, register_agent_record  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        agents = db.query(Agent).filter(Agent.status == "active").all()
        rows = [(a.id, a.name, a.method, dict(a.spec or {}), a.arn, a.version)
                for a in agents]
    finally:
        db.close()

    client = registry_control_client()
    registry_id = _registry_id()

    for agent_id, name, method, spec, arn, version in rows:
        skills = reg.derive_card_skills(spec)
        if args.dry_run:
            print(f"{name:28s} {method:12s} skills={json.dumps([s['id'] for s in skills])}")
            continue
        existing = reg.find_record(client, registry_id, name, "A2A")
        before = (existing or {}).get("status", "—")
        agent = Agent(id=agent_id, name=name, method=method, spec=spec,
                      arn=arn, version=version, status="active")
        result = register_agent_record(agent, auto_submit=True)
        record_id = result["record_id"]
        # updates transition through an async UPDATING state — settle first,
        # only then read/repair the approval status
        after = reg.wait_record_settled(client, registry_id, record_id).get(
            "status", "?")
        restored = ""
        if before == "APPROVED" and after != "APPROVED":
            reg.approve_record(client, registry_id, record_id)
            after = reg.get_record(client, registry_id, record_id).get("status", "?")
            restored = " (re-approved)"
        print(f"{name:28s} {before:18s} → {after}{restored} · "
              f"skills={json.dumps([s['id'] for s in skills])}")


if __name__ == "__main__":
    main()
