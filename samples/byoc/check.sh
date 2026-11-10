#!/usr/bin/env bash
# Hermetic check for the BYOC samples: byte-compile both entry points and run the
# stubbed pytest suite under samples/byoc/tests (no AWS, no network, no SDK needed —
# the tests fake bedrock_agentcore and boto3 before loading each sample).
#   bash samples/byoc/check.sh            # system python3 (needs pytest)
#   PYTHON="uv run --directory backend python" bash samples/byoc/check.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY=${PYTHON:-python3}
$PY -m py_compile "$HERE/hello-http/main.py" "$HERE/hello-container/main.py"
echo "py_compile: OK"
$PY -m pytest -q -p no:cacheprovider "$HERE/tests"
