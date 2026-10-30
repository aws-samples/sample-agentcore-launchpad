# E06 Memory 「记忆」 (section `memory`, `/v2/memory`) — acceptance standard

**Risk score:** 9.5 (card: 3 / 3 / 3 / 0.5; R2 OpenSearch / extractor upstream, R9 record deletion rights) → negatives ≥ 6 · **Recorded build:** to fill · **Status:** draft

## 1. Scope and roles
- **Routes (`route_policy.py`):** `GET /api/memory/overview`, `…/actors`, `…/sessions`, `…/events`, `…/namespaces`, `…/records`, `POST …/records/search`, `GET …/extraction-jobs`, `GET …/resources`, `GET …/resources/{id}` = `MEMBER`; `DELETE …/records/{record_id}` (handler adds admin-or-agent-creator), `POST/PUT/DELETE …/resources[/{id}]` = `perm:memory.manage`; `POST …/resources/{id}/adopt` = `ADMIN`.
- **Backend (CN):** self-hosted substrate — S3 snapshots for short-term events, OpenSearch k-NN for long-term records, extractor Lambda fed by SQS (`memory-runtime.md`, `memory-backend.md`). Strategy catalogue **after r13-c** = `semantic` (`/facts/{actorId}`), `user_preference` (`/preferences/{actorId}`), `summarization` (`/summaries/{actorId}/{sessionId}`); `episodic` is gone (N13-obs4). Actor = `<agent_id>__<username>`; the workspace default store is shared by every agent that does not pin a managed memory.
- **Roles:** `demo-admin` reads, deletes records, manages resources; `hr-intern` reads everything, may delete records only of agents they created, may create resources (default-granted perm). Test ids: `v2-memory-actors`, `v2-memory-sessions`, `v2-memory-event`, `v2-memory-longterm`, `v2-memory-query`, `v2-memory-open-chat`, `v2-memory-res-*`.
- **Out of scope:** Knowledge Bases (E17), the Chat rail itself (E04 AC-06), memory IAM bootstrap (deploy).

## 2. Capabilities and pass criteria (positive)
| id | action | observable pass criteria | evidence |
|---|---|---|---|
| AC-E06-01 | Chat turn 「上海办公室，2024-03 入职，工号 CC-7788」 → Memory page | `GET /api/memory/actors` lists `<wb006_id>__demo-admin` with the agent name resolved; `GET …/sessions?actor_id` lists the session; `GET …/events` count equals the Chat rail's `event_count`; UI `v2-memory-event` rows render role/text turns | actors.json, events.json, shot.png |
| AC-E06-02 | Long-term extraction | ≤ 10 min after the turn `GET …/records?actor_id&strategy_id=semantic|user_preference` returns ≥ 2 records in total; namespaces `/facts/<actor>` and `/preferences/<actor>`; `decode_record_text` yields a human line (no serialized object on the page) | records.json ×2, timing.txt |
| AC-E06-03 | New session recalls | a fresh session asking for office / join date / employee id recalls 2/2 planted facts (k-NN order may vary — soft) | history.json |
| AC-E06-04 | overview · short-term · long-term tabs | overview lists the store's 3 strategies, `actor_count` (+ `actor_count_truncated` flag), event expiry; `GET …/namespaces?actor_id` resolves `{actorId}` and flags `{sessionId}` templates `prefix`; `POST …/records/search {query, actor_id, strategy_id, top_k}` returns scored hits | overview.json, namespaces.json, search.json |
| AC-E06-05 | Delete one record | UI row Delete → list −1; `DELETE …/records/{id}?actor_id=<actor>` 200 → −1 again; the deleted id is gone from `GET …/records` and from search | before/after records.json |
| AC-E06-06 | Resources | list marks the workspace default and names the agents pinning each memory; `POST …/resources {name "hr_memory_test", strategies ["semantic"]}` 201 with `created_by`; `PUT` description / `event_expiry_days` reflected; `DELETE` 200; the strategy picker offers exactly 3 options | resources.json, shot.png |
| AC-E06-07 | Scope on camera | the same human talking to two agents yields two actors; `hr-intern`'s session is a third actor; the Chat rail's OPEN IN MEMORY deep link lands on the exact `actor` + `session` | shot.png |

