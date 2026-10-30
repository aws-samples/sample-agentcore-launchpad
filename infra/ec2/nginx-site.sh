#!/usr/bin/env bash
# The Launchpad nginx site — ONE source of truth, sourced (never executed) by
#   infra/ec2/bootstrap-host.sh   first boot: renders /etc/nginx/sites-available/launchpad
#   scripts/host-update.sh        every update: re-renders it, installs + reloads nginx ONLY
#                                 when the rendered file differs from the one nginx runs
# so a rule change here reaches running hosts on their next host-update (WB-003).
#
#   render_nginx_site  <tls: true|false> <console-port> [dist-root] [backend-port] [media-root]
#   refresh_nginx_site <site-path> <dist-root> [run-hook]        (host-update step 12)
#
# Rules (docs/cn/interfaces/deploy.md §8.1 "asset caching", §8.8 "self-hosted media"):
#   /api/, /v1/     → the backend on 127.0.0.1:<backend-port>, unbuffered (SSE), 1 h read timeout
#   /assets/        → Vite's content-hashed chunks: a year of immutable caching, and a hash nginx
#                     no longer has (dist/ was rebuilt under an open tab) answers 404 — never
#                     index.html, which the browser cannot parse as a module script; the console
#                     reloads itself once on that 404 (frontend/src/layout/RouteChunk.tsx)
#   /media/         → the demo-video library's self-hosted media (N13-obs11, user decision
#                     2026-10-17: no CDN). Static files only, aliased from <media-root>
#                     (/home/ubuntu/launchpad-media — OUTSIDE the git checkout, created by
#                     bootstrap-host.sh, never written by host-update.sh), no directory listing,
#                     explicit MIME types (Ubuntu's mime.types has no text/vtt, which <track>
#                     requires), a year of immutable caching (every path carries its revision:
#                     /media/<section>/<YYYYMMDD-v2-cn-hr>/<slug>.zh-CN.mp4). Byte ranges are
#                     nginx's static-file default (Accept-Ranges: bytes) — nothing here disables
#                     them. No CORS header: the player (<video crossOrigin="anonymous">) fetches
#                     from the console's own origin, so the media URL must use the same host:port
#                     the viewer's address bar shows (docs/video-library.md "Media hosting").
#   everything else → the SPA: the file if it exists, else index.html, Cache-Control: no-cache so
#                     a returning tab revalidates the entry and learns the new chunk names
# Security headers (N12-obs60; scan T-1..T-3, T-5). Emitted on EVERY response of EVERY location:
# an nginx `add_header` inside a location REPLACES the whole server-level set, so the block is
# rendered into each location, and `always` keeps the headers on 401 / 404 / 502 too.
#   Content-Security-Policy — enforced, explicit allowlist (every source below was read from the
#   bundle's actual needs, see docs/cn/interfaces/deploy.md §11):
#     default-src 'self'             fallback for any fetch directive not listed
#     base-uri 'self'                no <base> hijack of relative URLs
#     object-src 'none'              no plugin content
#     frame-ancestors 'none'         the console may not be framed (clickjacking; = X-Frame-Options)
#     frame-src 'none'               the console frames nothing (no <iframe> in the SPA / DCV SDK)
#     form-action 'self'             forms submit to the console only
#     script-src 'self' 'wasm-unsafe-eval' https://cdn.jsdelivr.net
#                                    Vite's hashed chunks; the DCV live view decodes video with
#                                    WebAssembly (no eval / new Function anywhere in that SDK);
#                                    @monaco-editor/react loads Monaco from jsdelivr (loader.js, AMD
#                                    modules, blob workers that importScripts the CDN)
#     style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net
#                                    Monaco / xyflow / Cloudscape / the DCV SDK inject <style>
#                                    elements and style="" attributes; Monaco's CSS is on the CDN
#     font-src 'self' data: https://cdn.jsdelivr.net
#                                    bundled @fontsource files; Monaco's codicon font from the CDN
#     img-src 'self' data: blob: https:
#                                    inline SVG data URIs, screenshots / canvases, video posters on
#                                    the library's external CDN
#     media-src 'self' blob: https:  the video library plays <video> / <track> from an external CDN
#     connect-src 'self' https: wss: data:
#                                    same-origin fetch + SSE; DCV fetches embedded data: WASM
#                                    decoder bytes and opens a presigned
#                                    wss:// stream on the AgentCore endpoint. Scheme-wide because the
#                                    partition host may not be written here (scripts/cn_lint.sh);
#                                    tighten once the live-view endpoint is a render parameter
#     worker-src 'self' blob:        DCV decoder workers (same origin) and Monaco's blob workers
#     manifest-src 'self'
#   X-Frame-Options DENY             legacy twin of frame-ancestors for older browsers
#   X-Content-Type-Options nosniff   no MIME sniffing of chunks or member uploads
#   Referrer-Policy strict-origin-when-cross-origin
#                                    CloudWatch / docs deep links learn the origin, never the path
#   Permissions-Policy camera=(), microphone=(), geolocation=(), payment=()
#                                    the console uses none of these; clipboard is deliberately NOT
#                                    restricted (copy buttons)
#   Strict-Transport-Security        TLS site only: max-age one year + includeSubDomains, no preload.
#                                    The backend's own `hsts` middleware (app/main.py) stays for the
#                                    dev proxy; the proxy location hides the upstream copy so every
#                                    response carries exactly one HSTS header (nginx's)
_nginx_security_headers() {   # <tls: true|false> — printed with the location indent
  local tls="$1"
  cat <<HEADERS
    add_header Content-Security-Policy "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; frame-src 'none'; form-action 'self'; script-src 'self' 'wasm-unsafe-eval' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; font-src 'self' data: https://cdn.jsdelivr.net; img-src 'self' data: blob: https:; media-src 'self' blob: https:; connect-src 'self' https: wss: data:; worker-src 'self' blob:; manifest-src 'self'" always;
    add_header X-Frame-Options DENY always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy strict-origin-when-cross-origin always;
    add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
HEADERS
  if [ "$tls" = "true" ]; then
    if [ "${LAUNCHPAD_CONSOLE_HTTP_LAB:-false}" = true ]; then
      echo '    add_header Strict-Transport-Security "max-age=0" always;'
    else
      echo '    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;'
    fi
  fi
}

