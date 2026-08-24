#!/usr/bin/env python3
"""Validate skilleval task files with the exact loader the CLIs use.

LAUNCHPAD ADDITION (see ../LAUNCHPAD_DEVIATIONS.md): this script does not exist
upstream. The Launchpad backend shells out to it so API-side task-set validation
and evaluate_skill.py/train.py acceptance can never drift — both are
`skillopt.envs.skilleval.dataloader.load_tasks`.

Usage:  python3 scripts/validate_tasks.py <file> [<file> ...]

Prints one JSON object to stdout:

    {"results": [{"path": "...", "ok": true, "count": 3, "error": null}, ...]}

Exit code 0 whenever the validator itself ran (validation failures are data,
carried in `error`); nonzero only on crashes, which callers map to a 500.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.dont_write_bytecode = True  # keep __pycache__ out of the vendored tree

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# dataloader's import chain reaches skillopt.model (via plugin → rollout), whose
# azure_openai module needs the `openai` package at import time — present in the
# skill-lab venv (the production interpreter) but not in a bare interpreter.
# load_tasks itself never calls a model, so a placeholder keeps hermetic tests
# able to run this validator with any interpreter without changing validation
# behavior.
try:  # noqa: SIM105
    import openai  # noqa: F401
except ImportError:  # pragma: no cover - exercised only outside the venv
    import types

    _stub = types.ModuleType("openai")
    _stub.OpenAI = object
    _stub.AzureOpenAI = object
    sys.modules["openai"] = _stub

from skillopt.envs.skilleval.dataloader import load_tasks  # noqa: E402


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets-dir")
    parser.add_argument("files", nargs="+")
    args = parser.parse_args(argv)
    results = []
    for path in args.files:
        try:
            tasks = load_tasks(path, assets_dir=args.assets_dir)
            results.append({"path": path, "ok": True, "count": len(tasks), "error": None})
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            results.append({"path": path, "ok": False, "count": 0, "error": str(exc)})
    json.dump({"results": results}, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
