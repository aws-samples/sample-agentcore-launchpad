#!/usr/bin/env bash
# Provision the studio local-debug execution interpreter.
#
# The launchpad control-plane backend is deliberately lean (no strands, no
# openai). Locally running un-deployed studio flows needs a separate
# interpreter that has the Strands runtime deps installed. This script creates
# an isolated uv venv at data/exec-venv and installs those deps into it. It is
# idempotent — re-running upgrades in place.
#
# Point the backend at a different interpreter with LAUNCHPAD_STUDIO_EXEC_PYTHON.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${REPO_ROOT}/data/exec-venv"
PY_BIN="${VENV_DIR}/bin/python"

echo "==> exec venv: ${VENV_DIR}"
uv venv "${VENV_DIR}" --python 3.12

echo "==> installing strands runtime deps"
# The [openai] extra carries `openai` AND `aws-bedrock-token-generator`, which
# the default Bedrock Mantle model needs: generated code calls
# OpenAIResponsesModel(bedrock_mantle_config=...), and the SDK mints a bearer
# token from the ambient AWS credentials (so local debug uses your AWS profile —
# no BEDROCK_API_KEY). The floor tracks the verified SDK (1.47.0); the real
# minimum is 1.46, where the `openai.gpt-5.*` → /openai/v1 base-path split the
# default model needs landed (1.45 served every Mantle id from /v1).
uv pip install --python "${PY_BIN}" \
  'strands-agents[openai]>=1.47,<2' \
  'strands-agents-tools[mem0_memory]' \
  'mcp' \
  'bedrock-agentcore'

echo "==> verifying imports"
"${PY_BIN}" -c "import strands, strands_tools, mcp, aws_bedrock_token_generator; from strands_tools import mem0_memory; from strands.models.openai_responses import OpenAIResponsesModel; from importlib.metadata import version; print('strands-agents', version('strands-agents'))"

echo "==> done. studio_exec_python = ${PY_BIN}"
