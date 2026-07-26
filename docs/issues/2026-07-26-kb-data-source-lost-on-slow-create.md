# Managed KB created in upload mode can end up permanently without a data source

| | |
|---|---|
| **Status** | **Fixed 2026-07-26** |
| **Severity** | High (silent data loss of intent — the KB looks fine and is unusable) |
| **Component** | `backend/app/services/knowledge.py`, `frontend/src/pages/knowledge/{CreateView,DetailView}.tsx` |
| **Affected area in Launchpad** | Knowledge Bases → 创建知识库 → 上传文件 |
| **Date recorded** | 2026-07-26 |

## Summary

Creating a managed knowledge base with an uploaded file left the KB `ACTIVE`
with **zero data sources**: ingestion never started, the uploaded PDF sat
unreferenced in `s3://launchpad-artifacts-…/kb/<KB_ID>/`, and nothing in the UI
indicated a problem. The KB was permanently unusable — an agent could mount it
and retrieve nothing.

Reproduced on the remote environment (us-east-1, KB `HINWFLTPHS`) while running
the hands-on lab: the KB took ~90 s to become ACTIVE and the operator switched to
another console page during that window.

## Root cause

`create_kb` waits up to 60 s for the KB to leave `CREATING`. KB creation takes
1.5–3 min, so the common path is the slow path, which returned `source_pending`
and handed the remaining work to the **browser**:

```python
status = _wait_kb_active(client, kb_id)          # 60 s window
if status == "ACTIVE":
    _create_data_source(client, kb_id, source)   # fast path
    return get_kb_detail(kb_id)
detail["source_pending"] = source                # client must replay
```

`CreateView` stashed the source in `sessionStorage`; `DetailView` replayed
`POST /data-sources` on mount once the KB turned ACTIVE. Navigating away,
reloading elsewhere or closing the tab in that 1–3 minute window dropped the
replay for good. `upload_files`' own comment already acknowledged the ordering
hazard ("files may land ahead of it").

## Fix

1. **Backend owns completion.** The slow path now starts a daemon thread
   (`_start_source_completion`) that polls the KB on its own client and creates
   the data source once it is ACTIVE, mirroring how deploys already run
   off-request. `source_pending` is still returned for API compatibility.
2. **Idempotent creation.** `_create_data_source` first looks for an existing
   data source at the same bucket/prefix (`_find_data_source_at`) and returns its
   id instead of adding a second connector — the fast path, the background
   thread, a manual `POST /data-sources` and any stale client can all race.
3. **UI backstop.** The KB detail view shows a warning when a KB is `ACTIVE`
   with zero data sources, with a `补建数据源` button. This covers the one case a
   thread cannot — the process restarting mid-wait — and repairs KBs created
   before this fix.
4. **Client replay removed.** `sessionStorage` hand-off and `pendingSourceKey`
   are gone; two writers only created races.

Why not a resumable ledger job: knowledge bases have no ledger table (AWS is the
source of truth and the console reads them back live), so a table added purely to
carry a two-minute hand-off would cost more than the failure it prevents. The UI
banner covers the residual case.

## Tests

`backend/tests/test_knowledge_kb.py`:

- the background completion creates the source once the KB turns ACTIVE
- a second attempt at the same location is a no-op (no duplicate connector)
- a KB that goes `FAILED` mid-wait is abandoned without a create call

## Guide impact

`docs/lab/04-capabilities.md` now states that the data source is finished in the
background (so leaving the page is safe) and documents the "ACTIVE but zero data
sources" symptom plus the repair button in its FAQ.

## How it was found

Verifying `docs/lab/` end to end on the remote prod environment
(`agentops_launchpad`, us-east-1). Full record of that run, including nine other
findings, in the task notes for `07-26-sonnet5-kb-fix-lab-revision`.
