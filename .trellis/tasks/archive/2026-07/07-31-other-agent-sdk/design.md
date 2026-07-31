# Design — Other Agent SDK entrance

## 1. Scope shape

This is a **presentation + naming** change plus one small persisted field. No
deploy-pipeline behavior changes. That framing is what keeps it safe to land last
in the parent task, after two children have already edited the same file.

## 2. What exists today

`frontend/src/pages/CreateAgent.tsx:911-986` — the picker is four hand-written
sibling JSX blocks inside `<div className="methods">`. There is no array or config
object, so **DOM order is display order**, and each sibling carries its own
staggered-animation index:

| DOM pos | `--i` | element | `data-method` | i18n block |
|---|---|---|---|---|
| 1 | 0 | `div` `:912-927` | `harness` | `create.methods.harness` |
| 2 | 1 | `div` `:928-943` | `container` | `create.methods.claudeSdk` |
| 3 | 2 | `div` `:944-966` | `zip_runtime` | `create.methods.studio` |
| 4 | 3 | `button` `:967-985` | `discovery` | `create.methods.discovery` |

Grid CSS: `frontend/src/theme/app.css:249-263` (4 fixed columns, collapses to 1
at ≤1180px) and `:713`.

Naming skew to preserve: the card titled "Strands Studio" **is** the
`zip_runtime` method; the canvas is reached by a nested `<Link>` with
`e.stopPropagation()` (`:959-965`) so clicking it does not select the card.

## 3. Card reorder

Move the `container` block (`:928-943`) after the `zip_runtime` block
(`:944-966`) and renumber `--i` to 0,1,2,3 in the new DOM order. Nothing else —
no CSS change (the grid is order-agnostic), no route change, no method-value
change.

Deliberately **not** refactored into a `METHODS` array. It is tempting, but this
task lands after two other children have edited the same JSX; a mechanical move
produces a reviewable diff, whereas a restructure would obscure whether the
copy/behavior actually changed. If an array is wanted, it is a separate cleanup.

## 4. Rename

`create.methods.claudeSdk.*` → `create.methods.otherSdk.*`. Current en copy:

```
badge: "CONTAINER · ~6 MIN"
title: "Claude Agent SDK"
desc:  "Full agentic loop with subagents, hooks and MCP servers. Packaged as an
        ARM64 container via CodeBuild, models via Bedrock."
spec3: "subagents via .claude/agents"
```

The `desc` and `spec3` are Claude-specific and must move down a level: the card
now describes the **category** ("bring your own agent SDK, packaged as an ARM64
container via CodeBuild"), and the Claude-specific facts belong to the sub-option.
Same for the two hard-coded non-i18n spec lines at `:939-940` —
`CodeBuild → ECR → Runtime` is categorical and stays; `CLAUDE_CODE_USE_BEDROCK=1`
is Claude-specific and moves to the sub-option.

Also rename:
- `create.configure.titleContainer` — currently
  `CONFIGURE — CLAUDE AGENT SDK · CONTAINER`; becomes an Other-Agent-SDK title
  that still names the selected SDK.
- `MethodChip.tsx:6` — `container: { label: "CLAUDE SDK" }`. The chip appears in
  the agent list and Registry against a `container` method. Keep the key
  `container`; the label follows the new naming.

Key names are internal, so renaming the block is safe as long as every `t(...)`
reference moves with it and `scripts/i18n_check.py` still sees identical en/zh-CN
trees.

## 5. Second-level SDK selector

### 5.1 UI

Rendered only when `method === "container"`, using the existing `selchips`
pattern — the same one the `zip_runtime` protocol selector uses at
`CreateAgent.tsx:1179-1204`. One chip today, `Claude Agent SDK`, always on. Under
it, the Claude-specific facts that came out of the card copy
(`CLAUDE_CODE_USE_BEDROCK=1`, `subagents via .claude/agents`) in the existing
`note` block style.

Placement: on the **step-2 configure panel**, not on the card. The cards are a
fixed-height 4-column grid (`app.css:249-263`); an expanding sub-selector inside
one card would break the row. Step 2 is also where the analogous protocol
sub-choice already lives, so this follows the page's own precedent.

`data-testid="agent-sdk-claude"` to match the file's testid convention.

### 5.2 Persisted field

```python
AgentSdk = Literal["claude_agent_sdk"]
...
agent_sdk: AgentSdk = "claude_agent_sdk"
```

Why persist a single-valued field: stored specs are JSON in the ledger. When a
second SDK arrives, either the field already exists and old rows default
correctly, or it does not and every `container` row is ambiguous. One line now
removes that ambiguity forever. This is the *only* speculative element accepted
here — explicitly **no** dispatch table, no registry, no per-SDK template
indirection until a second SDK exists.

`frontend/src/lib/api.ts::AgentSpecInput` gains `agent_sdk?: AgentSdk`, and
`buildSpec()` sends it. The container deployer
(`backend/app/deployer/container.py`) and
`backend/app/templates/claude_sdk_agent/` are untouched.

## 6. Interaction with children 1 and 2

Child 1 hides the Model source selector when `method === "container"` and pins
that method to `bedrock` + a Claude default. This task must preserve that: the SDK
sub-option sits in the same conditional region, so the two conditionals should
read as one coherent block ("container ⇒ show SDK choice, hide model source").

Conflict surface: both children edit `CreateAgent.tsx` step-2 rendering and the
`create.configure.*` / `create.methods.*` i18n blocks. Landing this child last
means resolving against already-merged code rather than the reverse.

## 7. Compatibility

| Scenario | Behavior |
|---|---|
| Existing `container` agent viewed / re-published | `agent_sdk` absent → defaults to `claude_agent_sdk`; deploy path unchanged. |
| Existing `container` agent loaded into the wizard | SDK chip pre-selected; model source pinned to Bedrock with its stored Claude id. |
| Anything keying on `data-method="container"` | Unchanged — only DOM position moved. |
| Public `/v1` API, ledger method values, `MethodChip` key set | Unchanged. |

## 8. Risks

| Risk | Mitigation |
|---|---|
| A stale `create.methods.claudeSdk.*` reference survives the rename and renders a raw key | Grep for `claudeSdk` across `frontend/src` after the rename; `i18n_check.py` catches tree mismatch but not orphaned lookups. |
| `--i` renumbering missed → animation cascade looks wrong | Explicit acceptance criterion on the 0,1,2,3 sequence. |
| A one-option selector reads as broken UI | Label the group so it is clearly a category with one member today; keep it visually secondary (chips + note, not a prominent control). |
| Chinese copy for "Other Agent SDK" reads as machine translation | Called out in the PRD as a requirement; write it as natural Chinese, not a transliteration. |

## 9. Test plan

- `backend/tests/test_agents_api.py` (or the container deployer test) — a spec
  dict without `agent_sdk` yields `agent_sdk == "claude_agent_sdk"`, and a
  container spec still renders a Claude model id.
- Frontend: `npm run lint`, `npx tsc --noEmit`, `npm run build`,
  `python3 scripts/i18n_check.py`.
- Visual: `make dev`, confirm the four-card order and that the Strands Studio
  canvas link still does not select its card.
