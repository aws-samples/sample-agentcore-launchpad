"""A minimal IAM evaluator over the CN hub's EC2 CloudFormation template (N17-obs117).

Renders the instance role's identity policy of ``infra/ec2/launchpad-cn-ec2.yaml`` for one
``(stack region, account, parameters)`` — ``!Sub`` with the pseudo parameters, ``!If`` with the
template's ``Conditions`` evaluated from the parameter map, ``AWS::NoValue`` dropped from lists —
into plain ``Statement`` rows, and answers "may this role perform ``action`` on ``resource``?" the
way IAM does for an identity policy without conditions: an explicit Deny wins, else any Allow whose
action glob and resource glob both match, else implicit deny. Globs are IAM's (``*`` any run of
characters, ``?`` one); action matching is case-insensitive, resource matching is not.

Conditions (N17-obs201): a statement's ``Condition`` block is modelled for the one shape the
template uses — ``StringLike`` / ``StringEquals`` on ``iam:ResourceTag/<key>`` — against the
``resource_tags`` the caller passes (the tags the resource carries when the call is made; a
bootstrap-made role is tagged in its ``CreateRole`` call). A missing tag key fails a positive
operator the way IAM does; any other operator or key raises ``NotImplementedError`` rather than
guessing. What this deliberately does NOT model: every other condition operator / key, permission
boundaries, SCPs and resource policies. The two AWS-managed policies ARE
modelled: ``AdministratorAccess`` (one ``Allow * on *`` statement — the ``HubAdminAccess=true``
path N17-obs112 was ruled onto) and ``AmazonSSMManagedInstanceCore`` (``SSM_CORE_ACTIONS`` on
``*`` — the SSM agent's grant, which includes ``ssm:GetParameter`` / ``ssm:GetParameters`` and so
DOES cover the model-credential stage's registry pass in every region; rev-r17 nit).
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_PATH = REPO_ROOT / "infra" / "ec2" / "launchpad-cn-ec2.yaml"

_NO_VALUE = object()


# ---------------------------------------------------------------------------
# loading (the cfn-flip trick: every `!Tag` → {"Fn::Tag": …} / {"Ref": …})
# ---------------------------------------------------------------------------


def _cfn_multi_constructor(loader: yaml.SafeLoader, tag_suffix: str, node: Any) -> Any:
    tag = "Ref" if tag_suffix == "Ref" else f"Fn::{tag_suffix}"
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    return {tag: value}


class _CfnLoader(yaml.SafeLoader):
    pass


_CfnLoader.add_multi_constructor("!", _cfn_multi_constructor)


def load_template(path: Path = TEMPLATE_PATH) -> dict[str, Any]:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_CfnLoader)


# ---------------------------------------------------------------------------
# rendering intrinsic functions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scope:
    """The values a render sees: pseudo parameters + the stack's parameters."""

    region: str
    account: str
    partition: str
    parameters: dict[str, str]
    conditions: dict[str, Any]
    stack_name: str = "launchpad-cn-ec2"

    def ref(self, name: str) -> Any:
        # the partition→suffix table lives in aws_clients; mirrored for the pseudo parameter
        suffix = "amazonaws.com" + (".cn" if self.partition == "aws-cn" else "")  # cn-lint: allow
        pseudo = {
            "AWS::Region": self.region,
            "AWS::AccountId": self.account,
            "AWS::Partition": self.partition,
            "AWS::StackName": self.stack_name,
            "AWS::URLSuffix": suffix,
        }
        if name == "AWS::NoValue":
            return _NO_VALUE
        if name in pseudo:
            return pseudo[name]
        if name in self.parameters:
            return self.parameters[name]
        raise KeyError(f"unresolvable Ref {name!r} (not a parameter or pseudo parameter)")


_SUB_TOKEN = re.compile(r"\$\{([^}]+)\}")


def _sub(text: str, scope: Scope, variables: dict[str, Any]) -> str:
    def repl(match: re.Match[str]) -> str:
        token = match.group(1)
        if token.startswith("!"):
            return "${" + token[1:] + "}"
        if token in variables:
            return str(render(variables[token], scope))
        return str(scope.ref(token))

    return _SUB_TOKEN.sub(repl, text)


def condition_value(name: str, scope: Scope) -> bool:
    return bool(render(scope.conditions[name], scope))


