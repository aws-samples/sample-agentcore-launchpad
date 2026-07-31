# Research — creation-method entrance UI map

Verified 2026-07-31 against `frontend/src/`.

## The picker has no data structure

`frontend/src/pages/CreateAgent.tsx` — `CreateAgent` is a query-param shell
(`:92-95`): `params.get("view") === "discover" ? <RuntimeDiscovery /> :
<CreateAgentWizard />`.

Method union: `:43` `type Method = "harness" | "zip_runtime" | "container";`
State: `:421` `useState<Method>("harness")`.

The cards are **four hand-written sibling JSX blocks** inside
`<div className="methods">` (`:911-986`). No array, no config object — DOM order
*is* display order, and each sibling carries its own animation index:

| DOM pos | `--i` | element | `data-method` | onClick | i18n block | icon |
|---|---|---|---|---|---|---|
| 1 | 0 | `div` `:912-927` | `harness` | `setMethod("harness")` | `create.methods.harness` | `◇` |
| 2 | 1 | `div` `:928-943` | `container` | `setMethod("container")` | `create.methods.claudeSdk` | `▣` |
| 3 | 2 | `div` `:944-966` | `zip_runtime` | `setMethod("zip_runtime")` | `create.methods.studio` | `⬡` |
| 4 | 3 | `button` `:967-985` | `discovery` | `navigate("/create?view=discover")` | `create.methods.discovery` | `<Search/>` |

**Naming skew:** the card titled "Strands Studio" **is the `zip_runtime`
method**. The canvas is a nested `<Link to="/create/studio">` with
`onClick={(e) => e.stopPropagation()}` (`:959-965`) so clicking it does not select
the card.

Hard-coded, non-i18n spec lines inside the cards: `:923`
(`CreateHarness · InvokeHarness`), `:939-940` (`CodeBuild → ECR → Runtime`,
`CLAUDE_CODE_USE_BEDROCK=1`), `:955` (`pip (arm64) → zip → S3 → Runtime`), `:981`
(`ListAgentRuntimes · GetAgentRuntime`).

Grid CSS: `frontend/src/theme/app.css:249-263` (4 fixed columns, collapses to 1 at
≤1180px) and `:713`. Focus ring for `.method` at `:667`. The grid is
order-agnostic — reordering needs no CSS change.

## i18n

`frontend/src/locales/en/common.json` and `zh-CN/common.json`, block
`create.methods` at **`:163-192` in both files** (identical key trees, same line
numbers).

Current en copy for the block being renamed:

```json
"claudeSdk": {
  "badge": "CONTAINER · ~6 MIN",
  "title": "Claude Agent SDK",
  "desc": "Full agentic loop with subagents, hooks and MCP servers. Packaged as an ARM64 container via CodeBuild, models via Bedrock.",
  "spec3": "subagents via .claude/agents"
}
```

Note `claudeSdk` has no `spec2` (the other three do). `studio` additionally has
`open` (= "Open the Strands Studio canvas").

Second place a method needs wiring — the step-2 panel title, selected at
`CreateAgent.tsx:1017-1023`:
- `create.configure.title` — `CONFIGURE — MANAGED HARNESS`
- `create.configure.titleContainer` — `CONFIGURE — CLAUDE AGENT SDK · CONTAINER`
- `create.configure.titleZip`

## Routes

`frontend/src/App.tsx:24-25`:
```tsx
<Route path="create" element={<CreateAgent />} />
<Route path="create/studio" element={<CreateAgentStudio />} />
```

Query-param second levels (not declared in `App.tsx`):
- `/create?view=discover` → `RuntimeDiscovery` (`CreateAgent.tsx:93`, navigated
  at `:971`, back at `:178`)
- `/create/studio?agent=<id>` → canvas edit mode (`CreateAgent.tsx:997`;
  consumed `CreateAgentStudio.tsx:62`, guarded `:196-200`)
- `/create?gateway=…` / `/create?skill=…` deep links from
  `frontend/src/pages/Registry.tsx:286,302`

## Method chip (a second naming surface)

`frontend/src/components/MethodChip.tsx:4-10` — a real `Record`, labels
hard-coded (not i18n), rendered in the agent list and Registry:

```ts
harness:            { tone: "amber",  icon: "◇", label: "HARNESS" },
container:          { tone: "blue",   icon: "▣", label: "CLAUDE SDK" },
zip_runtime:        { tone: "aqua",   icon: "⬡", label: "STRANDS" },
studio:             { tone: "aqua",   icon: "⬡", label: "STUDIO" },
discovered_runtime: { tone: "muted",  icon: "◎", label: "DISCOVERED RT" },
```

## Sub-option precedents in the codebase

No method card has a nested selector today. Four things to copy from instead:

1. **Chip-pair sub-option scoped to one method** — the `zip_runtime` protocol
   selector, `CreateAgent.tsx:1179-1204` (`data-testid="protocol-http"` /
   `"protocol-a2a"`), with a conditional third level (A2A skill rows) expanding
   beneath at `:1205-1263`. This is the closest match and lives on step 2.
2. **Query-param second level on the same route** — `?view=discover`
   (`CreateAgent.tsx:92-95`).
3. **Kind → kind-specific field block** — the canvas provider `<select>` at
   `frontend/src/studio/PropertyPanel.tsx:378-395` dispatching to
   `renderMantleFields` (`:289-372`) vs the Bedrock branch (`:397-459`), with
   defaults re-seeded on switch by `applyProviderChange` (`:258-286`).
4. **Modal second-level gallery** — `frontend/src/studio/SampleGallery.tsx`,
   opened from `CreateAgentStudio.tsx:70,318-325`.

CSS available: `.selchips` / `.selchip` / `.selchip.on` at
`frontend/src/theme/app.css:295-298`. There is **no** `select` element styling in
the platform theme; platform pages use `<select className="input">` with
`<option style={{ background: "#141816" }}>` — e.g.
`frontend/src/pages/EvaluationEvaluators.tsx:357-372`.

## Backend blast radius of the `container` method

For confirming nothing else needs to change: `container` is dispatched via
`register_method` side-effect imports in `backend/app/main.py`; the method value
appears in `backend/app/routers/agents.py`, `routers/registry.py:54`,
`services/invoke.py`, `evaluation/service.py`, `optimization/*`,
`services/observability.py`, `models/ledger.py:32`. This task changes **no**
method values, so none of these are touched.
