#!/usr/bin/env python3
"""Snapshot the evaluation / optimization slice of a Launchpad backend's OpenAPI.

N12 contract freeze (plan §A.5): the CN backend must expose the same routes, query
parameters and request schemas as the Global baseline ``4e27b1f4`` for every path under
the prefixes below, modulo the enumerated delta in
``backend/tests/contract/ALLOWED_DELTA.py``. This script renders that slice for whichever
checkout's ``app`` package is importable — run it from that checkout's ``backend/``::

    cd .worktrees/global-base/backend && uv run python ../../../scripts/snapshot_openapi.py \\
        --out ../../cn-n12-e0/backend/tests/contract/openapi_baseline_eval.json

It builds the app the way ``tests/conftest.py`` does (temp SQLite ledger, open console,
no price refresher, a minimal fake ``launchpad.yaml``) so no real AWS, network or ledger
is touched. Output is deterministic: keys sorted, ``description`` text dropped (handler
docstrings are documentation, not contract), ``required`` lists sorted.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

PREFIXES: tuple[str, ...] = (
    "/api/eval",
    "/api/experiments",
    "/api/runtime-canaries",
    "/api/observability/sessions/{session_id}/evaluate",
    "/api/overview/online-quality",
    "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan",
)

_SCHEMA_REF = "#/components/schemas/"


def _bootstrap_env() -> None:
    """Mirror tests/conftest.py: isolate the ledger + settings before any app import."""
    tmp = Path(tempfile.mkdtemp(prefix="launchpad-openapi-"))
    os.environ["LAUNCHPAD_DATABASE_URL"] = f"sqlite:///{tmp / 'snapshot.db'}"
    os.environ["LAUNCHPAD_ALLOW_OPEN_CONSOLE"] = "true"
    os.environ["LAUNCHPAD_MODEL_PRICES_REFRESH_HOURS"] = "0"
    os.environ.setdefault("LAUNCHPAD_ONLINE_EVAL_ENABLED", "0")
    sys.path.insert(0, os.getcwd())
    import app.core.config as config_mod

    config_mod.CONFIG_FILE = tmp / "launchpad.yaml"
    config_mod.CONFIG_FILE.write_text(
        "account_id: '111122223333'\n"
        "resources:\n"
        "  execution_role_arn: arn:aws-cn:iam::111122223333:role/launchpad-snapshot\n"
        "  artifacts_bucket: launchpad-artifacts-snapshot\n",
        encoding="utf-8",
    )
    config_mod.get_settings.cache_clear()


def _refs(node: Any, out: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(_SCHEMA_REF):
            out.add(ref[len(_SCHEMA_REF):])
        for value in node.values():
            _refs(value, out)
    elif isinstance(node, list):
        for value in node:
            _refs(value, out)


def _normalize(node: Any) -> Any:
    """Drop documentation text, sort ``required`` — keep everything that is contract."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == "description":
                continue
            if key == "required" and isinstance(value, list) and all(
                isinstance(v, str) for v in value
            ):
                out[key] = sorted(value)
            else:
                out[key] = _normalize(value)
        return out
    if isinstance(node, list):
        return [_normalize(v) for v in node]
    return node


def eval_slice(spec: dict[str, Any], prefixes: tuple[str, ...] = PREFIXES) -> dict[str, Any]:
    """The paths under ``prefixes`` + the schemas they reach (transitively)."""
    paths = {
        path: item for path, item in spec.get("paths", {}).items()
        if any(path == p or path.startswith(p + "/") or path.startswith(p + "?")
               for p in prefixes)
    }
    schemas = spec.get("components", {}).get("schemas", {})
    wanted: set[str] = set()
    _refs(paths, wanted)
    frontier = set(wanted)
    while frontier:
        more: set[str] = set()
        for name in frontier:
            _refs(schemas.get(name, {}), more)
        frontier = more - wanted
        wanted |= more
    return _normalize({
        "paths": paths,
        "components": {"schemas": {name: schemas[name] for name in sorted(wanted)
                                   if name in schemas}},
    })


def render(spec: dict[str, Any]) -> str:
    return json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    _bootstrap_env()
    from app.main import create_app

    spec = create_app().openapi()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(eval_slice(spec)), encoding="utf-8")
    kept = eval_slice(spec)
    print(f"wrote {args.out}: {len(kept['paths'])} paths, "
          f"{len(kept['components']['schemas'])} schemas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
