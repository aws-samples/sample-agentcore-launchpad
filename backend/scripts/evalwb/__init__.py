"""Local evaluation workbench (eval-accuracy loop, docs/cn/interfaces/evaluation-engine.md §26).

Replays REAL CloudWatch records of evaluated sessions through the production evaluation code
of the checkout it runs from — ``LaunchpadSpansProvider``, ``engine_core``, the evaluators —
and measures it: M1 extraction vs the runtime origin logs, and (``--judge`` / ``--global``)
M2–M4 against Global AgentCore ``Evaluate``. Read-only on AWS; the CLI is
``backend/scripts/eval_workbench.py`` (``make eval-local`` / ``make eval-diff``).

Modules: :mod:`.cache` (gitignored record cache + the cache-backed Logs client),
:mod:`.fetch` (read-only CN reads: Logs Insights, the run ledger over SSM),
:mod:`.reconcile` (M1), :mod:`.judge` (local judges), :mod:`.global_eval` (Global
``Evaluate``), :mod:`.metrics` (M2–M4), :mod:`.report` (report.md / report.json / ledger row).
"""
