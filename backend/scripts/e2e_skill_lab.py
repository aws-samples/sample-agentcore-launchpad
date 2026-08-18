"""Skill Lab foundation smoke — REAL AWS, not part of the verify gate.

Prereqs: `make bootstrap` has provisioned the exec worker + skill-lab venv
(resources `skill_lab_worker_*` present in config/launchpad.yaml).

    cd backend && uv run python scripts/e2e_skill_lab.py --ping
    cd backend && uv run python scripts/e2e_skill_lab.py --eval

--ping  sync-invokes the worker runtime with {"action":"ping","versions":true}.
--eval  runs the vendored evaluate_skill.py from the skill-lab venv on a 1-task
        inline skill/taskset: rollout in an AgentCore microVM, judge via the
        bedrock_chat backend. Asserts a valid judged result.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import REPO_ROOT, get_settings  # noqa: E402
from app.services.agentcore.client import data_client  # noqa: E402
from app.services.workspace import default_workspace_context  # noqa: E402
from app.skill_lab import infra  # noqa: E402

SKILL_MD = """---
name: color-facts
description: Answer questions about colors with a one-line fact.
---

# Color facts

When asked about a color, reply with exactly one line of the form
`FACT: <color> — <one short fact>` and also write the same line into a file
named `fact.txt` in the working directory.
"""

TASKS = [
    {
        "id": "task_001",
        "question": "Use the color-facts skill for the color blue.",
        "rubric": (
            "PASS if the response contains a line starting with 'FACT: blue'"
            " AND a file fact.txt exists containing that same line."
        ),
    }
]


def _resources() -> dict:
    ws = default_workspace_context()
    missing = [
        key
        for key in ("skill_lab_worker_runtime_arn", "artifacts_bucket")
        if not ws.resources.get(key)
    ]
    if missing:
        sys.exit(f"missing resources {missing} — run `make bootstrap` first")
    return ws.resources


def do_ping() -> int:
    ws = default_workspace_context()
    resources = _resources()
    client = data_client(ws)
    response = client.invoke_agent_runtime(
        agentRuntimeArn=resources["skill_lab_worker_runtime_arn"],
        qualifier="DEFAULT",
        runtimeSessionId=f"skill-lab-smoke-{uuid.uuid4().hex}",
        payload=json.dumps({"action": "ping", "versions": True}).encode(),
    )
    raw = response.get("response")
    body = raw.read() if hasattr(raw, "read") else raw
    text = body.decode() if isinstance(body, (bytes, bytearray)) else str(body)
    if text.startswith("data:"):
        text = text.strip().splitlines()[-1].removeprefix("data:").strip()
    result = json.loads(text)
    print(json.dumps(result, indent=2))
    if not result.get("ok"):
        sys.exit("worker ping failed")
    print("PING OK")
    return 0


def do_eval() -> int:
    settings = get_settings()
    resources = _resources()
    ws = default_workspace_context()
    python = settings.skill_lab_python
    if not Path(python).exists():
        sys.exit(f"skill-lab venv missing ({python}) — run `make bootstrap`")

    with tempfile.TemporaryDirectory(prefix="skill_lab_e2e_") as tmp:
        root = Path(tmp)
        skill_dir = root / "skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
        tasks_file = root / "tasks.json"
        tasks_file.write_text(json.dumps(TASKS), encoding="utf-8")
        out_root = root / "out"

        env = dict(os.environ)
        env.update(
            {
                # keep __pycache__ out of the vendored tree
                "PYTHONDONTWRITEBYTECODE": "1",
                "SKILLOPT_EXEC_RUNNER": "agentcore",
                "SKILLOPT_AGENTCORE_RUNTIME_ARN": resources["skill_lab_worker_runtime_arn"],
                "SKILLOPT_AGENTCORE_S3_BUCKET": resources["artifacts_bucket"],
                "SKILLOPT_AGENTCORE_S3_PREFIX": infra.EXEC_JOBS_PREFIX,
                "SKILLOPT_AGENTCORE_REGION": ws.region,
                "AWS_REGION": ws.region,
            }
        )
        cmd = [
            python,
            str(REPO_ROOT / "vendor" / "skillopt" / "scripts" / "evaluate_skill.py"),
            "--skill", str(skill_dir),
            "--tasks", str(tasks_file),
            "--out_root", str(out_root),
            "--target_backend", "claude_code_exec",
            "--model", settings.skill_lab_target_model_id,
            "--optimizer_backend", "bedrock_chat",
            "--optimizer_model", settings.skill_lab_judge_model_id,
            "--judge_mode", "chat",
            "--workers", "1",
            "--timeout", "600",
        ]
        print("running:", " ".join(cmd), flush=True)
        proc = subprocess.run(cmd, env=env, cwd=str(REPO_ROOT / "vendor" / "skillopt"))
        if proc.returncode != 0:
            sys.exit(f"evaluate_skill.py exited {proc.returncode}")

        results = json.loads((out_root / "results.json").read_text())
        print(json.dumps(results, indent=2)[:4000])
        row = results[0]
        if row.get("error") or row.get("score_valid") is False:
            sys.exit(
                f"invalid result: error={row.get('error')} "
                f"judge_error={row.get('judge_error')}"
            )
        print(f"EVAL OK · hard={row.get('hard')} soft={row.get('soft')}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ping", action="store_true")
    parser.add_argument("--eval", dest="do_eval", action="store_true")
    args = parser.parse_args()
    if not (args.ping or args.do_eval):
        parser.error("pass --ping and/or --eval")
    if args.ping:
        do_ping()
    if args.do_eval:
        do_eval()
    return 0


if __name__ == "__main__":
    sys.exit(main())
