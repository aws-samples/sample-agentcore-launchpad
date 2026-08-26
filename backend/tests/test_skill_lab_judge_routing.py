"""Judge routing: the derived judge_mode must not survive an import as an explicit one.

Slice 0 of `08-26-skill-lab-judge-route-readiness`. A live prod evaluation submitted with
`judge_mode: chat` escalated to the agentic judge anyway and failed with
`FileNotFoundError: 'claude'`. The mechanism, confirmed by probe:

`load_tasks` writes a derived `judge_mode: "auto"` into every item. `import_taskgen_taskset`
saves those normalized items verbatim, so the stored document *contains* a `judge_mode`
field — and on the next load the loader recomputes `_judge_mode_explicit` from the field's
presence, promoting a default nobody chose into an explicit per-task choice. Vendored
`evaluator.should_use_agentic` then lets it outrank the run-level mode, so `chat` is ignored.

The vendored half of that rule is asserted through `dataloader` only: `evaluator` imports
`openpyxl`, which the platform venv deliberately does not carry (that dependency lives in the
skill-lab venv and the worker image).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app.skill_lab import jobs, runner

VENDOR = Path(__file__).parents[2] / "vendor" / "skillopt"
RAW_TASK = {"id": "t1", "question": "build a workbook", "rubric": "PASS when it exists"}


def _load_tasks(tmp_path: Path, items: list[dict], name: str) -> list[dict]:
    """`load_tasks(items)` in a subprocess — the vendored boundary is subprocess-only."""
    source = tmp_path / name
    source.write_text(json.dumps(items), encoding="utf-8")
    probe = tmp_path / f"probe_{name}.py"
    probe.write_text(
        textwrap.dedent(
            """
            import json, sys, types
            sys.path.insert(0, sys.argv[1])
            openai = types.ModuleType("openai")
            openai.OpenAI = object
            openai.AzureOpenAI = object
            sys.modules.setdefault("openai", openai)
            from skillopt.envs.skilleval.dataloader import load_tasks
            print(json.dumps(load_tasks(sys.argv[2])))
            """
        )
    )
    proc = subprocess.run(
        [sys.executable, str(probe), str(VENDOR), str(source)],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        check=True,
    )
    return json.loads(proc.stdout)


def test_loader_promotes_a_saved_derived_judge_mode_to_an_explicit_one(tmp_path):
    """The vendored mechanism itself, pinned so a future loader change is visible."""
    normalized = _load_tasks(tmp_path, [RAW_TASK], "raw.json")
    assert normalized[0]["judge_mode"] == "auto"
    assert normalized[0]["_judge_mode_explicit"] is False

    # Exactly what saving the normalized items and reading them back does.
    reloaded = _load_tasks(tmp_path, normalized, "roundtrip.json")
    assert reloaded[0]["judge_mode"] == "auto"
    assert reloaded[0]["_judge_mode_explicit"] is True  # the promotion


def test_a_genuinely_declared_judge_mode_still_reads_as_explicit(tmp_path):
    """Control: the fix must not stop a task that really did choose a mode."""
    declared = _load_tasks(tmp_path, [dict(RAW_TASK, judge_mode="agentic")], "declared.json")
    assert declared[0]["judge_mode"] == "agentic"
    assert declared[0]["_judge_mode_explicit"] is True


@pytest.mark.xfail(
    strict=True,
    reason="slice 4 of 08-26-skill-lab-judge-route-readiness: jobs.strip_derived_task_fields "
    "does not exist yet. strict=True so this flips to a hard failure the moment the fix "
    "lands and the marker is forgotten.",
)
@pytest.mark.parametrize("derived_field", ("judge_mode", "_judge_mode_explicit"))
def test_taskgen_import_does_not_persist_a_derived_judge_mode(tmp_path, derived_field):
    """What the platform stores must look like an authored document.

    Expected to fail until slice 4: `import_taskgen_taskset` saves the loader's
    normalized items verbatim, so a mode nobody chose is stored and comes back
    explicit.
    """
    generated = _load_tasks(tmp_path, [RAW_TASK], "generated.json")
    assert derived_field in generated[0], "probe assumption changed"

    stored = jobs.strip_derived_task_fields(generated)

    assert derived_field not in stored[0]
    assert stored[0]["question"] == RAW_TASK["question"]
    assert _load_tasks(tmp_path, stored, "stored.json")[0]["_judge_mode_explicit"] is False


# ── routing flags are emitted for every mode ───────────────────────────────


def _judge_argv(mode: str, judge_model: str | None = None) -> list[str]:
    params = runner.clamp_params(
        {"judge_mode": mode, **({"judge_model": judge_model} if judge_model else {})}
    )
    command = runner.build_eval_command(
        skill_dir=Path("/tmp/skill"),
        tasks_file=Path("/tmp/tasks.json"),
        out_dir=Path("/tmp/out"),
        params=params,
    )
    return command


@pytest.mark.parametrize("mode", ("chat", "auto", "agentic"))
def test_routing_flags_are_passed_for_every_judge_mode(mode):
    """An escalation can happen under any mode, so the route must always be stated.

    Before the fix `chat` emitted `--judge_mode chat` and nothing else, leaving a
    per-task escalation on the vendored default backend.
    """
    argv = _judge_argv(mode)
    text = " ".join(argv)
    assert f"--judge_mode {mode}" in text
    for flag in ("--judge_exec_backend", "--judge_exec_model", "--judge_exec_effort",
                 "--judge_sandbox_command"):
        assert flag in argv, f"{flag} missing for mode={mode}"


@pytest.mark.parametrize("mode", ("chat", "auto", "agentic"))
def test_an_openai_family_judge_never_resolves_to_the_claude_cli(mode):
    argv = _judge_argv(mode, judge_model="us.openai.gpt-5.6-sol")
    backend = argv[argv.index("--judge_exec_backend") + 1]
    model = argv[argv.index("--judge_exec_model") + 1]
    assert backend == "codex_exec"
    # the inference-profile prefix is stripped: codex resolves via ~/.codex
    assert model == "openai.gpt-5.6-sol"


@pytest.mark.parametrize("mode", ("chat", "auto", "agentic"))
def test_a_non_openai_judge_keeps_the_claude_cli_and_the_full_model_id(mode):
    argv = _judge_argv(mode, judge_model="global.anthropic.claude-opus-5")
    assert argv[argv.index("--judge_exec_backend") + 1] == "claude_code_exec"
    assert argv[argv.index("--judge_exec_model") + 1] == "global.anthropic.claude-opus-5"


def test_the_train_path_states_the_route_for_chat_too():
    env = runner._train_judge_env(runner.clamp_train_params({"judge_mode": "chat"}))
    assert env["judge_backend"] == "codex_exec"
    assert set(env) == {"judge_backend", "judge_model", "judge_effort", "judge_sandbox_command"}


def test_chat_still_produces_chat_verdicts_when_the_route_is_stated(tmp_path):
    """The blast radius of always passing the flags: if the vendored gate keyed on
    flag PRESENCE rather than on the mode, this change would silently make every
    chat run agentic. It keys on the mode — pinned here against the real vendored
    resolver, with the parser dependencies stubbed (they live in the skill-lab
    venv and the worker image, not the platform venv)."""
    probe = tmp_path / "gate.py"
    probe.write_text(
        textwrap.dedent(
            """
            import json, sys
            from unittest.mock import MagicMock
            sys.path.insert(0, sys.argv[1])

            class Pkg(MagicMock):
                def __getattr__(self, item):
                    child = super().__getattr__(item)
                    sys.modules.setdefault(f"{self._mock_name}.{item}", child)
                    return child

            for name in ("openai", "openpyxl", "PIL", "pypdf", "docx", "pptx", "fitz", "mcp"):
                sys.modules[name] = Pkg(name=name)
            for name in ("openpyxl.utils", "openpyxl.utils.cell", "openpyxl.utils.datetime",
                         "openpyxl.styles", "openpyxl.worksheet",
                         "openpyxl.worksheet.worksheet", "PIL.Image", "PIL.ImageFile",
                         "mcp.server", "mcp.server.fastmcp"):
                sys.modules[name] = MagicMock(name=name)

            from skillopt.envs.skilleval.agentic_judge import AgenticJudgeConfig
            from skillopt.envs.skilleval.evaluator import should_use_agentic

            artifact = {"artifacts": [{"path": "r.xlsx", "size": 10, "change": "created"}]}
            derived = {"judge_mode": "auto", "_judge_mode_explicit": False}
            print(json.dumps({
                mode: should_use_agentic(
                    derived,
                    artifact,
                    AgenticJudgeConfig(mode=mode, backend="codex_exec",
                                       model="openai.gpt-5.6-sol"),
                )
                for mode in ("chat", "auto", "agentic")
            }))
            """
        )
    )
    proc = subprocess.run(
        [sys.executable, str(probe), str(VENDOR)],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        check=True,
    )
    gate = json.loads(proc.stdout)
    assert gate == {"chat": False, "auto": True, "agentic": True}, gate


# ── readiness follows the route, not a fixed CLI ───────────────────────────


def _status(client, present: set[str], judge_model: str, monkeypatch) -> dict:
    """Status with only `present` binaries resolvable on this host."""
    import shutil

    from app.skill_lab import routers as skill_lab_routers

    monkeypatch.setattr(
        shutil, "which", lambda binary: f"/usr/bin/{binary}" if binary in present else None
    )
    settings = skill_lab_routers.get_settings()
    monkeypatch.setattr(
        skill_lab_routers,
        "get_settings",
        lambda: settings.model_copy(update={"skill_lab_judge_model_id": judge_model}),
    )
    response = client.get("/api/skill-lab/status")
    assert response.status_code == 200, response.text
    return response.json()


def test_readiness_is_false_for_the_route_whose_cli_is_absent(client, monkeypatch):
    """The prod shape: bwrap + codex present, claude absent. Before this slice the
    status reported the agentic judge ready and every artifact task still died."""
    body = _status(client, {"bwrap", "codex"}, "global.anthropic.claude-opus-5", monkeypatch)
    assert body["agentic_judge_ready"] is True  # the shared prerequisite does hold
    assert body["judge_codex_ready"] is True
    assert body["judge_claude_ready"] is False
    # …but the configured judge model routes to claude, so the honest answer is no.
    assert body["judge_cli_ready"] is False


def test_readiness_is_true_when_the_routed_cli_is_present(client, monkeypatch):
    body = _status(client, {"bwrap", "codex"}, "us.openai.gpt-5.6-sol", monkeypatch)
    assert body["judge_cli_ready"] is True
    assert body["judge_claude_ready"] is False  # irrelevant for this route


def test_the_shared_prerequisite_is_reported_separately(client, monkeypatch):
    """A host with both CLIs but no sandbox launcher: per-CLI probes say yes, the
    shared one says no. Keeping them separate is what lets the wizard name the
    actual missing piece."""
    body = _status(client, {"claude", "codex"}, "us.openai.gpt-5.6-sol", monkeypatch)
    assert body["agentic_judge_ready"] is False
    assert body["judge_codex_ready"] is True and body["judge_claude_ready"] is True
    assert body["judge_cli_ready"] is True


def test_judge_cli_binary_matches_the_route():
    assert runner.judge_cli_binary("us.openai.gpt-5.6-sol") == "codex"
    assert runner.judge_cli_binary("global.anthropic.claude-opus-5") == "claude"
    assert set(runner.JUDGE_CLI_BINARY) == set(runner.TARGET_BACKENDS)


# ── a missing judge CLI is an actionable prerequisite, not a mystery row ────


def _write_results(tmp_path, monkeypatch, rows: list[dict]) -> dict:
    from app.skill_lab import artifacts

    monkeypatch.setattr(artifacts, "JOBS_DIR", tmp_path / "jobs")
    out = artifacts.out_root("job_x")
    out.mkdir(parents=True)
    (out / "results.json").write_text(json.dumps(rows), encoding="utf-8")
    result = artifacts.eval_results("job_x")
    assert result is not None
    return result


CLI_CRASH = (
    "judge worker exited 1: Traceback (most recent call last):\n"
    "  File \"…/codex_harness.py\", line 1160, in _run_claude_code_judge_cli\n"
    "FileNotFoundError: [Errno 2] No such file or directory: 'claude'"
)


def test_a_missing_judge_cli_is_named_on_the_row_and_summarised_once(tmp_path, monkeypatch):
    """The prod shape: two invalid rows whose only cause was a stack trace."""
    result = _write_results(
        tmp_path,
        monkeypatch,
        [
            {"id": "t1", "score_valid": False, "hard": 0, "judge_status": "evaluation_error",
             "judge_error": CLI_CRASH},
            {"id": "t2", "score_valid": False, "hard": 0, "judge_status": "evaluation_error",
             "judge_error": CLI_CRASH},
        ],
    )
    assert [row["judge_prerequisite"] for row in result["rows"]] == ["claude", "claude"]
    # Stated once for the run, not per row.
    assert result["summary"]["judge_prerequisite_missing"] == ["claude"]
    assert result["summary"]["invalid"] == 2


def test_an_absolute_cli_path_is_reduced_to_the_binary_name(tmp_path, monkeypatch):
    result = _write_results(
        tmp_path,
        monkeypatch,
        [{"id": "t1", "score_valid": False,
          "judge_error": "FileNotFoundError: [Errno 2] No such file or directory: "
                         "'/usr/local/bin/codex'"}],
    )
    assert result["rows"][0]["judge_prerequisite"] == "codex"
    assert result["summary"]["judge_prerequisite_missing"] == ["codex"]


@pytest.mark.parametrize(
    "judge_error",
    (
        # A file the judge itself could not find is NOT a host prerequisite.
        "FileNotFoundError: [Errno 2] No such file or directory: '/tmp/evidence/report.xlsx'",
        "ValueError: judge returned no verdict",
        "",
        None,
    ),
)
def test_an_unrelated_judge_failure_is_not_reported_as_a_prerequisite(
    tmp_path, monkeypatch, judge_error
):
    result = _write_results(
        tmp_path, monkeypatch, [{"id": "t1", "score_valid": False, "judge_error": judge_error}]
    )
    assert result["rows"][0]["judge_prerequisite"] is None
    assert result["summary"]["judge_prerequisite_missing"] == []


def test_a_healthy_run_carries_no_prerequisite_noise(tmp_path, monkeypatch):
    result = _write_results(
        tmp_path, monkeypatch, [{"id": "t1", "score_valid": True, "hard": 1, "soft": 1.0}]
    )
    assert result["rows"][0]["judge_prerequisite"] is None
    assert result["summary"]["judge_prerequisite_missing"] == []
    assert result["summary"]["passed"] == 1
