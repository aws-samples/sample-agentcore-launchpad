#!/usr/bin/env bash
# Root-only second stage, after the host stack has created its KMS key and network.
# No credentials in arguments: artifact URI, checksum, commit, region, nonsecret settings.
set -Eeuo pipefail
export PYTHONUNBUFFERED=1
[[ ( $# = 5 || $# = 6 ) && $EUID = 0 ]] || {
  echo "usage: $0 s3-uri sha256 commit region settings-json [direct-ipv4] (root)" >&2; exit 2;
}
ARTIFACT_URI=$1
EXPECTED_SHA=$2
EXPECTED_COMMIT=$3
DEPLOY_REGION=$4
POLICY_SETTINGS=$5
DIRECT_IP=${6:-}
[[ -z "$DIRECT_IP" || "$DIRECT_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || {
  echo "Expected an IPv4 address for the direct console certificate SAN" >&2; exit 2;
}
REPO=/home/ubuntu/launchpad
[[ "$(git -c safe.directory="$REPO" -C "$REPO" rev-parse HEAD)" = "$EXPECTED_COMMIT" ]] || {
  echo "Host checkout differs from planned release" >&2; exit 1;
}
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
aws s3 cp --region "$DEPLOY_REGION" --only-show-errors "$ARTIFACT_URI" "$STAGE/engine.tar.gz"
echo "$EXPECTED_SHA  $STAGE/engine.tar.gz" | sha256sum --check --quiet
# Only nonsecret settings enter the checkout. Bootstrap runs as the service user.
printf '%s' "$POLICY_SETTINGS" > "$STAGE/settings.json"
chmod 0755 "$STAGE"
chmod 0644 "$STAGE/settings.json"
# The EIP is associated AFTER the instance's first-boot success signal. Its cached
# origin/certificate may still name the temporary address. Reconcile before login.
# An operator invoking this helper for an upgrade can omit public_base_url.
PUBLIC_ORIGIN=$(jq -r '.public_base_url // empty' "$STAGE/settings.json")
HTTP_LAB=$(jq -r '.console_http_lab // false' "$STAGE/settings.json")
if [[ "$HTTP_LAB" = true ]]; then
  bash "$REPO/scripts/configure_http_lab_host.sh" "$PUBLIC_ORIGIN"
fi
if [[ -n "$DIRECT_IP" ]]; then
  # The optional sixth argument denotes the separate direct entry behind a public
  # NLB. host-update must retain this origin instead of replacing it from IMDS.
  sed -i '/^LAUNCHPAD_CONSOLE_ENTRY=/d' /etc/launchpad/env
  printf 'LAUNCHPAD_CONSOLE_ENTRY=public_nlb\n' >> /etc/launchpad/env
fi
if [[ -n "$PUBLIC_ORIGIN" ]]; then
  [[ "$PUBLIC_ORIGIN" =~ ^(https|http)://([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+):[0-9]+$ ]] || {
    echo "Expected the CloudFormation IPv4 console URL" >&2; exit 2;
  }
  [[ "${BASH_REMATCH[1]}" = https || "$HTTP_LAB" = true ]] || {
    echo "Plain HTTP requires explicit console_http_lab opt-in" >&2; exit 2;
  }
  CONSOLE_IP=${BASH_REMATCH[2]}
  sed -i "s|^LAUNCHPAD_PUBLIC_BASE_URL=.*|LAUNCHPAD_PUBLIC_BASE_URL=$PUBLIC_ORIGIN|" /etc/launchpad/env
  # x509 -checkip only prints a mismatch; its exit status can still be zero.
  # verify returns failure for a wrong IP or expired self-signed certificate.
  CERT_VALID=true
  CERT_SANS="IP:$CONSOLE_IP"
  [[ -z "$DIRECT_IP" || "$DIRECT_IP" = "$CONSOLE_IP" ]] || CERT_SANS+=",IP:$DIRECT_IP"
  for CERT_IP in "$CONSOLE_IP" ${DIRECT_IP:+"$DIRECT_IP"}; do
    openssl verify -CAfile /etc/nginx/launchpad-tls/cert.pem \
      -verify_ip "$CERT_IP" /etc/nginx/launchpad-tls/cert.pem >/dev/null 2>&1 || CERT_VALID=false
  done
  if [[ "$CERT_VALID" = false ]]; then
    bash "$REPO/infra/ec2/tls-cert.sh" --force --san "$CERT_SANS"
    nginx -t
    systemctl reload nginx
  fi
fi
set -a
# shellcheck source=/dev/null
. /etc/launchpad/env
set +a
export PATH=/usr/local/bin:/usr/bin:/usr/sbin:/bin:/sbin
(
  runuser -u ubuntu -m -- bash -s -- "$STAGE/settings.json" <<'OWNER'
set -Eeuo pipefail
cd /home/ubuntu/launchpad/backend
uv run python - "$1" <<'PY'
import json, sys
from app.services.bootstrap import write_config
write_config(json.load(open(sys.argv[1])))
PY
uv run python ../scripts/bootstrap.py --skip-cdk
OWNER
)
KEY_URI=$(cd "$REPO/backend" && runuser -u ubuntu -m -- /usr/local/bin/uv run python -c \
  'from app.core.config import get_settings; s=get_settings(); print("s3://"+s.resources["artifacts_bucket"]+"/policy/gateway/"+s.resources["gateway_id"]+"/engine-key")')
bash "$REPO/policy-engine/packaging/install.sh" \
  --tarball "$STAGE/engine.tar.gz" --sha256 "$EXPECTED_SHA" \
  --key-s3-uri "$KEY_URI" --region "$DEPLOY_REGION"
systemctl restart launchpad-backend
for _ in $(seq 1 60); do
  curl -fsS http://127.0.0.1:8000/api/health >/dev/null && break
  sleep 2
done
curl -fsS http://127.0.0.1:8000/api/health
# A successful systemd start alone is not sufficient. The same read-only checker
# also runs in a fresh SSM command on every deployment resume.
cd "$REPO/backend"
VERIFICATION_SETTINGS=$(jq --arg commit "$EXPECTED_COMMIT" '. + {commit: $commit}' "$STAGE/settings.json")
"$REPO/backend/.venv/bin/python" -B ../scripts/verify_deployment_host.py \
  --expected "$VERIFICATION_SETTINGS"
