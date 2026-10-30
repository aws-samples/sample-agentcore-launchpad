"""Ruling R15: one core, three byte-identical copies — and (cn/r17-t, N17-obs120) the shared
transient predicate `runtime_invoke.py` shipped into the Lambda bundle as a fourth copy."""

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
# Whole-line markers: the hub module's docstring also *mentions* the marker text.
START = re.compile(r"^# <launchpad-opensearch-core>$", re.M)
END = re.compile(r"^# </launchpad-opensearch-core>$", re.M)


def _block(text: str) -> str:
    start, end = START.search(text), END.search(text)
    assert start and end, "fence markers missing"
    return text[start.start(): end.end()]


def test_runtime_template_embeds_the_core_verbatim():
    core = _block((BACKEND / "app/services/opensearch_http.py").read_text(encoding="utf-8"))
    tmpl = _block((BACKEND / "app/templates/memory_runtime.py.tmpl").read_text(encoding="utf-8"))
    assert tmpl == core, "re-sync: see docs/cn/interfaces/memory-runtime.md §Mirror"


def test_lambda_bundle_carries_the_core_verbatim():
    core = (BACKEND / "app/services/opensearch_http.py").read_text(encoding="utf-8")
    copy = (BACKEND / "lambdas/memory_extractor/opensearch_http.py").read_text(encoding="utf-8")
    assert copy == core, (
        "re-sync: cp backend/app/services/opensearch_http.py backend/lambdas/memory_extractor/"
    )


def test_lambda_bundle_carries_the_shared_transient_predicate_verbatim():
    """N17-obs120 (cn/r17-t): `process_kb_ingest` retries a transient embedding fault with the
    ONE shared predicate (`runtime_invoke.is_upstream_transient`, also shipped into the eval
    worker zip); the Lambda cannot import `app.*`, so the module travels as a sibling copy —
    stdlib + botocore only, byte-identical."""
    src = (BACKEND / "app/services/strands_eval/runtime_invoke.py").read_text(encoding="utf-8")
    copy = (BACKEND / "lambdas/memory_extractor/runtime_invoke.py").read_text(encoding="utf-8")
    assert copy == src, (
        "re-sync: cp backend/app/services/strands_eval/runtime_invoke.py "
        "backend/lambdas/memory_extractor/"
    )
    forbidden = [line for line in src.splitlines()
                 if line.startswith(("from app", "import app"))]
    assert not forbidden, forbidden  # the Lambda packaging rule: no app.* imports
