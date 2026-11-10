"""hello-container — the hello-http agent packaged as a container (container_source kind).

Satisfies the Launchpad invoke contract with the bedrock-agentcore SDK:
`BedrockAgentCoreApp` serves POST /invocations + GET /ping on :8080, and the
`@app.entrypoint` receives the payload Launchpad sends —
``{"prompt": "...", "actor_id": "..."}``.

Model access follows the CN build's model-credential contract
(docs/cn/interfaces/model-credential.md): there is no Bedrock endpoint in the
runtime's own region, so the agent calls Bedrock Converse in
``LAUNCHPAD_MODEL_REGION`` (us-west-2) on a Global cross-region profile
(``MODEL_ID``, e.g. ``global.zai.glm-5.3``), authenticating with the
workspace's Global Bedrock API key. The key is never shipped with the code: the
runtime exchanges its AgentCore Identity workload access token for it with
``GetResourceApiKey`` on the API-key credential provider named by
``LAUNCHPAD_MODEL_CREDENTIAL`` — exactly what the generated Strands templates
do (``backend/app/templates/strands_agent/main.py.tmpl``, block
``<launchpad-model-helpers>``) — and exports it as ``AWS_BEARER_TOKEN_BEDROCK``
*before* the ``bedrock-runtime`` client is created, which is when botocore reads
it. Launchpad injects every one of those env vars and grants the execution role
the two Identity calls; nothing here needs ``bedrock:InvokeModel``.

Falls back to a plain echo naming the reason when the key or the model is
unreachable, so the sample deploys and chats regardless.
"""

import os

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import BedrockAgentCoreContext
from botocore.config import Config

# <launchpad-model-constants>  — same names and sources as the generated templates
MODEL_ID = os.environ.get("MODEL_ID", "global.zai.glm-5.3")
MODEL_REGION = os.environ.get("LAUNCHPAD_MODEL_REGION", "us-west-2")  # region-boundary: global-bedrock
MODEL_CREDENTIAL = os.environ.get("LAUNCHPAD_MODEL_CREDENTIAL", "launchpad-global-bedrock")
# GLM 5.3 reasons before it answers; under ~200 tokens the text block comes back
# empty, so the platform never renders a ceiling below 256. N17-obs114: at 1024 a
# puzzle-shaped prompt still exhausted the budget inside the reasoning block and the
# answer came back as "" — 4096 leaves room for the text, and `_answer()` names the
# stop reason instead of returning nothing when it still happens.
MODEL_MAX_TOKENS = 4096
# botocore authenticates bedrock-runtime with a bearer token when this is set at
# client creation (signing name "bedrock"); the api key is exported here, never
# written anywhere.
BEDROCK_BEARER_ENV = "AWS_BEARER_TOKEN_BEDROCK"
# </launchpad-model-constants>

app = BedrockAgentCoreApp()

# <launchpad-model-helpers>  — mirrors strands_agent/main.py.tmpl; keep in step
_BEDROCK_API_KEY: dict[str, str] = {}


def _identity_client():
    from bedrock_agentcore.services.identity import IdentityClient

    return IdentityClient(os.environ.get("AWS_REGION", "cn-northwest-1"))


def _workload_access_token() -> str | None:
    """The Runtime-injected token (the invoke carried runtimeUserId — the platform
    always sends it); otherwise self-mint for this runtime's own workload identity
    (LAUNCHPAD_WORKLOAD_NAME, set from the second deploy on)."""
    try:
        injected = BedrockAgentCoreContext.get_workload_access_token()
    except Exception:
        injected = None
    if injected:
        return injected
    workload = os.environ.get("LAUNCHPAD_WORKLOAD_NAME", "")
    if not workload:
        return None
    return _identity_client().get_workload_access_token(workload)["workloadAccessToken"]


def _resolve_bedrock_api_key() -> str:
    """The Global Bedrock API key from MODEL_CREDENTIAL, fetched once per process.

    Raises instead of returning "" — a model call without the key would fail with
    an opaque signing error against the Global endpoint.
    """
    cached = _BEDROCK_API_KEY.get("key")
    if cached:
        return cached
    token = _workload_access_token()
    if not token:
        raise RuntimeError(
            "no workload access token: the invoke must carry runtimeUserId (the platform "
            "always sends it) or LAUNCHPAD_WORKLOAD_NAME must name this runtime's identity"
        )
    response = _identity_client().dp_client.get_resource_api_key(
        workloadIdentityToken=token, resourceCredentialProviderName=MODEL_CREDENTIAL
    )
    key = str(response.get("apiKey") or "")
    if not key:
        raise RuntimeError(f"GetResourceApiKey({MODEL_CREDENTIAL}) returned no apiKey")
    _BEDROCK_API_KEY["key"] = key
    return key


def _bedrock_runtime_client():
    """A ``bedrock-runtime`` client in MODEL_REGION that signs with the bearer.

    The env var must be exported before the client exists (botocore reads it at
    construction); ``signature_version="bearer"`` pins the auth scheme the same
    way the hub's own bearer-only client does, so the runtime's IAM credentials
    (which hold no ``bedrock:InvokeModel`` in aws-cn) are never tried.
    """
    os.environ[BEDROCK_BEARER_ENV] = _resolve_bedrock_api_key()
    return boto3.client(
        "bedrock-runtime", region_name=MODEL_REGION, config=Config(signature_version="bearer")
    )


# </launchpad-model-helpers>


def _answer(prompt: str) -> str:
    response = _bedrock_runtime_client().converse(
        modelId=MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": MODEL_MAX_TOKENS},
    )
    # GLM/Kimi may return reasoningContent blocks first; only text blocks are the answer.
    text = "".join(block.get("text", "") for block in response["output"]["message"]["content"])
    if text.strip():
        return text
    # N17-obs114: no text block at all — the model stopped (usually `max_tokens`, the
    # whole budget spent on reasoning) before writing the answer. Say so: an empty
    # `{"result": ""}` is a silent turn in the console.
    stop_reason = str(response.get("stopReason") or "unknown")
    return (
        f"[no answer — the model stopped ({stop_reason}) before producing text; "
        f"MODEL_MAX_TOKENS={MODEL_MAX_TOKENS}: raise it or shorten the prompt]"
    )


@app.entrypoint
def invoke(payload):
    prompt = str(payload.get("prompt", ""))
    try:
        return {"result": _answer(prompt)}
    except Exception as exc:  # no key / no model access / throttling — echo instead of 500
        # A 401/403 from Bedrock means the cached key is stale (rotated): drop it so
        # the next turn re-exchanges the workload token instead of echoing forever.
        _BEDROCK_API_KEY.clear()
        reason = f"{type(exc).__name__}: {exc}".strip(": ")
        return {
            "result": (
                f"[echo — model unavailable ({MODEL_ID} via {MODEL_CREDENTIAL} in "
                f"{MODEL_REGION}): {reason}] {prompt}"
            )
        }


if __name__ == "__main__":
    app.run()
