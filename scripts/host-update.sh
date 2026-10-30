#!/usr/bin/env bash
# Update an existing Launchpad-CN host in place — the code-only path.
# N11 r3-B (obs18/obs20/obs21/obs22/obs24); docs/cn/interfaces/deploy.md §8.
#
#   sudo scripts/host-update.sh [--root <checkout>] [--branch <name>] [--with-cdk]
#                               [--verify] [--full] [--no-restart] [--fix-ownership]
#                               [--dry-run] [--help]
#
# N11-obs36: a fast-forward must never rewrite the EXECUTING file, so a NEW version of
# this script is run from a copy outside the checkout (deploy.md §8.1 "copy-then-run").
# The checkout it operates on is, in order of precedence: --root, $LAUNCHPAD_ROOT, the
# parent of this script's own directory (unchanged default for an in-checkout run).
#
# Runs on the EC2 host, as root — but root is used ONLY for systemctl/journalctl and for
# reading /etc/launchpad/env. Every step that writes under the checkout (uv sync, npm,
# the bootstrap, cdk, make verify) runs as the CHECKOUT OWNER through `as_owner_env`
# (`runuser -m`: the sourced environment travels, nothing on a command line), exactly as
# the stack's UserData ran the first bootstrap — N12-bug10: a root-run update left
# data/skill-lab-venv, backend/.venv, frontend/dist, backend/app/**/__pycache__ and the
# agent build dirs root-owned and the backend service (User=<owner>) failed its next
# repair with PermissionError. git talks to GitHub as the owner with the deploy key
# UserData installed. Every step is idempotent; the service keeps running until the
# restart at the end.
#
#   1. preflight    root, the checkout (--root / LAUNCHPAD_ROOT / own path), uv,
#                   /etc/launchpad/env
#   2. ledger       data/launchpad.db -> data/launchpad.db.bak-<UTC stamp>   (ALWAYS first)
#   3. lock drift   `git checkout -- backend/uv.lock infra/uv.lock` — obs21: a host-side
#                   `uv sync` rewrote the committed locks on every run; any OTHER local
#                   change aborts the update (resolve it by hand)
#   4. fetch        `git fetch --prune origin <branch>` under a LONG timeout (1800 s, 3
#                   attempts) — obs20: a 33 MB push at ~115 KB/s takes ~6 min; a 300 s
#                   timeout restarted from zero every retry and rebuilt the OLD commit
#   5. pull         `git merge --ff-only origin/<branch>` — never a merge commit on a host.
#                   N11-obs55: then `git diff --name-only <old> <new> -- ${CDK_PATHS[@]}`; a
#                   non-empty result without --with-cdk prints a WARNING (stdout + stderr),
#                   is repeated in step 9 and in the final summary (`cdk: pending (<n> files)`)
#                   — never an abort, the rest of the update is valid. A dry run plans
#                   against the fetched origin/<branch> tip so it reports the same set
#   6. deps        N17-obs485: `scripts/uv_lock_fresh.py` first — the committed uv.lock
#                   must still match pyproject.toml (`--frozen` never checks, and
#                   `uv lock --check` cannot run on a host: it resolves against the env's
#                   mirror index); a stale lock aborts the update. Then `uv sync --frozen` in
#                   backend/ and infra/ (installs exactly the committed lock and never
#                   rewrites it); frontend `npm ci` when package-lock.json changed — as the
#                   checkout owner
#   7. build        `npm run build` whenever frontend/dist/.launchpad-build (the commit the
#                   dist was built from, written after every build) is missing, names an
#                   unknown commit, or frontend/ differs between it and HEAD (or --full) —
#                   N11-r3-bug8: deciding from the ff range alone kept a dist/ built from an
#                   OLDER pin and the restart would have served a stale UI. Then writes
#                   data/build-info.json {sha, built_at, branch, dist_built_from} — what
#                   `GET /api/health` serves as `build` (N13-obs10)
#   8. verify       optional `make verify` (--verify), with the region env unset as the
#                   prod runbook requires
#   9. cdk          ONLY with --with-cdk: explicit `cdk deploy --all` with the context from
#                   LAUNCHPAD_CDK_CONTEXT (+ env_name from LAUNCHPAD_ENV_NAME) — obs18:
#                   `make bootstrap` never re-deploys a stack that already exists. Prints
#                   `cdk: <n> CDK-owned files changed — deploying` / `cdk: no CDK-owned
#                   changes — deploy is a no-op` first (obs55). Before the deploy,
#                   `scripts/bootstrap.py --check-memory-admin` (N11-obs44b): aborts when
#                   this host's ops role cannot assume launchpad-memory-admin-<region>
#                   (pre-D17 EC2 template without `MemoryAdminAssume` — deploy.md §8.3)
#                   Every `uv run` here and in step 10 is `uv run --frozen` (N17-obs485: a
#                   plain `uv run` re-locks against the env's CN index mirror and leaves the
#                   checkout's uv.lock rewritten)
#  10. bootstrap    `scripts/bootstrap.py --skip-cdk` as the checkout owner (idempotent;
#                   prints reused/created)
#  11. ownership    N12-bug10: lists anything under data/, config/, backend/.venv,
#                   infra/.venv, frontend/dist, frontend/node_modules, backend/app, the
#                   owner's ~/.cache/uv, ~/.cache/npm, ~/.npm (N11-r5-bug4; the home is
#                   LAUNCHPAD_OWNER_HOME or getent's) or the legacy /tmp/launchpad_build_*
#                   not owned by the checkout owner — a WARNING, never an abort.
#                   N11-r5-bug5: a `.tmp*` entry younger than 60 s (a concurrent uv/npm
#                   still writing it) is not a stray, and every candidate is stat'ed again
#                   before it counts, so one that vanished is never reported.
#                   `restore_ownership` (chown -R of a path only when it holds a stray)
#                   runs as a safety net after steps 7 and 10; hosts updated by the
#                   pre-r4-G script carry root-owned venvs that would make the owner-run
#                   steps fail, so step 1 refuses until `--fix-ownership` runs the same
#                   repair once, up front
#  12. nginx        WB-003: `refresh_nginx_site` from infra/ec2/nginx-site.sh — the SAME
#                   file bootstrap-host.sh rendered on first boot — re-renders
#                   /etc/nginx/sites-available/launchpad (TLS and port read from the live
#                   site's `listen` line, dist root = this checkout's frontend/dist) and
#                   installs + `nginx -t` + `systemctl reload nginx` ONLY when the rendered
#                   file differs from the one nginx runs: an unchanged host never touches
#                   nginx; a host whose site predates the /assets/ 404 + immutable-cache rule
#                   picks it up here. The step NEVER aborts the update (r7-e): a config that
#                   already fails `nginx -t` is skipped with a WARNING, a rendering nginx
#                   rejects is rolled back to <site>.prev with a WARNING — either way step 13
#                   still restarts the backend (an abort here left the OLD backend serving
#                   the NEW dist). Skipped (with a note) when the checkout has no
#                   infra/ec2/nginx-site.sh / no refresh_nginx_site, or the host has no site
#                   file (not an nginx host). LAUNCHPAD_NGINX_SITE overrides the path (tests)
# 12b. public origin N11-obs47: /etc/launchpad/env caches LAUNCHPAD_PUBLIC_BASE_URL from the
#                   first boot; an Elastic IP association (AllocateElasticIp=true, deploy.md
#                   §8.2) or a stop/start without one changes the public IPv4 and the 3LO
#                   return URL goes stale. Re-reads IMDSv2 public-ipv4 (the EIP when one is
#                   associated) and rewrites the line when its host is an IPv4 literal that
#                   differs; a hostname (operator override) is never touched
# 12c. uv frozen   N17-obs485: the env file gets `UV_FROZEN=1` (appended once) and every
#                   `ExecStart=… uv run uvicorn` in the backend unit or its drop-ins becomes
#                   `uv run --frozen uvicorn` (then `systemctl daemon-reload`), so neither the
#                   service nor an operator's `uv run` re-locks against the mirror. Idempotent;
#                   applied before the restart. The first-boot UserData path
#                   (infra/ec2/bootstrap-host.sh) is unchanged — parity at the next AMI /
#                   instance refresh
#  13. restart      `systemctl restart launchpad-backend` (+ launchpad-frontend if present)
#  14. health       poll /api/health, then count error/traceback lines in the journal
#
# Named environments migrating to the shared OpenSearch domain (D17, obs24) MUST pass
# --with-cdk: steps 9 → 10 → 13 is the required order (cdk, then the memory bootstrap
# stage, then — and only then — the backend restart). See deploy.md §8.3.
set -Eeuo pipefail

