"""The region-boundary gate script must pass on the tree and catch an unmarked literal."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "region_boundary_check.py"


def test_tree_has_no_unmarked_non_china_region_literal():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "only Global Bedrock model inference crosses the partition" in proc.stdout


def test_marked_exceptions_are_only_bedrock_or_reasoned_allows():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, check=False
    )
    allowed = [line for line in proc.stdout.splitlines() if line.startswith("   allowed")]
    assert allowed, "expected the model-region defaults to be listed as marked exceptions"
    for line in allowed:
        assert ("global-bedrock" in line) or ("allow — " in line), line
