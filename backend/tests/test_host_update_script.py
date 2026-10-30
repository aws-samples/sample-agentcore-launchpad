"""scripts/host-update.sh (N11 r3-B, obs20/21/22): syntax, shellcheck, and the dry-run plan.

The script is exercised from a copy inside a scratch git repository (it resolves its
repository root from its own location), so nothing here touches this checkout, the
network, systemd or AWS. N11-r5-bug5: every dry run also points LAUNCHPAD_OWNER_HOME at a
scratch home next to the repo, so the ownership scan never reads the live ~/.cache/uv —
on a shared dev box a concurrent `uv` creates `.tmpXXXX` entries there mid-test.
"""

import getpass
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "host-update.sh"
NGINX_LIB = Path(__file__).parents[2] / "infra" / "ec2" / "nginx-site.sh"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout


@pytest.fixture
def scratch_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "launchpad"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, repo / "scripts" / "host-update.sh")
    (repo / "infra" / "ec2").mkdir(parents=True)
    shutil.copy2(NGINX_LIB, repo / "infra" / "ec2" / "nginx-site.sh")  # WB-003 step 12
    for rel in ("backend/uv.lock", "infra/uv.lock", "frontend/package.json", "README.md"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(f"{rel}\n")
    _git(repo, "init", "-q", "-b", "cn/integration")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _owner_home(base: Path) -> Path:
    """The scratch owner home every dry run scans instead of the live one (N11-r5-bug5)."""
    home = base / "home"
    home.mkdir(exist_ok=True)
    return home


def _nginx_site_path(repo: Path) -> Path:
    return repo.parent / "etc-nginx" / "launchpad"


def _systemd_dir(repo: Path) -> Path:
    return repo.parent / "etc-systemd"


def _dry_run(repo: Path, *flags: str, env: dict | None = None) -> subprocess.CompletedProcess:
    run_env = {**os.environ, "LAUNCHPAD_ENV_FILE": str(repo / "no-such-env"), "UV": "/bin/true",
               "LAUNCHPAD_OWNER_HOME": str(_owner_home(repo.parent)),
               # WB-003: never look at a real /etc/nginx — a test that wants the nginx step
               # exercised writes this scratch site file first (see _nginx_site)
               "LAUNCHPAD_NGINX_SITE": str(_nginx_site_path(repo)),
               # N17-obs485: step 12c reads the backend unit + drop-ins — never the real ones
               "LAUNCHPAD_SYSTEMD_DIR": str(_systemd_dir(repo))}
    for key in (
        "LAUNCHPAD_CDK_CONTEXT", "LAUNCHPAD_ENV_NAME", "LAUNCHPAD_MEMORY_DOMAIN_MODE",
        "LAUNCHPAD_CONSOLE_ENTRY", "GIT_SSH_COMMAND",
    ):
        run_env.pop(key, None)
    run_env.update(env or {})
    return subprocess.run(
        ["bash", str(repo / "scripts" / "host-update.sh"), "--dry-run", *flags],
        cwd=repo, env=run_env, capture_output=True, text=True,
    )


def test_script_parses_and_passes_shellcheck():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    assert os.access(SCRIPT, os.X_OK), "scripts/host-update.sh must be executable"
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed")
    subprocess.run(["shellcheck", "-S", "warning", str(SCRIPT)], check=True)


def test_help_names_the_opt_in_flags():
    out = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True)
    assert out.returncode == 0
    for flag in ("--with-cdk", "--verify", "--dry-run", "--branch", "--no-restart"):
        assert flag in out.stdout


def test_http_lab_missing_site_stops_before_checkout_or_build(scratch_repo):
    env_file = scratch_repo / "lab-env"
    env_file.write_text("LAUNCHPAD_CONSOLE_HTTP_LAB=true\n")
    out = _dry_run(scratch_repo, env={"LAUNCHPAD_ENV_FILE": str(env_file)})
    assert out.returncode != 0
    assert "HTTP lab site missing" in out.stderr
    assert "git fetch" not in out.stdout
    assert "npm run build" not in out.stdout


def test_default_plan_is_code_only_with_long_fetch_and_skip_cdk(scratch_repo):
    out = _dry_run(scratch_repo)
    assert out.returncode == 0, out.stderr
    plan = out.stdout
    # obs20: long-timeout fetch, fast-forward only
    assert "timeout 1800 git fetch --prune origin cn/integration" in plan
    assert "git merge --ff-only origin/cn/integration" in plan
    # obs21: the committed lock is installed, never rewritten
    assert plan.count("/bin/true sync --frozen") == 2
    # obs18: bootstrap never deploys; cdk is opt-in
    assert "bootstrap.py --skip-cdk" in plan
    assert "cdk deploy" not in plan
    assert "--check-memory-admin" not in plan   # N11-obs44b: the precondition is cdk-only
    assert "pass --with-cdk" in plan
    assert "systemctl restart launchpad-backend" in plan
    # the first run installs the frontend deps and builds (no node_modules / dist yet)
    assert "npm ci" in plan and "npm run build" in plan


def test_with_cdk_runs_an_explicit_deploy_with_the_env_context(scratch_repo):
    out = _dry_run(
        scratch_repo, "--with-cdk",
        env={"LAUNCHPAD_CDK_CONTEXT": "env_name=t1,opensearch_version=OpenSearch_2.19",
             "LAUNCHPAD_ENV_NAME": "t1", "LAUNCHPAD_REGION": "cn-northwest-1"},
    )
    assert out.returncode == 0, out.stderr
    plan = out.stdout
    cdk_line = next(line for line in plan.splitlines() if "cdk deploy --all" in line)
    assert "--require-approval never" in cdk_line
    assert "--context env_name=t1" in cdk_line
    assert "--context opensearch_version=OpenSearch_2.19" in cdk_line
    assert "CDK_DEFAULT_REGION=cn-northwest-1" in cdk_line
    # D17/obs24 order: cdk before bootstrap before restart
    assert plan.index("cdk deploy") < plan.index("bootstrap.py --skip-cdk") < plan.index(
        "systemctl restart"
    )
    # N11-obs44b: the memory-admin precondition runs as the owner, inside step 9, BEFORE
    # the deploy — and only with --with-cdk
    step9 = plan[plan.index("9/14 cdk"):plan.index("10/14 bootstrap")]
    check = "run --frozen python ../scripts/bootstrap.py --check-memory-admin"
    assert check in step9 and "memory-admin precondition" in step9 and "N11-obs44b" in step9
    assert step9.index(check) < step9.index("cdk deploy --all")
    assert plan.count(check) == 1


def test_with_cdk_adds_env_name_from_the_setting_when_the_context_lacks_it(scratch_repo):
    out = _dry_run(scratch_repo, "--with-cdk", env={"LAUNCHPAD_ENV_NAME": "t2"})
    assert out.returncode == 0, out.stderr
    assert "--context env_name=t2" in out.stdout


def test_with_cdk_refuses_a_conflicting_env_name(scratch_repo):
    out = _dry_run(
        scratch_repo, "--with-cdk",
        env={"LAUNCHPAD_CDK_CONTEXT": "env_name=t1", "LAUNCHPAD_ENV_NAME": "t2"},
    )
    assert out.returncode != 0
    assert "make the two agree" in out.stderr


@pytest.mark.parametrize("context", ["env_name=t2", "memory_domain_mode=dedicated"])
def test_with_cdk_preserves_dedicated_domain_selection(scratch_repo, context):
    out = _dry_run(
        scratch_repo, "--with-cdk",
        env={"LAUNCHPAD_ENV_NAME": "t2", "LAUNCHPAD_MEMORY_DOMAIN_MODE": "dedicated",
             "LAUNCHPAD_CDK_CONTEXT": context},
    )
    assert out.returncode == 0, out.stderr
    deploy = next(line for line in out.stdout.splitlines() if "cdk deploy --all" in line)
    assert deploy.count("--context memory_domain_mode=dedicated") == 1
    assert "--context env_name=t2" in deploy


@pytest.mark.parametrize(("mode", "context", "env_name"), [
    ("dedicated", "memory_domain_mode=shared", "t2"),
    ("shared", "memory_domain_mode=dedicated", "t2"),
    ("dedicated", "memory_domain_mode=shared,memory_domain_mode=dedicated", "t2"),
    ("dedicated", "", ""),
    ("invalid", "", "t2"),
])
def test_with_cdk_refuses_invalid_domain_selection_before_deploy(
    scratch_repo, mode, context, env_name,
):
    out = _dry_run(
        scratch_repo, "--with-cdk",
        env={"LAUNCHPAD_ENV_NAME": env_name, "LAUNCHPAD_MEMORY_DOMAIN_MODE": mode,
             "LAUNCHPAD_CDK_CONTEXT": context},
    )
    assert out.returncode != 0
    assert "memory_domain_mode" in out.stderr
    assert "+ " not in out.stdout[out.stdout.index("9/14 cdk"):]


def test_with_cdk_legacy_shared_mode_needs_no_extra_context(scratch_repo):
    out = _dry_run(scratch_repo, "--with-cdk", env={"LAUNCHPAD_ENV_NAME": "t2"})
    assert out.returncode == 0, out.stderr
    deploy = next(line for line in out.stdout.splitlines() if "cdk deploy --all" in line)
    assert "memory_domain_mode" not in deploy


