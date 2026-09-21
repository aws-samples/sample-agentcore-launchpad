"""Render self-contained native-input helpers into deployable Python sources."""

import ast
from pathlib import Path

ATTACHMENT_CONTRACT = "v1"
ATTACHMENT_MARKER = "LAUNCHPAD_ATTACHMENT_CONTRACT"


def render_attachment_source() -> str:
    """No app imports or runtime SDK dependencies in this generated helper."""
    return Path(__file__).with_name("attachments.py.tmpl").read_text(encoding="utf-8")


def has_attachment_contract(code: str) -> bool:
    """Recognize an explicit top-level literal assignment, never a comment/string."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    return any(
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == ATTACHMENT_MARKER
        and isinstance(node.value, ast.Constant)
        and node.value.value == ATTACHMENT_CONTRACT
        for node in tree.body
    )
