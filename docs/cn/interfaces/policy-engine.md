# Interface: Launchpad policy engine (Dogwood) — service, compiler, simulator, operations

Stream C of `plans/DESIGN-POLICY-USER-PRINCIPAL.md` (§13-§15). Wire contracts (I1-I8 and
amendments 1-2): `policy-principal-contracts.md`. Everything is inert unless
`Settings.policy_gateway_interceptor` and the workspace's `gateway_interceptor` are on.

## What it is

`policy-engine/` is a Rust workspace pinned to `dogwood-local-engine =1.1.1` and
`dogwood-language =1.1.0` (Apache-2.0; `Cargo.lock` committed, builds `--locked`):

| Crate | Role |
|---|---|
| `lp-dogwood` (lib) | schema generation (`schema.rs`), JSON → typed events (`values.rs`, `events.rs`), Stage A / BT sets with LOG_ONLY shadows and durable BT stores (`workspace.rs`), one shard (`engine.rs`), the compiler (`compile.rs`), the simulator (`simulate.rs`) |
| `lp-policy-engine` (bin `launchpad-policy-engine`) | the service (I4 HTTP + HMAC, I5 Unix control socket, audit JSONL) and the offline commands `compile`, `replay`, `version` |

Assets: `assets/launchpad-template.cedarschema` (the single-namespace `AgentCore` template),
`assets/launchpad-events.dwschema` (Global session semantics: pins on `callerPrincipal` and
`sessionId`), `assets/demo-tools.json` + its generated golden `assets/demo.cedarschema`.

## Decisions (one service call, two evaluations)

* **Stage A** — principal = the calling agent's `AgentCore::IamEntity` (parent `Agent`, parent
  `Workspace`); stateless policies only; evaluated in memory, never recorded.
* **Stage BT** — principal = the token principal (or the agent when no token applies); stateless
  + temporal policies. A temporal-governed action (`temporal_referenced_actions`) is **recorded**
  in the session's owner shard's durable store; any other action is decided statelessly on any
  shard.
* **Nothing is permitted implicitly** (Global parity, 2026-10-28): the compiler adds no permit of
  its own — the former D7 "closure" permit, which let a user pass Stage BT whenever no BT permit
  covered the tool, is gone. A principal with no covering permit is denied by Cedar's default
  deny (`determining` empty); users are allowed only by the hub's baseline permits
  (`backend/app/policy/templates/permit_users_call_tools.cedar`, `permit_workspace_agents_call_tools.cedar`)
  or a narrower permit. Every stage is evaluated for every action; there is no `invoke` action
  (the hub has no invoke gate — policies decide Gateway tool calls only).
* **Evaluation errors fail closed**: Cedar skips a policy whose evaluation errors (e.g. a Long
  overflow), which could let an erroring `forbid` through; any evaluation error in either stage
  makes the decision `deny` with `errors[0] == "policy_evaluation_error"` (the deny reason the
  interceptor reports), followed by the Cedar diagnostics. The erroring stage itself reports
  `deny` (so `/v1/list-filter`, which reads `stage_a`, hides the tool), and the LOG_ONLY shadow
  never reports `allow` for an evaluation that errored (an erroring LOG_ONLY forbid is
  `log_only_would: "deny"`).
