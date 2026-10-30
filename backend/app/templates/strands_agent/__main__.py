"""`python -m app.templates.strands_agent --render-smoke`: render the default spec and
byte-compile it, so a template edit that breaks Python is caught without a deploy."""

import py_compile
import sys
import tempfile
from pathlib import Path

from app.schemas.agent import AgentSpec
from app.templates.strands_agent import render_main_py


def main(argv: list[str]) -> int:
    if argv != ["--render-smoke"]:
        print("usage: python -m app.templates.strands_agent --render-smoke", file=sys.stderr)
        return 2
    spec = AgentSpec(name="render-smoke", method="zip_runtime", system_prompt="smoke")
    code = render_main_py(spec)
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "main.py"
        target.write_text(code, encoding="utf-8")
        py_compile.compile(str(target), doraise=True)
    for needle in ("api_key=_resolve_bedrock_api_key()", "region_name=MODEL_REGION"):
        if needle not in code:
            print(f"render-smoke: FAIL — {needle!r} missing", file=sys.stderr)
            return 1
    print(f"render-smoke: OK ({len(code)} chars, model {spec.model_id} @ {spec.model_region})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
