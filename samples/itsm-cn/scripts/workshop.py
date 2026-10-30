"""Explicit workshop preparation stages. Run with LAUNCHPAD_LAB_CONFIG set."""

import argparse
import fcntl
import json
import subprocess
import sys
from pathlib import Path

from lab import CONFIG, ROOT, STAFF, initialize, load

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "stage", choices=("plan", "console", "tools", "policy", "staff", "manual", "all")
)
parser.add_argument(
    "--all-staff", action="store_true",
    help="staff stage: deploy a reference Agent for every staff account, not only the first",
)
args = parser.parse_args()
if args.stage == "plan":
    print(
        json.dumps(
            {
                "config": CONFIG,
                "existing_state": (ROOT / "manifest.json").exists(),
                "operations": [
                    f"create 10 learner accounts + {len(STAFF)} staff account(s): "
                    + ", ".join(STAFF),
                    "upload KB/skills/datasets",
                    "install synthetic service on the specified existing host",
                    "create isolated Lambda network and Gateway",
                    "publish workshop policies",
                ],
                "does_not_create": [
                    "EC2 host",
                    "learner Agents",
                    "chat history or evaluation results",
                ],
            },
            indent=2,
        )
    )
    load()
else:
    initialize()
    lock = (ROOT / "prepare.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    stages = (
        ("console", "tools", "policy", "staff", "manual") if args.stage == "all" else (args.stage,)
    )
    for stage in stages:
        if stage == "staff":
            for user in STAFF if args.all_staff else STAFF[:1]:
                subprocess.run(
                    [sys.executable, str(Path(__file__).with_name("agents.py")), user, "3"],
                    check=True,
                )
            subprocess.run(
                [sys.executable, str(Path(__file__).with_name("refresh_callers.py"))], check=True
            )
            continue
        if stage == "tools":
            subprocess.run(
                [sys.executable, str(Path(__file__).with_name("prepare_network.py"))], check=True
            )
        filename = {
            "console": "prepare_console.py",
            "tools": "deploy_tools.py",
            "policy": "prepare_policy.py",
            "manual": "build_manual.py",
        }[stage]
        subprocess.run([sys.executable, str(Path(__file__).with_name(filename))], check=True)
        if stage == "tools":
            # Roll service code + staff environment (and the Lambda bridge) on every run.
            subprocess.run(
                [sys.executable, str(Path(__file__).with_name("refresh_service.py"))], check=True
            )
