"""N12 E0-7 — `create_code_runtime` accepts the Runtime lifecycle configuration the
evaluation worker needs (idle 5 min, max lifetime 8 h) exactly like its container twin;
`platformVersion` stays pinned (spec C1)."""

from unittest.mock import MagicMock

from app.services.agentcore import runtime as rt

BASE = dict(runtime_name="launchpad_eval_worker", s3_bucket="b", s3_key="k.zip", role_arn="r")


def test_lifecycle_configuration_is_passed_through():
    client = MagicMock()
    rt.create_code_runtime(client, **BASE,
                           lifecycle={"idleRuntimeSessionTimeout": 300, "maxLifetime": 28800})
    params = client.create_agent_runtime.call_args.kwargs
    assert params["lifecycleConfiguration"] == {"idleRuntimeSessionTimeout": 300,
                                                "maxLifetime": 28800}
    assert params["platformVersion"] == rt.RUNTIME_PLATFORM_VERSION == "V2"


def test_lifecycle_is_omitted_by_default():
    client = MagicMock()
    rt.create_code_runtime(client, **BASE)
    assert "lifecycleConfiguration" not in client.create_agent_runtime.call_args.kwargs


def test_container_twin_keeps_the_same_kwarg():
    client = MagicMock()
    rt.create_container_runtime(client, runtime_name="n", container_uri="i", role_arn="r",
                                lifecycle={"idleRuntimeSessionTimeout": 300})
    assert client.create_agent_runtime.call_args.kwargs["lifecycleConfiguration"] == {
        "idleRuntimeSessionTimeout": 300}


def test_update_code_runtime_echoes_a_lifecycle_and_omits_it_when_none():
    """N12-bug7: UpdateAgentRuntime resets an omitted lifecycleConfiguration — the zip
    update wrapper carries the preset's lifecycle like the container one already did."""
    from unittest.mock import MagicMock

    client = MagicMock()
    rt.update_code_runtime(client, runtime_id="rt-1", s3_bucket="b", s3_key="k",
                           role_arn="arn:role",
                           lifecycle={"idleRuntimeSessionTimeout": 300, "maxLifetime": 28800})
    params = client.update_agent_runtime.call_args.kwargs
    assert params["lifecycleConfiguration"] == {"idleRuntimeSessionTimeout": 300,
                                                "maxLifetime": 28800}
    client = MagicMock()
    rt.update_code_runtime(client, runtime_id="rt-1", s3_bucket="b", s3_key="k",
                           role_arn="arn:role", lifecycle=None)
    assert "lifecycleConfiguration" not in client.update_agent_runtime.call_args.kwargs
