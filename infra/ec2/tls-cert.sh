#!/usr/bin/env bash
# The console's self-signed TLS certificate (N12-obs60; security scan T-4).
#
#   infra/ec2/tls-cert.sh [--out-dir DIR] [--cn NAME] [--san ENTRY[,ENTRY...]]... [--days N]
#                         [--force] [--print]
#
# Writes <DIR>/key.pem (0600) and <DIR>/cert.pem (0644) — the paths the nginx site renders
# (infra/ec2/nginx-site.sh). The certificate always carries a subjectAltName: `DNS:<cn>` first,
# then every --san entry (modern clients ignore the CN and match the SAN only). Entries are
# `DNS:name` / `IP:addr` as OpenSSL spells them; a bare IPv4 / IPv6 address becomes `IP:`, a bare
# host name `DNS:`; one --san may carry a comma-separated list; duplicates are dropped.
#
# Callers:
#   infra/ec2/bootstrap-host.sh   first boot, TlsMode=selfsigned: --san IP:<this host's IPv4>. The
#                                 EIP is associated by a separate CloudFormation resource and the
#                                 console NLB's address is never known at boot, so that SAN may be
#                                 the pre-EIP address.
#   an operator on the host       re-run with the deploy-time addresses, then reload nginx:
#                                   sudo infra/ec2/tls-cert.sh --force --san IP:<eip>,IP:<nlb-ip>
#                                   sudo nginx -t && sudo systemctl reload nginx
#                                 (every browser that imported the previous cert must re-import it.)
#
# Nothing here is committed: the key pair lives only on the host. Exit codes: 0 ok, 2 usage,
# 3 refused to overwrite an existing cert (pass --force), 4 openssl failed.
set -euo pipefail

OUT_DIR=/etc/nginx/launchpad-tls
CN=launchpad.internal
DAYS=825
FORCE=0
PRINT=0
SANS=()

usage() {
  cat <<USAGE
usage: $(basename "$0") [--out-dir DIR] [--cn NAME] [--san ENTRY[,ENTRY...]]... [--days N] [--force] [--print]
  --out-dir DIR   where key.pem / cert.pem go (default $OUT_DIR; created 0700)
  --cn NAME       subject CN, also the first DNS SAN (default $CN)
  --san ENTRY     extra SAN: DNS:name | IP:addr | bare host name | bare IPv4/IPv6; repeatable,
                  comma-separated lists accepted; duplicates dropped
  --days N        validity in days (default $DAYS)
  --force         replace an existing key pair (default: refuse, exit 3)
  --print         print the subjectAltName extension that would be used and exit (no files)
USAGE
}

die() { echo "tls-cert: $*" >&2; exit 2; }

# valid_dns_name <name>: dot-separated labels of [A-Za-z0-9-] (a leading `*` wildcard label is
# allowed), no empty label, no leading/trailing hyphen — what OpenSSL and browsers accept.
valid_dns_name() {
  local name="$1" label
  [ -n "$name" ] || return 1
  case "$name" in .*|*.|*..*) return 1 ;; esac
  IFS='.' read -r -a labels <<<"$name"
  for label in "${labels[@]}"; do
    [[ "$label" =~ ^(\*|[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)$ ]] || return 1
  done
  return 0
}

# normalise_san <entry> → DNS:… / IP:… (or dies on an entry OpenSSL would reject)
normalise_san() {
  local e="$1"
  e="${e//[[:space:]]/}"
  [ -n "$e" ] || die "empty --san entry"
  case "$e" in
    DNS:*|IP:*|email:*|URI:*) ;;
    *[!0-9.]*)
      if [[ "$e" == *:* ]]; then e="IP:$e"           # IPv6 literal
      else e="DNS:$e"; fi ;;
    *) e="IP:$e" ;;                                   # digits and dots only → IPv4
  esac
  local value="${e#*:}"
  case "$e" in
    DNS:*) valid_dns_name "$value" || die "not a host name: ${value}" ;;
    IP:*)  [[ "$value" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ || "$value" =~ ^[0-9A-Fa-f:]+$ ]] \
             || die "not an IP address: ${value}" ;;
  esac
  printf '%s' "$e"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --out-dir) [ $# -ge 2 ] || die "--out-dir needs a value"; OUT_DIR="$2"; shift 2 ;;
    --cn)      [ $# -ge 2 ] || die "--cn needs a value"; CN="$2"; shift 2 ;;
    --days)    [ $# -ge 2 ] || die "--days needs a value"
               [[ "$2" =~ ^[0-9]+$ ]] && [ "$2" -gt 0 ] || die "--days wants a positive integer"
               DAYS="$2"; shift 2 ;;
    --san)     [ $# -ge 2 ] || die "--san needs a value"
               IFS=',' read -r -a parts <<<"$2"
               for part in "${parts[@]}"; do SANS+=("$(normalise_san "$part")"); done
               shift 2 ;;
    --force)   FORCE=1; shift ;;
    --print)   PRINT=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *)         usage >&2; die "unknown option: $1" ;;
  esac
done

valid_dns_name "$CN" || die "--cn is not a host name: $CN"

# DNS:<cn> first, then the extras in the order given, each once.
ALL=("DNS:${CN}")
for s in "${SANS[@]+"${SANS[@]}"}"; do
  seen=0
  for have in "${ALL[@]}"; do [ "$have" = "$s" ] && seen=1 && break; done
  [ "$seen" = 1 ] || ALL+=("$s")
done
SAN_EXT="subjectAltName=$(IFS=','; printf '%s' "${ALL[*]}")"

if [ "$PRINT" = 1 ]; then
  printf '%s\n' "$SAN_EXT"
  exit 0
fi

KEY="$OUT_DIR/key.pem"
CERT="$OUT_DIR/cert.pem"
if [ -e "$CERT" ] || [ -e "$KEY" ]; then
  if [ "$FORCE" != 1 ]; then
    echo "tls-cert: $CERT already exists — re-run with --force to replace it" >&2
    echo "          (then: nginx -t && systemctl reload nginx)" >&2
    exit 3
  fi
fi

install -d -m 0700 "$OUT_DIR"
tmp_key="$(mktemp "$OUT_DIR/.key.XXXXXX")"
tmp_cert="$(mktemp "$OUT_DIR/.cert.XXXXXX")"
cleanup() { rm -f "$tmp_key" "$tmp_cert"; }
trap cleanup EXIT
if ! openssl req -x509 -nodes -newkey rsa:2048 -days "$DAYS" -sha256 \
      -keyout "$tmp_key" -out "$tmp_cert" -subj "/CN=${CN}" -addext "$SAN_EXT" 2>/dev/null; then
  echo "tls-cert: openssl req failed (OpenSSL >= 1.1.1 is needed for -addext)" >&2
  exit 4
fi
chmod 0600 "$tmp_key"
chmod 0644 "$tmp_cert"
mv -f "$tmp_key" "$KEY"
mv -f "$tmp_cert" "$CERT"
trap - EXIT

echo "tls-cert: wrote $KEY (0600) and $CERT"
echo "  subject   CN=${CN}"
echo "  SAN       ${SAN_EXT#subjectAltName=}"
echo "  expires   $(openssl x509 -in "$CERT" -noout -enddate | sed 's/^notAfter=//')"
echo "  reload    nginx -t && systemctl reload nginx (if nginx is already running)"
