"""Static checks on the optional console NLB template (`infra/ec2/launchpad-console-nlb.yaml`,
N12-obs52).

Plain CloudFormation, parsed with the same multi-constructor YAML loader as
`test_ec2_template.py`. cfn-lint (in `make verify`) says whether the template is *valid*;
these assertions say whether it is *safe* for the monitored account and faithful to the
user decision of 2026-10-16: exactly one listener, on `ListenerPort` (never 80/443), the
`0.0.0.0/0` ingress existing ONLY under the `OpenToInternet` access mode (both modes are
evaluated through the template's own Conditions), the host security group admitting only
the NLB, and no `arn:aws:` / Global DNS-suffix literal (spec C2).
"""

import re
from pathlib import Path

import yaml

TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "ec2" / "launchpad-console-nlb.yaml"

# the live default host (deploy-ops-2 discovery, 2026-10-16) - stands in for real values
LIVE_PARAMS = {
    "VpcId": "vpc-0123456789abcdef0",
    "PublicSubnetId": "subnet-0123456789abcdef0",
    "InstanceId": "i-0123456789abcdef0",
    "InstanceSecurityGroupId": "sg-0123456789abcdef0",
}


def _cfn_multi_constructor(loader: yaml.SafeLoader, tag_suffix: str, node):
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


def _load() -> dict:
    return yaml.load(TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=_CfnLoader)


def _resources_of_type(template: dict, type_: str) -> list[dict]:
    # Legacy/default mode remains one HTTPS listener. Optional resources are
    # exercised separately with the explicit HTTP-lab condition below.
    live = _Conditions(template, LIVE_PARAMS).live_resources(template)
    return [r for r in live.values() if r["Type"] == type_]


class _Conditions:
    """Evaluate the template's Conditions for a given parameter set - the subset of
    intrinsic functions the template uses (Ref, Equals, And, Or, Not, Condition,
    Select, Split, Join). Parameter defaults apply when a value is not given."""

    def __init__(self, template: dict, params: dict[str, str]):
        self.conditions = template["Conditions"]
        self.params = {}
        for name, spec in template["Parameters"].items():
            self.params[name] = params.get(name, spec.get("Default"))

    def _eval(self, expr):
        if not isinstance(expr, dict):
            return expr
        [(fn, arg)] = expr.items()
        if fn == "Ref":
            return self.params[arg]
        if fn in ("Fn::Condition", "Condition"):
            return self.active(arg)
        if fn == "Fn::Equals":
            a, b = (self._eval(x) for x in arg)
            return str(a) == str(b)
        if fn == "Fn::Not":
            return not self._eval(arg[0])
        if fn == "Fn::And":
            return all(self._eval(x) for x in arg)
        if fn == "Fn::Or":
            return any(self._eval(x) for x in arg)
        if fn == "Fn::Join":
            sep, items = arg
            return sep.join(str(self._eval(x)) for x in self._eval_list(items))
        if fn == "Fn::Split":
            sep, value = arg
            return str(self._eval(value)).split(sep)
        if fn == "Fn::Select":
            index, items = arg
            return self._eval_list(items)[int(self._eval(index))]
        raise AssertionError(f"unsupported intrinsic in a Condition: {fn}")

    def _eval_list(self, items):
        value = self._eval(items) if isinstance(items, dict) else items
        assert isinstance(value, list), items
        return value

    def active(self, name: str) -> bool:
        return bool(self._eval(self.conditions[name]))

    def live_resources(self, template: dict) -> dict[str, dict]:
        return {
            name: res for name, res in template["Resources"].items()
            if "Condition" not in res or self.active(res["Condition"])
        }


def test_no_partition_or_global_suffix_literals():
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "arn:aws:" not in text
    assert not re.search(r"\.amazonaws\.com([^.]|$)", text)