SELF_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT_ARG=""                      # --root wins over LAUNCHPAD_ROOT, which wins over SELF_ROOT
BRANCH=""
WITH_CDK=0
VERIFY=0
FULL=0
RESTART=1
FIX_OWNERSHIP=0
DRY_RUN=0
FETCH_TIMEOUT="${LAUNCHPAD_FETCH_TIMEOUT:-1800}"
FETCH_ATTEMPTS="${LAUNCHPAD_FETCH_ATTEMPTS:-3}"
HEALTH_TIMEOUT="${LAUNCHPAD_HEALTH_TIMEOUT:-180}"
ENV_FILE="${LAUNCHPAD_ENV_FILE:-/etc/launchpad/env}"
BACKEND_UNIT="${LAUNCHPAD_BACKEND_UNIT:-launchpad-backend}"
FRONTEND_UNIT="${LAUNCHPAD_FRONTEND_UNIT:-launchpad-frontend}"
NGINX_SITE="${LAUNCHPAD_NGINX_SITE:-/etc/nginx/sites-available/launchpad}"
SYSTEMD_DIR="${LAUNCHPAD_SYSTEMD_DIR:-/etc/systemd/system}"

usage() {
  cat <<'EOF'
usage: sudo scripts/host-update.sh [options]

  --root <checkout> the Launchpad checkout to update (default: $LAUNCHPAD_ROOT, else the
                    parent of this script's directory). Lets a NEW copy of the script run
                    from outside the checkout — a fast-forward must never rewrite the
                    executing file (N11-obs36; deploy.md §8.1 copy-then-run)
  --branch <name>   branch to fast-forward to (default: the checked-out branch)
  --with-cdk        also run `cdk deploy --all` for pulled CDK-owned changes (the CDK app
                    under infra/ and the Lambda asset dirs it packages — CDK_PATHS in this
                    script, deploy.md §8.1) and for the D17 shared-domain migration of a
                    named environment, before bootstrap. Without it such changes are
                    detected and reported (WARNING + `cdk: pending` in the summary) but
                    NOT applied (N11-obs55)
  --verify          run `make verify` before restarting
  --full            force `npm ci` + `npm run build` even when dist/ matches HEAD
  --no-restart      stop after bootstrap; do not restart the services
  --fix-ownership   one-time repair: chown -R <owner> every checkout path (and legacy
                    /tmp/launchpad_build_*) that still holds root-owned files from an
                    update made by the pre-N12-bug10 script; without it such files abort
                    step 1 with the hint to pass this flag
  --dry-run         print the commands instead of running them (no root needed)
  --help            this text

env: LAUNCHPAD_ROOT (the checkout; --root wins) · LAUNCHPAD_FETCH_TIMEOUT (1800 s) ·
     LAUNCHPAD_FETCH_ATTEMPTS (3) · LAUNCHPAD_HEALTH_TIMEOUT (180 s) ·
     LAUNCHPAD_ENV_FILE (/etc/launchpad/env) · UV ·
     LAUNCHPAD_NGINX_SITE (/etc/nginx/sites-available/launchpad — the site step 12 re-renders;
     a path that does not exist skips the step) ·
     LAUNCHPAD_OWNER_HOME (the checkout owner's home whose ~/.cache/uv, ~/.cache/npm and
     ~/.npm the ownership check scans; default: the owner's passwd entry — a test/ops knob,
     N11-r5-bug5: the hermetic dry-run tests point it at a scratch directory so they never
     read the live home) ·
     LAUNCHPAD_PUBLIC_IP / LAUNCHPAD_IMDS_ENDPOINT (step 12b, N11-obs47: override the public
     IPv4 / redirect the IMDSv2 public-ipv4 probe that refreshes LAUNCHPAD_PUBLIC_BASE_URL —
     test/ops knobs; the hermetic dry-run tests never reach 169.254.169.254) ·
     LAUNCHPAD_SYSTEMD_DIR (/etc/systemd/system — where step 12c looks for the backend unit
     and its drop-ins; a test/ops knob)
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --root) ROOT_ARG="${2:-}"; [ -n "$ROOT_ARG" ] || { echo "--root needs a value" >&2; exit 2; }; shift 2 ;;
    --branch) BRANCH="${2:-}"; [ -n "$BRANCH" ] || { echo "--branch needs a value" >&2; exit 2; }; shift 2 ;;
    --with-cdk) WITH_CDK=1; shift ;;
    --verify) VERIFY=1; shift ;;
    --full) FULL=1; shift ;;
    --no-restart) RESTART=0; shift ;;
    --fix-ownership) FIX_OWNERSHIP=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

log() { printf '\n── %s\n' "$*"; }
die() { printf 'host-update: %s\n' "$*" >&2; exit 1; }
plural() { if [ "$1" = 1 ]; then printf '%s %s' "$1" "$2"; else printf '%s %ss' "$1" "$2"; fi; }

# N11-obs55: the paths only a `cdk deploy` applies — the CDK app itself and every directory
# it packages as a Lambda asset (`Code.from_asset` in infra/stacks/*.py: the memory extractor
# from backend/lambdas/memory_extractor, the two sample Lambdas from samples/). A routine
# update pulled a handler.py change and left the deployed extractor on the old code because
# nothing but --with-cdk redeploys an asset. NOT CDK-owned, and deliberately absent: infra/ec2/
# (the EC2 template, applied with CloudFormation from outside the host — deploy.md §8.2 — and
# the UserData/nginx scripts the host runs directly), infra/spoke/ (the spoke-account
# CloudFormation template) and infra/tests/. This list is the single source of truth; the
# "CDK-owned paths and --with-cdk" paragraph of deploy.md §8.1 mirrors it (test-enforced).
CDK_PATHS=(
  infra/app.py
  infra/cdk.json
  infra/pyproject.toml
  infra/uv.lock
  infra/stacks
  backend/lambdas
  samples/hr_database_lambda
  samples/rest_api
)
run() {
  if [ "$DRY_RUN" = 1 ]; then printf '  + %s\n' "$*"; else "$@"; fi
}

