# Architect assistant resource preparation

## API and ownership

Conversation detail always contains `preparation: {revision, knowledge_bases: string[],
skills: string[], tools: string[], requirements: {id, kind, title, reason, materials: string[],
required: boolean}[]}`. `kind` is `knowledge_base | skill | tool | clarification`.
The preparation revision is independent of the immutable proposal revision/hash.

- `PUT /api/assistant/architect/conversations/{id}/preparation` accepts only
  `{expected_revision: integer >= 0, knowledge_bases: string[], skills: string[], tools?: string[]}`;
  KB/Skill selections are required, unique, and capped at ten entries. MCP tools
  are unique catalog keys, capped at twenty; omission preserves existing tools,
  explicit [] clears. Returns full
  conversation detail. It uses the live Harness reference validator, including the
  KB gateway's id, ARN, OAuth provider, live READY state and URL.
- `POST .../{id}/preparation/skills` requires `agents.deploy` and accepts only
  `{expected_revision, staging_id, selections: [{index: integer >= 0}]}` (one to ten
  unique indexes). Returns `{conversation: <detail>, results: [{name, ok, key?, error?}]}`.
  Sources are acquired through the existing `/api/registry/skills/inspect` flow.
  Staging is bound to its inspecting immutable principal and workspace.
- `POST .../{id}/catalog` retains its `catalog` response and also returns
  `conversation: <detail>`. Refresh advances preparation revision and may mint a
  proposal revision before approval, so clients must replace/reload full detail.
  After any approval it remains readable but never mints a proposal from resource
  binding drift, including when the latest proposal is a newer evaluation draft.
- Every operation starts from `owned_conversation`; a different workspace,
  principal, or legacy NULL principal receives 404 before any AWS access.
  Client-supplied paths/source metadata are forbidden.

An empty or migrated conversation has revision zero and empty requirements.
Until the member explicitly saves a selection, the public projection inherits the
latest valid proposal's KBs, Skills and tools. Before approval, a valid later proposal
advances preparation revision; old proposal content, bindings, hashes and approvals
are untouched.

After **any** proposal has been approved, KB/Skill/MCP resource selections are locked
for the conversation, regardless of deployment success, failure or Agent deletion.
The highest-revision approved proposal supplies all three resource lists; historical
conversations may contain several approvals, newer unapproved drafts and stale saved
preparation. Public preparation and model context always project the approved lists.
No API input/output shape or migration is needed for this lock. Resource changes
belong to Agent management and redeployment at `/create`.

## Mutable state and concurrency

`AssistantConversation.preparation` stores revision, requirements and selections plus
the private `selection_set` and `tools_selection_set` markers. Legacy KB/Skill-only
state inherits tools from the latest valid proposal until explicit selection.
`preparation_sources` stores server-owned imported
source entries with opaque `imported:<uuid>` keys, S3 paths and staging/index retry
identity. Neither private field is an API input. Additive DDL in `_migrate_assistant_columns`
supports existing ledgers.

Selection updates fetch the catalog outside the write lock, then acquire the existing
conversation lock and recheck ownership, revision and in-flight state. Stale writes
return 409 `assistant.preparation_stale`; unavailable selections return 409
`assistant.preparation_invalid`. Catalog failures carry explicit warnings and never
justify selecting resources not present in the live result.

Selection saves and Skill imports return 409 `assistant.resources_locked` after
approval, even for unchanged selections. Check before AWS/S3 I/O, then recheck after
ownership resolution under the conversation write lock. A selection read that races
with approval must refuse without persisting selections or minting a revision.
Imports check before claiming and under the lock; the persisted import claim prevents
a concurrent approval from landing during upload.

Imports acquire a persisted `preparation_token` before S3 I/O. Turns, proposal edits,
approval, refresh, selection saves and purge refuse while it is held. Startup clears
orphaned import claims along with turn claims. Imports use the existing staged validation,
upload and partial-upload cleanup helpers. Each successful server-owned source is saved
before the subsequent catalog read; retrying that staging/index reuses its recorded key.
Partial failures retain staging. At most 30 imported sources belong to a conversation.

Before approval, new selections/bindings on the latest draft or a shape-valid invalid
proposal mint a member revision. A missing binding produces an invalid reviewable
revision; changing selections can repair it. The member must still approve the new
revision separately. Explicit member selections are included in bounded model context
and restored into validly shaped model proposals before reference validation.

After approval, manual proposal edits compare each KB/Skill/MCP resource set with the
latest approved proposal under the conversation lock. Changing or omitting a nonempty
set returns 409 `assistant.resources_locked`, even when the rest of the edit is invalid.
Reordering a set is permitted; ordinary duplicate and content validation still applies.
Model proposals restore
the approved lists before validation, independently of legacy preparation flags.
Neither model nor member revisions update saved preparation selections after approval.
Evaluation edits and repair turns retaining these resources remain available and use
ordinary validation and exact-revision plan preparation; they do not deploy or alter
historical content, hashes, bindings or approvals. Advisory requirements may still change.
Approval also checks resource sets before live reads and inside its write transaction,
so a resource-changing draft saved before this guard cannot bypass it. Retries of an
already approved historical revision retain their existing idempotent outcome.

## Advisory protocol and live Skill pinning

