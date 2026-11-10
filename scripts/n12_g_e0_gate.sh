#!/usr/bin/env bash
# G(E0) gate for N12 W-E0 (plan §C.2): everything the foundation promised, in one run.
# Forbidden-API grep rule (lead ruling 2026-10-10, gate item 3): scope backend/app
# backend/lambdas infra scripts — frontend/src is excluded on purpose (verbatim-restored
# UI keeps the names in comments/copy/an api method; D14) — word-bound patterns;
# advisory until N12_STRICT=1 at the cn/n12 merge.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT/backend" || exit 2
FAIL=0
step() { printf '\n── %s\n' "$1"; }
ok()   { if [ "$1" -eq 0 ]; then echo "   OK"; else echo "   FAIL (exit $1)"; FAIL=1; fi; }

step "1. pins + import probe (E0-1)";            uv run pytest tests/test_strands_pins.py -q -p no:cacheprovider 2>&1 | tail -1; ok ${PIPESTATUS[0]}
step "2. restored suites + CN notes (E0-2)";     uv run pytest tests/evaluation tests/optimization -q -n auto -p no:cacheprovider 2>&1 | tail -1; ok ${PIPESTATUS[0]}
step "3. contract snapshot + response shapes (E0-3)"; uv run pytest tests/contract -q -p no:cacheprovider 2>&1 | tail -1; ok ${PIPESTATUS[0]}
step "3b. baseline JSON unchanged (sha256 pinned)"; grep -q "$(sha256sum tests/contract/openapi_baseline_eval.json | cut -d' ' -f1)" tests/contract/test_eval_contract_snapshot.py; ok $?
step "3c. snapshot determinism (CN app renders byte-identically twice)"
uv run python ../scripts/snapshot_openapi.py --out /tmp/g_e0_a.json >/dev/null 2>&1 && uv run python ../scripts/snapshot_openapi.py --out /tmp/g_e0_b.json >/dev/null 2>&1 && cmp -s /tmp/g_e0_a.json /tmp/g_e0_b.json; ok $?
step "4. tables / migrations / features / settings (E0-4)"; uv run pytest tests/test_ledger_migration.py tests/test_workspaces.py tests/test_capabilities.py tests/test_feature_backends.py tests/test_settings_eval.py -q -p no:cacheprovider 2>&1 | tail -1; ok ${PIPESTATUS[0]}
step "4b. features off → /api/eval and /api/experiments are 404"
LAUNCHPAD_FEATURES='["agents","chat"]' uv run python - <<'PY' 2>/dev/null
import os, tempfile
os.environ["LAUNCHPAD_DATABASE_URL"] = "sqlite:///" + tempfile.mkdtemp() + "/g.db"
os.environ["LAUNCHPAD_ALLOW_OPEN_CONSOLE"] = "true"; os.environ["LAUNCHPAD_MODEL_PRICES_REFRESH_HOURS"] = "0"
from fastapi.testclient import TestClient
from app.main import create_app
c = TestClient(create_app())
assert c.get("/api/eval/datasets").status_code == 404 and c.get("/api/experiments").status_code == 404
assert c.get("/api/capabilities").json()["features"] == ["agents", "chat"]
PY
ok $?
step "5. adapter layer / judge factory / engine slot / jobs / executor (E0-5)"; uv run pytest tests/evaluation/test_judge_model_factory.py tests/evaluation/test_spans_provider.py tests/evaluation/test_evaluator_mapping.py tests/evaluation/test_engine_slot.py tests/evaluation/test_jobs_protocol.py tests/evaluation/test_executor_selection.py -q -p no:cacheprovider 2>&1 | tail -1; ok ${PIPESTATUS[0]}
step "5b. funnel: no boto3 outside aws_clients; strands_eval builds nothing"; uv run pytest tests/test_client_funnel.py -q -p no:cacheprovider 2>&1 | tail -1; ok ${PIPESTATUS[0]}
step "5c. grep -rn boto3 app/services/strands_eval (must be empty)"; ! grep -rn "boto3" app/services/strands_eval; ok $?
step "7. preset generalisation + runtime wrapper (E0-7)"; uv run pytest tests/test_system_agents_presets.py tests/test_runtime_wrapper.py -q -p no:cacheprovider 2>&1 | tail -1; ok ${PIPESTATUS[0]}
step "6. interface docs present (E0-6)"; test -s ../docs/cn/interfaces/evaluation-engine.md && grep -q "N12" ../docs/cn/interfaces/features.md && grep -q "n12_forbidden_api" ../scripts/verify.sh; ok $?
step "C2. cn_lint"; bash ../scripts/cn_lint.sh | tail -1; ok ${PIPESTATUS[0]}
step "C5 (advisory until E1–E3): forbidden AgentCore eval/opt API hits (backend/app backend/lambdas infra scripts, word-bound)"
(cd .. && git grep -nP '\b(StartBatchEvaluation|GetBatchEvaluation|start_batch_evaluation|CreateEvaluator|create_evaluator|CreateDataset|CreateOnlineEvaluationConfig|create_online_evaluation_config|StartRecommendation|start_recommendation|CreateConfigurationBundle|create_configuration_bundle|CreateABTest|create_ab_test|GatewayRule|agentcore_eval)\b|\.create_dataset\(' -- backend/app backend/lambdas infra scripts ':!scripts/verify.sh' ':!scripts/n12_g_e0_gate.sh' | wc -l | sed 's/^/   hits: /')
step "AGENTS.md mirror + docs links"; uv run pytest tests/test_agents_md_mirror.py tests/test_docs_tracked_links.py -q -p no:cacheprovider 2>&1 | tail -1; ok ${PIPESTATUS[0]}
printf '\n════ G(E0): %s ════\n' "$([ $FAIL -eq 0 ] && echo PASS || echo FAIL)"
exit $FAIL
