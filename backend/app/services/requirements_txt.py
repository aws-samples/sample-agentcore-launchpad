"""User `requirements.txt` handling for the zip deploy paths (byoc `code_zip`
and platform zip runtimes): parsing, the supply-chain boundary, resolver-failure
summaries, and the upload-time dry resolve.

Parsing follows the pip requirements-file format — backslash continuations are
joined, inline comments stripped, blank lines tolerated, environment markers
passed through untouched. `--hash=` options are dropped: the platform re-locks
the file against the deploy target and generates its own hashes, so hashes
computed for another platform's wheels would only make every build fail.

Everything that reaches outside the platform's package index is refused —
`-r`/`-c` includes, editable/local paths, direct URLs and VCS references,
`--index-url`/`--extra-index-url`/`--find-links`. The build installs with the
platform's index and nothing else; a requirements file must not be able to
widen that boundary.
"""

import re
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from app.core.runtime_target import TARGET_PYTHON, uv_platform

MAX_REQUIREMENT_ENTRIES = 500

PRERESOLVE_TIMEOUT_S = 90

# Where a resolve can go from here when the index has no fitting wheel — the
# same three options at upload time and at deploy time.
RESOLVE_FIX_HINTS = (
    "switch to the container path (Dockerfile build), which installs for the "
    "image itself",
    "or vendor the dependencies inside the zip and disable requirement "
    "installation",
)

# pip's rule: a comment starts at `#` preceded by whitespace or line start.
_COMMENT_RE = re.compile(r"(^|\s)#.*$")
_HASH_OPT_RE = re.compile(r"\s--hash(=|\s+)\S+")

# option → why it is refused. Matched on the first whitespace-delimited token.
_REFUSED_OPTIONS: dict[str, str] = {
    "-r": "nested requirements files are not supported — inline the entries",
    "--requirement": "nested requirements files are not supported — inline the entries",
    "-c": "constraints files are not supported — inline the entries",
    "--constraint": "constraints files are not supported — inline the entries",
    "-e": "editable installs cannot run on the managed runtime",
    "--editable": "editable installs cannot run on the managed runtime",
    "-i": "the platform installs from its own package index only",
    "--index-url": "the platform installs from its own package index only",
    "--extra-index-url": "the platform installs from its own package index only",
    "--find-links": "the platform installs from its own package index only",
    "-f": "the platform installs from its own package index only",
    "--no-index": "the platform installs from its own package index only",
}

_URL_PREFIXES = ("http://", "https://", "ftp://", "git+", "hg+", "svn+", "bzr+", "file:")


def pip_python_version(python_version: str) -> str:
    """``PYTHON_3_13`` (the AgentCore runtime enum) → ``3.13`` (pip/uv shape).
    Values already in pip shape pass through."""
    return python_version.removeprefix("PYTHON_").replace("_", ".")


class RequirementsFileError(ValueError):
    """One offending line, with the line content and an actionable reason."""

    def __init__(self, line: str, reason: str) -> None:
        self.line = line
        self.reason = reason
        super().__init__(f"requirements.txt entry {line!r}: {reason}")


def _logical_lines(text: str) -> list[str]:
    """Join backslash line continuations the way pip does (drop `\\` + newline)."""
    lines: list[str] = []
    pending = ""
    for raw in text.splitlines():
        joined = pending + raw
        if joined.endswith("\\"):
            pending = joined[:-1]
            continue
        pending = ""
        lines.append(joined)
    if pending:
        lines.append(pending)
    return lines


def _reject(line: str) -> None:
    token = line.split()[0].split("=", 1)[0]
    reason = _REFUSED_OPTIONS.get(token)
    if line.startswith("-"):
        raise RequirementsFileError(
            line, reason or "pip options are not supported in an uploaded requirements.txt"
        )
    candidate = line.split(";", 1)[0].strip()
    target = candidate.split("@", 1)[1].strip() if " @ " in candidate else candidate
    if target.startswith(_URL_PREFIXES) or "://" in target:
        raise RequirementsFileError(
            line,
            "direct URL / VCS requirements are refused — the platform installs "
            "from its own package index only",
        )
    if candidate.startswith((".", "/", "~", "\\")):
        raise RequirementsFileError(
            line, "local paths cannot be installed — name an index package instead"
        )


def parse_requirements_txt(text: str, max_entries: int = MAX_REQUIREMENT_ENTRIES) -> list[str]:
    """The file's requirement entries, normalized and boundary-checked.

    Returns PEP 508 requirement strings (markers intact, `--hash` options
    dropped); the resolver downstream is what validates each entry's grammar.
    Raises :class:`RequirementsFileError` on anything outside the boundary.
    """
    entries: list[str] = []
    for logical in _logical_lines(text):
        line = _COMMENT_RE.sub("", logical).strip()
        if not line:
            continue
        line = _HASH_OPT_RE.sub("", line).strip()
        if not line:
            continue
        _reject(line)
        if re.search(r"\s--?[A-Za-z]", line):  # ` --global-option=…` and friends
            raise RequirementsFileError(
                line, "per-requirement pip options are not supported"
            )
        entries.append(line)
        if len(entries) > max_entries:
            raise RequirementsFileError(
                f"(entry {max_entries + 1})",
                f"requirements.txt lists more than {max_entries} entries",
            )
    return entries


