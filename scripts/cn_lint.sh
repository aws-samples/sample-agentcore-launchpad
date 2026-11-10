#!/usr/bin/env bash
# CN partition lint (spec C2): no hard-coded `arn:aws:`, `.amazonaws.com` or Global
# Management Console / sign-in host (`console.aws.amazon.com`, `signin.aws.amazon.com`)
# in shipped code. Backend code goes through WorkspaceContext.partition/dns_suffix/arn()
# and WorkspaceContext.console_host (console hosts do NOT follow the DNS suffix), CDK
# through self.partition/self.url_suffix. A line may opt out with the marker
# `cn-lint: allow` (reserved for the partition→suffix / partition→console-host mapping
# tables themselves and partition-tolerant regexes).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SCOPES=(backend/app backend/lambdas infra/stacks infra/ec2 frontend/src scripts)
PATTERNS=(
  'arn:aws:'
  '\.amazonaws\.com([^.]|$)'
  'amazonaws\\\.com'
  # Global console / sign-in hosts, plain or regex-escaped (`console\.aws\.amazon\.com`).
  # `<region>.console.aws.amazon.com` contains the bare host, so one pattern covers both.
  'console\\?\.aws\\?\.amazon\\?\.com'
  'signin\\?\.aws\\?\.amazon\\?\.com'
)
FAIL=0
for scope in "${SCOPES[@]}"; do
  [ -d "$scope" ] || continue
  for pattern in "${PATTERNS[@]}"; do
    hits="$(grep -rnE --include='*.py' --include='*.ts' --include='*.tsx' --include='*.json' \
              --include='*.sh' --include='*.mjs' --include='*.tmpl' --include='*.hbs' \
              --include='*.yaml' --include='*.yml' \
              --exclude-dir=tests --exclude-dir=__pycache__ --exclude-dir=node_modules \
              --exclude='*.test.ts' --exclude='*.test.tsx' --exclude='cn_lint.sh' \
              -e "$pattern" "$scope" | grep -v 'cn-lint: allow' || true)"
    if [ -n "$hits" ]; then
      printf '── cn_lint: pattern %s in %s\n%s\n' "$pattern" "$scope" "$hits"
      FAIL=1
    fi
  done
done
if [ "$FAIL" -eq 0 ]; then echo "cn_lint: OK"; fi
exit $FAIL
