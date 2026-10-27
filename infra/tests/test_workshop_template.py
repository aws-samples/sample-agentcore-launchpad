"""The Workshop Studio host template (`docs/workshop3/static/infrastructure/workshop-ec2.yaml`).

Nothing else in `make verify` reads it, and the one limit that has broken it is
invisible until a live `create-stack` reaches RunInstances: EC2 accepts at most
16384 bytes of RAW UserData (before base64, after `!Sub`), and refuses the
instance with "User data is limited to 16384 bytes". The script grows a line at
a time and has crossed that line before (issue #279), so this suite renders it
the way CloudFormation will and measures it.

The script is read straight from the text rather than through CDK's
`CfnInclude` (as test_spoke_template.py does): `CfnInclude` rejects this
template's `AWS::Logs::ResourcePolicy`, whose `PolicyDocument` is written as a
mapping, which CloudFormation itself accepts. No YAML dependency is added; the
UserData body is a literal block scalar, whose extent is defined by indentation.
"""

import re
import uuid
from pathlib import Path

TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "workshop3"
    / "static"
    / "infrastructure"
    / "workshop-ec2.yaml"
)

EC2_USER_DATA_LIMIT = 16384
# CloudFormation's own cap on a stack name, the one free-length value the script
# interpolates that no parameter constraint bounds.
MAX_STACK_NAME = 128


def _user_data_script(text: str) -> str:
    """The `Fn::Base64: !Sub |` literal block, as YAML reads it: content lines
    are de-indented by the first line's indent, the block ends at the first
    non-blank line indented less, and `|` keeps exactly one trailing newline."""
    head = re.search(r"^( *)Fn::Base64: !Sub \|\n", text, re.M)
    assert head, "UserData is expected to be a single `Fn::Base64: !Sub |` block"
    lines = text[head.end():].splitlines()
    indent = len(lines[0]) - len(lines[0].lstrip())
    assert indent > len(head.group(1))
    body = []
    for line in lines:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        body.append(line[indent:])
    return "\n".join(body).rstrip("\n") + "\n"


def _parameter_default(text: str, name: str) -> str:
    match = re.search(rf"^  {name}:\n(?:    .*\n)*?    Default: (\S+)$", text, re.M)
    assert match, f"parameter {name} has no Default"
    return match.group(1)


def _render(script: str, values: dict[str, str]) -> str:
    """`Fn::Sub` on a string body: `${Name}` is replaced, `${!Name}` is the
    escape that renders as a literal `${Name}`."""

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name.startswith("!"):
            return "${" + name[1:] + "}"
        assert name in values, f"UserData references ${{{name}}}, which the test does not model"
        return values[name]

    return re.sub(r"\$\{([^}]+)\}", substitute, script)


def test_user_data_fits_the_ec2_limit_with_worst_case_substitutions():
    text = TEMPLATE.read_text(encoding="utf-8")
    mapping = re.search(r"^  CloudFrontPrefixList:\n((?:    .*\n)+)", text, re.M)
    assert mapping, "CloudFrontPrefixList mapping not found"
    region = max(re.findall(r"^    ([a-z0-9-]+):$", mapping.group(1), re.M), key=len)
    stack_name = "s" * MAX_STACK_NAME
    values = {
        "AWS::Region": region,
        "AWS::StackName": stack_name,
        "AWS::StackId": (
            f"arn:aws:cloudformation:{region}:123456789012:stack/{stack_name}/{uuid.uuid4()}"
        ),
        "AWS::AccountId": "123456789012",
        "AWS::Partition": "aws",
        "GitBranch": _parameter_default(text, "GitBranch"),
        "GitRepositoryUrl": _parameter_default(text, "GitRepositoryUrl"),
    }

    rendered = len(_render(_user_data_script(text), values).encode())
    assert rendered <= EC2_USER_DATA_LIMIT, (
        f"rendered UserData is {rendered} bytes; EC2 accepts {EC2_USER_DATA_LIMIT}. "
        "Move explanation out of the script into the YAML comments above UserData."
    )
