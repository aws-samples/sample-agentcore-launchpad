## Summary

Self-evolution direction **SE-012 — Memory resources: edit description and event expiry (UpdateMemory)** (branch `evo/se-012-memory-resources-edit-description-and-ev`).

### Requirement

The Memory console's `?view=resources` sub-page can edit an existing memory resource's **description** and **event expiry (days, 7–365)**.

- New route `PUT /api/memory/resources/{memory_id}` in `backend/app/routers/memory_resources.py` → `memory_admin.update_memory_resource(workspace, memory_id, *, description, event_expiry_days)` → `UpdateMemory`. Both fields optional; at least one required (422 otherwise). Strategies and namespace keys are **not** editable here.
- **Namespace-key trap (must be handled and tested):** the model documents `UpdateMemory.namespaceKeys` as "fully replaces the existing set — any key you omit is removed". The update must therefore never send `namespaceKeys` (omit the member entirely) and the hermetic test asserts the call kwargs contain exactly `memoryId`, optional `description`, optional `eventExpiryDuration`, optional `clientToken` — nothing else. If live verification later shows omission also clears keys, the fallback is to re-send `GetMemory().namespaceKeys`; leave a comment naming that fallback.
- Guard rails mirror delete: the workspace default memory is editable (only description/expiry — harmless) but the UI states that expiry changes affect every agent using it; a memory referenced by live agents shows those agents in the confirm dialog (reuse `_agents_by_memory`).
- The structural read-only guarantee stays: `tests/test_memory_console.py` must still pass (no `UpdateMemory` in `memory_console.py` / `memory.py`); the admin pair is the only place it appears.
- UI: an EDIT action on each resource row opens an inline form (description, expiry) with the shared Btn/ConfirmDialog; both locales; `docs/architecture.md` Memory console table `resources` row + `docs/api.md` (+ zh-CN) updated.

### Evidence

- `backend/app/routers/memory_resources.py:109-160` — GET list, POST create, GET one, DELETE (with `memory.in_use` 409 guard); no PUT.
- `backend/app/services/memory_admin.py:93-103` (`_detail` exposes `description`, `event_expiry_days`), `:156-201` (`create_memory_resource` builds `eventExpiryDuration`, `description`, `memoryStrategies`, namespace keys), `:234` (`delete_memory_resource`).
- `frontend/src/pages/memory/ResourcesTab.tsx:74-155` (create form state), `:282-360` (inputs for name/description/expiry/strategies) — reuse the same field components for the edit form.
- `docs/architecture.md` §The Memory console — "Read-only is structural … no wrapper or handler for … `UpdateMemory` … exists in either file, and `tests/test_memory_console.py` asserts that. The one mutating surface — the `resources` view — therefore lives in a separate pair (`services/memory_admin.py` + `routers/memory_resources.py`)". This direction extends that pair only.
- botocore `bedrock-agentcore-control/2023-06-05` `UpdateMemory` members: `clientToken, memoryId*, description, eventExpiryDuration (7–365), memoryExecutionRoleArn, memoryStrategies, addIndexedKeys, namespaceKeys ("fully replaces the existing set — any key you omit is removed"), streamDeliveryResources`.
- Availability (2026-09-05): `Bedrock AgentCore Control+UpdateMemory` isAvailableIn us-west-2 and us-east-1.

### Acceptance checks

- [ ] `cd backend && uv run pytest tests/test_memory_resources.py tests/test_memory_console.py -q` — new tests: PUT with description only / expiry only / both → `update_memory` kwargs exactly as specified (no `namespaceKeys`, no `memoryStrategies`); expiry 6 or 366 → 422; neither field → 422; response is the refreshed `_detail` projection; the read-only structural test still passes.
- [ ] Frontend: EDIT opens inline form, saves, refreshes the row; `npx tsc --noEmit && npm run lint`; i18n parity.
- [ ] `docs/architecture.md` Memory console `resources` row lists update; `docs/api.md` + zh-CN document `PUT /api/memory/resources/{memory_id}`.
- [ ] `make verify` passes.
- [ ] Live AWS check: **declared, not required by the gate** — on a dev memory created for the test with one namespace key, update the description and confirm `GetMemory().namespaceKeys` is unchanged; delete the test memory afterwards. Left for the host; record as not run.


## Verification

```
── ruff: OK
── pytest: OK
── infra ruff: OK
── infra pytest: OK
── local lifecycle: OK
── eslint: OK
── tsc: OK
── vite build: OK
── i18n_check: OK
── i18n_zh_punct: OK
════ verify: PASS ════
```

🤖 Generated with [Claude Code](https://claude.com/claude-code)
