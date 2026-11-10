"""Model API keys → AgentCore Identity API-key credential providers (spec C4).

One provider per row of ``schemas.agent.providers()``. Each key enters the platform exactly
once, as the environment variable the table names for it (``api_key_env``:
``LAUNCHPAD_GLOBAL_BEDROCK_API_KEY`` for Global Bedrock, ``LAUNCHPAD_<NAME>_API_KEY`` for an
admin-registered provider such as SiliconFlow — on an EC2 host filled from SSM SecureString
parameters by ``infra/ec2/bootstrap-host.sh``) of the bootstrap process. It is handed to
``CreateApiKeyCredentialProvider`` (first run) or ``UpdateApiKeyCredentialProvider`` (a
later run whose key changed — rotation in place, no agent redeploy; an SSM-backed key whose
parameter version is unchanged is left alone, D21-obs5) and is never written to ``launchpad.yaml``,
the ledger or a log line; only the provider's name, ARN and the ARN of its backing Secrets
Manager secret are recorded. The Bedrock key is required; an optional provider whose
variable is absent is skipped with a "not configured" line and its models simply cannot be
reached until the operator stores the parameter. Generated agents and workers fetch a key
at run time with ``GetResourceApiKey(<credential_provider>)``
(docs/cn/interfaces/model-credential.md, docs/cn/interfaces/model-providers.md).
"""

import logging
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from botocore.exceptions import BotoCoreError, ClientError

from app.schemas.agent import DEFAULT_MODEL_CREDENTIAL, provider_row, providers

if TYPE_CHECKING:  # the registry imports nothing from here; kept lazy for import weight
    from sqlalchemy.orm import Session

    from app.models.model_registry import ModelProvider

log = logging.getLogger("launchpad.model_credential")

MODEL_CREDENTIAL_ENV = "LAUNCHPAD_GLOBAL_BEDROCK_API_KEY"
MODEL_CREDENTIAL_NAME = DEFAULT_MODEL_CREDENTIAL
_NOT_FOUND_CODES = frozenset({"ResourceNotFoundException"})
# The SSM names the registry may read (D21 plan §4) — the EC2 ops role's grant is exactly
# this pattern (`infra/ec2/launchpad-cn-ec2.yaml`, Sid `ReadLaunchpadSecretParameters`).
SSM_PARAMETER_RE = re.compile(r"^launchpad-[a-z0-9-]+-api-key$")
CREDENTIAL_SOURCE_SSM = "ssm"
CREDENTIAL_SOURCE_HOST_ENV = "host_env"


class MissingModelCredentialError(RuntimeError):
    """The bootstrap shell did not export a REQUIRED provider's API key."""

    def __init__(self, provider: str = "bedrock") -> None:
        row = provider_row(provider)
        super().__init__(
            f"{row['api_key_env']} is not set — export the {row['label']} API key in the "
            "shell that runs the bootstrap (it is read from the environment only, never from "
            "launchpad.yaml); every run re-reads it so rotation is explicit"
        )


def read_provider_api_key(provider: str, environ: Mapping[str, str] | None = None
                          ) -> str | None:
    """The key for ``provider`` from its ``api_key_env``; ``None`` when an OPTIONAL provider
    is not configured, ``MissingModelCredentialError`` when a required one is."""
    row = provider_row(provider)
    source = os.environ if environ is None else environ
    key = (source.get(row["api_key_env"]) or "").strip()
    if key:
        return key
    if row.get("required"):
        raise MissingModelCredentialError(provider)
    return None


def read_global_bedrock_api_key(environ: Mapping[str, str] | None = None) -> str:
    key = read_provider_api_key("bedrock", environ)
    assert key is not None  # required provider: read_provider_api_key raised otherwise
    return key


