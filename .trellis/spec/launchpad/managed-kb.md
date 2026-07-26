# Managed Knowledge Bases — management + harness agent attach

## Scenario: changing KB management, the KB gateway, or how agents mount KBs

### 1. Scope / Trigger

Cross-layer contract between `frontend/src/pages/KnowledgeBases.tsx` (+
`frontend/src/pages/knowledge/`), the CreateAgent KB picker, `backend/app/
routers/knowledge.py`, `backend/app/services/knowledge.py`, `backend/app/
services/kb_gateway.py`, `backend/app/deployer/harness.py` and the CDK roles in
`infra/stacks/base_stack.py`. Touch this spec when you add a data-source
connector, change the gateway/target topology, or extend KB attach to another
method. Introduced by task `07-13-managed-kb`.

**Load-bearing AWS facts** (live-verified 2026-07-13, botocore 1.43.44):
- Managed KB = Bedrock KB `type: MANAGED` (`bedrock-agent` client):
  `CreateKnowledgeBase(name, roleArn, knowledgeBaseConfiguration={type:
  MANAGED, managedKnowledgeBaseConfiguration:{embeddingModelType: MANAGED}})`.
  Vector store/embeddings/reranking are service-managed. **roleArn is NOT
  validated at create** — a bad role only fails at ingestion.
- KB creation is async: CREATING→ACTIVE takes **1.5–3 min** (not seconds).
  `create_kb` waits ≤60 s (fast path creates the data source inline); otherwise
  it starts a BACKEND daemon thread (`_start_source_completion`) that polls to
  ACTIVE on its own client and then creates the data source, and returns
  `source_pending` for API compatibility. **The client must not replay it** —
  it used to (sessionStorage → DetailView), and leaving the page in that 1–3 min
  window lost the data source permanently, with the KB looking ACTIVE and healthy
  (`docs/issues/2026-07-26-kb-data-source-lost-on-slow-create.md`).
  `_create_data_source` is therefore idempotent: `_find_data_source_at` returns an
  existing source at the same bucket/prefix instead of adding a second connector,
  since fast path / background thread / manual `POST /data-sources` can race.
  DetailView shows a warning + `补建数据源` repair action whenever a KB is ACTIVE
  with zero data sources (covers a backend restart mid-wait and pre-fix KBs), and
  still auto-fires the FIRST ingestion when a data source is AVAILABLE with zero
  jobs.
- Data sources: `CreateDataSource(type: MANAGED_KNOWLEDGE_BASE_CONNECTOR,
  managedKnowledgeBaseConnectorConfiguration.connectorParameters = {type: S3,
  version: "1", connectionConfiguration:{bucketName, bucketOwnerAccountId},
  filterConfiguration:{inclusionPrefixes}})` + `vectorIngestionConfiguration.
  parsingConfiguration.parsingStrategy = SMART_PARSING`. Also async
  (CREATING→AVAILABLE 2–5 min); `StartIngestionJob` before AVAILABLE →
  ValidationException; concurrent sync → ConflictException.
  **`connectorParameters` is a botocore document: GetDataSource returns it as a
  JSON *string*** — `_parse_ds_location` handles both.
- Retrieval data plane: `bedrock-agent-runtime.retrieve(knowledgeBaseId,
  retrievalQuery, retrievalConfiguration={managedSearchConfiguration:
  {numberOfResults}})` (NOT vectorSearchConfiguration for managed KBs).
- Document listing: `ListKnowledgeBaseDocuments(knowledgeBaseId, dataSourceId,
  maxResults, nextToken)` works on managed KBs — per doc: S3 uri identifier,
  `status` (INDEXED/FAILED/…, `statusReason` on failures), `updatedAt` (index
  time). Size + upload time come from S3 (`knowledge._s3_object_meta`, one
  list_objects_v2 over the source prefix, capped 5k keys, best-effort — external
  buckets may deny). `GET /{kb_id}/data-sources/{ds_id}/documents?page_size=
  1..100&token=` is token-paginated; the DetailView `SourceDocuments` section
  (lazy expand per source) renders name/size/uploaded/status/indexed with a
  LOAD MORE appender.
- Gateway connector: `create_gateway_target(targetConfiguration.mcp.connector
  ={source:{connectorId:"bedrock-knowledge-bases"}, configurations:[…]})`,
  credential type `GATEWAY_IAM_ROLE` only. Two tool entries: `Retrieve`
  (parameterValues.knowledgeBaseId, ONE KB per target) and
  `AgenticRetrieveStream` (parameterValues.retrievers[] — multi-KB — plus
  REQUIRED `agenticRetrieveConfiguration`, `{}`-able). Target validation is
  async (~5–30 s), poll `get_gateway_target` to READY; FAILED carries
  statusReasons. A just-deleted target lists as DELETING and CANNOT be updated
  — `sync_agentic_target` waits for it to vanish then creates fresh.