def test_uv_lock_drift_is_discarded_but_real_edits_abort(scratch_repo):
    (scratch_repo / "backend" / "uv.lock").write_text("rewritten by a host-side uv sync\n")
    out = _dry_run(scratch_repo)
    assert out.returncode == 0, out.stderr
    assert "git checkout -- backend/uv.lock infra/uv.lock" in out.stdout

    (scratch_repo / "README.md").write_text("a real local edit\n")
    out = _dry_run(scratch_repo)
    assert out.returncode != 0
    assert "local changes besides uv.lock drift" in out.stderr
    assert "README.md" in out.stderr


def test_no_restart_stops_after_bootstrap(scratch_repo):
    out = _dry_run(scratch_repo, "--no-restart")
    assert out.returncode == 0, out.stderr
    assert "bootstrap.py --skip-cdk" in out.stdout
    assert "systemctl restart" not in out.stdout


def test_unknown_option_is_refused():
    out = subprocess.run(["bash", str(SCRIPT), "--bogus"], capture_output=True, text=True)
    assert out.returncode == 2
    assert "unknown option" in out.stderr


def test_every_git_invocation_runs_as_the_checkout_owner():
    # N11-r3-bug6 (live): a bare `git rev-parse` as root on the ubuntu-owned checkout died
    # with "dubious ownership" before as_owner existed; every git call must go through it.
    import re

    text = SCRIPT.read_text(encoding="utf-8")
    offenders = []
    for n, line in enumerate(text.splitlines(), 1):
        code = line.split("#", 1)[0]
        for m in re.finditer(r"(?<![\w/.-])git\s+(?!-C\b)", code):
            prefix = code[: m.start()]
            inside_string = prefix.count('"') % 2 == 1  # "git fetch failed …" messages
            if inside_string or "as_owner" in prefix or "GIT_SSH_COMMAND" in code:
                continue
            offenders.append(f"{n}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


# ── N11-r3-bug8: dist/ is rebuilt from what it was BUILT FROM, not from the ff range ──
# Live: step 7 said "frontend unchanged — keeping dist/" because the fast-forward range
# had no frontend/ change, while dist/ had been built from an OLDER pin (b990afc8) by
# hand — the restart would have served a stale UI. `npm run build` now stamps the built
# commit into frontend/dist/.launchpad-build; the step rebuilds whenever the stamp is
# missing, names an unknown commit, or frontend/ differs between that commit and HEAD.

STAMP = Path("frontend/dist/.launchpad-build")


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def _dist(repo: Path, stamp: str | None) -> None:
    (repo / "frontend" / "dist").mkdir(parents=True, exist_ok=True)
    (repo / "frontend" / "dist" / "index.html").write_text("<html/>\n")
    if stamp is not None:
        (repo / STAMP).write_text(stamp + "\n")


def _commit(repo: Path, rel: str, text: str) -> str:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text)
    _git(repo, "add", rel)  # only the touched path — never the scratch dist/ itself
    _git(repo, "commit", "-q", "-m", f"touch {rel}")
    return _head(repo)


def test_dist_without_a_build_stamp_is_rebuilt(scratch_repo):
    _dist(scratch_repo, stamp=None)
    out = _dry_run(scratch_repo)
    assert out.returncode == 0, out.stderr
    assert "npm run build" in out.stdout
    assert "no .launchpad-build stamp" in out.stdout


def test_dist_built_from_head_is_kept(scratch_repo):
    head = _head(scratch_repo)
    _dist(scratch_repo, stamp=head)
    out = _dry_run(scratch_repo)
    assert out.returncode == 0, out.stderr
    assert "npm run build" not in out.stdout
    assert "keeping dist/" in out.stdout
    assert head[:8] in out.stdout  # the decision names what dist was built from


def test_dist_built_from_an_older_frontend_is_rebuilt_even_without_a_frontend_ff_change(
    scratch_repo,
):
    old = _head(scratch_repo)
    _dist(scratch_repo, stamp=old)
    new = _commit(scratch_repo, "frontend/src/App.tsx", "export const v = 2;\n")
    # The host is already AT the new commit (the pull happened earlier, or dist/ was
    # restored by hand): the fast-forward range of THIS run is empty.
    out = _dry_run(scratch_repo)
    assert out.returncode == 0, out.stderr
    assert "npm run build" in out.stdout
    assert f"built from {old[:8]}" in out.stdout and new[:8] in out.stdout


def test_dist_built_from_an_older_commit_is_kept_when_frontend_did_not_change(scratch_repo):
    old = _head(scratch_repo)
    _dist(scratch_repo, stamp=old)
    _commit(scratch_repo, "backend/app/main.py", "print('backend only')\n")
    out = _dry_run(scratch_repo)
    assert out.returncode == 0, out.stderr
    assert "npm run build" not in out.stdout
    assert "keeping dist/" in out.stdout


def test_dist_stamped_with_an_unknown_commit_is_rebuilt(scratch_repo):
    _dist(scratch_repo, stamp="deadbeef" * 5)
    out = _dry_run(scratch_repo)
    assert out.returncode == 0, out.stderr
    assert "npm run build" in out.stdout
    assert "unknown" in out.stdout


def test_full_forces_the_rebuild_even_when_the_stamp_matches_head(scratch_repo):
    _dist(scratch_repo, stamp=_head(scratch_repo))
    out = _dry_run(scratch_repo, "--full")
    assert out.returncode == 0, out.stderr
    assert "npm run build" in out.stdout


def test_a_build_writes_the_stamp_after_npm_run_build(scratch_repo):
    out = _dry_run(scratch_repo)  # no dist/ yet → builds
    assert out.returncode == 0, out.stderr
    plan = out.stdout
    assert plan.index("npm run build") < plan.index(".launchpad-build")
    assert f"{_head(scratch_repo)} | as_owner_env tee frontend/dist/.launchpad-build" in plan


def test_every_update_writes_the_build_info_the_health_route_serves(scratch_repo):
    """N13-obs10: `data/build-info.json` {sha, built_at, branch, dist_built_from} follows the
    build decision on EVERY run (rebuilt or kept), as the owner, before the restart — what
    `GET /api/health` serves as `build` so a demo's `recorded_version` can be checked off
    the console (E01-obs1)."""
    head = _head(scratch_repo)
    # (a) no dist yet → rebuild, then the info file names HEAD twice (sha = dist_built_from)
    out = _dry_run(scratch_repo)
    assert out.returncode == 0, out.stderr
    plan = out.stdout
    [line] = [ln for ln in plan.splitlines() if "as_owner_env tee data/build-info.json" in ln]
    assert plan.index(".launchpad-build") < plan.index("data/build-info.json") < plan.index("13/14")
    assert f'"sha": "{head}"' in line and f'"dist_built_from": "{head}"' in line
    assert '"branch": "' in line and '"built_at": "20' in line  # an ISO-8601 UTC timestamp
    assert line.strip().startswith("+ printf ")  # a dry run only prints the write
    # (b) a dist/ kept from an older commit → still written, naming what dist was built from
    older = _git(scratch_repo, "rev-parse", "HEAD~1") if _has_parent(scratch_repo) else head
    dist = scratch_repo / "frontend" / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / ".launchpad-build").write_text(older + "\n")
    out = _dry_run(scratch_repo)
    assert out.returncode == 0, out.stderr
    [line] = [ln for ln in out.stdout.splitlines() if "data/build-info.json" in ln]
    kept = "keeping dist/" in out.stdout  # frontend/ unchanged since `older`
    assert f'"sha": "{head}"' in line
    assert f'"dist_built_from": "{older if kept else head}"' in line


def _has_parent(repo: Path) -> bool:
    return subprocess.run(["git", "rev-parse", "-q", "--verify", "HEAD~1"], cwd=repo,
                          capture_output=True).returncode == 0


# ── N12-bug10: root-owned files after a host update ───────────────────────────────────
# Live (both hosts): the script ran every step as root, so data/skill-lab-venv (7k files),
# backend/.venv (step 6), frontend/dist (step 7), backend/app/**/__pycache__ and the agent
# build dirs (/tmp/launchpad_build_<name>/deployment_package.zip — the file the backend,
# User=ubuntu, then failed on with PermissionError) ended up root-owned. Root is now used
# for systemctl and the env file only; every writer runs as the checkout owner through
# as_owner_env, step 11 only asserts, and --fix-ownership repairs a host once.

WRITER_PREFIX = f"+ runuser -u {getpass.getuser()} -m -- "


def _plan_lines(plan: str) -> list[str]:
    return [line.strip() for line in plan.splitlines() if line.strip().startswith("+ ")]


def test_every_step_that_writes_under_the_checkout_runs_as_the_owner(scratch_repo):
    out = _dry_run(scratch_repo, "--with-cdk", "--verify", env={"LAUNCHPAD_ENV_NAME": "t1"})
    assert out.returncode == 0, out.stderr
    lines = _plan_lines(out.stdout)
    assert lines.count(f"{WRITER_PREFIX}/bin/true sync --frozen") == 2            # step 6
    assert f"{WRITER_PREFIX}npm ci --no-audit --no-fund" in lines                   # step 6
    assert f"{WRITER_PREFIX}npm run build" in lines                                 # step 7
    stamp = f"+ printf {_head(scratch_repo)} | as_owner_env tee frontend/dist/.launchpad-build"
    assert stamp in lines
    assert any(line.startswith(f"{WRITER_PREFIX}env -u AWS_REGION") and line.endswith("make verify")
               for line in lines)                                                   # step 8
    assert any(line.startswith(f"{WRITER_PREFIX}env CDK_DEFAULT_REGION=") and "cdk deploy" in line
               for line in lines)                                                   # step 9
    bootstrap = "/bin/true run --frozen python ../scripts/bootstrap.py --skip-cdk"   # N17-obs485
    assert f"{WRITER_PREFIX}{bootstrap}" in lines
    # no chown is issued when nothing is stray (the safety net chowns only affected paths)
    assert not any("chown" in line for line in lines)
    # the only commands NOT run as the owner are the ones that need root
    others = [line for line in lines if not line.startswith(WRITER_PREFIX)
              and "as_owner git" not in line and "as_owner timeout" not in line
              and not line.startswith("+ printf ")]
    assert others == ["+ systemctl restart launchpad-backend"], others


