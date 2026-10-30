# Recorded seam shapes (round 17, loop-engine round 6 — fault-injected walks, r17-l)

Copies of the REAL response / error shapes recorded at the AWS seams of Launchpad CN on
2026-10-19 by deploy-ops-6 (T42; source directory `designs/seams-20261019/` on the operator
host — build aaacb0a6, cn-northwest-1). Only the files the fault layer replays are copied,
**fields only**: `request_redacted` was dropped on copy; the recorder had already replaced
the account id with `000000000000`, runtime / browser / session ids with placeholders, and
kept no credential, token, cookie or header value (the copy step re-checks every file for an
access-key / bearer / cookie / real-account pattern and refuses it — C2 / C4).

| file | seam | what `tests/graph/faults.py` replays from it |
|---|---|---|
| `agentcore-runtime/invoke_unknown_runtime.json` | InvokeAgentRuntime | the data-plane 404 `ResourceNotFoundException` — "No endpoint or agent found with qualifier 'DEFAULT' for agent '<arn>'" (the runtime-gone / 404-after-create class) |
| `agentcore-runtime/invoke_read_timeout.json` | InvokeAgentRuntime | botocore `ReadTimeoutError` with the real `endpoint_url` (the URL-encoded `/runtimes/<arn>/invocations` path) and the pool's `Read timed out. (read timeout=…)` text |
| `agentcore-runtime/invoke_stream_success.json` | InvokeAgentRuntime (SSE) | a 200 whose ONE `data:` line is the agent template's terminal error frame (`{"error": "no workload access token: …", "erro` — the recorder cut the line at 300 chars, which is exactly the PARTIAL-FRAME shape a dropped connection leaves) |
| `agentcore-runtime/invoke_second_turn_same_session.json` | InvokeAgentRuntime (SSE) | the healthy Converse frame sequence (`messageStart`, an EMPTY first `contentBlockDelta`, text deltas, `messageStop`, `metadata`) the malformed-frame shapes are spliced into |
| `agentcore-runtime/eval_worker_invalid_job_payload.json` | InvokeAgentRuntime (JSON body) | the worker's `REJECTED` ack — a `contentType: application/json` body, not SSE |
| `opensearch-memory/read_timeout.json` | OpenSearch `_search` | `urllib3.exceptions.ReadTimeoutError` — `HTTPSConnectionPool(host='search-<domain>…', port=443): Read timed out. (read timeout=…)` |
| `opensearch-memory/index_not_found_404.json` | OpenSearch GET `/<index>/_search` | the 404 `index_not_found_exception` body |
| `opensearch-memory/iam_identity_403_instance_role.json` | OpenSearch (IAM) | the identity-policy 403 body (`User: arn:aws-cn:sts::000000000000:assumed-role/… is not authorized to perform: es:ESHttpGet …`) — the AccessDenied-not-NotFound class (N16-obs87); the body is what a leak oracle must never see in a console envelope |
| `opensearch-memory/search_bad_query_400.json` | OpenSearch `_search` | the 400 `parsing_exception` body |
| `agentcore-browser/stop_browser_session_already_stopped.json` | StopBrowserSession | 409 `ConflictException: Session already terminated: <id>` (catalogue only — no lifecycle graph reaches the Browser seam) |
| `agentcore-browser/get_browser_session_unknown.json` | GetBrowserSession | 404 `Session not found: <id>` (catalogue only) |
| `ssm-getparameter/get_parameter_not_found.json` | SSM GetParameter | HTTP **400** `ParameterNotFound` with an EMPTY `Error.Message` (the deploy stage's model-key read) |
| `console-api/agents_delete_seam-probe-unsupported.json` | DeleteAgentRuntime (through the console) | the 409 `aws.conflict` envelope — AWS text "The agent is currently being modified by another operation. Current status: CREATING." (replayed as the `ConflictException` the Runtime control plane raised) |

Shapes the fault layer uses that are NOT in this directory come from the existing hermetic
fault fixtures: `tests/faults/conftest.py` (`bedrock_5xx`, `throttle`, `read_timeout` frames
and `ClientError`s — the N-ARCH-obs18 live shapes) and
`tests/property/test_memory_merge_properties.REAL_FAULTS` (OpenSearch 429 / 503 / 502 bodies,
the N14-obs4 live shapes). Not captured on 2026-10-19 and therefore NOT replayed as recorded
(see `docs-notes.md` of the source directory): a Runtime `AccessDeniedException` text (the
fault layer synthesises one from the IAM sentence family above and says so), OpenSearch
429 / 5xx and the FGAC 403, SSM `AccessDeniedException`, Logs Insights `LimitExceededException`.
