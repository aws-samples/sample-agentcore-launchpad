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
#
# --hardened additionally provisions the T2 isolation for that interpreter: a
# dedicated unprivileged user for the subprocess to drop to, plus a firewall rule
# denying that user egress to the instance metadata service. Read the trade-off
# printed at the end before enabling it — it removes the ambient-AWS-credential
# convenience that the default Bedrock Mantle path relies on.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${REPO_ROOT}/data/exec-venv"
PY_BIN="${VENV_DIR}/bin/python"
EXEC_USER="${LAUNCHPAD_EXEC_USER:-launchpad-exec}"
HARDENED=0
for arg in "$@"; do
  case "${arg}" in
    --hardened) HARDENED=1 ;;
    -h|--help)
      echo "usage: $0 [--hardened]"
      echo "  --hardened  also create the ${EXEC_USER} account and block its"
      echo "              egress to the instance metadata service (Linux only)"
      exit 0
      ;;
    *) echo "unknown argument: ${arg}" >&2; exit 2 ;;
  esac
done

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

if [[ "${HARDENED}" -eq 1 ]]; then
  if [[ "$(uname -s)" != "Linux" ]]; then
    echo "!!  --hardened is Linux-only (it needs a uid-keyed firewall rule)." >&2
    echo "!!  Skipping. On this host local execution keeps the backend's uid and" >&2
    echo "!!  can reach the metadata service; the production default (endpoints" >&2
    echo "!!  disabled) is the mitigation there." >&2
    exit 1
  fi
  if [[ "${EUID}" -ne 0 ]]; then
    echo "!!  --hardened needs root (creates a user, installs a firewall rule)." >&2
    echo "!!  Re-run with sudo." >&2
    exit 1
  fi

  echo "==> dedicated execution user: ${EXEC_USER}"
  if id -u "${EXEC_USER}" >/dev/null 2>&1; then
    echo "    already exists"
  else
    useradd --system --no-create-home --shell /usr/sbin/nologin "${EXEC_USER}"
  fi
  EXEC_UID="$(id -u "${EXEC_USER}")"

  # The generated code runs as EXEC_USER but needs to read the interpreter.
  chmod -R a+rX "${VENV_DIR}"

  echo "==> denying ${EXEC_USER} (uid ${EXEC_UID}) egress to the metadata service"
  # IMDS is the reason an environment allowlist is not enough on EC2: credentials
  # arrive over the network, not through the environment, so code that asks for
  # them directly bypasses AWS_EC2_METADATA_DISABLED. Only a network rule keyed on
  # the uid actually closes it.
  if command -v nft >/dev/null 2>&1; then
    nft list table inet launchpad_exec >/dev/null 2>&1 || \
      nft add table inet launchpad_exec
    nft flush table inet launchpad_exec
    nft add chain inet launchpad_exec output \
      '{ type filter hook output priority 0; policy accept; }'
    nft add rule inet launchpad_exec output \
      meta skuid "${EXEC_UID}" ip daddr 169.254.169.254 drop
    nft add rule inet launchpad_exec output \
      meta skuid "${EXEC_UID}" ip6 daddr fd00:ec2::254 drop
    echo "    nft table inet launchpad_exec installed"
    echo "!!  nft rules are not persistent across reboot — add them to your"
    echo "!!  nftables.conf or a systemd unit for a long-lived host."
  elif command -v iptables >/dev/null 2>&1; then
    iptables -C OUTPUT -m owner --uid-owner "${EXEC_UID}" \
      -d 169.254.169.254 -j REJECT 2>/dev/null || \
      iptables -A OUTPUT -m owner --uid-owner "${EXEC_UID}" \
        -d 169.254.169.254 -j REJECT
    if command -v ip6tables >/dev/null 2>&1; then
      ip6tables -C OUTPUT -m owner --uid-owner "${EXEC_UID}" \
        -d fd00:ec2::254 -j REJECT 2>/dev/null || \
        ip6tables -A OUTPUT -m owner --uid-owner "${EXEC_UID}" \
          -d fd00:ec2::254 -j REJECT
    fi
    echo "    iptables OUTPUT rule installed"
    echo "!!  iptables rules are not persistent across reboot — persist them with"
    echo "!!  iptables-save / netfilter-persistent for a long-lived host."
  else
    echo "!!  neither nft nor iptables found — the user was created but IMDS is" >&2
    echo "!!  still reachable from it. Install one, or do not rely on this tier." >&2
    exit 1
  fi

  cat <<HARDENED_NOTE

==> hardening provisioned. Add to config/launchpad.yaml:

      studio_exec_user: ${EXEC_USER}
      studio_exec_forward_aws_credentials: false

    Trade-off, deliberately not applied for you:

      Local debug currently works off your AWS profile because the default
      Bedrock Mantle path mints its bearer token from the ambient credentials.
      Setting forward_aws_credentials: false is what makes the subprocess
      credential-less — and it means Studio local debug and AI Fix will need an
      explicit bedrock_api_key / openai_api_key with each request.

      Leaving it true keeps today's convenience: the uid drop and resource limits
      still apply, but the subprocess can still use your AWS credentials.

HARDENED_NOTE
fi

echo "==> done. studio_exec_python = ${PY_BIN}"