def test_no_bare_writer_survives_in_the_script_source():
    import re

    text = SCRIPT.read_text(encoding="utf-8")
    offenders = []
    for n, line in enumerate(text.splitlines(), 1):
        code = re.sub(r'"[^"]*"', '""', line.split("#", 1)[0])  # drop quoted prose
        # a bare `run <writer>` statement (root-run) — writers go through as_owner_env
        writer = re.search(r"(^|[;&(]\s*|\s)run\s+(\"\$UV\"|npm|cp|env|make|tee)\b", code)
        if writer and "as_owner_env" not in code:
            offenders.append(f"{n}: {line.strip()}")
    assert offenders == [], "\n".join(offenders)


def test_ownership_check_runs_after_the_bootstrap_and_before_the_restart(scratch_repo):
    (scratch_repo / "data").mkdir()
    (scratch_repo / "config").mkdir()
    out = _dry_run(scratch_repo)
    assert out.returncode == 0, out.stderr
    plan = out.stdout
    assert plan.index("bootstrap.py --skip-cdk") < plan.index("11/14 ownership check") < plan.index(
        "systemctl restart launchpad-backend"
    )
    # everything in the scratch checkout is owned by the test user: clean, no warning
    assert f"all files owned by {getpass.getuser()}" in plan
    assert "WARNING" not in out.stderr + plan
    assert "/tmp/launchpad_build_*" in plan  # the legacy build dirs are part of the check
    check = next(line for line in plan.splitlines() if "11/14 ownership check" in line)
    for path in ("backend/.venv", "infra/.venv", "skill-lab-venv", "data", "frontend/dist"):
        assert path in check, path


def test_fix_ownership_repairs_in_preflight_before_anything_runs_as_the_owner(scratch_repo):
    out = _dry_run(scratch_repo, "--fix-ownership")
    assert out.returncode == 0, out.stderr
    plan = out.stdout
    assert "ownership: 0 path(s) repaired" in plan  # nothing stray in the scratch repo
    assert plan.index("--fix-ownership: repairing") < plan.index("sync --frozen")


def test_help_names_fix_ownership():
    out = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True)
    assert out.returncode == 0 and "--fix-ownership" in out.stdout


# ── N11-r5-bug4: the owner-run caches live under the asserted tree ──────────────────
# Live (t1, the last pre-r4-G host): a root-run update left 1,625 root-owned entries in
# ubuntu's ~/.cache/uv and 4 in ~/.npm; the owner-run bootstrap died at its first cache
# write ("Failed to write to the client cache … Permission denied").


def test_owner_run_steps_use_the_pinned_caches_under_data(scratch_repo):
    out = _dry_run(scratch_repo, "--verify")
    assert out.returncode == 0, out.stderr
    plan = out.stdout
    note = "UV_CACHE_DIR=data/.uv-cache npm_config_cache=data/.npm-cache"
    for step in ("6/14 dependencies", "7/14 frontend build", "10/14 bootstrap"):
        line = next(ln for ln in plan.splitlines() if step in ln)
        assert note in line, line
    # the pin is an exported environment variable runuser -m carries — never VAR=value on
    # a writer's command line (which /proc/<pid>/cmdline would expose like any other)
    for line in _plan_lines(plan):
        assert "UV_CACHE_DIR=" not in line and "npm_config_cache=" not in line, line


