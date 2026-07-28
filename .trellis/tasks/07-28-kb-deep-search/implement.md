# Implementation plan

Ordered; each step ends in a runnable check.

## Step 1 — `kb_support.py`

- [ ] add `KB_DEEP_TOOL_NAME`, `KB_DEEP_ITERATIONS_SINGLE/MULTI`,
      `kb_deep_tool_description(kbs)`.
- [ ] rewrite `kb_prompt_section(kbs)` (drop the `tool_name` param) to name both tools
      with the prefer-which guidance.
- Check: `uv run ruff check .`

## Step 2 — Strands template

- [ ] `main.py.tmpl`: `KB_DEEP_ITERATIONS` placeholders, `_deep_targets` (shared kb_id
      resolution with `kb_search`), `_format_agentic`, `@tool kb_deep_search`; register it
      in `build_agent` next to `kb_search`.
- [ ] `__init__.py`: render both descriptions into `DEFAULT_TOOL_DESCRIPTIONS`, new
      `kb_prompt_section(kbs)` call.
- Check: `uv run pytest tests/test_strands_template.py -q`

## Step 3 — container template

- [ ] `main.py.tmpl`: `kb_deep_search_text` + `@tool("kb_deep_search", …)` async wrapper;
      add to the `create_sdk_mcp_server` tool list.
- [ ] `__init__.py`: new `kb_prompt_section(kbs)` call (ALLOWED_TOOLS unchanged — assert
      that in the test).
- Check: `uv run pytest tests/test_claude_sdk_template.py -q`

## Step 4 — tests

- [ ] stubbed-stream fixtures + the cases listed in design §6 for both templates.
- Check: `uv run ruff check . && uv run pytest -q`

## Step 5 — IAM

- [ ] `infra/stacks/base_stack.py`: `ManagedKbAgenticRetrieval` on `exec_role` (`*`), with
      the comment explaining why the resource cannot be narrowed.
- [ ] infra synth assertion.
- Check: `cd infra && uv run ruff check . && uv run pytest -q`

## Step 6 — frontend copy

- [ ] `kbNoteDirect` in `locales/{en,zh-CN}/common.json`.
- Check: `python3 scripts/i18n_check.py`

## Step 7 — full gate

- [ ] `make verify`

## Step 8 — real AWS (evidence → `research/`)

- [ ] `cd infra && uv run cdk deploy --require-approval never`; confirm the sid on the role.
- [ ] restart the local backend (it caches the old templates in-process).
- [ ] deploy `kb-deep-zip` (zip_runtime) + `kb-deep-container` (container) with
      `lab-fund-kb`; capture the job logs.
- [ ] ask a comparison question that should route to `kb_deep_search`; capture the answer,
      the SSE tool frames, and the `execute_tool kb_deep_search` span.
- [ ] also confirm a single-fact question still routes to the cheap `kb_search` (steering
      works, not just availability).
- [ ] delete both probe agents.

## Step 9 — docs + spec

- [ ] `docs/lab/02-deploy-runtime.md`: capability row + the two-channel note (the code
      methods are no longer single-shot only; the remaining harness difference is that its
      tools are gateway-hosted and admin-configured).
- [ ] `docs/lab/04-capabilities.md` §4.7: add the deep-search demo with real output.
- [ ] `.trellis/spec/launchpad/managed-kb.md`: §2b two tools + the live-verified API shape
      quirks (`content` is a struct; errors arrive inside the stream; no score/location on
      agentic results; step sequence is not guaranteed), §3 the `*` grant.
- Check: `make verify`, then review `git diff --stat`

## Rollback points

- After step 7 the change is one `git revert` away.
- Step 5 is the only AWS-visible change before step 8; reverting it needs `cdk deploy`.
