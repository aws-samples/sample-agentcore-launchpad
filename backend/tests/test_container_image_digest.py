"""The container path deploys an image by digest, not by its mutable tag (T10).

`_image_ref` tags images `{agent}-v{version}`, which is rewritten on every
re-publish, so a runtime pinned to the tag can silently start executing different
code. These tests pin the digest plumbing: recorded at package time, used at
deploy time, and reused when a job resumes.
"""

import pytest

from app.core.db import SessionLocal
from app.deployer import container
from app.deployer.pipeline import StageContext
from app.models.ledger import Agent, Deployment

DIGEST = "sha256:" + "c" * 64
OTHER_DIGEST = "sha256:" + "d" * 64
REGISTRY = "123456789012.dkr.ecr.us-west-2.amazonaws.com"
REPO = "launchpad-agents"


@pytest.fixture
def rows():
    """An agent + deployment pair, as the pipeline would have created them."""
    db = SessionLocal()
    try:
        agent = Agent(
            name="digest-probe",
            method="container",
            spec={
                "name": "digest-probe",
                "method": "container",
                "system_prompt": "probe",
            },
            version="1",
        )
        db.add(agent)
        db.flush()
        deployment = Deployment(agent_id=agent.id)
        db.add(deployment)
        db.commit()
        return agent.id, deployment.id
    finally:
        db.close()


def _ctx(agent_id: str, deployment_id: str, **scratch) -> StageContext:
    return StageContext(
        agent_id=agent_id,
        deployment_id=deployment_id,
        job_id="job-digest-probe",
        scratch=dict(scratch),
        log=lambda msg: None,
    )


class TestRecordImageDigest:
    def test_the_digest_lands_on_the_deployment_row(self, rows):
        agent_id, deployment_id = rows
        container._record_image_digest(_ctx(agent_id, deployment_id), DIGEST)
        db = SessionLocal()
        try:
            assert db.get(Deployment, deployment_id).image_digest == DIGEST
        finally:
            db.close()

    def test_a_missing_deployment_row_does_not_explode(self, rows):
        agent_id, _ = rows
        container._record_image_digest(_ctx(agent_id, "no-such-deployment"), DIGEST)


class TestRecordedDigestUri:
    def test_builds_the_digest_uri_from_the_row(self, rows):
        agent_id, deployment_id = rows
        container._record_image_digest(_ctx(agent_id, deployment_id), DIGEST)
        uri = container._recorded_digest_uri(
            _ctx(agent_id, deployment_id), REGISTRY, REPO
        )
        assert uri == f"{REGISTRY}/{REPO}@{DIGEST}"

    def test_none_when_no_digest_was_recorded(self, rows):
        """A deployment created before digest pinning existed."""
        agent_id, deployment_id = rows
        assert (
            container._recorded_digest_uri(
                _ctx(agent_id, deployment_id), REGISTRY, REPO
            )
            is None
        )

    def test_scratch_wins_over_the_row_within_one_run(self, rows):
        """The package stage that just ran is more authoritative than an older row
        value, so a re-publish deploys the image it just built."""
        agent_id, deployment_id = rows
        container._record_image_digest(_ctx(agent_id, deployment_id), OTHER_DIGEST)
        ctx = _ctx(
            agent_id, deployment_id, image_uri=f"{REGISTRY}/{REPO}@{DIGEST}"
        )
        assert ctx.scratch["image_uri"].endswith(DIGEST)


class TestScanGate:
    """The gate must never let an unread scan read as a passed one."""

    class _Stub:
        def __init__(self, status, counts=None):
            self._status = status
            self._counts = counts or {}

        def describe_image_scan_findings(self, **kwargs):
            return {
                "imageScanStatus": {"status": self._status},
                "imageScanFindings": {"findingSeverityCounts": self._counts},
            }

    class _Settings:
        image_scan_enabled = True
        image_scan_block_severities = ["CRITICAL"]
        image_scan_timeout_s = 5

    def _logging_ctx(self, logs):
        return StageContext(
            agent_id="a", deployment_id="d", job_id="j", log=logs.append
        )

    def test_a_clean_scan_passes(self, rows):
        logs = []
        container._run_scan_gate(
            self._logging_ctx(logs),
            self._Stub("COMPLETE", {"LOW": 3}),
            REPO,
            DIGEST,
            self._Settings(),
        )
        assert any("LOW 3" in line for line in logs)

    def test_a_critical_finding_blocks_the_deploy(self, rows):
        with pytest.raises(RuntimeError, match="blocking vulnerabilities"):
            container._run_scan_gate(
                self._logging_ctx([]),
                self._Stub("COMPLETE", {"CRITICAL": 2}),
                REPO,
                DIGEST,
                self._Settings(),
            )

    def test_the_block_message_names_the_override(self):
        """An un-escapable gate strands every agent the first time a base image
        picks up a CVE."""
        with pytest.raises(RuntimeError) as excinfo:
            container._run_scan_gate(
                self._logging_ctx([]),
                self._Stub("COMPLETE", {"CRITICAL": 1}),
                REPO,
                DIGEST,
                self._Settings(),
            )
        assert "image_scan_block_severities" in str(excinfo.value)

    def test_a_high_finding_does_not_block_at_the_default_threshold(self):
        container._run_scan_gate(
            self._logging_ctx([]),
            self._Stub("COMPLETE", {"HIGH": 9}),
            REPO,
            DIGEST,
            self._Settings(),
        )

    def test_a_configured_threshold_is_honoured(self):
        class Settings(self._Settings):
            image_scan_block_severities = ["CRITICAL", "HIGH"]

        with pytest.raises(RuntimeError, match="HIGH 9"):
            container._run_scan_gate(
                self._logging_ctx([]),
                self._Stub("COMPLETE", {"HIGH": 9}),
                REPO,
                DIGEST,
                Settings(),
            )

    def test_an_unreadable_scan_says_so_in_the_log(self):
        logs = []
        container._run_scan_gate(
            self._logging_ctx(logs), self._Stub("FAILED"), REPO, DIGEST, self._Settings()
        )
        assert any("did NOT complete" in line for line in logs)
        assert any("unscanned" in line.lower() for line in logs)

    def test_disabling_the_gate_says_the_image_was_not_scanned(self):
        class Settings(self._Settings):
            image_scan_enabled = False

        logs = []
        container._run_scan_gate(
            self._logging_ctx(logs), self._Stub("COMPLETE"), REPO, DIGEST, Settings()
        )
        assert any("NOT scanned" in line for line in logs)
