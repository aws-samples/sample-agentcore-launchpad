#!/usr/bin/env python3
"""Is a project's committed uv.lock still in step with its pyproject.toml? (N17-obs485)

host-update installs and runs with `--frozen` everywhere (step 6 `uv sync --frozen`, steps
9 / 10 `uv run --frozen`, the unit's `uv run --frozen uvicorn`, `UV_FROZEN=1` in the env
file), so a host never re-locks against its CN index mirror. `--frozen` also never checks the
lock: a pyproject.toml whose dependencies changed without a matching `uv lock` would install
the OLD set silently. `uv lock --check` / `--locked` cannot stand in on a host — they resolve
against the env's mirror index and so report every committed (pypi.org-sourced) lock as stale.

This check needs no index and no network: uv records the project's own declared requirements
in the lock (`[package.metadata] requires-dist` / `requires-dev`), so the lock is stale exactly
when those differ from pyproject.toml's `[project] dependencies` / `[dependency-groups]`, or
when `requires-python` differs. Stdlib only (tomllib, Python 3.11+); read-only.

usage: uv_lock_fresh.py <project-dir>   exit 0 = fresh (or no pyproject.toml), 2 = stale
"""

import re
import sys
import tomllib
from pathlib import Path

REQ = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[(?P<extras>[^\]]*)\])?"
    r"\s*(?P<spec>[^;]*?)\s*(?:;\s*(?P<marker>.*?))?\s*$"
)


def _name(raw: str) -> str:
    return re.sub(r"[-_.]+", "-", raw).lower()


def _spec(raw: str | None) -> tuple[str, ...]:
    return tuple(sorted(p for p in re.sub(r"\s+", "", raw or "").split(",") if p))


def _marker(raw: str | None) -> str:
    return re.sub(r"\s+", "", (raw or "").replace("'", '"'))


def _from_pyproject(req: str) -> tuple:
    m = REQ.match(req)
    if not m:
        raise SystemExit(f"uv_lock_fresh: cannot parse requirement {req!r}")
    extras = tuple(sorted(_name(e) for e in (m["extras"] or "").split(",") if e.strip()))
    return (_name(m["name"]), extras, _spec(m["spec"]), _marker(m["marker"]))


def _from_lock(entry: dict) -> tuple:
    extras = tuple(sorted(_name(e) for e in entry.get("extras", [])))
    return (_name(entry["name"]), extras, _spec(entry.get("specifier")),
            _marker(entry.get("marker")))


def stale_reasons(project: Path) -> list[str]:
    pyproject = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    lock_path = project / "uv.lock"
    if not lock_path.is_file():
        return [f"{lock_path} is missing"]
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    name = _name(pyproject["project"]["name"])
    roots = [p for p in lock.get("package", []) if _name(p["name"]) == name]
    if not roots:
        return [f"the lock has no entry for the project itself ({name})"]
    meta = roots[0].get("metadata", {})
    reasons = []
    want = {_from_pyproject(r) for r in pyproject["project"].get("dependencies", [])}
    have = {_from_lock(e) for e in meta.get("requires-dist", [])}
    if want != have:
        reasons.append(f"dependencies: pyproject-only {sorted(want - have)}, "
                       f"lock-only {sorted(have - want)}")
    groups = pyproject.get("dependency-groups", {})
    locked_groups = meta.get("requires-dev", {})
    for group in sorted(set(groups) | set(locked_groups)):
        want = {_from_pyproject(r) for r in groups.get(group, []) if isinstance(r, str)}
        have = {_from_lock(e) for e in locked_groups.get(group, [])}
        if want != have:
            reasons.append(f"dependency group {group!r}: pyproject-only {sorted(want - have)}, "
                           f"lock-only {sorted(have - want)}")
    want_py = _spec(pyproject["project"].get("requires-python"))
    have_py = _spec(lock.get("requires-python"))
    if want_py != have_py:
        reasons.append(f"requires-python: pyproject {want_py}, lock {have_py}")
    return reasons


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 64
    project = Path(argv[1])
    if not (project / "pyproject.toml").is_file():
        print(f"  {project}: no pyproject.toml — lock freshness not checked")
        return 0
    reasons = stale_reasons(project)
    if reasons:
        for reason in reasons:
            print(f"  {project}/uv.lock is STALE — {reason}", file=sys.stderr)
        return 2
    print(f"  {project}/uv.lock matches pyproject.toml (N17-obs485)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
