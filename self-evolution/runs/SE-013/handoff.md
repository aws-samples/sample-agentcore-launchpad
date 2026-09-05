# Direction SE-013 — Governance gateway detail manages Gateway rate limits

You are the implementation session for one direction of the AgentCore Launchpad
self-evolution loop. You are in a dedicated git worktree on a branch named
`evo/se-013-…` (check `git branch --show-current`), created from `main`. A host session
wrote this brief, will independently re-run the acceptance checks on your branch, and
owns push, PR and merge. Work only inside this worktree.

## Requirement

The Governance gateway detail (`/governance?view=gateway&…`) gains a **RATE LIMITS** panel that manages AgentCore Gateway rate limits (GA August 2026) for Launchpad-managed gateways.

- Routes under `/api/governance/gateways/{gateway_id}/rate-limits`:
  - `GET` → `{rate_limits: [...]}` — `ListGatewayRateLimits`, following every `nextToken` page; works on any gateway (read).
  - `POST` (create) → 201 with the created rate limit — `CreateGatewayRateLimit`.
  - `PUT /{rate_limit_id}` → updated rate limit — `UpdateGatewayRateLimit` (`entries` + optional `description`; `dimensionKeys` are immutable and are rejected with 422 if sent).
  - `DELETE /{rate_limit_id}` → 204 / `{deleted: true}` — `DeleteGatewayRateLimit`.
  Mutations require the Launchpad managed tag exactly like policy mutations (`_require_managed` → 409 `governance.gateway_not_managed`). Wrappers (`list_gateway_rate_limits`, `create_gateway_rate_limit`, `update_gateway_rate_limit`, `delete_gateway_rate_limit`) live in `backend/app/services/agentcore/policy.py` next to the other Gateway control-plane wrappers, taking the control client explicitly.
- Server-side validation before any AWS call (422 with a specific `detail.reason`): 1–10 dimension keys, each from the documented set — `targetName`, `toolName`, `qualifiedModelId`, `$.context.jwt.<claim>` (any claim name after the prefix), `$.context.iam.principal`, `$.context.iam.sourceIdentity`; no duplicate keys; 1–1000 entries; each entry's `dimensions` has exactly the parent keys; `*` only in trailing positions (if position N is `*`, every later position is `*`); at least one of `requests`/`tokens`/`connections` per entry; each rate config `rate` 0–10 000 000; period matrix — `requests` ∈ {`second`,`minute`}, `tokens` ∈ {`minute`}, `connections` ∈ {`second`}; description ≤ 512 chars. AWS `ConflictException` (duplicate dimension-key set, or gateway busy) maps to 409 through the existing ClientError envelope.
- Every mutation is journaled in `policy_changes` (`PolicyChange` rows, operations e.g. `rate_limit.create` / `rate_limit.update` / `rate_limit.delete`, `before` = the prior rate limit or `{}`, `requested` = the payload, `after` = the AWS response, status `succeeded`/`failed`) so the Audit view lists them. These are synchronous control-plane calls — do **not** use the async 202/operation machinery the policy mutations use; write the journal row inline around the call.
- UI panel in `frontend/src/pages/governance/GatewayDetailView.tsx`, after the POLICIES panel: table of rate limits (id, dimension keys, entry count, status chip `CREATING/ACTIVE/UPDATING/DELETING`, updated), expandable entries (dimension values + per-metric rate/period), **ADD RATE LIMIT** form (dimension-key picker over the six documented keys with a free-text claim name for `$.context.jwt.<claim>`, an entries editor with per-entry dimension values and per-metric rate + period, description), **EDIT ENTRIES** (same editor, keys locked), **DELETE** with `ConfirmDialog`. A visible note states the documented semantics: effective rate = min(service-managed, configured); propagation ≤ 30 s; fail-open; rate 0 blocks matching traffic; rate limits are evaluated **before** Policy. Disabled actions explain why through the shared `Btn` `disabledReason` (gateway not managed / status not ACTIVE / form invalid). Client-side validation mirrors the trailing-`*` rule and the period matrix. Both locales.
- Docs: `docs/architecture.md` **and** `docs/architecture.zh-CN.md` (Gateway row + §Existing Gateway governance); `docs/api.md` **and** `docs/api.zh-CN.md` for the four routes.
- IAM: **no IAM change.** The console runs on the instance role (hub) or the spoke workspace role (`bedrock-agentcore:*`); do not touch `workspace_iam.py`, `infra/`, or execution-role grants.

