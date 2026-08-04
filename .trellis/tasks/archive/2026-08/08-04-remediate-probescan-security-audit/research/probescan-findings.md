# ProbeScan Findings Inventory

## Source and baseline

- Input: `ProbeScanExport-84897f5e-e6ea-469c-82fc-219154aba214-main-20260804.csv`
- Parsed records: 94
- Severity labels: 94 `ERROR`
- Git baseline: `041450b8225bb79a16d44aeba6f8c81ecfa36743`
- Initial worktree: only the CSV was untracked

## Rule inventory

| Rule | Count | Initial assessment |
|---|---:|---|
| `arbitrary-sleep` | 41 | Intentional AWS/service/E2E polling; audit false positives |
| `dangerous-subprocess-use-audit` | 21 | List argv, default `shell=False`; audit findings |
| Dependency advisory | 14 | Actionable manifest/lock upgrades |
| `useless-inner-function` | 7 | Decorator/thread/stream callback use; false positives |
| `dangerous-asyncio-create-exec-audit` | 5 | `create_subprocess_exec(*argv)`; no shell parser |
| `B602` | 1 | `shell=True` in non-executed exported-code fixture |
| `detect-insecure-websocket` | 1 | Secure mapping exists but brittle string replacement is actionable |
| `generic-api-key` | 1 | Public OAuth provider name, not a secret |
| `insecure-document-method` | 1 | Actionable DOM API hardening |
| `missing-user` | 1 | Actionable container hardening |
| `useless-ternary` | 1 | Actionable code cleanup |

Total: 94.

## Dependency findings

| Package/version | Records | Active source | Disposition |
|---|---:|---|---|
| `minimatch` 3.1.2 | 3 | `apps/studio/package-lock.json` | Regenerate past all three GHSA ranges |
| `minimatch` 9.0.5 | 3 | `apps/studio/package-lock.json` | Regenerate past all three GHSA ranges |
| `tar` 7.4.3 | 5 | `apps/studio/package-lock.json` | Regenerate past all five GHSA ranges |
| `rollup` 4.50.1 | 1 | `apps/studio/package-lock.json` | Regenerate past GHSA-mw96-cpmx-2vgc |
| `python-multipart` 0.0.20 | 1 | Studio `requirements.txt`; lower bound in `pyproject.toml` | Raise/pin to a fixed release and regenerate uv lock |
| `aiohttp` 3.10.11 | 1 | Studio `requirements.txt` | Raise/pin to a fixed release and regenerate uv lock |

The Studio uv lock already resolved `aiohttp` 3.12.15, while the stale
requirements file still pinned 3.10.11. The main backend resolved
`python-multipart` 0.0.32 and `aiohttp` 3.14.1 at research time, so the CSV's
two Python versions were not present there.

## Subprocess audit evidence

AST inspection found:

- 21 synchronous findings use list argv and omit `shell`, so Python's default
  `shell=False` applies.
- 5 async findings use `asyncio.create_subprocess_exec(*argv)`, which does not
  invoke a command shell.
- Dynamic values are passed as individual argv elements. `shlex.escape()` would
  incorrectly alter those elements and is not a mitigation for list argv.
- The sole reported `shell=True` call is
  `backend/tests/fixtures/harness_export_main.py:49`, a source-text fixture
  excluded from runtime imports and lint.

Reported synchronous sites:

- `apps/studio/backend/deployment/agentcore/agentcore_deployment_service.py`:
  216, 752, 839
- `apps/studio/backend/deployment/ecs-fargate/container_build_service.py`:
  509, 513, 517, 547, 573, 578, 583
- `apps/studio/backend/deployment/lambda/lambda_deployment_service.py`:
  380, 675, 708, 775
- `backend/app/services/conversation_service.py`: 173
- `backend/app/services/harness_convert.py`: 138
- `backend/app/services/skill_ingest.py`: 481, 784, 872
- `scripts/bootstrap.py`: 30
- `start.py`: 404

Reported async sites:

- `apps/studio/backend/deployment/ecs-fargate/container_build_service.py`:
  239, 388, 458
- `backend/app/codegen/validators.py`: 327
- `backend/app/services/local_exec.py`: 317

## Intentional wait sites

The 41 `arbitrary-sleep` records are bounded polling intervals, retry delays,
startup readiness waits, or real-AWS E2E pacing:

