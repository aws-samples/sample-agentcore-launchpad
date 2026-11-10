# hello-container — the same agent as hello-http, packaged as a container.
# AgentCore Runtime containers run linux/arm64; Launchpad's CodeBuild project
# builds with --platform linux/arm64, so this base needs no platform pin here.
# BASE_IMAGE/PIP_INDEX_URL default to the public upstream image/index so this
# Dockerfile still builds standalone (`docker build .`) with no CN mirror
# configured; the CodeBuild buildspec overrides both when
# Settings.base_image_mirror_enabled is on (see scripts/mirror_base_images.sh).
ARG BASE_IMAGE=python:3.13-slim
FROM ${BASE_IMAGE}

ARG PIP_INDEX_URL=https://pypi.org/simple

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r requirements.txt
COPY main.py .

# Runtime contract: POST /invocations + GET /ping on :8080
EXPOSE 8080
CMD ["python", "main.py"]
