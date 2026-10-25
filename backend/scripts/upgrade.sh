#!/usr/bin/env bash
# One-command upgrade of a running Launchpad console (docs/upgrade.md "Manual upgrade").
#
# For a host on an older commit — including every version before online
# update existed, which cannot update itself — to the latest main (or any
# commit that fast-forwards from the running one). The script ships with the NEW
# version, so take it from there rather than from the old checkout:
#
#   cd /home/ubuntu/agentcore_launchpad
#   git fetch origin
#   git show origin/main:backend/scripts/upgrade.sh > /tmp/lp-upgrade.sh
#   sudo bash /tmp/lp-upgrade.sh --repo "$PWD" --dry-run   # checks + backup + rehearsal
#   sudo bash /tmp/lp-upgrade.sh --repo "$PWD"             # the upgrade
#
# Steps (each stops the run on failure, nothing changed before step 5):
#   1. preflight: checkout owner, clean tree, fast-forward to the target, free disk, uv/npm
#   2. in-flight work (backend/scripts/inflight.py of the RUNNING version, when it has it):
#      exit 3 (a restart would fail/clear work) stops unless --yes
#   3. online ledger backup (the TARGET's ledger_backup.py, verified), kept out of the
#      updater's rotation as …-preupgrade-<old sha>, with config/launchpad.yaml and
#      data/sso-secret.key copied next to it
#   4. rehearse the target's migration on that backup (rehearse_projects_migration.py,
#      when the target has it) in a temporary worktree — the live checkout is untouched
#   --dry-run ends here.
#   5. git merge --ff-only, uv sync --frozen, npm ci && npm run build (old dist kept as
#      frontend/dist.prev)
#   6. restart the units; the console must report the target's version.sha
#   7. any failure in 5–6: back to the previous commit + dist, uv sync, restart
#      ("rolled back"). The ledger is left as migrated (old versions start on it — see
#      docs/upgrade.md "Rollback"); the backup from step 3 is there if you need it.
#   --install-updater: after a successful upgrade, install the online updater
#      (backend/scripts/install_updater.sh) so the next upgrade can be done in the console.
#
# git / uv / npm run as the checkout's owner (never root), only systemctl as root.
# Re-running after a success is a no-op ("already at …"); after a rollback it retries.
set -uo pipefail

usage() {
  cat <<'EOF'
usage: upgrade.sh --repo DIR [options]
  --repo DIR          the console's git checkout (required)
  --target REF        commit, tag or branch to upgrade to (default: <remote>/main)
  --remote NAME       git remote to fetch (default: origin)
  --services "A B"    systemd units to restart (default: launchpad-backend, plus
                      launchpad-frontend when that unit exists)
  --health-url URL    reports {"version": {"sha": …}} (default: http://127.0.0.1:8000/api/auth/status)
  --health-timeout S  seconds to wait for the new version (default: 180)
  --rollback-timeout S  seconds to wait for the old version after a rollback (default: 180)
  --min-free-gb N     refuse below N GB free on the checkout's disk (default: 3)
  --dry-run           preflight, in-flight check, backup and rehearsal only
  --yes               go on when inflight.py reports work a restart would disrupt
  --install-updater   install the online updater after a successful upgrade
  -h, --help          this text
EOF
}

REPO="" TARGET="" REMOTE=origin SERVICES="" DRY_RUN=no YES=no INSTALL_UPDATER=no
HEALTH_URL=${LAUNCHPAD_HEALTH_URL:-http://127.0.0.1:8000/api/auth/status}
HEALTH_TIMEOUT=180 ROLLBACK_TIMEOUT=180 MIN_FREE_GB=3
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO=${2:-}; shift 2 ;;
    --target) TARGET=${2:-}; shift 2 ;;
    --remote) REMOTE=${2:-}; shift 2 ;;
    --services) SERVICES=${2:-}; shift 2 ;;
    --health-url) HEALTH_URL=${2:-}; shift 2 ;;
    --health-timeout) HEALTH_TIMEOUT=${2:-}; shift 2 ;;
    --rollback-timeout) ROLLBACK_TIMEOUT=${2:-}; shift 2 ;;
    --min-free-gb) MIN_FREE_GB=${2:-}; shift 2 ;;
    --dry-run) DRY_RUN=yes; shift ;;
    --yes) YES=yes; shift ;;
    --install-updater) INSTALL_UPDATER=yes; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "upgrade.sh: unknown argument: $1" >&2; usage >&2; exit 64 ;;
  esac
