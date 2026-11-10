"""LaunchpadMemoryStack — one EnvName environment's slice of the self-hosted Memory/KB
substrate (N6): the extraction queue (+ DLQ), the memory-extractor Lambda, the IAM grants
and the snapshot lifecycle on the base artifacts bucket. Since D17 the OpenSearch domain
itself is NOT here — it lives in the shared, unsuffixed `launchpad-memory-domain` stack
(`memory_domain_stack.py`) and this stack only references it: the domain/admin-role ARNs
are computed from their fixed names, the endpoint is resolved at deploy time from the SSM
parameter the domain stack publishes. The same `MemoryDomain*` / `MemoryAdminRoleArn`
outputs are re-exported here so the backend keeps reading one stack per environment.

Distribution extension: `memory_domain_mode=dedicated` selects an environment-owned
OpenSearch domain, admin role and discovery path. The D17 shared behavior described
below remains the default for existing configurations; see docs/cn/distribution.md.

Separate from launchpad-base so N9 can deploy base first; cross-stack references pin the
deletion order (memory before base). No CloudFormation export ties this stack to the domain
stack — `scripts/teardown.py` enforces that order instead (per-environment stacks first,
the domain only when no `launchpad-memory*` environment stack remains)."""

import math
from pathlib import Path

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as sources
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from stacks.base_stack import LaunchpadBaseStack, lambda_log_group
from stacks.memory_domain_stack import admin_role_name, regional_domain_name, ssm_parameter_name
from stacks.naming import env_name_from_context, memory_domain_env, suffixed
from stacks.principals import service_principal

LAMBDA_DIR = Path(__file__).resolve().parents[2] / "backend" / "lambdas" / "memory_extractor"

DEFAULT_EXPIRY_DAYS = 90
DEFAULT_EXTRACT_MODEL = "global.zai.glm-5.3"
DEFAULT_EMBED_MODEL = "amazon.titan-embed-text-v2:0"
MODEL_CREDENTIAL = "launchpad-global-bedrock"
# D21 (M1-d): the extractor may also read the key of any OTHER registry provider the hub
# names in a message's `embedding` descriptor (an OpenAI-compatible embedding row) — every
# Launchpad-managed Identity API-key provider is `launchpad-<provider>`, the same family the
# per-agent runtime roles are granted; never `*`.
MODEL_CREDENTIAL_FAMILY = "launchpad-*"
MODEL_REGION = "us-west-2"  # region-boundary: global-bedrock

# Fixed outer budget. Three initial model reads fit at the default ceiling
# (3 * (5 s connect + 120 s read) + 90 s finish reserve = 465 s).
# Token retries share the remaining time; they do not extend the invocation.
LAMBDA_TIMEOUT_SECONDS = 480
DEFAULT_EXTRACT_MODEL_READ_TIMEOUT_SECONDS = 120
# rev-r22 on N17-obs247: the extractor's SQS event source MaximumConcurrency (context knob
# `extract_max_concurrency`); the service minimum is 2
DEFAULT_EXTRACT_MAX_CONCURRENCY = 5
# AWS guidance: an SQS source queue's visibility timeout should be >= 6x the
# consumer Lambda's own timeout, so a retry/redrive never races a still-running
# invocation. Derived from LAMBDA_TIMEOUT_SECONDS so the two cannot drift apart.
QUEUE_VISIBILITY_TIMEOUT_SECONDS = LAMBDA_TIMEOUT_SECONDS * 6