def test_pinned_caches_are_exported_after_the_env_file_and_before_any_writer():
    text = SCRIPT.read_text(encoding="utf-8")
    lines = text.splitlines()
    uv_idx = lines.index('export UV_CACHE_DIR="$ROOT/data/.uv-cache"')
    npm_idx = lines.index('export npm_config_cache="$ROOT/data/.npm-cache"')
    env_idx = next(i for i, ln in enumerate(lines) if ln.strip() == '. "$ENV_FILE"')
    owner_env_idx = next(i for i, ln in enumerate(lines) if ln.startswith("as_owner_env() {"))
    # exported after the host env file is sourced (so the $ROOT value wins on every host,
    # including one whose /etc/launchpad/env predates the pin) and before as_owner_env exists
    assert env_idx < uv_idx < owner_env_idx and env_idx < npm_idx < owner_env_idx
    # the same two keys bootstrap-host.sh writes into /etc/launchpad/env on a cold host
    body = (SCRIPT.parents[1] / "infra" / "ec2" / "bootstrap-host.sh").read_text(encoding="utf-8")
    assert "printf 'UV_CACHE_DIR=%s\\n' /home/ubuntu/launchpad/data/.uv-cache" in body
    assert "printf 'npm_config_cache=%s\\n' /home/ubuntu/launchpad/data/.npm-cache" in body
    gitignore = (SCRIPT.parents[1] / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "data/.uv-cache/" in gitignore and "data/.npm-cache/" in gitignore


def test_ownership_check_covers_the_owners_default_cache_dirs(scratch_repo):
    (scratch_repo / "data").mkdir()
    out = _dry_run(scratch_repo)
    assert out.returncode == 0, out.stderr
    plan = out.stdout
    check = next(line for line in plan.splitlines() if "11/14 ownership check" in line)
    home = _owner_home(scratch_repo.parent)  # the fixture's scratch home, never the live one
    for path in (f"{home}/.cache/uv", f"{home}/.cache/npm", f"{home}/.npm"):
        assert path in check, (path, check)
    assert ".uv-cache" in check and ".npm-cache" in check
    # and the same three dirs are what --fix-ownership / the stray scan walk
    text = SCRIPT.read_text(encoding="utf-8")
    assert (
        'OWNER_HOME_PATHS=("$OWNER_HOME/.cache/uv" "$OWNER_HOME/.cache/npm" "$OWNER_HOME/.npm")'
    ) in text
    stray = text[text.index("stray_files() {"):text.index("restore_ownership() {")]
    assert '"${OWNER_HOME_PATHS[@]}"' in stray
    restore = text[text.index("restore_ownership() {"):text.index("assert_ownership() {")]
    assert '"${OWNER_HOME_PATHS[@]}"' in restore
    # shallow scan: a mature uv cache has millions of files (102 s to walk on the dev box);
    # root-run tools create whole top-level entries, and the repair chowns the whole dir
    assert 'OWNER_HOME_SCAN_DEPTH=3' in text
    assert stray.count('-maxdepth "$OWNER_HOME_SCAN_DEPTH"') == 1
    assert restore.count('-maxdepth "$OWNER_HOME_SCAN_DEPTH"') == 1


# ── N11-r5-bug5: the stray scan must survive a cache other processes are writing to ────
# Dev box (twice on 2026-10-12): the dry-run tests scanned the REAL ~/.cache/uv, a concurrent
# `uv sync` had just created `.tmpXXXX` there, and preflight died with "files under the
# checkout are not owned by ubuntu … --fix-ownership". Two fixes: the tests scan a scratch
# home (LAUNCHPAD_OWNER_HOME), and the scan itself ignores a `.tmp*` entry younger than
# 60 s and stat's every candidate again before counting it.


def _settle_strays(paths: list[str], owner_uid: int) -> list[str]:
    """Run the script's settle_strays filter alone, as the given owner uid (no privilege needed:
    with another user's uid every file the test owns looks like a stray)."""
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("settle_strays() {")
    fn = text[start:text.index("\n}\n", start) + 3]
    out = subprocess.run(
        ["bash", "-c", f"set -Eeuo pipefail; OWNER_UID={owner_uid}; {fn}\nsettle_strays"],
        input="".join(f"{p}\n" for p in paths), capture_output=True, text=True, check=True,
    )
    return out.stdout.splitlines()


FOREIGN_UID = 0 if os.getuid() != 0 else 65534


def test_settle_strays_drops_young_tmp_entries_and_vanished_paths_but_keeps_real_strays(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    young = cache / ".tmpAbCd"          # a concurrent uv/npm is still writing it
    young.write_text("")
    old = cache / ".tmpStale"            # a .tmp* left behind long ago is a real stray
    old.write_text("")
    import time
    stale = time.time() - 120
    os.utime(old, (stale, stale))
    plain = cache / "wheels-v7"          # the N11-r5-bug4 shape: any name, wrong owner
    plain.mkdir()
    vanished = cache / ".tmpGone"        # find saw it; it is gone by the time we stat it
    assert not vanished.exists()
    kept = _settle_strays([str(young), str(old), str(plain), str(vanished)], FOREIGN_UID)
    assert kept == [str(old), str(plain)], kept


def test_settle_strays_recheck_drops_a_candidate_the_owner_now_owns(tmp_path):
    # the recheck compares uids again: a file chowned (or a false candidate) is not reported
    f = tmp_path / "wheels-v7"
    f.mkdir()
    assert _settle_strays([str(f)], os.getuid()) == []
    assert _settle_strays([str(f)], FOREIGN_UID) == [str(f)]


def test_a_young_tmp_entry_in_the_owner_cache_does_not_abort_or_warn(scratch_repo):
    home = _owner_home(scratch_repo.parent)
    for rel in (".cache/uv/.tmpAbCd/part", ".npm/_cacache/.tmpX"):
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        (home / rel).write_text("")
    (scratch_repo / "data").mkdir()
    out = _dry_run(scratch_repo)
    assert out.returncode == 0, out.stderr
    assert f"all files owned by {getpass.getuser()}" in out.stdout
    assert "WARNING" not in out.stderr + out.stdout and "--fix-ownership" not in out.stderr
    out = _dry_run(scratch_repo, "--fix-ownership")
    assert out.returncode == 0, out.stderr
    assert "ownership: 0 path(s) repaired" in out.stdout


def test_every_dry_run_scans_the_scratch_owner_home_never_the_live_one(scratch_repo):
    out = _dry_run(scratch_repo)
    assert out.returncode == 0, out.stderr
    home = _owner_home(scratch_repo.parent)
    check = next(line for line in out.stdout.splitlines() if "11/14 ownership check" in line)
    assert f"{home}/.cache/uv" in check and f"{home}/.npm" in check
    live = os.path.expanduser("~")
    assert f"{live}/.cache" not in out.stdout + out.stderr
    copy = _copy_outside(scratch_repo)
    out = _dry_run_copy(copy, copy.parent, "--root", str(scratch_repo))
    assert out.returncode == 0, out.stderr
    assert f"{live}/.cache" not in out.stdout + out.stderr


def test_owner_home_override_is_documented_and_the_scan_routes_through_the_settle_filter():
    out = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True)
    assert out.returncode == 0 and "LAUNCHPAD_OWNER_HOME" in out.stdout
    text = SCRIPT.read_text(encoding="utf-8")
    lines = text.splitlines()
    override = next(i for i, ln in enumerate(lines) if 'OWNER_HOME="$LAUNCHPAD_OWNER_HOME"' in ln)
    home_export = next(i for i, ln in enumerate(lines) if ln.startswith('export HOME="$OWNER_HOME'))
    assert override < home_export
    assert "STRAY_TRANSIENT_MAX_AGE=60" in text
    stray = text[text.index("stray_files() {"):text.index("restore_ownership() {")]
    restore = text[text.index("restore_ownership() {"):text.index("assert_ownership() {")]
    assert stray.count("find_strays ") == 3 and "find " not in stray   # every walk is filtered
    assert restore.count("find_strays ") == 2 and "find " not in restore
    finder = text[text.index("find_strays() {"):text.index("stray_files() {")]
    assert "| settle_strays" in finder
    deploy_md = SCRIPT.parents[1] / "docs" / "cn" / "interfaces" / "deploy.md"
    deploy = deploy_md.read_text(encoding="utf-8")
    assert "LAUNCHPAD_OWNER_HOME" in deploy


# ── N11-obs36: run a NEW copy of the script from OUTSIDE the checkout ──────────────
# Live (deploy-ops 5k/5l first attempt): a fast-forward must never rewrite the executing
# file, so the operator ran a copy from /root — and the script derived ROOT from its own
# path and died in preflight with "/ is not a git checkout". `--root <checkout>` and
# `LAUNCHPAD_ROOT` name the checkout; the flag wins; the default (own path) is unchanged.


def _copy_outside(scratch_repo: Path) -> Path:
    """A copy of the script in a sibling directory that is NOT a git checkout."""
    outside = scratch_repo.parent / "root"
    outside.mkdir(exist_ok=True)
    copy = outside / "host-update-abc123.sh"
    shutil.copy2(SCRIPT, copy)
    return copy


def _dry_run_copy(copy: Path, cwd: Path, *flags: str, env: dict | None = None):
    run_env = {**os.environ, "LAUNCHPAD_ENV_FILE": str(cwd / "no-such-env"), "UV": "/bin/true",
               "LAUNCHPAD_OWNER_HOME": str(_owner_home(cwd.parent))}
    for key in ("LAUNCHPAD_CDK_CONTEXT", "LAUNCHPAD_ENV_NAME", "GIT_SSH_COMMAND", "LAUNCHPAD_ROOT"):
        run_env.pop(key, None)
    run_env.update(env or {})
    return subprocess.run(
        ["bash", str(copy), "--dry-run", *flags], cwd=cwd, env=run_env,
        capture_output=True, text=True,
    )


def test_a_copy_outside_the_checkout_fails_in_preflight_with_the_root_hint(scratch_repo):
    copy = _copy_outside(scratch_repo)
    out = _dry_run_copy(copy, copy.parent)
    assert out.returncode == 1
    assert "is not a git checkout" in out.stderr
    assert "--root <checkout>" in out.stderr and "LAUNCHPAD_ROOT" in out.stderr
    assert "ledger backup" not in out.stdout  # died in step 1, nothing else ran


def test_root_flag_points_a_copy_run_from_another_cwd_at_the_checkout(scratch_repo):
    copy = _copy_outside(scratch_repo)
    out = _dry_run_copy(copy, copy.parent, "--root", str(scratch_repo),
                        "--branch", "cn/integration")
    assert out.returncode == 0, out.stderr
    plan = out.stdout
    assert f"1/14 preflight ({scratch_repo.resolve()} · from --root)" in plan
    assert "git merge --ff-only origin/cn/integration" in plan
    assert "bootstrap.py --skip-cdk" in plan
    assert "systemctl restart launchpad-backend" in plan
    assert "a fast-forward may rewrite it" not in plan  # the copy lives outside the checkout


def test_launchpad_root_env_is_the_fallback_for_a_copy(scratch_repo):
    copy = _copy_outside(scratch_repo)
    out = _dry_run_copy(copy, copy.parent, env={"LAUNCHPAD_ROOT": str(scratch_repo)})
    assert out.returncode == 0, out.stderr
    assert f"1/14 preflight ({scratch_repo.resolve()} · from LAUNCHPAD_ROOT)" in out.stdout


def test_root_flag_wins_over_the_env_override(scratch_repo, tmp_path):
    copy = _copy_outside(scratch_repo)
    decoy = tmp_path / "decoy"
    decoy.mkdir()  # a directory but not a checkout — would fail if it were used
    out = _dry_run_copy(copy, copy.parent, "--root", str(scratch_repo),
                        env={"LAUNCHPAD_ROOT": str(decoy)})
    assert out.returncode == 0, out.stderr
    assert "from --root" in out.stdout and str(decoy) not in out.stdout


def test_default_root_is_still_the_scripts_own_checkout(scratch_repo):
    out = _dry_run(scratch_repo)
    assert out.returncode == 0, out.stderr
    assert f"1/14 preflight ({scratch_repo.resolve()} · from script location)" in out.stdout


def test_root_flag_refuses_a_missing_directory_and_an_empty_value(scratch_repo):
    copy = _copy_outside(scratch_repo)
    out = _dry_run_copy(copy, copy.parent, "--root", str(scratch_repo / "nope"))
    assert out.returncode == 1 and "is not a directory" in out.stderr
    out = _dry_run_copy(copy, copy.parent, "--root")
    assert out.returncode == 2 and "--root needs a value" in out.stderr


def test_help_names_root_and_the_env_override():
    out = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True)
    assert out.returncode == 0
    assert "--root <checkout>" in out.stdout and "LAUNCHPAD_ROOT" in out.stdout


# ── WB-003: step 12 re-renders the nginx site from infra/ec2/nginx-site.sh ───────────────
# Live (default env, 2026-10-12): a tab open across a host update failed to load the
# evaluation page — the old hashed chunk was gone and nginx answered index.html (200,
# text/html) for it. The rule lives in ONE file both bootstrap-host.sh and host-update.sh
# render; the update step installs + reloads nginx only when the rendered site differs.

OLD_SITE_TLS = """server {
  listen 8443 ssl default_server;
    ssl_certificate /etc/nginx/launchpad-tls/cert.pem;
    ssl_certificate_key /etc/nginx/launchpad-tls/key.pem;
  client_max_body_size 64m;
  location ~ ^/(api|v1)/ {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_read_timeout 3600s;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  }
  location / {
    root /home/ubuntu/launchpad/frontend/dist;
    try_files $uri /index.html;
  }
}
"""


def _render_site(repo: Path, tls: str, port: str) -> str:
    """What the shared function renders for this scratch checkout (dist = <repo>/frontend/dist)."""
    return subprocess.run(
        ["bash", "-c", '. "$1" && render_nginx_site "$2" "$3" "$4" 8000', "_",
         str(repo / "infra" / "ec2" / "nginx-site.sh"), tls, port, str(repo / "frontend" / "dist")],
        check=True, capture_output=True, text=True,
    ).stdout


def _nginx_site(repo: Path, text: str) -> Path:
    site = _nginx_site_path(repo)
    site.parent.mkdir(parents=True, exist_ok=True)
    site.write_text(text)
    return site


def _nginx_lines(plan: str) -> list[str]:
    return [line for line in _plan_lines(plan) if "nginx" in line]


def test_nginx_step_is_skipped_on_a_host_without_a_site_file(scratch_repo):
    out = _dry_run(scratch_repo)  # the default scratch env names a site that does not exist
    assert out.returncode == 0, out.stderr
    assert "12/14 nginx site" in out.stdout
    assert "not an nginx host" in out.stdout
    assert _nginx_lines(out.stdout) == []


def test_nginx_step_is_skipped_when_the_checkout_predates_the_shared_renderer(scratch_repo):
    _git(scratch_repo, "rm", "-q", "infra/ec2/nginx-site.sh")   # a committed pre-WB-003 tree
    _git(scratch_repo, "commit", "-q", "-m", "pre-WB-003")
    _nginx_site(scratch_repo, OLD_SITE_TLS)
    out = _dry_run(scratch_repo)
    assert out.returncode == 0, out.stderr
    assert "no infra/ec2/nginx-site.sh" in out.stdout
    assert _nginx_lines(out.stdout) == []