* **final** = A ∧ BT; `determining` lists what denied (or both stages' permits on allow) in a stable
  order; `log_only_would` is set when the ACTIVE + LOG_ONLY shadow would decide differently.
* History: per (session, principal) by pins; response / error events come from the RESPONSE
  interceptor; a publish keeps the history of unchanged policies (store diff by `@id` stamp) and a
  new temporal-set version invalidates open sessions (409 `session_invalidated`, T4).

## Compiler (`launchpad-policy-engine compile`, `app/policy/dogwood_compiler.compile`)

Input `{tools_list, policies:[{id, version, mode, statement}]}` → `{ok, schema, items[], errors[],
warnings[], actions[], temporal_count, temporal_referenced_actions[]}` (`closure_actions[]` was
removed with the closure on 2026-10-28; `dogwood_compiler.Compiled` defaults it to `[]`). `items`
are exactly the source statements (split per policy) — the compiler never adds an item.

* Tool names must match the Gateway grammar — `<target>___<tool>` or a bare tool, every
  `___`-separated segment 1–128 of `[A-Za-z0-9_-]` — anything else (quotes, backslashes,
  whitespace, an empty segment) is refused at compile / install, so no tool name can escape the
  generated schema. The tool part may itself carry `___`: an MCP-server Gateway target prefixes
  the remote server's `<server>___<tool>` names with its own name
  (`paipai___paipai-data___detect_anomaly` on launchpad-gw-example001, 2026-10-27), so the live
  manifest has nested separators; the target group is always the FIRST segment
  (`target:paipai`) and the action id the full name — the hub sends the full name and the D20
  rewrite of `resource.name like "paipai___*"` lands on `target:paipai`.
* Tool names with `-` are generated under an alias and their action ids restored; each Gateway
  target becomes a group `AgentCore::Action::"target:<target>"`; every tool action is also in
  `Action::"CallTool"`.
* `temporal_referenced_actions` comes from ONE function shared with the engine's `must_record`
  (`lp_dogwood::coverage`): it follows the action hierarchy (`CallTool` / `Mcp` = every tool,
  `target:<t>` = that target's tools) on the parsed policy, never on text. A statement that does
  not lower against the schema is reported against its own policy id.
* A statement is split per policy (`pol_x#k`). Stage from the principal scope; an unscoped head
  goes by its body (roles / users / keys → BT; Agent / IamEntity / Workspace → A; neither → both;
  both → rejected). Temporal statements are BT only and may not be agent-scoped.
* Global quotas (T8): ≤ 1000 policies, ≤ 20 temporal, ≤ 3 temporal blocks per policy, ≤ 10 KB per
  policy, ≤ 200 KB total, schema ≤ 400 KB; windows ≤ 24 h (event schema default).
* Each stage's set is lowered and schema-validated; diagnostics name the policy.
* `rewrite_d20` (Python) maps stored D20 statements to the Global vocabulary; untranslatable
  shapes are left with a warning and then fail `compile`.

## Simulator (`launchpad-policy-engine replay`, `dogwood_compiler.replay`)

Compiles the draft / pinned set, replays `history[]` (`{tool, kind, principal, input, output,
at_s}`) into a throwaway shard on a manual clock, then decides `request` — the live code path, so
the console simulator cannot disagree with the gateway. Returns `{ok, decision, trace[]}`.

## Operations

* **Build** (`make policy-engine-artifact` = `packaging/build-artifact.sh`): `cargo test --locked`,
  cross-build `aarch64-unknown-linux-gnu` (needs `gcc-aarch64-linux-gnu`; the binary needs glibc ≥
  2.35, the hub runs Ubuntu 24.04 / 2.39), licence gate, `tar.gz` + `.sha256`. Upload is an
  explicit operator step; CN hosts never fetch crates.
* **Install / upgrade** (`packaging/install.sh`, root, on the hub host): verifies the tarball sha,
  creates `lp-policy` (the hub user joins the group), installs `/opt/launchpad-policy-engine`,
  creates `/etc/launchpad-policy-engine/engine-key` once (0640 root:lp-policy; copying it to the
  interceptor's private S3 object is an operator step, the key is never printed), writes one socket
  drop-in per shard on the host's **private** IP (`7443 + n`), and restarts shard by shard gated on
  `/healthz`. It is never part of `scripts/host-update.sh`. The AWS region comes from `--region`,
  else the instance's own region from IMDSv2, and is passed to every `aws` call (a root shell
  such as SSM has no AWS config; N17-obs481). `install.sh --print-region` shows it, with no root needed.
* **Units**: `launchpad-policy-engine@<n>.socket` (socket activation — connections queue during a
  restart; the engine adopts fd 3 only when `LISTEN_PID` is its own pid) + `.service`
  (`Restart=always`, `UMask=0027`, `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`,
  `PrivateTmp`, `PrivateDevices`, `ProtectKernel{Tunables,Modules,Logs}`,
  `ProtectControlGroups`, `ProtectClock`, `ProtectHostname`, `RestrictNamespaces`,
  `RestrictRealtime`, `RestrictSUIDSGID`, `LockPersonality`, `MemoryDenyWriteExecute`, an empty
  `CapabilityBoundingSet`, `SystemCallFilter=@system-service ~@privileged @resources`,
  `MemoryMax=512M` and `TasksMax=2048` per shard; verified
  2026-10-26 with `systemd-run` on systemd 249: bench, install, recorded decides and a restart run
  with no denied syscall). State
  `/var/lib/launchpad-policy-engine/shard-<n>/w-<hex of ws id>/{bt.redb, bt-shadow.redb,
  installed.json}` (the directory name is always the hex of the workspace id, so two ids never
  share one), control `/run/launchpad-policy-engine/control-<n>.sock` (0660 lp-policy:lp-policy in
  the 0750 runtime directory; only group `lp-policy`, i.e. the hub user, can connect), audit
  `/var/lib/launchpad-policy-engine/audit/shard-<n>.jsonl`.
* **Data plane HTTP**: a minimal HTTP/1.1 server (keep-alive, no chunked bodies). Each
  connection is read by its own thread (≤ 1024 connections; beyond that a new one is closed at
  once); only a fully parsed request reaches the `--workers` handler pool, so a slow or idle peer
  never holds a worker. A request has ONE 5 s deadline from its first byte for header + body
  (a peer dribbling bytes cannot stretch it), a keep-alive connection is closed after 5 s idle,
  and both apply before anything is authenticated. Headers ≤ 16 KB / 64 lines (431), body ≤ 1 MB
  (413); `/v1/list-filter` takes ≤ 1024 `actions` (400). Every connection limit applies before
  authentication: ≤ 1024 connections in total and ≤ 256 per source IP
  (`--max-connections`, `--max-connections-per-peer`; Lambda in a VPC reaches the engine through
  shared Hyperplane ENIs, one per subnet + security group, so the whole interceptor fleet arrives
  from at most a few IPs: size the per-peer cap as the fleet's connections per subnet, and rely
  on the security group, which admits only the interceptor's SG, as the real peer control);
  request bodies held at once across all connections ≤ 64 MiB (`--max-inflight-body-bytes`, 503
  beyond it, before the body is read); `GET /healthz` always answers `Connection: close`. A handler panic answers 500 and the
  worker lives on; if the panic poisoned the shard lock the process exits (code 70) and
  `Restart=always` restarts the shard from its durable state — it never serves from a torn one.
  Framing is one plain `Content-Length` only: any `Transfer-Encoding`, a repeated or non-digit
  `Content-Length`, whitespace in or around a field name, obs-fold continuation lines, an extra
  request-line token or a version other than `HTTP/1.0` / `HTTP/1.1` is refused and the connection
  closed (nothing pipelined behind it is served). A repeated header reads as absent, so a
  duplicated `X-LP-*` header fails auth.
* **Install commit protocol**: validate (nothing is written for a set that does not compile) →
  `installed.pending.json` (temp + fsync + rename + directory fsync) → sync the durable stores by
  diff → rename pending → `installed.json` (the commit point) + directory fsync. A crash at any step
  is rolled FORWARD on open to the set being installed; a torn pending file falls back to the
  committed set; stray `*.tmp` files are never read. Crash-injection tests cover both steps. An
  install that fails after validation (a later step errors, or the commit rename fails) removes
  the pending file and re-syncs the store to the committed set by diff (history kept), so the
  running state and the state after a restart are both the old set; if that rollback itself
  fails, the workspace is quarantined (fails closed, 503) until the hub installs it again. The commit
  rename is the commit point: a directory fsync that fails after it is logged and the install
  reported committed (a crash then either kept the rename or rolls the pending file forward to
  the same set).
* **Restart / crash**: the durable stores replay their log (an acknowledged event is never lost —
  kill -9 test); readiness after recovery. A workspace whose store or metadata cannot be recovered
  is **quarantined alone**: its directory moves to `shard-<n>/quarantine/` (never rescanned) with
  a tombstone, its calls answer 503 `unavailable` until the hub installs it again, and the shard
  epoch and every other workspace's history are untouched. The re-install starts that workspace
  from an empty store with `history_lost_at_s` = the quarantine time, so recorded calls of its
  sessions started before then answer 409 `history_lost` (the hub then starts a fresh policy
  session). A first start loses nothing. A directory whose name is not `w-<hex>` of the workspace
  in its metadata (a stray or copied directory) is set aside into `quarantine/` without a
  tombstone when the correctly named directory exists, so no workspace is ever loaded twice;
  when the stray is the only copy of that workspace's state it is quarantined WITH a
  tombstone, so the re-install reports `history_lost` instead of silently starting empty.
* **Ownership**: `record: true` decides and every `/v1/respond` are accepted only on the session's
  owner shard (400 `session belongs to shard <n>` otherwise), so a request and its response
  always land in the same store.
* **Restore**: there is no store copy/restore procedure by design; windows are ≤ 24 h, so a lost
  store costs at most one day of session history, surfaced as `history_lost`.

## Tests

`cargo test` (policy-engine/: schema golden, values, clock, compile incl. the coverage cross-check
with the engine, shard API incl. restart / invalidation / LOG_ONLY / Stage A, the demo flow, store
safety: quarantine, crash injection, owner shard, evaluation-error deny, tool-name grammar,
directory naming); `cargo test --release --test bench_decide --
--nocapture` (engine-internal latency); `backend/tests/policy_principal/` — contracts, the compiler
wrapper and `rewrite_d20`, the real binary over I4 / I5 (`test_engine_service.py`), resilience
(`test_engine_resilience.py`: kill -9, socket activation, latency gate), HTTP limits (idle
connection, 413 / 431 / 411, list-filter bound, keep-alive, foreign `LISTEN_PID`), the
hr-approvals sample.
`make verify` runs the cargo step and a release build so the binary-backed tests run (SKIPPED with
a notice where cargo is absent).

## Latency bench (M1 exit criterion)

`launchpad-policy-engine bench [--iterations N] [--shards S] [--dir PATH]` (`make
policy-engine-bench`): per shard, `stateless` (any shard, no history) and `recorded` (durable
append + fsync) decide latency as p50 / p95 / p99 / max in ms on the engine's own code path, with
a 10 % warm-up excluded and a spread of 64 sessions; exits 0 only when every p99 ≤ 10 ms
(`meets_budget`). Run it on the hub host with `--dir /var/lib/launchpad-policy-engine`, so the
recorded figure is measured on the real volume. The timing tests in `make verify` are soak-only
(`LAUNCHPAD_SOAK=1`); this bench on a quiet hub host is the measurement of record.

## Known gaps

* No `datetime` request value in dogwood-language 1.1: time is `context.system.{now_epoch_s,
  local_hour, local_weekday}` (Longs), not Global's `context.system.now`.
* Taint tracking for a request whose response submit failed (design §12.4) is not implemented; a
  lost response only makes later "requires prior response" rules deny (the safe direction).
* `GET /healthz` is unauthenticated and returns the per-workspace `sets` map (workspace ids and
  set versions) on the private data-plane address. Reducing it to counts is deferred (lead,
  2026-10-26) because it changes the `engine_healthz` contract stream B reads.
* cedarpy is still the hub's evaluator until stream A moves narrowing / the simulator onto the
  engine; it is removed from the runtime dependencies afterwards.

## Install version CAS and restart replays (N17-obs492, 2026-10-27)

* `install` is a version CAS: a `policy_set_version` not newer than the installed one (same
  `<prefix>-<n>` family, `n` not greater, or the same string) is refused with `ok: false` unless
  `force: true` (the hub's rollback / re-sync); another prefix family is always newer.
* Request authentication refuses, for the first 360 s after process start, a timestamp older than
  the start minus 2 s — a request signed before a restart is never a valid replay (F9).

## Global parity (contract amendment 9, 2026-10-27)

The hub no longer sends `invoke` items (no hub invoke gate; policies decide Gateway tool calls
only). N17-obs501 (2026-10-28, branch `cn/pg-engine`) removed the engine side: the schema template
(`assets/launchpad-template.cedarschema`) and its golden `demo.cedarschema` no longer declare
`action "invoke"`, `engine.rs` has no `INVOKE` branch (Stage A is evaluated for every action),
`coverage.expand` has no invoke case and `StageVerdict::allow` is gone. A compiled statement that
names `AgentCore::Action::"invoke"` now fails the stage type-check like any unknown action.
`rewrite_d20` still DROPS a stored D20 statement that names `Launchpad::Action::"invoke"` (warning,
never a compile failure), so a workspace with a legacy invoke baseline still installs. Like every
interceptor-touching change this needs the artifact rebuild (R1) and Lambda update (R2) before it
is live.
