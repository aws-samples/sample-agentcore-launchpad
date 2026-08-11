#!/usr/bin/env bash
# Provision the studio local-debug DOCKER sandbox (LAUNCHPAD_STUDIO_EXEC_BACKEND=docker).
#
# Container twin of setup_exec_env.sh: builds the exec image from
# scripts/exec-image/Dockerfile (idempotent — re-run to upgrade deps).
#
# --harden-net additionally provisions the network isolation that makes
# studio_exec_forward_aws_credentials=false honest on EC2: a dedicated bridge
# network plus a DOCKER-USER iptables rule denying that subnet egress to the
# instance metadata service. Without it, a container on the default bridge can
# still mint IMDSv2 tokens whenever the instance's hop limit is >= 2 (both
# launchpad boxes ship with 2) and the credential-less posture is an illusion.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${LAUNCHPAD_STUDIO_EXEC_DOCKER_IMAGE:-launchpad-studio-exec:latest}"
NETWORK="${LAUNCHPAD_STUDIO_EXEC_DOCKER_NETWORK:-launchpad-exec}"
SUBNET="${LAUNCHPAD_STUDIO_EXEC_DOCKER_SUBNET:-172.30.100.0/24}"
HARDEN_NET=0
for arg in "$@"; do
  case "${arg}" in
    --harden-net) HARDEN_NET=1 ;;
    -h|--help)
      echo "usage: $0 [--harden-net]"
      echo "  --harden-net  also create the ${NETWORK} bridge network and install"
      echo "                a DOCKER-USER rule denying it egress to the instance"
      echo "                metadata service (Linux only, needs sudo/root)"
      exit 0
      ;;
    *) echo "unknown argument: ${arg}" >&2; exit 2 ;;
  esac
done

echo "==> building exec image: ${IMAGE}"
docker build -t "${IMAGE}" "${SCRIPT_DIR}/exec-image"

echo "==> verifying imports inside the image"
docker run --rm "${IMAGE}" python -c \
  "import strands, strands_tools, mcp, aws_bedrock_token_generator"

if [[ "${HARDEN_NET}" -eq 1 ]]; then
  if [[ "$(uname -s)" != "Linux" ]]; then
    echo "!!  --harden-net is Linux-only (it needs a DOCKER-USER iptables rule)." >&2
    exit 1
  fi
  if [[ "${EUID}" -ne 0 ]]; then
    echo "!!  --harden-net needs root (installs an iptables rule). Re-run with sudo." >&2
    exit 1
  fi

  echo "==> exec network: ${NETWORK} (${SUBNET})"
  if docker network inspect "${NETWORK}" >/dev/null 2>&1; then
    echo "    already exists"
  else
    docker network create --subnet "${SUBNET}" "${NETWORK}"
  fi

  echo "==> denying ${SUBNET} egress to the metadata service"
  # DOCKER-USER is evaluated before docker's own FORWARD rules and survives
  # daemon restarts within a boot; like setup_exec_env.sh --hardened, the rule
  # itself is NOT persistent across reboots.
  iptables -C DOCKER-USER -s "${SUBNET}" -d 169.254.169.254 -j REJECT 2>/dev/null || \
    iptables -I DOCKER-USER 1 -s "${SUBNET}" -d 169.254.169.254 -j REJECT
  echo "    DOCKER-USER rule installed"
  echo "!!  iptables rules are not persistent across reboot — persist them with"
  echo "!!  iptables-save / netfilter-persistent for a long-lived host."

  cat <<HARDENED_NOTE

==> hardened network provisioned. Add to config/launchpad.yaml:

      studio_exec_backend: docker
      studio_exec_docker_network: ${NETWORK}
      studio_exec_forward_aws_credentials: false

    Trade-off, deliberately not applied for you:

      forward_aws_credentials: false makes the container credential-less —
      Studio local debug and AI Fix then need an explicit bedrock_api_key /
      openai_api_key with each request. Leaving it true keeps today's
      convenience (the container uses the instance role via IMDS), at the cost
      of caller code being able to do the same.

HARDENED_NOTE
else
  cat <<NOTE

==> done. Enable with:

      LAUNCHPAD_STUDIO_EXEC_BACKEND=docker    (env)
   or studio_exec_backend: docker             (config/launchpad.yaml)

    Default posture: the container runs with --cap-drop ALL,
    no-new-privileges, read-only rootfs, memory/cpu/pids ceilings, and an
    environment allowlist — but CAN still reach the instance metadata service
    (IMDS hop limit permitting), which is what keeps the default Bedrock
    Mantle path working without API keys. For a credential-less sandbox, run
    this script again with sudo and --harden-net.

NOTE
fi
