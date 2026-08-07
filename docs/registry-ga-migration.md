# AWS Agent Registry GA migration

AWS Agent Registry moved from the `bedrock-agentcore` public-preview namespace
to the GA `agent-registry` namespace on 2026-08-06. Preview access ends on
2026-09-17. Existing registries and records are not migrated automatically.

Use the official AWS migration tool:

<https://github.com/awslabs/agentcore-samples/tree/main/01-features/07-centralize-and-govern-your-ai-infrastructure/03-registry/04-migrate-to-new-namespace>

## Launchpad migration sequence

1. Upgrade the backend to a boto3/botocore release that contains
   `agent-registry-control` and `agent-registry`.
2. Create an empty GA registry with the same discovery and approval
   configuration as the preview registry.
3. Run the tool's `check`, `extract`, and `load --dry-run` commands.
4. Resolve duplicate `(name, recordVersion)` identities before the live load.
   Launchpad permits the same display name across A2A, MCP, and Skill records;
   type-qualified versions such as `1.0.0-a2a` preserve that contract.
5. Stop Registry writers, then load the reviewed extract with `load --live`.
6. Compare record counts, type counts, approval states, and descriptor primary
   keys. Test the discoverable data plane as well as control-plane reads.
7. Update `resources.registry_id` / `registry_arn` in
   `config/launchpad.yaml`.
8. Apply the generated record-ID crosswalk to `agents.registry_record_id`.
   Clear ledger references whose preview records had already been deleted.
9. Start Launchpad and run Registry CRUD, approval, attachables, Overview, and
   front-desk discovery smoke tests.

The multi-file Skill payload remains in the existing artifacts S3 bucket. The
migration transforms Registry descriptors; it does not copy Skill bundle
objects.

## Rollback

Do not delete the preview registry during the migration window. To roll back,
deploy the preview-compatible Launchpad release and restore the preview
`registry_id` / `registry_arn` plus the pre-migration SQLite backup. Writes made
only in GA after cutover will not exist in preview.
