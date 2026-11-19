"""samples/itsm-cn on the Global CloudFront distribution (us-east-1, partition aws).

The lab accepts exactly the two distribution regions and derives every IAM / S3 / Lambda ARN
from the region's partition, so the same scripts serve cn-northwest-1 and us-east-1.
"""
from __future__ import annotations

import pytest

from tests.test_itsm_model_config import SCRIPTS, load_lab


@pytest.mark.parametrize("region,partition", [("cn-northwest-1", "aws-cn"), ("us-east-1", "aws")])
def test_lab_partition_follows_the_region(tmp_path, monkeypatch, region, partition):
    lab = load_lab(tmp_path, monkeypatch, f"p_{partition.replace('-', '_')}", region=region,
                   console="https://d1abc.cloudfront.net", api_base="https://d1abc.cloudfront.net")
    assert lab.PARTITION == partition
    [arn] = lab.publish_grant_resources(["s3://bucket/kb/source/"])
    assert arn == f"arn:{partition}:s3:::bucket/kb/source/*"


def test_lab_refuses_other_regions(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="This lab targets"):
        load_lab(tmp_path, monkeypatch, "west", region="us-west-2")


def test_no_hard_coded_partition_in_the_lab_scripts():
    for name in ("deploy_tools.py", "prepare_network.py", "lab.py"):
        text = (SCRIPTS / name).read_text()
        assert "arn:aws-cn:" not in text, name
