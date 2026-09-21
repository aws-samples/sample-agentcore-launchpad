# Playground attachment compatibility and proposed design

Status: implemented and verified. The new Claude container's cloud deployment
is blocked by the existing base-image vulnerability gate; its generated
attachment entrypoint was separately verified through real Bedrock SDK calls.
Verified on 2026-09-20 against the development workspace in AWS account
`434444145045`, `us-west-2`. Mantle model calls used `us-east-1`, matching the
generated runtime's configured provider path. No pre-existing user agent was redeployed.

## What was actually tested

The fixtures contain independent verification codes:

- PNG: `IMAGE-7K29`, rendered as pixels.
- PDF: `PDF-4R83`, rendered into an image-only PDF with no text layer.
- UTF-8 TXT: `TEXT-8M61`.
- An additional text-layer PDF tests the text-extraction fallback separately.

The question never contains these codes. Reading the codes therefore tests
content delivery, rather than accepting an HTTP status or an agent's claim that
it supports attachments. SDK probes have no tools, and Harness probes use
invocation-scoped `allowedTools: []`. Runtime probe sessions were explicitly
stopped after reading their responses. Harness has no session-stop operation.
Invocations produce ordinary AWS usage and telemetry; resource configurations
were not changed.

Installed versions: `bedrock-agentcore==1.17.0`, `boto3==1.43.83`,
`strands-agents==1.47.0`, and `claude-agent-sdk==0.2.116`. Temporary `uv run --with`
environments supplied the optional OpenAI/Mantle and PDF-parser dependencies;
the project's dependency files were not changed.

## Compatibility results

| Agent / route | Verified input behavior | Work required for Playground |
| --- | --- | --- |
| Managed Harness | Text blocks work. PNG and PDF blocks fail in the AWS event stream with `validationException`. | Decode text attachments into text blocks. Native images/PDFs cannot be enabled by a UI change or SDK upgrade alone. Decide whether to offer explicit preprocessing. |
| Strands + native Bedrock | TXT document, PNG image, and scanned PDF each returned the correct code using `global.anthropic.claude-sonnet-4-6`. | Preserve typed content blocks and decode transported base64 to bytes before calling Strands. |
| Existing generated Strands HTTP ZIP | `eval-target-v2` returned `NO_ATTACHMENT` when extra attachment/content fields were sent alongside `prompt`. | Update the generated entrypoint; publish a new runtime version and start a new session. |
| Strands A2A | Existing `aurora-faq-a2a` returned all three correct codes from one JSON-RPC request containing `FilePart` values. | Add A2A file parts to the platform's request builder. This specific deployed runtime already supports them. |
| Claude Agent SDK | A local SDK query through Bedrock returned all three correct codes. PNG/PDF were native base64 content blocks; TXT was decoded text. | Supply an async user-message iterator with content blocks to the SDK. |
| Existing Claude container | `fs-verify-agent` returned `NO_ATTACHMENT` for the current HTTP payload. | Update the template, rebuild and publish; use a new runtime session. |
| Studio | Existing `studio-canvas-e2e` returned `NO_ATTACHMENT`. The wrapper sends only `main(prompt, None)` and generated `main` expects strings. | Update both the platform wrapper and recognized generated-code input contract. Merely passing a list breaks `.strip()`. Arbitrary existing custom code is not automatically compatible. |
| Harness converted to ZIP | `aurora-support-rt-2` ignored extra fields next to `prompt`. Supplying its existing `messages` field with base64 image/PDF sources failed with `ConverseStream: Could not process image`. | The export accepts messages but lacks byte hydration. Add a checked conversion adaptation; do not assume every `code_bundle` supports attachments. |
| Strands + Mantle Responses | PNG worked with `openai.gpt-5.6-sol`. The stock SDK's PDF formatter failed; a prototype formatter using `file_data` + `filename` returned the correct scanned-PDF and TXT codes. | Apply the provider-specific file serialization fix in the generated runtime's adapter. |
| Imported/custom HTTP or A2A | Protocol labels alone do not establish the implementation's input contract. Arbitrary external deployments were not exhaustively probed. | Keep native attachment capability unknown until an explicit compatible contract is established. |

These results establish the tested routes and model IDs. They do not establish
that every model selectable in the console supports every modality.

### Harness: service rejection, not just client validation

The installed SDK and the published `HarnessContentBlock` contract contain only
`text`, `toolUse`, `toolResult`, and `reasoningContent`.

To distinguish an outdated SDK from a service restriction, separate signed HTTP
requests sent actual image/document blocks directly to `InvokeHarness`. Both
returned HTTP 200 with an **exception event**, not a successful assistant reply:

```text
:message-type = exception
:exception-type = validationException
5 validation errors for ContainerRequest
invokePayload.messages.0.content.1.HarnessContentBlock1.text
  Input should be a valid string
...
```

