"""Model-provider probes and the registry credential flow (D21 plan §4, §7, §9.3; M1-b).

`ModelProbeHooks` implements `services/model_registry.ProbeHooks` — the three slots the
admin routes of `routers/model_providers.py` call — and is registered once at startup
(`app/main.py`). Every method runs in the request thread, returns a secret-free dict in the
frozen contract shape and writes only through the registry's journal.

* **probe_provider** — OpenAI-compatible: `GET {base_url}/models` with the hub's key →
  `state ok|failed`, `models_seen` (≤ 200 ids); a 404 is "no list endpoint", not a failure.
  Bedrock (built-in): the Global bearer must be present (`require_global_bedrock_bearer`)
  — no network; `models_seen` = the registry's own Bedrock chat rows.
* **probe_model** — chat: exists → tools (`tool_choice: required`, retry `auto`) → streaming
  → `temperature` → `top_p`, ≤ 6 minimal completions (+ one `/models` read for the
  `raw_hint`); embedding: one embed call → `embedding_dimension`. A field the probe could
  not settle is absent from `findings` and explained in `errors[field]` (≤ 300 sanitised
  chars). The pre-insert call returns ``{findings, errors, record}``; the re-probe returns
  ``{findings, diff, errors, raw_hint, latency_ms, applied}`` and with ``apply`` writes only
  fields whose `capability_sources[field] != "manual"`. Both probes journal audit-only rows
  (`provider.probe` / `model.probe`, never a catalog version — D21-obs6); an applied
  re-probe that really changed ≥ 1 capability field adds one catalog-shaping `model.update`
  row (`note: "applied from probe"`, before/after limited to the changed fields).
* **register_credential** — reads the row's SSM parameter (`launchpad-*-api-key`) through
  `aws_clients.ssm_client`, hands the value **once** to Identity
  (`model_credential.register_from_registry`), records `model_credentials[<name>]` on the
  hub config and every workspace row, stores `key_version` / `key_modified_at`, journals
  `provider.credential`, invalidates the hub's cached key. The value is never returned,
  logged, journaled or persisted.

Egress (plan §9.3): before **every** request `guard_base_url` re-runs the write-time rules
(https, hostname, the operator denylist, the allowlist only in lock mode — N17-obs203) and
resolves the host, refusing loopback / private /
link-local (IMDS) / multicast / reserved addresses and `.internal`/`.local` names; the
transport follows no redirects and reads no proxy environment.

Every collaborator (HTTP transport, DNS resolver, key resolver, Bedrock runtime, SSM /
Identity clients, clock) is injectable so `tests/test_model_probe.py` stays hermetic.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.ledger import Workspace
from app.models.model_registry import Model, ModelProvider
from app.schemas import model_catalog as mc
from app.services import aws_clients, model_credential, model_registry
from app.services import model_probe_bedrock as bedrock
from app.services.model_probe_openai import (
    PING_TOOL,
    OpenAICompatibleProbe,
    ProbeBlocked,
    ProbeBudgetExhausted,
    ProbeError,
    ProbeResponse,
    ProbeTransportError,
    sanitize,
)
from app.services.workspace import (
    WorkspaceContext,
    default_workspace_context,
    merge_workspace_resources,
    workspace_context,
)

log = logging.getLogger("launchpad.model_probe")

# Error codes (additive to plan §3's list).
E_SSM_PARAMETER_MISSING = "model_provider.ssm_parameter_missing"
E_SSM_READ_FAILED = "model_provider.ssm_read_failed"
E_IDENTITY_FAILED = "model_provider.identity_failed"
# N17-obs203 §3: the template statement that grants the console host's SSM writes
SSM_WRITE_SID = "SsmWriteLaunchpadSecretParameters"
# the tags the console puts on a parameter it created (AddTagsToResource, best-effort)
SSM_PARAMETER_TAGS: tuple[tuple[str, str], ...] = (("launchpad:managed", "true"),)

PROVIDER_BUDGET_S = 15.0
MODEL_BUDGET_S = 30.0
MAX_MODELS_SEEN = 200
CHAT_FIELDS: tuple[str, ...] = ("exists", "tools", "streaming", "temperature", "top_p")
EMBEDDING_FIELDS: tuple[str, ...] = ("exists", "embedding_dimension")
# Capability fields a probe may write (⊂ `CAPABILITY_SOURCE_FIELDS`).
PROBED_FIELDS: tuple[str, ...] = ("tools", "streaming", "temperature", "top_p",
                                  "embedding_dimension")
# `note` of the catalog-shaping `model.update` row an applied re-probe writes when ≥ 1
# capability field actually changed (D21-obs6); the `model.probe` row itself is audit-only.
PROBE_APPLIED_NOTE = "applied from probe"

# ── SSRF guard (plan §9.3) ────────────────────────────────────────────────────────────
_BLOCKED_SUFFIXES = (".internal", ".local", ".localhost", ".localdomain", ".home.arpa")
_BLOCKED_NETWORKS = tuple(ipaddress.ip_network(net) for net in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.168.0.0/16", "198.18.0.0/15",
    "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
    "::/128", "::1/128", "::ffff:0:0/96", "64:ff9b::/96", "fc00::/7", "fe80::/10", "ff00::/8",
))

Resolver = Callable[[str], Iterable[str]]


class HostBlockedError(ValueError):
    """The host resolves to (or names) something a probe must never reach."""


def default_resolver(host: str) -> list[str]:
    """Every address the system resolver returns for ``host`` (TCP/443)."""
    infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    return [str(info[4][0]) for info in infos]


def address_blocked(address: str) -> bool:
    """Loopback, private, link-local (incl. 169.254.169.254), CGNAT, multicast, reserved,
    unspecified, documentation ranges — v4, v6 and v4-mapped v6."""
    try:
        addr = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if (addr.is_loopback or addr.is_link_local or addr.is_private or addr.is_multicast
            or addr.is_reserved or addr.is_unspecified):
        return True
    return any(addr in network for network in _BLOCKED_NETWORKS)


def guard_base_url(base_url: str | None, *, allowed_hosts: Iterable[str] | None = None,
                   denied_hosts: Iterable[str] | None = None,
                   resolver: Resolver | None = None) -> str:
    """The per-request half of the region boundary: `model_registry.validate_base_url`
    (https, a hostname, the operator denylist always and the allowlist only in lock mode —
    N17-obs203; raises the 422 `AppError`s) **plus** DNS resolution with every returned
    address checked by `address_blocked`. Raises `HostBlockedError` (message names host +
    address, never a key). Returns the URL without a trailing slash."""
    url = model_registry.validate_base_url(base_url, mc.PROVIDER_OPENAI_COMPATIBLE,
                                           allowed_hosts, denied_hosts)
    host = (urlsplit(url).hostname or "").lower()
    if host == "localhost" or host.endswith(_BLOCKED_SUFFIXES):
        raise HostBlockedError(f"host {host!r} is a local / internal name — refused (SSRF guard)")
    try:
        addresses = [str(a) for a in (resolver or default_resolver)(host)]
    except (OSError, UnicodeError, ValueError) as exc:
        raise HostBlockedError(
            f"host {host!r} did not resolve ({type(exc).__name__}) — refused") from exc
    if not addresses:
        raise HostBlockedError(f"host {host!r} resolved to no address — refused")
    for address in addresses:
        if address_blocked(address):
            raise HostBlockedError(
                f"host {host!r} resolves to {address}, a loopback / private / link-local "
                "address — refused (SSRF guard)")
    return url


# ── small helpers ────────────────────────────────────────────────────────────────────
def _iso(value: datetime | None) -> str | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _model_missing(response: ProbeResponse) -> bool:
    if response.status == 404:
        return True
    if response.status not in (400, 422):
        return False
    text = f"{response.message()} {response.text}".lower()
    return "model" in text and any(marker in text for marker in (
        "not found", "not exist", "doesn't exist", "not available", "not supported",
        "unsupported", "unknown", "invalid", "no such", "does not exist"))


def _has_tool_calls(response: ProbeResponse) -> bool:
    body = response.body if isinstance(response.body, dict) else {}
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return False
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return False
    calls = message.get("tool_calls")
    return (isinstance(calls, list) and len(calls) > 0) or bool(message.get("function_call"))


class _Stop(Exception):
    """Raised inside a probe run when every further request would fail the same way."""


class _Run:
    """One probe run's findings / errors with the §7.4 bookkeeping."""

    def __init__(self, fields: tuple[str, ...]) -> None:
        self.fields = fields
        self.findings: dict[str, Any] = {}
        self.errors: dict[str, str] = {}
        self.raw_hint: dict[str, Any] = {}

    def settle(self, field: str, fn: Callable[[], Any], *, unsettled: str) -> None:
        """Run ``fn``: a value settles the field, ``None`` leaves it unsettled with
        ``unsettled`` as the reason, a `ProbeError` stores its text; a blocked host or an
        exhausted budget stops the run (the remaining fields are marked skipped)."""
        try:
            value = fn()
        except (ProbeBlocked, ProbeBudgetExhausted) as exc:
            self.errors[field] = str(exc)[:300]
            raise _Stop from exc
        except ProbeError as exc:
            self.errors[field] = str(exc)[:300]
            return
        if value is None:
            self.errors[field] = unsettled[:300]
        else:
            self.findings[field] = value

    def skip_rest(self, reason: str) -> None:
        for field in self.fields:
            if field not in self.findings and field not in self.errors:
                self.errors[field] = f"skipped: {reason}"[:300]