- Tool names over MCP: `<target-name>___Retrieve` /
  `<target-name>___AgenticRetrieveStream`.
- **UpdateHarness omit=keep**: omitting `tools`/`skills` keeps the old values —
  `wrap_params_for_update` now always sends explicit `[]` so deselecting the
  last KB/tool actually detaches (bug found live; also affects plain tools).

### 2. Topology (product decision 2026-07-13)

One shared gateway `launchpad-kb-gw` (Cognito CUSTOM_JWT, same user-pool +
M2M clients as launchpad-gw, gateway role `launchpad-gateway-role`):
- per-KB `Retrieve` target `"{name-slug}-{kb_id_lower}"` — created lazily at
  first agent publish, deleted with the KB;
- per-agent `AgenticRetrieveStream` target `"agentic-{agent-slug}"` —
  retrievers bound to that agent's selected KBs; created/updated in the
  harness `provision` stage, deleted on agent delete or KB-less re-publish.
Soft isolation is accepted: every agent on kb-gw can list all targets; the
per-agent agentic target + system-prompt section are the steering mechanism.
Harness attaches kb-gw via `agentcore_gateway` tool `launchpad_kb_gw` with
OAuth CLIENT_CREDENTIALS (provider `launchpad-gw-m2m`, scope
`launchpad-gw/invoke`). Provision REBUILDS `create_params` after ensuring the
gateway (generate ran before kb_gateway_* existed on first attach).

**v1 is harness-only**: `AgentSpec._kb_needs_harness` rejects
`knowledge_bases` on other methods (container has no authenticated gateway
channel — mirrors "Gateway tools coming soon").

### 3. IAM

- `launchpad-gateway-role` += `bedrock:GetKnowledgeBase` + `bedrock:Retrieve`
  (knowledge-base/*) + `bedrock:AgenticRetrieveStream` (`*` — not
  resource-scopable).
- `launchpad-kb-role` (new, trusted by bedrock.amazonaws.com): reads artifacts
  bucket `kb/*`; external buckets get per-KB inline policy
  `launchpad-kb-{kb_id}` (mirrors `launchpad-fs-{agent}`), deleted with the KB.
- CDK output `KbRoleArn` → `resources.kb_role_arn`; kb gateway persisted
  lazily as `resources.kb_gateway_{id,arn,url}` by
  `ensure_kb_gateway_persisted` (write_config + get_settings.cache_clear).

### 4. Invariants

- Only `type == MANAGED` KBs are listable/addressable — the account holds
  VECTOR KBs that the connector cannot serve; `_require_managed` 404s them.
  (History: task `07-13-kb-unified-list` briefly surfaced non-managed KBs
  read-only in the list; river reversed that decision the same day after
  confirming the gateway connector's MANAGED-only constraint is an AWS hard
  limit — non-attachable KBs in the console added noise, not value. Reverted
  in `revert of 0828ade`; the PRD/journal under
  `.trellis/tasks/archive/2026-07/07-13-kb-unified-list/` records the
  original scope if it ever comes back.)
- `registry_console.ensure_default_records` reads only `resources.gateway_url`
  (launchpad-gw) — kb-gw targets must never become registry MCP records.
- KB delete: refuse with 409 `kb.has_attached_agents` (detail.agents) unless
  force; order = data sources → retrieve target (only if kb_gateway_id already
  exists — never provision during delete) → inline policy → KB.
- Upload files land at `kb/{kb_id}/{safe-filename}` in the artifacts bucket;
  uploads allowed when the KB has an artifacts-bucket source OR zero sources
  (pending slow-path creation).
- KB names: `^[0-9a-zA-Z][0-9a-zA-Z_-]{0,99}$` (no spaces) — frontend
  mirrors this in CreateView NAME_RE.

### 5. E2E scripts / evidence

`backend/scripts/e2e_knowledge_base.py` (KB chain: create/upload/sync/query
with content assertions, reuses `aurora-deck-docs`) and
`backend/scripts/e2e_kb_gateway.py` (gateway chain: targets, MCP tools/list +
tools/call, re-sync update path). Sample docs: `samples/kb_docs/`. Kept demo
resources: KB `aurora-deck-docs` (BL6ZKAVWFB) + harness agent `aurora-support`
+ gateway `launchpad-kb-gw`.
