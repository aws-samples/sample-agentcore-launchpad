# Interface: features & capabilities (N1 → N2…N7, N12, frontend)

Frozen 2026-10-08.

## Feature enum (`backend/app/core/features.py`)
`FEATURES = ("agents", "chat", "registry", "memory", "knowledge_bases", "identity", "gateway", "observability", "workspaces", "users", "evaluation", "optimization")` — closed set; `Settings.features` must be a subset (validator). Default = all twelve.

**Amended 2026-10-10 (N12 E0):** `evaluation` (datasets, evaluators, runs, online evaluation, pipelines, insights — `/api/eval/**`) and `optimization` (runtime canaries — `/api/runtime-canaries/**`; the experiments / A-B routes `/api/experiments/**` retired, N13-obs2). Their routers are mounted in `app/main.py` only when the feature is on (a hidden module answers 404), the frontend nav groups carry `feature: "evaluation" | "optimization"` (W-E4), and `PATH_FEATURE["eval"] = PATH_FEATURE["evaluation"] = "evaluation"`.

**Amended 2026-10-14 (D20 P1-a/P1-d):** `policy` (self-hosted Cedar Policy — `/api/policies/**`,
the Governance → Policy tabs; `docs/cn/interfaces/policy-backend.md`). Default on. The router is
mounted only with the feature; `GET /api/capabilities.features` lists `policy` **only while the
Cedar engine is registered** (`policy_backend.backend_or_none()`), so a build with the flag on
but no engine hides the tabs instead of showing a 503 module. The response gains the additive
block `policy: {mode: off|log_only|enforce, active_policies: <n>, runtime_guard: bool}`
(`policy_store.capability_summary`) under the same condition. `GET /api/overview` lights
`services.policy` iff the engine is registered AND `mode != off` AND ≥ 1 policy is ACTIVE;
`service_detail.policy` = `"<mode> · <n> active"` (plan §2.6).

## Settings (`backend/app/core/config.py`)
- `region: str = "cn-northwest-1"` — where AgentCore and workspace resources live
- `model_region: str = "us-west-2"` — where Bedrock models are invoked (N2 adds the credential)
- `features: list[str]` — env `LAUNCHPAD_FEATURES='["agents","chat"]'` or yaml `features: [...]`

## GET /api/capabilities (MEMBER)
```json
{"features": ["agents","chat","registry","memory","knowledge_bases","identity","gateway","observability","workspaces","users","evaluation","optimization"],
 "methods": {"harness": {"builtin_tools": ["browser","code-interpreter"], "tool_types": ["a2a","builtin","gateway","mcp"], "tool_auth_modes": [],
                         "native_tools": ["file_operations","shell"], "protocols": ["http"], "memory": true, "knowledge_bases": true,
                         "byoc_kinds": [], "requires": ["artifacts_bucket"]},
             "zip_runtime": {"...": "..."}, "studio": {"...": "..."}, "byoc": {"...": "..."}}}
```
`methods` (round 16, additive) is each deploy method's creation-time option domain —
`deployer.pipeline.MethodCapabilities.as_dict()` as declared at its `register_method`; the
wizard disables what the current method does not list, `POST /api/agents` refuses it with
422 `agent.method_unsupported`. The exact current values are frozen in
`backend/tests/fixtures/method_capabilities.json` (`docs/cn/interfaces/testing.md` §6).

## Frontend (`frontend/src/lib/capabilities.ts`)
`useFeatures()` loads once per page load; `hasFeature(set, f)` is `true` while loading; `featureForPath(pathname)` maps the first segment after an optional `/v2` (`governance → gateway`, `connections → identity`, `knowledge-bases → knowledge_bases`, `create → agents`); `/`, `/announcements`, `/auth/return` are never gated. Both nav tables carry `feature`; `layout/FeatureGate.tsx` renders NotFound for a hidden module's URL.

