# Hermetic quality suites — fault injection, the permission matrix, the API contract run

Quality pack Q4 + Q6 (branch `cn/r12-h`, N12-obs58). Three additions to the hermetic
backend suite (`backend/tests/`, run by `make verify` through the backend pytest step —
no AWS, no network, temp ledger). They exist so that three questions have a mechanical
answer instead of a reading of the code:

1. **What does the platform do when an upstream fails?** — `tests/faults/`
2. **Who may do what, to which resource, in which visibility mode?** —
   `tests/test_permission_matrix.py`
3. **Does the API answer what its OpenAPI says, for inputs nobody wrote by hand?** —
   `tests/contract/test_openapi_schemathesis.py`

Round 16 (branch `cn/r16-a`, N15-obs16 → `plans/CAPABILITY-MATRIX-DESIGN.md`) added two more
(§6): **Can every combination the creation form offers either deploy or be refused at
creation?** — `tests/test_creation_matrix.py`; and **Can a stage refuse an input the request
boundary should have refused?** — `tests/test_fail_early_guard.py`.

## 1. Fault injection — `backend/tests/faults/`

### 1.1 Why fixtures, not ad-hoc stubs

The production code *classifies* exceptions (`assistant/service._replayable_turn_error`,
`strands_eval/runtime_invoke.is_transient_invoke_error`, `strands_evals.is_throttling_error`)
and decides to replay, wait, record or fail on the exact shape. A test that raises
`RuntimeError("boom")` exercises none of that. The fixtures in `tests/faults/conftest.py`
render one vocabulary of faults into the three shapes the upstreams really produce, and
arm the suite's existing stubbed clients with them.

### 1.2 The vocabulary

| fixture | what it stands for | `frame` rendering (default) | `client` / `transport` rendering |
|---|---|---|---|
| `bedrock_5xx(n, surface=)` | a Bedrock `internalServerException` on the model call (the N-ARCH-obs18 live shape) | SDK frame `error_type: EventStreamError`, text names `internalServerException` | `ClientError` code `InternalServerException`, HTTP 500 |
| `throttle(n, surface=)` | the model / Runtime throttling the caller | SDK frame `error_type: ModelThrottledException` (`ThrottlingException` in the text) | `ClientError` code `ThrottlingException`, HTTP 429 |
| `read_timeout(n, surface=)` | a read timeout on the model (Harness side) or on the data-plane socket | SDK frame `error_type: ReadTimeoutError` | botocore `ReadTimeoutError` raised by the call (`surface="transport"`) |
| `sdk_error_frame(error_type, error=None)` | any terminal frame of the AgentCore SDK (`ValidationException`, `ContextWindowOverflowException`, an expired bearer, …) | one frame with that class name | — |
| `worker_replay_5xx(n, *session_ids)` | the eval worker's `InvokeAgentRuntime` answering 5xx on named sessions | — | `FlakyInvoke`: a `WorkerEnv.invoke` whose first `n` calls per session raise, then answer |
| `worker_judge_5xx(n)` / `worker_judge_throttle(n)` | the judge model failing | — | `FlakyJudge`: a scripted strands `Model` whose first `n` model calls raise a 500 / 429 `ClientError` |

Surfaces (`Fault.surface`): **frame** = the SDK's terminal error frame inside an otherwise
healthy stream (`{"error", "error_type", "message"}`, `rt.SdkStreamError`) — the Harness
side failed; **client** = a `ClientError` raised by the call itself — the Runtime answered
before any stream opened; **transport** = botocore's `ReadTimeoutError` on the call.

Injection points:

* `harness` — the architect preset's data plane. The package's `conftest` **replaces**
  `tests.test_assistant.harness` with `FaultyArchitectRuntime`, so `ready`, `_open`,
  `_turn` and the other imported helpers stream through it. `harness.inject(*faults,
  before=[...])` arms the next calls (one fault per `InvokeAgentRuntime` call; a `frame`
  fault streams `before` — default half a reply — then the frame); the scripted reply
  follows. `harness.stamps` holds the monotonic time of every call (back-off assertions),
  `harness.served` the faults consumed.
* `backoff(seconds)` sets `assistant_replay_backoff_seconds` (the suite pins it to 0).
* `spans_session` — `(provider, session_id)`: a real strands `Session` from
  `tests/fixtures/spans_session.json` through `LaunchpadSpansProvider`, for judge-path tests.
* `HAS_UPSTREAM_TRANSIENT` — the r12-f import guard (`rt.is_upstream_transient`). The
  scenarios that only hold once a shared transient classifier lands are `xfail(strict=True)`
  on a **behaviour probe** (`assistant_replays(exc)`, `judge_retries(fault)`), never on the
  symbol: they flip to real passes the moment the classifier changes, and a strict xfail
  that starts passing fails the gate until the mark is removed.

### 1.3 Scenarios pinned

`test_assistant_faults.py` — the architect turn (`TURN_TRANSIENT_RETRIES = 1`):

