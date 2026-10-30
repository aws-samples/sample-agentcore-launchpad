# Interface: memory runtime library (N6 → N7)

Frozen 2026-10-09 (N6 Task 1; finalized 2026-10-09, Task 8).

The self-hosted Memory substrate (OpenSearch k-NN + S3 session snapshots + SQS
extraction, `docs/cn/GRAPH-LOOP-SPEC.md` §3 N6) needs a small library inlined into
every generated agent's `main.py` so the runtime can write session snapshots, enqueue
extraction, and recall long-term facts/preferences before answering. This library is
rendered from `app/templates/memory_runtime.py.tmpl` via
`app.templates.memory_support.render_memory_source()`. N7's self-built Harness grafts
the same source into its own template (§6 below) — the names and env vars on this page
are load-bearing for that graft and must not be renamed without updating both.

## 1. Environment

Set by `deployer/environment.py` from the agent's `AgentSpec` and the workspace's
`resources` map (mirrors `model-credential.md`'s table). No key material is ever an
env var — see the last row.

| name | set when | value |
|---|---|---|
| `LAUNCHPAD_MEMORY_ID` | `spec.memory.short_term or spec.memory.long_term` | the store id: `spec.memory.memory_id` when the spec pins one, else `launchpad_memory-<workspace_id>` |
| `LAUNCHPAD_MEMORY_BUCKET` | same as above | the workspace's `memory_bucket` (falls back to `artifacts_bucket`) |
| `LAUNCHPAD_MEMORY_PREFIX` | same as above | `memory` |
| `LAUNCHPAD_MEMORY_OS_ENDPOINT` | same as above, **and** the workspace carries `memory_os_endpoint` | OpenSearch domain host, no scheme (e.g. `search-lp-memory-abc.cn-northwest-1.es.amazonaws.com.cn`) |
| `LAUNCHPAD_MEMORY_QUEUE_URL` | same as above | the extraction SQS queue URL |
| `LAUNCHPAD_MEMORY_LONG_TERM` | same as above | `"1"` when `spec.memory.long_term`, else `"0"` |
| `LAUNCHPAD_KB_OS_ENDPOINT` | `spec.knowledge_bases` is non-empty **and** the substrate exists | same OpenSearch domain host (shared with Memory) |
| `LAUNCHPAD_ENV_NAME` | either of the two endpoints above is set **and** `Settings.env_name` is non-empty (D17) | the EnvName of the environment that deployed the agent (e.g. `t1`). The library prefixes its index names with it (`launchpad-t1-memory`, `launchpad-t1-kb-<id>`); unset/empty = the default environment's unprefixed names |
| `LAUNCHPAD_MEMORY_HALF_LIFE_DAYS` | long-term on **and** the store's `recall_half_life_days` ≠ 90 (r14-a) | recall recency half-life in days; absent = 90 |
| `LAUNCHPAD_MEMORY_TTL_DAYS` | long-term on **and** the store has a `long_term_ttl_days` (r14-a) | soft TTL in days: older records are never recalled; absent = keep |
| `LAUNCHPAD_MODEL_REGION` / `LAUNCHPAD_MODEL_CREDENTIAL` / `AWS_REGION` | always | per `model-credential.md` — unrelated to memory, but every Bedrock call this library makes (Titan embeddings) reuses them |

No key material lives in the environment: Titan embedding calls authenticate through
`_resolve_bedrock_api_key()` (already defined earlier in `main.py` for the agent's own
model calls), which exports `AWS_BEARER_TOKEN_BEDROCK` right before the `bedrock-runtime`
client is created — this library never performs a second credential exchange.

## 2. Module contract

Names a graft may rely on (all defined inside the inlined
`__LAUNCHPAD_MEMORY_SOURCE__` block, after `_resolve_bedrock_api_key()` /
`MODEL_REGION` / `BEDROCK_BEARER_ENV` are already defined):

```python
SnapshotSessionManager(actor_id, session_id)             # subclass of strands.session.S3SessionManager; prefix memory/<store>/<actor> (R5)
make_session_manager(actor_id, session_id) -> SnapshotSessionManager | None   # None when LAUNCHPAD_MEMORY_ID unset
memory_session_manager = make_session_manager            # legacy name kept for main.py.tmpl / N7 anchors
OpenSearchMemoryStore(actor_id).search(query, namespaces=None, top_k=3) -> list[{text, namespace, score, similarity, created_at, updated_at}]
OpenSearchMemoryStore(actor_id).session_summaries(query, top_k=3) -> list[{text, namespace, score, similarity, created_at, updated_at}]  # ACTIVE /summaries/<actor>/<session> records (strategy summarization, never _actor), v2-ranked, text decoded
OpenSearchMemoryStore(actor_id).actor_summary() -> str        # rolling /summaries/<actor>/_actor text, '' when absent
make_memory_store(actor_id) -> OpenSearchMemoryStore | None   # None unless LONG_TERM=1 and endpoint set
recall_context(actor_id, query, top_k=3) -> str           # "\n"-joined texts from /preferences/<actor>, /facts/<actor>, then ≤ top_k "Summary of an earlier conversation: …" lines (session summaries, ≤ 2400 chars of text), then the actor summary line; "" on any failure
LaunchpadMemoryHook(actor_id, session_id)                # strands.hooks.HookProvider; AfterInvocationEvent →
                                                         #   PUT memory/<store>/<actor>/session_<sid>/events/<ts>_<uuid8>.json (AgentCore-shaped turn)
                                                         #   SQS SendMessage {"kind":"memory_extract","store_id","actor_id","session_id","event_key","long_term"[,"extract_model"]}
make_memory_hooks(actor_id, session_id) -> list           # [] when memory is off
Agent(..., session_manager=make_session_manager(a, s), hooks=make_memory_hooks(a, s))
```

Every function is fail-soft: a missing env var or an AWS error never aborts the turn
(stderr log; returns `""` / `None` / `[]`).

**`extract_model` (F-LOAD-02, N17-obs533, 2026-10-30).** The hook's message carries the
`memory_extract` model-role row when the deployer wrote `LAUNCHPAD_MEMORY_EXTRACT_MODEL`
(compact JSON of the hub's `embedding_backend.extract_model_block()`, incl. the F-LOAD-03
`temperature` capability; written next to `LAUNCHPAD_MEMORY_QUEUE_URL` by
`deployer/environment.py`). `_memory_env()["extract_model"]` parses it (`None` when the
variable is absent, unparseable or not an object with a `model_id`, and then the message omits
the key exactly as before — the extractor Lambda falls back to its own
`LAUNCHPAD_EXTRACT_MODEL_ID`). The value is baked in at deploy time: a model-role change takes
effect for an agent on its **next redeploy**, while the hub's own `create_turn_event` messages
follow the role immediately. Tests: `tests/test_memory_runtime.py::
test_after_invocation_sends_the_deployed_extract_model_block`,
`…::test_after_invocation_omits_extract_model_without_a_usable_env_value`.

**Recall (merge strategy v2, r14-a).** `search` asks k-NN for `min(4 × top_k, 20)`
candidates per namespace, filtered to ACTIVE records (no `superseded_by`, `deleted_at` or
`expired_at`) and, with `LAUNCHPAD_MEMORY_TTL_DAYS`, to records updated within the TTL.
It re-ranks them with the core's `rank_records`: cosine × `0.5^(age / half-life)` ×
`max(confidence, 0.5)`. It keeps `top_k` per namespace, so the default is 3 preferences +
3 facts. `score` is that recall score. `recall_context` then appends up to `top_k` lines
`Summary of an earlier conversation: <text>` from `session_summaries` — the actor's ACTIVE
per-session summaries (`/summaries/<actor>/<session>`, strategy `summarization`, the
`_actor` namespace excluded), ranked by the same rule, `text` decoded from the extractor's
`{"summary": …}` JSON (plain text for an older record), their total text bounded to
`RECALL_SESSION_SUMMARY_MAX_CHARS` (2400 ≈ 3 × 120 words; the summary that crosses the
bound is cut with `…` and the rest dropped, and a remainder under 200 chars is not spent on
a cut line) — and last `Summary of earlier conversations: <text>` when the extractor has
written the actor's rolling summary (`/summaries/<actor>/_actor`). The session summaries
close the freshness gap found 2026-10-29: the extractor writes one per extraction, so the
next conversation sees the previous one at once, while the rolling summary only changes
every `actor_summary_interval` extractions (default 1 since the same fix; 5 before). The
generated templates still prepend the result under "Known user context". Pre-v2 records read as active with confidence 1.0.
**Agents deployed before r14-a keep their old rendered library** (no active-record filter,
no ranking): they recall superseded and soft-deleted records until they are redeployed.
See [memory-backend.md](memory-backend.md#merge-strategy-v2--r14-a-n14-obs1).

`SnapshotSessionManager` is a thin subclass of Strands' own `S3SessionManager`
(verified present in `strands-agents==1.47.0`) rather than a bespoke class — short-term
session state (per-agent message history) is Strands' own concern; this library only
adds the Launchpad-shaped turn event (§3) alongside it. `OpenSearchMemoryStore` is a
plain class, **not** a `strands.memory.MemoryStore` — that interface does not exist in
1.47.0 (ruling R6). The one behaviour it overrides (N17-obs528): opening a session first
probes `session.json` with `ListObjectsV2` (a 200 with no contents when the session is new)
and only reads/creates through the base `GetObject`/`HeadObject` when the key exists, so a
new chat's first turn no longer records two 404 error spans and a "failed" trace; 403/5xx
on the probe still raise.

## 3. Snapshot layout (S3) — R5

```
memory/<store_id>/<actor_id>/session_<session_id>/session.json              # Strands' own S3SessionManager state
memory/<store_id>/<actor_id>/session_<session_id>/agents/agent_<id>/messages/message_<n>.json   # Strands' own per-agent messages
memory/<store_id>/<actor_id>/session_<session_id>/events/<UTC %Y%m%dT%H%M%S%f>Z_<uuid8>.json    # one Launchpad event per turn
```

The event body (written by `LaunchpadMemoryHook`, read by the console and the
extractor) is AgentCore-shaped — the same body `opensearch_http.turn_event()` builds:

```json
{
  "eventId": "<uuid4 hex>",
  "eventTimestamp": "2026-10-09T12:00:00.123456+00:00",
  "actorId": "<actor_id>",
  "sessionId": "<session_id>",
  "payload": [
    {"conversational": {"role": "USER", "content": {"text": "<prompt>"}}},
    {"conversational": {"role": "ASSISTANT", "content": {"text": "<answer>"}}}
  ]
}
```

**No event for an unanswered turn (N17-obs304).** Strands fires `AfterInvocationEvent` in a
`finally`, so the hook also runs for a turn whose model call raised (`result` is `None`, the
history ends in the user's message). The hook writes an event, and enqueues its extraction,
only when the turn has non-empty assistant text: the last assistant text block of the turn,
or else the stripped `result`. A failed turn, an empty or whitespace-only reply, or a tool call
that never reached a final answer writes nothing. Before this rule, a failed turn left
`USER: <question>` plus an `ASSISTANT` part with text `""`. The summarizer then answered the
question itself and stored invented content as a long-term record. The extractor also guards
against events that pre-fix runtimes still write (see `memory-backend.md`, "Unanswered turns").

## 4. Long-term records

Index `launchpad-memory` — `launchpad-<env>-memory` in a named EnvName environment, since
D17 every environment of an account+region shares ONE OpenSearch domain
(`launchpad-memory-domain` stack) and is told apart by its index prefix; the rule is
`app/core/naming.py::index_name` on the hub and the byte-identical `index_name` in the
mirrored core block here, fed by `LAUNCHPAD_ENV_NAME` (k-NN, Titan v2, 1024 dims, cosine
— see `opensearch_http.py`), one document per fact/preference/summary:

```json
{
  "store_id": "launchpad_memory-<workspace>", "workspace_id": "<workspace>",
  "actor_id": "<actor_id>", "namespace": "/facts/<actor_id>", "strategy": "semantic",
  "session_id": null, "text": "<extracted text>", "embedding": [1024 floats],
  "created_at": "2026-10-09T12:00:00+00:00", "source_event_ids": ["<event id>", "..."]
}
```

Namespaces: `/facts/<actor>` (semantic), `/preferences/<actor>` (user_preference),
`/summaries/<actor>/<session>` (summarization). The reserved `/episodes/<actor>/<session>`
(episodic) namespace was removed in R13 (N13-obs4); nothing writes or reads it.

**Deleting records (N11 r3-C).** Two hub-side paths remove documents from this index, both
scoped on the stored `store_id` + `actor_id` fields and never on the bare agent id:
`DELETE /api/memory/records/{id}?actor_id=` (one document, `OpenSearchHttp.delete_doc` —
added to the mirrored core, §7 re-synced into this template and the Lambda copy) and the
agent-delete residue step `memory_records` (`delete_by_query` on `store_id` + prefix
`actor_id: <agent_id>__`). The runtime library never deletes; there is no per-record S3
object to remove (S3 holds §3 snapshots and turn events only).

## 5. IAM

Statements on the per-agent execution role (`agent_iam.py`), all built with
`RoleContext.arn(...)` (never a literal ARN, per `partition.md`):

| Sid | Actions | Resource |
|---|---|---|
| `MemorySnapshotObjects` | `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` | `<bucket>/memory/<store>/*` |
| `MemorySnapshotList` | `s3:ListBucket` | `<bucket>` with condition `s3:prefix = memory/<store>/*` |
| `MemoryExtractionQueue` | `sqs:SendMessage` | the extraction queue ARN |
| `MemorySearch` | `es:ESHttpGet`, `es:ESHttpPost`, `es:ESHttpPut`, `es:ESHttpHead` | `<domain>/launchpad-memory/*` (`<domain>/launchpad-<env>-memory/*` in a named environment, D17) |
| `KnowledgeSearch` | `es:ESHttpGet`, `es:ESHttpPost` | `<domain>/launchpad-kb-<id>/*` (`launchpad-<env>-kb-<id>`) for every mounted KB |

`<domain>` is the **shared** domain `lp-memory-<region>` (D17). The IAM resource scopes
the agent to its environment's index names and the FGAC role `launchpad_memory_rw[_<env>]`
(mapped to the agent-role pattern `role/launchpad-agent-*[-<env>]`) confines it to the
environment's `index_patterns()` — two environments on the same domain never see each
other's records.
| `ModelCredentialApiKey` | (unconditional, from N2) | the model-credential token-vault/workload-identity resources |

Titan embedding calls reuse the unconditional `ModelCredentialApiKey` grant (N2) — no
separate statement, since the resolved bearer key is the same one the agent's own model
calls use.

## 6. Graft (N7)

1. Render `__LAUNCHPAD_MEMORY_SOURCE__` with
   `app.templates.memory_support.render_memory_source()` **after** the block defining
   `_resolve_bedrock_api_key()`, `MODEL_REGION`, `BEDROCK_BEARER_ENV` — this library's
   Titan calls assume those names already exist in scope.
2. Pass `session_manager=make_session_manager(actor, session)` and
   `hooks=make_memory_hooks(actor, session)` to the `Agent(...)` constructor.
3. Prepend `recall_context(actor, prompt)` to the system prompt, exactly as
   `strands_agent/main.py.tmpl` does — recall is advisory text, not a tool call, so it
   costs nothing when memory is off (`recall_context` returns `""`).

## 7. Mirror (re-syncing the three copies — ruling R15)

`backend/app/services/opensearch_http.py` is the one hand-edited source. Its fenced
block (`# <launchpad-opensearch-core>` … `# </launchpad-opensearch-core>`) must be
byte-identical inside `backend/app/templates/memory_runtime.py.tmpl`, and the **whole
file** must be byte-identical to `backend/lambdas/memory_extractor/opensearch_http.py`
(`backend/tests/test_memory_core_mirror.py` pins both). The block carries no index-name
*constants* since D17 — only the env-parameterised rule (`index_name(base, env_name)`,
`memory_index`, `jobs_index`, `kb_docs_index`, `kb_index_name(kb_id, env_name)`, all with
a **required** `env_name`); the template resolves `LAUNCHPAD_ENV_NAME` once right after
the block (`LAUNCHPAD_ENV_NAME`, `MEMORY_INDEX`, `launchpad_kb_index()`), the Lambda does
the same in `handler.py`, and the hub uses `app/core/naming.py` (`tests/test_index_naming.py`
pins the rule parity). After editing the hub module, re-sync with:

```python
import re
from pathlib import Path

BACKEND = Path("backend")
FENCE = re.compile(r"^# <launchpad-opensearch-core>$.*^# </launchpad-opensearch-core>$",
                    re.M | re.S)

core_text = (BACKEND / "app/services/opensearch_http.py").read_text()
block = FENCE.search(core_text).group(0)

tmpl_path = BACKEND / "app/templates/memory_runtime.py.tmpl"
tmpl_path.write_text(FENCE.sub(block, tmpl_path.read_text()))

(BACKEND / "lambdas/memory_extractor/opensearch_http.py").write_text(core_text)
```

Then `cd backend && uv run pytest tests/test_memory_core_mirror.py -q` to confirm all
three copies agree. The same test pins a FOURTH copy since `cn/r17-t` (N17-obs120):
`backend/lambdas/memory_extractor/runtime_invoke.py` must equal
`backend/app/services/strands_eval/runtime_invoke.py` (the shared transient predicate the KB
ingest's embedding retry consults — stdlib + botocore only; re-sync with `cp`).

## 8. Deploy: CDK context overrides (N9)

`scripts/bootstrap.py::deploy_cdk` passes through `LAUNCHPAD_CDK_CONTEXT`
(`"k=v,k2=v2"` → repeated `cdk deploy --context k=v`) so a real deployment can override
CDK context without editing the script. Set it when:
- `cn-northwest-1` does not offer `t3.small.search` + FGAC + the pinned OpenSearch
  version (ruling R13b) — add `opensearch_instance_type=...` and/or
  `opensearch_version=...` to the same comma-separated value (read by the shared
  `launchpad-memory-domain` stack, D17).

The former `memory_master_arn=<EC2 ops role ARN>` (ruling R13c) is **gone with D17**: the
shared domain's FGAC master is the domain stack's own `launchpad-memory-admin-<region>`
role, exported as `MemoryAdminRoleArn` by every per-environment memory stack and assumed by
each environment's backend for every OpenSearch call (the EC2 ops role carries
`sts:AssumeRole` on `role/launchpad-memory-admin-*`). The CDK app ignores the old key.
`docs/cn/interfaces/deploy.md` already shows the `-c key=value` convention for other
context keys (e.g. `base_image_mirror_enabled`); these names are chosen to match it.

## A2A runtimes — the hub-signed memory actor (N16-obs79 / N16-obs90)

The HTTP template receives the memory actor (`<agent>__<human>`, `memory_backend.scoped_actor`)
in the Launchpad request envelope. An A2A runtime receives a bare JSON-RPC `message/send`, so
the hub puts the actor into the A2A `message.metadata`:

| key | value |
|---|---|
| `launchpad_actor_id` | the actor, `<agent>__<human>` |
| `launchpad_actor_sig` | `hex(HMAC-SHA256(key, "<contextId>\x1f<actor>"))` — `services/a2a_actor.sign_actor` |

`key` = `a2a_actor.actor_key(agent_id)` = `hex(HMAC-SHA256(session_secret, agent_id))`, derived
in-process from the console's `session_secret` (`LAUNCHPAD_SESSION_SECRET` / `data/session_secret`)
— nothing new is stored — and injected at deploy as the platform env key `LAUNCHPAD_A2A_ACTOR_KEY`
(protocol `a2a` only; counted in `PLATFORM_ENV_KEYS`, member budget 22).

The template (`strands_a2a_agent/main.py.tmpl`) binds context → actor off the request body in
its middleware **only** when the signature verifies (`hmac.compare_digest`) **and** the actor
starts with `<AGENT_ID>__` — the agent's ledger id from `LAUNCHPAD_AGENT_ID`, i.e. exactly the
`scoped_actor(agent.id, human)` shape the console signs (N16-obs90a: the first build compared the
agent **name** and refused every legitimate turn) — and does not contain `__a2a__`; `agent_factory` then keys the S3
snapshot manager, the turn-event hook and recall on it. Any other call — a peer-to-peer invoke
(the consumer template sends no metadata), a missing / wrong / replayed signature, a runtime
deployed without the key, an actor of another agent — keeps the per-context actor
`<AGENT_ID>__a2a__<contextId>` (the agent name only on a runtime without `LAUNCHPAD_AGENT_ID`) and logs one WARNING per context with the reason, never the
actor value. Threat: any holder of `InvokeAgentRuntime` on the runtime (`agent_iam._a2a_peer_arns`
grants it to every spec with an `a2a` ToolRef, member-authored BYOC included) could otherwise
name `<agent>__<other human>` and read / write that human's memory.

Rotation: a redeploy re-derives the same key from the same secret; a `session_secret` change
invalidates every A2A agent's key until its next redeploy (the hub signs with the new key, the
runtime still holds the old one → per-context actor + warning, never a wrong binding).
Existing A2A agents deployed before this key existed keep the N16-obs79 symptom (memory
events land under the per-context actor, the Memory rail shows nothing) **until they are
redeployed** — a host update does not fix a running A2A runtime; deploy-ops schedules one
redeploy per A2A agent after the update.
