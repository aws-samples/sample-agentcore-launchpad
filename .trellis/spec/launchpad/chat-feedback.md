# Chat Feedback: Thumbs on Answers, the `saved` Event, and the Insights Card

> Ported from `origin/console-v3` by task `10-14-v2-chat-feedback`. People rate V2 Chat
> answers. Insights lists the thumbs-down answers, and those sessions can become an
> evaluation dataset.

## 1. Scope and triggers

- Read this before you touch `routers/chat.py` persistence or chat history, the
  `chat_feedback` table, `/api/feedback`, the V2 Chat thread, or the Insights page.

## 2. Contracts

- **Table `chat_feedback`.**
  - It is workspace-scoped: it is listed in `WORKSPACE_SCOPED_TABLES` and appears in the
    `_migrate_workspace_columns` ALTER and index map.
  - There is one verdict per person and message (`uq_chat_feedback_actor` on
    `(message_id, actor)`).
  - Columns are the console-v3 set **minus `correction`**. That is a hard rule: prod2's
    ledger kept the console-v3 table, and `schema_drift` passes only while main's
    columns are a subset of it. `test_feedback.py` builds the v3 table and asserts there
    is no drift.
  - **Never add a column here without an `ALTER` migration.** Prefer computing a value at
    read time instead. Question and answer previews are read from `chat_messages` with a
    500-character `substr`, and the question is the nearest preceding user turn.
- **The `saved {message_id}` SSE event.**
  - It is emitted only by the console route `POST /api/chat/{id}`. It arrives right
    before the event that closes a persisted answer (`tool`, `error` or `done`), or at the
    end of a stream that ends without `done`.
  - **`services/chat.py`, `services/invoke.py` and `/v1` are unchanged.** `/v1` never
    carries the event. `test_harness_execution.py` pins the difference between the two
    routes.
  - Every SSE consumer ignores unknown events. On the frontend, `stampSaved` gives the id
    to the bubble the event closes, so an answer split by a tool call gets one id per
    part.
- **History.** Every row carries its ledger `id` and the caller's own `verdict`.
- **Visibility.**
  - Rating checks `agent_access.agent_in(..., viewer=require_viewer, include_deleted=True)`.
    Any message the caller may not touch returns 404 `feedback.message_not_found`: an
    unknown id, another workspace's message, a non-agent row, or a hidden agent.
  - The list removes hidden agents in SQL, before counts and the limit.
  - The branch's list ignored the viewer. Main must not.
- **Range.** `?range=` filters on the verdict's `updated_at`, in UTC.
  `down_session_ids` (distinct, newest first, at most 50) go straight to
  `POST /api/eval/datasets/from-sessions`. That route skips sessions whose trace is gone
  and reports the skipped ones in its result.
- **Frontend.**
  - Thumbs appear only on finished assistant bubbles that have an id, and only in V2
    Chat. TryChat and classic Chat have none.
  - Updates are optimistic and keyed by message id, so a late reply after a session
    switch is harmless. On failure the verdict reverts and a toast shows.
  - Unknown `source` values (`share` / `review` rows from prod2) render as their raw
    text.

## 3. Out of scope

Comments and `correction`, answer rules (`answered_by` and the `rule` event), issues,
review and share links, guardrails, Agent-DLC admission, and feedback through `/v1`.

## 4. Tests

- `tests/test_feedback.py` covers:
  - the `saved` event's ordering and ids;
  - the history `id` and `verdict`;
  - rate, switch and withdraw;
  - previews and their caps;
  - the 404/422 matrix;
  - range filtering;
  - per-person verdicts;
  - isolated-workspace hiding;
  - the console-v3 column superset.
- `frontend/src/v2/pages/chat/messages.test.ts` covers `stampSaved`, the auth-card skip,
  tool-split ids and the verdict helpers.
- A browser check on dev ran one real chat turn and real feedback writes that were
  withdrawn afterwards. The dataset write was stubbed.
