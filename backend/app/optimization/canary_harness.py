"""Harness target-canary building blocks (pure, injected-client style).

A managed Harness cannot sit behind an ``http.agentcoreRuntime`` Gateway target
(the target validates a ``runtime/`` ARN), so a Harness canary fronts each version
with an HTTP **passthrough** target that SigV4-signs a call to InvokeHarness on a
named Harness endpoint. Verified live 2026-09-30 (us-west-2):

- the passthrough endpoint is ``https://bedrock-agentcore.<region>.amazonaws.com/
  harnesses/invoke`` with ``harnessArn`` + ``qualifier`` as static query
  parameters; a client POSTs the InvokeHarness JSON body to ``<gatewayUrl>/<target>/``
  (``/<target>/invocations`` is a 404 UnknownOperation; see ``TARGET_PATH_SUFFIX``);
- the gateway role is authorised as ``InvokeAgentRuntime`` on the harness ARN;
- a named endpoint's spans carry ``service.name = harness_<harnessName>.<endpoint>``
  and land in ``/aws/bedrock-agentcore/runtimes/<backingRuntimeId>-<endpoint>``;
- the session header survives the hop and the Harness spans carry the gateway's
  ``routing_experiment_variant_name`` (C / T1), so the A/B test attributes sessions.

Topology mirrors the Runtime canary (Model 1): one Harness, two immutable versions,
``control`` endpoint → the chosen earlier version, ``treatment`` endpoint → the
latest version (DEFAULT already serves it).
"""

import time
import uuid
from collections.abc import Callable
from typing import Any

from app.services.agentcore import harness as hc

Log = Callable[[str], None]
_sleep = time.sleep  # injectable
# POST to ``<gatewayUrl>/<target>/`` — the trailing slash is load-bearing: the A/B
# test's gatewayFilter ``/<target>/*`` only intercepts that form. A bare
# ``/<target>`` still reaches the Harness (HTTP 200) but bypasses the A/B test, so
# every session lands on control unattributed and the verdict never gets a sample
# (measured live 2026-09-30).
TARGET_PATH_SUFFIX = "/"
STICKY_HEADER = "$context.header.X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
STICKY_TIMEOUT_S = 3600
TRACES_SOURCE_SUFFIX = "-traces-source"
TRACES_DEST_SUFFIX = "-traces-destination"
# UpdateHarness fields a rollback re-publishes from the control version's config.
# Role, environment, authorizer and environment variables are left out (omit =
# keep): they describe where the Harness runs, not the behaviour under test.
_BEHAVIOUR_FIELDS = (
    "model", "systemPrompt", "tools", "skills", "allowedTools", "truncation",
    "maxIterations", "maxTokens", "timeoutSeconds",
)


def _noop(_msg: str) -> None:
    pass


# ─── naming ─────────────────────────────────────────────────────────────────
def control_endpoint(canary_id: str) -> str:
    """Harness endpoint names match ``[a-zA-Z][a-zA-Z0-9_]{0,47}``."""
    return f"ctl{canary_id[:6]}"


def treatment_endpoint(canary_id: str) -> str:
    return f"trt{canary_id[:6]}"


def endpoint_log_group(backing_runtime_id: str, endpoint: str) -> str:
    return f"/aws/bedrock-agentcore/runtimes/{backing_runtime_id}-{endpoint}"


def endpoint_service_name(harness_name: str, endpoint: str) -> str:
    return f"harness_{harness_name}.{endpoint}"


def backing_runtime_id(harness: dict[str, Any]) -> str:
    env = (harness.get("environment") or {}).get("agentCoreRuntimeEnvironment") or {}
    runtime_id = env.get("agentRuntimeId")
    if not runtime_id:
        raise RuntimeError("harness has no backing AgentCore Runtime environment")
    return str(runtime_id)


def invoke_body(prompt: str, *, actor_id: str | None = None,
                tools: list[dict[str, Any]] | None = None,
                allowed_tools: list[str] | None = None) -> dict[str, Any]:
    """The InvokeHarness JSON body the passthrough target relays verbatim."""
    body: dict[str, Any] = {"messages": [{"role": "user", "content": [{"text": prompt}]}]}
    if actor_id:
        body["actorId"] = actor_id
    if tools is not None:
        body["tools"] = tools
    if allowed_tools is not None:
        body["allowedTools"] = allowed_tools
    return body


# ─── versions ───────────────────────────────────────────────────────────────
def version_numbers(control: Any, harness_id: str) -> list[str]:
    """Every version of the Harness, ascending numerically."""
    rows = hc.list_harness_versions(control, harness_id)
    versions = {str(row.get("harnessVersion")) for row in rows if row.get("harnessVersion")}
    return sorted(versions, key=lambda v: (int(v) if v.isdigit() else 0, v))


