"""Pin validation for caller-supplied pip requirements (T10).

User `requirements` are concatenated into the list that the deploy pipeline
installs, so an unpinned entry means the artifact is whatever the index happened
to serve at build time — and can change under a rebuild without any spec change.
This module is the one definition of "pinned", shared by the `AgentSpec`
validator and `scripts/migrate_pin_requirements.py`.

What counts as pinned:

* `name==1.2.3` (extras and environment markers allowed)
* a direct URL carrying a `#sha256=` fragment
* `pkg @ git+https://host/repo@<40-hex commit>`

Everything else is refused — including `>=`, `~=`, `<`, `!=`, `==1.2.*`, a bare
name, and a VCS ref pointing at a branch or tag, all of which resolve differently
over time.

Deliberately **not** applied to the platform's own requirement lists
(`base_requirements()`, `STUDIO_EXTRA_REQUIREMENTS`, `MANTLE_EXTRA_REQUIREMENTS`,
`a2a_base_requirements()`). Those keep ranges on purpose — see the comment block
above `MANTLE_EXTRA_REQUIREMENTS`, which relies on pip intersecting two specs for
the same project — and reproducibility for the whole set comes from the hashed
lockfile the package stage generates, not from hand-pinned ranges.
"""

import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.core.runtime_target import TARGET_PYTHON as _TARGET_PYTHON
from app.core.runtime_target import uv_platform as _uv_platform

# name[extra1,extra2]==version, optionally followed by ; markers
_PINNED_RE = re.compile(
    r"""^
    (?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)      # project name
    (?:\[[A-Za-z0-9._,\s-]+\])?                # optional extras
    \s*==\s*
    (?P<version>[A-Za-z0-9][A-Za-z0-9.!+_-]*)  # a concrete version
    \s*$""",
    re.VERBOSE,
)

_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$")

REQUIRED_FORM = (
    "pin it as name==version (e.g. requests==2.32.3), a direct URL carrying "
    "#sha256=<hash>, or pkg @ git+https://host/repo@<full 40-character commit>"
)


def _strip_marker(entry: str) -> str:
    """Drop an environment marker — it constrains *when* a requirement applies,
    never *which version*, so it is orthogonal to pinning."""
    return entry.split(";", 1)[0].strip()


def _is_pinned_url(candidate: str) -> bool:
    """A direct URL is immutable only when it carries a content hash."""
    return "#sha256=" in candidate


def _is_pinned_vcs(candidate: str) -> bool:
    """`git+…@<ref>` is immutable only when the ref is a full commit SHA.

    A branch or tag moves, so `git+https://…@main` is refused even though it
    looks specific.
    """
    if "git+" not in candidate:
        return False
    _, _, ref = candidate.rpartition("@")
    return bool(_COMMIT_RE.match(ref.split("#", 1)[0].strip()))


def is_pinned(entry: str) -> bool:
    """Whether one requirement line names exactly one immutable artifact."""
    candidate = _strip_marker(entry)
    if not candidate:
        return False
    # `pkg @ url` and bare URLs both go down the URL/VCS branches.
    target = candidate.split("@", 1)[1].strip() if " @ " in candidate else candidate
    if target.startswith(("http://", "https://", "git+", "hg+", "svn+", "bzr+")):
        return _is_pinned_vcs(target) or _is_pinned_url(target)
    if "*" in candidate:  # ==1.2.* pins a range, not a release
        return False
    return bool(_PINNED_RE.match(candidate))


def unpinned(entries: list[str]) -> list[str]:
    """The subset that is not pinned, in input order."""
    return [entry for entry in entries if not is_pinned(entry)]


def assert_all_pinned(entries: list[str]) -> None:
    """Raise `ValueError` naming every offending entry and the required form."""
    loose = unpinned(entries)
    if not loose:
        return
    listed = ", ".join(repr(entry) for entry in loose)
    raise ValueError(
        f"every requirement must name one immutable artifact, so a rebuild "
        f"installs the same thing: {listed} {'is' if len(loose) == 1 else 'are'} "
        f"not pinned — {REQUIRED_FORM}"
    )


# ---------------------------------------------------------------------------
# Resolving ranges to pins
#
# Used where the platform derives requirements itself rather than a caller typing
# them — today that is harness→runtime conversion, which reads the source
# Harness's own `pyproject.toml` dependencies (ranges like `strands-agents >=
# 1.15.0`). Refusing those would block conversion outright, and exempting them
# would leave a spec that resolves differently on every rebuild; resolving them is
# the option that keeps the feature and the guarantee.
# ---------------------------------------------------------------------------

# The target the deploy pipeline installs for is the same single definition
# `zip_runtime.build_zip` resolves against: `_TARGET_PYTHON` / `_uv_platform`
# from app/core/runtime_target.py, imported above.

