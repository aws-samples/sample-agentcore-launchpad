#!/usr/bin/env bash
# N17-obs465 canary (NOT in make verify — it needs the npm registry): run the aws-cn trace hook's
# regression matrix against the NEWEST ADOT Node release. Reports only; never bumps the pinned
# matrix (backend/app/deployer/adot_cn/package.json + its lockfile).
#   make adot-node-canary            # the registry in ~/.npmrc / NPM_CONFIG_REGISTRY
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/backend/app/deployer/adot_cn"
PKG="@aws/aws-distro-opentelemetry-node-autoinstrumentation"
latest="$(npm view "$PKG" version)"
alias="adot-node-$(echo "$latest" | tr . -)"
if grep -q "@$latest\"" "$SRC/package.json"; then
  echo "adot-node-canary: $PKG $latest is already in the pinned matrix — nothing new"; exit 0
fi
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
cp "$SRC/launchpad-adot-cn.cjs" "$work/"; mkdir -p "$work/test"; cp "$SRC/test/hook.test.cjs" "$work/test/"
node -e '
const fs = require("fs"); const p = JSON.parse(fs.readFileSync(process.argv[1] + "/package.json"));
p.devDependencies = { [process.argv[2]]: "npm:" + process.argv[3] + "@" + process.argv[4],
  "adot-node-pinned": p.devDependencies[Object.keys(p.devDependencies).sort().pop()] };
fs.writeFileSync(process.argv[5] + "/package.json", JSON.stringify(p, null, 1));
' "$SRC" "$alias" "$PKG" "$latest" "$work"
(cd "$work" && npm install --no-audit --no-fund --silent >/dev/null)
echo "adot-node-canary: testing $PKG $latest (newest) against launchpad-adot-cn.cjs"
if (cd "$work" && node --test test/hook.test.cjs); then
  echo "adot-node-canary: $latest PASSES — add \"$alias\" to the pinned matrix in a reviewed change"
else
  echo "adot-node-canary: $latest FAILS — read the failing assertion: an internal the hook uses moved," \
       "or the release signs .cn natively (then the hook reports \"native\" and the matrix entry goes)"
  exit 1
fi
