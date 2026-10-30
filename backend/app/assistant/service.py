"""Architect assistant service: conversations, turns, proposal revisions, approval.

Invariants (``tests/test_assistant.py`` pins each one with request/fault probes):

* **Principal + workspace bound.** Every read and write filters on
  ``(workspace_id, owner_principal)``; another principal's, another workspace's or
  an unowned (NULL principal) conversation is a 404 — a recycled username inherits
  nothing. The one exception is an admin **share**: a ``shared`` conversation of
  the same workspace is open to every member for every action (``may_collaborate``
  — discussion, preparation, proposal review and approval, evaluation assets) under
  that action's usual permission, re-checked at each write; deleting it (and its
  footprint preview) stays with the owner. Unsharing closes it again at once. An
  administrator *reads* every conversation of the workspace (``may_view``: list,
  detail, evaluation plan, operation status, and a read-only tail of a live turn)
  but writes only where it collaborates — an unshared one is read-only until the
  admin (or its owner) shares it.
* **Discussion never writes AWS.** A turn makes exactly one data-plane call
  (``InvokeAgentRuntime`` on the preset's Runtime) and ledger writes. No proposal, valid or not,
  creates anything until a separate approval.
* **One in-flight turn per conversation.** A turn is an atomic conditional claim on
  the conversation row (``active_turn``); a concurrent turn is refused (409), a
  claim left behind by a dead process is reclaimed after ``TURN_CLAIM_TTL_S`` and on
  startup. The private runtime session id is written to the ledger *before* the
  data-plane call. A turn that is aborted (explicit cancel, producer failure, claim
  loss, dead process) persists the partial answer as an interrupted turn
  (``turn_interrupted`` marker, never the transport's exception) and keeps only a
  proposal block that had streamed completely — never one cut mid-fence.
* **A turn is a server-side unit of work, independent of any connection
  (N-ARCH-obs15).** ``start_turn`` runs the producer on a driver thread owned by the
  ``LiveTurn`` registry (one per conversation); every event it emits gets a monotonic
  ``seq`` and is appended to ``assistant_turn_events`` before the claim is released.
  The ``POST …/turns`` response and ``GET …/turns/{turn}/stream?from=`` are
  *subscribers*: closing one closes nothing but the subscription. The final
  transcript row and proposal are written exactly as for a watched turn.
* **Server-owned bounded replay.** The preset runs with persistent memory disabled
  and every turn uses a fresh runtime session id, so the transcript is replayed
  through the request's ``messages``, paired by turn, within one FINAL budget that
  includes the protocol preamble and the current message; omitted older turns are
  disclosed, the current message is never truncated (too large → 413).
* **Monotonic, unique revisions.** Model emissions and member edits both allocate
  their revision from the conversation's ``revision_seq`` inside the same short
  write transaction that stores the row (unique index), so two concurrent writers
  never share a number. One serialized-byte cap applies to both sources before
  validation; content is stored verbatim or not at all — never "fixed".
* **Approval is the only deployment executor.** Explicit preparation actions may
  upload Skills through the existing staged ingestion path. Approval names an exact
  revision + hash (content AND
  resolved bindings). Catalog and resource reads happen outside any lock; then one
  short transaction that first takes the conversation write lock re-resolves the
  caller's account, deploy permission, workspace grant and readiness from the
  database, re-reads the revision, claims the agent name atomically (shared with
  ordinary creation), claims the revision (draft → approved) and writes agent +
  deployment + job with their ids linked, in one commit. A repeated or concurrent
  approval of the same revision returns the recorded outcome (and re-wakes a queued
  job whose starter died); a different revision cannot ride on it.
* **Exact reviewed resources or nothing.** The pinned bindings (gateway ARN +
  outbound-auth identity, skill record + S3 content digest, KB-gateway
  prerequisites, memory ARN) are compared against the live resolution at approval
  and again at job entry; drift fails closed before any AWS write. No gateway is
  ever created by this flow.
"""

import copy
import hashlib
import json
import logging
import queue
import re
import threading
import time
import uuid
from collections.abc import Callable, Generator, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session
from sqlalchemy.orm.exc import ObjectDeletedError

from app.assistant import preparation, validation_codes
from app.assistant import proposal as proposal_contract
from app.assistant import runtime as architect_runtime
from app.assistant.principal import may_collaborate, may_view, principal_of, viewer_clause
from app.assistant.recommendation_intent import REPAIR_NOTE_PREFIX
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError, NotFoundError
from app.deployer.pipeline import create_deployment
from app.models.assistant import (
    AssistantConversation,
    AssistantMessage,
    AssistantProposal,
    AssistantTurnEvent,
)
from app.models.ledger import Agent, Job, User, Workspace
from app.routers.auth import Identity
from app.schemas import model_catalog
from app.schemas.agent import ToolRef, model_catalog_lines, principal_of_identity
from app.services import (
    agent_names,
    creators,
    knowledge_backend,
    live_turns,
    memory_bootstrap,
    method_validation,
    registry_backend,
    visibility,
)
from app.services.agentcore import runtime as rt
from app.services.agentcore.client import data_client
from app.services.skill_ingest import SKILL_BUNDLE_MAX_BYTES
from app.services.visibility import Viewer
from app.services.workspace import WorkspaceContext
from app.system_agents import service as system_agents
from app.system_agents.presets import ARCHITECT, refuse_reserved_name
from app.templates.gateway_support import runtime_user_id
from app.templates.kb_support import KB_DEEP_TOOL_NAME, KB_TOOL_NAME

logger = logging.getLogger("launchpad.assistant")

PRESET = ARCHITECT
PERMISSION_DEPLOY = "agents.deploy"
MAX_PROMPT_CHARS = 100_000
MAX_PROMPT_BYTES = 300_000
# The FINAL composed request (preamble + catalog + replayed turns + current message)
# never exceeds this many characters; the newest turns that fit are kept.
MAX_REPLAY_CHARS = 160_000
MAX_REPLAY_TURNS = 12
CATALOG_MAX_ENTRIES = 60
# Explicit state bounds: a conversation is a requirement-intake session, not a log.
MAX_TURNS = 200
MAX_REVISIONS = 50
# A turn claim older than this belongs to a dead process (the harness itself times
# out at ≤ 3600 s; the preset uses 900 s) and may be reclaimed.
TURN_CLAIM_TTL_S = 1800
# SSE keep-alive cadence while the upstream is silent; also the longest the turn's
# consumer ever blocks, so cancellation is observed within one interval. Shared with the
# Chat playground's detached turns (``services/live_turns.py``).
HEARTBEAT_S = live_turns.HEARTBEAT_S
# ``name`` of the transcript row that records Launchpad's rejection of a model-emitted
# proposal block; ``compose_messages`` replays it to the model on the next turn.
PROPOSAL_REJECTED_NAME = "proposal_rejected"
# An ``error`` row with this name marks a failed turn the member retried (usually with
# an edited message): the turn stays in the ledger and the thread, but is never
# replayed to the model again — a poisoned exchange must not follow the retry.
TURN_RETRIED_NAME = "turn_retried"
# N-ARCH-obs12: the ``error`` row of a turn the client left before the reply finished
# (tab closed, SSE dropped, the e2e's turn ceiling). The console renders the name as a
# bilingual notice; the text is for API readers and the ledger — never the transport's
# exception (`AttributeError: 'NoneType' object has no attribute 'read'` from urllib3 on
# the body the owner closed). Keeps the ``interrupted`` prefix every reader keys on.
TURN_INTERRUPTED_NAME = "turn_interrupted"
TURN_INTERRUPTED_TEXT = ("interrupted: the turn stopped before the reply finished — what had "
                         "streamed is kept; a proposal block that was already complete was "
                         "recorded as a revision")
# N-ARCH-obs15: the per-turn event log is flushed to the ledger at most this often while
# only deltas arrive (any other event, and the terminal one, flush at once). A subscriber
# in this process tails the in-memory buffer, so the cadence only bounds what a dead
# process loses and how far a cross-process poll lags.
TURN_EVENT_FLUSH_S = live_turns.EVENT_FLUSH_S
# Terminal events of a turn's event log; a stream ends after one of them.
TURN_TERMINAL_EVENTS = live_turns.TERMINAL_EVENTS
# A transient upstream failure mid-turn (a Harness stream cut short after a tool step or
# with nothing at all, a mid-stream runtimeClientError / internalServerException,
# throttling, a 5xx — see `_replayable_turn_error`) replays the whole
# turn this many times in a fresh Harness session before the member sees an error
# (live 2026-10-04: about one architect turn in five on prod and dev). One replay keeps
# two attempts inside ``TURN_CLAIM_TTL_S``; an execution timeout is never replayed —
# the member already waited the preset's whole budget.
TURN_TRANSIENT_RETRIES = 1
# How long a replay waits for the consumer to record its fresh session on the ledger.
RETRY_RECORD_WAIT_S = 30.0
# Ledger-only row reserving a replay's Harness session (``sessions.is_assistant_session``
# reads every row's session id): never shown in the transcript, never replayed.
SESSION_ROLE = "session"
REJECTION_NOTE_MAX_CHARS = 2400
# rev-r27 #6: the internal correction prompt must list EVERY error (many scenarios × several
# reference evaluators); ``proposal.MAX_ERRORS`` (40) sentences fit well inside this
AUTOCORRECT_PROMPT_MAX_CHARS = 16_000
# N17-obs295: the transcript notice of the automatic proposal correction — ONE internal
# model call inside the same turn when a model proposal failed ONLY structural
# evaluation-plan rules (``validation_codes.AUTOCORRECT_CODES``). The row is written
# ``pending`` before the correction call and renamed to its outcome; its ``- `` lines are
# the first version's errors. Never replayed to the model, never a failed turn.
PROPOSAL_AUTOCORRECT_PENDING = "proposal_autocorrect_pending"
PROPOSAL_AUTOCORRECTED = "proposal_autocorrected"
PROPOSAL_AUTOCORRECT_FAILED = "proposal_autocorrect_failed"
AUTOCORRECT_NAMES = (PROPOSAL_AUTOCORRECT_PENDING, PROPOSAL_AUTOCORRECTED,
                     PROPOSAL_AUTOCORRECT_FAILED)
# ``name`` of the hidden SESSION_ROLE row reserving the correction call's Harness session
AUTOCORRECT_SESSION_NAME = "proposal_autocorrect"
# N17-obs468: the transcript notice of a reply that carried NO proposal block although it
# imitated the replay placeholder or claimed a submitted revision (live 2026-10-26: 「修订已提交」
# with nothing stored). ``pending`` while ONE corrective internal call runs in the same
# turn, then ``recovered`` (that call stored a revision) or ``missing`` (the proposal was
# not updated). Never replayed to the model, never a failed turn.
PROPOSAL_MISSING_PENDING = "proposal_missing_pending"
PROPOSAL_MISSING_RECOVERED = "proposal_missing_recovered"
PROPOSAL_MISSING = "proposal_missing"
MISSING_NAMES = (PROPOSAL_MISSING_PENDING, PROPOSAL_MISSING_RECOVERED, PROPOSAL_MISSING)
# ``name`` of the hidden SESSION_ROLE row reserving the corrective call's Harness session
REASK_SESSION_NAME = "proposal_reask"
# rows of these names are notices, not failures: a turn carrying one is not retryable
NOTICE_NAMES = (PROPOSAL_REJECTED_NAME, TURN_RETRIED_NAME, *AUTOCORRECT_NAMES, *MISSING_NAMES)
# the turn's pending in-turn correction notices, whichever kind
PENDING_NAMES = (PROPOSAL_AUTOCORRECT_PENDING, PROPOSAL_MISSING_PENDING)

# N17-obs468: a reply imitating Launchpad's replay placeholder (or a close variant the model
# invents: "[No patch emitted in this reply; …]") / claiming a submitted revision. Both only
# count when the reply carried no proposal block or patch at all.
_PLACEHOLDER_RE = re.compile(
    r"omitted from the replay|proposal block of this reply"
    r"|no (?:proposal |change )?(?:patch|block|proposal block) (?:was )?emitted"
    r"|\[[^\]\n]{0,80}\b(?:proposal|patch)(?: block)?\b[^\]\n]{0,40}\b(?:omitted|emitted)\b",
    re.IGNORECASE)
_CLAIM_RE = re.compile(
    r"(?:修订|提案|方案|新版本)[^。！？\n]{0,8}已(?:重新)?提交"
    r"|已(?:重新)?提交(?:了)?(?:新的)?(?:修订|提案|方案|新版本)"
    r"|\b(?:re-?)?submitted\b[^.\n]{0,40}\b(?:revision|proposal)\b"
    r"|\b(?:revision|proposal)\b[^.\n]{0,40}\b(?:has|have) been (?:re-?)?submitted\b",
    re.IGNORECASE)


def phantom_reason(text: str) -> str | None:
    """Why a reply WITHOUT a proposal block reads as if it changed the proposal:
    ``replay_marker`` (it imitates the placeholder) or ``claimed_revision`` (it claims a
    submitted revision), else None. The caller checks that no block / patch was found."""
    if _PLACEHOLDER_RE.search(text or ""):
        return "replay_marker"
    if _CLAIM_RE.search(text or ""):
        return "claimed_revision"
    return None


def missing_note(outcome: str, base: int | None, recovered: int | None = None) -> str:
    """The transcript text of a ``MISSING_NAMES`` notice; ``still revision N`` / ``stored
    revision M`` are read back by ``missing_detail`` for the console's own wording."""
    still = f"still revision {base}" if base is not None else "no proposal is stored yet"
    if outcome == PROPOSAL_MISSING_PENDING:
        return ("The reply carried no proposal block, so nothing was stored — Launchpad is "
                f"asking the assistant once more ({still}).")
    if outcome == PROPOSAL_MISSING_RECOVERED:
        return ("The first reply carried no proposal block; Launchpad asked once more and "
                f"stored revision {recovered}.")
    return f"The reply carried no proposal block — the proposal was not updated ({still})."


_STILL_RE = re.compile(r"still revision (\d+)")
_STORED_RE = re.compile(r"stored revision (\d+)")


def missing_detail(text: str) -> dict[str, int | None]:
    still, stored = _STILL_RE.search(text or ""), _STORED_RE.search(text or "")
    return {"base": int(still.group(1)) if still else None,
            "revision": int(stored.group(1)) if stored else None}


def reask_prompt(base: int | None, reason: str) -> str:
    """The internal follow-up of the corrective call (N17-obs468): sent on the member's
    side of the replay, never stored as a member message."""
    what = ("contained Launchpad's replay placeholder" if reason == "replay_marker"
            else "said a revision was submitted")
    if base is not None:
        state = (f"the proposal is still revision {base} (the stored proposal in the protocol "
                 "preamble)")
        how = (f"ONE `{proposal_contract.PATCH_FENCE}` block with base_revision {base} against "
               f"that stored proposal (a complete `{proposal_contract.PROPOSAL_FENCE}` block "
               "only if the change rewrites most of it)")
    else:
        state = "no proposal is stored yet"
        how = f"the complete `{proposal_contract.PROPOSAL_FENCE}` block"
    return f"""## Launchpad — no proposal change was stored (internal step, not a member message)

Your previous reply {what}, but it carried no proposal block, so Launchpad stored
nothing: {state}. Never write the replay placeholder or any bracketed note about omitted or
emitted blocks — those are Launchpad's notes, never part of a reply; words alone change
nothing. If the member asked for a change, make it now as {how}. If no change was needed,
reply with one plain sentence saying the proposal was not changed and write no block. Ask
the member nothing."""


def _error_lines(errors: list[str], cap: int = REJECTION_NOTE_MAX_CHARS) -> str:
    body = "\n".join(f"- {e}" for e in errors) or "- (no detail)"
    if len(body) > cap:
        body = body[:cap] + "\n- … (more errors omitted)"
    return body


def autocorrect_note(errors: list[str], outcome: str, reason: str | None = None) -> str:
    """The transcript text of the auto-correction notice row (``AUTOCORRECT_NAMES``)."""
    head = {
        PROPOSAL_AUTOCORRECT_PENDING: "Launchpad is correcting the proposal automatically",
        PROPOSAL_AUTOCORRECTED: "Proposal auto-corrected",
        PROPOSAL_AUTOCORRECT_FAILED: "The automatic proposal correction did not produce a "
                                     "valid revision" + (f" ({reason})" if reason else ""),
    }[outcome]
    return (head + " — the first version failed these structural evaluation-plan rules:\n"
            + _error_lines(errors))


def autocorrect_prompt(revision: int, errors: list[str]) -> str:
    """The internal follow-up of the correction call (N17-obs295): sent on the member's
    side of the replay but never stored as a member message. The stored revision it
    patches is in the protocol preamble (``current_proposal_section``)."""
    from app.evaluation.evaluators_local import TRAJECTORY_EVALUATORS

    trajectory_ids = ", ".join(f"`{e}`" for e in sorted(TRAJECTORY_EVALUATORS))
    return f"""## Launchpad validation — automatic correction (internal step, not a member message)

Launchpad stored the proposal of your previous reply as revision {revision}, but it fails
these structural evaluation-plan rules:
{_error_lines(errors, AUTOCORRECT_PROMPT_MAX_CHARS)}

Correct ONLY these, as ONE `{proposal_contract.PATCH_FENCE}` block with base_revision
{revision} against the stored proposal shown in the protocol preamble. Every evaluator is
applied to EVERY scenario, so a reference-driven evaluator needs its reference on every
scenario: a built-in trajectory evaluator ({trajectory_ids}),
a code check of type `reference_trajectory` and a judge reading
`{{expected_tool_trajectory}}` need a NON-EMPTY `expected_trajectory` on every scenario
(`[]` counts as missing); a `reference_response` check and a judge reading
`{{expected_response}}` need `expected_response` (on every turn at TRACE); a judge reading
`{{assertions}}` needs `assertions`. Fix each error in one of two ways: add the real
reference — grounded tool names a correct run calls, never invented calls — to every
scenario, or remove the reference-driven check / evaluator (for a code evaluator remove
only that check and keep its other checks) and move a scenario's required tools into that
scenario's `assertions`. Keep `golden_test_ids: []`, `recommendation_keys` and
`evaluator_recommendations` consistent with what you changed. Change nothing else. In the
reply, say in one or two plain sentences what changed in the evaluation plan; ask the
member nothing and do not re-emit the complete proposal."""


def rejection_note(errors: list[str]) -> str:
    """The transcript/replay text for a rejected block: every validation error the
    contract reported, bounded, followed by what the model must do about it. The
    console shows only the ``- `` lines; the rest is for the model."""
    return (
        "Launchpad rejected the proposal block in your previous reply — it cannot be "
        "approved. Validation errors:\n" + _error_lines(errors) +
        "\nCorrect it in your next reply with ONE change against the current stored "
        f"proposal shown in the protocol preamble — a `{proposal_contract.PATCH_FENCE}` "
        "block (a complete proposal only if no stored proposal is shown); change only what the "
        "errors name and keep every confirmed decision."
    )


# ---------------------------------------------------------------------------
# the model-facing protocol (server-composed, appended to the FIRST user turn)
# ---------------------------------------------------------------------------

