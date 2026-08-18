"""Strict verdict parsing, host-side scoring, and deterministic checks.

`contracts.normalize_judge_contract` hands the evaluator a list of normalized
checks (``id``/``path``/``type``/``required``/``weight``/``spec``). This
module:

- Splits those checks into ``deterministic_checks`` (evaluated locally
  against the evidence snapshot through the trusted inspector registry) and
  ``agent_checks`` (scored by the judge model).
- Runs every deterministic check and returns criterion rows in the same
  shape the model returns for agent-owned checks.
- Synthesizes host failures for agent-owned checks whose required artifact
  is missing, corrupt, or unopenable, so the model is never asked about
  something it cannot evaluate.
- Strictly parses and validates the model's verdict JSON for the
  agent-owned checks only.
- Computes the host-side aggregate score (``score_criteria``), which is the
  single source of truth for ``hard``/``soft`` -- the model never overrides
  or computes these itself.

The orchestrator (agentic_judge.py, a later task) is responsible for calling
the model, merging the deterministic/synthesized/agent-scored criteria, and
persisting results; this module owns only the verdict/scoring semantics.
"""
from __future__ import annotations

import json
import math
import os

from skillopt.envs.skilleval.inspectors import (
    EvaluationError,
    InspectionError,
    extract_artifact,
    inspect_artifact,
    inventory_artifacts,
)
from skillopt.envs.skilleval.inspectors.base import validate_logical_path
from skillopt.envs.skilleval.inspectors.spreadsheet import evaluate_xlsx_check

DETERMINISTIC_CHECK_TYPES = frozenset(
    {
        "exists",
        "opens",
        "contains_text",
        "xlsx_cell",
        "xlsx_formula",
        "page_count",
        "slide_count",
        "image_dimensions",
    }
)
AGENT_CHECK_TYPES = frozenset({"visual", "rubric"})

_TOP_LEVEL_KEYS = frozenset({"schema_version", "status", "criteria", "coverage", "reason"})
_CRITERION_KEYS = frozenset({"id", "passed", "score", "reason", "evidence"})
_EVIDENCE_KEYS = frozenset({"path", "locator", "source"})
_COVERAGE_KEYS = frozenset({"artifacts", "units_inspected", "units_omitted"})


# ---------------------------------------------------------------------------
# Strict verdict parsing
# ---------------------------------------------------------------------------


def _reject_constant(token: str) -> None:
    raise ValueError(f"verdict JSON must not contain the out-of-range constant {token}")


def _validate_evidence(criterion_id: str, evidence: object, evidence_paths: set[str]) -> None:
    if not isinstance(evidence, list):
        raise ValueError(f"criterion {criterion_id!r} evidence must be a list")
    for entry in evidence:
        if not isinstance(entry, dict) or set(entry) != _EVIDENCE_KEYS:
            raise ValueError(
                f"criterion {criterion_id!r} evidence entries must have exactly the keys "
                f"{sorted(_EVIDENCE_KEYS)}"
            )
        path = entry["path"]
        if not isinstance(path, str) or path not in evidence_paths:
            raise ValueError(
                f"criterion {criterion_id!r} evidence path {path!r} is not part of the evidence snapshot"
            )
        if not isinstance(entry["locator"], str) or not isinstance(entry["source"], str):
            raise ValueError(f"criterion {criterion_id!r} evidence locator/source must be strings")


