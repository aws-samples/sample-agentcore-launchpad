# Implementation plan — fix container OTEL events import crash

Ordered; each stage ends with a runnable check.

## S1 — migrate `tracing.py` off the events API

- [ ] Replace `from opentelemetry._events import Event, get_event_logger` with
      `from opentelemetry._logs import LogRecord, SeverityNumber, get_logger`.
- [ ] `_event_logger = get_logger(EVAL_SCOPE)`.
- [ ] `_emit_event()` builds `LogRecord(timestamp, trace_id, span_id,
      trace_flags, severity_number=SeverityNumber.INFO, body,
      attributes={"session.id": ..., "event.name": EVAL_SCOPE})`.
- [ ] Update the module docstring's numbered requirement list so it still
      describes reality (scope + body shape), and add the note about why
      `event.name` is written as an attribute and `event_name` deliberately is not.
- Check: `cd backend && uv run python -c "import app.templates.claude_sdk_agent.tracing"`

## S2 — pin template dependencies

- [ ] `aws-opentelemetry-distro==0.19.*`, `claude-agent-sdk==0.2.*`, with a
      comment naming the failure mode being prevented.
- Check: `uv pip install --dry-run` style resolution probe in a throwaway venv.

## S3 — regression tests

- [ ] Static guard + pin assertions in `backend/tests/test_claude_sdk_template.py`.
- [ ] Behavioral guard emitting through an `InMemoryLogExporter`.
- Check: `cd backend && uv run pytest tests/test_claude_sdk_template.py -q`

## S4 — verify gate

- [ ] `make verify` (backend ruff+pytest, infra, frontend eslint+tsc+build, i18n).

## S5 — real AWS proof (R5)

- [ ] Confirm the local stack is up and `lab-fund-packager` still exists.
- [ ] Re-publish it (fresh CodeBuild image from the fixed template); watch the
      job log through all five stages.
- [ ] Invoke it for real; assert a non-error response.
- [ ] Check the runtime log group for the absence of the import traceback, and
      look up the trace to confirm gen_ai spans/events are present.
- [ ] If it still fails: capture the new CloudWatch error, stop, and report —
      do not paper over it in the docs.

## S6 — reconcile docs (R6)

- [ ] `docs/issues/2026-07-26-container-otel-events-import.md` → fixed state,
      with what was changed, how it was verified, and the still-open
      deploy-reporting gap.
- [ ] `docs/lab/03-deploy-harness.md` — the container chapter's FAQ/notes.
- [ ] `docs/lab/05-chat-memory.md` — the "关于容器 Agent 调用失败（本次实测）" section.
- [ ] `docs/lab/README.md` — the defect paragraph and the 未实跑 table.
- [ ] Keep the lab guide honest: it records what the live run hit **and** that
      the defect was fixed afterwards, with the re-verification result.
- Check: image/link integrity script; `python3 scripts/i18n_check.py` via verify.

## S7 — close out

- [ ] `make verify` again after doc edits.
- [ ] Trellis 3.3 spec-update judgment → `notes.md`.
- [ ] Report; ask before committing (parent task's changes are still uncommitted).

## Validation commands

```bash
cd backend && uv run ruff check . && uv run pytest tests/test_claude_sdk_template.py -q
make verify
curl -s http://127.0.0.1:8000/api/agents | python3 -m json.tool | head
```

## Rollback points

- After S1–S3: `git checkout -- backend/app/templates/claude_sdk_agent backend/tests`
- After S5 failure: the previous ECR image tag and the runtime's prior version
  are untouched; re-publish is repeatable.
