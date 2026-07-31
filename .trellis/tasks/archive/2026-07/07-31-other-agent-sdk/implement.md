# Implement — Other Agent SDK entrance

**Sequence this child last** in the parent task. Children 1 and 2 edit the same
regions of `frontend/src/pages/CreateAgent.tsx` and the same i18n blocks; land
those first and resolve against them.

## Step 1 — Backend field

- [ ] `backend/app/schemas/agent.py` — add `AgentSdk = Literal["claude_agent_sdk"]`
      next to `Method` (`:13`) and `agent_sdk: AgentSdk = "claude_agent_sdk"` on
      `AgentSpec`. Comment it as the extension point for future SDKs, and note
      that no dispatch exists yet.
- [ ] Do **not** touch `backend/app/deployer/container.py` or
      `backend/app/templates/claude_sdk_agent/`.
- [ ] Test: a spec dict with no `agent_sdk` yields `"claude_agent_sdk"`; a
      container spec still carries a Claude `model_id`.

Validate: `cd backend && uv run ruff check . && uv run pytest -q`

## Step 2 — i18n rename and re-copy

- [ ] Rename the `create.methods.claudeSdk` block to `create.methods.otherSdk`
      in **both** `frontend/src/locales/en/common.json` and
      `zh-CN/common.json` (`:163-192`), keeping the key trees identical.
- [ ] Rewrite `otherSdk.title` / `.desc` / `.badge` as the **category**: bring
      your own agent SDK, packaged as an ARM64 container via CodeBuild. Move the
      Claude-specific `subagents via .claude/agents` fact to the new sub-option
      keys.
- [ ] Add sub-option keys: `create.configure.agentSdk` (group label),
      `create.configure.agentSdkClaude` (= "Claude Agent SDK"), and a note key
      carrying the Claude-specific facts (`CLAUDE_CODE_USE_BEDROCK=1`,
      `.claude/agents`).
- [ ] Reword `create.configure.titleContainer` — currently
      `CONFIGURE — CLAUDE AGENT SDK · CONTAINER`.
- [ ] Write the zh-CN copy as natural Chinese, not a transliteration.

Validate: `python3 scripts/i18n_check.py`

## Step 3 — Card reorder and rename

`frontend/src/pages/CreateAgent.tsx`:

- [ ] Move the `container` card block (`:928-943`) to sit **after** the
      `zip_runtime` block (`:944-966`).
- [ ] Renumber `style={{ "--i": N }}` to 0,1,2,3 in the new DOM order
      (harness=0, zip_runtime=1, container=2, discovery=3).
- [ ] Point the moved card's `t(...)` calls at `create.methods.otherSdk.*`.
- [ ] Keep `data-method="container"` and `setMethod("container")` exactly as they
      are.
- [ ] Card spec lines (`:939-940`): keep `CodeBuild → ECR → Runtime`; remove
      `CLAUDE_CODE_USE_BEDROCK=1` (it moves to the sub-option note).
- [ ] Leave the Strands Studio card's nested `<Link>` and its
      `e.stopPropagation()` (`:959-965`) untouched.
- [ ] Grep: no `claudeSdk` reference remains anywhere under `frontend/src`.

## Step 4 — SDK sub-option

- [ ] Add `const [agentSdk, setAgentSdk] = useState<AgentSdk>("claude_agent_sdk")`.
- [ ] On the step-2 configure panel, in the same `method === "container"`
      conditional region that child 1 uses to hide the Model source selector,
      render a `selchips` group (one chip, `data-testid="agent-sdk-claude"`,
      always on) followed by a `note` block with the Claude-specific facts.
      Follow the protocol-selector shape at `:1179-1204`.
- [ ] `buildSpec()` (`:606-671`) — send `agent_sdk: agentSdk`.
- [ ] New-agent reset (`:562`) and load-existing (`:700`) — seed from
      `spec.agent_sdk ?? "claude_agent_sdk"`; add `agent_sdk?` to
      `StoredSpec` (`:46-47`).
- [ ] `frontend/src/lib/api.ts` — `agent_sdk?: AgentSdk` on `AgentSpecInput`.
- [ ] Verify the container carve-out from child 1 still holds: no Model source
      chips for `container`, Claude default model intact.

## Step 5 — Method chip

- [ ] `frontend/src/components/MethodChip.tsx:6` — update the `container` label
      from `CLAUDE SDK` to match the new naming. Keep the `container` key, the
      `blue` tone, and the `▣` icon so the agent list and Registry keep rendering.

## Step 6 — Docs

- [ ] `docs/architecture.md` — the three creation entrances by their new
      names/order, and that `agent_sdk` is an extension point with one member.
- [ ] Grep `docs/` and `README.md` for "Claude Agent SDK" used as an *entrance
      name* (as opposed to the SDK itself) and update those occurrences only.

## Step 7 — Full gate

- [ ] `make verify`.
- [ ] `make dev` — confirm the four-card order, the animation cascade, that the
      Strands Studio canvas link does not select its card, and that the third
      card reveals the SDK sub-option on step 2.
- [ ] Tick every acceptance criterion in `prd.md`.

## Review gate

After **Step 3**: the diff should read as a pure move plus `t()` key renames. If
it reads as a rewrite, the reorder was mixed with a refactor — split it.

## Rollback points

- Step 1 is additive with a backward-compatible default.
- Steps 2–5 are presentation-only; reverting them leaves the harmless
  `agent_sdk` field in place.