Out of scope: `BatchPutGatewayRateLimits` (whole-set replace), `GetGatewayRateLimit` as a separate route (the list carries full detail), token-estimation UI, rate-limit metrics/graphs.

## Repository evidence and extension points

- `backend/app/routers/governance.py:42-367` — gateway routes incl. manage/unmanage (`:72-80`), policies CRUD (`:156-251`), mode, audit (`:338`), decisions, operations — insertion point for the four routes; follow the router's dependency pattern (`require_workspace`, control client from the workspace context).
- `backend/app/services/governance.py:345-370` — `_require_managed(control, gateway_id)` (409 `governance.gateway_not_managed`), `manage_gateway`/`unmanage_gateway`, `invalidate_gateway_cache()`; `:925-965` — how a `PolicyChange` row is built (`workspace_id, gateway_id, gateway_arn, gateway_name, operation, operator=operator_identity(), status, before=json_snapshot(...), requested=json_snapshot(...)`) — reuse the helpers, set `after` and a terminal status inline; `:237-264, 601-653` gateway detail projection.
- `backend/app/services/agentcore/policy.py:70-100` (`get_gateway`, `list_gateway_targets`, `list_gateway_target_details` — pagination style), `:223-253` (`update_gateway_policy_configuration` rebuild with replace semantics — docstring style), `is_managed`/`list_tags`/`tag_managed`.
- `backend/app/models/ledger.py:230-247` — `PolicyChange` columns (`operation` String(48), `before/requested/after` JSON).
- `backend/app/core/route_policy.py:178-188` — governance routes are `MEMBER`; add the four new entries with the same posture; `tests/test_route_policy.py` fails on drift.
- `backend/tests/test_governance_gateways.py`, `test_governance_router_contracts.py`, `test_governance_policy_wrapper.py` — existing stub-client test patterns (109 governance tests); add `tests/test_governance_rate_limits.py`.
- `frontend/src/pages/governance/GatewayDetailView.tsx:334-898` — panels identity / registry / engine / iam / policies (`:750-836`) / targets (`:842-898`); `pages/governance/types.ts` for the gateway detail types; `frontend/src/lib/api.ts` is the single typed client.
- botocore `bedrock-agentcore-control/2023-06-05` (verify offline with `uv run python -c "import boto3; m=boto3.client('bedrock-agentcore-control', region_name='us-west-2').meta.service_model; print(m.operation_model('CreateGatewayRateLimit').input_shape.members)"`):
  - `CreateGatewayRateLimit(gatewayIdentifier*, clientToken, rateLimitId, description, dimensionKeys*: [str], entries*: [LimitEntry])`, `LimitEntry = {dimensions*: {key: value}, requests: [RateConfig], tokens: [RateConfig], connections: [RateConfig]}`, `RateConfig = {rate*: double, period*: "second"|"minute"}` → `GatewayRateLimitDetail{rateLimitId, gatewayIdentifier, description, dimensionKeys, entries, status, createdAt, updatedAt}`.
  - `UpdateGatewayRateLimit(gatewayIdentifier*, rateLimitId*, description, entries*)`; `ListGatewayRateLimits(gatewayIdentifier*, maxResults, nextToken)` → `{rateLimits: [...], nextToken}`; `GetGatewayRateLimit(gatewayIdentifier*, rateLimitId*)`; `DeleteGatewayRateLimit(gatewayIdentifier*, rateLimitId*)`.
- AWS docs (accessed 2026-09-05): release notes §"Gateway: Configurable rate limits" (Aug 2026); https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-rate-limits.html (components, status lifecycle `CREATING/ACTIVE/UPDATING/DELETING`, limits 50 per gateway / 1000 entries / 10 keys / rate 0–10 000 000 / description ≤ 512, fail-open, ≤ 30 s propagation, `ConflictException` on duplicate dimension keys); `…/gateway-rate-limits-dimensions.html` (six dimension keys; `*` trailing-only; most-specific match wins; unresolvable key ⇒ that rate limit is skipped); `…/gateway-rate-limits-metrics.html` (`requests` second/minute all targets, `tokens` minute inference targets only, `connections` second all targets; multiple metrics per entry evaluated independently); `…/gateway-rate-limits-examples.html` (boto3 examples: per-target RPS with a `*` default bucket; per-caller RPM by `$.context.jwt.sub`; three-key `[targetName, qualifiedModelId, $.context.jwt.sub]` requests+tokens; per-model `connections`; rate 0 to block a caller; update replaces `entries`). Rate limits are applied before Policy evaluation (AWS ML blog "Configure rate limits for AI traffic on AgentCore gateway"). `CreateGatewayRateLimit` is available in us-west-2 and us-east-1.
- `git log -S GatewayRateLimit` — never implemented or removed in this repository.