Completed replies may contain one optional `launchpad-preparation` fenced object
`{requirements: [...]}`. It is capped at 24,000 UTF-8 bytes and 20 unique requirements:
id 64 chars, title 200, reason 1,000, ten materials of 300 chars each. Unknown members,
invalid kinds, malformed/incomplete/multiple blocks and duplicate ids are rejected.
`[]` clears suggestions; an absent block preserves them. Failed/interrupted turns never
update requirements. Malformed suggestions produce a `preparation_rejected` transcript
error while otherwise valid prose/proposals remain usable. Successful updates emit a
`preparation` SSE event with the public projection.

The protocol describes explicit panel actions for KB creation/uploads and a new-tab
Registry Skill creation handoff (register, approve, return, refresh and select); it forbids automatic writes, tool/Gateway provisioning and Skill generation.
Upload completion does not establish KB searchability; ingestion completion is separate.

`preparation.live_catalog` merges imported sources and reads their actual S3 bytes on
refresh, selection save, approval and deploy-job entry. The ordinary proposal binding
digest and immutable content-addressed deployment copy remain authoritative. Approval
copies server-owned source metadata into the job payload so resumed jobs retain the
same validation even after conversation removal. Source drift fails before deployment.
Conversation deletion does not remove workspace KBs or Skill source objects.

## Verification

`backend/tests/test_assistant_preparation.py` exercises ownership, stale and concurrent
writes, completed-turn parsing, legacy projection/hash preservation, new proposal review,
import permission/staging boundaries, retry behavior and live digest drift. It also
covers post-approval save/import refusals before I/O, direct proposal resource edits,
approval races, refresh drift without new revisions, historical approval selection,
and evaluation repair with approved resource preservation. Keep the
existing assistant, staged ingestion, permission and ledger migration suites passing.

## Frontend behavior

`PreparationPanel` loads the managed KB list on mount without revising proposals.
Before approval, explicit catalog refresh updates the reviewed snapshot. The refresh response
must update full conversation state, while unsaved checkbox choices are preserved.
All KB create/upload/poll/sync, catalog and Skill inspect/import requests pin the
visible workspace. The rail is keyed by workspace and conversation, so a late
response cannot restore another conversation.

After approval the resource selectors are read-only and direct resource changes to
Agent management at `/create`. Catalog refresh can still show live availability;
availability changes do not revise the approved resource snapshot.
If a refresh discovers approval while the browser still has unsaved selections,
replace those choices with the approved projection before displaying the locked
controls. The page suppresses further creation approvals but retains proposal and
evaluation review. Prior to approval, the rail reports dirty choices to the page;
approval remains disabled with a visible save/cancel explanation until they resolve.

The translated ADLC figure is separate from the creation progress rail. Its six
methodology stages describe a feedback cycle, not six completed jobs. The rail
stays in normal document flow and scrolls with the page on desktop and mobile.
Step anchors reserve space only for the topbar. It has four required creation
steps; only job `succeeded` plus Agent `active`
completes the last one. Failure, pending deployment and unconfirmed readiness are
distinct. Evaluation, A/B and production feedback are three optional follow-ups;
navigating to them does not mark them complete.

`KnowledgeBaseCreate` retains the created id across upload failures and never
blindly retries an ambiguous create. New uploads start one initial indexing job
when the data source is AVAILABLE. Closing retains the workspace resource and
stops local polling. Existing KB selection only reads status. A completed prior
indexing job is described as the latest job, not proof every currently stored file
is indexed. New Skills are created in Registry through `/registry?view=register&type=AGENT_SKILLS`,
which preselects the Skill form and opens in a new tab. The rail retains only catalog
selection and refresh; earlier imported sources and their backend API remain compatible.
Natural-language authoring is excluded.

## Good and bad cases

- Good: importing a validated ZIP yields a private catalog key and a new review;
  a changed S3 digest later refuses approval until refreshed and reviewed.
- Base: a migrated conversation with no explicit preparation inherits the latest
  valid proposal selection and starts with no advisory requirements.
- Bad: a stale preparation revision, foreign staging id or arbitrary S3 path is
  rejected before upload. A successfully uploaded but unreadable bundle is reported
  as an import failure, remains unselected and can be retried without uploading again.
- Recovery: changing selections can repair a shape-valid proposal invalidated by
  resource drift; the prior revision keeps its content/hash and becomes superseded.

## Wrong versus correct

Wrong: treat a successful file upload or an ACTIVE KB as proof that all documents
are searchable, or display an empty catalog after a failed read.
Correct: show resource availability, upload result and indexing separately; retain
previous data and explicit errors, and use live server validation for attachment.

KB document/index links and the KB management link open with `target="_blank"`
and `rel="noopener noreferrer"`. Management uses the same `btn` link style as
Registry Skill creation, and both management links show the new-tab arrow.

Registry MCP selection uses the same refresh/save/cancel lifecycle and existing
catalog `tools` entries, including Gateway records. Unavailable selected entries
remain visible and removable. Runtime discovery failure is advisory for mounting,
but blocks literal positive evaluation rules. Tool requirement cards link to the
MCP section. Registry creation opens `/registry?view=register&type=MCP` in a new tab.
MCP preparation never provisions Gateways, invokes tools or grants native tools.
Before approval, changing selections mints a new proposal review; incompatible zero-call rules
remain invalid instead of being rewritten or omitted. Narrow MCP business
allowlists remain valid, with named write bans preserved.
