"""Deployed capability is bound to package-time evidence across job resumes."""

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.deployer.input_contract import record_input_contract, source_contract, stamp_input_contract
from app.deployer.pipeline import StageContext, create_deployment
from app.models.ledger import Agent, Job
from tests.conftest import ws_ctx


def test_only_top_level_explicit_contract_is_recognized():
    assert source_contract("LAUNCHPAD_ATTACHMENT_CONTRACT = 'v1'") == "v1"
    assert source_contract("# LAUNCHPAD_ATTACHMENT_CONTRACT = 'v1'") is None
    assert source_contract("def f():\n    LAUNCHPAD_ATTACHMENT_CONTRACT = 'v1'") is None
    assert source_contract("LAUNCHPAD_ATTACHMENT_CONTRACT = 'v2'") is None
    assert source_contract("broken (") is None


def test_package_marker_survives_resume_but_does_not_enable_before_deploy():
    with SessionLocal() as db:
        agent = Agent(
            workspace_id=DEFAULT_WORKSPACE_ID, name="contract", method="zip_runtime",
            version="1", status="active", spec={},
        )
        db.add(agent)
        db.commit()
        deployment, job = create_deployment(db, agent)
        ctx = StageContext(
            agent.id, deployment.id, job.id, workspace=ws_ctx(),
        )
        agent_id, job_id = agent.id, job.id
    record_input_contract(ctx, "LAUNCHPAD_ATTACHMENT_CONTRACT = 'v1'")
    with SessionLocal() as db:
        assert db.get(Agent, agent_id).attachment_version is None
        assert db.get(Job, job_id).payload["attachment_contract"] == "v1"
    resumed = StageContext(agent_id, deployment.id, job_id, workspace=ws_ctx())
    assert not resumed.scratch
    with SessionLocal() as db:
        agent = db.get(Agent, agent_id)
        agent.version = "2"
        stamp_input_contract(resumed, db, agent)
        db.commit()
        assert agent.attachment_version == "2"
    # A later package with a custom text-only entrypoint revokes native support.
    record_input_contract(ctx, "def invoke(payload): return payload['prompt']")
    with SessionLocal() as db:
        agent = db.get(Agent, agent_id)
        agent.version = "3"
        stamp_input_contract(resumed, db, agent)
        db.commit()
        assert agent.attachment_version is None