# ── OpenAI-compatible probes (plan §7.1–7.3) ─────────────────────────────────────────
def _status_error(client: OpenAICompatibleProbe, response: ProbeResponse) -> str:
    return f"HTTP {response.status} — {client.sanitize(response.message())}"


def probe_openai_chat(client: OpenAICompatibleProbe, model: str) -> _Run:
    run = _Run(CHAT_FIELDS)
    try:
        # raw_hint: what `/models` says about this id (never applied silently, plan §7.2)
        try:
            listing = client.list_models()
        except ProbeBlocked as exc:
            run.errors["exists"] = str(exc)[:300]
            raise _Stop from exc
        except ProbeError:
            listing = None
        if listing is not None and listing.status == 200 and isinstance(listing.body, dict):
            for entry in listing.body.get("data") or []:
                if isinstance(entry, dict) and entry.get("id") == model:
                    for source, target in (("max_output_tokens", "max_output_tokens"),
                                           ("max_tokens", "max_output_tokens"),
                                           ("context_length", "context_length"),
                                           ("context_window", "context_length")):
                        value = entry.get(source)
                        if isinstance(value, int) and not isinstance(value, bool):
                            run.raw_hint.setdefault(target, value)
                    break

        # 1. exists / reachable
        try:
            first = client.chat(model)
        except (ProbeBlocked, ProbeBudgetExhausted) as exc:
            run.errors["exists"] = str(exc)[:300]
            raise _Stop from exc
        except ProbeError as exc:
            run.errors["exists"] = str(exc)[:300]
            raise _Stop from exc
        if first.status == 200:
            run.findings["exists"] = True
        elif first.status in (401, 403):
            run.errors["exists"] = f"auth_failed: {_status_error(client, first)}"
            raise _Stop
        elif _model_missing(first):
            run.findings["exists"] = False
            run.errors["exists"] = f"model_missing: {_status_error(client, first)}"
            raise _Stop
        else:
            run.errors["exists"] = _status_error(client, first)
            raise _Stop

        # 2. tools — `tool_choice: required`, retry `auto` + instruction (plan §7.2)
        def tools_probe() -> bool | None:
            def with_auto() -> bool | None:
                retry = client.chat(
                    model, tools=[PING_TOOL], tool_choice="auto",
                    messages=[{"role": "user", "content": "Call the ping tool."}])
                if retry.status == 200:
                    return True if _has_tool_calls(retry) else None
                if retry.names("tool", "function"):
                    return False
                raise ProbeTransportError(_status_error(client, retry))

            response = client.chat(model, tools=[PING_TOOL], tool_choice="required")
            if response.status == 200:
                return True if _has_tool_calls(response) else with_auto()
            if response.names("tool_choice"):
                return with_auto()
            if response.names("tool", "function"):
                return False
            raise ProbeTransportError(_status_error(client, response))

        run.settle("tools", tools_probe,
                   unsettled="no tool call returned with tool_choice required or auto — "
                             "unverified")

        # 3. streaming — first `data:` chunk
        def streaming_probe() -> bool | None:
            response = client.chat(model, stream=True)
            if response.status == 200:
                return True if response.first_chunk else None
            if response.names("stream"):
                return False
            raise ProbeTransportError(_status_error(client, response))

        run.settle("streaming", streaming_probe,
                   unsettled="stream opened but no data chunk arrived — unverified")

        # 4./5. inference params — a 400 naming the field is a rejection (N12-bug11 class)
        def param_probe(field: str, value: float, *names: str) -> Callable[[], bool | None]:
            def probe() -> bool | None:
                response = client.chat(model, **{field: value})
                if response.status == 200:
                    return True
                if response.names(*names):
                    return False
                raise ProbeTransportError(_status_error(client, response))
            return probe

        run.settle("temperature", param_probe("temperature", 0.1, "temperature"),
                   unsettled="unverified")
        run.settle("top_p", param_probe("top_p", 0.9, "top_p", "top p", "topp"),
                   unsettled="unverified")
    except _Stop:
        run.skip_rest("earlier step failed or the probe time budget is exhausted")
    return run


