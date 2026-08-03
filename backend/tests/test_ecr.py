"""ECR digest resolution and the push-scan gate (T10).

The gate's whole value is that it cannot be mistaken for having passed when it did
not run, so the "unavailable" and "timeout" paths get as much attention as the
happy one.
"""

import pytest

from app.services import ecr

REPO = "launchpad-agents"
DIGEST = "sha256:" + "a" * 64


class StubEcr:
    """Returns a scripted sequence of describe_image_scan_findings responses."""

    def __init__(self, responses, images=None, raises=None):
        self._responses = list(responses)
        self._images = images
        self._raises = raises
        self.calls = 0

    def describe_image_scan_findings(self, **kwargs):
        self.calls += 1
        if self._raises:
            raise self._raises
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]

    def describe_images(self, **kwargs):
        return self._images


def _findings(status, counts=None, description=""):
    body = {"imageScanStatus": {"status": status, "description": description}}
    if counts is not None:
        body["imageScanFindings"] = {"findingSeverityCounts": counts}
    return body


class TestResolveDigest:
    def test_returns_the_digest_behind_the_tag(self):
        client = StubEcr([], images={"imageDetails": [{"imageDigest": DIGEST}]})
        assert ecr.resolve_digest(client, REPO, "agent-v1") == DIGEST

    def test_no_details_is_an_error_not_an_empty_string(self):
        client = StubEcr([], images={"imageDetails": []})
        with pytest.raises(ecr.ScanUnavailable, match="no digest"):
            ecr.resolve_digest(client, REPO, "agent-v1")

    def test_details_without_a_digest_is_an_error(self):
        client = StubEcr([], images={"imageDetails": [{}]})
        with pytest.raises(ecr.ScanUnavailable, match="no digest"):
            ecr.resolve_digest(client, REPO, "agent-v1")


class TestWaitForScan:
    def test_returns_severity_counts_when_complete(self):
        client = StubEcr([_findings("COMPLETE", {"HIGH": 2, "LOW": 5})])
        counts = ecr.wait_for_scan(client, REPO, DIGEST, sleeper=lambda _: None)
        assert counts == {"HIGH": 2, "LOW": 5}

    def test_polls_past_in_progress(self):
        client = StubEcr([
            _findings("IN_PROGRESS"),
            _findings("IN_PROGRESS"),
            _findings("COMPLETE", {"CRITICAL": 1}),
        ])
        seen = []
        counts = ecr.wait_for_scan(
            client, REPO, DIGEST, sleeper=lambda _: None, on_status=seen.append
        )
        assert counts == {"CRITICAL": 1}
        assert seen == ["IN_PROGRESS", "COMPLETE"]  # transitions only
        assert client.calls == 3

    def test_a_complete_scan_with_no_findings_is_empty_counts(self):
        client = StubEcr([_findings("COMPLETE", {})])
        assert ecr.wait_for_scan(client, REPO, DIGEST, sleeper=lambda _: None) == {}

    def test_timeout_raises_rather_than_returning_clean(self):
        client = StubEcr([_findings("IN_PROGRESS")])
        with pytest.raises(ecr.ScanTimeout):
            ecr.wait_for_scan(
                client, REPO, DIGEST, timeout_s=-1, sleeper=lambda _: None
            )

    def test_failed_scan_raises(self):
        client = StubEcr([_findings("FAILED", description="UnsupportedImageError")])
        with pytest.raises(ecr.ScanUnavailable, match="UnsupportedImageError"):
            ecr.wait_for_scan(client, REPO, DIGEST, sleeper=lambda _: None)

    def test_an_unknown_status_raises_rather_than_looping_forever(self):
        client = StubEcr([_findings("ACTIVE")])
        with pytest.raises(ecr.ScanUnavailable, match="unexpected"):
            ecr.wait_for_scan(client, REPO, DIGEST, sleeper=lambda _: None)

    def test_an_api_error_is_surfaced_as_unavailable(self):
        """Scanning not enabled on the registry lands here — and must not read as
        a passed gate."""
        client = StubEcr([], raises=RuntimeError("ScanNotFoundException"))
        with pytest.raises(ecr.ScanUnavailable, match="ScanNotFoundException"):
            ecr.wait_for_scan(client, REPO, DIGEST, sleeper=lambda _: None)


class TestBlockingFindings:
    def test_only_the_configured_severities_block(self):
        counts = {"CRITICAL": 1, "HIGH": 4, "LOW": 9}
        assert ecr.blocking_findings(counts, ["CRITICAL"]) == {"CRITICAL": 1}

    def test_multiple_severities(self):
        counts = {"CRITICAL": 1, "HIGH": 4, "LOW": 9}
        assert ecr.blocking_findings(counts, ["CRITICAL", "HIGH"]) == {
            "CRITICAL": 1,
            "HIGH": 4,
        }

    def test_zero_counts_do_not_block(self):
        assert ecr.blocking_findings({"CRITICAL": 0}, ["CRITICAL"]) == {}

    def test_matching_is_case_insensitive(self):
        assert ecr.blocking_findings({"CRITICAL": 2}, ["critical"]) == {"CRITICAL": 2}

    def test_an_empty_severity_list_blocks_nothing(self):
        assert ecr.blocking_findings({"CRITICAL": 2}, []) == {}

    def test_a_clean_image_blocks_nothing(self):
        assert ecr.blocking_findings({}, ["CRITICAL"]) == {}


class TestFormatCounts:
    def test_orders_heaviest_first(self):
        text = ecr.format_counts({"LOW": 3, "CRITICAL": 1, "MEDIUM": 2})
        assert text == "CRITICAL 1, MEDIUM 2, LOW 3"

    def test_no_findings_reads_as_none(self):
        assert ecr.format_counts({}) == "none"

    def test_an_unranked_severity_still_appears(self):
        assert "WEIRD 1" in ecr.format_counts({"WEIRD": 1})
