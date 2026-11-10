"""Preview or reset only this run's synthetic tickets; keep accounts and Agents.

``reset_student.py`` imports ``ticket_reset_plan`` / ``reset_tickets`` for its ticket step, so
the module does nothing at import time beyond reading the lab config.
"""

import argparse
import json
import shlex

from lab import (
    CONFIG,
    LEGACY_CONFIG_DIGEST,
    NAME,
    SCENARIO,
    legacy_config_digest,
    load,
    remote,
    run_marker_guard,
)

EXPECTED_PATH = "/srv/launchpad-demo/scenarios/" + SCENARIO
PRESERVED = [
    "EC2",
    "accounts",
    "Agents",
    "chat",
    "memory",
    "KB documents (including previously published articles)",
]


def service_unit(state) -> str:
    """The systemd unit of this run's synthetic service, after the ownership checks."""
    service = state["service"]
    if service["host"] != CONFIG["tools_host"] or service["path"] != EXPECTED_PATH:
        raise SystemExit("Service ownership does not match this run")
    # N17-obs536 (same class as N17-obs524): an environment installed under another naming
    # scheme records its real systemd unit in the manifest; prefer it over the derived NAME so
    # the stop / start pair acts on the live service instead of failing with "Unit ... not
    # loaded".
    unit = str(service.get("unit") or NAME + ".service").removesuffix(".service")
    if not unit:
        raise SystemExit("Service unit name is empty; fix the manifest before resetting")
    return unit


def ticket_reset_plan(state=None) -> dict:
    """What ``reset_tickets`` touches and what it preserves (read-only)."""
    state = state or load()
    return {
        "reset": state["service"],
        "unit": service_unit(state) + ".service",
        "preserve": list(PRESERVED),
    }


def reset_tickets(state=None) -> dict:
    """Stop the service, run ``service.py --reset`` on the tools host, start it again."""
    state = state or load()
    unit = shlex.quote(service_unit(state) + ".service")
    path = shlex.quote(EXPECTED_PATH)
    # N17-obs559: accept (and migrate) a pre-obs557 run marker; explain a mismatch on stdout
    guard = run_marker_guard(
        path, legacy_digests=(LEGACY_CONFIG_DIGEST, legacy_config_digest(state.get("model_id"))),
    )
    return remote(
        f"""set -eu
UNIT={unit}
test -n "$UNIT" || {{ echo "empty service unit name" >&2; exit 64; }}
{guard}
trap 'systemctl start "$UNIT"' EXIT
systemctl stop "$UNIT"
runuser -u {CONFIG["tools_user"]} -- /usr/bin/python3 {path}/service.py --reset
""",
        "reset-business",
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    state = load()
    print(json.dumps(ticket_reset_plan(state), indent=2))
    if args.apply:
        reset_tickets(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
