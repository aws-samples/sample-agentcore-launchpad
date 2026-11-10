.PHONY: adot-node-canary policy-engine policy-engine-artifact policy-engine-bench dev verify bootstrap backend frontend lint-cn contract metrics metrics-test mutation mutation-frontend graph-coverage graph-soak graph-faults eval-local eval-diff judge-calibration judge-drift

dev:
	bash scripts/dev.sh

verify:
	bash scripts/verify.sh

# Policy engine (Rust): tests + local release build; the arm64 artifact for the hub host
policy-engine:
	cd policy-engine && cargo test --locked -q && cargo build --locked --release

policy-engine-artifact:
	bash policy-engine/packaging/build-artifact.sh

# decide latency per shard, stateless vs recorded (design §13.5 budget; M1 exit criterion).
# On the hub host: /opt/launchpad-policy-engine/bin/launchpad-policy-engine bench --dir /var/lib/launchpad-policy-engine
policy-engine-bench:
	cd policy-engine && cargo build --locked --release -q && ./target/release/launchpad-policy-engine bench --iterations 2000 --shards 2

bootstrap:
	cd backend && uv run python ../scripts/bootstrap.py

backend:
	cd backend && uv run uvicorn app.main:app --reload --port 8000

frontend:
	cd frontend && npm run dev

lint-cn:
	bash scripts/cn_lint.sh

# Quality metrics board (quality pack): aggregates storyline REPORTs, the quarantine list, the
# findings registers, the latest security scan, the deploy-ops ledger (outside the repo) and the
# verify logs into docs/cn/reports/quality-metrics.md + quality-metrics-history.jsonl (one line per
# day). Read-only, stdlib only, no AWS. `metrics-test` runs the parser fixtures; neither target is
# part of `make verify`.
metrics:
	python3 scripts/quality_metrics.py

metrics-test:
	python3 scripts/tests/test_quality_metrics.py

# The property-based API contract run alone (it also runs inside `make verify`'s backend
# pytest step): hermetic, in-process, see docs/cn/interfaces/testing.md.
contract:
	cd backend && uv run pytest tests/contract/test_openapi_schemathesis.py -q --hypothesis-show-statistics

# Mutation testing (round 16 baseline, N16-obs60): how STRONG the tests are, not whether they pass.
# Neither target is part of `make verify` — one backend run takes ~15 min on the 32-core host.
# Scope and the test selection live in backend/pyproject.toml [tool.mutmut] and
# frontend/stryker.config.json; results: `cd backend && uv run mutmut results` /
# `uv run mutmut show <mutant>`, frontend/reports/mutation/mutation.{html,json}. Baseline report:
# docs/cn/reports/mutation-baseline-2026-10-19.md; how to read it: docs/cn/interfaces/testing.md.
# `backend/mutants/` is mutmut's scratch copy + result cache (git-ignored): a second run re-tests
# only the functions whose source or tests changed. Delete the directory for a cold run. Workers
# default to 8 (MUTMUT_CHILDREN) so the shared host keeps CPU for the verify gates (N16-obs67).
mutation:
	cd backend && uv run mutmut run --max-children $${MUTMUT_CHILDREN:-8} && uv run mutmut results

mutation-frontend:
	cd frontend && npx stryker run

# Lifecycle-graph coverage (round 16, r16-o; docs/cn/interfaces/testing.md §6.7): walks every
# prime path / edge pair of backend/tests/graph/graphs/*.yaml through the hermetic API executor
# (the same tests `make verify` runs) and writes the edges / prime paths covered per graph to
# docs/cn/reports/graph-coverage.md (+ .json), which the quality board reads
# (`graph_edges_covered`, `graph_prime_paths_covered`). Hermetic, no AWS. Set
# LAUNCHPAD_SOAK=1 to walk every edge pair (the soak twins) as well.
# Round 17 (r17-b, testing.md §6.8): the report also carries the loop-engine table (random
# walks, neighbourhood paths, cross-graph walks) and the loop rounds; the run ends by printing
# the loop-rule line (round · method · findings · zero-yield streak /3; stop after three
# consecutive zero-yield rounds, a zero round switches method) plus the reference metrics.
# It also runs the seeded fault walks (LAUNCHPAD_FAULT_WALKS=1) so the board's numbers keep
# counting them although `make verify` no longer does (N17-obs287).
graph-coverage:
	cd backend && LAUNCHPAD_GRAPH_REPORT=$(CURDIR)/docs/cn/reports/graph-coverage.md LAUNCHPAD_FAULT_WALKS=1 \
		uv run pytest tests/graph -q -n auto -p no:cacheprovider
	python3 scripts/quality_metrics.py --stop-rule

