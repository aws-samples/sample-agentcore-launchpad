# Interface: per-user principal, Gateway interceptor and Dogwood engine (stage-0 contracts)

Design: `plans/DESIGN-POLICY-USER-PRINCIPAL.md` §13-§15 (final decisions 2026-10-26). This file is
the **normative contract** the three streams build against; the machine-readable form is
`backend/app/policy/contracts/*.schema.json` (JSON Schema 2020-12), the reference wire code is
`backend/app/policy/contracts/codec.py`, fixtures are `backend/tests/fixtures/policy_principal/`
(public / synthetic only) and fakes are `backend/tests/policy_principal/fakes.py`. A change to
any of them is a contract change: bump the schema `$id` version and tell every stream.

It **extends the existing Policy module** (`policy-backend.md`): the ledger, publish, presets and
exemptions and the Governance pages keep their contracts. **Since amendment 11 (2026-10-28) the
module follows the Global AgentCore Policy model** — policy ENGINES as resources, policies owned by
an engine, an engine ATTACHED to one or many gateways with a mode `LOG_ONLY | ENFORCE`, the
interceptor deciding per gateway. The workspace mode, the two workspace switches, the invoke gate,
the runtime guard, deploy-time narrowing, apply / stale and agent scope no longer exist; where an
older sentence below names them, read amendments 9–11. The platform flag
`Settings.policy_gateway_interceptor` (+ `policy_engine_endpoints`, `policy_hub_role_arn`) remains
as environment configuration = "the self-hosted engine is deployed here"; flag off ⇒ rendered
templates and API responses are byte-identical to a build without the module.

| Stream | Owner | Branch | Builds |
|---|---|---|---|
| 0 contracts | r17-bn | `cn/pp-contracts` | this file, schemas, codec, fixtures, fakes, the flag |
| A hub + console | r17-bl | `cn/pp-a-hub` | token mint (I1), carrier injection (I2), policy store / publish wiring, all console UI |
| B gateway | r17-bk | `cn/pp-b-gateway` | interceptor Lambda (consumes I1-I4, emits I7/I8), bundle writer (I3), network, audit ingest |
| C engine | r17-bn | `cn/pp-c-engine` | engine service (I4/I5), compiler, simulator backend, demo tools, E2E |

## I1 — principal token

Compact JWS, ES256 (KMS `ECC_NIST_P256`, `ECDSA_SHA_256`; P0-verified in cn-northwest-1).
Header exactly `{"alg":"ES256","kid":"<kms key id>","typ":"lp-principal+jws"}`. Claims
(`principal_token_claims.schema.json`):

| Claim | Meaning |
|---|---|
| `iss` = `launchpad-hub`, `v` = 1 | issuer / contract version |
| `ws`, `env` | workspace id, `naming.current_env_name()` (`""` on default) |
| `agt`, `aud[]` | invoked agent; audience = `agt` + its declared A2A peers (`agt ∈ aud`) |
| `sid` | the Runtime session id of this turn |
| `psid` | policy session id (`codec.psid_for(ws, sid, tsv, gen)`, `[A-Za-z0-9-]{1,128}`) |
| `tsv` | the workspace's temporal-set version the token was minted under |
| `jti`, `iat`, `exp` | nonce; `exp − iat ≤ 900` s (platform templates), `≤ 300` s for BYOC (D4) |
| `ch` | `chat` \| `console` \| `v1` \| `assistant` \| `eval` \| `canary` \| `system` |
| `p` | `{kind: user\|api_key\|system, id, label?, role? (users only: admin\|member), user_id?, attrs?}` |

D1 (revised): `api_key` is its own principal (`AgentCore::ApiKey::"<key id>"`), carries **no**
role and no parent — per-user / role rules never match it unless they name `ApiKey`. D5: eval
dataset runs carry the launching user, online eval / canary `System::"eval"` / `"canary"`. D6:
an admin continuing a member's chat is the principal (sender), not the session owner.

**Verifier** (`codec.verify_token`): checks in this order and fails with the first code —
`malformed` → `bad_header` → `unknown_kid` → `bad_signature` → `unsupported_version` /
`bad_claims` → `not_yet_valid` → `expired` → `ttl_too_long` → `wrong_workspace` → `wrong_env` →
`agent_not_in_audience`. Skew ±60 s. `agent_id` passed to the verifier is the **calling agent
resolved from `awsPrincipalArn`** (P0: the plain role ARN `…:role/<name>`, mapped through the
bundle `role_map`), never a claim. The token is never logged, never put into model input,
memory or a span.

## I2 — carrier

`tools/call` and `tools/list` carry `params._meta["launchpad/principal"] = <token>`. The gateway
runs with `passRequestHeaders = false` (P0 b: `_meta` arrives; headers would also expose the
caller's SigV4 `Authorization` / `X-Amz-Security-Token`). The transport additionally sends
Global's `x-amzn-bedrock-agentcore-policy-session-id: <psid>` (informational, migration
readiness). The interceptor deletes `_meta["launchpad/principal"]` in `transformedGatewayRequest`.

## I3 — interceptor bundle

**Shape since amendment 11: `launchpad.policy.gateway/3` — one `attachment` per gateway, no
`workspaces` block (§11.3). The paragraph below describes the `/1` – `/2` shape the Lambda still
parses during the rollout window.**

`gateway_bundle.schema.json`; written by the existing publish path (`policy_bundle.write_bundle`,
immutable) to `s3://<artifacts>/policy/gateway/<gateway_id>/v<n>.json`. No secret: the engine
HMAC key is the separate object `engine.key_s3_key`, readable by the interceptor role only. Holds
per-workspace `mode` (the existing `off|log_only|enforce` governs the interceptor), the
`gateway_interceptor` flag, counts, set versions, the governed and temporal-referenced actions,
the `role_map`, `pubkeys` (base64 SPKI = KMS `GetPublicKey`), the engine endpoints / shard count,
`exempt_system_presets` and `empty_set_decision` (D20-obs1: zero ACTIVE policies ⇒ allow without
calling the engine).
**Action ids in the bundle are entity-formatted** — `governed_actions` and
`temporal_referenced_actions` carry `AgentCore::Action::"<target>___<tool>"` (exactly
`interceptor.core.action_entity(name)`), never the compiler's bare tool name; see contract
amendment 6 (N17-obs488).
The schema's item `pattern` and the Lambda's `parse_bundle` refuse a bare tool name, and since the hub
builds every bundle through that parser a bare state can never be published (N17-obs488 point 3).

## Vocabulary and event facts (contract amendment 1, C0 spike 2026-10-26)

Verified on `dogwood-local-engine 1.1.1` / `dogwood-language 1.1.0`
(`policy-engine/crates/lp-dogwood/tests/spike_demo.rs`):

* **One namespace, `AgentCore`.** The MCP schema generator accepts exactly one namespace, so the
  whole tool-call vocabulary — and, for one vocabulary everywhere, the hub's `invoke` rules —
  uses `AgentCore`: `AgentCore::Action::"<target>___<tool>"` (every tool action is `in
  AgentCore::Action::"CallTool"`), `AgentCore::Gateway::"<arn>"`, `AgentCore::IamEntity::"<role
  arn>"` (parent `AgentCore::Agent::"<id>"`, parent `AgentCore::Workspace::"<ws>"`),
  `AgentCore::User::"<username>"` (parent `AgentCore::Role::"admin"|"member"`; attrs
  `username`, `role` required, `user_id` optional), `AgentCore::ApiKey::"<id>"` (attr `name`
  required, no parent — D1 revised), `AgentCore::System::"hub"|"eval"|"canary"`. This replaces the
  `Launchpad::User` / `Launchpad::Role` strings of the stage-0 text; the I4 fixtures are updated.
  Entities must conform to the schema (missing required attrs ⇒ fail-closed deny).
* **Tool names with `-`** (targets like `hr-database`) cannot become generated type names: the
  generator runs on an alias (`-` → `_`) and the **action ids are restored**, so policies and
  requests always use the real `hr-database___create_payout`.