def render(node: Any, scope: Scope) -> Any:
    """Resolve the intrinsic functions the template's IAM section uses. Lists drop
    ``AWS::NoValue`` members (the template relies on that for optional ARNs)."""
    if isinstance(node, list):
        out = []
        for item in node:
            value = render(item, scope)
            if value is not _NO_VALUE:
                out.append(value)
        return out
    if not isinstance(node, dict):
        return node
    if len(node) == 1:
        (key, value), = node.items()
        if key == "Ref":
            return scope.ref(str(value))
        if key == "Fn::Sub":
            if isinstance(value, list):
                text, variables = value
                return _sub(str(text), scope, dict(variables or {}))
            return _sub(str(value), scope, {})
        if key == "Fn::If":
            cond, when_true, when_false = value
            chosen = when_true if condition_value(str(cond), scope) else when_false
            return render(chosen, scope)
        if key == "Fn::Equals":
            a, b = (render(v, scope) for v in value)
            return str(a) == str(b)
        if key == "Fn::Not":
            return not render(value[0], scope)
        if key == "Fn::And":
            return all(render(v, scope) for v in value)
        if key == "Fn::Or":
            return any(render(v, scope) for v in value)
        if key == "Fn::Join":
            sep, items = value
            return str(sep).join(str(v) for v in render(items, scope))
        if key == "Condition":
            return condition_value(str(value), scope)
    return {k: render(v, scope) for k, v in node.items()}


# ---------------------------------------------------------------------------
# the role's statements
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Condition:
    """One ``<operator>: {<key>: <values>}`` entry of a statement's ``Condition`` block."""

    operator: str  # StringLike | StringEquals (the modelled subset)
    key: str  # iam:ResourceTag/<tag key>
    values: tuple[str, ...]


@dataclass(frozen=True)
class Statement:
    sid: str
    effect: str  # Allow | Deny
    actions: tuple[str, ...]
    resources: tuple[str, ...]
    conditions: tuple[Condition, ...] = ()  # all must hold (IAM ANDs the block's entries)


ADMIN_SID = "AdministratorAccess (AWS managed, HubAdminAccess=true)"
SSM_CORE_SID = "AmazonSSMManagedInstanceCore (AWS managed)"
#: the SSM half of AmazonSSMManagedInstanceCore (the ssmmessages / ec2messages channel actions
#: are omitted: no bootstrap need names them). GetParameter / GetParameters on `*` are the ones
#: that matter here.
SSM_CORE_ACTIONS: tuple[str, ...] = (
    "ssm:DescribeAssociation", "ssm:GetDeployablePatchSnapshotForInstance", "ssm:GetDocument",
    "ssm:DescribeDocument", "ssm:GetManifest", "ssm:GetParameter", "ssm:GetParameters",
    "ssm:ListAssociations", "ssm:ListInstanceAssociations", "ssm:PutInventory",
    "ssm:PutComplianceItems", "ssm:PutConfigurePackageResult", "ssm:UpdateAssociationStatus",
    "ssm:UpdateInstanceAssociationStatus", "ssm:UpdateInstanceInformation",
)


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    return (str(value),)


SUPPORTED_CONDITION_OPERATORS = ("StringLike", "StringEquals")
RESOURCE_TAG_PREFIX = "iam:ResourceTag/"


def _conditions(block: Any) -> tuple[Condition, ...]:
    """Parse a rendered ``Condition`` block; refuse anything outside the modelled subset."""
    if not block:
        return ()
    out: list[Condition] = []
    for operator, entries in dict(block).items():
        if operator not in SUPPORTED_CONDITION_OPERATORS:
            raise NotImplementedError(f"condition operator {operator!r} is not modelled")
        for key, values in dict(entries).items():
            if not str(key).startswith(RESOURCE_TAG_PREFIX):
                raise NotImplementedError(f"condition key {key!r} is not modelled")
            out.append(Condition(str(operator), str(key), _as_tuple(values)))
    return tuple(out)


def template_scope(template: dict[str, Any], *, region: str, account: str, partition: str,
                   parameters: dict[str, str] | None = None) -> Scope:
    """Every template parameter at its ``Default`` unless overridden; the parameters without a
    default (the SSM parameter NAMES the stack is deployed with, the CIDR) take the stand-in
    values of ``REQUIRED_PARAMETER_STAND_INS`` unless overridden."""
    values: dict[str, str] = dict(REQUIRED_PARAMETER_STAND_INS)
    for name, spec in template.get("Parameters", {}).items():
        if "Default" in spec:
            values[name] = str(spec["Default"])
    for name, value in (parameters or {}).items():
        if name not in template.get("Parameters", {}):
            raise KeyError(f"{name!r} is not a parameter of the template")
        values[name] = str(value)
    missing = sorted(set(template.get("Parameters", {})) - set(values))
    if missing:
        raise KeyError(f"template parameters without a default or a stand-in: {missing}")
    return Scope(region=region, account=account, partition=partition, parameters=values,
                 conditions=dict(template.get("Conditions") or {}))


