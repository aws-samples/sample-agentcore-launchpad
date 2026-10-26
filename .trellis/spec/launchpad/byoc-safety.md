# BYOC artifact and permission safety

## 1. Scope / Trigger

Changes to BYOC upload packaging, deployment recovery, model selections, or the
shared Runtime response parser.

## 2. Signatures

- `ByocConfig.allowed_models`: optional list of 1–20 literal selections.
- `byoc_model_target(model_id) -> (resource_type, resource_id)`.
- `_package_code_zip` and `_package_container_source` own their temporary trees.
- `_stage_deploy` reconstructs a missing execution role through `_stage_provision`.

## 3. Contracts

- Names are workspace-local; temporary build paths must be private per attempt.
  Remove temporary trees only after the synchronous artifact consumer finishes.
- Successful provision does not persist `StageContext.scratch`. Recovery must
  reconcile the deterministic per-agent role instead of defaulting to shared IAM.
- BYOC accepts literal model IDs, foundation-model ARNs and derivable global/us/eu/
  apac system inference profiles. ARN selections keep their exact resource scope.
  Unknown IDs remain literal; only legacy non-BYOC methods keep permissive fallback.
- Answers alongside metadata or null errors remain visible. Actual native or
  Converse envelopes retain streaming, error handling and duplicate suppression.

## 4. Validation & Error Matrix

| Input/state | Result |
|---|---|
| Same name, two workspaces, overlapping packages | Each artifact contains its own source |
| Empty scratch after successful provision | Reconcile the per-agent role |
| Role reconciliation fails | Fail deploy before Runtime mutation |
| Explicit shared-role setting | Preserve shared-role behavior |
| Wildcard, IAM variable, unsupported ARN | Schema validation error |
| `{"answer":"hello","metadata":{},"error":null}` | Reply `hello` |
| Converse usage-only metadata event | No text |

## 5. Good / Base / Bad Cases

- Good: one full model ARN produces only that ARN in `BedrockModels`.
- Base: normal profile ID authorizes that profile and its derived model.
- Bad: an unrecognized selection authorizes `foundation-model/*`.

## 6. Tests Required

`test_byoc.py` interleaves both artifact kinds across workspaces and inspects
shipped bytes; it recreates contexts between provision and create/update for all
three artifact kinds. `test_agent_iam_policy.py` asserts complete resource lists.
`test_runtime_endpoints.py` exercises JSON/SSE replies through the shared invoke
path. Run the full repository verify gate before shipping.

## 7. Wrong vs Correct

- Wrong: `/tmp/launchpad_byoc_{agent.name}` plus deleting an existing directory.
  Correct: one `TemporaryDirectory` for the whole packaging attempt.
- Wrong: `scratch.get("execution_role_arn") or shared_role`.
  Correct: reconcile provision when scratch is missing; propagate IAM failures.
