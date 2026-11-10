"""D21 (M1-d): the hub attaches the descriptors of every catalog model a job names."""

import json

from app.evaluation import job_descriptors
from app.evaluation.jobs import JobSpec
from app.schemas.agent import model_descriptor

GLM = "global.zai.glm-5.3"
KIMI = "global.moonshotai.kimi-k3"


def test_referenced_model_ids_walks_every_model_id_key_at_any_depth():
    spec = {
        "judge_model_id": GLM, "actor_model_id": KIMI, "unrelated": {"model_id": "custom"},
        "evaluator_defs": [{"id": "e1", "model_id": KIMI}, {"id": "e2"},
                           {"nested": [{"optimizer_model_id": GLM}]}],
        "model_id": None, "model": "not-a-model-id-key",
    }
    assert job_descriptors.referenced_model_ids(spec) == [GLM, KIMI, "custom"]
    assert job_descriptors.referenced_model_ids({}) == []
    assert job_descriptors.referenced_model_ids(None) == []


def test_attach_fills_models_for_catalog_ids_only():
    job = JobSpec(job_id="j1", job_type="batch_eval", workspace={}, sink={},
                  spec={"judge_model_id": GLM, "evaluator_defs": [{"model_id": "custom"}]})
    assert job_descriptors.attach(job) is job
    assert job.models == {GLM: model_descriptor(GLM)}
    payload = json.loads(json.dumps(job.to_payload()))  # JSON-safe
    assert payload["models"][GLM]["credential_provider"] == "launchpad-global-bedrock"
    assert JobSpec.from_payload(payload).models == job.models


def test_submit_puts_the_descriptors_on_the_wire():
    from tests.test_runtime_executor import _job, _ws, executor

    ex, data, _s3 = executor()
    job = _job()
    job.spec = {**job.spec, "judge_model_id": GLM}
    assert ex.submit(_ws(), job) == job.job_id
    payload = json.loads(data.calls[0]["payload"])
    assert payload["models"] == {GLM: model_descriptor(GLM)}