def probe_openai_embedding(client: OpenAICompatibleProbe, model: str) -> _Run:
    run = _Run(EMBEDDING_FIELDS)
    try:
        response = client.embed(model)
    except ProbeError as exc:
        run.errors["exists"] = str(exc)[:300]
        run.skip_rest("the embed call failed")
        return run
    if response.status == 200:
        run.findings["exists"] = True
        data = response.body.get("data") if isinstance(response.body, dict) else None
        vector = (data[0].get("embedding") if isinstance(data, list) and data
                  and isinstance(data[0], dict) else None)
        if isinstance(vector, list) and vector:
            run.findings["embedding_dimension"] = len(vector)
        else:
            run.errors["embedding_dimension"] = "the response carried no embedding vector"
    elif response.status in (401, 403):
        run.errors["exists"] = f"auth_failed: {_status_error(client, response)}"
    elif _model_missing(response):
        run.findings["exists"] = False
        run.errors["exists"] = f"model_missing: {_status_error(client, response)}"
    else:
        run.errors["exists"] = _status_error(client, response)
    run.skip_rest("the embed call failed")
    return run


# ── Bedrock probes (plan §7.1–7.3; region-boundary: the Global bearer client) ───────
def _bedrock_step(fn: Callable[[], Any], *, missing_ok: bool = False) -> Any:
    """Run one Bedrock call; `ClientError` → `ProbeTransportError` (sanitised code +
    message) unless the caller classifies it itself."""
    try:
        return fn()
    except ClientError as exc:
        raise ProbeTransportError(bedrock.error_message(exc)) from exc
    except BotoCoreError as exc:
        raise ProbeTransportError(sanitize(f"{type(exc).__name__}: {exc}")) from exc


def probe_bedrock_chat(client: bedrock.BedrockProbe, model_id: str) -> _Run:
    run = _Run(CHAT_FIELDS)
    try:
        try:
            client.converse(model_id)
        except ClientError as exc:
            if bedrock.is_model_missing(exc):
                run.findings["exists"] = False
                run.errors["exists"] = f"model_missing: {bedrock.error_message(exc)}"
            elif bedrock.error_code(exc) in ("UnrecognizedClientException", "AccessDeniedException",
                                             "InvalidSignatureException"):
                run.errors["exists"] = f"auth_failed: {bedrock.error_message(exc)}"
            else:
                run.errors["exists"] = bedrock.error_message(exc)
            raise _Stop from exc
        except (BotoCoreError, ProbeBudgetExhausted) as exc:
            run.errors["exists"] = sanitize(f"{type(exc).__name__}: {exc}")
            raise _Stop from exc
        run.findings["exists"] = True

        def tools_probe() -> bool | None:
            try:
                response = client.converse(
                    model_id, toolConfig={"tools": [bedrock.PING_TOOL_SPEC],
                                          "toolChoice": {"any": {}}})
            except ClientError as exc:
                if bedrock.rejects_field(exc, "tool"):
                    return False
                raise ProbeTransportError(bedrock.error_message(exc)) from exc
            return True if response.get("stopReason") == "tool_use" else None

        run.settle("tools", tools_probe,
                   unsettled="Converse with toolChoice any did not stop on tool_use — unverified")

        def streaming_probe() -> bool | None:
            try:
                event = client.converse_stream_first_event(model_id)
            except ClientError as exc:
                if bedrock.rejects_field(exc, "stream"):
                    return False
                raise ProbeTransportError(bedrock.error_message(exc)) from exc
            return True if event is not None else None

        run.settle("streaming", streaming_probe,
                   unsettled="ConverseStream returned no event — unverified")

        def param_probe(field: str, value: float, *names: str) -> Callable[[], bool | None]:
            def probe() -> bool | None:
                try:
                    client.converse(model_id, inferenceConfig={field: value})
                except ClientError as exc:
                    if bedrock.rejects_field(exc, *names):
                        return False
                    raise ProbeTransportError(bedrock.error_message(exc)) from exc
                return True
            return probe

        run.settle("temperature", param_probe("temperature", 0.1, "temperature"),
                   unsettled="unverified")
        run.settle("top_p", param_probe("topP", 0.9, "topp", "top_p", "top p"),
                   unsettled="unverified")
    except _Stop:
        run.skip_rest("earlier step failed or the probe time budget is exhausted")
    return run


def probe_bedrock_embedding(client: bedrock.BedrockProbe, model_id: str) -> _Run:
    run = _Run(EMBEDDING_FIELDS)
    try:
        dimension = client.embedding_dimension(model_id)
    except ClientError as exc:
        if bedrock.is_model_missing(exc):
            run.findings["exists"] = False
            run.errors["exists"] = f"model_missing: {bedrock.error_message(exc)}"
        else:
            run.errors["exists"] = bedrock.error_message(exc)
        run.skip_rest("the InvokeModel call failed")
        return run
    except (BotoCoreError, ProbeBudgetExhausted) as exc:
        run.errors["exists"] = sanitize(f"{type(exc).__name__}: {exc}")
        run.skip_rest("the InvokeModel call failed")
        return run
    run.findings["exists"] = True
    if dimension:
        run.findings["embedding_dimension"] = dimension
    else:
        run.errors["embedding_dimension"] = ("the response carried no embedding vector "
                                             "(not a Titan-style embedder) — set it manually")
    return run


