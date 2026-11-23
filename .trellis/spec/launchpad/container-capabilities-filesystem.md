# Claude Agent SDK (container) — Registry Capabilities + Filesystem Config

## Scenario: extending the container method's capabilities or runtime storage

### 1. Scope / Trigger

Cross-layer contract between `frontend/src/pages/CreateAgent.tsx` (container
branch), `backend/app/schemas/agent.py`, `backend/app/deployer/container.py`,
`backend/app/templates/claude_sdk_agent/`, `backend/app/routers/agent_skills.py`
and `backend/app/services/agentcore/runtime.py`. Touch this spec when you add a
capability source, change filesystem/VPC handling, or alter what gets baked into
the container image. Introduced by task `07-12-agent-sdk-capabilities-fs`.

**Load-bearing AWS facts** (verified 2026-07-12, botocore 1.43.44 + devguide
`runtime-filesystem-configurations`):
- `filesystemConfigurations` (Create/UpdateAgentRuntime) is a union list:
  `sessionStorage{mountPath}` | `s3FilesAccessPoint{accessPointArn,mountPath}` |
  `efsAccessPoint{accessPointArn,mountPath}`. Limits: ≤5 total, ≤1 session,
  ≤2 s3, ≤2 efs. Mount path `/mnt/<one-level>` (`/mnt/[a-zA-Z0-9._-]+/?`,
  6–200 chars, unique, non-nested).
- BYO (s3/efs) REQUIRES `networkMode: VPC` + `networkModeConfig{subnets,
  securityGroups}` + execution-role mount permissions + TCP 2049 egress.
  Session storage needs none of that.
- Session storage is per-session, survives stop/resume, **reset on every
  runtime version update (= every re-publish)**, 14-day idle expiry.
- System python's botocore (1.42.x) lacks the BYO members — always run backend
  code with `backend/.venv`.

**Live-verified 2026-07-13** (fs-verify-agent + minimal S3 Files env, task
`07-13-fs-policy-getaccesspoint-fix`):
- **The devguide's execution-role policy example is wrong AND incomplete.**
  AgentCore's create/update validation needs THREE statements (see §2/§6):
  `GetAccessPoint` doesn't carry the `s3files:AccessPointArn` condition key and
  authorizes on the **access-point ARN**; validation ALSO demands
  `s3files:ListMountTargets` on the file-system ARN (absent from the docs;
  error surfaces one missing action per attempt).
- **IAM propagation race is real**: Create/UpdateAgentRuntime rejects with
  "Execution role is missing required permissions" when called seconds after
  the provision stage (re)writes the inline policy with a changed AP ARN —
  `_retry_iam_propagation` in the deploy stage retries exactly that rejection
  (observed: 1 retry × 10s sufficed).
- **Access-point root ownership gotcha**: `posixUser` only sets the operation
  identity. The root directory itself must be writable by that uid —
  `rootDirectory.creationPermissions{ownerUid,ownerGid,permissions}` applies
  ONLY if the directory does not already exist at first mount. Putting any
  bucket object under the root prefix beforehand creates it root-owned →
  Permission denied on write. Point APs at a fresh prefix (bucket-side objects
  synced later are fine; they appear root-owned but world-readable).
- Observed runtime behavior: S3 Files mount shows as `127.0.0.1:/` NFSv4.2,
  8.0E capacity; session storage `127.0.0.1:/export` 1.0G — both mounts coexist
  in VPC mode. Bidirectional sync latency ≈30–60s each way (async;
  close-to-open consistency NFS-side, eventual bucket-side).
- S3 Files env prerequisites (setup script pattern in the 07-13 task journal):
  versioned SSE bucket + sync role trusting **`elasticfilesystem.amazonaws.com`**
  (S3 Files reuses the EFS principal) + mount target in a **private** subnet
  (AgentCore VPC ENIs get no public IPs — NAT required for Bedrock egress) in a
  supported AZ (us-west-2: usw2-az1/2/3) + mount-SG ingress 2049 from runtime-SG.

### 2. Signatures

