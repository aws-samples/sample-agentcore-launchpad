"""Bootstrap idempotency with mocked boto3 clients."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import urllib3
from botocore.exceptions import ClientError, EndpointConnectionError

from app.services import aws_clients
from app.services import bootstrap as bs
from app.services import memory_bootstrap as mb
from app.services import opensearch_http as core_http
from tests.test_memory_bootstrap import OUTPUTS as MEMORY_OUTPUTS
from tests.test_memory_bootstrap import SecOS


@pytest.fixture(autouse=True)
def global_bedrock_key(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_GLOBAL_BEDROCK_API_KEY", "global-bedrock-key")


STUB_DEFAULTS = {"records": [], "created": 0, "approved": 0, "unavailable_reason": ""}
# bound before the autouse recorder below replaces the module attribute, for the tests
# that exercise the real step
_real_ensure_registry_defaults = bs.ensure_registry_defaults
_real_ensure_policy_decisions = bs.ensure_policy_decisions


EVAL_WORKER_STUB = {"installed": False, "created": False, "changed": False,
                    "unavailable_reason": "stubbed in tests"}


@pytest.fixture(autouse=True)
def eval_worker_stage(monkeypatch) -> list:
    """The `evaluation` stage (N12) installs a system preset through the deploy pipeline;
    this suite asserts the stack map, so the stage is recorded, not run."""
    calls: list = []
    monkeypatch.setattr(
        bs, "ensure_eval_worker",
        lambda config, **kwargs: calls.append((config, kwargs)) or dict(EVAL_WORKER_STUB),
    )
    return calls


POLICY_STAGE_STUB = {"engine": "cedarpy", "mode": "log_only", "active_policies": 0,
                     "log_group": "/launchpad/policy/decisions", "created": False,
                     "retention_days": 90, "unavailable_reason": "", "skipped": False}


@pytest.fixture(autouse=True)
def policy_stage(monkeypatch) -> list:
    """The `policy` stage (D20 P1-d) ensures the CloudWatch decisions log group through a
    `logs` client the per-test client maps do not all carry; recorded here, exercised for
    real in `TestPolicyStage` below with a stub logs client."""
    calls: list = []
    monkeypatch.setattr(
        bs, "ensure_policy_decisions",
        lambda region, log=print: calls.append(region) or dict(POLICY_STAGE_STUB),
    )
    return calls


@pytest.fixture(autouse=True)
def registry_defaults(monkeypatch) -> list:
    """The registry-defaults step (N10-bug1) talks to the gateway (MCP tools/list) and
    S3 (sample skill upload) through the real console code; this suite asserts the
    stack map and the AWS stubs, so the step is recorded, not run. Returns the recorder:
    one entry per call with the workspace context the step received."""
    calls: list = []
    monkeypatch.setattr(
        bs, "ensure_registry_defaults",
        lambda workspace, log=print: calls.append(workspace) or dict(STUB_DEFAULTS),
    )
    return calls


def merged_eval_reason(written: list[dict]) -> str:
    merged = {k: v for w in written for k, v in w.get("resources", {}).items()}
    assert merged["eval_worker_runtime_arn"] == "" and merged["eval_worker_agent_id"] == ""
    return merged["eval_worker_unavailable_reason"]


def _fake_logs_with_spans_policy():
    """CloudWatch Logs stub whose account already carries the aws/spans resource
    policy, so the hub observability block (N11-bug10) finds it and does not
    call put_resource_policy."""
    logs = MagicMock()
    logs.describe_resource_policies.return_value = {
        "resourcePolicies": [{"policyName": "TransactionSearchXRayAccess"}]
    }
    return logs


def _two_stack_outputs(base_outputs: dict, memory_outputs: dict | None = MEMORY_OUTPUTS):
    """`bs.get_stack_outputs(region, stack_name="launchpad-base")`, dispatching on
    `stack_name` the way the real two-stack hub bootstrap does."""

    def lookup(_region, stack_name="launchpad-base"):
        if stack_name == "launchpad-base":
            return base_outputs
        if memory_outputs is not None:
            return memory_outputs
        raise ClientError({"Error": {"Code": "ValidationError"}}, "DescribeStacks")

    return lookup


@pytest.fixture(autouse=True)
def _stub_skill_lab(monkeypatch):
    """The Skill Lab stage (worker role + CodeBuild image + runtime + host venv) has its
    own focused fakes in test_skill_lab_foundation.py; here it is a no-op that returns
    the resource keys run_bootstrap persists."""
    monkeypatch.setattr(
        bs,
        "ensure_skill_lab",
        lambda _account, _region, _resources: {
            "skill_lab_worker_role_arn": "arn:role/skill-lab",
            "skill_lab_worker_runtime_arn": "arn:rt/skill-lab",
            "skill_lab_worker_runtime_id": "rt-1",
            "skill_lab_worker_image_tag": "skill-lab-worker-abc",
            "skill_lab_worker_image_digest": "sha256:abc",
            "python": "/tmp/skill-lab-venv/bin/python",
        },
    )


def test_run_bootstrap_records_the_memory_substrate(monkeypatch):
    outputs = {
        "ArtifactsBucketName": "bucket",
        "EcrRepoName": "repo",
        "EcrRepoUri": "account.dkr.ecr.us-west-2.amazonaws.com/repo",
        "CodeBuildProjectName": "build",
        "AgentExecutionRoleArn": "arn:aws:iam::111:role/exec",
    }
    control = MagicMock()
    control.get_api_key_credential_provider.return_value = {
        "credentialProviderArn": (
            "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111:token-vault/default/"
            "apikeycredentialprovider/launchpad-global-bedrock"
        ),
        "apiKeySecretArn": (
            "arn:aws-cn:secretsmanager:cn-northwest-1:111:secret:"
            "bedrock-agentcore-identity!default/apikey/launchpad-global-bedrock-x"
        ),
    }
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "111"}
    clients = {
        "bedrock-agentcore-control": control,
        "sts": sts,
    }
    written: list[dict] = []
    fake_os = SecOS()

    monkeypatch.setattr(bs, "get_stack_outputs", _two_stack_outputs(outputs))
    monkeypatch.setattr(bs, "_client", lambda service, _region: clients[service])
    monkeypatch.setattr(bs, "load_config", lambda: {})
    monkeypatch.setattr(
        bs,
        "write_config",
        lambda update, replace=None: written.append(update | (replace or {})) or update,
    )
    monkeypatch.setattr(aws_clients, "opensearch_http", lambda _ctx, _endpoint: fake_os)

    summary = bs.run_bootstrap("us-west-2")

    # the Registry is local (N4) and the model credential is an Identity provider (N2):
    # the summary names both; the stack map (first write) carries neither
    assert set(summary) == {
        "account_id",
        "region",
        "registry",
        "model_credential",
        "model_credentials",  # N-R6: per-provider summary
        "model_providers",  # D21 M1-b: the registry (SSM-backed) credential pass
        "policy",  # D20 P1-d: engine / mode / decisions log group
        "gateway",
        "observability",
        "memory",
        "evaluation",
        "stack_outputs",
        "skill_lab",
    }
    assert summary["evaluation"] == EVAL_WORKER_STUB
    assert merged_eval_reason(written) == "stubbed in tests"
    assert summary["registry"] == {
        "id": "local-default", "created": False, "defaults": STUB_DEFAULTS}
    assert summary["gateway"] is None and summary["observability"] is None
    resources = written[0]["resources"]
    assert resources["artifacts_bucket"] == "bucket"
    assert resources["execution_role_arn"] == "arn:aws:iam::111:role/exec"
    assert not {k for k in resources if "registry" in k or "memory" in k}
    assert "kb_role_arn" not in resources
    assert summary["model_credential"] == {"name": "launchpad-global-bedrock", "created": False}
    control.update_api_key_credential_provider.assert_called_once_with(
        name="launchpad-global-bedrock", apiKey="global-bedrock-key"
    )
    merged = {k: v for w in written for k, v in w.get("resources", {}).items()}
    assert merged["model_credential_name"] == "launchpad-global-bedrock"
    assert merged["model_credential_arn"].endswith(
        "apikeycredentialprovider/launchpad-global-bedrock"
    )
    assert merged["model_credential_secret_arn"].startswith("arn:aws-cn:secretsmanager:")
    assert "global-bedrock-key" not in json.dumps(written)  # C4: never written
    assert merged["registry_id"] == "local-default"
    assert "kb_role_arn" not in merged
    for key in mb.MEMORY_RESOURCE_KEYS:
        assert key in merged
    assert merged["memory_id"] == "launchpad_memory-default"
    assert merged["memory_unavailable_reason"] == ""
    assert summary["memory"]["endpoint"] == MEMORY_OUTPUTS["MemoryDomainEndpoint"]
    assert summary["memory"]["unavailable_reason"] == ""
    assert "index launchpad-memory" in summary["memory"]["created"]


def test_run_bootstrap_without_the_memory_stack_records_the_reason(monkeypatch):
    outputs = {
        "ArtifactsBucketName": "bucket",
        "EcrRepoName": "repo",
        "EcrRepoUri": "account.dkr.ecr.us-west-2.amazonaws.com/repo",
        "CodeBuildProjectName": "build",
        "AgentExecutionRoleArn": "arn:aws:iam::111:role/exec",
    }
    control = MagicMock()
    control.get_api_key_credential_provider.return_value = {
        "credentialProviderArn": (
            "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111:token-vault/default/"
            "apikeycredentialprovider/launchpad-global-bedrock"
        ),
        "apiKeySecretArn": (
            "arn:aws-cn:secretsmanager:cn-northwest-1:111:secret:"
            "bedrock-agentcore-identity!default/apikey/launchpad-global-bedrock-x"
        ),
    }
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "111"}
    clients = {"bedrock-agentcore-control": control, "sts": sts}
    opensearch_calls: list[str] = []

    monkeypatch.setattr(bs, "get_stack_outputs", _two_stack_outputs(outputs, memory_outputs=None))
    monkeypatch.setattr(bs, "_client", lambda service, _region: clients[service])
    monkeypatch.setattr(bs, "load_config", lambda: {})
    monkeypatch.setattr(bs, "write_config", lambda update, replace=None: update)
    monkeypatch.setattr(
        aws_clients, "opensearch_http", lambda *_a, **_kw: opensearch_calls.append("called")
    )

    summary = bs.run_bootstrap("us-west-2")

    assert summary["memory"]["endpoint"] == ""
    assert summary["memory"]["unavailable_reason"].startswith("launchpad-memory stack not deployed")
    assert opensearch_calls == []


def test_run_bootstrap_degrades_when_the_fgac_endpoint_rejects_the_admin_role(monkeypatch):
    """A 403 (or any OpenSearch/ClientError/connection failure) from the FGAC
    endpoint must not crash bootstrap — it degrades like the stack-absent branch,
    and gateway/observability still run."""
    from app.services import gateway_bootstrap

    outputs = {
        "ArtifactsBucketName": "bucket",
        "EcrRepoName": "repo",
        "EcrRepoUri": "account.dkr.ecr.us-west-2.amazonaws.com/repo",
        "CodeBuildProjectName": "build",
        "AgentExecutionRoleArn": "arn:aws:iam::111:role/exec",
        "GatewayRoleArn": "arn:aws:iam::111:role/gateway",
    }
    control = MagicMock()
    control.get_api_key_credential_provider.return_value = {
        "credentialProviderArn": (
            "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111:token-vault/default/"
            "apikeycredentialprovider/launchpad-global-bedrock"
        ),
        "apiKeySecretArn": (
            "arn:aws-cn:secretsmanager:cn-northwest-1:111:secret:"
            "bedrock-agentcore-identity!default/apikey/launchpad-global-bedrock-x"
        ),
    }
    xray = MagicMock()
    xray.get_trace_segment_destination.return_value = {
        "Destination": "CloudWatchLogs",
        "Status": "ACTIVE",
    }
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "111"}
    clients = {
        "bedrock-agentcore-control": control,
        "sts": sts,
        "apigateway": MagicMock(),
        "xray": xray,
        "logs": _fake_logs_with_spans_policy(),
    }
    gateway_summary = {
        "gateway": {
            "id": "launchpad-gw-1",
            "arn": "arn:aws:bedrock-agentcore:us-west-2:111:gateway/launchpad-gw-1",
            "url": "https://gateway.example/mcp",
        },
        "api_key_provider": {"arn": "arn:provider/api-key"},
        "targets": {},
    }

    class ForbiddenOS:
        """Stands in for the funnel's `aws_clients.opensearch_http` client — the
        FGAC endpoint answers every call with a 403."""

        def ensure_index(self, name, body):
            raise core_http.OpenSearchError(403, "forbidden")

    monkeypatch.setattr(bs, "get_stack_outputs", _two_stack_outputs(outputs))
    monkeypatch.setattr(bs, "_client", lambda service, _region: clients[service])
    monkeypatch.setattr(bs, "load_config", lambda: {})
    written: list[dict] = []
    monkeypatch.setattr(
        bs,
        "write_config",
        lambda update, replace=None: written.append(update | (replace or {})) or update,
    )
    monkeypatch.setattr(aws_clients, "opensearch_http", lambda *_a, **_kw: ForbiddenOS())
    monkeypatch.setattr(
        gateway_bootstrap, "run_gateway_bootstrap", lambda *_args, **_kwargs: gateway_summary
    )

    summary = bs.run_bootstrap("us-west-2")

    assert summary["memory"]["unavailable_reason"].startswith("OpenSearch FGAC provisioning failed")
    assert "403" in summary["memory"]["unavailable_reason"]
    assert "forbidden" not in summary["memory"]["unavailable_reason"]  # no raw body/secrets
    # later stages still ran
    assert summary["gateway"] == gateway_summary
    assert summary["observability"] == {
        "enabled": True,
        "changed": False,
        "status": "ACTIVE",
    }
    merged = {k: v for w in written for k, v in w.get("resources", {}).items()}
    assert merged["memory_unavailable_reason"] == summary["memory"]["unavailable_reason"]
    assert merged["memory_os_endpoint"] == MEMORY_OUTPUTS["MemoryDomainEndpoint"]


def _healthy_domain_run(monkeypatch, fake_os, previous_reason: str, capsys, *,
                        sleeper=None, caller_arn: str = ""):
    """Shared fixture for the N-SCN-obs6 pair below: a full two-stack hub bootstrap whose
    memory stage talks to `fake_os`, with `launchpad.yaml` already carrying
    `previous_reason`. Returns (summary, merged resources, stdout)."""
    outputs = {
        "ArtifactsBucketName": "bucket",
        "EcrRepoName": "repo",
        "EcrRepoUri": "account.dkr.ecr.us-west-2.amazonaws.com/repo",
        "CodeBuildProjectName": "build",
        "AgentExecutionRoleArn": "arn:aws:iam::111:role/exec",
    }
    control = MagicMock()
    control.get_api_key_credential_provider.return_value = {
        "credentialProviderArn": (
            "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111:token-vault/default/"
            "apikeycredentialprovider/launchpad-global-bedrock"
        ),
        "apiKeySecretArn": (
            "arn:aws-cn:secretsmanager:cn-northwest-1:111:secret:"
            "bedrock-agentcore-identity!default/apikey/launchpad-global-bedrock-x"
        ),
    }
    sts = MagicMock()
    sts.get_caller_identity.return_value = (
        {"Account": "111", "Arn": caller_arn} if caller_arn else {"Account": "111"}
    )
    clients = {"bedrock-agentcore-control": control, "sts": sts}
    monkeypatch.setattr(bs, "get_stack_outputs", _two_stack_outputs(outputs))
    monkeypatch.setattr(bs, "_client", lambda service, _region: clients[service])
    monkeypatch.setattr(
        bs, "load_config",
        lambda: {"resources": {"memory_unavailable_reason": previous_reason}},
    )
    written: list[dict] = []
    monkeypatch.setattr(
        bs,
        "write_config",
        lambda update, replace=None: written.append(update | (replace or {})) or update,
    )
    monkeypatch.setattr(aws_clients, "opensearch_http", lambda *_a, **_kw: fake_os)
    kwargs = {"sleeper": sleeper} if sleeper is not None else {}
    summary = bs.run_bootstrap("us-west-2", **kwargs)
    merged = {k: v for w in written for k, v in w.get("resources", {}).items()}
    return summary, merged, capsys.readouterr().out


class _ProvisionedButFlakyOS(SecOS):
    """The live N-SCN-obs6 shape: every index and the FGAC role/mapping already exist
    (pre-seeded here), the first idempotent security GET times out — and keeps timing
    out past the funnel's retries, so the stage raises — while the probe's
    `_cluster/health`, index HEADs, role GET and mapping GET all answer."""

    def __init__(self):
        super().__init__()
        for name, body in mb.fixed_indices():
            self.ensure_index(name, body)
        self.security[f"/_plugins/_security/api/roles/{mb.security_role()}"] = (
            mb.security_role_body()
        )
        self.security[f"/_plugins/_security/api/rolesmapping/{mb.security_role()}"] = {
            "users": ["arn:aws-cn:iam::111:role/exec"]
        }
        self.probe_role_gets = 0
        self.probe_mapping_gets = 0
        self.stage_running = True

    def request(self, method, path, body=None, params=None):
        if path == "/_cluster/health":
            self.stage_running = False  # the probe has started
            return {"status": "green", "number_of_shards": 31}
        if method == "GET" and "/api/roles/" in path and self.stage_running:
            raise urllib3.exceptions.ReadTimeoutError(None, path, "Read timed out")
        if method == "GET" and "/api/roles/" in path:
            self.probe_role_gets += 1
        if method == "GET" and "/api/rolesmapping/" in path:
            self.probe_mapping_gets += 1
        return super().request(method, path, body, params)


@pytest.mark.parametrize(
    "previous_reason", ["", "launchpad-memory stack not deployed in us-west-2"]
)
def test_memory_stage_clears_the_reason_when_the_substrate_probe_passes(
    monkeypatch, capsys, previous_reason
):
    """N-SCN-obs6 (c): a transient failure of one idempotent FGAC step on a healthy,
    already-provisioned domain must not become `memory_unavailable_reason` — that flipped
    `workspace_has_shared_memory()` to False on the live default environment while
    `/api/memory/overview` was ACTIVE. The probe covers everything the stage provisions,
    so a healthy probe is the stage's own verdict: the reason is written empty (a stale
    one left by an earlier failure included — review follow-up), the WARN names what it
    cleared, and the summary carries the warning."""
    fake_os = _ProvisionedButFlakyOS()
    summary, merged, out = _healthy_domain_run(monkeypatch, fake_os, previous_reason, capsys)

    assert summary["memory"]["unavailable_reason"] == ""
    assert merged["memory_unavailable_reason"] == ""
    assert merged["memory_os_endpoint"] == MEMORY_OUTPUTS["MemoryDomainEndpoint"]
    assert summary["memory"]["endpoint"] == MEMORY_OUTPUTS["MemoryDomainEndpoint"]
    assert summary["memory"]["created"] == [] and summary["memory"]["reused"] == []
    assert summary["memory"]["warning"] == (
        "substrate healthy, provisioning step skipped: ReadTimeoutError"
    )
    assert "memory stage WARN:" in out
    assert "(substrate healthy, provisioning step skipped: ReadTimeoutError" in out
    assert "memory substrate unavailable" not in out
    if previous_reason:
        assert f"cleared stale memory_unavailable_reason={previous_reason!r}" in out
    else:
        assert "memory_unavailable_reason stays empty" in out
    # the probe asked the security API for both the role and its mapping
    assert fake_os.probe_role_gets == 1 and fake_os.probe_mapping_gets == 1
    assert mb.workspace_has_shared_memory(merged)  # the architect keeps shared memory


class _DeadDomainOS:
    """Stage and probe both fail: the domain does not answer at all."""

    def ensure_index(self, name, body):
        raise urllib3.exceptions.ReadTimeoutError(None, f"/{name}", "Read timed out")

    def request(self, method, path, body=None, params=None):
        raise urllib3.exceptions.ReadTimeoutError(None, path, "Read timed out")


def test_memory_stage_records_the_reason_when_the_probe_fails_too(monkeypatch, capsys):
    """N-SCN-obs6 (d): the pre-existing degrade path is unchanged when the probe agrees
    the substrate is unusable — the reason is recorded and bootstrap carries on."""
    summary, merged, out = _healthy_domain_run(monkeypatch, _DeadDomainOS(), "", capsys)

    reason = "OpenSearch FGAC provisioning failed (ReadTimeoutError)"
    assert summary["memory"]["unavailable_reason"] == reason
    assert merged["memory_unavailable_reason"] == reason
    assert "warning" not in summary["memory"]
    assert "memory substrate unavailable — " + reason in out
    assert "probe failed (ReadTimeoutError)" in out
    assert "memory stage WARN" not in out
    assert not mb.workspace_has_shared_memory(merged)


class _IndexMissingOS(_ProvisionedButFlakyOS):
    def index_exists(self, name):
        return name != mb.naming.kb_docs_index()


def test_memory_stage_probe_requires_every_fixed_index(monkeypatch, capsys):
    """Half-provisioned is not healthy: a green cluster missing one of the three fixed
    indices records the reason like before (the probe says which index is missing)."""
    summary, merged, out = _healthy_domain_run(monkeypatch, _IndexMissingOS(), "", capsys)
    assert summary["memory"]["unavailable_reason"].startswith("OpenSearch FGAC provisioning")
    assert f"missing indices: {mb.naming.kb_docs_index()}" in out


class _MappingMissingOS(_ProvisionedButFlakyOS):
    """Role present, mapping never PUT — a bootstrap interrupted between the two FGAC
    steps. Without the mapping no role (exec, extractor, agent) can reach the indices."""

    def __init__(self):
        super().__init__()
        del self.security[f"/_plugins/_security/api/rolesmapping/{mb.security_role()}"]


def test_memory_stage_probe_requires_the_role_mapping(monkeypatch, capsys):
    """Review follow-up: the probe covers everything `ensure_memory_substrate`
    provisions — a green cluster with all indices and the role but no role mapping is
    not healthy, the reason is recorded and the probe detail names the mapping."""
    summary, merged, out = _healthy_domain_run(monkeypatch, _MappingMissingOS(), "", capsys)
    assert summary["memory"]["unavailable_reason"].startswith("OpenSearch FGAC provisioning")
    assert f"role mapping {mb.security_role()} absent" in out
    assert "memory stage WARN" not in out
    assert not mb.workspace_has_shared_memory(merged)


def _assume_denied() -> ClientError:
    """What botocore raises from inside the first signed OpenSearch request when the hub
    role may not assume the memory admin role — the live N11-obs44 shape."""
    return ClientError(
        {"Error": {"Code": "AccessDenied", "Message": (
            "User: arn:aws-cn:sts::111:assumed-role/launchpad-cn-ops-cn-northwest-1-t1/i-0 "
            "is not authorized to perform: sts:AssumeRole on resource: "
            + MEMORY_OUTPUTS["MemoryAdminRoleArn"]
        )}},
        aws_clients.ASSUME_ROLE_OPERATION,
    )


class _AssumeDeniedOS:
    """Every OpenSearch call fails at credential time: the AssumeRole is refused."""

    def __init__(self):
        self.calls = 0

    def _deny(self):
        self.calls += 1
        raise _assume_denied()

    def ensure_index(self, name, body):
        self._deny()

    def index_exists(self, name):
        self._deny()

    def request(self, method, path, body=None, params=None):
        self._deny()


def test_memory_stage_names_the_assume_role_denial_without_retrying_or_probing(
    monkeypatch, capsys, eval_worker_stage
):
    """N11-obs44a: a pre-D17 ops role (no `MemoryAdminAssume` statement) cannot assume
    `launchpad-memory-admin-<region>`. The stage used to call that "AccessDenied right
    after an IAM change (propagation)", retry 2×15 s and then record an OpenSearch
    verdict (`OpenSearch FGAC provisioning failed (ClientError AccessDenied)`). Now the
    first denial ends the stage, the probe (which would fail identically) is skipped and
    the recorded reason names the role, the caller and the missing statement.
    N11-obs44c: the stage degrades — the stages after memory still run."""
    slept: list[float] = []
    fake_os = _AssumeDeniedOS()
    summary, merged, out = _healthy_domain_run(monkeypatch, fake_os, "", capsys,
                                               sleeper=slept.append)

    reason = summary["memory"]["unavailable_reason"]
    assert reason == merged["memory_unavailable_reason"]
    assert reason.startswith(
        f"cannot assume the memory admin role {MEMORY_OUTPUTS['MemoryAdminRoleArn']} "
        "(AccessDenied on sts:AssumeRole)"
    )
    assert "`MemoryAdminAssume`" in reason and "role/launchpad-memory-admin-*" in reason
    assert "deploy.md §8.3" in reason
    assert "is not authorized" not in reason  # the STS message body is not echoed
    assert "memory substrate unavailable — cannot assume the memory admin role" in out
    # not propagation: no retry, no back-off, no misleading OpenSearch verdict, no probe
    assert slept == [] and fake_os.calls == 1
    assert "propagation" not in out
    assert "OpenSearch FGAC provisioning failed" not in out and "probe" not in out
    assert "warning" not in summary["memory"]
    assert not mb.workspace_has_shared_memory(merged)
    # N11-obs44c: degraded, not aborted — the evaluation stage after memory ran
    assert len(eval_worker_stage) == 1
    assert summary["evaluation"] == EVAL_WORKER_STUB


def test_memory_stage_denial_reason_names_the_caller_when_sts_reports_it(
    monkeypatch, capsys
):
    """The caller ARN STS returned is part of the reason, so the operator knows WHICH
    ops role needs the statement (t1's role vs the default environment's)."""
    caller = "arn:aws-cn:sts::111:assumed-role/launchpad-cn-ops-cn-northwest-1-t1/i-0abc"
    summary, _merged, _out = _healthy_domain_run(
        monkeypatch, _AssumeDeniedOS(), "", capsys, caller_arn=caller
    )
    reason = summary["memory"]["unavailable_reason"]
    assert f"the caller {caller} lacks the `MemoryAdminAssume` statement" in reason


class _UnreachableOS:
    """A transport failure below botocore's ClientError: STS/OpenSearch unreachable."""

    def _fail(self, what):
        raise EndpointConnectionError(endpoint_url=f"https://{what}.example")

    def ensure_index(self, name, body):
        self._fail("sts")

    def index_exists(self, name):
        self._fail("sts")

    def request(self, method, path, body=None, params=None):
        self._fail("sts")


