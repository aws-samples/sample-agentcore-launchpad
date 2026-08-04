# Registry Skill Ingestion — Multi-Source Pipeline

## Scenario: adding/maintaining skill sources (inline / zip / git / url)

### 1. Scope / Trigger

Cross-layer contract between `frontend/src/pages/registry/*` and
`backend/app/routers/registry.py` + `backend/app/services/skill_ingest.py`.
Touch this spec whenever you add a skill source kind, change the staging flow,
or alter validation caps. Introduced by task `07-11-registry-skill-multi-source`
(commits 2b7f47f / 2d39ca3 / cbbb8e6).

**Load-bearing AWS facts** (verified 2026-07-11 against botocore model + docs):
- AGENT_SKILLS registry records carry ONLY inline descriptors:
  `skillMd.inlineContent` (required) and `skillDefinition.inlineContent`,
  each ≤ **102,400** bytes. No zip/git/S3 source exists AWS-side.
- `synchronizationType: URL` is MCP/A2A-only — **not available for skills**.
  All source diversity therefore lives in the Launchpad layer.
- **AWS parses the skillMd frontmatter and enforces `description` ≤ 1024
  chars** (hit live 2026-07-12: anthropics/skills `claude-api` has a 1068-char
  description → `ValidationException: agentSkills.skillMd 'description' must
  not exceed 1024 characters` AFTER S3 upload; pipeline cleanup removed all 66
  objects). Launchpad pre-validates this since task
  `07-12-skill-desc-1024-prevalidation`: `SKILL_DESCRIPTION_MAX_CHARS = 1024`
  checked in `bundle_errors` — fails at inspect (422 / invalid row) with zero
  S3 churn.
- Multi-file bundle bytes live at `s3://{artifacts_bucket}/skills/{name}/`.
  The deploy-time consumers (`bundle_skills_into` for Studio and
  `bundle_skill_paths_into` for generated zip/container artifacts) converge on
  the same whole-prefix downloader, so producer-side changes need no consumer
  edits.

> Extended by task `07-12-registry-subpage-register-edit` (commits 67d7348 /
> a2d6be2): register/edit are standalone `?view=` sub-pages and records are
> editable via `PUT /records/{id}` — see §8 below.

> Extended by task `07-12-agent-sdk-capabilities-fs`: the inspect staging now
> has a SECOND consumer — `POST /api/agent-skills/import` uploads selected
> staged bundles to `agent-skills/{uid8}/{name}/` **without creating a registry
> record** (create-wizard custom skill sources). It shares `_staging`,
> `_match_bundle` and `upload_bundle_files` (factored out of
> `register_skill_bundle`; keys append to the caller's list AS they land so a
> mid-batch failure leaves an exact cleanup list). Changing staging semantics
> or the upload loop now affects both consumers — see
> [container-capabilities-filesystem.md](./container-capabilities-filesystem.md).

### 2. Signatures

Backend service (`app/services/skill_ingest.py`) — every acquirer returns the
same `SkillBundle` (staging `TemporaryDirectory` + name/description/version/
files/skill_md/`SkillSource` provenance); callers MUST `close()` bundles:

```python
bundle_from_inline(skill_md: str) -> SkillBundle
bundle_from_zip(data: bytes) -> SkillBundle
bundles_from_git(url, ref=None, subdir=None, token=None) -> list[SkillBundle]  # monorepo → many
bundle_from_url(url: str) -> SkillBundle          # zip or raw SKILL.md by detection
bundle_from_source(source: dict) -> SkillBundle   # reimport re-acquire (git/url only)
```

Registry console (`app/services/registry_console.py`):

```python
register_skill_bundle(bundle, *, name_override=None, description_override=None) -> record
register_skill(name, description, skill_md) -> record   # thin inline wrapper
reimport_skill(record_id) -> record                     # git/url sources only
```

HTTP API:

| Endpoint | Method | Body |
|---|---|---|
| `/api/registry/skills/inspect` | POST | multipart `file` (.zip) **or** JSON `{"source":{"kind":"git"\|"url",...}}` (content-type dispatch) |
| `/api/registry/skills/import` | POST | `{staging_id, selections:[{index?, name?, name_override?, description_override?}]}` |
| `/api/registry/skills/capabilities` | GET | — |
| `/api/registry/skills/capabilities/git-install` | POST | empty (explicit user action; mutates the host) |
| `/api/registry/records/{id}/reimport` | POST | empty |

### 3. Contracts

