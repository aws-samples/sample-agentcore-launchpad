# Delete canary candidate packages from S3 on cleanup

Parent: `07-29-workshop-backlog`. ISSUE-011 (P2).

## Problem

`canary_infra.mint_candidate_version` uploads the built zip to
`agents/<agent>/canary/<uuid4>.zip` (`canary_infra.py:146-148`) and only **logs** the
key — it returns `(v_current, v_candidate)`, so the key is never recorded in the canary
artifacts. `canary_service.act_cleanup` (:781) therefore tears down the gateway,
targets, both endpoints, online-eval configs and the A/B test, reports eight `deleted`
categories, and leaves the S3 objects behind.

The helper is called twice per canary run — once to mint the candidate
(`canary_service.py:404`) and once on rollback to re-publish the current spec
(:706) — each with a fresh uuid key, so a rolled-back run orphans two ~37 MB objects
(~75 MB per run). Re-running mint after a failure adds more.

For contrast, the normal zip deploy path writes a **fixed** key
(`agents/<name>/deployment_package.zip`, `deployer/zip_runtime.py:349`), so it
overwrites instead of accumulating.

## Design decision — delete, but never the live version's object

Two candidate fixes were considered:

- *Lifecycle-managed prefix* (CDK rule expiring `agents/*/canary/`): no code deletes
  anything, but orphans linger for the lifecycle window and the fix is invisible in the
  cleanup report the operator reads.
- *Record the keys and delete them at cleanup*: matches how the platform already
  handles S3 for skill bundles (`registry_console._delete_keys` / `_delete_prefix`) and
  shows up in the cleanup list.

Chosen: **record + delete at cleanup**, with one safety rule — the object belonging to
the runtime version that is currently live must never be deleted. AgentCore's public
docs do not state whether a published version keeps reading its S3 artifact, so:

- rollback path: production runs the *restored* version → the candidate's object is
  deleted, the restored version's object is kept;
- promote path: production runs the *candidate* → its object is kept.

Deleting only non-live objects is safe under either assumption. Keys also become
deterministic (`agents/<agent>/canary/<canary_id>-<role>.zip`) so a retried mint
overwrites rather than accumulating, bounding an interrupted run's orphans to at most
two objects.

## Requirements

1. `mint_candidate_version` reports the S3 key it uploaded (return value, not just the
   log line), and takes the key/role from its caller so the key is deterministic per
   canary record + role (`candidate` / `restore`) instead of a fresh uuid.
2. The key is recorded in the canary artifacts (`setup.candidate_s3_key`,
   `rollback.restored_s3_key`) at the same point the version is recorded.
3. `act_cleanup` deletes the recorded keys, skipping the one whose runtime version is
   live at cleanup time, and appends one result row per key
   (`{"category": "s3:<key>", "status": "deleted"|"skipped", "detail": ...}`) so the
   cleanup report accounts for them. A delete failure is `skipped` with the reason and
   never fails the cleanup (existing cleanup convention).
4. Old canary rows without recorded keys clean up exactly as today — no crash, no
   guessing keys from S3 listings.
5. `mint_candidate_version` keeps its test seams (`uploader` / `pip_runner` /
   `build_root`) and still never mutates a ledger `Agent` row.

## Acceptance Criteria

- [ ] `mint_candidate_version` returns the uploaded key; unit test asserts the
      deterministic per-canary/role key shape and that the injected `uploader` received
      it.
- [ ] Canary setup artifacts carry `candidate_s3_key`; a rollback records
      `restored_s3_key` (unit tests with stubbed AWS).
- [ ] `act_cleanup` on a rolled-back canary deletes the candidate key, keeps the
      restored key, and reports both decisions in the cleanup list; on a promoted canary
      it keeps the candidate key.
- [ ] `act_cleanup` on a legacy row with no recorded keys behaves as before.
- [ ] Spec `.trellis/spec/launchpad/*` records the cleanup ownership change (S3 objects
      are now canary-owned, minus the live one).
- [ ] `make verify` passes. (Real-AWS confirmation is out of scope for the verify gate —
      note in the task what a live run should show.)

## Live-run note (out of verify scope)

`make verify` covers the unit level only. A real canary run should show, in the
cleanup list: `deleted s3:agents/<agent>/canary/<canary_id>-candidate.zip` after a
rollback, alongside `skipped s3:…-restore.zip · artifact of the live version <n>`
(and the mirror image after a promote). Confirm afterwards with
`aws s3 ls s3://<artifacts-bucket>/agents/<agent>/canary/` — at most one object per
canary should remain, the live one.