```python
# schemas/agent.py
class FilesystemConfig:   # AgentSpec.filesystem (default: session ON @ /mnt/workspace)
    session_storage: SessionStorageFs | None   # JSON null = disabled
    s3_files: list[ByoMount]  # ≤2, ARN must contain :s3files: …/access-point/
    efs: list[ByoMount]       # ≤2, ARN must match elasticfilesystem access-point
    byo: bool                 # property
AgentSpec.network: VpcNetwork | None  # REQUIRED when filesystem.byo (model_validator)

# deployer/container.py (pure, unit-tested)
_filesystem_configurations(spec) -> list[dict]  # AWS union shapes
_vpc(spec) -> dict | None                       # only when byo AND network set
_build_context(spec, agent, log) -> Path        # assemble + bundle spec.skills into .claude/skills/

# services/agent_iam.py — MOVED here 2026-08-03 with per-agent roles (T3)
fs_policy_document(spec) -> dict | None
#   s3_files → THREE statements (live-verified 2026-07-13):
#     ClientMount+ClientWrite  · Resource=FS-arns · Condition ArnEquals AccessPointArn
#     GetAccessPoint           · Resource=AP-arns · NO condition (key unsupported)
#     ListMountTargets         · Resource=FS-arns (undocumented AgentCore requirement)
#   efs → ClientMount+ClientWrite · Resource "*" + AP condition (unverified live)
# DO NOT re-derive these shapes. The AWS devguide example is wrong and incomplete;
# this shape came from IAM simulator + live UpdateAgentRuntime probes. A
# characterisation test in tests/test_agent_iam_policy.py pins it byte-for-byte.
provision_execution_role(agent, spec, settings, log, iam=None) -> (arn, detail)
delete_execution_role(agent, settings, log, iam=None) -> bool   # never raises
retry_iam_propagation(fn, log, attempts=6, delay_s=10, sleeper)  # deploy-stage guard;
#   predicate covers assume-role / AccessDenied wording too — a brand-new role is a
#   longer consistency window than a rewritten inline policy

# deployer/zip_runtime.py
bundle_skill_paths_into(paths, dest_parent, log, *, s3_client=None)  # explicit s3 prefixes

# services/agentcore/runtime.py — create/update_container_runtime new kwargs
filesystem_configurations: list[dict] | None, vpc: dict | None  # vpc→networkMode VPC

# templates/claude_sdk_agent/__init__.py
_mcp_servers(spec)  # env LAUNCHPAD_MCP_SERVERS JSON ∪ spec.tools mcp refs (registry wins)
# ALLOWED_TOOLS = ["Task"] + ["Skill" if spec.skills] + ["mcp__{k}" per merged server]
```

HTTP: `POST /api/agent-skills/import {staging_id, selections:[{index?,name?,
name_override?}]}` → `{skills:[{name, ok, path?, description?, error?,
error_code?}]}`. Consumes the registry's inspect staging
(`/api/registry/skills/inspect`) but uploads to
`s3://{artifacts_bucket}/agent-skills/{uid8}/{name}/` and creates **no registry
record**. Staging drops only when every selection succeeded (frontend batches
its picks in ONE call — per-item calls would drop staging after the first).

### 3. Contracts

- Container capabilities: `spec.tools` mcp refs (`{type:"mcp",name,config.url}`)
  → merged into rendered `MCP_SERVERS` as `{name:{type:"http",url}}`; registry
  entries override same-named free-text JSON keys. Gateway targets are
  harness-only (no auth wiring in the SDK template).
- `spec.skills` (both registry `skills/{name}/` and custom
  `agent-skills/{uid}/{name}/` prefixes) are downloaded at generate-time into
  the build context `.claude/skills/{name}/` — the claude CLI discovers them
  under `/app/.claude` (HOME=/app; the scaffold ships no baked-in subagents
  since 2026-07-13 — the fact-checker sample was dropped as not SDK-native).
  Skill failures log + skip, never sink a deploy.
- Deploy passes `filesystemConfigurations` + `vpc` on BOTH create and update;
  network flips PUBLIC→VPC exactly when BYO mounts exist. Old specs (no
  `filesystem` key) default to session-ON — safe because the version bump resets
  session storage anyway.