class LaunchpadMemoryStack(Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *, base: LaunchpadBaseStack, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        def ctx(key, default):
            return self.node.try_get_context(key) or default

        # `-c env_name=<x>` (stacks/naming.py): same rule as the base stack — every
        # fixed physical name below gets `-<x>` appended; empty leaves it unchanged.
        self.env_name = env_name_from_context(self.node)
        domain_env = memory_domain_env(self.node)

        def name(base: str) -> str:
            return suffixed(base, self.env_name)

        # The hub exec role's SQS statement below builds this queue's ARN by hand (no
        # cross-stack token), so the name is computed once and used in both places.
        queue_name = name(f"launchpad-memory-extract-{self.region}")

        # ---- the shared domain (D17): fixed names → ARNs; endpoint from SSM ----
        # `lp-memory-<region>` and `launchpad-memory-admin-<region>` are deterministic
        # (memory_domain_stack.py), so — like the queue ARN below — their ARNs are built
        # by hand rather than imported: no export couples this stack to the domain stack.
        # Only the endpoint hostname is unknowable here; it is read at deploy time from
        # the SSM parameter the domain stack publishes (an SSM-typed CloudFormation
        # parameter), which is also why app.py makes this stack depend on that one.
        domain_arn = (
            f"arn:{self.partition}:es:{self.region}:{self.account}:"
            f"domain/{regional_domain_name(self.region, domain_env)}"
        )
        admin_role_arn = (
            f"arn:{self.partition}:iam::{self.account}:role/"
            f"{admin_role_name(self.region, domain_env)}"
        )
        domain_endpoint = ssm.StringParameter.value_for_string_parameter(
            self, ssm_parameter_name(self.region, "endpoint", domain_env)
        )

        # ---- memory-extractor Lambda role ----
        extractor_role = iam.Role(
            self,
            "MemoryExtractorRole",
            role_name=name(f"launchpad-memory-extractor-{self.region}"),
            assumed_by=service_principal("lambda"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )

        # ---- extraction queue + DLQ ----
        dlq = sqs.Queue(
            self,
            "MemoryDeadLetterQueue",
            queue_name=name(f"launchpad-memory-extract-dlq-{self.region}"),
            retention_period=Duration.days(14),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
        )
        queue = sqs.Queue(
            self,
            "MemoryQueue",
            queue_name=queue_name,
            visibility_timeout=Duration.seconds(QUEUE_VISIBILITY_TIMEOUT_SECONDS),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=dlq),
        )

        # ---- least-privilege grants: hub exec role (S3 + SQS send only) ----
        # The exec role's policy is attached in its *home* stack (launchpad-base).
        # `queue.grant_send_messages(base.exec_role)` would embed a token for this
        # stack's queue ARN into that base-stack policy, making launchpad-base
        # depend on launchpad-memory — the reverse of the intended direction
        # (R21: memory must depend on base, so memory deletes first) and a
        # synth-time cycle once this stack's own resources need to reference
        # base's. The queue name is a deterministic, context-free string, so the
        # ARN is built by hand instead — no cross-stack token, no cycle.
        base.artifacts.grant_read_write(base.exec_role, "memory/*")
        base.artifacts.grant_read(base.exec_role, "kb/*")
        base.exec_role.add_to_policy(
            iam.PolicyStatement(
                sid="MemoryExtractionQueueSend",
                actions=["sqs:SendMessage"],
                resources=[f"arn:{self.partition}:sqs:{self.region}:{self.account}:{queue_name}"],
            )
        )
        # Pre-D17 the domain's access policy (then in this stack) named `base.exec_role`,
        # which made launchpad-base export the role ARN. Nothing imports it any more, but
        # dropping the export would make the first `cdk deploy --all` on a LIVE
        # environment fail — CloudFormation updates launchpad-base before this stack
        # and refuses to remove an export the not-yet-updated launchpad-memory still
        # imports. Keeping the export (same auto-generated name) costs nothing and
        # keeps the base stack's outputs byte-identical to the pre-D17 snapshot.
        base.export_value(base.exec_role.role_arn)
        # D17: the shared domain's access policy no longer names this environment's
        # roles (it cannot know them); it delegates to IAM via the account root, so the
        # exec role carries its own grant here. FGAC then scopes it to this
        # environment's indices (`launchpad_memory_rw[_<env>]`, memory_bootstrap.py).
        base.exec_role.add_to_policy(
            iam.PolicyStatement(
                sid="MemoryDomainReadWrite",
                actions=["es:ESHttpGet", "es:ESHttpPost", "es:ESHttpPut", "es:ESHttpHead"],
                resources=[f"{domain_arn}/*"],
            )
        )

        # ---- least-privilege grants: extractor Lambda role ----
        base.artifacts.grant_read(extractor_role, "memory/*")
        base.artifacts.grant_read(extractor_role, "kb/*")
        queue.grant_consume_messages(extractor_role)
        # N17-obs247: the sync message fans one `kb_ingest_doc` message per changed document
        # out onto the SAME queue — the only new grant, on this stack's own queue
        queue.grant_send_messages(extractor_role)
        extractor_role.add_to_policy(
            iam.PolicyStatement(
                sid="MemoryDomainReadWrite",
                actions=[
                    "es:ESHttpGet",
                    "es:ESHttpPost",
                    "es:ESHttpPut",
                    "es:ESHttpHead",
                    "es:ESHttpDelete",
                ],
                resources=[f"{domain_arn}/*"],
            )
        )
        # N10-bug2 (live 2026-10-10): AgentCore Identity authorises
        # GetApiKeyCredentialProvider against the token VAULT
        # (`…:token-vault/default`), not against the provider ARN the call names —
        # every extractor invocation was denied with exactly that resource in the
        # AccessDenied message, so KB ingestion and long-term memory never ran.
        # The vault is one per account+region and is not EnvName-suffixed (the
        # provider `MODEL_CREDENTIAL` is shared by every environment too); the
        # provider ARN stays listed so the grant also matches should the service
        # start evaluating it there (same dual shape as the runtime's
        # `agent_iam._model_credential_statements`, which works live).
        vault_arn = (
            f"arn:{self.partition}:bedrock-agentcore:{self.region}:{self.account}:"
            "token-vault/default"
        )
        extractor_role.add_to_policy(
            iam.PolicyStatement(
                sid="ModelCredentialProvider",
                actions=["bedrock-agentcore:GetApiKeyCredentialProvider"],
                resources=[
                    vault_arn,
                    f"{vault_arn}/apikeycredentialprovider/{MODEL_CREDENTIAL}",
                    # D21: the `launchpad-*` family (OpenAI-compatible embedding rows)
                    f"{vault_arn}/apikeycredentialprovider/{MODEL_CREDENTIAL_FAMILY}",
                ],
            )
        )
        extractor_role.add_to_policy(
            iam.PolicyStatement(
                sid="ModelCredentialVaultSecret",
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:{self.partition}:secretsmanager:{self.region}:{self.account}:"
                    f"secret:bedrock-agentcore-identity!default/apikey/{MODEL_CREDENTIAL}-*",
                    # D21: the `launchpad-*` family (OpenAI-compatible embedding rows)
                    f"arn:{self.partition}:secretsmanager:{self.region}:{self.account}:"
                    f"secret:bedrock-agentcore-identity!default/apikey/"
                    f"{MODEL_CREDENTIAL_FAMILY}-*",
                ],
            )
        )

        configured_model_timeout = self.node.try_get_context("extract_model_read_timeout_seconds")
        model_read_timeout = float(
            DEFAULT_EXTRACT_MODEL_READ_TIMEOUT_SECONDS
            if configured_model_timeout is None else configured_model_timeout
        )
        if not math.isfinite(model_read_timeout) or not (
            1 <= model_read_timeout <= DEFAULT_EXTRACT_MODEL_READ_TIMEOUT_SECONDS
        ):
            raise ValueError("extract_model_read_timeout_seconds must be between 1 and 120")

        # ---- memory-extractor Lambda (arm64, SQS-triggered) ----
        fn = lambda_.Function(
            self,
            "MemoryExtractor",
            function_name=name(f"launchpad-memory-extractor-{self.region}"),
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_DIR)),
            role=extractor_role,
            timeout=Duration.seconds(LAMBDA_TIMEOUT_SECONDS),
            memory_size=512,
            log_group=lambda_log_group(self, "MemoryExtractorLogs"),  # N11-obs56
            environment={
                "LAUNCHPAD_MEMORY_OS_ENDPOINT": domain_endpoint,
                # D17: this environment's index prefix on the shared domain
                # (`launchpad-<env>-memory` …; empty = the unprefixed default names).
                "LAUNCHPAD_ENV_NAME": self.env_name,
                "LAUNCHPAD_MEMORY_BUCKET": base.artifacts.bucket_name,
                "LAUNCHPAD_MODEL_REGION": MODEL_REGION,
                "LAUNCHPAD_MODEL_CREDENTIAL": MODEL_CREDENTIAL,
                "LAUNCHPAD_EXTRACT_MODEL_ID": ctx("extract_model_id", DEFAULT_EXTRACT_MODEL),
                "LAUNCHPAD_EMBED_MODEL_ID": ctx("embed_model_id", DEFAULT_EMBED_MODEL),
                "LAUNCHPAD_EXTRACT_CONTEXT_EVENTS": "4",
                "LAUNCHPAD_EXTRACT_MODEL_READ_TIMEOUT_SECONDS": str(model_read_timeout),
                # N17-obs247: where the planner enqueues the per-document messages
                "LAUNCHPAD_MEMORY_QUEUE_URL": queue.queue_url,
            },
        )
        # N17-obs247: one message per invocation — a `kb_ingest_doc` message is one whole
        # document's embeddings + writes, and five of them in one invocation would put the
        # timeout back where the single-invocation sync had it; Lambda's concurrency,
        # not the batch, is the parallelism — CAPPED (rev-r22): the event source's
        # MaximumConcurrency bounds how many invocations a large KB fan-out can launch at once
        # against the single t3.small OpenSearch node (obs245 pressure) and how much of the
        # account's Lambda concurrency it may take. Context knob `extract_max_concurrency`
        # (default 5; the service minimum is 2).
        max_concurrency = int(ctx("extract_max_concurrency", str(DEFAULT_EXTRACT_MAX_CONCURRENCY)))
        if max_concurrency < 2:
            raise ValueError(
                f"extract_max_concurrency must be >= 2 (SQS event source minimum), "
                f"got {max_concurrency}"
            )
        fn.add_event_source(
            sources.SqsEventSource(queue, batch_size=1, report_batch_item_failures=True,
                                   max_concurrency=max_concurrency)
        )

        # ---- S3 lifecycle: expire memory/ snapshots on the base artifacts bucket (R12) ----
        base.artifacts.add_lifecycle_rule(
            id="memory-snapshot-expiry",
            prefix="memory/",
            expiration=Duration.days(int(ctx("memory_event_expiry_days", DEFAULT_EXPIRY_DAYS))),
        )

        # The domain values are re-exported so `memory_bootstrap.memory_resources()` keeps
        # reading one stack per environment (and assumes `MemoryAdminRoleArn` for every
        # OpenSearch call, as it always did when an admin role was exported).
        for output_name, value in (
            ("MemoryDomainEndpoint", domain_endpoint),
            ("MemoryDomainArn", domain_arn),
            ("MemoryDomainName", regional_domain_name(self.region, domain_env)),
            ("MemoryQueueUrl", queue.queue_url),
            ("MemoryQueueArn", queue.queue_arn),
            ("MemoryExtractorRoleArn", extractor_role.role_arn),
            ("MemoryExtractorFunctionName", fn.function_name),
            ("MemoryAdminRoleArn", admin_role_arn),
        ):
            CfnOutput(self, output_name, value=value)