def test_memory_stage_degrades_on_any_error_and_the_later_stages_still_run(
    monkeypatch, capsys, eval_worker_stage, registry_defaults
):
    """N11-obs44c: during a D17 migration the memory stage runs after the ~16 min cdk
    deploy and before the evaluation / gateway / observability / registry / Skill Lab
    stages. A failure the old except tuple did not name (a botocore
    `EndpointConnectionError` is not a `ClientError`) used to abort the whole run and,
    through `set -e`, the host update before the restart. Now every exception degrades
    the stage like a FGAC failure does: the probe decides, the reason is recorded, the
    stages after memory run and `run_bootstrap` returns its summary."""
    summary, merged, out = _healthy_domain_run(monkeypatch, _UnreachableOS(), "", capsys)

    reason = "OpenSearch FGAC provisioning failed (EndpointConnectionError)"
    assert summary["memory"]["unavailable_reason"] == reason
    assert merged["memory_unavailable_reason"] == reason
    assert "memory substrate unavailable — " + reason in out
    assert "probe failed (EndpointConnectionError)" in out
    assert not mb.workspace_has_shared_memory(merged)
    # the stages after memory ran and reported
    assert len(eval_worker_stage) == 1 and summary["evaluation"] == EVAL_WORKER_STUB
    assert len(registry_defaults) == 1 and summary["registry"]["defaults"] == STUB_DEFAULTS
    assert summary["skill_lab"]["skill_lab_worker_runtime_id"] == "rt-1"
    assert "skill_lab_worker_runtime_arn" in merged


