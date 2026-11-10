"""Shared fakes for the D20 Policy tests: an S3 stub for the publish copy and a
``FakeEngine`` that implements the ``PolicyEngine`` Protocol without cedarpy."""

from __future__ import annotations

from io import BytesIO
from typing import Any

from botocore.exceptions import ClientError

from app.services.policy_backend import AuthzDecision, AuthzRequest, ValidationReport


class FakeS3:
    """Records ``put_object`` calls; ``fail_with`` makes the next call raise."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.calls: list[dict[str, Any]] = []
        self.fail_with: str | None = None

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.fail_with:
            code, self.fail_with = self.fail_with, None
            raise ClientError({"Error": {"Code": code, "Message": code}}, "PutObject")
        key = (kwargs["Bucket"], kwargs["Key"])
        if kwargs.get("IfNoneMatch") == "*" and key in self.objects:
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed", "Message": "exists"}}, "PutObject"
            )
        self.objects[key] = kwargs["Body"]
        return {"ETag": '"fake"'}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        """``{"Body": <reader>}`` for a stored key; ``NoSuchKey`` otherwise (A6 engine-down
        reads the agent's last guard bundle through this)."""
        key = (kwargs["Bucket"], kwargs["Key"])
        if key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject")
        body = self.objects[key]
        return {"Body": BytesIO(body if isinstance(body, bytes) else str(body).encode("utf-8"))}


class FakeEngine:
    """Deterministic engine: ``decide`` is the decision every ``authorize`` returns;
    ``raises`` makes it blow up; ``valid`` controls ``validate``."""

    def __init__(self, decide: str = "allow", *, valid: bool = True, raises: bool = False):
        self.decide = decide
        self.valid = valid
        self.raises = raises
        self.requests: list[AuthzRequest] = []

    def validate(self, statement: str, schema: str) -> ValidationReport:
        return ValidationReport(ok=self.valid, policy_ids=["policy0"])

    def format(self, statement: str) -> str:
        return statement.strip() + "\n"

    def compile(self, statement: str) -> Any:
        return ("compiled", statement)

    def authorize(self, request, policies, entities) -> AuthzDecision:
        if self.raises:
            raise RuntimeError("engine exploded")
        self.requests.append(request)
        no_decision = self.decide == "no_decision"
        return AuthzDecision(decision=self.decide, reasons=[] if no_decision else ["policy0"],
                             errors=["boom"] if no_decision else [])

    def authorize_batch(self, requests, policies, entities) -> list[AuthzDecision]:
        return [self.authorize(r, policies, entities) for r in requests]

    def policy_ids(self, statement: str) -> list[str]:
        return ["policy0"]


# Global parity (contract amendment 9): the default-deny baseline is the two `call_tool`
# permits — every human / system principal may call tools (Stage BT: statements #0–#2) and
# every agent of the `default` workspace may call its declared tools (Stage A: #3). There is
# no `invoke` action. The first line is one complete statement (tests annotate it alone).
BASELINE = '''
permit(principal in Launchpad::Role::"member", action == Launchpad::Action::"call_tool", resource);
permit(principal in Launchpad::Role::"admin", action == Launchpad::Action::"call_tool", resource);
permit(principal == Launchpad::System::"hub", action == Launchpad::Action::"call_tool", resource);
permit(principal in Launchpad::Workspace::"default",
       action == Launchpad::Action::"call_tool", resource);
'''

FORBID_KEY = '''
forbid(principal == Launchpad::ApiKey::"__KEY__",
       action == Launchpad::Action::"call_tool", resource);
'''

TOOLS_ADMIN_ONLY = '''
permit(principal, action == Launchpad::Action::"call_tool", resource)
  when { resource.name like "hr-database___*" };
forbid(principal, action == Launchpad::Action::"call_tool",
       resource == Launchpad::Tool::"hr-database___create_payout")
  unless { principal in Launchpad::Role::"admin" };
'''

BROKEN = "permit(principal, action, resource) when { "
