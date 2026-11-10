#!/usr/bin/env bash
# Canonical verify gate for AgentCore Launchpad.
# Each section runs only if its directory exists; any failure exits non-zero.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAIL=0

section() { printf '\n════ %s ════\n' "$1"; }
result() {
  if [ "$1" -eq 0 ]; then printf '── %s: OK\n' "$2"; else printf '── %s: FAIL (exit %s)\n' "$2" "$1"; FAIL=1; fi
}

# Policy engine (Rust, policy-engine/; design plans/DESIGN-POLICY-USER-PRINCIPAL.md §13-§15):
# its own tests, then a release build so the backend's engine-service tests
# (tests/policy_principal/test_engine_*.py, skipped without the binary) run against it.
# `--locked` = the committed Cargo.lock only; SKIPPED with a notice where cargo is absent.
if [ -f "$ROOT/policy-engine/Cargo.toml" ]; then
  section "policy-engine · cargo test + release build"
  if ! command -v cargo >/dev/null 2>&1; then
    printf '── policy-engine: SKIPPED (cargo not on PATH — engine tests and the backend engine-service tests do not run)\n'
  else
    (cd "$ROOT/policy-engine" && cargo test --locked -q && cargo build --locked --release -q)
    result $? "policy-engine"
  fi
fi

