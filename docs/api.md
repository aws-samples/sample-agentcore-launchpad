# Public API (/v1) / 公开 API

Every deployed agent is callable through the platform's `/v1` surface — the
same invoke chain the Chat playground uses. Interactive docs: **`/api/docs`**.

Auth: `X-Api-Key` header. Create a key in the console (Chat → API KEYS) or:

```bash
curl -s -X POST localhost:8000/api/apikeys -H 'Content-Type: application/json' \
  -d '{"name": "integration"}'
# → {"id": "…", "prefix": "lp_live_ab12…", "key": "lp_live_<full-key-shown-once>"}
```

Keys are stored **hashed (sha256)** — the full key is shown exactly once.
密钥仅创建时展示一次,后端只保存哈希。

## Sync invoke / 同步调用

```bash
curl -s -X POST localhost:8000/v1/agents/<AGENT_ID>/invoke \
  -H "X-Api-Key: $LP_KEY" -H 'Content-Type: application/json' \
  -d '{"prompt": "What is 2+2?", "session_id": null}'
# → {"agent":"…","text":"4","session_id":"…","latency_ms":1234}
```

## Streaming invoke (SSE) / 流式调用

```bash
curl -N -s -X POST localhost:8000/v1/agents/<AGENT_ID>/invoke-stream \
  -H "X-Api-Key: $LP_KEY" -H 'Content-Type: application/json' \
  -d '{"prompt": "Tell me a two-sentence story."}'
# event: meta   → {"session_id": "…", "mode": "stream"}
# event: delta  → {"text": "Once"} … (incremental chunks)
# event: done   → {"latency_ms": 2100}
```

Pass the returned `session_id` on the next call to continue the conversation
(session context + AgentCore Memory ride on it).

## Python

```python
import requests

BASE, KEY, AGENT = "http://localhost:8000", "lp_live_…", "<AGENT_ID>"

# sync
r = requests.post(
    f"{BASE}/v1/agents/{AGENT}/invoke",
    headers={"X-Api-Key": KEY},
    json={"prompt": "How many vacation days does EMP-1024 have left?"},
    timeout=120,
)
print(r.json()["text"])

# streaming (SSE)
with requests.post(
    f"{BASE}/v1/agents/{AGENT}/invoke-stream",
    headers={"X-Api-Key": KEY},
    json={"prompt": "Summarize our HR policy in one line."},
    stream=True, timeout=300,
) as stream:
    for line in stream.iter_lines(decode_unicode=True):
        if line.startswith("data:"):
            print(line[5:].strip())
```

Errors use the platform envelope `{code, message, detail}` — e.g.
`auth.missing_api_key` (401), `agent.not_active` (409), `agent.not_found` (404).

## Console Governance API