def test_nginx_site_that_matches_the_rendering_is_not_reloaded(scratch_repo):
    _nginx_site(scratch_repo, _render_site(scratch_repo, "true", "8443"))
    out = _dry_run(scratch_repo)
    assert out.returncode == 0, out.stderr
    assert "rendered for tls=true port=8443" in out.stdout   # TLS + port read from the live site
    assert "unchanged — nginx not reloaded" in out.stdout
    assert _nginx_lines(out.stdout) == []
    assert "systemctl reload nginx" not in out.stdout
    # the plain-http variant too
    _nginx_site(scratch_repo, _render_site(scratch_repo, "false", "80"))
    out = _dry_run(scratch_repo)
    assert out.returncode == 0, out.stderr
    assert "rendered for tls=false port=80" in out.stdout and "unchanged" in out.stdout


def test_nginx_site_predating_the_assets_rule_is_rerendered_and_reloaded(scratch_repo):
    site = _nginx_site(scratch_repo, OLD_SITE_TLS)
    out = _dry_run(scratch_repo)
    assert out.returncode == 0, out.stderr
    plan = out.stdout
    assert "rendered for tls=true port=8443" in plan
    assert "site differs from the rendered template" in plan
    # the printed diff shows the operator what changes: the new /assets/ rule and headers
    assert "+  location /assets/ {" in plan
    assert '+    add_header Cache-Control "public, max-age=31536000, immutable";' in plan
    assert "+    try_files $uri =404;" in plan
    # root-run, in order: test the CURRENT config (r7-e: a pre-existing failure skips the
    # step instead of being blamed on the rendering), keep the previous file, install, test,
    # reload — all before the restart
    lines = _plan_lines(plan)
    tests = [i for i, line in enumerate(lines) if line == "+ nginx -t"]
    assert len(tests) == 2, lines
    i_precheck, i_test = tests
    i_prev = lines.index(f"+ install -m 0644 {site} {site}.prev")
    i_install = next(i for i, line in enumerate(lines)
                     if line.startswith("+ install -m 0644 /tmp/") and line.endswith(f" {site}"))
    i_reload = lines.index("+ systemctl reload nginx")
    i_restart = lines.index("+ systemctl restart launchpad-backend")
    assert i_precheck < i_prev < i_install < i_test < i_reload < i_restart
    assert plan.index("checking the CURRENT nginx config") < plan.index(f"{site}.prev")
    assert plan.index("bootstrap.py --skip-cdk") < plan.index("12/14 nginx site")
    # the dry run never touched the site itself
    assert site.read_text() == OLD_SITE_TLS and not (site.parent / "launchpad.prev").exists()


def test_nginx_site_without_a_listen_line_is_left_alone_with_a_warning(scratch_repo):
    _nginx_site(scratch_repo, "server {\n  # hand-edited\n}\n")
    out = _dry_run(scratch_repo)
    assert out.returncode == 0, out.stderr
    assert "no recognisable 'listen <port>' line" in out.stderr
    assert _nginx_lines(out.stdout) == []


def test_nginx_step_is_skipped_when_the_checkout_lib_predates_refresh_nginx_site(scratch_repo):
    # r7-e: `--branch` onto a tree whose nginx-site.sh (r7-B) only renders — the NEW script
    # must not die on an undefined function; it skips with a note and the restart still runs
    lib = scratch_repo / "infra" / "ec2" / "nginx-site.sh"
    body = lib.read_text(encoding="utf-8")
    lib.write_text(body[: body.index("_nginx_site_exec()")], encoding="utf-8")
    _git(scratch_repo, "commit", "-q", "-am", "r7-B lib (render only)")
    _nginx_site(scratch_repo, OLD_SITE_TLS)
    out = _dry_run(scratch_repo)
    assert out.returncode == 0, out.stderr
    assert "has no refresh_nginx_site (pre-r7-e)" in out.stdout
    assert _nginx_lines(out.stdout) == []
    assert "+ systemctl restart launchpad-backend" in _plan_lines(out.stdout)


def test_bootstrap_and_host_update_render_the_site_from_the_same_file():
    body = (Path(__file__).parents[2] / "infra" / "ec2" / "bootstrap-host.sh").read_text()
    assert "nginx-site.sh" in body and "render_nginx_site " in body
    assert "cat >/etc/nginx/sites-available/launchpad" not in body   # no second copy of the rules
    script = SCRIPT.read_text(encoding="utf-8")
    assert 'NGINX_LIB="$ROOT/infra/ec2/nginx-site.sh"' in script
    assert "try_files" not in script   # host-update inlines nothing
    # r7-e: the whole refresh lives in the lib (unit-tested with fake binaries in
    # infra/tests/test_nginx_site.py); host-update only passes its `run` hook
    assert 'refresh_nginx_site "$NGINX_SITE" "$ROOT/frontend/dist" run' in script
    assert 'render_nginx_site "' not in script and "run nginx -t" not in script
    assert "LAUNCHPAD_BACKEND_PORT" not in script   # dropped — nothing else ever read it


# ── N11-obs55: CDK-owned changes are reported when --with-cdk is missing ───────────────
# Live (2026-10-12): a routine update to a tip that changed
# backend/lambdas/memory_extractor/handler.py left the deployed extractor Lambda on the OLD
# code — the handler is a CDK asset of launchpad-memory[-env], step 9 runs only with
# --with-cdk, and nothing else redeploys Lambda code; the operator noticed only when asked.
# The script now diffs <old>..<new> against CDK_PATHS (the one list of CDK-owned paths) and,
# without the flag, warns on stdout AND stderr, names it in step 9 and in the summary
# (`cdk: pending (<n> files …)`) — rc stays 0. A dry run plans against the fetched tip.


def _fetched_tip(repo: Path, files: dict[str, str]) -> tuple[str, str]:
    """Commit `files` as the tip a fetch left in origin/cn/integration, then move the
    checkout back to the previous HEAD: the next run sees <old> -> <tip>."""
    old = _head(repo)
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
        _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", "incoming")
    new = _head(repo)
    _git(repo, "update-ref", "refs/remotes/origin/cn/integration", new)
    _git(repo, "reset", "-q", "--hard", old)
    return old, new


def _summary(plan: str) -> str:
    return next(line for line in plan.splitlines() if line.startswith("══ host-update done"))


def _cdk_paths_from_script() -> list[str]:
    text = SCRIPT.read_text(encoding="utf-8")
    block = text[text.index("CDK_PATHS=(") + len("CDK_PATHS=("):]
    block = block[: block.index(")")]
    return [tok for tok in block.split() if tok and not tok.startswith("#")]


def test_changed_lambda_without_with_cdk_warns_on_both_streams_and_in_the_summary(scratch_repo):
    old, new = _fetched_tip(
        scratch_repo, {"backend/lambdas/memory_extractor/handler.py": "def handler(): ...\n"}
    )
    out = _dry_run(scratch_repo)
    assert out.returncode == 0, out.stderr          # never an abort — the rest is valid
    plan = out.stdout
    assert f"{old} -> {new} (1 files)" in plan        # the dry run planned against the tip
    assert "planning against the fetched origin/cn/integration tip" in plan
    head = (f"WARNING: CDK-owned files changed in {old}..{new} but --with-cdk was not given"
            " — these are NOT applied:")
    for stream in (plan, out.stderr):
        assert head in stream, stream
        assert "    backend/lambdas/memory_extractor/handler.py" in stream
        assert "re-run with --with-cdk" in stream
    # step 9 repeats it and still names the flag; step 10 (bootstrap) and the restart run
    step9 = plan[plan.index("9/14 cdk"):plan.index("10/14 bootstrap")]
    assert "1 CDK-owned file changed" in step9 and "NOT applied" in step9
    assert "pass --with-cdk" in step9 and "cdk deploy" not in step9
    assert "bootstrap.py --skip-cdk" in plan and "systemctl restart launchpad-backend" in plan
    assert "cdk: pending (1 file — re-run with --with-cdk)" in _summary(plan)
    assert "cdk deployed" not in plan


def test_several_cdk_owned_files_are_all_listed_and_counted(scratch_repo):
    files = {
        "infra/stacks/memory_stack.py": "class LaunchpadMemoryStack: ...\n",
        "infra/uv.lock": "a new lock\n",
        "samples/rest_api/app.py": "def handler(): ...\n",
        "backend/app/main.py": "print('not cdk-owned')\n",
    }
    _fetched_tip(scratch_repo, files)
    out = _dry_run(scratch_repo)
    assert out.returncode == 0, out.stderr
    warning = out.stderr[out.stderr.index("WARNING: CDK-owned"):]
    for rel in ("infra/stacks/memory_stack.py", "infra/uv.lock", "samples/rest_api/app.py"):
        assert f"    {rel}\n" in warning
    assert "backend/app/main.py" not in warning
    assert "cdk: pending (3 files — re-run with --with-cdk)" in _summary(out.stdout)


