"""Isolated Claude Code / Codex judge-client worker.

Runs in its own process so target and judge exec globals cannot race. It reads
a trusted JSON request, applies the judge exec configuration in this process,
and calls the restricted exec runner with its fail-closed per-call policy. Its
working directory holds only trusted request/schema files and backend traces;
it never receives the rollout or evidence directories as an added directory --
the only path to evidence is the required networkless Artifact MCP server named
in the policy.

The worker prints exactly one JSON object:

    {"response": "...", "usage": {"input": 0, "output": 0}}
"""
from __future__ import annotations

import json
import sys
from typing import Any

from skillopt.model.backend_config import (
    configure_claude_code_exec,
    configure_codex_exec,
)
from skillopt.model.codex_harness import (
    extract_exec_usage,
    run_claude_code_exec,
    run_codex_exec,
)


def _select_runner(backend: str):
    # Resolved through the module namespace (not a captured dict) so tests can
    # monkeypatch ``run_claude_code_exec`` / ``run_codex_exec``.
    if backend == "claude_code_exec":
        return run_claude_code_exec
    if backend == "codex_exec":
        return run_codex_exec
    return None


def _apply_judge_config(request: dict) -> None:
    """Apply judge exec configuration in this worker process only.

    Judge mode always uses the CLI transport so every policy field is
    expressible; the fail-closed per-call ``policy`` (not these globals) drives
    the restricted command, so we only pin the transport and effort here.
    """
    backend = str(request.get("backend", ""))
    effort = str(request.get("effort", "") or "") or None
    if backend == "claude_code_exec":
        configure_claude_code_exec(use_sdk="cli", effort=effort)
    elif backend == "codex_exec":
        configure_codex_exec(use_sdk="cli", reasoning_effort=effort)


def run_worker_request(request: dict) -> dict[str, Any]:
    """Run one judge request and return ``{"response", "usage"}``."""
    backend = str(request.get("backend", ""))
    runner = _select_runner(backend)
    if runner is None:
        raise ValueError(f"unsupported judge backend: {backend!r}")
    policy = request.get("backend_policy")
    if not isinstance(policy, dict) or policy.get("judge") is not True:
        raise ValueError("judge worker request is missing a judge backend policy")

    _apply_judge_config(request)
    response, raw = runner(
        work_dir=request["judge_client_dir"],
        prompt=request["prompt"],
        model=request.get("model", ""),
        timeout=int(request["timeout"]),
        images=[],
        data_dirs=[],
        policy=policy,
    )
    usage = extract_exec_usage(raw) or {}
    input_tokens = (
        int(usage.get("input", 0) or 0)
        + int(usage.get("cache_read", 0) or 0)
        + int(usage.get("cache_write", 0) or 0)
    )
    return {
        "response": str(response or ""),
        "usage": {"input": input_tokens, "output": int(usage.get("output", 0) or 0)},
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        with open(args[0], "r", encoding="utf-8") as handle:
            request = json.load(handle)
    else:
        request = json.load(sys.stdin)
    result = run_worker_request(request)
    sys.stdout.write(json.dumps({"response": result["response"], "usage": result["usage"]}))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
