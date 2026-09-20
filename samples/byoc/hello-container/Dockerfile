# hello-container — the same agent as hello-http, packaged as a container.
# AgentCore Runtime containers run linux/arm64; Launchpad's CodeBuild project
# builds with --platform linux/arm64, so this base needs no platform pin here.
FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

# Runtime contract: POST /invocations + GET /ping on :8080
EXPOSE 8080
CMD ["python", "main.py"]
