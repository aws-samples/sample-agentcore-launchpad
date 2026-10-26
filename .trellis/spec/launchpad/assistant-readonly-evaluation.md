# Read-only evaluator capability contracts

## 1. Scope

Assistant proposal seeds, concrete evaluation plans, binding existing managed code
evaluators, and ordinary dataset/session/time-window evaluation runs. Read-only
business behavior permits approved retrieval and Skill support tools.

## 2. Signatures

- `capability_conflict_errors(evaluators, proposal_content, *, scenarios=())`
- `code_rule_capability_conflict_errors(checks, proposal_content, *, evaluator_key,
  scenarios=())`
- `seed_errors(seed, *, proposal_content=None)`

The shared helpers in `assistant/evaluation_plan.py` are pure and return actionable
errors. They do not rewrite persisted rows or call AWS.

## 3. Contract

Inspect all three capability lists: `tools`, `knowledge_bases`, `skills`. Any
nonempty list conflicts with a global `tool_count` whose `max` is zero and whose
`tool` is absent/empty, or an exact empty `tool_sequence`. Explicit expected
trajectories of included scenarios are also evidence of intended tool use; empty
trajectories are unspecified and blocked scenarios do not impose requirements.

`proposal._check_lists` passes full source content to seed validation.
`validate_plan` applies the same guard using its bound source proposal. The
materialization runner rechecks rules of known managed existing evaluators before
marking the reference ready. Ordinary run preflight uses the actual Agent spec and
rules recovered from the owning operation; rejection precedes run creation,
queueing, telemetry reads, invocation and batch evaluation.

## 4. Error matrix

| Input | Result |
|---|---|
| KB-only, Skill-only or explicit tools plus global max=0 | Invalid seed/plan |
| Exact empty tool sequence plus mounted resources | Invalid seed/plan |
| Same managed evaluator selected for a tool-enabled run | 422 `run.evaluator_capability_conflict` |
| Specific verified `send_email` max=0 | Allowed |
| No resources or expected tools plus global max=0 | Allowed |
| External evaluator named `no_tool_calls`, unknown code | No name-based inference |

Errors identify evaluator/rule, conflicting capabilities and the distinction
between business writes and read-only tools.

## 5. Cases

Good: allow financial KB retrieval and Skill loading while scenario assertions
forbid changing reports, sending messages or claiming those actions occurred.
Base: preserve a truly tool-free agent's legitimate zero-call evaluator.
Bad: interpreting `tools=[]` as zero tools when the proposal separately mounts a KB.

## 6. Tests

`test_assistant_readonly_evaluation.py` covers rule variants, all capability lists,
named bans, valid positive counts, expected trajectories and immutable inputs.
`test_evaluation_assets.py` covers prepare/edit/materialize, reuse of historical
managed evaluators, no-effects run rejection in all three scopes, and preservation
of original operation resources and approval hashes.

## 7. Wrong versus correct

Wrong: promote "refuse to modify this report" into a universal NoToolCalls gate.
Correct: preserve necessary reads and evaluate prohibited business actions and false
claims. The architect Skill release 1.4.3 and protocol preamble explain this rule.
Repair by appending corrected revisions and reusing compatible evaluator IDs;
never silently rewrite historical evaluator Lambda code or prior results.

## Runtime tool names

`assistant/tool_catalog.py` resolves approved attachment catalogs for rule review.
External MCP tools use the exact Harness attachment-name prefix; Gateway descriptors
retain their tool names. MCP tools/list pagination must finish within bounds; missing,
malformed or duplicate names/cursors are unavailable, never an empty guessed catalog.
No tool call is made. Positive allowlists are checked against selected resources at
plan save and must preserve implicit `skills`, per-KB Retrieve and agentic retrieval
support. Literal rule fields cannot contain configuration selectors (`mcp:`, etc.).
Pure selector/support checks also protect reuse and ordinary run preflight.
Catalog enrichment is advisory for attachment eligibility but authoritative for
positive-rule review. Explicit business-write bans can remain narrower than all
mounted tools. The architect Skill version 1.4.5 explains names and the 180-second
default execution budget; natural-language no-tool assertions must not ban legitimate
Skill/KB reads.

Native Harness shell/file_operations are available as explicit optional allowlist
choices. They are never appended to an approved rule merely because a trace used
them. Native names come from the documented managed runtime contract.

Registry MCP preparation changes are reviewed as new proposal revisions.
`rule_catalog_errors` validates positive named tool counts and tool sequences as
well as allowlists against selected attachments. Named max=0/write bans do not
require attaching the forbidden tool. Adding an MCP never expands a business
allowlist automatically. Skill 1.4.7 instructs the assistant to review golden
tests, trajectories and evaluator rules after changing MCP selections.

Scenario expected trajectories use the same selected callable catalog at save and
materialization, including reference-trajectory and judge expectations. Removed
MCP names, unselected native tools and attachment selectors cannot remain positive
expectations. Empty trajectories are unspecified; blocked tests are not scenarios.
Legacy shared-Gateway descriptor short names are restored only after live
discovery verifies the target-qualified name; discovery failure leaves the
catalog unavailable. No Registry record or old rule is rewritten.

## Semantic acceptance criteria

Architect Skill 1.4.8 and the proposal protocol select scorers by evidence.
Natural-language acceptance criteria, including grounded facts, refusal and false
execution claims, use a suitable listed evaluator, scenario assertions or a domain
judge. Literal code rules cannot establish semantic compliance; high risk does not
justify requiring a code evaluator for prose.

`reference_response` remains a literal containment rule, suitable only when the
contract explicitly requires the entire reference text in the answer. A rubric
such as "identify Lito X1 and give manual-supported pre-flight checks" belongs in
scenario assertions, not this rule. Equivalent wording and formatting must remain
acceptable. Code checks remain appropriate for observable tool invariants and
explicit literal contracts. Missing structured evidence requires external
verification, not a fabricated code check.

This is Skill/protocol guidance, not a new server-side semantic validator. Keep
the preset system prompt, pain-point mapping, deliverable template and proposal
self-check consistent.
Skill changes require matching `SKILL.md` and preset catalogue versions; existing
installed bundles and evaluation assets change only through explicit updates.
Preset repair preserves a stored system prompt. Updating its old default therefore
requires an explicit `reset: ["system_prompt"]` after verifying that the installed
prompt matches that default; preserve administrator-customized text and settings.