Load-bearing patterns from `CLAUDE.md`: all boto3 clients come from `app/services/aws_clients.py` (`tests/test_client_funnel.py` guards it); AgentCore client names and preview drift stay in `app/services/agentcore/`; errors go through `app/core/errors` (`AppError`, `AWS_ERROR_MAP`); every user-facing string is an i18n key with en ↔ zh-CN parity and full-width zh-CN punctuation (`python3 scripts/i18n_check.py`, `python3 scripts/i18n_zh_punct.py --check`).

Read `CLAUDE.md`, then `docs/architecture.md` §Existing Gateway governance and the Gateway/Policy rows, then the files above, before editing.

## Acceptance checks (the host re-runs these — make each one pass)

- [ ] `cd backend && uv run pytest tests/ -q -k rate_limit` — hermetic tests with a stub control client: list follows two `nextToken` pages; create sends exactly the validated payload (`gatewayIdentifier`, `dimensionKeys`, `entries`, optional `description`, plus `clientToken` if you add one); each validation rule yields 422 with its reason — unknown key, duplicate key, > 10 keys, `*` in a leading position, entry dimensions ≠ keys, no metric on an entry, `tokens` with period `second`, `connections` with `minute`, rate > 10 000 000, rate < 0, description > 512, `dimensionKeys` sent on update; mutation on an unmanaged gateway → 409 `governance.gateway_not_managed` with no AWS mutation call; stub `ConflictException` → 409 `aws.conflict` envelope; one `policy_changes` row per mutation with the right `operation` and `after`; delete → `delete_gateway_rate_limit(gatewayIdentifier, rateLimitId)`.
- [ ] `cd backend && uv run pytest tests/test_route_policy.py tests/test_client_funnel.py -q` pass.
- [ ] Frontend: panel renders list / empty / error; ADD form blocks a leading `*` and a wrong period client-side and explains why on the disabled SAVE; `cd frontend && npx tsc --noEmit && npm run lint`; `python3 scripts/i18n_check.py` and `python3 scripts/i18n_zh_punct.py --check` clean.
- [ ] Docs: `docs/architecture.md` + `docs/architecture.zh-CN.md` and `docs/api.md` + `docs/api.zh-CN.md` updated (four routes, validation rules, journaling).
- [ ] `make verify` passes in this worktree.
- [ ] Live AWS check: **declared, not required by the gate** — the host may later create a `[targetName]` / `*` requests-per-minute limit on the dev gateway, observe `ACTIVE`, and delete it. Do not call AWS; say in the report that it was not exercised live.

## Boundaries

- Run EVERY command in the FOREGROUND — never `run_in_background`, never wait on a background task. In this non-interactive session your turn ends the moment you stop issuing foreground tool calls; an unfinished background `make verify` means the run ends with nothing committed.
- **Never** `git push`, open PRs, merge, rebase or force anything. Commit on the current branch with clear conventional messages; leave the tree clean (`git status --short` empty at the end).
- **Never** run `make bootstrap`, teardown scripts, `cdk deploy`, `make dev`, or anything against AWS or the production box. No AWS calls are needed; tests stub the client.
- Do not edit `apps/studio/`, `vendor/`, `vendor-src/`, `backend/samples/frontdesk_agent`, `infra/`, or `workspace_iam.py`.
- Do not widen scope. If the requirement turns out to be wrong or already covered, stop and say so in the report instead of building something adjacent.
- Commit only files you changed (`git add <paths>`), never `git add .` or `git add docs/`.
- Save any probe output to the ABSOLUTE host path `/home/ubuntu/workspace/agentcore_launchpad/.claude/self-evolution/runs/SE-013/`. Nothing under `.claude/` is committed.
- Stay within the budget cap the host set; if you are running out, commit what is verified (backend + tests first, then UI, then docs) and report what remains.

## Final report (the host reads only this)

End with exactly these sections:

1. **Changed** — files and what changed, one line each.
2. **Verified** — the commands you ran with their pass/fail outcome (paste the `make verify` tail).
3. **Acceptance checks** — the list above, each ✅/❌ with the evidence.
4. **Not done / deviations** — anything left, anything you interpreted differently, and why.
5. **Commits** — `git log --oneline main..HEAD`.
