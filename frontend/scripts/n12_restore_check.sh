#!/usr/bin/env bash
# N12 W-E4 gate: the evaluation / optimization frontend surface is restored
# VERBATIM from the Global baseline 4e27b1f4 (plan §A.3). Every file listed in
# FILES must be byte-identical to the baseline, except the files that carry one
# of the enumerated CN edits of plan §A.4 — those must equal baseline + the
# committed patch `n12_allowed_edits/<path with / → __>.patch`, nothing else.
# Exit 0 = clean. Run from anywhere inside the repo.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
BASE="${N12_BASELINE:-4e27b1f4}"
PATCHES="frontend/scripts/n12_allowed_edits"
FILES=(
  components/EvaluationNav.tsx components/InsightClusters.tsx components/RunResultsPanel.tsx
  lib/evaluation.ts lib/evaluators.ts lib/experiments.ts
  pages/Evaluation.tsx pages/EvaluationDatasets.tsx pages/EvaluationEvaluators.tsx
  pages/EvaluationExperiment.tsx pages/EvaluationOnline.tsx pages/EvaluationRuntimeCanary.tsx
  v2/EvaluatorPicker.tsx v2/InsightClusters.tsx v2/InsightsPanel.tsx v2/ResultsView.tsx
  v2/online.ts v2/results.ts v2/tasks.ts
  v2/pages/DataCenter.tsx v2/pages/Evaluators.tsx v2/pages/Experiments.tsx v2/pages/Insights.tsx
  v2/pages/Online.tsx v2/pages/Tasks.tsx v2/pages/assistant/RecommendationHistory.tsx
  v2/pages/canary/CanaryDetail.tsx v2/pages/canary/CanaryList.tsx v2/pages/canary/CanaryStart.tsx
  v2/pages/canary/RampPlan.tsx v2/pages/canary/common.ts
  v2/pages/data/AddToDataset.tsx v2/pages/data/Datasets.tsx v2/pages/data/Pipelines.tsx v2/pages/data/Traces.tsx
  v2/pages/experiments/ExperimentDetail.tsx v2/pages/experiments/ExperimentList.tsx
  v2/pages/experiments/ExperimentStart.tsx v2/pages/experiments/StageCard.tsx v2/pages/experiments/common.ts
  v2/pages/online/OnlineDetail.tsx v2/pages/online/OnlineEditor.tsx v2/pages/online/OnlineList.tsx
  v2/pages/tasks/LogSourceFields.tsx v2/pages/tasks/LogStreamPicker.tsx v2/pages/tasks/RunRecommendations.tsx
  v2/pages/tasks/TaskDetail.tsx v2/pages/tasks/TaskList.tsx v2/pages/tasks/TaskWizard.tsx
  # W-E5 (CA-5): the architect assistant's evaluation-assets panels
  v2/pages/assistant/EvalAssets.tsx pages/EvaluationAssetsPanel.tsx
)
# Files NOT restored (plan §A.3 / C5): must stay absent.
ABSENT=(v2/pages/assistant/HarnessCanary.tsx)
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
FAIL=0; VERBATIM=0; EDITED=0
for f in "${FILES[@]}"; do
  path="frontend/src/$f"
  patch_file="$PATCHES/${f//\//__}.patch"
  if [ ! -f "$path" ]; then echo "n12_restore_check: MISSING $path"; FAIL=1; continue; fi
  if [ -f "$patch_file" ]; then
    git show "$BASE:$path" > "$TMP/base" || { echo "n12_restore_check: no baseline for $path"; FAIL=1; continue; }
    if ! patch --quiet --no-backup-if-mismatch -o "$TMP/expected" "$TMP/base" < "$patch_file"; then
      echo "n12_restore_check: patch does not apply: $patch_file"; FAIL=1; continue
    fi
    if cmp -s "$TMP/expected" "$path"; then EDITED=$((EDITED+1)); else
      echo "n12_restore_check: $path differs from baseline + $patch_file:"; diff -u "$TMP/expected" "$path" | head -40; FAIL=1
    fi
  else
    if git diff --quiet "$BASE" -- "$path"; then VERBATIM=$((VERBATIM+1)); else
      echo "n12_restore_check: $path is not verbatim $BASE (no allowed-edit patch):"; git diff --stat "$BASE" -- "$path"; FAIL=1
    fi
  fi
done
for f in "${ABSENT[@]}"; do
  [ -e "frontend/src/$f" ] && { echo "n12_restore_check: $f must not be restored (C5)"; FAIL=1; }
done
# every committed patch must name a listed file
for p in "$PATCHES"/*.patch; do
  [ -e "$p" ] || continue
  name="$(basename "$p" .patch)"; name="${name//__//}"
  printf '%s\n' "${FILES[@]}" | grep -qx "$name" || { echo "n12_restore_check: stray patch $p"; FAIL=1; }
done
echo "n12_restore_check: ${#FILES[@]} files · $VERBATIM verbatim · $EDITED with enumerated edits · $( [ $FAIL -eq 0 ] && echo OK || echo FAIL )"
exit $FAIL