| scenario | contract |
|---|---|
| one transient (every `RECOVERING` rendering: 5xx / throttle as frame or client, read timeout as frame) | one `retry` event naming the fault and `backoff_s`; the fresh attempt waits the back-off and runs in a **new** runtime session; the half reply is discarded (deltas = half + full, transcript = one clean exchange); the `turn_replay` row ends `(waited Ns)`; the claim is released |
| the same fault twice | exactly 2 calls, one `retry`, terminal `error`: `detail.error_type` / `detail.error` for a frame (N-ARCH-obs18), the botocore text for a client fault; one `error` row (+ the last attempt's partial text as an `assistant` row for a frame); one logged terminal event; the claim released; the next turn works |
| transport `ReadTimeoutError` on the call | **xfail(strict)** — the turn ends on `error`; the eval worker (and r12-f's `is_upstream_transient`) treat it as transient — the one open finding |
| any SDK frame (`ValidationException`, …) twice | replayed once with **no** wait (not an upstream-transient marker), then `error` naming the class |

`test_worker_faults.py` — the eval worker library (`TRANSIENT_SCENARIO_RETRIES = 2`):

| scenario | contract |
|---|---|
| replay recovers after 1 / 2 × 5xx | `status: ok`, `retries == n`, every attempt on the **same** pre-assigned session id, `on_retry(next_attempt, exc)` announced each |
| replay exhausts the budget | an **errored scenario** (`error_kind: infrastructure`, `attempts == 3`, the `k/N attempts` wording), never a crashed run |
| faults are per session | S2 answers first time while S1 replays |
| judge throttled 1 / 2 × | retried (bounded back-off): one scored record, `n + 1` model calls |
| judge 5xx once | retried: one scored record, 2 model calls (holds since r12-f's shared classifier merged; the behaviour probe `judge_retries` would turn it back into a strict xfail if the retry were lost) |
| judge 5xx forever | one error record per evaluator (`ClientError … InternalServerException`), the run goes on |

`test_restart_sweeps.py` — a backend restart mid-work, through the real startup
(`create_app(resume_jobs=True)`): an architect turn and a Chat turn left with their claims
are persisted `interrupted` from their event logs and get a terminal `error` event (a
reattaching client sees the end, the claim is cleared); a Runtime canary loses its
`running_action` flag and carries `… interrupted by a backend restart —
retry the action`; an eval run queued before dispatch is failed honestly (`submit the run
again`), one whose worker job was dispatched is re-attached by its job id and settles
(every dispatched phase; a `FAILED` job settles the row failed with the worker's reason);
a second start sweeps nothing.

### 1.4 Adding a scenario

Arm the stub, drive the real entry point, assert the documented marker:

```python
def test_my_case(client, ready, harness, bedrock_5xx):
    harness.reply("ok")
    harness.inject(bedrock_5xx(1, surface="client"))
    events = _turn(client, _open(client), "q")
    assert [k for k, _ in events][-1] == "done"
```

A new fault kind is one `Fault(...)` factory in `conftest.py` (both renderings) plus a
fixture; a new injection point is a stub subclass that pops `self.faults` per call.

## 2. The permission matrix — `backend/tests/test_permission_matrix.py`

### 2.1 Shape

One table — `EXPECTED` (status per role) and `LISTED` (whether the seeded row appears in a
list) — covers **19 kinds × their operations × 5 roles × 2 visibility modes = 700 cells**
(N17-obs426 added `online_configs`, `online_configs_pause` and `online_configs_resume`),
every one asserted against the running app. The table is the living permission spec;
the code it was derived from is `app/core/route_policy.py` and
`docs/cn/interfaces/users-and-ownership.md` §4 / §7.

| axis | values |
|---|---|
| roles | `anon` (no session → 401 from the auth middleware), `member` (the creator of the seeded row), `member-other` (another approved member), `admin` (a `users` row with role admin), `config-admin` (the `LAUNCHPAD_AUTH_USERNAME` operator — an administrator without a `users` row) |
| kinds | agents, chat sessions, API keys, datasets, evaluators, runs, Runtime canaries, knowledge bases, managed memories, users, workspace visibility setting, videos (manage + the member feed), announcements (manage + the member feed), policies. **Configuration experiments (`/api/experiments*`) are deliberately absent**: the user decided to remove them (cn/r13-b), so neither the matrix nor the contract run pins routes about to disappear; Runtime canaries stay. |
| operations | list, get-by-id, create, update, delete — only the ones a kind has (datasets have no by-id GET; chat sessions no delete; users no get; the visibility setting only get / update) |
| modes | `shared`, `isolated` (`Workspace.settings.resource_visibility`, switched per cell) |

A cell value is one status, or `(shared, isolated)`. Reading guide:

* `HIDDEN = (200, 404)` — visible under `shared`, the kind's own not-found under
  `isolated` (`agent.not_found`, `dataset.not_found`, `memory.not_managed`, …).
* Agents: create / redeploy / delete ride `perm:agents.*` (default granted) **and** the
  creator-or-admin gate: a stranger gets `403 agent.forbidden` under `shared`, 404 once
  hidden. API keys and chat sessions follow the step-1 member rules in **both** modes
  (own keys / own sessions listed; a foreign id is 404, never 403); the by-id session stop
  stays open under `shared` (the list is the rule) and is gated under `isolated`.
* Datasets, evaluators, runs, KBs, memories: anyone with a session acts on what they can
  see (`VISIBLE_RW`). Canaries: the same reads; create is a `422` probe and the action a
  `409 stage_not_ready` probe — both prove the gate was passed without touching AWS.
* Users, the visibility setting, videos / announcements management: `ADMIN` (members
  403); `POST /api/auth/register` is `PUBLIC` (an anonymous caller creates an account);
  the member feeds list only published snapshots (a fresh draft is not listed).
* Policies stay shared in both modes; `perm:policy.manage` is default granted.

Every mutation that would reach AWS is replaced at the handler's seam inside the `world`
fixture (deploy start, agent teardown, runtime session stop, the KB / memory backends,
the experiment / canary stage check, an S3 fake behind the client funnel), so a cell's
outcome is the **authorization** decision. One test per kind builds one world and walks
every cell on a freshly seeded row (ids carry the cell counter); every mismatch is reported
together, not just the first.

### 2.2 Disagreements

A cell where the running code and the spec disagree is **kept as the spec** and listed in
`DISAGREEMENTS` with the reason; the walk then requires it to keep disagreeing (strict-xfail
semantics by hand — a cell that starts to agree fails the kind's test until it is removed).
Nothing is ever rewritten to match the code. The list is **empty** (r12-h; still so with N17-obs426): all 700
cells answer as the spec says.

### 2.3 Adding a cell

1. Add a `Kind(name, seed, ops, list_ids)`: `seed(world)` inserts the row **as `member`**
   (use `world.cell` for unique names / ids) and returns the id the paths use; `ops` maps
   `list` / `get` / `create` / `update` / `delete` to `(method, path, json, query)` builders;
   `list_ids` extracts the ids of a list response.
2. Add its row to `EXPECTED` (one entry per operation, 5 statuses in `ROLES` order) and, when
   it has a list, to `LISTED`.
3. Run `uv run pytest tests/test_permission_matrix.py -q`; update the size assertion
   (`test_matrix_size_is_what_the_doc_says`) and this page.

## 3. The API contract run — `backend/tests/contract/test_openapi_schemathesis.py`

Schemathesis 4 (`schemathesis.openapi.from_asgi`) generates requests from
`GET /api/openapi.json` and calls the app **in-process** — no server, no socket (every
`socket.connect` is refused for the module), no AWS (the client funnel raises at once with
*add its path to `EXCLUDED_PATHS`*, instead of waiting out botocore's connection retries).
It is authenticated as the administrator the open console grants
(`LAUNCHPAD_ALLOW_OPEN_CONSOLE`), stateless, `derandomize=True`, `MAX_EXAMPLES = 8` per
operation, no Hypothesis database.

| | |
|---|---|
| scope | every `GET` except `EXCLUDED_PATHS` (the `/api/experiments*` prefix no longer exists since cn/r13-b) (routes that only exist against a live account or a running sub-process — discovery, identity, gateway, Logs / X-Ray, memory data plane, S3-backed artefacts, …) and the two pure `POST`s `/api/policies/validate`, `/api/policies/format`. No other `POST` / `PUT` / `PATCH` / `DELETE`: those create billable resources, deploy or destroy (the permission matrix covers their authorization). `/v1` is out (key auth, its own tests). |
| checks | `not_a_server_error` (no 5xx at all); `success_schema_conformance` (a 2xx body matches its declared schema); `error_envelope` (every 4xx / 5xx body is the `{code, message, detail}` envelope of `core/errors.py` with a dotted code — the project's error contract) |
| budget | ~90 operations, ≈ 40 s serial; inside the parallel backend step it costs a few seconds of wall time |
| run alone | `make contract` (adds `--hypothesis-show-statistics`) |
| scope guard | `test_the_scope_is_what_the_doc_says` pins the method set, the `POST` allowlist and that no excluded path is exercised |

Why `error_envelope` replaces the stock `status_code_conformance`: FastAPI documents only
the success status and `422` per operation, so every handler-raised `404` / `409` / `503`
would be "undeclared" — while the API's real contract is the envelope on **every** error.
The schema check skips 4xx for the same reason: FastAPI's generated `422` schema
(`HTTPValidationError`, `detail: ValidationError[]`) does not describe the envelope the app
returns (`detail` is `null` for a handler-raised 422). Declaring the envelope in the
OpenAPI document is a contract decision for the lead (the evaluation slice is frozen by
`openapi_baseline_eval.json`).

### 3.1 Findings (r12-h, 2026-10-17)

| finding | outcome |
|---|---|
| unbounded integer query parameters (`offset` on users / announcements / eval runs, `expected_revision` on videos and announcements, `before` on the model-registry journal) accepted a Python int above SQLite's range and raised `OverflowError` → 500 | **fixed**: `core/db.SQLITE_MAX_INT` bounds them (`le=`), the contract-frozen `GET /api/eval/runs` clamps in the handler instead |
| the reattach routes (`GET /api/chat/{agent}/sessions/{sid}/turns/{turn}/stream`, `GET /api/assistant/architect/conversations/{id}/turns/{turn}/stream`) read `?from=` raw from the query string (invisible to the OpenAPI document, so never generated here) and took an unbounded `turn`: a value above SQLite's range raised `OverflowError` in `chat_turns.turn_events` (N16-obs86, t1 journal 2026-10-19) | **fixed** (r16-j): `turn` is `Path(ge=0, le=SQLITE_MAX_INT)`, `from` is a declared query parameter parsed by `live_turns.parse_from` (`0..SQLITE_MAX_INT`, else the route's `*.invalid_seq`); `tests/test_turn_param_bounds.py` pins 2**63 / 2**63-1 per parameter and the OpenAPI presence |
| FastAPI's generated `422` schema does not describe the launchpad envelope | documented above; `success_schema_conformance` skips 4xx — the lead decides whether to declare the envelope in OpenAPI |
| `/api/identity/connections/oidc-sources` and `/api/registry/attachables` need the control plane even for a read | listed in `EXCLUDED_PATHS` (e2e territory) |

### 3.2 Adding an operation / handling a new failure

* A new `GET` route is in scope automatically. If it needs a live account the run fails
  with the funnel's message — add its path to `EXCLUDED_PATHS` with the API it calls.
* A new side-effect-free `POST` goes into `SAFE_POSTS`.
* A genuine finding (a 500, a non-envelope error, a 2xx that does not match its schema) is
  a bug in the app, never a reason to loosen a check; record it in the N12 register.

## 3b. Property and model tests — memory, KB, credentials, registry (`backend/tests/property/`, r16-d)

Round 16's channel-A deep tests for E06 / E17 / E08 / E16 (BUG-DISCOVERY-PLAN §4). They are
Hypothesis properties and one `RuleBasedStateMachine` over the real routers / backends on the
hermetic substrate (`tests/memory_fakes.py`, the `MagicMock` vault of
`tests/test_identity_connections.py`, SQLite). Three rules every test follows:

* **every invariant cites its contract clause** in a comment (`memory-backend.md` "Merge
  strategy (v2)", `memory-runtime.md` "Recall", `knowledge-backend.md` "Delete contract" /
  "Error codes" / R18, `model-credential.md` "Deletion guard", `users-and-ownership.md` §7–§8,
  `registry-backend.md` "State machine"); thresholds are typed from the contract, never
  imported from the implementation (the one identity function that IS imported —
  `handler._record_id`, so seeds collide the way production does — says so);
* **oracles are differential, round-trip or metamorphic**: a sequential model of the merge
  decision table vs the extractor's job counts and document delta; bytes in == bytes out for
  uploads; the router's refusal order replayed on the request; a paraphrase relation (same
  embedding, other wording → same op class); the R4 transition table vs `/action`;
* **a failing shrunk example is a finding, never a reason to weaken the invariant**: it stays
  `pytest.mark.xfail(strict=True, reason="N16-obsN …")` next to a deterministic repro, and the
  generator that is still expected to pass is narrowed explicitly (e.g. "one op per candidate
  per event") so the boundary of the defect is pinned too. Register:
  `docs/cn/reports/bugs-round-16.md` (N16-obs30–39).

| file | what it drives | invariants (short) |
|---|---|---|
| `ledger_invariants.py` | **reusable** — `assert_ledger_invariants(SessionLocal)` / `scan_ledger` over the SQLite ledger, `assert_memory_index_invariants(docs)` / `scan_memory_index` over a memory index's documents | no orphan reference-by-convention (agent → KB pin, memory pin, registry record); registry status ∈ R4 table, stamps match status, `(workspace, name, version)` unique; KB rows never rest in `DELETING`, data sources inside `kb/<kb_id>/` (R18); memory stores' strategies in the catalogue and merge knobs inside the contract bounds; identity providers one per (workspace, kind, name); memory: `superseded_by` chains resolve and are acyclic, `supersedes` members exist and are retired, no self-supersede, no two ACTIVE records ≥ 0.92 in one facts / preferences namespace. Call it at the end of every state machine / walk. |
| `test_memory_merge_properties.py` | the extractor Lambda (`process_memory_extract`) + the hub backend over one fake substrate; `VectorEmbedder` assigns every text its vector so similarities (0.6–1.0), confidences (0–1 / null / absent) and legacy pre-v2 documents are generated, not guessed | decision table == job counts; document delta == adds + supersedes; soft delete only on resolvable targets with stated confidence ≥ 0.6; history never resurrected; recall ⊆ active, ranked by cosine × 0.5^(age/half-life) × max(conf, 0.5) over the min(4k, 20) over-fetch; caps → one bounded consolidation pass per event, convergence, single/unknown/reused ids ignored; TTL marks ≤ 50 / event, hides on read, never deletes, never the actor summary; paraphrase metamorphic; **seam faults** (`FaultyOpenSearch` replays `OpenSearchError(429/503/502, body)` and `urllib3.exceptions.ReadTimeoutError`, the N14-obs4 shape) → 503 `memory.backend_unavailable` / 502 `memory.unavailable`, never a traceback or body, store delete still 200, record delete of a gone row 404; the extractor's `RetryableError` classification |
| `test_knowledge_ingest_properties.py` | `POST …/files` with random file lists (0 bytes, the per-file / total caps, non-UTF-8, 50 MB), `kb_ingest` over random documents, random create / attach / detach / delete / delete-data-source walks | refusal order `kb.no_files` → `kb.too_many_files` → `kb.upload_too_large` with no S3 write, round trip of accepted files; every object → exactly one kb-docs record, chunks iff INDEXED; mounted KB → 409 naming each agent once, unmounted → full `cleanup` block, retried delete 404, ingestion-job cache / `kb_cleanup` degrade never 5xx; ledger scan after every step |
| `test_credential_reference_matrix.py` | `DELETE /api/identity/connections/{kind}/{name}` as a 120-cell random matrix: kind × referrers (own / other / admin / deleted pin, model provider, tool auth) × role (admin, member ± `identity.manage`) × shared / isolated × order (delete, repoint, rename provider) | 403 before any guard for a member without the permission; `identity.connection_referenced` before `identity.in_use` before `identity.external_connection` before 404; `referenced_by` / `total` / `truncated` follow the viewer's masked list; repoint lifts, rename keeps; secrets never echoed; ledger clean |
| `test_registry_state_machine.py` | `RegistryMachine` — four actors (config admin, admin account, two members) × register / legacy NULL-creator rows / PUT (description, url, SKILL.md) / DELETE / reimport / `/action` / reads × shared / isolated toggles, `LAUNCHPAD_REGISTRY_SM_EXAMPLES` (default 25) | §8 creator-or-admin on content mutations (403 with the creator detail before any type / status check; NULL creator → admins), §7 reads shared in both modes, R4 transitions legal only (409 `registry.invalid_transition` with from / to), description edits keep status + version, content edits → DRAFT + minor bump, decisions never change the version, same-name register refused, deleted → 404 everywhere; invariants: ledger scan, list == model for every actor, live skill records have their files |

Run them alone with `cd backend && uv run pytest tests/property -q -n auto` (≈ 2 min serial; they
are part of the backend step of `make verify`). The properties cap at 40–150 examples with
`deadline=None`; the expected-failure properties run with no shrink phase (`HYP_XFAIL`) so a
registered finding costs seconds, not minutes.

## 4. Commands

| what | command |
|---|---|
| the three suites | `cd backend && uv run pytest tests/faults tests/test_permission_matrix.py tests/contract -q -n auto` |
| one fault scenario | `cd backend && uv run pytest "tests/faults/test_assistant_faults.py::test_turn_recovers_after_one_transient[throttle-client]" -q` |
| the matrix, one kind | `cd backend && uv run pytest "tests/test_permission_matrix.py::test_every_cell_of_the_kind_answers_as_the_spec_says[agents]" -q` |
| the contract run alone | `make contract` |
| the round-16 model / property suites (§6) | `cd backend && uv run pytest tests/model tests/property -q -n auto` |
| one soak variant (10× steps) | `cd backend && LAUNCHPAD_SOAK=1 uv run pytest tests/model/test_canary_state_machine.py::test_soak_canary_state_machine -q` |
| the seeded fault walks (§6.9; not in `make verify` since r17-s2) | `make graph-faults` (= `LAUNCHPAD_FAULT_WALKS=1`) |
| the loop-engine soak (§6.8; not in `make verify`) | `make graph-soak` |
| the real-engine interceptor tests (`tests/policy_principal/test_interceptor_real_engine.py` — the Lambda code end to end against the Rust engine; in `make verify`, where they run against the per-worktree release binary `policy-engine/target/release/launchpad-policy-engine` that the gate's policy-engine step builds before the backend pytest step, and SKIP — never fail — while it is absent; `LP_ENGINE_BIN` / `LP_ENGINE_ASSETS` override, N17-obs516) | `cd policy-engine && cargo build --locked --release && cd ../backend && uv run pytest tests/policy_principal/test_interceptor_real_engine.py -q` |

| the creation matrix (+ a JSON line per cell) | `cd backend && CREATION_MATRIX_REPORT=/tmp/cm.jsonl uv run pytest tests/test_creation_matrix.py -q -n auto` |
| the fail-early guard + the declaration parity | `cd backend && uv run pytest tests/test_fail_early_guard.py tests/test_method_capabilities.py -q` |
| re-freeze the method declaration fixture | `cd backend && UPDATE_FIXTURES=1 uv run pytest tests/test_method_capabilities.py -q` |

| the property / model tests (§6) | `cd backend && uv run pytest tests/property -q` · `cd frontend && npx vitest run src/v2/scorecard.property.test.ts` |
| the same, 7× the examples (the round's 1000-step stop rule) | `cd backend && LAUNCHPAD_PROPERTY_SCALE=7 uv run pytest tests/property/test_pipeline_state_machine.py -q` |
| everything | `make verify` |
| the property / model suites (round 16) | `cd backend && uv run pytest tests/property -q -n auto` |

## 5. Quality metrics board (`cn/q-metrics`)

`make metrics` (= `python3 scripts/quality_metrics.py`) aggregates the quality evidence that the
rounds leave behind into one page and one history line per day. It is **read-only** over its
sources, **stdlib only**, makes **no AWS call** (C8), and masks anything that looks like a
credential before it writes (C4: `redact()` — access-key ids, `key=` / `token=` / `password=`
assignments, bearer strings, JWTs, long base64 runs).

### Outputs

| path | content |
|---|---|
| `docs/cn/reports/quality-metrics.md` | the board: a 12-line summary with trend arrows vs the previous snapshot, then one table per source (§7 Discovery and §8 Mutation since round 16 — see §6 below) |
| `docs/cn/reports/quality-metrics-history.jsonl` | one JSON line per **day** (`{"date", "kpis": {…}}`); re-running on the same day replaces that day's line (idempotent), so the file is safe to commit after every run |

Both are committed. The markdown is deterministic for a given tree, external inputs and `--date`,
so a diff of `quality-metrics.md` shows exactly what changed in the evidence.

### Sources and what is read from each

| source | default location | what is extracted |
|---|---|---|
| storyline runs | `docs/cn/evidence/round-*/**/REPORT.md` (+ sibling `json/run-summary.json`, `progress.md`) | per run: date, host sha (`host <sha>`), passed / failed / skipped, wall-clock duration (`N h M min` or `(N min)` in the header), §6 verdict, failing steps with their spec file and **class** |
| quarantine | `tests/e2e/cn/QUARANTINE.md` | rows of the table; a row is *active* while its state cell says `quarantined` and not `exited` / `released`; max age = today − `first` |
| registers | `docs/cn/reports/bugs-round-*.md` | every table row whose first cell starts with a finding id (`N12-obs49`, `N-ARCH-obs18`, `N10v3-bug2`, `WB-003`, …); per register: unique ids, state, severity, user-reported flag |
| security | latest `docs/cn/reports/security-scan-*.md` (by file-name date) | the surface × severity table (`Critical` / `High` / `Moderate` or `Medium` / `Low` columns) summed per column |
| ops | `--ops-ledger` (default `/home/ubuntu/workspace/launchpad-cn/deploy-ops-ledger.md`, outside the repo) | `host-update done <a> → <b>` lines (host = first `t1` / `default` in the line, unit minutes when the line carries `hh:mm -> hh:mm ~N min`), restart lines, rollback lines, `DRILL` lines with their `stop → healthy = N s` timing |
| gate | `--verify-glob` (default `/tmp/verify-*.log`), last `--gate-last` (10) by mtime | `verify: PASS/FAIL`, `verify_rc=N`, the longest pytest summary line (= backend suite: passed / failed / seconds), failed `── section: FAIL` names. No wall clock is logged, so the backend pytest time stands in for gate duration; a log without a verdict is a gate still running |

Every source is optional: a missing file produces an empty section, never an error.

### Heuristics (how a value is decided)

- **Counts** come from `json/run-summary.json` when it carries at least one graded test
  (`expected` / `flaky` → pass, `unexpected` → fail, `skipped` → skip); otherwise from the
  REPORT text `N passed / M failed [/ K skipped]` — the header match first, else the **last**
  match in the file (the latest attempt of a repair-run REPORT).
- **Failing steps** = `unexpected` tests of the summary, or table rows whose verdict cell is
  `FAIL`, or `**sNNx FAIL` bullets, or the `(s08c, s09)` list after the failed count.
- **Failure class** per step, from the REPORT lines that mention the step, in the README's
  order: an explicit `**ENV**` / `**TEST**` / `**PRODUCT**` tag wins; else the raw error text
  (`UPSTREAM_TRANSIENT_RE` set: `internalServerException`, `ServiceUnavailable`, `Throttling`,
  "Try your request again", read timeouts, HTTP 502–504, host-update window, tunnel / DNS)
  → **ENV**; else TEST markers (`scenario state … missing`, test-side, strictness, planning
  error, counting nit, blank screenshot, stale assertion) → **TEST**; else product markers
  (`product bug`, `register row`, an `N*-bugN` id) → **PRODUCT**; else **UNCLASSIFIED**. A
  step whose context says `cascade of sNN` inherits that step's class and is marked so.
- **Register state**: an explicit `state` / `status` cell decides (`fixed` / `resolved` /
  `closed` → fixed; `open` / `pending` / `scheduled` / `deferred` / `candidate` / `recorded`
  → pending; `—` → n/a); without one, a non-empty `fix` / `commit` cell → fixed; else the same
  keywords over the whole row; else **recorded** (an observation row with no state at all —
  the older 7-column registers). A later row with the same id is a **state update** of that
  id, not a new finding (ids count once per register). `live re-test pending` / `pending live
  confirmation` sets a separate flag and does not change the state.
- **Severity**: the `severity` / `sev` cell, else the first `Critical | High | Medium |
  Moderate | Low | Info | P0–P3` token in the row; `P0→Critical, P1→High, P2→Medium, P3→Low,
  Moderate→Medium`.
- **Severity**: the `severity` / `sev` cell, else the first `Critical | High | Medium | Moderate |
  Low | Info | P0–P3` token in the row, **case-insensitive** since round 16 (the round-14/15 cells
  spell `medium (PRODUCT)`); `P0→Critical, P1→High, P2→Medium, P3→Low, Moderate→Medium`.
- **User-reported / escaped**: id prefix `WB-`, the `found-by: user` marker (§6), or the row says
  `user-reported` / `user report` / `user finding` / `reported by the user` / `found by the user` /
  the word `USER` / `逃逸`. "User decision" rows (round-13 deletions, round-5 `USER DECISION`
  rulings) are **not** user-reported defects. **Defect escape ratio** = user-reported ids / all ids.
- **Ops rollbacks**: `rolled back` / `rollback` lines, excluding `--disable-rollback` and
  product canary / experiment rollbacks (`canary`, `control-wins`, `experiment` in the line).
- **Trend arrows** in the summary compare each KPI with the most recent history line dated
  before the snapshot: `→ unchanged`, `↑ from X (better|worse)`, `↓ from X (better|worse)`;
  `(no previous)` on the first run. "Better" follows the metric's direction (more fixed,
  fewer pending / ENV failures / Critical+High, fewer quarantine days, more gate passes,
  shorter pytest).

### Running it

```bash
make metrics                      # today's snapshot, defaults above
python3 scripts/quality_metrics.py --date 2026-10-18 --ops-ledger /path/ledger.md \
    --verify-glob '/tmp/verify-*.log' --gate-last 10          # explicit inputs
python3 scripts/quality_metrics.py --no-write --json          # compute only, print the snapshot
make metrics-test                 # hermetic parser tests on scripts/tests/fixtures/quality_metrics
```

`metrics` and `metrics-test` are **not** part of `make verify`: the board reads evidence that the
gate must not depend on (external ledger, `/tmp` logs) and its regeneration is a deliberate,
dated act, not a side effect of every gate run. Register: N13-obs12
(`docs/cn/reports/bugs-round-13.md`).

## 6. Round-16 discovery suites (channel A)

One section per suite family; every invariant cites its contract clause, a reproduced failure is a strict `xfail` register row (`N16-obs*`), and nothing is weakened to go green.

### 6.1 Model tests — assistant, canary, skill lab, chat, governance (`cn/r16-e`, round 16)

Round 16's channel A for the two architect / governance deep dives of `BUG-DISCOVERY-PLAN.md`
§4: instead of one scenario per test, a **model** drives the real routes through random
programs (Hypothesis `RuleBasedStateMachine`) and checks invariants after every step, each
invariant naming the contract sentence it enforces — never a threshold copied from code —
and preferring **differential**, **round-trip** and **metamorphic** oracles over expected
values. A failing shrunk program is a FINDING: it is registered (`docs/cn/reports/
bugs-round-16.md`, `N16-obs40..49`) and pinned as `pytest.mark.xfail(strict=True,
reason="N16-obsN: …")` with the minimal repro in the docstring; the invariant is never
weakened to go green, and the random walk steers around a *known* pinned path only so the
gate stays deterministic. Every file runs < 60 s in the gate (`-n auto`); the `@pytest.mark.
slow` soak twins (10× steps / examples) run only with `LAUNCHPAD_SOAK=1`.

| file | model | oracle / invariants | contract |
|---|---|---|---|
| `tests/model/test_assistant_state_machine.py` (E02) | one conversation: user turn (stubbed architect — prose, valid / invalid block, RFC 6902 change incl. a stale base and a failing `test`), member edit (random subset of the 22 contract keys, `evaluation_plan` kept / absent / `null`), approve (latest / older / wrong hash / unknown), reject, deploy completes, canary attached, CLEAR (inline or job), restart (`create_app(resume_jobs=True)`), dead-process turn + sweep | **differential**: the stored revision == `proposal.validate(effective raw)` (status, content bytes, errors); **idempotence**: a `draft` re-validates to itself with no errors; revisions contiguous, ≤ 1 open (`draft` / `invalid`) and it is the newest; approve only a current `draft` with its hash (`proposal_stale` / `proposal_not_approvable` otherwise), idempotent re-approve; ONE Agent per conversation — a later draft is `409 already_approved` naming the approved revision, no row (N16-obs40); `resources_locked` after approval; **round-trip**: unedited keys byte-identical, `proposal_repaired` note ⇔ the seed was carried; CLEAR: ledger-only → inline 200, live deploy job → 409 `conversation_busy`, else 202 + job; afterwards no conversation row of any kind, Agent `deleted`, canary `cleaned`, repeated DELETE 404, nothing left to resume; interrupted turn → exactly one `turn_interrupted` row, terminal event appended, a complete block becomes a revision, a cut fence none, second sweep a no-op; the shared ledger scan; **metamorphic**: paraphrased requirement texts → identical revision shape (content, bindings, hash) and an identical preamble | architecture.md §Architect assistant (*Proposal submission, validation and revisions*, *Validation reports every failing stage*, resource lock), assistant-turns.md §2 (sweep) and §10 (CLEAR), bugs-round-15.md N15-obs13 (carry rule) |
| `tests/model/test_canary_state_machine.py` (E13) | one canary: every action × caller (creator / other member / admin) × `allow_non_significant` × drawn verdict × a dead action thread (stuck `running_action`) × conversation CLEAR (request / execute) × restart; the AWS-touching actions are ledger emulations writing the real artifacts, `advance` / `complete` / `cleanup` are the real functions with the control-plane / S3 seams recorded | the route answers exactly the model's code and a refusal has **no side effect** (row + seams unchanged): §8 `403 canary.forbidden` first, `assistant.agent_clearing` while a clear is live, `action_in_flight`, `stage_not_ready` per the ramp table, the verdict gate codes (`verdict_blocked` / `override_required`); ledger mirrors the model (status, ramp stage, weights, A/B `RUNNING` ↔ `STOPPED`, exactly two owner online configs while live, none + no assignments once `cleaned`); `complete` promotes the candidate spec / version; a restart clears a stuck flag with the documented error unless a clear is live; a clear leaves the canary `cleaned`, the Agent `deleted`; NULL creator → administrator-only at every stage; cleanup idempotent | users-and-ownership.md §8, evaluation-engine.md §routing / verdict (N-SCN-obs14 / obs15), assistant-turns.md §10, testing.md §1.3 (restart sweep), N15-obs10 |
| `tests/model/test_skill_lab_state_machine.py` (E15) | task sets (single / split, a sleeping task) created by members; eval / train submit by creator / other / admin with the model credential present or missing; wait, cancel, resume, publish, delete; stub CLIs as real subprocesses through the real queue | 503 `model_credential_missing` at submit **and** at resume with no row / dir / status change; §8 gate on cancel / resume / publish / delete before any side effect (the Registry seam is not reached); lifecycle codes (`job_not_cancellable`, `job_not_resumable` for eval / non-interrupted, `publish_unsupported`, `job_not_deletable`); rows ↔ artifact directories agree (no orphan directory, no row without its directory, a deleted job's directory gone); creator stamps; the ledger scan. Scenarios: restart → `interrupted` with the honest reason, creator's resume continues from the checkpoint (`RESUMED`), other member 403; publish twice (N16-obs44 — fixed: the second call is `unchanged: true` with no Registry write; the `publish` rule expects the write only when the record moved past this job's last publish) | users-and-ownership.md §8, CLAUDE.md / N13-obs20 (credential at submit), architecture.md §Skill Lab (resume-from-checkpoint, publish = minor bump, idempotent on content), `jobs.sweep_interrupted_jobs` |
| `tests/property/test_chat_concurrency.py` (E04) | two tabs on one session (one `TestClient` per tab): send / held send / racing sends (barrier) / reconnect from a seq / refresh / stop / abort-subscription programs over a gated producer; the real SDK terminal error frame (`tests/faults` `Fault.frame()`) at a random position; dead-process turns swept twice; a session state machine | exactly one turn per racing pair (`[200, 409 chat.turn_in_progress]`); `seq` contiguous and unique per turn, POST frames == log, exactly one terminal event; **history == the log replayed through `TranscriptRecorder`** (no lost / duplicate delta); reattach == the events after `from`, ending on the same terminal; claim released; stop ends only the runtime session; a frame fault → one `error` naming `error_type`, partial kept, next send is `turn + 1`; the sweep writes the `interrupted:` row and terminal event exactly once and is idempotent | assistant-turns.md §4, §8.2 (claim, reducer-first, sweep), §8.3 (reattach), §8.5 pins |
| `tests/property/test_cedar_differential.py` (E07) | random policy sets (1–4 permit / forbid `call_tool` rules: literal, `like` prefix, `target ==`; agent-scoped or not; 110-char and Unicode-target literals) × declared literals / the record wildcard × `log_only` / `enforce`; a `policy_stale` state machine (publish / retire / mode / `mark_applied`); random mode-toggle sequences through `gate_invoke`; Unicode + 2 kB statements | **differential**: `policy_bundle.evaluate` outcome == the runtime guard's `policy_decide` (exec'd from the template) over `policy_bundle.build` for every ledger-known tool **and** for every `<target>___<word>` the strategy can name whether declared or not (N16-obs47 — the rules' own `like` / `target ==` / `tool ==` / literal shapes are table keys; plus nine deterministic shape cases), NO_DECISION never; stale ⇔ the `(mode, policy_versions, runtime guard)` signature changed (both guard directions — N16-obs48), `off`/`off` never stale, `mark_applied` clears it; recorded `policy_decisions` rows are byte-identical across later mode toggles and grow by exactly one per evaluated call; `format∘format == format` | policy-backend.md §Decision flow, §Enforcement (table, *Stale*), §Runtime guard (bundle, "one compiler"), §Decision audit |
| `tests/property/ledger_invariants.py::scan_control_plane` (+ `test_ledger_invariants.py`) | the control-plane section added to r16-d's shared scan (`scan_ledger` / `assert_ledger_invariants` run it too): orphan assistant rows, canaries / assignments / owner-prefixed online configs naming a missing Agent or canary, Skill Lab jobs naming a missing task set, a live purge job that is not its conversation's claim, unknown statuses | every machine runs `scan_ledger(SessionLocal)` between steps, **restricted to the rows it owns** (another test's detached turn driver may land rows in the worker's ledger after that test's table wipe — cross-test leakage the scan must not blame on the walk) | assistant-turns.md §10, evaluation-engine.md §routing, users-and-ownership.md §8 (creator columns) |

**Findings of the first run** (register rows; every pin has since flipped from strict
`xfail` to pass — the invariants were never weakened): N16-obs40 (E02: a second creation
approval minted a second Agent — **fixed** in `cn/r16-l`: `409 assistant.already_approved`),
N16-obs44 (E15: publish was not idempotent — **fixed** in `cn/r16-l`: `unchanged: true`, no
second version), N16-obs45 (E04: a live-tail reattach from at / past the last seq ended with no
terminal event — **fixed** in `live_turns.tail_turn`, `cn/r16-e`), N16-obs47 (E07: simulator
DENY vs guard ALLOW for an undeclared literal under a finer `like` forbid — **fixed** in
`cn/r16-l`: the rules' own shapes are bundle keys, the guard lets a forbid-backed key win),
N16-obs48 (E07: `is_stale` ignored the runtime-guard flag — **fixed** in `cn/r16-l`).

**Waiting on a detached turn (E04, N16-obs89d).** A wait for a chat turn to finish is a
`Thread.join` on that turn's own driver — recorded through the `LiveTurnRegistry.start` seam by
the `producer` fixture (`Producer.wait_finished(agent_id, session_id)`, teardown
`wait_all_finished()`) — never a poll of the process's thread list: a gated turn held open on
purpose on the model's own session is live by design, so the former "no `chat-turn-driver-*`
thread anywhere" predicate made `fresh_session` fail after its 20 s budget whenever the random
walk picked it mid-turn; the join returns the instant the turn is over, and the budget
(`BUDGET_S`, 20 s × `LAUNCHPAD_TEST_BUDGET_SCALE`, default 1) is reached only on a real hang.

**Writing a rule.** Compute the expected answer from the contract *before* the call, call the
real route, assert the status / code, then assert the ledger did (or did not) change — a
refusal must leave the row and every recorded seam untouched. Keep the AWS seams at the
functions the production code calls (`canary_service.act_*`, `canary_infra.*`,
`purge.agent_*`, `registry_console.*`), never deeper. Reset the example's world in
`__init__` (`tests/model/worlds.py`): the autouse table wipe runs once per test, not per
example.

### 6.2 Mutation testing & discovery KPIs (round 16, `cn/r16-f`)

The round-16 bug-discovery plan (`plans/BUG-DISCOVERY-PLAN.md` §0 / §4) measures how **strong**
the tests are, not whether they pass, and tracks five discovery KPIs on the board. Register:
N16-obs60 (`docs/cn/reports/bugs-round-16.md`); baseline:
`docs/cn/reports/mutation-baseline-2026-10-19.md`.

#### 6.2.1 Mutation testing

A mutation test rewrites one expression of the production code (`>=` → `>`, a string to `""`, a
`return x` to `return None`, …), re-runs the tests that reach it and asks whether any test fails.
A **killed** mutant means the behaviour is pinned by a test; a **survived** mutant means no test
distinguishes the original from the mutated program — that behaviour is untested, however green the
suite. A mutant that **no test reaches** is also untested (`no tests` / `no coverage`) and counts as
survived in the score the board shows; `score (covered)` leaves it out and tells how sharp the
tests are *where they exist*. `timeout` mutants (an infinite loop) count as killed by convention
in both tools but are listed separately.

| | backend | frontend |
|---|---|---|
| tool | `mutmut` 3.8.0 (pinned dev dependency in `backend/pyproject.toml`) | `@stryker-mutator/core` + `vitest-runner` 10.0.0 (pinned in `frontend/package.json`) |
| target | `make mutation` — a cold run is **hours** (5 476 mutants; every worker imports the app afresh, ≈ 5 s CPU, and the host ran at load 160–340 on 32 cores during the baseline, which turned that into minutes of wall time per mutant); later runs are incremental — mutmut keeps its verdicts in `backend/mutants/`, so an interrupted run resumes where it stopped | `make mutation-frontend` (≈ 2 min) |
| scope | `[tool.mutmut] source_paths` — the validation layer and the guards of §4 only: `schemas/agent.py`, `deployer/harness_local.py`, `services/{ownership,visibility,login_guard,users,builtin_tools}.py` (`assistant/evaluation_plan.py`, 40 % of the mutants, is deferred to the next baseline run — N16-obs67) | `stryker.config.json` `mutate` — `src/lib/{agent-spec,assistant,memory,model-defaults}.ts`, `src/v2/{scorecard,compare,ownership}.ts` |
| tests run | `pytest_add_cli_args_test_selection` — the directly related test files only (listed per source file in the config); per mutant mutmut runs only the tests its stats phase saw reach the mutated function | `testFiles` — the ten test files that import the mutated modules; Stryker's `perTest` coverage runs only the tests that cover each mutant |
| results | `cd backend && uv run mutmut results` · `uv run mutmut show <mutant>` · `uv run mutmut browse` | `frontend/reports/mutation/mutation.html` (+ `.json`) |
| report | `python3 scripts/mutation_report.py` prints the per-file score tables and the surviving-mutant appendices in the shape the board parses; the findings sections of the baseline report are written by hand | same script, same output |

Neither target is part of `make verify`. Two tool-level facts that shaped the configuration:
mutmut's default forks every worker from a process that already imported the suite, and
`tests/conftest.py` binds **one** SQLite file per process at import — so the config uses
`process_isolation = "forkserver"` with `forkserver_warmup = "none"` (each worker imports the
suite afresh, ≈ 4 s) rather than share one ledger between 24 workers; and Stryker forces vitest's
`threads` pool, where a test that sets `process.env.TZ` at runtime is not honoured (N16-obs61).
`backend/mutants/`, `frontend/.stryker-tmp/` and `frontend/reports/` are git-ignored; ruff,
eslint and vitest exclude them so a finished or aborted run cannot fail the gate.

**Reading the score.** The target in the plan is **≥ 80 %** for the validation layer and the
renderers. A survivor is a FINDING of class TEST when it reveals a behaviour the contract names and
no test pins (a boundary, a refusal, a mask); it is **noise** when it changes something no contract
cares about (a log message, a cache key, an equivalent spelling) — the baseline report groups the
first kind into register rows and leaves the second kind in the appendix. Do not kill mutants by
asserting the implementation line by line: write the test the contract clause asks for, and the
mutants under it die as a side effect.

#### 6.2.2 Discovery KPIs on the board (`make metrics` §7)

The registers already carry the finding; round 16 adds optional **markers**, written anywhere in
a row (by convention in the id cell, after the author / date), so the board can compute the plan's
KPIs without a second ledger:

| marker | meaning | KPI |
|---|---|---|
| `class: PRODUCT` / `TEST` / `ENV` | the failure class (vocabulary of `tests/e2e/README.md`); without the marker the first upper-case `PRODUCT` / `TEST` / `ENV` word of the row counts, else *unclassified* | findings per round by class |
| severity cell (`P1`/`High`, `P2`/`Medium`, `P3`/`Low`; `Critical` weighs as P1) | — | **weighted score** = Σ 5 / 3 / 1 |
| `found-by: user` | the finding reached the round from outside its channels — an **escaped** finding (legacy spellings also count: `user report`, the word `USER`, `逃逸`, `WB-` ids) | escaped count and ratio (plan: ≤ 10 %) |
| `channel: A` / `B` / `C` (`A+B` when both saw it) | discovery channel: A automation, B exploratory session, C collaborator acceptance | per-channel counts; **capture–recapture** |
| `repro-hash: <8 hex>` | `sha1` of the lower-cased, whitespace-collapsed minimal repro, first 8 hex — `python3 scripts/quality_metrics.py --repro-hash "<repro>"`; two ids with the same hash are **one defect seen by two channels** | the A∩B overlap `m` |
| `session: <id>/<hours>h` | on a channel-B row: the exploratory session it came from; hours count once per session id | **exploratory yield** = B findings ÷ Σ session hours (plan: ≥ 3/h early, stop at < 1/h) |
| `module: Enn` | the module of the plan's §1 table | per-module counts in the snapshot JSON |
| `kind: process` / `kind: ruling` | a bookkeeping row (this baseline, a deploy row, a decision) — excluded from the discovery counts | — |

**Capture–recapture.** With `nA` findings from channel A, `nB` from channel B and `m` seen by both
(the same id tagged `A+B`, or two ids sharing a `repro-hash`), the Chapman estimate of the total
defect population is `(nA+1)(nB+1)/(m+1) − 1`; **estimated remaining** = estimate − distinct found
`(nA + nB − m)`. The plan's stop rule is remaining < 2 per deep-dive path. Both stay blank until a
register carries both channels (every register before round 16 is *untagged*). The KPIs land in
the history line as `discovery_*` and `mutation_score_{backend,frontend}` (the latter parsed from
the latest `docs/cn/reports/mutation-baseline-*.md`, whose per-file tables the board reads).

### 6.3 Creation matrix & fail-early guard — `backend/tests/test_creation_matrix.py`, `backend/tests/test_fail_early_guard.py`

Round 16, channel A (design `plans/CAPABILITY-MATRIX-DESIGN.md`). Trigger: N15-obs16 —
`office-agent` selected the `browser` builtin on a Harness, `POST /api/agents` answered 202,
the deploy job died at its first stage with `HarnessRenderError`. The class of bug is "the
console / API lets a member select a combination the method's generate stage cannot render";
these two suites make it red in `make verify` instead of after the user clicks publish.

#### 6.3.1 The declaration (one source of truth)

`deployer.pipeline.MethodCapabilities` carries, next to the artifact flags, the method's
**creation-time option domain**: `builtin_tools`, `tool_types` (ToolRef types rendered
without auth), `tool_auth_modes` (acting modes of an authenticated rest / mcp tool; empty =
no identity channel), `native_tools`, `protocols`, `memory`, `knowledge_bases`, `byoc_kinds`,
`requires` (workspace resource keys needed from the first stage; a Gateway tool additionally
needs `gateway_id`, a `container_source` artifact `codebuild_project`). Each method declares it at its `register_method` call
(`harness_local.py`, `zip_runtime.py` ×2, `byoc.py`). Three consumers read it and nothing
else:

| consumer | where |
|---|---|
| creation-time validation | `schemas.agent.validate_against_method(spec, caps, workspace)` through `services/method_validation.require_method_support` — called by `POST /api/agents`, `routers.agents._start_redeploy` (every re-publish) and `assistant.service.approve_proposal`, before any row / job / AWS call. 422 `agent.method_unsupported {method, field, value, supported}`; 409 `workspace.resource_missing {resource, how_to_fix}`; with the request's `db`, `require_known_references` adds 422 `agent.reference_missing {field, value, kind, how_to_fix}` for a `knowledge_bases[].kb_id` no ledger row of the workspace has (N16-obs73 — the one ledger lookup of the validator). `agent.builtin_unsupported` (cn/r15-h) is an alias kept one round: a `tools.builtin` refusal carries it as `detail.legacy_code`. |
| publication | `GET /api/capabilities.methods[<method>]` = `MethodCapabilities.as_dict()`; `GET /api/tools` builtin rows' `methods` are derived from it (`routers.tools.builtin_methods`) |
| the wizard | `lib/capabilities.ts` (`useMethodCapabilities`) → `lib/agent-spec.ts optionOffered / flagOffered / builtinOffered`; `MethodSections.tsx` **disables** (never hides) a builtin / native tool / protocol / byoc artifact kind the current method does not declare, with the reason as the tooltip; a null declaration (older server, outage) offers everything and the server's 422 decides |

`tests/test_method_capabilities.py` pins `BUILTIN_RUNTIME_TOOLS.keys() == caps("harness").
builtin_tools`, the schema constants (`KB_METHODS`, `TOOL_AUTH_METHODS`, `A2A_CONSUMER_METHODS`,
`SUPPORTED_ACTING_MODES`) to the declarations, and freezes the wire form in
`tests/fixtures/method_capabilities.json`; `frontend/src/lib/method-capabilities.test.ts`
reads that file and pins `BUILTIN_TOOLS`, `SUPPORTED_ACTING_MODES`, `HARNESS_NATIVE_TOOLS`,
`BYOC_KINDS` and the A2A protocol to it. A declaration change re-freezes the fixture
(`UPDATE_FIXTURES=1`) and tells the frontend which constant must follow.

#### 6.3.2 The creation matrix

Dimensions: method (4) × builtin (none / code-interpreter / browser) × tool_type (none /
gateway / mcp / mcp_auth / rest / a2a) × protocol (http / a2a) × memory (on / off) × kb (0 / 1)
× native (none / shell) × byoc_kind (code_zip / container_source / container_image; byoc only);
builtin also carries an unknown name (N16-obs80) and kb a missing id (N16-obs73).
Coverage: a greedy **pairwise** covering array (in-file generator, no dependency) plus every
**single-factor** cell on each method's baseline, plus two degraded workspaces on the
baselines (`ws=no_bucket`, `ws=no_gateway`) — 93 cells, ~20 s on 8 workers. Hermetic: deploy
start stubbed, the Harness renders the vendored fixture template, no AWS client is built.

Per cell exactly one of: (a) `POST /api/agents` → 202 **and** the method's generate stage
dry-runs clean — `harness`: `render_harness_bundle`; `zip_runtime` / `studio`:
`_generate_code` + `_method_requirements`; `byoc`: `_config` + the static half of
`describe_image` — **and** the rendered artifact mentions every tool the spec mounts (an
unauthenticated mcp URL, `ask_<peer>`, the builtin's callable: a silently dropped tool is as
much a creation-time lie as a render error), **and** the package stage's static precondition
(an artifacts bucket) holds; or (b) a 4xx with no agent row and no job — when the code is
`agent.method_unsupported`, `detail.supported` must equal the declaration. Anything else
(202 + dry-run error, a 5xx) fails the test naming the cell. `KNOWN_RED` lists a red cell
as `xfail(strict=True, reason="N16-obsN")` while its bug is tracked — a fix must remove the
row. `CREATION_MATRIX_REPORT=<file>` appends one JSON line per cell for a register.

Run on the unmodified code (8641897c) the final matrix is red in **18 of 88 cells** (the
`kb=missing` value excepted — a missing ledger row is invisible to a generate dry-run and was
found live, N16-obs73) — N16-obs1..5 and the unknown-builtin cells of N16-obs80 in
`docs/cn/reports/bugs-round-16.md`: harness × browser (N15-obs16), zip_runtime / studio ×
builtin incl. an unknown name (silently dropped), zip_runtime / studio × unauthenticated mcp
(silently dropped), a gateway tool on a workspace without a Gateway, any method without an
artifacts bucket. **0 of 93** are red with the validator (30 deployable, 39 schema-refused, 12
`agent.method_unsupported`, 7 `workspace.resource_missing`, 5 `agent.reference_missing`); the
cherry-picked cn/r15-h turned harness × browser green on its own (commit 64d50824).

#### 6.3.3 The fail-early guard

`CASES` drives each **moved** condition of design §0.3 through `POST /api/agents` — an unknown
builtin, a builtin on zip_runtime / studio, a Harness mcp with a config-level auth marker,
an unauthenticated mcp on zip_runtime, an mcp without an http(s) URL, a Gateway tool on a
workspace without `gateway_id` (harness and zip_runtime), a `container_source` artifact on a
workspace without `codebuild_project`, byoc without its block, a public registry image, an image
in another account / region, no artifacts bucket (all four methods), a `__LAUNCHPAD_` substring
in member text, the `obo` acting mode — and asserts the declared code with **zero agent rows and
zero jobs**; one case pins that the re-publish route shares the validator and leaves the
stored spec untouched.

The **AST reverse guard** parses every `app/deployer/*.py`, collects each
`raise HarnessRenderError(` / `raise RuntimeError(` and requires it to be listed in
`tests/fail_early_registry.py` — keyed by file + the constant prefix of the message (f-string
placeholders dropped, so line shifts never break it; a message edit does, on purpose) — as
`moved` (with the creation-time code) or `late` (with the reason the request alone cannot
decide it: a package index, the uploaded artifact in S3, ECR, a build step, an internal
assertion). An unlisted site fails naming `file:line` and the message; a stale row fails too.
Nobody can add a new input-only refusal to a stage without classifying it. The byoc
entrypoint / Dockerfile checks are classified **late** (they need the artifact; the upload
response already carries `detected.entrypoint_candidates` / `detected.has_dockerfile` for the
console) — design §0.3 proposed moving them to the upload route, which does not know the
artifact kind yet; left for the lead to schedule.

### 6.4 Property and model tests — auth, workspaces, model roles (`backend/tests/property/`, r16-c)

Round 16 channel A for E18 / E19 / E21 (`plans/BUG-DISCOVERY-PLAN.md`). Hypothesis (a dev
dependency through schemathesis) drives contract-derived oracles against the real services and
routes; every file is marked `property` and runs inside the ordinary backend pytest step (≈ 40 s
wall on 8 workers; the three state machines are the long poles at 15–20 s each). A failing
generated example is a FINDING, registered in `docs/cn/reports/bugs-round-16.md` and kept as
`xfail(strict=True, reason="N16-obsN")` so the gate stays green and a fix is detected the day it
lands. Budgets are capped (≤ 200 examples per property, 12–25 examples × 25–30 steps per machine,
`deadline=None`).

| file | what is modelled | oracle | findings |
|---|---|---|---|
| `test_auth_properties.py` | `validate_password` × policy mode × minimum 6..16 × random username / email / password | the §9 text re-stated as a function (length first, then identity, then the compact common list); the advertised `password_policy(settings)` payload; `generate_password` round-trip; monotonicity in the minimum; relaxed ⊇ strict | N16-obs20 (fixed); the API differential (`POST /api/auth/register` verdict vs the `password_policy` the same server advertises, strict-12 / relaxed-8 / relaxed-6) holds for non-empty passwords; the empty password is N16-obs26 (strict xfail) |
| `test_login_guard_model.py` | `RuleBasedStateMachine` over the REAL `POST /api/auth/login` with an injected clock (`services.login_guard.time` replaced): wrong / right password, config-admin wrong / right, a second peer, time advance, admin password change, admin unlock, backend restart (new `LoginGuard`) | `AccountModel` / `PeerModel` written from §9 (threshold → 60 s, doubling, 1800 cap, 2 × max streak window; peer window consulted after a failed check only); every 429 body + `Retry-After` header vs the clock; `guard.state` and the Users list agree; v3 cookies flip on the session epoch | none — the lockout contract holds under the reachable limits (threshold 3 / peer limit 4, set through the documented settings) AND under the §9 numbers themselves (`contract_world`: 5 → 60 … 1800, 20 / 300: the full ladder through the route, a tripped peer window never refusing a correct password); a second machine adds clock steps backwards and jumps forwards — no deviation; the three behaviours §9 leaves unsaid are registered as N16-obs25 |
| `test_admin_guard_model.py` | `RuleBasedStateMachine` over 2–4 administrator rows + a member: role / status / expiry / `extend_days` patches and deletes through `PATCH` / `DELETE /api/users/{id}` (config admin) and `delete_user(actor_user_id=…)` (an admin row) | a `Row` model; a step is refused (409 `users.last_admin` / `users.self_delete`) exactly when the model says the active-admin set would become empty; refused rows unchanged; `active_admin_ids` == the model after every step | N16-obs21 (the concurrent corner — fixed: a serialised guard section; the barrier test is now a plain pin) |
| `test_visibility_switch.py` | 3 reader threads list / read agents as a member while a 4th flips the default workspace shared ⇄ isolated 60 times; a hidden id vs an unknown id; a random sequence of flips and role changes watched by three viewers | every page ∈ {shared set, isolated set} (never mixed); the by-id 404 body and headers equal the unknown-id 404; the §7 rule (NULL / own / a CURRENT admin's) as the list oracle | none |
| `test_model_roles_matrix.py` | 7 roles × 9 model states (enabled, no tools, disabled row, disabled provider, deleted, embedding, empty, whitespace, unknown) × process events (`cache_clear`, racing sibling build, cold snapshot + `load_overrides`); the referrer rule for model and provider; the precedence differential console > env > yaml > default per role (yaml through a repointed `config.CONFIG_FILE`) | §12.3 rule 1 as `expected(role, state)` (422 reasons exactly; nothing refused reaches the ledger / journal / Settings); 409 `model.in_use` / `model_provider.in_use` naming the role; `effective()` rows after every layer change | N16-obs22 and N16-obs24 (fixed per lead ruling; plain pins incl. the `default_disabled` flag and the 422 reset refusal), N16-obs23 (strict xfail, open) |

Conventions: test accounts lower `users._ITERATIONS` to 1000 so a sign-in costs milliseconds
(the guard does not depend on the KDF cost); machines swap `login_guard.guard` for a fresh
instance per example and wipe the `users` table in their constructor, since Hypothesis examples
share one test's fixtures; `HealthCheck.function_scoped_fixture` is suppressed only where a
`@given` test deliberately reuses one app across examples and its oracle reads the live state.
Adding a property: write the oracle from the contract page, never from the code; cap the budget;
when it fails on the current code, shrink, register the row, then `xfail(strict)` it.

### 6.5 Property and model tests — evaluation (`backend/tests/property/`, round 16)

Round 16's channel A for the evaluation family (`plans/BUG-DISCOVERY-PLAN.md` §E09–E14). The
goal is **finding bugs, not green tests**: Hypothesis generates the inputs, an independent
oracle or a reference model says what the contract promises, and every falsifying example the
current code produces is a FINDING in `docs/cn/reports/bugs-round-16.md` (N16-obs10..19) pinned
by `pytest.mark.xfail(strict=True, reason="N16-obsN")` (or vitest `it.fails`) until it is fixed
— so the gate stays green and a silent fix turns into a strict XPASS that must be acknowledged.
Hermetic (C8): the run queue, the executor, Logs Insights and the cloud dataset store are the
existing seams / fakes of `tests/evaluation`; no AWS.

#### 6.5.1 Modules

| module | module / subsystem | what is generated | invariants (the contract) |
|---|---|---|---|
| `test_scorecard_properties.py` | E11 — `evaluation/scorecard.py`, `GET /scorecard`, `GET /runs/compare`, `POST /runs/{id}/rerun` | snapshot timelines (deployed / not, deployment ends) × runs (stamp / none, scope, creation time); scenario maps; isolated **worlds** (datasets / agents owned by ada / bob / an admin / nobody, runs mixing them, every scope, status and version incl. the unrecorded one); rerun sources (dataset / window / persona, evaluators incl. `[]` and an unknown id, names, descriptions) | `resolve_snapshots` == a six-line reference model of §23's rules; `build_cases` places every scenario once and a side is null only when that run lacks it; **hidden names appear nowhere** in a member's scorecard / list / by-id / compare (string search), the administrator keeps every stored name; **rerun == create**: same status, same error code, same payload modulo `{id, created_at, updated_at, queue_position, name}`, same ledger stamps, the caller as creator; the re-run name suffix is idempotent |
| `scorecard.property.test.ts` (frontend) | E11 — `v2/scorecard.ts` (the console owns the composite / regression math) | seeded PRNG (mulberry32; `SEED`, `PROPERTY_EXAMPLES`, default 2000) scorecards: 1–8 runs, random versions / statuses / evaluator sets / 0.01-grid scores, missing judge scores | `fell` / `rose` / `isRegression` fire at exactly −0.05 (composite) / −0.1 (evaluator) and never for less (float slack); a same-version re-run is noise (`sameVersion`, never `regression`); the headline signal is `act` ⇔ the rule; the version matrix lists each version once, newest first, every scored run counted; composite ∈ [0, 1] or null |
| `test_pipeline_state_machine.py` | E09 — `evaluation/pipeline_routers.py`, `POST /datasets/upload` | a `RuleBasedStateMachine`: create (by name / existing dataset, random source filter) · start · finish (the captured thread body) · run again · **restart** (cleared `_live_runs` + the startup sweep) · **reap** (a running row whose thread is gone) · update · delete · vary the session listing (subset / failing); random JSONL (prompt / scenario / persona items with arbitrary Unicode, blank lines, non-object lines, broken JSON, duplicate scenario ids) + explicit boundaries (U+2028 / NEL / FS inside a string, CRLF, 201 items, a 1.1 MB line, 100 000-deep nesting, invalid UTF-8) | status moves only `idle|succeeded|failed → running → succeeded|failed`; a previous outcome stands until the run ends; **one output dataset per pipeline** and every later run appends to it; the dataset stays valid (≤ 200 items, unique scenario ids); `added ≤ matched ≤ scanned`; a failed run names its error; `running` rows reaped to `failed` with `PIPELINE_RESTART_ERROR`; the upload is a precise 4xx or a 201 whose `item_count` equals the valid lines of an independent oracle — never a 500 |
| `test_online_fault_matrix.py` | E12 — `strands_eval/jobs_online.run` (the online worker handler) | per evaluator a homogeneous judge-fault queue (mid-stream 5xx `EventStreamError`, 500 `ClientError`, botocore read timeout, throttle, non-transient `ValueError`) × count 0–4 × a cancel at a random second of the simulated clock × the transient back-off | the handler returns and the waits are bounded by the schedules; `done.json` is `COMPLETED` or `STOPPED` — `STOPPED` ⇔ the cancel marker was written; ≤ 1 record per (session, evaluator), exactly 1 on `COMPLETED`, unique ids, `result_count` = records; one session settles each evaluator per §17 (< 3 transient → scored, ≥ 3 → one error record typed as the cause; ≤ 3 throttles → scored, 4 → error; non-transient → one attempt); a cancel is honoured within one slice |
| `test_evaluator_role_matrix.py` | E10 — `services/model_roles.py` × `model_registry.update_model`, `POST/PUT /evaluators` | random role → model assignments over four rows (tool-capable / no tools / disabled / the built-in Kimi) + the two full-matrix examples; 200-char descriptions of arbitrary Unicode | each `PUT /api/settings/model-roles/{role}` answers as the role's rules say (`empty` / `disabled` / `no_tool_use` / 200); disabling a model is 409 `model.in_use` listing EXACTLY the roles whose effective value it is (Kimi carries `skill_lab_target`), 200 otherwise; GLM answers `model.is_default` first; 200 chars accepted, 201 refused (create and update); a run refuses a judge whose model was disabled since |
| `test_insights_window_properties.py` | E14 — `POST /runs {mode: insights, lookback_hours}` | hours 1…336 with zero / some sessions; 0, −1, 337, 720 | zero sessions → 409 `run.no_sessions` naming the hours, one discovery over exactly the window, no row; with sessions → 201, `window:<N>h`, ids pinned and never re-read; out of bound → 422 without a query; an evaluators-mode window is not resolved at submit |

#### 6.5.2 Conventions

* **Example counts** scale with `LAUNCHPAD_PROPERTY_SCALE` (default 1; the directory is ≈ 66 s
  wall under `-n auto`, ≈ 3 min serial — a longest single test of ≈ 15 s is the budget). The round's stop rules ("1000
  state-machine steps, 0 violations"; "2000 scorecard examples") are met at scale 7 and by the
  vitest default respectively; the register row N16-obs19 records the counts that were run.
* **HTTP properties clean up per example** (`_wipe()`): the autouse table wipe of
  `conftest.py` runs once per TEST, not per Hypothesis example, and the auth-enabled worlds keep
  their members (`tests/test_visibility` fixtures) across examples. Hypothesis'
  `function_scoped_fixture` health check is suppressed on purpose for these.
* **Every invariant cites its contract line** in a comment or docstring (`evaluation-engine.md`
  §17 / §21a / §23, `users-and-ownership.md` §7 / §8, `model-registry.md` §9 / §12, the `api.md`
  route rows) — a number is never copied from `scorecard.ts`, `compare.ts` or the Python. Where
  no contract line exists the invariant is a **differential** (the real `v2/scorecard.ts`
  bundled with the repo's esbuild and run under node against the backend rule on one grid;
  rerun vs new run) and the missing line is flagged in the register (round 16: N16-obs19p
  lists five gaps).
* **Prefer differential / round-trip / metamorphic oracles** over expected values: renaming a
  dataset changes `benchmark.label` and nothing else; the page order and the other runs never
  change a run's version; permuting a run's sessions never changes the case pairing; a
  reference model written from the doc (`_reference` for §23, `_oracle` for §21a); the request
  body itself (rerun vs create). A property that re-derives the implementation's formula is
  not a property.
* **Fault bodies are real shapes.** Copy the fields the fakes emit from a recorded incident —
  `tests/faults/conftest.py` (N-ARCH-obs18), the registers (N12-obs53 / 55 texts), the
  deploy-ops-6 seam recordings under `~/designs/seams-20261019/` (cite the fixture file; copy
  only the fields, keep them redacted) — never an invented `RuntimeError("boom")`.
* **A falsifying example is a finding, never a reason to loosen the property.** Keep the
  shrunk example as an explicit `@example`, mark the test `xfail(strict=True,
  reason="N16-obsN")`, register the row; fix in-branch only when the fix is ≤ 30 lines and
  local (round 16: N16-obs13 / 14 / 16 in `routers.upload_dataset`). A test marked for a fixed
  finding keeps the id in its docstring.

### 6.6 Consistency layer — `backend/tests/consistency/` + `backend/tests/property/` (round 16, r16-g)

The round-16 plan (§4) keeps only *consistency assertions* for the lower-risk modules:
"UI option sets == interface capability declarations; navigation == permission matrix". They
live in the backend suite (hermetic, part of `make verify`'s backend pytest step) and read the
frontend **as text** — there is no TypeScript runtime in the Python suite, so `nav.ts`,
`lib/capabilities.ts` and every `export const X = [ … ]` under `frontend/src/lib/*.ts` are parsed
with regexes while the backend twin is imported. Every assertion is a differential between two
sources the contracts say must agree; none copies a value out of either.

#### 6.6.1 `tests/consistency/test_surface_consistency.py`

| section | left side | right side | contract |
|---|---|---|---|
| (a) navigation | `v2/nav.ts` entries (`to`, `feature`, `admin`) | `core/features.FEATURES`, `core/route_policy.ROUTE_POLICY` (per-page API prefix table `NAV_API_PREFIXES` in the test), `lib/capabilities.ts::PATH_FEATURE`, `App.tsx` routes, `GET /api/capabilities` with each feature off, the features.md backend-boundary table (404 unmounted / 503 `feature.unavailable`) | features.md, E01 AC-01, route_policy.py docstring |
| (b) option lists | `INVENTORY`: every exported string array under `lib/*.ts` with its relation (`==`, `==list`, `<=`) to a named server set — or `None` = UI-only vocabulary, listed so the review sees it | `_server_sets()` imports: `harness_tool_access`, `routers/tools.BUILTIN_TOOLS`, `schemas/agent` Literals / model fields / `allowed_model_ids()`, `assistant/proposal.ProposalContent`, `services/governance`, `skill_lab/jobs`, `evaluation/online_evaluators`, `strands_eval/evaluators.LEVELS`, `workspace_bootstrap.STAGE_ORDER`, `opensearch_http.MERGE_SETTINGS_*`, `registry_local`, `users.AGENT_PERMISSIONS` | the frontend comments naming their twin ("mirrors backend … exactly") |
| (c) error codes | `apiErrors.*` locale keys | the backend error-code registry: every `AppError(` / `NotFoundError(` / `envelope(` literal, f-string codes as patterns, prefixing helpers (`_error("suffix")` → `prefix.suffix`), `AWS_ERROR_MAP`, `OPENSEARCH_ERROR_MESSAGES` | core/errors.py, `lib/api.ts::localizedMessage` |
| (d) locale usage | static `t("…")` keys (plural-aware: `k` resolves through `k_one` / `k_other`), dynamic prefixes, data literals | `locales/{en,zh-CN}/common.json` | `scripts/i18n_check.py --strict` |

Adding a hard-coded list to `frontend/src/lib/*.ts` fails
`test_the_inventory_covers_every_exported_string_array_in_lib` until it is classified in
`INVENTORY`; a list that drifts from its twin fails its matrix cell. `KNOWN_DRIFT` holds a
drifted cell as a register row (strict-xfail semantics, like the permission matrix's
`DISAGREEMENTS`): empty since N16-obs50 was fixed in the same branch.

#### 6.6.2 `tests/property/` (Hypothesis, module properties)

| file | module | oracle | depth |
|---|---|---|---|
| `test_workbench_matrix.py` | E01 | the §7 visibility rule recomputed from the drawn ledger state, and `GET /api/agents` / `GET /api/eval/runs` as the same viewer | 40 ledger states × 4 viewers × 2 modes |
| `test_trace_properties.py` | E05 | the raw span list (no loss / no duplication / nesting / depth / stable sibling order); real `ClientError` codes and `GetQueryResults` statuses → `502 observability.query_failed`, the Chat rail's `unavailable_reason`; malformed rows → never a bare 500 | 300 forests, 40 API trees, 6 start faults × 5 routes, 3 terminal statuses, deadline, 10 row shapes |
| `test_announcement_lifecycle.py` | E20 | a `RuleBasedStateMachine` model (revision, draft, snapshot, publish time) under an injected `announcements.datetime`; both projections recomputed after every step; 8 racing writers | 25 runs × 20 steps |
| `test_video_routing_matrix.py` | E22 (API half) | `ROUTE_POLICY` for roles × every `/api/videos*` route; a row model for publish / withdraw / republish sequences against the member catalog; the request body for chapter round trips | 16 routes × 3 roles, 40 sequences, 60 chapter lists + boundary table |
| `frontend/src/v2/pages/videos/routing-matrix.test.tsx` | E22 (page half) | `legacyVideoManagementToV2` as the landing oracle for 2 roles × 8 entry URLs; the flag-off shell; `useVideoPlayback` seek clamping at 0 / D / D+1 / NaN | vitest |

Findings from this layer are rows `N16-obs50..57` in `docs/cn/reports/bugs-round-16.md`;
the ones still open are `xfail(strict=True)` cells that fail the gate the day the product
catches up, so the register row is removed in the same commit.

### 6.7 Lifecycle graphs — prime paths, edge pairs and the loops (`backend/tests/graph/`, r16-o)

Round 16's GRAPH + LOOP layer (BUG-DISCOVERY-PLAN §0 / §4, "state machine" and "persistence
invariant" rows of the six-layer stack, on EVERY module). Where §6.1 lets Hypothesis walk a
model at random, this layer enumerates the walk: one **lifecycle graph per entity**, every
edge **citing the contract sentence** it enforces, and a generator that turns the graph into
the finite set of paths a tester would want to see run on every build.

**The graphs** (`tests/graph/graphs/*.yaml`, loaded and validated by `tests/graph/paths.py`):

| graph | module | nodes → edges | what the edges are |
|---|---|---|---|
| `agent` | E03 | absent · deploying · active · failed · redeploying · redeploying_cold · deleted — 28 | create (202 / 422 `name_reserved` / 409 `name_exists`), the job finishing (active / failed / cancelled when the row was marked deleted mid-job), redeploy (202; refused 409 `deploy_in_progress` while a job is live), the failed update-mode job that keeps the live version, delete (200 / 409 / 404 / 403 for another member), chat on every state |
| `chat_session` | E04 | none · open · open_v1 · live_turn · ended — 22 | a gated live turn, 409 `turn_in_progress`, reattach (replay == the POST's frames), the `/v1` entrance on the same session (differential), END SESSION, 409 `session_ended`, every by-id route for another member (404) and the administrator (409 `turn_not_yours`) |
| `eval_run` | E11 | absent · queued · completed · failed · stopped · deleted — 21 | create (201 / 400 `agent.not_active` / 422 `no_evaluators`), the held queue dispatching into the hermetic worker (completed / failed), stop (202 queued / 409 terminal), delete (200 failed·stopped / 409 completed / 404), recheck, rerun (differential against `POST /runs`), compare (200 scenario-aligned / 422 with a queued side), the dataset's in-use guard before and after the run finished |
| `runtime_canary` | E13 | absent · fresh · ready · traffic_sent · verdict_recorded · completed · rolled_back · cleaned — 24 | the ramp (setup → traffic → verdict → advance → … → complete at 1/99), every `stage_not_ready` refusal, 422 `dataset_required`, 403 `canary.forbidden` for another member (verdict open to all), rollback as the safety valve, idempotent cleanup, create refusals (400 `agent_not_active`, 409 `already_running`) |
| `architect_proposal` | E02 | no_proposal · draft · invalid · rejected · approved · approved_newer · deployed · cleared — 30 | turns (prose / valid / invalid block), member edits (valid → new draft, invalid, repair; differential against `proposal.validate`), reject (200 / 409 stale / 409 already_approved), approve (202 / 200 idempotent / 409 stale · not_approvable · already_approved), `resources_locked`, CLEAR (200 inline / 409 `conversation_busy` / 202 job → Agent deleted, rows 0) and the 404s afterwards |
| `memory_record` | E06 | absent · active · superseded · soft_deleted · consolidated · gone — 14 | the extractor's `add` / duplicate / `update` / `supersede` / `delete` (≥ 0.6 soft, < 0.6 noop) / consolidation over the cap, `list_records` hiding inactive rows, the console delete (200 / 404 `memory.not_found` / 403 `record_forbidden`) |
| `reference` | E-all | wired · agent_gone · run_done · canary_done — 16 | every cross-entity guard: KB / memory / dataset / evaluator deletes while an agent, run or canary points at them (409 `*.in_use` / `has_attached_agents`, 403 for a member's `force`), the same deletes once the referrer is gone or finished, and what the deleted agent's id answers on chat / run create / canary create |
| `workspace` | E19 | absent · registered · probed · s_validate … s_policy (one node per stage, the job interrupted before the next) · ready · failed_early · failed_mid · detached · purged — 39 | register (201), the validate-only probe (202 → job succeeded, status handed back), the full job one stage per edge through the real `execute_bootstrap_job` (interrupted / resumed between every pair), the N17-obs112 denial at `validate-access` and a foreign-bucket failure at `storage`, the resume from both (carried-forward stages), detach (200 / 409 `in_use`), purge (200 with `resource_keys` / 409 `purge_refused`), 409 `bootstrap_conflict`, 422 `bootstrap_stages_unsupported`, 409 `workspace.not_ready` writes, 403 for a member, 404 after removal (r17-ws, §6.10) |
| `policy_principal` | E07 | absent · engine_created · policy_published · attached_a · attached_ab · decided · audited · a_unguarded — 13 | one policy ENGINE attached to two gateways — the Global AgentCore Policy model of contract amendment 11 (pg-contracts, 2026-10-28; the earlier r17-bl shape walked the workspace switch / stale carrier / apply, all removed): the administrator creates an engine (201; a member's 403 and a duplicate name's 409 `policy_engine.name_taken` as refusals), publishes a policy into it (201 draft `log_only` → publish → promote; the engine's ONE Dogwood set installed on every fake shard under the ENGINE id as `<engine>-<n>`, no bundle written yet), attaches it to gateway A in `ENFORCE` (200; the `/3` bundle written BEFORE the Lambda attach; `enforce` spelling → 422 `gateway.policy_engine_mode_invalid` rewrites nothing) and to gateway B in `LOG_ONLY` (two bundles naming the same set, differing only in gateway and mode), deleting the attached engine → 409 `policy_engine.attached`; the interceptor Lambda in-process over each bundle decides the SAME denied call — refused through A (I8 `-32002`, line `mode: ENFORCE`, `gateway_id: A`) and passed through B with a `would_deny` line (`LOG_ONLY`, `gateway_id: B`); the hub ingests both lines (`gateway_id` / `engine_id` per row, `ingested_at`) and `GET /api/policy-engines/{eid}/decisions?gateway_id=` filters per gateway; detaching A (200) rewrites bundle A with `attachment: null`, detaches the Lambda, leaves B and the rows alone, and a call through A then passes without a line (unguarded). Every edge cites an amendment-11 sentence; the paths are strict-xfail "pg-backend pending" until the hub's engine routes land (feature-detected on `app.services.policy_engines`) |

Rules of a graph file: `kind: refusal` edges are self-loops (the executor asserts they wrote
NOTHING — a per-table sha256 over the sorted rows' full column tuples of 19 ledger tables, plus
the world's own stores, is compared before and after, so an in-place UPDATE counts as a write);
every edge has `cites: [<contract id>]` resolving to a `{doc, section, quote}` entry whose
`quote` is `…`-separated VERBATIM fragments of the cited document (`test_paths.py` asserts every
fragment appears there after alnum-only normalisation — a paraphrase cannot detect contract
drift), or a `gap:` text with a `register:` id — **an edge the contract does not name is a finding**, never a
threshold copied from code (`validate()` refuses a file that breaks any of this; nodes must
be reachable). The `ui:` block names the V2 page / view and the inventory `data-testid`s that
take the edge, or `null` for an edge that is not a console action.

**The generator** (`tests/graph/paths.py`, `tests/graph/test_paths.py`): **prime paths** =
maximal simple edge paths (no node repeats, a closing cycle allowed; a refusal loop is a prime
path of its own — "reach the state, attempt the refused action"); **edge pairs** = every
`(e1, e2)` with `e1.dst == e2.src`; a **test path** = the BFS shortest prefix from the initial
node (edges in id order, so the output never depends on dict ordering) + the requirement. The
pairs a prime test path already contains as a contiguous pair are not generated again; the
soak twins (`LAUNCHPAD_SOAK=1`) walk every pair. `uv run python -m tests.graph.paths --paths`
prints every path; 154 edges → 202 prime paths and 706 edge pairs across the seven graphs
(701 before `cn/r16-q` turned the reference graph's agent-delete edge into a refusal self-loop).

**The API executor** (`tests/graph/executor.py` + `test_graph_<graph>.py`, in `make verify`,
each file < 60 s under `-n auto`): one **world** per graph on the auth-enabled console (ada the
creator, bob another member, the administrator — `tests/test_visibility`), the AWS seams the
sibling suites already own (the pipeline stages replaced by recording fakes, r16-e's canary
`Seams` and architect `harness`, r16-d's memory `World`, the hermetic eval worker behind a held
queue, the gate-able chat `Producer`). An `edge_<id>` method per edge performs the action
through the real route and asserts the status / code the contract names (`Edge.expect`); after
EVERY edge the shared ledger scan (`ledger_invariants.scan_ledger`, restricted to the rows the
world created) and the memory index scan must be clean. Staging an edge needs (a second
completed run for a compare, an older revision for a stale reject) happens in `World.before`,
before the refusal snapshot — so staging is never mistaken for a write the refused call made.
A path through an edge no world can drive is **skipped** with the reason from `UNDRIVEN` (none
today); a path through a registered finding is pinned `xfail(strict=True)` from `XFAIL`. The
`make graph-coverage` run records the edges / paths walked per worker and writes
`docs/cn/reports/graph-coverage.md` (+ `.json`), which the board reads (§7a,
`graph_edges_covered`, `graph_prime_paths_covered`): 147 / 150 edges and 194 / 197 prime paths
covered on the first run (2026-10-19); after the rev-r16 corrections 150 / 154 and 198 / 202 — the
uncovered edges are exactly the pinned findings. `cn/r16-q` (same day) fixed two of them
(N16-obs111 → the reference edge is a refusal self-loop; N16-obs114) and turned the N16-obs110 /
112 / 113 gap edges into `cites:` edges: 152 / 154 edges, 200 / 202 prime paths; the pinned
findings left were N16-obs88 (`chat_session.v1_send_ended`) and N16-obs117
(`architect_proposal.reject_approved`). `cn/r17-a` (2026-10-19) fixed both and flipped the
pins: **154 / 154 edges, 202 / 202 prime paths, 706 / 706 edge pairs** — no pinned finding is
left, and the only gap rows are N16-obs116's four `runtime_canary` edges (DOC).

**Findings of the first run** (register rows, every path pinned strict): N16-obs110 (DOC,
E03 — four redeploy edges no contract names; architecture.md says a failed job marks the Agent
`failed`, the code keeps an update-mode agent `active` with `last_deploy_failed`), N16-obs111
(**PRODUCT medium**, E13 — `DELETE /api/agents/{id}` answers 200 while a Runtime canary on the
agent is `running`, leaving the canary routing to a deleted Agent), N16-obs112 (DOC, E04 — a
reattach to a `/v1` sync turn answers a bare `done`; neither contract sentence names it),
N16-obs113 (DOC, E06 — api.md's `aws.not_found` / `memory.platform_protected` vs the backend
contract's `memory.not_found` / `memory.default_protected`), N16-obs114 (PRODUCT low, E06 — the
console delete of a superseded record leaves the newer record's `supersedes` dangling),
N16-obs115 (TEST-DEBT — 5 contract-named edges have no inventory control: the agent edit
re-publish submit and the task detail's stop carry no `data-testid`). The per-table digest oracle then found
N16-obs117 (PRODUCT low, E02 — the refused reject of an executed revision still stamps the
conversation's `updated_at`; invisible to a row-count oracle), and the verbatim-quote rule found
N16-obs116 (DOC, E13 — rollback's precondition and the terminal-state `stage_not_ready` answer
have no sentence) and corrected N16-obs113 (api.md documents no memory record route at all). Two modelling lessons
worth keeping: a node must split when the contract's answer differs by history (`redeploying`
vs `redeploying_cold`, `open` vs `open_v1`), and a refused reject of an approved revision is
`proposal_stale` when a later draft exists (the route checks currency first).

**The UI executor** (`tests/e2e/cn/graph/`, NOT in verify): `edge-controls.mjs --check` joins
every edge's `ui.controls` with the r16-m inventory offline (109 of 197 prime paths are walkable
on the console; 45 edges are api-only by design; 5 gap edges = N16-obs115); `--walk --live`
walks the walkable prime paths on a deployed console through the inventory selectors, asserting
the recorded effect and the graph's status per click — it refuses to start without the flag and
the test-host env, and by default runs nothing live (C8). Design in `tests/e2e/cn/graph/README.md`.

**The loops.**

* (a) **bug-neighbourhood re-exploration** — every open register row touching an edge is
  already surrounded: the executor walks every edge pair through that edge (its predecessors
  and successors) on every run, and the soak twin walks the pairs the prime paths subsume.
  When a row is fixed, flip its `XFAIL` entry off (the strict pin fails until then) — the
  neighbourhood keeps running after the fix. First exercised in `cn/r17-a`: N16-obs88 and
  N16-obs117 flipped, then the soak twins of `chat_session` and `architect_proposal` (578
  paths, every edge pair — 9 through `v1_send_ended`, 10 through `reject_approved`) re-walked
  clean, so neither fix moved a neighbouring edge.
* (b) **exploration → path** — the explore-1 session findings are graph edges now, so they stay
  covered: N16-obs70 / 71 → `chat_session.other_member_{history,stop,post,reattach}` and
  `admin_reattach`; N16-obs72 → `chat_session.invalid_session_id`; N16-obs74 → the
  `chat_while_*deploying` refusals and the `redeploying` nodes; N16-obs76 → `agent.delete_*`
  (the 200 report) and `chat_deleted`; N16-obs78 is the schema layer (`tests/test_chat_api`);
  N16-obs79 → `chat_session.send_ended` / `stop_ended` and the `v1_send_ended` pin (obs88);
  N16-obs73 / 77 / 75 are creation-time and docs rows outside a lifecycle (creation matrix,
  §6.3; api.md). A future exploratory finding lands as one YAML edge + one `edge_<id>` method.
* (c) **fix → mutation** — the files a fix touches name the `[tool.mutmut] source_paths` to add
  for the next `make mutation` (no run here): N16-obs111 → `app/routers/agents.py`
  (`teardown_agent_row`) and `app/optimization/canary_service.py`; N16-obs114 →
  `app/services/memory_selfhosted.py` (`delete_record`); N16-obs110's keep-live rule →
  `app/deployer/pipeline.py` (`_keeps_live_version`, `_fail_job`, `_finish`); the chat
  `/v1` seam (obs88 / obs112) → `app/services/chat_ledger.py`, `app/services/chat_turns.py`;
  the obs88 fix (`cn/r17-a`) → `app/services/invoke.py` (`require_session_open`),
  `app/routers/public_api.py` (the stream pre-check); the obs117 fix → `app/assistant/service.py`
  (`reject_proposal`).
  The executors are the tests mutmut should select for those files (`test_graph_agent`,
  `test_graph_reference`, `test_graph_memory_record`, `test_graph_chat_session`).
* (d) **stop rule on the board** — superseded by the user's loop rule of 2026-10-19 (§6.8): the
  loop runs round after round and stops only after THREE consecutive zero-yield rounds, a
  zero-yield round forcing a different method next; `graph_prime_paths_covered` (202 / 202 after
  `cn/r17-a` for the round-16 graphs) and the register's capture–recapture remaining (§6.2.2)
  are reference metrics the board keeps printing, not the stop decision.

### 6.8 Loop engine — random walks, neighbourhood re-exploration, cross-graph walks, soak (`backend/tests/graph/`, r17-b)

Round 17 keeps the GRAPH + LOOP layer of §6.7 *running* instead of green: the enumerated prime
paths and edge pairs cover every edge and every adjacent pair exactly once, so after one round
they stop finding; the loop engine drives the same worlds through histories the enumeration never
builds, and treats every failure as a finding to shrink, register and pin.

**Random walks (`tests/graph/walks.py`, `test_<graph>_walk[seed]`).** A walk of 20–60 edges
draws the next edge uniformly among the edges enabled at the current node — refusal self-loops
included, so a refusal is attempted in the middle of a long history (create → redeploy → delete
→ create-again → …). It is a pure function of `(graph, seed)` (`random.Random(seed)`, edges
sorted by id before the draw), so a failing seed replays byte-identically anywhere. The executor's
per-edge oracles run after EVERY step (`World.step`: the edge method's contract assertion, the
per-table sha256 digest on every refusal, the own-id ledger scan and the world's extra scans). A
walk stops early only at a dead end (a node with no enabled edge; `memory_record.consolidated`).
Edges a world pins `XFAIL` are excluded from the draw — the neighbourhood owns them — so the walk
keeps exploring instead of stopping at a known failure. Budget: 8 seeds per graph in `make verify`
(≈ 40 s of wall for the 56 walks under `-n auto`, the longest single walk 7 s), 200 seeds per
graph under `LAUNCHPAD_SOAK=1`. Each world module exposes a `make_world` factory (a fresh world
per call); `world` is `make_world()`.

**Shrinking before registering.** A failing walk is cut after the failing edge, then every
*cycle* of the prefix (a sub-sequence that leaves and re-enters the same node; a self-loop is the
shortest) is removed — longest first — while the same edge keeps failing with the same exception
type (`walks.Failure.same_as` compares the failing edge id and the exception TYPE only, not the
message: a shrink that turns a 409-mismatch into a digest violation at the same edge is accepted
as "the same failure" — read the final message, not just the label), each attempt on a fresh
world (≤ 40 replays). The result is the minimal edge sequence the
register row carries; its `repro-hash:` is the board's convention (`scripts/quality_metrics.py
--repro-hash "<label>"` ≡ `walks.repro_hash`). A shrunk sequence whose hash is in `walks.KNOWN`
is a registered finding and xfails at runtime; the dedicated strict pin lives next to the graph's
walk test; everything else fails with the register-ready text (walk id, length, minimal label,
hash, the oracle's message). `walks.FINDINGS` / `crossgraph.FINDINGS` are the append-only ledgers
of the rows the engine opened; the loop-engine table counts them per graph.

**Neighbourhood re-exploration (`tests/graph/neighbourhood.py`, `test_<graph>_neighbourhood`).**
Loop (a) of §6.7, formalised. A pinned edge is one a register row touches — the YAML `register:`
ids (contract gaps: N16-obs116 on six canary edges), the world's `XFAIL` table (a reproduced
finding, strict xfail) and the world's `FIXED` table (`{register id: [edge ids]}`): **a flipped pin
stays in the neighbourhood set as FIXED** — N16-obs88 `chat_session.v1_send_ended` and N16-obs117
`architect_proposal.reject_approved` moved from `XFAIL` to `FIXED` with `cn/r17-a`, so their
neighbourhoods (158 / 145 paths) keep running green instead of vanishing with the fix; N17-obs13
`complete_final` joins `FIXED` in the cross module when `cn/r17-c` lands.
Around every pinned edge `p` three requirement kinds are generated, each prefixed from the initial
node and run as its own test id `<graph>/<register>/<kind><n>`: every edge pair whose two edges lie
within line-graph distance 2 of `p` (edges are adjacent when one ends where the other starts;
distance = hops over that adjacency), every connected 3-edge **window** through `p` (`a>p>b`,
`p>a>b`, `a>b>p`) and every prime path containing `p`. Paths through an `XFAIL` edge are
`xfail(strict=True)`, so the moment a fix flips the pin the whole neighbourhood runs green or
fails loudly. Two requirements that prefix to the same test path run once (the prime path or the
window claims it). Only the **windows** run in `make verify`
(`test_<graph>_neighbourhood`, `neighbourhood.VERIFY_KINDS`): the prime paths through a pinned /
FIXED edge are the prime shards' own tests already (rev-r17 trim, `cn/r17-d`: ~240 duplicates
left `test_graph_runtime_canary_loops.py`), and the distance-2 **pairs are re-runs of enumerated
paths**; primes and pairs run **under `LAUNCHPAD_SOAK=1` only** (`test_soak_<graph>_neighbourhood_pairs`; rev-r17: the canary's
obs116 pairs alone were ~200 re-runs per gate). **Per-file budget (< 60 s alone under `-n auto`):**
the unrolled canary graph spreads over three files — `test_graph_runtime_canary.py` (prime paths,
shard 0 of 2: `executor.params_for(…, shard=(0, 2))`), `test_graph_runtime_canary_primes_b.py`
(shard 1) and `test_graph_runtime_canary_loops.py` (edge pairs, walks, neighbourhood); measured
alone on the 32-core host: 34 s / 32 s / 41 s, the other graph files 22–36 s, the cross module
23 s. `neighbourhood.summarize` reports what the
enumerated set (prime paths + every edge pair) had NOT covered as a contiguous sub-sequence: 566
neighbourhood paths today (`chat_session` 158 / `architect_proposal` 145 / `runtime_canary` 263),
of which 175 windows are new coverage (53 / 62 / 60).

**Cross-graph walks (`tests/graph/crossgraph.py`, `test_graph_crossgraph.py`).** The reference
graph names every cross-entity guard once; the orphan class behind N16-obs83 / obs111 / obs114
lives in the histories around those edges. A cross walk interleaves two worlds on one shared
entity — `agent × runtime_canary` and `agent × chat_session` on the agent the agent graph created,
`dataset × eval_run` with an administrator's `?force=true` delete of the replayed dataset — one
seed drawing which side moves at every step and the step in the middle third at which the
**delete is injected**. Each side keeps its own graph expectations; the composite adds the cross
rules the contracts name: the agent's `DELETE` is `409 agent.in_use` while a canary on it is
`running` (agents.md §1.1) else 200 (its refusals are enumerated, a live chat turn is not one);
after the delete every `/api/chat/{id}*` route and `/v1` answer `404 agent.not_found` and write
nothing (api.md "Error codes"); while the agent is `deploying` every chat send and `/v1` invoke
is `409 agent.not_active` (N16-obs74 ruling; N17-obs12 records the precedence over the session
gates) and a second canary create is `400 canary.agent_not_active` (N17-obs15); the canary's
`complete` / `rollback` while the agent's deploy job is live is the shared `409 agent.deploy_in_progress`
(agents.md §1.2 + architecture.md "Spec snapshots": both publish the live version) — accepted
until `cn/r17-c` fixed it (**N17-obs13**, PRODUCT). While it stood the pin was narrow:
`Cross.expect(…, symptom=202)` raises `RegisteredSymptom` only for the observed 202, the dedicated
test was `xfail(strict=True, raises=RegisteredSymptom)` and the `PINS` predicate xfailed a walk only
on that type at that step — a 409 that still writes, a dangling-reference trip or any other oracle
failure there FAILED, never xfailed (the pattern every future cross-graph pin follows). After the
fix the pin and the predicate are gone, the cross walk treats the two publishes under a live job as
refusal self-loops, `test_N17_obs13_*` drive the minimal sequences for both actions, and
`CanaryWorld.FIXED` keeps the edges in the neighbourhood set;
after the forced dataset delete a new run / rerun is the dataset's 404 (§23) and the queued run
still completes from its own copy of the items (§22). After EVERY step both worlds' own-id scans
and `crossgraph.dangling_references` run — a status-aware scan that treats a `deleted` Agent
named by a LIVE referrer (a `running` canary, an ACTIVE run) as dangling, the blind spot of
`scan_control_plane` that let N16-obs111 pass. 6 seeds per pairing in verify, 60 under soak. The
round-2 **redeploy injection** (`Plan.redeploy_at`) is soak-only for the three round-1 pairings —
seeds ≥ 60, so verify's seeds 0–5 replay round 1's plans byte-identically — and always on for
`agent × run`, the pairing round 2 added.

**Modelling rules the engine enforced (N17-obs10 / 11 / 14).** (1) An edge method that branches
on a history flag (`if self.dataset_deleted: expect 404`) is a hidden node: the contract's answer
differs by history, so the node splits (`eval_run.completed_dataset_gone`; the reference graph's
`*_again` refusals) and the method asserts the history instead. (2) A counter the contract bounds
is unrolled, never cycled: the canary ramp has three named stages (AC-E13-07), so `ready /
traffic_sent / verdict_recorded` exist per stage and `advance_at_final` is the NC-E13-05 refusal;
a cycle let a walk `advance` a third time and the executor could not name the answer. (3) An edge
method never performs other edges' work (the old `complete_final` ramped by itself).

**Soak (`make graph-soak`).** `LAUNCHPAD_SOAK=1`: every edge pair of every graph (the §6.7 soak
twins), 200 random walks per graph, 60 cross-graph walks per pairing. First run (2026-10-19,
32-core host, quiet): 3 758 tests, 5 min 31 s wall — 3 549 passed, 199 xfailed (the pinned
neighbourhood windows + the N17-obs13 pins), 10 failed → two distinct findings after shrinking
and dedupe by repro-hash (N17-obs14 ×3 walks, N17-obs15 ×7 cross walks); the verify-shaped first
batch had found N17-obs10 (×2), the audit it prompted N17-obs11, and the cross walks N17-obs12 /
N17-obs13. Second run after the model fixes (same host, same day): 4 332 tests, 6 min 22 s wall —
4 131 passed, 197 xfailed, 0 failed (the extra tests are the unrolled canary ramp's prime paths
and pairs). The verify-shaped graph suite (prime paths, edge pairs, 8 walks per graph, the
neighbourhood windows + primes, 6 cross walks per pairing) is 1 377 passed / 1 xfailed (the
N17-obs13 pin) in 2 min 10 s wall on the same host after the `cn/r17-a` merge and the rev-r17
corrections (the neighbourhood pairs moved to the soak).

**Round 2 (r17-e, 2026-10-20 — same method, fresh inputs).** A round is never a replay:
`LAUNCHPAD_LOOP_ROUND=n` (default 1; `LAUNCHPAD_LOOP_ROUND=2 make graph-soak`) makes the soak draw
walk seeds `[(n-1)·200, n·200)` and cross seeds `[(n-1)·60, n·60)` while the verify shape (8 / 6
seeds) stays fixed for a deterministic gate. Round 2 added, inside the same method: (1) a second
cross-graph injection — a **redeploy** of the shared agent at another step of the middle third
(cross seeds ≥ 60 and the new pairing — soak-only for the three round-1 pairings, so verify's
seeds 0–5 replay round 1's plans byte-identically), routed through the canary guard: a redeploy of
the champion while the canary runs answers the delete's `409 agent.in_use` (agents.md §1.1 —
**N17-obs16**, found by review on round 1's output and fixed in `cn/r17-d`; the cross walk treats it
as a refusal self-loop, `AgentCanary.cross_redeploy`, and `AgentWorld.FIXED` keeps the edge's
neighbourhood; while the fix was pending round 2 carried it as a KNOWN pin so it was never
re-registered); (2) a fourth pairing
**`agent_x_run`** — the evaluation-run graph on the agent the agent graph created, the delete and
the redeploy injected while runs are queued / finished, with the §23 rule (a run / rerun on a
non-active agent is `400 agent.not_active`) and the dangling oracle's "ACTIVE run naming a deleted
agent" rule as the oracle; (3) `RegisteredDangling`: an oracle line a register row already names
(`REGISTERED_DANGLING`, substring match) is the only dangling failure a pin may xfail — any other
line is new; (4) the FIXED neighbourhoods of N16-obs88 / obs117 and the N17-obs13 publish-under-job
rule (`CANARY_PUBLISHES`, fixed on `cn/r17-c`) re-explored by the soak-only pairs and the cross
walks. Yield: **N17-obs17** (DOC — `POST /api/eval/runs {evaluators: []}` on a deploying / deleted
agent is `400 agent.not_active`, not `422 run.no_evaluators`: the status gate precedes body
validation, the precedence class of obs12 / obs15) and **N17-obs18** (PRODUCT — `DELETE
/api/agents/{id}` is accepted while a run on the agent is `queued`; the queued run keeps naming the
deleted agent and later dispatches to `completed` against the torn-down Runtime; pinned strict +
narrow via `RegisteredDangling`). Soak numbers in the round-2 note of `loop_rounds.yaml`.

**Round 3 (r17-g, 2026-10-20 — same method, fresh inputs).** `LAUNCHPAD_LOOP_ROUND=3` (walks
400–599, cross 120–179) and three additions inside the method: (1) a third cross injection — the
**visibility flip**: the administrator sets `resource_visibility` to `isolated` at one middle-third
step and back to `shared` in the last third (`PUT /api/workspaces/{id}/settings/visibility`); while
isolated, every other-member edge of the pairings (`MEMBER_EDGES`: the agent graph's
`member_redeploy_other` / `member_delete_other`, the chat graph's `other_member_*`, the canary graph's
`member_verdict*` / `other_member_advance*`) must answer the kind's existing not-found
(users-and-ownership.md §7 "A hidden id answers the kind's existing not-found") and write nothing —
the cross walk performs the other member's raw request and checks that, the node unchanged; (2)
pairing **`agent_x_kbmem`** — an agent whose spec mounts a knowledge base and pins a managed memory
store, a live chat on it, and the KB / memory deletes (plain and `?force=true`) landing at their own
steps: `409 kb.has_attached_agents` / `409 memory.in_use` while the agent lives (api.md), 200 once it
is deleted, the chat memory rail (`GET /api/chat/{id}/memory`) answering 200 while the agent is active
and the agent's 404 afterwards, and the shared ledger scan's "pins a KB / memory that no longer
exists" as the oracle; (3) pairing **`canary_x_dataset`** — the canary's replay dataset deleted
mid-walk (§22 names no canary among a dataset's referrers, so the plain delete is 200 while a member's
`force` is a 403) and every later `traffic {dataset_id}` the dataset's 404 with the canary row
unchanged (`running`, same stage, no `error`). Yield: **N17-obs30** (DOC — a non-creator member's
`?force=true` reads `403 dataset.forbidden` from the §8 ownership gate before §22's `403
auth.forbidden`; two 403s the contracts never order) and **N17-obs31** (PRODUCT medium — the
self-hosted `delete_kb(force=True)` never strips the mount: `GET /api/agents/{id}` still lists the KB
and the scan fires, where api.md promises "force=true strips it from every mounted agent's spec
first"; pinned strict + narrow via `RegisteredSymptom` / `RegisteredDangling`). Soak numbers in the
round-3 note of `loop_rounds.yaml`.

**Round 4 (r17-i, 2026-10-20 — same method, fresh inputs).** `LAUNCHPAD_LOOP_ROUND=4` (walks
600–799, cross 180–239 on all six pairings) and two systematic additions inside the method:

* the **refusal × non-happy-state matrix** (`tests/graph/refusal_matrix.py`,
  `test_graph_refusal_matrix.py`): every refusal edge of the agent, chat, canary and run graphs is
  attempted while the subject is in each other non-happy state — the shared agent moved through
  REAL transitions (`redeploy` → a live update job, `redeploy_succeeds`, `delete_active`), the
  caller the creator / another member / the administrator, the workspace `shared` / `isolated`
  (the administrator's flip), the body valid / empty. A **gate** is one contract-cited reason to
  refuse with the answer it names (§7's hidden-id 404 · api.md's deleted-agent 404 · the deploying
  agent's 409 `agent.not_active` / `deploy_in_progress` · §4 / §8 ownership 403 or the personal
  session's 404 · the schema's 422 · the edge's own refusal); `resolve` lists the gates that apply
  to a cell and, where a contract sentence or a register row orders them (`KNOWN_ORDER`,
  `known_order`), the expected answer. Per cell: the per-table digest is unchanged (a refusal
  writes nothing), a hidden id answers exactly the kind's 404 (anything else is a **leak**,
  PRODUCT), the answer is one of the applicable gates' (else **unexpected**, a finding to shrink),
  and a cell whose applicable gates nothing orders is an **unordered pair** — a DOC gap registered
  once per (route family, pair) and then listed in `REGISTERED_PAIRS` so the next run tags it
  instead of failing. Verify runs every state × caller × visibility with a valid body plus the
  creator's empty-body cells; the soak runs the full grid. **Real coverage, not grid size** (rev-r17
  on `cn/r17-i`, fixed in `cn/r17-j`): round 4 observed 504 of the 741 verify grid cells (662 of 978
  under soak) — every `agent × canary` refusal ran at `active` only, because a running canary
  refuses the redeploy / delete that would move the agent (agents.md §1.1, N17-obs16 — the canary
  owns the agent, the cells are unreachable by design, never faked); `agent × run`'s
  `dataset_delete_while_queued` / `compare_with_queued` miss `deleted` for the same reason
  (N17-obs18); and seven agent-graph refusal edges have no cells because their source nodes
  (`absent`, `deploying` = a FIRST deploy, `redeploying_cold`) are outside `AGENT_STATES` —
  listed in `OUTSIDE_STATES` with the reason. Every skipped (state, reason) is now written per
  test (`skipped` next to `cells` in the JSONL, the world's trace) so a report line never reads
  as a cell that ran. `LAUNCHPAD_MATRIX_REPORT=<path.jsonl>` appends every test's observations. Round-4 yield:
  **N17-obs50** (DOC — the rerun route answers its own 404 / 422 before the agent-status 400; the
  observed order is run lookup → scope → agent status → dataset, refining N17-obs17) and
  **N17-obs51** (DOC — another member's agent delete under a live deploy job reads the §4 ownership
  403, not the §1 `deploy_in_progress` 409; the chat route orders status vs ownership the other
  way, N17-obs12). No leak: on every route the pairings touch, a hidden id answered the kind's
  404 and no refusal wrote.
* the **strip / unlink oracles** (`test_graph_strip_oracle.py`) on every force / delete seam of
  the self-hosted ports (the N16-obs114 / N17-obs31 class): the KB force strips every mounting
  agent (creator's, another member's) and refuses as a whole while a mount is `deploying` (409
  `agent.deploy_in_progress`, nothing written — api.md after cn/r17-h); the memory store delete is
  409 `memory.in_use` while a live agent pins it and `404 memory.not_managed` once dropped; the
  dataset / evaluator admin force deletes with `forced: true` while the referencing rows keep
  their dangling id (§22 — a queued run's `dataset_id`, an online config's evaluator); the registry
  skill-record delete leaves the objects under `skills/<name>/` in the bucket — **N16-obs35
  rediscovered** (open since r16-d), pinned strict as KNOWN with the cross-reference.

**Round 5 (r17-k, 2026-10-20 — same method, fresh inputs).** `LAUNCHPAD_LOOP_ROUND=5` (walks
800–999, cross 240–299 on all seven pairings) and two additions inside the method:

* pairing **`agent_x_offboarding`** (users × ownership, api keys × offboarding): the chat graph on
  ada's agent while the administrator offboards ada mid-walk — `PATCH /api/users/{ada} {status:
  disabled}` at the delete slot, a re-enable probe at the flip-back slot, `DELETE /api/users/{ada}`
  after the walk. Contract rules the composite asserts: a non-active account's cookie is no session
  (every request of hers 401 `auth.required`, nothing written — `routers/auth.py`); her `/v1` key is
  offboarded in the same request (401 `auth.invalid_api_key`, listed `creator_offboarded: disabled`,
  users-and-ownership.md §3 — eagerly on the status write, and "never re-enabled" by her return:
  only the administrator's `POST /api/apikeys/{id}/enable` brings it back); a live turn started
  before the offboarding runs to `done` (api.md); the agent's deploy job keeps running (workers are
  unfiltered); her agent and sessions keep `created_by = ada`, so after the account's deletion the
  key reads `creator_offboarded: deleted`, and under `isolated` the dangling creator "matches none
  of the three → admin-only" (§7): the other member reads the agent's 404.
* the matrix's **second fresh agent**: the agent graph's own refusals at `absent` /
  `deploying` (a FIRST deploy in flight) / `redeploying_cold` (a re-publish from `failed`) run on a
  second agent of ada's brought there through the agent graph (`drive_second_agent`), so every
  refusal edge of the agent graph has cells (`OUTSIDE_STATES` is empty); `LIVE_JOB_STATES` names
  the three deploying states for the §1 gate; the create refusals address a NAME, not an id, so §7's
  hidden-id gate does not apply to them — instead `name_leak` checks that the duplicate-name 409
  never carries a hidden agent's id (it does not: `detail.agent_id` is `null` for a hidden row).

Deferred to the next round, with reasons: skill_lab × registry (the Skill Lab harness is a
subprocess stub stack — `tests/model/test_skill_lab_state_machine.lab` — worth its own composite),
memory × chat consolidation (the memory_record graph's walks already interleave the extractor's
consolidation with the console deletes under the r16-d scan; the chat-rail reads add little).

**The stop rule (user ruling 2026-10-19).** The loop runs round after round with no fixed cap: a
round that FOUND bugs (reproduced, classified, registered — PRODUCT / TEST / DOC all count) is
followed by a round with the SAME method, a round that found NONE must be followed by a DIFFERENT
method / idea (walks → cross-graph interleaving → fault-injected walks on the recorded seam error
shapes → time-shifted walks with clock injection → role-swapped walks → concurrency pairs → …),
and the loop STOPS only after THREE CONSECUTIVE zero-yield rounds — any finding resets the
streak. Rounds are the append-only ledger `tests/graph/loop_rounds.yaml` (round, date, method,
finding ids); `loop_report` renders them into `graph-coverage.md` as one line each (`round N ·
method · findings K (ids) · streak S`), and the board reads those lines: keys `loop_round`,
`loop_round_method`, `loop_round_findings`, `loop_zero_streak` (stop at 3), `loop_stop`,
`loop_switch_method`; `make graph-coverage` ends with that line (`scripts/quality_metrics.py
--stop-rule`; §7b of `quality-metrics.md`). **Round 1** = this branch (walks + neighbourhood +
cross-graph + soak): 6 findings, streak 0 → round 2 keeps the method. Prime-path coverage, the
"consecutive registers without a graph finding" count and capture–recapture (overall and per
deep-dive path, plan §4) stay on the board as **reference metrics only** (`graph_stop_prime_pct`,
`graph_stop_zero_rounds`, `graph_stop_est_remaining`): today 100 % (388 / 388 prime paths, 177 /
177 edges after `cn/r17-a` fixed N16-obs88 / obs117) · 0 · blank (no channel-B row yet). Additive loop-engine keys: `graph_walks`, `graph_walk_findings`,
`neighbourhood_paths`, `neighbourhood_new`, `graph_cross_walks`, `graph_cross_findings`,
`graph_findings_latest_round`.

### 6.9 Fault-injected walks — the recorded seam shapes replayed mid-walk (`backend/tests/graph/faults.py`, round 6, r17-l)

Round 5 (cross-graph interleaving, fresh inputs) yielded nothing, so the loop's rule switched the
method (§6.8 "The stop rule"; the ladder in `loop_rounds.yaml`). Rounds 1–5 walked the graphs
over a *healthy* stubbed AWS; round 6 keeps the same worlds, the same graphs and the same walk
generator, and makes the data plane answer the way it really does when it fails.

**The shapes (`faults.SHAPES`, `backend/tests/fixtures/seams/README.md`).** Every shape is a REAL
recording or an existing pinned live shape, never a hand-rolled `RuntimeError("boom")`:

| family | shapes | recorded where |
|---|---|---|
| `5xx` | Runtime `InternalServerException` as a `ClientError` and as the SDK's terminal error frame; OpenSearch 503 (no master) / 502 (HTML bad gateway); S3 `InternalError` | `tests/faults/conftest.py` (N-ARCH-obs18), `test_memory_merge_properties.REAL_FAULTS` (N14-obs4) |
| `throttle` | Runtime `ThrottlingException` (client + frame); OpenSearch 429 `es_rejected_execution_exception`; S3 `SlowDown` | same |
| `timeout` | botocore `ReadTimeoutError` with the real `/runtimes/<arn>/invocations` endpoint URL; the read-timeout SDK frame; urllib3 `ReadTimeoutError` with the real pool text | `seams/agentcore-runtime/invoke_read_timeout.json`, `seams/opensearch-memory/read_timeout.json` |
| `frame` | the agent template's terminal error frame (`no workload access token …`); the **partial frame** (the recorder's 300-char cut = a connection dropped mid-line) alone and after healthy Converse deltas; the eval worker's `REJECTED` JSON body | `seams/agentcore-runtime/invoke_stream_success.json`, `invoke_second_turn_same_session.json`, `eval_worker_invalid_job_payload.json` |
| `not_found` | Runtime data-plane 404 (`No endpoint or agent found with qualifier 'DEFAULT'`); OpenSearch `index_not_found_exception`; SSM `ParameterNotFound` (HTTP **400**, empty message); S3 `NoSuchKey` | `seams/agentcore-runtime/invoke_unknown_runtime.json`, `seams/opensearch-memory/index_not_found_404.json`, `seams/ssm-getparameter/get_parameter_not_found.json` |
| `access_denied` | the IAM identity-policy 403 body (role ARN inside), the OpenSearch 400 `parsing_exception` body; a Runtime `AccessDeniedException` synthesised from the recorded IAM sentence (the Runtime text was not captured — the shape says so); S3 `AccessDenied` | `seams/opensearch-memory/iam_identity_403_instance_role.json`, `search_bad_query_400.json` |
| `conflict` | `ConflictException` "The agent is currently being modified by another operation. Current status: CREATING." (the console's recorded 409 `aws.conflict`); `RetryableConflictException` (api.md END SESSION) | `seams/console-api/agents_delete_seam-probe-unsupported.json` |
| `absent` | the runtime already gone on readback (`RUNTIME_ABSENT`, N16-obs87); an eventually-consistent EMPTY OpenSearch read | the wrappers' own return values |
| `interrupted` | a `BaseException` that kills the worker mid-step (the pipeline's `except Exception` never sees it — the job is left exactly as a process death leaves it) | `tests/faults/test_restart_sweeps.py` class |

Browser shapes (`stop_browser_session_already_stopped`, `get_browser_session_unknown`) are in the
fixtures directory for the catalogue; no lifecycle graph reaches the Browser seam, so they are not
served. The runtime-canary and architect-proposal graphs have no fault world: their seams are
ledger emulations (`tests/model/test_canary_state_machine.Seams`) and the architect harness, whose
fault behaviour `tests/faults/test_assistant_faults.py` already pins.

**The hook.** `World.fault(edge, shape) → Outcome` is implemented once per graph in
`tests/graph/fault_seams.py` (`AgentFaults`, `ChatFaults`, `EvalRunFaults`, `MemoryFaults`,
`ReferenceFaults` — subclasses of the §6.7 worlds). For ONE step it re-points the seam the edge
reaches — the agent world's recording fake stage (`hooks[stage]` raises the shape inside
`provision` / `deploy` / `register`, exactly where the real stage lets the botocore exception
propagate), `zip_method.delete_agent_resources`, the chat producer or the real invoke chain over a
fake data plane for the SSE-level shapes, the `/v1` sync wrapper, the StopRuntimeSession plane,
the eval worker's `invoke_errors` / judge, the extractor's and the hub's OpenSearch (a once-only
fault at the candidate read or at one of the op's writes; the hub's `get_doc` / `delete_doc` /
`update_doc` / `search`), the memory store's S3 / OpenSearch purge, the KB cleanup steps — drives
the edge, judges the answer, and returns the node the CONTRACT says the entity is in now
(`fault_dst`, pure: the walk is planned from it). The classmethod `fault_shapes(edge, family)`
says which shapes an edge can serve (with `at` variants: the stage, the OpenSearch method, the
replay attempt).

**The oracles — every one cites a clause or an invariant (`FaultMixin.fault_step`):**

* the contract's answer where a sentence exists: api.md "Errors" (`AWS_ERROR_MAP` → `aws.*`,
  `detail {aws_error_code, operation}`, the generic `/v1` message; "Any other AWS error code …
  remains an unhandled 500" is the ONE documented bare 500 and is accepted as such), N16-obs87
  (AccessDenied with the runtime present → 403, row kept; already gone → 200, `runtime` skipped),
  agents.md §4.2 / §4.3 (a first deploy / cold re-publish that fails → `failed`; a failed update
  keeps the live version unless the failure is inside `deploy`), api.md END SESSION
  (NotFound → `already_ended: true`; a conflict → 409, `ended_at` untouched), assistant-turns.md §4
  / §8.2 (`chat_stream` never raises mid-stream — the terminal `error` keeps the partial answer,
  `{message}` verbatim for a non-AppError), evaluation-engine.md N-SCN-bug1 (a transient is
  replayed on the SAME pre-assigned session; an errored scenario is one error record per
  evaluator and a completed-with-errors run; only every scenario failing fails the run),
  memory-backend.md "Error codes" + `routers/memory._guard` (429 / 5xx → 503, transport → 502,
  404 → 404; nothing removed on a failed delete), handler.py `RetryableError`, WB-008 (every KB
  storage step best-effort, the pending ones to a `kb_cleanup` job named in the response);
* **no half-written row** — the per-table digest before / after the faulted step must differ only
  in the tables the contract's failure state names (`Outcome.writes`: the Job / Deployment /
  Agent of a failed stage, a turn's three tables, a run's two, the memory index for an applied
  op) — anything else is a `HalfWrite`; the own-id ledger scan and the world's extra scans
  (`scan_memory_index`) after every step;
* the **terminal state** — a bounded wait that is a join on the driver (never a sleep): the
  turn's thread, `producer.wait_finished`, the run's `wait_terminal`; the entity is at the planned
  node (a disagreement between `fault_dst` and the observed state is itself a finding);
* the **envelope** — `{code, message, detail}` on every HTTP failure; an exception that escapes
  the handlers (TestClient re-raises it) is an `EnvelopeViolation` unless it is the documented
  unhandled 500; no raw seam text in an envelope (`LeakedSeamText`: the IAM sentence, a role ARN,
  an OpenSearch `root_cause`, botocore's framing — `opensearch_error_handler`'s own rule);
* the **recovery path converges** — `execute_deploy_job` on the terminal job is inert and the
  row stays; a job a process death left `running` is adopted by the resume and ends
  `active` / `succeeded` with every stage `succeeded` / `skipped`; a reattach after a faulted turn
  replays the persisted log to the same terminal frame; the extractor's redelivery (the same event
  again, seam healthy) leaves a clean index with the op applied.

A fault at an edge for which NO sentence names the outcome (the partial frame, the `REJECTED`
body, the stale empty read) is still driven: the observed behaviour goes into
`Outcome.observation`, `LAUNCHPAD_FAULT_REPORT=<path.jsonl>` writes every walk's observations,
and the round registers them as DOC / PRODUCT rows.

**The walks (`faults.random_fault_walk`, `test_graph_fault_walks_<graph>.py`).** The route is the
§6.8 uniform random walk; 1–3 of its steps carry a shape. The slots are drawn among the steps of
the UNFAULTED route at which a shape could be served — transition edges first (a refusal's seam
is never reached) — so a graph with one seam still faults it; a slot the diverged route cannot
serve moves to the next step that can. After a faulted step the walk continues from `fault_dst`.
Deterministic in `(graph, seed, family)`. A failure is shrunk over the planned nodes (faults
included — a faulted step is removed only inside a removed cycle) with the §6.8 shrinker
(`same_as`: the failing step token and the exception type), reported with the register-ready
text and `repro-hash`. Fixed shape: 4 seeds per graph, seed *i* walking family `FAMILIES[i]`
(four families per run; the five graph files measured 7–96 s alone on the loaded 32-core
host, under `-n auto` well inside the budget). **Off the default gate since r17-s2 (N17-obs287,
design review 10.5):** rounds 8–9 of this method yielded nothing, so under the loop stop rule the
seeded walks — the per-graph `test_<graph>_fault_walk` cells and the cross fault walks of
`test_graph_fault_cross.py` — are collected but skipped by `make verify`; `make graph-faults`
(`LAUNCHPAD_FAULT_WALKS=1`) runs the fixed shape, `make graph-coverage` sets the same switch so
the board keeps counting them, and `make graph-soak` runs the full window below. What stays in
the gate is deterministic: the refusal-seam axis (`test_<graph>_refusal_seam_axis`), the
workspace stage injection, `test_clear_deployed_fault_probe`, the pinned `test_N17_obs*`
regressions, the `test_graph_fault_probes_*` files and the engine's own tests
(`test_fault_engine.py`, incl. the switch itself). Soak (`LAUNCHPAD_SOAK=1 LAUNCHPAD_LOOP_ROUND=6`):
every family × 20 seeds per graph in the round's fresh window `[(r-1)·20, r·20)` — 180 walks per
graph. One family a graph has no seam for (the memory graph's `frame`, `conflict`) skips with the
reason.

**Pins keyed on the symptom.** The shrunk label of a fault walk varies with the random prefix
while the failing step and its oracle message are the finding, so `faults.KNOWN_SYMPTOMS` maps a
substring of the oracle's message (`"the failed extraction left a half-written pair"`, `"the
memory store's ledger row is GONE"`, …) to the register id: a walk (or an axis cell) that
hits it xfails with the id; the dedicated strict pin `test_N17_obs9x_*` next to the graph's fault
file drives the minimal sequence through `fault_step` with `raises=<the symptom type>` — the
moment the fix lands the strict xfail fails loudly, the mark is deleted and the symptom's
`KNOWN_SYMPTOMS` line with it (the pin keeps its name and drives the same sequence as a plain
test: obs94 / obs95 since `cn/r17-o`). `faults.FINDINGS` is the append-only ledger
the loop-engine table counts (`graph_fault_walks`, `graph_fault_findings`).

**The refusal × seam-faulted axis (`test_<graph>_refusal_seam_axis`).** The round-4 matrix gains
the axis it lacked: every refusal edge of the five graphs is attempted — on the BFS prefix to its
source node — with the shape armed on EVERY seam of the world (`FaultMixin.
refusal_with_faulted_seams`): the refusal must answer as its contract says, write nothing (digest)
and never reach the seam (`seam_calls == []`; the chat world also proves the armed producer
script was never consumed). Where the contract's decision IS the seam's read — the memory record
delete reads the stored `store_id` / `actor_id` before its 404 (memory-backend.md `delete_record`)
— the fault's own envelope is the answer and nothing is removed. Three families in verify (`5xx`,
`timeout`, `access_denied`), all under soak.

**Cross-graph walks with the shared seam faulted (`test_graph_fault_cross.py`).** `agent × chat`
where the injected delete of the shared agent first meets a Runtime shape on `DeleteAgentRuntime`
(the family drawn from the seed and the round): the refused delete must be the mapped envelope
or the documented unhandled 500, write nothing, and the chat side keeps walking on the agent that
stayed; the plan's delete then lands clean on the next step and the §6.8 dangling scan runs after
every step.

**Recovery oracles, in one place.** (1) deploy: after a faulted stage, `execute_deploy_job` is
inert on the terminal job and `in_flight_deploy_job` is None; after the `interrupted` shape the
resume completes the job (the N16-obs83 finalizer guards hold: nothing is written onto a deleted
row — the agent graph's `row_marked_deleted_mid_job` keeps that edge). (2) chat: the reattach
`GET …/turns/{n}/stream?from=0` after a faulted turn answers the same seq-stamped frames as the
POST streamed and ends on the terminal `error`; the base `reattach_open` edge is overridden in
the fault world to accept the turn's recorded terminal (`done` or `error`) — §8.3 names "the
terminal event", the graph model assumed `done`. (3) memory: after a faulted extraction the
index scan is clean and the redelivery converges; the obs32 poison classification (a domain-wide
fault inside the `_job` bookkeeping) is NOT re-explored — the fault is once-only by design, so
the handler's own `_job(FAILED)` write succeeds and the half-write question stays separable from
the open round-16 item.

**Round 6 yield (r17-l, 2026-10-20).** Twelve findings — nine PRODUCT, three DOC — plus one
rediscovery (N16-obs32: ONE fault on the extractor's job bookkeeping escapes raw — cross-referenced,
pinned, not a new id), from the fault walks' verify-shaped batch, the dedicated probes of the two
shapes the seeds had not drawn and the soak; soak numbers in the round-6 note of `loop_rounds.yaml`:

| id | graph · step | observed vs contract | class |
|---|---|---|---|
| N17-obs90 | `memory_record` · `add>supersede@os_503_no_master/update_doc` | the extractor's supersede writes the successor (`put_doc`, `supersedes=[target]`) before retiring the target (`update_doc`): a fault between leaves BOTH active, the successor naming a target with no `superseded_by` — the index scan fires; memory-backend.md "the target gets `superseded_by`" in the same operation | PRODUCT |
| N17-obs91 | `memory_record` · `add>supersede`, then DELETE the chain head with `delete_doc` faulted | the unlink runs before the delete: the predecessor is revived (`superseded_by` cleared) while the head that retired it is still there — two active facts, the retired one back ("the fact that retired it is gone") | PRODUCT |
| N17-obs92 | `memory_record` · `api_delete_again@os_bad_query_400/get_doc` (and `wrong_actor`, `os_iam_403`) | `routers/memory._guard` answers an unmapped OpenSearch status with 502 `memory.unavailable` whose message embeds `str(exc)` — the raw body (the IAM sentence with the role ARN, the `parsing_exception` root cause) reaches the console; `opensearch_error_handler`: "a static, per-code sentence, never str(exc)" | PRODUCT |
| N17-obs93 | `reference` · `agent_delete_releases_kb>memory_delete_after_agent_gone@s3_slow_down/list_objects_v2` | the memory store delete commits the row's deletion, then the UNGUARDED S3 purge raises → 502 `memory.unavailable` (botocore text in the message) for a store that is gone, and the OpenSearch purge never runs; memory-backend.md / N14-obs4 name the OpenSearch purge as best-effort only | PRODUCT (+ DOC) |
| N17-obs94 | `chat_session` · `…>v1_turn@rt_read_timeout`; also `agent.delete_active`, `chat_session.stop_open`, the faulted cross delete | a botocore transport exception (`ReadTimeoutError`) has no envelope mapping on ANY route — a bare 500; api.md "Errors" maps `ClientError` codes only and its "never a bare 500" names no transport family | PRODUCT (+ DOC) |
| N17-obs95 | `chat_session` · `first_turn>turn_completes>v1_turn@rt_frame_no_workload_token` | a `/v1` sync invoke whose stream ends on the SDK terminal error frame escapes as `SdkStreamError` — a bare 500 where Chat renders the same frame as an `error` event; api.md `/v1`: "Errors use the platform envelope" | PRODUCT |
| N17-obs96 | `chat_session` · `first_turn@rt_partial_frame_only` | a truncated `data:` line that is the whole response is rendered VERBATIM as the assistant's reply with `done` (`_sse_payloads` yields non-JSON data as text, `_runtime_payload_events` makes a bare string a `complete`); after healthy deltas the same line is silently dropped | PRODUCT |
| N17-obs97 | `chat_session` · `first_turn@rt_rejected_json_body/json` | a structured refusal body (`state: REJECTED`, `code`, `message`) is rendered as the reply with `done` (the free-form `message` key wins) instead of an `error` | PRODUCT |
| N17-obs100 | `agent` · `…>redeploy_succeeds@rt_conflict_modifying/deploy>redeploy_from_failed>cold_redeploy_fails` | after a failure inside `deploy` (agent `failed`, §4.3 — the live state unknown) a re-publish that fails before `deploy` flips the agent back to `active` (`_keeps_live_version` finds the older succeeded deployment); §4.2 says `failed` | PRODUCT (+ DOC) |
| N17-obs101 | `memory_record` · `add>add_duplicate@os_empty_read/knn_search` | an eventually-consistent empty candidate read lets the extractor ADD the near-duplicate — two ACTIVE records at cosine 0.95 (two concurrent extractions for one actor are the live shape); "no two ACTIVE records at cosine ≥ the dedupe threshold" | PRODUCT (+ DOC) |
| N17-obs99 | `reference` · `kb_force_delete_strips@<any storage shape>` | the KB force delete behaves as WB-008 says (200, the step `pending`, a `cleanup_job_id`) but its `cleanup_warnings` carry the raw seam text — the OpenSearch body (IAM sentence / `root_cause`), botocore's framing; nothing says what a warning may carry | DOC |
| N17-obs98 | `chat_session` · `first_turn@runtime_bedrock_5xx_frame` | the chat `error` event for the SDK frame carries `{message}` only; §4 gives the architect turn `detail {error_type, error, message}`, §8 "Chat playground parity" names no exception | DOC |

Not findings, recorded: the `absent` family's stale empty read on `update` / `supersede` turns the
op into an add (the contract's "no candidate → add" rule; the dedupe consequence is obs101); the extractor classifies a permanent OpenSearch 403 / 400 as `RetryableError` (SQS
redelivery until the DLQ — `RetryableError`'s "content unusable" rule does not cover an infra
denial, defensible). Yield > 0 → the zero-yield streak resets to 0 and **round 7 keeps the
method** (fresh seed window `LAUNCHPAD_LOOP_ROUND=7`, more seams: the canary's cleanup
`delete_endpoint_quiet`, the KB ingest's S3 / Bedrock embedder, the registry's S3).

**Round 7 (r17-m, 2026-10-20 — same method, fresh inputs).** `LAUNCHPAD_LOOP_ROUND=7` (fault seeds
120–139 per family) and, inside the method: (1) **edge-anchored symptom pins** (rev-r17 nit B on
round 6): `faults.KNOWN_SYMPTOMS` is keyed `(edge prefix, message substring)` and
`known_symptom(exc, token)` matches the failing step's edge — the same oracle text produced by
another edge is a NEW finding, never a silent xfail; the one route-independent row is the missing
transport-exception handler (N17-obs94), the same defect on every route by construction. (2) A
**sixth fault world, `CanaryFaults`** (`test_graph_fault_walks_canary.py`), at the cleanup's AWS
seams: `delete_endpoint_quiet` run over a raising control plane (the exception carries the
service's MODELLED class name — `type(exc).__name__ == "ResourceNotFoundException"` — because that
is what `canary_infra._NOT_FOUND` matches; a bare `ClientError` would be a stub artefact), the
live-version read guarding the candidate-zip delete, the zip delete itself, and the endpoints-gone
wait answering "still listed" (the `absent` family). Oracle = evaluation-engine.md N-SCN-obs3
(every delete per-category tolerant; a refused endpoint delete is a `skipped` row; "the row flips
to `cleaned` either way"; `endpoints_pending`), `cleanup_blockers` ("`skipped` is never success"
except the live-version zip and the gone marker), the ledger parts always done (no online configs,
no assignments), and the recovery oracle: a second `cleanup` on a healthy seam clears the blockers.
A canary rolled back from `fresh` owns nothing to delete — the seam is not reached and the step is
plain. (3) **Probes at the storage seams no graph edge reaches**
(`test_graph_fault_probes_storage.py`): the KB ingest Lambda over the ingest property world (S3
`get_object` for one object, the embedding model for one chunk, the OpenSearch `bulk` for one
document — handler.py "one bad document never fails the job", scanned = indexed + failed, a FAILED
document owns no chunks), the registry skill-record sweep with a refused per-key `delete_object`
(api.md "best-effort — `objects.error` on an S3 failure"), and the S3 `DeleteObjects`
partial-failure body (`Errors[]` on a 200) on the memory store purge. New shapes:
`bedrock_embed_validation` / `_throttle` / `_5xx` (the pinned botocore shapes on `InvokeModel`) and
`s3_delete_partial` (a body, not an exception — `Shape.partial_delete(keys)`).

Round-7 yield: **four findings — three PRODUCT, one DOC**; soak numbers in the round-7 note of
`loop_rounds.yaml`:

| id | seam · shape | observed vs contract | class |
|---|---|---|---|
| N17-obs118 | registry skill sweep · `delete_object` refused (AccessDenied / SlowDown / InternalError) | `objects_deleted: true` with the SKILL.md still in the bucket — `skill_ingest._delete_keys` swallows every per-key exception, so the contract's `objects.error` branch is reachable only for a failed listing | PRODUCT |
| N17-obs119 | memory store purge · `DeleteObjects` 200 with `Errors[]` | `_purge_s3_prefix` never reads the response: refused keys stay, the delete answers `{deleted: true}`, nothing logged (the KB cleanup's `s3` step deletes the same way) | PRODUCT (+ DOC) |
| N17-obs120 | KB ingest · a transient embedding fault (throttle / 5xx) on one chunk | the document is FAILED permanently after one attempt, the job COMPLETE; the memory extractor replays the same shapes and `is_upstream_transient` is the one shared predicate the ingest never consults | PRODUCT (+ DOC) |
| N17-obs121 | canary cleanup · a refused endpoint delete | `cleaned` with a `cleanup_blockers` entry, as N-SCN-obs3 says — and agents.md §1.1 says `cleaned` never blocks the agent delete: the endpoint stays with no retry path named (a manual `cleanup` again does clear it) | DOC |

Yield > 0 → streak 0 → **round 8 keeps the method** (`LAUNCHPAD_LOOP_ROUND=8`; next seams: the
Gateway / Identity control plane, the eval worker's S3 artefact store, the Lambda's SQS redelivery).


**Round 8 (r17-r, 2026-10-20 — same method, fresh inputs).** `LAUNCHPAD_LOOP_ROUND=8` (fault seeds
140–159 per family) and three seams: (1) the **eval worker's S3 artefact store as the hub's poller
reads it** — `EvalRunFaults` gains `store_status` / `store_results` variants of the S3 shapes
(`OnceFaultyStore` raises on the first `get` of `status.json` / `results.jsonl`; a `NoSuchKey` is
the store's own "not yet written" and the run completes); oracle = evaluation-engine.md §5
(`execute_run` never raises, the row carries the failure; `recheck_run` re-attaches by
`batch_eval_id` and settles from `done.json` — the recovery oracle inside the step), the worker's
own completion (`executor.wait`) and the four result rows; (2) the **extractor's SQS redelivery**
(`test_graph_fault_probes_redelivery.py`): a processed `add` delivered again leaves one record (the
id derives from the text), a processed `supersede` delivered again neither chains nor leaves a
second active fact, and the redelivery after obs90's half-written supersede must retire the
original; (3) the **Identity control plane behind the connection delete** (`identity_providers.
delete_connection`, the vault delete first, the audit row second): a vault fault leaves the row and
every user grant untouched and surfaces as the mapped `aws.*` code, a provider vanished between the
listing and the delete answers the 404 once and the next attempt cleans the row (N16-obs87 class,
two attempts converge), a throttled listing refuses before any write. Gateway targets were not
probed (AWS-only state, no ledger row to half-write).

Round-8 yield: **two findings, both PRODUCT**; soak numbers in the round-8 note of `loop_rounds.yaml`:

| id | seam · shape | observed vs contract | class |
|---|---|---|---|
| N17-obs122 | extractor redelivery · after obs90's half-written supersede | the replayed `supersede` targets the SUCCESSOR (now the nearest active candidate) → the self-supersede guard makes it a noop → the original stays active for good; `RetryableError`'s redelivery is the only repair path the contract names and it cannot repair this state (raises obs90) | PRODUCT |
| N17-obs123 | eval poller · one `SlowDown` / `InternalError` / `AccessDenied` on `status.json` or `results.jsonl` | the run is `failed` with the ClientError while the worker job completes unread; nothing retries the read, `recheck` recovers only by hand; §5 names stale and lost jobs, not a transient read | PRODUCT (+ DOC) |

Held: redelivery idempotency (one record after a duplicate `add`; no chain after a duplicate
`supersede`), the Identity vault seam on every shape. Yield > 0 → streak 0 → **round 9 keeps the
method** (`LAUNCHPAD_LOOP_ROUND=9`; next seams: the Gateway target control plane, the Skill Lab
subprocess harness, the architect purge's AWS readbacks).

**Fixed in `cn/r17-n` (r17-fixA, 2026-10-20) — the memory seams.** Six of the rows above flipped
their strict pins (the xfail marks and the `KNOWN_SYMPTOMS` entries are gone; the tests stay as
the regression pins): N17-obs90 (the supersede pair is ONE `bulk` request — `EXTRACTOR_CALLS`
lists `("knn_search", "bulk")`, the pin faults `bulk`), N16-obs32 (job bookkeeping before the
work → `RetryableError`, after it fail-soft; `lambda_handler` retries a raw transient — also the
three `domain_wide` xfails and the `classification` test of `test_memory_merge_properties`),
N17-obs91 (`_relink_history` undoes the unlink when `delete_doc` fails), N17-obs92 (static 502
message), N17-obs93 (both store purges best-effort; `_faulted_memory_delete` now asserts the
`skipped` step and runs the seam-text oracle on `cleanup_warnings`), N17-obs101 (backend half:
`_refresh` before the nearest-record re-check; `EmptyRead` models near-real-time search — stale
until a `_refresh` request — so the pre-fix code still reproduces the finding; the per-actor
serialisation is the open infra half, design in memory-backend.md). N17-obs99 (DOC) is closed by
`app.core.errors.redacted_error`: a leaked `cleanup_warnings` line is a `LeakedSeamText`
FAILURE of `kb_force_delete_strips` now, no longer an observation.

### 6.10 The workspace permission contract, the workspace lifecycle graph and the validate-only release smoke (r17-ws, N17-obs117)

A same-account workspace registered in the hub's OTHER region failed at `validate-access` on
2026-10-20 with `ecr DescribeRepositories … AccessDeniedException` (N17-obs112) and no layer of
§1–§6.9 could have caught it: the backend suite stubs AWS (a denial exists only where a test
injects one), `infra/tests/test_ec2_template.py` checks the ops policy's SHAPE, no lifecycle
graph covered the workspace, and the live audits ran only in the hub region — one workspace
per `(account, region)` means the stage had never run live in the stack's own region either.
The IAM contract between `infra/ec2/launchpad-cn-ec2.yaml` and `workspace_bootstrap.STAGES` was
simply never written down. Three additions close the gap.

**The permission contract (hermetic, in `make verify`).** `app/services/workspace_permissions.py`
is the declarative manifest: one `Need(stage, service, action, resource, source, when, degrades,
implicit)` per IAM action a stage issues, derived by reading each `_stage_*` function and the
wrappers it calls (`source` names them), with the resource written the way the SERVICE
authorises the call — `{partition}` / `{region}` / `{account}` are the WORKSPACE's values, an
action without resource-level permissions is `*`, and a call the stage issues WITHOUT naming a
resource (the probes: `describe_repositories(maxResults=1)`, `list_projects()`,
`list_roles(MaxItems=1)`) is the wildcard resource the service evaluates it against —
`repository/*`, exactly the ARN the live error named. A conditional call (`when`: an adopted
role whose trust document drifted, the hub-colocated memory stage) and a stage that DEGRADES on a
denial (`degrades`: registry defaults, memory, skill-lab, observability, policy) are marked so
the evaluator reports them apart. `tests/consistency/iam_policy_eval.py` renders the template's
instance role for a `(hub region, account, parameters)` triple — `!Sub` with the pseudo
parameters, `!If` over the template's `Conditions` from the parameter map (defaults, or the
`REQUIRED_PARAMETER_STAND_INS` for the three parameters without one), `AWS::NoValue` dropped
from lists, `AdministratorAccess` behind `GrantHubAdmin` as one `Allow * on *` — into
`Statement` rows and evaluates `(action, resource)` as IAM does for an identity policy without
conditions: explicit Deny → matching Allow (IAM globs; actions case-insensitive, resources not) →
implicit deny. Not modelled, on purpose: policy `Condition` blocks (the role has none),
boundaries, SCPs, resource policies, `AmazonSSMManagedInstanceCore`.
`tests/consistency/test_workspace_permissions.py` is the differential: hub region
`cn-northwest-1`, the stack's own region → every unconditional, resource-scoped need is allowed
(what `make bootstrap` has relied on all along, re-derived for the staged job); the other region
`cn-north-1` under the default parameters → DENIED: ECR (the storage stage's by-name describe),
CodeBuild (the foreign-resource check, `UpdateProject`), Secrets Manager (AgentCore Identity's
secret, authorised against the caller — N11-bug7), plus the degrading `codebuild:StartBuild` /
`BatchGetBuilds` / `ecr:DescribeImages` of the Skill Lab build and the conditional CloudTrail
channel — every one a statement whose resource ARN carries `${AWS::Region}` (the one SSM need,
the model-credential registry pass's `ssm:GetParameter`, is granted on `*` by the AWS-managed
`AmazonSSMManagedInstanceCore`, which the evaluator models too — rev-r17 nit); the first probe
IAM refuses is `ecr:DescribeRepositories` on `repository/*`, the one the live job log showed.
`HubAdminAccess=true` → every need allowed in both regions (the N17-obs112 ruling). Pins (strict):
**N17-obs112** (`test_N17_obs112_other_region_bootstrap_is_grantable_under_the_default_parameters`
— flips when the scoped policy is widened or the default host's parameter becomes the template's
default); **N17-obs130** — the three probe shapes are not grantable by the scoped policy in ANY
region (`ecr:DescribeRepositories` on `repository/*`, `codebuild:ListProjects` on `*`,
`iam:ListRoles`, which the policy does not grant at all), so a least-privilege host fails
`validate-access` in its own region too; the positive twin names the fix (probe by name:
`describe_repositories(repositoryNames=[<repo>])` tolerating `RepositoryNotFoundException`,
`batch_get_projects(names=[…])`, `get_role` tolerating `NoSuchEntity` — shapes the policy already
grants); **N17-obs131** — `workspace_iam.ensure_role` refreshes an adopted role's trust policy
when it differs, and `IamAdoptAgentRoles` grants `iam:UpdateAssumeRolePolicy` on
`role/launchpad-agent-*` and `role/launchpad-skill-lab-worker-*` only, so the gateway and
CodeBuild roles of a resumed bootstrap are denied the moment their trust document changes (the
execution role passes by prefix accident). Adding a stage call: add its `Need` (the manifest
asserts `STAGE_NEEDS` keys == `STAGE_ORDER`; a differential pins `_PROBES` ⊆ the
`validate-access` needs); the hub-region test fails on a need the policy cannot grant — widen
the template or register the gap, never drop the need.

**The validate-only bootstrap mode and the release smoke (not in the gate).**
`POST /api/workspaces/{id}/bootstrap {"validate_only": true}` (api.md "Console Workspaces API —
bootstrap") runs the `validate-access` stage alone through the same job machinery
(`workspace_bootstrap.MODE_VALIDATE_ONLY`): nothing is provisioned, the row is handed back the
status it had, the job's terminal status is the verdict, and a later full run never carries the
probe's green stage forward (`tests/test_workspace_bootstrap_validate_only.py` pins all of it
hermetically, including the N17-obs112 denial shape and the resume across a probe).
`backend/scripts/e2e_workspace_region_smoke.py` (REAL AWS, deploy-ops, deploy.md §8.1 "After each
host update") registers `smoke-<region>-<stamp>` in the hub's other region, runs the mode, waits
for the job and purges the workspace again: exit `0` = the host can provision there, `2` = denied
(by design on the least-privilege t1, a regression on the default host), `1` = anything else. The
console's workspace detail page offers the same probe as 「仅校验访问」/ "Validate access only"
(`v2-ws-validate-access`) next to 「运行初始化」.

**The workspace lifecycle graph (`tests/graph/graphs/workspace.yaml`, `test_graph_workspace.py`).**
The eighth graph: 19 nodes, 39 edges, 28 prime paths, 108 edge pairs; every edge cites
architecture.md "Workspaces — multi-account/multi-region environments" (the CN stage list, the
`validate-access` verdicts, "every stage records the identifiers it produced … as it goes", the
resume, the interrupted job, the conflict, the validate-only run, detach / purge), api.md "Console
Workspaces API — bootstrap" or the E19 acceptance sheet — sentences this round ADDED to those docs
where the behaviour had none (the paragraph still listed a Cognito stage). The modelling choice
that makes the job walkable: a node per stage, `s_<stage>` = the job INTERRUPTED right before the
next stage (the shape a backend restart leaves — `running`, the next stage `running`, the row
`bootstrapping`), so every stage is one edge driven by the REAL `execute_bootstrap_job` over the
stateful fake AWS of `test_workspace_bootstrap.py` (create-then-list returns what was created, so
a resumed stage really adopts), and the resume path is walked between every pair of stages. The
other nodes: `registered` / `probed` (a validate-only job exists — the split that makes
`DELETE` answer 200 vs 409 `in_use`), `failed_early` (the N17-obs112 denial at `validate-access`,
nothing recorded) / `failed_mid` (a foreign-bucket failure at `storage` after three stages — the
resume carries them forward, the purge's `resource_keys` names them), `ready`, `detached`,
`purged`. Oracles beyond the executor's: the WORKSPACE ROW digest (status + resource map; the
19 counted tables do not include `workspaces`), the fake's creation lists (nothing provisioned
twice across an interruption), and the secret oracle — a distinctive Global Bedrock API key value
must never appear in the job log, the job error, a stage detail or the row. Verify: 96 prime /
pair paths + 8 walks ≈ 55 s under `-n 8`; the two soak twins under `LAUNCHPAD_SOAK=1`. Making this
affordable surfaced a harness cost every job-shaped suite paid: SQLite's default
`synchronous=FULL` fsyncs every commit and the bootstrap job commits per log line — one 13-stage
path took 8.8 s; `tests/conftest.py` now sets `PRAGMA synchronous=OFF` per connection on the
temp ledger (durability across an OS crash is irrelevant there; journal mode untouched) and the
same path takes 1.3 s.

**The workspace fault world (`fault_seams_workspace.WorkspaceFaults`,
`test_graph_fault_walks_workspace.py`).** The lead's four injections per stage, each a
`faults.FAMILIES` family served at the stage's seam — the first AWS call the stage issues (`ecr
DescribeRepositories` for `validate-access`, `iam CreateRole`, the Identity provider read / create,
`s3 CreateBucket` / `ecr CreateRepository`, `codebuild CreateProject`, `CreateGateway`, the xray /
logs calls of the degrading stages) or the two service-level stubs (the registry default import,
the Skill Lab worker): **AccessDenied** (armed for the whole step so `retry_access_denied`'s
propagation retry sees it again) plus the **AssumeRole failure** variant (`at` suffix
`/assume_role`: the same `ClientError` with `operation_name == "AssumeRole"`, served at the
stage's first signed call — `sts:GetCallerIdentity` for `validate-access`); **region-unsupported**
(the new family `region_unsupported`: botocore `UnknownServiceError` — `ws_unknown_service` —,
`UnrecognizedClientException` — `_REGION_UNSUPPORTED_CODES`; since N17-obs200 the
`EndpointConnectionError` shape `ws_endpoint_unreachable` is served in the transport `timeout`
family instead — see the fix paragraph below); **interruption mid-stage** (the
`interrupted` family's `process_death`, fired AFTER the write for a create call — the process died
once the resource existed, so the resume must adopt, not re-create); and the transient classes
(`5xx`, `throttle`, `timeout`). `fault_dst == edge.dst` for every stage edge: the step fails,
proves the failure oracles, heals the seam, resumes and reaches the planned node (the recovery
converges — the walk continues as if the edge had succeeded). Oracles: **stage status persisted**
(job `failed`, stage `failed` with a detail that names the error — for `validate-access` the
stage's own sentence; a degrading stage `succeeded` with `unavailable · <code>` / `defaults not
imported (<class>)` and the job went on; an interrupted job `running` with the stage `running`);
**no half-recorded resource ids** (the map after a failure equals the map the previous stage left
— `registry` may hold its identifier before the best-effort import; after the resume no fake
resource was created twice); **job resumable** (`POST …/bootstrap` 202 again, the succeeded stages
carried forward); **no secret in the error text**. Three test shapes: the seeded fault walks
(`faults.params()`), the refusal × seam-faulted axis (every fake method armed — a workspace
refusal is decided on the ledger) and the per-stage **injection matrix** (one canonical edge per
stage × {access_denied, region_unsupported, interrupted, 5xx} × the stage's seams — 95 cells,
deterministic, in verify). Yield: **N17-obs132** (`register>run_validate@rt_read_timeout/
ecr.describe_repositories`, repro-hash beacaaf6 — `_probe` maps EVERY `BotoCoreError` to the
missing-endpoint verdict, so a transient `ReadTimeoutError` tells the operator "region not
supported; register the workspace in a region where …"; pinned strict + symptom-keyed). Everything
else held: the N17-obs112 denial text at every probe shape, the AssumeRole diagnostic at every
stage, the degrade details, every interruption-after-write adopted on resume, no secret anywhere.

**Fixed in `cn/r17-v` (r17-fixE, 2026-10-20) — N17-obs130.** `_PROBES` asks BY NAME for the
resources the later stages own: `describe_repositories(repositoryNames=[ecr_repo_name()])`
(`RepositoryNotFoundException` = usable), `batch_get_projects(names=[codebuild_project_name()])`
(an empty `projects` list = usable), `get_role(RoleName=<regional execution role>)`
(`NoSuchEntity` = usable); `_PROBE_NOT_FOUND_CODES` is the tolerated set and the stage logs
`<service> <Operation>: not created yet — the permission is in place`. The manifest's
`validate-access` needs are `ecr:DescribeRepositories` on `repository/launchpad-agents*`,
`codebuild:BatchGetProjects` on `project/launchpad-agent-builder*` and `iam:GetRole` on
`role/launchpad-agent-execution-role-{region}*` (the unfiltered `ListProjects` / `ListRoles` rows
and the `repository/*` resource are gone); the hub-region test asserts the EMPTY denial set and a
positive twin names the statement each probe is granted by (`EcrForLaunchpadRepos`,
`CodeBuildForLaunchpadProjects`, `IamForLaunchpadRoles`, `AgentCoreControlPlane`,
`CdkAndStackLifecycle`) while proving the three wildcard shapes stay denied — the fix is the call
shape, not a wider policy. The other-region (N17-obs112) denial set gains the by-name ECR probe
and keeps `ecr:DescribeRepositories` as the first refused probe; `5 services probed` is unchanged.

**Fixed in `cn/r17-v` (r17-fixE, 2026-10-20) — N17-obs131.** `IamAdoptAgentRoles` names the
four role shapes `workspace_iam.ensure_role` creates and re-adopts: the two existing patterns
(`role/launchpad-agent-*` — the per-agent roles and, by prefix, the regional execution role —
and `role/launchpad-skill-lab-worker-*`) plus `role/launchpad-gateway-role-*` and
`role/launchpad-codebuild-role-*` (with and without the `-<env>` suffix; the statement still
carries the one action on `launchpad-*` role names, nothing on `*`, and the broad
`IamForLaunchpadRoles` statement still lacks the action). The pin is replaced by
`test_hub_region_every_conditional_call_is_grantable_too` (the EMPTY denial set with the
conditional needs included) and `test_the_trust_policy_refresh_is_granted_for_every_role_the_bootstrap_adopts`
(each of the four `iam:UpdateAssumeRolePolicy` needs allowed BY `IamAdoptAgentRoles` in both
regions; the ops / memory-admin / spoke roles still denied); `infra/tests/test_ec2_template.py`
pins the four patterns. `HubAdminAccess=true` is no longer consulted for any hub-region need
(the admin test asserts it). The t1 stack must be UPDATED with the new template before a
resumed bootstrap's gateway / CodeBuild trust refresh succeeds there (deploy-ops).

**Fixed in `cn/r17-v` (r17-fixE, 2026-10-20) — N17-obs132.** `_probe` classifies instead of
mapping every `BotoCoreError` to the region sentence: `EndpointConnectionError` /
`UnknownServiceError` (by type name) and the `_REGION_UNSUPPORTED_CODES` → the region verdict,
at once; a transport timeout (`ReadTimeoutError`, `ConnectTimeoutError`), a throttle
(`ThrottlingException`, `SlowDown`, `TooManyRequestsException`, …) or a server-side 5xx (the
`InternalError` / `ServiceUnavailable` family, or any `ClientError` with an HTTP status ≥ 500 —
`_TRANSIENT_CODES` + `_is_transient`) → ONE bounded retry (`Timeouts.probe_attempts` = 2,
`probe_retry_delay_s` = 2 s through the injectable `sleeper`, logged `… transient failure (<cause>)
— retrying once in 2s`) and then `<service> <Operation> failed in <region> after 2 attempts
(<cause>) — transient — retry the bootstrap; neither the region nor the hub's permissions are in
question`, the cause being `core.errors.redacted_error` (the type / code and operation, never
botocore's message with the endpoint URL); every other `ClientError` is the denial sentence, final.
The 5xx class is a widening of the lead's list (the fault world's `s3_internal_error` cell showed
a 500 reported as "the hub's credentials must be able to provision this workspace", the same
misreport in the other direction). Tests: `TestValidateAccess` has one parametrised test per
class (three region shapes with no retry; six transient shapes retried once, the endpoint host
redacted, the sleeper called with the delay; a transient that clears on the retry passes; a
denial never retried; the two code sets disjoint); the fault world expects `<code>` +
`transient — retry` and NEITHER the region nor the denial sentence for every `5xx` / `throttle` /
`timeout` cell at `validate-access`; the strict pin is replaced by
`test_a_transport_timeout_at_a_probe_is_a_transient_verdict_not_a_region_one` (`run_validate`
and `validate_only`, the seam counted twice for the one retry) and the three
`KNOWN_SYMPTOMS` entries are deleted (the dict is empty). The graph's `ARCH_WS_VALIDATE` quote
and architecture.md / api.md carry the third verdict sentence.

**Fixed in `cn/r17-w` (r17-fixF, 2026-10-20) — N17-obs200.** The region verdict no longer
swallows `EndpointConnectionError`: botocore raises it AFTER the endpoint resolved (DNS lookup /
TCP / TLS to the host failed), so a supported region's endpoint during a network or DNS outage on
the hub produced "region not supported; register the workspace in a region where …".
`_REGION_UNSUPPORTED_CODES` now holds the endpoint-RESOLUTION failures only (`UnknownServiceError`,
`NoRegionError` — raised before any connect) plus the services' own codes; the new
`_ENDPOINT_UNREACHABLE_CODES` = `{EndpointConnectionError}` is retried exactly like a transient
(`_TransientProbeFailure(unreachable=True)`, the log line says `endpoint unreachable (<cause>) —
retrying once in 2s`) and then fails the stage with the FOURTH sentence — `<service> endpoint
(<service> · <region>) unreachable from this host after 2 attempts (<Operation>:
EndpointConnectionError) — network/DNS, not the region: check the hub host's egress and DNS
resolution for this service in this region, then retry the bootstrap` — the host redacted to the
service and the region (never the URL, never an account id), never the region sentence and never
the plain `transient — retry` one. Tests: `TestValidateAccess` gains the class-4 pair (retried once,
then named as network with the URL redacted; clears on the retry → the stage passes), the class-1
parametrisation swaps `EndpointConnectionError` for `NoRegionError`, and the code-set test proves
the three sets pairwise disjoint. The fault world keeps `region_unsupported` a region verdict by
replacing the `ws_endpoint_unreachable` shape in that family with `ws_unknown_service` (a new
`unknown_service` shape kind, `UnknownServiceError`), moves `ws_endpoint_unreachable` into the
transport `timeout` family (`STAGE_FAMILIES`), expects `EndpointConnectionError` + `unreachable
from this host` + `network/DNS, not the region` for the `endpoint` kind at `validate-access`, asserts
the plain transient sentence and the endpoint URL are absent, and adds the deterministic cell
`test_an_unreachable_endpoint_at_a_probe_is_a_network_verdict_not_a_region_one` (`run_validate` /
`validate_only`, the seam counted twice). The injection matrix stays 95 cells (two region shapes).
deploy.md §8.1 carries the verdict table.

**Fixed in `cn/r17-w` (r17-fixF, 2026-10-20) — N17-obs201.** The trust-refresh grant is two
statements with resource-tag Conditions: `IamAdoptAgentRoles` = `role/launchpad-agent-*` under
`StringLike iam:ResourceTag/launchpad:agent-id: "*"` (what `agent_iam.ensure_role` tags in its
`CreateRole` call) and the new `IamAdoptWorkspaceRoles` = the four bootstrap role shapes
(`role/launchpad-agent-execution-role-*` — named explicitly now instead of riding on the
`launchpad-agent-*` prefix —, `role/launchpad-skill-lab-worker-*`, `role/launchpad-gateway-role-*`,
`role/launchpad-codebuild-role-*`) under `StringLike iam:ResourceTag/launchpad:workspace: "*"` (what
`workspace_iam.ensure_role` tags in its `CreateRole` call — the tag is applied BEFORE any trust
write on every path: create carries `Tags=`, the retired-owner adopt re-stamps with `TagRole`
before `UpdateAssumeRolePolicy`, and the unverified adopt (N11-r3-bug5) now simply gets IAM's
`AccessDenied` when the role really is untagged). The split is forced by the tags themselves: the
per-agent roles carry `launchpad:agent-id`, not `launchpad:workspace`, so one Condition could not
cover both families. `tests/consistency/iam_policy_eval.py` models the Condition block for exactly
this shape (`Condition(operator, key, values)` on each `Statement`; `evaluate(…,
resource_tags=)`; `StringLike` / `StringEquals` on `iam:ResourceTag/<key>` ANDed within a statement,
a missing key false, any other operator / key `NotImplementedError` — never a silent allow);
`workspace_permissions.Need.tags` declares the tags the bootstrap's roles carry at call time and
`_role_needs` sets them on the four `UpdateAssumeRolePolicy` needs. Tests: the trust-refresh test
now expects `IamAdoptWorkspaceRoles` with the tags; `test_N17_obs201_the_hubs_cdk_made_twins_are_
denied_the_trust_refresh_by_iam_itself` (the CDK twins' `launchpad:managed`-only tags and bare roles
DENIED with no statement matching, the same names with `launchpad:workspace` allowed, the adopt
path's other actions still on the unconditional `IamForLaunchpadRoles`),
`…the_per_agent_roles_need_their_own_tag` (agent-id tagged allowed by `IamAdoptAgentRoles`; bare /
CDK-tagged / workspace-tagged denied; the CDK execution role denied through the agent glob too),
`test_evaluator_models_a_resource_tag_condition_and_refuses_the_rest`; `infra/tests/
test_ec2_template.py` pins both statements' exact Condition blocks, the pattern lists, that these two
are the ONLY `UpdateAssumeRolePolicy` grants and that the broad statement stays unconditional.
`test_hub_region_every_conditional_call_is_grantable_too` (the EMPTY hub-region denial set) still
holds with the tags evaluated. Deploy-ops: an IAM-only in-place stack update (deploy.md §8.2).


**Round 9 (r17-s, 2026-10-20 — same method, fresh inputs; the method's first zero-yield round).**
`LAUNCHPAD_LOOP_ROUND=9` (fault seeds 160–179 per family) and two seams: (1) a **seventh fault
world, `ProposalFaults`** (`test_graph_fault_walks_architect.py`) at the CLEAR job's AWS readbacks
(assistant-turns.md §10): `agent_remaining` (GetAgentRuntime inside `_wait_gone`),
`foreign_endpoints` (ListAgentRuntimeEndpoints), `delete_agent_cloud_resource` (DeleteAgentRuntime)
and the residue sweep's load-bearing role step, every recorded Runtime shape at each — the cloud
fixture of the purge suite replaced the readback wrappers, so the wrappers' own not-found swallow
(`purge._is_not_found`) is applied at the same layer by the fault stub (a not-found readback reads
as gone). Oracle = the step's exception is "recorded, retryable": the job `failed` with the named
blocker (`purge_step_failed`, `purge_agent_conflict` for a `ConflictException` on the delete,
`purge_role_remains`), the conversation rows and the Agent row kept (the ledger flips only after
the resource is read back gone), the checkpoint `payload.progress[agent:<id>]` carrying
`delete_requested` exactly when the delete was accepted, and the recovery oracle — a second DELETE
(a new job unioned with the previous pins) converging to `succeeded` without re-issuing
DeleteAgentRuntime when it was accepted. (2) the **Skill Lab subprocess harness**
(`test_graph_fault_probes_skilllab.py`): the stub CLI's task-question knobs `FAIL:<code>` and
`ERROR:infra|exec` — skill-lab-executor.md §6 + `verdict.all_rollouts_failed_reason` (N10v3-bug3):
every rollout failing is a FAILED job naming the first error and the infrastructure / exec split,
one scored rollout keeps `succeeded`, a non-zero exit is `failed` with the code. Gateway targets
were not probed: `identity_gateway_targets` keeps nothing in the ledger (AWS-only state — a mapped
envelope either way).

**Round-9 yield: 0.** Every seam held its contract (16 purge probes × 4 shapes, 180 architect fault
walks, 5 Skill Lab probes); soak numbers in the round-9 note of `loop_rounds.yaml`. **The
seed→walk mapping for rounds ≤ 8 changed on r17-s** (rev-r17 item): the generator's slot
carry-forward rule (a transition slot the diverged route cannot serve moves to the next
servable transition step instead of being filled from a refusal edge — `FaultWalk.slot_kinds`)
re-routes 27 of the 216 verify walks and 170 / 185 / 185 of the 1 080 round-6 / 7 / 8 soak walks of
the six earlier worlds (the first divergence is always the old generator serving a transition
slot on a refusal edge); the findings stay reproducible through their strict pins /
`KNOWN_SYMPTOMS` entries, and windows 6–8 were re-soaked once under the new generator on
2026-10-20 (numbers in the round-9 note). Per the stop
rule the zero-yield streak is **1/3** and **round 10 must switch method**: the ladder's next rung is
**time-shifted walks with clock injection** — the walk advances an injected clock between steps so
expiries, lockouts and retention sweeps fire mid-history (the session cookie's `expires_at`, the
sign-in lockout windows, the chat claim's stale timeout, the canary's `cleanup_endpoints_gone`
deadline, the purge's `AGENT_GONE_TIMEOUT_S`, the API-key creator offboarding, the log-retention
and staging sweeps), with the same worlds, oracles and shrinker.
