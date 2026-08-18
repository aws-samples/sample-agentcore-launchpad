# SkillOpt AgentCore exec worker (linux/arm64) — Launchpad Skill Lab variant.
#
# LAUNCHPAD PATCH (see ../../LAUNCHPAD_DEVIATIONS.md): upstream copied the build
# host's claude/codex standalone binaries into the image. This variant installs a
# PINNED claude CLI version inside the image (reproducible, host-independent) and
# drops codex entirely (Skill Lab v1 is claude_code_exec only). Built by the
# Launchpad CodeBuild pipeline from the context assembled in
# backend/app/skill_lab/worker_build.py — not by upstream's build_and_push.sh.
FROM public.ecr.aws/docker/library/python:3.12-slim-bookworm

ARG CLAUDE_CLI_VERSION=2.1.234

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

RUN useradd -m -u 1000 agent
COPY skillopt/ /opt/skillopt/skillopt/

ENV PYTHONPATH=/opt/skillopt \
    PYTHONUNBUFFERED=1 \
    HOME=/home/agent \
    AWS_REGION=us-west-2 \
    CLAUDE_CODE_USE_BEDROCK=1 \
    CLAUDE_CODE_EXEC_USE_SDK=cli \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

USER agent
WORKDIR /home/agent
EXPOSE 8080
CMD ["python", "-m", "skillopt.model.agentcore_worker"]