# N11-obs47: the console's public origin. IMDS `public-ipv4` returns the address the
# instance is reachable at — the Elastic IP once one is associated (the association
# supersedes the auto-assigned address) — so the same probe bootstrap-host.sh used on the
# first boot is the source here. LAUNCHPAD_PUBLIC_IP overrides the probe (a test/ops knob:
# the hermetic dry-run tests never reach 169.254.169.254); LAUNCHPAD_IMDS_ENDPOINT points
# the probe elsewhere. Off EC2 (or without a public address) the probe is empty and the
# step is skipped — never a failure: the probe is bounded by --max-time.
IMDS_ENDPOINT="${LAUNCHPAD_IMDS_ENDPOINT:-http://169.254.169.254}"
imds_public_ipv4() {   # stdout: current public IPv4 (the EIP when associated), empty when unknown
  if [ -n "${LAUNCHPAD_PUBLIC_IP:-}" ]; then printf '%s' "$LAUNCHPAD_PUBLIC_IP"; return 0; fi
  local token
  token="$(curl --fail --silent --max-time 2 --request PUT \
    --header 'X-aws-ec2-metadata-token-ttl-seconds: 300' \
    "$IMDS_ENDPOINT/latest/api/token" 2>/dev/null)" || return 0
  curl --fail --silent --max-time 2 --header "X-aws-ec2-metadata-token: $token" \
    "$IMDS_ENDPOINT/latest/meta-data/public-ipv4" 2>/dev/null || true
}
refresh_public_base_url() {   # refresh_public_base_url <env-file>: rewrite an IPv4 origin that drifted
  local env_file="$1" current console_entry scheme rest host port_part public_ip new
  if [ ! -r "$env_file" ]; then
    echo "  skipped — no readable $env_file"; return 0
  fi
  current="$(grep -E '^LAUNCHPAD_PUBLIC_BASE_URL=' "$env_file" | tail -1 | cut -d= -f2- || true)"
  if [ -z "$current" ]; then
    echo "  skipped — $env_file has no LAUNCHPAD_PUBLIC_BASE_URL line (set elsewhere, e.g. a systemd drop-in)"
    return 0
  fi
  # install_policy_host.sh records which entry owns this origin. An NLB's static IP
  # deliberately differs from IMDS's host IP; only direct entries follow the host.
  console_entry="$(grep -E '^LAUNCHPAD_CONSOLE_ENTRY=' "$env_file" | tail -1 | cut -d= -f2- || true)"
  if [ "$console_entry" = public_nlb ]; then
    echo "  kept — $current: public_nlb origin is independent of the host IP"
    return 0
  fi
  scheme="${current%%://*}"
  rest="${current#*://}"
  host="${rest%%:*}"; host="${host%%/*}"
  if ! printf '%s' "$host" | grep -qE '^[0-9]{1,3}(\.[0-9]{1,3}){3}$'; then
    echo "  kept — $current: host is not an IPv4 literal (operator-set origin, deploy.md §3)"
    return 0
  fi
  public_ip="$(imds_public_ipv4)"
  if [ -z "$public_ip" ]; then
    echo "  skipped — IMDS reports no public IPv4 (not on EC2, or no public address); $current kept"
    return 0
  fi
  if [ "$host" = "$public_ip" ]; then
    echo "  unchanged — $current matches IMDS public-ipv4"
    return 0
  fi
  port_part="${rest#"$host"}"
  new="$scheme://$public_ip$port_part"
  echo "  $current -> $new (public IPv4 changed: Elastic IP association or stop/start, N11-obs47)"
  run sed -i "s|^LAUNCHPAD_PUBLIC_BASE_URL=.*|LAUNCHPAD_PUBLIC_BASE_URL=$new|" "$env_file"
  echo "  note: the 3LO return URL is now $new/auth/return — re-register it with every OAuth2 provider used by as-user tools"
}

