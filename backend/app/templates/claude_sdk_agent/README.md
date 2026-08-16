# Claude Agent SDK container template

ARM64 image for AgentCore Runtime. Pinned versions verified 2026-08-16:

| Component | Version |
|---|---|
| python base | `python:3.12-slim-bookworm` (arm64) |
| node | 22.x (nodesource) |
| `@anthropic-ai/claude-code` CLI | ≥2.1 (2.1.205 verified) |
| `claude-agent-sdk` (python) | 0.2.* (0.2.114 verified) |
| `bedrock-agentcore` | 1.17.* |
| `aws-opentelemetry-distro` | 0.19.* |
| `openinference-instrumentation-claude-agent-sdk` | ≥0.1.3,<0.2 (0.1.10 verified) |

Bedrock mode: `CLAUDE_CODE_USE_BEDROCK=1` is baked into the image; the claude CLI
uses the runtime execution role for `bedrock:InvokeModel*` — no API key anywhere.

Telemetry: `opentelemetry-instrument` discovers the installed Claude Agent SDK
instrumentation automatically. The runtime wraps each SDK query with
`using_session(runtime_session_id)`, so the native `ClaudeAgentSDK.query` span
uses the same session id as Launchpad Evaluation and Observability.

Streaming: the SDK enables partial messages, maps text deltas and tool calls to
AgentCore Runtime SSE events, then emits one final completion event after
native query telemetry and Memory persistence. Updating this template requires
republishing an existing agent; deployed images are immutable. AgentCore also
pins an existing runtime session to the version that first served it, so use a
new Chat session after republishing to verify the new image.

Subagents/skills: drop markdown definitions into `.claude/agents/` (none ship by
default) — registry/custom skills selected in the wizard are bundled into
`.claude/skills/` at build time. `setting_sources=["project"]` makes the SDK
load both from `/app/.claude`.
