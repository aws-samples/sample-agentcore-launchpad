## Summary

A console request that hit an AWS `ClientError` the platform had not anticipated came back as a bare `500 Internal Server Error` (Knowledge Base detail → `AccessDeniedException`, Registry edit → `ValidationException` for a malformed id) or, via the Memory wrapper, as a 502 whose message was botocore's raw `An error occurred (ResourceNotFoundException) when calling …` text. The console showed "HTTP 500" / raw boto text and the backend logged a traceback for a client-side bad id.

- `app/core/errors.py`: the global `ClientError` handler (formerly assume-role only) now maps `ResourceNotFoundException`→404 `aws.not_found`, `ValidationException`→400 `aws.validation`, `AccessDeniedException`/`UnauthorizedException`→403 `aws.access_denied`, throttling codes→429 `aws.throttled`, `ConflictException`/`ResourceInUseException`→409 `aws.conflict`. Message = AWS message with the boto prefix stripped; `detail` = `{aws_error_code, operation}`. Unmapped codes still re-raise (500 + traceback); assume-role failures keep their 502 diagnostic; service-level `AppError`s (`kb.not_found`, …) still take precedence.
- **`/v1` boundary**: on the public API the status and code are kept but the message is a generic per-code sentence and `detail` carries only `aws_error_code` — the raw text names the deployment's role ARN, instance id and operation, which an API-key holder must not see.
- Memory / Chat `memory.unavailable` wrappers let a *mapped* `ClientError` through to the handler; unmapped failures still become 502 `memory.unavailable`.
- Console: `apiErrors.aws.*` copy (en + zh-CN); KB detail and Registry edit render it.
- Docs: `docs/api.md` error table + `docs/architecture.md` (both with zh-CN twins).

## Verification

- `make verify` PASS. New hermetic tests `backend/tests/test_errors_aws.py` (mapped codes → envelopes, prefix stripping, unmapped → 500, service mapping precedence, assume-role unchanged, `/v1` redaction vs `/api` full message).
- Live read-only check from a throwaway backend on the branch: `/api/registry/records/does-not-exist` → 400 `aws.validation`; `/api/knowledge-bases/does-not-exist` → 403 `aws.access_denied`; `/api/memory/sessions?actor_id=does-not-exist` → 404 `aws.not_found`; `/api/eval/runs/does-not-exist` still 404 `run.not_found`.

Self-evolution direction SE-005 (`ux` path).