def rollback_params(version_config: dict[str, Any], harness_id: str) -> dict[str, Any]:
    """UpdateHarness kwargs that re-publish one version's behaviour as a NEW version.

    ``memory`` must sit in ``{"optionalValue": …}`` (and an absent config means
    disabled); ``tools``/``skills`` are sent even when empty so the rollback detaches
    what the rejected version added (omit = keep)."""
    params: dict[str, Any] = {"harnessId": harness_id, "clientToken": str(uuid.uuid4())}
    for field in _BEHAVIOUR_FIELDS:
        value = version_config.get(field)
        if value is not None:
            params[field] = value
    params["memory"] = {"optionalValue": version_config.get("memory") or {"disabled": {}}}
    params.setdefault("tools", [])
    params.setdefault("skills", [])
    return params


# ─── gateway pieces ─────────────────────────────────────────────────────────
def create_passthrough_target(
    control: Any,
    *,
    gateway_id: str,
    name: str,
    harness_arn: str,
    qualifier: str,
    region: str,
    log: Log = _noop,
) -> str:
    """Create (or adopt) a passthrough target fronting one Harness endpoint; READY."""
    log(f"creating gateway target {name} → harness endpoint {qualifier}…")
    try:
        target = control.create_gateway_target(
            gatewayIdentifier=gateway_id,
            name=name,
            targetConfiguration={
                "http": {
                    "passthrough": {
                        "endpoint": f"https://bedrock-agentcore.{region}.amazonaws.com"
                                    "/harnesses/invoke",
                        "protocolType": "CUSTOM",
                        "staticQueryParameters": {
                            "harnessArn": harness_arn, "qualifier": qualifier,
                        },
                        "stickinessConfiguration": {
                            "identifier": STICKY_HEADER, "timeout": STICKY_TIMEOUT_S,
                        },
                    }
                }
            },
            credentialProviderConfigurations=[{
                "credentialProviderType": "GATEWAY_IAM_ROLE",
                "credentialProvider": {
                    "iamCredentialProvider": {"service": "bedrock-agentcore", "region": region}
                },
            }],
            clientToken=str(uuid.uuid4()),
        )
        target_id = target["targetId"]
    except Exception as exc:
        if type(exc).__name__ != "ConflictException":
            raise
        items = control.list_gateway_targets(gatewayIdentifier=gateway_id).get("items", [])
        target_id = next(t["targetId"] for t in items if t.get("name") == name)
    for _ in range(60):
        detail = control.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
        status = detail.get("status")
        if status == "READY":
            return target_id
        if status in {"FAILED", "UPDATE_UNSUCCESSFUL"}:
            raise RuntimeError(f"gateway target {name} {status}: {detail.get('statusReasons')}")
        _sleep(5)
    raise TimeoutError(f"gateway target {name} not READY")


def trace_delivery_names(gateway_id: str) -> tuple[str, str]:
    return f"{gateway_id}{TRACES_SOURCE_SUFFIX}", f"{gateway_id}{TRACES_DEST_SUFFIX}"


def enable_gateway_tracing(logs: Any, *, gateway_id: str, gateway_arn: str,
                           log: Log = _noop) -> dict[str, str]:
    """Gateway spans → aws/spans (source TRACES → XRAY destination → delivery).

    The A/B test attributes a session to a variant from the gateway's routing
    attributes; AWS documents trace delivery as required for non-runtime targets."""
    source, dest = trace_delivery_names(gateway_id)
    log("enabling gateway trace delivery…")
    try:
        logs.put_delivery_source(name=source, logType="TRACES", resourceArn=gateway_arn)
    except Exception as exc:
        if type(exc).__name__ != "ConflictException":
            raise
    try:
        dest_arn = logs.put_delivery_destination(
            name=dest, deliveryDestinationType="XRAY"
        )["deliveryDestination"]["arn"]
    except Exception as exc:
        if type(exc).__name__ != "ConflictException":
            raise
        dest_arn = logs.get_delivery_destination(name=dest)["deliveryDestination"]["arn"]
    try:
        logs.create_delivery(deliverySourceName=source, deliveryDestinationArn=dest_arn)
    except Exception as exc:
        if type(exc).__name__ != "ConflictException":
            raise
    return {"source": source, "destination": dest}


