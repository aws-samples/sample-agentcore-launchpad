"""The one-command upgrade (backend/scripts/upgrade.sh), run with bash against
temporary git repositories: fake `systemctl`, `uv` and `npm`, and a local HTTP server
standing in for the restarted console (it reports the checkout's HEAD as
version.sha, or 503 for a commit in `refuse`)."""

import json
import re
import shutil
import sqlite3
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
UPGRADE = BACKEND / "scripts" / "upgrade.sh"

pytestmark = pytest.mark.skipif(
    any(shutil.which(t) is None for t in ("git", "bash", "curl")),
    reason="needs git, bash and curl",
)


def sh(*args: str, cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, message: str, files: dict[str, str]) -> str:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    sh("git", "add", "-A", cwd=repo)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m",
       message, cwd=repo)
    return sh("git", "rev-parse", "HEAD", cwd=repo)


def fake_bin(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


class _Console(BaseHTTPRequestHandler):
    repo: Path
    refuse: set[str]

    def do_GET(self):  # noqa: N802
        head = sh("git", "rev-parse", "HEAD", cwd=self.repo)
        if head in self.refuse:
            self.send_response(503)
            self.end_headers()
            return
        body = json.dumps({"version": {"sha": head}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture
def host(tmp_path):
    """origin main (c1 → c2) and the live checkout at c1 with a built dist."""
    origin = tmp_path / "origin"
    origin.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=origin)
    c1 = commit(origin, "c1", {
        ".gitignore": "data/\nfrontend/dist*\nfrontend/node_modules*\n",
        "backend/scripts/inflight.py": "# the running version's probe (run through uv)\n",
        "README.md": "one\n",
    })
    live = tmp_path / "live"
    sh("git", "clone", "-q", str(origin), str(live), cwd=tmp_path)
    c2 = commit(origin, "c2", {
        "backend/scripts/ledger_backup.py":
            (BACKEND / "scripts" / "ledger_backup.py").read_text(encoding="utf-8"),
        "backend/scripts/rehearse_projects_migration.py": "# run through uv\n",
        "README.md": "two\n",
    })
    (live / "frontend" / "dist").mkdir(parents=True)
    (live / "frontend" / "dist" / "index.html").write_text("c1", encoding="utf-8")
    (live / "data").mkdir()
    with sqlite3.connect(live / "data" / "launchpad.db") as conn:
        conn.execute("CREATE TABLE agents (id TEXT)")
    (live / "config").mkdir()
    (live / "config" / "launchpad.yaml").write_text("region: us-west-2\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.log"
    knobs = tmp_path / "knobs"  # exit codes the fakes return: inflight / rehearse / build
    knobs.mkdir()
    systemctl = fake_bin(bin_dir / "systemctl", f"""
echo "systemctl $*" >>{calls}
case "$1" in
  cat) [ "$2" = launchpad-backend ] ;;
  *) exit 0 ;;
esac
""")
    uv = fake_bin(bin_dir / "uv", f"""
echo "uv $* in $PWD" >>{calls}
case "$*" in
  *inflight.py*) exit "$(cat {knobs}/inflight 2>/dev/null || echo 0)" ;;
  *rehearse_projects_migration.py*)
    echo "OK no drift"; exit "$(cat {knobs}/rehearse 2>/dev/null || echo 0)" ;;
esac
""")
    npm = fake_bin(bin_dir / "npm", f"""
echo "npm $* in $PWD" >>{calls}
if [ "$1 $2" = "run build" ]; then
  rc=$(cat {knobs}/build 2>/dev/null || echo 0); [ "$rc" = 0 ] || exit "$rc"
  mkdir -p dist && git rev-parse HEAD > dist/index.html
fi
""")
    handler = type("Console", (_Console,), {"repo": live, "refuse": set()})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin",
        "HOME": str(tmp_path),
        "LAUNCHPAD_SYSTEMCTL": str(systemctl),
        "LAUNCHPAD_UV": str(uv),
        "LAUNCHPAD_NPM": str(npm),
        "LAUNCHPAD_SUDO": "",
        "LAUNCHPAD_PYTHON": shutil.which("python3") or "python3",
    }
    url = f"http://127.0.0.1:{server.server_port}/api/auth/status"
    yield {"live": live, "origin": origin, "c1": c1, "c2": c2, "env": env, "calls": calls,
           "knobs": knobs, "refuse": handler.refuse, "url": url}
    server.shutdown()


def upgrade(host, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(UPGRADE), "--repo", str(host["live"]), "--health-url", host["url"],
         "--health-timeout", "4", "--min-free-gb", "0", *extra],
        env=host["env"], capture_output=True, text=True, timeout=120, check=False,
    )


def head(host) -> str:
    return sh("git", "rev-parse", "HEAD", cwd=host["live"])


def dist(host) -> str:
    return (host["live"] / "frontend" / "dist" / "index.html").read_text(encoding="utf-8").strip()


def backups(host) -> list[Path]:
    """The ledger backups themselves (their config / key companions share the prefix)."""
    return sorted(p for p in (host["live"] / "data").glob("launchpad.db.bak-*-preupgrade-*")
                  if re.fullmatch(r"launchpad\.db\.bak-\d{8}-\d{6}-preupgrade-[0-9a-f]{8}", p.name))


def restarts(host) -> list[str]:
    calls = host["calls"].read_text() if host["calls"].exists() else ""
    return [line for line in calls.splitlines() if line.startswith("systemctl restart")]


def test_upgrades_to_the_latest_console_v3(host):
    done = upgrade(host)
    assert done.returncode == 0, done.stdout + done.stderr
    assert head(host) == host["c2"]
    assert dist(host) == host["c2"]  # rebuilt, not the old dist
    assert (host["live"] / "frontend" / "dist.prev" / "index.html").read_text() == "c1"
    [backup] = backups(host)
    assert backup.name.endswith(f"-preupgrade-{host['c1'][:8]}")
    assert (backup.parent / f"{backup.name}.launchpad.yaml").is_file()
    conn = sqlite3.connect(backup)
    assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    conn.close()
    assert restarts(host) == ["systemctl restart launchpad-backend"]
    assert "UPGRADED" in done.stdout
    assert (host["live"] / "data" / "upgrade" / "prev-sha").read_text().strip() == host["c1"]


def test_a_second_run_is_a_no_op(host):
    assert upgrade(host).returncode == 0
    again = upgrade(host)
    assert again.returncode == 0
    assert "nothing to do" in again.stdout
    assert len(restarts(host)) == 1


def test_dry_run_backs_up_and_rehearses_but_changes_nothing(host):
    done = upgrade(host, "--dry-run")
    assert done.returncode == 0, done.stdout + done.stderr
    assert head(host) == host["c1"] and dist(host) == "c1"
    assert len(backups(host)) == 1
    assert "rehearse_projects_migration.py" in host["calls"].read_text()
    assert restarts(host) == []


def test_a_dirty_checkout_is_refused_before_anything_runs(host):
    (host["live"] / "README.md").write_text("local edit\n", encoding="utf-8")
    done = upgrade(host)
    assert done.returncode == 1
    assert "local changes" in done.stdout
    assert head(host) == host["c1"] and backups(host) == [] and restarts(host) == []


def test_a_target_that_does_not_fast_forward_is_refused(host):
    commit(host["live"], "local", {"LOCAL.md": "x\n"})
    done = upgrade(host)
    assert done.returncode == 1
    assert "not an ancestor" in done.stdout
    assert backups(host) == [] and restarts(host) == []


def test_work_a_restart_would_disrupt_stops_the_run_unless_yes(host):
    (host["knobs"] / "inflight").write_text("3")
    stopped = upgrade(host)
    assert stopped.returncode == 1
    assert "--yes" in stopped.stdout
    assert head(host) == host["c1"] and backups(host) == []
    assert upgrade(host, "--yes").returncode == 0
    assert head(host) == host["c2"]


def test_a_failed_rehearsal_stops_before_the_code_changes(host):
    (host["knobs"] / "rehearse").write_text("1")
    done = upgrade(host)
    assert done.returncode == 1
    assert "rehearsal failed" in done.stdout
    assert head(host) == host["c1"] and restarts(host) == []
    assert not list((host["live"] / "data" / "upgrade").glob("rehearsal-*"))  # worktree removed


def test_a_console_that_does_not_come_back_is_rolled_back(host):
    host["refuse"].add(host["c2"])
    done = upgrade(host)
    assert done.returncode == 2, done.stdout + done.stderr
    assert "ROLLED BACK" in done.stdout
    assert head(host) == host["c1"]
    assert dist(host) == "c1"  # the old build is back
    assert len(restarts(host)) == 2  # the upgrade, then the rollback


def test_a_failed_build_is_rolled_back_before_any_restart_of_new_code(host):
    (host["knobs"] / "build").write_text("1")
    done = upgrade(host)
    assert done.returncode == 2, done.stdout + done.stderr
    assert head(host) == host["c1"] and dist(host) == "c1"
    assert len(restarts(host)) == 1  # only the rollback's restart


def test_usage_errors(host):
    assert subprocess.run(["bash", str(UPGRADE)], capture_output=True, check=False).returncode == 64
    assert upgrade(host, "--bogus").returncode == 64