# ── the hooks ────────────────────────────────────────────────────────────────────────
class ModelProbeHooks:
    """`model_registry.ProbeHooks` for the real AWS / HTTP collaborators; every one is
    injectable (tests pass `httpx.MockTransport`, fake resolvers / clients / clocks)."""

    def __init__(
        self,
        *,
        transport: Any = None,
        resolver: Resolver | None = None,
        key_resolver: Callable[[str], str] | None = None,
        bearer_check: Callable[[], str] | None = None,
        bedrock_runtime: Callable[[str], Any] | None = None,
        ssm_factory: Callable[[WorkspaceContext], Any] | None = None,
        ssm_fresh_factory: Callable[[WorkspaceContext], Any] | None = None,
        control_factory: Callable[[WorkspaceContext], Any] | None = None,
        hub_context: Callable[[], WorkspaceContext] | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._transport = transport
        self._resolver = resolver
        self._key_resolver = key_resolver or aws_clients.provider_api_key
        self._bearer_check = bearer_check or aws_clients.require_global_bedrock_bearer
        self._bedrock_runtime = bedrock_runtime or aws_clients.global_bedrock_runtime_client
        self._ssm_factory = ssm_factory or aws_clients.ssm_client
        # N17-obs232: the one retry of a refused tag call runs on an uncached client; a test
        # that injects a fake `ssm_factory` gets the same fake unless it says otherwise
        self._ssm_fresh_factory = ssm_fresh_factory or (
            ssm_factory if ssm_factory is not None
            else (lambda ctx: aws_clients.ssm_client(ctx, fresh=True)))
        self._control_factory = control_factory or (
            lambda ctx: ctx.client("bedrock-agentcore-control"))
        self._hub_context = hub_context or default_workspace_context
        self._clock = clock
        self._now = now

    # ── helpers ──
    def _guard(self, url: str) -> str:
        return guard_base_url(url, resolver=self._resolver)

    def _openai_client(self, provider: ModelProvider, api_key: str, budget: float
                       ) -> OpenAICompatibleProbe:
        return OpenAICompatibleProbe(
            str(provider.base_url or ""), api_key, guard=self._guard,
            deadline=self._clock() + budget, transport=self._transport, clock=self._clock)

    def _key_for(self, provider: ModelProvider) -> tuple[str | None, str | None]:
        """(key, error) — the error names the SSM parameter / variable, never a value."""
        try:
            return self._key_resolver(provider.name), None
        except RuntimeError as exc:
            return None, f"credential_unavailable: {sanitize(exc)}"

    # ── ProbeHooks.probe_provider ──
    def probe_provider(self, db: Session, provider: ModelProvider, actor: str) -> dict[str, Any]:
        started = self._clock()
        before = dict(provider.probe) if isinstance(provider.probe, dict) else dict(
            model_registry.PROBE_NEVER)
        if provider.kind == mc.PROVIDER_BEDROCK:
            state, seen, error = self._probe_bedrock_provider(db, provider)
        else:
            state, seen, error = self._probe_openai_provider(provider)
        result: dict[str, Any] = {
            "state": state,
            "checked_at": _iso(self._now()),
            "latency_ms": int((self._clock() - started) * 1000),
            "models_seen": [str(m) for m in seen][:MAX_MODELS_SEEN],
            "error": (error[:300] if error else None),
        }
        provider.probe = result
        provider.updated_by = actor
        db.flush()
        model_registry._journal(db, "provider.probe", actor, provider.id,
                                {"probe": before}, {"probe": dict(result)},
                                scope=model_registry._journal_scope(provider))
        db.commit()
        model_registry.refresh(db)
        log.info("model-provider probe %s: %s (%s ms, %d models)", provider.name, state,
                 result["latency_ms"], len(result["models_seen"]))
        return dict(result)

    def _probe_bedrock_provider(self, db: Session, provider: ModelProvider
                                ) -> tuple[str, list[str], str | None]:
        try:
            self._bearer_check()
        except RuntimeError as exc:
            return "failed", [], f"credential_unavailable: {sanitize(exc)}"
        rows = db.scalars(select(Model).where(Model.provider_id == provider.id,
                                              Model.kind == mc.MODEL_KIND_CHAT)
                          .order_by(Model.sort_order, Model.created_at)).all()
        return "ok", [str(m.api_model_name) for m in rows], None

    def _probe_openai_provider(self, provider: ModelProvider
                               ) -> tuple[str, list[str], str | None]:
        key, error = self._key_for(provider)
        if key is None:
            return "failed", [], error
        with self._openai_client(provider, key, PROVIDER_BUDGET_S) as client:
            try:
                response = client.list_models()
            except ProbeError as exc:
                return "failed", [], str(exc)
            if response.status == 200:
                data = response.body.get("data") if isinstance(response.body, dict) else None
                ids = [str(e["id"]) for e in (data or []) if isinstance(e, dict) and e.get("id")]
                return "ok", ids, None
            if response.status in (401, 403):
                return "failed", [], f"auth_failed: {_status_error(client, response)}"
            if response.status == 404:
                return "ok", [], ("models_endpoint_unsupported (HTTP 404): the endpoint has no "
                                  "/models list — model probes still work")
            return "failed", [], _status_error(client, response)

    # ── ProbeHooks.probe_model ──
    def probe_model(self, db: Session, provider: ModelProvider, api_model_name: str,
                    kind: str, model: Model | None, *, apply: bool, actor: str
                    ) -> dict[str, Any]:
        started = self._clock()
        run = self._run_model_probe(provider, api_model_name, kind)
        latency_ms = int((self._clock() - started) * 1000)
        fields = EMBEDDING_FIELDS if kind == mc.MODEL_KIND_EMBEDDING else CHAT_FIELDS
        record = {
            "checked_at": _iso(self._now()),
            "latency_ms": latency_ms,
            "findings": {field: run.findings.get(field) for field in fields},
            "errors": dict(run.errors),
            "raw_hint": dict(run.raw_hint),
        }
        settled = {k: v for k, v in run.findings.items() if v is not None}
        if model is None:
            return {"findings": settled, "errors": dict(run.errors), "record": record}
        return self._reprobe(db, provider, model, settled, run, record, latency_ms,
                             apply=apply, actor=actor)

    def _run_model_probe(self, provider: ModelProvider, api_model_name: str, kind: str) -> _Run:
        fields = EMBEDDING_FIELDS if kind == mc.MODEL_KIND_EMBEDDING else CHAT_FIELDS
        if provider.kind == mc.PROVIDER_BEDROCK:
            try:
                runtime = self._bedrock_runtime(provider.model_region or mc.DEFAULT_MODEL_REGION)
            except RuntimeError as exc:
                run = _Run(fields)
                run.errors["exists"] = f"credential_unavailable: {sanitize(exc)}"
                run.skip_rest("no Global Bedrock bearer")
                return run
            client = bedrock.BedrockProbe(runtime, deadline=self._clock() + MODEL_BUDGET_S,
                                          clock=self._clock)
            if kind == mc.MODEL_KIND_EMBEDDING:
                return probe_bedrock_embedding(client, api_model_name)
            return probe_bedrock_chat(client, api_model_name)
        key, error = self._key_for(provider)
        if key is None:
            run = _Run(fields)
            run.errors["exists"] = error or "credential_unavailable"
            run.skip_rest("no credential")
            return run
        with self._openai_client(provider, key, MODEL_BUDGET_S) as client:
            if kind == mc.MODEL_KIND_EMBEDDING:
                return probe_openai_embedding(client, api_model_name)
            return probe_openai_chat(client, api_model_name)

    def _reprobe(self, db: Session, provider: ModelProvider, model: Model,
                 settled: Mapping[str, Any], run: _Run, record: dict[str, Any],
                 latency_ms: int, *, apply: bool, actor: str) -> dict[str, Any]:
        params = dict(model.inference_params or {})
        sources = dict(model.capability_sources or {})

        def stored(field: str) -> Any:
            if field in ("temperature", "top_p"):
                return bool(params.get(field, True))
            return getattr(model, field, None)

        diff = {
            field: {"stored": stored(field), "probed": value,
                    "source": sources.get(field, mc.SOURCE_MANUAL)}
            for field, value in settled.items() if field in PROBED_FIELDS
        }
        applied = False
        if apply:
            before = model_registry.model_dict(model, provider)
            # D21-obs6: `model.probe` is an audit row (never a catalog version); the fields
            # whose stored value really changed are journaled separately below as one
            # catalog-shaping `model.update` row, so the version bumps exactly when the
            # catalog content did.
            changed_before: dict[str, Any] = {}
            changed_after: dict[str, Any] = {}
            for field, value in settled.items():
                if field not in PROBED_FIELDS or sources.get(field) == mc.SOURCE_MANUAL:
                    continue
                previous = stored(field)
                if field in ("temperature", "top_p"):
                    value = bool(value)
                    params[field] = value
                else:
                    setattr(model, field, value)
                sources[field] = mc.SOURCE_PROBED
                applied = True
                if previous != value:
                    changed_before[field] = previous
                    changed_after[field] = value
            model.inference_params = params
            model.capability_sources = sources
            model.probe = record
            model.updated_by = actor
            if applied and not any(sources.get(f) == mc.SOURCE_MANUAL
                                   for f in mc.CAPABILITY_SOURCE_FIELDS):
                model.source = mc.SOURCE_PROBED
            db.flush()
            scope = model_registry._journal_scope(provider)
            model_registry._journal(db, "model.probe", actor, model.id, before,
                                    model_registry.model_dict(model, provider), scope=scope)
            if changed_after:
                model_registry._journal(db, "model.update", actor, model.id, changed_before,
                                        changed_after, note=PROBE_APPLIED_NOTE, scope=scope)
            db.commit()
            model_registry.refresh(db)
        return {
            "findings": dict(settled),
            "diff": diff,
            "errors": dict(run.errors),
            "raw_hint": dict(run.raw_hint),
            "latency_ms": latency_ms,
            "applied": applied,
        }

    # ── ProbeHooks.register_credential (plan §4) ──
    def register_credential(self, db: Session, provider: ModelProvider, actor: str
                            ) -> dict[str, Any]:
        hub = self._hub_context()
        ssm_name = str(provider.credential_ssm_parameter or "")
        env_name = str(provider.credential_env or "")
        secret: model_credential.SsmSecret | None = None
        source = model_credential.CREDENTIAL_SOURCE_SSM
        if provider.credential_source == model_credential.CREDENTIAL_SOURCE_HOST_ENV and env_name:
            value = os.environ.get(env_name, "").strip()
            if value:
                secret = model_credential.SsmSecret(value=value, version=None, modified_at=None)
                source = model_credential.CREDENTIAL_SOURCE_HOST_ENV
                del value
        if secret is None and ssm_name:
            try:
                secret = model_credential.read_ssm_parameter(self._ssm_factory(hub), ssm_name)
            except model_credential.ParameterMissingError as exc:
                raise AppError(
                    E_SSM_PARAMETER_MISSING,
                    f"{exc} ({provider.label}; host variable {env_name or 'n/a'} is unset too)",
                    {"ssm_parameter": ssm_name, "env": env_name or None,
                     "empty": bool(exc.empty)}, status_code=422) from exc
            except ClientError as exc:
                code = bedrock.error_code(exc)
                raise AppError(
                    E_SSM_READ_FAILED,
                    f"SSM parameter {ssm_name} could not be read ({code}) — the console host's "
                    "role needs ssm:GetParameter on launchpad-*-api-key (deploy.md §8.6)",
                    {"ssm_parameter": ssm_name, "code": code}, status_code=502) from exc
            except (BotoCoreError, ValueError) as exc:
                raise AppError(
                    E_SSM_READ_FAILED,
                    f"SSM parameter {ssm_name} could not be read ({type(exc).__name__})",
                    {"ssm_parameter": ssm_name, "code": type(exc).__name__},
                    status_code=502) from exc
        if secret is None:
            raise AppError(
                E_SSM_PARAMETER_MISSING,
                f"{provider.label} has no key source: set credential_ssm_parameter "
                "(launchpad-<name>-api-key) or export "
                f"{env_name or 'its host variable'}",
                {"ssm_parameter": ssm_name or None, "env": env_name or None}, status_code=422)
        control = self._control_factory(hub)
        # D21-obs5: an unchanged SSM parameter (same Version / LastModifiedDate as the hub row
        # recorded) is not re-pushed to Identity and not journaled — the admin sees
        # `rotated: false` and the catalog version stays put. A `host_env` key has no version
        # to compare → always rotated in place (that path never journaled a version anyway).
        current = model_credential.key_is_current(
            secret, version=provider.key_version, modified_at=provider.key_modified_at)
        try:
            result = model_credential.register_from_registry(
                control, provider, secret.value, log.info,
                source=(f"SSM parameter {ssm_name} (version {secret.version})"
                        if source == model_credential.CREDENTIAL_SOURCE_SSM else env_name),
                rotate=not current, key_version=secret.version)
        except ClientError as exc:
            code = bedrock.error_code(exc)
            raise AppError(
                E_IDENTITY_FAILED,
                f"AgentCore Identity refused {provider.credential_provider} ({code})",
                {"credential_provider": provider.credential_provider, "code": code},
                status_code=502) from exc
        except BotoCoreError as exc:
            raise AppError(
                E_IDENTITY_FAILED,
                f"AgentCore Identity unreachable for {provider.credential_provider} "
                f"({type(exc).__name__})",
                {"credential_provider": provider.credential_provider,
                 "code": type(exc).__name__}, status_code=502) from exc
        version, modified_at = secret.version, secret.modified_at
        del secret  # the value's only reference — it went to Identity and nowhere else
        record = model_credential.credential_record(result, key_version=version, source=source)
        workspaces_updated = self._record_on_workspaces(db, hub, provider.name, record)
        created, rotated = bool(result["created"]), bool(result["rotated"])
        model_credential.record_registered_key(
            db, provider, version=version, modified_at=modified_at,
            created=created, rotated=rotated, actor=actor)
        aws_clients.invalidate_provider_api_key(provider.name)
        log.info("model-credential %s registered from %s by %s (%s; %d workspace(s))",
                 provider.credential_provider,
                 f"SSM parameter {ssm_name}" if source == "ssm" else env_name, actor,
                 "created" if created else ("rotated" if rotated else "up to date"),
                 workspaces_updated)
        return {
            "configured": True,
            "created": created,
            "rotated": rotated,
            "workspaces_updated": workspaces_updated,
            "key_version": version,
        }

    # ── N17-obs203 §3: a key typed in the console ──
    def probe_api_key(self, provider: ModelProvider, api_key: str) -> None:
        """Reach `{base_url}/models` with the TYPED key before anything is stored (the same
        request as `probe_provider`, with this key instead of the hub's). 401 / 403 → 422
        `model_provider.probe_failed` (`reason: auth_failed …`); a blocked host, timeout or
        unexpected status → 422 with the sanitised reason; **404 passes** (some gateways have
        no `/models` list — the endpoint answered, the key is used by the first model probe).
        A Bedrock row takes no key (422 `model_provider.api_key_unsupported`). The value is
        this argument alone; every message is sanitised against it."""
        if provider.kind == mc.PROVIDER_BEDROCK:
            raise AppError(model_registry.E_API_KEY_UNSUPPORTED,
                           "a Global Bedrock provider rides the Global Bedrock API key of the "
                           "host — it takes no key of its own", {"kind": provider.kind},
                           status_code=422)
        with self._openai_client(provider, api_key, PROVIDER_BUDGET_S) as client:
            try:
                response = client.list_models()
            except ProbeError as exc:
                reason = client.sanitize(str(exc))
                raise AppError(model_registry.E_PROVIDER_PROBE_FAILED,
                               f"{provider.label or provider.name}: the endpoint could not be "
                               f"reached with the key you entered — nothing was stored ({reason})",
                               {"reason": reason, "host": urlsplit(
                                   str(provider.base_url or "")).hostname}, status_code=422
                               ) from exc
            if response.status in (200, 404):
                return
            reason = (f"auth_failed: {_status_error(client, response)}"
                      if response.status in (401, 403) else _status_error(client, response))
            raise AppError(model_registry.E_PROVIDER_PROBE_FAILED,
                           f"{provider.label or provider.name}: the endpoint refused the key you "
                           f"entered — nothing was stored ({reason})",
                           {"reason": reason, "status": response.status}, status_code=422)

    def store_api_key(self, db: Session, provider: ModelProvider, api_key: str, actor: str
                      ) -> dict[str, Any]:
        """Write the typed key to the row's SSM SecureString on the hub's `ssm` client, drop
        it, then run the existing `register_credential` path (SSM → Identity → hub record) and
        keep ONLY the last four characters (`model_registry.record_entered_key`, audit-only
        journal row). The value never reaches a log line, the journal, the ledger or the
        response. 502 `model_provider.ssm_write_failed` names the IAM statement.

        Tagging (N17-obs232, deploy-ops-29 on t1: `PutParameter` / `DeleteParameter` succeeded
        while the separate best-effort `AddTagsToResource` was refused twice right after the
        IAM update): a parameter that does not exist yet is CREATED with its tags in the one
        `PutParameter` call (`Overwrite=False, Tags=[…]` — what the API allows only when not
        overwriting), so no second call is needed; `ParameterAlreadyExists` falls back to the
        overwrite (a key replacement — the parameter already carries its tags) followed by a
        best-effort `AddTagsToResource` with ONE retry on a fresh client. A refused tag call is
        a WARNING, never a gate; a create refused WITH tags is retried without them."""
        hub = self._hub_context()
        name = str(provider.credential_ssm_parameter or "")
        if not model_credential.SSM_PARAMETER_RE.fullmatch(name):
            raise AppError(model_registry.E_PROVIDER_INVALID_SSM,
                           f"{name!r} does not match launchpad-*-api-key",
                           {"ssm_parameter": name}, status_code=422)
        last4 = str(api_key)[-4:]
        tags = self._parameter_tags(provider)
        ssm = self._ssm_factory(hub)
        try:
            created = self._put_parameter(ssm, name, api_key, tags)
        except ClientError as exc:
            code = bedrock.error_code(exc)
            raise AppError(
                model_registry.E_SSM_WRITE_FAILED,
                f"SSM parameter {name} could not be written ({code}) — the console host's role "
                f"needs ssm:PutParameter on launchpad-*-api-key (Sid {SSM_WRITE_SID}, "
                "deploy.md §8.9)",
                {"ssm_parameter": name, "code": code, "statement": SSM_WRITE_SID},
                status_code=502) from exc
        except BotoCoreError as exc:
            raise AppError(
                model_registry.E_SSM_WRITE_FAILED,
                f"SSM parameter {name} could not be written ({type(exc).__name__})",
                {"ssm_parameter": name, "code": type(exc).__name__, "statement": SSM_WRITE_SID},
                status_code=502) from exc
        finally:
            del api_key  # the value's only reference in this frame
        if created != "created_with_tags":
            self._tag_parameter(hub, ssm, provider, name, tags)
        aws_clients.invalidate_provider_api_key(provider.name)
        result = self.register_credential(db, provider, actor)
        # rev-r22 (obs232 nit): a parameter that PRE-EXISTED the first console entry was the
        # operator's — overwriting its value does not make it console-written, so a later
        # `forget_api_key` (delete / host change) leaves it in place
        console_written = created != "overwritten" or provider.key_entered_at is not None
        model_registry.record_entered_key(db, provider, last4=last4, actor=actor,
                                          entered_at=self._now(),
                                          console_written=console_written)
        log.info("model-credential %s: key entered in the console by %s written to SSM "
                 "parameter %s (…%s, %s%s)", provider.name, actor, name, last4, created,
                 "" if console_written else " — operator-created parameter, kept on delete")
        return {**result, "last4": last4}

    @staticmethod
    def _parameter_tags(provider: ModelProvider) -> list[dict[str, str]]:
        return ([{"Key": k, "Value": v} for k, v in SSM_PARAMETER_TAGS]
                + [{"Key": "launchpad:provider", "Value": provider.name}])

    @staticmethod
    def _put_parameter(ssm: Any, name: str, value: str, tags: list[dict[str, str]]) -> str:
        """`PutParameter` the way N17-obs232 wants it: create WITH tags when the parameter is
        new, overwrite without them when it exists. Returns `created_with_tags` /
        `created_untagged` (the tagged create was refused — AccessDenied on the tag half — and
        the untagged create went through) / `overwritten`. The value lives in the arguments
        only. Raises the SSM `ClientError` / `BotoCoreError` of a failed write."""
        base = {"Name": name, "Value": value, "Type": "SecureString"}
        try:
            ssm.put_parameter(**base, Overwrite=False, Tags=tags)
            return "created_with_tags"
        except ClientError as exc:
            code = bedrock.error_code(exc)
            if code == "ParameterAlreadyExists":
                ssm.put_parameter(**base, Overwrite=True)
                return "overwritten"
            if code not in ("AccessDeniedException", "AccessDenied"):
                raise
            # the create itself may be allowed while the tag half is not — never let a tag
            # convenience refuse the key write
            log.warning("model-credential: tagged create of SSM parameter %s refused (%s) — "
                        "creating untagged, tags best-effort", name, code)
        try:
            ssm.put_parameter(**base, Overwrite=False)
            return "created_untagged"
        except ClientError as exc:
            if bedrock.error_code(exc) != "ParameterAlreadyExists":
                raise
            ssm.put_parameter(**base, Overwrite=True)
            return "overwritten"

    def _tag_parameter(self, hub: WorkspaceContext, ssm: Any, provider: ModelProvider,
                       name: str, tags: list[dict[str, str]]) -> bool:
        """Best-effort `AddTagsToResource launchpad:managed / launchpad:provider` — once on the
        hub's client, once more on a fresh client (N17-obs232); a refusal is a WARNING naming
        the error code only. Returns whether the tags landed."""
        for attempt, client_ in enumerate((ssm, None)):
            try:
                target = client_ if client_ is not None else self._ssm_fresh_factory(hub)
                target.add_tags_to_resource(ResourceType="Parameter", ResourceId=name, Tags=tags)
                return True
            except (ClientError, BotoCoreError) as exc:  # tags are a convenience, never a gate
                code = (bedrock.error_code(exc) if isinstance(exc, ClientError)
                        else type(exc).__name__)
                log.warning("model-credential %s: could not tag SSM parameter %s (%s)%s",
                            provider.name, name, code,
                            " — retrying once on a fresh client" if attempt == 0
                            else "; left untagged (ssm:AddTagsToResource on "
                                 "launchpad-*-api-key, deploy.md §8.9)")
        return False

    def forget_api_key(self, db: Session, provider: ModelProvider, actor: str
                       ) -> dict[str, Any]:
        """Best-effort `DeleteParameter` of a key the CONSOLE wrote (`key_entered_at` set) —
        called before the row is deleted; an operator-created parameter is never touched.
        Returns the residue report `{ssm_parameter, deleted, error}` the delete journals."""
        name = str(provider.credential_ssm_parameter or "")
        if provider.key_entered_at is None or not name:
            return {"ssm_parameter": name or None, "deleted": False, "error": None,
                    "skipped": "not written by the console"}
        try:
            self._ssm_factory(self._hub_context()).delete_parameter(Name=name)
        except ClientError as exc:
            code = bedrock.error_code(exc)
            if code != "ParameterNotFound":
                log.warning("model-credential %s: SSM parameter %s left behind on delete (%s)",
                            provider.name, name, code)
                return {"ssm_parameter": name, "deleted": False, "error": code}
        except BotoCoreError as exc:
            log.warning("model-credential %s: SSM parameter %s left behind on delete (%s)",
                        provider.name, name, type(exc).__name__)
            return {"ssm_parameter": name, "deleted": False, "error": type(exc).__name__}
        aws_clients.invalidate_provider_api_key(provider.name)
        log.info("model-credential %s: SSM parameter %s deleted with the provider by %s",
                 provider.name, name, actor)
        return {"ssm_parameter": name, "deleted": True, "error": None}

    def forget_credential_record(self, db: Session, provider: ModelProvider) -> int:
        """N17-obs229: a provider's `resources.model_credentials[<name>]` record survived its
        row — a successor row of the same name (any member's personal `foo` after the admin's
        `foo` was deleted) read `configured: true` off the workspace maps and `source: ssm` made
        `aws_clients.provider_api_key` look the successor's parameter up as registered. The
        record is kept (the Identity provider is not deleted — rotation history) but flipped to
        `configured: false` on the hub row and on every spoke row that carries it, and the hub
        key cache is dropped. Since N17-obs280 the hub's SSM read no longer consults the record
        (a successor row has no key facts); the flip keeps every workspace's availability
        honest. Returns the number of rows written."""
        count = self._unconfigure_record(db, provider, {"deleted_provider_id": provider.id})
        if count:
            log.info("model-credential %s: hub record marked unconfigured on %d workspace(s) "
                     "with the provider's deletion", provider.name, count)
        return count

    def _unconfigure_record(self, db: Session, provider: ModelProvider,
                            extra: Mapping[str, Any]) -> int:
        """The local ledger half (N17-obs231: lives in the registry so it exists without the
        probe module too); drops the process key cache."""
        return model_registry.unconfigure_credential_record(db, provider.name, **extra)

    # ── N17-obs228: the host changed, no key re-entered → the stored key is cleared ──
    def clear_api_key(self, db: Session, provider: ModelProvider, actor: str
                      ) -> dict[str, Any]:
        """Remove the key the hub holds for ``provider`` everywhere it can reach, best-effort,
        before the row is re-pointed at another host — three independent steps, in this order:

        1. the hub / spoke record → `configured: false` + `cleared_on_host_change` and the
           process cache (a LOCAL ledger write, no AWS). FIRST (N17-obs231), so no workspace
           keeps offering the provider while a later step fails. Since N17-obs280 the record
           no longer licenses `aws_clients.provider_api_key`'s SSM read — the row's key facts,
           reset in the row commit before this hook runs, are the only gate;
        2. the SSM parameter when the console wrote it (`forget_api_key`; an operator-created
           parameter is never touched);
        3. the Identity API-key provider (`DeleteApiKeyCredentialProvider` — a runtime's
           `GetResourceApiKey` for the OLD host stops working at once).

        One step's failure never skips the next: AWS refusals (`ClientError` / `BotoCoreError`)
        are logged and reported as before; anything else is collected per step and raised ONCE
        at the end as `model_registry.CredentialClearError` carrying the same report, after
        every step ran. Returns the residue report the `provider.update` journal note carries:
        `{ssm_parameter, ssm_deleted, identity_deleted, record_unconfigured, error,
        failed_steps}` — never a value."""
        report: dict[str, Any] = {"ssm_parameter": str(provider.credential_ssm_parameter or "")
                                  or None,
                                  "ssm_deleted": False, "identity_deleted": False,
                                  "record_unconfigured": 0, "error": None,
                                  "failed_steps": []}
        errors: list[str] = []

        def step(name: str, fn: Callable[[], None]) -> None:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 — collected; the next step still runs
                log.warning("model-credential %s: clearing step %s failed on host change (%s)",
                            provider.name, name, type(exc).__name__)
                report["failed_steps"].append(name)
                errors.append(f"{name}: {type(exc).__name__}")

        def unconfigure() -> None:
            report["record_unconfigured"] = self._unconfigure_record(
                db, provider, {"cleared_on_host_change": True})

        def forget_ssm() -> None:
            residue = self.forget_api_key(db, provider, actor)
            report["ssm_parameter"] = residue.get("ssm_parameter")
            report["ssm_deleted"] = bool(residue.get("deleted"))
            if residue.get("error"):
                errors.append(f"ssm: {residue['error']}")

        def forget_identity() -> None:
            name = str(provider.credential_provider or "")
            if not name:
                return
            try:
                self._control_factory(self._hub_context()).delete_api_key_credential_provider(
                    name=name)
                report["identity_deleted"] = True
            except ClientError as exc:
                code = bedrock.error_code(exc)
                if code != "ResourceNotFoundException":
                    log.warning("model-credential %s: Identity provider %s left in place on "
                                "host change (%s)", provider.name, name, code)
                    errors.append(f"identity: {code}")
            except (BotoCoreError, AttributeError) as exc:  # a control plane without the call
                log.warning("model-credential %s: Identity provider %s left in place on host "
                            "change (%s)", provider.name, name, type(exc).__name__)
                errors.append(f"identity: {type(exc).__name__}")

        step("record", unconfigure)
        step("ssm", forget_ssm)
        step("identity", forget_identity)
        report["error"] = "; ".join(errors) or None
        log.info("model-credential %s: stored key cleared by %s on host change (record %d, "
                 "SSM %s, Identity %s%s)", provider.name, actor, report["record_unconfigured"],
                 "deleted" if report["ssm_deleted"] else "kept",
                 "deleted" if report["identity_deleted"] else "kept",
                 f", errors: {report['error']}" if report["error"] else "")
        if report["failed_steps"]:
            raise model_registry.CredentialClearError(report)
        return report

    @staticmethod
    def _record_on_workspaces(db: Session, hub: WorkspaceContext, provider_name: str,
                              record: Mapping[str, Any]) -> int:
        """`resources.model_credentials[<name>]` on the HUB row (the Identity call targeted
        the hub's account/region — the full record, mirrored into `config/launchpad.yaml` by
        `merge_workspace_resources`) and on every spoke row that is ALREADY `configured` for
        the provider (its own bootstrap registered the provider in its account: it keeps its
        own `name` / `arn` / `secret_arn` and only learns `configured`, `key_version`,
        `source`). A spoke without the credential is left alone — `schemas.agent.
        provider_configured` reads `configured` off the WORKSPACE map to decide availability,
        and stamping the hub's ARN there would let a deploy succeed whose runtime
        `GetResourceApiKey` fails (D21-obs3). Returns the number of rows written."""
        rows = db.scalars(select(Workspace).order_by(Workspace.created_at, Workspace.id)).all()
        count = 0
        for row in rows:
            existing = (row.resources or {}).get("model_credentials")
            existing = dict(existing) if isinstance(existing, Mapping) else {}
            current = existing.get(provider_name)
            current = current if isinstance(current, Mapping) else {}
            if row.id == hub.id:
                entry: dict[str, Any] = dict(record)
            elif current.get("configured"):
                entry = {**current, "configured": True}
                for key in ("key_version", "source"):
                    if key in record:
                        entry[key] = record[key]
            else:
                continue
            merge_workspace_resources(workspace_context(row),
                                      {"model_credentials": {**existing, provider_name: entry}})
            count += 1
        db.expire_all()
        return count