## Backend boundaries (503 `feature.unavailable` until registered)
| feature | module | registration |
|---|---|---|
| memory | `app/services/memory_backend.py` (`MemoryBackend`) | `memory_backend.set_backend(impl)` in `app/main.py` (N6) |
| registry | `app/services/registry_backend.py` (`RegistryBackend`) | `registry_backend.set_backend(impl)` (N4) |
| knowledge_bases | `app/services/knowledge_backend.py` (`KnowledgeBackend`) | `knowledge_backend.set_backend(impl)` (N6) |
| policy | `app/services/policy_backend.py` (`PolicyEngine`) | `policy_backend.set_backend(CedarEngine())` in `app/main.py` (D20 P1-a); routers 503 `feature.unavailable {"feature": "policy"}` until then; the decision audit sink `policy_audit.install(CloudWatchDecisionSink.from_settings())` rides along when `policy_audit_enabled` (P1-d); e2e: `tests/e2e/cn/scenario/s11-govern.spec.ts` (scenario step s11, `docs/cn/reports/e2e-scenario-plan.md` §3; REQUIREMENTS §4.1 cases 32–37) |
| harness method | `deployer.pipeline.register_method("harness", …)` | N7 |
| evaluation (engine) | `app/evaluation/engine.py` (`EvaluationEngine`) | `engine.set_engine(impl)` in `app/main.py` (N12 W-E1); routers 503 `feature.unavailable {"feature": "evaluation"}` until then |
| evaluation (executor) | `app/evaluation/executor.py` (`EvalExecutor`) | `executor.set_runtime_executor(impl)` (N12 W-E1-3b); submit paths 503 `evaluation.worker_missing` until then; `eval_executor=inprocess` is dev / SCORE NOW only (409 `evaluation.executor_not_allowed` in prod) |

Unregistered → `AppError("feature.unavailable", "...", {"feature": "<name>"}, status_code=503)`.

## Protocol method lists

The three `Protocol` class bodies below are pasted verbatim from
`backend/app/services/{memory,registry,knowledge}_backend.py` so N4/N6 can read exact
signatures from this one file. See `docs/cn/interfaces/memory-backend.md`,
`registry-backend.md` and `knowledge-backend.md` for the fuller contract (call sites,
error/status semantics, and the Task-5 contract notes for N4/N6).

### `MemoryBackend` (`app/services/memory_backend.py`)