def disable_gateway_tracing(logs: Any, gateway_id: str) -> list[dict[str, str]]:
    """Delete the delivery, source and destination; each failure is reported."""
    source, dest = trace_delivery_names(gateway_id)
    results: list[dict[str, str]] = []

    def attempt(category: str, fn: Callable[[], Any]) -> None:
        try:
            fn()
            results.append({"category": category, "status": "deleted", "detail": ""})
        except Exception as exc:
            if type(exc).__name__ == "ResourceNotFoundException":
                results.append({"category": category, "status": "absent", "detail": ""})
            else:
                results.append({"category": category, "status": "skipped",
                                "detail": f"{type(exc).__name__}: {exc}"})

    def delete_deliveries() -> None:
        # DescribeDeliveries pages account-wide (measured live: the canary's
        # delivery was not on the first page), so walk every page
        kwargs: dict[str, Any] = {}
        while True:
            page = logs.describe_deliveries(**kwargs)
            for delivery in page.get("deliveries", []):
                if delivery.get("deliverySourceName") == source:
                    logs.delete_delivery(id=delivery["id"])
            token = page.get("nextToken")
            if not token:
                return
            kwargs = {"nextToken": token}

    attempt(f"trace-delivery:{source}", delete_deliveries)
    attempt(f"trace-source:{source}", lambda: logs.delete_delivery_source(name=source))
    attempt(f"trace-destination:{dest}", lambda: logs.delete_delivery_destination(name=dest))
    return results


def ensure_log_group(logs: Any, name: str) -> None:
    """CreateOnlineEvaluationConfig refuses a log group that does not exist yet, and
    a fresh Harness endpoint only creates its group on the first invocation."""
    try:
        logs.create_log_group(logGroupName=name)
    except Exception as exc:
        if type(exc).__name__ != "ResourceAlreadyExistsException":
            raise


# ─── paired replay ───────────────────────────────────────────────────────────
# A Harness canary replays a dataset **paired**: every prompt goes to BOTH endpoints
# (InvokeHarness with ``qualifier``, two fresh sessions), and the verdict compares
# the two versions question by question. Gateway A/B assignment is random per
# session, so a replayed set split 50/50 hands each version a different mix of
# questions, and repeating the set only re-samples the same questions; pairing
# removes the mix and makes each question one unit of evidence. The per-endpoint
# online evaluations score both sessions (100 % sampling on the endpoint's own
# service name — no variant filter), and the verdict reads those scores back by
# session id.
PAIRED_MODE = "paired"
PAIRED_ALPHA = 0.05
# exact sign-flip enumeration up to 2**16 sign patterns, seeded Monte Carlo beyond
_EXACT_MAX = 16
_MONTE_CARLO_SAMPLES = 20000
# Logs Insights caps a query at 10 000 characters: session ids per query
SCORE_QUERY_CHUNK = 60


def paired_sign_flip_p(diffs: list[float]) -> float:
    """Two-sided paired sign-flip (randomisation) test on the mean difference.

    Under H0 each pair's difference is equally likely to have either sign; the
    p-value is the share of sign patterns whose |sum| is at least the observed
    |sum|. Zero differences carry no sign and are left out. Exact for up to
    ``_EXACT_MAX`` non-zero pairs, a seeded Monte Carlo estimate beyond (stable
    across calls)."""
    import random

    nonzero = [d for d in diffs if d != 0]
    if not nonzero:
        return 1.0
    observed = abs(sum(nonzero)) - 1e-12
    k = len(nonzero)
    if k <= _EXACT_MAX:
        hits = 0
        for mask in range(1 << k):
            total = sum(-d if mask >> i & 1 else d for i, d in enumerate(nonzero))
            if abs(total) >= observed:
                hits += 1
        return hits / (1 << k)
    rng = random.Random(7)
    hits = sum(
        1 for _ in range(_MONTE_CARLO_SAMPLES)
        if abs(sum(d if rng.random() < 0.5 else -d for d in nonzero)) >= observed
    )
    return (hits + 1) / (_MONTE_CARLO_SAMPLES + 1)


def paired_scores_query(config_ids: list[str], session_ids: list[str]) -> str:
    """Per-session mean score per evaluator from the two arms' online-eval results.

    The evaluator ARN rides along so :func:`parse_paired_scores` can key a custom
    judge by its id (the ARN tail) — its ``gen_ai.evaluation.name`` is the judge's
    NAME, which need not equal the id the canary was configured with."""
    from app.evaluation.agentcore_eval import ONLINE_EVAL_RESULTS_PREFIX

    configs = ", ".join(f'"{c}"' for c in config_ids)
    sessions = ", ".join(f'"{s}"' for s in session_ids)
    return (
        f"SOURCE logGroups(namePrefix: ['{ONLINE_EVAL_RESULTS_PREFIX}'])\n"
        "| fields attributes.session.id as sid, attributes.gen_ai.evaluation.name as evaluator,"
        " attributes.aws.bedrock_agentcore.evaluator.arn as arn,"
        " attributes.gen_ai.evaluation.score.value as score\n"
        f'| filter name = "gen_ai.evaluation.result" and onlineEvaluationConfigId in [{configs}]'
        f" and sid in [{sessions}] and ispresent(score)\n"
        # the aggregate needs its own name: reusing `score` is a MalformedQueryException
        # ("Ephemeral field is already defined") on the live service
        "| stats avg(score) as mean by sid, evaluator, arn\n"
        "| limit 10000"
    )


