"""IAM service principals for the CDK stacks.

Service-principal names are partition-invariant: IAM in the aws-cn partition
accepts ``bedrock-agentcore`` + ``amazonaws.com`` and rejects
the ``.com.cn`` form with "Invalid principal in policy"
(N11-bug5, observed live in cn-northwest-1, 2026-10-10). Only resource ARNs
and endpoint hostnames differ by partition (``self.partition`` /
``self.url_suffix``); never build a principal from ``url_suffix``.
"""

from aws_cdk import aws_iam as iam


def service_principal(service: str, **kwargs) -> iam.ServicePrincipal:
    """Return the principal for ``service`` (e.g. ``"bedrock-agentcore"``).

    ``kwargs`` (``conditions=...``) pass straight through to ``iam.ServicePrincipal``.
    """
    return iam.ServicePrincipal(f"{service}.amazonaws.com", **kwargs)  # cn-lint: allow