```python
class MemoryBackend(Protocol):
    """What a memory backend must provide. One method per console/runtime read or
    write; every method takes the workspace it targets first."""

    # ── console reads (routers/memory.py) ──────────────────────────────────
    def memory_overview(self, workspace: WorkspaceContext) -> dict[str, Any]:
        """``GET /api/memory/overview`` — landing-view summary of the memory store."""
        ...

    def get_strategies(self, workspace: WorkspaceContext) -> list[dict[str, Any]]:
        """Long-term strategies the store runs (used by ``resolve_namespaces``)."""
        ...

    def list_actors(
        self, workspace: WorkspaceContext, next_token: str | None = None,
        max_results: int | None = None,
    ) -> dict[str, Any]:
        """``GET /api/memory/actors`` — page of ``{items, next_token}``; each item carries
        the ``decode_actor()`` fields (``actor_id``, ``agent_id``, ``human_actor``, ``scoped``)."""
        ...

    def list_sessions(
        self, workspace: WorkspaceContext, actor_id: str, next_token: str | None = None,
        max_results: int | None = None,
    ) -> dict[str, Any]:
        """``GET /api/memory/sessions`` — page of ``{items: [{session_id, …}], next_token}``."""
        ...

    def list_events(
        self, workspace: WorkspaceContext, actor_id: str, session_id: str,
        include_payloads: bool = True, next_token: str | None = None,
        max_results: int | None = None,
    ) -> dict[str, Any]:
        """``GET /api/memory/events`` — page of short-term events (``{items, next_token}``);
        also read by ``services/observability.session_transcript``."""
        ...

    def resolve_namespaces(
        self, workspace: WorkspaceContext, actor_id: str,
        strategies: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /api/memory/namespaces`` — ``[{strategy_id, namespace, resolvable}, …]``
        for one actor; also used by ``/records`` and ``/records/search`` to derive a
        namespace from ``(actor_id, strategy_id)``."""
        ...

    def list_records(
        self, workspace: WorkspaceContext, namespace: str, strategy_id: str | None = None,
        next_token: str | None = None, max_results: int | None = None,
        include_superseded: bool = False,
    ) -> dict[str, Any]:
        """``GET /api/memory/records`` — page of long-term records in one namespace; records
        superseded / soft-deleted / expired by the merge strategy (r14-a) are hidden unless
        ``include_superseded``."""
        ...

    def search_records(
        self, workspace: WorkspaceContext, namespace: str, query: str, top_k: int = 5,
        strategy_id: str | None = None,
    ) -> dict[str, Any]:
        """``POST /api/memory/records/search`` — semantic retrieval over one namespace."""
        ...

    def list_extraction_jobs(
        self, workspace: WorkspaceContext, actor_id: str | None = None,
        session_id: str | None = None, strategy_id: str | None = None,
        status: str | None = None, next_token: str | None = None,
        max_results: int | None = None,
    ) -> dict[str, Any]:
        """``GET /api/memory/extraction-jobs`` — page of long-term extraction jobs."""
        ...

    # ── runtime/data plane (chat, invoke) ──────────────────────────────────
    def create_turn_event(
        self, workspace: WorkspaceContext, actor_id: str, session_id: str,
        prompt: str, answer: str,
    ) -> dict[str, Any]:
        """Record one USER/ASSISTANT turn as a short-term event (chat/invoke write path)."""
        ...

    def retrieve_records(
        self, workspace: WorkspaceContext, namespace: str, query: str, top_k: int = 3,
    ) -> list[dict]:
        """Long-term records relevant to ``query`` (runtime prompt enrichment)."""
        ...

    def session_memory_summary(
        self, workspace: WorkspaceContext, actor_id: str, session_id: str,
        memory_id: str | None = None,
    ) -> dict[str, Any]:
        """``GET /api/chat/{agent_id}/memory`` — the chat rail's short-/long-term summary
        for one (scoped actor, session); ``memory_id`` is the spec-pinned store, if any."""
        ...

    # ── resource lifecycle (routers/memory_resources.py) ───────────────────
    def list_memory_resources(self, workspace: WorkspaceContext) -> dict[str, Any]:
        """``GET /api/memory/resources`` — ``{items: [{id, …}]}`` of memory stores."""
        ...

    def get_memory_resource(self, workspace: WorkspaceContext, memory_id: str) -> dict[str, Any]:
        """``GET /api/memory/resources/{memory_id}`` (and the admin ``/adopt`` probe)."""
        ...

    def create_memory_resource(
        self, workspace: WorkspaceContext, name: str, description: str,
        event_expiry_days: int, strategies: list[str],
        namespace_keys: list[dict[str, Any]] | None = None,
        merge: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``POST /api/memory/resources`` — returns the created store detail (``id`` set);
        ``merge`` = the merge strategy v2 knobs the caller set (r14-a)."""
        ...

    def update_memory_resource(
        self, workspace: WorkspaceContext, memory_id: str, *,
        description: str | None = None, event_expiry_days: int | None = None,
        merge: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``PUT /api/memory/resources/{memory_id}`` — returns the refreshed detail; ``merge``
        is a partial patch of the merge strategy v2 knobs (r14-a)."""
        ...

    def delete_memory_resource(self, workspace: WorkspaceContext, memory_id: str) -> dict[str, Any]:
        """``DELETE /api/memory/resources/{memory_id}``."""
        ...
```

