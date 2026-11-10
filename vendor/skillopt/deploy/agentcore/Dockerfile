# SkillOpt AgentCore exec worker (linux/arm64) — Launchpad Skill Lab variant (CN).
#
# LAUNCHPAD PATCH (D16 / N13, see ../../LAUNCHPAD_DEVIATIONS.md and
# docs/cn/reports/n13-opencode-spike.md §6): the exec agent is the OpenCode CLI —
# the npm platform package `opencode-linux-arm64` IS a single binary, fetched as a
# tarball from the China npm mirror and sha256-checked; no Node runtime, no npm, no
# GitHub, no Docker Hub, no claude.ai installer. The former Claude Code / Codex
# installs are gone. Built by the Launchpad CodeBuild pipeline for linux/arm64 from
# the context assembled in backend/app/skill_lab/worker_build.py, which renders every
# mirror below as a build arg from Settings (base_image_mirror_*, apt_mirror,
# codebuild_pip_index_url / codebuild_pip_fallback_index_url, npm_registry) and the pin
# from skill_lab_worker_opencode_version.
# The defaults keep a standalone `docker build --platform linux/arm64 .` working.
ARG BASE_IMAGE=docker.m.daocloud.io/library/python:3.13-slim
FROM ${BASE_IMAGE}

ARG APT_MIRROR=mirrors.aliyun.com
# PyPI (N11-r3-bug7): this image is only ever built inside CodeBuild, whose NAT egress
# tuna answers with an immediate 4xx (the hosts get 200 from the same URL), so the
# defaults are the BUILD mirrors — Settings.codebuild_pip_index_url (NJU, the one
# independent uv-complete mirror probed) and codebuild_pip_fallback_index_url (Tencent) —
# never the host mirror. Every index must be uv-complete (N11-obs25: mirrors.aliyun.com
# advertises PEP 658 .metadata it does not serve, so `uv pip` hard-fails against it).
ARG PIP_INDEX_URL=https://mirror.nju.edu.cn/pypi/web/simple
ARG PIP_FALLBACK_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple
ARG PIP_EXTRA_INDEX_URL=
ARG NPM_CONFIG_REGISTRY=https://registry.npmmirror.com
# uv release used to install the Python deps (the repo's pinned uv).
ARG UV_VERSION=0.9.10
# Pinned OpenCode release (spike §9): exact version + the tarball's sha256, both
# checked at build time. Settings.skill_lab_worker_opencode_version mirrors the pin.
ARG OPENCODE_VERSION=1.18.35
ARG OPENCODE_SHA256=57fd6ecfea7927c79d3943766db39ef9267f38bdb32babe6b3d8dea1f21484ba

# Debian apt via the configured mirror (bookworm images ship debian.sources).
RUN set -eux; \
    for f in /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list; do \
        [ -f "$f" ] && sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" "$f" || true; \
    done; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl git ripgrep procps unzip jq; \
    apt-get clean; find /var/lib/apt/lists -type f -delete

# OpenCode: the platform package tarball from the npm mirror, verified, installed as
# /usr/local/bin/opencode. `--version` proves the arm64 binary runs in this layer.
RUN set -eux; \
    curl -fsSL -o /tmp/opencode.tgz \
        "${NPM_CONFIG_REGISTRY}/opencode-linux-arm64/-/opencode-linux-arm64-${OPENCODE_VERSION}.tgz"; \
    echo "${OPENCODE_SHA256}  /tmp/opencode.tgz" | sha256sum -c -; \
    tar -xzf /tmp/opencode.tgz -C /tmp package/bin/opencode; \
    install -m 0755 /tmp/package/bin/opencode /usr/local/bin/opencode; \
    unlink /tmp/opencode.tgz; \
    /usr/local/bin/opencode --version

# openai/pyyaml: imported by skillopt.model / skillopt.config at import time;
# bedrock-agentcore: the Runtime app + Identity client; inspector deps for binary assets.
# Installed through uv — the resolver every agent image/zip uses — so a mirror fault
# surfaces as uv's real error (TLS, 404 metadata) instead of pip's "No matching
# distribution found for boto3" (N13-bug1 / N11-obs25). uv itself is one wheel from
# the primary index, fetched again from the fallback when the primary refuses the build
# egress (N11-r3-bug7). The uv step lists the primary as the preferred `--index` and the
# fallback as the `--default-index` (lowest priority) — uv's `--extra-index-url` would
# have OUT-RANKED the primary — and retries on the fallback alone if that resolve fails;
# the optional secondary index is added only when the build arg is set.
RUN set -eux; \
    pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" "uv==${UV_VERSION}" \
      || pip install --no-cache-dir --index-url "${PIP_FALLBACK_INDEX_URL}" "uv==${UV_VERSION}"; \
    PKGS="boto3 bedrock-agentcore openai pyyaml openpyxl==3.1.5 Pillow==11.3.0 pypdf==6.0.0"; \
    uv pip install --system --no-cache \
        --index "${PIP_INDEX_URL}" --default-index "${PIP_FALLBACK_INDEX_URL}" \
        ${PIP_EXTRA_INDEX_URL:+--extra-index-url "${PIP_EXTRA_INDEX_URL}"} \
        $PKGS \
      || uv pip install --system --no-cache --default-index "${PIP_FALLBACK_INDEX_URL}" $PKGS

# The CLI's ripgrep is seeded from apt (spike finding 2: it would otherwise download
# ripgrep from GitHub and glob/grep/skill tools fail). The per-job home the worker
# creates gets the same symlink from opencode_backend.ensure_ripgrep.
RUN useradd -m -u 1000 agent \
    && mkdir -p /home/agent/.cache/opencode/bin \
    && ln -s /usr/bin/rg /home/agent/.cache/opencode/bin/rg \
    && chown -R agent:agent /home/agent/.cache
COPY skillopt/ /opt/skillopt/skillopt/

# No model region, credential or key here: the runtime env carries AWS_REGION,
# SKILLOPT_OPENCODE_BASE_URL, SKILLOPT_OPENCODE_NO_PROXY and LAUNCHPAD_MODEL_CREDENTIAL;
# the worker resolves the Global Bedrock API key through Identity per session
# (agentcore_worker.py) and builds the per-job CLI env (dead proxy + NO_PROXY, XDG under
# the job home) in opencode_backend.subprocess_env. The OPENCODE_DISABLE_* set is pinned
# here too so a stray invocation never reaches GitHub or the share service.
ENV PYTHONPATH=/opt/skillopt \
    PYTHONUNBUFFERED=1 \
    HOME=/home/agent \
    OPENCODE_DISABLE_AUTOUPDATE=1 \
    OPENCODE_DISABLE_MODELS_FETCH=1 \
    OPENCODE_DISABLE_SHARE=1 \
    OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1 \
    OPENCODE_DISABLE_LSP_DOWNLOAD=1 \
    OPENCODE_DISABLE_DEFAULT_PLUGINS=1

USER agent
WORKDIR /home/agent
EXPOSE 8080
CMD ["python", "-m", "skillopt.model.agentcore_worker"]
