# V2 Agent Create Flow: Deploy Line, List Filters, Integrate, TryChat, Inline KB

> The V2 agent detail and wizard improvements ported from `origin/console-v3` (task
> `10-13-v2-create-flow`). Frontend-only, on main's existing routes.

## 1. Scope / Trigger

- Changing V2 agent detail (`v2/pages/Agents.tsx`), the agents list, the wizard's
  knowledge-base field, or the public `/v1` snippets.

## 2. Pieces and contracts

| Piece | Files | Contract |
|---|---|---|
| Deploy line | `pages/agents/deployStatus.ts` (pure), `DeployProgress` in `Agents.tsx` | `deployLine(method, deployment, now)` returns `none`, `running`, `done` or `failed@stage`. Ledger times go through `parseTimestamp`. The stages, job error and log live in `<details data-testid="v2-agent-technical">`. It is **closed on success and open on failure** (`key` remounts it so a new failure opens it). **Scripts that read `v2-agent-log` after a successful deploy must open `v2-agent-technical` first.** |
| List filters | `pages/agents/agentsListParams.ts` | Covers `q`, `status` and `method`. Defaults are omitted and unknown values ignored. Filters are written with functional `setParams(prev => …, { replace: true })`. Detail, Identity, delete and convert keep the filters. **"New agent" carries them in navigation state, not the URL**, because the wizard reads `?method=` as its prefill. There is no sort param; the list has no sort control. |
| Integrate | `lib/snippets.ts`, `pages/agents/IntegrationCard.tsx` | curl, Python and JS snippets for `POST /v1/agents/{id}/invoke` and `/invoke-stream` (`X-Api-Key`, body `{prompt}`, response `text`, SSE `delta`/`error`/`done`), on `window.location.origin`. The agent id is a percent-encoded path segment. curl arguments are single-quoted (`'\''`). Python and JS use `JSON.stringify` literals. **A real key value is never rendered.** Copying uses the shared `copyText`. The keys link goes to `/v2/chat?agent=<id>&tab=api`; Chat opens its API panel from `?tab=api`. |
| TryChat | `pages/agents/TryChat.tsx`, `tryChatTurns.ts` (pure) | It shows only when `status === "active" && invoke_capability.eligible`, the same rule as Chat. It uses the same `chatApi.stream` as Chat. **A try is a real invoke and a real chat session, visible in Chat.** One panel mount is one session. The stream is aborted on unmount (`chatApi.stream(…, signal)`). IME composition is left alone. There are **no suggested-question chips**: their route exists only on the branch. |
| Inline KB | `pages/agents/inlineKbState.ts` (pure), `useInlineKb.ts`, `InlineKb.tsx`; wired into `SkillsKbCard` (`kbSlot`), the submit gate and `WizardReview` | The flow is create (name), then upload files, then attach, as if the KB had been picked. Polling runs every 5 s, and the first sync starts once when the data source is ready with no job. **Deploy waits until the KB itself is ACTIVE, not for ingestion** (branch rule). `submitBlock` returns `busy`, `waiting`, `failed` or `null`, and the UI always shows the reason. **Remove and leaving the wizard never delete the KB**, and a note says so. |

## 3. Gotchas

- The `create.stages.*` labels are uppercase in en, so a sentence must not embed them
  mid-phrase. Use "failed at stage: PROVISION".
- New zh-CN copy says **Agent** (not 智能体) and **你** (not 您), as the rest of V2 does.
- `display_name` does not exist on main. Strip it from any branch port.
- `delete_kb` returns 409 while the KB is CREATING (managed-kb spec). That is one more
  reason the wizard never auto-deletes.

## 4. Tests

- `deployStatus.test.ts`: the phrase per stage and method, the elapsed time, failure,
  and no deployment. TZ is pinned.
- `agentsListParams.test.ts`: defaults, invalid values, `view` and `id` untouched, and
  round-trips.
- `snippets.test.ts`: URLs, quoting round-trips through a shell parser and JS eval,
  hostile id and origin, and no key value.
- `tryChatTurns.test.ts`: streaming into one bubble, tool rows splitting the reply, auth
  and policy warnings, the error codes, and the cap.
- `inlineKbState.test.ts`: phases, polling, the sync-once rule, attach and detach, and
  every block reason.
- Playwright on dev, with GET stubs, non-GET requests aborted, and **exactly one real
  TryChat POST**. All knowledge-base writes are stubbed.


## Scenario: Runtime platform and billing

### 1. Scope / Trigger

Changing Runtime creation/edit controls, agent specs or any Runtime update path.
The shared V2 wizard handles zip, container and BYOC (including MCP); V3 Runtime
entrances reuse it. Harness exposes no controls and rejects explicit settings.

### 2. Signatures

`AgentSpec.runtime_config: RuntimeConfig | None` is omitted from old-spec dumps.
`POST /api/agents` and `POST /api/agents/{id}/redeploy` accept it through the existing
spec. `RuntimeConfigInput` in `lib/api.ts` mirrors the backend.

### 3. Contracts

The object has required `platform_version: V1 | V2`, required
`billing_model: consumption | committed_baseline`, and optional strict integer
`minimum_memory_mib`. New agent deployments normalize absence to V2/consumption
before snapshot recording. Legacy parsing never installs that default.

`deployer/runtime_config.runtime_config_kwargs` maps intent to the four wrapper
kwargs (`platform_version`, `resource_configurations`). AWS update omission
preserves platform but resets resources: configured baselines must be echoed.
Legacy updates reuse successful live reads; explicit consumption deliberately
omits resource configurations. `carry_runtime_config` also protects target intent
when old promotion bundles and canary artifacts omit it. Failure restore must use
proven original settings: `original_runtime_config` reads the pre-candidate version,
and direct promotion captures legacy platform/billing before publish. Missing
version proof, failed reads or unsupported metadata refuse recovery rather than
preserving the rejected candidate. Ordinary old snapshot rollback remains a
preserve-current operation. Runtime worker defaults remain unchanged.

### 4. Validation & Error Matrix

| Input | Outcome |
|---|---|
| V1 or V2 + consumption, no baseline | Accepted |
| V2 + committed + 1024/2048/4096/8192 MiB | Accepted |
| V1 + committed; missing/unsupported/float/string/bool size | Schema validation error before deployment |
| Consumption + non-null baseline; unknown config field; Harness config | Schema validation error before deployment |
| Legacy preservation read fails | Update fails; never fall back to consumption |

### 5. Good/Base/Bad Cases

Good: explicit V2/committed/4096 reaches AWS as one resource entry on create/update.
Base: a new form uses V2/consumption; a legacy edit stays on Keep existing settings.
Bad: applying a schema-wide V2 default upgrades old agents; silently omitting a
configured baseline changes their billing.

### 6. Tests Required

`test_runtime_config.py`, `test_runtime_config_deployers.py`,
`test_runtime_config_recovery.py`, API/deployer/canary and snapshot tests assert
schema strictness, fresh-only defaults, preservation,
explicit removal and unchanged worker payloads. `lib/runtime-config.test.ts`,
`lib/agent-spec.test.ts` and `runtime-config-card.test.tsx` assert defaults,
hydration/build/carry, V1 clearing, required baseline, translated controls and review.
English/zh-CN parity and full-width punctuation remain gate requirements.

### 7. Wrong vs Correct

Wrong: default every deserialized AgentSpec to V2 or omit resources on every update.
Correct: normalize only new agent creation; preserve legacy live settings and
always echo a configured baseline. Consumption is an explicit selection, not an
inference from a missing field. Display session-duration charges, not a spend cap.
