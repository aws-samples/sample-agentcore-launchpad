# Launchpad coding runtime (linux/arm64): the AgentCore HTTP contract (:8080
# GET /ping, POST /invocations) in front of `codex app-server`, one microVM per
# member project. Built by the shared launchpad-agent-builder CodeBuild project
# from the context backend/app/coding/image_build.py assembles (this directory
# + the shared buildspec); the image tag is a hash of that context.
#
# Zero npm dependencies for the server itself (src/*.mjs uses Node's stdlib only).
FROM public.ecr.aws/docker/library/node:22-bookworm-slim

# git: the project repo + per-turn baselines; python3: export zips, and what most
# agents are written in; tar/procps/curl: snapshots, diagnostics, the agent's
# own smoke tests (curl localhost:8080).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git ca-certificates curl python3 python3-venv procps tar ripgrep \
 && rm -rf /var/lib/apt/lists/*

# uv / uvx: Python envs for the projects, and the Toolkit's aws-mcp proxy.
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /uvx /usr/local/bin/

# Pinned codex CLI (the app-server protocol the server speaks is 0.163's;
# tests/test_coding_runtime.py pins this ARG, so a bump is a deliberate change).
ARG CODEX_VERSION=0.163.0
RUN npm install -g "@openai/codex@${CODEX_VERSION}" \
 && npm cache clean --force \
 && codex --version </dev/null

# CODEX_HOME template: provider config + the Agent Toolkit for AWS plugins
# (aws-core, aws-agents). The server copies config.toml into each project's
# CODEX_HOME on every boot and links plugins/ + .tmp/ back here. stdin is closed
# on every codex call — an open, idle stdin blocks it.
ENV CODEX_TEMPLATE=/opt/codex-home
COPY codex-config.toml /opt/codex-home/config.toml
RUN CODEX_HOME=/opt/codex-home codex plugin marketplace add aws/agent-toolkit-for-aws </dev/null \
 && CODEX_HOME=/opt/codex-home codex plugin add aws-core@agent-toolkit-for-aws </dev/null \
 && CODEX_HOME=/opt/codex-home codex plugin add aws-agents@agent-toolkit-for-aws </dev/null \
 && UV_CACHE_DIR=/var/lib/coding-deps/uv-cache uvx mcp-proxy-for-aws-cli==1.7.0 --help </dev/null >/dev/null

RUN git config --system user.name "Launchpad Coding Agent" \
 && git config --system user.email "coding-agent@launchpad.local" \
 && git config --system init.defaultBranch main \
 && git config --system safe.directory '*'

# Project templates (AGENTS.md with the Launchpad deploy contract, .gitignore).
COPY templates/ /opt/coding-templates/
COPY src/ /app/src/

# Local disk (not session storage): codex SQLite, dependency dirs, tool caches.
# The uv cache above was warmed at the path the server hands codex at runtime
# (UV_CACHE_DIR=/var/lib/coding-deps/uv-cache), so the Toolkit's aws-mcp proxy
# starts without a download on a cold microVM.
RUN mkdir -p /var/lib/coding-state /var/lib/coding-deps
ENV CODING_STATE_DIR=/var/lib/coding-state \
    CODING_DEPS_DIR=/var/lib/coding-deps \
    CODING_TEMPLATES=/opt/coding-templates \
    CODING_WORKSPACE=/mnt/workspace \
    NODE_ENV=production

EXPOSE 8080
CMD ["node", "/app/src/server.mjs"]