done
[ -n "$REPO" ] || { usage >&2; exit 64; }
[ -d "$REPO/.git" ] || { echo "upgrade.sh: $REPO is not a git checkout" >&2; exit 64; }
REPO=$(cd "$REPO" && pwd)
case "$HEALTH_TIMEOUT$ROLLBACK_TIMEOUT$MIN_FREE_GB" in *[!0-9]*|"") echo "upgrade.sh: timeouts/sizes are whole numbers" >&2; exit 64 ;; esac

# test seams, like apply_update.sh
SYSTEMCTL=${LAUNCHPAD_SYSTEMCTL:-systemctl}
SUDO=${LAUNCHPAD_SUDO-sudo}
PYTHON=${LAUNCHPAD_PYTHON:-python3}
DATA_DIR=${LAUNCHPAD_DATA_DIR:-$REPO/data}
LEDGER_DB=${LAUNCHPAD_LEDGER_DB:-$DATA_DIR/launchpad.db}
OWNER=$(stat -c %U "$REPO" 2>/dev/null || stat -f %Su "$REPO")
OWNER_HOME=$(getent passwd "$OWNER" 2>/dev/null | cut -d: -f6); OWNER_HOME=${OWNER_HOME:-$HOME}
STAMP=$(date -u +%Y%m%d-%H%M%S)
WORK="$DATA_DIR/upgrade"
LOG="$WORK/upgrade-$STAMP.log"

as_owner() {
  if [ "$(id -un)" = "$OWNER" ]; then "$@"
  elif [ "$(id -u)" = 0 ]; then sudo -u "$OWNER" -H -- "$@"
  else echo "upgrade.sh: run as root or as the checkout's owner ($OWNER)" >&2; exit 64
  fi
}
as_root() {  # systemctl and the updater install; LAUNCHPAD_SUDO="" (tests) runs them as is
  if [ "$(id -u)" = 0 ] || [ -z "$SUDO" ]; then "$@"; else $SUDO -- "$@"; fi
}

as_owner mkdir -p "$WORK" || { echo "upgrade.sh: cannot create $WORK" >&2; exit 1; }
as_owner touch "$LOG"
say() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*" | as_owner tee -a "$LOG" >/dev/null; printf '── %s\n' "$*"; }
die() { say "STOP: $*"; say "nothing was changed — log: $LOG"; exit 1; }
run() { as_owner "$@" >>"$LOG" 2>&1; }   # a quiet owner command, output to the log

find_tool() {  # find_tool <name> <override>: the owner's binary (non-login PATH lacks ~/.local/bin)
  if [ -n "$2" ]; then echo "$2"; return; fi
  for c in "$OWNER_HOME/.local/bin/$1" "/usr/local/bin/$1" "/usr/bin/$1" "$OWNER_HOME/.cargo/bin/$1"; do
    [ -x "$c" ] && { echo "$c"; return; }
  done
  as_owner bash -lc "command -v $1" 2>/dev/null
}

# ---- 1. preflight ----------------------------------------------------------------
say "upgrade $REPO (owner $OWNER) — log $LOG"
UV=$(find_tool uv "${LAUNCHPAD_UV:-}"); NPM=$(find_tool npm "${LAUNCHPAD_NPM:-}")
[ -n "$UV" ] || die "uv not found for $OWNER (install uv >= 0.8, docs/setup.md)"
[ -n "$NPM" ] || die "npm not found for $OWNER (install Node.js >= 20)"
dirty=$(as_owner git -C "$REPO" status --short --untracked-files=no)
[ -z "$dirty" ] || die "the checkout has local changes (git status) — a fast-forward needs a clean tree:
$dirty"
PREV=$(as_owner git -C "$REPO" rev-parse HEAD)
run git -C "$REPO" fetch --quiet "$REMOTE" || die "git fetch $REMOTE failed"
TARGET=${TARGET:-$REMOTE/main}
NEW=$(as_owner git -C "$REPO" rev-parse --verify --quiet "$TARGET^{commit}") || die "unknown target $TARGET"
if [ "$NEW" = "$PREV" ]; then say "already at $TARGET (${NEW:0:8}) — nothing to do"; exit 0; fi
as_owner git -C "$REPO" merge-base --is-ancestor "$PREV" "$NEW" \
  || die "${PREV:0:8} is not an ancestor of $TARGET (${NEW:0:8}) — local commits or a rewritten history; resolve that first, never force it"
