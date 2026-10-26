# Isolated code evaluator Lambda packages

## 1. Scope

Assistant evaluation-asset materialization, retries, existing-evaluator references,
initial Lambda revision recovery, cleanup, review counts and ordinary run preflight.
Live AgentCore callbacks can omit both documented evaluator identity fields.

## 2. Signatures and persisted shape

- `compose_intents(plan, op_id)` creates one resource chain per `CodeEvaluator`.
- Single-code operations retain bare keys; multi-code keys are `<kind>:<evaluator key>`
  and carry `code_group`. Function/role names use the operation id plus a deterministic
  twelve-character hash of the evaluator key.
- `_resource(resources, key, code_group=...)` requires one exact match.
- `_approved_package_rules(plan, resource)` checks the package against the bound
  approved evaluator; `_require_isolated_packages(resources, plan)` checks all chains
  before writes. The package is checked again at publication.
- `managed_package_evaluator_count(db, owner)` reads the saved package selected by its
  published Lambda ARN or explicit group. Unknown metadata stays unknown.
- `plan_summary(plan, resources=None)` counts intended new chains or actual recorded
  historical resources.

## 3. Contracts

Each chain owns a role, log group, Lambda function, invocation permission and optional
execution-role grant. Every function's `rules.json` contains exactly its evaluator's
rule set. Each evaluator request names that function's immutable published version.
Operation checkpoints always persist the full resources array. Failure/retry state
and dependencies are scoped by group. Cleanup removes an independent chain only after
its evaluators are gone; historical shared operations keep their former topology.

Initialization revision recovery selects the function by the recorded expected
CreateFunction RevisionId. Only its blocked dependents are released. Existing review
requirements, including resolving unrelated conflicts first, remain in force.

## 4. Validation and errors

- Pending/resumed legacy multi-rule packages are refused before new cloud writes.
- The ledger-only operation projection exposes `requires_new_plan`, using the same
  saved-package check as retry and worker entry. Explicit retry refuses incompatible
  packages with `409 assistant.evaluation_assets_new_plan_required` before queueing or
  consuming attempts.
- The console offers a replacement draft using the saved plan's exact content through
  the existing edit API. It preserves member edits and source binding, requires normal
  review/approval, and never reuses the historical shared package. A successful legacy
  operation also withholds evaluation next steps.
- A single-rule package whose content or group differs from the approved evaluator
  is refused even if its ZIP digest was recomputed consistently.
- Binding a known ambiguous managed evaluator produces a resource conflict.
- Ordinary runs using a known package with count other than one return
  `422 run.evaluator_package_ambiguous` before invocation or run creation.
- Unknown external code packages are not classified from their names.
- The handler still rejects an identityless multi-rule package and explicit unknown
  identities. Never substitute a random/first rule or a fabricated PASS.

## 5. Cases

Good: two code evaluators produce two distinct single-rule ZIPs and published ARNs;
the same identityless callback can correctly PASS one rule and FAIL the other.
Base: an existing single-code operation retains its keys, publication and cleanup.
Bad: removing an evaluator from a newer plan while reusing a legacy multi-rule Lambda
does not remove ambiguity from the deployed package.

## 6. Tests

`test_evaluator_lambda_isolation.py` covers published ZIPs and actual ARN binding,
identityless callbacks, independent retries/conflicts/cleanup, resource drift,
initial-revision recovery and historical shared artifacts. `test_evaluator_package_preflight.py`
covers review counts, run preflight and approved-rule substitution prevention.
Keep the existing evaluation-assets/revision-review tests and full verify gate green.

The live repair also exposed two decoder issues after disambiguation: mixed tool
input history hid valid model output logs, and the later-starting provider span
masked the output held by its direct Strands chat wrapper. `test_evaluator_output_wrappers.py`
covers precise tool-log classification and coalescing one wrapper/provider pair
using explicit same-trace parentage, source kinds and valid nested intervals.
Every finish reason and output copy remains checked; unrelated calls, truncation,
missing output and inconsistent evidence never become a PASS.

## 7. Wrong versus correct

Wrong: dispatch an identityless callback among multiple rule sets using a guessed name.
Correct: make the invocation target single-purpose by construction and retain strict
ambiguity rejection. Repair historical users with a replacement evaluator/plan rather
than overwriting a published version or rewriting old results.

## 8. Permission writes and cleanup snapshots

Qualified AddPermission may advance the published GetFunction RevisionId and
LastModified, as confirmed by a real us-east-1 before/after probe. Verify the
recorded published/latest identities and inventories before the write; use the
GetPolicy revision for CAS, never a function configuration revision. Only a successful
write followed by exact whole-policy verification and a revision-only published
configuration change, with LastModified allowed only inside the successful call
window plus five seconds of clock skew, can move the cleanup snapshot. Record both
timestamps alongside the revisions. Keep latest configuration,
concurrency and inventories exact and record the transition with the write request ID.
Do not bless legacy snapshots or lost-response drift by observing a matching SID.
The cleanup ownership checks stay strict. Cover the actual revision transition,
independent chains, retries, failed readbacks, CAS and external drift in tests.
