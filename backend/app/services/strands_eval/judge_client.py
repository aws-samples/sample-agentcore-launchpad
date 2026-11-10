"""How long a stalled judge call can hold its thread: the judge CLIENT's bounds.

``deadlines.DeadlineEvaluator`` stops WAITING for a judge call after ``eval_judge_deadline_s``
(300 s by default), but the abandoned call keeps its thread and connection until the client
itself gives up. That was read timeout × botocore attempts plus a connect timeout per attempt:
on the hub 3 × (300 s + 60 s) = 18 min, in the worker (strands' 120 s read timeout, botocore's
legacy 5 attempts, 60 s connect) 5 × 180 s = 15 min, and an OpenAI-compatible judge (600 s,
2 retries) 30 min. These bounds cut it to about 1–2× the judge deadline:

- Bedrock (ConverseStream): a ``read_timeout`` of ``JUDGE_READ_TIMEOUT_S`` per socket read —
  a streaming judge sends bytes all along, so only a stall trips it — a
  ``JUDGE_CONNECT_TIMEOUT_S`` connect, ``JUDGE_CLIENT_ATTEMPTS`` attempts in total:
  worst case ``2 × (150 + 10) = 320 s``;
- OpenAI-compatible: ``JUDGE_OPENAI_TIMEOUT_S`` per request — it must also cover a judge that
  answers unstreamed, all at once — and ``JUDGE_CLIENT_ATTEMPTS − 1`` retries: worst case
  ``2 × 300 = 600 s``.

The engine's own transient retries (``engine_core.JUDGE_TRANSIENT_ATTEMPTS``) do not multiply
this: each attempt runs under the deadline wrapper, and ``JudgeDeadlineExceeded`` is not
retried. Client config only — no judge prompt changes, so the judge fingerprint is unchanged.

Behind a response-timeout proxy (N17-obs547 — the ``litellm`` provider behind CloudFront,
origin response timeout 120 s): the judge's requests STREAM whenever the catalog row says
``streaming`` (``OpenAIModel(stream=True)``, every Agent turn is one
``chat.completions.create(stream=True)`` — pinned by ``test_judge_openai_streaming``), and a
proxy response timeout is a between-bytes timeout, so a streamed judgement is only cut when
the model goes silent for the whole window. ``JUDGE_OPENAI_TIMEOUT_S`` is the OpenAI SDK's
httpx timeout — per socket read on a stream, the whole body otherwise — and stays 300 s: a
shorter proxy window fires first and arrives as an ``openai.InternalServerError`` (504),
retried once by the SDK (``max_retries`` 1) and then classified transient by the engine
(``runtime_invoke.is_upstream_transient`` matches the class name), so a lower client timeout
would gain nothing while a direct provider still needs the full window.
Used by the hub (``aws_clients.build_chat_model``) and the worker (``judge_model``); shipped
in the worker zip.
"""

from __future__ import annotations

from typing import Any

from botocore.config import Config

JUDGE_READ_TIMEOUT_S = 150
JUDGE_CONNECT_TIMEOUT_S = 10
JUDGE_CLIENT_ATTEMPTS = 2
JUDGE_OPENAI_TIMEOUT_S = 300


def bedrock_stall_bound_s() -> int:
    """The longest a stalled Bedrock judge call holds its thread."""
    return JUDGE_CLIENT_ATTEMPTS * (JUDGE_READ_TIMEOUT_S + JUDGE_CONNECT_TIMEOUT_S)


def openai_stall_bound_s() -> int:
    """The longest a stalled OpenAI-compatible judge call holds its thread."""
    return JUDGE_CLIENT_ATTEMPTS * JUDGE_OPENAI_TIMEOUT_S


def bedrock_client_config(*, read_timeout_s: int = JUDGE_READ_TIMEOUT_S,
                          signature_version: str | None = None) -> Config:
    """The botocore config of a Bedrock judge model's runtime client."""
    return Config(
        **({"signature_version": signature_version} if signature_version else {}),
        read_timeout=read_timeout_s,
        connect_timeout=JUDGE_CONNECT_TIMEOUT_S,
        retries={"total_max_attempts": JUDGE_CLIENT_ATTEMPTS, "mode": "standard"},
    )


def openai_client_limits(timeout_s: float | None = None) -> dict[str, Any]:
    """The ``openai.AsyncOpenAI`` timeout / retry arguments of a judge (``timeout_s`` when a
    caller names its own, else ``JUDGE_OPENAI_TIMEOUT_S``)."""
    return {"timeout": timeout_s or JUDGE_OPENAI_TIMEOUT_S,
            "max_retries": JUDGE_CLIENT_ATTEMPTS - 1}


def limit_openai_client(model: Any) -> Any:
    """``model`` (a strands ``OpenAIModel``) with the judge limits in its client arguments;
    any other model unchanged."""
    client_args = getattr(model, "client_args", None)
    if isinstance(client_args, dict):
        client_args.update(openai_client_limits())
    return model