`inspect` 200: `{staging_id, skills:[{index, name, description, version,
files[], skill_md_excerpt, source{kind,url,ref,subdir,imported_at}, valid,
errors[]}]}`. Staging is an **in-process** dict, TTL 10 min (single-process
uvicorn assumption — revisit if the deploy shape ever adds workers).

`import` 200 (always 200; per-item results): `{records:[{name, ok,
record?: <record_out>, error?: str, error_code?: str}]}`. Staging is dropped
**only when every item succeeded** — kept on failure so the user can rename
and retry without re-uploading.

`capabilities` 200: `{git:{available, version, fallback_hosts[],
install:{auto_installable, package_manager, hint}}}`.

`reimport` 200: updated `<record_out>` — `version` minor-bumped
(`1.0.0→1.1.0`), descriptor `source.imported_at` refreshed, registered `name`
preserved even if the upstream frontmatter name changed (S3 prefix + record
identity are keyed by name).

`skillDefinition.inlineContent` JSON (stored on the AWS record):
`{name, description, version, path: "s3://…/skills/{name}/", files: [real
list], source: {kind, url, ref, commit, subdir, imported_at}}`. Old records may
lack `files`/`source`/`source.commit` — readers must treat all three as optional.

**Git sources must resolve to a commit (T10).** A skill becomes deployable code,
so `ref` alone is not enough — it is usually a branch, which moves, and a record
then cannot say which revision a deployed agent carries. `source.commit` is the
full 40-hex SHA the files actually came from:

- clone path: read with `git rev-parse HEAD` **inside `_git_clone`**, which is the
  only place it exists — that function deletes `.git` on its way out;
- archive fallback (git CLI missing): **rejected** unless the caller passed a full
  commit SHA as `ref`. The endpoint serves `…/zip/<ref>` and the only place a SHA
  could surface is the wrapper directory name, which is `repo-<branch>` for a
  branch. The refusal names both remedies (install git via the `git-install`
  capability, or pass a SHA), and sits **after** the host check so an unsupported
  host still reports the more fundamental problem.

This deliberately narrows the documented git-missing fallback: that path cannot
produce an immutable source, which is the point of recording one.

Shared caps (single source of truth in `skill_ingest.py`; consumer imports
them): `SKILL_MD_MAX_BYTES=102_400`, `SKILL_BUNDLE_MAX_BYTES=50MB`,
`SKILL_FILE_COUNT_MAX=200`; repo-archive extraction uses repo-scale caps
(`_REPO_*`) because per-skill caps apply per bundle AFTER discovery.

### 4. Validation & Error Matrix

| Condition | Error |
|---|---|
| non-.zip upload / bad JSON / unknown source kind | 400 `registry.invalid_upload` / `registry.invalid_source` |
| upload > 50MB | 413 `registry.upload_too_large` |
| unsafe archive (traversal/symlink/zip-bomb), missing SKILL.md, SKILL.md > 100KB, descriptor JSON > 100KB, bad name | 422 `registry.skill_invalid` |
| unknown/expired staging_id | 410 `registry.staging_expired` |
| name already registered | per-item `registry.name_exists` (409-origin) |
| git missing + host not in fallback list | 503 `registry.git_unavailable` (+ `detail.install.hint`) |
| git missing + ref is absent or not a 40-hex commit SHA | 422 `registry.skill_invalid` ("must resolve to an immutable commit") |
| reimport on inline/zip source or DEPRECATED record | 400 `registry.not_reimportable` |
| non-https or non-public host (SSRF guard) | 422 `registry.skill_invalid` |

Envelope everywhere: `{code, message, detail}` (`app/core/errors.py`);
frontend reads `body.message`.

### 5. Good/Base/Bad Cases

- **Good**: zip with `SKILL.md + scripts/ + references/` → inspect preview →
  import → S3 prefix holds all files, `definition.files` lists them.
- **Base**: git monorepo (e.g. anthropics/skills, 18 skills) → one staging,
  multi-select import by `index`; git absent → github archive fallback yields
  identical discovery.
- **Bad**: zip whose entry is `../../etc/passwd` → 422, zero S3 writes; import
  retried after one item's 409 → staging still alive, only failed rows resent.

### 6. Tests Required