# N17-obs485: a `uv run` without --frozen re-locks first and records the index it resolved
# against — the env file's CN mirror (UV_DEFAULT_INDEX), not the committed lock's pypi.org —
# so every restart / bootstrap / operator `uv run` left backend/uv.lock (and infra/uv.lock)
# rewritten in the checkout, hiding a real local edit in `git status`. UV_FROZEN=1 in the env
# file and `--frozen` in the unit's ExecStart stop it at the source; step 3 still discards
# what an older host left behind.
ensure_uv_frozen_env() {   # ensure_uv_frozen_env <env-file>: exactly one effective UV_FROZEN=1
  local env_file="$1" current
  if [ ! -r "$env_file" ]; then
    echo "  env: skipped — no readable $env_file"; return 0
  fi
  current="$(grep -E '^UV_FROZEN=' "$env_file" | tail -1 | cut -d= -f2- || true)"
  if [ "$current" = 1 ]; then
    echo "  env: unchanged — UV_FROZEN=1"
  elif [ -n "$current" ]; then
    echo "  env: UV_FROZEN=$current -> 1"
    run sed -i "s|^UV_FROZEN=.*|UV_FROZEN=1|" "$env_file"
  else
    echo "  env: + UV_FROZEN=1 ($env_file)"
    # keep a last line without a newline intact, then append (>> keeps the 0600 mode)
    run sh -c '[ -z "$(tail -c 1 "$1")" ] || printf "\n" >> "$1"; printf "UV_FROZEN=1\n" >> "$1"' _ "$env_file"
  fi
}
ensure_uv_frozen_unit() {   # ensure_uv_frozen_unit <unit>: `uv run --frozen uvicorn` in ExecStart
  local unit="$1" f stale=() frozen=0
  for f in "$SYSTEMD_DIR/$unit.service" "$SYSTEMD_DIR/$unit.service.d"/*.conf; do
    [ -f "$f" ] || continue
    if grep -qE '^ExecStart=.*[/ ]uv run uvicorn' "$f"; then stale+=("$f"); fi
    if grep -qE '^ExecStart=.*[/ ]uv run --frozen uvicorn' "$f"; then frozen=1; fi
  done
  if [ "${#stale[@]}" -eq 0 ]; then
    if [ "$frozen" = 1 ]; then echo "  unit: unchanged — ExecStart already runs \`uv run --frozen uvicorn\`"
    else echo "  unit: skipped — no ExecStart running \`uv run uvicorn\` in $SYSTEMD_DIR/$unit.service[.d]"
    fi
    return 0
  fi
  for f in "${stale[@]}"; do
    echo "  unit: $f: uv run uvicorn -> uv run --frozen uvicorn"
    run sed -i -E 's#^(ExecStart=.*[/ ]uv) run uvicorn#\1 run --frozen uvicorn#' "$f"
  done
  run systemctl daemon-reload
}

# ── 1. preflight ────────────────────────────────────────────────────────────────
# N11-obs36: resolve the checkout before anything else — flag, then env, then own path.
if [ -n "$ROOT_ARG" ]; then ROOT_SRC="--root"; ROOT_RAW="$ROOT_ARG"
elif [ -n "${LAUNCHPAD_ROOT:-}" ]; then ROOT_SRC="LAUNCHPAD_ROOT"; ROOT_RAW="$LAUNCHPAD_ROOT"
else ROOT_SRC="script location"; ROOT_RAW="$SELF_ROOT"
fi
[ -d "$ROOT_RAW" ] || die "$ROOT_RAW ($ROOT_SRC) is not a directory"
ROOT="$(cd "$ROOT_RAW" && pwd)"
log "1/14 preflight ($ROOT · from $ROOT_SRC)"
cd "$ROOT"
[ -e "$ROOT/.git" ] || die "$ROOT ($ROOT_SRC) is not a git checkout — pass --root <checkout> (or set LAUNCHPAD_ROOT) when running a copy of this script from outside it"
if [ "$ROOT_SRC" != "script location" ] && [ "$SELF_ROOT" = "$ROOT" ]; then
  echo "  (this script is itself inside the checkout; a fast-forward may rewrite it — prefer the copy-then-run procedure, deploy.md §8.1)"
fi
if [ "$DRY_RUN" != 1 ] && [ "$(id -u)" != 0 ]; then
  die "run as root (sudo): systemctl, $ENV_FILE and the bootstrap need it"
fi
OWNER="$(stat -c %U "$ROOT")"
OWNER_UID="$(stat -c %u "$ROOT")"
# N11-r5-bug5: LAUNCHPAD_OWNER_HOME overrides the passwd lookup — a test/ops knob so the
# hermetic dry-run tests scan a scratch home instead of the live one (deploy.md §8.1).
if [ -n "${LAUNCHPAD_OWNER_HOME:-}" ]; then
  OWNER_HOME="$LAUNCHPAD_OWNER_HOME"
else
  OWNER_HOME="$(getent passwd "$OWNER" | cut -d: -f6)"
  [ -n "$OWNER_HOME" ] || OWNER_HOME="$HOME"
fi
OWNER_GROUP="$(id -gn "$OWNER" 2>/dev/null || echo "$OWNER")"
export HOME="$OWNER_HOME"   # uv / npm caches where UserData left them


UV="${UV:-}"
if [ -z "$UV" ]; then
  for cand in "$(command -v uv || true)" /usr/local/bin/uv "$OWNER_HOME/.local/bin/uv"; do
    if [ -n "$cand" ] && [ -x "$cand" ]; then UV="$cand"; break; fi
  done
fi
[ -n "$UV" ] || die "uv not found (PATH, /usr/local/bin/uv, $OWNER_HOME/.local/bin/uv) — set UV=/path/to/uv"

if [ -r "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
elif [ "$DRY_RUN" = 1 ]; then
  echo "  (no readable $ENV_FILE — dry run continues without the host environment)"
else
  die "$ENV_FILE is not readable — this does not look like a Launchpad host (or run with sudo)"
fi
if [ "${LAUNCHPAD_CONSOLE_HTTP_LAB:-false}" = true ]; then
  # Fail BEFORE fetching/building, not after new assets replaced the old dist.
  [ -f "${NGINX_SITE}-http-lab" ] ||
    die "HTTP lab site missing; rerun configure_http_lab_host.sh before updating"
  grep -qE '^[[:space:]]*listen[[:space:]]+[0-9]+[[:space:]]+default_server;' \
    "${NGINX_SITE}-http-lab" || die "HTTP lab site has no valid plain listener"
  HTTP_LINK="${LAUNCHPAD_NGINX_HTTP_LINK:-/etc/nginx/sites-enabled/launchpad-http-lab}"
  [ "$(readlink -f "$HTTP_LINK")" = "$(readlink -f "${NGINX_SITE}-http-lab")" ] ||
    die "HTTP lab site is not enabled; rerun configure_http_lab_host.sh before updating"
  [ "${LAUNCHPAD_CONSOLE_ENTRY:-}" = public_nlb ] &&
    [[ "${LAUNCHPAD_PUBLIC_BASE_URL:-}" == http://*":${LAUNCHPAD_CONSOLE_HTTP_PORT:-8088}" ]] ||
    die "HTTP lab primary origin/entry differs from its persisted listener"
fi

# N11-r5-bug4 (t1, the last pre-r4-G host): the root-run update of 2026-10-10 left 1,625
# root-owned entries in the owner's ~/.cache/uv (archive-v0, wheels-v7) and 4 in ~/.npm, and
# the owner-run bootstrap then died at its first cache write (`Failed to write to the client
# cache … Permission denied`). Pin both caches UNDER the asserted tree so every owner-run
# step (6 deps, 7 build, 8 verify, 10 bootstrap) and the backend service share one
# owner-owned cache the ownership check covers; bootstrap-host.sh writes the same two keys
# into /etc/launchpad/env on a cold host (sourced above), so this is a no-op there and the
# repair for hosts that predate it. Exported, never on a command line — runuser -m carries it.
export UV_CACHE_DIR="$ROOT/data/.uv-cache"
export npm_config_cache="$ROOT/data/.npm-cache"
CACHE_NOTE="UV_CACHE_DIR=data/.uv-cache npm_config_cache=data/.npm-cache"

# git runs as the checkout owner: its safe.directory, its known_hosts, its deploy key
# (UserData exported this same GIT_SSH_COMMAND only for the clone; it is not persisted).
if [ -z "${GIT_SSH_COMMAND:-}" ] && [ -f "$OWNER_HOME/.ssh/id_ed25519" ]; then
  export GIT_SSH_COMMAND="ssh -i $OWNER_HOME/.ssh/id_ed25519 -o IdentitiesOnly=yes -o UserKnownHostsFile=$OWNER_HOME/.ssh/known_hosts -o StrictHostKeyChecking=yes -o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=3"
fi
as_owner() {
  if [ "$(id -un)" = "$OWNER" ]; then
    "$@"
  elif [ -n "${GIT_SSH_COMMAND:-}" ]; then
    sudo -u "$OWNER" -H env GIT_SSH_COMMAND="$GIT_SSH_COMMAND" "$@"
  else
    sudo -u "$OWNER" -H "$@"
  fi
}
# N12-bug10: root is for systemctl and the env file only. Everything that WRITES under the
# checkout (uv sync, npm, the bootstrap, cdk, make verify) runs as the checkout owner — the
# backend service runs as that user and rewrites data/ (ledger, builds) and config/ on every
# repair/redeploy, and a root-owned file there fails it with PermissionError. as_owner_env
# hands the WHOLE current environment down (the sourced env file, HOME, PATH) through
# `runuser -m` — never VAR=value on a command line, which /proc/<pid>/cmdline would expose.
as_owner_env() {   # a dry run prints the exact runuser form the host will execute
  if [ "$DRY_RUN" = 1 ]; then printf '  + runuser -u %s -m -- %s\n' "$OWNER" "$*"
  elif [ "$(id -un)" = "$OWNER" ]; then "$@"
  else runuser -u "$OWNER" -m -- "$@"
  fi
}
# data/ covers the ledger, data/skill-lab-venv (7k files), data/builds/<agent> and, since
# N11-r5-bug4, the pinned uv/npm caches (data/.uv-cache, data/.npm-cache)
OWNED_PATHS=(data config backend/.venv infra/.venv frontend/dist frontend/node_modules backend/app)
# N11-r5-bug4: the owner's default cache dirs — what a pre-r5 root-run update polluted and
# what an older host's service/bootstrap still reads until /etc/launchpad/env pins the caches.
# Scanned to depth 3 only: a root-run uv/npm creates WHOLE entries (wheels-v7/<hash>/…,
# _cacache/content-v2/<xx>/…), so their top levels carry the stray ownership, and a full walk
# of a mature uv cache (millions of files) would cost minutes on every update; the repair is a
# chown -R of the whole dir regardless.
OWNER_HOME_PATHS=("$OWNER_HOME/.cache/uv" "$OWNER_HOME/.cache/npm" "$OWNER_HOME/.npm")
OWNER_HOME_SCAN_DEPTH=3
LEGACY_BUILD_GLOB="/tmp/launchpad_build_*"   # pre-r4-G zip_runtime build dirs (now data/builds/)
# N11-r5-bug5: a shared cache is written by OTHER processes while this script scans it — uv
# and npm stage downloads as `.tmpXXXX` entries that exist for seconds (the dev box saw the
# dry-run tests die on `~/.cache/uv/.tmpAbCd` created by a concurrent `uv sync`). A `.tmp*`
# entry younger than STRAY_TRANSIENT_MAX_AGE is therefore not a stray, and every candidate
# `find` produced is stat'ed ONCE more before it counts: one that vanished (or was chowned)
# in between is not reported. A real root-owned file (any name, or an old `.tmp*`) still is.
STRAY_TRANSIENT_MAX_AGE=60   # seconds
settle_strays() {   # stdin: candidates from find → stdout: the ones that are really stray
  local -a candidates=() kept=()
  local entry name mtime uid now
  mapfile -t candidates
  now="$(date +%s)"
  for entry in "${candidates[@]}"; do
    name="${entry##*/}"
    if [[ "$name" == .tmp* ]]; then
      mtime="$(stat -c %Y "$entry" 2>/dev/null)" || continue                  # already gone
      [ $((now - mtime)) -ge "${STRAY_TRANSIENT_MAX_AGE:-60}" ] || continue   # still being written
    fi
    uid="$(stat -c %u "$entry" 2>/dev/null)" || continue   # vanished between find and now
    [ "$uid" != "$OWNER_UID" ] || continue                 # chowned in the meantime
    kept+=("$entry")
  done
  if [ ${#kept[@]} -gt 0 ]; then printf '%s\n' "${kept[@]}" 2>/dev/null || true; fi
  return 0
}
find_strays() {   # find_strays <path> [find options, e.g. -maxdepth N] → settled stray list
  local path="$1"
  shift
  find "$path" "$@" ! -user "$OWNER" -print 2>/dev/null | settle_strays
}
stray_files() {   # every file under the owned paths (+ owner caches, legacy build dirs) not owned by $OWNER
  local path
  for path in "${OWNED_PATHS[@]}"; do
    if [ -e "$ROOT/$path" ]; then find_strays "$ROOT/$path"; fi
  done
  for path in "${OWNER_HOME_PATHS[@]}"; do
    if [ -e "$path" ]; then find_strays "$path" -maxdepth "$OWNER_HOME_SCAN_DEPTH"; fi
  done
  for path in $LEGACY_BUILD_GLOB; do
    if [ -e "$path" ]; then find_strays "$path"; fi
  done
  return 0
}
restore_ownership() {   # chown -R only the top-level paths that hold a stray (idempotent)
  local path n=0
  for path in "${OWNED_PATHS[@]/#/$ROOT/}" $LEGACY_BUILD_GLOB; do
    if [ -e "$path" ] && [ -n "$(find_strays "$path")" ]; then
      run chown -R "$OWNER:$OWNER_GROUP" "$path"
      n=$((n + 1))
    fi
  done
  for path in "${OWNER_HOME_PATHS[@]}"; do
    if [ -e "$path" ] && [ -n "$(find_strays "$path" -maxdepth "$OWNER_HOME_SCAN_DEPTH")" ]; then
      run chown -R "$OWNER:$OWNER_GROUP" "$path"
      n=$((n + 1))
    fi
  done
  echo "  ownership: $n path(s) repaired"
}
assert_ownership() {    # warn — never fail — on anything still not owned by $OWNER
  local stray
  stray="$(stray_files | head -20)"
  if [ -n "$stray" ]; then
    echo "  WARNING: not owned by $OWNER (the $BACKEND_UNIT service runs as $OWNER) — re-run with --fix-ownership:" >&2
    printf '%s\n' "$stray" | sed 's/^/    /' >&2
  else
    echo "  ${OWNED_PATHS[*]} ${OWNER_HOME_PATHS[*]} $LEGACY_BUILD_GLOB: all files owned by $OWNER"
  fi
  return 0
}

# N11-r3-bug6: every git call runs as the checkout owner — root has no safe.directory for
# the ubuntu-owned tree on a UserData host and would die with "dubious ownership".
as_owner git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "$ROOT is not a readable git checkout for $OWNER"
[ -n "$BRANCH" ] || BRANCH="$(as_owner git rev-parse --abbrev-ref HEAD)"
[ "$BRANCH" != "HEAD" ] || die "detached HEAD — pass --branch <name>"
echo "  owner=$OWNER uv=$UV branch=$BRANCH env_name=${LAUNCHPAD_ENV_NAME:-<default>} with_cdk=$WITH_CDK"
if [ "$FIX_OWNERSHIP" = 1 ]; then
  echo "  --fix-ownership: repairing ownership before anything runs as $OWNER"
  restore_ownership
else
  STRAY="$(stray_files | head -5)"
  if [ -n "$STRAY" ]; then
    printf '%s\n' "$STRAY" >&2
    die "files under the checkout are not owned by $OWNER (an update by the pre-N12-bug10 script ran as root) — re-run once with --fix-ownership"
  fi
fi

# ── 2. ledger backup ────────────────────────────────────────────────────────────
log "2/14 ledger backup"
LEDGER="$ROOT/data/launchpad.db"
if [ -f "$LEDGER" ]; then
  as_owner_env cp -a "$LEDGER" "$LEDGER.bak-$(date -u +%Y%m%d-%H%M%S)"
else
  echo "  no $LEDGER yet — nothing to back up"
fi

# ── 3. lock drift ───────────────────────────────────────────────────────────────
log "3/14 local changes"
DIRTY="$(as_owner git status --porcelain --untracked-files=no)"
if [ -n "$DIRTY" ]; then
  OTHER="$(printf '%s\n' "$DIRTY" | grep -vE ' (backend|infra)/uv\.lock$' || true)"
  if [ -n "$OTHER" ]; then
    printf '%s\n' "$OTHER" >&2
    die "local changes besides uv.lock drift — resolve them first (the update never discards real edits)"
  fi
  echo "  discarding uv.lock drift (obs21: the committed locks are the only locks a host may run)"
  run as_owner git checkout -- backend/uv.lock infra/uv.lock
else
  echo "  clean"
fi

# ── 4. fetch ────────────────────────────────────────────────────────────────────
log "4/14 fetch origin/$BRANCH (timeout ${FETCH_TIMEOUT}s × ${FETCH_ATTEMPTS})"
fetched=0
for attempt in $(seq 1 "$FETCH_ATTEMPTS"); do
  if run as_owner timeout "$FETCH_TIMEOUT" git fetch --prune origin "$BRANCH"; then
    fetched=1
    break
  fi
  echo "  fetch attempt $attempt/$FETCH_ATTEMPTS failed; retrying in 20s" >&2
  sleep 20
done
[ "$fetched" = 1 ] || die "git fetch failed after $FETCH_ATTEMPTS attempts"

# ── 5. pull (fast-forward only) ─────────────────────────────────────────────────
log "5/14 fast-forward"
OLD="$(as_owner git rev-parse HEAD)"
run as_owner git merge --ff-only "origin/$BRANCH"
if [ "$DRY_RUN" = 1 ]; then
  # N11-obs55: a dry run plans against the tip the last fetch left in origin/<branch> —
  # when that tip is a fast-forward of HEAD, exactly what `--ff-only` would land — so the
  # changed-file set (CDK-owned files included) matches the real run; otherwise HEAD -> HEAD.
  NEW="$(as_owner git rev-parse --verify -q "refs/remotes/origin/$BRANCH^{commit}" 2>/dev/null || true)"
  if [ -z "$NEW" ] || ! as_owner git merge-base --is-ancestor "$OLD" "$NEW" 2>/dev/null; then NEW="$OLD"; fi
  [ "$NEW" = "$OLD" ] || echo "  (dry run: planning against the fetched origin/$BRANCH tip)"
else
  NEW="$(as_owner git rev-parse HEAD)"
fi
CHANGED="$(as_owner git diff --name-only "$OLD" "$NEW" 2>/dev/null || true)"
echo "  $OLD -> $NEW ($(printf '%s\n' "$CHANGED" | grep -c . || true) files)"
changed() { [ "$FULL" = 1 ] || printf '%s\n' "$CHANGED" | grep -qE "$1"; }

# N11-obs55: CDK-owned changes are applied by step 9 ONLY — say so now, loudly, when the
# flag is missing. Never an abort: code, deps, dist, bootstrap and the restart stay valid.
CDK_CHANGED="$(as_owner git diff --name-only "$OLD" "$NEW" -- "${CDK_PATHS[@]}" 2>/dev/null || true)"
CDK_N="$(printf '%s\n' "$CDK_CHANGED" | grep -c . || true)"
cdk_pending_warning() {   # stdout; the caller sends a second copy to stderr
  printf '  WARNING: CDK-owned files changed in %s..%s but --with-cdk was not given — these are NOT applied:\n' "$OLD" "$NEW"
  printf '%s\n' "$CDK_CHANGED" | sed 's/^/    /'
  printf '  re-run with --with-cdk (N11-obs55; deploy.md §8.1 "CDK-owned paths and --with-cdk")\n'
}
if [ "$CDK_N" -gt 0 ]; then
  if [ "$WITH_CDK" = 1 ]; then
    echo "  cdk: $(plural "$CDK_N" "CDK-owned file") changed — step 9 deploys"
  else
    cdk_pending_warning
    cdk_pending_warning >&2
  fi
else
  echo "  cdk: no CDK-owned changes (${CDK_PATHS[*]})"
fi
# N17-obs472: what an earlier PLAIN update skipped stays pending in data/cdk-pending (one
# "<old>..<new> <path>" line per file) until a --with-cdk run applies it — the <old>..<new>
# diff of a later plain update no longer contains those files, so it alone would forget them
# while the stacks still drift. Reported on every update; only a successful step 9 clears it.
CDK_PENDING_FILE="$ROOT/data/cdk-pending"
CARRIED="$(grep -v '^[[:space:]]*$' "$CDK_PENDING_FILE" 2>/dev/null || true)"
CARRIED_N="$(printf '%s\n' "$CARRIED" | grep -c . || true)"
carried_warning() {   # stdout; the caller sends a second copy to stderr
  printf '  WARNING: CDK-owned changes skipped by an earlier plain update are still NOT applied (data/cdk-pending):\n'
  printf '%s\n' "$CARRIED" | sed 's/^/    /'
  printf '  re-run with --with-cdk (N17-obs472)\n'
}
if [ "$CARRIED_N" -gt 0 ]; then
  if [ "$WITH_CDK" = 1 ]; then
    echo "  cdk: step 9 applies $(plural "$CARRIED_N" "carried CDK-owned file") from data/cdk-pending too"
  else
    carried_warning
    carried_warning >&2
  fi
fi
if [ "$CDK_N" -gt 0 ] && [ "$WITH_CDK" != 1 ]; then
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    as_owner_env sh -c 'mkdir -p "$(dirname "$2")" && printf "%s\n" "$1" >> "$2"' _ "$OLD..$NEW $path" "$CDK_PENDING_FILE"
  done <<<"$CDK_CHANGED"
fi

# ── 6. dependencies ─────────────────────────────────────────────────────────────
log "6/14 dependencies (uv sync --frozen · npm ci on package-lock change; as $OWNER; $CACHE_NOTE)"
for project in backend infra; do
  # N17-obs485: `--frozen` installs the lock without checking it, so a stale lock would
  # install the OLD set silently — check it against pyproject.toml first (read-only, no index)
  if [ -f scripts/uv_lock_fresh.py ]; then
    python3 -I scripts/uv_lock_fresh.py "$project" \
      || die "$project: the committed uv.lock is stale against pyproject.toml (N17-obs485) — run \`uv lock\` on a dev machine, commit, and update again (never \`uv lock\` on a host)"
  else
    echo "  $project: no scripts/uv_lock_fresh.py in this checkout — lock freshness not checked"
  fi
  if ! (cd "$project" && as_owner_env "$UV" sync --frozen); then
    die "$project: uv sync --frozen failed — see the uv error above; a lock change belongs on a dev machine (\`uv lock\`, commit, update again — never \`uv lock\` on a host)"
  fi
done
if changed '^frontend/package(-lock)?\.json$' || [ ! -d frontend/node_modules ]; then
  (cd frontend && as_owner_env npm ci --no-audit --no-fund)
else
  echo "  frontend deps unchanged"
fi

# ── 7. frontend build ───────────────────────────────────────────────────────────
# N11-r3-bug8: the decision is made from what dist/ was BUILT FROM (the stamp every
# build writes), never from this run's fast-forward range — a dist/ restored by hand from
# an older pin has an empty range and would otherwise survive the update.
log "7/14 frontend build (as $OWNER; $CACHE_NOTE)"
STAMP="frontend/dist/.launchpad-build"
REBUILD_REASON="" BUILT_FROM=""
decide_frontend_build() {   # sets REBUILD_REASON (empty = keep dist/) and BUILT_FROM
  if [ "$FULL" = 1 ]; then REBUILD_REASON="--full"; return; fi
  if [ ! -d frontend/dist ]; then REBUILD_REASON="no frontend/dist yet"; return; fi
  if [ ! -f "$STAMP" ]; then
    REBUILD_REASON="dist/ carries no .launchpad-build stamp (built before r3-D or by hand)"; return
  fi
  BUILT_FROM="$(tr -d '[:space:]' <"$STAMP")"
  if ! as_owner git cat-file -e "${BUILT_FROM}^{commit}" 2>/dev/null; then
    REBUILD_REASON="dist/ was built from unknown commit '${BUILT_FROM}'"; return
  fi
  if [ "$BUILT_FROM" != "$NEW" ] && ! as_owner git diff --quiet "$BUILT_FROM" "$NEW" -- frontend/; then
    REBUILD_REASON="dist/ was built from ${BUILT_FROM:0:8}; frontend/ changed since (HEAD ${NEW:0:8})"
  fi
}
decide_frontend_build
if [ -n "$REBUILD_REASON" ]; then
  echo "  rebuilding frontend/dist: $REBUILD_REASON"
  (cd frontend && as_owner_env npm run build)
  if [ "$DRY_RUN" = 1 ]; then
    printf '  + printf %s | as_owner_env tee %s\n' "$NEW" "$STAMP"
  else
    printf '%s\n' "$NEW" | as_owner_env tee "$STAMP" >/dev/null
  fi
  BUILT_FROM="$NEW"   # dist/ now matches HEAD (data/build-info.json below says so)
  restore_ownership   # safety net — nothing should be stray now that npm ran as $OWNER
else
  echo "  frontend/dist built from ${BUILT_FROM:0:8} (frontend/ unchanged since) — keeping dist/"
fi
# N13-obs10: the deployed commit, readable off the console — `GET /api/health` serves
# `build: {sha, built_at}` from this file (services/build_info.py), so a demo video's
# `recorded_version` (E01-obs1) can be checked against the running console. Written on EVERY
# update, as the owner (the backend, User=$OWNER, must be able to rewrite data/).
BUILD_INFO="data/build-info.json"
BUILD_INFO_JSON="$(printf '{"sha": "%s", "built_at": "%s", "branch": "%s", "dist_built_from": "%s"}' \
  "$NEW" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$BRANCH" "${BUILT_FROM:-$NEW}")"
