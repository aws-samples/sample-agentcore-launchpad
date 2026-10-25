# Upgrade / 升级

How to move a running Launchpad console from an older version to a newer one: the
code, the backend and frontend dependencies, and the SQLite ledger
(`data/launchpad.db`). Read it once end to end before you start.

中文版: [upgrade.zh-CN.md](upgrade.zh-CN.md)

> The sample is not hardened for production use (see
> [setup.md](setup.md#hosted-deployment--托管部署)). The steps below keep the data you
> already have safe, but you are responsible for testing an upgrade on your own
> host before your users depend on it.

## What an upgrade changes

| Part | Where it lives | How it is upgraded |
|---|---|---|
| Code | the git checkout | `git merge --ff-only` to the new commit |
| Backend dependencies | `backend/uv.lock` → `backend/.venv` | `uv sync --frozen` |
| Frontend | `frontend/package-lock.json` → `node_modules`, built into `frontend/dist` | `npm ci && npm run build` |
| Ledger | `data/launchpad.db` (SQLite) | **migrates itself on the next backend start** — there is no separate migration command |
| Configuration | `config/launchpad.yaml`, the systemd unit environment, `/etc/launchpad-*.env` | untouched; `config/` and `data/` are ignored by git |

The ledger migration runs inside `init_db` (`backend/app/core/db.py`) every time the
backend starts. It creates the tables that are new (`create_all`), adds the new
columns and indexes (`_migrate`, idempotent `ALTER TABLE … ADD COLUMN`), seeds the
`default` workspace and the `default` project, files every existing row into the
`default` project once (guarded by the `migration.projects_v1` marker in
`platform_settings`) and then checks that the schema matches the models and that
every row has a workspace and every project-owned row a project. If a check fails,
the backend refuses to start and says why in the journal. Restarting it again does
nothing new: every step is idempotent.

AWS stays the source of truth: the ledger holds identifiers and derived progress.
An upgrade does not create, change or delete AWS resources.

## Choose a path

| Path | When |
|---|---|
| [One-command upgrade](#one-command-upgrade) | The recommended first upgrade: the manual steps below in one command, rolled back automatically if the new version does not come up. |
| [Manual upgrade](#manual-upgrade) | Always possible. **Required for the first upgrade** from a version that has no **Configure → System update** page (any version before the online update feature) — or use the one-command upgrade. |
| [Online update](#online-update-configure--system-update) | Once you run a version with the online update and have installed the root updater. An administrator prepares, schedules and (if needed) rolls back from the console. |

The steps below use the reference host of the workshop: an Ubuntu EC2 instance, the
checkout at `/home/ubuntu/agentcore_launchpad` owned by `ubuntu`, the systemd unit
`launchpad-backend` running `uv run uvicorn app.main:app` from `backend/`, and nginx
serving `frontend/dist`. Substitute your own path, owner and units. If your host
also runs a `launchpad-frontend` unit (`vite preview`, see
[setup.md](setup.md#hosted-deployment--托管部署)), restart it together with the
backend wherever this page restarts `launchpad-backend`.

## One-command upgrade

`backend/scripts/upgrade.sh` runs every step of the [manual upgrade](#manual-upgrade) below in order and stops at the first one that fails. It ships with the **new** version, so take it from the release branch rather than from the old checkout (older versions do not have it):

```bash
cd /home/ubuntu/agentcore_launchpad
git fetch origin
git show origin/main:backend/scripts/upgrade.sh > /tmp/lp-upgrade.sh
sudo bash /tmp/lp-upgrade.sh --repo "$PWD" --dry-run      # checks, backup and rehearsal only — changes nothing
sudo bash /tmp/lp-upgrade.sh --repo "$PWD"                # the upgrade
sudo bash /tmp/lp-upgrade.sh --repo "$PWD" --install-updater   # the same, then install the online updater
```

| Step | What it does | On failure |
|---|---|---|
| 1. Preflight | checkout owner, no local changes, fast-forward to the target, free disk (`--min-free-gb`, default 3), `uv` and `npm` found, the systemd units exist | stops, nothing changed |
| 2. In-flight work | the running version's `inflight.py`; exit `3` stops the run unless `--yes` | stops, nothing changed |
| 3. Ledger backup | the **target's** `ledger_backup.py`, online and verified, renamed `….bak-<stamp>-preupgrade-<old sha>` (out of the rotation), with `config/launchpad.yaml` and `data/sso-secret.key` copied next to it | stops, nothing changed |
| 4. Rehearsal | the target's `rehearse_projects_migration.py` migrates a copy of that backup in a temporary worktree | stops, nothing changed |
| 5. Update | `git merge --ff-only`, `uv sync --frozen`, `npm ci && npm run build` (the old build kept as `frontend/dist.prev`) | rolled back |
| 6. Restart and verify | restarts the units (the ledger migrates on this start) and waits for `/api/auth/status` to report the target's `version.sha` (`--health-timeout`, default 180 s) | rolled back |

`--dry-run` ends after step 4. The automatic rollback restores the code and `frontend/dist` of the previous commit, runs `uv sync --frozen`, restarts and checks the console answers again. The ledger stays migrated (old versions start on it — see [Rollback](#rollback)); the step-3 backup is still there.

Exit codes: `0` upgraded (or already at the target); `1` stopped in steps 1–4, nothing changed; `2` the upgrade failed and was rolled back; `3` the rollback failed too — follow [Rollback](#rollback) by hand; `64` bad arguments. Each run's full log is `data/upgrade/upgrade-<UTC stamp>.log`. See `bash /tmp/lp-upgrade.sh --help` for the rest (`--target` for a commit or branch, `--services` for the units to restart, `--health-url`).

## Manual upgrade

Run every command **as the owner of the checkout** (`ubuntu` here), not as root:
`git`, `uv` and `npm` running as root leave root-owned files in `.git`, `.venv`,
`node_modules` and `dist` that the service and later upgrades cannot rewrite. Only
`systemctl` needs `sudo`. Over a non-interactive SSH session `uv` may not be on the
`PATH`; use its absolute path (`/home/ubuntu/.local/bin/uv`).

### 1. Pre-checks

```bash
cd /home/ubuntu/agentcore_launchpad
stat -c %U .                                   # the owner: run everything below as this user
git status --short                             # must print nothing: local edits block a fast-forward
git rev-parse HEAD | tee ~/launchpad-prev-sha  # the commit to roll back to — keep it
df -h .                                        # free disk (see below)
(cd backend && uv run python scripts/inflight.py); echo "inflight exit=$?"   # if your version has it
```

- **Disk.** The upgrade needs room for a ledger backup (the size of
  `data/launchpad.db`), new Python packages, `node_modules` and a new `dist`. Keep a
  few GB free; the online update refuses to prepare below
  `LAUNCHPAD_UPDATE_MIN_FREE_GB` (default 3).
- **Work in flight.** A restart interrupts background work: evaluation runs that have
  not started their batch, Skill Lab jobs, open architect-assistant turns, running
  data pipelines. Deployments and workspace bootstraps resume from their checkpoint.
  `scripts/inflight.py` reads the ledger read-only and lists each item with what the
  restart will do to it. Exit `0` (nothing) or `2` (only items that resume): go
  ahead. Exit `3`: something would fail or be cleared — wait and run it again, or
  restart knowingly and tell the owners what to re-submit. The table of outcomes is
  in [agent-runbook-prod.md](agent-runbook-prod.md#update-recipe-verified-sequence).
  The online update asks for the same acknowledgement (see below).
- **Toolchain.** `uv` ≥ 0.8 and Node.js ≥ 20, as in
  [setup.md](setup.md#prerequisites--前置条件). The backend requires Python ≥ 3.12;
  `uv` provides it.
- **Network.** The host must reach the git remote, PyPI and the npm registry. The
  frontend lockfile also pulls `xlsx` from `https://cdn.sheetjs.com`, so allow that
  host if outbound traffic is filtered.

### 2. Back up the ledger

```bash
(cd backend && uv run python scripts/ledger_backup.py); echo "backup exit=$?"   # stop unless 0
```

`ledger_backup.py` copies the live ledger with SQLite's online backup API, so it is
safe while the backend is serving (a plain `cp` of the live file is not). It writes
`data/launchpad.db.bak-<UTC YYYYmmdd-HHMMSS>`, runs `PRAGMA integrity_check` on the
copy and prints its path, size and the `agents` / `jobs` / `eval_runs` row counts.
Exit `0` = written and verified; `1` = no backup; `2` = the copy failed the integrity
check (kept as `….corrupt`) — do not continue until you understand why.

- **A version that predates the script** can run the new version's copy with the
  system `python3` (with `--db` the script needs no virtual environment):

  ```bash
  git fetch origin
  git show origin/main:backend/scripts/ledger_backup.py | python3 - --db data/launchpad.db
  ```

- **Keep this backup out of the rotation.** The online updater prunes
  `launchpad.db.bak-<stamp>` files beyond the newest 10 (`--keep 10`); a name with a
  suffix is never pruned. Rename the pre-upgrade copy, for example
  `mv data/launchpad.db.bak-<stamp> data/launchpad.db.bak-<stamp>-preupgrade`, and
  keep it until the new version has run for a while.
- **Also copy what is not in the ledger:** `config/launchpad.yaml`, and
  `data/sso-secret.key` if it exists (the key that encrypts the single-sign-on
  client secrets stored in the ledger; versions with SSO create it on first use
  unless `LAUNCHPAD_SSO_SECRET_KEY` is set). A ledger restored without its key cannot
  decrypt those secrets.

### 3. Rehearse the migration on the backup

Run the new version's migration on a copy first. `rehearse_projects_migration.py` ships
with the new version, so check the new commit out into a scratch worktree beside the
running checkout — the running service is not touched:

```bash
git fetch origin
git merge-base --is-ancestor HEAD origin/main && echo "fast-forward: ok"
git log --oneline HEAD..origin/main | wc -l          # commits you are behind
git worktree add --detach /tmp/launchpad-rehearsal origin/main
cd /tmp/launchpad-rehearsal/backend
uv sync --frozen
uv run python scripts/rehearse_projects_migration.py \
  /home/ubuntu/agentcore_launchpad/data/launchpad.db.bak-<stamp>-preupgrade
cd /home/ubuntu/agentcore_launchpad
git worktree remove --force /tmp/launchpad-rehearsal
```

The ledger you pass is only read. The script copies it to a temporary directory, runs
`init_db` on the copy exactly as the backend does on start (with AWS clients refused)
and prints, per table, the rows, the rows without a project before and after, and the
orphans (derived rows whose parent row is gone, which take their workspace's default
project), then the time `init_db` took and the result:

```text
table                          kind         rows  null before  null after  orphans
api_keys                       strict          …            …           0        …
…
init_db took 0.53s
OK every row has a workspace, every strict row a project; no drift
```

Exit `0` = the real upgrade will succeed. Exit `1` = a check failed; the migrated copy
is kept and its path printed — do not upgrade, keep the output and open an issue.
`--out /tmp/rehearsal.db` keeps the migrated copy for inspection. On the workshop
hosts `init_db` took 0.53 s (us-west-2) and 0.62 s (us-east-1), so the first start of
the new version is not noticeably slower.

If the new version has no `rehearse_projects_migration.py` (it ships with projects),
skip this step; the backup from step 2 is still your safety net.

### 4. Update the code

```bash
cd /home/ubuntu/agentcore_launchpad
git fetch origin
git merge --ff-only origin/main
git log --oneline -1
```

`main` is the release branch, and the branch the online update follows by
default. A host that still tracks the old `console-v3` branch can switch with
the same command: every `console-v3` commit is an ancestor of `main`, so the
merge is a fast-forward. Track a different branch or a specific commit the same way
(`git merge --ff-only <sha>`). `--ff-only` refuses anything that is not a
fast-forward of your commit, such as local commits or a history rewrite; resolve
that before you go on rather than forcing it.

### 5. Backend dependencies

```bash
cd backend && uv sync --frozen && cd ..
```

`--frozen` installs exactly what `backend/uv.lock` pins and fails instead of
re-resolving. Between a7f550d and the projects version, for example, the lock gained
`authlib` and `python3-saml` (single sign-on), which bring `xmlsec` and `lxml`. The
lock carries prebuilt `xmlsec` wheels for Linux x86_64 and aarch64 with glibc 2.28 or
newer (Ubuntu 20.04+, Amazon Linux 2023), macOS and Windows. On an older Linux
(Amazon Linux 2 has glibc 2.26) `uv` builds `xmlsec` from source and needs the
system libraries first — on Debian / Ubuntu
`sudo apt-get install -y pkg-config libxml2-dev libxmlsec1-dev libxmlsec1-openssl`.

The `systemd` unit runs `uv run`, which would also sync on start; running
`uv sync --frozen` yourself surfaces a failure before the restart instead of after.

### 6. Frontend

```bash
rm -rf frontend/dist.prev && cp -a frontend/dist frontend/dist.prev   # the old build, for a quick rollback
cd frontend && npm ci && npm run build && cd ..
```

`npm ci` installs exactly `package-lock.json`. nginx (or `vite preview`) serves
`frontend/dist`, so **the build is required** — without it the browser keeps the old
console against the new API. nginx needs no restart.

### 7. Restart — the ledger migrates itself

```bash
sudo systemctl restart launchpad-backend          # + launchpad-frontend if you run that unit
sudo journalctl -u launchpad-backend --since "2 min ago" -q
```

On this start `init_db` migrates the ledger as described in
[What an upgrade changes](#what-an-upgrade-changes). The first start of the projects
version creates the `default` project, binds it to every workspace, files every
existing agent, job, dataset and their derived rows into it, and adds every
non-administrator with a workspace grant as an `inherit` member of that workspace's
default project. With one project per workspace nothing visible changes for your
users. Details: [architecture.md](architecture.md#operating-projects).

### 8. Verify

```bash
curl -s http://127.0.0.1:8000/api/auth/status        # public; includes "version": {"sha": …}
git rev-parse HEAD                                   # must equal version.sha
systemctl is-active launchpad-backend
sudo journalctl -u launchpad-backend --since "5 min ago" -q | grep -icE "error|traceback"   # want 0
```

`version.sha` is the commit the running process loaded; if it still shows the old
commit, the restart did not take. Then log in as an administrator in the browser,
open a few pages (agents, evaluation, **Configure → Projects**) and check that your
agents and history are there. When everything looks right, remove the old build:
`rm -rf frontend/dist.prev`.

## Online update (Configure → System update)

Once the host runs a version with the online update, an administrator can do the
next upgrades from the console. Install the updater once, as root, from the
checkout:

```bash
sudo backend/scripts/install_updater.sh                 # defaults from this checkout
# the workshop host has no launchpad-frontend unit (nginx serves dist/):
sudo backend/scripts/install_updater.sh --user ubuntu --services "launchpad-backend"
systemctl status launchpad-updater.path                 # expect active (waiting)
```

It copies `backend/scripts/apply_update.sh` to `/usr/local/lib/launchpad/` (an update
never replaces the script that applies it), writes `/etc/launchpad-updater.env` and
the units `launchpad-updater.path` / `launchpad-updater.service`. Re-run it only when
you deliberately want a newer `apply_update.sh`.

On the page:

1. **Check** compares the running commit with the newest commit of
   `LAUNCHPAD_UPDATE_BRANCH` (default `main`) in `LAUNCHPAD_UPDATE_REPO`
   (default `https://github.com/aws-samples/sample-agentcore-launchpad`). Only a
   fast-forward is accepted: a host on a commit that is not an ancestor of that
   branch cannot update online — upgrade it manually once onto the branch.
   The default moved from `console-v3` to `main` once `console-v3` merged into
   `main`. A host whose environment sets `LAUNCHPAD_UPDATE_BRANCH=console-v3`
   explicitly keeps following that branch; remove the line (or set `main`) and
   restart the backend to switch.
2. **Prepare** builds the new commit in `data/updates/stage-<sha>` (`uv sync --frozen`,
   `npm ci`, `npm run build`, an import check and a start check on a temporary port
   with a throwaway ledger). The running console is not affected.
3. **Schedule restart** (in one minute, or at a time within 7 days) publishes an
   announcement. **If the restart would interrupt tasks in progress, the page lists
   them and you must acknowledge them before it schedules** (the API answers
   `update.inflight_unacknowledged` otherwise). At the time, the updater backs up
   the ledger (`ledger_backup.py --keep 10`), fast-forwards the checkout, swaps the
   built `dist` in, runs `uv sync --frozen`, restarts the units and waits up to 120 s
   for `/api/auth/status` to report the new `version.sha`. If that fails, it resets to
   the previous commit, restores the old `dist`, restarts and reports `rolled_back`.
4. **Roll back** prepares the commit the last update replaced; schedule it like an
   update.

Without the updater the page still checks and prepares, then shows the commands to
apply the prepared stage by hand. The full description is in
[setup.md](setup.md#hosted-deployment--托管部署), step 4 of *Hosted deployment*.

## Rollback

Roll back the code when the new version does not start or misbehaves:

```bash
cd /home/ubuntu/agentcore_launchpad
git reset --hard "$(cat ~/launchpad-prev-sha)"
cd backend && uv sync --frozen && cd ..
rm -rf frontend/dist && mv frontend/dist.prev frontend/dist      # or: cd frontend && npm ci && npm run build
sudo systemctl restart launchpad-backend
git rev-parse HEAD; systemctl is-active launchpad-backend
```

Versions older than the online update do not report `version.sha`; check the commit
with `git rev-parse HEAD` and the service with `systemctl` and the journal.

**Keep the migrated ledger if you can.** The migration only adds tables, columns and
indexes and fills new columns, and an older backend's startup check looks only for
columns its own models need, so an older version generally still starts on a migrated
ledger (checked for a7f550d: its `init_db` runs cleanly on a ledger the projects
version migrated). Data the new version wrote stays in the ledger; the older code
just does not show what it does not know (projects other than `default`, SSO
providers, network profiles). One exception: the projects version turns an agent's
inline VPC network (subnets + security groups, only used by agents with S3 Files /
EFS mounts) into a reference to a network profile. Older code cannot read that
reference, so such agents may fail to load there.

When you upgrade again after a rollback, rows the older version wrote without a
project are adopted on start into their workspace's project (a WARNING in the
journal) as long as each workspace has one project. In a workspace bound to several
projects the start refuses and names the tables; file the rows with
`backend/scripts/backfill_projects.py` (stop the backend first, see
[architecture.md](architecture.md#operating-projects)).

**Restore the backup only when you must** — the migrated ledger is damaged, or the
older version cannot work with it — and only if you accept losing everything written
since the backup (new agents, runs, users, audit rows). With the service stopped a
plain copy is safe:

```bash
sudo systemctl stop launchpad-backend
mv data/launchpad.db data/launchpad.db.failed-upgrade
cp -a data/launchpad.db.bak-<stamp>-preupgrade data/launchpad.db
sudo systemctl start launchpad-backend
```

Restore `data/sso-secret.key` together with the ledger it belongs to, if you copied
one.

## Several instances or Regions

Each deployment (for example one per Region) is an independent console with its own
checkout, ledger and workspaces; nothing is shared between them. Upgrade them one at
a time: back up, rehearse, upgrade and verify the first one, let it run, and only then
move to the next. A rehearsal per host is cheap (well under a second of `init_db` on
the workshop ledgers) and catches data that only that ledger holds.

## New optional settings

All of these have defaults, so an upgrade needs none of them. Each is a key in
`config/launchpad.yaml` or the upper-cased name with a `LAUNCHPAD_` prefix in the
unit environment. Added since a7f550d (`backend/app/core/config.py`):

| Setting | Default | What it does |
|---|---|---|
| `update_repo` / `update_branch` | `https://github.com/aws-samples/sample-agentcore-launchpad` / `main` | Source of the online update. |
| `update_enabled` | unset | `false` turns the online update off; unset = scheduling only where the updater is installed. |
| `update_min_free_gb` / `update_stage_max_gb` | `3` / `6` | Free disk needed to prepare; size cap of a prepared stage. |
| `eval_cost_prices` | `{}` | Overrides of the evaluation unit prices used for cost estimates; keys you leave out keep their defaults. |
| `project_inference_profiles` | `false` | Per-project Bedrock application inference profiles for isolated projects (cost per project). |
| `assistant_digest_model_id` | `""` | Model of the architect assistant's digest of confirmed items; empty = the workspace's default model. |
| `assistant_context_window_tokens`, `assistant_replay_max_tokens`, `assistant_replay_max_turns` | `0`, `200000`, `12` | The architect assistant's replay budget. |
| `model_context_windows` | `{}` | Input context windows by model-id substring, for models without one in the price table. |

Environment only: `LAUNCHPAD_SSO_SECRET_KEY` (a Fernet key for SSO client secrets;
without it a key file `data/sso-secret.key` is created) and
`LAUNCHPAD_PUBLIC_BASE_URL` (the browser URL, needed for single sign-on). Single
sign-on, cost tags, network profiles and storage are configured on their
**Configure** pages, not in settings; see [setup.md](setup.md) and
[sso-identity-center.md](sso-identity-center.md).

## Troubleshooting

| Symptom | What to do |
|---|---|
| `git merge --ff-only` refuses | Your checkout has local commits or edits (`git status`, `git log origin/main..HEAD`). Move them aside, or check out the release branch on a clean checkout; do not force-reset over work you need. |
| `uv sync --frozen` fails building `xmlsec` / `lxml` | No prebuilt wheel for your platform: install `pkg-config` and the `libxml2` / `libxmlsec1` development packages (step 5) and re-run. |
| `npm ci` fails | Check access to the npm registry and to `cdn.sheetjs.com` (the `xlsx` package), and the Node.js version (≥ 20). `npm ci` deletes `node_modules` first; a half-finished run is fixed by running it again. |
| The page looks unchanged after the restart | `npm run build` was skipped or failed — `frontend/dist` is still the old build. Build again; hard-reload the browser. |
| The service does not come up | `sudo journalctl -u launchpad-backend -n 200 --no-pager`. Look for the first `Traceback`. An `ImportError` / `ModuleNotFoundError` means the dependencies were not synced: run `uv sync --frozen` as the owner. A `Permission denied` under `.venv`, `node_modules` or `data/` means a step ran as root: `sudo chown -R ubuntu: <path>`. |
| `ledger schema is behind the models` | A column the code expects was not added by `_migrate`. Do not edit the ledger by hand: roll the code back (the ledger is still usable) and report the listed `ALTER TABLE` lines. |
| `ledger rows with no workspace_id` / `ledger rows with no project_id` | Rows that the startup checks cannot place. For `project_id` in a workspace with several projects, run `backend/scripts/backfill_projects.py --workspace <id> --project <id>` with the backend stopped. For `workspace_id`, roll the code back and report it. |
| Any other migration error, or the rehearsal fails | Stop. Keep the journal or the rehearsal output, roll the code back, and restore the ledger backup only if the migrated ledger does not work with the old version. |
| `curl /api/auth/status` reports the old `version.sha` | The process was not restarted, or it runs from another checkout: check `WorkingDirectory` with `systemctl cat launchpad-backend`. |

## Reference: the 2026-10-16 upgrade

The workshop host in us-west-2 went from `a7f550d` to the projects version (`0c70391`,
252 commits) with exactly the manual sequence above: online backup to
`data/launchpad.db.bak-<UTC timestamp>-preprojects`, rehearsal on a copy
(`init_db` 0.53 s; 0.62 s for the us-east-1 ledger; "OK every row has a workspace,
every strict row a project; no drift"), `git fetch` + checkout, `uv sync --frozen`
(new: `python3-saml`, `xmlsec`, `lxml`), `npm ci && npm run build`,
`systemctl restart launchpad-backend`, and `/api/auth/status` reporting the new
commit as `version.sha`.
