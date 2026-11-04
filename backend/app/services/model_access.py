"""Recognize an agent turn that failed because its model call was refused.

When an agent's execution role does not allow the model it calls (or the account
has no access to that model), Bedrock answers the model call inside the agent with
``AccessDeniedException``. The platform does not see a ``ClientError`` for it: a
code runtime reports it as ``RuntimeError("runtime returned error: …")`` and a
Harness as a streamed ``runtimeClientError`` (measured live 2026-11-04 on a
zip_runtime and a harness agent whose role lost its ``BedrockModels`` grant). Left
alone, the harness form is classed as a transient upstream failure: the console
retries it by itself, tells the person to send it again, and evaluations replay
it, although no retry can succeed. ``model_access_denied`` names it instead.

Kept free of other app imports: ``services/invoke``, ``services/chat`` and
``evaluation/service`` all call it.
"""

import re
from typing import Any

from app.core.errors import AppError

MODEL_ACCESS_DENIED = "agent.model_access_denied"

# "… is not authorized to perform: bedrock:InvokeModelWithResponseStream on
# resource: arn:aws:bedrock:…:inference-profile/global.zai.glm-5.3 because …"
_ACTION = re.compile(
    r"not authorized to perform: (bedrock(?:-mantle)?:[A-Za-z*]+)"
    r"(?: on resource: (arn:[^\s,;\"']+))?"
)
# Bedrock's answer when model access is not enabled for the account / model.
_NO_MODEL_ACCESS = "don't have access to the model"
# Only the actions of a model call: an AccessDenied on bedrock:Retrieve (a
# knowledge base) or any other Bedrock API is a different problem.
_MODEL_ACTIONS = ("bedrock:InvokeModel", "bedrock:Converse", "bedrock-mantle:")


def model_access_denied(exc: BaseException, *, public: bool = False) -> AppError | None:
    """The named error for a turn whose model call was refused, else ``None``.

    ``public`` (``/v1``) leaves the raw AWS text, which names the role and the
    account, out of the detail."""
    if isinstance(exc, AppError):
        return exc if exc.code == MODEL_ACCESS_DENIED else None
    text = str(exc)
    if "AccessDenied" not in text and _NO_MODEL_ACCESS not in text:
        return None
    action, model = "", ""
    found = _ACTION.search(text)
    if found:
        action = found.group(1)
        if not action.startswith(_MODEL_ACTIONS):
            return None
        model = (found.group(2) or "").rsplit("/", 1)[-1]
    elif _NO_MODEL_ACCESS not in text:
        return None
    detail: dict[str, Any] = {"model": model, "action": action, "retryable": False}
    if not public:
        detail["upstream_message"] = text[:800]
    return AppError(
        MODEL_ACCESS_DENIED,
        f"the agent may not call its model{f' {model}' if model else ''}: the "
        "execution role does not allow it or the account has no access to it — a "
        "retry cannot succeed; grant the model on the agent's role (redeploy the "
        "agent), enable model access, or pick another model",
        detail,
        status_code=502,
    )