def test_run_bootstrap_gateway_does_not_provision_policy(monkeypatch):
    from app.services import gateway_bootstrap

    outputs = {
        "ArtifactsBucketName": "bucket",
        "EcrRepoName": "repo",
        "EcrRepoUri": "account.dkr.ecr.us-west-2.amazonaws.com/repo",
        "CodeBuildProjectName": "build",
        "AgentExecutionRoleArn": "arn:aws:iam::111:role/exec",
        "GatewayRoleArn": "arn:aws:iam::111:role/gateway",
    }
    control = MagicMock()
    control.get_api_key_credential_provider.return_value = {
        "credentialProviderArn": (
            "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111:token-vault/default/"
            "apikeycredentialprovider/launchpad-global-bedrock"
        ),
        "apiKeySecretArn": (
            "arn:aws-cn:secretsmanager:cn-northwest-1:111:secret:"
            "bedrock-agentcore-identity!default/apikey/launchpad-global-bedrock-x"
        ),
    }
    xray = MagicMock()
    xray.get_trace_segment_destination.return_value = {
        "Destination": "CloudWatchLogs",
        "Status": "ACTIVE",
    }
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "111"}
    clients = {
        "bedrock-agentcore-control": control,
        "sts": sts,
        "apigateway": MagicMock(),
        "xray": xray,
        "logs": _fake_logs_with_spans_policy(),
    }
    requested_clients: list[str] = []
    written: list[dict] = []
    gateway_summary = {
        "gateway": {
            "id": "launchpad-gw-1",
            "arn": "arn:aws:bedrock-agentcore:us-west-2:111:gateway/launchpad-gw-1",
            "url": "https://gateway.example/mcp",
        },
        "api_key_provider": {"arn": "arn:provider/api-key"},
        "targets": {},
    }

    def client(service: str, _region: str):
        requested_clients.append(service)
        if service in ("bedrock-agentcore-policy", "policy"):
            raise AssertionError(f"bootstrap requested Policy client {service}")
        return clients[service]

    monkeypatch.setattr(
        bs, "get_stack_outputs", _two_stack_outputs(outputs, memory_outputs=None)
    )
    monkeypatch.setattr(bs, "_client", client)
    monkeypatch.setattr(bs, "load_config", lambda: {})
    monkeypatch.setattr(
        bs,
        "write_config",
        lambda update, replace=None: written.append(update | (replace or {})) or update,
    )
    monkeypatch.setattr(
        gateway_bootstrap,
        "run_gateway_bootstrap",
        lambda *_args, **_kwargs: gateway_summary,
    )

    summary = bs.run_bootstrap("us-west-2")

    assert summary["gateway"] == gateway_summary
    # D20: `summary["policy"]` is the SELF-HOSTED stage (engine / mode / decisions log
    # group) — nothing AgentCore-Policy-shaped is provisioned (asserted below)
    assert summary["policy"] == POLICY_STAGE_STUB
    assert summary["observability"] == {
        "enabled": True,
        "changed": False,
        "status": "ACTIVE",
    }
    assert requested_clients.count("xray") == 1
    # N11-bug10: the hub path now places the aws/spans resource policy (one
    # logs client) before enabling Transaction Search; no put when present.
    assert requested_clients.count("logs") == 1
    clients["logs"].put_resource_policy.assert_not_called()
    assert all(
        "policy_engine_id" not in (entry.get("resources") or {})
        and "policy_engine_arn" not in (entry.get("resources") or {})
        for entry in written
    )
    # the self-hosted stage records only the decisions log group (D20 P1-d)
    merged = {k: v for w in written for k, v in (w.get("resources") or {}).items()}
    assert merged["policy_log_group"] == "/launchpad/policy/decisions"
    assert merged["policy_log_group_unavailable_reason"] == ""
    control.create_policy_engine.assert_not_called()
    control.create_policy.assert_not_called()
    control.update_gateway.assert_not_called()


