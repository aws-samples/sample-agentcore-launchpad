"""The Global CloudFront console entry: one VPC origin + one distribution, nothing public."""
import json
from pathlib import Path

import yaml

from tests.test_ec2_template import _CfnLoader

TEMPLATE = Path(__file__).resolve().parents[1] / "ec2/launchpad-console-cloudfront.yaml"


def _load() -> dict:
    return yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=_CfnLoader)


def test_only_a_vpc_origin_and_its_distribution():
    template = _load()
    types = {name: r["Type"] for name, r in template["Resources"].items()}
    assert types == {"ConsoleVpcOrigin": "AWS::CloudFront::VpcOrigin",
                     "ConsoleDistribution": "AWS::CloudFront::Distribution"}
    text = json.dumps(template)
    assert "0.0.0.0/0" not in text and "CustomOriginConfig" not in text


def test_origin_is_the_host_over_the_vpc_origin():
    template = _load()
    endpoint = template["Resources"]["ConsoleVpcOrigin"]["Properties"]["VpcOriginEndpointConfig"]
    assert "${AWS::Partition}" in endpoint["Arn"]["Fn::Sub"]
    assert "${InstanceId}" in endpoint["Arn"]["Fn::Sub"]
    assert endpoint["HTTPPort"] == {"Ref": "ConsolePort"}
    assert endpoint["OriginProtocolPolicy"] == "http-only"
    config = template["Resources"]["ConsoleDistribution"]["Properties"]["DistributionConfig"]
    [origin] = config["Origins"]
    assert origin["DomainName"] == {"Ref": "InstancePrivateDnsName"}
    assert origin["VpcOriginConfig"]["VpcOriginId"] == {"Fn::GetAtt": "ConsoleVpcOrigin.Id"}
    assert origin["VpcOriginConfig"]["OriginReadTimeout"] == 120  # SSE chat


def test_viewer_side_is_https_uncached_and_unbuffered():
    template = _load()
    behavior = template["Resources"]["ConsoleDistribution"]["Properties"][
        "DistributionConfig"]["DefaultCacheBehavior"]
    assert behavior["ViewerProtocolPolicy"] == "redirect-to-https"
    assert behavior["Compress"] is False
    assert behavior["CachePolicyId"] == "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"  # CachingDisabled
    assert behavior["OriginRequestPolicyId"] == "216adef6-5c7f-47e4-b989-5492eafa07d3"  # AllViewer
    assert {"POST", "DELETE", "PATCH", "PUT"} <= set(behavior["AllowedMethods"])
    outputs = template["Outputs"]
    assert outputs["ConsoleUrl"]["Value"] == {"Fn::Sub": "https://${ConsoleDistribution.DomainName}"}
    assert {"DistributionId", "InstanceId", "VpcOriginId"} <= set(outputs)
