## Summary

Self-evolution direction **SE-013 — Governance gateway detail manages Gateway rate limits** (branch `evo/se-013-governance-gateway-detail-manages-gatewa`).

### Requirement

The Governance gateway detail (`/governance?view=gateway&…`) gains a **RATE LIMITS** panel that manages AgentCore Gateway rate limits (GA August 2026) for Launchpad-managed gateways.

- Routes under `/api/governance/gateways/{gateway_id}/rate-limits`: `GET` (list, follows `nextToken`), `POST` (create), `PUT /{rate_limit_id}` (update `entries` + `description`; `dimensionKeys` are immutable), `DELETE /{rate_limit_id}`. Mutations require the Launchpad managed tag exactly like policy mutations do; reads work on any gateway. Wrappers live in `backend/app/services/agentcore/policy.py` (or a sibling `gateway_limits.py` under `agentcore/`) and take the control client explicitly.
- Validation server-side before calling AWS (422 with a specific reason): 1–10 dimension keys drawn from the documented set (`targetName`, `toolName`, `qualifiedModelId`, `$.context.jwt.<claim>`, `$.context.iam.principal`, `$.context.iam.sourceIdentity`); each entry's `dimensions` has exactly the parent keys; `*` only in trailing positions; rate 0–10 000 000; metric/period matrix — `requests` {second, minute}, `tokens` {minute}, `connections` {second}; at least one metric per entry. AWS `ConflictException` (duplicate dimension-key set) maps to 409 through the existing ClientError envelope.
- UI panel: table of rate limits (id, dimension keys, entry count, status chip `CREATING/ACTIVE/UPDATING/DELETING`, updated), expandable entries, **ADD RATE LIMIT** form (dimension-key picker with a free-text JWT claim, entries editor with per-metric rate + period, description), **EDIT ENTRIES**, **DELETE** with ConfirmDialog. A visible note states the documented semantics: effective rate = min(service-managed, configured), propagation ≤ 30 s, fail-open, rate 0 blocks. Disabled actions explain why (`disabledReason`: not managed / status not ACTIVE). Both locales.
- Every mutation is journaled in `policy_changes` like other Gateway mutations (see `docs/architecture.md` "Every mutation is journaled locally while AWS remains the source of current state"), and the audit view shows them.
- Docs: `docs/architecture.md` Gateway/Policy rows + §Existing Gateway governance; `docs/api.md` (+ zh-CN) for the four routes.

Out of scope: `BatchPutGatewayRateLimits` (whole-set replace) and token-limit estimation UI.

### Evidence

- `backend/app/routers/governance.py:42-367` — gateway routes incl. manage/unmanage, policies CRUD (`:156-251`), mode, audit, decisions — the managed-tag gate and 202/operation pattern to mirror.
- `backend/app/services/governance.py:237-264, 601-653` — gateway detail projection (`targets`, `target_count`, actions); `backend/app/services/agentcore/policy.py:223-253` — `update_gateway` rebuild with replace semantics (style reference for wrapper docstrings).
- `frontend/src/pages/governance/GatewayDetailView.tsx:334-898` — panels identity / registry / engine / iam / policies / targets; insert RATE LIMITS after policies.
- botocore `bedrock-agentcore-control/2023-06-05` (apiVersion 2023-06-05): `CreateGatewayRateLimit(gatewayIdentifier*, clientToken, rateLimitId, description, dimensionKeys*[DimensionKey], entries*[LimitEntry{dimensions*: map, requests: [RateConfig{rate*, period*}], tokens: [...], connections: [...]}])` → `GatewayRateLimitDetail{rateLimitId, gatewayIdentifier, description, dimensionKeys, entries, status, createdAt, updatedAt}`; `UpdateGatewayRateLimit(gatewayIdentifier*, rateLimitId*, description, entries*)`; `ListGatewayRateLimits(gatewayIdentifier*, maxResults, nextToken)` → `rateLimits[]`; `GetGatewayRateLimit`, `DeleteGatewayRateLimit`, `BatchPutGatewayRateLimits` also present.
- Docs (accessed 2026-09-05): release notes https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/release-notes.html §"Gateway: Configurable rate limits" (Aug 2026); https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-rate-limits.html (components, status lifecycle, limits 50/gateway, 1000 entries, 10 keys, rate 0–10 000 000, fail-open, ≤30 s); `gateway-rate-limits-dimensions.html` (six dimension keys, `*` trailing-only, most-specific wins, unresolvable key ⇒ limit skipped); `gateway-rate-limits-metrics.html` (metric/period/target matrix). Availability: `Bedrock AgentCore Control+CreateGatewayRateLimit` isAvailableIn us-west-2 and us-east-1.
- `git log -S GatewayRateLimit` — never implemented or removed in this repository.

### Acceptance checks

- [ ] `cd backend && uv run pytest tests/ -q -k rate_limit` — hermetic tests with a stub control client: list follows pagination; create sends exactly the validated payload; each validation rule above yields 422 with its reason (bad key, `*` in a leading position, wrong period for `tokens`, rate > 10 000 000, entry dimensions ≠ keys, no metric); mutation on an unmanaged gateway → 403/409 per the existing manage gate; stub `ConflictException` → 409 envelope; a `policy_changes` row is written per mutation.
- [ ] `tests/test_client_funnel.py` passes.
- [ ] Frontend: panel renders list/empty/error; ADD form blocks invalid `*` placement client-side too; `npx tsc --noEmit && npm run lint`; i18n parity + zh-CN punctuation check.
- [ ] Docs: architecture + api (en/zh-CN) updated.
- [ ] `make verify` passes.
- [ ] Live AWS check: **declared, not required by the gate** — create a `[targetName]` / `*` requests-per-minute limit on the dev `launchpad-gw`, observe `ACTIVE`, delete it. Left for the host; record as not run.


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