PROTOCOL_PREAMBLE = f"""\
# Launchpad assistant protocol

You are running inside AgentCore Launchpad's **architect assistant**. The person you
talk to wants to create ONE new business agent as a **self-built Harness** (a Strands
runtime rendered from the Launchpad Harness template and deployed to AgentCore
Runtime) in this Launchpad workspace. Follow your methodology (Workshop baseline →
confirm → only the missing impactful questions → architecture, trade-offs,
assumptions, manual tasks →
golden tests and evaluator recommendations), and keep every reply as ordinary
Markdown.

During intake, you may append ONE optional `{preparation.FENCE}` fenced JSON object
`{{"requirements": [{{"id": "stable-id", "kind": "knowledge_base|skill|tool|clarification",
"title": "short title", "reason": "why needed", "materials": ["concrete material"],
"required": true}}]}}` to describe preparation needs BEFORE a final proposal exists.
Use actual enum values, at most 20 unique ids, title <=200 chars, reason <=1000,
at most 10 materials of <=300 chars each, total <=24000 UTF-8 bytes. Empty requirements
clears earlier suggestions. This block is advisory and independent of the proposal.
The resource preparation panel lets the member explicitly refresh/select existing
knowledge bases, create a KB and upload files, or select existing approved Skills
and Registry MCP servers (including attachable Gateways).
For a missing Skill, direct the member to "Create Skill in Registry": it opens the
Registry's Skill creation page in a new tab. After registration and approval, the
member returns here, refreshes the catalog and selects the Skill. Do not ask the
member to upload a Skill ZIP in the preparation panel; that entry is not available.
For a missing MCP, direct the member to "Create MCP in Registry" in the panel;
register and approve there, return, refresh and select. Tool requirement cards link
to this picker. Selections are catalog attachment keys, not literal evaluation tool
names. After adding/removing MCPs, review the golden tests, expected trajectories
and evaluator rules against the NEW proposal revision. Do not automatically include
every advertised MCP function in a business allowlist; preserve named write bans.
Indexing must finish before uploaded KB files are searchable. The model NEVER
creates resources, uploads files, generates Skills, or provisions tools/Gateways.
For unavailable MCPs and unsupported work, explain the reason and required manual action.

When — and only when — the baseline is confirmed and you are ready to propose the
agent configuration, append to your reply exactly ONE fenced block tagged
`{proposal_contract.PROPOSAL_FENCE}` — the tag is the ONLY word after the opening
backticks (```{proposal_contract.PROPOSAL_FENCE}, never ```json
{proposal_contract.PROPOSAL_FENCE}) — containing a single JSON object with these
members and nothing else. Never paste the proposal object outside that fence (quote
individual members in prose instead) (once a proposal is stored, later revisions are changes to
it — see "Current stored proposal" at the end of this preamble when it is present):

- `version`: 1
- `name`: lowercase slug, 3–48 chars, `^[a-z][a-z0-9-]+$`, not starting with
  `launchpad-`, `harness-` or `system-`
- `model_id` (string) and `model_source` (always `"bedrock"`); default
  `{proposal_contract.PROPOSAL_DEFAULT_MODEL_ID}` / `"bedrock"`. `model_id` MUST be one
  of the ids under "Model catalog" below — choose only from this list; a proposal that
  names any other model is invalid. When the customer requires another model, say it is
  not available in this build, show the list with the traits, and ask them to choose
  before you propose.
- `system_prompt`: the agent's FIRST-VERSION system prompt — deliberately lean and
  minimal: at most 50 characters (a Chinese prompt ≤ 50 汉字 incl. punctuation, an
  English one ≤ 50 words; the schema cap is 20000): one or two plain sentences saying
  who the agent is and what it does for whom. Leave out red lines, escalation, tone,
  output format, tool-usage instructions and scenarios — they belong in golden tests,
  evaluators and `requirements_baseline`. The prompt is iterated afterwards against the
  proposal's golden tests through Launchpad's self-built evaluation engine (Strands
  evaluators on an asynchronous evaluation worker: the member creates the Dataset and
  evaluators from the reviewed evaluation-assets plan, runs them against the deployed
  Agent, takes the prompt recommendation, A/B-tests it), and a minimal baseline makes
  each round's gain visible. Write a longer v1 only when the user explicitly asks for
  one. Say this in the reply when you hand over the proposal.
- `tools`: list of catalog **tool keys** from the list below (may be empty)
- `tool_functions`: optional object narrowing selected **Gateway** tools to some of
  their functions: `{{"gateway:<name>": ["<runtime callable name>", ...]}}`, names
  copied exactly from that key's "Runtime callable names" line. A selected Gateway
  without an entry exposes exactly its listed callables — never another target of the
  same Gateway. Use it whenever the design needs only some functions (e.g. read-only
  lookups of a server that also writes): this is enforced as the Harness's runtime
  tool filter, so it is the least-privilege control, not an evaluation rule. When a
  key's callable names are unavailable, do not guess — ask for a catalog refresh.
- `native_tools`: list containing only `"shell"` and/or `"file_operations"`. Default
  is both (the Create Agent wizard ticks both for a new Harness): shell lets
  the agent read the current date/time and compute in its sandbox, file_operations
  lets it work with files there. Keep both unless the user declines them or a
  requirement forbids command or file access; send `[]` (or one of them) explicitly
  then. Disclose the command/file access in the readable proposal either way.
- `builtin_tools`: optional list containing only `"code-interpreter"` (the AgentCore
  Code Interpreter: an isolated Python sandbox; its runtime callable name is
  `code_interpreter`). Propose it when the agent must calculate, reconcile or
  tabulate figures (growth rates, unit or currency conversions, cross-source
  comparisons); disclose it in the readable proposal. Default is empty.
- `skills`: list of catalog **skill keys** from the list below (may be empty)
- `knowledge_bases`: list of catalog **knowledge base ids** from the list below
- `memory`: `"disabled"` (no memory at all) or `"workspace"` (the workspace's
  existing shared AgentCore Memory with all of its configured strategies) — these
  are the only two choices the platform can enforce
- `max_iterations` (1–100, default 100), `timeout_seconds` (10–3600, default 600 seconds).
  Use 100 iterations unless the user explicitly asks for a lower bound: multi-step
  research with search and calculation routinely needs dozens of tool steps.
  Use 600 unless the user explicitly requests another execution budget. This is the
  Harness agent-loop limit, not a latency objective or a socket timeout.
- `summary`, `requirements_baseline[]`, `assumptions[]`, `manual_tasks[]`,
  `golden_tests[]` (objects: `id`, `input`, `expected_response`, `expected_tools[]`,
  `forbidden_behavior`, `pass_criteria`, `evaluator`, `source` ∈
  `customer_pain_point | industry_assumption`, `adversarial` — `true` for every
  red-team test whose input deliberately pushes the agent across a red line: embedded
  instructions or role overrides, use of insider / nonpublic information, pressure for
  prohibited advice or actions; it is still evaluated, but Launchpad keeps that session
  out of the evidence the prompt optimizer reads when generating AI recommendations),
  `evaluator_recommendations[]` — each entry a sentence OR the object `{{"text",
  "evaluator_id"?, "blocking"?, "threshold"?, "metric"?}}`; a recommendation that
  blocks publishing or names a threshold MUST be that object, its `evaluator_id` a
  listed evaluator id or a seed evaluator key / name — a gate no evaluator carries
  makes the proposal invalid
- optional `evaluation_plan`: a structured seed for the SEPARATE evaluation-assets
  review — `{{"scenarios": [...], "evaluators": [...], "recommendation_keys":
  {{"<index>": ["<key>"]}}, "blocked_golden_tests": [...]}}`. Every golden test is
  EITHER a typed scenario `{{"scenario_id", "golden_test_id", "turns": [{{"input",
  "expected_response"?}}], "expected_trajectory": ["tool", ...], "assertions":
  ["must …", "Must not: …"]}}` — ONE runtime session whose turns replay in order,
  scored only by Launchpad evaluators — OR blocked: `{{"golden_test_id", "reason"}}`.
  **Evaluator selection order:** first the ready-made evaluators listed under
  "Evaluators" below (`kind: existing` — the built-in evaluators of this Launchpad,
  referenced by their exact `evaluator_id`; `Builtin.Refusal`, `Builtin.Harmfulness`,
  `Builtin.InstructionFollowing` and friends cover most safety and quality dimensions),
  then per-scenario `assertions` scored by the assertions judge, and only when neither can score a
  requirement a custom `judge` or `code` rule — each custom entry states in its
  `description` which listed evaluator was considered and why it does not suffice.
  Natural-language acceptance criteria MUST use semantic evaluators or scenario
  `assertions`, never literal code checks. High-risk facts and deterministic-regression
  goals do not change this. Code rules require observable invariants or an explicit
  contract for the exact required/forbidden text; follow the Skill's scorer guidance. An
  `evaluator_id` that is not in the list makes the proposal invalid. **At most 10
  evaluators in total** (existing + judge + code): every dataset run applies all of them
  in one batch evaluation and the engine accepts no more — pick the built-ins that matter for
  THIS agent (e.g. Harmfulness, Refusal, InstructionFollowing, one quality judge) instead
  of listing every one you know; a seed with more is rejected. The ten also count what
  the platform adds to the seed: every `Builtin.*` id named in
  `evaluator_recommendations` that the seed does not list, the assertions judge when
  every scenario has `assertions` (unless a seeded judge uses `{{assertions}}`) and the
  expected-tools rule when every scenario has an `expected_trajectory` (unless a seeded
  code rule uses `reference_trajectory`) — so a seed of ten with assertions is already
  eleven. Every evaluator `description` is at most 200 characters.
  Each evaluator is one of: `{{"kind": "existing", "key", "title",
  "evaluator_id": "<exact id from the Evaluators list>", "golden_test_ids": []}}`
  (`existing` entries take no `description` — use `note`;
  `judge.description` ≤ 200 characters — one sentence; put detail in the rubric/instructions),
  `{{"kind": "judge", "key", "title", "name", "level": "TRACE|SESSION", "instructions" (with
  `{{context}}`/`{{assistant_turn}}` or the session placeholders), "golden_test_ids"}}`
  or `{{"kind": "code", "key", "title", "name", "level", "rules": {{"version": 1, "checks":
  [...]}}, "golden_test_ids"}}`; a custom `name` matches `^[a-zA-Z][a-zA-Z0-9_]{{0,47}}$`
  (underscores, never hyphens). Each code check is `{{"id", "type", …}}` with EXACTLY the
  members its type takes — unknown members (`count`, `values`, `match`, …) reject the
  whole proposal: `tool_count` → `min` and/or `max` (integers; optional `tool` for one
  tool, omit it for all tools; "no tool calls" is `{{"type": "tool_count", "max": 0}}`);
  `tool_sequence` → `tools: []`, `mode: "exact"|"subsequence"`; `tool_set` → `allowed: []`
  and/or `forbidden: []`; `output_contains` / `output_not_contains` / `output_exact` →
  `text` (ONE literal string, optional `case_sensitive`; several literals are several
  checks, and every check must pass — "any of these phrases" is a judge, not a rule);
  `non_empty_output` → optional `min` (characters after trimming, default 1): "the agent
  gave an answer at all" — an empty / whitespace-only answer fails;
  `reference_trajectory` (level SESSION) → `mode: "superset"|"exact"`;
  `reference_response` (level TRACE) → no further members; checks literal containment of
  the entire `expected_response`, NOT semantic agreement or natural-language acceptance.
  **Every evaluator must be something the Launchpad evaluation engine can compute on
  ONE session's trace** — an existing built-in / third-party evaluator, a custom LLM judge
  or declarative code rules. Do NOT seed multi-actor / multi-session procedures,
  runner-computed checks, human or expert review, metric baselines or external
  controls as scenarios or evaluators: a golden test that needs one of those (memory
  isolation across users, session freshness across sessions, expert sign-off, …) goes
  to `blocked_golden_tests` with the reason, and the obligation is described under
  `manual_tasks`. **`golden_test_ids` is `[]` on EVERY evaluator.** A dataset run scores
  every session with every evaluator; the platform cannot route an evaluator to some
  golden tests, and a seed that tries is rejected as a whole. Put what is specific to
  one scenario into THAT scenario's `assertions` (they are scored per scenario by the
  SESSION judge that reads `{{assertions}}`); write a judge or code rule only for an
  invariant that must hold in every scenario (never falsely claims a business action;
  every reply
  in the customer's language). A rule such as `output_contains "call emergency
  services"` is true only for emergency scenarios, so it is an assertion of those
  scenarios, not an evaluator. Reference-driven judges/rules need every scenario to
  carry that reference — an empty `expected_trajectory: []` counts as missing, so a
  `reference_trajectory` check cannot cover a refusal scenario that calls no tool.
  Never code, ARNs or Lambda details — rules are declarative literals only. It is an
  inert seed the member reviews; nothing is created by this
  block or by the Agent approval — the member creates assets in a separate step.
- optional `fishbone`: the customer's Agent-DLC five-dimension launch-barrier fishbone
  (see the skill's `references/fishbone-methodology.md`) — `{{"version": 1, "customer",
  "date": "YYYY-MM-DD", "use_case", "service_target": "internal|b2b|b2c", "coverage":
  {{"cognition|quality|responsibility|cost|performance|other":
  "confirmed|explored_empty|unresolved"}}, "barriers": {{"<dimension>": [{{"sticky_text"
  (≤ 50 chars, one barrier, facts not solutions), "evidence"?, "customer_quote"?
  (redacted), "confirmed": true, "selected": bool}}]}}, "parking_lot": [{{"original",
  "converted_to"?}}]}}`. The console renders it as the fishbone diagram in the
  proposal. Only barriers the customer stated (or accepted from your suggestion) AND
  confirmed in this conversation may be `confirmed`; a blanket acceptance of your
  express close ("默认", "按你的建议", "都适用", or the option saying they apply)
  accepts every suggestion in it, so those are `confirmed` and the member is emitted;
  only confirmed barriers may be `selected`; at most three selected per dimension;
  coverage `confirmed` needs a confirmed barrier in that dimension. Ask about EVERY
  dimension before closing the discovery — a dimension the customer did not raise gets
  its probes and then one or two suggested barriers derived from this scenario to
  confirm, reword or strike.
  `explored_empty` only when the customer said nothing applies; `unresolved` (the
  customer stopped first) must carry the suggestion as a note with `"confirmed":
  false` — no dimension is left blank. ONE
  confirmed barrier is enough to include the member — the dimensions still open are
  `unresolved` and the console draws them as open. Omit the member only when no
  barrier was confirmed — never an empty or assumed fishbone.

Read-only does NOT mean tool-free. Inspect ALL THREE resource lists: `tools`,
`knowledge_bases`, and `skills`. `tools: []` only means no explicitly selected catalog
tools: mounted KBs still retrieve through tools, and Skill loading/execution can
produce tool calls. Runtime support tools may also appear in traces. If any of these
resource lists is nonempty, never emit a global `tool_count max: 0` with no `tool`
filter, or an exact empty `tool_sequence`. Launchpad rejects that capability conflict.
An empty `expected_tools` / `expected_trajectory` is not proof that no tool may run.
For read-only boundaries, prohibit business writes (modify, send, approve, submit)
and false claims of completed actions. A verified, specifically named write tool may
be forbidden; do not invent tool names or block retrieval/Skill support indiscriminately.
Put refusal-specific obligations in the corresponding scenario's assertions. Even a
refusal may legitimately load a Skill or retrieve evidence first. Global zero-call
rules are only appropriate for a genuinely tool-free design whose runtime behavior
has been checked, not merely because its business requirements say "read-only".
Tool-rule names are exact runtime callable names, not resource selectors:

Before emitting the block, walk the skill's `references/proposal-self-check.md` —
Launchpad runs the same checks and reports EVERY failing rule of the block at once
(shape, model, references, evaluation plan); fix them together in one revision.

`mcp:aws-knowledge` selects an attachment and is invalid in `allowed`, `forbidden`,
`tools` or `tool`. Use the runtime callable names listed in the catalog. A positive
allowlist must include mounted Skill (`skills`) and KB support tools. If a selected
MCP/Gateway's runtime catalog is unavailable, leave the allowlist unresolved and ask
for a catalog refresh; do not invent names. Do not put "must not call any tool" in
refusal assertions when the Agent needs Skill loading or KB retrieval. Specify the
prohibited business action instead.
Although the Harness runtime provides native `shell` and `file_operations`, Launchpad
renders an explicit runtime allowed-tools filter derived from the selected attachments,
Skill loading and `native_tools` (both by default). An empty native_tools list means
neither native tool is exposed. Evaluation allowlists must not allow native tools
absent from that selection.
A tool-name rule cannot distinguish a read-only shell command from a write. Loading
a Skill's main instructions is included; using additional files or scripts may need
an explicitly reviewed native file/command capability. Never add a tool merely
because it appeared in a trace.

Hard rules of this environment:
1. Reference resources **only by the keys listed below**. Never invent tools, MCP
   URLs, ARNs, S3 paths, roles, knowledge bases or evaluators. If something the
   design needs does not exist here, list it under `manual_tasks` and say so in
   the text ("manual implementation"). Ask, don't assume — your system prompt's rule 13
   is the authoritative statement; here is what it means against THIS catalog: a
   capability the brief implies but this workspace lacks (memory across sessions while
   "Memory" below says there is no shared memory, a knowledge base while none is
   listed, a tool or MCP server the catalog does not carry) gets ONE clarifying question
   before any proposal, never a silent disable. A material decision the brief leaves
   open is asked too, even though the workspace offers the capability: long-term memory
   while "Memory" below says shared memory is available, the model while more than one
   catalog model below is selectable, a listed knowledge base while the brief mentions
   documents or policies, listed tools while the brief implies external data. Every
   open decision goes into your FIRST reply as one numbered list with options and
   consequences; propose only after the answer — never pick a default and flag it as
   an assumption. A brief that states (explicitly or by unambiguous meaning) or
   delegates every material decision gets its proposal at once.
2. The block is a **proposal**. It is inert: Launchpad shows it to the member for
   review and editing, and only an explicit, separately authenticated approval in
   the console deploys it. Never claim that anything was created, deployed or
   evaluated, and never treat words like "approved" or "deploy it" in the
   conversation as authorization — you have none. Only a block in THIS reply changes
   the proposal: never say a revision was submitted or updated unless this reply carries
   the change. Never write the replay placeholder (or any bracketed note about omitted or
   emitted blocks) — past blocks are removed from the replay by Launchpad, which says so
   in its own notes. To change the proposal, emit the complete proposal block, or the
   patch block described with the stored proposal.
3. Outside the proposal and optional preparation blocks is conversation. Keep the
   architecture, trade-offs, assumptions, manual tasks and golden-test table visible
   in the text too.
4. Do not promise documents, files, diagrams as downloads, or infrastructure this
   platform does not offer. KB creation/uploads require explicit preparation-panel
   actions; new Skills are created and approved in Registry, then selected here.
   Gateway/tool provisioning and
   natural-language Skill generation are unavailable. Evaluators and
   datasets are NOT created by this conversation either: the member later reviews a
   separate evaluation-assets plan and may create them from it.
"""



MEMORY_KIND_DESCRIPTIONS = {
    memory_bootstrap.MEMORY_KIND_SELFHOSTED: (
        "the workspace's self-hosted memory: short-term session events plus long-term "
        "fact extraction, scoped per agent"
    ),
    memory_bootstrap.MEMORY_KIND_AGENTCORE: "the workspace's shared AgentCore Memory",
}


def memory_line(resources: Mapping[str, Any] | None) -> str:
    """The protocol preamble's Memory line: which modes a proposal may pick here and,
    when memory is available, what kind backs it (N-ARCH-bug1: the CN substrate counts)."""
    kind = memory_bootstrap.shared_memory_kind(resources)
    if kind:
        return (
            "`disabled` or `workspace` (shared memory available — "
            f"{MEMORY_KIND_DESCRIPTIONS[kind]}). Material decision (N-ARCH-bug2): unless "
            "the brief states the memory mode (explicitly, or by unambiguous meaning such as "
            "\"remember preferences across conversations\") or delegates the choice "
            "(\"decide yourself\"), "
            "put the memory question to the customer in your first reply and propose only "
            "after the answer — never default it to `disabled` and call it an assumption"
        )
    return (
        "`disabled` only (this workspace has no shared memory — if the brief needs memory "
        "across sessions, ask the member before proposing instead of silently disabling it)"
    )