def test_exactly_one_listener_on_listener_port_tcp_passthrough():
    template = _load()
    [listener] = _resources_of_type(template, "AWS::ElasticLoadBalancingV2::Listener")
    props = listener["Properties"]
    assert "Condition" not in listener
    assert props["Protocol"] == "TCP"  # TLS passthrough: the host's self-signed cert stays
    assert props["Port"] == {"Ref": "ListenerPort"}
    assert "Certificates" not in props
    [action] = props["DefaultActions"]
    assert action == {"Type": "forward", "TargetGroupArn": {"Ref": "TargetGroup"}}
    assert template["Parameters"]["ListenerPort"]["Default"] == 8443
    # no literal 80/443 port anywhere in a listener or a security-group rule
    for res in template["Resources"].values():
        props = res.get("Properties", {})
        for key in ("Port", "FromPort", "ToPort"):
            assert props.get(key) not in (80, 443, 8080, "80", "443", "8080"), res


def test_rules_refuse_icp_gated_ports_and_use_only_rule_functions():
    """CloudFormation Rules support only Fn::And/Contains/EachMemberEquals/EachMemberIn/
    Equals/If/Not/Or/RefAll/ValueOf/ValueOfAll (+ Ref) - cfn-lint does NOT flag Select/Join/
    Split there, so this guards the deployability of the Rules block."""
    template = _load()
    rules = template["Rules"]
    text = str(rules["ListenerPortIsNotIcpGated"])
    for port in ("80", "443", "8080"):
        assert port in text
    assert "AllowlistNeedsTheSecurityGroup" in rules
    allowed = {
        "Fn::And", "Fn::Contains", "Fn::EachMemberEquals", "Fn::EachMemberIn", "Fn::Equals",
        "Fn::If", "Fn::Not", "Fn::Or", "Fn::RefAll", "Fn::ValueOf", "Fn::ValueOfAll", "Ref",
    }

    def functions(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key.startswith("Fn::") or key == "Ref":
                    yield key
                yield from functions(value)
        elif isinstance(node, list):
            for value in node:
                yield from functions(value)

    assert set(functions(rules)) <= allowed, set(functions(rules)) - allowed


def test_allowed_cidrs_pattern_caps_the_list_at_four_and_refuses_any_zero_prefix():
    """A String with an AllowedPattern, not a CommaDelimitedList: Rules cannot count list
    members, so the cap (rev-r11 nit a) and the /0 refusal both live in the pattern."""
    template = _load()
    param = template["Parameters"]["AllowedCidrs"]
    assert param["Type"] == "String"
    regex = re.compile(param["AllowedPattern"])
    assert regex.match("198.51.100.10/32")
    assert regex.match("198.51.100.10/32,10.0.0.0/8,192.168.1.0/24,172.16.0.0/12")
    assert not regex.match("1.1.1.1/32,2.2.2.2/32,3.3.3.3/32,4.4.4.4/32,5.5.5.5/32")  # a fifth
    assert not regex.match("0.0.0.0/0")
    assert not regex.match("1.2.3.4/32,0.0.0.0/0")
    assert not regex.match("1.2.3.4/0")
    assert not regex.match("")
    assert not regex.match("1.2.3.4/32,")
    assert regex.match(param["Default"])


def test_world_ingress_exists_only_in_open_to_internet_mode():
    """The 0.0.0.0/0 rule is the user's deliberate choice (AccessMode=OpenToInternet, the
    default); under AllowedCidrs it must not exist, and the CIDR rules must not exist under
    OpenToInternet. Evaluated through the template's own Conditions, both modes."""
    template = _load()
    world_rules = {
        name for name, res in template["Resources"].items()
        if res.get("Properties", {}).get("CidrIp") == "0.0.0.0/0"
    }
    assert world_rules == {"NlbIngressOpen", "HttpIngress"}
    assert template["Resources"]["NlbIngressOpen"]["Properties"]["GroupId"] == {
        "Ref": "NlbSecurityGroup"
    }
    assert template["Parameters"]["AccessMode"]["Default"] == "OpenToInternet"

    open_mode = _Conditions(template, {**LIVE_PARAMS, "AccessMode": "OpenToInternet"})
    live = open_mode.live_resources(template)
    assert "NlbIngressOpen" in live
    assert "HttpIngress" not in live
    assert not any(name.startswith("NlbIngressCidr") for name in live)
    assert "InstanceIngressFromNlb" in live and "InstanceIngressFromSubnet" not in live

    one_cidr = _Conditions(template, {**LIVE_PARAMS, "AccessMode": "AllowedCidrs"})
    live = one_cidr.live_resources(template)
    assert "NlbIngressOpen" not in live
    assert {n for n in live if n.startswith("NlbIngressCidr")} == {"NlbIngressCidr1"}

    three = _Conditions(template, {
        **LIVE_PARAMS, "AccessMode": "AllowedCidrs",
        "AllowedCidrs": "198.51.100.10/32,10.0.0.0/8,192.168.1.0/24",
    })
    live = three.live_resources(template)
    assert {n for n in live if n.startswith("NlbIngressCidr")} == {
        "NlbIngressCidr1", "NlbIngressCidr2", "NlbIngressCidr3"
    }
    cidr_rules = ("NlbIngressCidr1", "NlbIngressCidr2", "NlbIngressCidr3", "NlbIngressCidr4")
    for i, res in enumerate(cidr_rules):
        assert template["Resources"][res]["Properties"]["CidrIp"] == {
            "Fn::Select": [i, {"Fn::Split": [",", {"Ref": "AllowedCidrs"}]}]
        }


def test_no_security_group_escape_hatch_admits_the_subnet_only():
    template = _load()
    no_sg = _Conditions(template, {**LIVE_PARAMS, "NlbSecurityGroupMode": "disabled"})
    live = no_sg.live_resources(template)
    assert "NlbSecurityGroup" not in live
    assert "NlbIngressOpen" not in live and "InstanceIngressFromNlb" not in live
    assert "InstanceIngressFromSubnet" in live
    props = template["Resources"]["InstanceIngressFromSubnet"]["Properties"]
    assert props["CidrIp"] == {"Ref": "PublicSubnetCidr"}
    assert template["Parameters"]["PublicSubnetCidr"]["Default"] == "10.44.0.0/24"
    assert not re.compile(template["Parameters"]["PublicSubnetCidr"]["AllowedPattern"]).match(
        "0.0.0.0/0"
    )
    [alb] = _resources_of_type(template, "AWS::ElasticLoadBalancingV2::LoadBalancer")
    assert alb["Properties"]["SecurityGroups"] == {
        "Fn::If": ["UseSg", [{"Ref": "NlbSecurityGroup"}], {"Ref": "AWS::NoValue"}]
    }


def test_host_security_group_gets_only_nlb_sourced_rules_and_is_never_declared():
    template = _load()
    host_rules = [
        (name, res) for name, res in template["Resources"].items()
        if res["Type"] == "AWS::EC2::SecurityGroupIngress"
        and res["Properties"]["GroupId"] == {"Ref": "InstanceSecurityGroupId"}
    ]
    assert {name for name, _ in host_rules} == {
        "InstanceIngressFromNlb", "InstanceIngressFromSubnet", "HttpInstanceIngress"
    }
    from_nlb = template["Resources"]["InstanceIngressFromNlb"]["Properties"]
    assert from_nlb["SourceSecurityGroupId"] == {"Ref": "NlbSecurityGroup"}
    assert from_nlb["FromPort"] == from_nlb["ToPort"] == {"Ref": "InstancePort"}
    assert "CidrIp" not in from_nlb
    # the only SecurityGroup resource is the NLB's own - the host's is never rewritten
    [sg] = _resources_of_type(template, "AWS::EC2::SecurityGroup")
    assert sg["Condition"] == "UseSg"
    assert "SecurityGroupIngress" not in sg["Properties"]
    [egress] = sg["Properties"]["SecurityGroupEgress"]
    assert egress["DestinationSecurityGroupId"] == {"Ref": "InstanceSecurityGroupId"}
    assert egress["FromPort"] == egress["ToPort"] == {"Ref": "InstancePort"}
    assert "CidrIp" not in egress


def test_http_lab_adds_only_listener_target_and_scoped_rules():
    template = _load()
    default = _Conditions(template, LIVE_PARAMS).live_resources(template)
    enabled = _Conditions(template, {
        **LIVE_PARAMS, "ConsoleHttpLab": "true",
    }).live_resources(template)
    assert set(enabled) - set(default) == {
        "HttpListener", "HttpTargetGroup", "HttpIngress", "HttpEgress", "HttpInstanceIngress",
    }
    for name in default:
        assert enabled[name] == default[name]
    assert enabled["HttpListener"]["Properties"]["Port"] == {"Ref": "HttpPort"}
    assert enabled["HttpTargetGroup"]["Properties"]["HealthCheckProtocol"] == "HTTP"
    host = enabled["HttpInstanceIngress"]["Properties"]
    assert host["SourceSecurityGroupId"] == {"Ref": "NlbSecurityGroup"}
    assert "CidrIp" not in host
    assert enabled["HttpIngress"]["Properties"]["CidrIp"] == "0.0.0.0/0"
    assert template["Outputs"]["ConsoleUrl"]["Value"]["Fn::If"][0] == "HttpLab"


def test_static_ip_is_owned_and_mapped_onto_the_single_subnet():
    template = _load()
    [eip] = _resources_of_type(template, "AWS::EC2::EIP")
    assert eip["Properties"]["Domain"] == "vpc"
    assert "Condition" not in eip
    # the user advertises the address: it must survive stack deletion and replacement
    assert eip["DeletionPolicy"] == "Retain"
    assert eip["UpdateReplacePolicy"] == "Retain"
    assert "EipAllocationId" in template["Outputs"]
    [nlb] = _resources_of_type(template, "AWS::ElasticLoadBalancingV2::LoadBalancer")
    props = nlb["Properties"]
    assert props["Type"] == "network"
    assert props["Scheme"] == "internet-facing"
    assert props["IpAddressType"] == "ipv4"
    assert "Subnets" not in props
    assert props["SubnetMappings"] == [{
        "SubnetId": {"Ref": "PublicSubnetId"},
        "AllocationId": {"Fn::GetAtt": "StaticIp.AllocationId"},
    }]
    attrs = {a["Key"]: a["Value"] for a in props["LoadBalancerAttributes"]}
    assert attrs["deletion_protection.enabled"] == "false"
    assert attrs["access_logs.s3.enabled"] == "false"
    assert "Name" not in props
    # the stack creates no network it does not own
    owned_elsewhere = (
        "AWS::EC2::VPC", "AWS::EC2::Subnet", "AWS::EC2::InternetGateway", "AWS::EC2::Route",
    )
    for type_ in owned_elsewhere:
        assert _resources_of_type(template, type_) == [], type_


def test_target_group_health_checks_the_console_api_without_client_ip_preservation():
    template = _load()
    [group] = _resources_of_type(template, "AWS::ElasticLoadBalancingV2::TargetGroup")
    props = group["Properties"]
    assert props["TargetType"] == "instance"
    assert props["Protocol"] == "TCP"
    assert props["Port"] == {"Ref": "InstancePort"}
    assert props["Targets"] == [{"Id": {"Ref": "InstanceId"}, "Port": {"Ref": "InstancePort"}}]
    assert props["HealthCheckProtocol"] == "HTTPS"
    assert props["HealthCheckPath"] == "/api/health"
    assert props["HealthCheckPort"] == "traffic-port"
    assert props["Matcher"] == {"HttpCode": "200"}
    attrs = {a["Key"]: a["Value"] for a in props["TargetGroupAttributes"]}
    assert attrs["preserve_client_ip.enabled"] == "false"  # so the host trusts the NLB SG only
    assert attrs["deregistration_delay.timeout_seconds"] == "30"


def test_outputs_and_parameters_carry_no_secret_and_name_the_fixed_entry():
    template = _load()
    outputs = template["Outputs"]
    for key in ("StaticIp", "NlbDnsName", "ConsoleUrl", "SecurityGroupId", "TargetGroupArn"):
        assert key in outputs
    assert outputs["ConsoleUrl"]["Value"] == {"Fn::If": [
        "HttpLab", {"Fn::Sub": "http://${StaticIp}:${HttpPort}"},
        {"Fn::Sub": "https://${StaticIp}:${ListenerPort}"},
    ]}
    assert outputs["SecurityGroupId"]["Condition"] == "UseSg"
    for name, param in template["Parameters"].items():
        assert "NoEcho" not in param, name
        assert "Password" not in name and "Key" not in name and "Secret" not in name, name
    assert template["Parameters"]["AllowedCidrs"]["Default"] == "198.51.100.10/32"
    # nothing t1 - the t1 host lives in a separate, same-CIDR VPC (one stack per VPC/host)
    names = list(template["Parameters"]) + list(template["Resources"])
    assert not any("T1" in name for name in names)
