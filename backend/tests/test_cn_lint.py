import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run_in(tmp_path, filename: str, content: str) -> subprocess.CompletedProcess:
    scope = tmp_path / "backend" / "app"
    scope.mkdir(parents=True)
    (scope / filename).write_text(content)
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts" / "cn_lint.sh"
    script.write_text((ROOT / "scripts" / "cn_lint.sh").read_text())
    return subprocess.run(["bash", str(script)], capture_output=True, text=True)


def test_cn_lint_flags_an_arn_literal(tmp_path):
    proc = _run_in(tmp_path, "bad.py", 'X = "arn:aws:s3:::b"\n')
    assert proc.returncode == 1 and "bad.py" in proc.stdout


def test_cn_lint_flags_an_escaped_regex(tmp_path):
    proc = _run_in(tmp_path, "bad.py", 'R = r"\\.amazonaws\\.com/"\n')
    assert proc.returncode == 1


def test_cn_lint_allows_marked_lines(tmp_path):
    proc = _run_in(tmp_path, "ok.py", 'SUFFIX = "amazonaws.com"  # cn-lint: allow\n')
    assert proc.returncode == 0


# ── Global console / sign-in hosts (cn/r11-i) ─────────────────────────────────────
# `services/traces.py` carried `console.aws.amazon.com` for months because the lint only
# matched `arn:aws:` / `.amazonaws.com`. Console hosts do not follow the DNS suffix, so
# they get their own patterns.


def test_cn_lint_flags_the_global_console_host(tmp_path):
    proc = _run_in(tmp_path, "bad.py", 'URL = f"https://{region}.console.aws.amazon.com/x"\n')
    assert proc.returncode == 1 and "bad.py" in proc.stdout


def test_cn_lint_flags_the_global_signin_host(tmp_path):
    proc = _run_in(tmp_path, "bad.py", 'URL = "https://signin.aws.amazon.com/"\n')
    assert proc.returncode == 1 and "bad.py" in proc.stdout


def test_cn_lint_flags_an_escaped_console_regex(tmp_path):
    proc = _run_in(tmp_path, "bad.py", 'R = r"console\\.aws\\.amazon\\.com"\n')
    assert proc.returncode == 1


def test_cn_lint_allows_a_marked_console_host(tmp_path):
    proc = _run_in(tmp_path, "ok.py", 'HOST = "console.aws.amazon.com"  # cn-lint: allow\n')
    assert proc.returncode == 0


def test_cn_lint_ignores_the_china_console_host(tmp_path):
    proc = _run_in(tmp_path, "ok.py", 'HOST = "console.amazonaws.cn"\n')
    assert proc.returncode == 0