if [ -d "$ROOT/backend" ]; then
  section "backend · ruff"
  (cd "$ROOT/backend" && uv run ruff check .); result $? "ruff"

  section "backend · pytest"
  # -n auto fans the ~2.8k hermetic tests across every core (6.4 min -> ~1 min).
  # The tests are process-safe: conftest.py redirects the ledger to a per-process
  # temp SQLite file before any app import. Kept out of addopts so a single
  # `uv run pytest tests/foo.py::bar` stays serial.
  (cd "$ROOT/backend" && uv run pytest -q -n auto); result $? "pytest"

  section "CN distribution · deployment and workshop scripts"
  (
    cd "$ROOT/backend" &&
    uv run ruff check ../scripts/deploy_cn.py ../scripts/deploy_cn_capacity.py \
      ../scripts/verify_deployment_host.py \
      ../samples/itsm-cn/scripts ../samples/itsm-cn/lab-assets/*.py &&
    bash -n ../scripts/install_policy_host.sh
  ); result $? "CN distribution"
fi

if [ -d "$ROOT/infra" ]; then
  section "infra · ruff"
  (cd "$ROOT/infra" && uv run ruff check .); result $? "infra ruff"

  section "infra · pytest"
  (cd "$ROOT/infra" && uv run pytest -q); result $? "infra pytest"
fi

# N11-obs16: a template-grammar error (`Fn::Equals` against a bare number) reached
# CloudFormation before anything local caught it — the static YAML tests parse the
# template, they do not validate it. cfn-lint runs from uv's tool cache, so after its
# first use it needs no network; when `uvx` is missing or cfn-lint cannot be fetched
# (offline before that first use) the step is SKIPPED with a notice rather than
# failing the gate — only real lint findings fail it.
if [ -f "$ROOT/infra/ec2/launchpad-cn-ec2.yaml" ]; then
  section "infra · cfn-lint"
  if ! command -v uvx >/dev/null 2>&1; then
    printf '── cfn-lint: SKIPPED (uvx not on PATH — install uv to lint infra/ec2/*.yaml)\n'
  elif ! uvx cfn-lint --version >/dev/null 2>&1; then
    printf '── cfn-lint: SKIPPED (uvx could not provide cfn-lint — run `uvx cfn-lint --version` once with network)\n'
  else
    uvx cfn-lint "$ROOT"/infra/ec2/*.yaml; result $? "cfn-lint"
  fi
fi

if [ -f "$ROOT/start.py" ] && [ -f "$ROOT/stop.sh" ]; then
  section "local lifecycle · syntax"
  (
    cd "$ROOT/backend" &&
    uv run ruff check ../start.py &&
    python3 -m py_compile ../start.py &&
    bash -n ../stop.sh
  ); result $? "local lifecycle"
fi

if [ -d "$ROOT/frontend" ]; then
  section "frontend · eslint"
  (cd "$ROOT/frontend" && npm run --silent lint); result $? "eslint"

  section "frontend · tsc"
  (cd "$ROOT/frontend" && npx tsc --noEmit); result $? "tsc"

  section "frontend · vitest"
  (cd "$ROOT/frontend" && npm run --silent test); result $? "vitest"

  section "frontend · vite build"
  (cd "$ROOT/frontend" && npm run --silent build); result $? "vite build"
fi

# Round 16: the combination suites' pairwise generator and wizard option tables are pure TS
# with node:test unit tests (tests/e2e/cn/combinations/*.unit.test.ts). Node strips the types
# in place from 22.6 on; an older runtime SKIPs with a notice (same convention as cfn-lint)
# rather than failing the gate — only a failing test does.
if ls "$ROOT"/tests/e2e/cn/combinations/*.unit.test.ts >/dev/null 2>&1; then
  section "e2e · combination generators (node --test)"
  if node -e 'const [a,b]=process.versions.node.split(".").map(Number); process.exit(a>22||(a===22&&b>=6)?0:1)'; then
    (cd "$ROOT/tests/e2e" && node --no-warnings --test cn/combinations/*.unit.test.ts); result $? "combination generators"
  else
    printf '── combination generators: SKIPPED (node %s < 22.6 cannot strip types; run with a newer node)\n' "$(node --version)"
  fi
fi
# N17-obs465: the aws-cn trace hook for TypeScript agents against every supported ADOT Node
# version (lockfile-pinned npm aliases): an ADOT release that moves an internal the hook needs
# must fail loudly here. node_modules is installed from the lockfile (`npm ci`) when missing or
# stale (npm's own node_modules/.package-lock.json differs from package-lock.json); an install
# that fails is one clear FAIL line, never a confusing test error.
if [ -f "$ROOT/backend/app/deployer/adot_cn/test/hook.test.cjs" ]; then
  section "deploy · ADOT Node aws-cn hook matrix (node --test)"
  ADOT_CN="$ROOT/backend/app/deployer/adot_cn"
  if ! node -e '
    const fs = require("fs"); const [lock, installed] = process.argv.slice(1);
    if (!fs.existsSync(installed)) process.exit(1);
    const pkgs = (f) => { const p = { ...JSON.parse(fs.readFileSync(f)).packages }; delete p[""];
      return JSON.stringify(Object.keys(p).sort().map((k) => [k, p[k].version, p[k].integrity])); };
    process.exit(pkgs(lock) === pkgs(installed) ? 0 : 1);
  ' "$ADOT_CN/package-lock.json" "$ADOT_CN/node_modules/.package-lock.json"; then
    printf '── adot-cn hook matrix: node_modules missing or stale — npm ci from the lockfile\n'
    if ! (cd "$ADOT_CN" && npm ci --no-audit --no-fund --silent >/dev/null 2>&1); then
      printf '── adot-cn hook matrix: FAIL (npm ci failed in backend/app/deployer/adot_cn — check the npm registry / network, then rerun)\n'
      FAIL=1
    fi
  fi
  if [ -f "$ADOT_CN/node_modules/.package-lock.json" ]; then
    (cd "$ADOT_CN" && node --test test/hook.test.cjs); result $? "adot-cn hook matrix"
  fi
fi
# The quality board's parsers (scripts/quality_metrics.py) and their fixtures: a red parser used
# to ship unnoticed because this file was only run by `make metrics-test` (rev-r16 on r16-o).
if [ -f "$ROOT/scripts/tests/test_quality_metrics.py" ]; then
  section "scripts · quality metrics tests"
  python3 "$ROOT/scripts/tests/test_quality_metrics.py"; result $? "quality_metrics tests"
fi

if [ -f "$ROOT/scripts/i18n_check.py" ] && [ -d "$ROOT/frontend/src/locales" ]; then
  section "i18n · key parity"
  python3 "$ROOT/scripts/i18n_check.py"; result $? "i18n_check"

  section "i18n · zh-CN full-width punctuation"
  python3 "$ROOT/scripts/i18n_zh_punct.py" --check; result $? "i18n_zh_punct"
fi

if [ -f "$ROOT/scripts/cn_lint.sh" ]; then
  section "cn · partition lint"
  bash "$ROOT/scripts/cn_lint.sh"; result $? "cn_lint"
fi

# N13: the restored Skill Lab surface must stay byte-identical to the Global baseline
# except for the enumerated CN edits (OpenCode executor, partition, model ids).
if [ -f "$ROOT/scripts/n13_restore_check.sh" ]; then
  section "cn · N13 Skill Lab restore check"
  bash "$ROOT/scripts/n13_restore_check.sh"; result $? "n13_restore_check"
fi

# Region boundary (user requirement 2026-10-10): only Global Bedrock model inference may
# cross the partition; every other region literal in shipped code fails unless marked.
if [ -f "$ROOT/scripts/region_boundary_check.py" ]; then
  section "cn · region boundary"
  python3 "$ROOT/scripts/region_boundary_check.py" | grep -v '^   allowed' ; result ${PIPESTATUS[0]} "region_boundary"
fi

# N11 r2-C: the BYOC samples mirror the templates' model-credential helper block;
# their hermetic tests (fake bedrock_agentcore/boto3) + py_compile live outside
# backend/tests because samples/ is not a package of the backend.
if [ -f "$ROOT/samples/byoc/check.sh" ]; then
  section "cn · BYOC sample self-check"
  PYTHON="uv run --directory $ROOT/backend python" bash "$ROOT/samples/byoc/check.sh"; result $? "byoc_samples_check"
fi

# G(N7a): the vendored Harness template keeps every anchor the renderer grafts on
# (docs/cn/interfaces/harness-template.md) and renders+compiles for a representative spec.
if [ -f "$ROOT/backend/app/templates/harness_runtime/main.py.tmpl" ]; then
  section "cn · harness template self-check"
  (cd "$ROOT/backend" && uv run python ../scripts/harness_template_check.py); result $? "harness_template_check"
fi

# N12 (gate item 3): the AgentCore Evaluations / Optimizations / Datasets / ABTests /
# ConfigurationBundles / Gateway-rule APIs do not exist in aws-cn and must not be called.
# Lead ruling 2026-10-10: scope = backend/app backend/lambdas infra scripts (never
# frontend/src — the verbatim-restored UI carries these names in comments, copy and an api
# method name, and D14 keeps the frontend unchanged), patterns word-bound. The restored
# baseline wrappers are deleted by W-E1…W-E3; until all three have merged the grep is
# advisory — the lead flips it on with N12_STRICT=1 at the cn/n12 merge. `create_dataset`
# is matched in its client-call shape (`<client>.create_dataset(`) because the FastAPI
# handler `def create_dataset(` is contract-frozen (operationId) — lead ruling Q1, 2026-10-10.
section "cn · forbidden AgentCore eval/opt APIs"
FORBIDDEN='\b(StartBatchEvaluation|GetBatchEvaluation|start_batch_evaluation|CreateEvaluator|create_evaluator|CreateDataset|CreateOnlineEvaluationConfig|create_online_evaluation_config|StartRecommendation|start_recommendation|CreateConfigurationBundle|create_configuration_bundle|CreateABTest|create_ab_test|GatewayRule|agentcore_eval)\b|\.create_dataset\('
hits="$(cd "$ROOT" && git grep -nP "$FORBIDDEN" -- backend/app backend/lambdas infra scripts ':!scripts/verify.sh' ':!scripts/n12_g_e0_gate.sh' 2>/dev/null || true)"
if [ -n "$hits" ]; then
  count="$(printf '%s\n' "$hits" | wc -l | tr -d ' ')"
  if [ "${N12_STRICT:-0}" = "1" ]; then
    printf '%s\n' "$hits"; result 1 "n12_forbidden_api ($count hits)"
  else
    printf '── n12_forbidden_api: ADVISORY (%s hits remain until W-E1…W-E3 land; set N12_STRICT=1 to enforce)\n' "$count"
  fi
else
  result 0 "n12_forbidden_api"
fi

printf '\n════ verify: %s ════\n' "$([ $FAIL -eq 0 ] && echo PASS || echo FAIL)"
exit $FAIL
