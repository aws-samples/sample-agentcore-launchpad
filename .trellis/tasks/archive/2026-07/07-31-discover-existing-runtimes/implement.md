# Implementation plan

## 1. Runtime discovery service

- [x] Add paginated Runtime listing to the AgentCore runtime wrapper.
- [x] Add sanitized candidate projection, eligibility, and import upsert service.
- [x] Add Pydantic request schemas and typed scan/import routes before the dynamic
      `/agents/{agent_id}` route.
- [x] Add focused wrapper and API tests.

Validation:

```bash
cd backend
uv run ruff check app/services/agentcore/runtime.py app/services/runtime_discovery.py app/routers/agents.py tests/test_runtime_discovery.py
uv run pytest tests/test_runtime_discovery.py tests/test_agents_api.py -q
```

## 2. Ownership and invocation integration

- [x] Add `discovered_runtime` projections without adding it to AgentSpec creation.
- [x] Add shared invoke capability enforcement across console, Chat, and `/v1`.
- [x] Dispatch eligible discovered HTTP/A2A runtimes through the shared invoke
      service.
- [x] Make imported deletion detach-only and reject re-publish explicitly.
- [x] Verify experiment, canary, and evaluation exclusion.

Validation:

```bash
cd backend
uv run pytest tests/test_runtime_discovery.py tests/test_agents_api.py tests/test_chat_api.py -q
```

## 3. Agent Management UI

- [x] Add the fourth discovery card and `?view=discover` sub-view.
- [x] Add typed API contracts and batch import client.
- [x] Add candidate table controls and imported-row action restrictions.
- [x] Add method chip, Runtime version display, invoke-capability gating, and
      detach-only confirmation.
- [x] Add English and Chinese strings and run parity.

Validation:

```bash
cd frontend
npm run lint
npx tsc --noEmit
npm run build
cd ..
python3 scripts/i18n_check.py
```

## 4. Documentation and full verification

- [x] Update `docs/architecture.md`.
- [x] Add `.trellis/spec/launchpad/runtime-discovery.md` and index it.
- [x] Run the complete verification gate.
- [x] Run the local stack and browser-test discovery at desktop and mobile
      widths, including scan, selection, import controls, list actions, and
      no-overlap screenshots. Do not perform a real import during browser QA.
- [x] Confirm a live scan still returns all Region runtimes without AWS mutation.

Validation:

```bash
make verify
```

## Rollback points

- After step 1, routes can be removed without ledger changes if import has not
  been exercised.
- After step 2, imported rows are externally owned and safe to leave in place.
- Frontend and documentation changes are independently reversible.