def _error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def ensure_api_key_credential_provider(
    control: Any,
    name: str,
    api_key: str,
    log: Callable[[str], None] = lambda _m: None,
    *,
    source: str | None = None,
    rotate: bool = True,
    key_version: int | None = None,
) -> dict[str, Any]:
    """Create-or-rotate the API-key credential provider ``name``.

    Returns ``{"arn", "secret_arn", "name", "created", "rotated"}``. Idempotent: an
    existing provider is updated in place, which rotates the key for every agent without
    redeploying. The key itself is never logged. ``source`` names where the key came from
    for the rotation line (default: the provider's environment variable); D21 passes the
    SSM parameter name.

    ``rotate=False`` (D21-obs5) tells the function the caller has already established that
    the stored key is the one Identity holds (same SSM parameter version); an existing
    provider is then left untouched — no ``UpdateApiKeyCredentialProvider`` call, one
    ``up to date`` line — and the result says ``rotated: False``. A missing provider is
    still created regardless of ``rotate``.
    """
    try:
        existing = control.get_api_key_credential_provider(name=name)
    except ClientError as exc:
        if _error_code(exc) not in _NOT_FOUND_CODES:
            raise
        existing = None
    if existing is None:
        created = control.create_api_key_credential_provider(name=name, apiKey=api_key)
        log(f"api-key credential provider {name} created")
        return {
            "arn": created["credentialProviderArn"],
            "secret_arn": _secret_arn(created),
            "name": name,
            "created": True,
            "rotated": False,
        }
    result = {
        "arn": existing["credentialProviderArn"],
        "secret_arn": _secret_arn(existing),
        "name": name,
        "created": False,
        "rotated": bool(rotate),
    }
    if not rotate:
        suffix = f" (key version {key_version})" if key_version is not None else ""
        log(f"api-key credential provider {name} up to date{suffix}")
        return result
    control.update_api_key_credential_provider(name=name, apiKey=api_key)
    log(f"api-key credential provider {name} reused · key rotated from "
        f"{source or _api_key_env_for(name)}")
    return result


def _api_key_env_for(credential_provider: str) -> str:
    for row in providers().values():
        if row["credential_provider"] == credential_provider:
            return str(row["api_key_env"])
    return "the environment"


def _secret_arn(provider: dict) -> str:
    """`apiKeySecretArn` may be a string (Global) or ``{"secretArn": …}`` (cn preview)."""
    value = provider.get("apiKeySecretArn", "")
    if isinstance(value, dict):
        value = value.get("secretArn") or value.get("arn") or ""
    return value if isinstance(value, str) else ""