def parse_verdict(text: str, checks: list[dict], evidence_paths: set[str]) -> dict:
    """Strictly parse and validate an agent-owned verdict JSON payload.

    ``checks`` must be exactly the agent-owned checks the model was asked to
    score (never the deterministic checks -- those are never sent to the
    model and the model cannot be trusted to invent or override them).
    Raises ``ValueError`` on any schema, id, evidence, or numeric violation.
    Returns the parsed payload dict unchanged once fully validated.
    """
    try:
        payload = json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise ValueError(f"verdict text does not decode to a JSON object: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("verdict text must decode to a JSON object")
    if set(payload) != _TOP_LEVEL_KEYS:
        raise ValueError(
            f"verdict must have exactly the top-level keys {sorted(_TOP_LEVEL_KEYS)}, "
            f"got {sorted(payload)}"
        )
    if isinstance(payload["schema_version"], bool) or not isinstance(payload["schema_version"], int):
        raise ValueError("verdict schema_version must be an integer")
    if not isinstance(payload["status"], str) or not payload["status"]:
        raise ValueError("verdict status must be a non-empty string")
    if not isinstance(payload["reason"], str):
        raise ValueError("verdict reason must be a string")

    coverage = payload["coverage"]
    if not isinstance(coverage, dict) or set(coverage) != _COVERAGE_KEYS:
        raise ValueError(f"verdict coverage must have exactly the keys {sorted(_COVERAGE_KEYS)}")
    for key in _COVERAGE_KEYS:
        values = coverage[key]
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError(f"verdict coverage.{key} must be a list of strings")

    criteria = payload["criteria"]
    if not isinstance(criteria, list):
        raise ValueError("verdict criteria must be a list")

    expected_ids = {check["id"] for check in checks}
    seen_ids: set[str] = set()
    for row in criteria:
        if not isinstance(row, dict) or set(row) != _CRITERION_KEYS:
            raise ValueError(f"verdict criterion must have exactly the keys {sorted(_CRITERION_KEYS)}")
        row_id = row["id"]
        if not isinstance(row_id, str) or row_id not in expected_ids:
            raise ValueError(f"criterion {row_id!r} is not a declared agent-owned check id")
        if row_id in seen_ids:
            raise ValueError(f"criterion {row_id!r} is duplicated in the verdict")
        seen_ids.add(row_id)

        if not isinstance(row["passed"], bool):
            raise ValueError(f"criterion {row_id!r} passed must be boolean")
        score = row["score"]
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError(f"criterion {row_id!r} score must be numeric")
        if not math.isfinite(float(score)):
            raise ValueError(f"criterion {row_id!r} score must be finite")
        if not isinstance(row["reason"], str):
            raise ValueError(f"criterion {row_id!r} reason must be a string")
        _validate_evidence(row_id, row["evidence"], evidence_paths)

    missing = expected_ids - seen_ids
    if missing:
        raise ValueError(f"verdict is missing criteria for agent-owned check id(s) {sorted(missing)}")

    return payload


# ---------------------------------------------------------------------------
# Host-side scoring (verbatim per spec)
# ---------------------------------------------------------------------------


def score_criteria(checks: list[dict], criteria: list[dict]) -> tuple[int, float]:
    criteria_by_id = {row["id"]: row for row in criteria}
    weighted = 0.0
    total_weight = 0.0
    hard = 1
    for check in checks:
        row = criteria_by_id[check["id"]]
        score = float(row["score"])
        if not 0.0 <= score <= 1.0:
            raise ValueError(f"criterion {check['id']!r} score must be in [0, 1]")
        if not isinstance(row["passed"], bool):
            raise ValueError(f"criterion {check['id']!r} passed must be boolean")
        if check.get("required", True) and not row["passed"]:
            hard = 0
        weight = float(check.get("weight", 1.0))
        weighted += score * weight
        total_weight += weight
    return hard, weighted / total_weight


# ---------------------------------------------------------------------------
# Deterministic / agent-owned check split
# ---------------------------------------------------------------------------


def split_checks(checks: list[dict]) -> tuple[list[dict], list[dict]]:
    """Partition normalized checks into (deterministic_checks, agent_checks)."""
    deterministic = [check for check in checks if check["type"] in DETERMINISTIC_CHECK_TYPES]
    agent = [check for check in checks if check["type"] in AGENT_CHECK_TYPES]
    unknown = {
        check["type"]
        for check in checks
        if check["type"] not in DETERMINISTIC_CHECK_TYPES and check["type"] not in AGENT_CHECK_TYPES
    }
    if unknown:
        raise ValueError(f"unknown check type(s): {sorted(unknown)}")
    return deterministic, agent


def _criterion_row(check: dict, passed: bool, reason: str, *, score: float | None = None, locator: str = "") -> dict:
    return {
        "id": check["id"],
        "passed": passed,
        "score": (1.0 if passed else 0.0) if score is None else score,
        "reason": reason,
        "evidence": [{"path": check["path"], "locator": locator, "source": "structure"}],
    }


def _collect_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        collected: list[str] = []
        for item in value.values():
            collected.extend(_collect_strings(item))
        return collected
    if isinstance(value, list):
        collected = []
        for item in value:
            collected.extend(_collect_strings(item))
        return collected
    return []


def _resolve_evidence_file(evidence_dir: str, logical_path: str) -> str:
    """Resolve a validated logical evidence path to a real file path.

    ``evaluate_xlsx_check`` operates on a plain filesystem path rather than
    staging through the sandboxed inspector registry, so this performs its
    own defense-in-depth validation: re-checking the logical path shape and
    confirming the resolved real path cannot escape the evidence root before
    handing it a raw path.
    """
    validate_logical_path(logical_path)
    root = os.path.realpath(evidence_dir)
    candidate = os.path.realpath(os.path.join(root, logical_path))
    if candidate != root and not candidate.startswith(root + os.sep):
        raise InspectionError(f"artifact path escapes the evidence root: {logical_path!r}")
    if not os.path.isfile(candidate):
        raise InspectionError(f"evidence file does not exist: {logical_path!r}")
    return candidate


def _evaluate_exists(check: dict, inventory_paths: frozenset[str]) -> dict:
    if check["path"] in inventory_paths:
        return _criterion_row(check, True, "output exists in evidence")
    return _criterion_row(check, False, f"output not found in evidence: {check['path']!r}")


def _evaluate_opens(check: dict, *, evidence_dir: str, scratch_dir: str) -> dict:
    inspect_artifact(check["path"], evidence_dir=evidence_dir, scratch_dir=scratch_dir)
    return _criterion_row(check, True, "artifact opened successfully")


_SPREADSHEET_SUFFIXES = frozenset({".xlsx", ".xls"})


def _evaluate_contains_text(check: dict, *, evidence_dir: str, scratch_dir: str) -> dict:
    expected = check["spec"]["text"]
    result = extract_artifact(check["path"], evidence_dir=evidence_dir, scratch_dir=scratch_dir)
    haystack = "\n".join(_collect_strings(result))
    if expected in haystack:
        return _criterion_row(check, True, "expected text found in extracted content")
    # A no-selector spreadsheet extract maps every sheet to page=None and emits
    # no cell content, so the haystack above is sheet names + metadata only.
    # Page through the cells explicitly before concluding the text is absent.
    if os.path.splitext(check["path"])[1].lower() in _SPREADSHEET_SUFFIXES and _spreadsheet_contains_text(
        check["path"], expected, evidence_dir=evidence_dir, scratch_dir=scratch_dir
    ):
        return _criterion_row(check, True, "expected text found in spreadsheet cells")
    return _criterion_row(check, False, f"expected text not found: {expected!r}")


def _spreadsheet_contains_text(
    path: str, expected: str, *, evidence_dir: str, scratch_dir: str
) -> bool:
    """Page a spreadsheet's cell content within the inspector's own budgets.

    The default (no-selector) extract never emits cells (every sheet maps to
    ``page=None``), so a cell-only match is silently missed. The compact index
    reports each sheet's cell total, letting us walk ``sheet:<name>:page:<n>``
    selectors until the text appears or the pages are exhausted -- each extract
    is bounded by the inspector's per-page cap and the sheet totals bound the
    number of pages. Stops early on the first match. Does not change the
    inspector's default ``page=None`` semantics, which other callers rely on.
    """
    index = inspect_artifact(path, evidence_dir=evidence_dir, scratch_dir=scratch_dir)
    sheets = index.get("sheets") if isinstance(index, dict) else None
    if not isinstance(sheets, list):
        return False
    for sheet in sheets:
        if not isinstance(sheet, dict) or sheet.get("type") != "worksheet":
            continue
        name = sheet.get("name")
        cell_page = sheet.get("cell_page")
        # The selector grammar addresses sheets as 'sheet:<name>[:page:<n>]', so
        # a ':' in the name is unaddressable (Excel disallows it in names anyway).
        if not isinstance(name, str) or ":" in name or not isinstance(cell_page, dict):
            continue
        total = cell_page.get("total")
        page_size = cell_page.get("page_size")
        if not isinstance(total, int) or isinstance(total, bool) or total <= 0:
            continue
        if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size <= 0:
            page_size = total
        page_count = (total + page_size - 1) // page_size
        for page in range(1, page_count + 1):
            detail = extract_artifact(
                path,
                evidence_dir=evidence_dir,
                scratch_dir=scratch_dir,
                selectors=[f"sheet:{name}:page:{page}"],
            )
            if expected in "\n".join(_collect_strings(detail)):
                return True
    return False


def _evaluate_xlsx_cell_or_formula(check: dict, *, evidence_dir: str) -> dict:
    real_path = _resolve_evidence_file(evidence_dir, check["path"])
    return evaluate_xlsx_check(real_path, check)


def _evaluate_page_count(check: dict, *, evidence_dir: str, scratch_dir: str) -> dict:
    expected = check["spec"]["value"]
    if isinstance(expected, bool) or not isinstance(expected, int):
        return _criterion_row(check, False, "page_count spec value must be an integer")
    result = inspect_artifact(check["path"], evidence_dir=evidence_dir, scratch_dir=scratch_dir)
    actual = result.get("pages") if isinstance(result, dict) else None
    if not isinstance(actual, int) or isinstance(actual, bool):
        return _criterion_row(check, False, "page count is not available for this artifact format")
    if actual == expected:
        return _criterion_row(check, True, "page count matches exactly")
    return _criterion_row(check, False, f"page count mismatch: expected {expected}, found {actual}")


def _evaluate_slide_count(check: dict, *, evidence_dir: str, scratch_dir: str) -> dict:
    expected = check["spec"]["value"]
    if isinstance(expected, bool) or not isinstance(expected, int):
        return _criterion_row(check, False, "slide_count spec value must be an integer")
    result = inspect_artifact(check["path"], evidence_dir=evidence_dir, scratch_dir=scratch_dir)
    actual = result.get("slide_count") if isinstance(result, dict) else None
    if not isinstance(actual, int) or isinstance(actual, bool):
        return _criterion_row(check, False, "slide count is not available for this artifact format")
    if actual == expected:
        return _criterion_row(check, True, "slide count matches exactly")
    return _criterion_row(check, False, f"slide count mismatch: expected {expected}, found {actual}")


def _evaluate_image_dimensions(check: dict, *, evidence_dir: str, scratch_dir: str) -> dict:
    spec = check["spec"]
    expected_width, expected_height = spec["width"], spec["height"]
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (expected_width, expected_height)):
        return _criterion_row(check, False, "image_dimensions spec width/height must be integers")
    result = inspect_artifact(check["path"], evidence_dir=evidence_dir, scratch_dir=scratch_dir)
    width = result.get("width") if isinstance(result, dict) else None
    height = result.get("height") if isinstance(result, dict) else None
    if (
        not isinstance(width, int)
        or isinstance(width, bool)
        or not isinstance(height, int)
        or isinstance(height, bool)
    ):
        return _criterion_row(check, False, "image dimensions are not available for this artifact format")
    if width == expected_width and height == expected_height:
        return _criterion_row(check, True, "image dimensions match exactly")
    return _criterion_row(
        check,
        False,
        f"image dimensions mismatch: expected {expected_width}x{expected_height}, found {width}x{height}",
    )