def test_host_side_infra_changes_are_not_cdk_owned_and_do_not_warn(scratch_repo):
    _fetched_tip(scratch_repo, {
        "infra/ec2/bootstrap-host.sh": "#!/usr/bin/env bash\necho host script\n",
        "infra/ec2/launchpad-cn-ec2.yaml": "Resources: {}\n",
        "infra/spoke/launchpad-workspace-role.yaml": "Resources: {}\n",
        "infra/tests/test_stack.py": "def test_x(): ...\n",
        "backend/app/main.py": "print('code only')\n",
    })
    out = _dry_run(scratch_repo)
    assert out.returncode == 0, out.stderr
    assert "WARNING" not in out.stdout + out.stderr
    assert "cdk: no CDK-owned changes" in out.stdout
    assert "cdk: no changes" in _summary(out.stdout)
    assert "pass --with-cdk" in out.stdout            # step 9 still names the opt-in


def test_with_cdk_names_the_changed_files_before_deploying(scratch_repo):
    _fetched_tip(scratch_repo, {
        "backend/lambdas/memory_extractor/handler.py": "def handler(): ...\n",
        "infra/stacks/base_stack.py": "class LaunchpadBaseStack: ...\n",
    })
    out = _dry_run(scratch_repo, "--with-cdk", env={"LAUNCHPAD_ENV_NAME": "t1"})
    assert out.returncode == 0, out.stderr
    plan = out.stdout
    assert "WARNING" not in plan + out.stderr
    assert "cdk: 2 CDK-owned files changed — step 9 deploys" in plan   # step 5
    line = "cdk: 2 CDK-owned files changed — deploying"
    assert plan.index(line) < plan.index("cdk deploy --all")            # step 9, before the deploy
    assert "cdk: deployed (2 files)" in _summary(plan)


def test_with_cdk_and_no_cdk_owned_changes_prints_the_no_op_line_and_still_deploys(scratch_repo):
    _fetched_tip(scratch_repo, {"backend/app/main.py": "print('code only')\n"})
    out = _dry_run(scratch_repo, "--with-cdk")
    assert out.returncode == 0, out.stderr
    plan = out.stdout
    assert "cdk: no CDK-owned changes — deploy is a no-op" in plan
    assert "cdk deploy --all" in plan                 # D17 migration / drift still applied
    assert "cdk: deployed (no CDK-owned changes)" in _summary(plan)


def test_dry_run_ignores_a_stale_origin_ref_that_is_not_a_fast_forward_of_head(scratch_repo):
    # origin/<branch> BEHIND HEAD (a stale ref the dry run did not fetch) must not produce a
    # reverse diff: the plan is HEAD -> HEAD, and nothing is reported as changed
    base = _head(scratch_repo)
    _git(scratch_repo, "update-ref", "refs/remotes/origin/cn/integration", base)
    head = _commit(scratch_repo, "backend/lambdas/memory_extractor/handler.py", "x\n")
    out = _dry_run(scratch_repo)
    assert out.returncode == 0, out.stderr
    assert f"{head} -> {head} (0 files)" in out.stdout
    assert "planning against" not in out.stdout
    assert "WARNING" not in out.stdout + out.stderr
    assert "cdk: no changes" in _summary(out.stdout)


def test_cdk_paths_cover_every_asset_the_stacks_package_and_nothing_host_side():
    root = SCRIPT.parents[1]
    paths = _cdk_paths_from_script()
    assert paths == [
        "infra/app.py", "infra/cdk.json", "infra/pyproject.toml", "infra/uv.lock",
        "infra/stacks", "backend/lambdas", "samples/hr_database_lambda", "samples/rest_api",
    ]
    for rel in paths:
        assert (root / rel).exists(), rel
    # every Code.from_asset(...) directory in the CDK app resolves under a CDK-owned path
    import re

    assets: list[Path] = []
    for stack in sorted((root / "infra" / "stacks").glob("*.py")):
        text = stack.read_text(encoding="utf-8")
        if "LAMBDA_DIR" in text and "from_asset(str(LAMBDA_DIR)" in text:
            assets.append(root / "backend" / "lambdas" / "memory_extractor")
        for m in re.finditer(r'from_asset\(str\(SAMPLES_DIR / "([^"]+)"\)', text):
            assets.append(root / "samples" / m.group(1))
    assert len(assets) >= 3, assets
    owned = [root / rel for rel in paths]
    for asset in assets:
        assert asset.is_dir(), asset
        assert any(asset == o or o in asset.parents for o in owned), asset
    # host-side infra/ is deliberately NOT in the list (applied by other paths — §8.2)
    for rel in ("infra/ec2", "infra/spoke", "infra/tests", "infra"):
        assert rel not in paths, rel
    # deploy.md §8.1 mirrors the list under the paragraph the warning points at
    deploy = (root / "docs" / "cn" / "interfaces" / "deploy.md").read_text(encoding="utf-8")
    heading = "**CDK-owned paths and --with-cdk (N11-obs55).**"   # what the WARNING points at
    assert heading in deploy
    para = deploy[deploy.index(heading):]
    para = para[: para.index("\n\n**")] if "\n\n**" in para else para
    for rel in paths:
        assert f"`{rel}" in para, rel
    assert "cdk: pending" in deploy


def test_help_describes_the_cdk_owned_detection():
    out = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True)
    assert out.returncode == 0
    assert "CDK_PATHS" in out.stdout and "cdk: pending" in out.stdout


# ─── N11-obs47: step 12b, the public origin ────────────────────────────────────────────
# /etc/launchpad/env caches LAUNCHPAD_PUBLIC_BASE_URL=<scheme>://<ip>:<port> from the first
# boot (bootstrap-host.sh). t1's in-place stack update re-assigned the public IPv4 and the
# file kept the old one; the EC2 template now allocates an Elastic IP (one address change on
# the way in), and this step rewrites the cached line from IMDSv2 public-ipv4 — which reports
# the EIP once associated — whenever the host part is an IPv4 literal that differs.


def _env_file(repo: Path, origin: str) -> Path:
    env = repo.parent / "launchpad-env"
    env.write_text(f"LAUNCHPAD_PUBLIC_BASE_URL={origin}\n")
    return env


def _origin_run(repo: Path, env: Path, public_ip: str) -> subprocess.CompletedProcess:
    # LAUNCHPAD_PUBLIC_IP stands in for the IMDS probe; an empty value falls through to the
    # probe, which is pointed at the discard port so it fails fast and hermetically.
    return _dry_run(repo, env={"LAUNCHPAD_ENV_FILE": str(env), "LAUNCHPAD_PUBLIC_IP": public_ip,
                               "LAUNCHPAD_IMDS_ENDPOINT": "http://127.0.0.1:9"})


def test_public_origin_step_rewrites_an_ipv4_origin_that_drifted_from_imds(scratch_repo):
    env = _env_file(scratch_repo, "https://203.0.113.40:8443")
    out = _origin_run(scratch_repo, env, "203.0.113.80")
    assert out.returncode == 0, out.stderr
    plan = out.stdout
    # between the nginx refresh and the restart, so the restarted backend sees the new value
    assert plan.index("12/14 nginx site") < plan.index("12b/14 public origin") < plan.index(
        "13/14 restart"
    )
    assert "https://203.0.113.40:8443 -> https://203.0.113.80:8443" in plan
    assert ("+ sed -i s|^LAUNCHPAD_PUBLIC_BASE_URL=.*|LAUNCHPAD_PUBLIC_BASE_URL="
            f"https://203.0.113.80:8443| {env}") in plan
    assert "https://203.0.113.80:8443/auth/return" in plan   # the 3LO return URL to re-register
    assert env.read_text() == "LAUNCHPAD_PUBLIC_BASE_URL=https://203.0.113.40:8443\n"  # dry run


def test_public_origin_step_leaves_a_matching_or_hostname_origin_alone(scratch_repo):
    env = _env_file(scratch_repo, "https://203.0.113.80:8443")
    out = _origin_run(scratch_repo, env, "203.0.113.80")
    assert out.returncode == 0, out.stderr
    assert "unchanged — https://203.0.113.80:8443 matches IMDS public-ipv4" in out.stdout
    assert "sed -i" not in out.stdout
    # an operator-set hostname (DNS / reverse proxy / ICP-filed domain, deploy.md §3) is
    # never rewritten, whatever IMDS says
    env = _env_file(scratch_repo, "https://console.corp.example:8443")
    out = _origin_run(scratch_repo, env, "203.0.113.80")
    assert out.returncode == 0, out.stderr
    assert ("kept — https://console.corp.example:8443: host is not an IPv4 literal"
            in out.stdout)
    assert "sed -i" not in out.stdout


def test_public_origin_step_is_skipped_without_an_env_file_or_a_public_ip(scratch_repo):
    out = _dry_run(scratch_repo)   # the default harness: no env file -> no probe at all
    assert out.returncode == 0, out.stderr
    assert "12b/14 public origin" in out.stdout
    assert "skipped — no readable" in out.stdout
    # an env file without the line (origin set through a systemd drop-in, prod runbook)
    env = scratch_repo.parent / "launchpad-env"
    env.write_text("LAUNCHPAD_RUN_MODE=prod\n")
    out = _origin_run(scratch_repo, env, "203.0.113.80")
    assert "has no LAUNCHPAD_PUBLIC_BASE_URL line" in out.stdout
    # off EC2 / no public address: the probe is empty and the value is kept
    env = _env_file(scratch_repo, "http://10.44.0.12:8443")
    out = _origin_run(scratch_repo, env, "")
    assert out.returncode == 0, out.stderr
    assert "skipped — IMDS reports no public IPv4" in out.stdout
    assert env.read_text() == "LAUNCHPAD_PUBLIC_BASE_URL=http://10.44.0.12:8443\n"


