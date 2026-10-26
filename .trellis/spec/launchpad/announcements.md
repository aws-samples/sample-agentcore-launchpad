# Announcements

- Hub-global ledger state: `app/models/announcement.py`, not workspace-scoped or
  AWS-backed. Router import registers the new table before `init_db.create_all`.
- Keep editable `content` and `published_content` separate. Save never changes the
  published view; only publish copies the draft. Withdraw retains draft; delete
  removes the row. No startup/read seeds or hidden publication.
- All writes and editable reads require ADMIN in the centralized route table.
  Only published GET is MEMBER. Include every route in WORKSPACE_EXEMPT and the
  `/api/announcements` hub-global prefix. UI hiding alone is not authorization.
- The no-workspace-grant screen also renders the published feed for authenticated
  members. Keep it independent of WorkspaceContext; do not mount AWS pages there.
  Its header wraps on narrow screens so the feed never inherits an oversized grid.
- Pass `expected_revision` on all mutations of an existing row; both the request
  check and SQLAlchemy version predicate guard against lost edits. Map stale DB
  writes/deletes to409 and rollback.
- Never inject `require_identity` directly with Depends: its optional `Settings`
  argument becomes an unintended second request-body field in FastAPI. Wrap it
  with a Request-only dependency when recording the caller.
- Plain authored content is not translated, HTML/Markdown-rendered, or interpreted
  as instructions. UI chrome retains en/zh parity. Link URL and label are paired;
  accept only single-slash app-relative paths or credential-free HTTPS.
  Keep backend validation and the frontend link guard aligned: reject malformed
  percent escapes/UTF-8, decoded controls (including DEL), decoded backslashes,
  and an encoded leading second slash. Encoded spaces in paths/queries are valid.
  HTTPS links also pass `HttpUrl` host/port validation; preserve the author's
  original string rather than storing its normalized output.
- Explicit initial notice publication uses `config/announcements.initial.json`
  through `backend/scripts/publish_announcements.py`; deployments have independent
  notice data. Reusing video CDN URLs does not imply globally shared notices.
- Test lifecycle snapshots, unauthenticated/member/admin roles, no-workspace-grant
  reads, unsafe links, revision conflicts including competing DB sessions,
  pagination, and migration from a database without this table.