BEHIND=$(as_owner git -C "$REPO" rev-list --count "$PREV..$NEW")
free_kb=$(df -Pk "$REPO" | awk 'NR==2 {print $4}')
[ "${free_kb:-0}" -ge $((MIN_FREE_GB * 1024 * 1024)) ] \
  || die "only $((free_kb / 1024 / 1024)) GB free on $REPO's disk (need $MIN_FREE_GB, --min-free-gb)"
if [ -z "$SERVICES" ]; then
  SERVICES=launchpad-backend
  as_root "$SYSTEMCTL" cat launchpad-frontend >/dev/null 2>&1 && SERVICES="$SERVICES launchpad-frontend"
fi
for unit in $SERVICES; do
  as_root "$SYSTEMCTL" cat "$unit" >/dev/null 2>&1 || die "systemd unit $unit not found (--services)"
done
echo "$PREV" | as_owner tee "$WORK/prev-sha" >/dev/null
say "from ${PREV:0:8} to $TARGET ${NEW:0:8} ($BEHIND commits) · units: $SERVICES"

# ---- 2. in-flight work ------------------------------------------------------------
if [ -f "$REPO/backend/scripts/inflight.py" ]; then
  (cd "$REPO/backend" && as_owner "$UV" run --frozen python scripts/inflight.py) | as_owner tee -a "$LOG"
  rc=${PIPESTATUS[0]}
  case "$rc" in
    0|2) ;;
    3) [ "$YES" = yes ] || die "a restart would disrupt the work listed above — wait and re-run, or re-run with --yes and tell its owners"
       say "--yes: restarting over the work listed above" ;;
    *) die "inflight.py could not read the ledger (exit $rc)" ;;
  esac
else
  say "this version has no inflight.py — check for running evaluations / jobs yourself"
fi

# ---- 3. backup ----------------------------------------------------------------------
[ -f "$LEDGER_DB" ] || die "no ledger at $LEDGER_DB (LAUNCHPAD_LEDGER_DB)"
as_owner git -C "$REPO" show "$NEW:backend/scripts/ledger_backup.py" >"$WORK/ledger_backup.py" 2>/dev/null \
  || as_owner cp "$REPO/backend/scripts/ledger_backup.py" "$WORK/ledger_backup.py" 2>/dev/null \
  || die "no ledger_backup.py in $TARGET or the checkout"
out=$(as_owner "$PYTHON" "$WORK/ledger_backup.py" --db "$LEDGER_DB" 2>&1); rc=$?
printf '%s\n' "$out" | as_owner tee -a "$LOG" >/dev/null
[ $rc -eq 0 ] || die "ledger backup failed (exit $rc): $(printf '%s' "$out" | tail -2)"
made=$(printf '%s\n' "$out" | sed -n 's/^backup: *//p' | tail -1)
[ -f "$made" ] || die "ledger_backup.py reported no backup file"
BACKUP="$made-preupgrade-${PREV:0:8}"
as_owner mv "$made" "$BACKUP" || die "cannot rename $made"
for extra in "$REPO/config/launchpad.yaml" "$DATA_DIR/sso-secret.key"; do
  [ -f "$extra" ] && as_owner cp -a "$extra" "$BACKUP.$(basename "$extra")"
done
say "ledger backed up and verified: $BACKUP"

# ---- 4. rehearse the migration on the backup ---------------------------------------
if as_owner git -C "$REPO" cat-file -e "$NEW:backend/scripts/rehearse_projects_migration.py" 2>/dev/null; then
  REH="$WORK/rehearsal-$STAMP"
  run git -C "$REPO" worktree add --detach --quiet "$REH" "$NEW" || die "cannot create the rehearsal worktree"
  rehearse_ok=no
  if (cd "$REH/backend" && as_owner "$UV" sync --frozen --quiet) >>"$LOG" 2>&1; then
    (cd "$REH/backend" && as_owner "$UV" run --frozen python scripts/rehearse_projects_migration.py "$BACKUP") \
      2>&1 | as_owner tee -a "$LOG" | tail -3
    [ "${PIPESTATUS[0]}" -eq 0 ] && rehearse_ok=yes
  fi
  run git -C "$REPO" worktree remove --force "$REH"
  [ $rehearse_ok = yes ] || die "the migration rehearsal failed on a copy of the ledger — do not upgrade; keep $LOG and open an issue"
  say "migration rehearsal passed"
else
  say "$TARGET has no rehearse_projects_migration.py — rehearsal skipped (the backup is the safety net)"
