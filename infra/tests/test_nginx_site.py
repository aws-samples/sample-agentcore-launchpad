"""infra/ec2/nginx-site.sh (WB-003): the ONE nginx site both bootstrap-host.sh (first boot)
and scripts/host-update.sh (every update) render. Hermetic: bash renders to stdout."""

import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

EC2 = Path(__file__).resolve().parents[1] / "ec2"
LIB = EC2 / "nginx-site.sh"
BOOTSTRAP = EC2 / "bootstrap-host.sh"


def render(tls: str, port: str, *extra: str) -> str:
    return subprocess.run(
        ["bash", "-c", '. "$1" && shift && render_nginx_site "$@"', "_", str(LIB), tls, port,
         *extra],
        check=True, capture_output=True, text=True,
    ).stdout


def test_library_parses_and_is_shellcheck_clean():
    subprocess.run(["bash", "-n", str(LIB)], check=True)
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed")
    subprocess.run(["shellcheck", "-S", "warning", str(LIB)], check=True)


def test_hashed_assets_404_and_cache_immutably_while_the_spa_entry_revalidates():
    site = render("false", "80")
    assets = site[site.index("location /assets/ {"):]
    assets = assets[: assets.index("}") + 1]
    assert "try_files $uri =404;" in assets                       # never index.html for a chunk
    assert 'add_header Cache-Control "public, max-age=31536000, immutable";' in assets
    spa = site[site.index("location / {"):]
    spa = spa[: spa.index("}") + 1]
    assert "try_files $uri /index.html;" in spa
    assert 'add_header Cache-Control "no-cache";' in spa
    # /assets/ is matched before the catch-all (prefix locations: longest match wins anyway,
    # but keep the file readable in that order)
    assert site.index("location /assets/") < site.index("location / {")


def test_proxy_block_is_unchanged_from_the_inline_version():
    site = render("false", "80")
    for line in (
        "location ~ ^/(api|v1)/ {",
        "proxy_pass http://127.0.0.1:8000;",
        "proxy_http_version 1.1;",
        "proxy_buffering off;",
        "proxy_read_timeout 3600s;",
        "proxy_set_header Host $host;",
        "proxy_set_header X-Forwarded-Proto http;",
        "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
        "client_max_body_size 64m;",
    ):
        assert line in site, line
    assert "\\$" not in site                                       # heredoc escapes resolved


def test_tls_and_port_and_roots_are_parameters():
    tls = render("true", "8443", "/srv/dist", "8001")
    assert "listen 8443 ssl default_server;" in tls
    assert "ssl_certificate /etc/nginx/launchpad-tls/cert.pem;" in tls
    assert "proxy_set_header X-Forwarded-Proto https;" in tls
    assert tls.count("root /srv/dist;") == 2
    assert "proxy_pass http://127.0.0.1:8001;" in tls
    plain = render("false", "80")
    assert "listen 80 default_server;" in plain and "ssl" not in plain
    assert plain.count("root /home/ubuntu/launchpad/frontend/dist;") == 2