def test_run_bootstrap_creates_the_model_credential_when_absent(monkeypatch):
    outputs = {
        "ArtifactsBucketName": "b",
        "EcrRepoName": "r",
        "EcrRepoUri": "u",
        "CodeBuildProjectName": "c",
        "AgentExecutionRoleArn": "arn:aws-cn:iam::111:role/exec",
    }
    control = MagicMock()
    control.get_api_key_credential_provider.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException"}}, "GetApiKeyCredentialProvider"
    )
    control.create_api_key_credential_provider.return_value = {
        "credentialProviderArn": (
            "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111:token-vault/default/"
            "apikeycredentialprovider/launchpad-global-bedrock"
        ),
        "apiKeySecretArn": "arn:aws-cn:secretsmanager:cn-northwest-1:111:secret:x",
    }
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "111"}
    clients = {"bedrock-agentcore-control": control, "sts": sts}
    monkeypatch.setattr(
        bs, "get_stack_outputs", _two_stack_outputs(outputs, memory_outputs=None)
    )
    monkeypatch.setattr(bs, "_client", lambda service, _region: clients[service])
    monkeypatch.setattr(bs, "load_config", lambda: {})
    monkeypatch.setattr(bs, "write_config", lambda update, replace=None: update)

    summary = bs.run_bootstrap("cn-northwest-1")

    assert summary["model_credential"] == {"name": "launchpad-global-bedrock", "created": True}
    control.create_api_key_credential_provider.assert_called_once_with(
        name="launchpad-global-bedrock", apiKey="global-bedrock-key"
    )
    control.update_api_key_credential_provider.assert_not_called()


def test_run_bootstrap_without_the_key_fails_before_touching_identity(monkeypatch):
    from app.services.model_credential import MissingModelCredentialError

    monkeypatch.delenv("LAUNCHPAD_GLOBAL_BEDROCK_API_KEY")
    control = MagicMock()
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "111"}
    clients = {"bedrock-agentcore-control": control, "sts": sts}
    monkeypatch.setattr(
        bs,
        "get_stack_outputs",
        lambda _region: {
            "ArtifactsBucketName": "b",
            "EcrRepoName": "r",
            "EcrRepoUri": "u",
            "CodeBuildProjectName": "c",
            "AgentExecutionRoleArn": "arn:aws-cn:iam::111:role/exec",
        },
    )
    monkeypatch.setattr(bs, "_client", lambda service, _region: clients[service])
    monkeypatch.setattr(bs, "load_config", lambda: {})
    monkeypatch.setattr(bs, "write_config", lambda update, replace=None: update)
    with pytest.raises(MissingModelCredentialError, match="LAUNCHPAD_GLOBAL_BEDROCK_API_KEY"):
        bs.run_bootstrap("cn-northwest-1")
    control.create_api_key_credential_provider.assert_not_called()
    control.update_api_key_credential_provider.assert_not_called()


def test_merge_config_deep_merges():
    base = {"region": "us-west-2", "resources": {"a": 1, "b": 2}, "keep": True}
    update = {"resources": {"b": 3, "c": 4}, "region": "us-west-2"}
    merged = bs.merge_config(base, update)
    assert merged == {
        "region": "us-west-2",
        "resources": {"a": 1, "b": 3, "c": 4},
        "keep": True,
    }
    # base untouched
    assert base["resources"] == {"a": 1, "b": 2}