- IAM: inline policy `launchpad-fs-{agent.name}` on **the agent's own execution
  role** (`launchpad-agent-{name}-{id8}`), not the shared one — accumulating
  per-agent mount grants on a principal every agent assumes is the T3 problem this
  moved away from. Attached when BYO, deleted when mounts are removed on re-publish,
  and removed wholesale with the role when the agent is deleted. The role must be
  deleted **after** the runtime; the reverse order can wedge the runtime's own
  deletion. On the `per_agent_execution_roles=false` fallback the old shared-role
  cleanup still runs, or a stale policy would pile up on the shared principal.
- Frontend `buildSpec()` sends `filesystem`/`network` for container only;
  harness payload unchanged. Edit reload derives custom-chip names from the
  path tail (`/agent-skills/` marker).

### 4. Validation & Error Matrix

| Condition | Error |
|---|---|
| bad mount path / dup paths / >2 rows / wrong-service ARN | 422 `validation.invalid_request` (pydantic) |
| BYO mounts with `network: null` | 422 (`AgentSpec` model_validator) |
| attach with unknown/expired staging | 410 `registry.staging_expired` |
| attach selection not staged | per-item `registry.skill_not_staged` |
| bad name override / invalid bundle | per-item `registry.skill_invalid` |
| S3 failure mid-upload | per-item `agents.skill_attach_failed`, partial keys deleted |

### 5. Tests Required

`test_agent_spec_filesystem.py` (validators), `test_runtime_container_fs.py`
(param shapes create+update), `test_container_provision_iam.py` (policy doc +
lifecycle), `test_container_skill_bundle.py` (S3→.claude/skills),
`test_claude_sdk_template.py` (MCP merge/ALLOWED_TOOLS),
`test_agent_skills_attach.py` (staging→S3, no record, cleanup),
`test_agents_api.py::test_create_container_agent_*` (API round-trip).
Browser evidence: `frontend/scripts/sdk_caps_fs_evidence.mjs` →
`design/screenshots/agent-sdk-caps-fs/`.

### 6. Wrong vs Correct

```python
# WRONG: requiring network for every container spec (breaks old agents + session-only)
if spec.method == "container" and spec.network is None: raise ...
# CORRECT: gate on BYO only
if self.filesystem.byo and self.network is None: raise ...

# WRONG: deriving the EFS file-system ARN from the access-point ARN (not encoded there)
resource = efs_ap_arn.split("/access-point/")[0]
# CORRECT: Resource "*" scoped by the ArnEquals AccessPointArn condition

# WRONG: copying the devguide's single combined s3files statement (live-hit 2026-07-13):
{"Action": ["s3files:ClientMount","s3files:ClientWrite","s3files:GetAccessPoint"],
 "Resource": fs_arn, "Condition": {"ArnEquals": {"s3files:AccessPointArn": ap_arns}}}
# GetAccessPoint never matches (wrong resource type + unsupported condition key)
# → UpdateAgentRuntime: "Ensure the role has s3files:GetAccessPoint";
# and even the fixed shape then hits "…ListMountTargets".
# CORRECT: three statements — see _fs_policy_document in §2.

# WRONG: calling Create/UpdateAgentRuntime immediately after put_role_policy changed
# the policy content — IAM propagation window → "missing required permissions".
# CORRECT: wrap the call in _retry_iam_propagation (targeted retry, 10s steps).

# WRONG (ops): seeding bucket objects under an AP's rootDirectory prefix BEFORE the
# first mount — the sync creates the directory root-owned and creationPermissions
# never applies → agent (uid 1001) gets Permission denied on write.
# CORRECT: point the AP at a not-yet-existing prefix; seed the bucket afterwards.

# WRONG (frontend): attaching monorepo picks one-per-request
for i of picked: api.attachSkillSources(sid, [{index: i}])   # 1st success drops staging → 410
# CORRECT: one batched call
api.attachSkillSources(sid, picked.map(index => ({index})))
```

## Scenario: `s3BucketMount` entries (S3 bucket mounts, all Runtime methods)

