"""Preview or reset only this run's synthetic tickets; keep accounts and Agents."""

import argparse
import json
import shlex

from lab import CONFIG, CONFIG_DIGEST, NAME, SCENARIO, load, remote

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--apply", action="store_true")
args = parser.parse_args()
state = load()
service = state["service"]
expected_path = "/srv/launchpad-demo/scenarios/" + SCENARIO
if service["host"] != CONFIG["tools_host"] or service["path"] != expected_path:
    raise SystemExit("Service ownership does not match this run")
print(
    json.dumps(
        {
            "reset": service,
            "preserve": [
                "EC2",
                "accounts",
                "Agents",
                "chat",
                "memory",
                "KB documents (including previously published articles)",
            ],
        },
        indent=2,
    )
)
if args.apply:
    path = shlex.quote(expected_path)
    remote(
        f"""set -eu
test "$(cat {path}/.scenario)" = {shlex.quote(SCENARIO)}
test "$(cat {path}/.run)" = {shlex.quote(CONFIG_DIGEST)}
trap 'systemctl start {NAME}.service' EXIT
systemctl stop {NAME}.service
runuser -u {CONFIG["tools_user"]} -- /usr/bin/python3 {path}/service.py --reset
""",
        "reset-business",
    )