# The loop-engine soak (NOT in `make verify`; ~15–30 min on the 32-core host): every edge pair
# of every graph, 200 seeded random walks per graph, 60 cross-graph walks per pairing. A
# failure is a finding: shrink it (the walk runner prints the minimal sequence + repro-hash),
# register it in docs/cn/reports/bugs-round-<n>.md and pin it — never weaken the oracle.
# `LAUNCHPAD_LOOP_ROUND=<n> make graph-soak` draws round n's FRESH seed window (walks
# [(n-1)·200, n·200), cross walks [(n-1)·60, n·60)) so a later round never replays an earlier one
# (testing.md §6.8); default 1.
graph-soak:
	cd backend && LAUNCHPAD_SOAK=1 LAUNCHPAD_LOOP_ROUND=$${LAUNCHPAD_LOOP_ROUND:-1} \
		uv run pytest tests/graph -q -n auto -p no:cacheprovider --durations=40

# The seeded fault walks (testing.md §6.9) at their fixed verify shape — 4 seeds per graph and
# the 6 cross fault walks — on demand (NOT in `make verify` since r17-s2 / N17-obs287: rounds
# 8–9 of the fault-walk method yielded nothing, so under the loop stop rule they left the gate).
# `make verify` still runs every deterministic fault grid next to them: the refusal-seam axis,
# the workspace stage injection, the pinned `test_N17_obs*` regressions and the
# `test_graph_fault_probes_*` probes. `make graph-soak` runs the walks at full family breadth.
graph-faults:
	cd backend && LAUNCHPAD_FAULT_WALKS=1 \
		uv run pytest tests/graph/test_graph_fault_walks_*.py tests/graph/test_graph_fault_cross.py \
		tests/graph/test_fault_engine.py -q -n auto -p no:cacheprovider

# Local evaluation workbench (eval-accuracy loop; docs/cn/interfaces/evaluation-engine.md §26):
# replays an eval run's REAL CloudWatch records through THIS checkout's production evaluation
# code — M1 extraction vs the runtime origin logs; `eval-diff` adds the local judges and Global
# AgentCore `Evaluate` (M2–M4). Read-only on AWS; records + reports in backend/.eval-cache/
# (gitignored). NOT part of `make verify`. Examples:
#   make eval-local RUN=fb8a7e230b11 [HOST=t1] [EVAL_ARGS="--judge --refresh"]
#   make eval-local SESSIONS=<id>,<id> LOG_GROUP=/aws/bedrock-agentcore/runtimes/<rt>-DEFAULT
#   make eval-diff RUN=fb8a7e230b11 [EVAL_ARGS="--global-reps 3 --round 1"]
EVAL_WB_ARGS = $(if $(RUN),--run $(RUN)) $(if $(SESSIONS),--sessions $(SESSIONS)) \
	$(if $(LOG_GROUP),--log-group $(LOG_GROUP)) --host $(or $(HOST),default) $(EVAL_ARGS)

eval-local:
	cd backend && uv run python scripts/eval_workbench.py $(EVAL_WB_ARGS)

eval-diff:
	cd backend && uv run python scripts/eval_workbench.py --judge --global $(EVAL_WB_ARGS)

# Judge drift watch (Round 3 R3-e; evaluation-engine.md §26.z). `judge-calibration` judges the
# full calibration set — the easy tier, then the hard tier (EVAL-TRUST 2.1b) — on both CN judges
# (3 reps) and records the summaries under
# docs/cn/reports/judge-calibration/<fingerprint>{,-hard}.{json,md} — the judge gate in `make verify`
# (tests/evaluation/test_judge_calibration_gate.py) asks for that record whenever a judge
# prompt or judge model id changes the fingerprint. `judge-drift` is the daily mini-run (the
# mini-set, base variant, 1 rep) against the record: exit 1 on drift. Both call the
# judges (the Global Bedrock key from CN SSM); neither is part of `make verify`.
JUDGES ?= global.zai.glm-5.3,global.moonshotai.kimi-k3
JUDGE_REGION = AWS_DEFAULT_REGION=$${AWS_DEFAULT_REGION:-cn-northwest-1}

judge-calibration:
	cd backend && $(JUDGE_REGION) uv run python scripts/eval_workbench.py --offline --calibration \
		--calibration-models $(JUDGES) --reps 3 --concurrency 12 --calibration-record $(EVAL_ARGS)
	cd backend && $(JUDGE_REGION) uv run python scripts/eval_workbench.py --offline --calibration \
		--calibration-tier hard --calibration-models $(JUDGES) --reps 3 --concurrency 12 \
		--calibration-record $(EVAL_ARGS)

judge-drift:
	cd backend && $(JUDGE_REGION) uv run python scripts/eval_workbench.py --offline --calibration \
		--calibration-mini --calibration-models $(JUDGES) --reps 1 --concurrency 12 $(EVAL_ARGS)

# N17-obs465: the aws-cn trace hook for TypeScript agents against the NEWEST ADOT Node release
# (needs the npm registry; reports only, never bumps the pinned matrix). The pinned versions
# run in `make verify` (backend/app/deployer/adot_cn — `npm ci` there once).
adot-node-canary:
	bash scripts/adot_node_canary.sh
