"""Tracked docs never point a reader at a gitignored path.

`docs/*.md` and the two READMEs ship with every clone; the local-only trees
(`.trellis/` specs, `docs/issues/`, `self-evolution/`) do not. A link from the
first set into the second reads as a promise the clone cannot keep, so this pins
the relation the way `test_agents_md_mirror.py` pins CLAUDE.md ↔ AGENTS.md.

`.claude/` is deliberately absent from the list: `architecture.md` names the
`.claude/skills/<name>/` path *inside a built container image*, which is a real
runtime location rather than a repository file.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Gitignored repository roots (see .gitignore) that a shipped doc must not cite.
IGNORED_ROOTS = (".trellis/", "docs/issues/", "self-evolution/")

# Anything that looks like a path mention: a markdown link target or an
# inline-code span. Both forms have shipped a dead `.trellis/` reference.
_MENTION_RE = re.compile(r"\]\(([^)]+)\)|`([^`\n]+)`")


def _strip_relative_prefix(mention: str) -> str:
    """Drop leading `../`, `./` and `/` segments, keeping a dotted first name.

    `str.lstrip("./")` cannot do this: it would also eat the dot of `.trellis`,
    which is exactly the reference this test exists to catch.
    """
    mention = mention.strip()
    while mention.startswith(("../", "./", "/")):
        mention = mention.split("/", 1)[1]
    return mention


def _tracked_docs() -> list[Path]:
    paths = sorted((ROOT / "docs").glob("*.md"))
    paths += [p for p in (ROOT / "README.md", ROOT / "README.zh-CN.md") if p.is_file()]
    return [p for p in paths if p.is_file()]


def test_tracked_docs_do_not_link_into_gitignored_roots() -> None:
    docs = _tracked_docs()
    if not docs:
        pytest.skip("docs/ and README*.md not present (packaged checkout)")
    offenders: list[str] = []
    for doc in docs:
        for lineno, line in enumerate(
            doc.read_text(encoding="utf-8").splitlines(), start=1
        ):
            for match in _MENTION_RE.finditer(line):
                raw = match.group(1) or match.group(2)
                mention = _strip_relative_prefix(raw)
                if mention.startswith(IGNORED_ROOTS):
                    offenders.append(
                        f"{doc.relative_to(ROOT).as_posix()}:{lineno} → {mention}"
                    )
    assert not offenders, (
        "tracked docs link into gitignored trees a clone does not have — replace each "
        "with a tracked target or an inline summary citing the code path:\n  "
        + "\n  ".join(offenders)
    )