def test_write_config_replace_drops_stale_keys(monkeypatch, tmp_path):
    """A deep merge would resurrect a deleted demo user's password from the file."""
    config_file = tmp_path / "launchpad.yaml"
    config_file.write_text(
        "demo_users:\n  passwords:\n    river: stale\n    demo: kept\nregion: us-west-2\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bs, "CONFIG_FILE", config_file)

    merged = bs.write_config(
        {"resources": {"a": "1"}},
        replace={"demo_users": {"passwords": {"admin": "new", "demo": "kept"}}},
    )

    assert merged["demo_users"]["passwords"] == {"admin": "new", "demo": "kept"}
    assert merged["region"] == "us-west-2"
    assert "river" not in config_file.read_text(encoding="utf-8")


def test_write_config_preserves_legacy_policy_ids(monkeypatch, tmp_path):
    config_file = tmp_path / "launchpad.yaml"
    config_file.write_text(
        "resources:\n"
        "  policy_engine_id: pe-existing\n"
        "  policy_engine_arn: arn:policy-engine/pe-existing\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bs, "CONFIG_FILE", config_file)

    merged = bs.write_config({"resources": {"gateway_id": "gw-existing"}})

    assert merged["resources"] == {
        "gateway_id": "gw-existing",
        "policy_engine_id": "pe-existing",
        "policy_engine_arn": "arn:policy-engine/pe-existing",
    }




# ───────────── N10-bug1: registry defaults at the end of the hub bootstrap ─────────────


def _hub_outputs(**extra):
    return {
        "ArtifactsBucketName": "bucket",
        "EcrRepoName": "repo",
        "EcrRepoUri": "account.dkr.ecr.us-west-2.amazonaws.com/repo",
        "CodeBuildProjectName": "build",
        "AgentExecutionRoleArn": "arn:aws:iam::111:role/exec",
        **extra,
    }


def _hub_clients():
    control = MagicMock()
    control.get_api_key_credential_provider.return_value = {
        "credentialProviderArn": (
            "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111:token-vault/default/"
            "apikeycredentialprovider/launchpad-global-bedrock"
        ),
        "apiKeySecretArn": "arn:aws-cn:secretsmanager:cn-northwest-1:111:secret:x",
    }
    xray = MagicMock()
    xray.get_trace_segment_destination.return_value = {
        "Destination": "CloudWatchLogs", "Status": "ACTIVE"}
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "111"}
    return {"bedrock-agentcore-control": control, "sts": sts, "apigateway": MagicMock(),
            "xray": xray, "logs": _fake_logs_with_spans_policy()}


def _cumulative_write_config(monkeypatch) -> dict:
    """`write_config` as the real one behaves: returns the merged map, so the registry
    step sees every key written before it."""
    merged: dict = {}

    def write(update, replace=None):
        merged.update(bs.merge_config(merged, update))
        for key, value in (replace or {}).items():
            merged[key] = value
        return merged

    monkeypatch.setattr(bs, "load_config", lambda: {})
    monkeypatch.setattr(bs, "write_config", write)
    return merged


def test_run_bootstrap_imports_the_registry_defaults_last_with_the_gateway_url(
    monkeypatch, registry_defaults
):
    """The step runs after the gateway (its MCP records point at the gateway URL) with
    the resource map THIS run wrote — bucket from the stack, URL from the gateway
    stage — on the hub's default workspace."""
    from app.services import gateway_bootstrap

    order: list[str] = []
    gateway_summary = {
        "gateway": {"id": "gw-1", "arn": "arn:gw", "url": "https://gateway.example/mcp"},
        "api_key_provider": {"arn": "arn:provider/api-key"},
        "targets": {},
    }
    monkeypatch.setattr(
        bs, "get_stack_outputs",
        _two_stack_outputs(_hub_outputs(GatewayRoleArn="arn:aws:iam::111:role/gateway"), None),
    )
    monkeypatch.setattr(bs, "_client", lambda service, _region: _hub_clients()[service])
    _cumulative_write_config(monkeypatch)
    monkeypatch.setattr(
        gateway_bootstrap, "run_gateway_bootstrap",
        lambda *_a, **_k: order.append("gateway") or gateway_summary,
    )
    monkeypatch.setattr(
        bs, "ensure_registry_defaults",
        lambda workspace, log=print: order.append("registry") or (
            registry_defaults.append(workspace) or {"records": [{"name": "hr-database"}],
                                                      "created": 1, "approved": 1,
                                                      "unavailable_reason": ""}),
    )

    summary = bs.run_bootstrap("us-west-2")

    assert order == ["gateway", "registry"]
    (workspace,) = registry_defaults
    assert workspace.id == "default" and workspace.account_id == "111"
    assert workspace.region == "us-west-2"
    assert workspace.resources["gateway_url"] == "https://gateway.example/mcp"
    assert workspace.resources["artifacts_bucket"] == "bucket"
    assert summary["registry"]["defaults"]["created"] == 1


def test_run_bootstrap_without_a_gateway_still_runs_the_registry_step(registry_defaults,
                                                                     monkeypatch):
    monkeypatch.setattr(bs, "get_stack_outputs", _two_stack_outputs(_hub_outputs(), None))
    monkeypatch.setattr(bs, "_client", lambda service, _region: _hub_clients()[service])
    _cumulative_write_config(monkeypatch)

    summary = bs.run_bootstrap("us-west-2")

    (workspace,) = registry_defaults
    assert "gateway_url" not in workspace.resources
    assert summary["registry"]["defaults"] == STUB_DEFAULTS


class TestEnsureRegistryDefaults:
    """The real step, with the console import stubbed: it installs the local backend in
    the CLI process, counts, and degrades instead of failing the bootstrap."""

    def _ws(self):
        from tests.conftest import ws_ctx

        return ws_ctx({"artifacts_bucket": "bucket", "gateway_url": "https://gw/mcp"},
                      id="default")

    def test_registers_the_local_backend_when_the_process_has_none(self, monkeypatch):
        from app.core import db as core_db
        from app.services import registry_backend, registry_console
        from app.services.registry_local import LocalRegistryBackend

        registry_backend.set_backend(None)
        inits: list[bool] = []
        monkeypatch.setattr(core_db, "init_db", lambda bind=None: inits.append(True))
        seen: dict = {}
        monkeypatch.setattr(
            registry_console, "ensure_default_records",
            lambda ws, log=None, auto_approve=False: seen.update(auto_approve=auto_approve) or [
            {"name": "hr-database", "type": "MCP", "record_id": "r1", "created": True,
             "status": "APPROVED"},
            {"name": "office-facts", "type": "MCP", "record_id": "r2", "created": False,
             "status": "APPROVED"},
            {"name": "expense-report-writer", "type": "AGENT_SKILLS", "record_id": "r3",
             "created": True, "status": "PENDING_APPROVAL"},
            ],
        )
        lines: list[str] = []

        out = _real_ensure_registry_defaults(self._ws(), log=lines.append)

        assert inits == [True]
        # N17-obs257: host bootstrap may approve the platform's own targets
        assert seen == {"auto_approve": True}
        assert isinstance(registry_backend.backend_or_none(), LocalRegistryBackend)
        assert (out["created"], out["approved"], out["unavailable_reason"]) == (2, 2, "")
        assert [r["name"] for r in out["records"]] == [
            "hr-database", "office-facts", "expense-report-writer"]
        assert lines[-1] == "registry defaults: 3 record(s) · 2 new · 2 approved"

    def test_reuses_an_already_registered_backend(self, monkeypatch):
        from app.core import db as core_db
        from app.services import registry_backend, registry_console

        sentinel = object()
        registry_backend.set_backend(sentinel)  # type: ignore[arg-type]
        monkeypatch.setattr(core_db, "init_db", lambda bind=None: pytest.fail("init_db ran"))
        monkeypatch.setattr(
            registry_console, "ensure_default_records", lambda ws, log=None, auto_approve=False: []
        )

        assert _real_ensure_registry_defaults(self._ws(), log=lambda _m: None)["created"] == 0
        assert registry_backend.backend_or_none() is sentinel

    def test_degrades_with_the_reason_when_the_import_fails(self, monkeypatch):
        from app.services import registry_backend, registry_console

        registry_backend.set_backend(object())  # type: ignore[arg-type]

        def boom(ws, log=None, auto_approve=False):
            raise RuntimeError("gateway.rpc_error: tools/list refused")

        monkeypatch.setattr(registry_console, "ensure_default_records", boom)
        lines: list[str] = []

        out = _real_ensure_registry_defaults(self._ws(), log=lines.append)

        assert out["records"] == [] and out["created"] == 0
        assert out["unavailable_reason"].startswith("RuntimeError: gateway.rpc_error")
        assert "POST /api/registry/sync-defaults" in lines[-1]

    def test_skips_when_the_registry_feature_is_off(self, monkeypatch):
        from types import SimpleNamespace

        from app.services import registry_console

        monkeypatch.setattr(bs, "get_settings", lambda: SimpleNamespace(features={"agents"}))
        monkeypatch.setattr(registry_console, "ensure_default_records",
                            lambda ws, log=None, auto_approve=False: pytest.fail("import ran"))

        out = _real_ensure_registry_defaults(self._ws(), log=lambda _m: None)

        assert out["unavailable_reason"] == "registry feature disabled in Settings.features"


# ───────────── N11-obs11: AccessDenied retry around bootstrap stages ─────────────


def _denied(code="AccessDeniedException"):
    return ClientError({"Error": {"Code": code, "Message": "not authorized"}}, "GetFoo")


class TestRetryAccessDenied:
    def test_a_denial_that_clears_is_retried_once_after_the_back_off(self):
        calls: list[int] = []
        slept: list[float] = []
        lines: list[str] = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise _denied()
            return "ok"

        out = bs.retry_access_denied(flaky, label="stage gateway", sleeper=slept.append,
                                     log=lines.append)

        assert out == "ok" and len(calls) == 2
        assert slept == [bs.ACCESS_DENIED_DELAY_S]
        assert 10 <= bs.ACCESS_DENIED_DELAY_S <= 20 and bs.ACCESS_DENIED_ATTEMPTS == 3
        assert lines == ["stage gateway: AccessDenied right after an IAM change (propagation)"
                         " — retry 1/2 in 15s"]

    def test_gives_up_after_the_last_attempt_with_the_original_error(self):
        calls: list[int] = []
        slept: list[float] = []

        def denied():
            calls.append(1)
            raise _denied("AccessDenied")

        with pytest.raises(ClientError) as info:
            bs.retry_access_denied(denied, label="x", sleeper=slept.append, log=lambda _m: None)

        assert info.value.response["Error"]["Code"] == "AccessDenied"
        assert len(calls) == 3 and len(slept) == 2

    def test_an_assume_role_denial_is_not_retried_as_propagation(self):
        """N11-obs44a: STS refusing sts:AssumeRole is a missing policy/trust statement,
        not the stage's own IAM change settling — raised at once, no back-off, no line."""
        calls: list[int] = []
        slept: list[float] = []
        lines: list[str] = []

        def denied():
            calls.append(1)
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "m"}},
                              aws_clients.ASSUME_ROLE_OPERATION)

        with pytest.raises(ClientError) as info:
            bs.retry_access_denied(denied, label="stage memory", sleeper=slept.append,
                                   log=lines.append)

        assert info.value.operation_name == "AssumeRole"
        assert len(calls) == 1 and slept == [] and lines == []

    def test_any_other_error_propagates_at_once(self):
        slept: list[float] = []

        def other():
            raise ClientError({"Error": {"Code": "ValidationError", "Message": "m"}}, "Op")

        with pytest.raises(ClientError):
            bs.retry_access_denied(other, label="x", sleeper=slept.append, log=lambda _m: None)
        with pytest.raises(RuntimeError):
            bs.retry_access_denied(lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                                   label="x", sleeper=slept.append)
        assert slept == []


def test_run_bootstrap_retries_a_stage_denied_right_after_an_iam_change(monkeypatch):
    """The hub runner re-runs a block on a transient AccessDenied (obs11): the model
    credential stage is denied once, then passes — bootstrap completes."""
    calls: list[int] = []
    slept: list[float] = []
    real = bs.ensure_provider_credentials

    def flaky(control, log=print):
        calls.append(1)
        if len(calls) == 1:
            raise _denied()
        return real(control, log=log)

    monkeypatch.setattr(bs, "ensure_provider_credentials", flaky)
    monkeypatch.setattr(bs, "get_stack_outputs", _two_stack_outputs(_hub_outputs(), None))
    monkeypatch.setattr(bs, "_client", lambda service, _region: _hub_clients()[service])
    _cumulative_write_config(monkeypatch)

    summary = bs.run_bootstrap("us-west-2", sleeper=slept.append)

    assert len(calls) == 2 and slept == [bs.ACCESS_DENIED_DELAY_S]
    assert summary["model_credential"]["name"] == "launchpad-global-bedrock"



# ───────────── N12: the `evaluation` stage installs the worker preset idempotently ─────────────


