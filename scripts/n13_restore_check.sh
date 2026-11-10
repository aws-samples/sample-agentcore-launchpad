#!/usr/bin/env bash
# N13 restore check (D16): the Skill Lab surface is restored VERBATIM from the fork
# baseline 4e27b1f4, and the only files allowed to differ are the enumerated CN edits
# below (partition/hostnames, model ids, the OpenCode executor replacing the
# claude_code/codex executors, the CN-mirror worker image, the opencode-only picker).
#
#   bash scripts/n13_restore_check.sh            # full check (identity + CN-edit rules)
#   bash scripts/n13_restore_check.sh --verbatim # identity only (the pre-CN-edit commit)
#
# Fails on: a restored file outside the allowlist that differs from the baseline; a
# baseline file missing from the tree; a deleted executor file that came back; any
# restored file still carrying a Claude Code / Codex code path (delete-not-flag).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
BASE=4e27b1f4
MODE="${1:-full}"
FAIL=0

# Every Skill Lab file the CN branch deleted (the lead's enumeration) + the one
# N12-owned helper Skill Lab imports (restored byte-identical from the same baseline).
# The classic Skill Lab pages (frontend/src/pages/SkillLab*.tsx, pages/skillLab/*,
# components/SkillLabNav.tsx) are excluded since r13-a retired the classic console
# (N13-obs1): the Skill Lab surface is the V2 page, which is checked below.
mapfile -t RESTORED < <(
  git ls-tree -r --name-only "$BASE" | grep -i -E 'skill_?lab|skillopt|SkillLab' \
    | grep -v -E '^(backend/app/(core|main|services)|frontend/src/(App|lib/api|layout|locales|pages/Registry|pages/SkillLab|pages/skillLab/|components/SkillLabNav|v2/pages/registry|v2/nav)|scripts/|docs/|README|backend/tests/test_(bootstrap|system_skill|videos|workspace)|frontend/src/studio|\.gitignore)'
  printf '%s\n' backend/app/evaluation/__init__.py backend/app/evaluation/queue.py
)

# Enumerated CN edits — the ONLY restored files allowed to differ from the baseline.
# (a) partition/hostnames  (b) model ids / Global Bedrock bearer  (c) opencode executor
# (d) CN-mirror arm64 worker image  (e) opencode-only UI copy  (t) tests adapted to c/d/e
# (f) creator columns — Family B step 1 (upstream #261 port, N12-obs50): `created_by` /
#     `created_by_name` on jobs + tasksets, stamped by the routers, shown by the V2 lists
# (g) mutation ownership — Family B step 3 (r13-g, N13-obs15): job cancel / resume / publish /
#     delete and task set PUT / DELETE / apply-expansion are creator-or-admin in the routers;
#     the V2 pages hide those actions for rows the member cannot change
# (h) status enums worded (N17-obs457): a registry record status, a job status shown as the
#     progress fallback and the publish toasts read through the console's i18n labels
# (i) display-zone times (N17-obs463): the artifact clock formats in the API's display zone,
#     and task_assets' upload-limit body leaves through DisplayTimeJSONResponse
# (j) explicit HTTP lab clipboard compatibility (35a5e229): TasksetEditor's copy-example
#     action uses the shared modern / legacy / manual fallback and only toasts on success;
#     taskset contents, validation, submission and evaluation behavior are unchanged
ALLOWED_DIFF=(
  # backend (a)(b)(c)(d)(f)
  backend/app/skill_lab/runner.py
  backend/app/skill_lab/infra.py
  backend/app/skill_lab/worker_build.py
  backend/app/skill_lab/routers.py              # (f) creator stamp, (g) creator-or-admin gates
  backend/app/skill_lab/jobs.py
  backend/app/skill_lab/samples.py
  backend/app/skill_lab/task_assets.py          # reserved workspace roots: the exec CLI's own dirs; (i)
  backend/app/skill_lab/models.py               # (f) creator columns
  backend/app/skill_lab/tasksets.py             # (f) creator stamp + taskset_info fields
  # vendored SkillOpt (b)(c)(d)
  vendor/skillopt/LAUNCHPAD_DEVIATIONS.md
  vendor/skillopt/deploy/agentcore/Dockerfile
  vendor/skillopt/configs/_base_/default.yaml
  vendor/skillopt/configs/skilleval/default.yaml
  vendor/skillopt/scripts/evaluate_skill.py
  vendor/skillopt/scripts/generate_tasks.py
  vendor/skillopt/scripts/train.py
  vendor/skillopt/skillopt/config.py
  vendor/skillopt/skillopt/engine/trainer.py
  vendor/skillopt/skillopt/gradient/reflect.py
  vendor/skillopt/skillopt/model/__init__.py
  vendor/skillopt/skillopt/model/agentcore_runner.py
  vendor/skillopt/skillopt/model/agentcore_worker.py
  vendor/skillopt/skillopt/model/backend_config.py
  vendor/skillopt/skillopt/model/bedrock_chat.py
  vendor/skillopt/skillopt/model/codex_harness.py
  vendor/skillopt/skillopt/model/common.py
  vendor/skillopt/skillopt/envs/skilleval/adapter.py
  vendor/skillopt/skillopt/envs/skilleval/contracts.py   # reserved workspace roots
  vendor/skillopt/skillopt/envs/skilleval/dataloader.py  # reserved workspace roots
  vendor/skillopt/skillopt/envs/skilleval/agentic_judge.py
  vendor/skillopt/skillopt/envs/skilleval/judge_worker.py
  vendor/skillopt/skillopt/envs/skilleval/rollout.py
  # frontend (e)
  frontend/src/lib/skillLab.ts
  frontend/src/lib/skillLabTasksets.ts          # reserved workspace roots (mirror of task_assets.py)
  frontend/src/v2/pages/skilllab/ModelFields.tsx
  frontend/src/v2/pages/skilllab/EvalWizard.tsx
  frontend/src/v2/pages/skilllab/TaskgenWizard.tsx
  frontend/src/v2/pages/skilllab/TrainWizard.tsx
  frontend/src/v2/pages/skilllab/EvalDetail.tsx      # (e), (g) creator-only actions, (h)
  frontend/src/v2/pages/skilllab/TaskgenDetail.tsx   # (e), (g) creator-only cancel, (h)
  frontend/src/v2/pages/skilllab/TaskgenList.tsx     # (e), (g) creator-only cancel
  frontend/src/v2/pages/skilllab/TrainDetail.tsx     # (g) creator-only cancel / resume / delete / publish, (h)
  frontend/src/v2/pages/skilllab/SkillPicker.tsx     # (h) the record status by its label
  frontend/src/v2/pages/skilllab/ArtifactBrowser.tsx # (i) the artifact clock in the display zone
  frontend/src/v2/pages/skilllab/JobList.tsx         # (f) Creator column, (g) creator-only actions
  frontend/src/v2/pages/skilllab/TasksetDetail.tsx   # (f) Creator row, (g) creator-only edit / delete
  frontend/src/v2/pages/skilllab/TasksetList.tsx     # (f) Creator column, (g) creator-only edit / delete
  frontend/src/v2/pages/skilllab/TasksetEditor.tsx   # (j) HTTP-compatible copy-example action only
  # tests (t)
  backend/tests/test_skill_lab_foundation.py
  backend/tests/test_skill_lab_jobs.py
  backend/tests/test_skill_lab_judge_routing.py
  backend/tests/test_skill_lab_taskgen.py
  backend/tests/test_skill_lab_taskgen_vendored.py
  backend/tests/test_skill_lab_asset_rollout.py
  backend/tests/test_skill_lab_bedrock_chat.py
  backend/tests/test_skill_lab_task_assets.py
  backend/tests/test_skill_lab_train.py
  backend/tests/test_skill_lab_samples.py            # (t) N13-obs20: submit probes the bearer
  backend/scripts/e2e_skill_lab.py
  backend/scripts/e2e_skill_lab_taskgen_assets.py
)