| Probe | AWS request ID |
| --- | --- |
| PNG rejection | `89dae174-df08-4d66-b5ba-e75afb789720` |
| PDF rejection | `cc29947a-6d38-4db9-9fe7-6a842e152a9c` |
| Successful text control | `52ae7f27-7801-4da4-bcef-9dadb2bd9a04` |

The initial SDK-hook image/PDF probes timed out at 60 seconds. Those timeouts
are not the evidence for rejection; the separate raw requests above captured
the actual service exception.

A separate `pypdf` probe extracted `PDF-TEXT-3C72` from the text-layer PDF;
Harness returned that exact code from the extracted text. The image-only PDF
yielded an empty extraction. This validates the narrower fallback while proving
that it cannot handle scanned pages.

### Mantle: two file fields have different semantics

Strands 1.47.0's `OpenAIResponsesModel._format_request_message_content` emits:

```json
{"type": "input_file", "file_url": "data:application/pdf;base64,..."}
```

AWS returned HTTP 400: `Only S3 URLs are supported for file_url.`
The following prototype change succeeded with the same scanned PDF:

```json
{
  "type": "input_file",
  "filename": "sample.pdf",
  "file_data": "data:application/pdf;base64,..."
}
```

This proves an adapter fix is viable without adding an S3 upload to this flow.

## Current code boundaries

- `frontend/src/pages/Chat.tsx`: composer, request construction, streamed
  messages and restored history.
- `backend/app/routers/chat.py`: text-only `ChatRequest`, session attribution
  and text-only transcript persistence.
- `backend/app/schemas/agent.py`: text-only shared `InvokeRequest`.
- `backend/app/services/chat.py`: Harness streaming request always builds one
  text content block.
- `backend/app/services/invoke.py` and `services/agentcore/runtime.py`: shared
  HTTP/A2A/canary dispatch and runtime payload construction.
- `backend/app/templates/strands_agent/main.py.tmpl`: casts `payload.prompt`
  to a string before invoking Strands.
- `backend/app/templates/claude_sdk_agent/main.py.tmpl`: passes a string to
  `query`; the installed SDK also accepts an async iterable of user messages.
- `backend/app/templates/studio_agent/__init__.py`: calls `main(prompt, None)`.
  `frontend/src/studio/lib/code-generator.ts` and `graph-code-generator.ts`
  generate the corresponding string-based input handling.
- `backend/app/services/harness_convert.py`: owns checked adaptation of the
  exported runtime bundle.

## Proposed first implementation

### Interaction and file contract

Add file selection, drag-and-drop, image paste, removable attachment chips,
image previews, and per-file validation to the existing composer. Sending may
contain text, attachments, or both. Show filenames in restored conversation
history and distinguish native file delivery from extracted text.

Start with PNG/JPEG/WebP/GIF, PDF, and UTF-8 text formats such as
TXT/MD/CSV/JSON/log/source files. Unknown binary formats are rejected with a
clear explanation. Arbitrary archives, executables, audio/video, and Office
document parsing need separate format support; base64 text is not a substitute
for a file reader.

Use an optional shared `attachments` field on chat/invoke requests:

```json
{
  "prompt": "Read the attached document",
  "session_id": null,
  "attachments": [
    {
      "name": "report.pdf",
      "media_type": "application/pdf",
      "data": "<base64>"
    }
  ]
}
```

Suggested initial platform limits: five files, 3 MiB per file, 10 MiB total
decoded bytes, plus a bounded encoded request body. Validate bytes, encoding,
image dimensions and PDF validity on the backend before starting SSE or an
AWS invocation. Limits must come from one server contract exposed to the UI;
the selected provider/model can impose tighter bounds.

Keep bytes transient in the invocation path. Persist only attachment metadata
with the existing transcript; do not put base64 blobs in SQLite or application
logs. Reloaded history therefore shows metadata, not a promise that the console
can download the original file. Agent-owned Memory behavior must also be checked
when validating multimodal follow-up turns.

### Shared invocation and adapters

Resolve and validate attachments once in the shared invocation layer so console
SSE and public sync/SSE cannot disagree.

- Text fallback: decode UTF-8 and delimit the filename/content as user data.
- Strands: native image/document blocks with decoded `bytes`.
- Claude: native image/document blocks inside an async user-message input.
- A2A: `FilePart` with `mimeType`, neutral filename and base64 `bytes`.
- Mantle: native image input and `input_file.file_data`, preserving the original
  file extension.
- Studio: an explicit generated-code input contract that accepts decoded blocks;
  retain compatibility for text-only/custom code.
- Converted Harness: a checked, idempotent byte-hydration graft.

Propagate attachments through canary routing or reject the unsupported
combination before sending anything. A fallback must never silently discard
files or switch to an untested runtime version.

### Capability and version handling

Compute UI capability on the server from the deployed input contract,
protocol, model/provider support, and deployment version. Do not enable
native attachments simply because `method == "zip_runtime"`.

Set the native-input contract only after successful deployment of the updated
artifact. An old runtime session remains pinned to its original version, so
republishing must lead to a new session before using its new attachment
capability. Existing text-only agents keep ordinary text invocation.