class TestEnsureEvalWorker:
    """The real stage with the deploy job stubbed: the ledger is brought up, the default
    workspace converged on the resource map, `install_preset` claims the row, the job runs
    synchronously, the worker's identifiers come back; a second pass changes nothing."""

    CONFIG = {"resources": {"artifacts_bucket": "bucket",
                            "execution_role_arn": "arn:aws-cn:iam::111:role/exec"}}

    def _deployer(self, arn="arn:aws-cn:bedrock-agentcore:cn-northwest-1:111:runtime/w-1"):
        from app.core.db import SessionLocal
        from app.models.ledger import Agent, Deployment, Job

        runs: list[str] = []

        def run_job(job_id: str) -> None:
            runs.append(job_id)
            with SessionLocal() as db:
                job = db.get(Job, job_id)
                agent = db.get(Agent, job.payload["agent_id"])
                agent.status, agent.arn, agent.resource_id = "active", arn, "w-1"
                deployment = db.get(Deployment, job.payload["deployment_id"])
                deployment.status = job.status = "succeeded"
                db.commit()

        run_job.runs = runs  # type: ignore[attr-defined]
        return run_job

    def test_installs_once_then_is_a_no_op(self, client, monkeypatch):
        from app.core.db import SessionLocal
        from app.models.ledger import Agent, Workspace
        from app.system_agents.presets import EVAL_WORKER
        from app.templates import eval_worker as ew

        monkeypatch.undo()  # the autouse stub — this test runs the real stage
        deployer = self._deployer()
        first = bs.ensure_eval_worker(self.CONFIG, account_id="111", region="cn-northwest-1",
                                      log=lambda _m: None, run_job=deployer)
        assert first["installed"] is True and first["created"] is True and first["changed"]
        assert first["runtime_arn"].endswith("runtime/w-1") and first["runtime_id"] == "w-1"
        assert first["release"] == ew.release_tag() and first["unavailable_reason"] == ""
        assert len(deployer.runs) == 1
        with SessionLocal() as db:
            row = db.get(Workspace, "default")
            assert row.bootstrap_status == "ready" and row.account_id == "111"
            assert row.resources["artifacts_bucket"] == "bucket"
            agent = db.get(Agent, first["agent_id"])
            assert agent.system_key == EVAL_WORKER.key and agent.method == "zip_runtime"
            assert agent.name == EVAL_WORKER.name and agent.status == "active"
            assert agent.spec["env"][ew.RELEASE_ENV] == ew.release_tag()

        second = bs.ensure_eval_worker(self.CONFIG, account_id="111", region="cn-northwest-1",
                                       log=lambda _m: None, run_job=deployer)
        assert second["installed"] is True and second["created"] is False
        assert second["changed"] is False and second["agent_id"] == first["agent_id"]
        assert len(deployer.runs) == 1  # no second job
        assert bs.eval_worker_resources(second) == {
            "eval_worker_runtime_arn": first["runtime_arn"],
            "eval_worker_agent_id": first["agent_id"],
            "eval_worker_runtime_id": "w-1", "eval_worker_release": ew.release_tag(),
            "eval_worker_unavailable_reason": "",
        }

    def test_a_stale_release_is_repaired(self, client, monkeypatch):
        from app.core.db import SessionLocal
        from app.models.ledger import Agent
        from app.templates import eval_worker as ew

        monkeypatch.undo()
        deployer = self._deployer()
        first = bs.ensure_eval_worker(self.CONFIG, account_id="111", region="cn-northwest-1",
                                      log=lambda _m: None, run_job=deployer)
        with SessionLocal() as db:  # an older checkout installed another release
            agent = db.get(Agent, first["agent_id"])
            agent.spec = {**agent.spec, "env": {ew.RELEASE_ENV: "1.0.0-000000000000"}}
            db.commit()
        second = bs.ensure_eval_worker(self.CONFIG, account_id="111", region="cn-northwest-1",
                                       log=lambda _m: None, run_job=deployer)
        assert second["changed"] is True and second["created"] is False
        assert second["release"] == ew.release_tag() and len(deployer.runs) == 2

    def test_a_failed_deploy_degrades_with_the_reason(self, client, monkeypatch):
        from app.core.db import SessionLocal
        from app.models.ledger import Agent, Job

        monkeypatch.undo()

        def failing(job_id: str) -> None:
            with SessionLocal() as db:
                job = db.get(Job, job_id)
                agent = db.get(Agent, job.payload["agent_id"])
                agent.status, agent.error = "failed", "CreateAgentRuntime: AccessDenied"
                job.status = "failed"
                db.commit()

        out = bs.ensure_eval_worker(self.CONFIG, account_id="111", region="cn-northwest-1",
                                    log=lambda _m: None, run_job=failing)
        assert out["installed"] is False and "AccessDenied" in out["unavailable_reason"]
        resources = bs.eval_worker_resources(out)
        assert resources["eval_worker_runtime_arn"] == ""
        assert "AccessDenied" in resources["eval_worker_unavailable_reason"]

    def test_workspace_requirements_block_the_install_without_a_job(self, client, monkeypatch):
        from app.system_agents import service as system_agents

        monkeypatch.undo()
        # a hub whose per-agent roles are switched off (a preset never runs on the shared role)
        monkeypatch.setattr(system_agents, "workspace_requirements", lambda row, settings=None: [
            {"code": system_agents.REQ_PER_AGENT_ROLES,
             "message": "per-agent execution roles are disabled — execution_role_arn only"}])
        deployer = self._deployer()
        out = bs.ensure_eval_worker({"resources": {"artifacts_bucket": "bucket"}},
                                    account_id="111", region="cn-northwest-1",
                                    log=lambda _m: None, run_job=deployer)
        assert out["installed"] is False and "execution_role_arn" in out["unavailable_reason"]
        assert deployer.runs == []


def test_run_bootstrap_runs_the_evaluation_stage_after_memory_and_records_its_resources(
    monkeypatch, eval_worker_stage
):
    monkeypatch.setattr(bs, "get_stack_outputs", _two_stack_outputs(_hub_outputs(), None))
    monkeypatch.setattr(bs, "_client", lambda service, _region: _hub_clients()[service])
    merged = _cumulative_write_config(monkeypatch)
    installed = {"installed": True, "created": True, "changed": True, "agent_id": "agt",
                 "runtime_arn": "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111:runtime/w",
                 "runtime_id": "w", "release": "1.0.0-abc", "unavailable_reason": ""}
    monkeypatch.setattr(bs, "ensure_eval_worker",
                        lambda config, **kwargs: eval_worker_stage.append((config, kwargs))
                        or dict(installed))

    summary = bs.run_bootstrap("cn-northwest-1")

    assert summary["evaluation"] == installed
    (config, kwargs), = eval_worker_stage
    assert kwargs == {"account_id": "111", "region": "cn-northwest-1", "log": print}
    # the stage sees the resource map written so far (bucket + role + memory keys)
    assert config["resources"]["artifacts_bucket"] == "bucket"
    assert "memory_unavailable_reason" in config["resources"]
    assert merged["resources"]["eval_worker_runtime_arn"].endswith("runtime/w")
    assert merged["resources"]["eval_worker_agent_id"] == "agt"
    assert merged["resources"]["eval_worker_release"] == "1.0.0-abc"
    assert merged["resources"]["eval_worker_unavailable_reason"] == ""


def test_run_bootstrap_degrades_when_the_evaluation_stage_raises(monkeypatch, eval_worker_stage):
    monkeypatch.setattr(bs, "get_stack_outputs", _two_stack_outputs(_hub_outputs(), None))
    monkeypatch.setattr(bs, "_client", lambda service, _region: _hub_clients()[service])
    merged = _cumulative_write_config(monkeypatch)

    def boom(config, **kwargs):
        raise RuntimeError("uv pip compile failed")

    monkeypatch.setattr(bs, "ensure_eval_worker", boom)
    summary = bs.run_bootstrap("cn-northwest-1")
    assert summary["evaluation"]["installed"] is False
    assert "uv pip compile failed" in summary["evaluation"]["unavailable_reason"]
    assert "uv pip compile failed" in merged["resources"]["eval_worker_unavailable_reason"]
    assert merged["resources"]["eval_worker_runtime_arn"] == ""
    assert summary["registry"]["id"] == "local-default"  # the rest of the bootstrap ran


def test_bootstrap_registers_the_deploy_methods_before_installing_presets():
    # N12-bug1: the bootstrap CLI does not import app.main, where the deploy methods are
    # registered by side effect; ensure_eval_worker must register them itself.
    from app.deployer import pipeline

    bs.register_deploy_methods()
    for method in ("zip_runtime", "byoc", "harness"):
        assert method in pipeline._METHODS, method


# ─── N-R6: per-provider credentials at bootstrap ────────────────────────────────────


def _credential_bootstrap(monkeypatch, control):
    outputs = {
        "ArtifactsBucketName": "b",
        "EcrRepoName": "r",
        "EcrRepoUri": "u",
        "CodeBuildProjectName": "c",
        "AgentExecutionRoleArn": "arn:aws-cn:iam::111:role/exec",
    }
    control.get_api_key_credential_provider.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException"}}, "GetApiKeyCredentialProvider"
    )
    control.create_api_key_credential_provider.side_effect = lambda **kw: {
        "credentialProviderArn": (
            "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111:token-vault/default/"
            f"apikeycredentialprovider/{kw['name']}"
        ),
        "apiKeySecretArn": "arn:aws-cn:secretsmanager:cn-northwest-1:111:secret:x",
    }
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "111"}
    clients = {"bedrock-agentcore-control": control, "sts": sts}
    monkeypatch.setattr(bs, "get_stack_outputs", _two_stack_outputs(outputs, memory_outputs=None))
    monkeypatch.setattr(bs, "_client", lambda service, _region: clients[service])
    monkeypatch.setattr(bs, "load_config", lambda: {})
    written: list[dict] = []

    def write_config(update, replace=None):
        written.append(update)
        return update

    monkeypatch.setattr(bs, "write_config", write_config)
    return written