### `RegistryBackend` (`app/services/registry_backend.py`)

```python
class RegistryBackend(Protocol):
    """Records are returned in the platform contract (``recordId``, ``name``,
    ``description``, ``descriptorType`` ∈ A2A/MCP/AGENT_SKILLS/CUSTOM, ``status``,
    ``statusReason``, ``recordVersion``, ``descriptors``, ``createdAt``, ``updatedAt``) —
    the shape ``normalize_record`` produces and ``routers/registry._record_out`` reads."""

    def find_record(
        self, workspace: WorkspaceContext, name: str, descriptor_type: str | None = None,
    ) -> dict[str, Any] | None:
        """Exact-name lookup (deployer register stage, system skill registration)."""
        ...

    def upsert_record(
        self, workspace: WorkspaceContext, *, name: str, description: str,
        descriptor_type: str, descriptors: dict[str, Any], record_version: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Create or refresh by name → ``(record, created)``. Deployer register stage,
        ``POST /api/registry/records``, skill import/reimport."""
        ...

    def get_record(self, workspace: WorkspaceContext, record_id: str) -> dict[str, Any]:
        """``GET /api/registry/records/{record_id}`` — full record incl. ``descriptors``."""
        ...

    def wait_record_settled(
        self, workspace: WorkspaceContext, record_id: str, timeout_s: int = 60,
        sleeper: Any = time.sleep,
    ) -> dict[str, Any]:
        """Block until the record leaves CREATING/UPDATING (deployer register stage)."""
        ...

    def list_records(
        self, workspace: WorkspaceContext, descriptor_type: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /api/registry/records`` (publisher view, every status); Overview tiles;
        ``GET /api/registry/attachables`` and the zip deployer read ``APPROVED`` only."""
        ...

    def list_discoverable_records(
        self, workspace: WorkspaceContext, descriptor_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /api/registry/records/discoverable`` — consumer view (APPROVED only,
        summaries without ``descriptors``). ``ValueError`` on an unknown type."""
        ...

    def update_record(
        self, workspace: WorkspaceContext, record_id: str, *, description: str | None = None,
        descriptors: dict[str, Any] | None = None, record_version: str | None = None,
    ) -> dict[str, Any]:
        """``PUT /api/registry/records/{record_id}``; system skill roll-forward."""
        ...

    def submit_record(self, workspace: WorkspaceContext, record_id: str) -> dict[str, Any]:
        """``POST …/action {submit}`` → PENDING_APPROVAL; also deployer auto-submit."""
        ...

    def set_record_status(
        self, workspace: WorkspaceContext, record_id: str, status: str, reason: str,
    ) -> dict[str, Any]:
        """Primitive behind approve/disable/reject."""
        ...

    def approve_record(self, workspace: WorkspaceContext, record_id: str) -> dict[str, Any]:
        """``POST …/action {approve|publish}`` → APPROVED."""
        ...

    def disable_record(self, workspace: WorkspaceContext, record_id: str) -> dict[str, Any]:
        """``POST …/action {disable}`` → DEPRECATED (terminal; only delete remains)."""
        ...

    def reject_record(self, workspace: WorkspaceContext, record_id: str) -> dict[str, Any]:
        """``POST …/action {reject}`` → REJECTED (recoverable)."""
        ...

    def delete_record(self, workspace: WorkspaceContext, record_id: str) -> None:
        """``DELETE /api/registry/records/{record_id}``."""
        ...

    def search_records(
        self, workspace: WorkspaceContext, query: str, max_results: int = 20,
    ) -> list[dict[str, Any]]:
        """``GET /api/registry/records/search?q=`` — full-text search over records."""
        ...
```

### `KnowledgeBackend` (`app/services/knowledge_backend.py`)

