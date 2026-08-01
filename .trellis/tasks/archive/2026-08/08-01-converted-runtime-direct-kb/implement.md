# Implementation Plan

1. Extract the inline direct KB runtime block into a reusable source template.
2. Update the Strands renderer to inline that rendered source without changing
   generated ZIP behavior.
3. Add an idempotent Harness-export KB graft and produce
   `launchpad_kb_tools.py` for KB-bearing conversions.
4. Carry `knowledge_bases`, direct prompt defaults, tool-description defaults,
   and conversion notes into the converted `AgentSpec`.
5. Extend conversion and template tests for good, KB-less, idempotent, and
   missing-anchor cases.
6. Run:
   - `cd backend && uv run ruff check app tests`
   - `cd backend && uv run pytest tests/test_harness_convert.py tests/test_strands_template.py tests/test_knowledge_kb.py -q`
   - `make verify`
7. Update `harness-conversion.md` and `managed-kb.md` to record the new direct
   conversion contract.

## Rollback

Revert the renderer extraction and conversion graft together. No persistent
schema or AWS resource migration is involved; already converted agents retain
their materialized code bundles.
