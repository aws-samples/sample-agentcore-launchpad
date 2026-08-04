# Add Registry Skills to zip runtimes

## Problem

The Create Agent wizard exposes APPROVED Registry `AGENT_SKILLS` records to
managed Harness and container agents, but excludes `method="zip_runtime"` from
both the picker and the submitted `AgentSpec.skills` field.

The zip deployer has a second exclusion: it bundles skills only when Studio
generated code contains an `AgentSkills(...)` reference. The platform-generated
HTTP and A2A zip templates neither receive selected skill paths nor initialize
the Strands `AgentSkills` plugin. A user can therefore create a zip runtime, but
cannot explicitly attach and use a Registry Skill through the normal wizard.

## Goal

Allow platform-generated zip runtimes to select APPROVED Registry Skills in the
Create Agent wizard, package the selected multi-file bundles into the runtime
artifact, and expose them to the Strands agent through progressive disclosure.

## Requirements

### R1 - Create and edit experience

- The existing Registry/custom Skill picker must be available for
  `method="zip_runtime"` as well as Harness and container methods.
- Selected skill S3 prefixes must be persisted in `AgentSpec.skills` for zip
  runtimes and restored when editing or re-publishing an agent.
- The existing Registry contract remains unchanged: only APPROVED
  `AGENT_SKILLS` records appear in the catalog.
- Mounted Registry Skills remain distinct from A2A AgentCard skills:
  `AgentSpec.skills` controls runtime instruction bundles, while
  `AgentSpec.a2a_skills` controls advertised routing metadata.

### R2 - Zip artifact and runtime behavior

- For a platform-generated zip runtime, every selected `spec.skills` prefix
  must be copied into the artifact under `skills/<skill-name>/`, preserving
  nested files.
- Both generated zip protocols are supported:
  - HTTP template (`BedrockAgentCoreApp`)
  - A2A template (`A2AServer`)
- The generated Strands agent must register `AgentSkills` only when packaged
  skill content is present. A skill download that is skipped under the existing
  log-and-continue policy must not make the runtime fail at import time.
- Skill use follows Strands progressive disclosure: lightweight metadata is
  visible to the model and full `SKILL.md` instructions/resources are loaded
  only when the skill is activated.
- No additional Python dependency is required solely for Skills; the current
  Strands 1.x dependency already exports `AgentSkills`.

### R3 - Compatibility boundaries

- Studio remains code-driven: generated `AgentSkills(...)` references continue
  to determine which APPROVED Registry bundles are packaged.
- Harness-to-runtime conversions remain bundle-driven: a zip runtime with
  `code_bundle` keeps its exported runtime-side Skill fetcher and must not also
  receive the new platform-template snapshot behavior.
- Harness and container Skill behavior must remain unchanged.
- Invocation, chat, Registry, and public `/v1` routes must not query Registry on
  each request; discovery stays create-time and bundling stays deploy-time.
- Existing per-agent IAM behavior remains the permission source:
  `spec.skills` grants access only to that agent's selected artifact prefixes.
- Reimporting or editing a Registry Skill does not hot-update an existing zip
  runtime. The agent must be re-published to package the new snapshot.

### R4 - Verification and documentation

- Add hermetic backend coverage for HTTP/A2A template rendering, explicit
  `spec.skills` bundling, nested files, skipped downloads, and the converted
  `code_bundle` compatibility boundary.
- Verify the frontend payload and edit/re-publish state through browser
  automation.
- Update the English architecture/spec documentation that describes
  method-specific Skill mounting and Harness conversion semantics.
- Run the canonical `make verify` gate.
- Validate through the real Portal/AWS workflow in `us-west-2`: create,
  deploy, and invoke HTTP and A2A zip runtimes with a distinctive APPROVED
  Registry Skill, then verify the response demonstrates that Skill's behavior.

## Acceptance Criteria

- [x] The Create Agent wizard shows the existing Skill catalog/custom-source
      controls for `zip_runtime`; selected paths appear in the create/redeploy
      payload and survive edit rehydration.
- [x] An HTTP zip artifact contains every selected bundle at
      `skills/<name>/**`, and its rendered agent initializes `AgentSkills`
      against the packaged Skill root.
- [x] An A2A zip artifact has the same mounted-Skill behavior while preserving
      `a2a_skills` exclusively as AgentCard metadata.
- [x] A zip runtime without selected Skills keeps its current behavior and does
      not initialize an empty/broken Skill plugin.
- [x] Studio Skill packaging remains code-reference-driven.
- [x] A converted Harness `code_bundle` with `spec.skills` is not re-bundled;
      its exported fetcher and IAM-only `spec.skills` semantics remain intact.
- [x] Per-agent IAM tests continue to prove selected Skill prefixes are scoped
      to the owning runtime.
- [x] `make verify` passes.
- [x] Portal/AWS evidence proves one HTTP and one A2A zip runtime reach ACTIVE
      and invoke the attached Skill successfully.
- [x] Documentation states that zip/container/Studio Skill content is a
      deployment snapshot and requires re-publish after Registry updates.

## Non-goals

- Runtime Registry search or automatic hot reload.
- Changing Registry ingestion, approval, storage, or descriptor formats.
- Changing the Harness native Skill source contract.
- Replacing Studio's generated-code reference extraction.
- Changing A2A AgentCard discovery or deriving card metadata from mounted
  `AGENT_SKILLS`.
