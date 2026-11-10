#!/usr/bin/env python3
"""G(N7a): anchor + redaction + render self-check for the vendored Harness template.
Run from backend/: `uv run python ../scripts/harness_template_check.py [--template DIR]`."""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
DEFAULT_TEMPLATE = ROOT / "backend" / "app" / "templates" / "harness_runtime"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    args = parser.parse_args(argv)
    from app.deployer import harness_local  # app import after sys.path

    problems = harness_local.check_template(args.template)
    for problem in problems:
        print(f"── {problem}")
    if problems:
        print(f"harness_template_check: FAIL ({len(problems)} problem(s))")
        return 1
    # every vendored source file (the *.tmpl sources incl. requirements.txt.tmpl; not
    # the _cli codegen template or MANIFEST.md) — 7 for the contract's directory
    sources = [
        p for p in args.template.rglob("*.tmpl")
        if p.parts[len(args.template.parts)] not in harness_local.SKIP_DIRS
    ]
    print(
        f"harness_template_check: OK · {len(sources)} files · anchors A1–A12 · "
        "render+compile OK"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
