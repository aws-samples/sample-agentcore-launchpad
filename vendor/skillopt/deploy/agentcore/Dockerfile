# SkillOpt AgentCore exec worker (linux/arm64) — Launchpad Skill Lab variant.
#
# LAUNCHPAD PATCH (see ../../LAUNCHPAD_DEVIATIONS.md): upstream copied the build
# host's claude/codex standalone binaries into the image. This variant installs
# PINNED claude and codex CLI versions inside the image (reproducible,
# host-independent). Built by the Launchpad CodeBuild pipeline from the context
# assembled in backend/app/skill_lab/worker_build.py — not by upstream's
# build_and_push.sh. That assembler also stages codex-home/ (config.toml from
# this directory + the bedrock model catalog read from the backend host).
FROM public.ecr.aws/docker/library/python:3.12-slim-bookworm

ARG CLAUDE_CLI_VERSION=2.1.234
ARG CODEX_CLI_VERSION=0.147.0

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git ripgrep procps unzip jq \
    && rm -rf /var/lib/apt/lists/*

# openai/pyyaml: imported by skillopt.model / skillopt.config at import time
RUN pip install --no-cache-dir boto3 bedrock-agentcore openai pyyaml

# Pinned standalone claude CLI (official installer, version-locked).
RUN curl -fsSL https://claude.ai/install.sh | bash -s -- "${CLAUDE_CLI_VERSION}" \
    && install -m 0755 "$(readlink -f /root/.local/bin/claude)" /usr/local/bin/claude \
    && rm -rf /root/.local/share/claude /root/.local/bin/claude \
    && /usr/local/bin/claude --version

# Pinned codex CLI (official standalone release binary, version-locked).
RUN curl -fsSL -o /tmp/codex.tar.gz \
        "https://github.com/openai/codex/releases/download/rust-v${CODEX_CLI_VERSION}/codex-aarch64-unknown-linux-musl.tar.gz" \
    && tar -xzf /tmp/codex.tar.gz -C /tmp codex-aarch64-unknown-linux-musl \
    && install -m 0755 /tmp/codex-aarch64-unknown-linux-musl /usr/local/bin/codex \
    && rm -f /tmp/codex.tar.gz /tmp/codex-aarch64-unknown-linux-musl \
    && /usr/local/bin/codex --version

RUN useradd -m -u 1000 agent
COPY --chown=agent:agent codex-home/ /home/agent/.codex/
COPY skillopt/ /opt/skillopt/skillopt/

ENV PYTHONPATH=/opt/skillopt \
    PYTHONUNBUFFERED=1 \
    HOME=/home/agent \
    AWS_REGION=us-west-2 \
    CLAUDE_CODE_USE_BEDROCK=1 \
    CLAUDE_CODE_EXEC_USE_SDK=cli \
    CODEX_EXEC_USE_SDK=cli \
    # codex >= 0.147 dropped `exec --full-auto`; false makes the harness pass
    # `--sandbox workspace-write` (the same policy --full-auto used to imply).
    CODEX_EXEC_FULL_AUTO=false \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

USER agent
WORKDIR /home/agent
EXPOSE 8080
CMD ["python", "-m", "skillopt.model.agentcore_worker"]