## 3. Negative / boundary cases (≥ 6)
| id | input | expected | source | risk |
|---|---|---|---|---|
| NC-E06-01 | anonymous read; member with `perm:memory.manage` revoked on `DELETE …/records/{id}` or `POST …/resources` | `401 auth.required`; `403` | `test_route_policy::test_fully_revoked_member_gets_403`, `test_memory_ownership::test_route_policy_gates_memory_writes`, `test_memory_record_delete::test_route_is_gated_on_memory_manage` | R9 |
| NC-E06-02 | `DELETE …/records/{id}` without `actor_id`; `hr-intern` deletes a record of the admin's agent; actor not scoped to any agent | `422 validation.invalid_request`; `403 memory.record_forbidden {actor_id, agent_id}`; unscoped actor → admin only (same 403) | `routers/memory.py::_require_record_owner`; `test_memory_record_delete::test_member_may_delete_only_records_of_agents_they_own` | R9 (irreversible delete) |
| NC-E06-03 | record id with another actor's `actor_id` | `404 memory.not_found` "memory record … not found for this actor" — the record is actor- and store-scoped | `services/memory_selfhosted.py`; `test_memory_record_delete::test_delete_record_is_actor_and_store_scoped` | R9 |
| NC-E06-04 | create resource `name "1bad"` or 49 chars; `strategies ["episodic"]`; duplicate name | `422 validation.invalid_request` (`^[a-zA-Z][a-zA-Z0-9_]{0,47}$`); `422 memory.unknown_strategy {strategies}` (post-R13); `409 memory.name_exists {name}` | `routers/memory_resources.py`, `services/memory_selfhosted.py`; `test_memory_selfhosted::test_create_refuses_a_duplicate_name_and_unknown_strategies` | deletion regression (N13-obs4) |
| NC-E06-05 | `event_expiry_days 2` or `366` on create; `PUT` with neither `description` nor `event_expiry_days` | `422 validation.invalid_request` (create `ge 3 le 365`; update `ge 7 le 365`, "provide description and/or event_expiry_days") | `routers/memory_resources.py` | input hygiene |
| NC-E06-06 | per-id route on a memory this workspace does not manage; `hr-intern` `POST …/adopt` | `404 memory.not_managed {memory_id}` (same answer as a foreign workspace's id); `403` (ADMIN route) | `services/memory_ownership.py::require_managed`; `test_memory_ownership::test_per_id_routes_refuse_an_unmanaged_memory` | R9 |
| NC-E06-07 | delete the workspace default store; delete a store an agent pins | `409 memory.default_protected`; `409 memory.in_use` | `services/memory_selfhosted.py::delete_memory_resource`, `routers/memory_resources.py` | data loss guard |
| NC-E06-08 | `GET …/sessions` without `actor_id`; unknown actor or session; `GET …/records` with an unresolvable namespace | `422`; `200` with an empty list (**not** 404); `400 memory.namespace_required` | `test_memory_selfhosted::test_unknown_actor_or_session_lists_empty_not_404`; `routers/memory.py::_resolve_namespace` | expectation setting |
| NC-E06-09 | **fault injection**: OpenSearch 5xx / 429; substrate endpoint missing; other botocore failure | `503 memory.backend_unavailable`; `503 memory.not_provisioned`; `502 memory.unavailable` — the page shows the error state, never a blank | `core/errors.py`, `test_memory_record_delete::test_delete_route_maps_opensearch_outages_to_503`, `test_memory_selfhosted::test_every_substrate_method_503s_memory_not_provisioned_without_an_endpoint` | R2 |
| NC-E06-10 | a pre-R13 store row that still lists `episodic` | loads with the entry ignored (one `ignoring retired strategies` log line); the console shows 3 strategies | `test_memory_selfhosted::test_legacy_store_listing_episodic_loads_without_it` (cn/r13-c) | deletion regression |

## 4. Evidence
Per TEMPLATE §4. Mandatory here: `timing.txt` (turn `done` → first ≥ 2 records), `records.json` per strategy before/after the delete, `ledger.redacted.json` of `managed_memories` for AC-06, extractor Lambda log lines for the recorded actor (`cw/extractor-<actor>.jsonl`). No secrets are involved.

## 5. Automation mapping and GAPs
| case | existing automation | status |
|---|---|---|
| AC-01/02/03 | `tests/e2e/cn/06-memory-longterm.spec.ts::case 6` (≥ 2 records ≤ 10 min, recall 2/2), `scenario/s05-use::s05b` | covered (needs HR facts fixtures) |
| AC-04/05 | `20-memory-admin.spec.ts::case 20` (rail ↔ page, UI delete −1, API delete −1); `test_memory_selfhosted.py` (22), `test_memory_record_delete.py` (9) | covered |
| AC-06, NC-04/05/07 | `test_memory_selfhosted::test_create_update_delete_a_store`, `::test_create_refuses_a_duplicate_name_and_unknown_strategies`, `::test_the_default_store_is_synthesized_until_edited` | covered (API); **GAP**: no e2e drives the Resources tab (create / edit / delete, 3-option picker) → new spec `51-memory-resources` |
| NC-01/02/03/06 | `test_memory_ownership.py` (15), `test_memory_record_delete.py`, `test_route_policy.py` | covered (unit); member-identity e2e **GAP** |
| NC-08 | `test_memory_selfhosted::test_unknown_actor_or_session_lists_empty_not_404` | covered |
| NC-09 | `test_memory_selfhosted::test_opensearch_errors_surface_as_503_backend_unavailable_through_the_api` | covered (unit); live injection **GAP** → Q4a `FaultyOpenSearch` |
| NC-10 | cn/r13-c test | covered once merged |

## 6. Failure classification
`ENV`: extraction > 10 min (SQS / Lambda lag; check `GET …/extraction-jobs` and the extractor log group — N10-bug2 / N11-r3-bug2 history), OpenSearch 503, k-NN order jitter on recall (soft, not a failure if 2/2 within top_k). `TEST`: 60 s waits on `v2-memory-*`. Else `PRODUCT`.

## 7. Open rulings
- Members may create billable memory resources by default (`perm:memory.manage` default-granted); keep or flip the default — lead decision (also affects E19).
- `docs/architecture.md` §Memory still describes the upstream AgentCore Memory backend (`memory.not_configured 409`); CN answers `memory.not_provisioned 503`. Standards follow the CN code.

## 8. Merge strategy v2 rows (r14-a, N14-obs1 — appended 2026-10-18)
| id | action | observable pass criteria | evidence |
|---|---|---|---|
| AC-E06-08 | Chat 「我在上海办公室」, later 「我调到杭州办公室了」 | ≤ 10 min after the second turn `GET …/records?actor_id&strategy_id=semantic` lists the Hangzhou fact **only**. With `include_superseded=true` the Shanghai fact is listed with `superseded_by` = the Hangzhou record id, or it was rewritten in place (`last_op: update`, same `record_id`, newer `updated_at`). A new session recalls Hangzhou, never Shanghai | records.json ×2, history.json |
| AC-E06-09 | Chat 「忘掉我养狗这件事」 after a planted dog fact | the fact disappears from the listing and from search; with `include_superseded=true` it shows `deleted_at` (soft). The extractor job counts `deletes: 1` | records.json, job.json |
| AC-E06-10 | V2 long-term tab → **Show superseded** toggle | off: only active rows. On: superseded / deleted / expired rows appear with a State tag; the record drawer shows confidence, last operation, superseded-by / supersedes. The Chat 会话记忆 rail never lists a superseded record | shot.png ×2 |
| AC-E06-11 | Resources → edit the default store → **Merge strategy** card | the five knobs show their defaults (90 / 200 / 100 / keep forever / 5). Saving half-life 30 shows it on the detail; out-of-bounds input disables Save with an inline error; `PUT {merge: {max_facts_per_actor: 5}}` answers `422` | detail.json, shot.png |
| AC-E06-12 | 5 extractions for one actor (default interval) | `/summaries/<actor>/_actor` exists (`metadata.kind: actor_summary`, ≤ 150 words); the next turn's recall appends `Summary of earlier conversations: …` | records.json, extractor log |

| id | input | expected | source | risk |
|---|---|---|---|---|
| NC-E06-11 | legacy index (pre-v2 mapping, pre-v2 docs) then `make bootstrap` | the bootstrap reports `mapping launchpad-memory v2 fields +…` once and reused afterwards. Legacy records list as `active: true`, `confidence: 1.0`, with nothing reindexed | `test_memory_merge_v2::test_ensure_mapping_adds_the_v2_fields_once_and_legacy_docs_read_as_active` | upgrade regression |
| NC-E06-12 | model `delete` with an unknown target or confidence < 0.6; `update` naming no candidate | delete ignored with a job warning; update becomes `add`. No record is ever hard-deleted by the extractor | `test_memory_merge_v2::test_delete_is_soft_and_needs_a_known_target_and_confidence`, `::test_update_or_supersede_without_a_known_target_degrades_to_add_and_noop_is_counted` | silent data loss |

Automation: `backend/tests/test_memory_merge_v2.py` (28), `frontend/src/v2/pages/memory/LongTermTab.test.tsx`, `frontend/src/lib/memory.test.ts`. **GAP**: no live e2e spec yet for AC-08..12 → spec `06-memory-longterm` case 6b (lead to schedule).