@pytest.mark.siliconflow(registry=True)
def test_run_bootstrap_creates_the_siliconflow_provider_when_its_key_is_present(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_SILICONFLOW_API_KEY", "sf-key")
    control = MagicMock()
    written = _credential_bootstrap(monkeypatch, control)
    summary = bs.run_bootstrap("cn-northwest-1")
    assert summary["model_credential"] == {"name": "launchpad-global-bedrock", "created": True}
    assert summary["model_credentials"] == {
        "bedrock": {"name": "launchpad-global-bedrock", "created": True, "configured": True},
        "siliconflow": {"name": "launchpad-siliconflow", "created": True, "configured": True},
    }
    names = [c.kwargs["name"] for c in control.create_api_key_credential_provider.call_args_list]
    assert names == ["launchpad-global-bedrock", "launchpad-siliconflow"]
    keys = [c.kwargs["apiKey"] for c in control.create_api_key_credential_provider.call_args_list]
    assert keys == ["global-bedrock-key", "sf-key"]
    resources = next(w["resources"] for w in written
                     if "model_credentials" in w.get("resources", {}))
    assert resources["model_credentials"]["siliconflow"]["arn"].endswith(
        "apikeycredentialprovider/launchpad-siliconflow")
    assert "sf-key" not in str(written) and "sf-key" not in str(summary)


@pytest.mark.siliconflow(registry=True)
def test_run_bootstrap_skips_an_unconfigured_siliconflow_provider_and_says_so(monkeypatch, capsys):
    monkeypatch.delenv("LAUNCHPAD_SILICONFLOW_API_KEY", raising=False)
    control = MagicMock()
    _credential_bootstrap(monkeypatch, control)
    summary = bs.run_bootstrap("cn-northwest-1")
    assert summary["model_credentials"]["siliconflow"] == {
        "name": "launchpad-siliconflow", "created": False, "configured": False}
    names = [c.kwargs["name"] for c in control.create_api_key_credential_provider.call_args_list]
    assert names == ["launchpad-global-bedrock"]
    out = capsys.readouterr().out
    assert "model-credential launchpad-siliconflow not configured" in out
    assert "LAUNCHPAD_SILICONFLOW_API_KEY" in out and "launchpad-siliconflow-api-key" in out


# ── r8-nits (rev-r8 on N11-obs44c): a programming error in the stage is shown, not hidden ──


def _raise_type_error(*_a, **_kw):
    raise TypeError("ensure_memory_substrate() got an unexpected keyword argument 'x'")


def test_memory_stage_prints_the_traceback_for_a_programming_error_and_marks_the_warning(
    monkeypatch, capsys, eval_worker_stage, registry_defaults
):
    """Degrade-on-any-error also swallowed a TypeError/KeyError in the stage itself; a
    healthy probe then cleared it as "provisioning step skipped" and the traceback was
    never seen. Still never an abort — but the traceback is printed once and the summary
    warning is marked `unexpected error`, so a step that silently did NOT run is visible."""
    monkeypatch.setattr(mb, "ensure_memory_substrate", _raise_type_error)
    fake_os = _ProvisionedButFlakyOS()
    summary, merged, out = _healthy_domain_run(monkeypatch, fake_os, "", capsys)

    assert "memory stage: unexpected error in the provisioning step (TypeError)" in out
    assert "Traceback (most recent call last)" in out
    assert "TypeError: ensure_memory_substrate() got an unexpected keyword argument" in out
    assert summary["memory"]["warning"] == (
        "unexpected error: TypeError — substrate healthy, provisioning step NOT applied; "
        "fix the stage and re-run bootstrap"
    )
    assert "step skipped" not in summary["memory"]["warning"]
    assert "memory stage WARN:" in out and "unexpected error: TypeError" in out
    # the healthy probe still keeps shared memory usable …
    assert summary["memory"]["unavailable_reason"] == ""
    assert merged["memory_unavailable_reason"] == ""
    assert mb.workspace_has_shared_memory(merged)
    # … and the stages after memory ran (never-abort contract)
    assert len(eval_worker_stage) == 1 and len(registry_defaults) == 1


def test_memory_stage_programming_error_with_a_failed_probe_names_it_in_the_reason(
    monkeypatch, capsys
):
    monkeypatch.setattr(mb, "ensure_memory_substrate", _raise_type_error)
    summary, merged, out = _healthy_domain_run(monkeypatch, _DeadDomainOS(), "", capsys)

    reason = "OpenSearch FGAC provisioning failed (unexpected error: TypeError)"
    assert summary["memory"]["unavailable_reason"] == reason
    assert merged["memory_unavailable_reason"] == reason
    assert "Traceback (most recent call last)" in out and "TypeError" in out


def test_memory_stage_prints_no_traceback_for_an_operational_error(monkeypatch, capsys):
    """The N-SCN-obs6 shape (a read timeout on a healthy domain) keeps its one-line WARN."""
    summary, _merged, out = _healthy_domain_run(monkeypatch, _ProvisionedButFlakyOS(), "", capsys)
    assert summary["memory"]["warning"] == (
        "substrate healthy, provisioning step skipped: ReadTimeoutError"
    )
    assert "Traceback" not in out and "unexpected error" not in out


@pytest.mark.parametrize("exc, expected", [
    (TypeError("x"), True),
    (KeyError("MemoryDomainEndpoint"), True),
    (AttributeError("x"), True),
    (NameError("x"), True),
    (AssertionError(), True),
    (IndexError(), True),
    (ValueError("not JSON"), False),            # a non-JSON body is operational
    (urllib3.exceptions.ReadTimeoutError(None, "/x", "t"), False),
    (EndpointConnectionError(endpoint_url="https://x"), False),
    (ClientError({"Error": {"Code": "AccessDenied"}}, "AssumeRole"), False),
])
def test_is_unexpected_error_separates_programming_from_operational_errors(exc, expected):
    assert bs.is_unexpected_error(exc) is expected


# ─── D20 P1-d: the `policy` stage (engine + CloudWatch decisions log group) ─────────────


class TestPolicyStage:
    """`ensure_policy_decisions` for real, with a stub `logs` client — the autouse fixture
    above records the stage for every other test."""

    @staticmethod
    def _logs(create_code: str | None = None):
        logs = MagicMock()
        if create_code:
            logs.create_log_group.side_effect = ClientError(
                {"Error": {"Code": create_code, "Message": create_code}}, "CreateLogGroup")
        return logs

    def _run(self, monkeypatch, logs):
        requested: list[str] = []

        def client(service, _region):
            requested.append(service)
            assert service == "logs", service
            return logs

        monkeypatch.setattr(bs, "_client", client)
        lines: list[str] = []
        summary = _real_ensure_policy_decisions("cn-northwest-1", log=lines.append)
        return summary, lines, requested

    def test_creates_the_group_with_retention_and_registers_the_engine(self, monkeypatch):
        from app.services import policy_backend

        policy_backend.set_backend(None)
        logs = self._logs()
        summary, lines, requested = self._run(monkeypatch, logs)
        assert requested == ["logs"]
        logs.create_log_group.assert_called_once_with(logGroupName="/launchpad/policy/decisions")
        logs.put_retention_policy.assert_called_once_with(
            logGroupName="/launchpad/policy/decisions", retentionInDays=90)
        assert summary == {
            "engine": "cedarpy", "mode": "log_only", "active_policies": 0,
            "log_group": "/launchpad/policy/decisions", "created": True, "retention_days": 90,
            "unavailable_reason": "", "skipped": False,
        }
        assert policy_backend.backend_or_none() is not None  # the CLI process has one now
        assert lines == ["policy decisions log group /launchpad/policy/decisions created · "
                         "retention 90 d"]
        assert bs.policy_resources(summary) == {
            "policy_log_group": "/launchpad/policy/decisions",
            "policy_log_group_unavailable_reason": "",
        }

    def test_second_run_reuses_the_group(self, monkeypatch):
        summary, lines, _ = self._run(monkeypatch, self._logs("ResourceAlreadyExistsException"))
        assert summary["created"] is False and summary["unavailable_reason"] == ""
        assert "exists" in lines[0]

    def test_counts_the_default_workspace_active_policies(self, monkeypatch):
        from app.core.db import SessionLocal
        from app.services import aws_clients, policy_backend, policy_store
        from app.services.policy_cedar import CedarEngine
        from tests.conftest import ws_ctx
        from tests.policy_fakes import BASELINE, FakeS3

        policy_backend.set_backend(CedarEngine())
        monkeypatch.setattr(aws_clients, "client", lambda service, ctx, **kw: FakeS3())
        db = SessionLocal()
        try:
            row = policy_store.create_policy(db, "default", name="baseline",
                                             statement=BASELINE, operator="t")
            policy_store.publish_policy(db, ws_ctx({"artifacts_bucket": "b"}), row.id,
                                        operator="t")
        finally:
            db.close()
        summary, _, _ = self._run(monkeypatch, self._logs())
        assert summary["active_policies"] == 1 and summary["engine"] == "cedarpy"

    def test_a_denied_create_degrades_with_the_grant_in_the_warn_line(self, monkeypatch):
        summary, lines, _ = self._run(monkeypatch, self._logs("AccessDeniedException"))
        assert summary["unavailable_reason"] == "AccessDeniedException"
        assert summary["engine"] == "cedarpy"  # the engine still registers
        assert lines and lines[0].startswith("policy stage WARN:")
        assert "logs:CreateLogGroup" in lines[0] and "log-group:/launchpad/policy/*" in lines[0]
        assert bs.policy_resources(summary) == {
            "policy_log_group": "",
            "policy_log_group_unavailable_reason": "AccessDeniedException",
        }

    def test_skips_when_the_policy_feature_is_off(self, monkeypatch):
        monkeypatch.setattr(bs, "get_settings", lambda: SimpleNamespace(
            features=["agents"], policy_default_mode="log_only", log_group_retention_days=90))
        monkeypatch.setattr(bs, "_client", lambda *_a: pytest.fail("no client when skipped"))
        lines: list[str] = []
        summary = _real_ensure_policy_decisions("cn-northwest-1", log=lines.append)
        assert summary["skipped"] is True and summary["unavailable_reason"] == "policy feature off"
        assert summary["log_group"] == "" and "skipped" in lines[0]
        assert bs.policy_resources(summary) == {
            "policy_log_group": "", "policy_log_group_unavailable_reason": "policy feature off"}

    def test_engine_registration_reports_a_missing_wheel_instead_of_crashing(self, monkeypatch):
        from app.services import policy_backend, policy_cedar

        policy_backend.set_backend(None)

        class Boom:
            def __init__(self):
                raise ImportError("No module named cedarpy")

        monkeypatch.setattr(policy_cedar, "CedarEngine", Boom)
        assert bs.ensure_policy_engine().startswith("unavailable: ImportError")
        assert policy_backend.backend_or_none() is None

    def test_run_bootstrap_records_the_policy_stage_after_observability(self, monkeypatch,
                                                                         policy_stage):
        """The stage runs on every hub bootstrap (gateway or not) and its resource keys land
        in the map; the summary row is `summary["policy"]`."""
        outputs = _hub_outputs()
        monkeypatch.setattr(bs, "get_stack_outputs", _two_stack_outputs(outputs, None))
        monkeypatch.setattr(bs, "_client", lambda service, _region: _hub_clients()[service])
        merged = _cumulative_write_config(monkeypatch)
        summary = bs.run_bootstrap("us-west-2")
        assert policy_stage == ["us-west-2"]
        assert summary["policy"] == POLICY_STAGE_STUB
        assert merged["resources"]["policy_log_group"] == "/launchpad/policy/decisions"
        assert merged["resources"]["policy_log_group_unavailable_reason"] == ""


# ── D21 (M1-b): the `model-providers` stage — registry rows whose key is an SSM parameter ──


def _registry_bootstrap(monkeypatch, control, ssm):
    """`_credential_bootstrap` plus an `ssm` client (the registry pass reads
    `launchpad-<provider>-api-key` through the funnel)."""
    written = _credential_bootstrap(monkeypatch, control)
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "111"}
    clients = {"bedrock-agentcore-control": control, "sts": sts, "ssm": ssm}
    monkeypatch.setattr(bs, "_client", lambda service, _region: clients[service])
    return written