_DETERMINISTIC_EVALUATORS = {
    "contains_text": _evaluate_contains_text,
    "page_count": _evaluate_page_count,
    "slide_count": _evaluate_slide_count,
    "image_dimensions": _evaluate_image_dimensions,
}


def run_deterministic_checks(
    checks: list[dict],
    *,
    evidence_dir: str,
    scratch_dir: str,
) -> tuple[list[dict], frozenset[str]]:
    """Evaluate every deterministic check against the evidence snapshot.

    Returns ``(criteria, broken_required_paths)``. A path lands in
    ``broken_required_paths`` only when a *required* check on that path
    could not be inspected at all (missing, corrupt, or unopenable) --
    signalling that any agent-owned check on the same path is impossible to
    evaluate. A path is never added merely because a value/formula/text
    assertion failed on an artifact that opened successfully.

    The inspector registry almost never lets a bare exception escape --
    ``inspect_artifact``/``extract_artifact`` wrap unexpected crashes too --
    so exception *type*, not "did something raise", is what carries the
    artifact-vs-evaluation distinction here:

    - A plain ``InspectionError`` (parse/validation/structure rejection --
      the artifact is missing, corrupt, or unopenable) is caught and turned
      into a failing criterion row, with the path added to
      ``broken_required_paths`` when the check is required. This is scored
      as an agent/artifact failure (``artifact_failure``).
    - ``EvaluationError`` -- its subclass raised at infrastructure failure
      points (subprocess timeouts/crashes in ``safe_run``, sandbox/process-
      supervisor failures, or an unexpected exception caught by the
      registry's catch-all wrappers) -- is deliberately NOT caught here. It
      propagates uncaught so the caller can classify the whole row as an
      ``evaluation_error`` instead of scoring a harness failure as if the
      agent under evaluation had produced a bad artifact.
    - Any other exception (a bug, or a failure outside the trust contract of
      the inspector registry entirely) also propagates uncaught; callers
      must not silently swallow it.
    """
    inventory_paths = frozenset(row["path"] for row in inventory_artifacts(evidence_dir, scratch_dir))
    criteria: list[dict] = []
    broken: set[str] = set()
    for check in checks:
        check_type = check["type"]
        if check_type not in DETERMINISTIC_CHECK_TYPES:
            raise ValueError(f"{check_type!r} is not a deterministic check type")
        file_level_failure = False
        try:
            if check_type == "exists":
                row = _evaluate_exists(check, inventory_paths)
                file_level_failure = not row["passed"]
            elif check_type == "opens":
                row = _evaluate_opens(check, evidence_dir=evidence_dir, scratch_dir=scratch_dir)
            elif check_type in ("xlsx_cell", "xlsx_formula"):
                row = _evaluate_xlsx_cell_or_formula(check, evidence_dir=evidence_dir)
            else:
                row = _DETERMINISTIC_EVALUATORS[check_type](
                    check, evidence_dir=evidence_dir, scratch_dir=scratch_dir
                )
        except EvaluationError:
            # Infrastructure failure (timeout, crash, sandbox error): not the
            # agent's fault, so it must not be scored as a failing criterion.
            # Propagate uncaught -- see the docstring above for the contract.
            raise
        except InspectionError as exc:
            row = _criterion_row(check, False, str(exc))
            file_level_failure = True
        if file_level_failure and check.get("required", True):
            broken.add(check["path"])
        criteria.append(row)
    return criteria, frozenset(broken)