`backend/tests/test_skill_ingest.py` (zip safety: traversal/symlink/bomb/
unwrap), `test_skill_ingest_git.py` (local-repo clone fixture, token
redaction incl. URL-embedded creds, archive fallback incl. >200-file repo,
SSRF host rejection), `test_skill_ingest_url.py` (zip-vs-md detection, public
-address guard + redirect-hop guard), `test_registry_skill_ingest.py`
(S3 funnel, cleanup-on-failure, staging lifecycle), `test_registry_reimport.py`
(delete-before-upload ordering, version bump, name preservation, gating).
Assertion points: S3 keys uploaded/deleted, descriptor JSON contents,
staging survival semantics, error codes.

Commit pinning must also be asserted: a clone records the real `rev-parse HEAD`
(40 chars); two imports across an upstream commit record different SHAs; the
archive fallback accepts a SHA ref and **refuses** an absent or branch ref with a
message naming both remedies; and a stored `source` dict with no `commit` key still
constructs (old records must keep loading).

### 7. Wrong vs Correct

#### Wrong
```python
# reading the commit after the clone helper returns — .git is already gone:
_git_clone(url, ref, token, dst)
commit = _git_head_sha(dst)                     # always None
# per-skill caps applied to a whole-repo archive (live bug, fixed):
_extract_zip_safely(repo_archive, dst)          # 200-file cap kills monorepos
# rollback deleting fresh files under a live record on reimport failure:
_delete_keys(uploaded)                          # strands the record over an empty prefix
# binding caps at def-time breaks test monkeypatching:
def _extract_zip_safely(data, dest, max_files=SKILL_FILE_COUNT_MAX): ...
```

#### Correct
```python
_extract_zip_safely(repo_archive, dst, max_files=_REPO_FILE_COUNT_MAX,
                    max_bytes=_REPO_MAX_BYTES)  # per-skill caps enforced later per bundle
# reimport: delete old prefix BEFORE upload; keep fresh files on late failure
def _extract_zip_safely(data, dest, *, max_files=None, max_bytes=None):
    max_files = SKILL_FILE_COUNT_MAX if max_files is None else max_files
```

### Design Decisions

- **inspect→import two-step with server staging** (not one-shot): required for
  monorepo multi-select; zip/url reuse it so there is ONE interaction model.
- **git CLI subprocess over GitPython**; archive download is the *fallback*
  when git is missing (github/gitlab/gitee/bitbucket), not the primary path.
- **Tokens are transient**: injected into clone URL / Authorization header,
  redacted (`_redact`: token + `://user:pass@` userinfo) from every error/log,
  never persisted — so reimport of a private repo intentionally fails.
- **SSRF guard** (`_assert_public_url` + `_GuardingTransport`) applies to url
  fetch, archive fallback, AND git clone; every redirect hop re-checked.
  DNS-rebinding TOCTOU accepted for this lab deployment.

## 8. Record update (`PUT /api/registry/records/{record_id}`)

Frontend surfaces: `?view=register` (RegisterView.tsx, Evaluation-style
sub-page, replaces the old inline drawer) and `?view=edit&record=<id>`
(EditView.tsx; edit entry hidden for A2A / DEPRECATED). Registry.tsx
dispatches on `searchParams.get("view")`; enter with push (browser back →
list), leave with `{replace:true}`.

Request (all optional, ≥1 required):
`{description? (≤500), url? (MCP only), skill_md? (≤102400, AGENT_SKILLS
only), staging_id? + index?=0 (AGENT_SKILLS only)}` — `skill_md` and
`staging_id` mutually exclusive; `staging_id` comes from the SAME
`/skills/inspect` staging as import (consumed on success, kept on failure).

Branch semantics (`registry_console.update_record`):
- desc-only → resend existing descriptors unchanged, **no version bump**
- MCP url → `build_mcp_descriptors` rebuild, minor bump
- skill_md → overwrite ONLY `skills/{name}/SKILL.md`; definition keeps
  `files`+`source` from the old definition (legacy/missing/unparseable →
  safe fallbacks), version from new frontmatter (fallback old), rebuilt
  definition re-guarded ≤102,400B; minor bump
- staging bundle → full replace (paginated old-prefix delete → upload →
  fresh definition), **name always forced to the record's registered name**
  (name is immutable — S3 prefix/attachables/deploy dirs key on it); minor bump

Error additions to the §4 matrix: 400 `registry.nothing_to_update` /
`registry.field_conflict` / `registry.field_type_mismatch` /
`registry.not_editable` (DEPRECATED or A2A) / `registry.skill_not_staged`
(index out of range). 200 returns the refreshed `<record_out>`.

Gotcha: the LIST endpoint returns `descriptors: null` — only
`GET /records/{id}` carries descriptors; EditView must load via GET-by-id.
