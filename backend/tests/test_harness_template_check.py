"""G(N7a): the self-check script passes on the fixture template and fails loudly otherwise."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "harness_template_check.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=ROOT / "backend", capture_output=True, text=True
    )


def test_script_passes_on_the_fixture_template():
    proc = _run("--template", str(ROOT / "backend" / "tests" / "fixtures" / "harness_runtime_min"))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "harness_template_check: OK" in proc.stdout
    assert "7 files" in proc.stdout and "anchors A1–A12" in proc.stdout


def test_script_fails_on_a_missing_template(tmp_path):
    proc = _run("--template", str(tmp_path / "absent"))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "harness_template_check: FAIL (1 problem(s))" in proc.stdout
    assert "harness-template.md" in proc.stdout
