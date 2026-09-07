"""AGENTS.md mirrors CLAUDE.md's guidance body above its Trellis-managed block.

CLAUDE.md is read by Claude Code; AGENTS.md carries the same body for non-Claude
agents. The two drift silently when only one is edited, so this pins the relation.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TRELLIS_START = "<!-- TRELLIS:START -->"


def test_agents_md_body_mirrors_claude_md() -> None:
    claude, agents = ROOT / "CLAUDE.md", ROOT / "AGENTS.md"
    if not (claude.is_file() and agents.is_file()):
        pytest.skip("CLAUDE.md / AGENTS.md not present (packaged checkout)")
    # Drop CLAUDE.md's title + intro sentence (first four lines).
    claude_body = "".join(claude.read_text(encoding="utf-8").splitlines(keepends=True)[4:])
    agents_text = agents.read_text(encoding="utf-8")
    assert TRELLIS_START in agents_text, "AGENTS.md lost its Trellis block"
    agents_body = agents_text.split(TRELLIS_START, 1)[0]
    assert agents_body.rstrip("\n") == claude_body.rstrip("\n"), (
        "AGENTS.md body drifted from CLAUDE.md — regenerate it: "
        "tail -n +5 CLAUDE.md + blank line + the existing Trellis block"
    )
