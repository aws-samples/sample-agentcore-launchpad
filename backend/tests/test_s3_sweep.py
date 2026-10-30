"""`services/s3_sweep` — the one reporting S3 deleter (N17-obs118 / N17-obs119, cn/r17-t).

A `DeleteObjects` 200 whose `Errors[]` names refused keys is residue the sweep REPORTS (per
key, by S3 code); a raised call propagates to the caller's guard; the summary and the
`S3SweepResidue` text carry counts and codes only — never a key or an S3 message — so
`redacted_error` may hand them to a response verbatim.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from app.core.errors import redacted_error
from app.services import s3_sweep
from tests.memory_fakes import FakeS3

BUCKET = "bkt"


class PartialS3(FakeS3):
    """`delete_objects` refuses every key whose name contains ``refuse`` (200 + `Errors[]`),
    and records the batches it was asked for."""

    def __init__(self) -> None:
        super().__init__()
        self.batches: list[list[str]] = []

    def delete_objects(self, *, Bucket, Delete, **kw):  # noqa: N803 — boto kwargs
        keys = [o["Key"] for o in Delete["Objects"]]
        self.batches.append(keys)
        kept = [k for k in keys if "refuse" in k]
        super().delete_objects(Bucket=Bucket, Delete={"Objects": [
            {"Key": k} for k in keys if k not in kept]})
        return {
            "Deleted": [{"Key": k} for k in keys if k not in kept],
            "Errors": [{"Key": k, "Code": "AccessDenied",
                        "Message": "User: arn:aws-cn:iam::123456789012:role/hub is not "
                                   "authorized to perform: s3:DeleteObject"} for k in kept],
        }


def _seed(s3: FakeS3, keys: list[str]) -> None:
    for k in keys:
        s3.put_object(Bucket=BUCKET, Key=k, Body=b"{}")


def test_a_clean_sweep_deletes_every_key_and_is_complete():
    s3 = PartialS3()
    keys = [f"p/{i}.json" for i in range(3)]
    _seed(s3, keys)
    result = s3_sweep.delete_prefix(s3, BUCKET, "p/")
    assert result.complete and result.deleted == keys and result.failed == {}
    assert result.summary() == "3 object(s) deleted"
    assert result.raise_for_residue() is result
    assert not [k for (_b, k) in s3.objects if k.startswith("p/")]


def test_refused_keys_in_a_200_are_reported_per_key_with_the_code_only():
    s3 = PartialS3()
    keys = ["p/a.json", "p/refuse-1.json", "p/refuse-2.json"]
    _seed(s3, keys)
    result = s3_sweep.delete_prefix(s3, BUCKET, "p/")
    assert result.deleted == ["p/a.json"]
    assert result.failed == {"p/refuse-1.json": "AccessDenied", "p/refuse-2.json": "AccessDenied"}
    assert not result.complete and result.attempted == 3
    assert result.summary() == "2 of 3 objects refused (AccessDenied ×2)"
    with pytest.raises(s3_sweep.S3SweepResidue) as exc:
        result.raise_for_residue()
    text = redacted_error(exc.value)
    assert text == "S3SweepResidue: 2 of 3 objects refused (AccessDenied ×2)"
    for forbidden in ("refuse-1", "is not authorized", "arn:"):
        assert forbidden not in text
    assert exc.value.result is result
    # the keys S3 refused are exactly what is still there — honest residue
    assert sorted(k for (_b, k) in s3.objects) == ["p/refuse-1.json", "p/refuse-2.json"]


def test_mixed_codes_are_counted_separately_and_sorted():
    result = s3_sweep.SweepResult(deleted=["a"], failed={"b": "AccessDenied", "c": "InternalError",
                                                         "d": "AccessDenied"})
    assert result.summary() == "3 of 4 objects refused (AccessDenied ×2, InternalError)"


def test_keys_are_batched_to_the_delete_objects_ceiling():
    s3 = PartialS3()
    keys = [f"p/{i:04d}.json" for i in range(2 * s3_sweep.DELETE_BATCH + 7)]
    _seed(s3, keys)
    result = s3_sweep.delete_keys(s3, BUCKET, keys)
    assert [len(b) for b in s3.batches] == [1000, 1000, 7]
    assert result.complete and len(result.deleted) == len(keys)


def test_a_raised_delete_objects_propagates_to_the_callers_guard():
    s3 = PartialS3()
    _seed(s3, ["p/a.json"])

    def boom(**kw):
        raise ClientError({"Error": {"Code": "SlowDown", "Message": "Please reduce"}},
                          "DeleteObjects")

    s3.delete_objects = boom  # type: ignore[method-assign]
    with pytest.raises(ClientError) as exc:
        s3_sweep.delete_prefix(s3, BUCKET, "p/")
    assert redacted_error(exc.value) == "ClientError: SlowDown (DeleteObjects)"


def test_an_empty_prefix_is_refused_and_nothing_is_called():
    s3 = PartialS3()
    _seed(s3, ["p/a.json"])
    with pytest.raises(ValueError):
        s3_sweep.delete_prefix(s3, BUCKET, "")
    assert s3.batches == [] and len(s3.objects) == 1


def test_no_keys_means_no_call():
    s3 = PartialS3()
    result = s3_sweep.delete_keys(s3, BUCKET, [])
    assert result.complete and result.attempted == 0 and s3.batches == []
    assert s3_sweep.delete_prefix(s3, BUCKET, "nothing/").complete
