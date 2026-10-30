"""Graph-suite plumbing: fast password hashes for the auth-enabled worlds, and the coverage
report written when ``LAUNCHPAD_GRAPH_REPORT=<path.md>`` is set (``make graph-coverage``).

Under ``pytest-xdist`` every worker flushes a shard at session end; the controller (the
process without ``workerinput``) merges them into the markdown table the quality board
parses (``scripts/quality_metrics.py`` §7) and a JSON twin next to it.
"""

from __future__ import annotations

import json
import shutil

import pytest

from app.services import users as users_service
from tests.graph import coverage, loop_report
from tests.graph.paths import load_all, merge_hits, render_report, summarize


@pytest.fixture(autouse=True)
def fast_hashes(monkeypatch):
    """Registering ada / bob / carol per path would otherwise spend the budget on PBKDF2."""
    monkeypatch.setattr(users_service, "_ITERATIONS", 1_000)


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001 — pytest hook signature
    report = coverage.report_path()
    if report is None:
        return
    coverage.flush(report)
    if hasattr(session.config, "workerinput"):
        return  # an xdist worker: the controller merges
    graphs = load_all()
    hits = merge_hits(coverage.read_shards(report))
    report.parent.mkdir(parents=True, exist_ok=True)
    # the r16-o coverage table, then the round-17 loop-engine table (walks, neighbourhood,
    # cross-graph) the board reads as well
    report.write_text(render_report(graphs, hits) + "\n" + loop_report.render(graphs, hits),
                      encoding="utf-8")
    report.with_suffix(".json").write_text(
        json.dumps(
            {name: {**summarize(g, hits.get(name)),
                    "loop": loop_report.summarize(g, hits.get(name))}
             for name, g in sorted(graphs.items())},
            indent=1, sort_keys=True, ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    shutil.rmtree(coverage.shard_dir(report), ignore_errors=True)