#: the deploy.md §1 / §3 names for the parameters the template requires (SSM parameter names,
#: never values; the CIDR is the operator's)
REQUIRED_PARAMETER_STAND_INS: dict[str, str] = {
    "AllowedCidr": "203.0.113.7/32",
    "GlobalBedrockApiKeyParameter": "launchpad-global-bedrock-api-key",
    "ConsolePasswordParameter": "launchpad-console-password",
}


def instance_role_statements(template: dict[str, Any], scope: Scope) -> list[Statement]:
    roles = [r for r in template["Resources"].values() if r["Type"] == "AWS::IAM::Role"]
    assert len(roles) == 1, "the EC2 template declares exactly one IAM role"
    props = roles[0]["Properties"]
    out: list[Statement] = []
    for policy in render(props.get("Policies") or [], scope):
        name = str(policy.get("PolicyName") or "")
        for i, stmt in enumerate(policy["PolicyDocument"]["Statement"]):
            out.append(Statement(
                sid=str(stmt.get("Sid") or f"{name}#{i}"),
                effect=str(stmt.get("Effect") or "Allow"),
                actions=_as_tuple(stmt.get("Action")),
                resources=_as_tuple(stmt.get("Resource")),
                conditions=_conditions(stmt.get("Condition")),
            ))
    for arn in render(props.get("ManagedPolicyArns") or [], scope):
        if str(arn).endswith(":policy/AdministratorAccess"):
            out.append(Statement(ADMIN_SID, "Allow", ("*",), ("*",)))
        elif str(arn).endswith(":policy/AmazonSSMManagedInstanceCore"):
            out.append(Statement(SSM_CORE_SID, "Allow", SSM_CORE_ACTIONS, ("*",)))
    return out


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------


def _glob(pattern: str, value: str, *, fold: bool) -> bool:
    if fold:
        pattern, value = pattern.lower(), value.lower()
    # IAM wildcards only: escape fnmatch's `[` so an ARN with brackets never becomes a class
    escaped = pattern.replace("[", "[[]")
    return fnmatch.fnmatchcase(value, escaped)


def action_matches(pattern: str, action: str) -> bool:
    return _glob(pattern, action, fold=True)


def resource_matches(pattern: str, resource: str) -> bool:
    return _glob(pattern, resource, fold=False)


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    by: str | None  # the Sid that decided (an Allow that matched, or the Deny)

    @property
    def text(self) -> str:
        return ("allowed by " + self.by) if self.allowed else (
            f"DENIED by {self.by}" if self.by else "DENIED (no statement matches)")


def condition_holds(condition: Condition, resource_tags: Mapping[str, str]) -> bool:
    """``iam:ResourceTag/<key>`` against the resource's tags: a missing key is false for the
    positive operators (IAM evaluates a missing context key as not matching); ``StringLike`` is
    IAM's glob (case-sensitive), ``StringEquals`` exact."""
    tag_key = condition.key[len(RESOURCE_TAG_PREFIX):]
    if tag_key not in resource_tags:
        return False
    value = resource_tags[tag_key]
    if condition.operator == "StringLike":
        return any(_glob(pattern, value, fold=False) for pattern in condition.values)
    return value in condition.values  # StringEquals


def evaluate(statements: list[Statement], action: str, resource: str, *,
             resource_tags: Mapping[str, str] | None = None) -> Verdict:
    """Identity-policy evaluation: explicit Deny → Allow → implicit deny. A statement matches
    when its action glob, its resource glob AND every entry of its ``Condition`` block hold
    against ``resource_tags`` (N17-obs201: the tags the resource carries at call time; ``None`` =
    an untagged resource, so a conditional statement never matches)."""
    tags = dict(resource_tags or {})
    matches = [
        s for s in statements
        if any(action_matches(a, action) for a in s.actions)
        and any(resource_matches(r, resource) for r in s.resources)
        and all(condition_holds(c, tags) for c in s.conditions)
    ]
    for s in matches:
        if s.effect == "Deny":
            return Verdict(False, s.sid)
    for s in matches:
        if s.effect == "Allow":
            return Verdict(True, s.sid)
    return Verdict(False, None)
