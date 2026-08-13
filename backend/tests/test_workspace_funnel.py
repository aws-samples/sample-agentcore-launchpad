"""The environment a piece of code targets always arrives as an argument.

Sibling of `test_client_funnel.py`: that one guards *client construction*, this
one guards the two remaining ways a code path can silently bind itself to the hub
— reading the resource map out of `Settings`, or building the default workspace
context instead of accepting the caller's.
"""

import re
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"

RESOURCES_RE = re.compile(r"(get_settings\(\)|settings|current)\.resources\b")
DEFAULT_CONTEXT_RE = re.compile(r"\bdefault_workspace_context\(")

# Files allowed to read `settings.resources`, with the reason:
RESOURCES_ALLOWED = {
    # builds a WorkspaceContext from settings — the bridge itself
    "services/workspace.py",
    # migration seeds/mirrors the `default` row from settings
    "core/db.py",
    # `resources` is declared here
    "core/config.py",
}

# Files allowed to build the default context, with the reason:
DEFAULT_CONTEXT_ALLOWED = {
    # defines it
    "services/workspace.py",
    # model prices are hub-global config and so is their discovery (parent design
    # D10 defers multi-region price discovery)
    "services/model_prices.py",
}


def _offenders(pattern: re.Pattern[str], allowed: set[str]) -> list[str]:
    found: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        rel = path.relative_to(APP_DIR).as_posix()
        if rel in allowed:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                found.append(f"{rel}:{lineno}: {line.strip()}")
    return found


def test_resources_are_read_from_the_workspace_not_from_settings():
    offenders = _offenders(RESOURCES_RE, RESOURCES_ALLOWED)
    assert not offenders, (
        "read the resource map off the workspace the caller passed in "
        "(`ctx.resources`), not off Settings — Settings only describes the hub:\n"
        + "\n".join(offenders)
    )


def test_the_default_context_is_never_built_ad_hoc():
    offenders = _offenders(DEFAULT_CONTEXT_RE, DEFAULT_CONTEXT_ALLOWED)
    assert not offenders, (
        "take the workspace as an argument (a request's `ws.context`, or "
        "`context_for_workspace(row.workspace_id)` in a background worker) rather "
        "than defaulting to the hub's:\n" + "\n".join(offenders)
    )


def test_allowlists_are_not_stale():
    """An entry that no longer needs the exemption hides the next real offender."""
    for rel in RESOURCES_ALLOWED - {"core/config.py"}:
        text = (APP_DIR / rel).read_text(encoding="utf-8")
        assert RESOURCES_RE.search(text), f"{rel} no longer reads settings.resources — drop it"
    for rel in DEFAULT_CONTEXT_ALLOWED:
        text = (APP_DIR / rel).read_text(encoding="utf-8")
        assert DEFAULT_CONTEXT_RE.search(text), (
            f"{rel} no longer builds the default context — drop it"
        )
