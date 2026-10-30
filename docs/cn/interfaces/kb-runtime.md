# Interface: knowledge-base runtime library (N6 → N7)

Frozen 2026-10-09 (N6 Task 1; finalized 2026-10-09, Task 8).

Companion to `memory-runtime.md`: the retrieval tools a generated agent calls to
search a mounted Knowledge Base, backed by the same OpenSearch domain and Titan
embedder. Rendered from `app/templates/direct_kb_tools.py.tmpl` / `kb_support.py`;
N7's self-built Harness grafts the same tool functions.

## Mounted knowledge bases

```python
MOUNTED_KBS: list[dict] = [{"kb_id": "KB1A2B3C4D5E", "name": "Runbooks", "description": "..."}]
```

One entry per `spec.knowledge_bases[]` the agent was deployed with; literal, baked in
at generation time (not read from an env var — a KB's name/description can change
after deploy without redeploying every agent that mounts it, so the tools' retrieval
targets `kb_id` only).

## Tool contract (ruling R10 — k-NN only, no FM planner)

```python
kb_search(query: str, kb_id: str = "") -> str
kb_deep_search(query: str, kb_id: str = "") -> str
```

- `kb_search`: one k-NN query (`KB_RESULTS = 8` results) against the Titan embedding
  of `query`.
- `kb_deep_search`: a **wider multi-query k-NN** — the full question plus up to three
  clause-split sub-queries, 2× `KB_RESULTS`, results de-duplicated by
  `(doc_uri, chunk_index)`. No `AgenticRetrieveStream`, no FM query planner (that AWS
  Bedrock KB capability has no CN equivalent).
- `kb_id=""` searches every mounted KB and concatenates each KB's result block;
  a non-empty `kb_id` must name one of `MOUNTED_KBS` or the call returns the
  "not mounted" error text below instead of raising.
- `KB_DEEP_TOOL_NAME` is unchanged from the AWS-KB implementation (N7's
  `harness_convert.py` keys off this constant, so it needs no edit for this node).

Both tools return **plain text** (not a tool-error exception) in every case:

```
[<kb name>] N passage(s):
1. score=0.8421 · source=s3://bucket/kb/KB1A.../ds1/runbook.md
<chunk text>
2. score=0.7103 · source=s3://bucket/kb/KB1A.../ds1/other.md
<chunk text>
```

Error folds (same return-text contract, never an exception):
- `"[<kb name>] search failed: <reason>"` — an OpenSearch/Bedrock call failed.
- `"knowledge base '<kb_id>' is not mounted. Mounted: <comma-separated kb_ids>"` —
  an unknown `kb_id` was passed.
- `"kb_search needs a non-empty query."` — empty/whitespace-only `query`.

## Index and document shape

One OpenSearch index per knowledge base: `launchpad-kb-<kb_id lower>` — in a named
EnvName environment `launchpad-<env>-kb-<kb_id lower>`, since every environment shares one
OpenSearch domain (D17) — (k-NN, Titan v2, 1024 dims, cosine; the hub names it through
`app/core/naming.py::kb_index()`, the rendered tools through the library's
`launchpad_kb_index()`, both the same rule as `opensearch_http.kb_index_name(kb_id,
env_name)`; body `kb_index_body()`). One document per indexed chunk:

```json
{
  "kb_id": "KB1A2B3C4D5E", "ds_id": "DS1A2B3C4D", "doc_uri": "s3://bucket/kb/KB.../ds.../file.md",
  "doc_name": "file.md", "chunk_index": 3, "text": "<chunk text>", "embedding": [1024 floats],
  "metadata": {}, "indexed_at": "2026-10-09T12:00:00+00:00"
}
```

## Environment

| name | set when | value |
|---|---|---|
| `LAUNCHPAD_KB_OS_ENDPOINT` | `spec.knowledge_bases` non-empty and the substrate exists | the OpenSearch domain host (shared with Memory, no scheme) |

Embedding calls authenticate through `_resolve_bedrock_api_key()` — the same
mechanism `memory-runtime.md` §1 describes; no second credential path.

## IAM

Sid `KnowledgeSearch` (see `memory-runtime.md` §5): `es:ESHttpGet`, `es:ESHttpPost` on
`<domain>/launchpad-kb-<id>/*` (`launchpad-<env>-kb-<id>` in a named environment) for
every KB in `spec.knowledge_bases[]`, on the shared domain `lp-memory-<region>`.

## `existing` sources (ruling R18)

A knowledge base whose data source `mode == "existing"` must point at the workspace's
own artifacts bucket — the extractor Lambda and every agent runtime are granted that
bucket only, never an arbitrary external one. The hub backend rejects any other bucket
at creation time (`kb.external_bucket_unsupported`, 422); this runtime library never
needs to check it, since by the time an agent is generated the data source already
passed that validation.

The `prefix` is constrained the same way (N6 node-fix round): it must fall inside this
KB's own `kb/<kb_id>/` namespace, since that is the only place a member can
legitimately already have placed documents — the extractor Lambda's role grants
`s3:List*` on the whole bucket plus `GetObject` on `memory/*` and `kb/*`, so an
unconstrained prefix would let one KB's ingestion read another workspace's memory
events or another KB's documents. The hub rejects any other prefix (or an empty/`..`/
absolute one) at `kb.external_prefix_unsupported` (422); this runtime library never
needs to check it either, for the same reason.

## Mirror

The OpenSearch/Titan core these KB tools call through (`opensearch_http.kb_index_name`
(+ the env-resolving `launchpad_kb_index` the library defines just after the block),
`kb_index_body`, `knn_search`, `TitanEmbedder`) is the **same** mirrored module Memory
uses — one hub source, two byte-identical copies (ruling R15). There is no separate
KB-only copy to re-sync: see [memory-runtime.md](memory-runtime.md) §7 for the re-sync
snippet and the pinning test (`backend/tests/test_memory_core_mirror.py`).
