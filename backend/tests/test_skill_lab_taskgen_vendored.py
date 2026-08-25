"""Vendored taskgen generator: attachment materialization, prompt, validation.

Runs the real `vendor/skillopt/scripts/generate_tasks.py` in a subprocess with only
the exec backend stubbed, so the contract these tests pin is the one the worker
actually executes. Kept out of the platform process because the vendored boundary
is subprocess-only.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

VENDOR = Path(__file__).parents[2] / "vendor" / "skillopt"
SCRIPT = VENDOR / "scripts" / "generate_tasks.py"

# What the stubbed agent writes back, per scenario. `RECORD` also dumps the prompt
# and a listing of its working directory so the tests can assert on both.
AGENT_STUB = '''
import json, pathlib
EVIDENCE = pathlib.Path(%(evidence)r)
TASKS = json.loads(%(tasks)r)

def _record(work_dir, prompt):
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "prompt.txt").write_text(prompt, encoding="utf-8")
    listing = sorted(
        str(path.relative_to(work_dir))
        for path in pathlib.Path(work_dir).rglob("*")
        if path.is_file()
    )
    (EVIDENCE / "workdir.json").write_text(json.dumps(listing), encoding="utf-8")
    copies = {
        str(path.relative_to(work_dir)): path.read_bytes().hex()
        for path in pathlib.Path(work_dir).rglob("*")
        if path.is_file()
    }
    (EVIDENCE / "bytes.json").write_text(json.dumps(copies), encoding="utf-8")
    (pathlib.Path(work_dir) / "generated_tasks.json").write_text(
        json.dumps(TASKS), encoding="utf-8"
    )
    return "stub response", "raw"

def run_claude_code_exec(**kwargs):
    return _record(kwargs["work_dir"], kwargs["prompt"])

def run_codex_exec(**kwargs):
    return _record(kwargs["work_dir"], kwargs["prompt"])
'''


def _run(tmp_path, tasks, attachments=(), count=1, raw_manifest=None):
    """Drive the real CLI; returns (completed_process, evidence_dir, out_root)."""
    skill = tmp_path / "skill"
    skill.mkdir(exist_ok=True)
    (skill / "SKILL.md").write_text("---\nname: xlsx\n---\n# xlsx\nRead workbooks.\n")

    assets = tmp_path / "assets"
    assets.mkdir(exist_ok=True)
    manifest = []
    for name, data, media in attachments:
        digest = hashlib.sha256(data).hexdigest()
        (assets / digest).write_bytes(data)
        manifest.append(
            {"name": name, "media_type": media, "size": len(data), "sha256": digest}
        )
    manifest_path = tmp_path / "attachments.json"
    manifest_path.write_text(
        json.dumps(manifest) if raw_manifest is None else raw_manifest
    )

    evidence = tmp_path / "evidence"
    stub = tmp_path / "agent_stub.py"
    stub.write_text(
        AGENT_STUB % {"evidence": str(evidence), "tasks": json.dumps(tasks)}
    )

    argv = [
        "generate_tasks.py",
        "--skill", str(skill),
        "--count", str(count),
        "--out_root", str(tmp_path / "out"),
    ]
    if attachments or raw_manifest is not None:
        argv += ["--attachments", str(manifest_path), "--attachment-assets", str(assets)]

    driver = tmp_path / "driver.py"
    driver.write_text(
        textwrap.dedent(
            f"""
            import importlib.util, sys, types
            sys.path.insert(0, {str(VENDOR)!r})
            openai = types.ModuleType("openai")
            openai.OpenAI = object
            openai.AzureOpenAI = object
            sys.modules.setdefault("openai", openai)
            spec = importlib.util.spec_from_file_location("agent_stub", {str(stub)!r})
            agent = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(agent)
            # Patch only the two entry points: the real module carries other names
            # that skillopt.envs.skilleval.rollout imports.
            import skillopt.model.codex_harness as harness
            harness.run_claude_code_exec = agent.run_claude_code_exec
            harness.run_codex_exec = agent.run_codex_exec
            sys.argv = {argv!r}
            globals_dict = {{"__name__": "__main__", "__file__": {str(SCRIPT)!r}}}
            exec(compile(open({str(SCRIPT)!r}).read(), {str(SCRIPT)!r}, "exec"), globals_dict)
            """
        )
    )
    proc = subprocess.run(
        [sys.executable, str(driver)],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return proc, evidence, tmp_path / "out"


CSV = b"region,revenue\nAPAC,1240\nEMEA,980\n"
MD = b"# Q2\n\nRevenue up 14%.\n"
ATTACHED = (("rows.csv", CSV, "text/csv"), ("brief.md", MD, "text/markdown"))
DECLARING_TASK = [
    {
        "id": "t1",
        "question": "What is total revenue in data/rows.csv?",
        "rubric": "PASS when the answer is 2220",
        "attachments": ["rows.csv"],
    }
]


def test_attachments_are_materialized_and_declared_through_to_the_output(tmp_path):
    proc, evidence, out_root = _run(tmp_path, DECLARING_TASK, ATTACHED)
    assert proc.returncode == 0, proc.stderr[-2000:]

    # The agent sees the documents at the contract path, byte for byte.
    listing = json.loads((evidence / "workdir.json").read_text())
    assert "data/rows.csv" in listing and "data/brief.md" in listing
    written = json.loads((evidence / "bytes.json").read_text())
    assert bytes.fromhex(written["data/rows.csv"]) == CSV
    assert bytes.fromhex(written["data/brief.md"]) == MD

    # The declaration survives load_tasks into the artifact the platform imports.
    tasks = json.loads((out_root / "generated_tasks.json").read_text())
    assert tasks[0]["attachments"] == ["rows.csv"]
    summary = json.loads((out_root / "gen_summary.json").read_text())
    assert summary["attachments"] == ["rows.csv", "brief.md"]


def test_prompt_states_the_path_contract_and_the_declaration_rule(tmp_path):
    proc, evidence, _ = _run(tmp_path, DECLARING_TASK, ATTACHED)
    assert proc.returncode == 0, proc.stderr[-2000:]
    prompt = (evidence / "prompt.txt").read_text()
    assert "## Attached documents" in prompt
    # Names, sizes and types are all present, and the declaration name is explicit.
    assert "`data/rows.csv` (34 bytes, text/csv)" in prompt
    assert 'declare as "rows.csv"' in prompt
    # The two rules that keep the mapping honest.
    assert '"attachments" (array of strings, optional)' in prompt
    assert "must be declared, never inlined" in prompt


def test_prompt_is_unchanged_when_nothing_is_attached(tmp_path):
    plain = [{"id": "t1", "question": "q", "rubric": "PASS"}]
    proc, evidence, out_root = _run(tmp_path, plain)
    assert proc.returncode == 0, proc.stderr[-2000:]
    prompt = (evidence / "prompt.txt").read_text()
    assert "## Attached documents" not in prompt
    assert "attachments" not in prompt
    assert 'provide it inline via its "files" field.' in prompt
    assert json.loads((out_root / "gen_summary.json").read_text())["attachments"] == []


@pytest.mark.parametrize(
    ("tasks", "attachments", "reason"),
    (
        # A name that was never attached: the platform maps names to verified
        # assets, so this would otherwise become a silently missing input.
        (
            [dict(DECLARING_TASK[0], attachments=["ghost.csv"])],
            ATTACHED,
            "was not attached to this run",
        ),
        # Case must match exactly: the mapping is by name, and two names differing
        # only in case would be ambiguous on a case-insensitive filesystem.
        (
            [dict(DECLARING_TASK[0], attachments=["Rows.csv"])],
            ATTACHED,
            "must match the attached name exactly",
        ),
        ([dict(DECLARING_TASK[0], attachments=["rows.csv", "rows.csv"])], ATTACHED,
         "is declared twice"),
        ([dict(DECLARING_TASK[0], attachments="rows.csv")], ATTACHED,
         '"attachments" must be an array'),
        ([dict(DECLARING_TASK[0], attachments=[""])], ATTACHED,
         "must be non-empty strings"),
        # Declaring anything when the run had no documents at all.
        ([dict(DECLARING_TASK[0], attachments=["rows.csv"])], (),
         "no documents were attached"),
        # Re-inlining an attached document's path duplicates the input.
        (
            [dict(DECLARING_TASK[0], files={"data/rows.csv": "region,revenue\n"})],
            ATTACHED,
            "do not inline them",
        ),
    ),
)
def test_bad_declarations_fail_the_run_with_a_reason_the_agent_can_act_on(
    tmp_path, tasks, attachments, reason
):
    proc, _, _ = _run(tmp_path, tasks, attachments)
    assert proc.returncode != 0
    combined = proc.stdout + proc.stderr
    assert reason in combined, combined[-2000:]
    # The message reaches the agent as retry feedback before the run gives up.
    assert "attempt 2/2" in combined


@pytest.mark.parametrize(
    ("raw_manifest", "reason"),
    (
        ("{}", "must be a non-empty JSON array"),
        ("[]", "must be a non-empty JSON array"),
        ("not json", "invalid --attachments manifest"),
        (json.dumps([{"name": "x/y.csv", "sha256": "0" * 64, "size": 1}]), "unsafe name"),
        (json.dumps([{"name": "a.csv", "sha256": "zz", "size": 1}]), "invalid sha256"),
        (json.dumps([{"name": "a.csv", "sha256": "0" * 64, "size": 1}]), "bytes missing"),
        (
            json.dumps(
                [
                    {
                        "name": "rows.csv",
                        "sha256": hashlib.sha256(CSV).hexdigest(),
                        "size": len(CSV) + 1,
                    }
                ]
            ),
            "size mismatch",
        ),
        (
            json.dumps(
                [
                    {"name": "rows.csv", "sha256": hashlib.sha256(CSV).hexdigest(),
                     "size": len(CSV)},
                    {"name": "ROWS.CSV", "sha256": hashlib.sha256(CSV).hexdigest(),
                     "size": len(CSV)},
                ]
            ),
            "repeats the name",
        ),
    ),
)
def test_manifest_is_decoded_strictly_before_any_model_call(tmp_path, raw_manifest, reason):
    """A broken snapshot must fail loudly up front, not be discovered by the agent."""
    proc, evidence, _ = _run(tmp_path, DECLARING_TASK, ATTACHED, raw_manifest=raw_manifest)
    assert proc.returncode != 0
    combined = proc.stdout + proc.stderr
    assert reason in combined, combined[-1500:]
    # No model call happened at all: the prompt was never built.
    assert not (evidence / "prompt.txt").exists()
