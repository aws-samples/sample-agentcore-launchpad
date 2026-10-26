# Playground Attachments

## Scope and input contract

Console chat, console invoke and public `/v1` sync/SSE share
`schemas.attachments.AttachmentRequest`: `prompt` may be empty when
`attachments` is nonempty. Each attachment is `{name, media_type, data}`, where
`data` is standard base64. `AttachmentBodyCap` bounds the actual received body,
including chunked transfer, to 15 MiB before JSON validation.

`services.attachments.prepare_attachments` validates and produces
`PreparedAttachments`. Text is added to the ordinary user prompt; native files
stay separate until the method adapter. Limits are five files, 3 MiB per file,
10 MiB total decoded bytes, 100,000 combined prompt/extracted-text characters,
50 PDF pages, image dimensions at most 8,000 pixels and at most 25 million
pixels total. The UI receives limits and extensions from `attachment_capability`.

The backend validates content, not merely extension or browser MIME. Text must
be UTF-8, nonempty, contain no binary signature/NUL/control bytes. Images must
decode as their declared format. PDF parsing runs in a child process with a
512 MiB address-space cap, five CPU seconds and eight wall-clock seconds.
Encrypted, malformed or over-limit PDFs fail before AWS invocation.

## Accepted Harness behavior

The user explicitly chose actual capability over an extra preprocessing model:

- Harness receives UTF-8 text files as text.
- PDFs with readable text use a clearly labeled text-only extraction.
- A nonblank PDF page with no extractable text fails rather than disappearing.
- Images and scanned PDFs direct the user to a native-capable agent.
- No OCR/vision model, S3 upload, or independent inference step is implied.

This is based on signed live `InvokeHarness` probes. An HTTP 200 may contain a
`validationException` event; status alone does not prove attachment support.

## Native capability and version binding

`Agent.attachment_version` is server-owned and absent from `AgentSpec`.
`deployer.input_contract.record_input_contract` records the packaged source's
top-level `LAUNCHPAD_ATTACHMENT_CONTRACT = 'v1'` in `Job.payload`.
Only after AWS reports READY does `stamp_input_contract` bind the declaration
to the deployed runtime version. A resumed job uses this durable evidence, not
the current template in the backend checkout. A later text-only package clears
the capability.

Native UI capability requires an active agent, a recognized multimodal model
family and a matching deployed contract. Generated Strands A2A has a separately
live-verified FilePart path. Imported/custom HTTP agents without the contract
retain text/text-PDF fallback; method names alone never imply native support.
Native input is refused during an active canary because its two artifacts have
not both been proven compatible.

`ChatSession.runtime_version` records the version at session creation. Native
files on an older/unknown live console session fail with
`chat.attachment_new_session_required` before invocation. New runtime sessions
are necessary after republishing.

Native HTTP requests additionally require a runtime acknowledgement:

```json
{"event": "attachments", "contract": "v1"}
```

It must precede any tool/text event. Buffered runtimes use
`{"attachment_contract": "v1", "result": "..."}`. The transport consumes this
marker; it is not answer text. Missing acknowledgement fails rather than
presenting an old entrypoint's response as successful file processing. This
also covers public API session IDs that have no console ledger history.

## Method adapters

- Strands: hydrate base64 into native image/document `source.bytes`.
- Claude SDK: async user-message iterator with native image/document blocks;
  keep the original text prompt for existing memory hooks.
- Studio: explicit generated input contract accepting content blocks, not an
  arbitrary replacement of `.strip()` strings with a list.
- Converted Harness: checked, idempotent adaptation of its payload extractor
  and streaming entrypoint.
- A2A: `FilePart` with base64 `bytes`, `mimeType`, and filename.
- Mantle Responses: `input_file.file_data` plus `filename`, never a data URL
  in `file_url` (AWS accepts only S3 URLs there).

Provider-facing filenames are neutral `attachment-N.ext`; the prompt supplies
the original-filename mapping as user content. Bytes remain unchanged.

## Persistence and errors

Chat stores `{name, media_type, size, delivery}` metadata on
`ChatMessage.attachments`, where delivery is `native`, `text`, or `pdf_text`.
Metadata accompanies SSE `meta` and restored history. No base64 or file bytes
are stored in the chat ledger. Reloaded history shows file metadata without
promising a download of the original. Framework-owned AWS Memory can retain
native content under its existing session policy.

Pydantic attachment errors redact rejected input so the validation response
cannot echo uploaded file data. Error codes use `chat.attachment_*` and have
both locale translations. Format and capability errors happen before SSE;
upstream model/runtime errors remain stream errors.

## Verification

Generated Strands HTTP/Studio/A2A requirements pin `strands-agents==1.47.0`
alongside AgentCore 1.17. Fresh resolution to Strands 1.56.0 was live-proven to
fail initialization because AgentCore imports the removed
`BidiAfterInvocationEvent`. `test_runtime_dependency_compatibility.py` guards
that known incompatible pair; re-verify the Memory integration import and a
real runtime invocation before changing this pin.

- `test_chat_attachments.py`: file bytes, PDF extraction/native separation,
  invalid formats and bounds, public/console parity, metadata, stale sessions,
  canary rejection, raw-body cap and no unacknowledged success.
- `test_attachment_contract.py`: explicit source marker, durable resume evidence,
  package/deploy separation and capability revocation.
- Runtime/conversion tests exercise generated code with real byte blocks and
  acknowledgement order, including text-only compatibility.
- `make verify`, rendered browser send/restore/drop/paste/error checks, and real
  AWS native and Harness text-PDF probes must pass.

Research: `docs/playground-attachments-research.md`.