### 1. Scope / Trigger
- Trigger: AgentCore Runtime V2 mounts an S3 bucket prefix itself (Mountpoint for S3) —
  a new `filesystemConfigurations` union member that is customer storage but **not NFS**.
  Any code that classifies entries, merges them on update, guards invokes, or maps invoke
  errors must use the helpers below. Probe: `backend/scripts/probe_s3_bucket_mount.py`
  (live 2026-11-23, us-west-2; results in task `11-23-s3mount-p0-compat-probe/research/`).

### 2. Signatures
```python
# app/services/agentcore/filesystem_kinds.py  (leaf; re-exported by deployer/filesystem.py)
NFS_KINDS = {"s3FilesAccessPoint", "efsAccessPoint"}            # need networkMode VPC
BYO_KINDS = NFS_KINDS | {"s3BucketMount"}
LIVE_KIND = {"s3FilesAccessPoint": "s3files", "efsAccessPoint": "efs",
             "s3BucketMount": "s3bucket"}
def member_kind(entry) -> str            # union key, '' if none
def needs_vpc(entry) -> bool             # member_kind in NFS_KINDS
def per_session_prefix(entry) -> bool    # s3BucketMount with '{runtimeSessionId}' in prefix
# app/services/invoke_session.py
SESSION_ID_MOUNT_RE = r"^[A-Za-z0-9][A-Za-z0-9_-]{32,255}$"
def assert_session_id_compatible(agent, session_id) -> None   # 400 invoke.session_id_incompatible
# app/services/mount_failure.py
def mount_failure(exc) -> AppError | None                     # 424 agent.mount_failed
```

### 3. Contracts
- Wire shape (botocore ≥ 1.44.6; `backend/pyproject.toml` floor `boto3>=1.44.6`):
  `{"s3BucketMount": {"bucket", "mountPath", "prefix"?, "s3BucketOwner"?, "s3BucketRegion"?}}`.
- Requires `platformVersion: "V2"`; works on PUBLIC and VPC; same echo-on-update rule as
  every member (UpdateAgentRuntime without the list clears it — probe P6).
- `live_mounts` entry: `{kind: "s3bucket", mount_path, bucket, prefix, bucket_owner,
  bucket_region, access_point_arn: None, origin, storage_id, storage_name}`; members it does
  not model (e.g. `capacityProviderVolume`) are skipped.
- Per-session IAM shape (probe P4): ListBucket `s3:prefix` [`<P>${aws:AgentCoreRuntimeSourceSid}/*`,
  `<P>${aws:AgentCoreRuntimeSourceSid}`]; objects `<P>${aws:AgentCoreRuntimeSourceSid}/*`.
  Bucket-policy confinement (probe P4b): Deny `Principal *`, `NotResource …/${sid}/*`,
  condition `Null aws:AgentCoreRuntimeSourceSid = false` — requests without the key (the
  backend) are unaffected.

### 4. Validation & Error Matrix
| Condition | Result |
|---|---|
| s3BucketMount on PUBLIC | allowed (create guard, re-publish merge, canary candidate) |
| S3 Files / EFS on PUBLIC | refused before AWS (create) / detached + logged (re-publish) |
| V1 runtime + s3BucketMount | AWS 400 at create: "s3BucketMount can only be used when platformVersion is V2." |
| Harness + s3BucketMount | CreateHarness OK, backing runtime CREATE_FAILED ("Each filesystemConfigurations entry must specify exactly one of …") → **unsupported**, refuse |
| spec has per-session mount + caller id outside `SESSION_ID_MOUNT_RE` | 400 `invoke.session_id_incompatible` (before AWS; AWS would answer ValidationException) |
| invoke error text contains `S3 mount failed:` | 424 `agent.mount_failed`, `detail.reason` = AWS phrase, not retryable |
| HTTP 424 without that text | unchanged handling — it is the generic `RuntimeClientError` of any failing container |
| boundary/role lacks S3 actions | 424 "S3 mount failed: access denied" (mount uses the role session — boundaries apply) |
| missing bucket | 424 "S3 mount failed: the S3 bucket was not found" |

### 5. Good / Base / Bad Cases
- Good: PUBLIC V2 zip runtime, `prefix: "sessions/{runtimeSessionId}/"`, role scoped with SourceSid.
- Base: externally added s3BucketMount on a platform agent → kept on every re-publish, shown as `external`.
- Bad: classifying with `"sessionStorage" not in fs` (treats S3 buckets as NFS); mapping
  every 424 to a mount failure; appending/renaming inside the mount (EPERM / ENOSYS;
  in-place edit EBADF — write whole files).