# ---------------------------------------------------------------------------
# Resolver-failure summaries
#
# uv's resolution errors are written for a terminal session: a derivation tree
# over the whole conflict. A deploy log or a wizard banner needs the offending
# package and the reason, not the tree — and never the caller's requirement
# list echoed back.
# ---------------------------------------------------------------------------

# (pattern, reason template). Ordered: first match wins.
_FAILURE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"(?:Because\s+)?([A-Za-z0-9][A-Za-z0-9._-]*)(?:==\S+)? has no usable wheels"),
        "{pkg} publishes no wheel installable on {platform} / Python {python} "
        "(source builds are disabled on the platform)",
    ),
    (
        re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*) was not found in the package registry"),
        "{pkg} does not exist on the package index",
    ),
    (
        re.compile(r"there is no version of ([A-Za-z0-9][A-Za-z0-9._-]*)==?(\S+?)(?:\s|$)"),
        "{pkg} has no release matching the requested version",
    ),
    (
        # pip's shape, for the install step
        re.compile(r"No matching distribution found for ([A-Za-z0-9][A-Za-z0-9._-]*)"),
        "{pkg} has no distribution installable on {platform} / Python {python}",
    ),
]

_UNSATISFIABLE_RE = re.compile(r"requirements are unsatisfiable")


def summarize_resolver_failure(
    raw: str,
    *,
    python_version: str = TARGET_PYTHON,
    hints: Sequence[str] = (),
) -> str:
    """A concise, package-naming summary of a failed uv/pip resolve.

    ``raw`` is the resolver's stderr/stdout. The output names the offending
    package(s) and the reason, then the fix hints — never the input list.
    """
    platform = uv_platform()
    reasons: list[str] = []
    for pattern, template in _FAILURE_PATTERNS:
        for match in pattern.finditer(raw):
            reason = template.format(pkg=match.group(1), platform=platform,
                                     python=python_version)
            if reason not in reasons:
                reasons.append(reason)
        if reasons:
            break
    if not reasons and _UNSATISFIABLE_RE.search(raw):
        reasons.append(
            "the requirements conflict — no set of versions satisfies all of "
            "them together"
        )
    if not reasons:
        # unknown shape: keep a bounded tail of the raw output rather than nothing
        reasons.append((raw or "").strip()[-400:] or "resolver produced no output")
    summary = "; ".join(reasons)
    all_hints = list(hints)
    if any("wheel" in reason or "distribution" in reason for reason in reasons):
        # only wheel-availability failures are fixable by choosing another release
        all_hints.insert(0, f"pin a version that ships {platform} wheels")
    if not all_hints:
        return f"{summary}."
    return f"{summary}. Fixes: {'; '.join(all_hints)}."


# ---------------------------------------------------------------------------
# Upload-time dry resolve
# ---------------------------------------------------------------------------


def preresolve(
    requirements_text: str,
    *,
    python_version: str = TARGET_PYTHON,
    hints: Sequence[str] = (),
    runner: Callable[..., Any] = subprocess.run,
    timeout_s: float = PRERESOLVE_TIMEOUT_S,
) -> dict[str, Any]:
    """Dry-resolve a requirements.txt against the deploy target, without
    installing anything: ``{status: ok|failed|skipped, package_count, error}``.

    ``failed`` means the deploy's package stage would fail the same way;
    ``skipped`` means the check could not run (timeout, no ``uv`` on PATH) and
    says nothing about resolvability.
    """
    try:
        entries = parse_requirements_txt(requirements_text)
    except RequirementsFileError as exc:
        return {"status": "failed", "package_count": None, "error": str(exc)}
    if not entries:
        return {"status": "ok", "package_count": 0, "error": None}

    with tempfile.TemporaryDirectory(prefix="byoc-preresolve-") as tmp:
        src = Path(tmp) / "requirements.in"
        out = Path(tmp) / "resolved.txt"
        src.write_text("\n".join(entries) + "\n", encoding="utf-8")
        try:
            proc = runner(
                [
                    "uv", "pip", "compile", str(src), "--quiet",
                    "--python-version", python_version,
                    "--python-platform", uv_platform(),
                    "--only-binary=:all:",
                    "--no-header", "--no-annotate",
                    "-o", str(out),
                ],
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "skipped",
                "package_count": None,
                "error": f"resolver did not finish within {int(timeout_s)}s — "
                         "the deploy will run the full resolve",
            }
        except FileNotFoundError:
            return {
                "status": "skipped",
                "package_count": None,
                "error": "uv is not available on the control plane — "
                         "the deploy will run the full resolve",
            }
        if proc.returncode != 0:
            error = summarize_resolver_failure(
                (proc.stderr or proc.stdout or ""),
                python_version=python_version,
                hints=hints,
            )
            return {"status": "failed", "package_count": None, "error": error}
        count = sum(
            1 for line in out.read_text(encoding="utf-8").splitlines()
            if "==" in line and not line.lstrip().startswith("#")
        )
        return {"status": "ok", "package_count": count, "error": None}
