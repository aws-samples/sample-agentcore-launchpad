"""hello-http — minimal BYOC agent for AgentCore Launchpad (code_zip kind).

Satisfies the Launchpad invoke contract with the bedrock-agentcore SDK:
`BedrockAgentCoreApp` serves POST /invocations + GET /ping on :8080, and the
`@app.entrypoint` receives the payload Launchpad sends —
``{"prompt": "...", "actor_id": "..."}``.

Answers with Bedrock Converse (model id from env ``MODEL_ID``); falls back to a
plain echo when the account has no access to the model, so the sample deploys
and chats even before any model access is granted.
"""

import os

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp

MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-3-5-haiku-20241022-v1:0")

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload):
    prompt = str(payload.get("prompt", ""))
    try:
        bedrock = boto3.client("bedrock-runtime")
        response = bedrock.converse(
            modelId=MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
        )
        text = "".join(
            block.get("text", "")
            for block in response["output"]["message"]["content"]
        )
        return {"result": text}
    except Exception as exc:  # no model access / throttling — echo instead of 500
        return {"result": f"[echo — model unavailable: {type(exc).__name__}] {prompt}"}


if __name__ == "__main__":
    app.run()
