#!/usr/bin/env python
"""Per-user policy stream B3: attach (or detach) the gateway policy interceptor Lambda.

    cd backend && uv run python scripts/attach_policy_interceptor.py                 # dry run
    cd backend && uv run python scripts/attach_policy_interceptor.py --apply         # attach
    cd backend && uv run python scripts/attach_policy_interceptor.py --detach --apply
    cd backend && uv run python scripts/attach_policy_interceptor.py --probe ...     # M1

Attaching is an ``UpdateGateway`` on a live resource: run ``--apply`` on ``launchpad-gw-t1`` /
``launchpad-gw`` ONLY with the user's explicit go at that moment (design §15.5). The update
carries every field ``GetGateway`` reports and changes only ``interceptorConfigurations``,
then reads the gateway back and exits 1 if anything else changed
(``gateway_bootstrap.set_interceptor``). Attach refuses a gateway still on
``exceptionLevel: DEBUG`` (N17-obs474) and a slot held by another interceptor.

Targets: this host's platform gateway (``gateway_name()``), or with ``--probe`` the kept P0
probe gateway ``launchpad-probe-gw`` (milestone M1; there ``--replace-probe`` swaps out the P0
recorder ``launchpad-probe-interceptor``). The function is the ``policy-interceptor`` stage's
(``policy_interceptor_function_arn`` in the resource map) or ``--function-arn``. Removal
order: always ``--detach`` before the function is ever deleted.
"""

import argparse
import json
import re
import sys

from app.services import gateway_bootstrap as gb
from app.services.agentcore.client import control_client

_PLATFORM_GATEWAY = re.compile(r"^launchpad-gw(-[a-z0-9]+)?$")
PROBE_GATEWAY = "launchpad-probe-gw"
PROBE_RECORDER = re.compile(r"^arn:aws[a-z-]*:lambda:[a-z0-9-]+:\d{12}:function:"
                            r"launchpad-probe-interceptor$")
_FUNCTION = re.compile(r"^arn:aws[a-z-]*:lambda:[a-z0-9-]+:\d{12}:function:"
                       r"launchpad-policy-interceptor(-[a-z0-9]+)?$")


def target_allowed(name: str, *, probe: bool) -> bool:
    if probe:
        return name == PROBE_GATEWAY
    return bool(_PLATFORM_GATEWAY.match(name or ""))


def _gateway_id(control, name: str) -> str:
    matches, token = [], None
    while True:
        page = control.list_gateways(maxResults=100, **({"nextToken": token} if token else {}))
        matches += [g for g in page.get("items", []) if g.get("name") == name]
        token = page.get("nextToken")
        if not token:
            break
    if len(matches) != 1:
        raise SystemExit(f"expected exactly one gateway named {name!r}, found {len(matches)}")
    return matches[0]["gatewayId"]


def _recorder(gateway_id: str, function_arn: str):
    """Persist the verified attach state in the hub ledger (``policy_gateway_state``) — the
    state stream A's runtime-guard disarm reads."""
    def record(attached: bool) -> None:
        from app.core.db import SessionLocal, engine
        from app.models.policy import PolicyGatewayAttachment
        from app.services import policy_gateway_state

        # only this table (the running service owns every other migration)
        PolicyGatewayAttachment.__table__.create(bind=engine, checkfirst=True)
        with SessionLocal() as db:
            if attached:
                policy_gateway_state.record_attach(db, gateway_id, function_arn)
            else:
                policy_gateway_state.record_detach(db, gateway_id)
    return record


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="perform the update (default: dry run)")
    ap.add_argument("--detach", action="store_true", help="remove the interceptor instead")
    ap.add_argument("--probe", action="store_true", help=f"target {PROBE_GATEWAY} (M1)")
    ap.add_argument("--replace-probe", action="store_true",
                    help="with --probe: detach the P0 recorder first")
    ap.add_argument("--function-arn", help="default: the policy-interceptor stage's function")
    args = ap.parse_args(argv)
    from app.core.config import get_settings
    from app.services.workspace import default_workspace_context

    name = PROBE_GATEWAY if args.probe else gb.gateway_name()
    if not target_allowed(name, probe=args.probe):
        print(f"refused: {name!r} is not a platform gateway", file=sys.stderr)
        return 2
    function_arn = args.function_arn or str(
        (get_settings().resources or {}).get("policy_interceptor_function_arn") or "")
    if not _FUNCTION.match(function_arn):
        print(f"refused: {function_arn!r} is not a launchpad-policy-interceptor function "
              "(run the policy-interceptor bootstrap stage or pass --function-arn)",
              file=sys.stderr)
        return 2
    control = control_client(default_workspace_context())
    gateway_id = _gateway_id(control, name)
    try:
        if args.probe and args.replace_probe and not args.detach:
            current = control.get_gateway(gatewayIdentifier=gateway_id)
            for entry in current.get("interceptorConfigurations") or []:
                arn = ((entry.get("interceptor") or {}).get("lambda") or {}).get("arn") or ""
                if PROBE_RECORDER.match(arn):
                    print(json.dumps(gb.set_interceptor(control, gateway_id, arn, attach=False,
                                                        apply=args.apply), indent=2, default=str))
        report = gb.set_interceptor(control, gateway_id, function_arn, attach=not args.detach,
                                    apply=args.apply,
                                    record=_recorder(gateway_id, function_arn))
    except RuntimeError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