def parse_paired_scores(rows: list[dict[str, str]]) -> dict[str, dict[str, float]]:
    """``{session_id: {evaluator id: mean score}}`` from :func:`paired_scores_query` rows.

    The evaluator is keyed by the tail of its ARN (``…:evaluator/<id>``) when the row
    carries one, else by its evaluation name — the same rule as
    ``agentcore_eval._record_evaluator_id``, so configured ids match custom judges."""
    out: dict[str, dict[str, float]] = {}
    for row in rows:
        sid = row.get("sid")
        arn = row.get("arn") or ""
        evaluator = arn.rsplit("/", 1)[-1] if "/" in arn else row.get("evaluator")
        try:
            score = float(row.get("mean") or "")
        except ValueError:
            continue
        if sid and evaluator:
            out.setdefault(sid, {})[evaluator] = score
    return out


def budget_stop_only(error: str | None) -> bool:
    """A pair error (``"<arm>: <code>, …"``) made only of the agent's own budget stops —
    a timeout after its replay or an iteration / token limit. That side answered as
    far as its budget allowed: the pair stays in the verdict and its session is scored
    as it stands, the same rule a dataset evaluation run applies
    (``evaluation.service.BUDGET_STOP_CODES``)."""
    from app.evaluation.service import BUDGET_STOP_CODES

    if not error:
        return False
    return all(part.split(": ", 1)[-1].strip() in BUDGET_STOP_CODES
               for part in error.split(", "))


def paired_metrics(
    pairs: list[dict[str, Any]], scores: dict[str, dict[str, float]],
    *, polarity: Callable[[str], int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Per-evaluator paired comparison in the A/B metric shape, plus per-question rows.

    Only pairs where BOTH sessions have a score for the evaluator count. Each
    metric keeps the A/B shape the verdict and the UI read (``control`` /
    ``variants[0]``) and adds the paired facts: ``pairs``, ``meanDiff`` (treatment
    − control), ``wins`` / ``losses`` / ``ties`` from the treatment's side
    (polarity-aware) and the sign-flip ``pValue``."""
    evaluators = sorted({e for p in pairs for side in ("control", "treatment")
                         for e in scores.get(p.get(f"{side}_session_id") or "", {})})
    metrics: list[dict[str, Any]] = []
    for evaluator in evaluators:
        sign = polarity(evaluator)
        both = []
        for p in pairs:
            c_scores = scores.get(p.get("control_session_id") or "", {})
            t_scores = scores.get(p.get("treatment_session_id") or "", {})
            if evaluator in c_scores and evaluator in t_scores:
                both.append((c_scores[evaluator], t_scores[evaluator]))
        if not both:
            continue
        n = len(both)
        diffs = [t - c for c, t in both]
        mean_c = sum(c for c, _ in both) / n
        mean_t = sum(t for _, t in both) / n
        oriented = [sign * d for d in diffs]
        p_value = paired_sign_flip_p(diffs)
        metrics.append({
            "evaluatorId": evaluator,
            "label": evaluator.rsplit("/", 1)[-1],
            "polarity": sign,
            "paired": True,
            "control": {"name": "C", "mean": mean_c, "sampleSize": n},
            "variants": [{
                "name": "T1", "mean": mean_t, "sampleSize": n,
                "pValue": round(p_value, 4),
                "percentChange": round((mean_t - mean_c) / mean_c * 100, 2) if mean_c else None,
                "isSignificant": p_value < PAIRED_ALPHA,
                "pairs": n,
                "meanDiff": round(mean_t - mean_c, 4),
                "wins": sum(1 for d in oriented if d > 0),
                "losses": sum(1 for d in oriented if d < 0),
                "ties": sum(1 for d in oriented if d == 0),
            }],
        })
    rows = [
        {
            "scenario_id": p.get("scenario_id"),
            "prompt": p.get("prompt"),
            "control": scores.get(p.get("control_session_id") or "", {}),
            "treatment": scores.get(p.get("treatment_session_id") or "", {}),
            "error": None if budget_stop_only(p.get("error")) else p.get("error"),
            "budget_stop": p.get("error") if budget_stop_only(p.get("error")) else None,
        }
        for p in pairs
    ]
    return metrics, rows
