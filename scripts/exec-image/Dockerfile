# Studio local-debug execution sandbox image.
#
# Container twin of scripts/setup_exec_env.sh: the same Strands runtime dep set,
# but baked into an image so caller-supplied generated code runs in a one-shot
# container instead of a host subprocess (LAUNCHPAD_STUDIO_EXEC_BACKEND=docker).
# Build with scripts/setup_exec_docker.sh; keep the pins below in lockstep with
# setup_exec_env.sh when upgrading.
#
# No USER is baked in: the backend passes `--user <backend-uid>:<gid>` at run
# time so the bind-mounted workdir is readable/writable on both sides. HOME is
# likewise set per run (-e HOME=/tmp, backed by a tmpfs).
FROM public.ecr.aws/docker/library/python:3.12-slim

# The [openai] extra carries `openai` AND `aws-bedrock-token-generator`, which
# the default Bedrock Mantle model needs (see setup_exec_env.sh for the floor
# rationale).
RUN pip install --no-cache-dir \
      'strands-agents[openai]>=1.47,<2' \
      'strands-agents-tools' \
      'mcp' \
      'bedrock-agentcore' \
    && python -c "import strands, strands_tools, mcp, aws_bedrock_token_generator; \
from strands.models.openai_responses import OpenAIResponsesModel; \
from importlib.metadata import version; \
print('strands-agents', version('strands-agents'))"