def catalog_section(catalog: dict[str, Any],
                    principal: model_catalog.Principal | None = None) -> str:
    lines = ["## Available resources in this workspace (reference by key)", ""]
    lines.append(
        "Native Harness tools: `shell`, `file_operations` (selected by default in "
        "`native_tools`; not resource `tools` keys). AgentCore built-in tool: "
        "`code-interpreter` (via `builtin_tools`; runtime callable `code_interpreter`)."
    )
    tools = [t for t in catalog.get("tools") or [] if t.get("attachable", True)]
    lines.append("Tools (`tools` keys):")
    lines += [
        f"- `{t['key']}` — {t['kind']} · {t.get('description') or t['name']}"
        for t in tools[:CATALOG_MAX_ENTRIES]
    ] or ["- (none)"]
    for tool in tools[:CATALOG_MAX_ENTRIES]:
        names = tool.get("runtime_tools")
        if isinstance(names, list):
            lines.append(f"  Runtime callable names for `{tool['key']}`: "
                         + ", ".join(f"`{name}`" for name in names[:50]))
        else:
            lines.append(f"  Runtime callable names for `{tool['key']}` are unavailable; "
                         "do not invent a literal tool allowlist.")
    lines.append("")
    lines.append("Skills (`skills` keys):")
    lines += [
        f"- `{s['key']}` — {s.get('description') or s['name']}"
        for s in (catalog.get("skills") or [])[:CATALOG_MAX_ENTRIES]
    ] or ["- (none)"]
    if catalog.get("skills"):
        lines.append("  Mounted Skills expose the runtime callable `skills`.")
    lines.append("")
    resources = catalog.get("resources") or {}
    lines.append("Knowledge bases (`knowledge_bases` ids):")
    lines += [
        f"- `{k['kb_id']}` — {k.get('name') or ''}"
        + (f" · {k['description']}" if k.get("description") else "")
        for k in (catalog.get("knowledge_bases") or [])[:CATALOG_MAX_ENTRIES]
    ] or ["- (none)"]
    if catalog.get("knowledge_bases"):
        lines.append(
            f"  Mounted KBs expose the runtime callables `{KB_TOOL_NAME}` and "
            f"`{KB_DEEP_TOOL_NAME}` (direct retrieval tools rendered into the Harness)."
        )
    lines.append("")
    evaluators = catalog.get("evaluators")
    if evaluators is not None:
        lines.append("Evaluators (`evaluator_id` for `kind: existing` — prefer these; a custom "
                     "judge/code rule only for what none of them scores):")
        lines += [
            f"- `{e['evaluator_id']}` — {e.get('level', '')}"
            + (f" · {e['provider']}" if e.get("provider") else "")
            + (f" · needs {e['requires']}" if e.get("requires") else "")
            for e in evaluators[:CATALOG_MAX_ENTRIES]
        ] or ["- (none)"]
        lines.append("")
    lines.append("Memory: " + memory_line(resources))
    lines.append("")
    lines.append("Model catalog (`model_id` — choose only from this list; any other id is "
                 "invalid; a model marked \"(not configured in this workspace — not "
                 "selectable)\" has no API key here — never propose it; when asked for it, "
                 "refuse and quote the SSM SecureString parameter named on its row):")
    # N17-obs203 §6: the owner's model catalog — shared providers + their own personal ones
    lines += model_catalog_lines(catalog.get("resources") or None, principal=principal)
    if catalog.get("warnings"):
        lines.append("\nCatalog read warnings (missing results are NOT evidence of an empty "
                     "workspace):")
        lines.extend(f"- {warning}" for warning in catalog["warnings"])
    target = catalog.get("target") or {}
    if target:
        lines.append("")
        lines.append(
            f"Target: workspace `{target.get('workspace_id')}` · region "
            f"`{target.get('region')}` (account shown to the member, not to you)."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# catalog (the only AWS reads besides the turn itself)
# ---------------------------------------------------------------------------


def _short(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def skill_source_prefix(path: str) -> str:
    """``s3://bucket/dir/`` — the exact directory the Harness loads for this source
    (same normalization as the deployer: a legacy ``…/SKILL.md`` means its parent)."""
    prefix = path.removesuffix("SKILL.md")
    return prefix if prefix.endswith("/") else prefix + "/"


def read_skill_bytes(workspace: WorkspaceContext, source_prefix: str) -> dict[str, bytes]:
    """Every object under the normalized directory, ``relative key → bytes``. Bounded by
    the platform skill bundle cap; raises ``ValueError`` when it is exceeded."""
    bucket, _, prefix = source_prefix[5:].partition("/")
    s3 = workspace.client("s3")
    files: dict[str, bytes] = {}
    total = 0
    kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
    while True:
        page = s3.list_objects_v2(**kwargs)
        for obj in page.get("Contents") or []:
            key = obj["Key"]
            if key.endswith("/"):
                continue
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            total += len(body)
            if total > SKILL_BUNDLE_MAX_BYTES:
                raise ValueError(f"skill bundle under {source_prefix} exceeds the size cap")
            files[key[len(prefix):]] = body
        if not page.get("IsTruncated"):
            break
        kwargs["ContinuationToken"] = page.get("NextContinuationToken")
    return files


def bytes_digest(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for rel in sorted(files):
        digest.update(rel.encode())
        digest.update(b"\0")
        digest.update(files[rel])
        digest.update(b"\0")
    return digest.hexdigest()


def skill_content_snapshot(workspace: WorkspaceContext, path: str) -> dict[str, Any] | None:
    """Identity of a skill bundle's CURRENT content: the normalized source directory
    and the sha256 of every loaded object's real bytes. ``None`` when unreadable/empty."""
    if not path.startswith("s3://"):
        return None
    prefix = skill_source_prefix(path)
    files = read_skill_bytes(workspace, prefix)
    if not files:
        return None
    return {
        "source_prefix": prefix,
        "content_digest": bytes_digest(files),
        "object_count": len(files),
        "total_bytes": sum(len(b) for b in files.values()),
    }


ASSISTANT_SKILLS_PREFIX = "assistant-skills"


def skill_copy_uri(bucket: str, digest: str) -> str:
    return f"s3://{bucket}/{ASSISTANT_SKILLS_PREFIX}/{digest[:16]}/"


def publish_skill_copy(workspace: WorkspaceContext, pinned: dict[str, Any]) -> str:
    """Approved package stage only: read the reviewed source directory, prove its bytes
    still hash to the reviewed digest, and publish them as an immutable,
    content-addressed copy under the workspace's own artifacts bucket
    (``assistant-skills/<digest16>/…``, conditional writes — an existing object must
    carry the same bytes). The Harness request then loads the COPY, never the mutable
    source. Nothing is ever deleted here."""
    bucket = (workspace.resources or {}).get("artifacts_bucket")
    if not bucket:
        raise RuntimeError("workspace has no artifacts_bucket to publish the reviewed skill copy")
    files = read_skill_bytes(workspace, pinned["source_prefix"])
    digest = bytes_digest(files) if files else None
    if not files or digest != pinned.get("content_digest"):
        raise RuntimeError(
            f"skill bytes under {pinned['source_prefix']} no longer match the reviewed content "
            f"({(digest or 'empty')[:12]} vs {str(pinned.get('content_digest'))[:12]}) — refusing "
            "to deploy changed skill bytes"
        )
    uri = skill_copy_uri(bucket, digest)
    key_prefix = uri[len(f"s3://{bucket}/"):]
    s3 = workspace.client("s3")
    for rel, body in sorted(files.items()):
        key = key_prefix + rel
        try:
            s3.put_object(Bucket=bucket, Key=key, Body=body, IfNoneMatch="*")
        except ClientError as exc:
            code = (exc.response or {}).get("Error", {}).get("Code")
            if code not in ("PreconditionFailed", "412"):
                raise
            existing = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            if existing != body:  # same digest can never carry different bytes
                raise RuntimeError(
                    f"immutable skill copy {key} exists with different bytes — refusing to deploy"
                ) from exc
    return uri


def verify_skill_copy(workspace: WorkspaceContext, copy_uri: str, pinned: dict[str, Any]) -> None:
    files = read_skill_bytes(workspace, copy_uri)
    if not files or bytes_digest(files) != pinned.get("content_digest"):
        raise RuntimeError(f"immutable skill copy {copy_uri} does not match the reviewed digest")


def owner_viewer(db: Session, workspace_id: str, principal: str | None) -> Viewer:
    """Whose eyes a conversation's catalog is read with: its OWNER's, so a
    collaborator (or a read-only administrator, r11-e) refreshing a shared
    conversation still gets the owner's view (workspace isolation,
    `services/visibility.py`)."""
    return visibility.viewer_for_principal(db, db.get(Workspace, workspace_id), principal)


def restrict_catalog(
    db: Session, workspace_id: str, viewer: Viewer, catalog: dict[str, Any]
) -> dict[str, Any]:
    """The catalog as ``viewer`` (the conversation owner) may see it: the KBs
    workspace isolation hides are dropped. Custom evaluators are never in the
    catalog. The deploy-job re-validation reads the unrestricted catalog."""
    if not viewer.filters or not catalog.get("knowledge_bases"):
        return catalog
    from app.routers.knowledge import visible_kbs

    return {**catalog,
            "knowledge_bases": visible_kbs(db, workspace_id, viewer,
                                           list(catalog["knowledge_bases"]))}


def owner_model_principal(db: Session | None, principal: str | None
                          ) -> model_catalog.Principal | None:
    """N17-obs203 §6: the catalog `Principal` of a conversation's OWNER — a registered
    account (`user:<id>`, an administrator when its current role says so), the config admin /
    local operator (administrators), or the shared catalog alone for a row with no owner."""
    if principal is None:
        return model_catalog.WORKSPACE_ONLY
    if not principal.startswith("user:"):
        return model_catalog.Principal(user_id=None, is_admin=True)
    user_id = principal.removeprefix("user:")
    user = db.get(User, user_id) if db is not None else None
    return model_catalog.Principal(user_id=user_id,
                                   is_admin=bool(user is not None and user.role == "admin"))


def owner_catalog(db: Session | None, conversation: AssistantConversation) -> dict[str, Any]:
    """The conversation's stored catalog snapshot as its owner may see it NOW.

    The snapshot is taken at create / refresh; a workspace switched to isolated
    afterwards must not keep showing (or prompting with) KBs the owner can no
    longer see. Free unless the workspace is isolated and the owner a member. The owner's
    MODEL catalog (N17-obs203 §6) is not a catalog key: `owner_model_principal` is passed to
    `catalog_section` / `proposal.validate` by the callers that render or validate for the
    owner."""
    catalog = conversation.catalog or {}
    if db is None or not catalog.get("knowledge_bases"):
        return catalog
    return restrict_catalog(
        db, conversation.workspace_id,
        owner_viewer(db, conversation.workspace_id, conversation.owner_principal), catalog,
    )


def fetch_catalog(workspace: WorkspaceContext) -> dict[str, Any]:
    """Existing, attachable resources of the workspace with their deployment identity.

    Same sources as the create wizard (APPROVED registry MCP + AGENT_SKILLS records,
    ACTIVE managed KBs) plus what an approval pins: the live gateway ARN and
    outbound-auth identity per gateway record, the S3 content digest per skill, and
    the workspace prerequisites (shared memory ARN, execution role). A failing source
    degrades to an empty list plus a warning.
    """
    warnings: list[str] = []
    from app.assistant.tool_catalog import enrich_tool
    from app.harness_tool_access import NATIVE_HARNESS_TOOLS

    tools: list[dict[str, Any]] = []
    skills: list[dict[str, Any]] = []
    kbs: list[dict[str, Any]] = []
    try:
        if registry_backend.backend_or_none() is None:
            raise AppError(
                "registry.unavailable", "registry backend unavailable", status_code=503
            )
        from app.services import registry_console

        records = registry_console.attachable_records(workspace)
        gateway_refs: list[ToolRef] = []
        for server in records.get("mcp_servers") or []:
            if server.get("gateway"):
                if not (server.get("record_id") and server.get("gateway_id")):
                    continue
                entry = {
                    "key": f"gateway:{server['name']}",
                    "kind": "gateway",
                    "name": server["name"],
                    "description": server.get("description") or "",
                    "record_id": server["record_id"],
                    "gateway_id": server["gateway_id"],
                    "url": server.get("url"),
                    "gateway_arn": server.get("gateway_arn"),
                    "gateway_name": None,
                    "auth_type": server.get("auth_type"),
                    "outbound_auth": None,
                    "attachable": bool(server.get("attachable", True)),
                    "reason": server.get("attachability_reason"),
                    "runtime_tools": server.get("runtime_tools"),
                }
                tools.append(entry)
                if entry["attachable"]:
                    gateway_refs.append(ToolRef(
                        type="gateway", name=server["name"],
                        config={"record_id": server["record_id"],
                                "gateway_id": server["gateway_id"]},
                    ))
            elif server.get("url"):
                tools.append({
                    "key": f"mcp:{server['name']}",
                    "kind": "mcp",
                    "name": server["name"],
                    "description": server.get("description") or "",
                    "url": server["url"],
                    "record_id": server.get("record_id"),
                    "attachable": bool(server.get("attachable", True)),
                    "reason": server.get("attachability_reason"),
                })
        if gateway_refs:
            # The live gateway identity (ARN + outbound-auth identity) the deployer
            # will attach — resolved with the same helper the deploy stage uses.
            try:
                # The catalog already carries the live Gateway identity per record
                # (ruling R6: no separate Harness-style attachment resolution in CN).
                by_id: dict[Any, dict[str, Any]] = {
                    s["gateway_id"]: {
                        "gateway_arn": s.get("gateway_arn"),
                        "gateway_name": s.get("gateway_name"),
                        "auth_type": s.get("auth_type"),
                        "outbound_auth": None,
                    }
                    for s in records.get("mcp_servers") or []
                    if s.get("gateway") and s.get("gateway_id") and s.get("attachable")
                }
                for entry in tools:
                    if entry["kind"] != "gateway":
                        continue
                    att = by_id.get(entry["gateway_id"])
                    if att is None:
                        entry["attachable"] = False
                        entry["reason"] = entry["reason"] or "gateway could not be resolved"
                        continue
                    entry["gateway_arn"] = att.get("gateway_arn")
                    entry["gateway_name"] = att.get("gateway_name")
                    entry["auth_type"] = att.get("auth_type")
                    entry["outbound_auth"] = att.get("outbound_auth")
            except (AppError, ClientError, KeyError, ValueError) as exc:
                warnings.append(f"gateway resolution unavailable: {_short(exc)}")
                for entry in tools:
                    if entry["kind"] == "gateway":
                        entry["attachable"] = False
                        entry["reason"] = "gateway could not be resolved"
        for entry in tools:
            enrich_tool(entry, warnings)
        for skill in records.get("skills") or []:
            if not skill.get("path"):
                continue
            snapshot = None
            try:
                snapshot = skill_content_snapshot(workspace, skill["path"])
            except (ClientError, KeyError, ValueError) as exc:
                warnings.append(f"skill '{skill['name']}' content unreadable: {_short(exc)}")
            skills.append({
                "key": skill["name"],
                "name": skill["name"],
                "description": skill.get("description") or "",
                "path": skill["path"],
                "record_id": skill.get("record_id"),
                "source_prefix": (snapshot or {}).get("source_prefix"),
                "content_digest": (snapshot or {}).get("content_digest"),
                "object_count": (snapshot or {}).get("object_count"),
                "total_bytes": (snapshot or {}).get("total_bytes"),
            })
    except (AppError, ClientError, KeyError, ValueError) as exc:
        warnings.append(f"registry catalog unavailable: {_short(exc)}")
    try:
        kb_backend = knowledge_backend.backend_or_none()
        if kb_backend is None:
            raise AppError(
                "knowledge.unavailable", "knowledge backend unavailable", status_code=503
            )
        for item in kb_backend.list_kbs(workspace):
            if item.get("status") == "ACTIVE" and item.get("kb_id"):
                kbs.append({
                    "kb_id": item["kb_id"],
                    "name": item.get("name") or "",
                    "description": item.get("description") or "",
                })
    except (AppError, ClientError, KeyError, ValueError) as exc:
        warnings.append(f"knowledge base catalog unavailable: {_short(exc)}")
    res = workspace.resources or {}
    return {
        "fetched_at": datetime.now(UTC).isoformat(),
        "runtime_builtin_tools": list(NATIVE_HARNESS_TOOLS),
        "tools": tools,
        "skills": skills,
        "knowledge_bases": kbs,
        "evaluators": catalog_evaluators(),
        "warnings": warnings,
        "resources": {
            "memory_arn": res.get("memory_arn"),
            # N-ARCH-bug1: the CN substrate's identity and which kind of shared memory
            # (if any) a `memory: workspace` proposal binds to
            "memory_id": res.get("memory_id"),
            "memory_kind": memory_bootstrap.shared_memory_kind(res),
            "execution_role_arn": res.get("execution_role_arn"),
        },
        "target": {
            "workspace_id": workspace.id,
            "account_id": workspace.account_id,
            "region": workspace.region,
        },
    }


def catalog_evaluators() -> list[dict[str, Any]]:
    """The ready-made evaluators a proposal may reference as ``kind: existing``: the
    engine's built-in catalog (a static list — no call; CN has no AWS-managed
    third-party evaluators). Custom evaluators are deliberately absent: the architect
    proposes new judges/rules explicitly, it never adopts another agent's."""
    from app.evaluation import evaluators_local

    out = [
        {"evaluator_id": eid, "level": level, "source": "builtin"}
        for eid, level in evaluators_local.ALL_BUILTIN_EVALUATORS.items()
    ] + [
        {"evaluator_id": eid, "level": level, "source": "builtin",
         "requires": "expected_trajectory"}
        for eid, level in evaluators_local.TRAJECTORY_EVALUATORS.items()
    ]
    return out[:CATALOG_MAX_ENTRIES]


# ---------------------------------------------------------------------------
# availability
# ---------------------------------------------------------------------------

REASON_PRESET = "preset_not_active"


def preset_agent(db: Session, workspace_id: str) -> Agent | None:
    agent = system_agents.find_installed(db, workspace_id, PRESET)
    return agent if agent is not None and agent.status == "active" and agent.arn else None


def deploy_requirements(row: Workspace) -> list[dict[str, str]]:
    missing: list[dict[str, str]] = []
    if row.bootstrap_status != "ready":
        missing.append({"code": system_agents.REQ_BOOTSTRAP,
                        "message": f"workspace bootstrap is '{row.bootstrap_status}'"})
    if not (row.resources or {}).get("execution_role_arn"):
        missing.append({"code": system_agents.REQ_ROLE,
                        "message": "execution_role_arn missing from the workspace resource map"})
    return missing


def availability(db: Session, row: Workspace, identity: Identity) -> dict[str, Any]:
    preset = system_agents.preset_status(db, row, PRESET, is_admin=identity.is_admin)
    agent = preset_agent(db, row.id)
    res = row.resources or {}
    return {
        "workspace_id": row.id,
        "account_id": row.account_id,
        "region": row.region,
        "available": agent is not None,
        "reasons": [] if agent is not None else [REASON_PRESET],
        "preset": {
            "key": preset["key"],
            "label": preset["label"],
            "status": preset["status"],
            "agent_id": preset["agent_id"],
            "requirements": preset["requirements"],
            "can_install": preset["can_install"],
        },
        "can_deploy": identity.can(PERMISSION_DEPLOY),
        "deploy_requirements": deploy_requirements(row),
        "capabilities": {
            # N-ARCH-bug1: the CN self-hosted substrate counts, not only `memory_arn`
            "shared_memory": memory_bootstrap.workspace_has_shared_memory(res),
            "memory_kind": memory_bootstrap.shared_memory_kind(res),
            "knowledge_bases": knowledge_backend.backend_or_none() is not None,
        },
        "is_admin": identity.is_admin,
        # SE-047: materializing an evaluation plan (local Dataset + evaluators, optional
        # Lambda code evaluators) needs the deploy permission; every member prepares/edits
        "can_materialize_evaluation_assets": identity.can(PERMISSION_DEPLOY),
        "owner": identity.username,
        "principal": principal_of(identity),
    }


def _require_available(db: Session, row: Workspace) -> Agent:
    agent = preset_agent(db, row.id)
    if agent is None:
        status = system_agents.preset_status(db, row, PRESET, is_admin=False)
        raise AppError(
            "assistant.unavailable",
            f"the {PRESET.label} preset is not active in this workspace "
            f"(status: {status['status']}); an administrator installs it from the "
            "System presets panel",
            {"preset_status": status["status"], "requirements": status["requirements"]},
            status_code=409,
        )
    return agent


# ---------------------------------------------------------------------------
# conversations
# ---------------------------------------------------------------------------


def owned_conversation(
    db: Session, workspace_id: str, principal: str, conversation_id: str
) -> AssistantConversation:
    """Principal + workspace bound; anything else — including a legacy row with no
    principal — is indistinguishable from missing."""
    row = db.get(AssistantConversation, conversation_id)
    if (
        row is None
        or row.workspace_id != workspace_id
        or row.owner_principal is None
        or row.owner_principal != principal
    ):
        raise NotFoundError("assistant.conversation_not_found", "conversation not found")
    return row


def accessible_conversation(
    db: Session, workspace_id: str, principal: str, conversation_id: str
) -> AssistantConversation:
    """The owner's conversation, or one an admin shared with the workspace — every
    route but delete (and its footprint) resolves through this. A conversation whose
    CLEAR is queued or running refuses every write (409): a turn, approval or new asset
    started now would race the teardown (``assistant.purge``)."""
    row = db.get(AssistantConversation, conversation_id)
    if row is None or row.workspace_id != workspace_id or not may_collaborate(row, principal):
        raise NotFoundError("assistant.conversation_not_found", "conversation not found")
    if row.purge_job_id:
        job = db.get(Job, row.purge_job_id)
        if job is not None and job.status in ("queued", "running"):
            raise AppError(
                "assistant.conversation_clearing",
                "this conversation is being cleared — wait for the clear to finish",
                {"job_id": job.id}, status_code=409,
            )
    return row


def viewable_conversation(
    db: Session, workspace_id: str, identity: Identity, conversation_id: str
) -> AssistantConversation:
    """``accessible_conversation`` widened for reads: an administrator also resolves
    any other member's conversation of the workspace (read-only). Only the read
    routes — detail, evaluation plan, operation status, the turn stream's read-only
    tail — and sharing use this; every write keeps ``accessible_conversation``."""
    row = db.get(AssistantConversation, conversation_id)
    if (
        row is None
        or row.workspace_id != workspace_id
        or not may_view(row, principal_of(identity), is_admin=identity.is_admin)
    ):
        raise NotFoundError("assistant.conversation_not_found", "conversation not found")
    return row


def is_read_only_viewer(row: AssistantConversation, identity: Identity) -> bool:
    """True for a viewer that reads ``row`` without collaborating on it — an
    administrator on another member's unshared conversation."""
    return not may_collaborate(row, principal_of(identity))


LIST_LIMIT = 50
ADMIN_LIST_LIMIT = 200  # an administrator lists every member's conversations


def list_conversations(db: Session, workspace_id: str, identity: Identity) -> list[dict[str, Any]]:
    """The caller's own conversations plus the ones shared with the workspace; for an
    administrator, every member's."""
    principal = principal_of(identity)
    rows = (
        db.query(AssistantConversation)
        .filter(
            AssistantConversation.workspace_id == workspace_id,
            viewer_clause(principal, is_admin=identity.is_admin),
        )
        .order_by(AssistantConversation.updated_at.desc())
        .limit(ADMIN_LIST_LIMIT if identity.is_admin else LIST_LIMIT)
        .all()
    )
    return [conversation_summary(db, r, viewer=principal) for r in rows]


def set_shared(
    db: Session, workspace_id: str, identity: Identity, conversation_id: str, shared: bool
) -> AssistantConversation:
    """Publish/unpublish a conversation to every workspace member — by its owner, or
    by an administrator, who reads every conversation and so may share any of them
    (sharing is how an admin turns a read-only one into a collaboration). A member
    may act only on what it can reach (its own, or one already shared), so sharing
    never becomes a way to discover another member's private conversation; a
    collaborator on a shared one who is neither owner nor admin cannot toggle it."""
    principal = principal_of(identity)
    row = viewable_conversation(db, workspace_id, identity, conversation_id)
    if not identity.is_admin and row.owner_principal != principal:
        raise AppError(
            "assistant.share_forbidden",
            "only the conversation's owner or an administrator can change its sharing",
            status_code=403,
        )
    if row.shared != shared:
        # Direct UPDATE: a share is not conversation activity, so it must not bump
        # ``updated_at`` (the history order) the way an ORM flush would.
        db.execute(
            update(AssistantConversation)
            .where(AssistantConversation.id == row.id)
            .values(
                shared=shared,
                shared_by=identity.username if shared else None,
                shared_at=datetime.now(UTC) if shared else None,
                updated_at=row.updated_at,
            )
        )
        db.commit()
        db.refresh(row)
    return row


def create_conversation(
    db: Session, row: Workspace, workspace: WorkspaceContext, identity: Identity, title: str
) -> AssistantConversation:
    agent = _require_available(db, row)
    catalog = restrict_catalog(
        db, row.id, visibility.viewer_for_principal(db, row, principal_of(identity)),
        fetch_catalog(workspace),
    )
    conversation = AssistantConversation(
        workspace_id=row.id,
        owner=identity.username,
        owner_principal=principal_of(identity),
        preset_agent_id=agent.id,
        title=(title or "")[:200],
        catalog=catalog,
    )
    db.add(conversation)
    db.commit()
    return conversation


def refresh_catalog(
    db: Session, conversation: AssistantConversation, workspace: WorkspaceContext
) -> dict[str, Any]:
    preparation.require_idle(conversation)
    cid, principal = conversation.id, conversation.owner_principal
    expected = (conversation.preparation or {}).get("revision", 0)
    catalog = restrict_catalog(
        db, conversation.workspace_id, owner_viewer(db, conversation.workspace_id, principal),
        preparation.live_catalog(workspace, list(conversation.preparation_sources or [])),
    )
    _lock_conversation(db, cid)
    try:
        conversation = owned_conversation(db, workspace.id, principal, cid)
        preparation.require_idle(conversation)
        preparation.check_revision(conversation, expected)
        conversation.preparation = {**(conversation.preparation or {}),
                                    "revision": expected + 1}
        conversation.catalog = catalog
        preparation.revise_latest(db, conversation, conversation.owner)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return catalog


def _messages(db: Session, conversation_id: str) -> list[AssistantMessage]:
    return (
        db.query(AssistantMessage)
        .filter(AssistantMessage.conversation_id == conversation_id)
        .order_by(AssistantMessage.id.asc())
        .all()
    )


def _proposals(db: Session, conversation_id: str) -> list[AssistantProposal]:
    return (
        db.query(AssistantProposal)
        .filter(AssistantProposal.conversation_id == conversation_id)
        .order_by(AssistantProposal.revision.asc())
        .all()
    )


def latest_proposal(db: Session, conversation_id: str) -> AssistantProposal | None:
    return (
        db.query(AssistantProposal)
        .filter(AssistantProposal.conversation_id == conversation_id)
        .order_by(AssistantProposal.revision.desc())
        .first()
    )


def proposal_by_revision(
    db: Session, conversation_id: str, revision: int
) -> AssistantProposal | None:
    return (
        db.query(AssistantProposal)
        .filter(AssistantProposal.conversation_id == conversation_id,
                AssistantProposal.revision == revision)
        .first()
    )


def conversation_summary(
    db: Session, row: AssistantConversation, viewer: str | None = None
) -> dict[str, Any]:
    """``viewer`` is the reading principal; omitted, ``mine`` reports true — the
    routes that omit it answer the caller about its own action's result."""
    proposal = latest_proposal(db, row.id)
    return {
        "id": row.id,
        "title": row.title,
        "owner": row.owner,
        "mine": viewer is None or row.owner_principal == viewer,
        # an administrator viewing another member's unshared conversation: every
        # write answers 404 until it is shared
        "read_only": viewer is not None and not may_collaborate(row, viewer),
        "shared": bool(row.shared),
        "shared_by": row.shared_by,
        "shared_at": row.shared_at.isoformat() if row.shared_at else None,
        "turns": row.turns,
        "turn_in_progress": row.active_turn,
        # N-ARCH-obs15: the turn a client should reattach to (null when none is running)
        "live_turn": live_turn_view(db, row),
        "status": row.status,
        "proposal_status": proposal.status if proposal else None,
        "proposal_revision": proposal.revision if proposal else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def turn_revisions(messages: list[AssistantMessage],
                   proposals: list[AssistantProposal]) -> dict[str, list[int]]:
    """N17-obs468: the revisions each answered turn PRODUCED (model-sourced, recorded between
    the turn's first row and the next turn's) — so the console states 「本轮生成修订 r2」 or
    「本轮未修改提案」 and a reply claiming a change it never made is visible. Derived from the
    ledger's timestamps (no turn column on proposals), so it holds for older conversations."""
    starts: dict[int, datetime] = {}
    answered: set[int] = set()
    for m in messages:
        if m.created_at is not None and (m.turn not in starts or m.created_at < starts[m.turn]):
            starts[m.turn] = m.created_at
        if m.role == "assistant":
            answered.add(m.turn)
    order = sorted(starts)
    out: dict[str, list[int]] = {}
    for i, turn in enumerate(order):
        if turn not in answered:
            continue
        end = starts[order[i + 1]] if i + 1 < len(order) else None
        out[str(turn)] = sorted(
            p.revision for p in proposals
            if p.source == "model" and p.created_at is not None
            and p.created_at >= starts[turn] and (end is None or p.created_at < end))
    return out


def conversation_detail(
    db: Session, row: AssistantConversation, viewer: str | None = None
) -> dict[str, Any]:
    messages = _messages(db, row.id)
    proposals = _proposals(db, row.id)
    return {
        **conversation_summary(db, row, viewer),
        "catalog": owner_catalog(db, row),
        "preparation": preparation.view(db, row),
        "messages": [
            {
                "id": m.id,
                "turn": m.turn,
                "role": m.role,
                "text": m.text,
                "name": m.name,
                # rows from before authors were recorded were always the owner's
                "author": (m.author or row.owner) if m.role == "user" else None,
                "at": m.created_at.isoformat() if m.created_at else None,
                # N17-obs296: the coded errors of a notice row (rejection / auto-correction)
                # so the console renders them in the member's language
                **({"errors": validation_codes.details(validation_codes.note_lines(m.text))}
                   if m.role == "error" and m.name in (PROPOSAL_REJECTED_NAME,
                                                       *AUTOCORRECT_NAMES) else {}),
                # N17-obs468: the revision a "no proposal block" notice names
                **({"missing": missing_detail(m.text)}
                   if m.role == "error" and m.name in MISSING_NAMES else {}),
            }
            for m in messages
            if m.role != SESSION_ROLE
        ],
        "proposals": [proposal_out(db, p) for p in proposals],
        "turn_revisions": turn_revisions(messages, proposals),
    }


def proposal_out(db: Session, p: AssistantProposal) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": p.id,
        "conversation_id": p.conversation_id,
        "revision": p.revision,
        "source": p.source,
        "status": p.status,
        "content": p.content,
        "content_hash": p.content_hash,
        "bindings": proposal_contract.bindings_view(p.bindings),
        "validation_errors": p.validation_errors or [],
        # N17-obs296: the same errors as ``{code, params, message}`` (message verbatim)
        "validation_error_details": validation_codes.details(p.validation_errors),
        "created_by": p.created_by,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "approval": None,
        "rejected_by": p.rejected_by,
        "rejected_at": p.rejected_at.isoformat() if p.rejected_at else None,
        "notes": list(p.notes or []),
        "name_conflict": name_conflict(db, p),
    }
    if p.status == "approved":
        out["approval"] = _approval_out(db, p)
    return out


def name_conflict(db: Session, p: AssistantProposal) -> dict[str, str] | None:
    """N17-obs469: ``{name}`` when a live agent of the workspace already holds a draft /
    invalid revision's name — the rule ``claim_agent_name`` applies at approval (which stays
    the backstop: 409 ``agent.name_exists``). The holder is not named (workspace isolation;
    the 409 names it only to a viewer who may see it)."""
    if p.status not in ("draft", "invalid"):
        return None
    name = (p.content or {}).get("name")
    if not isinstance(name, str) or not name:
        return None
    workspace_id = p.workspace_id
    if workspace_id is None:
        conversation = db.get(AssistantConversation, p.conversation_id)
        workspace_id = conversation.workspace_id if conversation is not None else None
    if workspace_id is None or agent_names.live_holder(db, workspace_id, name) is None:
        return None
    return {"name": name}


def _approval_out(db: Session, p: AssistantProposal) -> dict[str, Any]:
    agent = db.get(Agent, p.agent_id) if p.agent_id else None
    job = db.get(Job, p.job_id) if p.job_id else None
    return {
        "approved_by": p.approved_by,
        "approved_at": p.approved_at.isoformat() if p.approved_at else None,
        "agent_id": p.agent_id,
        "agent_name": agent.name if agent else None,
        "agent_status": agent.status if agent else None,
        "agent_error": agent.error if agent else None,
        "deployment_id": p.deployment_id,
        "job_id": p.job_id,
        "job_status": job.status if job else None,
    }


# ---------------------------------------------------------------------------
# the conversation write lock + turn claim
# ---------------------------------------------------------------------------


def _lock_conversation(db: Session, conversation_id: str) -> None:
    """Take the SQLite write lock on the conversation row as the FIRST statement
    of a transaction. Concurrent writers block until this transaction ends and
    then re-read current state; nothing here reads before it writes, so a shared
    → reserved upgrade can never deadlock. Callers keep the transaction short and
    never do network I/O while holding it."""
    db.rollback()  # start clean: no stale shared lock from an earlier read
    db.expire_all()
    rows = db.execute(
        update(AssistantConversation)
        .where(AssistantConversation.id == conversation_id)
        .values(updated_at=datetime.now(UTC))
    ).rowcount
    if rows != 1:
        db.rollback()
        raise NotFoundError("assistant.conversation_not_found", "conversation not found")


def _turn_limit_error() -> AppError:
    return AppError(
        "assistant.conversation_full",
        f"this conversation reached {MAX_TURNS} turns; start a new one",
        {"max_turns": MAX_TURNS}, status_code=409,
    )


def _claim_is_stale(row: AssistantConversation, now: datetime) -> bool:
    if row.active_turn is None or row.active_turn_started_at is None:
        return False
    started = row.active_turn_started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return now - started > timedelta(seconds=TURN_CLAIM_TTL_S)


# Single-host live ownership: turns streaming in THIS process. A claim whose owner is
# still alive here is never taken over, whatever its age; TTL takeover is for orphans
# of a dead process only. The durable token still fences every write.
_LIVE_TURNS: dict[str, str] = {}
_LIVE_TURNS_LOCK = threading.Lock()


def _turn_live_here(conversation_id: str) -> bool:
    with _LIVE_TURNS_LOCK:
        return conversation_id in _LIVE_TURNS


def claim_turn(db: Session, conversation_id: str) -> tuple[int, str]:
    """Atomically reserve the next turn number, refusing while another turn is in
    flight (`409 assistant.turn_in_progress`) or the conversation is full. A claim
    older than ``TURN_CLAIM_TTL_S`` is taken over; the previous holder's token is
    invalidated so it can never publish (fail-safe, no result stealing)."""
    now = datetime.now(UTC)
    token = uuid.uuid4().hex
    # ONE ownership acquisition: the process-level registry lock is held across the
    # durable claim AND its local publication, so no other claimer in this process
    # can observe "claimed but not yet live" and take the turn over as an orphan.
    with _LIVE_TURNS_LOCK:
        return _claim_turn_locked(db, conversation_id, now, token)


def _claim_turn_locked(
    db: Session, conversation_id: str, now: datetime, token: str
) -> tuple[int, str]:
    _lock_conversation(db, conversation_id)
    preparation.require_no_import(db.get(AssistantConversation, conversation_id))
    claimed = db.execute(
        update(AssistantConversation)
        .where(
            AssistantConversation.id == conversation_id,
            AssistantConversation.active_turn.is_(None),
            AssistantConversation.turns < MAX_TURNS,
        )
        .values(turns=AssistantConversation.turns + 1,
                active_turn=AssistantConversation.turns + 1,
                active_turn_started_at=now, active_turn_token=token)
    ).rowcount
    if claimed != 1:
        row = db.get(AssistantConversation, conversation_id)
        if (row is not None and _claim_is_stale(row, now) and row.turns < MAX_TURNS
                and conversation_id not in _LIVE_TURNS):  # registry lock is held
            stale_turn = row.active_turn
            claimed = db.execute(
                update(AssistantConversation)
                .where(AssistantConversation.id == conversation_id,
                       AssistantConversation.active_turn == row.active_turn,
                       AssistantConversation.active_turn_token == row.active_turn_token)
                .values(turns=AssistantConversation.turns + 1,
                        active_turn=AssistantConversation.turns + 1,
                        active_turn_started_at=now, active_turn_token=token)
            ).rowcount
            if claimed == 1 and stale_turn is not None:
                # the orphaned turn's worker is fenced out from here on: an
                # auto-correction it left pending is settled by its successor (rev-r27 #1)
                _settle_pending_autocorrect(db, conversation_id, row.workspace_id, stale_turn,
                                            "the turn's claim expired and was taken over")
        if claimed != 1:
            db.rollback()
            if row is None:
                raise NotFoundError("assistant.conversation_not_found", "conversation not found")
            if row.turns >= MAX_TURNS and row.active_turn is None:
                raise _turn_limit_error()
            # another request held the claim when ours ran (it may have finished
            # since — a lost race is still "someone else's turn", never a fabricated one)
            raise AppError(
                "assistant.turn_in_progress",
                "another turn of this conversation was in flight; send the message again",
                {"active_turn": row.active_turn}, status_code=409,
            )
    db.commit()
    _LIVE_TURNS[conversation_id] = token  # published before any other claimer may run
    db.expire_all()
    turn = db.execute(
        select(AssistantConversation.active_turn)
        .where(AssistantConversation.id == conversation_id)
    ).scalar_one()
    return turn, token


def _holds_claim(db: Session, conversation_id: str, turn: int, token: str) -> bool:
    row = db.execute(
        select(AssistantConversation.active_turn, AssistantConversation.active_turn_token)
        .where(AssistantConversation.id == conversation_id)
    ).first()
    return row is not None and row[0] == turn and row[1] == token


def release_turn(db: Session, conversation_id: str, turn: int, token: str) -> None:
    db.rollback()
    db.execute(
        update(AssistantConversation)
        .where(AssistantConversation.id == conversation_id,
               AssistantConversation.active_turn == turn,
               AssistantConversation.active_turn_token == token)
        .values(active_turn=None, active_turn_started_at=None, active_turn_token=None,
                updated_at=datetime.now(UTC))
    )
    db.commit()


def clear_stale_turn_claims() -> int:
    """Startup: a claim can only be held by a live turn of THIS process, so every claim
    found at boot belongs to a process that died mid-turn. N-ARCH-obs15: such a turn is
    first persisted as *interrupted* from its event log (what had streamed, plus a
    proposal block that had streamed completely — ``_persist_interrupted``) and its log
    gets a terminal ``error`` event, so a client that reattaches sees the turn end; it
    is never resumed blind. Then the claim is cleared."""
    # Single-process assumption (the production deployment): EVERY claim found at boot is
    # finalized, whatever its age — ``_claim_is_stale`` is deliberately not consulted. A
    # multi-process deployment would need TTL gating or a process marker here, or this
    # sweep would interrupt a sibling's live turn (docs/cn/interfaces/assistant-turns.md §7).
    db = SessionLocal()
    try:
        orphans = db.execute(
            select(AssistantConversation.id)
            .where(AssistantConversation.active_turn.isnot(None))
        ).scalars().all()
        for conversation_id in orphans:
            try:
                _finalize_dead_turn(db, conversation_id)
            except Exception:  # pragma: no cover — the sweep must still clear the claim
                db.rollback()
                logger.exception("assistant: could not finalize the dead turn of %s",
                                 conversation_id)
        count = db.execute(
            update(AssistantConversation)
            .where(AssistantConversation.active_turn.isnot(None))
            .values(active_turn=None, active_turn_started_at=None, active_turn_token=None)
        ).rowcount
        db.execute(update(AssistantConversation)
                   .where(AssistantConversation.preparation_token.isnot(None))
                   .values(preparation_token=None))
        db.commit()
        return count
    finally:
        db.close()


def require_turn_capacity(conversation: AssistantConversation) -> None:
    """409 before a stream opens (a refusal inside the stream would be a 500)."""
    preparation.require_no_import(conversation)
    if (conversation.turns or 0) + 1 > MAX_TURNS:
        raise _turn_limit_error()
    if conversation.active_turn is not None and (
        _turn_live_here(conversation.id) or not _claim_is_stale(conversation, datetime.now(UTC))
    ):
        raise AppError(
            "assistant.turn_in_progress",
            f"turn {conversation.active_turn} of this conversation is still streaming; wait "
            "for it to finish",
            {"active_turn": conversation.active_turn}, status_code=409,
        )


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


def patch_base(db: Session, conversation_id: str) -> AssistantProposal | None:
    """The revision a ``launchpad-proposal-patch`` applies to: the newest one whose
    stored content is a real proposal object (an oversized / unparseable block leaves
    only a marker, so the revision before it stays the base). Any status: an invalid
    revision is corrected in place, an approved one is iterated after deployment."""
    for row in reversed(_proposals(db, conversation_id)):
        if proposal_contract.is_patch_base(row.content):
            return row
    return None


def current_proposal_section(base: AssistantProposal | None) -> str:
    """The exact stored JSON the next revision is written against. Replayed replies
    carry no proposal blocks (``compose_messages``), so this is the model's single view
    of the proposal, and the operations it writes address exactly these members."""
    if base is None:
        return ""
    payload = json.dumps(base.content, ensure_ascii=False, separators=(",", ":"))
    errors = ""
    if base.status == "invalid" and base.validation_errors:
        errors = ("\nIt does not validate yet — the errors to fix:\n"
                  + _error_lines(list(base.validation_errors)) + "\n")
    example = (f'{{"base_revision": {base.revision}, "operations": [{{"op": "replace", '
               '"path": "/system_prompt", "value": "…"}]}')
    how = (f"append ONE fenced `{proposal_contract.PATCH_FENCE}` block instead of a full "
           f"proposal: `{example}`")
    full = (f"Emit a full `{proposal_contract.PROPOSAL_FENCE}` block only when the change "
            "rewrites most of the proposal.")
    return f"""

## {proposal_contract.STORED_PROPOSAL_MARKER} — revision {base.revision} ({base.status})

This is the exact JSON Launchpad stored for the latest proposal (defaults filled in,
member edits and resource selections applied). {REPLAY_BLOCK_NOTE}{errors}
To change it — a correction, a member's request, an optimization round — {how}.
Operations are RFC 6902 `add` / `remove` / `replace` / `test` with JSON Pointer paths
into THIS JSON (array indexes as stored; `-` appends to an array; `~1` escapes `/`).
They apply in order, all or none, and the result is validated like a full proposal.
{full} The member does not see the JSON: in the conversation describe what changed in
plain words, and never mention patches, operations, JSON Pointer or revision mechanics.

```json
{payload}
```
"""


def _base_of(conversation: AssistantConversation) -> AssistantProposal | None:
    db = object_session(conversation)
    return patch_base(db, conversation.id) if db is not None else None


def protocol_preamble() -> str:
    """`PROTOCOL_PREAMBLE` with the LIVE agent default (r15-e `agent_default` role) in the
    `model_id` default line — the constant is rendered at import from the built-in id."""
    live = proposal_contract.agent_default_model_id()
    if live == proposal_contract.PROPOSAL_DEFAULT_MODEL_ID:
        return PROTOCOL_PREAMBLE
    return PROTOCOL_PREAMBLE.replace(
        f"`{proposal_contract.PROPOSAL_DEFAULT_MODEL_ID}` / `\"bedrock\"`",
        f"`{live}` / `\"bedrock\"`", 1)


# N17-obs469: the agent names already in use, read LIVE on every turn (the stored catalog
# is a snapshot) — approval creates a new agent, so a proposal must not reuse one
TAKEN_NAMES_HEADING = "## Agent names already in use in this workspace"
TAKEN_NAMES_MAX = 100


def taken_names_section(conversation: AssistantConversation) -> str:
    db = object_session(conversation)
    if db is None or not conversation.workspace_id:
        return ""
    viewer = owner_viewer(db, conversation.workspace_id, conversation.owner_principal)
    names = [n for (n,) in db.query(Agent.name).filter(
        Agent.workspace_id == conversation.workspace_id, Agent.status != "deleted",
        visibility.clause(viewer, Agent.created_by)).order_by(Agent.name).distinct()]
    lines = [f"- `{n}`" for n in names[:TAKEN_NAMES_MAX]] or ["- (none)"]
    if len(names) > TAKEN_NAMES_MAX:
        lines.append(f"- … and {len(names) - TAKEN_NAMES_MAX} more not listed")
    base = _base_of(conversation)
    taken = ""
    name = (base.content or {}).get("name") if base is not None else None
    if (base is not None and base.status in ("draft", "invalid") and isinstance(name, str)
            and agent_names.live_holder(db, conversation.workspace_id, name) is not None):
        taken = (f"\nThe stored proposal's name `{name}` is already taken: approval would be "
                 "refused — propose a distinct name.")
    return (f"\n\n{TAKEN_NAMES_HEADING}\n\nApproval creates a NEW agent, so a proposal's "
            "`name` must not be one of these (a deleted agent's name is free again; names "
            "you cannot see may be taken too — Launchpad then warns the member):\n"
            + "\n".join(lines) + taken + "\n")


def _preamble(conversation: AssistantConversation) -> str:
    return (protocol_preamble() + "\n"
            + catalog_section(owner_catalog(object_session(conversation), conversation),
                              owner_model_principal(object_session(conversation),
                                                    conversation.owner_principal))
            + taken_names_section(conversation)
            + preparation.context(conversation)
            + current_proposal_section(_base_of(conversation)))


# The placeholder replays used to put inside the assistant's own message where a past block
# was removed. N17-obs468: the model imitated it in NEW replies instead of emitting a block
# (「修订已提交」 with nothing stored), so a replayed reply no longer carries it — the
# block is removed outright and Launchpad says so on the member's side of the next turn
# (``REPLAY_BLOCK_NOTE``). Kept as the shape ``phantom_reason`` recognises.
REPLAY_PROPOSAL_MARKER = (
    "[proposal block of this reply omitted from the replay — its stored result is part "
    "of the current stored proposal in the protocol preamble]"
)
REPLAY_BLOCK_NOTE = ("The proposal blocks of the replayed earlier replies are removed from "
                     "them (no placeholder is left in their place); what they produced is this "
                     "stored proposal.")
REPLAY_NO_BLOCK_NOTE = ("Launchpad note (not part of any reply): your previous reply carried "
                        "no proposal block, so nothing was stored from it.")
_BRACKET_RE = re.compile(r"\[[^\[\]\n]{0,300}\]")


def _strip_proposal_blocks(text: str) -> str:
    # the same recognition extraction uses (tag anywhere in the info string, ~~~ fences,
    # the untagged fallback) — a block read as a proposal is never replayed as prose
    return proposal_contract.strip_proposal_blocks(text, "")


def _scrub_placeholders(text: str) -> str:
    """A replayed reply without the placeholder (or a bracketed variant) it imitated."""
    out = (text or "").replace(REPLAY_PROPOSAL_MARKER, "")
    return _BRACKET_RE.sub(lambda m: "" if _PLACEHOLDER_RE.search(m.group(0)) else m.group(0),
                           out)


def check_retry(db: Session, conversation: AssistantConversation, turn: int) -> None:
    """A retry names the conversation's LATEST turn, and only one that failed."""
    rows = [m for m in _messages(db, conversation.id) if m.turn == turn]
    latest = max((m.turn for m in _messages(db, conversation.id)), default=0)
    failed = any(m.role == "error" and m.name not in NOTICE_NAMES for m in rows)
    if not rows or turn != latest or not failed:
        raise AppError(
            "assistant.retry_not_allowed",
            "only the latest turn, and only a failed one, can be retried",
            {"turn": turn, "latest": latest}, status_code=409,
        )


def check_prompt(conversation: AssistantConversation, prompt: str) -> None:
    """The current message is never truncated: too large for the final request → 413."""
    if len(prompt) > MAX_PROMPT_CHARS or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise AppError(
            "assistant.prompt_too_large",
            f"the message exceeds {MAX_PROMPT_CHARS} characters / {MAX_PROMPT_BYTES} bytes",
            {"max_chars": MAX_PROMPT_CHARS, "max_bytes": MAX_PROMPT_BYTES}, status_code=413,
        )
    if len(_preamble(conversation)) + len(prompt) + 200 > MAX_REPLAY_CHARS:
        raise AppError(
            "assistant.prompt_too_large",
            "the message plus the protocol preamble exceeds the request budget; shorten "
            "the message or split it over several turns",
            {"max_request_chars": MAX_REPLAY_CHARS}, status_code=413,
        )


def compose_messages(
    conversation: AssistantConversation, history: list[AssistantMessage], prompt: str,
) -> tuple[list[dict[str, Any]], int]:
    """The bounded ``messages`` replay sent to the architect runtime and how many
    older turns were omitted from it.

    Turns are paired by their turn number (never by insert order): each replayed
    turn is the member's text plus the assistant's reply when there was one; a
    failed/interrupted turn is replayed with an explicit marker instead of a reply
    and counts against the budget like any other. The newest turns that fit the
    FINAL budget (preamble + catalog + turns + current message) are kept, the count
    of omitted older turns is disclosed in the preamble, and the current message is
    always sent whole (``check_prompt`` refuses one that cannot fit).
    """
    by_turn: dict[int, dict[str, list[str]]] = {}
    preamble = _preamble(conversation)
    strip = _base_of(conversation) is not None
    retried = {m.turn for m in history if m.role == "error" and m.name == TURN_RETRIED_NAME}
    for m in history:
        if not m.text or m.turn in retried:
            continue
        if m.role == "error" and m.name == PROPOSAL_REJECTED_NAME:
            # Launchpad's own verdict on that turn's block, replayed as the member's
            # side of the NEXT turn (never as words the assistant said)
            by_turn.setdefault(m.turn, {"user": [], "assistant": [], "verdict": [],
                                            "notes": []})
            by_turn[m.turn]["verdict"].append(m.text)
            continue
        if m.role not in ("user", "assistant"):
            continue
        slot = by_turn.setdefault(m.turn, {"user": [], "assistant": [], "verdict": [],
                                            "notes": []})
        if m.role == "user":
            slot["user"].append(m.text)
            continue
        # a stored proposal is shown once, as stored, in the preamble; replaying every
        # earlier 20–60k-char block too would crowd the turns themselves out of budget.
        # N17-obs468: the block goes without a trace in the reply and Launchpad says what
        # happened on the member's side of the next turn — a placeholder inside the
        # model's own message was imitated in new replies instead of a block
        text = m.text
        has_block = bool(proposal_contract.find_blocks(text))
        if strip and has_block:
            text = _strip_proposal_blocks(text)  # the preamble says so (REPLAY_BLOCK_NOTE)
        scrubbed = _scrub_placeholders(text)
        if (not has_block and (scrubbed != text or phantom_reason(text))
                and REPLAY_NO_BLOCK_NOTE not in slot["notes"]):
            slot["notes"].append(REPLAY_NO_BLOCK_NOTE)
        slot["assistant"].append(scrubbed.strip() or (
            "(the reply was only the proposal block)" if has_block else "(no text)"))
    turns: list[tuple[str, str | None]] = []
    carried: list[str] = []
    # N17-obs468: Launchpad's notes on the previous reply (no block was stored from it),
    # after the member's words of the next turn — never inside the model's own message
    notes: list[str] = []
    for _, slot in sorted(by_turn.items()):
        if slot["user"]:
            turns.append(("\n\n".join(carried + slot["user"] + notes),
                          "\n\n".join(slot["assistant"]) or None))
            carried, notes = [], []
        carried += slot["verdict"]
        notes += slot["notes"]
    if carried or notes:
        prompt = "\n\n".join(carried + [prompt] + notes)
    budget = MAX_REPLAY_CHARS - len(preamble) - len(prompt) - 200
    kept: list[tuple[str, str | None]] = []
    used = 0
    for user_text, reply in reversed(turns):
        size = len(user_text) + len(reply or "") + 80
        if len(kept) >= MAX_REPLAY_TURNS or used + size > budget:
            break
        kept.insert(0, (user_text, reply))
        used += size
    omitted = len(turns) - len(kept)
    if omitted:
        preamble += (
            f"\n\n> Replay note: {omitted} earlier turn(s) of this conversation were omitted "
            "to fit the request; ask the member to restate anything you need from them."
        )
    messages: list[dict[str, Any]] = []
    pending_user: list[str] = []
    for user_text, reply in kept:
        pending_user.append(user_text)
        if reply is None:
            pending_user.append("(no assistant reply was produced for the message above)")
            continue
        messages.append({"role": "user", "content": [{"text": "\n\n".join(pending_user)}]})
        messages.append({"role": "assistant", "content": [{"text": reply}]})
        pending_user = []
    pending_user.append(prompt)
    messages.append({"role": "user", "content": [{"text": "\n\n".join(pending_user)}]})
    first = messages[0]["content"][0]["text"]
    messages[0]["content"][0]["text"] = preamble + "\n\n---\n\n## Member message\n\n" + first
    return messages, omitted


# ---------------------------------------------------------------------------
# revisions
# ---------------------------------------------------------------------------


def _allocate_revision(db: Session, conversation_id: str) -> int:
    """Next revision number, unique per conversation, allocated inside the caller's
    write transaction (which already holds the conversation lock)."""
    db.execute(
        update(AssistantConversation)
        .where(AssistantConversation.id == conversation_id)
        .values(revision_seq=AssistantConversation.revision_seq + 1)
    )
    revision = db.execute(
        select(AssistantConversation.revision_seq)
        .where(AssistantConversation.id == conversation_id)
    ).scalar_one()
    if revision > MAX_REVISIONS:
        db.rollback()
        raise AppError(
            "assistant.conversation_full",
            f"this conversation reached {MAX_REVISIONS} proposal revisions; start a new one",
            {"max_revisions": MAX_REVISIONS}, status_code=409,
        )
    return revision


def _supersede_drafts(db: Session, conversation_id: str) -> None:
    db.execute(
        update(AssistantProposal)
        .where(
            AssistantProposal.conversation_id == conversation_id,
            AssistantProposal.status.in_(("draft", "invalid")),
        )
        .values(status="superseded")
    )


def effective_raw(
    db: Session, conversation_id: str, raw: Any, *, source: str,
    approved: dict[str, Any] | None,
) -> Any:
    """``raw`` with the member's explicit preparation applied — what validation and
    storage actually see (``record_proposal``)."""
    if approved is not None:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                pass  # ordinary validation still reports malformed model output
        if source == "member":
            preparation.require_matching_resources(raw, approved)
        elif isinstance(raw, dict):
            raw = {**raw, **approved}
    elif source == "model":
        conversation = db.get(AssistantConversation, conversation_id)
        state = conversation.preparation or {}
        # Explicit member selections are authoritative, even when the model omits
        # them. Legacy conversations retain their original proposal behavior.
        if state.get("selection_set"):
            parsed, _ = proposal_contract.parse_content(raw)
            if parsed is not None:
                raw = {**proposal_contract.content_dump(parsed),
                       "skills": state.get("skills", []),
                       "knowledge_bases": state.get("knowledge_bases", [])}
                if state.get("tools_selection_set"):
                    raw["tools"] = state.get("tools", [])
    return raw


def record_proposal(
    db: Session,
    conversation_id: str,
    catalog: dict[str, Any],
    workspace_id: str | None,
    raw: Any,
    *,
    source: str,
    created_by: str,
    extra_errors: list[str] | None = None,
    notes: list[str] | None = None,
) -> AssistantProposal:
    """Store one new revision inside the caller's locked transaction (no commit):
    valid → ``draft`` with bindings; otherwise ``invalid`` with the errors and the
    bounded raw object. Explicit member preparation overrides resource lists in a
    well-shaped model proposal before reference validation. ``notes`` are the
    revision-level ``proposal_repaired`` notes the caller recorded (N15-obs13)."""
    approved = preparation.approved_resources(db, conversation_id)
    raw = effective_raw(db, conversation_id, raw, source=source, approved=approved)
    owner = db.get(AssistantConversation, conversation_id)
    content, display, errors = proposal_contract.validate(
        raw, catalog,
        owner_model_principal(db, owner.owner_principal if owner is not None else None))
    errors = list(extra_errors or []) + errors
    if proposal_contract.serialized_bytes(display) > proposal_contract.PROPOSAL_MAX_BYTES:
        # the NORMALIZED content (defaults filled in) is what gets stored and hashed;
        # it must respect the same cap as the raw input
        display = {"_rejected": "normalized proposal exceeds the size limit"}
        errors = errors + [
            f"normalized proposal exceeds {proposal_contract.PROPOSAL_MAX_BYTES} bytes"]
    valid = content is not None and not errors
    bindings = proposal_contract.resource_bindings(content, catalog) if valid else None
    revision = _allocate_revision(db, conversation_id)
    _supersede_drafts(db, conversation_id)
    row = AssistantProposal(
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        revision=revision,
        source=source,
        content=display,
        content_hash=proposal_contract.revision_hash(display, bindings),
        bindings=bindings,
        validation_errors=errors,
        status="draft" if valid else "invalid",
        created_by=created_by,
        notes=list(notes or []),
    )
    db.add(row)
    db.flush()
    if valid and approved is None:
        conversation = db.get(AssistantConversation, conversation_id)
        state = conversation.preparation or {}
        if not state.get("selection_set") or (
            state.get("skills", []) != display["skills"]
            or state.get("knowledge_bases", []) != display["knowledge_bases"]
            or state.get("tools", []) != display["tools"]
            or (source == "member" and not state.get("tools_selection_set"))
        ):
            # A valid model proposal also changes legacy preparation's projection.
            # Advance its revision so a stale rail cannot overwrite the new selection.
            conversation.preparation = {
                **state, "revision": state.get("revision", 0) + 1,
                "skills": display["skills"], "knowledge_bases": display["knowledge_bases"],
                "tools": display["tools"],
                "tools_selection_set": source == "member"
                or bool(state.get("tools_selection_set")),
                "selection_set": source == "member" or bool(state.get("selection_set")),
            }
    return row


# ---------------------------------------------------------------------------
# turns
# ---------------------------------------------------------------------------


def _model_proposal_input(
    db: Session, conversation_id: str,
    block: str | None, block_errors: list[str],
    patch: str | None, patch_errors: list[str],
) -> tuple[Any, list[str]]:
    """What a reply hands ``record_proposal``: ``(raw, extra_errors)``, or ``(None,
    errors)`` when there is nothing to validate. A change block is applied to the
    stored patch base here, so the result is validated exactly like a full block."""
    if (block is not None or block_errors) and (patch is not None or patch_errors):
        return None, ["reply carries both a complete proposal and a proposal change; "
                      "send exactly one"]
    if block is not None or block_errors:
        raw: Any = block if block is not None else {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                pass  # kept as the string; parse_content reports the JSON error
        return raw, block_errors
    if patch_errors:
        return None, patch_errors
    base = patch_base(db, conversation_id)
    if base is None:
        return None, ["there is no stored proposal to change yet; send the complete proposal"]
    return proposal_contract.apply_patch(base.content, patch, base_revision=base.revision)


def record_unusable(
    db: Session, conversation_id: str, workspace_id: str | None, errors: list[str], *,
    created_by: str,
) -> AssistantProposal:
    """An ``invalid`` revision for a reply whose proposal could not even be assembled
    (a change that does not apply, two kinds of block). It stores only a marker, so the
    revision before it stays the base the next change is written against."""
    display = {"_rejected": "the proposal in this reply could not be assembled"}
    revision = _allocate_revision(db, conversation_id)
    _supersede_drafts(db, conversation_id)
    row = AssistantProposal(
        workspace_id=workspace_id, conversation_id=conversation_id, revision=revision,
        source="model", content=display,
        content_hash=proposal_contract.revision_hash(display, None), bindings=None,
        validation_errors=errors, status="invalid", created_by=created_by,
    )
    db.add(row)
    db.flush()
    return row


def _persist_partial(
    db: Session, conversation: AssistantConversation | tuple[str, str | None], turn: int,
    token: str, session_id: str, text: str, error: str, name: str | None = None,
) -> bool:
    """Persist a partial/failed turn — only while this worker still holds the claim
    (a reclaimed turn's late worker writes nothing). ``conversation`` may be the plain
    ``(id, workspace_id)`` pair: the finalizer runs after the owning session may have
    been closed, so it must not touch a (detached) ORM instance."""
    if isinstance(conversation, tuple):
        conversation_id, workspace_id = conversation
    else:
        conversation_id, workspace_id = conversation.id, conversation.workspace_id
    _lock_conversation(db, conversation_id)
    if not _holds_claim(db, conversation_id, turn, token):
        db.rollback()
        return False
    if text:
        db.add(AssistantMessage(
            workspace_id=workspace_id, conversation_id=conversation_id,
            turn=turn, role="assistant", text=text, runtime_session_id=session_id,
        ))
    db.add(AssistantMessage(
        workspace_id=workspace_id, conversation_id=conversation_id,
        turn=turn, role="error", name=name, text=error[:4000], runtime_session_id=session_id,
    ))
    db.commit()
    return True


def _persist_interrupted(
    db: Session, conversation: AssistantConversation | tuple[str, str | None], turn: int,
    token: str, session_id: str, text: str, *, created_by: str,
) -> None:
    """The client left (or the owner cancelled) before the reply finished: keep what the
    member saw, mark the turn with ``TURN_INTERRUPTED_NAME`` — never the transport's
    exception text (N-ARCH-obs12) — and keep a proposal the member would otherwise lose:
    a ``launchpad-proposal`` / patch block that had streamed COMPLETELY (closed fence) is
    recorded exactly like a completed reply's (same validation, same ``proposal_rejected``
    note when invalid). An unclosed fence is not a block, so a cut inside the JSON still
    yields no proposal. Writes only while this worker holds the claim."""
    conversation_id = conversation[0] if isinstance(conversation, tuple) else conversation.id
    if not _persist_partial(db, conversation, turn, token, session_id, text,
                            TURN_INTERRUPTED_TEXT, name=TURN_INTERRUPTED_NAME):
        return
    block, block_errors = proposal_contract.extract_block(text)
    patch, patch_errors = proposal_contract.extract_patch(text)
    if (block is None and patch is None) or block_errors or patch_errors:
        return  # nothing complete to keep (two blocks / oversize are not salvaged either)
    try:
        _lock_conversation(db, conversation_id)
        if not _holds_claim(db, conversation_id, turn, token):
            db.rollback()
            return
        fresh = db.get(AssistantConversation, conversation_id)
        raw, input_errors = _model_proposal_input(db, conversation_id, block, [], patch, [])
        if raw is None:
            db.rollback()
            return  # e.g. a patch with no stored base: the member sees the marker only
        revision = record_proposal(
            db, conversation_id, fresh.catalog or {}, fresh.workspace_id, raw,
            source="model", created_by=created_by, extra_errors=input_errors,
        )
        if revision.status == "invalid":
            db.add(AssistantMessage(
                workspace_id=fresh.workspace_id, conversation_id=conversation_id,
                turn=turn, role="error", name=PROPOSAL_REJECTED_NAME,
                text=rejection_note(revision.validation_errors or []),
                runtime_session_id=session_id,
            ))
        db.commit()
    except Exception:  # the marker is already durable; a salvage failure must not mask it
        db.rollback()
        logger.exception("assistant: could not keep the streamed proposal of turn %s", turn)


class _ClaimLost(AppError):
    """Raised inside a turn when its claim was taken over: writes stop immediately."""

    def __init__(self) -> None:
        super().__init__(
            "assistant.turn_superseded",
            "this turn's claim was taken over; nothing was written or invoked for it",
            status_code=409,
        )


class TurnRun:
    """Ownership handle for one streaming turn.

    The response object (not garbage collection) drives cleanup: ``cancel`` flags
    the run, closes the upstream event stream so a blocked read returns, and then
    closes the generator (bounded retries while it is still executing in the
    worker thread) so ``run_turn``'s ``finally`` persists the partial answer as an
    interrupted turn and releases the claim. Closing the transport is not a claim
    that the AWS-side computation stopped.
    """

    def __init__(self) -> None:
        self.cancelled = False
        self.upstream: Any = None
        self.generator: Iterator[Any] | None = None  # the response body iterator
        self.inner: Iterator[dict[str, Any]] | None = None  # run_turn itself
        self.finished = False
        # the upstream stream ended; the reply is being validated / persisted
        self.finishing = False
        self._lock = threading.Lock()

    def attach_upstream(self, stream: Any) -> None:
        self.upstream = stream
        if self.cancelled:
            self._close_upstream()

    def _close_upstream(self) -> None:
        close = getattr(self.upstream, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # pragma: no cover — best effort
                pass

    def signal(self) -> None:
        """Non-blocking: mark cancelled and close the upstream stream so a worker
        blocked in the read returns NOW — called from the response's own disconnect
        handling before anything waits for the worker to unwind."""
        with self._lock:
            if self.finished:
                return
            self.cancelled = True
        self._close_upstream()

    def cancel(self, wait_s: float = 5.0) -> None:
        with self._lock:
            if self.finished:
                return
            self.cancelled = True
        self._close_upstream()
        deadline = time.monotonic() + wait_s
        # outer (response body) first, then run_turn itself: explicit, never refcount-
        # or GC-dependent
        for gen in (self.generator, self.inner):
            if gen is None:
                continue
            while True:
                try:
                    gen.close()  # raises GeneratorExit at the pending yield → finally runs
                    break
                except ValueError:  # "generator already executing" in the worker thread
                    if time.monotonic() > deadline:
                        logger.warning("assistant: turn generator still executing after %.1fs",
                                       wait_s)
                        return
                    time.sleep(0.02)
                except Exception:  # pragma: no cover — cleanup must not raise
                    logger.exception("assistant: closing the turn generator failed")
                    break


def _replayable_turn_error(exc: BaseException) -> bool:
    """Transient upstream failures, plus a model call cut off at ``max_tokens``: live
    2026-10-05 a proposal call stopped there 40 s in, far short of the 65 536-token
    budget (the manual retry wrote the 11 k-token proposal in 3 min). Timeouts and
    iteration limits are never replayed."""
    if getattr(exc, "code", None) == "harness.execution_limit":
        detail = getattr(exc, "detail", None)
        return isinstance(detail, dict) and detail.get("stop_reason") == "max_tokens"
    transient = (
        # the Runtime's stream failed or ended mid-turn (a runtimeClientError /
        # internalServerException event, a stream cut off after a tool step or empty)
        isinstance(exc, architect_runtime.RuntimeStreamError)
        or getattr(exc, "code", None) == "harness.incomplete_response"
        # the InvokeAgentRuntime call itself failed on a transient upstream error — a
        # throttle / 5xx code, or (upstream #227) a ``RuntimeClientError`` "Runtime health
        # check failed or timed out" with a 4xx status; one predicate with the evaluation
        # worker's scenario replay (``rt.is_upstream_transient``)
        or (isinstance(exc, ClientError) and rt.is_upstream_transient(exc))
    )
    return transient and getattr(exc, "code", None) != "harness.execution_timeout"


def _replay_backoff_s(exc: BaseException) -> float:
    """How long the single replay waits before its fresh session (N-ARCH-obs18, live
    2026-10-16: the replay 3 s after a Bedrock ``internalServerException`` on the
    post-tool ConverseStream hit the identical 5xx). Transient upstream errors —
    a 5xx / throttle / read timeout named in a ``RuntimeStreamError`` or botocore error,
    or a stream cut with no reply (``harness.incomplete_response``) — wait
    ``assistant_replay_backoff_seconds``; a ``max_tokens`` replay never waits. The marker
    test is the shared ``rt.is_upstream_transient`` (one predicate with the evaluation
    worker's replay and judge retries — N12-obs53 / N12-obs55)."""
    backoff = max(0.0, float(get_settings().assistant_replay_backoff_seconds or 0))
    if backoff == 0:
        return 0.0
    if getattr(exc, "code", None) == "harness.incomplete_response":
        return backoff
    if isinstance(exc, ClientError):
        return backoff  # only the transient codes reach here (``_replayable_turn_error``)
    if isinstance(exc, architect_runtime.RuntimeStreamError):
        return backoff if rt.is_upstream_transient(exc) else 0.0
    return 0.0


def run_turn(
    db: Session,
    conversation: AssistantConversation,
    row: Workspace,
    workspace: WorkspaceContext,
    identity: Identity,
    prompt: str,
    run: TurnRun | None = None,
    retry_of: int | None = None,
) -> Iterator[dict[str, Any]]:
    """One assistant turn as SSE-ready events:
    ``meta → (tool|delta)* → (proposal)? → done`` or ``error``.

    The turn number is claimed atomically first; the user row — carrying the fresh
    private runtime session id — is committed before the data-plane call; the reply
    and any proposal land together in one locked transaction once the stream
    completed. A stream that errors persists the partial answer with an ``error`` row
    (no proposal) and releases the claim; one closed by the client before completion
    persists it with the ``TURN_INTERRUPTED_NAME`` marker and keeps a proposal block
    that had already streamed completely (N-ARCH-obs12). A transient upstream failure
    is first replayed ``TURN_TRANSIENT_RETRIES`` times in a fresh runtime session: the
    failed attempt's partial text and tool rows are discarded, and a ``retry`` event
    tells the client to clear what it streamed.
    """
    run = run or TurnRun()
    agent = _require_available(db, row)
    check_prompt(conversation, prompt)
    conversation_id = conversation.id
    turn, token = claim_turn(db, conversation_id)  # durable claim + live publication
    conversation = db.get(AssistantConversation, conversation_id)
    ids = (conversation_id, conversation.workspace_id)  # plain values for the finalizer
    session_id = rt.new_session_id()
    parts: list[str] = []
    terminal = False  # True once a terminal row (reply or error) was persisted
    producer: threading.Thread | None = None

    def owned() -> bool:
        with _LIVE_TURNS_LOCK:
            live = _LIVE_TURNS.get(conversation_id) == token
        return live and _holds_claim(db, conversation_id, turn, token)

    try:
        history = [m for m in _messages(db, conversation_id) if m.turn != turn]
        if retry_of is not None:  # the retried exchange is not replayed (see TURN_RETRIED_NAME)
            history = [m for m in history if m.turn != retry_of]
        messages, omitted = compose_messages(conversation, history, prompt)
        # the FIRST write is fenced like every other one: composing the replay took
        # time, and ownership may have been replaced meanwhile
        _lock_conversation(db, conversation_id)
        if not owned():
            db.rollback()
            raise _ClaimLost()
        conversation = db.get(AssistantConversation, conversation_id)
        db.add(AssistantMessage(
            workspace_id=conversation.workspace_id, conversation_id=conversation_id,
            turn=turn, role="user", text=prompt, runtime_session_id=session_id,
            author=identity.username,
        ))
        if retry_of is not None:
            db.add(AssistantMessage(
                workspace_id=conversation.workspace_id, conversation_id=conversation_id,
                turn=retry_of, role="error", name=TURN_RETRIED_NAME,
                text=f"retried in turn {turn}",
            ))
        if not conversation.title:
            conversation.title = prompt.strip().splitlines()[0][:120] if prompt.strip() else ""
        conversation.preset_agent_id = agent.id
        db.commit()
        yield {"event": "meta", "data": {"conversation_id": conversation_id, "turn": turn,
                                         "session_id": session_id, "agent": agent.name,
                                         "omitted_turns": omitted}}

        def stream_phase(
            phase_messages: list[dict[str, Any]], sink: list[str], *, phase: str | None = None,
        ) -> Iterator[dict[str, Any]]:
            """One Harness call (plus its single transient replay) as SSE events; the
            reply's text accumulates in ``sink``. Raises the turn's failure. ``phase`` marks
            an in-turn follow-up call (``autocorrect`` N17-obs295, ``reask`` N17-obs468):
            its replay tells the client to drop only what the follow-up streamed, never the
            reply before it."""
            nonlocal session_id, producer
            # Upstream production runs in its own thread; this generator only ever
            # waits on the queue for one heartbeat interval at a time. The response
            # layer can therefore always reach a send/cancel point promptly, and a
            # disconnect closes the upstream (unblocking the producer) instead of
            # waiting behind an uncancellable blocked read.
            events: queue.Queue = queue.Queue()
            tool_rows: dict[str, int] = {}  # toolUseId -> ledger row awaiting its input

            # the current attempt's Harness session (a replay starts a fresh one)
            attempt_session = [session_id]

            def produce() -> None:
                attempt = 0
                while True:
                    try:
                        attempt_once()
                        events.put(("end", None))
                        return
                    except BaseException as exc:  # surfaced to the consumer below
                        if (attempt < TURN_TRANSIENT_RETRIES and not run.cancelled
                                and _replayable_turn_error(exc)):
                            attempt += 1
                            attempt_session[0] = rt.new_session_id()
                            backoff = _replay_backoff_s(exc)
                            logger.warning("assistant: turn %s replayed after %s (backoff %ss)",
                                           turn, _short(exc), backoff)
                            # a transient upstream failure: give the model endpoint a
                            # moment before the fresh session (N-ARCH-obs18), in
                            # heartbeat slices so a cancel still lands promptly
                            if backoff > 0:
                                wait_until = time.monotonic() + backoff
                                while not run.cancelled:
                                    left = wait_until - time.monotonic()
                                    if left <= 0:
                                        break
                                    time.sleep(min(HEARTBEAT_S, left))
                                if run.cancelled:
                                    events.put(("error", exc))
                                    return
                            # the new session is on the ledger before its data-plane call
                            # (like the first one): wait for the consumer to record it
                            recorded = threading.Event()
                            events.put(("retry", {"attempt": attempt, "reason": _short(exc),
                                                  "backoff_s": backoff,
                                                  "session_id": attempt_session[0],
                                                  "recorded": recorded}))
                            deadline = time.monotonic() + RETRY_RECORD_WAIT_S
                            while not recorded.wait(HEARTBEAT_S):
                                if run.cancelled or time.monotonic() > deadline:
                                    break
                            if recorded.is_set() and not run.cancelled:
                                continue
                        events.put(("error", exc))
                        return

            def attempt_once() -> None:
                # one InvokeAgentRuntime call on the preset's self-built Harness (R12):
                # the bounded replay as ``messages``; the member's immutable principal as
                # ``runtimeUserId``/actor. ``on_stream`` lets a cancel close the upstream.
                client = data_client(workspace)
                for event in architect_runtime.stream_architect_turn(
                    client, agent.arn, phase_messages, session_id=attempt_session[0],
                    runtime_user_id=runtime_user_id(agent.spec, principal_of(identity)),
                    on_stream=run.attach_upstream,
                ):
                    if run.cancelled:
                        return
                    events.put(("event", event))

            # no data-plane call without CURRENT ownership
            _lock_conversation(db, conversation_id)
            if not owned():
                db.rollback()
                raise _ClaimLost()
            db.rollback()
            producer = threading.Thread(target=produce, daemon=True,
                                        name=f"assistant-turn-{conversation_id[:8]}-{turn}")
            producer.start()
            while True:
                try:
                    kind, event = events.get(timeout=HEARTBEAT_S)
                except queue.Empty:
                    if run.cancelled:
                        break
                    yield {"event": "heartbeat", "data": {}}
                    continue
                if kind == "end":
                    run.finishing = True
                    break
                if kind == "error":
                    if run.cancelled:
                        # the owner closed the upstream (client gone): the producer's
                        # failure is that closed transport (urllib3's AttributeError on the
                        # cleared ``_fp``, a RuntimeStreamError, …), not the turn's result
                        break
                    raise event
                if kind == "retry":
                    # the replay starts over: drop what the failed attempt streamed and its
                    # tool rows; the user row keeps the first (already invoked) session and
                    # a hidden SESSION_ROLE row reserves the new one before its call
                    sink.clear()
                    tool_rows.clear()
                    failed_session = session_id
                    session_id = event["session_id"]
                    _lock_conversation(db, conversation_id)
                    if not _holds_claim(db, conversation_id, turn, token):
                        db.rollback()
                        raise _ClaimLost()
                    db.execute(delete(AssistantMessage).where(
                        AssistantMessage.conversation_id == conversation_id,
                        AssistantMessage.turn == turn, AssistantMessage.role == "tool",
                        AssistantMessage.runtime_session_id == failed_session))
                    db.add(AssistantMessage(
                        workspace_id=conversation.workspace_id, conversation_id=conversation_id,
                        turn=turn, role=SESSION_ROLE, name="turn_replay",
                        text=(f"replay {event['attempt']} after {event['reason']}"
                              + (f" (waited {event['backoff_s']:g}s)"
                                 if event["backoff_s"] else ""))[:4000],
                        runtime_session_id=session_id,
                    ))
                    db.commit()
                    event["recorded"].set()
                    data = {"attempt": event["attempt"], "reason": event["reason"],
                            "backoff_s": event["backoff_s"]}
                    if phase:
                        data["phase"] = phase
                    yield {"event": "retry", "data": data}
                    continue
                if run.cancelled:
                    break  # the owner cancelled: handled as interrupted below
                if event["event"] == "tool":
                    # fenced like every other write: only the current claim holder
                    _lock_conversation(db, conversation_id)
                    if not _holds_claim(db, conversation_id, turn, token):
                        db.rollback()
                        raise _ClaimLost()
                    tool_row = AssistantMessage(
                        workspace_id=conversation.workspace_id, conversation_id=conversation_id,
                        turn=turn, role="tool", text="", name=event["data"].get("name"),
                        runtime_session_id=session_id,
                    )
                    db.add(tool_row)
                    db.commit()
                    tool_id = event["data"].get("id")
                    if tool_id is not None:
                        tool_rows[tool_id] = tool_row.id
                elif event["event"] == "tool_input":
                    # the call's arguments, bounded upstream — recorded on the row the
                    # ``tool`` event created so the transcript shows WHAT was asked
                    row_id = tool_rows.pop(event["data"].get("id"), None)
                    if row_id is not None and event["data"].get("input"):
                        _lock_conversation(db, conversation_id)
                        if not _holds_claim(db, conversation_id, turn, token):
                            db.rollback()
                            raise _ClaimLost()
                        db.execute(
                            update(AssistantMessage)
                            .where(AssistantMessage.id == row_id)
                            .values(text=event["data"]["input"])
                        )
                        db.commit()
                elif event["event"] == "delta":
                    sink.append(event["data"].get("text", ""))
                yield event

        def correction_phase(
            prompt: str, *, start_event: dict[str, Any], phase: str, session_name: str,
            session_text: str, settle: Callable[[AssistantProposal | None, str | None], None],
        ) -> Generator[dict[str, Any], None, tuple[bool, AssistantProposal | None]]:
            """ONE internal follow-up call inside the turn (N17-obs295 auto-correction,
            N17-obs468 re-ask): ``prompt`` goes on the member's side of the replay (never
            stored as a member message), the call gets its own Harness session reserved
            on the ledger first, its reply is stored, and a block it carries is recorded
            as the next revision. ``settle(revision, reason)`` names the outcome on the
            turn's notice — ``revision`` None when the call changed nothing (``reason``).
            Returns ``(ended, revision)``: ``ended`` when the turn already ended
            (interrupted / superseded; the terminal event was sent). Never loops."""
            nonlocal terminal, session_id
            yield start_event
            parts.clear()  # from here on ``parts`` is the follow-up reply
            run.finishing = False
            history = [m for m in _messages(db, conversation_id)
                       if retry_of is None or m.turn != retry_of]
            fix_messages, _ = compose_messages(
                db.get(AssistantConversation, conversation_id), history, prompt)
            failure: str | None = None
            try:
                # the follow-up's own Harness session is on the ledger before its call
                fix_session = rt.new_session_id()
                _lock_conversation(db, conversation_id)
                if not owned():
                    db.rollback()
                    raise _ClaimLost()
                db.add(AssistantMessage(
                    workspace_id=conversation.workspace_id, conversation_id=conversation_id,
                    turn=turn, role=SESSION_ROLE, name=session_name, text=session_text,
                    runtime_session_id=fix_session,
                ))
                db.commit()
                session_id = fix_session
                yield from stream_phase(fix_messages, parts, phase=phase)
            except Exception as exc:
                if not run.cancelled:
                    failure = _short(exc)
                    logger.warning("assistant: turn %s %s call failed: %s",
                                   turn, phase, failure)
            if run.cancelled:
                _persist_interrupted(db, ids, turn, token, session_id, "".join(parts),
                                     created_by=identity.username)
                _settle_interrupted_autocorrect(db, ids, turn, token, "interrupted")
                terminal = True
                yield {"event": "error", "data": {"message": "interrupted"}}
                return True, None
            fix_text = "".join(parts) if failure is None else ""
            superseded = {"event": "error", "data": {
                "code": "assistant.turn_superseded",
                "message": "this turn's claim expired and was taken over; its reply was "
                           "discarded"}}

            _lock_conversation(db, conversation_id)
            if not _holds_claim(db, conversation_id, turn, token):
                db.rollback()
                terminal = True
                yield superseded
                return True, None
            if fix_text:
                db.add(AssistantMessage(
                    workspace_id=conversation.workspace_id, conversation_id=conversation_id,
                    turn=turn, role="assistant", text=fix_text, runtime_session_id=session_id,
                ))
            fix_block, fix_block_errors = proposal_contract.extract_block(fix_text)
            fix_patch, fix_patch_errors = proposal_contract.extract_patch(fix_text)
            if failure is None and (fix_block is not None or fix_block_errors
                                    or fix_patch is not None or fix_patch_errors):
                try:
                    fresh = db.get(AssistantConversation, conversation_id)
                    raw, input_errors = _model_proposal_input(
                        db, conversation_id, fix_block, fix_block_errors, fix_patch,
                        fix_patch_errors)
                    if raw is None:
                        final = record_unusable(db, conversation_id, fresh.workspace_id,
                                                input_errors, created_by=identity.username)
                    else:
                        final = record_proposal(
                            db, conversation_id, fresh.catalog or {}, fresh.workspace_id, raw,
                            source="model", created_by=identity.username,
                            extra_errors=input_errors)
                    settle(final, None)
                    db.commit()
                    return False, final
                except AppError as exc:  # e.g. assistant.conversation_full (rolled back)
                    db.rollback()
                    failure = exc.code
                    _lock_conversation(db, conversation_id)
                    if not _holds_claim(db, conversation_id, turn, token):
                        db.rollback()
                        terminal = True
                        yield superseded
                        return True, None
                    if fix_text:
                        db.add(AssistantMessage(
                            workspace_id=conversation.workspace_id,
                            conversation_id=conversation_id, turn=turn, role="assistant",
                            text=fix_text, runtime_session_id=session_id,
                        ))
            elif failure is None:
                failure = "the correction reply carried no proposal change"
            settle(None, failure)
            db.commit()
            return False, None

        def rejected_row(final: AssistantProposal) -> None:
            db.add(AssistantMessage(
                workspace_id=conversation.workspace_id, conversation_id=conversation_id,
                turn=turn, role="error", name=PROPOSAL_REJECTED_NAME,
                text=rejection_note(final.validation_errors or []),
                runtime_session_id=session_id,
            ))

        def autocorrect_phase(
            first: AssistantProposal, notice_id: int,
        ) -> Generator[dict[str, Any], None, AssistantProposal | None]:
            """N17-obs295: the ONE automatic correction call of a model proposal that
            failed only structural evaluation-plan rules. The first reply and its invalid
            revision are already committed; this sends the validation errors back as an
            internal follow-up (never a member message), records what the correction
            produced as the next revision, and renames the notice row to the outcome.
            Returns the revision the member is shown, or ``None`` when the turn already
            ended (interrupted / superseded). Never loops: a failed correction leaves the
            last invalid revision with its ``proposal_rejected`` row, as a turn without
            the correction would."""
            errors = list(first.validation_errors or [])

            def settle(final: AssistantProposal | None, reason: str | None) -> None:
                final = final or db.get(AssistantProposal, first.id)
                outcome = (PROPOSAL_AUTOCORRECTED if final.status != "invalid"
                           else PROPOSAL_AUTOCORRECT_FAILED)
                row = db.get(AssistantMessage, notice_id)
                if row is not None:
                    row.name = outcome
                    row.text = autocorrect_note(
                        errors, outcome,
                        (reason or "the corrected version still fails validation")
                        if outcome == PROPOSAL_AUTOCORRECT_FAILED else None)
                if final.status == "invalid":
                    rejected_row(final)

            ended, final = yield from correction_phase(
                autocorrect_prompt(first.revision, errors),
                start_event={"event": "autocorrect", "data": {
                    "revision": first.revision, "errors": errors,
                    "validation_error_details": validation_codes.details(errors)}},
                phase="autocorrect", session_name=AUTOCORRECT_SESSION_NAME,
                session_text=f"automatic correction of proposal revision {first.revision}",
                settle=settle)
            if ended:
                return None
            return final or db.get(AssistantProposal, first.id)

        def reask_phase(
            base: int | None, reason: str, notice_id: int,
        ) -> Generator[dict[str, Any], None, tuple[bool, AssistantProposal | None]]:
            """N17-obs468: the ONE corrective call of a reply that carried no proposal block
            although it imitated the replay placeholder or claimed a submitted revision.
            ``(ended, revision)`` — the revision it stored, or None (the notice then says
            the proposal was not updated)."""

            def settle(final: AssistantProposal | None, _reason: str | None) -> None:
                row = db.get(AssistantMessage, notice_id)
                if row is not None:
                    row.name = PROPOSAL_MISSING_RECOVERED if final is not None else PROPOSAL_MISSING
                    row.text = missing_note(row.name, base,
                                            final.revision if final is not None else None)
                if final is not None and final.status == "invalid":
                    rejected_row(final)

            return (yield from correction_phase(
                reask_prompt(base, reason),
                start_event={"event": "reask", "data": {"revision": base, "reason": reason}},
                phase="reask", session_name=REASK_SESSION_NAME,
                session_text="corrective re-ask of a reply without a proposal block",
                settle=settle))

        try:
            yield from stream_phase(messages, parts)
        except Exception as exc:
            if run.cancelled:
                # cancelled first, failed second: whatever raised after the owner closed
                # the transport is a consequence of the disconnect, not the turn's result
                logger.info("assistant: turn %s interrupted; late failure %s", turn,
                            _short(exc))
            else:
                _persist_partial(db, ids, turn, token, session_id, "".join(parts),
                                 f"{type(exc).__name__}: {exc}")
                terminal = True
                payload: dict[str, Any] = {"message": f"{type(exc).__name__}: {exc}"}
                if isinstance(exc, AppError):
                    payload = {"code": exc.code, "message": exc.message}
                elif isinstance(exc, architect_runtime.RuntimeStreamError) and exc.detail:
                    # the SDK's terminal error frame (N-ARCH-obs18): the member and the
                    # turn event log get the real cause, structured — ``error_type`` is
                    # the Harness-side exception class (ReadTimeout, ModelThrottled…)
                    payload["detail"] = exc.detail
                yield {"event": "error", "data": payload}
                return
        if run.cancelled:
            # the owner cancelled while we were blocked upstream: the stream ended
            # because it was closed, not because the reply completed — keep the partial
            # text and a complete streamed block (N-ARCH-obs12)
            _persist_interrupted(db, ids, turn, token, session_id, "".join(parts),
                                 created_by=identity.username)
            terminal = True
            yield {"event": "error", "data": {"message": "interrupted"}}
            return
        text = "".join(parts)
        block, block_errors = proposal_contract.extract_block(text)
        patch, patch_errors = proposal_contract.extract_patch(text)
        requirements, preparation_errors = preparation.extract(text)
        _lock_conversation(db, conversation_id)
        if not _holds_claim(db, conversation_id, turn, token):
            # our claim was reclaimed as stale while we streamed: never publish
            db.rollback()
            terminal = True
            yield {"event": "error", "data": {"code": "assistant.turn_superseded",
                                              "message": "this turn's claim expired and was "
                                                         "taken over; its reply was discarded"}}
            return
        db.add(AssistantMessage(
            workspace_id=conversation.workspace_id, conversation_id=conversation_id,
            turn=turn, role="assistant", text=text, runtime_session_id=session_id,
        ))
        fresh = db.get(AssistantConversation, conversation_id)
        preparation_event = None
        if requirements is not None:
            state = fresh.preparation or {}
            if requirements != state.get("requirements", []):
                fresh.preparation = {**state, "requirements": requirements,
                                     "revision": state.get("revision", 0) + 1}
        if preparation_errors:
            db.add(AssistantMessage(
                workspace_id=fresh.workspace_id, conversation_id=conversation_id,
                turn=turn, role="error", name=preparation.REJECTED_NAME,
                text=("Preparation suggestions could not be read: "
                      + "; ".join(preparation_errors))[:4000],
                runtime_session_id=session_id,
            ))
        proposal_event: dict[str, Any] | None = None
        first: AssistantProposal | None = None  # an invalid revision to auto-correct
        notice: AssistantMessage | None = None
        missing: AssistantMessage | None = None  # a pending N17-obs468 re-ask notice
        missing_base: int | None = None
        missing_reason: str | None = None
        if block is not None or block_errors or patch is not None or patch_errors:
            fresh = db.get(AssistantConversation, conversation_id)
            raw, input_errors = _model_proposal_input(
                db, conversation_id, block, block_errors, patch, patch_errors)
            if raw is None:
                revision = record_unusable(db, conversation_id, fresh.workspace_id,
                                           input_errors, created_by=identity.username)
            else:
                revision = record_proposal(
                    db, conversation_id, fresh.catalog or {}, fresh.workspace_id, raw,
                    source="model", created_by=identity.username, extra_errors=input_errors,
                )
            if (revision.status == "invalid" and raw is not None and not input_errors
                    and get_settings().assistant_proposal_autocorrect
                    and validation_codes.autocorrectable(revision.validation_errors)):
                # N17-obs295: only structural evaluation-plan rules failed — ONE internal
                # correction call follows in this turn; the member sees its outcome
                first = revision
                notice = AssistantMessage(
                    workspace_id=conversation.workspace_id, conversation_id=conversation_id,
                    turn=turn, role="error", name=PROPOSAL_AUTOCORRECT_PENDING,
                    text=autocorrect_note(list(revision.validation_errors or []),
                                          PROPOSAL_AUTOCORRECT_PENDING),
                    runtime_session_id=session_id,
                )
                db.add(notice)
            else:
                if revision.status == "invalid":
                    # the rejection becomes part of the transcript: the member sees it in
                    # line and the next turn replays it to the model (``compose_messages``),
                    # so a bad block is corrected without the member relaying the errors
                    db.add(AssistantMessage(
                        workspace_id=conversation.workspace_id,
                        conversation_id=conversation_id,
                        turn=turn, role="error", name=PROPOSAL_REJECTED_NAME,
                        text=rejection_note(revision.validation_errors or []),
                        runtime_session_id=session_id,
                    ))
                proposal_event = {"event": "proposal", "data": proposal_out(db, revision)}
        elif (missing_reason := phantom_reason(text)) is not None:
            # N17-obs468: no block, yet the reply imitates the replay placeholder or claims
            # a submitted revision — the member is told, and (when enabled) ONE corrective
            # call follows in this turn
            base_row = patch_base(db, conversation_id)
            missing_base = base_row.revision if base_row is not None else None
            reask = get_settings().assistant_proposal_reask
            name = PROPOSAL_MISSING_PENDING if reask else PROPOSAL_MISSING
            missing = AssistantMessage(
                workspace_id=conversation.workspace_id, conversation_id=conversation_id,
                turn=turn, role="error", name=name, text=missing_note(name, missing_base),
                runtime_session_id=session_id,
            )
            db.add(missing)
            logger.warning("assistant: turn %s of %s carried no proposal block (%s)",
                           turn, conversation_id, missing_reason)
            if not reask:
                missing = None
        if first is None and (requirements is not None or proposal_event is not None):
            preparation_event = {"event": "preparation", "data": preparation.view(db, fresh)}
        db.commit()
        if first is not None:
            final = yield from autocorrect_phase(first, notice.id)
            if final is None:
                return  # interrupted / superseded: the terminal event was sent
            proposal_event = {"event": "proposal", "data": proposal_out(db, final)}
            preparation_event = {"event": "preparation",
                                 "data": preparation.view(
                                     db, db.get(AssistantConversation, conversation_id))}
        elif missing is not None:
            ended, recovered = yield from reask_phase(missing_base, missing_reason or "",
                                                      missing.id)
            if ended:
                return  # interrupted / superseded: the terminal event was sent
            if recovered is not None:
                proposal_event = {"event": "proposal", "data": proposal_out(db, recovered)}
                preparation_event = {"event": "preparation",
                                     "data": preparation.view(
                                         db, db.get(AssistantConversation, conversation_id))}
        terminal = True
        if proposal_event is not None:
            yield proposal_event
        if preparation_event is not None:
            yield preparation_event
        yield {"event": "done", "data": {"turn": turn}}
    finally:
        if not terminal:
            # GeneratorExit (client went away) or an unexpected error before the
            # terminal row: keep what the member saw, mark the turn interrupted, and
            # never derive a proposal from an incomplete reply.
            try:
                _persist_interrupted(db, ids, turn, token, session_id, "".join(parts),
                                     created_by=identity.username)
            except Exception:  # pragma: no cover — teardown must not mask the cause
                logger.exception("assistant: could not persist interrupted turn %s", turn)
            # an auto-correction in flight is settled, never left pending (rev-r27 #1)
            _settle_interrupted_autocorrect(db, ids, turn, token, "interrupted")
        try:
            release_turn(db, conversation_id, turn, token)
        except Exception:  # pragma: no cover
            logger.exception("assistant: could not release turn claim %s", turn)
        with _LIVE_TURNS_LOCK:
            if _LIVE_TURNS.get(conversation_id) == token:
                del _LIVE_TURNS[conversation_id]
        # A terminal ledger row does not mean the transport is closed: on every exit
        # (completion, early error, claim loss, cancel) close the upstream stream and
        # give the producer a bounded chance to finish — never leave a blocked reader.
        run.signal()
        if producer is not None:
            producer.join(timeout=HEARTBEAT_S * 3)
            if producer.is_alive():  # pragma: no cover — logged, never hidden
                logger.warning("assistant: turn %s producer still alive after cleanup", turn)
        run.finished = True


# ---------------------------------------------------------------------------
# live turns (N-ARCH-obs15): the producer is owned by the process, not the connection
# ---------------------------------------------------------------------------


class LiveTurn(live_turns.LiveTurn):
    """One running turn of one conversation: the driver thread that iterates
    ``run_turn``, the in-memory event buffer every subscriber tails (the shared
    ``live_turns.LiveTurn``) and the handle (``run``) an explicit abort would use.
    ``state`` is ``finishing`` while ``run_turn``'s own flag says the upstream ended and
    the reply is being validated / persisted."""

    def __init__(self, conversation_id: str, workspace_id: str | None, principal: str,
                 username: str) -> None:
        super().__init__(conversation_id, workspace_id, username)
        self.conversation_id = conversation_id
        self.principal = principal
        self.run = TurnRun()

    def is_finishing(self) -> bool:
        return self.run.finishing


_LIVE_RUNS = live_turns.LiveTurnRegistry()


def live_run(conversation_id: str, turn: int | None = None) -> LiveTurn | None:
    """The turn running in THIS process for the conversation (and, when given, for
    exactly that turn number), or ``None`` — ``LiveTurnRegistry.get``: a live run whose
    ``meta`` is not published yet is never assumed to be the asked-for turn (the caller
    waits up to ``HEARTBEAT_S``), so a reattach to turn N never tails a just-started N+1."""
    live = _LIVE_RUNS.get(conversation_id, turn)
    assert live is None or isinstance(live, LiveTurn)
    return live


def start_turn(
    conversation_id: str,
    workspace_id: str,
    row: Workspace,
    workspace: WorkspaceContext,
    identity: Identity,
    prompt: str,
    retry_of: int | None = None,
) -> LiveTurn:
    """Start one turn as a server-side unit of work and return its live handle. The
    caller has already run the pre-stream refusals (``require_turn_capacity`` etc.);
    the claim itself is taken by ``run_turn`` on the driver thread, so a lost claim
    race surfaces as the stream's ``error`` event exactly as before. A second start for
    a conversation whose turn is still live here is refused (409)."""
    principal = principal_of(identity)
    live = LiveTurn(conversation_id, workspace_id, principal, identity.username)

    def conflict(current: live_turns.LiveTurn) -> AppError:
        return AppError(
            "assistant.turn_in_progress",
            f"turn {current.turn} of this conversation is still running; wait for it "
            "to finish",
            {"active_turn": current.turn}, status_code=409,
        )

    _LIVE_RUNS.start(live, _drive_turn, (live, row, workspace, identity, prompt, retry_of),
                     name=f"assistant-turn-driver-{conversation_id[:8]}", conflict=conflict)
    return live


def _drive_turn(
    live: LiveTurn, row: Workspace, workspace: WorkspaceContext, identity: Identity,
    prompt: str, retry_of: int | None,
) -> None:
    """The producer: iterate ``run_turn`` to its end whatever any subscriber does,
    publish every event with its ``seq`` and append it to the event log. ``run_turn``
    yields only between its own committed transactions, so the log writes below never
    interleave with an open write of the turn; they use their own session anyway."""
    session = SessionLocal()
    log_db = SessionLocal()
    log = live_turns.EventLogBatcher(
        lambda events: _append_turn_events(log_db, live, events),
        lambda: live.turn is not None,
    )

    try:
        try:
            conversation = accessible_conversation(
                session, live.workspace_id, live.principal, live.conversation_id)
            live.run.inner = run_turn(
                session, conversation, row, workspace, identity, prompt, run=live.run,
                retry_of=retry_of,
            )
            for event in live.run.inner:
                if event["event"] == "heartbeat":
                    log.idle()
                    continue
                if event["event"] == "meta":
                    live.turn = event["data"]["turn"]  # ``publish`` wakes ``live_run`` waiters
                log.add(live.publish(event))
        except AppError as exc:  # a claim refusal after the start (run_turn's own writes are done)
            log.pending.append(live.publish({"event": "error",
                                             "data": {"code": exc.code, "message": exc.message}}))
        except Exception as exc:  # pragma: no cover — run_turn reports its own failures
            logger.exception("assistant: turn driver of %s failed", live.conversation_id)
            log.pending.append(live.publish({"event": "error",
                                             "data": {"message": f"{type(exc).__name__}: {exc}"}}))
        finally:
            try:
                log.flush()
            except Exception:  # pragma: no cover — the transcript row is already durable
                logger.exception("assistant: could not flush the event log of turn %s",
                                 live.turn)
    finally:
        # Never left to GC (like ``TurnStreamResponse``): a failure in the loop body
        # above (publish / flush) leaves ``run_turn`` suspended at a yield, and only
        # ``close()`` runs its ``finally`` — interrupted row, claim release, upstream
        # close — before subscribers are told the turn is done. Idempotent on a
        # generator that already finished or raised.
        if live.run.inner is not None:
            try:
                live.run.inner.close()
            except Exception:  # pragma: no cover — cleanup must not mask the cause
                logger.exception("assistant: closing run_turn of %s failed",
                                 live.conversation_id)
        live.finish()
        _LIVE_RUNS.drop(live)
        log_db.close()
        session.close()


def _append_turn_events(db: Session, live: LiveTurn, events: list[dict[str, Any]]) -> None:
    db.rollback()
    db.add_all([
        AssistantTurnEvent(
            workspace_id=live.workspace_id, conversation_id=live.conversation_id,
            turn=live.turn, seq=e["seq"], event=e["event"], data=e["data"],
        )
        for e in events
    ])
    db.commit()


def turn_events(db: Session, conversation_id: str, turn: int, after: int = 0
                ) -> list[AssistantTurnEvent]:
    return (
        db.query(AssistantTurnEvent)
        .filter(AssistantTurnEvent.conversation_id == conversation_id,
                AssistantTurnEvent.turn == turn, AssistantTurnEvent.seq > after)
        .order_by(AssistantTurnEvent.seq.asc())
        .all()
    )


def _event_out(row: AssistantTurnEvent) -> dict[str, Any]:
    return {"event": row.event, "data": row.data, "seq": row.seq}


def turn_sender(db: Session, row: AssistantConversation, turn: int) -> str | None:
    """Display username of the member who sent ``turn`` (the live starter while it runs
    here, else the turn's ``user`` row — rows from before authors were recorded were
    the owner's); ``None`` when the turn left no user row yet."""
    live = live_run(row.id, turn)
    if live is not None:
        return live.username
    user_row = (
        db.query(AssistantMessage)
        .filter(AssistantMessage.conversation_id == row.id, AssistantMessage.turn == turn,
                AssistantMessage.role == "user")
        .order_by(AssistantMessage.id.asc())
        .first()
    )
    if user_row is None:
        return None
    return user_row.author or row.owner


def live_turn_view(db: Session, row: AssistantConversation) -> dict[str, Any] | None:
    """``{turn, seq, started_at, state, author}`` of the turn a client should reattach
    to: the one live in this process, else a still-valid claim (another process, or an
    orphan the next startup sweep will finalize); ``None`` when nothing is running."""
    live = live_run(row.id)
    if live is not None and live.turn is not None:
        return {"turn": live.turn, "seq": live.seq, "started_at": live.started_at.isoformat(),
                "state": live.state, "author": live.username}
    if row.active_turn is None or _claim_is_stale(row, datetime.now(UTC)):
        return None
    last = db.execute(
        select(AssistantTurnEvent.seq)
        .where(AssistantTurnEvent.conversation_id == row.id,
               AssistantTurnEvent.turn == row.active_turn)
        .order_by(AssistantTurnEvent.seq.desc()).limit(1)
    ).scalar()
    started = row.active_turn_started_at
    return {"turn": row.active_turn, "seq": last or 0,
            "started_at": started.isoformat() if started else None,
            "state": "running", "author": turn_sender(db, row, row.active_turn)}


def require_turn(
    db: Session, row: AssistantConversation, turn: int, identity: Identity, *,
    read_only: bool = False,
) -> None:
    """404 for a turn number the conversation never reached; 409
    ``assistant.turn_not_yours`` when another member sent it (the same member who sent
    a turn is the one who may reattach to it). ``read_only`` is the viewer that cannot
    collaborate at all (an administrator on another member's unshared conversation):
    it was never a sender and never will be, so it tails any turn of the conversation
    read-only — the stream is a subscription, it writes nothing. A collaborator keeps
    the sender rule."""
    if turn < 1 or turn > (row.turns or 0):
        raise NotFoundError("assistant.turn_not_found", f"turn {turn} does not exist")
    if read_only:
        return
    sender = turn_sender(db, row, turn)
    if sender is not None and sender != identity.username:
        raise AppError("assistant.turn_not_yours",
                       f"turn {turn} was sent by another member", {"author": sender},
                       status_code=409)


def stream_turn(conversation_id: str, turn: int, after: int = 0) -> Iterator[dict[str, Any]]:
    """Reattach to a turn: the persisted events with ``seq > after``, then the live tail
    (this process's buffer, or the ledger polled while the claim is held elsewhere),
    then the terminal event. A turn with no log and no claim ends at once (``done``
    when it has a reply row, else ``error: interrupted``). Opens its own session."""

    def claim_held(db: Session) -> bool:
        conversation = db.get(AssistantConversation, conversation_id)
        return (conversation is not None and conversation.active_turn == turn
                and not _claim_is_stale(conversation, datetime.now(UTC)))

    def ended(db: Session) -> dict[str, Any]:
        replied = db.query(AssistantMessage).filter(
            AssistantMessage.conversation_id == conversation_id,
            AssistantMessage.turn == turn, AssistantMessage.role == "assistant",
        ).first() is not None
        return ({"event": "done", "data": {"turn": turn}} if replied
                else {"event": "error", "data": {"message": "interrupted"}})

    yield from live_turns.tail_turn(
        live=lambda: live_run(conversation_id, turn),
        read_events=lambda db, last: [_event_out(r)
                                      for r in turn_events(db, conversation_id, turn, last)],
        claim_held=claim_held, ended=ended, after=after,
    )


def _settle_pending_autocorrect(
    db: Session, conversation_id: str, workspace_id: str | None, turn: int, reason: str,
) -> int:
    """rev-r27 #1: an auto-correction that ended without its outcome (cancel, client gone,
    claim taken over, dead process) is settled like a failed one — never left
    ``pending``. Inside the caller's transaction (no lock, no commit). The latest revision
    decides: a valid one (a complete correction block salvaged by ``_persist_interrupted``)
    → ``proposal_autocorrected``; otherwise ``proposal_autocorrect_failed`` with ``reason``
    and a ``proposal_rejected`` row for the latest invalid revision unless the turn already
    has one, so the next turn replays the errors. Returns the number of notices settled."""
    rows = [m for m in _messages(db, conversation_id) if m.turn == turn]
    latest = latest_proposal(db, conversation_id)
    # N17-obs468: a re-ask that ended without its outcome — recovered only when a model
    # revision beyond the one the notice named was stored meanwhile
    settled = 0
    for notice in [m for m in rows if m.role == "error" and m.name == PROPOSAL_MISSING_PENDING]:
        base = missing_detail(notice.text)["base"]
        recovered = (latest is not None and latest.source == "model"
                     and (base is None or latest.revision > base))
        notice.name = PROPOSAL_MISSING_RECOVERED if recovered else PROPOSAL_MISSING
        notice.text = missing_note(notice.name, base, latest.revision if recovered else None)
        settled += 1
    pending = [m for m in rows if m.role == "error" and m.name == PROPOSAL_AUTOCORRECT_PENDING]
    if not pending:
        return settled
    corrected = latest is not None and latest.status != "invalid"
    for notice in pending:
        errors = validation_codes.note_lines(notice.text)
        outcome = PROPOSAL_AUTOCORRECTED if corrected else PROPOSAL_AUTOCORRECT_FAILED
        notice.name = outcome
        notice.text = autocorrect_note(errors, outcome, None if corrected else reason)
    rejected = any(m.role == "error" and m.name == PROPOSAL_REJECTED_NAME for m in rows)
    if not corrected and not rejected and latest is not None:
        db.add(AssistantMessage(
            workspace_id=workspace_id, conversation_id=conversation_id, turn=turn,
            role="error", name=PROPOSAL_REJECTED_NAME,
            text=rejection_note(latest.validation_errors or []),
            runtime_session_id=pending[-1].runtime_session_id,
        ))
    return settled + len(pending)


def _settle_interrupted_autocorrect(
    db: Session, ids: tuple[str, str | None], turn: int, token: str, reason: str,
) -> None:
    """``_settle_pending_autocorrect`` fenced by the turn's claim, in its own transaction
    (a no-op when the turn has no pending notice or the claim is gone — the claimer that
    took it over settles it then, ``_claim_turn_locked``)."""
    conversation_id, workspace_id = ids
    try:
        _lock_conversation(db, conversation_id)
        if not _holds_claim(db, conversation_id, turn, token):
            db.rollback()
            return
        if _settle_pending_autocorrect(db, conversation_id, workspace_id, turn, reason):
            db.commit()
        else:
            db.rollback()
    except Exception:  # the turn's own terminal rows are durable; never mask them
        db.rollback()
        logger.exception("assistant: could not settle the auto-correction of turn %s", turn)


def _finalize_dead_autocorrect(
    db: Session, row: AssistantConversation, turn: int, notice: AssistantMessage
) -> None:
    """Startup, N17-obs295: the process died during a turn's automatic proposal
    correction. The first reply and its invalid revision are durable; the correction is
    abandoned — the notice is settled ``failed``, the latest invalid revision gets its
    ``proposal_rejected`` row (so the next turn replays the errors) and the event log its
    terminal ``error``. A correction revision recorded before the crash is kept as is."""
    events = turn_events(db, row.id, turn)
    _settle_pending_autocorrect(db, row.id, row.workspace_id, turn,
                                "interrupted by a backend restart")
    if any(e.event in TURN_TERMINAL_EVENTS for e in events):
        db.commit()
        return
    db.add(AssistantTurnEvent(
        workspace_id=row.workspace_id, conversation_id=row.id, turn=turn,
        seq=(events[-1].seq if events else 0) + 1, event="error",
        data={"message": "interrupted"},
    ))
    db.commit()
    logger.warning("assistant: the proposal auto-correction of turn %s of %s was left by a "
                   "dead process; settled failed", turn, row.id)


def _finalize_dead_turn(db: Session, conversation_id: str) -> None:
    """Startup: the claim holder is gone. Persist the turn as interrupted from its event
    log — the text after the last ``retry`` (a replay discards the attempt before it),
    a proposal block that had streamed completely — and append a terminal ``error``
    event so a reattaching client sees the turn end. Writes nothing when the turn
    already has its terminal row or never wrote its user row."""
    row = db.get(AssistantConversation, conversation_id)
    if row is None or row.active_turn is None or row.active_turn_token is None:
        return
    turn, token = row.active_turn, row.active_turn_token
    rows = [m for m in _messages(db, conversation_id) if m.turn == turn]
    user_rows = [m for m in rows if m.role == "user"]
    pending = [m for m in rows if m.role == "error" and m.name in PENDING_NAMES]
    if user_rows and pending:
        _finalize_dead_autocorrect(db, row, turn, pending[-1])
        return
    if not user_rows or any(m.role in ("assistant", "error") for m in rows):
        return
    events = turn_events(db, conversation_id, turn)
    if any(e.event in TURN_TERMINAL_EVENTS for e in events):
        return
    parts: list[str] = []
    for e in events:
        if e.event == "retry":
            parts.clear()
        elif e.event == "delta":
            parts.append(str((e.data or {}).get("text", "")))
    sessions = [m for m in rows if m.role == SESSION_ROLE]
    session_id = (sessions[-1] if sessions else user_rows[0]).runtime_session_id or ""
    _persist_interrupted(db, (conversation_id, row.workspace_id), turn, token, session_id,
                         "".join(parts), created_by=user_rows[0].author or row.owner)
    db.rollback()
    db.add(AssistantTurnEvent(
        workspace_id=row.workspace_id, conversation_id=conversation_id, turn=turn,
        seq=(events[-1].seq if events else 0) + 1, event="error",
        data={"message": "interrupted"},
    ))
    db.commit()
    logger.warning("assistant: turn %s of %s was left by a dead process; persisted interrupted",
                   turn, conversation_id)


# ---------------------------------------------------------------------------
# member edits / reject
# ---------------------------------------------------------------------------


def edit_proposal(
    db: Session, conversation: AssistantConversation, content: Any, identity: Identity
) -> AssistantProposal:
    """A member edit is a NEW revision (never a mutation of an approved one) and
    needs its own approval. The byte cap is enforced before anything is stored."""
    if proposal_contract.serialized_bytes(content) > proposal_contract.PROPOSAL_MAX_BYTES:
        raise AppError(
            "assistant.proposal_too_large",
            f"the proposal exceeds {proposal_contract.PROPOSAL_MAX_BYTES} bytes",
            {"max_bytes": proposal_contract.PROPOSAL_MAX_BYTES}, status_code=413,
        )
    conversation_id = conversation.id
    _lock_conversation(db, conversation_id)
    fresh = db.get(AssistantConversation, conversation_id)
    preparation.require_idle(fresh)
    content, notes = carry_evaluation_plan(content, latest_proposal(db, conversation_id))
    row = record_proposal(db, conversation_id, fresh.catalog or {}, fresh.workspace_id, content,
                          source="member", created_by=identity.username, notes=notes)
    db.commit()
    return row


EVALUATION_PLAN_CARRIED_NOTE = (
    f"{REPAIR_NOTE_PREFIX}: evaluation_plan carried from revision {{revision}} — "
    "the editor did not submit it"
)


def carry_evaluation_plan(
    content: Any, previous: AssistantProposal | None,
) -> tuple[Any, list[str]]:
    """N15-obs13 — the server-side defence for a member edit that OMITS the
    ``evaluation_plan`` key while the revision it edits carries one: the previous seed
    (evaluators, ``recommendation_keys``, scenarios, blocked tests) is carried forward
    verbatim and a ``proposal_repaired`` note is recorded on the new revision, so an
    older console build or a hand-written edit cannot strand the gates a blocking
    recommendation names. Semantics of the submitted key:

    * **absent** → carried from ``previous`` when it has a non-null seed (note recorded);
      nothing happens when the previous revision has none;
    * **``null``** → an explicit removal, respected: the stored content has no seed and
      no note is recorded (a member may drop the seed deliberately);
    * **an object** (edited or unchanged) → stored as submitted, never merged.

    Returns ``(content, notes)``; ``content`` is the same object when nothing changed."""
    if not isinstance(content, dict) or "evaluation_plan" in content or previous is None:
        return content, []
    prior = previous.content if isinstance(previous.content, dict) else {}
    seed = prior.get("evaluation_plan")
    if seed is None:
        return content, []
    return ({**content, "evaluation_plan": copy.deepcopy(seed)},
            [EVALUATION_PLAN_CARRIED_NOTE.format(revision=previous.revision)])


def reject_proposal(
    db: Session, conversation: AssistantConversation, revision: int, identity: Identity
) -> AssistantProposal:
    conversation_id = conversation.id
    _lock_conversation(db, conversation_id)
    row = proposal_by_revision(db, conversation_id, revision)
    preparation.require_idle(db.get(AssistantConversation, conversation_id))
    latest = latest_proposal(db, conversation_id)
    if row is None or latest is None or latest.revision != revision:
        db.rollback()
        raise AppError("assistant.proposal_stale",
                       "that proposal revision is not the current one",
                       {"current_revision": latest.revision if latest else None},
                       status_code=409)
    changed = db.execute(
        update(AssistantProposal)
        .where(AssistantProposal.id == row.id, AssistantProposal.status.in_(("draft", "invalid")))
        .values(status="rejected", rejected_by=identity.username, rejected_at=datetime.now(UTC))
    ).rowcount
    if changed != 1:
        # N16-obs117: a refusal writes nothing. The write lock above is an UPDATE that
        # stamps the conversation's ``updated_at``; committing it here used to re-sort the
        # conversations list on a refused click. Roll the transaction back BEFORE deciding
        # the answer, so neither the already-approved 409 nor the idempotent re-reject
        # leaves a changed row behind.
        db.rollback()
        db.expire_all()
        row = db.get(AssistantProposal, row.id)
        if row is None:  # pragma: no cover — cleared between the lock and the re-read
            raise NotFoundError("assistant.conversation_not_found", "conversation not found")
        if row.status == "approved":
            raise AppError("assistant.proposal_already_approved",
                           "an approved proposal cannot be rejected; its deployment already "
                           "exists",
                           {"revision": row.revision, "approval": _approval_out(db, row)},
                           status_code=409)
        return row  # already rejected — nothing to write
    db.commit()
    db.expire_all()
    return db.get(AssistantProposal, row.id)


# ---------------------------------------------------------------------------
# approval — the only executor
# ---------------------------------------------------------------------------


@dataclass
class ApprovalOutcome:
    proposal: AssistantProposal
    agent: Agent
    job_id: str | None
    deployment_id: str | None
    started: bool  # True ⇔ this call created the job (caller launches it)
    job_status: str | None = None


def _outcome(db: Session, row: AssistantProposal, *, started: bool) -> ApprovalOutcome:
    agent = db.get(Agent, row.agent_id) if row.agent_id else None
    if agent is None:  # pragma: no cover — the approval commit always links an agent
        raise AppError("assistant.approval_inconsistent",
                       "approved proposal has no agent row", status_code=500)
    job = db.get(Job, row.job_id) if row.job_id else None
    return ApprovalOutcome(row, agent, row.job_id, row.deployment_id, started,
                           job.status if job else None)


def _stale(current: AssistantProposal | None, message: str) -> AppError:
    return AppError("assistant.proposal_stale", message,
                    {"current_revision": current.revision if current else None},
                    status_code=409)


def _prior_approval(db: Session, conversation_id: str) -> AssistantProposal | None:
    """The newest approved revision of the conversation that produced an Agent row."""
    return (
        db.query(AssistantProposal)
        .filter(AssistantProposal.conversation_id == conversation_id,
                AssistantProposal.status == "approved",
                AssistantProposal.agent_id.isnot(None))
        .order_by(AssistantProposal.revision.desc())
        .first()
    )


def _refuse_second_approval(db: Session, conversation_id: str, proposal_id: str) -> None:
    """N16-obs40 — architecture.md §Architect assistant: a conversation proposes ONE new
    managed-Harness agent and after approval "no longer offers another creation approval
    in that conversation". The server rule: once a revision of this conversation was
    approved and minted an Agent, no later draft is approvable here — even after that Agent
    was deleted (the member starts a new conversation, or CLEARs this one). 409
    ``assistant.already_approved`` names the approved revision and its Agent; the historical
    approval's own idempotent retry (same revision + hash) is unaffected.

    N17-obs462: the prior approval of THIS proposal (a twin of this very request that
    committed while it was running) is never a second creation — the caller goes on to its
    re-read and answers that recorded outcome."""
    prior = _prior_approval(db, conversation_id)
    if prior is None or prior.id == proposal_id:
        return
    agent = db.get(Agent, prior.agent_id)
    raise AppError(
        "assistant.already_approved",
        f"this conversation already created an Agent from revision {prior.revision} — a "
        "conversation proposes one agent; manage it from Agents or start a new conversation",
        {"agent_id": prior.agent_id, "revision": prior.revision, "job_id": prior.job_id,
         "agent_name": agent.name if agent is not None else None,
         "agent_status": agent.status if agent is not None else None},
        status_code=409,
    )


Recheck = Callable[[Session], tuple[Identity, Workspace]]


def _revalidate_caller(
    db: Session, conversation_id: str, identity: Identity, row: Workspace,
    recheck: "Recheck | None",
) -> None:
    """Current auth + permission + grant + readiness (via ``recheck``) and the immutable
    principal still collaborating on the conversation (its owner, or anyone while it is
    shared). Raises the auth error itself."""
    if recheck is not None:
        approver, fresh_row = recheck(db)
    else:
        approver, fresh_row = identity, db.get(Workspace, row.id)
    conversation = db.get(AssistantConversation, conversation_id)
    if (
        conversation is None
        or fresh_row is None
        or principal_of(approver) != principal_of(identity)
        or not may_collaborate(conversation, principal_of(approver))
    ):
        raise NotFoundError("assistant.conversation_not_found", "conversation not found")


def approve_proposal(
    db: Session,
    conversation: AssistantConversation,
    row: Workspace,
    workspace: WorkspaceContext,
    identity: Identity,
    *,
    revision: int,
    content_hash: str,
    recheck: Recheck | None = None,
) -> ApprovalOutcome:
    """Execute exactly one revision, exactly once.

    ``recheck(db)`` re-resolves the caller (account still active, still holds the
    deploy permission, still granted this workspace) and the workspace row from the
    database **inside** the write transaction, so a revocation during the catalog
    reads is honoured; without it the request-start identity is used (tests only).
    """
    conversation_id = conversation.id
    # 1. the exact requested revision — an already approved one returns its outcome
    #    even when newer revisions exist (that is the idempotent retry path)
    proposal = proposal_by_revision(db, conversation_id, revision)
    latest = latest_proposal(db, conversation_id)
    if proposal is None:
        raise _stale(latest, "that proposal revision does not exist — review the latest")
    if proposal.content_hash != content_hash:
        raise _stale(latest, "the proposal content changed since it was shown — review it again")
    if proposal.status == "approved":
        _revalidate_caller(db, conversation_id, identity, row, recheck)
        return _outcome(db, proposal, started=False)
    if proposal.status != "draft":
        raise AppError("assistant.proposal_not_approvable",
                       f"a {proposal.status} proposal cannot be approved",
                       {"status": proposal.status, "errors": proposal.validation_errors},
                       status_code=409)
    if latest is None or latest.revision != revision:
        raise _stale(latest, "that proposal revision is not the current one — review the latest")
    # 2. request-start snapshot checks (cheap refusals before any cloud read)
    if not identity.can(PERMISSION_DEPLOY):
        raise AppError("auth.permission_required",
                       f"This action requires the '{PERMISSION_DEPLOY}' permission",
                       {"permission": PERMISSION_DEPLOY}, status_code=403)
    requirements = deploy_requirements(row)
    if requirements:
        raise AppError("assistant.workspace_not_ready",
                       "this workspace cannot deploy yet: "
                       + "; ".join(m["message"] for m in requirements),
                       {"requirements": requirements}, status_code=409)
    def winner_after_io() -> ApprovalOutcome | None:
        """A twin may have executed this exact revision while we were reading: its
        recorded outcome is authoritative — but only for a caller who is STILL the
        authorized owner right now (re-resolved), never as a way around an auth
        or ownership failure."""
        db.rollback()
        db.expire_all()
        try:
            current = db.get(AssistantProposal, proposal.id)
        except ObjectDeletedError:
            current = None
        if current is None:
            # the conversation was cleared — or its account deleted, which cascades to
            # its conversations (N-ARCH-obs6) — while the catalog was being read
            raise NotFoundError("assistant.conversation_not_found", "conversation not found")
        if current.status != "approved":
            return None
        _revalidate_caller(db, conversation_id, identity, row, recheck)
        return _outcome(db, current, started=False)

    # N16-obs40: one Agent per conversation — a later draft is never a second creation. But a
    # twin that executed THIS revision after our draft read (step 1) is the idempotent retry,
    # not a second creation: answer its recorded outcome (N17-obs462 — the session still holds
    # this proposal as a draft, so the re-read rolls back and expires before deciding)
    prior = _prior_approval(db, conversation_id)
    if prior is not None and prior.id == proposal.id:
        winner = winner_after_io()
        if winner is not None:
            return winner
    _refuse_second_approval(db, conversation_id, proposal.id)
    # 3. LIVE catalog + resource identity — network I/O, outside every lock
    try:
        live_catalog = restrict_catalog(
            db, conversation.workspace_id,
            owner_viewer(db, conversation.workspace_id, conversation.owner_principal),
            preparation.live_catalog(workspace, list(conversation.preparation_sources or [])),
        )
    except Exception as exc:
        winner = winner_after_io()
        if winner is not None:
            return winner
        raise AppError(
            "assistant.catalog_unavailable",
            f"the workspace catalog could not be read: {_short(exc)}",
            {"revision": proposal.revision}, status_code=502,
        ) from exc
    winner = winner_after_io()
    if winner is not None:
        return winner
    content, _display, errors = proposal_contract.validate(
        proposal.content, live_catalog, principal_of_identity(identity))  # N17-obs203 §6
    if content is None or errors:
        raise AppError("assistant.proposal_invalid",
                       "the proposal no longer validates against the workspace: "
                       + "; ".join(errors),
                       {"errors": errors, "revision": proposal.revision}, status_code=409)
    live_bindings = proposal_contract.resource_bindings(content, live_catalog)
    if proposal.bindings is None or live_bindings != proposal.bindings:
        raise AppError(
            "assistant.bindings_changed",
            "the resources this proposal binds to changed since it was reviewed — refresh "
            "the catalog and review a new revision",
            {"revision": proposal.revision,
             "changed": proposal_contract.binding_diff(proposal.bindings, live_bindings)},
            status_code=409,
        )
    refuse_reserved_name(content.name)  # 422 agent.name_reserved (N16-obs82 family rule)
    spec = proposal_contract.to_agent_spec(content, live_catalog)
    # round 16 (N15-obs16): the same creation-time domain check as POST /api/agents — an
    # option the Harness cannot render / a workspace resource it needs, before any write.
    # No ``db``: the proposal's KB / tool references were just validated against the LIVE
    # catalog above (409 assistant.proposal_invalid), which is the knowledge backend's view
    # of the same ledger rows — the ledger lookup would only repeat it.
    method_validation.require_method_support(spec, workspace)
    resolved_spec = spec.model_dump()
    changed = [key for key in _SPEC_KEYS if resolved_spec.get(key) != live_bindings.get(key)]
    if changed:
        raise AppError(
            "assistant.bindings_changed",
            "the resolved resources differ from the reviewed bindings — refresh "
            "the catalog and review a new revision",
            {"revision": proposal.revision, "changed": changed},
            status_code=409,
        )
    # 4. ONE short write transaction: lock → recheck principal/workspace → re-read
    #    revision → name claim → revision claim → agent + deployment + job → commit
    _lock_conversation(db, conversation_id)
    try:
        if recheck is not None:
            approver, fresh_row = recheck(db)
        else:
            approver, fresh_row = identity, db.get(Workspace, row.id)
        # The principal that started the request and the principal resolved NOW must
        # be one and the same, and still collaborating (owner, or the conversation
        # is still shared): an account replaced under the same username during the
        # catalog read is not the approver.
        fresh_conversation = db.get(AssistantConversation, conversation_id)
        if (
            fresh_conversation is None
            or principal_of(approver) != principal_of(identity)
            or not may_collaborate(fresh_conversation, principal_of(approver))
        ):
            raise NotFoundError("assistant.conversation_not_found", "conversation not found")
        preparation.require_idle(fresh_conversation)
        if fresh_row is None or deploy_requirements(fresh_row):
            raise AppError("assistant.workspace_not_ready",
                           "this workspace cannot deploy yet",
                           {"requirements": deploy_requirements(fresh_row) if fresh_row else []},
                           status_code=409)
        # fresh, not the identity-mapped draft this session read at step 1 (N17-obs462)
        current = db.get(AssistantProposal, proposal.id, populate_existing=True)
        if current.status == "approved":  # recheck() already ran above in this transaction
            db.rollback()
            return _outcome(db, current, started=False)
        if current.status != "draft" or current.content_hash != content_hash:
            raise _stale(latest_proposal(db, conversation_id),
                         "the proposal changed while it was being approved — review the latest")
        _refuse_second_approval(db, conversation_id, proposal.id)  # re-checked under the lock
        now = datetime.now(UTC)
        claimed = db.execute(
            update(AssistantProposal)
            .where(AssistantProposal.id == proposal.id, AssistantProposal.status == "draft")
            .values(status="approved", approved_by=approver.username, approved_at=now)
        ).rowcount
        if claimed != 1:  # pragma: no cover — the lock serializes writers
            raise _stale(latest_proposal(db, conversation_id), "the proposal changed meanwhile")
        agent = Agent(
            workspace_id=fresh_row.id,
            name=spec.name,
            method="harness",
            status="deploying",
            spec=spec.model_dump(),
        )
        # the materialized agent's creator is the plan's approver (upstream #263 shape)
        creators.stamp(agent, creators.Creator.of(approver))
        db.add(agent)
        db.flush()
        agent_names.claim_agent_name(  # 409 → rollback; names the holder only if visible
            db, fresh_row.id, spec.name, agent.id,
            viewer=visibility.viewer_for_principal(db, fresh_row, principal_of(approver)),
        )
        deployment, job = create_deployment(
            db, agent, commit=False,
            payload_extra={"assistant": {
                "conversation_id": conversation_id,
                "proposal_id": proposal.id,
                "revision": proposal.revision,
                "approved_by": approver.username,
                "content": proposal.content,
                "bindings": proposal.bindings,
                # Source metadata comes from this owner-bound conversation, not
                # proposal/browser paths. Preserve it for resumed/deleted-history jobs.
                "preparation_sources": list(fresh_conversation.preparation_sources or []),
            }},
        )
        db.execute(
            update(AssistantProposal)
            .where(AssistantProposal.id == proposal.id)
            .values(agent_id=agent.id, deployment_id=deployment.id, job_id=job.id)
        )
        db.commit()
    except (AppError, IntegrityError) as exc:
        if isinstance(exc, AppError) and exc.code.split(".")[0] in ("auth", "workspace") or (
            isinstance(exc, NotFoundError)
        ):
            db.rollback()
            raise  # an authorization/ownership failure is never converted into a success
        db.rollback()
        db.expire_all()
        current = db.get(AssistantProposal, proposal.id)
        if current is not None and current.status == "approved":
            # a racing approval of this very revision won — hand back ITS outcome, to
            # a caller who is still the authorized owner
            _revalidate_caller(db, conversation_id, identity, row, recheck)
            return _outcome(db, current, started=False)
        raise
    db.expire_all()
    proposal = db.get(AssistantProposal, proposal.id)
    logger.info("assistant: proposal %s r%s approved by %s → agent %s job %s",
                proposal.id, proposal.revision, approver.username, agent.id, job.id)
    return ApprovalOutcome(proposal, agent, job.id, deployment.id, True, "queued")


# ---------------------------------------------------------------------------
# job entry guard: the pipeline deploys the reviewed bindings or nothing
# ---------------------------------------------------------------------------

_SPEC_KEYS = ("name", "method", "model_id", "model_source", "system_prompt", "tools",
              "skills", "knowledge_bases", "memory", "max_iterations", "timeout_seconds")


def assert_job_bindings_pinned(
    payload: dict[str, Any] | None, agent: Agent, workspace: WorkspaceContext
) -> None:
    """Run at deploy-job entry (fresh or resumed) BEFORE any stage: the approved
    content must still resolve, in the live workspace, to exactly the pinned
    bindings, the KB gateway prerequisites must exist (no gateway is created), and
    the agent's stored spec must be the pinned one. Any drift raises → the job lands
    as failed without touching AWS."""
    pin = (payload or {}).get("assistant") or {}
    pinned = pin.get("bindings")
    content_raw = pin.get("content")
    if not isinstance(pinned, dict) or not isinstance(content_raw, dict):
        raise RuntimeError("assistant job has no pinned bindings — refusing to deploy")
    copies = pin.get("skill_copies") or {}
    for key in _SPEC_KEYS:
        if key == "skills" and copies:
            expected = [copies.get(path, path) for path in pinned.get("skills") or []]
            if (agent.spec or {}).get("skills") == expected:
                continue
        if (agent.spec or {}).get(key) != pinned.get(key):
            raise RuntimeError(
                f"assistant job: agent spec member '{key}' differs from the approved "
                "bindings — refusing to deploy"
            )
    live_catalog = preparation.live_catalog(workspace, pin.get("preparation_sources") or [])
    content, _display, errors = proposal_contract.validate(content_raw, live_catalog)
    if content is None or errors:
        raise RuntimeError("assistant job: approved proposal no longer validates in this "
                           "workspace: " + "; ".join(errors))
    live = proposal_contract.resource_bindings(content, live_catalog)
    if live != pinned:
        changed = proposal_contract.binding_diff(pinned, live)
        raise RuntimeError(
            "assistant job: reviewed resource bindings changed since approval "
            f"({', '.join(changed)}) — refusing to deploy the drifted resources; approve a "
            "fresh revision"
        )
