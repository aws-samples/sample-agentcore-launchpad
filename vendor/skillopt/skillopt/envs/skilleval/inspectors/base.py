"""Shared contracts and security helpers for artifact inspectors."""
from __future__ import annotations

import errno
import json
import math
import os
import subprocess
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol, TypeAlias, cast

MAX_RENDER_PIXELS = 500_000_000
DEFAULT_RESPONSE_BYTES = 1_000_000
# Leaves room for the smallest trusted CLI error or MCP envelope as valid JSON.
MIN_RESPONSE_BYTES = 512
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
DEFAULT_EXTRACT_CHARS = 500_000
MAX_EXTRACT_CHARS = 4_000_000
DEFAULT_SCRATCH_BYTES = 1024 * 1024 * 1024
MAX_SCRATCH_BYTES = 4 * 1024 * 1024 * 1024
DEFAULT_SCRATCH_ENTRIES = 4_096
MAX_SCRATCH_ENTRIES = 100_000
DEFAULT_SCRATCH_DEPTH = 32
MAX_SCRATCH_DEPTH = 64
MAX_COMMAND_OUTPUT_CHARS = 64_000
MAX_SELECTORS = 256
MAX_SELECTOR_CHARS = 256
MAX_COMMAND_TIMEOUT_SECONDS = 3_600
MAX_LOGICAL_PATH_CHARS = 4_096
MAX_LOGICAL_PATH_BYTES = 4_096
MAX_LOGICAL_COMPONENTS = 64
MAX_LOGICAL_COMPONENT_BYTES = 255

JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


class InspectionError(RuntimeError):
    """Raised when an artifact cannot be inspected within the trust contract."""


class EvaluationError(InspectionError):
    """Raised when the inspection *infrastructure* fails, not the artifact.

    A plain ``InspectionError`` means the artifact itself is missing,
    corrupt, unopenable, or otherwise rejected by parse/validation/structure
    checks -- the agent under evaluation is responsible for that outcome.
    ``EvaluationError`` is the narrower subclass raised at infrastructure
    failure points instead: subprocess timeouts/crashes in ``safe_run``,
    sandbox/process-supervisor failures, and unexpected exceptions caught by
    the inspector registry's catch-all wrappers. These are harness failures
    independent of the artifact's content, so callers (see
    ``skillopt.envs.skilleval.verdict.run_deterministic_checks``) must not
    score them as a failing criterion; the exception is left to propagate so
    the caller can classify the whole row as an evaluation error rather than
    an artifact failure. Because it subclasses ``InspectionError``, existing
    ``except InspectionError`` call sites that have not been updated to draw
    this distinction keep working unchanged.
    """


