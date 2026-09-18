"""Text judge evidence must include complete ordinary reports and rewritten inputs."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

VENDOR = Path(__file__).parents[2] / "vendor" / "skillopt"


def _evidence(tmp_path: Path, mode: str, artifacts: list[dict]) -> str:
    """Load the vendored text formatter in a subprocess without optional judge SDKs."""
    probe = textwrap.dedent(
        """
        import importlib.util
        import json
        import sys
        import types

        agentic = types.ModuleType("skillopt.envs.skilleval.agentic_judge")
        agentic.AgenticJudgeConfig = object
        agentic.run_agentic_judge = None
        sys.modules[agentic.__name__] = agentic
        model = types.ModuleType("skillopt.model")
        model.chat_optimizer = None
        sys.modules[model.__name__] = model
        spec = importlib.util.spec_from_file_location("text_judge", sys.argv[1])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        item = {
            "id": "report", "question": "Rewrite the report", "rubric": "Use actual evidence",
            "files": {"input.log": "input", "report.md": "old"},
        }
        result = {
            "id": "report", "work_dir": sys.argv[2], "response": "The report is saved.",
            "artifacts": json.loads(sys.argv[4]),
        }
        captured = []
        def judge(item, response, evidence):
            captured.append(evidence)
            return {"hard": 1, "soft": 1.0}
        if sys.argv[3] == "legacy":
            module.merge_scores([item], [result], judge)
        else:
            module.evaluate_rollouts(
                [item], [result], state_hash="seed", out_root=sys.argv[2],
                judge_config=None, chat_judge=judge,
            )
        print(json.dumps(captured[0]))
        """
    )
    proc = subprocess.run(
        [
            sys.executable, "-c", probe,
            str(VENDOR / "skillopt/envs/skilleval/evaluator.py"),
            str(tmp_path), mode, json.dumps(artifacts),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


@pytest.mark.parametrize("mode", ["legacy", "routed"])
def test_text_judge_reads_rewritten_seeded_report_beyond_old_preview(tmp_path, mode):
    (tmp_path / "input.log").write_text("UNCHANGED_INPUT_MUST_NOT_BE_EVIDENCE")
    report = "# Report\n" + "Measured facts.\n" * 200 + "\nNEXT_ACTIONS_AT_END"
    (tmp_path / "report.md").write_text(report)

    evidence = _evidence(tmp_path, mode, [{"path": "report.md", "change": "modified"}])

    assert report in evidence
    assert "NEXT_ACTIONS_AT_END" in evidence
    assert "UNCHANGED_INPUT_MUST_NOT_BE_EVIDENCE" not in evidence
    assert "(truncated:" not in evidence


def test_text_judge_does_not_treat_unchanged_seeded_report_as_output(tmp_path):
    (tmp_path / "report.md").write_text("STALE_REPORT_BODY_MUST_NOT_BE_EVIDENCE")

    evidence = _evidence(tmp_path, "routed", [])

    assert "STALE_REPORT_BODY_MUST_NOT_BE_EVIDENCE" not in evidence


def test_text_judge_marks_truncation_and_keeps_large_output_bounded(tmp_path):
    (tmp_path / "report.md").write_text("x" * 16000 + "UNREAD_TAIL")

    evidence = _evidence(tmp_path, "routed", [{"path": "report.md"}])

    assert "truncated: first 16000 chars" in evidence
    assert "UNREAD_TAIL" not in evidence
    assert len(evidence) < 17000


def test_text_judge_bounds_total_evidence_across_files(tmp_path):
    artifacts = []
    for index in range(5):
        name = f"output-{index}.md"
        (tmp_path / name).write_text(chr(ord("A") + index) * 16000)
        artifacts.append({"path": name})

    evidence = _evidence(tmp_path, "routed", artifacts)

    assert "more file(s) not shown" in evidence
    assert "E" * 100 not in evidence
    assert len(evidence) < 66000