def synthesize_dependent_failures(
    agent_checks: list[dict],
    broken_required_paths: frozenset[str],
) -> tuple[list[dict], list[dict]]:
    """Split agent-owned checks into host-synthesized failures and the rest.

    A check whose path overlaps a broken required output cannot be judged --
    there is nothing usable for the model to inspect. The host synthesizes a
    failing criterion for it directly; the model is never asked about it.
    Returns ``(synthesized_criteria, remaining_checks_needing_model)``. When
    ``remaining_checks_needing_model`` is empty (no agent checks at all, or
    every one of them was synthesized), the caller can skip the model call
    entirely.
    """
    synthesized: list[dict] = []
    remaining: list[dict] = []
    for check in agent_checks:
        if check["path"] in broken_required_paths:
            synthesized.append(
                _criterion_row(
                    check,
                    False,
                    f"required output {check['path']!r} is missing, corrupt, or unopenable; "
                    "unable to evaluate this criterion",
                )
            )
        else:
            remaining.append(check)
    return synthesized, remaining


__all__ = [
    "AGENT_CHECK_TYPES",
    "DETERMINISTIC_CHECK_TYPES",
    "parse_verdict",
    "run_deterministic_checks",
    "score_criteria",
    "split_checks",
    "synthesize_dependent_failures",
]
