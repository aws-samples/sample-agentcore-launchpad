# Design: ProbeScan Security Remediation

## Finding model

The CSV has 94 records in 12 rule families:

| Family | Count | Disposition |
|---|---:|---|
| Dependency advisories | 14 | Upgrade manifests/resolved locks and audit exact packages |
| `arbitrary-sleep` | 41 | Preserve intentional polling/E2E waits; annotate exact calls |
| `dangerous-subprocess-use-audit` | 21 | Verify argv provenance and `shell=False`; annotate exact calls |
| `dangerous-asyncio-create-exec-audit` | 5 | Verify exec API/argv provenance; annotate exact calls |
| `useless-inner-function` | 7 | Preserve decorator/thread callback use; annotate definitions |
| `B602` | 1 | Preserve non-executed exported fixture behavior; annotate fixture call |
| `detect-insecure-websocket` | 1 | Replace chained string substitution with URL-based scheme mapping |
| `generic-api-key` | 1 | Annotate the public OAuth provider name as non-secret |
| `insecure-document-method` | 1 | Replace HTML parsing with text/node construction |
| `missing-user` | 1 | Add an unprivileged runtime user to the ECS image |
| `useless-ternary` | 1 | Collapse identical branches |

The detailed file/line inventory and evidence live in
`research/probescan-findings.md`.

## Remediation boundaries

### Dependency ownership

The reported npm versions resolve in `apps/studio/package-lock.json`. Update
Studio's direct ranges only where required, then let npm regenerate the lock.
The Python report versions are pinned in
`apps/studio/backend/requirements.txt`; `python-multipart` also has a direct
range in `apps/studio/backend/pyproject.toml`. Keep the requirements and uv
project consistent and regenerate `uv.lock`.

The main backend already resolves versions newer than the two Python versions
in the CSV. A package-native audit may expose newer advisories, but unrelated
SDK or cryptography upgrades are outside this task unless a listed dependency
cannot be remediated without them. This avoids silently changing the preview
AgentCore SDK contract pinned by the project.

`vendor-src/` is an upstream mirror and is not the runtime/build input. Do not
edit it merely to make repository-wide text search clean.

### Subprocess findings

The reported platform and Studio calls use list argv and default
`shell=False`; `asyncio.create_subprocess_exec` has no shell interpretation.
User-controlled prompts, JSON, names, paths, and deployment values occupy
individual argv slots and cannot introduce additional commands. Executable
selection is internal/config-admin controlled, not request controlled.

For these audit rules, the correct closure is a narrow `nosemgrep` annotation
at the reviewed call, accompanied by a short reason where the trust boundary
is not obvious. Do not quote or escape list elements. Do not introduce a
generic subprocess wrapper solely to evade the scanner.

The only `shell=True` record is a verbatim Harness export fixture excluded from
runtime imports and loaded as text by conversion tests. Preserve its contents'
behavior and use Bandit's rule-specific `nosec B602` annotation.

### Intentional waits and callbacks

All reported sleeps are bounded polling intervals, retry backoff, startup
readiness waits, or real-AWS E2E pacing. Removing them changes API throttling
and lifecycle semantics. Mark the exact calls with `nosemgrep:
arbitrary-sleep`, retaining the surrounding deadline/status checks.

The inner functions are reached through FastAPI decorators, middleware
registration, a worker thread target, or a streaming callback. Keep those
framework-local closures and annotate only the false-positive rule.

### Actionable code changes

- WebSocket: parse the configured base with `URL`, map `http:` to `ws:` and
  `https:` to `wss:`, set `/ws`, and clear query/hash. This preserves local
  HTTP while guaranteeing WSS on secure origins.
- Mockup DOM: build the breadcrumb using `textContent` and a `<b>` node, then
  `replaceChildren`; no string is parsed as markup.
- ECS image: create a fixed unprivileged user/group, give it the working
  directory it may need, and set a final `USER` before the health check/CMD.
- Invoke label: replace the identical AgentCore ternary branches with the
  literal label.
- Secret detector: `launchpad-gw-m2m` is a public AWS resource name, not a
  credential. Preserve it and annotate the exact constant.

## Verification strategy

1. Reconcile the CSV counts and inspect the diff for one-to-one closure.
2. Verify exact resolved versions with structured package-lock/uv-lock parsing.
3. Run package-native audits for the named packages. Preserve and report any
   newer unrelated audit results.
4. Run focused tests/builds for backend and Studio, then the canonical
   `make verify`.
5. Build/inspect the ECS image when Docker is available.
6. With Browser plugin unavailable, use the repository Playwright CLI workflow
   against the running Studio surface for page identity, nonblank render,
   overlay/console checks, and one target interaction.
7. Rerun ProbeScan only if its invocation is discoverable. Otherwise document
   the missing scanner and run targeted static searches proving the actionable
   patterns and listed versions are gone.

## Rollback

Dependency changes can be reverted per manifest/lock pair. Source hardening and
audit annotations are independent and can be reverted by finding family.
Never roll back the generated lock without its owning manifest change.
