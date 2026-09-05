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

if [ -d "$ROOT/backend" ]; then
  section "backend · ruff"
  (cd "$ROOT/backend" && uv run ruff check .); result $? "ruff"

  section "backend · pytest"
  (cd "$ROOT/backend" && uv run pytest -q); result $? "pytest"
fi

if [ -d "$ROOT/infra" ]; then
  section "infra · ruff"
  (cd "$ROOT/infra" && uv run ruff check .); result $? "infra ruff"

  section "infra · pytest"
  (cd "$ROOT/infra" && uv run pytest -q); result $? "infra pytest"
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

  section "frontend · vite build"
  (cd "$ROOT/frontend" && npm run --silent build); result $? "vite build"
fi

if [ -f "$ROOT/scripts/i18n_check.py" ] && [ -d "$ROOT/frontend/src/locales" ]; then
  section "i18n · key parity"
  python3 "$ROOT/scripts/i18n_check.py"; result $? "i18n_check"

  section "i18n · zh-CN full-width punctuation"
  python3 "$ROOT/scripts/i18n_zh_punct.py" --check; result $? "i18n_zh_punct"
fi

printf '\n════ verify: %s ════\n' "$([ $FAIL -eq 0 ] && echo PASS || echo FAIL)"
exit $FAIL