### 6. Tests Required
- `tests/test_filesystem_kinds.py`: botocore model has `s3BucketMount`; helpers; PUBLIC create
  passes for bucket / refused for NFS; PUBLIC re-publish keeps bucket, detaches NFS with the
  right log; canary candidate keeps bucket; harness update keeps external bucket; session-id
  guard (fires only with a per-session spec mount, never on 64-hex ids) through
  `invoke_agent_text`, `invoke_agent_events`, `chat_stream`; mount_failure text-only match
  (a bare 424 `RuntimeClientError` → None).
- `tests/test_live_mounts.py`: s3files / efs / s3bucket classification, session storage skipped.
- `frontend/src/v3/pages/storage/storage.test.tsx`: StorageCard bucket row.

### 7. Wrong vs Correct
```python
# WRONG: every non-session entry is "BYO needing a VPC"
if any("sessionStorage" not in fs for fs in entries) and not vpc: raise ...
# CORRECT: only NFS members need a VPC
if any(needs_vpc(fs) for fs in entries) and not vpc: raise ...

# WRONG: status-based mount detection — relabels every agent crash
if status == 424: raise AppError("agent.mount_failed", ...)
# CORRECT: match AWS's phrase
if re.search(r"S3 mount failed:?\s*(.*)", text): raise AppError("agent.mount_failed", ...)
```

## Scenario: the `s3bucket` storage catalog kind (P1)

### 1. Scope / Trigger
- Trigger: any change to how an administrator-registered S3 bucket prefix becomes a
  `s3BucketMount`: register/check, spec stamp, mapping, IAM, boundary, preflight, promotion.
  Code: `services/storage_mounts.py`, `models/storage.py`, `deployer/filesystem.py`,
  `deployer/mounts.py`, `services/project_iam.py`, `infra/spoke/*.yaml` (template v3).

### 2. Signatures
```python
# models/storage.StorageMount (additive columns; `access_point_arn` stays NOT NULL -> "")
kind="s3bucket"; bucket; prefix ("" | "x/y/"); scope ("per_session"|"shared");
bucket_owner; bucket_region; encryption; kms_key_arn
# services/agentcore/filesystem_kinds.py
mounted_prefix(entry_prefix, scope, agent_id) -> str   # per_session: "<P><agent_id>/{runtimeSessionId}/"
iam_prefix(prefix) -> str                              # token -> ${aws:AgentCoreRuntimeSourceSid}
# services/storage_mounts.py
register(...)                      # kind s3bucket | access point; validate_bucket_fields
normalize_spec_storage(db, ws, project, spec, *, creating=False)  # stamp + assert_bucket_platform
filesystem_entries(spec, agent_id) # ValueError for a per-session mount without agent_id
storage_iam_statements(spec, resources=None, agent_id=None, partition="aws")
bucket_iam_statements(grants, partition) / bucket_policy_preview(db, row)
boundary_statements(inp)           # project_iam.EXTRA_BOUNDARY_STATEMENT_BUILDERS
# deployer/filesystem.py
filesystem_configurations(spec, agent_id=None); runtime_update_mounts(..., agent_id=None)
# schemas/agent.py
BUCKET_MOUNT_UNSUPPORTED = {"harness": "..."}   # data-driven refusal; delete a row to lift it
FilesystemConfig.needs_vpc (NFS only) / bucket_mounts(); MAX_FILESYSTEM_ENTRIES = 5
```

### 3. Contracts
- Layout is platform-forced (D1.2): admins set only the entry prefix; `per_session` mounts
  `<entry prefix><agent_id>/{runtimeSessionId}/`. The agent id is a REQUIRED input of every
  mapping/IAM call: thread it, never default it. Catalog prefixes carry no `* ? { } $` (they
  become IAM `s3:prefix` / resource patterns).
