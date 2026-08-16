# Claude Agent SDK runtime image — linux/arm64 (AgentCore Runtime requirement).
# Slim on purpose: python base + node (for the claude CLI) + the SDK, no dev deps.
FROM --platform=linux/arm64 python:3.12-slim-bookworm

# node 22 + claude CLI (the python claude-agent-sdk drives this binary)
# Git is intentionally absent: generated agents do not expose Bash/Edit by
# default, and keeping git pulls the full Perl runtime into the production image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/workspace

# bypassPermissions refuses to run as root — use a dedicated user.
RUN useradd -m -u 1001 agent && chown -R agent:agent /app
USER agent

# Claude CLI talks to Bedrock with the runtime execution role — no API key.
ENV CLAUDE_CODE_USE_BEDROCK=1 \
    HOME=/app \
    PYTHONUNBUFFERED=1

EXPOSE 8080
CMD ["opentelemetry-instrument", "python", "main.py"]
