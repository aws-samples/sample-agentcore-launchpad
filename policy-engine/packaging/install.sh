#!/usr/bin/env bash
# Install / upgrade the policy engine on the hub host (run as root, from the unpacked
# artifact directory). Idempotent. Design §13.5 / §13.6.
#
#   sudo ./packaging/install.sh --sha256 <expected sha of the tarball> --tarball <path>
#        --key-s3-uri s3://<artifacts>/policy/gateway/<gateway_id>/engine-key
#        [--shards 2] [--hub-user ubuntu] [--aws-profile <p>] [--region <r>]
#   ./packaging/install.sh --print-region [--region <r>]   (no root: print the resolved region)
#
# * resolves the AWS region once — `--region`, else the instance's own region from IMDSv2 —
#   and passes it to every aws call: a root shell (e.g. SSM) has no AWS config, and without a
#   region aws-cli misses the aws-cn endpoint (N17-obs481),
# * creates the system user/group lp-policy (the hub user joins the group: it may use the
#   control sockets and read the audit JSONL, nothing else),
# * installs the binary to /opt/launchpad-policy-engine/bin after verifying the tarball sha,
# * installs /etc/launchpad-policy-engine/engine-key (0640 root:lp-policy) byte-for-byte from
#   the shared S3 object the interceptor bootstrap stage created (contract amendment 3:
#   exactly 32 raw bytes; the key is never printed),
# * writes one socket drop-in per shard on the host's PRIVATE IP (IMDSv2), ports 7443+n,
# * enables the sockets + services and performs a ROLLING restart (shard by shard,
#   waiting for /healthz ready) — never part of scripts/host-update.sh.
set -euo pipefail

SHARDS=2
HUB_USER=ubuntu
SHA=""
TARBALL=""
KEY_URI=""
PROFILE_ARGS=()
REGION=""
PRINT_REGION=0
IMDS="http://169.254.169.254"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --shards) SHARDS="$2"; shift 2 ;;
    --hub-user) HUB_USER="$2"; shift 2 ;;
    --sha256) SHA="$2"; shift 2 ;;
    --tarball) TARBALL="$2"; shift 2 ;;
    --key-s3-uri) KEY_URI="$2"; shift 2 ;;
    --aws-profile) PROFILE_ARGS=(--profile "$2"); shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --print-region) PRINT_REGION=1; shift ;;
    *) echo "unknown flag $1" >&2; exit 2 ;;
  esac
done
imds() {  # IMDSv2: a session token first, then the path
  local token
  token="$(curl -sf -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' "$IMDS/latest/api/token")" || return 1
  curl -sf -H "X-aws-ec2-metadata-token: $token" "$IMDS/latest/meta-data/$1"
}
if [[ -z "$REGION" ]]; then
  REGION="$(imds placement/region)" || { echo "no --region and IMDSv2 gave no region" >&2; exit 1; }
fi
[[ "$REGION" =~ ^[a-z]{2}(-[a-z]+)+-[0-9]+$ ]] || { echo "not a region: '$REGION'" >&2; exit 1; }
if [[ $PRINT_REGION -eq 1 ]]; then echo "$REGION"; exit 0; fi

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
[[ -n "$SHA" && -f "$TARBALL" && -n "$KEY_URI" ]] || {
  echo "--sha256, --tarball and --key-s3-uri are required" >&2; exit 2; }
echo "$SHA  $TARBALL" | sha256sum -c --quiet - || { echo "sha256 mismatch: refusing" >&2; exit 1; }

getent group lp-policy >/dev/null || groupadd --system lp-policy
id lp-policy >/dev/null 2>&1 || useradd --system --gid lp-policy --home-dir /var/lib/launchpad-policy-engine \
  --shell /usr/sbin/nologin lp-policy
usermod -aG lp-policy "$HUB_USER"

STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
tar -C "$STAGE" -xzf "$TARBALL"
install -d -m 0755 /opt/launchpad-policy-engine/bin
install -m 0755 "$STAGE/bin/launchpad-policy-engine" /opt/launchpad-policy-engine/bin/launchpad-policy-engine.new
mv -f /opt/launchpad-policy-engine/bin/launchpad-policy-engine.new /opt/launchpad-policy-engine/bin/launchpad-policy-engine
install -m 0644 "$STAGE/packaging/launchpad-policy-engine@.service" /etc/systemd/system/
install -m 0644 "$STAGE/packaging/launchpad-policy-engine@.socket" /etc/systemd/system/

install -d -m 0750 -o root -g lp-policy /etc/launchpad-policy-engine
KEY_TMP="$(mktemp)"; chmod 0600 "$KEY_TMP"
aws s3 cp "${PROFILE_ARGS[@]}" --region "$REGION" --only-show-errors "$KEY_URI" "$KEY_TMP"
[[ "$(stat -c %s "$KEY_TMP")" -eq 32 ]] || { rm -f "$KEY_TMP"; echo "engine key must be 32 bytes" >&2; exit 1; }
install -m 0640 -o root -g lp-policy "$KEY_TMP" /etc/launchpad-policy-engine/engine-key
rm -f "$KEY_TMP"
echo "LP_ENGINE_SHARDS=$SHARDS" > /etc/launchpad-policy-engine/engine.env
install -d -m 0750 -o lp-policy -g lp-policy /var/lib/launchpad-policy-engine /var/lib/launchpad-policy-engine/audit

PRIVATE_IP="$(imds local-ipv4)"
[[ "$PRIVATE_IP" =~ ^10\.|^172\.(1[6-9]|2[0-9]|3[01])\.|^192\.168\. ]] || {
  echo "refusing to bind a non-private address ($PRIVATE_IP)" >&2; exit 1; }

systemctl daemon-reload
for ((n = 0; n < SHARDS; n++)); do
  install -d -m 0755 "/etc/systemd/system/launchpad-policy-engine@$n.socket.d"
  printf '[Socket]\nListenStream=%s:%d\n' "$PRIVATE_IP" $((7443 + n)) \
    > "/etc/systemd/system/launchpad-policy-engine@$n.socket.d/listen.conf"
done
systemctl daemon-reload
for ((n = 0; n < SHARDS; n++)); do
  systemctl enable --now "launchpad-policy-engine@$n.socket" >/dev/null
  systemctl enable "launchpad-policy-engine@$n.service" >/dev/null
  systemctl restart "launchpad-policy-engine@$n.service"
  for _ in $(seq 1 50); do
    curl -sf "http://$PRIVATE_IP:$((7443 + n))/healthz" | grep -q '"ready":true' && break
    sleep 0.2
  done
  curl -sf "http://$PRIVATE_IP:$((7443 + n))/healthz" | grep -q '"ready":true' \
    || { echo "shard $n did not become ready" >&2; exit 1; }
  echo "shard $n ready on $PRIVATE_IP:$((7443 + n))"
done