- IAM shape (probe P4): ListBucket `s3:prefix` [`<P>*`, `<P>` without slash] on the bucket
  (no condition for a whole-bucket shared mount), object actions on `<bucket>/<P>*`
  (`GetObject`; rw adds Put/AbortMultipartUpload/Delete), KMS Decrypt (+GenerateDataKey rw)
  on the key the check recorded. Golden tests pin it in `tests/test_storage_s3bucket.py`.
- Isolated projects: the boundary needs its own S3 statements (probe P9); the S3 Files/EFS
  ones already come from the `WorkspaceWide` statement. `test_project_iam.py::
  test_s3_bucket_mount_grants_survive_the_boundary` proves identity grants fit.
- Spoke template v3 (`S3CatalogRead`: HeadBucket/Location/Encryption/BPA/Versioning on
  `arn:*:s3:::*`) is what the check needs in a multi-account workspace.

### 4. Validation & Error Matrix
| Condition | Result |
|---|---|
| bad bucket / prefix / owner / Region, directory bucket | 422 `storage.invalid_bucket` |
| Harness + s3bucket entry | 422 `storage.method_unsupported` |
| Runtime without explicit V2 (existing agent) or V1 | 422 `storage.requires_v2` (new agents default V2) |
| role policy over the inline cap | 422 `storage.policy_too_large` |
| target workspace's entry is another kind (promotion) | 409/unmapped `storage.unmapped` |
| bucket missing / unreachable / wrong Region (check) | block `s3.bucket_missing` / `s3.bucket_access` / `s3.region_mismatch` |

### 5. Wrong vs Correct
```python
# WRONG: render the mount without its agent -> a shared prefix for every agent's sessions
filesystem_entries(spec)            # raises ValueError for per_session now
# CORRECT
filesystem_configurations(spec, row.id)

# WRONG: key access-point lookups on rows of every kind (s3bucket rows store "")
by_arn = {r.access_point_arn: r for r in rows}
# CORRECT: skip kind == "s3bucket"; match a live bucket entry by (bucket, mounted prefix)
```

## Scenario: session output files (`filesystem.outputs`, P2)

### 1. Scope / Trigger
- Trigger: any change to the per-session output folder — the spec field, its stamp, the
  agent-files bucket (CDK + spoke bootstrap), the mapping/IAM/boundary, the files API,
  the delete hooks, or the chat Files tab. Code: `services/agent_files.py`,
  `services/storage_mounts.py` (`normalize_outputs`, `restamp_outputs`,
  `storage_iam_statements`), `deployer/filesystem.outputs_entry`, `deployer/mounts.py`,
  `services/agentcore/filesystem_kinds.py` (`outputs_*`), `services/project_iam.py`,
  `routers/chat.py`, `infra/stacks/base_stack.py`, `services/workspace_bootstrap.py`.

### 2. Signatures
```python
# schemas/agent.py
class OutputsFs: mount_path="/mnt/outputs"; bucket; project_key   # last two server-stamped
FilesystemConfig.outputs: OutputsFs | None = None                 # counts as 1 of the 5 entries
# services/agentcore/filesystem_kinds.py  (the ONE prefix formula)
outputs_bucket_name(account, region) -> "launchpad-agent-files-<a>-<r>"
outputs_agent_prefix(project_key, agent_id) -> "projects/<project|shared>/agents/<agent>/"
outputs_prefix(project_key, agent_id, session=SESSION_TOKEN) -> ".../sessions/<session>/"
# services/storage_mounts.py
normalize_outputs(spec, resources, project_id, *, creating) -> spec   # stamp + V2/method/bucket gate
restamp_outputs(spec, resources, project_id) -> bool                  # promotion; False = target has no bucket
# services/agent_files.py
bucket_policy(bucket, partition) / lifecycle_configuration()          # spoke bootstrap applies; CDK twin
list_files / presign / clean_relative_path / delete_prefix / delete_agent_files
```

> **Gotcha — presign host (live 2026-11-23):** `presign` must use
> `Config(signature_version="s3v4", s3={"addressing_style": "virtual"})`. The default client
> signs SigV2 on the global host (S3 answers `307`); `signature_version="s3v4"` alone still
> signs `<bucket>.s3.amazonaws.com`, which S3 rejects with `400 AuthorizationQueryParametersError`
> ("the region 'us-west-2' is wrong; expecting 'us-east-1'"). Only `addressing_style="virtual"`
> yields `<bucket>.s3.<region>.amazonaws.com`. Unit test: `test_presign_asks_for_a_sigv4_regional_host_client`.
HTTP: `GET /api/chat/{agent_id}/sessions/{session_id}/files`,
`GET …/files/download?path=` (both `MEMBER` in `ROUTE_POLICY`; the service checks owner/admin).

