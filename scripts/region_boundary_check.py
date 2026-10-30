#!/usr/bin/env python3
"""Region-boundary gate (user requirement 2026-10-10).

Everything the CN build provisions or calls must live in the China partition; the ONLY
cross-partition dependency is Global Bedrock model inference (agent / judge / embedding /
codegen models reached with the bearer API key in ``Settings.model_region``).

A non-China AWS region literal in shipped code fails the gate unless the line carries
``region-boundary: global-bedrock`` (reserved for model-region defaults and the Bedrock
OpenAI-compatible endpoint) or ``region-boundary: allow <reason>`` (anything else; the
reason is printed so the exception stays visible). Python comments and docstrings are
ignored via tokenize; ``//``, ``#``, ``*`` comment lines are ignored in other files.
Hostnames are policed by scripts/cn_lint.sh, client construction by
backend/tests/test_client_funnel.py.
"""

from __future__ import annotations

import io
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCOPES = [
    "backend/app", "backend/lambdas", "infra/stacks", "infra/ec2", "infra/app.py",
    "scripts", "frontend/src", "samples",
]
EXTS = {".py", ".ts", ".tsx", ".tmpl", ".hbs", ".yaml", ".yml", ".sh", ".json"}
SKIP_DIRS = {"tests", "__pycache__", "node_modules", "dist", ".venv"}
REGION_RE = re.compile(
    r"\b(?:us|eu|ap|sa|ca|me|af|il)-(?:east|west|north|south|central|northeast|southeast)-[0-9]\b"
)
MARK_BEDROCK = "region-boundary: global-bedrock"
MARK_ALLOW = re.compile(r"region-boundary: allow (.+)")
BEDROCK_LINE = re.compile(r"model_region|MODEL_REGION|bedrock|openai/v1", re.I)
COMMENT_LINE = re.compile(r"^\s*(#|//|\*|/\*)")


def _py_code_lines(text: str) -> set[int]:
    """Line numbers that carry a region literal OUTSIDE comments and docstrings."""
    lines: set[int] = set()
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError):
        return {i + 1 for i, line in enumerate(text.splitlines()) if REGION_RE.search(line)}
    prev_significant = None
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING:
            # a STRING that starts a statement is a docstring -> ignore
            if prev_significant in (None, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
                prev_significant = tok.type
                continue
        if tok.type not in (tokenize.NL, tokenize.ENCODING):
            prev_significant = tok.type
        if tok.type in (tokenize.STRING, tokenize.NAME, tokenize.OP, tokenize.NUMBER) and REGION_RE.search(tok.string):
            for offset, piece in enumerate(tok.string.splitlines()):
                if REGION_RE.search(piece):
                    lines.add(tok.start[0] + offset)
    return lines


def main() -> int:
    failures: list[str] = []
    allowed: list[str] = []
    for scope in SCOPES:
        base = ROOT / scope
        if not base.exists():
            continue
        files = [base] if base.is_file() else [
            p for p in base.rglob("*") if p.is_file() and p.suffix in EXTS
            and not (set(p.relative_to(ROOT).parts) & SKIP_DIRS)
            and not p.name.endswith((".test.ts", ".test.tsx"))
            and p.name != "region_boundary_check.py"
        ]
        for path in files:
            text = path.read_text(encoding="utf-8", errors="replace")
            src_lines = text.splitlines()
            if path.suffix == ".py":
                hit_lines = _py_code_lines(text)
            else:
                hit_lines = {i + 1 for i, line in enumerate(src_lines)
                             if REGION_RE.search(line) and not COMMENT_LINE.match(line)}
            for n in sorted(hit_lines):
                line = src_lines[n - 1]
                # a marker on the line itself or on the line just above (for template
                # literals / generated code where a trailing comment would leak)
                marker_text = line + ("\n" + src_lines[n - 2] if n >= 2 else "")
                rel = path.relative_to(ROOT)
                if MARK_BEDROCK in marker_text:
                    if not BEDROCK_LINE.search(line):
                        failures.append(f"{rel}:{n}: '{MARK_BEDROCK}' on a non-Bedrock line")
                    else:
                        allowed.append(f"{rel}:{n}: global-bedrock")
                    continue
                m = MARK_ALLOW.search(marker_text)
                if m:
                    allowed.append(f"{rel}:{n}: allow — {m.group(1).strip()}")
                    continue
                failures.append(f"{rel}:{n}: {line.strip()[:140]}")
    for a in allowed:
        print(f"   allowed  {a}")
    if failures:
        print("── region_boundary: non-China region literal(s) without a marker:")
        for f in failures:
            print(f"   {f}")
        return 1
    print(f"region_boundary: OK ({len(allowed)} marked exceptions; only Global Bedrock model inference crosses the partition)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
