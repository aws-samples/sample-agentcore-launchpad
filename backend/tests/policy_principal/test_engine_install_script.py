"""policy-engine/packaging/install.sh resolves the AWS region before any aws call
(N17-obs481): `--region`, else the instance's own region from IMDSv2. Driven through the
non-root `--print-region` mode with a stub `curl` on PATH; no AWS, no root."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "policy-engine" / "packaging" / "install.sh"

STUB_CURL = """#!/usr/bin/env bash
# IMDSv2 stand-in: a token for the PUT, the region only for a request carrying that token
echo "$*" >> "$CURL_LOG"
args="$*"
if [[ "$args" == *"-X PUT"*"/latest/api/token"* ]]; then
  [[ -n "${STUB_TOKEN:-}" ]] && { printf '%s' "$STUB_TOKEN"; exit 0; }
  exit 22
fi
if [[ "$args" == *"/latest/meta-data/placement/region"* ]]; then
  [[ "$args" == *"X-aws-ec2-metadata-token: ${STUB_TOKEN:-}"* ]] || exit 22
  printf '%s' "$STUB_REGION"; exit 0
fi
exit 22
"""


def _run(tmp_path: Path, *args: str, token: str = "tok-1", region: str = "cn-northwest-1"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(STUB_CURL)
    curl.chmod(0o755)
    log = tmp_path / "curl.log"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "CURL_LOG": str(log),
           "STUB_TOKEN": token, "STUB_REGION": region}
    proc = subprocess.run(["bash", str(SCRIPT), "--print-region", *args], env=env,
                          capture_output=True, text=True, timeout=30)
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls


def test_region_comes_from_imdsv2_with_a_token(tmp_path):
    proc, calls = _run(tmp_path)
    assert (proc.returncode, proc.stdout.strip()) == (0, "cn-northwest-1"), proc.stderr
    assert any("-X PUT" in c and "/latest/api/token" in c for c in calls)
    assert any("placement/region" in c and "X-aws-ec2-metadata-token: tok-1" in c for c in calls)


def test_an_explicit_region_wins_and_imds_is_not_asked(tmp_path):
    proc, calls = _run(tmp_path, "--region", "cn-north-1")
    assert (proc.returncode, proc.stdout.strip()) == (0, "cn-north-1"), proc.stderr
    assert calls == []


def test_no_region_anywhere_refuses(tmp_path):
    proc, _ = _run(tmp_path, token="")
    assert proc.returncode == 1
    assert "no --region and IMDSv2 gave no region" in proc.stderr


@pytest.mark.parametrize("bad", ["", "cn northwest 1", "cn-northwest-1; rm", "CN-NORTHWEST-1"])
def test_a_malformed_region_is_refused(tmp_path, bad):
    proc, _ = _run(tmp_path, "--region", bad) if bad else _run(tmp_path, region="nope")
    assert proc.returncode == 1


def test_every_aws_call_carries_the_region_and_imds_goes_through_one_helper():
    text = SCRIPT.read_text()
    code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    aws_calls = [ln for ln in code if re.search(r"(^|[\s;(|&])aws\s", ln)]
    assert aws_calls, "install.sh no longer calls aws: update this guard"
    for line in aws_calls:
        assert '--region "$REGION"' in line, line
    imds_lines = [ln for ln in code if "169.254.169.254" in ln]
    assert imds_lines == ['IMDS="http://169.254.169.254"'], imds_lines