fi

if [ "$DRY_RUN" = yes ]; then
  say "dry run complete: the upgrade to ${NEW:0:8} is ready — re-run without --dry-run"
  exit 0
fi

# ---- 5–7. apply, restart, verify; revert on failure --------------------------------
FE="$REPO/frontend"
DIST_SAVED=no
restart() { as_root "$SYSTEMCTL" restart $SERVICES >>"$LOG" 2>&1; }  # word-split on purpose
reports() {  # reports <sha>: the console reports <sha> within HEALTH_TIMEOUT
  local deadline=$((SECONDS + HEALTH_TIMEOUT)) got
  while [ $SECONDS -lt $deadline ]; do
    got=$(curl -fsS --max-time 5 "$HEALTH_URL" 2>/dev/null \
      | "$PYTHON" -c 'import json,sys; print((json.load(sys.stdin).get("version") or {}).get("sha",""))' 2>/dev/null)
    [ "$got" = "$1" ] && return 0
    sleep 3
  done
  return 1
}
answers() {  # answers: something healthy answers (an old version reports no version.sha)
  # its own budget: a short --health-timeout must not make a good rollback look failed
  local deadline=$((SECONDS + ROLLBACK_TIMEOUT))
  while [ $SECONDS -lt $deadline ]; do
    curl -fsS --max-time 5 -o /dev/null "$HEALTH_URL" 2>/dev/null && return 0
    sleep 3
  done
  return 1
}
revert() {
  say "rolling back to ${PREV:0:8}"
  run git -C "$REPO" reset --hard --quiet "$PREV"
  if [ $DIST_SAVED = yes ] && [ -d "$FE/dist.prev" ]; then
    run rm -rf "$FE/dist"
    run mv "$FE/dist.prev" "$FE/dist"
  fi
  (cd "$REPO/backend" && as_owner "$UV" sync --frozen) >>"$LOG" 2>&1 || say "uv sync on rollback failed — see $LOG"
  restart || say "restart on rollback failed — see $LOG"
  if answers; then
    say "ROLLED BACK: the console runs ${PREV:0:8} again — log: $LOG · backup: $BACKUP"
    exit 2
  fi
  say "ROLLBACK FAILED: the console did not come back on ${PREV:0:8} — see $LOG and docs/upgrade.md \"Rollback\""
  exit 3
}
step() {  # step <what> <function>: a failure reverts
  say "$1"
  "$2" >>"$LOG" 2>&1 || { say "FAILED: $(tail -3 "$LOG" | tr '\n' ' ')"; revert; }
}
do_merge() { as_owner git -C "$REPO" merge --ff-only --quiet "$NEW"; }
do_sync() { (cd "$REPO/backend" && as_owner "$UV" sync --frozen); }
do_build() { (cd "$FE" && as_owner "$NPM" ci && as_owner "$NPM" run build); }

if [ -d "$FE/dist" ]; then
  run rm -rf "$FE/dist.prev"
  run cp -a "$FE/dist" "$FE/dist.prev" || die "cannot keep a copy of frontend/dist"
  DIST_SAVED=yes
fi
step "git merge --ff-only ${NEW:0:8}" do_merge
step "backend: uv sync --frozen" do_sync
step "frontend: npm ci && npm run build" do_build
say "restarting $SERVICES (the ledger migrates on this start)"
restart || { say "FAILED: systemctl restart"; revert; }
if ! reports "$NEW"; then
  say "FAILED: $HEALTH_URL did not report ${NEW:0:8} within ${HEALTH_TIMEOUT}s"
  as_root journalctl -u launchpad-backend --since "-5min" --no-pager -q 2>/dev/null | tail -20 | as_owner tee -a "$LOG" >/dev/null
  revert
fi
say "UPGRADED: the console runs ${NEW:0:8} ($BEHIND commits)"
say "keep $BACKUP until the new version has run for a while; then: rm -rf $FE/dist.prev"

if [ "$INSTALL_UPDATER" = yes ]; then
  if [ -f "$REPO/backend/scripts/install_updater.sh" ]; then
    as_root bash "$REPO/backend/scripts/install_updater.sh" --user "$OWNER" --services "$SERVICES" >>"$LOG" 2>&1 \
      && say "online updater installed: later upgrades from Settings → System update" \
      || say "installing the updater failed (the upgrade itself succeeded) — see $LOG"
  else
    say "${NEW:0:8} has no install_updater.sh — updater not installed"
  fi
fi
exit 0
