#!/usr/bin/env bash
# N10 API smoke for the Launchpad-CN console. Read-only; prints one line per probe.
#   LAUNCHPAD_E2E_BASE_URL   https://<host>:8443          (default below)
#   LAUNCHPAD_E2E_PASSWORD   console admin password — env only, never echoed
set -u
BASE="${LAUNCHPAD_E2E_BASE_URL:-https://203.0.113.60:8443}"
USER_NAME="${LAUNCHPAD_E2E_USERNAME:-admin}"
: "${LAUNCHPAD_E2E_PASSWORD:?set LAUNCHPAD_E2E_PASSWORD in-process (e.g. from SSM), never in a file}"
JAR="$(mktemp)"; trap 'rm -f "$JAR"' EXIT
fail=0
ok()   { printf 'PASS  %-44s %s\n' "$1" "$2"; }
bad()  { printf 'FAIL  %-44s %s\n' "$1" "$2"; fail=1; }
get()  { curl -sk -b "$JAR" -w '\n%{http_code}' "$BASE$1"; }

code=$(curl -sk -o /dev/null -w '%{http_code}' "$BASE/")
[ "$code" = 200 ] && ok "GET /" "$code" || bad "GET /" "$code"

body=$(curl -sk -c "$JAR" -H 'content-type: application/json' -w '\n%{http_code}' \
  --data-binary "$(jq -cn --arg u "$USER_NAME" --arg p "$LAUNCHPAD_E2E_PASSWORD" '{username:$u,password:$p}')" \
  "$BASE/api/auth/login")
code=${body##*$'\n'}
[ "$code" = 200 ] && ok "POST /api/auth/login" "$code role=$(printf '%s' "${body%$'\n'*}" | jq -r .role)" || bad "POST /api/auth/login" "$code"

body=$(get /api/auth/status); code=${body##*$'\n'}
[ "$(printf '%s' "${body%$'\n'*}" | jq -r .authenticated)" = true ] && ok "GET /api/auth/status" "authenticated" || bad "GET /api/auth/status" "$code"

body=$(get /api/capabilities); feats=$(printf '%s' "${body%$'\n'*}" | jq -c .features)
# Known feature lists: N10 baseline (10), N12/N13 (13: + evaluation, optimization, skill_lab),
# D20 (14: 13 + policy — the default on a host with the Cedar engine registered). When
# LAUNCHPAD_E2E_FEATURES is set (a JSON array) it is the only accepted list.
want="${LAUNCHPAD_E2E_FEATURES:-}"
want10='["agents","chat","registry","memory","knowledge_bases","identity","gateway","observability","workspaces","users"]'
want13='["agents","chat","registry","memory","knowledge_bases","identity","gateway","observability","workspaces","users","evaluation","optimization","skill_lab"]'
want14='["agents","chat","registry","memory","knowledge_bases","identity","gateway","observability","workspaces","users","evaluation","optimization","skill_lab","policy"]'
if [ -n "$want" ]; then
  [ "$(printf '%s' "$feats" | jq -S .)" = "$(printf '%s' "$want" | jq -S .)" ] && ok "GET /api/capabilities" "$(printf '%s' "$feats" | jq length) features (LAUNCHPAD_E2E_FEATURES)" || bad "GET /api/capabilities" "$feats (want LAUNCHPAD_E2E_FEATURES=$want)"
elif [ "$(printf '%s' "$feats" | jq -S .)" = "$(printf '%s' "$want14" | jq -S .)" ]; then ok "GET /api/capabilities" "14 features (13 + D20 policy)"
elif [ "$(printf '%s' "$feats" | jq -S .)" = "$(printf '%s' "$want13" | jq -S .)" ]; then ok "GET /api/capabilities" "13 features (N12 evaluation + optimization, N13 skill_lab)"
elif [ "$(printf '%s' "$feats" | jq -S .)" = "$(printf '%s' "$want10" | jq -S .)" ]; then ok "GET /api/capabilities" "10 features (N10 baseline)"
else bad "GET /api/capabilities" "$feats"; fi

body=$(get /api/workspaces); st=$(printf '%s' "${body%$'\n'*}" | jq -r '.workspaces[] | select(.is_default) | .bootstrap_status')
[ "$st" = ready ] && ok "GET /api/workspaces" "default bootstrap_status=ready" || bad "GET /api/workspaces" "$st"

for p in /api/agents /api/system-agents /api/identity/connections /api/governance/gateways /api/tools \
         /api/registry/records /api/knowledge-bases /api/memory/overview /api/memory/resources \
         /api/observability/dashboard /api/overview; do
  body=$(get "$p"); code=${body##*$'\n'}
  [ "$code" = 200 ] && ok "GET $p" "$code $(printf '%s' "${body%$'\n'*}" | head -c 60 | tr '\n' ' ')…" || bad "GET $p" "$code"
done

body=$(get /api/identity/connections)
printf '%s' "${body%$'\n'*}" | jq -e '.connections[] | select(.name=="launchpad-global-bedrock" and .kind=="api_key")' >/dev/null \
  && ok "identity provider launchpad-global-bedrock" "present (api_key)" || bad "identity provider launchpad-global-bedrock" "missing"

body=$(get /api/governance/gateways)
printf '%s' "${body%$'\n'*}" | jq -e '.gateways[0].targets[] | select(.name=="hr-database" and .status=="READY")' >/dev/null \
  && ok "gateway target hr-database" "READY" || bad "gateway target hr-database" "missing/not READY"

exit $fail