* **Context of every tool action** = `input` (typed from `inputSchema`), `output?` (from
  `outputSchema`), `system: {now_epoch_s, local_hour, local_weekday}` (Longs — the 1.1 request
  value has no `datetime`; Global's `context.system.now` is therefore not available) and
  `launchpad: {agent_id, channel, sessionId}`. This is the **Dogwood-side** context the policies read; the engine
  builds it from the I4 wire request — `launchpad.agent_id` ← `agent.agent_id`,
  `launchpad.channel` ← `context.channel`, `launchpad.sessionId` ← the top-level `session`
  (the `codec.session_key` hash, which already binds ws + psid + principal), `system.*` ← the
  engine clock. Callers never send a `launchpad` block, a raw psid or time. Numbers are `decimal` (equality-only in temporal
  comparisons; ordering in stateless Cedar via `.lessThan` etc.).
* **Sessions are native pins**, not a compiler rewrite: the event schema pins `callerPrincipal =
  principal` and `sessionId = context.launchpad.sessionId` on request / response / error, which
  is Global's (session id, identity) binding. `sessionId` = the token `psid`.
* **Events**: `Event::builder_for(&["AgentCore", "Action"], "<tool>", kind)`; the request context
  and the logged fields are separate bags (inputs go to both); decisions name their determining
  policies via the `@id` annotation (`<policy_id>:v<n>`).

## Contract amendment 2 (engine service, 2026-10-26)

* `POST /v1/decide` gains optional `tsv` (the token's `tsv`) and `session_started_at` (the
  token's `iat`). The engine answers 409 `session_invalidated` when `tsv` is older than the
  workspace's temporal-set version (T4) and 409 `history_lost` when a recorded call's session
  started before the shard's store epoch. The interceptor sends both.
* `POST /v1/respond` now **requires** `principal`, `agent` and `resource` (the same objects as on
  the decide call): the response event is pinned to the same (session, principal) as its request.
  A response for an action no temporal policy references is acknowledged without writing
  (`event_seq: 0`).
* `record: true` is required for a temporal-governed action (400 otherwise) and must go to the
  session's owner shard (400 `session belongs to shard <n>` otherwise).
* `POST /v1/list-filter` is conservative: it hides a tool only when **Stage A** (the agent itself)
  is denied; per-user, input- and history-dependent rules are decided at call time.
* Control sockets are per shard: `/run/launchpad-policy-engine/control-<shard>.sock`; the hub
  installs on every shard. The engine's own audit lines go to the local JSONL
  `/var/lib/launchpad-policy-engine/audit/shard-<shard>.jsonl` (hub host), read by the hub
  ingester beside the interceptor's CloudWatch lines.
* Each `install.policies[]` item is exactly **one** statement **without** `@id` (the engine stamps
  `@id("<id>:v<version>")`); `stage` ∈ `a|bt`, temporal statements only in `bt`, `kind` must match
  the statement. A publish changes only the policies whose stamp changed: unchanged policies keep
  their history (the store is updated by diff, never re-installed).

## Contract amendment 3 (stream B items, 2026-10-26)

* **Interceptor check order** for a `tools/call` (each failure answers I8 with the reason shown):
  1. caller ARN not in `role_map` and not `hub_role` → deny `unknown_caller` (Stage A);
  2. workspace `mode = off` / zero ACTIVE policies with `empty_set_decision = allow` → allow, no
     engine call;
  3. Stage-B-governed call (any BT policy applies) with a missing / invalid token → deny
     `principal_missing` / `principal_invalid` — **before** the session check;
  4. workspace has ≥ 1 temporal policy and no valid `psid` → `-32602 policy_session_required`;
  5. the engine decide.
  A `role_map` entry with `system: true` (a platform preset) acts as its own
  `AgentCore::IamEntity` in both stages (no human principal); `exempt_system_presets` still
  allows + audits it.
* **Set handshake**: `POST /v1/decide` may carry `context.expect = {policy_set_version,
  temporal_set_version}` from the caller's bundle; the engine answers 409 `set_mismatch` with
  `have` = its installed `policy_set_version` when they differ (design §12.7: the interceptor then
  denies `temporal_set_mismatch` in `enforce`).
* **Time** is the engine's: `context.system.{now_epoch_s, local_hour, local_weekday}` are filled
  from the engine shard's clock; the interceptor never sends them.
* **Shared S3 objects** (artifacts bucket):
  * `policy/gateway/<gateway_id>/current.json` — the bundle pointer
    (`gateway_bundle_pointer.schema.json`) naming the immutable
    `policy/gateway/<gateway_id>/v<n>.json`;
  * `policy/gateway/<gateway_id>/engine-key` — the engine HMAC key, exactly **32 raw bytes**,
    created once by the interceptor bootstrap stage (`IfNoneMatch`); the engine host installs the
    same object byte-for-byte as `/etc/launchpad-policy-engine/engine-key` (0640 root:lp-policy).
    The HMAC uses the raw bytes (no hex, no trimming) on both sides.
* **Engine audit lines** carry `ws`; each shard writes them to its local JSONL and the hub mirrors
  them to CloudWatch `/launchpad/policy/engine[-env]` (ingest into `policy_decisions` can follow).
* Fixtures: `claims_valid.json` psids are now derived with `codec.psid_for` (a test asserts it);
  `gateway_bundle_pointer.json`, `jsonrpc_errors.json#unknown_caller` added.

## Contract amendment 4 — the I4 item of an INVOKE decision (2026-10-26)

The hub's invoke gate, the simulator and narrowing send `invoke` items through I5
`authorize_stateless` (never recorded). The item is an ordinary `engine_decide_request`
(fixture `engine_invoke.json`, real-engine test `test_engine_invoke.py`):

* `action` = `AgentCore::Action::"invoke"`; `record` = `false`; no `tsv` /
  `session_started_at` (invoke is never temporal).
* `resource` = `AgentCore::Agent::"<agent_id>"` of **the same agent as the `agent` block**.
  The engine builds that entity, with `method` / `system` from the block and its parent
  `Workspace::"<ws>"`, so `resource.system`, `resource.method` and `resource in
  Workspace::"<ws>"` work. A resource naming another agent has no attributes: a rule that reads
  them errors, and the item is denied with `errors[0] = "policy_evaluation_error"` (pinned in the
  fixture as a wrong item).
* `agent` = the invoked agent: `{entity: AgentCore::IamEntity::"<its execution role ARN>",
  agent_id, method, system}`, the same block the interceptor sends for that agent's tool calls.
  The invoke action has no Stage A, so the IamEntity is only carried, never a principal.
* `principal` = the turn's I1 principal object (`User` with its `Role` parent / `ApiKey` /
  `System`), exactly as for tool calls. `IamEntity` is not a valid invoke principal.
* `session` = `codec.session_key(ws, psid, kind, id)`, the same key the turn's tool calls carry,
  with `psid = codec.psid_for(ws, runtime_session_id, tsv)` (or `"none"` where the hub mints no
  psid), so `context.launchpad.sessionId` matches across invoke and tool calls.
* `context` = `{input: {}, agent_id, channel: <the token's ch>, expect: {policy_set_version,
  temporal_set_version}}`. `expect` makes a stale hub answer 409 `set_mismatch`.
* (Retired with amendment 4; the D7 closure itself is REMOVED by amendment 11.) Default-deny applies as today: the compiler's D7 closure covers tool actions only, so a
  workspace needs the invoke permits (the D20 baseline `permit_members_invoke`, rewritten:
  members, admins, `System::"hub"`). `ApiKey` matches only rules that name it (D1 revised).

## Contract amendment 6 — the bundle's action ids (N17-obs488, 2026-10-27)

Found live in M2 step 9u (t1, b230dba3): the hub-written bundle v2 of `launchpad-gw-t1` carried
`workspaces.default.governed_actions = ["hr-database___get_employee", …]` — the Dogwood
compiler's BARE tool names (`dogwood_compiler.Compiled.actions`) — while the interceptor compares
the call's `action_entity(name)` = `AgentCore::Action::"hr-database___get_employee"` by string
(`bundle.any_enforcing` for unknown callers, `core.handle` step 2 for known ones). Nothing was
governed: an operator IAM user with no token got the real `get_employee` result through the
attached interceptor (FAIL-OPEN). M1's hand-written probe bundle used the entity form, the
fixture `gateway_bundle.json` too, so the interceptor side was always the contract.

* **The entity form is canonical** for `governed_actions` and `temporal_referenced_actions` in
  I3: `AgentCore::Action::"<tool name>"` with Cedar string escaping, byte-identical to
  `interceptor.core.action_entity` / `principal_entities.ref("Action", name)`.
* **The hub converts at the ONE place it composes the lists** —
  `policy_engine_sync.gateway_actions()` when `engine_state` is written after an install
  (`install_workspace`); `policy_gateway_publish._states` → `GatewayWorkspaceState` → the bundle
  copy it verbatim. The function refuses an already-formatted id (no double conversion) and an
  empty name.
* **A state written before this amendment is repaired automatically**: `engine_state` items
  that are not entity ids (`policy_engine_sync.engine_state_is_legacy`) make the workspace a
  stale set — `reconcile_workspace` (switch re-enable, 409 `set_mismatch`) and the gateway
  reconciler (startup, every 300 s) re-install it (`ps-<n+1>`, entity ids) and rewrite the bundle
  (`v<n+1>`), so a deployed fix repairs a live gateway without an operator step; a refused
  re-install is logged and retried on the next pass, the previous set stays live.
* **The engine install message (I5) is unchanged**: the compiler keeps bare names in
  `actions` / `temporal_referenced_actions` and the install items' statements; `ps-<n>` on the
  shards is not affected, the engine's `must_record` still derives the recorded tools itself.
* The other string-compared pieces were checked and are consistent: `role_map` keys are IAM role
  NAMES on both sides (`agent_iam.role_name_for` ↔ `core.role_name(arn)`); policy ids /
  `<id>#k` split ids are never compared by the interceptor (the engine answers them in
  `determining`, the interceptor records them); `hub_role` is a role name on both sides.
* Tests: `tests/policy_principal/test_governed_actions_roundtrip.py` — a REAL compile on a live
  local engine → `engine_state` → `_states` → `build_gateway_bundle` → the Lambda's
  `parse_bundle`: `any_enforcing(action_entity("hr-database___create_payout")) == (True, False)`
  under `enforce`; a known caller without a token is refused `principal_missing` (never passed);
  an unknown caller is `unknown_caller`; a writer guard fails on any item not starting with
  `AgentCore::Action::"`.
## Contract amendment 7 — the bundle describes every workspace; "the interceptor decides" needs a published workspace (re-review F1 / F2 / F5, N17-obs490 / obs491 / obs494, 2026-10-27)

**Superseded by amendment 11 (2026-10-28)** for the per-workspace blocks, `published`,
`workspace_unpublished` and the switch-driven rewrites; the writer rules (forward-only CAS, drift
rewrite, `bundle_pending` retry, the Lambda S3 client config) stay in force.

* **I3 block, `published: boolean`** (`workspaces.<ws>.published`). The hub now writes bundle
  schema **`launchpad.policy.gateway/2`**, where `published` is REQUIRED on every block (the
  Lambda's parser and the JSON schema's `if/then` refuse a /2 block without it); schema `/1` — the
  M1 hand-written shape — stays accepted with an absent `published` read as `true` (back-compat
  only). `true` iff the engine holds the hub's CURRENT ACTIVE set
  for the workspace (`policy_engine_decide.engine_decides`: global + workspace switch,
  `engine_publish`, a set installed, digest match) and the install left its `engine_state`
  (`policy_gateway_publish.workspace_published`). The writer (`_states`) emits a block for
  **EVERY workspace on the gateway**, not only the installed ones: the live `mode`, the live switch
  (`gateway_interceptor` = the workspace flag AND the global `Settings.policy_gateway_interceptor`),
  `published`, and the installed set's facts when there is one — a never-installed workspace
  carries zero counts and `policy_set_version: "unpublished"`.
* **Interceptor (REQUEST, known caller):** `enabled: false` → pass, no row (as before);
  `enabled: true` and `published: false` → deny **`workspace_unpublished`** (I8 `reason_code`,
  stage `final`; `log_only` → `would_deny` row + pass) before any token or engine step — the
  interceptor never passes a governed workspace unchecked because nothing was installed for it;
  a KNOWN caller whose workspace has **no block at all** (a bundle older than the workspace) is
  the same deny with a row; UNKNOWN callers are judged by `any_enforcing` exactly as before.
* **Hub:** `policy_enforcement.interceptor_decides` / `carrier_decision` = the deploy renders the
  carrier AND the gateway's interceptor is verified attached AND
  `policy_gateway_publish.interceptor_governs(ws)` = `workspace_published(ws)` AND
  `engine_state.bundle_pending` is false (the bundle write after the install succeeded). Only then
  is the runtime guard disarmed and the narrowing Stage-A-only; a switch off, an install not yet
  written, a stale install or a detach keep the guard armed and the full narrowing. The deploy
  snapshot records it (`policy_applied.interceptor_decides`, written only when true;
  `mark_applied(interceptor=)`, the deployer passes `job_carrier_decision()[1]`) and `is_stale`
  compares it with the rule now, beside the carrier fingerprint — a workspace that stops governing
  leaves its disarmed agents `policy_stale` until an apply re-arms them.
* **Writes (F2):** `set_settings` rewrites the bundle on EVERY change of `gateway_interceptor`,
  `engine_publish` or the mode (not only after an install), setting `bundle_pending` first so a
  failed write is retried; `publish_gateway` is gated on the bundle settings (`configured()`) and
  a gateway only — never on `publish_enabled` — so a switch turned OFF reaches the Lambda (block
  disabled / unpublished) and a retired set never stays live at the gateway. The reconciler (every
  300 s and at startup, now also with the global switch OFF) rewrites a `bundle_pending` gateway
  and, for every other gateway, compares the composed bundle with the live one (`same_content`:
  `content_digest` = sha256 of the canonical body minus `version` / `issued_at`) and writes on
  drift — the global kill switch, a flag or mode changed while the hub was down, a hand-written
  bundle; at most ONE drift rewrite per pass, through the same forward-only CAS writer, and an
  unchanged state writes nothing (no endless `v+1` series).
* **Stale list (intended):** because `is_stale` now compares the deploy's `interceptor_decides`
  with the rule now, every toggle of `gateway_interceptor` / `engine_publish` (and the global
  switch, an attach / detach, an install not yet written) lights up every governed agent of the
  workspace as `policy_stale` until an apply re-arms or re-disarms its guard — that is the
  console telling the operator the live versions no longer match the posture, not a defect.
* **Lambda S3 client (F5):** `boto3.client("s3", config=Config(connect_timeout=0.5,
  read_timeout=1.5, retries={"max_attempts": 1}))` (`handler.s3_client_config`) — a stalled read
  becomes a refresh failure in ~2 s, inside the 5 s function timeout; the SWR source keeps serving
  the last bundle until `max_stale`, then `bundle_stale` (fail closed). Never the gateway's own
  timeout.
* **403 `policy.denied` detail (lead ruling on re-review I7):** `policy_names` is `{}` for a
  `/v1` (API-key, channel `v1`) caller — ids only; console channels keep the names.
* Tests: `tests/policy_principal/test_gateway_coverage.py` — REAL composed bundles
  (`policy_gateway_publish.compose` from a real `engine_state`, parsed by the Lambda's
  `bundle.py`) through the Lambda for every switch combination × known / unknown caller, the
  stale install, the no-block caller, switch OFF rewrites, the failed-write reconcile, the global
  kill switch; `test_principal_deploy.py` (publish state in the disarm matrix and the stale
  signature); `test_bundle_swr.py` (client config, stalled read); `test_policy_invoke_gate.py` (v1
  names).

## Contract amendment 8 — install version CAS, partial installs, set drift, restart replays (N17-obs492 = re-review F3 / F6 / F9, 2026-10-27)

* **I5 `install` is a version CAS.** `install.force` (default `false`). Without it the engine
  refuses a `policy_set_version` that is not NEWER than the installed one for the workspace —
  same `<prefix>-<n>` family with `n` not greater, or the identical string — with
  `ok: false, errors: ["policy_set_version ps-7 is not newer than the installed ps-7 (force: true
  to re-install or roll back)"]`; nothing changes on the shard. Another prefix family is newer
  (the hub's `ps-1` replaces a hand-installed `ps-m1-1`). `force: true` installs regardless: the
  hub sends it only to roll a shard back after a partial install and to re-sync shards that
  disagree (`policy_engine_sync.reconcile_workspace`, shard-disagreement path).
* **A partial `install_all` is rolled back.** The hub installs one set at a time per process
  (`policy_engine_control._INSTALL_LOCK`); when a shard refuses or cannot be reached, the shards
  that already accepted are re-installed with the previous set (`force: true`, from the last
  install every shard accepted in this process) BEFORE the publish fails with 409
  `policy.engine_install_failed`, so the shards never stay on different sets. A rollback the
  engine refuses or a hub that restarted in between (no previous set known) is logged; the
  reconcile's shard-disagreement check then re-syncs with `force`.
* **Gateway `temporal_set_mismatch` rows drive a reconcile.** `policy_audit.ingest_gateway_events`
  asks `policy_engine_decide.schedule_reconcile(ws)` (one per workspace per minute) for every
  new `source: gateway` row whose `reason` is `temporal_set_mismatch` — the Lambda's 409 path
  now heals the engine like the hub's own decide path does.
* **Set drift fails closed in `enforce` (F6).** When the engine's answer carries a
  `policy_set_version` other than the bundle's, the interceptor treats it exactly like a 409
  `set_mismatch`: forced bundle refresh + deny `temporal_set_mismatch` in `enforce`;
  `log_only` keeps the note (`engine set ps-99 ≠ bundle ps-17`) and lets the call through.
* **Restart replays (F9).** The nonce cache is per process; for the first `SKEW_S + NONCE_TTL_S`
  (360 s) after start the engine also refuses a request whose `X-LP-Ts` is older than the process
  start minus 2 s (`auth::START_GRACE_S`), so a signed request captured before a restart cannot be
  replayed after it; a live caller's timestamp is never older than "now". Residual: a replay whose
  timestamp falls inside those 2 s before the start.

## Contract amendment 9 — Global parity: Gateway tool calls only (USER ruling 2026-10-27 14:10Z)

The user ruled 「我们不需要扩展，保持和 global 同样的策略就可以」 / 「去掉」. Policies decide **Gateway tool
calls only**, exactly as Global AgentCore Policy:

* **Nothing attached ⇒ allow.** A gateway without the interceptor decides nothing.
* **Attached + installed set ⇒ Cedar default-deny.** A call passes only when a Stage A permit (the
  agent) AND a Stage BT permit (the user / system principal) apply and no forbid matches; an
  installed EMPTY set denies (`policy_empty_set_decision = deny`); a workspace the bundle marks
  `published: false` denies `workspace_unpublished` (attached engine, nothing installed for it).
* **No invoke policy.** The hub invoke gate, the `invoke` action's hub consumers
  (`policy_bundle.invoke_request`, `policy_engine_decide.invoke_item` / `evaluate_invoke`, the
  invoke `policy_denied` frame, `policy_invoke_fail_mode`) and the D20 template
  `permit_members_invoke` are removed. Chat / `/v1` / eval / canary turns run under auth and
  workspace grants; `/v1` API keys are plain auth again.
* **No deploy-time narrowing, no runtime guard.** A deploy renders every declared tool; the hub's
  cedarpy narrowing of the Agent principal is removed. The principal CARRIER is unchanged
  (`principal_carrier_for`), stamped as `policy_applied.carrier`; `is_stale` ⇔ the next deploy
  would render a different carrier; `POST /api/policies/apply` = redeploy to carry the token.
  **Superseded by amendment 11 §11.5: the carrier renders on every deploy; `is_stale` and
  `apply` are removed.**
* **Workspace mode** (`off | log_only | enforce`) = the gateway bundle block's mode only.
  Switching to `enforce` without `would_deny` evidence needs the typed workspace id AND a
  non-empty `override_reason` (GLOBAL-DEVIATIONS item 7(i), 2026-10-27). **Superseded by
  amendment 11: the mode is the gateway attachment's `LOG_ONLY | ENFORCE`; no evidence gate.**
* **New policies start `log_only`** (GLOBAL-DEVIATIONS item 5): `create_policy` sets
  `enforcement_mode = log_only`; the first publish installs a shadow; promotion is the existing
  `PUT /{policy_id}/enforcement-mode {active}`. Rollout order: publish → promote → `enforce`.
* **Vocabulary.** Hub side: `Launchpad::Action::"invoke"` is removed from the D20 schema
  (`backend/app/policy/schema.cedarschema.json`) and `CONTEXT_ATTRIBUTES`; a NEW statement naming
  it fails validation / publish with 422 `policy.invalid_statement` ("the invoke action no longer
  exists: policies decide Gateway tool calls only"). COMPAT: an EXISTING stored statement naming
  `invoke` (t1's `m2_members_invoke`) is DROPPED by `rewrite_d20` with a warning — the engine
  install succeeds without it (no 422 on existing rows, no reconcile 409) until the operator
  retires the policy. Engine side: `invoke` was removed from the lp-dogwood template/schema by
  pg-engine (cn/pg-engine 5daedb8d, 2026-10-28 — N17-obs501 CLOSED by this batch; R1 + R2 ride
  the amendment-11 deploy). Amendment 4 (the invoke item) is retired.
* **Baseline templates** replacing `permit_members_invoke`: `permit_workspace_agents_call_tools`
  (`permit(principal in Launchpad::Workspace::"__WORKSPACE_ID__", action == call_tool, resource)`,
  Stage A) and `permit_users_call_tools` (member / admin / `principal is Launchpad::System` /
  `principal is Launchpad::ApiKey`, Stage BT — the API-key permit is the lead's option D of
  2026-10-27: a `/v1` key stands where Global's M2M client does, covered by the baseline and
  narrowed by `forbid(principal == Launchpad::ApiKey::"<id>", …)`).
* **How the two baselines compose on the engine (verified by replay 2026-10-27, lead question
  via r17-bl2) — SUPERSEDED by amendment 11 (pg-engine 5daedb8d): the D7 closure permit is
  REMOVED. Stage BT is plain Cedar default-deny: a user, API key or System principal without a
  covering permit is denied at Stage BT, so the users baseline permit (`permit_users_call_tools`)
  is REQUIRED for any user traffic; the engine reports `has_user_permit` per installed set and
  the compiler warns `no_user_permit` when a set has none. Historic text:** `permit(principal in Launchpad::Workspace::"<ws>", …)` matches the agent chain
  (`IamEntity in Agent in Workspace`) at Stage A only — a `User` has the `Role` parent and no
  `Workspace` parent, so the Workspace permit does NOT match users at Stage BT. What makes "the
  Workspace permit alone lets every user call every tool" true is the compiler's **D7 closure
  permit**: Stage BT permits every tool that NO user-stage permit covers, so with zero user permits
  the closure covers everything (`determining = closure` at `bt`). Consequences: (1) the Stage-A
  agent permit alone = "agents governed, users ungoverned" — by design (D7); (2) as soon as ANY
  user-stage permit is installed the closure shrinks to the tools it does not cover, so every BT
  principal that should pass needs its own permit — hence `permit_users_call_tools` lists member,
  admin, `System` AND `ApiKey` (with the users baseline installed, closure = ∅ and an API key
  without its permit is denied at BT); (3) an agent calling without a token on such a set is
  `stage_a allow / bt deny` on the engine — consistent with the interceptor, which refuses a
  token-less non-exempt caller as `principal_missing` before the engine. `default-policies-v4.json`
  therefore stays as is (workspace permit + the four-statement users permit; the ApiKey permit is
  required, not redundant).
* **Audit.** `policy_decisions` rows come from the gateway (`source: gateway`) and the simulator;
  no hub invoke rows are written; historic rows stay readable.
* **Spec and owners:** `designs/policy-default-20261027/GLOBAL-PARITY-NOTE.md` (hub r17-bl,
  vocabulary/templates r17-bn, console r17-bp, operator r17-bk).

## Contract amendment 10 — evaluation and system agents are exempt from policy (USER ruling 2026-10-27 「评估 agent 和助手等系统 agent 不要用 policy 限制」; lead option α)

**Who is exempt.** System presets (`Agent.system_key`: the architect assistant, the eval-worker
preset, the Skill Lab worker), canaries, every agent **while an evaluation run invokes it** (the
eval worker / eval-ts agents, including a user's ordinary agent for that run), and any other
system agent. NOT exempt: a user's ordinary agent on a chat, console or `/v1` turn.

**Why a token is still minted for eval traffic.** The Gateway sees the calling agent's execution
role — the same role on an eval turn and on a chat turn — so "this call belongs to an eval run"
cannot be read from the caller. The only unforgeable carrier of that fact is a hub signature.
Hence D5 becomes: **one hub-signed token per evaluation job**, `p = System::"eval"`, `ch = eval`
(canaries keep `System::"canary"`), audience = the run's agents, TTL kept alive by the existing
I9 refresh loop, deleted at the terminal state; `tokens.json` v2 = `{schema, job_id, written_at,
token}` — no per-session, no per-user minting, no role re-derivation, no `BulkMinter` pacing.
**Plain statement for the operator: an evaluation run on a governed agent with NO token at all
is `principal_missing` and is DENIED in `enforce` (passed with a `would_deny` row in
`log_only`) — the ruling cannot be met by "no token", only by a system token.**

**Interceptor (stream B, r17-bp, branch `cn/r17-bp-exempt`; Lambda code ⇒ R2 on t1 and default).**
1. EXEMPT caller (`role_map[*].exempt`, see 3): pass BEFORE the token step with an `allow` row
   `reason: system_exempt`, no engine call — **unconditional**: the bundle field
   `exempt_system_presets` is no longer consulted (the hub writes it fixed `true` until the schema
   is next bumped) and the Settings knob `policy_exempt_system_presets` is deleted (hub
   `policy_enforcement.is_exempt` = `bool(Agent.system_key)`).
2. NEW step right after the principal token is verified: a principal of kind `system`
   (`System::"eval"`, `System::"canary"`, any `ch`) ⇒ `allow` row `system_exempt` (row principal
   `{kind: system, id: <id>}`, `session_psid` = the token's psid), pass, **no `/v1/decide`, no T9
   session requirement, no temporal recording**. Before this a System token was evaluated by the
   engine (the users baseline permits it, a forbid such as `payout_admin_only` would deny it) — under
   the ruling it is never evaluated. An INVALID System token is still `principal_invalid`: exemption
   needs a verified token.
3. `role_map[*].exempt: bool` — additive optional field of the I3 bundle (schema `/2`, version
   unchanged; `gateway_bundle.schema.json` role_map entry gains `exempt`, not required). Absent ⇒ the
   value of `system`. The hub writer `policy_bundle.gateway_role_map` sets `exempt =
   bool(Agent.system_key)`. A non-boolean value is `bundle_invalid` (the last good bundle stays in
   force).
4. RESPONSE under a System token: no `/v1/respond`, `tools/list` returned unfiltered.
5. Row shape unchanged (I7 `launchpad.policy.decision`, `policy_ids: []`, `stage: final`; argument
   names only, as before).
Tests: `tests/policy_principal/test_interceptor.py` (System token on a governed agent with a forbid →
allow + `system_exempt` row + no engine call; a member on the same tool still denied; invalid System
token → `principal_invalid`; response / list; non-boolean `exempt` → `bundle_invalid`),
`test_gateway_attach.py` (the composed role_map carries `exempt` and round-trips through the Lambda
parser).

**Hub (stream A, r17-bl2).** `eval_tokens.prepare` mints the single system token (I9 v2);
`refresh` re-signs it (no principal re-derivation); the worker (`strands_eval/principal_tokens.py`)
puts the one token in `launchpad_meta.principal` for every session; a missing file no longer marks
sessions `principal_missing` — the run proceeds token-less and the Gateway decides (presets pass;
an ordinary agent is denied, visible in Decisions). `PATH_EVAL` circuit and `BulkMinter` go.
Chat / console / `/v1` turns: unchanged (user or API-key principal).

**Not changed by this amendment:** the `/v1` API key stays a principal (`Launchpad::ApiKey`),
covered by the users baseline (option D above) — external callers are never exempt.

**Tests (hub).** One token per job, `System::"eval"` claims, refresh re-signs, terminal delete,
the worker injects the same token into every session, missing file ⇒ token-less run; the
interceptor tests are listed above.

## Contract amendment 11 — policy engines and gateway attachments: the Global AgentCore Policy model (USER ruling 2026-10-28 09:40–10:05 Beijing, `designs/policy-global-model-20261028/REQUIREMENTS.zh.md`; lead API draft + rulings 10:20 Beijing)

The user ruled that the policy module's **model, names, console entry points and decision
semantics match Global AgentCore Policy**: the policy engine is its own resource, policies belong
to an engine, an engine is attached to one or many gateways, and the interceptor on a gateway
decides the tool calls through that gateway. CN has no managed Policy service, so the engine stays
self-hosted (Dogwood) — but every surface presents it as Global does. This amendment supersedes
the workspace-posture model: amendment 7 (per-workspace bundle blocks, `published`, the switches
that rewrote the bundle), amendment 9's rows *Workspace mode*, *Stale* and the `apply` route, the
Stream A bullets *Gates* and *Agent scoping on the engine*, and the Stream A6 bullets *Switches*
and *Tab visibility*. Everything else in this file stays in force; where an older sentence names
a workspace mode or switch, read this amendment.

### 11.1 Model

* **PolicyEngine** (`policy_engines`: `id` = `pe_<12 hex>`, `workspace_id`, `name`
  `^[A-Za-z][A-Za-z0-9_]{0,47}$` unique per workspace, `description`, `status active | deleting`,
  `created_by`, `created_by_name`, timestamps). A workspace may hold many engines. Global:
  `CreatePolicyEngine {name, description}`; the workspace scope is CN's tenancy boundary.
* **Policy** belongs to exactly ONE engine: `policies.engine_id` is NOT NULL once the migration
  has run; `scope` and `agent_ids` are removed (§11.7). `enforcement_mode active | log_only` is
  Global's `enforcementMode ACTIVE | LOG_ONLY`; `status draft | active | retired`, versions and the
  journal are unchanged. A new policy still starts `log_only` (amendment 9).
* **Attachment** (`policy_engine_attachments`: `gateway_id` PK, `engine_id`, `mode`, `attached_by`,
  `attached_at`, `updated_at`) — Global's `Gateway.policyEngineConfiguration = {policyEngineArn,
  mode}`. A gateway carries at most one attachment: one engine and one mode, `mode ∈ {LOG_ONLY,
  ENFORCE}` (Global's spelling; the per-policy `log_only` keeps the hub's spelling). One engine may
  be attached to many gateways. The table is distinct from `policy_gateway_attachments`
  (`PolicyGatewayAttachment`: whether the interceptor Lambda is attached to the gateway as last
  verified by `gateway_bootstrap.set_interceptor`) — the first is "which engine, which mode", the
  second is "is the Lambda on the gateway".
* **Routes** (admin unless noted): `GET/POST /api/policy-engines`, `GET/PUT/DELETE
  /api/policy-engines/{eid}` (`DELETE` → 409 `policy_engine.attached` while any attachment exists;
  409 `policy_engine.has_policies` unless `?force=true`, which retires them); the per-policy routes
  keep their shapes under `/api/policy-engines/{eid}/policies[/{pid}…]` (`publish` carries NO
  confirmation fields); `validate`, `format`, `simulate {gateway_id, …}` and
  `entities?gateway_id=` under the engine; `GET /api/policy-engines/{eid}/decisions?gateway_id=`,
  `GET …/changes`. Attachment: `GET / PUT / DELETE /api/governance/gateways/{gid}/policy-engine`
  (lead ruling 7; `PUT {engine_id, mode}` → 200, idempotent — changing the engine or the mode
  rewrites that gateway's bundle; 409 `gateway.policy_engine_busy` while a previous attach or
  detach is in flight; 422 `gateway.policy_engine_mode_invalid`; `DELETE` → 200 — the bundle is
  rewritten with `attachment: null` and the interceptor is detached from that gateway; the
  decisions history is kept). **Foreign gateways (lead ruling 24, 2026-10-28):** the gateway list is account-wide, so a
  gateway whose interceptor names ANOTHER host's Lambda (another Launchpad environment in the
  same account) is `foreign: true` on `GET …/policy-engine`; `PUT` on it is refused with 409
  `gateway.policy_engine_foreign` and nothing is written; a foreign interceptor is never
  detached or replaced (`set_interceptor` already refuses to replace another interceptor).
  `GET /api/policies*` and `POST /api/policies/apply` answer 410
  `policy.moved` with `{engines_url}` for one release. `GET /api/capabilities.policy =
  {engines, attached_gateways}`.
* **Journal.** `policy_changes.operation` gains `policy_engine.create | update | delete |
  migrated` and `gateway.attach | gateway.detach` (`requested {gateway_id, engine_id, mode}`,
  `after` = the attachment or `null`).

### 11.2 Engine install — one Dogwood set per engine

* **The engine holds one set per ENGINE, keyed by the engine id.** The I5 `install.ws` and the I4
  `decide.ws` / `respond.ws` / `list-filter.ws` fields carry the **engine id** (`pe_…`) — the
  field keeps its name so the engine binary is unchanged (no R1 for this amendment; the Lambda
  changes ⇒ R2). `codec.session_key(<engine id>, psid, kind, id)` likewise; psids already embed
  the workspace and the Runtime session, so two gateways on one engine never share a session.
* `policy_set_version` of an engine's set is `<engine id>-<n>` (`n` = `policy_engines`' install
  sequence) — the `<prefix>-<n>` family the install CAS of amendment 8 compares; a hand-installed
  `ps-m1-1` is another family and is replaced. `temporal_set_version` moves per engine exactly as
  it moved per workspace (T4; the token's `tsv` claim is the attached engine's version for the
  agent's gateway at mint time, `0` when the gateway is unguarded).
* **A publish, retire or enforcement-mode change of a policy re-installs its ENGINE's set on every
  shard before the change commits (CAS, partial-install rollback, 409 `policy.engine_install_failed`
  — amendment 8 unchanged) and then rewrites the bundle of EVERY gateway the engine is attached
  to**; one engine on two gateways therefore yields two bundle writes that name the same set. An
  engine attached nowhere is installed too (the simulator decides on it); its bundles do not exist.
* The engine schema no longer declares the `invoke` action (pg-engine 5daedb8d closes N17-obs501;
  the hub's D20 schema lost it with amendment 9) — a stored statement naming it is dropped by
  `rewrite_d20` with a warning, a new one is 422 (unchanged).
* The engine install is no longer gated on any switch: the engine deployment itself
  (`Settings.policy_engine_endpoints`, `policy_hub_role_arn`, `policy_gateway_interceptor`) is the
  only precondition, and it is environment configuration — Global needs none because AWS hosts
  the engine.

### 11.3 The bundle — `launchpad.policy.gateway/3`, one attachment per gateway

```
{schema: "launchpad.policy.gateway/3", gateway_id, gateway_arn, version, issued_at, env, hub_role,
 pubkeys, role_map{<role>: {agent_id, ws, method, system, byoc, exempt}}, engine{endpoints, shards, key_s3_key},
 empty_set_decision: "deny",
 attachment: null | {engine_id, mode: "LOG_ONLY" | "ENFORCE", policy_set_version, temporal_set_version,
                     active_count, temporal_count, governed_actions[], temporal_referenced_actions[]}}
```

* **The `workspaces` block is removed.** The bundle describes ONE gateway and carries at most one
  attachment; the set facts (`policy_set_version`, counts, the entity-formatted action lists of
  amendment 6) live INSIDE `attachment`, so `attachment: null` carries no set facts at all.
  `published`, `gateway_interceptor` and `mode` per workspace no longer exist; the reason code
  `workspace_unpublished` no longer exists. `exempt_system_presets` is dropped (the exemption is
  unconditional — amendment 10 — and read from `role_map[*].exempt`, REQUIRED in `/3`).
  `empty_set_decision` is the constant `"deny"` (the Settings knob `policy_empty_set_decision` is
  deleted); the Lambda refuses a `/3` bundle with any other value as `bundle_invalid`.
* Schema `/1` and `/2` stay parseable by the Lambda during the rollout window (an attached engine is
  read from their `default` block as ENFORCE/LOG_ONLY per its `mode`); the hub never writes them
  again. `gateway_bundle.schema.json` `$id` moves to `v3`; `attachment` is required in `/3`.
* **Detach ⇒ the bundle is rewritten with `attachment: null` AND the interceptor is detached from
  that gateway** (`gateway_bootstrap.set_interceptor(attach=False)`, `policy_gateway_state.record_detach`).
  Attach ⇒ the bundle is written first, then the Lambda is attached (first attach) — the
  interceptor never runs a gateway whose bundle it cannot read. The reconciler (every 300 s and at
  startup) compares the composed `/3` bundle of every attached gateway with the live one and rewrites
  on drift, one gateway per pass, through the forward-only CAS writer (amendment 7's writer rules
  are unchanged).

### 11.4 What the interceptor reads and decides

* **The interceptor reads `attachment` only.** `attachment: null` ⇒ every call through the gateway
  passes with the token stripped and no row — the gateway is **unguarded**. The hub also detaches
  the Lambda on `DELETE`, so either state alone — Lambda absent, or `attachment: null` — makes the
  gateway unguarded; nothing attached ⇒ allow (amendment 9).
* With an attachment, the mode governs every caller through that gateway — the caller's workspace
  (`role_map[*].ws`) names the row's `ws` and nothing else: **`ENFORCE` refuses a `deny` with the
  I8 error; `LOG_ONLY` records `would_deny` and passes.** Rows carry `mode` as `LOG_ONLY` or
  `ENFORCE` (lead ruling 4; the `off | log_only | enforce` spelling stays accepted on historic
  rows), `gateway_id` (NOT NULL on `source: gateway` rows, filled from the bundle's gateway) and
  `engine_id`; the I7 line gains `gateway_id` and `engine_id` and the console view adds
  `gateway_name` and `log_only_would`.
* Steps, in order, for a `tools/call` on an attached gateway (unchanged where not named):
  unknown caller ⇒ `unknown_caller` (deny in ENFORCE, `would_deny` + pass in LOG_ONLY);
  `attachment.active_count == 0` ⇒ `empty_set` deny (an attached engine with no ACTIVE policy — the
  Global default-deny); a tool neither governed nor temporal-referenced ⇒ pass, no row (the
  short-cut is unchanged); exempt caller ⇒ `system_exempt` allow (amendment 10); missing / invalid
  token ⇒ `principal_missing` / `principal_invalid`; verified System principal ⇒ `system_exempt`;
  the engine `/v1/decide` on the set `attachment.policy_set_version` names — set drift
  (`temporal_set_mismatch`), `engine_unavailable` and the SWR behaviour are those of amendments
  7 and 8 and N17-obs498 (fail closed in ENFORCE, note + pass in LOG_ONLY).
* `tools/list` is filtered in `ENFORCE` only, unfiltered on any failure (unchanged).
* **System exemption is unchanged** (amendment 10): `role_map[*].exempt` callers and verified
  `System::"…"` principals pass with a `system_exempt` row and no engine call on every gateway,
  whatever the attachment's mode.

### 11.5 Carrier and deploy

The principal carrier renders on **every** deploy of an agent that declares Gateway tools under a
platform template (`principal_carrier_for`: Gateway tools, not an exempt preset, the platform
flag on) — there is no workspace switch to go stale on, so `policy_stale`, `stale_agents` and
`POST /api/policies/apply` are removed (`apply` answers 410 for one release; the console button
is gone). `policy_applied` keeps `carrier` as information. An agent whose live version was
deployed WITHOUT the carrier is `principal_missing` at an ENFORCE gateway until it is redeployed:
the migration journal row lists them (`after.agents_without_carrier`) for the operator.

### 11.6 Migration (startup, idempotent, per workspace — `policy_engine.migrated`)

1. A workspace with ≥ 1 policy and no engine gets `default_policy_engine` (「默认策略引擎」) and
   every policy of the workspace gets `engine_id` = that engine. Agent-scoped rows are BAKED, not
   dropped: an ACTIVE `scope = agents` row is republished as the statement the engine compiled
   for it (`scope_statement`, byte-identical), journal `policy.publish` reason `scope baked`.
2. If the old `policy_settings` had `gateway_interceptor AND engine_publish` on and the workspace
   gateway's interceptor was verified attached, the gateway gets the attachment `{engine_id, mode}`
   with `enforce → ENFORCE`, `log_only → LOG_ONLY`; `off` ⇒ no attachment and the interceptor is
   detached.
3. That gateway's bundle is rewritten as `/3`; the old settings columns (`mode`,
   `gateway_interceptor`, `engine_publish`, `evidence_window`, `invoke_fail_mode`,
   `runtime_guard`) are dropped from the model (the ledger keeps the columns until the next
   `_migrate` pass drops them); every `policy_decisions` row is kept, `gateway_id` filled from
   the bundle's gateway when missing.
4. One journal row `policy_engine.migrated` per workspace (`after {engine_id, policies, attachment,
   agents_without_carrier}`). **Running the migration again is a no-op: no second engine, no second
   attachment, no second journal row, and the bundle is not rewritten.**

### 11.7 Removed vocabulary and behaviour

| removed | replaced by |
|---|---|
| workspace mode `off \| log_only \| enforce`, `policy_default_mode`, `PUT /api/policies/mode`, the evidence-or-override gate (`evidence_window`, `evidence`, 409 `policy.confirmation_required`, `confirmation_name`, `override_reason`, 422 `override_reason_required`) | the attachment's `mode LOG_ONLY \| ENFORCE` per gateway; no evidence gate (REQUIREMENTS §1 row 7 — Global's API has none; the upstream Launchpad console's `evidence_required` gate is consciously NOT ported). N17-obs505 / obs506 close by removal. |
| switches `policy_settings.gateway_interceptor`, `engine_publish`, `PUT /api/policies/settings`, 409 `policy.interceptor_unavailable`, `enforcement_live`, the readiness-gated tabs (N17-obs497 rule) | attach / detach; 「策略」 is top-level navigation beside 网关 / 身份; the gateway page shows its attachment |
| agent scope: `Policy.scope`, `agent_ids`, 422 `policy.unknown_agent` / `policy.scope_mismatch`, `AgentSpec.policy_ids` (now inert — ignored, documented), `scope_map` / `scope_statement`, `applies_to`, the editor's scope / agent pickers, the agent wizard's policy card | a plain Cedar condition — `principal in Launchpad::Agent::"<id>"` (Stage A) or `when { context.agent_id == "<id>" }` (Stage BT) — exactly how Global restricts one agent by its IamEntity ARN |
| the D7 closure permit (Stage BT permitting every tool no user-stage permit covers) and the `context.launchpad.agent_id` injection | the compiler emits the statement as written (pg-engine 5daedb8d); Stage BT is plain Cedar default-deny — a user, API key or System principal without a covering permit is denied at Stage BT, so `permit_users_call_tools` (member / admin / System / ApiKey) is REQUIRED for user traffic beside Stage A's `permit_workspace_agents_call_tools`; the engine's `status` / install answer reports `has_user_permit` per set and the compiler emits the warning `no_user_permit` for a set without one (the console surfaces it) |
| `stale_agents`, `policy_stale`, `POST /api/policies/apply`, `PolicyApplyResults` | the carrier on every deploy (§11.5); 410 for one release |
| per-workspace bundle blocks, `published`, `workspace_unpublished`, `exempt_system_presets`, `policy_empty_set_decision`, `UNPUBLISHED_SET`, `any_enforcing` | `attachment` (§11.3 / §11.4) |
| `/api/policies*` | `/api/policy-engines*`; 410 `policy.moved` for one release |

Kept: system principal exemption (amendment 10), fail-closed (`engine_unavailable`, `bundle_stale`,
set drift), SWR settle-on-RESPONSE (N17-obs498), the replay window, the per-gateway KMS circuit, the
install CAS and shard rollback (amendment 8), the deny card and its link, the per-gateway Decisions
/ Audit tabs (observability only).

### 11.8 Tests and graph

`tests/policy_principal/test_engine_attachments.py` (hermetic; strict-xfail "pg-backend pending"
until the hub lands): one engine attached to two gateways with different modes composes two `/3`
bundles that name the same set and differ only in `gateway_id` / `gateway_arn` / `attachment.mode`;
the interceptor over gateway A (ENFORCE) refuses what the interceptor over gateway B (LOG_ONLY)
passes with a `would_deny` row, both rows carrying their own `gateway_id`; detach ⇒ the next bundle
has `attachment: null`, the Lambda is detached and the interceptor passes without a row; the
migration over a seeded old-shape ledger creates exactly one default engine, one attachment and one
journal row and is a no-op the second time. The E07 lifecycle graph (`tests/graph/graphs/
policy_principal.yaml`, testing.md §6.7) is rewritten on this model: engine create → policy publish
→ attach A (ENFORCE) → attach B (LOG_ONLY) → a decision per gateway → ingest → detach A → A
unguarded; every edge cites a sentence of this amendment.

### 11.9 Errata 2026-10-28 (code freeze fa16d702 — recorded against the text above, which is kept as written)

The code review of fa16d702 (`designs/policy-global-model-20261028/CODE-REVIEW-20261028.zh.md`)
compared this amendment with the shipped code. Where they differ, the CODE is the contract from this
date on, except the last item, which stays open. Nothing above is rewritten; read these corrections
over it.

1. **Set version.** §11.2 says `policy_set_version` is `<engine id>-<n>`. The engine's set version
   is **`ps-<n>` per engine** (the set KEY is the engine id, the version keeps the `ps-` prefix the
   Rust install CAS compares); amendment 12 and policy-engines.md deviation 1 already say so.
2. **Scope baking is a NEW version, not a byte-identical republish.** §11.6 step 1 says an ACTIVE
   `scope = agents` row is republished as the compiled `scope_statement`, byte-identical, journal
   `policy.publish` reason `scope baked`. The migration (`policy_scope_migration.py`) appends
   `when { ["<agent id>", …].contains(context.launchpad.agent_id) }` to every statement of the
   policy, stores it as a **new version** (the old versions stay untouched) and journals
   **`policy.scope_baked`** (operator `migration`, `before` / `after`).
3. **`policy_settings` stays physically.** §11.6 step 3 says the old settings columns are dropped
   from the model and the ledger drops them on the next `_migrate` pass. The table and its columns
   remain in the ledger, **inert** — no route reads or writes them (policy-engines.md deviation 7).
4. **Engine status enum.** §11.1 says `status active | deleting`. The enum is **`active | deleting
   | deleted`** (`ENGINE_STATUSES`): a forced delete archives the engine as `deleted` — its
   policies are retired, versions / decisions / journal stay readable by id, the live name is
   released, mutations answer 404.
5. **409 `gateway.policy_engine_foreign` has TWO meanings** in the shipped code, and the console
   renders both as 「由其他环境管理」: (a) ruling 24 — the gateway's interceptor configuration names a
   Lambda other than THIS host's interceptor (another Launchpad environment in the same account;
   `GET` exposes `foreign: true` + `foreign_lambda`); (b) the gateway is already held by ANOTHER
   WORKSPACE's engine on this host (`routers/policy_engines.py` attach / detach). N17-obs518
   records (b) as a plausible tenancy gap (an attach to another workspace's unattached gateway is
   not refused) and asks for a distinct error code.
6. **Console path.** The console module lives under `frontend/src/v2/pages/policy/*`
   (EngineList / EngineDetail / AttachEngineDialog / Decisions / Audit / EngineSimulation …), not
   `pages/governance/*`; `pages/governance/*` keeps the gateway page and its attachment card.
7. **OPEN — ungoverned tools under `/3`.** §11.4 (the steps list, "a tool neither governed nor
   temporal-referenced ⇒ pass, no row (the short-cut is unchanged)") does not match the code:
   `core.py` passes such a tool only for `/1` / `/2` bundles and **denies it under `/3`** (reason
   code `policy`, no engine call). Because `governed_actions` is the gateway `tools/list` snapshot
   of the LAST install, a target added to an attached gateway after that install is denied until a
   publish / retire / mode change re-installs the set. Registered as **N17-obs517**; the decision
   (fix the sentence or restore the pass) is pending. Ops rule meanwhile: after adding targets to
   an attached gateway, re-install once.

## I9 — eval / canary tokens (D5, 2026-10-26) — **v2 since amendment 10 (2026-10-27, parity batch 1)**

**I9 v2 (in force once parity batch 1 is deployed; USER ruling 「评估 agent … 不要用 policy 限制」,
lead option α; owner r17-bl2, batch-1 commit 5).** Evaluation and canary traffic carries NO user
identity and is never evaluated by the engine; the token exists only so the Gateway can tell an eval
turn from a chat turn (the caller role is the same — see amendment 10).

* **Object**: `s3://<artifacts>/evaluations/<job_id>/tokens.json` (same key, same readers, same
  bucket Deny `EvalTokensReadersOnly`) = `{schema: "launchpad.policy.eval-tokens/2", job_id,
  written_at (UTC ISO 8601), token: <I1 token>}` — **one token per job**, not a per-session map.
* **Token claims**: `p = System::"eval"` for evaluation runs (dataset, online, experiments),
  `System::"canary"` for canaries; `ch = eval | canary`; `aud` = the run's agents (the launched
  agent and, for A2A arms, its declared peers); `sid` / `psid` = the job id (one policy session per
  job — irrelevant to the decision, since a system principal is exempt; kept so the row carries a
  psid); `tsv` as today. No launching user, no role, no per-session minting, no `BulkMinter`
  pacing, no `PATH_EVAL` circuit: one `KmsSigner.sign` per job plus refreshes.
* **Hub, at submission**: `eval_tokens.prepare` mints the one token and writes the file before the
  job is dispatched; the job spec is marked `principal_tokens` as before. A no-op while the Gateway
  interceptor is off for the workspace or the agent is an exempt preset (unchanged).
* **Job watcher**: re-signs the same claims (new `jti` / `iat` / `exp`) once the file is
  `refresh_s` old — `min(EVAL_TOKENS_REFRESH_S = 300 s, ttl_for(method) / 2)`
  (`eval_tokens.refresh_s`), so a 5-min BYOC token is refreshed every 150 s — while the job runs;
  deletes the file (and its noncurrent versions) at a terminal state; a dead watcher leaves a token
  that expires within its TTL.
* **Eval worker** (`strands_eval/principal_tokens.py`): reads the file once before the first turn,
  puts the single token into `launchpad_meta.principal` for EVERY session of the job, re-reads when
  less than 120 s remain. **A missing or unreadable file no longer errors the sessions**: the run
  proceeds token-less and the Gateway decides — an exempt preset passes (`role_map.exempt`), an
  ordinary agent is `principal_missing` (denied in `enforce`, `would_deny` in `log_only`), visible
  in Decisions. `session_invalidated` can no longer occur for eval traffic (no engine call).
* **Interceptor**: a verified `System::"…"` principal ⇒ `allow` row `system_exempt`, pass, no
  engine call, no temporal recording (amendment 10 §Interceptor 2).
* **Compatibility**: a `/1` file (per-session map) is still read by the worker during the batch-1
  rollout window (first matching session key, else the first token) and written by no hub after
  batch 1; the hub never writes `/1` again.

The v1 text below stays as the historical record of the per-user eval tokens (M1–M2).

### I9 v1 (per-session, per-user — superseded by v2)

Eval and canary turns are not started by a browser, so the hub hands their principal tokens to
the eval worker through the job's S3 prefix (`eval_tokens.schema.json`, fixture
`eval_tokens.json`). The I1 TTL stays 900 s; nothing in I1 / I2 changes.

* **Object**: `s3://<artifacts>/evaluations/<job_id>/tokens.json` (`contracts.EVAL_TOKENS_KEY`;
  one file per job, shared by its attempts) = `{schema: "launchpad.policy.eval-tokens/1", job_id,
  written_at (UTC ISO 8601), tokens: {<Runtime session id>: <I1 token>}}`, 1–10 000 sessions.
* **Hub, at submission**: mints one token per session of the run — `sid` = the map key, `psid` =
  `codec.psid_for(ws, sid, tsv)`, `ch` = `eval` | `canary`, `p` = the launching user (dataset
  runs) or `System::"eval"` / `"canary"` (online eval / canary) — and writes the file before the
  job starts.
* **Job watcher, while the job runs**: rewrites the file every `EVAL_TOKENS_REFRESH_S` (300 s).
  A refresh re-mints `jti` / `iat` / `exp` only; `sid`, `psid`, `tsv` and `p` stay those of the
  submission, so each session stays one policy session. A temporal-set change mid-job is not
  followed: the engine answers 409 `session_invalidated` for that session; the worker records that
  sample as **errored** with the reason `session_invalidated` visible in the run's results (never
  silently dropped) and carries on with the other samples. At a terminal state (succeeded / failed / cancelled) the
  watcher deletes the file. If the watcher dies, the tokens expire within 900 s.
* **Eval worker**: before each session's first turn, reads the file and copies the session's token
  into `launchpad_meta.principal`. It re-reads the file when the token it holds has less than 120 s
  left (multi-turn sessions). A session id missing from the file is not invoked and never run
  token-less: the sample is recorded as errored with the reason `principal_missing` (the deny the
  gateway would give), and the job **carries on** with the other samples; it never aborts.
* **Bearer material**: the file is SSE-encrypted in the private artifacts bucket, readable only
  by the eval worker role (on `evaluations/*/tokens.json`). The bucket's
  `EvalTokensReadersOnly` Deny admits the hub role and EVERY eval worker role of this
  environment by name pattern (`launchpad-agent-launchpad-eval-worker[-<env>]-????????…`, one
  role per workspace's worker), not per workspace: a workspace's worker can therefore read the
  token files of every job in the same artifacts bucket, other workspaces' included — the
  boundary is the environment's bucket, not the workspace (the worker reads only the key its
  own job spec names). It is never logged, copied into
  results / artefacts / spans, or shown in the console. Fixtures hold `__TOKEN__` placeholders;
  tests sign with the ephemeral key.

## I4 — engine data plane

HTTP/1.1 JSON over TCP on the hub host's private IP; one port per shard (`7443`, `7444`). Every
request except `GET /healthz` carries `X-LP-Ts` (unix seconds), `X-LP-Nonce` (uuid) and
`X-LP-Sig = hex(HMAC-SHA256(key, METHOD\nPATH\nTS\nNONCE\nsha256hex(body)))`
(`codec.engine_signature`); ±60 s skew, nonce replay rejected for 5 min, failure = 401
`unauthorized`. Routes (schemas `engine_*.schema.json`):

* `POST /v1/decide` — stage A (agent, stateless, no history) and stage BT (token principal or the
  agent, recorded when `record`) in one call; `decision = allow` iff both allow. `session` is
  `codec.session_key(ws, psid, kind, id)`; the owner shard is `codec.shard_for(session, shards)`.
  A call whose action is temporal-referenced goes to the owner shard with `record: true`; any
  other call may go to any shard with `record: false`.
* `POST /v1/respond` — the `response` / `error` event after the call (RESPONSE interceptor; P0 c:
  it also runs after a REQUEST short-circuit). `output` is parsed from `result.content[].text`
  JSON (Lambda targets return no `structuredContent`).
* `POST /v1/list-filter` — `tools/list` filtering, read-only.
* `GET /healthz` — readiness after recovery, store epoch, per-workspace set versions.

Errors (`engine_error.schema.json`): 409 `session_invalidated` (temporal set changed, T4) /
`history_lost` (new store epoch) / `set_mismatch` (`have` = engine's version), 400
`bad_request`, 401 `unauthorized`, 503 `unavailable`. Client timeouts: 100 ms stateless + one
50 ms retry on the other shard; 1500 ms owner-shard.

## I5 — engine control plane

JSON lines on the shard's `/run/launchpad-policy-engine/control-<shard>.sock` (0660
lp-policy:lp-policy in the 0750 runtime directory; the hub user is the only member of group
`lp-policy`, so only the hub can connect):
`install{ws, policy_set_version, temporal_set_version, policies[{id, version, mode, stage, kind,
text}], schema, event_schema}` → `{ok, errors[]}`; `authorize_stateless{ws, requests[]}` →
`{ok, decisions[]}` (never records — invoke gate, simulator, narrowing); `status{}` →
`{ok, shards[]}`. A failed install leaves the previous set live and is reported (journal
`engine.install` failed, publish answers 409 `policy.engine_install_failed`).

## I7 — audit lines

CloudWatch JSON lines ingested by `services/policy_audit.py` into the existing
`policy_decisions` (new columns `stage`, `session_psid`, `event_seq`, `store_epoch`,
`input_keys`; `source` gains `gateway`), idempotent on `decision_id = codec.decision_id(jti,
jsonrpc_id, stage)`. `launchpad.policy.decision` (interceptor) and `launchpad.policy.engine`
(engine — our own audit; the engine's log is pruned). Argument **names** only, never values;
never a token or header. **Ingest stamp (N17-obs482).** A `gateway` row's `created_at` is the
line's own `ts` (the decision time); the hub's ingest additionally stamps `ingested_at` (UTC, the
hub's clock, set by `ingest_gateway_events` only — a line is never believed for it) so the
ingest lag (design §14.3 "rows within 60 s") is auditable after the fact. The audit view of a
`gateway` row adds `ingested_at` and `ingest_lag_ms` (both null for rows ingested before the
column existed); the timeline order stays `created_at` descending, and every other row's view is
byte-identical.

## I8 — JSON-RPC shapes the interceptor returns

Deny: `{"code": -32002, "message": "Tool Execution Denied: Tool call not allowed due to policy
enforcement [<reason>]", "data": {decision_id, policy_id, stage, reason_code}}` — the message is
Global's exact text (`contracts.deny_message`), so `policy_denials.parse_tool_denial` and the
console `PolicyDenyCard` read it unchanged. Session required (T9): `-32602
"policy_session_required"`. Conflict (T4): `-32002 "ConflictException: the policy session was
invalidated by a policy change; start a new session"`. The interceptor **never raises** (P0 d: a
raised error reaches the caller with the stack trace under `exceptionLevel=DEBUG`).

## Runtime deny frame (console deny card for gateway denials)

The model sees the gateway denial as a tool error. With the interceptor on, the rendered
templates add an AfterToolCall hook that turns a tool result carrying the I8 deny text into a
`policy_denied` frame (`runtime_policy_denied_frame.schema.json`, `source: "gateway"`), drained by
the existing `__LAUNCHPAD_POLICY_DRAIN__` splice — so Chat renders the existing deny card.
`record_runtime_decision` writes **no** row for `source: "gateway"` (the gateway line is the
system of record) and only forwards the frame.

## Behaviour when the engine is unreachable (lead note 1, 2026-10-26 — hub side retired by amendment 9, 2026-10-27)

* **Hub enforcement (retired).** The hub invoke gate (`gate_invoke`, `policy_invoke_fail_mode`,
  503 `policy.undecidable`), the deploy-time narrowing with its engine-down fallback table /
  cedarpy floor / `policy_applied.engine_fallback` stale mark, and the runtime guard's fallback
  bundle were removed on 2026-10-27 (user ruling: policies govern Gateway tool calls only, exactly
  like Global AgentCore Policy — amendment 9). Nothing in the hub asks the engine on an invoke or
  a deploy any more; a deploy renders every declared tool and never fails on engine health. The
  hub's remaining engine consumers are the simulator (`policy_bundle.evaluate` → I5
  `authorize_stateless` when `engine_decides`; an unreachable engine or a 409 `set_mismatch` is
  reported to the caller, never a permit, and one background reconcile is scheduled) and the
  install / bundle path (`policy_engine_sync`, amendment 8).
* **Tool calls** (interceptor — the ONLY enforcement point): the fail matrix of the design §12.7 /
  §13.5 — an unreachable engine is a `deny` with `reason_code: engine_unavailable` for governed
  tools in `enforce` (fail closed, one gateway row), `log_only` allows and records `error`; the
  hub answers the invoke 200 and the agent reports the tool error (pinned by
  `test_interceptor.py` — the E07 graph no longer walks engine-down since amendment 11;
  `default-accept` P2.12 expects exactly this, no hub row).
* Tests: the gateway-side engine-down cases in `tests/policy_principal/test_interceptor*.py`; the
  hub-gate / fallback-table matrices (`test_engine_down.py`) were deleted with the code.

## Runtime guard (lead note 3 — retired by amendment 9, 2026-10-27)

Removed outright (Global has no runtime guard): no `policy_runtime_guard` knob, no
`policy_settings.runtime_guard` consumer, no guard rendering in the Strands / Harness templates,
no per-agent bundle under `policy/bundles/`. The hub still forwards and audits the
`policy_denied` / `policy_decision` frames a runtime stream may carry (the template's re-emit of
an I8 Gateway denial keeps the Gateway's decision id — the chat deny card reads it as before).

## Where each change lands in the existing module

The full mapping (ledger columns, `policy_store`, `policy_bundle`, `policy_enforcement`,
`routers/policies.py`, the Governance pages `frontend/src/v2/pages/governance/*` incl. the
simulator and audit views) is the design's §15.1 table; each stream's PR cites the rows it
implements. Demo accounts for the video: `demo-admin` (admin) and `hr-intern` (member); the
built-in `admin` is never on screen.

## Stream B — interceptor implementation notes (`backend/app/policy/interceptor/`)

Normative for the interceptor are amendment 1 (vocabulary), 2 (request fields) and 3 (check
order, set handshake, time, shared S3 objects) above; this section only records how stream B
implements them and the details those amendments leave to the interceptor. Tests:
`backend/tests/policy_principal/test_interceptor*.py` (the `_real_engine` file drives stream C's
binary — the per-worktree release build `make verify` produces, or `LP_ENGINE_BIN` /
`LP_ENGINE_ASSETS`; it skips while no binary exists, N17-obs516).

* **Packaging.** Self-contained (stdlib + `cryptography`; `boto3` only inside `handler.py`),
  zipped as is by the `policy-interceptor` bootstrap stage (entry `main.handler`); `codec.py` is
  a verbatim copy of the contract codec and a test pins the bytes and the shared constants.
* **Bundle source (N17-obs483: stale-while-revalidate, bounded staleness).** A bundle confirmed
  less than `LAUNCHPAD_POLICY_BUNDLE_TTL_S` (30 s) ago is served as is. Past the TTL the cached
  bundle is still served and ONE background refresh reads `current.json` (and, when it moved
  forward, the new `v<n>.json`), at most one attempt per TTL, so no request ever waits on S3 for
  a TTL refresh. Past `LAUNCHPAD_POLICY_BUNDLE_MAX_STALE_S` (120 s, i.e. refreshes kept failing)
  the request refreshes synchronously and, if the bundle is still that old, denies
  `interceptor_error` (`reason_code: bundle_stale`). A policy change therefore takes effect
  within TTL + one refresh in normal operation and within `MAX_STALE` at worst. The refresh after
  a 409 `set_mismatch` and the cold-start load stay synchronous; a cold Lambda that never loaded
  one denies `bundle_unavailable`; a failed refresh keeps the last good bundle (until
  `MAX_STALE`); a pointer behind the loaded version is never followed unless forced. A
  client-context `GATEWAY_ARN` that is not the bundle's denies `gateway_mismatch`.
  **N17-obs498 (M2 9c p99 outliers).** A Lambda FREEZES the container the moment the handler
  returns: a background refresh thread spawned at the end of a REQUEST makes no progress
  between invocations and a 2 ms invocation cannot finish an S3 round trip, so under spaced
  traffic the refresh never completed, the age reached `MAX_STALE` and the REQUEST path paid the
  synchronous pointer read (77 / 117 ms on gw-t1 — one S3 round trip from the VPC); the third
  outlier (205 ms, 1.2 s billed) was a cold start (Init 281 ms + the S3 client build + three
  sequential reads). Since obs498: (1) the RESPONSE invocation that follows every REQUEST calls
  `BundleSource.settle()` — it joins the in-flight thread (or runs the due refresh on a thread)
  for at most `SETTLE_BUDGET_S` (2 s), off the decision path, so the bundle is fresh for the
  next REQUEST and the request path never fetches while traffic flows; an attempt in flight for
  longer than a TTL counts as frozen and a new one may start; (2) the cold load fetches the
  bundle and the engine key in parallel (one round trip less) and the Lambda's INIT phase
  pre-loads the bundle (`handler.preload()` at import) so the first request's duration no longer
  carries the client build and the reads. **Residual (by design):** a container that saw no
  RESPONSE invocation for longer than `MAX_STALE` — idle for > 120 s, or whose last REQUEST was
  denied (a short-circuit never reaches the RESPONSE slot) — pays ONE synchronous pointer read on
  its next REQUEST and fails closed (`bundle_stale`) if that read fails; `settle` is never called on
  the REQUEST path, so the decision latency carries no refresh while traffic flows; the operator may raise `LAUNCHPAD_POLICY_BUNDLE_MAX_STALE_S` (staleness bound vs
  one round trip after an idle gap). Provisioned concurrency would remove only the cold-start
  outlier (one of three) — the frozen-thread class is a Lambda property that provisioned
  environments share; it is not needed for the p99 target under steady traffic.
* **Step 1 refinement (lead ruling 2026-10-26).** An unknown caller is denied only when an
  `enforce` workspace governs the tool; under a `log_only` one it passes with a `would_deny`
  row, else it passes without a row. Every such row has `reason: unknown_caller`,
  `ws: unknown` (ingested into the default workspace).
* **Step 2 details.** A tool neither governed nor temporal-referenced passes without an engine
  call or row; an `exempt_system_presets` system agent is allowed with an `allow` row
  `reason: system_exempt` (the invoke gate's reason).
* **Step 3 / 4.** A missing token on a non-system agent is `principal_missing`; an invalid
  one `principal_invalid:<verifier code>` (stage `bt`). A token-less system agent is its own
  `AgentCore::IamEntity`; with a temporal policy in the workspace it has no psid and gets
  `policy_session_required`.
* **`log_only`** turns every deny of steps 1-5 into a pass with a `would_deny` (policy) or
  `error` (engine failure / conflict) row. Any internal exception: `tools/call` → deny
  `interceptor_error` (pass when the workspace was already known to be `log_only`); other
  methods pass; RESPONSE passes the gateway response through.
* **Engine failures** other than the 409s (unreachable, over the 100 / 50 / 1500 ms budget,
  5xx, 400, 401) deny `engine_<code>` (T7); `set_mismatch` denies `temporal_set_mismatch`.
* **RESPONSE.** `tools/call` on a temporal-referenced tool with a valid token submits
  `/v1/respond` with the decide call's `principal` / `agent` / `resource`: `kind = error` for a
  JSON-RPC error carrying no `reason_code` or `reason_code: policy` (our own non-policy
  refusals were never recorded and submit nothing) or `result.isError`, else `response` with
  `output` = the first `content[].text` parsed as a JSON object (else `{ok: true}`). A failed
  submit is a diagnostics line; the response is never changed. `tools/list` in `enforce` with a
  valid token is filtered through `/v1/list-filter` over its governed actions and left
  unfiltered on any failure, or (one WARN line) when there are more than 1024 governed actions.
  An engine `errors[0] = policy_evaluation_error` is a decided deny with that reason
  (`reason_code: policy_evaluation_error`, never retried; its RESPONSE still submits the
  `error` event).
* **Lines.** One I7 `launchpad.policy.decision` line (stage `final`) per decided call;
  `decision_id = codec.decision_id(jti, jsonrpc id, "final")`, or `req:<gateway REQUEST_ID>` in
  place of the `jti` for a token-less call. Non-decision diagnostics use the key
  `launchpad.policy.interceptor` with a fixed vocabulary (`bundle_unavailable`,
  `respond_failed`, `list_filter_failed`, `list_filter_skipped` (WARN: more governed actions
  than the engine's 1024-action list-filter limit — the list stays unfiltered; calls are still
  decided), `internal_error` + exception class, and — obs498 follow-up — `bundle_refresh`
  `{mode: sync | background | settle, outcome: loaded | unchanged | behind | failed, ms, version,
  error}` emitted by `BundleSource` after every refresh attempt: `mode` is the path that ran it
  (`sync` = on a REQUEST: cold load, past MAX_STALE or a forced refresh after `set_mismatch`;
  `background` = the thread a REQUEST spawned past the TTL; `settle` = started by the RESPONSE
  invocation), `outcome` whether a new version was loaded, the pointer was unchanged, behind the
  loaded version, or the attempt failed (`error` = the code), `ms` the attempt's duration — so a
  latency re-test tells which path paid an S3 round trip) and are not ingested. No line carries a token, header or argument value (a test fails on a JWS).

## Stream A — hub runtime sessions (`backend/app/services/principal_mint.py`)

The runtime holds the turn's token per process (one microVM per Runtime session), so with the
carrier on (`Settings.policy_gateway_interceptor` and the workspace's
`policy_settings.gateway_interceptor` both on, agent not an exempt preset) two principals must
never share a Runtime session. This changes some visible behaviour:

* **Per-principal runtime sessions.** The Runtime session of a turn is
  `lpp-` + 60 hex of sha256(workspace, agent, caller session id, principal) — stable for one
  principal in one conversation, different for every other principal. The caller (Chat,
  console invoke, `/v1`, the A2A branch) keeps its own session id in requests, responses and the
  chat ledger; each derived id is recorded in `policy_runtime_sessions` under the caller's
  session. Consequence: an administrator continuing a member's conversation, or two API keys
  sharing a session id, each get their **own agent context** (short-term memory, tool state,
  idle timer) — the conversation's history in the console stays one thread, the agent's working
  context is split per principal. One in-flight turn per derived session (`409
  policy.session_busy`).
* **Principal identity.** A console user is keyed by `users.id`; the built-in config admin (no
  `users` row) by its username — so renaming a registered user keeps its runtime sessions, and a
  config admin whose username changes starts fresh ones. API keys are keyed by key id.
* **Reserved prefix.** A caller-supplied session id starting with `lpp-` (any case) is `422
  chat.session_id_reserved` on every entrance, switch on or off.
* **Fail closed.** If the derived-session record cannot be read or written the turn is `503
  policy.session_unavailable` — never a fallback to the shared caller id.
* **Signer failure circuits (N17-obs493, re-review F4).** A failed KEY resolution (DescribeKey:
  the one hub key) is remembered hub-wide for 60 s — no path mints and no KMS call is made. A
  failed KMS **Sign** is remembered for 60 s per **(caller path, workspace)**: the interactive
  path (chat / console / `/v1` / A2A turns and the hub's own Gateway calls, `PATH_TURN`) and the
  eval / canary bulk minter (`PATH_EVAL`) each have their own window per workspace, so one
  member's failing eval replay never makes a chat turn token-less and one workspace's failure
  never touches another's (`principal_mint._circuit_check(path, ws)` / `_trip`; `circuit_open`
  reads it). A token-less turn is only ever the consequence of ITS OWN path's failure.
* **Readers follow the conversation** (`services/runtime_sessions.py`, one helper
  `runtime_sessions_for(db, ws, caller_sid, viewer)`). Ending a chat session stops every derived
  session of it; the Chat trace rail and the observability session detail read their spans; the
  observability session list shows them under their conversation (one row, additive
  `runtime_session_ids`) and the trace list maps each trace's session to the conversation
  (additive `runtime_session_id`); the chat memory rail sums their events and records; the
  memory session list names each derived session's conversation (`conversation_session_id`, its
  ledger entry); a passive evaluation over chat session ids reads the derived sessions (the
  caller id only when console messages predate the first derived session by more than 5 s —
  clock-skew slack, since the user message is saved just before the turn records its derived
  session; the cutoff is anchored to the first derived session's `created_at`). Under workspace
  isolation the derived ids are hidden exactly when their conversation or agent is
  (`HiddenAgents.session_ids`, `session_visibility.hidden_session_ids`). Deleting an agent
  forgets its records.
* **Switch toggles.** Turning the carrier ON mid-conversation moves the next turn into the
  derived session (the earlier turns' runtime context stays under the caller id); turning it OFF
  moves turns back to the caller id (the derived sessions idle out; their records remain for the
  readers). Nothing changes while the switch stays off: ids are byte-identical to before.

## Stream A6 — hub decisions on the engine (`backend/app/services/policy_engine_decide.py`)

* **One routing switch.** `policy_engine_decide.engine_decides(db, ws)` is true only when the
  global `policy_gateway_interceptor`, the workspace's `gateway_interceptor` and
  `engine_publish` are on, the engine has accepted a set from this hub (`policy_set_seq > 0`),
  that set is the hub's CURRENT ACTIVE set (`engine_state.set_digest` = the
  `policy_engine_sync.active_digest` of the ACTIVE rows and their agent scoping; it is written
  only by a successful install, so a publish, a retire or a pin change made while any switch
  was off keeps the workspace on cedarpy). Agent-scoped and spec-pinned policies are scoped on
  the engine by A5a-2 (N17-obs484). Otherwise cedarpy decides, exactly as before. Engine-down behaviour and the console build on this predicate.
* **Reconcile.** `policy_engine_sync.reconcile_workspace(db, ws)` re-installs the hub's ACTIVE
  set when the stored digest is stale, or when a shard reports another set version than
  `ps-<policy_set_seq>` (an install whose ledger commit failed afterwards). A switch re-enable
  (the settings route) calls it. A 409 `set_mismatch` fails the decision closed and schedules
  one background reconcile per workspace per minute.
* **Where it applies.** `policy_bundle.evaluate` for an ACTIVE-set `invoke` / `call_tool` with an
  agent (the invoke gate, the cedarpy-shaped simulator) and `policy_bundle.decide_tools_detailed`
  (deploy-time narrowing) take the engine path after the empty-set check (D20-obs1 unchanged).
  Drafts, pinned versions and agent-less requests stay on cedarpy. The items are I4 decide items
  (contract amendment 4 for invoke) sent through I5 `authorize_stateless` to the first shard
  that answers. A stale hub (409 `set_mismatch`) or no answering shard raises
  `EngineControlError`: the caller's fail mode applies, never a permit. An evaluation error is
  the engine's fail-closed deny (`errors[0] = "policy_evaluation_error"`), not `no_decision`.
* **Deploy-time narrowing is Stage A only when the interceptor decides.** Without a principal,
  `decide_tools_detailed` answers the agent's own (Stage A) rules only when
  `policy_enforcement.interceptor_decides(db, agent)` holds: this deploy renders the principal
  carrier AND the workspace Gateway's interceptor is verified attached. It is the same rule the
  runtime guard is disarmed by, and the deploy job's own carrier decision threads through
  `narrowing_for`. Otherwise the engine answers with the agent as principal, the full
  narrowing, exactly like cedarpy. When the interceptor decides, a per-user rule (e.g. the `forbid_tool_for_members` template,
  "forbid … unless admin") is not a narrowing input on the engine: the tool stays in the agent's
  list, the Gateway interceptor denies members per call and filters `tools/list` per principal,
  and administrators keep the tool. On cedarpy the same rule narrows the tool away for the agent
  principal, and so for every user of the agent.
* **cedarpy fail-closed guard.** Off the engine route, a cedarpy decision whose applicable set
  (ACTIVE, drafts or a pinned version, agent scope applied) holds a `dialect='dogwood'` row is
  an evaluation error. The invoke gate / simulator (`policy_bundle.evaluate`, reason
  `policy_evaluation_error`, the dogwood stamps as determining) and the narrowing
  (`decide_tools_detailed`, every tool) answer DENY with `errors[0] = "policy_evaluation_error"`.
  The row is never skipped and the answer is never an allow, even when the text happens to parse
  as Cedar. cedarpy never compiles dogwood text. The editor accepts dogwood only while
  `engine_publish` is on; this guard is the backstop (`test_dogwood_cedarpy_guard.py`).
* **Simulator.** `POST /api/policies/simulate` takes an additive `engine` block
  (`{tool?, agent_id?, input, at?, history[{tool, kind: request|response|error, input, output,
  at}]}`). When `engine_decides` holds it answers `{engine: true, stage_a, stage_bt, final,
  log_only_would, determining[], errors[], trace[], source_set, policy_versions, warnings[]}` from
  the engine's `replay`. That includes temporal rules over `history`, on the ACTIVE / draft /
  pinned set chosen per agent like the cedarpy simulator. Otherwise it answers 409
  `policy.engine_not_routed`. Without the block, the simulator is unchanged. Bounds: `input` and
  each history `input` / `output` ≤ 16 KB of JSON, `history` ≤ 50 events (422), and 30 engine
  simulations per user per minute (429 `policy.simulate_rate_limited`). `action: list_tool`
  answers visibility (`final` = `stage_a`, `list_filter: true`), as the interceptor's
  `tools/list` filter decides it.
* **Tests.** `test_engine_decide.py` covers the items, fail-over, a stale set and simulate.
  `test_engine_parity.py` checks cedarpy against an engine set installed through A5's real path,
  including agent scope and spec pins (N17-obs484). `test_engine_routing.py` covers the
  switch, cedarpy never being asked when routed, fail-closed when the engine is down, the
  simulate route and flag-off.

## Stream A — engine publish and the gateway bundle (`policy_engine_sync.py`, `policy_gateway_publish.py`)

* **Engine install (I5).** A publish / retire compiles the workspace's ACTIVE set with stream
  C's `dogwood_compiler` (a stored D20 row — dialect `cedar` — is rewritten with `rewrite_d20`
  for the engine's copy only; the stored text never changes) and installs it on every shard
  before the change commits: a set that does not compile is `422 policy.invalid_statement`, an
  engine that refuses / cannot be reached / has no binary is `409 policy.engine_install_failed`
  and the change is rolled back (journal `engine.install` failed; the previous set stays live).
  On success `ps-<n+1>`; the temporal-set version moves only when the temporal items change.
  Rewrite notes (N17-obs495): a D20 `principal is Launchpad::Agent` (any agent) becomes
  `principal is AgentCore::IamEntity` — the engine's Stage-A principal type (the agent chain is
  `IamEntity → Agent → Workspace`; `is AgentCore::Agent` would never apply to a tool call and the
  validator rejects it); `in Launchpad::Agent::"<id>"` keeps the `Agent` parent and
  `in Launchpad::Workspace::"<ws>"` the `Workspace` parent. The simulator (`replay`) installs
  the set under the REAL workspace id (`SimInput.ws`, passed by `policy_engine_decide.simulate`),
  so workspace-named rules decide in the simulator as at the gateway.
* **Gates.** Only when `Settings.policy_gateway_interceptor`, the workspace's
  `policy_settings.gateway_interceptor` AND its `policy_settings.engine_publish` are on — an
  engine set installed by hand (t1's M1 `ps-m1-1`) is never replaced until an operator turns
  `engine_publish` on.
* **Gateway bundle (I3).** After a successful install and after a mode change the hub composes
  the bundle across every workspace on the same gateway that it has installed
  (`policy_settings.engine_state`; a workspace never installed by the hub is not in it) and
  writes it with stream B's forward-only CAS writer (`v<n>` with `IfNoneMatch`, then
  `current.json` only forward). Inputs: `Settings.policy_engine_endpoints` (`<ip>:<port>` per
  shard) and `Settings.policy_hub_role_arn` (the bundle's `hub_role` is its role name) — both
  empty by default = no bundle is written (deploy-ops sets them per environment). A write
  failure never undoes the install: journal `gateway.bundle` failed, `engine_state.bundle_pending`
  stays true, and the next publish / mode change, the startup reconcile or the periodic one
  (every 300 s + up to 30 s jitter while the switch is on; one bundle write at a time per hub
  process) writes it again.
* **Agent scoping on the engine (N17-obs484).** The engine holds ONE set per workspace, so every
  statement of an agent-scoped policy (`Policy.scope = "agents"`) carries
  `when { [<agent ids>].contains(context.launchpad.agent_id) }` — its `agent_ids` plus every live
  agent whose spec pins it (`AgentSpec.policy_ids`), the hub's own `applies_to` rule; a scoped
  policy no agent uses is not installed. A change of who it applies to re-installs: editing a
  live policy's `agent_ids` (before the change commits, `409` when refused), and a deploy or
  delete that changes an agent's pins (after it; a failure is journalled, never fails the deploy).

## Stream A6 — console and its API (`routers/policies.py`, `frontend/src/v2/pages/governance/*`)

Everything here is additive and invisible while `Settings.policy_gateway_interceptor` is off
(API output and console unchanged); the routes and fields are listed in
`policy-backend.md` §"HTTP surface".

**Global parity (contract amendment 9, user ruling 2026-10-27) — what the console shows.**
Policies decide GATEWAY TOOL CALLS only; an invoke is never a policy decision and no tool is
narrowed at deploy time. The console follows (`cn/r17-bp-global-parity-ui`):

* **Deny card** — one kind, the Gateway tool denial (`policy_denied` frame, `lib/policy-deny.ts`
  `PolicyDenyKind = "tool"`); the invoke deny card, its `error` `policy.denied` wiring in Chat and
  the `invoke` stage are gone. A `policy` history row an older build wrote without a tool still
  renders as a tool card with no tool name.
* **Decisions** — the source filter offers `gateway` and `simulate`; `hub` (the retired invoke
  gate) and `runtime` (the retired runtime guard) are HISTORIC: offered only while such rows are
  on the timeline, rendered greyed with a `historic` mark (`v2-decision-historic-<id>`), no stage;
  their detail opens the same card. The N17-obs499 mirror banner keeps its source scoping.
* **Policies / editor** — the template picker lists `permit_workspace_agents_call_tools` (Stage A:
  `permit(principal in Launchpad::Workspace::"<ws>", action == call_tool, resource)`, the
  console substitutes the workspace id), `permit_users_call_tools` (Stage BT: member / admin /
  `principal is System` / `principal is ApiKey` — option D), `permit_agent_declared_tools`,
  `forbid_tool_for_members`, custom;
  `permit_members_invoke` is gone. The first-run hint says to publish the workspace Stage-A
  permit first (先放行工作区内智能体的工具调用), then the users permit. The builders emit the
  canonical form of the server templates (`governance-prefill.test.ts` pins them against the
  `.cedar` files when present).
* **Simulator** — `call_tool` only (the action is shown, not chosen); principals user / API key /
  system; the resource is always a Gateway tool; the engine block's calling-agent picker (Stage A)
  is always shown and `engine.tool` / `engine.agent_id` are always sent.
* **Settings card** — "网关执行模式 / Gateway enforcement mode" (`off` / `log_only` / `enforce` =
  the gateway bundle block's mode) + the two switches (InterceptorCard); the runtime-guard
  checkbox, the fail-mode row (`invoke_fail_mode`) and the coverage block are gone;
  `empty_set_decision` renders as `deny` (`v2-policy-empty-set`); the stale list reads "需要重新
  部署以携带用户令牌的智能体 / agents to redeploy to carry the user token" and the apply button
  "Redeploy to carry the user token"; `PolicyApplyResults` rows show `carrier` only (no
  patterns / coverage / "Gateway dropped"). `api.ts`: `PolicyAction = call_tool | list_tool`,
  `PolicyResourceKind = tool`, `PolicySettings` without `runtime_guard` / `coverage` /
  `invoke_fail_mode` (+ optional `empty_set_decision`), `PolicyApplyAgent = {agent_id, job_id,
  carrier?, skipped?}`, `AgentInfo` without `policy_coverage` (`policy_stale` keeps the carrier
  meaning), `PolicyDenyStage = a | bt | final`.
* **Agents page** — the stale tag reads "Redeploy for user token"; the coverage tag is gone.

* **Switches — superseded by amendment 11 (attach / detach an engine per gateway; no workspace
  switches).** Historic text: `PUT /api/policies/settings {gateway_interceptor?, engine_publish?}` is an
  administrator's (403 otherwise, 409 `policy.interceptor_unavailable` while the platform switch
  is off) and journalled in the `mode.set` row. Turning the engine publish on installs the ACTIVE
  set at once and replaces whatever the engine holds for the workspace; the console asks first
  (InterceptorCard, a confirmation per switch and direction). A switch turned back ON
  **reconciles** rather than blindly installs (`policy_engine_sync.reconcile_workspace`: a publish
  that happened while a switch was off, or a set installed by hand, is installed; an engine
  already on the hub's set is left alone); a refusal rolls the switch back (409
  `policy.engine_install_failed`). Toggling the Gateway interceptor also changes what the next
  deploy renders (the principal carrier): `mark_applied(carrier=)` snapshots whether the live
  version carries it (the deploy job's decision; a caller without one — the Runtime-canary
  promote / rollback — gets the module rule now) and `is_stale` compares that with
  `principal_carrier_for` now, so a governed agent goes stale either way; with the platform switch off nothing is snapshotted and staleness
  is unchanged.
* **Per-policy enforcement.** `PUT /api/policies/{id}/enforcement-mode {active | log_only}`
  (administrator) — the editor's "log only on the policy engine" toggle; an ACTIVE policy is
  re-installed at once (the engine's `mode` per policy), a refusal rolls it back; journalled
  `policy.enforcement_mode`. The engine block of `GET /settings` adds `temporal_count` and
  `bundle_pending` (A5a-2 `engine_state`); the health panel shows the count and warns while the
  gateway bundle of the last install is pending.
* **Engine health.** `GET /api/policies/engine` reads I5 `status` per shard and shows only this
  workspace's set (`in_sync` = the hub's last `ps-<n>`); the last install is the newest
  `engine.install` journal row. An absent engine is `ready: false` per shard, never a 5xx.
* **Dogwood statements.** `dialect: dogwood` on validate / create / update compiles on the
  engine's compiler with the ACTIVE set (T8 quotas) and is accepted only while the workspace
  publishes to the engine. The hub's cedarpy set cannot parse a Dogwood row: an ACTIVE one there
  is a `no_decision` (fail closed, as for any statement that no longer parses), so leaving the
  engine with one ACTIVE is refused (409 `policy.dogwood_active`). Until `engine_decides` holds
  for the workspace (e.g. N17-obs484's scoped-policy exclusion) the invoke gate and narrowing
  therefore fail closed on such a set; an operator keeps Dogwood rows to routed workspaces.
* **Simulator.** The console sends the `engine` block of the A6 simulate (calling agent, tool
  input, request time in the display zone, history rows as minutes before the request) and
  renders Stage A / Stage BT / final / determining / trace; on 409 `policy.engine_not_routed`
  it hides the block and shows the reason. (Amendment 9: tool calls only — see above.)
* **Tab visibility (N17-obs497, user ruling 2026-10-27) — superseded by amendment 11: 「策略」 is
  top-level navigation and the gateway page shows its attachment; nothing is gated on readiness.**
  Historic text: the Policies / Decisions / Audit tabs,
  the deny card's links, the agent page's policy hints and the home card follow the workspace's
  LIVE posture — `settings.interceptor.enforcement_live` (`gateway_interceptor` AND `engine_publish`
  AND the gateway's interceptor verified `attached`) — read once per 30 s by `v2/policyPosture.ts`
  and applied by `hiddenSurfaces.tabShown(tab, live)`; while the predicate is false (or before the
  first read) a deep link falls back to the Gateway list, as the phase-2 constant did.
* **Decisions.** Gateway rows add stage, the short policy session id, the event sequence and the
  argument names (`input_keys`, never values); the `gateway` source filter and these columns
  appear once such a row is on the timeline. Clicking a row's decision opens its detail (every
  column, the policies named from the workspace's list) and, for a `deny` / `would_deny`, the
  same deny card Chat shows.
* **Deny card (user decision).** When a tool call is denied, the console shows the card in its
  own language (zh-CN / en), naming the denying POLICY (its name, else id) and the engine STAGE —
  阶段 A / 阶段 BT (`stage.a` / `stage.bt`), 最终 (`final`); (`invoke` retired by amendment 9) —
  with the raw English engine text ("Tool Execution Denied: … [Policy evaluation denied due
  to …]") collapsed under a details toggle (`frontend/src/v2/pages/chat/PolicyDenyCard.tsx`,
  `lib/policy-deny.ts`). The facts come from the I8 `data` where the frame carries them, else the
  hub: the `policy_denied` SSE frame (`source`, `stage`, `policy_name`, and `gateway_id` /
  `engine_id` = the DECIDING gateway — the tool's own gateway from the agent's Gateway ToolRef,
  falling back to the workspace gateway (N17-obs526) — policy-backend.md
  §"Stream frames → hub"), the `policy` history row (`policy{…}`) and a Decisions row (`stage`,
  `policy_ids` + the list's names). A frame without them renders as before. (The 403
  `policy.denied` envelope of the invoke gate is retired by amendment 9.)



## Amendment 12 — takeover integration corrections (2026-10-28)

The final numbered rulings in the Global-model API contract supersede earlier draft examples: install `ws` is the engine ID, set versions remain `ps-<n>`, and each gateway has its own key object and bundle version. Policy names are unique within an engine. Forced engine deletion retires policies and retains read-only version/audit history under an archived owner.

Engine/policy writes retain the established default-granted `policy.manage` permission; gateway attachment mutations require admin. Graph refusal paths use a member whose `policy.manage` permission has explicitly been revoked.

I1 claims schema `/v2` adds optional `engine_versions: {engine_id: tsv}` and required-with-it `generation`. The signed token wire `v` stays 1, so the R2-first Lambda continues accepting old tokens and /2 bundles. For /3, new-token REQUEST and RESPONSE choose the epoch using the trusted bundle attachment ID, derive `psid_for(claims.ws, claims.sid, epoch, generation)`, then use the usual identity-bound session key. An absent target entry requires a new turn (`session_invalidated`). Legacy tokens retain scalar tsv/psid during rollout. Minted maps contain only declared gateways and audience peers, not unrelated engines; compact epochs avoid repeated psids, and the 4096-character wire limit is enforced before returning a token.

New regression coverage includes live/cached attachment recovery, foreign and cross-region refusal without writes, per-target endpoint normalization, multiple generated gateway clients with independent teardown, target-engine session selection, maximum audience token size, and archived policy history/name reuse.
