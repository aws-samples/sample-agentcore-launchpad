"""infra/ec2/tls-cert.sh (N12-obs60, scan T-4): the console's self-signed certificate with a
subjectAltName. Hermetic — real openssl into a tmp dir; nothing is committed, no AWS."""

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "ec2" / "tls-cert.sh"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True)


def test_script_parses_is_executable_and_shellcheck_clean():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    assert SCRIPT.stat().st_mode & stat.S_IXUSR
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed")
    subprocess.run(["shellcheck", "-S", "warning", str(SCRIPT)], check=True)


def test_print_shows_the_san_extension_cn_first_with_auto_prefixes_and_no_duplicates():
    out = run("--print", "--san", "203.0.113.10,console.example", "--san", "IP:203.0.113.10",
              "--san", "DNS:launchpad.internal", "--san", " 2001:db8::1 ")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == (
        "subjectAltName=DNS:launchpad.internal,IP:203.0.113.10,DNS:console.example,IP:2001:db8::1"
    )
    out = run("--print", "--cn", "lp.internal")
    assert out.stdout.strip() == "subjectAltName=DNS:lp.internal"   # a SAN even with no --san


@pytest.mark.parametrize("bad", ["bad name!", "DNS:", "IP:999.1.1", "a..b", "--san"])
def test_an_entry_openssl_would_reject_is_refused_before_anything_is_written(bad, tmp_path: Path):
    out = run("--out-dir", str(tmp_path / "tls"), "--san", bad)
    assert out.returncode == 2, (out.stdout, out.stderr)
    assert "tls-cert:" in out.stderr
    assert not (tmp_path / "tls").exists()


def test_generates_key_and_cert_with_the_san_and_tight_modes(tmp_path: Path):
    if shutil.which("openssl") is None:
        pytest.skip("openssl not installed")
    out_dir = tmp_path / "tls"
    out = run("--out-dir", str(out_dir), "--san", "10.0.0.5", "--san", "DNS:console.example",
              "--days", "3")
    assert out.returncode == 0, out.stderr
    assert (out_dir.stat().st_mode & 0o777) == 0o700
    assert ((out_dir / "key.pem").stat().st_mode & 0o777) == 0o600
    assert ((out_dir / "cert.pem").stat().st_mode & 0o777) == 0o644
    assert sorted(p.name for p in out_dir.iterdir()) == ["cert.pem", "key.pem"]  # no temp files
    x509 = subprocess.run(
        ["openssl", "x509", "-in", str(out_dir / "cert.pem"), "-noout", "-subject",
         "-ext", "subjectAltName"], check=True, capture_output=True, text=True,
    ).stdout
    assert "CN = launchpad.internal" in x509 or "CN=launchpad.internal" in x509
    assert "DNS:launchpad.internal" in x509
    assert "IP Address:10.0.0.5" in x509 and "DNS:console.example" in x509
    assert "SAN       DNS:launchpad.internal,IP:10.0.0.5,DNS:console.example" in out.stdout
    assert "nginx -t && systemctl reload nginx" in out.stdout      # the operator's next step


def test_refuses_to_overwrite_without_force_and_replaces_with_it(tmp_path: Path):
    if shutil.which("openssl") is None:
        pytest.skip("openssl not installed")
    out_dir = tmp_path / "tls"
    assert run("--out-dir", str(out_dir), "--san", "10.0.0.5", "--days", "3").returncode == 0
    before = (out_dir / "cert.pem").read_bytes()
    out = run("--out-dir", str(out_dir), "--san", "10.0.0.6", "--days", "3")
    assert out.returncode == 3
    assert "already exists" in out.stderr and "--force" in out.stderr
    assert (out_dir / "cert.pem").read_bytes() == before            # untouched
    out = run("--out-dir", str(out_dir), "--san", "10.0.0.6", "--days", "3", "--force")
    assert out.returncode == 0, out.stderr
    assert (out_dir / "cert.pem").read_bytes() != before
    san = subprocess.run(
        ["openssl", "x509", "-in", str(out_dir / "cert.pem"), "-noout", "-ext", "subjectAltName"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert "IP Address:10.0.0.6" in san and "10.0.0.5" not in san


def test_usage_errors_and_help():
    out = run("--days", "abc", "--print")
    assert out.returncode == 2 and "positive integer" in out.stderr
    out = run("--bogus")
    assert out.returncode == 2 and "unknown option" in out.stderr
    out = run("--help")
    assert out.returncode == 0 and "--san ENTRY" in out.stdout and "--force" in out.stdout
    assert not Path("/etc/nginx/launchpad-tls/.nope").exists()     # nothing touched the default dir


def test_no_certificate_or_key_material_is_committed():
    repo = SCRIPT.parents[2]
    tracked = subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True, text=True)
    if tracked.returncode != 0:
        pytest.skip("not a git checkout")
    offenders = [f for f in tracked.stdout.splitlines()
                 if f.endswith((".pem", ".key", ".crt")) and "node_modules" not in f]
    assert offenders == []
