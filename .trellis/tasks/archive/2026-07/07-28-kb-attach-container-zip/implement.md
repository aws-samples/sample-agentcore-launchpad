# Implementation plan

Ordered; each step ends in a runnable check. Backend commands from `backend/`, infra from
`infra/`, frontend from `frontend/` — always via `uv run` / `npm`.

## Step 1 — schema gate

- [ ] `app/schemas/agent.py`: `_kb_needs_harness` → `_kb_method_supported` (allow
      `harness|zip_runtime|container`, reject `studio` and `protocol=a2a` with distinct
      messages); update the `knowledge_bases` field comment to describe both channels.
- Check: `uv run pytest tests/test_agents_api.py -q`

## Step 2 — shared KB support module

- [ ] new `app/templates/kb_support.py`: `KB_TOOL_NAME`, `KB_MCP_SERVER`,
      `mounted_kbs(spec)`, `kb_tool_description(kbs)`, `kb_prompt_section(kbs, tool_name)`.
- [ ] comment in `app/deployer/harness.py::_kb_prompt` cross-referencing it (gateway names
      vs direct tool name — intentionally not shared).
- Check: `uv run ruff check .`

## Step 3 — Strands ZIP template

- [ ] `app/templates/strands_agent/main.py.tmpl`: `MOUNTED_KBS` placeholder, `_kb_runtime`,
      `_format_passages`, `@tool kb_search(query, kb_id="")` (fan-out over all mounted KBs
      by default, readable errors, never raises), conditional append in `build_agent`.
- [ ] `app/templates/strands_agent/__init__.py::render_main_py`: render
      `__LAUNCHPAD_MOUNTED_KBS__`, seed `DEFAULT_TOOL_DESCRIPTIONS["kb_search"]` via the
      existing `__LAUNCHPAD_TOOL_DESCRIPTION_OVERRIDES__` merge point (KB default must not
      clobber a user override), append `kb_prompt_section` to the rendered system prompt.
- Check: `uv run pytest tests/test_strands_template.py -q`

## Step 4 — container template

- [ ] `app/templates/claude_sdk_agent/main.py.tmpl`: `MOUNTED_KBS`, `KB_TOOL_DESCRIPTION`,
      `@tool("kb_search", …)` async wrapper over `asyncio.to_thread`, `_kb_mcp_servers()`,
      merge into `build_options()`.
- [ ] `app/templates/claude_sdk_agent/__init__.py::render_main_py`: `MOUNTED_KBS` +
      description placeholders, `mcp__launchpad_kb` into `ALLOWED_TOOLS`, prompt section
      appended to `SYSTEM_PROMPT`.
- Check: `uv run pytest tests/test_claude_sdk_template.py -q`

## Step 5 — non-harness gateway guard

- [ ] `app/services/knowledge.py::_strip_kb_from_agents`: only `sync_agentic_target` when
      the agent's spec method is `harness`.
- Check: `uv run pytest tests/test_knowledge_kb.py -q`

## Step 6 — IAM

- [ ] `infra/stacks/base_stack.py`: `ManagedKbRetrieval` statement on `exec_role`.
- [ ] infra test asserting the action is present on the execution role.
- Check: `cd infra && uv run ruff check . && uv run pytest -q`

## Step 7 — frontend + i18n

- [ ] `CreateAgent.tsx`: drop the method reset effect, drop the submit-site method guard,
      render chips for all methods, method-dependent note.
- [ ] `locales/{en,zh-CN}/common.json`: add `create.configure.kbNoteDirect`, remove
      `kbSoon`, reword `kbNote`.
- Check: `npm run lint && npx tsc --noEmit` and `python3 scripts/i18n_check.py`

## Step 8 — full gate

- [ ] `make verify`

## Step 9 — real-AWS validation (evidence → `research/`)

- [ ] `make bootstrap` (lands the IAM statement; idempotent).
- [ ] Pick an existing ACTIVE managed KB with indexed content (`GET /api/knowledge-bases`).
- [ ] Deploy a `zip_runtime` agent with that KB mounted; capture `GET /api/jobs/{id}`.
- [ ] Invoke it with a document-only question; assert the answer is grounded.
- [ ] Same for a `container` agent (CodeBuild ~2–6 min).
- [ ] Save request/response transcripts under `research/e2e-*.md`.
- [ ] Clean up the two probe agents (`DELETE /api/agents/{id}`) unless they are worth
      keeping as demo resources — decide with river.

## Step 10 — docs + spec

- [ ] `docs/lab/02-deploy-runtime.md`: capability-table row 「挂载知识库（托管 RAG）」 →
      三方式均支持, with the channel difference and the agentic-retrieval caveat; add a
      troubleshooting row for "retrieval returns AccessDenied → run `make bootstrap`".
- [ ] `docs/lab/04-capabilities.md`: rewrite the 「KB 只能挂 Harness」 note (line ~243-245)
      into the two-channel description.
- [ ] `.trellis/spec/launchpad/managed-kb.md`: replace §2's "v1 is harness-only" with the
      two-channel contract, add the exec-role grant to §3, add invariants (no gateway
      target for non-harness agents; studio/a2a still rejected; re-publish is what
      attaches/detaches for code methods).
- [ ] Update `docs/architecture.md` only if it makes a harness-only claim (grep says it
      does not — verify).
- Check: `make verify` again (i18n + build), then `git diff --stat` review.

## Rollback points

- After step 8 the change is self-contained and revertible with one `git revert`.
- Step 6 is the only externally-visible AWS change before step 9; reverting it needs
  `make bootstrap` again.
