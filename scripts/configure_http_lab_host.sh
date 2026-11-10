#!/usr/bin/env bash
# Explicit isolated-demo opt-in. Prepare one shared HTTP/HTTPS application mode.
# Called by install_policy_host; existing-host operators run before host-update.
# Does not restart the backend: the caller owns its idle check / restart window.
set -Eeuo pipefail
if [[ $# != 1 || $EUID != 0 ]]; then
  echo "usage: sudo bash scripts/configure_http_lab_host.sh http://NLB_IPV4:8088" >&2
  exit 2
fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ORIGIN=$1
ENV_FILE=${LAUNCHPAD_ENV_FILE:-/etc/launchpad/env}
SITE=${LAUNCHPAD_NGINX_SITE:-/etc/nginx/sites-available/launchpad}
HTTP_SITE="${SITE}-http-lab"
LINK=/etc/nginx/sites-enabled/launchpad-http-lab
[[ -f "$ENV_FILE" && -f "$SITE" ]] || { echo "Existing host environment/site required" >&2; exit 2; }
# Parse strictly before writing: no paths, userinfo, query, whitespace or port collisions.
PORT=$("$ROOT/backend/.venv/bin/python" -B - "$ORIGIN" "$SITE" <<'PY'
import ipaddress,re,sys
from pathlib import Path
from urllib.parse import urlsplit
u=urlsplit(sys.argv[1])
try:
 ip=str(ipaddress.IPv4Address(u.hostname))
 port=u.port
 assert u.scheme=="http" and sys.argv[1]==f"http://{ip}:{port}"
 assert 1024<=port<=65535 and port!=8080
 site=Path(sys.argv[2]).read_text()
 match=re.search(r"listen\s+(\d+)\s+ssl",site)
 assert match and port!=int(match[1])
except (ValueError,TypeError,AssertionError):
 raise SystemExit("Expected a canonical HTTP IPv4 origin and a distinct existing TLS listener")
print(port)
PY
)
nginx -t
BACKUP=$(mktemp -d /etc/launchpad/http-lab-config.XXXXXXXX)
chmod 0700 "$BACKUP"
cp -a "$ENV_FILE" "$BACKUP/env"
cp -a "$SITE" "$BACKUP/site"
[[ ! -f "$HTTP_SITE" ]] || cp -a "$HTTP_SITE" "$BACKUP/http-site"
[[ ! -L "$LINK" ]] || readlink "$LINK" > "$BACKUP/link"
restore() {
  cp -a "$BACKUP/env" "$ENV_FILE"
  cp -a "$BACKUP/site" "$SITE"
  if [[ -f "$BACKUP/http-site" ]]; then cp -a "$BACKUP/http-site" "$HTTP_SITE"; else rm -f "$HTTP_SITE"; fi
  if [[ -f "$BACKUP/link" ]]; then ln -sfn "$(cat "$BACKUP/link")" "$LINK"; else rm -f "$LINK"; fi
  nginx -t && systemctl reload nginx
}
trap 'restore' ERR
"$ROOT/backend/.venv/bin/python" -B - "$ENV_FILE" "$ORIGIN" "$PORT" <<'PY'
import os,sys
from pathlib import Path
p=Path(sys.argv[1])
values={
 "LAUNCHPAD_CONSOLE_HTTP_LAB":"true",
 "LAUNCHPAD_CONSOLE_HTTP_PORT":sys.argv[3],
 "LAUNCHPAD_CONSOLE_ENTRY":"public_nlb",
 "LAUNCHPAD_PUBLIC_BASE_URL":sys.argv[2],
 "LAUNCHPAD_OAUTH_RETURN_URL":"",
}
lines=[line for line in p.read_text().splitlines() if line.split("=",1)[0] not in values]
tmp=p.with_name(p.name+".http-lab.tmp")
fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
with os.fdopen(fd,"w") as f:
 f.write("\n".join(lines+[f"{k}={v}" for k,v in values.items()])+"\n")
os.replace(tmp,p)
PY
export LAUNCHPAD_CONSOLE_HTTP_LAB=true
# shellcheck source=../infra/ec2/nginx-site.sh
source "$ROOT/infra/ec2/nginx-site.sh"
TLS_PORT=$(sed -nE 's/^[[:space:]]*listen[[:space:]]+([0-9]+)[[:space:]]+ssl.*/\1/p' "$SITE" | head -1)
render_nginx_site true "$TLS_PORT" "$ROOT/frontend/dist" > "$BACKUP/rendered-tls"
render_nginx_site false "$PORT" "$ROOT/frontend/dist" > "$BACKUP/rendered-http"
install -m 0644 "$BACKUP/rendered-tls" "$SITE"
install -m 0644 "$BACKUP/rendered-http" "$HTTP_SITE"
ln -sfn "$HTTP_SITE" "$LINK"
nginx -t
systemctl reload nginx
trap - ERR
echo "HTTP lab configuration prepared; restart backend in the approved maintenance window."
echo "Primary console: $ORIGIN"
echo "Configuration rollback files: $BACKUP"