- Production/control plane:
  `backend/app/evaluation/agentcore_eval.py` (268, 535),
  `backend/app/services/bootstrap.py` (128),
  `gateway_bootstrap.py` (90, 246), `kb_gateway.py` (245),
  `knowledge.py` (526, 586), `model_prices.py` (238, 249),
  `observability.py` (144, 206), `policy_bootstrap.py` (49, 205, 254, 279),
  and `start.py` (179, 342).
- Real-AWS/operator scripts:
  `deploy_frontdesk_agent.py` (109), `e2e_chat_memory.py` (112),
  `e2e_claude_sdk.py` (74), `e2e_eval_extended.py` (65, 129),
  `e2e_eval_run.py` (40, 67), `e2e_experiment.py` (53, 73),
  `e2e_gateway_policy_management.py` (57), `e2e_gateway_tool.py` (54),
  `e2e_golden_path.py` (52, 180), `e2e_harness.py` (63),
  `e2e_knowledge_base.py` (52, 62, 81), `e2e_observability.py` (74),
  `e2e_registry.py` (39, 53), `e2e_runtime_canary.py` (152),
  `e2e_traces.py` (48), and `e2e_zip_runtime.py` (68).

## Other source findings

- Decorator/callback false positives:
  `backend/app/main.py` (90, 160), `backend/app/core/errors.py` (45, 52, 59),
  `backend/app/codegen/service.py` (316), and `apps/studio/backend/main.py`
  (497).
- `backend/app/services/gateway_bootstrap.py:108` is the stable public resource
  name `launchpad-gw-m2m`, not an API key or credential.
- `apps/studio/src/lib/api-client.ts:372` already selected WSS for HTTPS but
  used chained literal replacement; replace it with structured URL handling.
- `design/mockup.html:850` assigned a navigation label through `innerHTML`;
  the source is current static text, but node/text APIs remove the sink.
- `apps/studio/backend/deployment/ecs-fargate/Dockerfile` had no final `USER`.
- `apps/studio/src/components/invoke-panel.tsx:1399` returned the same label
  from both ternary branches.

## Live-audit context outside the CSV

On 2026-08-04, `npm audit` reported additional advisories in both active
frontends. The main backend Python audit also reported advisories newer than
the CSV, including one requiring an AgentCore SDK version outside the
project's documented 1.17 pin. These results must be reported separately;
silently expanding this task into a preview-SDK migration would violate the
current architecture contract and require separate compatibility/live-AWS
validation.

The full Studio Python audit could not complete because pip-audit attempted to
build `lxml` without system `libxml2`/`libxslt` development headers. This is an
audit-tool failure, not evidence of a clean dependency set.

## Implementation verification

- Exact reconciliation: 94 report records = 76 rule-level audited
  annotations + 18 actionable/dependency records.
- Report dependency closure:
  - npm resolves `minimatch` 3.1.5 and 10.2.6, `rollup` 4.62.4, and no
    `tar`; `npm audit` reports none of the three report packages.
  - Python resolves `python-multipart` 0.0.32 and `aiohttp` 3.14.3;
    targeted pip-audit reports no known vulnerability for either.
- Backend focused suite: 309 passed.
- Canonical gate: `make verify` passed twice; final run had 1,574 backend
  tests and 11 infra tests plus frontend lint/type-check/build and i18n parity.
- Studio: dependency sync, Python import/compile, TypeScript/Vite build, and
  Playwright interaction checks passed. Full lint retains an existing
  85-error baseline; the touched legacy files have the same error counts as
  `HEAD`, while new `websocket-url.ts` is clean.
- WebSocket helper assertions cover relative HTTP, explicit HTTP, explicit
  HTTPS, base paths, and query/hash removal. Browser evidence captured the live
  `ws://127.0.0.1:5273/ws` connection with no console errors or Vite overlay.
- Mockup script parses successfully and contains no `innerHTML`/`outerHTML`
  assignment.
- Bandit `-t B602` exits zero and confirms the fixture annotation is honored.
- Docker:
  - source Dockerfile's final user is `studio`;
  - production base lookup at public ECR was blocked by HTTP 403;
  - a temporary Docker Hub mirror build successfully executed the
    group/user/chown layer, then stopped because `generated_agent.py` is
    created only in the real deployment build context.
- The original ProbeScan invocation/config was not present in the repository,
  so no claim is made that the external scanner itself was rerun.
