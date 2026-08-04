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


def _finding(name, severity, package, version):
    return {
        "name": name,
        "severity": severity,
        "attributes": [
            {"key": "package_name", "value": package},
            {"key": "package_version", "value": version},
        ],
    }


class TestBlockingPackages:
    """Counts alone cannot tell an operator whether the CVEs are theirs or the base
    image's. Shapes here mirror a real basic-scan response (glibc/perl in Debian)."""

    def _client(self, findings):
        return StubEcr([{
            "imageScanStatus": {"status": "COMPLETE"},
            "imageScanFindings": {"findings": findings},
        }])

    def test_names_the_cve_and_package(self):
        client = self._client([_finding("CVE-1", "CRITICAL", "glibc", "2.41-12")])
        assert ecr.blocking_packages(client, REPO, DIGEST, ["CRITICAL"]) == [
            "CVE-1 (glibc 2.41-12)"
        ]

    def test_skips_severities_that_did_not_block(self):
        client = self._client([
            _finding("CVE-crit", "CRITICAL", "glibc", "2.41"),
            _finding("CVE-med", "MEDIUM", "curl", "8.0"),
        ])
        out = ecr.blocking_packages(client, REPO, DIGEST, ["CRITICAL"])
        assert out == ["CVE-crit (glibc 2.41)"]

    def test_heaviest_first_across_blocking_severities(self):
        client = self._client([
            _finding("CVE-high", "HIGH", "perl", "5.40"),
            _finding("CVE-crit", "CRITICAL", "glibc", "2.41"),
        ])
        out = ecr.blocking_packages(client, REPO, DIGEST, ["CRITICAL", "HIGH"])
        assert out == ["CVE-crit (glibc 2.41)", "CVE-high (perl 5.40)"]

    def test_caps_the_list(self):
        client = self._client(
            [_finding(f"CVE-{i}", "CRITICAL", "pkg", "1") for i in range(20)]
        )
        assert len(ecr.blocking_packages(client, REPO, DIGEST, ["CRITICAL"], limit=3)) == 3

    def test_missing_attributes_do_not_crash(self):
        client = self._client([{"name": "CVE-x", "severity": "CRITICAL"}])
        assert ecr.blocking_packages(client, REPO, DIGEST, ["CRITICAL"]) == ["CVE-x (? ?)"]

    def test_a_read_failure_is_advisory_only(self):
        """The caller is already raising a blocking error; this must not replace it."""
        client = StubEcr([], raises=RuntimeError("boom"))
        assert ecr.blocking_packages(client, REPO, DIGEST, ["CRITICAL"]) == []