### 3. Contracts
- Wire: `s3BucketMount {bucket, mountPath, prefix: projects/<project|shared>/agents/<agent_id>/sessions/{runtimeSessionId}/}`,
  always read-write, `{runtimeSessionId}` substituted by AgentCore (probe P1/P4). IAM is the
  catalog builder (`bucket_iam_statements`) with that prefix — never a second shape.
- The stamp is server-side: whatever a client sends in `bucket` / `project_key` is replaced. A re-publish
  files the prefix under the agent's own project (`agent.project_id`), a create under the request's.
  `outputs: {}` means "on, default path" — test `is None`, never truthiness.
- Bucket policy = TLS-only + the probe-verified (P4b) confinement: object-action Deny with
  `NotResource …/projects/*/agents/*/sessions/${aws:AgentCoreRuntimeSourceSid}/*` AND a ListBucket Deny with
  `StringNotLike s3:prefix`, both `Null aws:AgentCoreRuntimeSourceSid=false`. Unversioned, 30-day expiry,
  CDK `RETAIN`. CDK and `agent_files.bucket_policy` are twins: change both (tests pin each).
- Boundary: `BoundaryInputs.agent_files_bucket` is set only when a project agent has the folder
  (`_Needs.outputs`); it allows `projects/<pid>/*` of that bucket.
- Files API authorization order: agent visibility (`_agent_in`) → ledger `ChatSession` row of
  (workspace, agent, session) → `actor_id == caller` (or `river` with auth off) or admin → else
  `404 chat.session_not_found`. The prefix comes from the agent row + session id, never the client.
- Delete hooks (`_delete_agent_resources`, conversation purge, deferred sweep) call
  `agent_files.delete_agent_files`: only `projects/<project|shared>/agents/<agent_id>/` (project from the
  agent row and its stamped spec), best-effort, never raises. A workspace purge touches no AWS.

### 4. Validation & Error Matrix
| Condition | Result |
|---|---|
| Harness + outputs | `AgentSpec` validation error (same `BUCKET_MOUNT_UNSUPPORTED` row as catalog buckets) |
| Runtime not on V2 (or existing agent without explicit V2) | 422 `storage.requires_v2` |
| Workspace has no `agent_files_bucket` | 422 `storage.agent_files_unavailable` (promotion: unmapped, same code) |
| Caller is neither the session's user nor admin / unknown session / other agent | 404 `chat.session_not_found` |
| `path` empty, absolute, `.`/`..`/empty segment, backslash, control char, > 1024 chars | 422 `chat.invalid_file_path` |
| Agent without the folder: list / download | `{enabled:false, files:[]}` / 404 `chat.files_unavailable` |

### 5. Tests Required
`backend/tests/test_agent_files.py` (stamp, gates, mapping, IAM golden, boundary, policy + lifecycle golden,
manifest/prompt, promotion, list/presign/path matrix, owner/admin/other-member API, delete prefix/paging/never-raises),
`test_workspace_bootstrap.py` (storage stage: bucket posture, idempotent), `infra/tests/test_stack.py`
(bucket + policy twin), `infra/tests/test_spoke_template.py` (`s3:PutLifecycleConfiguration`),
`frontend/src/v3/pages/chat/files.test.tsx`, `inspect.test.ts`, `storage.test.tsx` (StorageCard row).

### 6. Wrong vs Correct
```python
# WRONG: a delete prefix assembled from a loose id (agent "abc" would also match "abcd…")
prefix = f"projects/{p}/agents/{agent_id}"
# CORRECT: the trailing slash is part of the formula
prefix = outputs_agent_prefix(p, agent_id)   # ".../agents/<id>/"

# WRONG: if not outputs: return spec      ({} = on with defaults is falsy)
# CORRECT: if outputs is None: return spec
```