### Accepted product decision: Harness fallback

The user selected this behavior on 2026-09-20: expose actual capability. Harness accepts text attachments and
an explicitly labeled text-extraction path for PDFs that have readable text.
Images and image-only PDFs explain that a compatible Strands/Claude agent is
needed. Never imply that extracted text preserves diagrams or scanned pages.

No independent vision/document preprocessing model is added.

## Implementation and acceptance sequence

1. Resolve the Harness fallback decision and record PRD/design/implementation
   artifacts for the resulting scope.
2. Implement the bounded shared file contract, validation, capability projection
   and transcript metadata.
3. Connect the verified adapters and generated runtime/conversion contracts.
4. Add the composer and bilingual messages with capability-specific guidance.
5. Test meaningful failures: invalid bytes, size/count limits, workspace
   attribution, text-only compatibility, unknown/old deployments, old sessions,
   canary routing and provider errors.
6. Publish disposable validation agents for changed runtime templates and test
   actual image/scanned-PDF/text delivery, streaming and follow-up turns.
7. Validate the browser interactions and run `make verify`.

Changing the console/backend alone cannot upgrade deployed runtime artifacts.
Existing user agents should be republished explicitly, not mutated in bulk.

## Evidence and references

### Implementation validation

The final `make verify` passed with **4,486 backend tests**, **47 infra tests**,
frontend lint/typecheck/build and both i18n checks. The pre-existing
`EvaluationExperiment.tsx` hook warning remains.

Real invocation checks after implementation:

| Path | Evidence |
| --- | --- |
| Generated Strands HTTP, version 2 | Real browser send returned `IMAGE-7K29`, `PDF-4R83` and `TEXT-8M61`; a second turn recalled all three through AgentCore Memory; reload restored attachment metadata. |
| Generated Studio, version 2 | HTTP invoke returned both native PNG/PDF codes through the generated canvas entrypoint. |
| Fresh Harness-to-ZIP conversion, version 1 | HTTP invoke returned both codes through the checked export adaptation. |
| Existing generated A2A | Platform `/api/agents/{id}/invoke` returned both codes using the new FilePart adapter. |
| Harness | Browser send returned `PDF-TEXT-3C72` after text extraction; image selection and scanned-PDF send were explicitly rejected. Failed files remained available for removal/retry. |
| Claude generated template | The rendered entrypoint emitted its attachment acknowledgement, real SDK text deltas and both native file codes through Bedrock. The local working directory was redirected to a temporary directory and tools were disabled. |

Browser validation used real APIs at `http://127.0.0.1:5199` → `:8011`, with an
isolated temporary ledger. It covered a 1440×1080 desktop, a 390×844 mobile
viewport, English/Chinese, file selection, drag/drop, image paste, removal,
attachment-only send, failure retention and history. No page errors or page-wide
mobile overflow were observed. Screenshots and result JSON remain in the
temporary probe directory below.

Live packaging also exposed a pre-existing dependency incompatibility:
`bedrock-agentcore==1.17.0` imports `BidiAfterInvocationEvent`, removed in
`strands-agents==1.56.0`. The open Strands range selected that release and the
first new runtime failed initialization. Generated HTTP/Studio/A2A packages now
pin the verified `strands-agents==1.47.0`; republishing the temporary HTTP and
Studio agents fixed initialization and their attachment checks passed.

The Claude image built successfully, but the existing CRITICAL vulnerability
gate refused deployment: four findings in base-image OpenSSL/Perl
(`CVE-2026-75803`, `CVE-2026-13221`, `CVE-2026-57433`, `CVE-2026-12087`).
The gate was not disabled. A patched base image is still required before that
new container can be deployed; no successful cloud-container deployment is
claimed here.

The current repository baseline passed
`env -u AWS_REGION -u AWS_DEFAULT_REGION -u LAUNCHPAD_REGION make verify`:
4,385 backend tests, 47 infra tests, lint/typecheck/build and both i18n gates.
ESLint reported one existing `react-hooks/exhaustive-deps` warning in
`EvaluationExperiment.tsx`. This earlier baseline predates the implementation
checks above.

Local probe programs, generated fixtures and individual result JSON files are
under `/tmp/launchpad-attachment-probe/`. These are temporary investigation
artifacts, not part of the deployed application.

- [AWS HarnessContentBlock contract](https://docs.aws.amazon.com/sdk-for-python/v1/reference/clients/bedrock-agentcore/unions/HarnessContentBlock/)
- [InvokeHarness API](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_InvokeHarness.html)
- [Strands documentation](https://strandsagents.com/llms-full.txt)
- [OpenAI file input contract](https://developers.openai.com/api/docs/guides/file-inputs)
- Installed Strands sources: `models/openai_responses.py` and
  `multiagent/a2a/executor.py`.
- Installed Claude SDK source: `query.py`, including the async user-message
  input contract.