# TLS (T-5): TLS 1.2 + 1.3 only; under 1.2 just the ECDHE + AEAD suites (forward secrecy, no CBC,
# no static RSA — Mozilla "intermediate" minus CBC; every evergreen browser negotiates 1.3 anyway);
# server order wins. nginx's TLS 1.3 suites are not configurable here and are all AEAD.
NGINX_TLS_CIPHERS='ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256'

render_nginx_site() {
  local tls="${1:?render_nginx_site <tls: true|false> <console-port> [dist-root] [backend-port] [media-root]}"
  local port="${2:?render_nginx_site: console port missing}"
  local dist_root="${3:-/home/ubuntu/launchpad/frontend/dist}" backend_port="${4:-8000}"
  local media_root="${5:-/home/ubuntu/launchpad-media}"
  local listen_block proto_header headers
  if [ "$tls" = "true" ]; then
    listen_block="listen ${port} ssl default_server;
    ssl_certificate /etc/nginx/launchpad-tls/cert.pem;
    ssl_certificate_key /etc/nginx/launchpad-tls/key.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ${NGINX_TLS_CIPHERS};
    ssl_prefer_server_ciphers on;"
    proto_header='proxy_set_header X-Forwarded-Proto https;
    proxy_hide_header Strict-Transport-Security;'
  else
    listen_block="listen ${port} default_server;"
    proto_header='proxy_set_header X-Forwarded-Proto http;
    proxy_hide_header Strict-Transport-Security;'
  fi
  headers="$(_nginx_security_headers "$tls")"
  cat <<NGINX
server {
  $listen_block
  server_tokens off;
  client_max_body_size 64m;
  location ~ ^/(api|v1)/ {
    proxy_pass http://127.0.0.1:${backend_port};
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_read_timeout 3600s;
    proxy_set_header Host \$host;
    proxy_set_header Forwarded "";
    proxy_set_header X-Forwarded-Host \$host;
    proxy_set_header X-Forwarded-Port \$server_port;
    $proto_header
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
${headers}
  }
  location /assets/ {
    root ${dist_root};
    try_files \$uri =404;
    add_header Cache-Control "public, max-age=31536000, immutable";
${headers}
  }
  location /media/ {
    alias ${media_root%/}/;
    autoindex off;
    types {
      video/mp4 mp4;
      video/webm webm;
      text/vtt vtt;
      image/jpeg jpg jpeg;
    }
    default_type application/octet-stream;
    add_header Cache-Control "public, max-age=31536000, immutable";
${headers}
  }
  location / {
    root ${dist_root};
    try_files \$uri /index.html;
    add_header Cache-Control "no-cache";
${headers}
  }
}
NGINX
}