def ensure_model_credential(
    control: Any,
    name: str = MODEL_CREDENTIAL_NAME,
    log: Callable[[str], None] = lambda _m: None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The Global Bedrock provider alone: env var → provider (kept for callers/tests that
    only know the Bedrock key; `ensure_provider_credentials` is the bootstrap entry point)."""
    return ensure_api_key_credential_provider(
        control, name, read_global_bedrock_api_key(environ), log
    )


def ensure_provider_credentials(
    control: Any,
    log: Callable[[str], None] = lambda _m: None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """The bootstrap entry point (hub and workspace alike): one Identity API-key provider
    per configured row of ``providers()``. Returns ``{provider: result}``; an optional provider
    without its key is reported as ``{"configured": False, "name": …}`` after a readable log
    line naming the variable and the SSM parameter the operator must create. The Bedrock
    provider is required — its absence raises before Identity is touched."""
    rows = providers()
    keys = {provider: read_provider_api_key(provider, environ) for provider in rows}
    results: dict[str, dict[str, Any]] = {}
    for provider, row in rows.items():
        key = keys[provider]
        if key is None:
            log(f"model-credential {row['credential_provider']} not configured — "
                f"{row['api_key_env']} is unset (store the key as SSM SecureString "
                f"{row['ssm_parameter']} / export it, then re-run bootstrap); "
                f"{row['label']} models stay unavailable")
            results[provider] = {"configured": False, "name": row["credential_provider"],
                                 "created": False}
            continue
        result = ensure_api_key_credential_provider(control, row["credential_provider"], key,
                                                    log)
        results[provider] = {**result, "configured": True}
    return results


def model_credential_resources(result: Mapping[str, Any]) -> dict[str, str]:
    """The resource-map entries both bootstraps record (no secret material)."""
    return {
        "model_credential_arn": str(result["arn"]),
        "model_credential_name": str(result["name"]),
        "model_credential_secret_arn": str(result.get("secret_arn") or ""),
    }


def provider_credential_resources(results: Mapping[str, Mapping[str, Any]]
                                  ) -> dict[str, Any]:
    """Resource-map entries for every provider: the historical Bedrock keys plus
    ``model_credentials`` (``{provider: {name, arn, secret_arn, configured}}``) — no secret
    material anywhere."""
    out: dict[str, Any] = {}
    if results.get("bedrock", {}).get("configured"):
        out.update(model_credential_resources(results["bedrock"]))
    out["model_credentials"] = {
        provider: credential_record(result) for provider, result in results.items()
    }
    return out


def credential_record(result: Mapping[str, Any], *, key_version: int | None = None,
                      source: str | None = None) -> dict[str, Any]:
    """One ``resources.model_credentials[<provider>]`` entry: names + configured-ness; D21
    adds the SSM parameter ``version`` and the ``source`` the key was registered from
    (``ssm`` | ``host_env``) when known — `aws_clients.provider_api_key` reads SSM only for
    a record that says ``ssm``. No secret material."""
    record: dict[str, Any] = {
        "name": str(result["name"]),
        "arn": str(result.get("arn") or ""),
        "secret_arn": str(result.get("secret_arn") or ""),
        "configured": bool(result.get("configured")),
    }
    version = key_version if key_version is not None else result.get("key_version")
    if version is not None:
        record["key_version"] = int(version)
    origin = source or result.get("source")
    if origin:
        record["source"] = str(origin)
    return record


def credential_summary(results: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """``{provider: {name, created, configured}}`` for the bootstrap summary."""
    return {
        provider: {"name": str(r["name"]), "created": bool(r.get("created")),
                   "configured": bool(r.get("configured"))}
        for provider, r in results.items()
    }


# ── D21 (M1-b): registry-driven credentials — the key comes from an SSM parameter ──────
@dataclass(frozen=True, repr=False)
class SsmSecret:
    """One decrypted `launchpad-*-api-key` parameter. ``repr`` / ``str`` show the version
    and date only, so a stray f-string or traceback cannot leak the value."""

    value: str
    version: int | None
    modified_at: datetime | None

    def __repr__(self) -> str:
        return f"SsmSecret(version={self.version}, modified_at={self.modified_at})"

    __str__ = __repr__


class ParameterMissingError(RuntimeError):
    """The named SSM parameter does not exist (or is empty)."""

    def __init__(self, name: str, *, empty: bool = False) -> None:
        self.parameter = name
        self.empty = empty
        super().__init__(
            f"SSM parameter {name} {'is empty' if empty else 'does not exist'} — create it as "
            "a SecureString (aws ssm put-parameter --type SecureString; deploy.md §1), then "
            "register it again"
        )


def _error_code_of(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code") or "ClientError")


def read_ssm_parameter(ssm: Any, name: str) -> SsmSecret:
    """`GetParameter(WithDecryption=True)` for one `launchpad-*-api-key` name.

    The name is re-checked against the pattern (defence in depth: the registry validated it
    at write time, the IAM grant is the same pattern). `ParameterMissingError` for an
    absent / empty parameter; other `ClientError`s (AccessDenied → the ops role lacks the
    grant) propagate for the caller to classify. The returned value is the caller's to
    hand over and drop — this function never logs it.
    """
    if not SSM_PARAMETER_RE.fullmatch(name or ""):
        raise ValueError(f"{name!r} does not match launchpad-*-api-key")
    try:
        response = ssm.get_parameter(Name=name, WithDecryption=True)
    except ClientError as exc:
        if _error_code_of(exc) == "ParameterNotFound":
            raise ParameterMissingError(name) from exc
        raise
    parameter = response.get("Parameter") if isinstance(response, Mapping) else None
    parameter = parameter if isinstance(parameter, Mapping) else {}
    value = str(parameter.get("Value") or "").strip()
    if not value:
        raise ParameterMissingError(name, empty=True)
    raw_version = parameter.get("Version")
    version = int(raw_version) if isinstance(raw_version, int | str) and str(
        raw_version).isdigit() else None
    modified = parameter.get("LastModifiedDate")
    if isinstance(modified, datetime):
        modified = modified if modified.tzinfo else modified.replace(tzinfo=UTC)
    else:
        modified = None
    return SsmSecret(value=value, version=version, modified_at=modified)


def register_from_registry(control: Any, provider: "ModelProvider", api_key: str,
                           log: Callable[[str], None] = lambda _m: None, *,
                           source: str | None = None, rotate: bool = True,
                           key_version: int | None = None) -> dict[str, Any]:
    """Plan §4: the registry row's Identity API-key provider (`launchpad-<name>`), created
    or rotated in place with ``api_key``. Returns the `ensure_api_key_credential_provider`
    result plus ``configured: True``; the caller drops ``api_key`` afterwards.
    ``rotate=False`` (the stored key version equals the SSM parameter's — D21-obs5) leaves
    an existing provider untouched."""
    result = ensure_api_key_credential_provider(
        control, str(provider.credential_provider), api_key, log, source=source,
        rotate=rotate, key_version=key_version)
    return {**result, "configured": True}


def key_is_current(secret: "SsmSecret", *, version: int | None,
                   modified_at: datetime | None) -> bool:
    """D21-obs5 — the idempotency rule: the key Identity holds is the one the parameter
    holds when the stored ``key_version`` equals the parameter's ``Version`` and, when both
    sides know it, the stored ``key_modified_at`` equals its ``LastModifiedDate``. Without a
    stored version (first registration, a `host_env` key, a pre-D21 record) nothing can be
    compared and the key is treated as changed → rotate."""
    if version is None or secret.version is None or int(version) != int(secret.version):
        return False
    if modified_at is None or secret.modified_at is None:
        return True
    return _iso(modified_at) == _iso(secret.modified_at)


def _iso(value: datetime | None) -> str | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def record_registered_key(db: "Session", provider: "ModelProvider", *,
                          version: int | None, modified_at: datetime | None, created: bool,
                          actor: str, rotated: bool | None = None) -> bool:
    """Persist what the admin may see about a registered key — the SSM parameter's
    ``Version`` and ``LastModifiedDate`` (never its value) — and journal the registration
    as `provider.credential {configured, created, rotated, key_version, key_modified_at}`.
    Commits and republishes the snapshot.

    ``rotated`` defaults to ``not created`` (the pre-D21-obs5 meaning). When the caller
    reports neither a creation nor a rotation — Identity was left untouched because the
    key is current — nothing is written and **no journal row is emitted** (a `provider.
    credential` row is audit, and there was nothing to audit). Returns whether a row was
    journaled."""
    from app.services import model_registry

    rotated = (not created) if rotated is None else bool(rotated)
    if not created and not rotated:
        return False
    before = {"configured": True, "key_version": provider.key_version,
              "key_modified_at": _iso(provider.key_modified_at)}
    provider.key_version = version
    provider.key_modified_at = modified_at
    provider.updated_by = actor
    db.flush()
    after = {"configured": True, "created": bool(created), "rotated": rotated,
             "key_version": version, "key_modified_at": _iso(modified_at)}
    model_registry._journal(db, "provider.credential", actor, provider.id, before, after,
                            scope=model_registry._journal_scope(provider))
    db.commit()
    model_registry.refresh(db)
    return True


def ensure_registry_credentials(
    control: Any,
    ssm: Any,
    log: Callable[[str], None] = lambda _m: None,
    *,
    db: "Session | None" = None,
    actor: str = "bootstrap",
    record_on_row: bool = True,
    known_versions: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """The registry half of the bootstrap credential stage (plan §4): every provider row
    whose key comes from SSM (`credential_source: ssm` — every admin-registered
    OpenAI-compatible provider, e.g. SiliconFlow; none is seeded since R13) is read from its
    parameter and created / rotated in Identity, exactly like `POST /{id}/credential` does from the
    console, so a parameter stored after the hosts are up is picked up by the next
    bootstrap without a reboot.

    Per provider, degrades with one readable line (naming the parameter, never a value):
    absent parameter → ``configured: False``; an SSM or Identity `ClientError` → the same
    plus ``error`` (code only). Returns ``{provider name: result}`` in the
    `ensure_provider_credentials` shape (+ ``key_version`` / ``source: "ssm"``).

    ``record_on_row`` — write ``key_version`` / ``key_modified_at`` + the `provider.credential`
    journal row onto the hub-global `model_providers` row. Only the **hub** pass may do that
    (the console's `/credential` and `services/bootstrap.py`); a spoke workspace's bootstrap
    passes ``False`` and keeps the version on its own resource map only — otherwise the hub
    row would show whichever workspace registered last (D21-obs4).

    **Idempotent (D21-obs5).** The parameter is read on every run (its ``Version`` is the
    only way to know whether the key changed), but Identity is updated and the journal
    written only when the stored version differs or the provider is missing. The stored
    version is the hub row's ``key_version`` / ``key_modified_at`` on the hub pass, or
    ``known_versions[<name>]["key_version"]`` — the caller's existing
    ``resources.model_credentials`` map — on a spoke pass. An unchanged key logs
    ``credential provider <name> up to date (key version N)`` and returns ``rotated:
    False`` with no journal row, so a bootstrap without admin edits leaves the catalog
    version where it was."""
    from sqlalchemy import select

    from app.core.db import SessionLocal
    from app.models.model_registry import ModelProvider
    from app.services import model_registry

    own = db is None
    db = db or SessionLocal()
    results: dict[str, dict[str, Any]] = {}
    try:
        model_registry.ensure_seeded(db)
        rows = list(db.scalars(select(ModelProvider).order_by(
            ModelProvider.builtin.desc(), ModelProvider.created_at, ModelProvider.id)))
        for row in rows:
            name = str(row.credential_ssm_parameter or "")
            if row.credential_source != CREDENTIAL_SOURCE_SSM or not name:
                continue
            credential = str(row.credential_provider)
            try:
                secret = read_ssm_parameter(ssm, name)
            except ParameterMissingError as exc:
                log(f"model-credential {credential} not configured — SSM parameter {name} "
                    f"{'is empty' if exc.empty else 'does not exist'} (store the "
                    f"{row.label} API key as SecureString {name}, then re-run bootstrap or "
                    "click Register credential under Settings → Model Providers); "
                    f"{row.label} models stay unavailable")
                results[row.name] = {"configured": False, "name": credential,
                                     "created": False}
                continue
            except ClientError as exc:
                code = _error_code_of(exc)
                log(f"model-credential {credential} skipped — SSM parameter {name} could not "
                    f"be read ({code}; the ops role needs ssm:GetParameter on "
                    "launchpad-*-api-key, deploy.md §8.6)")
                results[row.name] = {"configured": False, "name": credential,
                                     "created": False, "error": code}
                continue
            except (BotoCoreError, ValueError) as exc:
                log(f"model-credential {credential} skipped — SSM parameter {name} could not "
                    f"be read ({type(exc).__name__})")
                results[row.name] = {"configured": False, "name": credential,
                                     "created": False, "error": type(exc).__name__}
                continue
            if record_on_row:
                current = key_is_current(secret, version=row.key_version,
                                         modified_at=row.key_modified_at)
            else:
                known = (known_versions or {}).get(row.name)
                known = known if isinstance(known, Mapping) else {}
                current = bool(known.get("configured")) and key_is_current(
                    secret, version=known.get("key_version"), modified_at=None)
            try:
                result = register_from_registry(
                    control, row, secret.value, log,
                    source=f"SSM parameter {name} (version {secret.version})",
                    rotate=not current, key_version=secret.version)
            except ClientError as exc:
                code = _error_code_of(exc)
                log(f"model-credential {credential} failed — Identity refused the "
                    f"registration ({code})")
                results[row.name] = {"configured": False, "name": credential,
                                     "created": False, "error": code}
                continue
            if record_on_row:
                record_registered_key(db, row, version=secret.version,
                                      modified_at=secret.modified_at,
                                      created=bool(result["created"]),
                                      rotated=bool(result["rotated"]), actor=actor)
            results[row.name] = {**result, "key_version": secret.version,
                                 "source": CREDENTIAL_SOURCE_SSM}
            del secret
        return results
    finally:
        if own:
            db.close()