These `/api` routes back the authenticated console. They are not part of the
public `/v1` agent invocation contract.

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/governance/gateways` | Live MCP Gateway inventory |
| `GET` | `/api/governance/gateways/{id}` | Targets, actions, Registry, Engine, IAM, and attachability detail |
| `POST/DELETE` | `/api/governance/gateways/{id}/manage` | Add/remove only Launchpad management tags |
| `GET` | `/api/governance/gateways/{id}/registry-preview` | Gateway-level record diff and legacy matches |
| `POST` | `/api/governance/gateways/{id}/registry-import` | Create/reuse/update and submit; never approve |
| `POST` | `/api/governance/gateways/{id}/retire-legacy-records` | Explicit retirement after Gateway record approval |
| `POST` | `/api/governance/gateways/{id}/engine` | Create/adopt and attach an Engine in `LOG_ONLY` |
| `GET/POST` | `/api/governance/gateways/{id}/policies` | List or create `LOG_ONLY` policies |
| `PUT` | `/api/governance/gateways/{id}/policies/{policy_id}` | Update LOG_ONLY or create an ACTIVE-policy candidate |
| `POST` | `/api/governance/gateways/{id}/policies/{policy_id}/promote` | Evidence-gated activation/cutover |
| `POST` | `/api/governance/gateways/{id}/policies/{policy_id}/rollback` | Audited snapshot/candidate rollback |
| `POST` | `/api/governance/gateways/{id}/mode` | Gateway `LOG_ONLY`/`ENFORCE` transition |
| `POST` | `/api/governance/gateways/{id}/generations` | Start NL → Cedar generation for review only |
| `GET` | `/api/governance/gateways/{id}/generations/{generation_id}` | Poll generation status and read draft assets |
| `GET` | `/api/governance/gateways/{id}/decisions` | AWS decision projection or explicit unavailable state |
| `GET` | `/api/governance/gateways/{id}/audit` | Immutable local change journal |
| `GET` | `/api/governance/operations/{operation_id}` | Async operation status |

Policy and Gateway mutations return `202`:

```json
{"operation": {"id": "...", "status": "pending", "operation": "policy_create"}}
```

Generation start returns
`{"operation": …, "generation_id": …, "status": …}`; a generated asset is only
a draft for the editor and never activates a policy.

Poll the operation route until `succeeded`, `failed`, `partial`, or
`interrupted`. `interrupted` means a restart could not prove the AWS effect and
the operation must be retried explicitly — the backend never replays it.
Mutation requests carry the live timestamps and confirmations that apply to the
operation:

```json
{
  "expected_gateway_updated_at": "2026-07-16T09:00:00+00:00",
  "expected_policy_updated_at": "2026-07-16T09:01:00+00:00",
  "acknowledged_gateway_ids": ["gw-a", "gw-b"],
  "confirmation_name": "finance-gateway",
  "override_reason": null
}
```

Common conflict codes are `governance.gateway_not_managed`,
`governance.concurrent_change`, `governance.shared_engine_changed`,
`governance.iam_preflight_failed`, `governance.evidence_required`, and
`governance.registry_record_not_approved`.

## Console Memory API

`/api/memory/*` backs the read-only Memory console (console 05) over the shared
`launchpad_memory` singleton. Every route is a read: there is no endpoint that
writes events, deletes records, triggers extraction, or changes the memory
resource. See [architecture.md](architecture.md#the-memory-console-console-05).

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/memory/overview` | Resource config, long-term strategies, bounded actor count, sibling memories |
| `GET` | `/api/memory/actors` | Actors with the compound `<agent_id>__<human>` id decoded and the agent name resolved |
| `GET` | `/api/memory/sessions?actor_id=` | Sessions for one actor, joined to the ChatSession ledger when the console wrote them |
| `GET` | `/api/memory/events?actor_id=&session_id=` | Short-term events; conversational payloads carry role + full text, blobs only a byte count |
| `GET` | `/api/memory/namespaces?actor_id=` | Strategy namespace templates with `{actorId}` substituted, plus a `resolvable` flag |
| `GET` | `/api/memory/records?actor_id=&strategy_id=` or `?namespace=` | Long-term records for the resolved namespace |
| `POST` | `/api/memory/records/search` | Semantic retrieval (`{query, actor_id, strategy_id?, namespace?, top_k}`) with relevance scores |
| `GET` | `/api/memory/extraction-jobs` | Failed (retry-eligible) extraction jobs, filterable by `actor_id`/`session_id`/`strategy_id`/`status` — **not surfaced in the console**; AWS's `status` enum is `FAILED` only, so a healthy resource returns an empty list |

Every list route accepts and returns `next_token` (AWS pages at 100 items) and
accepts `max_results` (clamped to 100) — nothing is capped silently. Namespace
resolution order on `/records` and `/records/search`: an explicit `namespace`
wins, otherwise it derives from `actor_id` (+ optional `strategy_id`).

Error codes: `memory.not_configured` (409, bootstrap has not run — except
`/overview`, which instead returns `{"configured": false, …}` so the page can
render a setup state), `memory.namespace_required` (400, no namespace could be
derived), `memory.unavailable` (502, the underlying AWS call failed).

## Console Accounts API

`/api/auth/*` gates the console and `/api/users/*` manages the accounts behind
it. Neither surface touches AWS. See
[architecture.md](architecture.md#console-authentication-and-accounts).

| Method | Path | Auth | Result |
|---|---|---|---|
| `GET` | `/api/auth/status` | open | `{auth_required, authenticated, registration_enabled, username, role, email, account_expires_at}` — identity fields are null until authenticated |
| `POST` | `/api/auth/login` | open | Sets the `launchpad_session` cookie (12h, clamped to the account validity) and echoes the identity |
| `POST` | `/api/auth/register` | open | `201` — creates a `member` account valid for `auth_registration_valid_days` (default 7) |
| `POST` | `/api/auth/logout` | session | Clears the cookie |
| `GET` | `/api/users?q=&status=all\|active\|expired\|disabled&limit=&offset=` | admin | Paged account list with derived `state` / `days_remaining` |
| `GET` | `/api/users/stats` | admin | Totals, `expiring_soon` (≤3 days), 7-day registration/sign-in counts, a 14-day registration series, top email domains |
| `PATCH` | `/api/users/{id}` | admin | Any of `status`, `role`, `extend_days`, `expires_at` (`null` = never expires), `password` (`null` = generate and return once) |
| `DELETE` | `/api/users/{id}` | admin | Removes the account |

Registration error codes: `auth.registration_disabled` (400, gate off or
registration disabled), `auth.invalid_username` / `auth.invalid_email` /
`auth.email_domain_blocked` / `auth.weak_password` (400),
`auth.username_taken` / `auth.email_taken` (409).

Sign-in error codes: `auth.invalid_credentials` (401), plus
`auth.account_disabled` / `auth.account_expired` (401) once the submitted
credentials themselves are correct.

Session and role errors: `auth.required` (401 — missing, tampered, or expired
cookie, and also an account that has since been disabled, expired, or deleted),
`auth.forbidden` (403 — member session on `/api/users*`), `users.not_found`
(404).