def _refresh_public_base_url(env_file: Path, public_ip: str) -> str:
    """Run the script's refresh function alone, for real (DRY_RUN=0), against a scratch file."""
    text = SCRIPT.read_text(encoding="utf-8")
    fns = ""
    for header in ("\nrun() {", "\nimds_public_ipv4() {", "\nrefresh_public_base_url() {"):
        start = text.index(header)
        fns += text[start:text.index("\n}\n", start) + 3]
    out = subprocess.run(
        ["bash", "-c",
         f"set -Eeuo pipefail; DRY_RUN=0; LAUNCHPAD_PUBLIC_IP={public_ip}; "
         f"IMDS_ENDPOINT=http://127.0.0.1:9; {fns}\nrefresh_public_base_url \"$1\"",
         "_", str(env_file)],
        capture_output=True, text=True, check=True,
    )
    return out.stdout


def test_refresh_public_base_url_rewrites_only_that_line_keeping_scheme_port_and_mode(tmp_path):
    env = tmp_path / "env"
    env.write_text(
        "AWS_BEARER_TOKEN_BEDROCK=not-a-real-key\n"
        "LAUNCHPAD_PUBLIC_BASE_URL=http://203.0.113.40:8443\n"
        "LAUNCHPAD_RUN_MODE=prod\n"
    )
    env.chmod(0o600)
    out = _refresh_public_base_url(env, "203.0.113.80")
    assert "http://203.0.113.40:8443 -> http://203.0.113.80:8443" in out
    assert env.read_text() == (
        "AWS_BEARER_TOKEN_BEDROCK=not-a-real-key\n"
        "LAUNCHPAD_PUBLIC_BASE_URL=http://203.0.113.80:8443\n"
        "LAUNCHPAD_RUN_MODE=prod\n"
    )
    assert (env.stat().st_mode & 0o777) == 0o600   # sed -i keeps the secrets file's mode
    # idempotent: a second run is a no-op
    assert "unchanged" in _refresh_public_base_url(env, "203.0.113.80")


def test_public_nlb_origin_survives_a_host_update(scratch_repo):
    env = _env_file(scratch_repo, "https://203.0.113.30:8443")
    env.write_text(env.read_text() + "LAUNCHPAD_CONSOLE_ENTRY=public_nlb\n")
    before = env.read_text()
    out = _origin_run(scratch_repo, env, "203.0.113.80")
    assert out.returncode == 0, out.stderr
    assert "public_nlb" in out.stdout
    assert "https://203.0.113.80:8443/auth/return" not in out.stdout
    # Run the actual mutation path too: this must remain a no-op, not just a dry-run claim.
    assert "public_nlb" in _refresh_public_base_url(env, "203.0.113.80")
    assert env.read_text() == before


def test_explicit_direct_entry_still_refreshes_the_host_address(tmp_path):
    env = tmp_path / "env"
    env.write_text(
        "LAUNCHPAD_CONSOLE_ENTRY=direct\n"
        "LAUNCHPAD_PUBLIC_BASE_URL=https://203.0.113.40:8443\n"
    )
    _refresh_public_base_url(env, "203.0.113.80")
    assert env.read_text() == (
        "LAUNCHPAD_CONSOLE_ENTRY=direct\n"
        "LAUNCHPAD_PUBLIC_BASE_URL=https://203.0.113.80:8443\n"
    )


def test_help_names_the_public_origin_knobs():
    out = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True)
    assert out.returncode == 0
    assert "LAUNCHPAD_PUBLIC_IP" in out.stdout and "LAUNCHPAD_IMDS_ENDPOINT" in out.stdout
    assert "12b" in out.stdout


# ── N17-obs472: a CDK change a plain update skipped stays pending until --with-cdk ─────────
# Live (2026-10-26, t1): the obs465 builder ECR-pull grant (infra/stacks/base_stack.py) was
# skipped by a plain update (`cdk: pending`), and the NEXT plain update — whose <old>..<new>
# no longer contained the file — said "cdk: no changes" while the stack still drifted. The
# script now records what a plain update skipped in data/cdk-pending, reports it on EVERY
# update until a --with-cdk run applies it, and only that successful run clears it.

PENDING = "data/cdk-pending"


def _pending(repo: Path, *lines: str) -> Path:
    path = repo / PENDING
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines))
    return path


def test_a_skipped_cdk_change_is_recorded_for_the_next_updates(scratch_repo):
    old, new = _fetched_tip(scratch_repo, {"infra/stacks/base_stack.py": "class Base: ...\n"})
    out = _dry_run(scratch_repo)
    assert out.returncode == 0, out.stderr
    record = next(line for line in out.stdout.splitlines() if PENDING in line and "+ " in line)
    assert f"{old}..{new} infra/stacks/base_stack.py" in record
    assert "cdk: pending (1 file — re-run with --with-cdk)" in _summary(out.stdout)


def test_a_carried_pending_change_is_reported_even_when_this_range_has_none(scratch_repo):
    _pending(scratch_repo, "aaaa111..bbbb222 infra/stacks/base_stack.py")
    _fetched_tip(scratch_repo, {"backend/app/main.py": "print('code only')\n"})
    out = _dry_run(scratch_repo)
    assert out.returncode == 0, out.stderr
    head = ("WARNING: CDK-owned changes skipped by an earlier plain update are still NOT applied"
            f" ({PENDING}):")
    for stream in (out.stdout, out.stderr):
        assert head in stream, stream
        assert "    aaaa111..bbbb222 infra/stacks/base_stack.py" in stream
    step9 = out.stdout[out.stdout.index("9/14 cdk"):out.stdout.index("10/14 bootstrap")]
    assert "1 carried CDK-owned file still NOT applied" in step9
    assert "cdk: pending (1 carried file — re-run with --with-cdk)" in _summary(out.stdout)
    assert "cdk: no changes" not in _summary(out.stdout)


def test_new_and_carried_pending_changes_add_up(scratch_repo):
    _pending(scratch_repo, "aaaa111..bbbb222 infra/stacks/base_stack.py")
    _fetched_tip(scratch_repo, {"backend/lambdas/memory_extractor/handler.py": "x = 1\n"})
    out = _dry_run(scratch_repo)
    assert "cdk: pending (1 file + 1 carried file — re-run with --with-cdk)" in _summary(out.stdout)


def test_only_a_with_cdk_run_clears_the_marker_after_the_deploy(scratch_repo):
    _pending(scratch_repo, "aaaa111..bbbb222 infra/stacks/base_stack.py")
    _fetched_tip(scratch_repo, {"backend/app/main.py": "print('code only')\n"})
    out = _dry_run(scratch_repo, "--with-cdk", env={"LAUNCHPAD_ENV_NAME": "t1"})
    assert out.returncode == 0, out.stderr
    plan = out.stdout
    assert "step 9 applies 1 carried CDK-owned file" in plan
    clear = next(line for line in plan.splitlines() if "rm -f" in line and PENDING in line)
    assert plan.index("cdk deploy --all") < plan.index(clear)   # cleared only after the deploy
    assert "cdk: deployed (no CDK-owned changes; 1 carried file applied)" in _summary(plan)
    assert "WARNING" not in plan + out.stderr
    # a plain run never clears it
    plain = _dry_run(scratch_repo)
    assert not any("rm -f" in line and PENDING in line for line in plain.stdout.splitlines())


# ── N17-obs485: no `uv run` on a host re-locks against the env's CN index mirror ──────────
# Live (2026-10-27, t1 + default): after every update backend/uv.lock (and infra/uv.lock after
# a --with-cdk run) was left modified — 125 / 28 `source = { registry = … }` lines rewritten
# from pypi.org to the UV_DEFAULT_INDEX mirror. A plain `uv run` re-locks first; `--frozen`
# (and UV_FROZEN=1) never touches the lock but also never checks it, so a stale lock is
# caught by scripts/uv_lock_fresh.py instead.

FRESH = Path(__file__).parents[2] / "scripts" / "uv_lock_fresh.py"


def test_every_uv_run_in_the_script_is_frozen():
    import re

    text = SCRIPT.read_text(encoding="utf-8")
    runs = re.findall(r'"\$UV" run\b[^\n]*', text)
    assert len(runs) == 3, runs   # step 9 precondition + cdk deploy, step 10 bootstrap
    assert all(r.startswith('"$UV" run --frozen ') for r in runs), runs
    assert '"$UV" sync --frozen' in text   # step 6


def test_with_cdk_plan_runs_the_precondition_deploy_and_bootstrap_frozen(scratch_repo):
    out = _dry_run(scratch_repo, "--with-cdk", env={"LAUNCHPAD_ENV_NAME": "t1"})
    assert out.returncode == 0, out.stderr
    uv_lines = [line for line in _plan_lines(out.stdout) if "/bin/true run" in line]
    assert len(uv_lines) == 3, uv_lines
    assert all("/bin/true run --frozen " in line for line in uv_lines), uv_lines


def _unit(repo: Path, exec_start: str, *, dropin: bool = True) -> Path:
    d = _systemd_dir(repo)
    if dropin:
        path = d / "launchpad-backend.service.d" / "10-execstart.conf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"[Service]\nExecStart=\nExecStart={exec_start}\n")
    else:
        d.mkdir(parents=True, exist_ok=True)
        path = d / "launchpad-backend.service"
        path.write_text(f"[Service]\nUser=ubuntu\nExecStart={exec_start}\nRestart=on-failure\n")
    return path


UNFROZEN = ("/usr/local/bin/uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 "
            "--proxy-headers --forwarded-allow-ips=127.0.0.1")