if [ "$DRY_RUN" = 1 ]; then
  printf '  + printf %s | as_owner_env tee %s\n' "'$BUILD_INFO_JSON'" "$BUILD_INFO"
else
  printf '%s\n' "$BUILD_INFO_JSON" | as_owner_env tee "$BUILD_INFO" >/dev/null
fi

# ── 8. verify (opt-in) ──────────────────────────────────────────────────────────
log "8/14 verify"
if [ "$VERIFY" = 1 ]; then
  as_owner_env env -u AWS_REGION -u AWS_DEFAULT_REGION -u LAUNCHPAD_REGION make verify
else
  echo "  skipped (--verify to run make verify before restarting)"
fi

# ── 9. cdk (opt-in) ─────────────────────────────────────────────────────────────
cdk_context_args() {
  # Mirrors app.services.bootstrap.cdk_context_args: "k=v,k2=v2" → repeated --context,
  # plus env_name and memory_domain_mode from host settings; conflicts are refused.
  local raw="${LAUNCHPAD_CDK_CONTEXT:-}" env_name="${LAUNCHPAD_ENV_NAME:-}" have_env=0 pair
  local mode="${LAUNCHPAD_MEMORY_DOMAIN_MODE:-shared}" have_mode=0
  local -a pairs=() args=()
  case "$mode" in
    shared|dedicated) ;;
    *) die "memory_domain_mode must be shared or dedicated (LAUNCHPAD_MEMORY_DOMAIN_MODE)" ;;
  esac
  if [ "$mode" = dedicated ] && [ -z "$env_name" ]; then
    die "memory_domain_mode=dedicated requires a nonempty LAUNCHPAD_ENV_NAME"
  fi
  IFS=',' read -ra pairs <<<"$raw"
  for pair in "${pairs[@]}"; do
    pair="${pair// /}"
    [ -n "$pair" ] || continue
    case "$pair" in
      env_name=*)
        have_env=1
        [ "${pair#env_name=}" = "$env_name" ] || die "LAUNCHPAD_CDK_CONTEXT carries $pair but LAUNCHPAD_ENV_NAME is '${env_name}' — make the two agree (scripts/bootstrap.py refuses the same mismatch)"
        ;;
      memory_domain_mode=*)
        have_mode=1
        [ "${pair#memory_domain_mode=}" = "$mode" ] || die "LAUNCHPAD_CDK_CONTEXT carries $pair but LAUNCHPAD_MEMORY_DOMAIN_MODE is '$mode' — make the two agree (scripts/bootstrap.py refuses the same mismatch)"
        ;;
    esac
    args+=(--context "$pair")
  done
  if [ "$have_env" = 0 ] && [ -n "$env_name" ]; then args+=(--context "env_name=$env_name"); fi
  if [ "$have_mode" = 0 ] && [ "$mode" != shared ]; then
    args+=(--context "memory_domain_mode=$mode")
  fi
  if [ ${#args[@]} -gt 0 ]; then printf '%s\n' "${args[@]}"; fi
}
log "9/14 cdk"
if [ "$WITH_CDK" = 1 ]; then
  if [ "$CDK_N" -gt 0 ]; then
    echo "  cdk: $(plural "$CDK_N" "CDK-owned file") changed — deploying"
  else
    echo "  cdk: no CDK-owned changes — deploy is a no-op (still run: a D17 migration or a drifted/missing stack is applied)"
  fi
  # captured first: a `die` inside a process substitution would only end that subshell
  CTX_RAW="$(cdk_context_args)" || exit 1
  CTX=()
  if [ -n "$CTX_RAW" ]; then mapfile -t CTX <<<"$CTX_RAW"; fi
  CDK_REGION="${LAUNCHPAD_REGION:-${AWS_REGION:-cn-northwest-1}}"
  # N11-obs44b: fail fast BEFORE the (~16 min) deploy when this host's ops role cannot
  # assume the memory admin role — the memory stage in step 10 could only degrade with
  # the same reason (N11-obs44a). The check prints its own `memory-admin precondition:` line.
  echo "  memory-admin precondition (scripts/bootstrap.py --check-memory-admin; N11-obs44b)"
  (cd backend && as_owner_env "$UV" run --frozen python ../scripts/bootstrap.py --check-memory-admin) \
    || die "memory-admin precondition failed — this host's ops role cannot assume launchpad-memory-admin-${CDK_REGION}; update the EC2 stack to the current template (MemoryAdminAssume; deploy.md §8.2/§8.3) and re-run --with-cdk (N11-obs44b)"
  (cd infra && as_owner_env env CDK_DEFAULT_REGION="$CDK_REGION" "$UV" run --frozen cdk deploy --all --require-approval never "${CTX[@]}")
  # N17-obs472: everything pending is applied now (the deploy above ran every stack)
  as_owner_env rm -f "$CDK_PENDING_FILE"
elif [ "$CDK_N" -gt 0 ]; then
  echo "  skipped — $(plural "$CDK_N" "CDK-owned file") changed in $OLD..$NEW and NOT applied (obs18/obs55, WARNING in step 5); pass --with-cdk"
  if [ "$CARRIED_N" -gt 0 ]; then echo "  + $(plural "$CARRIED_N" "carried CDK-owned file") still NOT applied (data/cdk-pending)"; fi
elif [ "$CARRIED_N" -gt 0 ]; then
  echo "  skipped — $(plural "$CARRIED_N" "carried CDK-owned file") still NOT applied (data/cdk-pending, N17-obs472); pass --with-cdk"
else
  echo "  skipped — no CDK-owned changes; infra/ changes are NOT applied by bootstrap (obs18); pass --with-cdk"
fi

# ── 10. bootstrap ───────────────────────────────────────────────────────────────
log "10/14 bootstrap (--skip-cdk; idempotent; as $OWNER; $CACHE_NOTE)"
(cd backend && as_owner_env "$UV" run --frozen python ../scripts/bootstrap.py --skip-cdk)
restore_ownership       # safety net — the bootstrap ran as $OWNER too

# ── 11. ownership check ─────────────────────────────────────────────────────────
log "11/14 ownership check ($OWNER on ${OWNED_PATHS[*]} (data incl. skill-lab-venv, builds, .uv-cache, .npm-cache) ${OWNER_HOME_PATHS[*]} $LEGACY_BUILD_GLOB)"
assert_ownership

# ── 12. nginx site ──────────────────────────────────────────────────────────────
# WB-003: a host bootstrapped before the /assets/ rule serves index.html (200, text/html)
# for a content-hashed chunk that a rebuild removed, and an open tab then fails to load the
# page it navigates to. The whole refresh (render → compare → install → nginx -t → reload,
# with the .prev rollback and the never-abort semantics) is `refresh_nginx_site` in
# infra/ec2/nginx-site.sh — one source of truth with bootstrap-host.sh, and unit-tested
# with fake nginx/systemctl/install in infra/tests/test_nginx_site.py. `run` is the hook
# every root-run command goes through, so --dry-run prints the plan and writes nothing.
log "12/14 nginx site ($NGINX_SITE)"
NGINX_LIB="$ROOT/infra/ec2/nginx-site.sh"
if [ ! -f "$NGINX_LIB" ]; then
  echo "  skipped — no infra/ec2/nginx-site.sh in this checkout (pre-WB-003 branch)"
else
  # shellcheck source=/dev/null
  . "$NGINX_LIB"
  if ! declare -F refresh_nginx_site >/dev/null; then
    echo "  skipped — infra/ec2/nginx-site.sh in this checkout has no refresh_nginx_site (pre-r7-e)"
  else
    refresh_nginx_site "$NGINX_SITE" "$ROOT/frontend/dist" run
    if [ "${LAUNCHPAD_CONSOLE_HTTP_LAB:-false}" = true ]; then
      # Persisted opt-in owns the additional site as well as the primary origin.
      refresh_nginx_site "${NGINX_SITE}-http-lab" "$ROOT/frontend/dist" run
    fi
  fi
fi

# ── 12b. public origin ──────────────────────────────────────────────────────────
# N11-obs47: refreshed BEFORE the restart so the backend comes up with the right 3LO
# return URL. Numbered 12b to keep the 1..14 step labels stable for operators and tests.
log "12b/14 public origin ($ENV_FILE LAUNCHPAD_PUBLIC_BASE_URL ← IMDSv2 public-ipv4)"
refresh_public_base_url "$ENV_FILE"

# ── 12c. uv frozen ──────────────────────────────────────────────────────────────
# N17-obs485: before the restart, so the restarted service already runs frozen.
log "12c/14 uv frozen ($ENV_FILE UV_FROZEN=1 · $BACKEND_UNIT ExecStart \`uv run --frozen\`; N17-obs485)"
ensure_uv_frozen_env "$ENV_FILE"
ensure_uv_frozen_unit "$BACKEND_UNIT"

# ── 13. restart ─────────────────────────────────────────────────────────────────
log "13/14 restart"
if [ "$RESTART" = 1 ]; then
  run systemctl restart "$BACKEND_UNIT"
  if systemctl list-unit-files "$FRONTEND_UNIT.service" 2>/dev/null | grep -q "^$FRONTEND_UNIT"; then
    run systemctl restart "$FRONTEND_UNIT"
  fi
else
  echo "  skipped (--no-restart)"
fi

# ── 14. health ──────────────────────────────────────────────────────────────────
log "14/14 health"
if [ "$DRY_RUN" = 1 ] || [ "$RESTART" = 0 ]; then
  echo "  skipped"
else
  PORT=8000   # what the systemd unit binds (bootstrap-host.sh); nginx proxies to the same port
  deadline=$((SECONDS + HEALTH_TIMEOUT))
  until curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/api/health"; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      journalctl -u "$BACKEND_UNIT" --since "3 min ago" -q --no-pager | tail -40 >&2
      die "backend not healthy after ${HEALTH_TIMEOUT}s"
    fi
    sleep 3
  done
  errs="$(journalctl -u "$BACKEND_UNIT" --since "3 min ago" -q --no-pager | grep -ciE 'error|traceback' || true)"
  echo "  /api/health OK · journal error/traceback lines since restart: ${errs} (want 0)"
fi

# N11-obs55: the summary always carries a `cdk:` field — `pending` is the one that needs a
# second run with --with-cdk.
CARRIED_NOTE=""
if [ "$CARRIED_N" -gt 0 ]; then CARRIED_NOTE="$(plural "$CARRIED_N" "carried file")"; fi
if [ "$WITH_CDK" = 1 ]; then
  if [ "$CDK_N" -gt 0 ]; then CDK_SUMMARY="deployed ($(plural "$CDK_N" file)"; else CDK_SUMMARY="deployed (no CDK-owned changes"; fi
  if [ -n "$CARRIED_NOTE" ]; then CDK_SUMMARY="$CDK_SUMMARY; $CARRIED_NOTE applied"; fi
  CDK_SUMMARY="$CDK_SUMMARY)"
elif [ "$CDK_N" -gt 0 ] && [ -n "$CARRIED_NOTE" ]; then
  CDK_SUMMARY="pending ($(plural "$CDK_N" file) + $CARRIED_NOTE — re-run with --with-cdk)"
elif [ "$CDK_N" -gt 0 ]; then
  CDK_SUMMARY="pending ($(plural "$CDK_N" file) — re-run with --with-cdk)"
elif [ -n "$CARRIED_NOTE" ]; then
  CDK_SUMMARY="pending ($CARRIED_NOTE — re-run with --with-cdk)"
else
  CDK_SUMMARY="no changes"
fi
printf '\n══ host-update done · %s -> %s · branch %s · cdk: %s ══\n' "$OLD" "$NEW" "$BRANCH" "$CDK_SUMMARY"