def _siliconflow_row():
    from sqlalchemy import select

    from app.core.db import SessionLocal
    from app.models.model_registry import ModelProvider

    db = SessionLocal()
    try:
        row = db.scalar(select(ModelProvider).where(ModelProvider.name == "siliconflow"))
        return (row.key_version, row.key_modified_at) if row is not None else (None, None)
    finally:
        db.close()


@pytest.mark.siliconflow(registry=True)
def test_model_providers_stage_registers_an_ssm_backed_provider_without_a_host_env(
    monkeypatch, capsys
):
    """The built-in SiliconFlow row is `credential_source: ssm`: with the parameter stored
    (and no `LAUNCHPAD_SILICONFLOW_API_KEY` on the host) the stage reads it, creates the
    Identity provider, records `source: ssm` + the parameter version on the hub config and
    the ledger row — and never prints or writes the value."""
    monkeypatch.delenv("LAUNCHPAD_SILICONFLOW_API_KEY", raising=False)
    control = MagicMock()
    ssm = MagicMock()
    ssm.get_parameter.return_value = {"Parameter": {
        "Name": "launchpad-siliconflow-api-key", "Value": "sf-KEY-0123456789", "Version": 7}}
    written = _registry_bootstrap(monkeypatch, control, ssm)
    summary = bs.run_bootstrap("cn-northwest-1")
    assert summary["model_providers"] == {"configured": ["siliconflow"], "not_configured": [],
                                          "unavailable_reason": ""}
    # the env pass reported it unconfigured; the registry pass configured it from SSM
    assert summary["model_credentials"]["siliconflow"]["configured"] is False
    ssm.get_parameter.assert_called_once_with(Name="launchpad-siliconflow-api-key",
                                              WithDecryption=True)
    names = [c.kwargs["name"] for c in control.create_api_key_credential_provider.call_args_list]
    assert names == ["launchpad-global-bedrock", "launchpad-siliconflow"]
    assert control.create_api_key_credential_provider.call_args_list[-1].kwargs["apiKey"] == (
        "sf-KEY-0123456789")
    records = [w["resources"]["model_credentials"]["siliconflow"] for w in written
               if "siliconflow" in (w.get("resources") or {}).get("model_credentials", {})]
    assert records[-1] == {"name": "launchpad-siliconflow", "configured": True, "key_version": 7,
                           "source": "ssm", "arn": records[-1]["arn"],
                           "secret_arn": records[-1]["secret_arn"]}
    assert records[-1]["arn"].endswith("apikeycredentialprovider/launchpad-siliconflow")
    assert _siliconflow_row()[0] == 7
    out = capsys.readouterr().out
    assert "api-key credential provider launchpad-siliconflow created" in out
    assert "sf-KEY-0123456789" not in out and "sf-KEY-0123456789" not in json.dumps(written)
    assert "sf-KEY-0123456789" not in json.dumps(summary)


@pytest.mark.siliconflow(registry=True)
def test_model_providers_stage_reports_a_missing_parameter_and_an_unreadable_one(
    monkeypatch, capsys
):
    monkeypatch.delenv("LAUNCHPAD_SILICONFLOW_API_KEY", raising=False)
    control = MagicMock()
    ssm = MagicMock()
    ssm.get_parameter.side_effect = ClientError(
        {"Error": {"Code": "ParameterNotFound", "Message": "not found"}}, "GetParameter")
    _registry_bootstrap(monkeypatch, control, ssm)
    summary = bs.run_bootstrap("cn-northwest-1")
    assert summary["model_providers"] == {"configured": [], "not_configured": ["siliconflow"],
                                          "unavailable_reason": ""}
    names = [c.kwargs["name"] for c in control.create_api_key_credential_provider.call_args_list]
    assert names == ["launchpad-global-bedrock"]
    out = capsys.readouterr().out
    assert "launchpad-siliconflow-api-key does not exist" in out
    assert _siliconflow_row() == (None, None)

    ssm.get_parameter.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "GetParameter")
    summary = bs.run_bootstrap("cn-northwest-1")
    assert summary["model_providers"]["not_configured"] == ["siliconflow"]
    assert "could not be read (AccessDeniedException" in capsys.readouterr().out


def test_model_providers_stage_degrades_when_the_pass_itself_fails(monkeypatch, capsys):
    """A failure of the pass (here: no `ssm` client at all) is a line + `unavailable_reason`,
    never an aborted bootstrap — the later stages still run and the summary is complete."""
    monkeypatch.delenv("LAUNCHPAD_SILICONFLOW_API_KEY", raising=False)
    control = MagicMock()
    _credential_bootstrap(monkeypatch, control)  # `_client` knows no "ssm"
    summary = bs.run_bootstrap("cn-northwest-1")
    assert summary["model_providers"]["configured"] == []
    assert summary["model_providers"]["unavailable_reason"].startswith("KeyError")
    assert summary["registry"]["id"] == "local-default"  # a later stage ran
    assert "model-providers stage skipped" in capsys.readouterr().out


# ── N17-obs233: the model-providers stage meets the OLD ledger schema on a host update ──────
def _hub_run(monkeypatch, *, ssm=None):
    """`run_bootstrap` on the hub stubs with an `ssm` client in the map (the model-providers
    stage builds one); returns the summary."""
    control = MagicMock()
    control.get_api_key_credential_provider.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException"}}, "GetApiKeyCredentialProvider"
    )
    control.create_api_key_credential_provider.return_value = {
        "credentialProviderArn": "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111:token-vault/"
                                 "default/apikeycredentialprovider/launchpad-global-bedrock",
        "apiKeySecretArn": "arn:aws-cn:secretsmanager:cn-northwest-1:111:secret:x",
    }
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "111"}
    clients = {"bedrock-agentcore-control": control, "sts": sts, "ssm": ssm or MagicMock()}
    outputs = {"ArtifactsBucketName": "b", "EcrRepoName": "r", "EcrRepoUri": "u",
               "CodeBuildProjectName": "c", "AgentExecutionRoleArn": "arn:aws-cn:iam::111:role/x"}
    monkeypatch.setattr(bs, "get_stack_outputs", _two_stack_outputs(outputs, memory_outputs=None))
    monkeypatch.setattr(bs, "_client", lambda service, _region: clients[service])
    monkeypatch.setattr(bs, "load_config", lambda: {})
    monkeypatch.setattr(bs, "write_config", lambda update, replace=None: update)
    return bs.run_bootstrap("cn-northwest-1")


def _drop_column(table: str, column: str) -> None:
    from sqlalchemy import inspect, text

    from app.core.db import engine

    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
    assert column not in {c["name"] for c in inspect(engine).get_columns(table)}


def test_the_model_providers_stage_migrates_the_ledger_before_reading_it(monkeypatch, capsys):
    """Both hosts logged `model-providers stage skipped — OperationalError: no such column:
    model_providers.key_last4` on the 2026-10-21 host update: `make bootstrap` (step 10) ran
    the NEW code against the ledger the OLD backend still served (restart = step 13). The stage
    now runs `init_db` — the startup migration — first, so the column exists when it reads."""
    from sqlalchemy import inspect

    from app.core.db import engine, init_db

    init_db()
    _drop_column("model_providers", "key_last4")  # the ledger a pre-obs203 backend created
    summary = _hub_run(monkeypatch)
    out = capsys.readouterr().out
    assert "model-providers stage skipped" not in out, out
    assert summary["model_providers"]["unavailable_reason"] == ""
    assert "key_last4" in {c["name"] for c in inspect(engine).get_columns("model_providers")}


def test_a_column_still_missing_skips_the_stage_with_one_line_naming_it(monkeypatch, capsys):
    """Belt for a column the migration did not add: no traceback, one INFO-style line that
    names the column and says the schema is applied at the next start."""
    from sqlalchemy.exc import OperationalError

    def raise_missing(control, ssm, log=print):
        raise OperationalError("SELECT model_providers.key_last4", {},
                               Exception("no such column: model_providers.key_last4"))

    monkeypatch.setattr(bs, "ensure_registry_credentials", raise_missing)
    summary = _hub_run(monkeypatch)
    out = capsys.readouterr().out
    assert ("model-providers stage skipped — ledger column model_providers.key_last4 missing "
            "(applied at next start)") in out
    assert "Traceback" not in out
    assert summary["model_providers"] == {
        "configured": [], "not_configured": [],
        "unavailable_reason": "ledger column model_providers.key_last4 missing "
                              "(applied at next start)",
    }
    # the later stages still ran
    assert "registry_id" in summary or "model_credential" in summary


def test_missing_column_of_reads_only_the_sqlite_shape():
    from sqlalchemy.exc import OperationalError

    exc = OperationalError("stmt", {}, Exception("no such column: agents.policy_applied"))
    assert bs.missing_column_of(exc) == "agents.policy_applied"
    assert bs.missing_column_of(RuntimeError("database is locked")) is None


def test_ensure_ledger_schema_refreshes_the_settings_before_seeding(monkeypatch):
    """rev-r22 (obs233 nit): the seed reads Settings, which this run's `write_config` calls may
    have changed on disk — a stale cache must not be what the default workspace is seeded from."""
    from app.core.config import get_settings

    before = get_settings()
    monkeypatch.setenv("LAUNCHPAD_REGION", "cn-north-1")
    assert get_settings() is before  # still the cached object
    bs.ensure_ledger_schema()
    try:
        assert get_settings().region == "cn-north-1" and get_settings() is not before
    finally:
        monkeypatch.delenv("LAUNCHPAD_REGION")
        get_settings.cache_clear()
