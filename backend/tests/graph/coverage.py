"""Runtime coverage of the lifecycle graphs (which edges / test paths the executor walked).

Hits are kept per process; ``flush`` writes one shard per worker when
``LAUNCHPAD_GRAPH_REPORT`` names the report file (``make graph-coverage``), and the
controller merges the shards into the markdown + JSON the quality board reads
(``tests/graph/conftest.py``).
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

REPORT_ENV = "LAUNCHPAD_GRAPH_REPORT"

HITS: dict[str, dict[str, set[str]]] = defaultdict(lambda: {"edges": set(), "paths": set()})


def record_edge(graph: str, edge_id: str) -> None:
    HITS[graph]["edges"].add(edge_id)


def record_path(graph: str, path_id: str) -> None:
    HITS[graph]["paths"].add(path_id)


def report_path() -> Path | None:
    value = os.environ.get(REPORT_ENV)
    return Path(value) if value else None


def shard_dir(report: Path) -> Path:
    return report.with_suffix(report.suffix + ".d")


def flush(report: Path) -> None:
    """Write this process's hits as one shard (idempotent per pid)."""
    directory = shard_dir(report)
    directory.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        name: {"edges": sorted(v["edges"]), "paths": sorted(v["paths"])}
        for name, v in HITS.items()
    }
    (directory / f"hits-{os.getpid()}.json").write_text(
        json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8"
    )


def read_shards(report: Path) -> list[dict[str, Any]]:
    directory = shard_dir(report)
    if not directory.is_dir():
        return []
    return [
        json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("hits-*.json"))
    ]
