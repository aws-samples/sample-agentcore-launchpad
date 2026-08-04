#!/usr/bin/env python
"""Report — and optionally fix — what the T10 pinning rules now refuse.

Validation runs on **write**, so existing rows keep loading; only the next deploy
or edit enforces pinning. That is what makes this script sufficient instead of a
grandfather flag, and why "report" is the default: nothing is broken until someone
re-publishes.

    cd backend && uv run python scripts/migrate_pin_requirements.py
    cd backend && uv run python scripts/migrate_pin_requirements.py --apply
    cd backend && uv run python scripts/migrate_pin_requirements.py --skip-registry

Two kinds of finding:

* **agents** whose `spec.requirements` contain an unpinned entry. `--apply`
  resolves each to a concrete version using the same resolver the harness
  conversion path uses, and writes it back to the ledger.
* **git skill records** with no recorded commit. These cannot be fixed here — a
  SHA needs a fetch — so they are listed for re-import from the Registry console.

The agent half is local (SQLite). The skill half reads AgentCore Registry, so it
needs AWS credentials and a bootstrapped `config/launchpad.yaml`; pass
`--skip-registry` to run the local half alone.
"""

import argparse
import json
import sys

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.ledger import Agent
from app.schemas.requirements import resolve_pins, unpinned


def _skill_source(record: dict) -> dict | None:
    """The stored `skillDefinition.inlineContent.source`, if this is a skill record."""
    skills = (record.get("descriptors") or {}).get("agentSkills") or {}
    raw = (skills.get("skillDefinition") or {}).get("inlineContent")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    source = parsed.get("source")
    return source if isinstance(source, dict) else None


def _report_agents(db) -> list[tuple[Agent, list[str]]]:
    found = []
    for agent in db.query(Agent).all():
        loose = unpinned((agent.spec or {}).get("requirements") or [])
        if loose:
            found.append((agent, loose))
    print(f"agents with unpinned requirements: {len(found)}")
    for agent, loose in found:
        print(f"  {agent.name} ({agent.id}) → {', '.join(loose)}")
    return found


def _report_skill_records() -> int:
    """List git skill records lacking a commit. Returns the count."""
    from app.services.agentcore import registry as reg
    from app.services.agentcore.client import control_client

    settings = get_settings()
    registry_id = settings.resources.get("registry_id")
    if not registry_id:
        print("\nregistry_id missing from config — run scripts/bootstrap.py, or pass "
              "--skip-registry")
        return 0

    client = control_client()
    # ListRegistryRecords returns descriptors as null, so each record has to be
    # fetched to see its stored source.
    summaries = reg.list_records(client, registry_id, descriptor_type="AGENT_SKILLS")
    stale = []
    for summary in summaries:
        record_id = summary.get("registryRecordId") or summary.get("recordId")
        if not record_id:
            continue
        record = reg.get_record(client, registry_id, record_id)
        source = _skill_source(record)
        if source and source.get("kind") == "git" and not source.get("commit"):
            stale.append((record.get("name", record_id), record_id, source))

    print(f"\ngit skill records with no recorded commit: {len(stale)}")
    for name, record_id, source in stale:
        print(f"  {name} ({record_id}) ← {source.get('url')} "
              f"@ {source.get('ref') or 'default branch'}")
    if stale:
        print("  → re-import these from the Registry to capture a commit "
              "(a SHA needs a fetch, so it cannot be filled in locally)")
    return len(stale)


def _apply(db, found: list[tuple[Agent, list[str]]]) -> int:
    """Pin the listed agents. Returns the number that failed."""
    failures = 0
    for agent, _loose in found:
        requirements = (agent.spec or {}).get("requirements") or []
        try:
            pinned = resolve_pins(requirements)
        except ValueError as exc:
            print(f"  {agent.name}: FAILED — {exc}")
            failures += 1
            continue
        # A new dict, not an in-place mutation: SQLAlchemy would not see a mutated
        # JSON column as dirty and the change would silently not persist.
        agent.spec = {**agent.spec, "requirements": pinned}
        changed = [
            f"{before} → {after}"
            for before, after in zip(requirements, pinned, strict=True)
            if before != after
        ]
        print(f"  {agent.name}: {'; '.join(changed)}")
    db.commit()
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="pin the unpinned agent requirements (skill records still need re-import)",
    )
    parser.add_argument(
        "--skip-registry", action="store_true",
        help="skip the AgentCore Registry scan (no AWS credentials needed)",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        found = _report_agents(db)
        if not args.skip_registry:
            try:
                _report_skill_records()
            except Exception as exc:  # noqa: BLE001 — reporting must not hard-fail
                print(f"\ncould not scan the Registry ({type(exc).__name__}: {exc})")
                print("  → re-run with --skip-registry, or check AWS credentials")

        if not args.apply:
            if found:
                print("\nre-run with --apply to pin the agent requirements")
            return 0
        if not found:
            print("\nnothing to apply")
            return 0
        print()
        failures = _apply(db, found)
        print(f"\npinned {len(found) - failures} agent(s), {failures} failed")
        return 1 if failures else 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
