#!/usr/bin/env python
"""N17-obs474 (security, user-approved): take this environment's platform gateway off
``exceptionLevel: DEBUG``.

    cd backend && uv run python scripts/repair_gateway_exception_level.py            # dry run
    cd backend && uv run python scripts/repair_gateway_exception_level.py --apply    # repair

With DEBUG an interceptor / target Lambda error returns the Lambda's errorMessage AND stack
trace to the calling agent. ``gateway_bootstrap.ensure_gateway`` no longer creates gateways
that way; this repairs an existing one. ``UpdateGateway`` replaces the whole configuration,
so the update carries every field ``GetGateway`` reports and changes only exceptionLevel,
then reads the gateway back and refuses (exit 1) if anything else changed
(``gateway_bootstrap.repair_exception_level``).

It targets exactly this host's gateway (``gateway_name()`` — ``launchpad-gw`` /
``launchpad-gw-<env>``) in the default workspace or ``--workspace <id>``. With ``--probe`` it
targets the kept P0 probe gateway ``launchpad-probe-gw`` instead (by that exact name — the
per-user policy milestone M1 attaches the interceptor there, and attach refuses a DEBUG
gateway); ``--apply`` on it needs the lead's OK, and the probe resources are never deleted.
Any other gateway is refused. Run on a host with its own config.
"""

import argparse
import json
import re
import sys

from app.services import gateway_bootstrap as gb
from app.services.agentcore.client import control_client

_PLATFORM_GATEWAY = re.compile(r"^launchpad-gw(-[a-z0-9]+)?$")
PROBE_GATEWAY = "launchpad-probe-gw"


def target_allowed(name: str, *, probe: bool = False) -> bool:
    """Only a platform gateway (``launchpad-gw`` or ``launchpad-gw-<env>``), or with
    ``probe`` exactly the P0 probe gateway."""
    if probe:
        return name == PROBE_GATEWAY
    return bool(_PLATFORM_GATEWAY.match(name or ""))


def _workspace(workspace_id: str | None):
    from app.services.workspace import default_workspace_context, workspace_context

    if not workspace_id:
        return default_workspace_context()
    from app.core.db import SessionLocal
    from app.models.ledger import Workspace

    db = SessionLocal()
    try:
        row = db.get(Workspace, workspace_id)
        if row is None:
            raise SystemExit(f"no workspace {workspace_id!r}")
        return workspace_context(row)
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="perform the update (default: dry run)")
    ap.add_argument("--workspace", help="workspace id (default: the default workspace)")
    ap.add_argument("--probe", action="store_true",
                    help=f"target the kept P0 probe gateway {PROBE_GATEWAY} (M1)")
    args = ap.parse_args(argv)
    name = PROBE_GATEWAY if args.probe else gb.gateway_name()
    if not target_allowed(name, probe=args.probe):
        print(f"refused: {name!r} is not a platform gateway", file=sys.stderr)
        return 2
    control = control_client(_workspace(args.workspace))
    matches = []
    token = None
    while True:
        page = control.list_gateways(maxResults=100, **({"nextToken": token} if token else {}))
        matches += [g for g in page.get("items", []) if g.get("name") == name]
        token = page.get("nextToken")
        if not token:
            break
    if len(matches) != 1:
        print(f"expected exactly one gateway named {name!r}, found {len(matches)}",
              file=sys.stderr)
        return 2
    try:
        report = gb.repair_exception_level(control, matches[0]["gatewayId"], apply=args.apply)
    except RuntimeError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