_NAME_EXTRAS_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[(?P<extras>[^\]]+)\])?"
)


def _canonical(name: str) -> str:
    """PEP 503 normalisation, so `Strands_Agents` matches `strands-agents`."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _split_name_extras(entry: str) -> tuple[str, str | None]:
    match = _NAME_EXTRAS_RE.match(_strip_marker(entry))
    if not match:
        return "", None
    return match.group("name"), match.group("extras")


def resolve_pins(
    entries: list[str],
    platform: list[str],
    runner: Callable[..., Any] = subprocess.run,
) -> list[str]:
    """Resolve each entry to `name[extras]==version`, preserving input order.

    Already-pinned entries and URL/VCS entries pass through untouched. The rest go
    through `uv pip compile`, resolved for the deploy target rather than this host.

    `platform` is every requirement the deploy will install alongside `entries`
    (`zip_runtime.platform_requirements(...)`), and it goes into the compile input
    with `entries`. Without it, each entry resolves to the newest release
    satisfying *that entry alone*, and the package stage — which compiles the two
    together into a hashed lockfile — can then find the result unsatisfiable, so
    the agent never produces an artifact at all. Measured instance: `mcp>=1.19.0`
    alone resolves to `mcp==2.0.0`, but every published `strands-agents` caps
    `mcp<2.0.0`, so the lock fails.

    There is deliberately **no `--no-deps`**, and no default for `platform`:

    * The caps that matter are usually transitive (nothing names `mcp` directly;
      `strands-agents` does), so the dependency walk is what makes them visible.
      Passing `platform` as `--constraint` while keeping `--no-deps` was measured
      and does *not* work — constraints only bound projects that appear in the
      resolve, and `--no-deps` never pulls in the edges that carry the cap.
    * A defaulted `platform` would let a new call site silently reintroduce the
      isolated resolve. Callers with genuinely no platform contribution pass `[]`.

    The return value stays limited to `entries`: the compile output now carries the
    whole transitive closure, but only the callers' own names are read out of it
    (the closure belongs in the build's lockfile, not in a spec). The compile also
    **drops extras** from its output (`botocore[crt]` resolves to `botocore==…`),
    which would silently change what gets installed, so extras are re-attached here
    from the input.
    """
    loose = [entry for entry in entries if not is_pinned(entry) and _strip_marker(entry)]
    if not loose:
        return list(entries)

    with tempfile.TemporaryDirectory(prefix="pin-resolve-") as tmp:
        src = Path(tmp) / "requirements.in"
        out = Path(tmp) / "resolved.txt"
        src.write_text("\n".join([*platform, *loose]) + "\n", encoding="utf-8")
        proc = runner(
            [
                "uv", "pip", "compile", str(src), "--quiet",
                "--python-version", _TARGET_PYTHON,
                "--python-platform", _uv_platform(),
                # Keep conversion-time pins inside the same wheel-only artifact
                # set the package stage can install for AgentCore Runtime.
                "--only-binary=:all:",
                "-o", str(out),
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[-1000:]
            raise ValueError(
                "could not resolve requirements to pinned versions — the input "
                "includes the platform's own requirement lists, so the conflict "
                f"may be between those and the requested entries: {detail}"
            )
        resolved_text = out.read_text(encoding="utf-8")

    # name -> "name==version", from the compile output
    pinned_by_name: dict[str, str] = {}
    for line in resolved_text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped or "==" not in stripped:
            continue
        name, _, _ = stripped.partition("==")
        pinned_by_name[_canonical(name.strip())] = stripped

    out_entries: list[str] = []
    for entry in entries:
        if is_pinned(entry) or not _strip_marker(entry):
            out_entries.append(entry)
            continue
        name, extras = _split_name_extras(entry)
        pin = pinned_by_name.get(_canonical(name))
        if pin is None:
            raise ValueError(
                f"requirement {entry!r} was not present in the resolver output — "
                "cannot pin it"
            )
        if extras:
            pinned_name, _, version = pin.partition("==")
            pin = f"{pinned_name}[{extras}]=={version}"
        out_entries.append(pin)
    return out_entries


# ---------------------------------------------------------------------------
# Re-resolving a conversion's pins when the platform moves (#300)
#
# A converted Runtime stores exact pins that were resolved against the platform's
# requirement lists *as they were at conversion time*. When the platform later
# raises a floor or a cap (`boto3>=1.43.66`, `strands-agents==1.47.0` capping
# `mcp<2.0.0`), those pins stop locking and every rebuild fails. The package stage
# then re-resolves them from the source Harness's ranges — recorded on
# `AgentSpec.requirement_ranges` — or, for a twin converted before that field
# existed, from the bare project names.
# ---------------------------------------------------------------------------

MAX_RANGE_ENTRIES = 100
MAX_RANGE_LENGTH = 300

# name[extras] (op version (, op version)*)? (; marker)?
#
# Deliberately narrower than PEP 508: the field is copied verbatim into a
# requirements file, so anything that file format would treat as more than one
# index requirement — an option, a URL, a `pkg @ <url>` direct reference, a
# comment, a line continuation — must not fit. The marker alphabet therefore has
# no `@`, `/`, `:`, `#` or `\`.
_VERSION_OP = r"(?:~=|===|==|!=|<=|>=|<|>)"
_VERSION = r"[A-Za-z0-9][A-Za-z0-9.*+!_-]*"
_PLAIN_RANGE_RE = re.compile(
    rf"""^
    [A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?               # project name
    (?:\s*\[\s*[A-Za-z0-9][A-Za-z0-9._-]*
        (?:\s*,\s*[A-Za-z0-9][A-Za-z0-9._-]*)*\s*\])?        # optional extras
    (?:\s*{_VERSION_OP}\s*{_VERSION}
        (?:\s*,\s*{_VERSION_OP}\s*{_VERSION})*)?             # optional specifiers
    (?:\s*;\s*[A-Za-z('"][A-Za-z0-9 _.'"<>=!~(),*+-]*)?       # optional marker
    \s*$""",
    re.VERBOSE,
)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def is_plain_range(entry: str) -> bool:
    """Whether `entry` is one index requirement and nothing else.

    Refuses options (`-i …`), URLs, VCS references, `@` direct references, and any
    control character (`\\n`, `\\r`, tab): a newline would let one entry carry a
    second line — an `--index-url` — into the compile input, and a ` -` would be
    read as a per-requirement option. ASCII only: a PEP 508 requirement needs no
    more, and it keeps the C1 `\\x85` and Unicode line separators — which
    `str.splitlines()` (pip's requirements reader) treats as line breaks — and
    every other non-ASCII space out of the regex's `\\s`.
    """
    if (
        not entry
        or len(entry) > MAX_RANGE_LENGTH
        or not entry.isascii()
        or _CONTROL_RE.search(entry)
    ):
        return False
    if re.search(r"\s-", entry):
        return False
    return bool(_PLAIN_RANGE_RE.match(entry))


def assert_plain_ranges(entries: list[str] | None) -> None:
    """Validation for `AgentSpec.requirement_ranges`; `None` and `[]` are valid.

    Raises `ValueError` naming every offending entry. The ranges are compile input
    on a later rebuild, so they must not be able to widen the build's
    package-index boundary (see `services/requirements_txt` for the same rule on an
    uploaded requirements.txt).
    """
    if not entries:
        return
    if len(entries) > MAX_RANGE_ENTRIES:
        raise ValueError(
            f"requirement_ranges lists {len(entries)} entries — at most "
            f"{MAX_RANGE_ENTRIES} are allowed"
        )
    bad = [entry for entry in entries if not is_plain_range(entry)]
    if not bad:
        return
    listed = ", ".join(repr(entry[:80]) for entry in bad)
    raise ValueError(
        f"requirement_ranges must hold plain 'name[extras] <specifiers> [; marker]' "
        f"lines of at most {MAX_RANGE_LENGTH} characters — URLs, VCS references, "
        f"options and control characters are refused: {listed}"
    )


def reresolve_inputs(pins: list[str], ranges: list[str]) -> list[str]:
    """The entries a re-resolve of `pins` should compile, in `pins` order.

    For each pin:

    * a recorded range for the same project (PEP 503 name match) → that range, so
      the source Harness's own floors survive;
    * else a `name[extras]==version[; marker]` pin → `name[extras][; marker]` —
      the legacy fallback for a twin converted before ranges were recorded. Extras
      stay (`botocore[crt]` must not quietly become `botocore`), and so does the
      marker;
    * else (URL / VCS / anything this module cannot parse) → unchanged.
      `resolve_pins` passes pinned entries through, so they stay as they are.
    """
    by_name: dict[str, str] = {}
    for entry in ranges:
        name, _ = _split_name_extras(entry)
        if name:
            by_name.setdefault(_canonical(name), entry)

    out: list[str] = []
    for pin in pins:
        requirement, has_marker, marker = pin.partition(";")
        requirement = requirement.strip()
        match = _PINNED_RE.match(requirement)
        if not match or "*" in requirement:
            out.append(pin)
            continue
        recorded = by_name.get(_canonical(match.group("name")))
        if recorded is not None:
            out.append(recorded)
            continue
        name, extras = _split_name_extras(requirement)
        relaxed = f"{name}[{extras}]" if extras else name
        if has_marker and marker.strip():
            relaxed = f"{relaxed} ; {marker.strip()}"
        out.append(relaxed)
    return out