def _positive_int(value: object, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InspectionError(f"{name} budget must be a positive integer")
    if value > maximum:
        raise InspectionError(f"{name} budget exceeds maximum {maximum}")
    return value


@dataclass(frozen=True)
class RenderBudget:
    """Cumulative pixel budget for one render request."""

    max_pixels: int = MAX_RENDER_PIXELS
    max_scratch_bytes: int = DEFAULT_SCRATCH_BYTES
    max_scratch_entries: int = DEFAULT_SCRATCH_ENTRIES
    max_scratch_depth: int = DEFAULT_SCRATCH_DEPTH

    def __post_init__(self) -> None:
        _positive_int(self.max_pixels, "render pixel", MAX_RENDER_PIXELS)
        _positive_int(
            self.max_scratch_bytes,
            "scratch byte",
            MAX_SCRATCH_BYTES,
        )
        _positive_int(
            self.max_scratch_entries,
            "scratch entry",
            MAX_SCRATCH_ENTRIES,
        )
        _positive_int(
            self.max_scratch_depth,
            "scratch depth",
            MAX_SCRATCH_DEPTH,
        )


@dataclass(frozen=True)
class ResponseBudget:
    """Bounds JSON serialization and extracted artifact-controlled text."""

    max_bytes: int = DEFAULT_RESPONSE_BYTES
    max_extract_chars: int = DEFAULT_EXTRACT_CHARS
    max_scratch_bytes: int = DEFAULT_SCRATCH_BYTES
    max_scratch_entries: int = DEFAULT_SCRATCH_ENTRIES
    max_scratch_depth: int = DEFAULT_SCRATCH_DEPTH

    def __post_init__(self) -> None:
        _positive_int(self.max_bytes, "response byte", MAX_RESPONSE_BYTES)
        if self.max_bytes < MIN_RESPONSE_BYTES:
            raise InspectionError(
                f"response byte budget must be at least {MIN_RESPONSE_BYTES}"
            )
        _positive_int(
            self.max_extract_chars,
            "extraction character",
            MAX_EXTRACT_CHARS,
        )
        _positive_int(
            self.max_scratch_bytes,
            "scratch byte",
            MAX_SCRATCH_BYTES,
        )
        _positive_int(
            self.max_scratch_entries,
            "scratch entry",
            MAX_SCRATCH_ENTRIES,
        )
        _positive_int(
            self.max_scratch_depth,
            "scratch depth",
            MAX_SCRATCH_DEPTH,
        )


class Inspector(Protocol):
    """Contract implemented by format-specific inspectors."""

    def inspect(
        self,
        path: str,
        scratch_dir: str,
        *,
        response_budget: ResponseBudget,
    ) -> JSONValue: ...

    def render(
        self,
        path: str,
        scratch_dir: str,
        selectors: list[str],
        budget: RenderBudget,
    ) -> list[str]: ...

    def extract(
        self,
        path: str,
        scratch_dir: str,
        selectors: list[str],
        *,
        response_budget: ResponseBudget,
    ) -> JSONValue: ...


def bounded_diagnostic(value: object, max_chars: int = 1_000) -> str:
    """Return a single bounded diagnostic string."""
    text = str(value).replace("\x00", "\\0")
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return f"{text[:max_chars]}...[truncated {omitted} chars]"


def normalize_selectors(selectors: object | None) -> list[str]:
    """Validate and copy an inspector selector list."""
    if selectors is None:
        return []
    if not isinstance(selectors, list):
        raise InspectionError("selectors must be a list of strings")
    if len(selectors) > MAX_SELECTORS:
        raise InspectionError(f"selector count exceeds maximum {MAX_SELECTORS}")
    normalized: list[str] = []
    for selector in selectors:
        if (
            not isinstance(selector, str)
            or not selector
            or "\x00" in selector
            or len(selector) > MAX_SELECTOR_CHARS
        ):
            raise InspectionError(
                "each selector must be a non-empty bounded string without NUL"
            )
        normalized.append(selector)
    return normalized


def _validate_json_value(value: object) -> int:
    if value is None or isinstance(value, (str, bool)):
        return len(value) if isinstance(value, str) else 0
    if isinstance(value, int):
        return 0
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InspectionError("inspector result must contain finite numbers")
        return 0
    if isinstance(value, list):
        return sum(_validate_json_value(item) for item in value)
    if isinstance(value, dict):
        total = 0
        for key, child in value.items():
            if not isinstance(key, str):
                raise InspectionError(
                    "inspector result must use string object keys"
                )
            total += len(key) + _validate_json_value(child)
        return total
    raise InspectionError(
        f"inspector result is not JSON-compatible: {type(value).__name__}"
    )


def validate_json_result(
    value: object,
    budget: ResponseBudget,
    *,
    enforce_extract_chars: bool = False,
) -> JSONValue:
    """Validate recursive JSON types and serialized response size."""
    text_chars = _validate_json_value(value)
    if enforce_extract_chars and text_chars > budget.max_extract_chars:
        raise InspectionError(
            "extraction character budget exceeded: "
            f"{text_chars} > {budget.max_extract_chars}"
        )
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > budget.max_bytes:
        raise InspectionError(
            f"response byte budget exceeded: {len(encoded)} > {budget.max_bytes}"
        )
    return cast(JSONValue, value)


def _absolute_path(raw_path: str, label: str) -> str:
    if (
        not isinstance(raw_path, str)
        or not raw_path
        or "\x00" in raw_path
    ):
        raise InspectionError(f"{label} must be a non-empty path without NUL")
    return os.path.abspath(raw_path)


def _open_real_directory(path: str, label: str) -> int:
    absolute = _absolute_path(path, label)
    try:
        descriptor = os.open(os.path.abspath(os.sep), _DIRECTORY_FLAGS)
    except OSError as exc:  # pragma: no cover - a broken POSIX runtime
        raise InspectionError(
            f"{label} root could not be opened: {bounded_diagnostic(exc)}"
        ) from exc
    try:
        for part in absolute.split(os.sep)[1:]:
            if not part:
                continue
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    detail = "contains a symlink or non-directory component"
                elif exc.errno == errno.ENOENT:
                    detail = "does not exist"
                else:
                    detail = f"could not be opened: {bounded_diagnostic(exc)}"
                raise InspectionError(f"{label} {detail}: {absolute}") from exc
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def validate_roots(evidence_dir: str, scratch_dir: str) -> tuple[str, str]:
    """Validate real, disjoint evidence and scratch directory roots."""
    evidence = _absolute_path(evidence_dir, "evidence root")
    scratch = _absolute_path(scratch_dir, "scratch root")
    evidence_fd = _open_real_directory(evidence, "evidence root")
    scratch_fd = _open_real_directory(scratch, "scratch root")
    try:
        common = os.path.commonpath((evidence, scratch))
        if common in {evidence, scratch}:
            raise InspectionError(
                "evidence and scratch roots must not overlap"
            )
    finally:
        os.close(scratch_fd)
        os.close(evidence_fd)
    return evidence, scratch


def validate_logical_path(logical_path: object) -> tuple[str, ...]:
    """Validate a bounded normalized POSIX-relative artifact path."""
    if (
        not isinstance(logical_path, str)
        or not logical_path
        or "\x00" in logical_path
        or "\\" in logical_path
    ):
        raise InspectionError(
            "artifact path must be a non-empty logical POSIX relative path"
        )
    if len(logical_path) > MAX_LOGICAL_PATH_CHARS:
        raise InspectionError(
            "artifact path character length exceeds maximum "
            f"{MAX_LOGICAL_PATH_CHARS}"
        )
    try:
        encoded = logical_path.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InspectionError("artifact path must be valid UTF-8") from exc
    if len(encoded) > MAX_LOGICAL_PATH_BYTES:
        raise InspectionError(
            "artifact path UTF-8 length exceeds maximum "
            f"{MAX_LOGICAL_PATH_BYTES} bytes"
        )
    logical = PurePosixPath(logical_path)
    parts = logical.parts
    if (
        logical.is_absolute()
        or not parts
        or logical.as_posix() != logical_path
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise InspectionError(
            "artifact path must be a normalized logical POSIX relative path"
        )
    if len(parts) > MAX_LOGICAL_COMPONENTS:
        raise InspectionError(
            "artifact path component count exceeds maximum "
            f"{MAX_LOGICAL_COMPONENTS}"
        )
    for part in parts:
        if len(part.encode("utf-8")) > MAX_LOGICAL_COMPONENT_BYTES:
            raise InspectionError(
                "artifact path component UTF-8 length exceeds maximum "
                f"{MAX_LOGICAL_COMPONENT_BYTES} bytes"
            )
    return parts


def _open_child(
    parent_descriptor: int,
    name: str,
    *,
    directory: bool,
    label: str,
) -> int:
    flags = _DIRECTORY_FLAGS if directory else _FILE_FLAGS
    try:
        return os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            detail = "contains a symlink component"
        elif exc.errno == errno.ENOENT:
            detail = "does not exist"
        else:
            detail = f"could not be opened: {bounded_diagnostic(exc)}"
        raise InspectionError(f"{label} {detail}") from exc


def safe_run(
    command: list[str],
    *,
    timeout: int | float = 120,
    cwd: str,
    home: str,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """Run a command through the focused process-isolation implementation."""
    from ._process import safe_run as _safe_run

    return _safe_run(
        command,
        timeout=timeout,
        cwd=cwd,
        home=home,
        pass_fds=pass_fds,
    )


__all__ = [
    "DEFAULT_EXTRACT_CHARS",
    "DEFAULT_RESPONSE_BYTES",
    "DEFAULT_SCRATCH_BYTES",
    "DEFAULT_SCRATCH_DEPTH",
    "DEFAULT_SCRATCH_ENTRIES",
    "EvaluationError",
    "InspectionError",
    "Inspector",
    "JSONValue",
    "MAX_COMMAND_OUTPUT_CHARS",
    "MAX_EXTRACT_CHARS",
    "MAX_LOGICAL_COMPONENTS",
    "MAX_LOGICAL_COMPONENT_BYTES",
    "MAX_LOGICAL_PATH_BYTES",
    "MAX_LOGICAL_PATH_CHARS",
    "MAX_RENDER_PIXELS",
    "MAX_RESPONSE_BYTES",
    "MAX_SCRATCH_BYTES",
    "MAX_SCRATCH_DEPTH",
    "MAX_SCRATCH_ENTRIES",
    "MIN_RESPONSE_BYTES",
    "RenderBudget",
    "ResponseBudget",
    "bounded_diagnostic",
    "normalize_selectors",
    "safe_run",
    "validate_json_result",
    "validate_logical_path",
    "validate_roots",
]