```python
class KnowledgeBackend(Protocol):
    """What a knowledge-base backend must provide (routers/knowledge.py + deployer)."""

    def list_kbs(self, workspace: WorkspaceContext) -> list[dict[str, Any]]:
        """``GET /api/knowledge-bases`` — ``[{kb_id, name, description, status, …}]``;
        also the assistant catalog and the Create-Agent picker (``?status=ACTIVE``)."""
        ...

    def get_kb_detail(self, workspace: WorkspaceContext, kb_id: str) -> dict[str, Any]:
        """``GET /api/knowledge-bases/{kb_id}`` — detail incl. data sources and recent jobs;
        also the system-preset install's KB verification."""
        ...

    def create_kb(
        self, workspace: WorkspaceContext, name: str, description: str, source: dict[str, Any],
    ) -> dict[str, Any]:
        """``POST /api/knowledge-bases`` (202) — ``source`` is ``{mode, bucket, prefix}``."""
        ...

    def update_description(
        self, workspace: WorkspaceContext, kb_id: str, description: str,
    ) -> dict[str, Any]:
        """``PATCH /api/knowledge-bases/{kb_id}``."""
        ...

    def delete_kb(
        self, workspace: WorkspaceContext, kb_id: str, force: bool = False,
    ) -> dict[str, Any]:
        """``DELETE /api/knowledge-bases/{kb_id}`` — raises ``KBAttachedError`` (409) when
        agents still mount the KB and ``force`` is false."""
        ...

    def upload_files(
        self, workspace: WorkspaceContext, kb_id: str, files: list[tuple[str, bytes]],
    ) -> list[str]:
        """``POST /api/knowledge-bases/{kb_id}/files`` — returns the stored object keys."""
        ...

    def add_data_source(
        self, workspace: WorkspaceContext, kb_id: str, source: dict[str, Any],
    ) -> dict[str, Any]:
        """``POST /api/knowledge-bases/{kb_id}/data-sources`` (201)."""
        ...

    def delete_data_source(
        self, workspace: WorkspaceContext, kb_id: str, ds_id: str,
    ) -> dict[str, Any]:
        """``DELETE /api/knowledge-bases/{kb_id}/data-sources/{ds_id}``."""
        ...

    def start_sync(self, workspace: WorkspaceContext, kb_id: str, ds_id: str) -> dict[str, Any]:
        """``POST /api/knowledge-bases/{kb_id}/data-sources/{ds_id}/sync``."""
        ...

    def list_ingestion_jobs(
        self, workspace: WorkspaceContext, kb_id: str, ds_id: str,
    ) -> list[dict[str, Any]]:
        """``GET /api/knowledge-bases/{kb_id}/data-sources/{ds_id}/ingestion-jobs``."""
        ...

    def list_documents(
        self, workspace: WorkspaceContext, kb_id: str, ds_id: str, *,
        page_size: int = 50, token: str | None = None,
    ) -> dict[str, Any]:
        """``GET /api/knowledge-bases/{kb_id}/data-sources/{ds_id}/documents`` — a page
        ``{items, next_token}``."""
        ...

    def query(
        self, workspace: WorkspaceContext, kb_id: str, text: str, number_of_results: int = 8,
    ) -> list[dict[str, Any]]:
        """``POST /api/knowledge-bases/{kb_id}/query`` — the retrieval playground."""
        ...

    # ledger-side guards the deployer and uninstall use
    def attached_agents(self, workspace: WorkspaceContext, kb_id: str) -> list[str]:
        """Names of live agents whose spec mounts ``kb_id`` (delete guard)."""
        ...

    def refuse_if_attached_to_system_agent(self, workspace: WorkspaceContext, kb_id: str) -> None:
        """Raise when a system-managed preset mounts ``kb_id`` (system_agents/uninstall)."""
        ...
```

### `EvaluationEngine` (`app/evaluation/engine.py`, N12)

See `docs/cn/interfaces/evaluation-engine.md` §1 for the Protocol body, §5 for the
`EvalExecutor` seam and the backend ↔ worker job protocol.
