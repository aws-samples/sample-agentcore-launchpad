"""N12 gate item 2 — the CN evaluation/optimization HTTP contract equals the Global
baseline ``4e27b1f4`` modulo the enumerated delta (plan §A.5, CA-1).

``openapi_baseline_eval.json`` was generated once from ``.worktrees/global-base`` with
``scripts/snapshot_openapi.py`` and is never regenerated without a lead decision — its
digest is pinned below so a silent regeneration fails here.
"""

import difflib
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from app.main import create_app
from tests.contract.ALLOWED_DELTA import ALLOWED_DELTA, PENDING_DELTA, apply_delta

CONTRACT_DIR = Path(__file__).resolve().parent
BASELINE = CONTRACT_DIR / "openapi_baseline_eval.json"
SNAPSHOT_SCRIPT = CONTRACT_DIR.parents[2] / "scripts" / "snapshot_openapi.py"
# sha256 of the committed baseline (E0-3, 2026-10-10). Regenerating it is a contract
# decision: update this constant in the same commit, with the lead's ruling in the message.
BASELINE_SHA256 = "82cdd1ea7a567543700281bf8900bcdb7ced37d97faeb33f986b7244ead28b63"


def _snapshot_module():
    spec = importlib.util.spec_from_file_location("snapshot_openapi", SNAPSHOT_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def snapshot():
    return _snapshot_module()


def _diff(expected: str, actual: str) -> str:
    return "\n".join(difflib.unified_diff(
        expected.splitlines(), actual.splitlines(),
        fromfile="baseline 4e27b1f4 + ALLOWED_DELTA", tofile="CN app", lineterm="", n=3,
    ))


def test_the_baseline_snapshot_is_the_committed_one():
    assert hashlib.sha256(BASELINE.read_bytes()).hexdigest() == BASELINE_SHA256, (
        "openapi_baseline_eval.json changed — it is generated once from 4e27b1f4; "
        "regenerate only on a lead ruling and update BASELINE_SHA256 in the same commit"
    )


def test_cn_eval_contract_equals_the_baseline_modulo_the_enumerated_delta(snapshot):
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    expected = snapshot.render(apply_delta(baseline, ALLOWED_DELTA + PENDING_DELTA))
    actual = snapshot.render(snapshot.eval_slice(create_app().openapi()))
    assert actual == expected, "contract drift (only ALLOWED_DELTA may differ):\n" + _diff(
        expected, actual
    )


def test_every_delta_entry_still_resolves_against_the_baseline():
    """A delta that no longer matches is stale: either the baseline changed (never) or a
    PENDING route came back and its owner forgot to delete the entry."""
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    for entry in ALLOWED_DELTA + PENDING_DELTA:
        apply_delta(baseline, [entry])


def test_the_snapshot_covers_the_frozen_surface(snapshot):
    """The §A.1 prefixes — the slice must keep covering every frozen router."""
    assert set(snapshot.PREFIXES) >= {
        "/api/eval", "/api/experiments", "/api/runtime-canaries",
        "/api/observability/sessions/{session_id}/evaluate", "/api/overview/online-quality",
    }
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    # 52 paths / 71 operations: the 61 frozen routes (plan §A.1) + 2 adjacent + 8 W-E5
    assert len(baseline["paths"]) == 52
    assert sum(len(item) for item in baseline["paths"].values()) == 71
