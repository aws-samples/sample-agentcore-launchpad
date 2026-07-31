# Rename Claude Agent SDK entrance to Other Agent SDK

Child 3 of `07-31-mantle-default-models`. Read the parent `prd.md` first.

Substantively independent of children 1 and 2, but it edits the same region of
`frontend/src/pages/CreateAgent.tsx`, so **sequence it last** to avoid rebasing
the card JSX.

## Goal

Restructure the three creation entrances so the Claude-only path is visibly the
third option and no longer implies that Claude is *the* SDK:

1. Managed Harness
2. Strands Studio (zip)
3. **Other Agent SDK** — with `Claude Agent SDK` demoted to a second-level
   choice, leaving room for further SDKs
4. Discover existing runtimes (unchanged, stays last)

Today the order is Harness, **Claude Agent SDK**, Strands Studio, Discovery
(`frontend/src/pages/CreateAgent.tsx:911-986`), with no sub-option anywhere.

## Requirements

### R1 — Card order

- Display order becomes Managed Harness → Strands Studio → Other Agent SDK →
  Discover existing runtimes.
- The cards are four hand-written sibling JSX blocks whose DOM order *is* the
  display order, each carrying its own `style={{ "--i": N }}` staggered-animation
  index. Reordering must renumber `--i` so the animation still cascades
  left-to-right.
- The underlying method values (`harness`, `zip_runtime`, `container`) do not
  change — this is presentation only. `data-method` attributes stay as they are
  so existing selectors and tests keep working.

### R2 — Rename to Other Agent SDK

- The `container`-method card is titled **Other Agent SDK**, with a description
  and badge that describe the category rather than Claude specifically.
- The step-2 configure panel title for `container`
  (`create.configure.titleContainer`) matches the new name.
- The card's two hard-coded, non-i18n spec lines (`CodeBuild → ECR → Runtime`,
  `CLAUDE_CODE_USE_BEDROCK=1`) are reviewed: the pipeline line stays, the
  Claude-specific env line moves to the sub-option level or the description,
  since it is a property of the Claude Agent SDK, not of the category.

### R3 — Second-level SDK selector

- Selecting the Other Agent SDK card reveals a second-level SDK choice whose
  only current option is **Claude Agent SDK**, pre-selected.
- The chosen SDK is persisted on the spec so a future second SDK does not
  require migrating stored specs: `AgentSpec.agent_sdk: Literal["claude_agent_sdk"]
  = "claude_agent_sdk"`.
- The container deployer's behavior is unchanged for `claude_agent_sdk`; no
  dispatch table is introduced until there is a second SDK to dispatch to.

### R4 — Claude's default model is untouched

Per the parent PRD: the Claude Agent SDK can only drive Claude models, so the
`container` method keeps `model_source: "bedrock"` and its Claude default model.
Child 1 already pins this by hiding the Model source selector for `container`;
this task must not weaken that.

### R5 — i18n

- Rename the `create.methods.claudeSdk.*` key block to something category-shaped
  (e.g. `create.methods.otherSdk.*`) and add the sub-option keys, in **both**
  `en` and `zh-CN` with identical key trees
  (`frontend/src/locales/*/common.json:163-192`).
- The Chinese copy must read naturally, not as a transliteration of "Other Agent
  SDK".

## Acceptance criteria

- [ ] `/create` shows exactly four entrances in the order Managed Harness →
      Strands Studio → Other Agent SDK → Discover existing runtimes, with
      `--i` values 0,1,2,3 in that order.
- [ ] Clicking the third card selects `method === "container"` (unchanged
      `data-method="container"`) and reveals the SDK sub-option with
      `Claude Agent SDK` pre-selected.
- [ ] Creating an agent through that card produces a spec with
      `method: "container"`, `agent_sdk: "claude_agent_sdk"`,
      `model_source: "bedrock"`, and a Claude `model_id` — i.e. deployment
      behavior identical to before this work.
- [ ] `AgentSpec` built from a stored spec with **no** `agent_sdk` yields
      `agent_sdk == "claude_agent_sdk"` — regression test for existing agents.
- [ ] No `create.methods.claudeSdk.*` key remains referenced anywhere; the
      `Strands Studio` card's `Open the Strands Studio canvas` link still works
      and still does not select the card when clicked (it uses
      `e.stopPropagation()`).
- [ ] `MethodChip` labels (`frontend/src/components/MethodChip.tsx:4-10`) still
      render correctly for `container` in the agent list and Registry, updated to
      the new naming if they said "Claude SDK".
- [ ] `make verify` green, including `python3 scripts/i18n_check.py`.

## Non-goals

- Adding an actual second SDK, or any dispatch machinery for one.
- Changing the container build pipeline (CodeBuild → ECR → Runtime), the
  Dockerfile, or `backend/app/templates/claude_sdk_agent/`.
- Touching the Discovery card beyond keeping it last.
- Changing method values in the ledger, `MethodChip`'s key set, or the public
  `/v1` API.