def test_http_lab_keeps_one_proxy_auth_path_and_clears_tls_hsts(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_CONSOLE_HTTP_LAB", "true")
    plain = render("false", "8088")
    tls = render("true", "8443")
    assert "add_header Strict-Transport-Security" not in plain
    assert '"max-age=0"' in tls
    assert "max-age=31536000; includeSubDomains" not in tls
    for site in (plain, tls):
        assert 'proxy_set_header Forwarded "";' in site
        assert "proxy_set_header X-Forwarded-Host $host;" in site
        assert "proxy_set_header X-Forwarded-Port $server_port;" in site
        assert "proxy_pass http://127.0.0.1:8000;" in site
        assert "proxy_buffering off;" in site


def test_bootstrap_host_sources_the_library_instead_of_inlining_the_site():
    body = BOOTSTRAP.read_text(encoding="utf-8")
    assert '. "$(dirname "${BASH_SOURCE[0]}")/nginx-site.sh"' in body
    assert 'render_nginx_site "$LAUNCHPAD_BOOT_TLS" "$LAUNCHPAD_BOOT_CONSOLE_PORT"' in body
    assert "try_files" not in body and "LISTEN_BLOCK" not in body


# ── refresh_nginx_site (host-update step 12; r7-e review follow-up on WB-003) ────────────
# The update-time half runs here against fake `nginx`, `systemctl` and `install` binaries on
# PATH (the fake `install` logs, then delegates to the real one so the .prev / restore
# dance leaves real files to inspect). Every outcome must return 0 — the backend restart
# that follows step 12 is the reason the step may never abort the update.

FAKE_NGINX = """#!/usr/bin/env bash
printf 'nginx %s\\n' "$*" >> "$FAKE_LOG"
case "${FAKE_NGINX_MODE:-ok}" in
  fail) echo "nginx: [emerg] unknown directive" >&2; exit 1 ;;
  fail-new) if grep -q 'location /assets/' "$FAKE_SITE"; then
              echo "nginx: [emerg] rejected rendering" >&2; exit 1; fi ;;
esac
exit 0
"""
FAKE_SYSTEMCTL = """#!/usr/bin/env bash
printf 'systemctl %s\\n' "$*" >> "$FAKE_LOG"
[ "${FAKE_SYSTEMCTL_MODE:-ok}" = fail ] && exit 1
exit 0
"""
FAKE_INSTALL = """#!/usr/bin/env bash
printf 'install %s\\n' "$*" >> "$FAKE_LOG"
case "${FAKE_INSTALL_MODE:-ok}" in
  fail) echo "install: cannot create regular file: Read-only file system" >&2; exit 1 ;;
  fail-restore) if [ "${@: -2:1}" = "${FAKE_SITE}.prev" ]; then   # source = <site>.prev
                  echo "install: cannot create regular file: No space left on device" >&2; exit 1
                fi ;;
esac
exec "$FAKE_REAL_INSTALL" "$@"
"""

OLD_SITE = """server {
  listen 8443 ssl default_server;
    ssl_certificate /etc/nginx/launchpad-tls/cert.pem;
    ssl_certificate_key /etc/nginx/launchpad-tls/key.pem;
  location / {
    root /home/ubuntu/launchpad/frontend/dist;
    try_files $uri /index.html;
  }
}
"""


@pytest.fixture
def fake_host(tmp_path: Path):
    """A scratch /etc/nginx site + fake root-run binaries; returns a runner for the function."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("nginx", FAKE_NGINX), ("systemctl", FAKE_SYSTEMCTL),
                       ("install", FAKE_INSTALL)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    site = tmp_path / "etc-nginx" / "launchpad"
    site.parent.mkdir()
    log = tmp_path / "calls.log"
    dist = tmp_path / "dist"

    def refresh(
        mode: str = "ok", *, dry_run: bool = False,
        install_mode: str = "ok", systemctl_mode: str = "ok",
    ) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAKE_LOG": str(log),
            "FAKE_SITE": str(site),
            "FAKE_NGINX_MODE": mode,
            "FAKE_INSTALL_MODE": install_mode,
            "FAKE_SYSTEMCTL_MODE": systemctl_mode,
            "FAKE_REAL_INSTALL": shutil.which("install") or "/usr/bin/install",
        }
        body = (
            '. "$1"; plan() { printf "  + %s\\n" "$*"; }; refresh_nginx_site "$2" "$3" plan'
            if dry_run else '. "$1"; refresh_nginx_site "$2" "$3"'
        )
        return subprocess.run(["bash", "-c", body, "_", str(LIB), str(site), str(dist)],
                              env=env, capture_output=True, text=True)

    def calls() -> list[str]:
        return log.read_text().splitlines() if log.exists() else []

    return site, dist, refresh, calls


def test_refresh_leaves_an_unchanged_site_alone(fake_host):
    site, dist, refresh, calls = fake_host
    site.write_text(render("true", "8443", str(dist)))
    out = refresh()
    assert out.returncode == 0, out.stderr
    assert "rendered for tls=true port=8443" in out.stdout
    assert "unchanged — nginx not reloaded" in out.stdout
    assert calls() == []                                            # nginx never touched
    assert not (site.parent / "launchpad.prev").exists()


def test_refresh_installs_and_reloads_when_the_site_differs_and_nginx_t_passes(fake_host):
    site, dist, refresh, calls = fake_host
    site.write_text(OLD_SITE)
    out = refresh()
    assert out.returncode == 0, out.stderr
    assert "site differs from the rendered template" in out.stdout
    assert "+  location /assets/ {" in out.stdout                     # the diff is printed
    prev = site.parent / "launchpad.prev"
    assert prev.read_text() == OLD_SITE                              # previous site kept
    assert site.read_text() == render("true", "8443", str(dist))    # rendering installed
    log = calls()
    assert log[0] == "nginx -t"                                      # pre-check on the live config
    assert log[1] == f"install -m 0644 {site} {prev}"
    assert log[2].startswith("install -m 0644 ") and log[2].endswith(f" {site}")
    assert log[3:] == ["nginx -t", "systemctl reload nginx"]
    assert "WARNING" not in out.stderr


def test_refresh_warns_and_returns_0_when_the_reload_fails(fake_host):
    """A stopped nginx passes `nginx -t` (config only) and fails `systemctl reload`; the step
    must warn and return 0 so the backend restart still runs — the installed site is valid."""
    site, dist, refresh, calls = fake_host
    site.write_text(OLD_SITE)
    out = refresh(systemctl_mode="fail")
    assert out.returncode == 0, out.stderr
    assert site.read_text() == render("true", "8443", str(dist))    # rendering stays installed
    assert "WARNING: nginx reload failed" in out.stderr
    assert calls()[-1] == "systemctl reload nginx"


def test_refresh_restores_prev_and_returns_0_when_the_rendering_fails_nginx_t(fake_host):
    site, dist, refresh, calls = fake_host
    site.write_text(OLD_SITE)
    out = refresh("fail-new")                                       # passes before, fails after
    assert out.returncode == 0, out.stderr                          # the restart step still runs
    assert site.read_text() == OLD_SITE                             # restored from .prev
    assert (site.parent / "launchpad.prev").read_text() == OLD_SITE
    assert "WARNING: the rendered nginx site failed nginx -t" in out.stderr
    assert f"restored {site} from {site}.prev" in out.stderr
    log = calls()
    assert log.count("nginx -t") == 2
    assert "systemctl reload nginx" not in log                       # never onto a bad site
    assert log[-1] == f"install -m 0644 {site}.prev {site}"


def test_refresh_skips_with_a_warning_when_nginx_t_already_fails(fake_host):
    site, dist, refresh, calls = fake_host
    site.write_text(OLD_SITE)
    out = refresh("fail")
    assert out.returncode == 0, out.stderr
    assert ("WARNING: nginx config already failing before our change — site refresh skipped"
            in out.stderr)
    assert site.read_text() == OLD_SITE
    assert not (site.parent / "launchpad.prev").exists()
    assert calls() == ["nginx -t"]                                   # looked, did not touch


def test_refresh_dry_run_prints_the_plan_and_writes_nothing(fake_host):
    site, dist, refresh, calls = fake_host
    site.write_text(OLD_SITE)
    out = refresh(dry_run=True)
    assert out.returncode == 0, out.stderr
    plan = [line[4:] for line in out.stdout.splitlines() if line.startswith("  + ")]
    assert plan[0] == "nginx -t"
    assert plan[1] == f"install -m 0644 {site} {site}.prev"
    assert plan[2].startswith("install -m 0644 ") and plan[2].endswith(f" {site}")
    assert plan[3:] == ["nginx -t", "systemctl reload nginx"]
    assert calls() == []                                             # the hook ran everything
    assert site.read_text() == OLD_SITE
    assert not (site.parent / "launchpad.prev").exists()


def test_refresh_skips_a_host_without_a_site_file_or_a_listen_line(fake_host):
    site, dist, refresh, calls = fake_host
    out = refresh()
    assert out.returncode == 0 and "not an nginx host" in out.stdout
    site.write_text("server {\n  # hand-edited\n}\n")
    out = refresh()
    assert out.returncode == 0
    assert "no recognisable 'listen <port>' line" in out.stderr
    assert calls() == []


def test_refresh_warns_and_returns_0_when_the_site_cannot_be_written(fake_host):
    # r7-e item 8: read-only /etc or ENOSPC — a bare failing `install` would end host-update
    # under `set -e` before the backend restart
    site, dist, refresh, calls = fake_host
    site.write_text(OLD_SITE)
    out = refresh(install_mode="fail")
    assert out.returncode == 0, out.stderr
    assert f"WARNING: could not write {site} — nginx not reloaded" in out.stderr
    assert site.read_text() == OLD_SITE                              # nothing installed
    assert not (site.parent / "launchpad.prev").exists()
    log = calls()
    assert log[0] == "nginx -t" and log[1].startswith("install ")   # failed on the .prev copy
    assert len(log) == 2                                             # no second install, no reload


def test_refresh_warns_distinctly_and_returns_0_when_the_restore_fails(fake_host):
    site, dist, refresh, calls = fake_host
    site.write_text(OLD_SITE)
    out = refresh("fail-new", install_mode="fail-restore")   # rendering rejected, restore fails
    assert out.returncode == 0, out.stderr
    assert f"WARNING: restore of {site} from {site}.prev FAILED" in out.stderr
    assert "may be invalid; fix before the next nginx reload" in out.stderr
    assert "restored" not in out.stderr.replace("restore of", "")   # not the success wording
    assert site.read_text() == render("true", "8443", str(dist))    # the rejected site stays
    assert (site.parent / "launchpad.prev").read_text() == OLD_SITE
    log = calls()
    assert "systemctl reload nginx" not in log
    assert log[-1] == f"install -m 0644 {site}.prev {site}"


# ── N12-obs60: browser security headers, HSTS on the SPA, server_tokens, TLS ciphers ──────
# Security scan 2026-10-17 rows T-1..T-3 and T-5. nginx `add_header` inside a location replaces
# the server-level set, so every location must carry the whole block, and `always` keeps it on
# 401 / 404 / 502. The CSP is enforced (not report-only) and explicit — each source below is a
# need read from the bundle (Monaco from jsdelivr, DCV WebAssembly + blob workers, video CDN,
# presigned wss:// live-view stream); see the directive table in infra/ec2/nginx-site.sh.

SECURITY_HEADERS = (
    "add_header X-Frame-Options DENY always;",
    "add_header X-Content-Type-Options nosniff always;",
    "add_header Referrer-Policy strict-origin-when-cross-origin always;",
    'add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;',
)
HSTS = 'add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;'


def _locations(site: str) -> dict[str, str]:
    """{location selector: block body} for the four top-level locations (incl. r13-e's /media/)."""
    out: dict[str, str] = {}
    for selector in ("location ~ ^/(api|v1)/ {", "location /assets/ {", "location /media/ {",
                     "location / {"):
        body = site[site.index(selector) + len(selector):]
        out[selector] = body[: body.index("\n  }")]
    return out


def _csp(block: str) -> dict[str, list[str]]:
    line = next(ln for ln in block.splitlines() if "Content-Security-Policy" in ln)
    assert line.strip().startswith('add_header Content-Security-Policy "'), line
    assert line.rstrip().endswith('" always;'), line                 # enforced, on every status
    assert "Report-Only" not in line
    policy = line[line.index('"') + 1: line.rindex('"')]
    directives = {}
    for part in policy.split(";"):
        name, *sources = part.split()
        directives[name] = sources
    return directives


@pytest.mark.parametrize("tls", ["true", "false"])
def test_every_location_carries_the_security_headers_on_every_status(tls):
    site = render(tls, "8443")
    blocks = _locations(site)
    assert len(blocks) == 4
    # r13-e's /media/ carries its own Cache-Control add_header, which would otherwise suppress
    # the inherited set — the block must be rendered there too
    media = blocks["location /media/ {"]
    assert "Content-Security-Policy" in media
    if tls == "true":
        assert HSTS in media
    for selector, block in blocks.items():
        for header in SECURITY_HEADERS:
            assert header in block, (selector, header)
        csp = _csp(block)
        assert csp["frame-ancestors"] == ["'none'"]                 # clickjacking twin of DENY
        assert csp["object-src"] == ["'none'"]
        assert csp["base-uri"] == ["'self'"]
        assert csp["form-action"] == ["'self'"]
        assert csp["default-src"] == ["'self'"]
    # one CSP per location, byte-identical everywhere (one policy, not four drifting ones)
    policies = {str(_csp(block)) for block in blocks.values()}
    assert len(policies) == 1
    # the server block itself has none — a server-level add_header would be silently shadowed
    head = site[: site.index("location ~")]
    assert "add_header" not in head


@pytest.mark.parametrize("tls", ["true", "false"])
def test_csp_allows_what_the_bundle_needs_and_nothing_wider(tls):
    csp = _csp(_locations(render(tls, "8443"))["location / {"])
    # scripts: Vite chunks, DCV's WebAssembly decoders, Monaco from jsdelivr — but never eval or
    # inline scripts (index.html has none) and never a bare wildcard
    assert csp["script-src"] == ["'self'", "'wasm-unsafe-eval'", "https://cdn.jsdelivr.net"]
    assert "'unsafe-eval'" not in csp["script-src"] and "'unsafe-inline'" not in csp["script-src"]
    # Monaco / xyflow / Cloudscape / DCV inject <style> elements → inline styles are the one
    # concession; Monaco's own CSS and codicon font come from the CDN
    assert csp["style-src"] == ["'self'", "'unsafe-inline'", "https://cdn.jsdelivr.net"]
    assert csp["font-src"] == ["'self'", "data:", "https://cdn.jsdelivr.net"]
    # the video library streams from an external CDN (posters are images, captions are media)
    assert "https:" in csp["media-src"] and "https:" in csp["img-src"]
    assert "blob:" in csp["img-src"]                                 # screenshots / canvases
    # same-origin fetch + SSE, DCV presigned streams, and embedded WASM decoder fetches.
    # data: is not an external network destination; script/worker policies remain narrow.
    assert csp["connect-src"] == ["'self'", "https:", "wss:", "data:"]
    assert csp["worker-src"] == ["'self'", "blob:"]                  # DCV workers, Monaco workers
    assert csp["frame-src"] == ["'none'"]                            # the console frames nothing
    for name, sources in csp.items():
        assert "*" not in sources, name
        assert not any(s.startswith("http:") for s in sources), name  # nothing over plain HTTP


def test_hsts_is_emitted_by_nginx_on_the_tls_site_only_and_exactly_once_per_response():
    tls = render("true", "8443")
    blocks = _locations(tls)
    for selector, block in blocks.items():
        assert block.count("Strict-Transport-Security") == (2 if "api" in selector else 1), selector
        assert HSTS in block, selector
    # the backend's own hsts middleware still answers /api — nginx hides that copy so the response
    # carries nginx's header once, not two identical ones
    assert "proxy_hide_header Strict-Transport-Security;" in blocks["location ~ ^/(api|v1)/ {"]
    plain = render("false", "80")
    assert "add_header Strict-Transport-Security" not in plain  # never pin an HTTP origin
    assert "proxy_hide_header Strict-Transport-Security;" in plain
    assert "max-age=31536000; includeSubDomains" in HSTS and "preload" not in HSTS


@pytest.mark.parametrize("tls", ["true", "false"])
def test_server_tokens_off_hides_the_nginx_version(tls):
    site = render(tls, "8443")
    head = site[: site.index("location ~")]
    assert "server_tokens off;" in head


def test_tls_site_offers_tls12_and_13_with_ecdhe_aead_suites_only():
    tls = render("true", "8443")
    head = tls[: tls.index("location ~")]
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in head
    assert "ssl_prefer_server_ciphers on;" in head
    line = next(ln for ln in head.splitlines() if ln.strip().startswith("ssl_ciphers "))
    suites = line.strip()[len("ssl_ciphers "):].rstrip(";").split(":")
    assert len(suites) == 6
    for suite in suites:
        # forward secrecy (ECDHE) + AEAD (GCM / ChaCha20) — no CBC, no static RSA, no ARIA/Camellia
        assert (
            suite.startswith(("ECDHE-ECDSA-", "ECDHE-RSA-")) and
            suite.endswith(("-GCM-SHA256", "-GCM-SHA384", "CHACHA20-POLY1305"))
        ), suite
    assert len(set(suites)) == 6
    # the plain-HTTP variant (TlsMode=none) carries no TLS directive at all
    plain = render("false", "80")
    assert "ssl_" not in plain


@pytest.mark.parametrize("tls", ["true", "false"])
def test_rendered_site_passes_nginx_t_when_a_local_nginx_is_available(tls, tmp_path: Path):
    """Not a stub: the real nginx binary parses the rendering (`always`, the cipher string, the
    quoted CSP). Skipped where nginx / openssl are not installed; the gate hosts have both."""
    if shutil.which("nginx") is None or shutil.which("openssl") is None:
        pytest.skip("nginx or openssl not installed")
    tls_dir = tmp_path / "tls"
    tls_dir.mkdir()
    subprocess.run(
        ["openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048", "-days", "1",
         "-keyout", str(tls_dir / "key.pem"), "-out", str(tls_dir / "cert.pem"),
         "-subj", "/CN=test"], check=True, capture_output=True,
    )
    site = render(tls, "18443", str(tmp_path / "dist"), "18000")
    site = site.replace("/etc/nginx/launchpad-tls", str(tls_dir))
    (tmp_path / "site.conf").write_text(site)
    (tmp_path / "logs").mkdir()
    conf = tmp_path / "nginx.conf"
    conf.write_text(
        f"pid {tmp_path}/nginx.pid;\nerror_log {tmp_path}/logs/error.log;\nevents {{}}\n"
        f"http {{\n  access_log off;\n  include {tmp_path}/site.conf;\n}}\n"
    )
    out = subprocess.run(["nginx", "-t", "-p", str(tmp_path), "-c", str(conf)],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "test is successful" in out.stderr


def test_bootstrap_host_generates_the_cert_through_tls_cert_sh_with_an_ip_san():
    body = BOOTSTRAP.read_text(encoding="utf-8")
    assert "openssl req" not in body                               # no inline, SAN-less cert
    assert 'tls-cert.sh" --out-dir /etc/nginx/launchpad-tls' in body
    assert '--san "IP:${PUBLIC_IP}"' in body

# ── /media/ — self-hosted demo-video media (N13-obs11, user decision 2026-10-17: no CDN) ──
# The library's recordings are static files under /home/ubuntu/launchpad-media (OUTSIDE the
# checkout), served by nginx on the console port. The block must stay static-only, listing-
# free, immutable-cached, typed for <video>/<track>, and must not disable byte ranges.

HOST_UPDATE = Path(__file__).resolve().parents[2] / "scripts" / "host-update.sh"


def _media_block(site: str) -> str:
    block = site[site.index("location /media/ {"):]
    return block[: block.index("\n  }") + 4]


def test_media_block_serves_the_host_directory_as_immutable_static_files():
    site = render("false", "80")
    media = _media_block(site)
    assert "alias /home/ubuntu/launchpad-media/;" in media            # outside the checkout
    assert "autoindex off;" in media
    assert 'add_header Cache-Control "public, max-age=31536000, immutable";' in media
    for line in ("video/mp4 mp4;", "video/webm webm;", "text/vtt vtt;", "image/jpeg jpg jpeg;"):
        assert line in media, line                                     # explicit MIME map
    assert "default_type application/octet-stream;" in media
    # static files only: no proxy, no SPA fallback, no CORS (same origin), ranges untouched
    for forbidden in ("proxy_pass", "try_files", "index.html", "Access-Control-Allow-Origin",
                      "max_ranges", "root "):
        assert forbidden not in media, forbidden
    # the media alias never points into the checkout dist/ root
    assert "/home/ubuntu/launchpad/" not in media
    # prefix order: after the hashed assets, before the catch-all
    assert site.index("location /assets/") < site.index("location /media/") < site.index(
        "location / {")


def test_media_root_is_a_parameter_normalised_to_a_trailing_slash():
    assert "alias /srv/media/;" in _media_block(render("false", "80", "/srv/dist", "8000",
                                                       "/srv/media"))
    assert "alias /srv/media/;" in _media_block(render("false", "80", "/srv/dist", "8000",
                                                       "/srv/media/"))
    # the defaults every host renders with (refresh_nginx_site passes three arguments)
    assert "alias /home/ubuntu/launchpad-media/;" in _media_block(render("true", "8443",
                                                                         "/srv/dist"))
    # the other two static roots are unchanged by the new parameter
    assert render("false", "80", "/srv/dist").count("root /srv/dist;") == 2


def test_rendered_site_passes_a_real_nginx_t(tmp_path: Path):
    """Ubuntu's mime.types has no text/vtt, so the /media/ block carries its own `types {}`
    map — nginx must accept that grammar. Runs the real binary when one is installed."""
    nginx = shutil.which("nginx") or ("/usr/sbin/nginx" if Path("/usr/sbin/nginx").exists()
                                      else None)
    if nginx is None:
        pytest.skip("nginx not installed")
    # `nginx -t` really opens the listen socket, so the test port must be free and unprivileged
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = str(probe.getsockname()[1])
    (tmp_path / "site.conf").write_text(render("false", port, str(tmp_path / "dist")))
    conf = tmp_path / "nginx.conf"
    conf.write_text(
        f"pid {tmp_path}/nginx.pid;\nerror_log {tmp_path}/error.log;\nevents {{}}\n"
        f"http {{ access_log off; include {tmp_path}/site.conf; }}\n"
    )
    result = subprocess.run([nginx, "-t", "-p", str(tmp_path), "-c", str(conf)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "syntax is ok" in result.stderr and "test is successful" in result.stderr


def test_bootstrap_provisions_the_media_dir_outside_the_checkout_and_host_update_never_touches_it():
    body = BOOTSTRAP.read_text(encoding="utf-8")
    assert "install -d -m 0755 -o ubuntu -g ubuntu /home/ubuntu/launchpad-media" in body
    # created after the 0711 traversal grant on the home (www-data must reach it)
    assert body.index("chmod 0711 /home/ubuntu") < body.index("/home/ubuntu/launchpad-media")
    # the update script rebuilds/chowns only paths under the checkout — the media directory
    # must survive every update, so it may not appear there at all
    update = HOST_UPDATE.read_text(encoding="utf-8")
    assert "launchpad-media" not in update
    owned = re.search(r"^OWNED_PATHS=\((.*)\)$", update, re.M)
    assert owned, "OWNED_PATHS array not found in host-update.sh"
    assert all(not p.startswith("/") for p in owned.group(1).split()), owned.group(1)


def test_backend_unit_honours_nginx_forwarded_for_so_the_peer_is_the_real_client():
    """r12-g (rev-r11): the per-peer login / register / `/v1` windows key on
    `request.client.host`. nginx must forward the client address and uvicorn must be told
    to trust it from loopback only, otherwise every caller is 127.0.0.1 and the windows
    collapse into one global bucket."""
    unit = BOOTSTRAP.read_text(encoding="utf-8")
    exec_start = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    assert "--host 127.0.0.1" in exec_start
    assert "--proxy-headers" in exec_start
    assert "--forwarded-allow-ips=127.0.0.1" in exec_start
    for tls in ("selfsigned", "off"):
        rendered = render(tls, "8443")
        assert "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;" in rendered