# Baseline files whose executor is gone for good (delete-not-flag) — must NOT exist.
DELETED=(
  vendor/skillopt/deploy/agentcore/codex-config.toml
  vendor/skillopt/skillopt/model/claude_backend.py
)

is_allowed() {
  local f="$1" a
  for a in "${ALLOWED_DIFF[@]}"; do [ "$a" = "$f" ] && return 0; done
  return 1
}
is_deleted() {
  local f="$1" a
  for a in "${DELETED[@]}"; do [ "$a" = "$f" ] && return 0; done
  return 1
}

checked=0 identical=0 cn_edited=0
for f in "${RESTORED[@]}"; do
  checked=$((checked + 1))
  if is_deleted "$f"; then
    if [ "$MODE" != "--verbatim" ] && [ -e "$f" ]; then
      echo "n13_restore_check: $f must stay deleted (delete-not-flag)"; FAIL=1
    fi
    continue
  fi
  if [ ! -f "$f" ]; then
    echo "n13_restore_check: missing restored file $f"; FAIL=1; continue
  fi
  if git show "$BASE:$f" | cmp -s - "$f"; then
    identical=$((identical + 1))
    continue
  fi
  if [ "$MODE" = "--verbatim" ] || ! is_allowed "$f"; then
    echo "n13_restore_check: $f differs from $BASE and is not an enumerated CN edit"
    FAIL=1
  else
    cn_edited=$((cn_edited + 1))
  fi
done

if [ "$MODE" != "--verbatim" ]; then
  # Delete-not-flag: no Claude Code / Codex code path survives in the restored surface.
  # The deviations log may NAME the former executors once, in its history section.
  FORBIDDEN='claude_code_exec|codex_exec|CLAUDE_CODE_|CODEX_(EXEC|HOME|CLI)|claude\.ai/install|public\.ecr\.aws|run_claude_code_exec|run_codex_exec|configure_codex_exec|configure_claude_code_exec'
  # (the foundation test asserts the ABSENCE of these strings in the Dockerfile)
  hits="$(printf '%s\n' "${RESTORED[@]}" | grep -v -E 'LAUNCHPAD_DEVIATIONS\.md$|test_skill_lab_foundation\.py$' \
          | xargs -r grep -n -E -e "$FORBIDDEN" -- 2>/dev/null \
          | grep -v 'OPENCODE_DISABLE_CLAUDE_CODE' || true)"  # OpenCode's own switch, not an executor
  if [ -n "$hits" ]; then
    printf 'n13_restore_check: Claude Code / Codex code path still present:\n%s\n' "$hits"; FAIL=1
  fi
  # The executor replacement exists and the worker image pins the CLI.
  [ -f vendor/skillopt/skillopt/model/opencode_backend.py ] \
    || { echo "n13_restore_check: vendor/skillopt/skillopt/model/opencode_backend.py missing"; FAIL=1; }
  [ -f backend/app/skill_lab/executors/opencode.py ] \
    || { echo "n13_restore_check: backend/app/skill_lab/executors/opencode.py missing"; FAIL=1; }
  grep -q -E '^ARG OPENCODE_VERSION=\S+' vendor/skillopt/deploy/agentcore/Dockerfile \
    || { echo "n13_restore_check: worker Dockerfile lost its OPENCODE_VERSION pin"; FAIL=1; }
fi

if [ "$FAIL" -eq 0 ]; then
  echo "n13_restore_check: OK ($checked files · $identical identical to $BASE · $cn_edited enumerated CN edits · mode ${MODE#--})"
fi
exit $FAIL