def test_uv_frozen_step_plans_the_env_line_and_the_execstart_before_the_restart(scratch_repo):
    env = scratch_repo.parent / "launchpad-env"
    env.write_text("UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple\n")
    dropin = _unit(scratch_repo, UNFROZEN)
    out = _dry_run(scratch_repo, env={"LAUNCHPAD_ENV_FILE": str(env),
                                      "LAUNCHPAD_PUBLIC_IP": "",
                                      "LAUNCHPAD_IMDS_ENDPOINT": "http://127.0.0.1:9"})
    assert out.returncode == 0, out.stderr
    plan = out.stdout
    assert plan.index("12b/14 public origin") < plan.index("12c/14 uv frozen") < plan.index(
        "13/14 restart"
    )
    step = plan[plan.index("12c/14 uv frozen"):plan.index("13/14 restart")]
    assert f"env: + UV_FROZEN=1 ({env})" in step
    assert f"unit: {dropin}: uv run uvicorn -> uv run --frozen uvicorn" in step
    assert "+ systemctl daemon-reload" in step
    # a dry run writes nothing
    assert env.read_text() == "UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple\n"
    assert f"ExecStart={UNFROZEN}\n" in dropin.read_text()


def test_uv_frozen_step_is_a_no_op_on_a_host_already_frozen(scratch_repo):
    env = scratch_repo.parent / "launchpad-env"
    env.write_text("UV_FROZEN=1\n")
    _unit(scratch_repo, UNFROZEN.replace("uv run uvicorn", "uv run --frozen uvicorn"))
    out = _dry_run(scratch_repo, env={"LAUNCHPAD_ENV_FILE": str(env), "LAUNCHPAD_PUBLIC_IP": "",
                                      "LAUNCHPAD_IMDS_ENDPOINT": "http://127.0.0.1:9"})
    assert out.returncode == 0, out.stderr
    step = out.stdout[out.stdout.index("12c/14 uv frozen"):out.stdout.index("13/14 restart")]
    assert "env: unchanged — UV_FROZEN=1" in step
    assert "unit: unchanged — ExecStart already runs `uv run --frozen uvicorn`" in step
    assert "+ " not in step   # nothing to run


def _ensure_env(env_file: Path) -> subprocess.CompletedProcess:
    """Run the step's env function for real (DRY_RUN=0) against a scratch file."""
    text = SCRIPT.read_text(encoding="utf-8")
    fns = ""
    for header in ("\nrun() {", "\nensure_uv_frozen_env() {"):
        start = text.index(header)
        fns += text[start:text.index("\n}\n", start) + 3]
    return subprocess.run(
        ["bash", "-c", f'set -Eeuo pipefail; DRY_RUN=0; {fns}\nensure_uv_frozen_env "$1"',
         "_", str(env_file)],
        capture_output=True, text=True, check=True,
    )


def test_ensure_uv_frozen_env_appends_once_keeping_the_mode_and_a_missing_newline(tmp_path):
    env = tmp_path / "env"
    env.write_text("AWS_BEARER_TOKEN_BEDROCK=not-a-real-key\nLAUNCHPAD_RUN_MODE=prod")  # no \n
    env.chmod(0o600)
    out = _ensure_env(env)
    assert "env: + UV_FROZEN=1" in out.stdout
    assert env.read_text() == (
        "AWS_BEARER_TOKEN_BEDROCK=not-a-real-key\nLAUNCHPAD_RUN_MODE=prod\nUV_FROZEN=1\n"
    )
    assert (env.stat().st_mode & 0o777) == 0o600
    # idempotent: the second run changes nothing
    assert "env: unchanged — UV_FROZEN=1" in _ensure_env(env).stdout
    assert env.read_text().count("UV_FROZEN=") == 1
    # an operator's UV_FROZEN=0 is corrected in place, not duplicated
    env.write_text("UV_FROZEN=0\nLAUNCHPAD_RUN_MODE=prod\n")
    assert "env: UV_FROZEN=0 -> 1" in _ensure_env(env).stdout
    assert env.read_text() == "UV_FROZEN=1\nLAUNCHPAD_RUN_MODE=prod\n"


def _ensure_unit(systemd_dir: Path) -> subprocess.CompletedProcess:
    text = SCRIPT.read_text(encoding="utf-8")
    fns = ""
    for header in ("\nrun() {", "\nensure_uv_frozen_unit() {"):
        start = text.index(header)
        fns += text[start:text.index("\n}\n", start) + 3]
    return subprocess.run(
        ["bash", "-c",
         f'set -Eeuo pipefail; DRY_RUN=0; SYSTEMD_DIR="$1"; '
         f'systemctl() {{ echo "systemctl $*"; }}; {fns}\nensure_uv_frozen_unit launchpad-backend',
         "_", str(systemd_dir)],
        capture_output=True, text=True, check=True,
    )


@pytest.mark.parametrize("dropin", [True, False], ids=["drop-in", "unit-file"])
def test_ensure_uv_frozen_unit_rewrites_only_the_execstart_and_reloads_once(scratch_repo, dropin):
    path = _unit(scratch_repo, UNFROZEN, dropin=dropin)
    before = path.read_text()
    out = _ensure_unit(_systemd_dir(scratch_repo))
    assert out.stdout.count("systemctl daemon-reload") == 1
    assert path.read_text() == before.replace("uv run uvicorn", "uv run --frozen uvicorn")
    assert "ExecStart=\n" in path.read_text() or not dropin   # the reset line is kept
    # idempotent: the second run neither edits nor reloads
    again = _ensure_unit(_systemd_dir(scratch_repo))
    assert "unchanged" in again.stdout and "daemon-reload" not in again.stdout
    assert path.read_text().count("--frozen") == 1


def test_ensure_uv_frozen_unit_leaves_a_unit_without_uv_run_alone(scratch_repo):
    path = _unit(scratch_repo, "/home/ubuntu/launchpad/backend/.venv/bin/uvicorn app.main:app")
    before = path.read_text()
    out = _ensure_unit(_systemd_dir(scratch_repo))
    assert "unit: skipped — no ExecStart running `uv run uvicorn`" in out.stdout
    assert "daemon-reload" not in out.stdout
    assert path.read_text() == before


def _project(base: Path, deps: list[str], locked: list[dict], *, registry: str) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    (base / "pyproject.toml").write_text(
        '[project]\nname = "launchpad-backend"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n'
        "dependencies = [" + ", ".join(f'"{d}"' for d in deps) + "]\n"
        '\n[dependency-groups]\ndev = ["pytest>=8.0"]\n'
    )
    dist = ", ".join(
        f'{{ name = "{e["name"]}", specifier = "{e["specifier"]}" }}' for e in locked
    )
    (base / "uv.lock").write_text(
        'version = 1\nrevision = 3\nrequires-python = ">=3.12"\n\n'
        '[[package]]\nname = "fastapi"\nversion = "0.115.0"\n'
        f'source = {{ registry = "{registry}" }}\n\n'
        '[[package]]\nname = "launchpad-backend"\nversion = "0.1.0"\n'
        'source = { editable = "." }\n\n'
        f"[package.metadata]\nrequires-dist = [{dist}]\n\n"
        '[package.metadata.requires-dev]\ndev = [{ name = "pytest", specifier = ">=8.0" }]\n'
    )
    return base


def _fresh(project: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["python3", "-I", str(FRESH), str(project)],
                          capture_output=True, text=True)


def test_lock_freshness_accepts_the_committed_locks_and_a_mirror_rewritten_one(tmp_path):
    root = Path(__file__).parents[2]
    for project in ("backend", "infra"):
        out = _fresh(root / project)
        assert out.returncode == 0, out.stderr
        assert "matches pyproject.toml" in out.stdout
    # the obs485 drift itself (registry URLs only) is NOT staleness
    locked = [{"name": "fastapi", "specifier": ">=0.115,<1"}]
    mirror = _project(tmp_path / "mirror", ["fastapi>=0.115, <1"], locked,
                      registry="https://pypi.tuna.tsinghua.edu.cn/simple")
    assert _fresh(mirror).returncode == 0


def test_lock_freshness_fails_loudly_on_a_dependency_the_lock_does_not_record(tmp_path):
    locked = [{"name": "fastapi", "specifier": ">=0.115,<1"}]
    stale = _project(tmp_path / "stale", ["fastapi>=0.115,<1", "six>=1"], locked,
                     registry="https://pypi.org/simple")
    out = _fresh(stale)
    assert out.returncode == 2
    assert "uv.lock is STALE" in out.stderr and "six" in out.stderr
    # a changed specifier is stale too
    bumped = _project(tmp_path / "bumped", ["fastapi>=0.120,<1"], locked,
                      registry="https://pypi.org/simple")
    assert _fresh(bumped).returncode == 2


def test_a_stale_lock_aborts_the_update_in_step_6_before_any_sync(scratch_repo):
    (scratch_repo / "scripts" / "uv_lock_fresh.py").write_text(FRESH.read_text())
    locked = [{"name": "fastapi", "specifier": ">=0.115,<1"}]
    _project(scratch_repo / "backend", ["fastapi>=0.115,<1", "six>=1"], locked,
             registry="https://pypi.org/simple")
    _git(scratch_repo, "add", "-A")
    _git(scratch_repo, "commit", "-q", "-m", "stale lock")
    out = _dry_run(scratch_repo)
    assert out.returncode != 0
    assert "the committed uv.lock is stale against pyproject.toml (N17-obs485)" in out.stderr
    assert "backend/uv.lock is STALE" in out.stderr
    assert not any("sync --frozen" in line for line in _plan_lines(out.stdout))