# refresh_nginx_site <site-path> <dist-root> [run-hook]
#   The update-time half (scripts/host-update.sh step 12): re-render <site-path> for this
#   checkout's <dist-root> and install + `nginx -t` + `systemctl reload nginx` ONLY when the
#   rendering differs from the file nginx runs. TLS and the console port are read from the
#   live site's `listen` line; the backend port is 8000 (what the systemd unit binds).
#   Every root-run command (install / nginx -t / systemctl) goes through <run-hook>
#   (default: execute it), so a dry run passes a hook that prints the plan and writes
#   nothing. The function NEVER fails its caller — every outcome returns 0 — because the
#   backend restart that follows must still run: an aborted step 12 left the OLD backend
#   serving the NEW dist (r7-e review follow-up on WB-003). Outcomes:
#     no site file                      → "skipped — … (not an nginx host)"
#     no `listen <port>` line           → WARNING, site left untouched
#     rendering == live site            → "unchanged — nginx not reloaded" (nginx untouched)
#     differs, `nginx -t` already fails → WARNING "already failing before our change", skipped
#     differs, `nginx -t` passes after  → <site>.prev kept, rendering installed, nginx reloaded
#     differs, `nginx -t` fails after   → <site>.prev restored, WARNING, nginx NOT reloaded
#     an install itself fails           → WARNING, nginx NOT reloaded (a failed RESTORE gets its
#       (read-only /etc, ENOSPC …)        own WARNING: the rejected rendering is still on disk)
#   Every install is guarded for the same reason — under the caller's `set -e` a bare failing
#   `install` would end the script before the restart.
_nginx_site_exec() { "$@"; }
refresh_nginx_site() {
  local site="${1:?refresh_nginx_site <site-path> <dist-root> [run-hook]}"
  local dist_root="${2:?refresh_nginx_site: dist root missing}"
  local run="${3:-_nginx_site_exec}"
  if [ ! -f "$site" ]; then
    echo "  skipped — $site does not exist (not an nginx host)"; return 0
  fi
  local listen_line port tls=false rendered
  listen_line="$(grep -m1 -E '^[[:space:]]*listen[[:space:]]+[0-9]+' "$site" || true)"
  port="$(printf '%s' "$listen_line" | sed -nE 's/^[[:space:]]*listen[[:space:]]+([0-9]+).*/\1/p')"
  if [ -z "$port" ]; then
    echo "  WARNING: $site has no recognisable 'listen <port>' line — left untouched" >&2
    return 0
  fi
  case " $listen_line " in *" ssl "*) tls=true ;; esac
  rendered="$(mktemp)"
  render_nginx_site "$tls" "$port" "$dist_root" >"$rendered"
  echo "  rendered for tls=$tls port=$port dist=$dist_root"
  if cmp -s "$rendered" "$site"; then
    echo "  unchanged — nginx not reloaded"
    rm -f "$rendered"
    return 0
  fi
  echo "  site differs from the rendered template:"
  diff -u "$site" "$rendered" | sed 's/^/    /' || true
  # A config that already fails is not ours to fix — and must not be blamed on the rendering.
  echo "  checking the CURRENT nginx config before touching the site:"
  if ! "$run" nginx -t; then
    echo "  WARNING: nginx config already failing before our change — site refresh skipped" >&2
    rm -f "$rendered"
    return 0
  fi
  echo "  installing the rendered site and reloading nginx:"
  if ! "$run" install -m 0644 "$site" "$site.prev" ||
     ! "$run" install -m 0644 "$rendered" "$site"; then
    echo "  WARNING: could not write $site — nginx not reloaded" >&2
    rm -f "$rendered"
    return 0
  fi
  rm -f "$rendered"
  if "$run" nginx -t; then
    # nginx -t only checks the config: a stopped nginx passes it and fails the reload. Warn and
    # continue — the site on disk is valid, the service restart that follows still runs.
    "$run" systemctl reload nginx \
      || echo "  WARNING: nginx reload failed (is nginx running?) — $site is installed and valid;" \
              "reload or start nginx by hand" >&2
  elif "$run" install -m 0644 "$site.prev" "$site"; then
    echo "  WARNING: the rendered nginx site failed nginx -t — restored $site from $site.prev" \
         "(nginx not reloaded; the service restart still runs)" >&2
  else
    echo "  WARNING: restore of $site from $site.prev FAILED — $site may be invalid;" \
         "fix before the next nginx reload" >&2
  fi
  return 0
}
