"""Regression for the live 1.17/1.56 Memory integration import failure."""

from packaging.requirements import Requirement

from app.deployer.zip_runtime import platform_requirements


def test_every_generated_strands_path_excludes_the_incompatible_release():
    for method, source, protocol in (
        ("zip_runtime", "bedrock", "http"),
        ("zip_runtime", "mantle", "http"),
        ("zip_runtime", "bedrock", "a2a"),
        ("studio", "bedrock", "http"),
    ):
        requirements = [
            Requirement(item) for item in platform_requirements(method, source, protocol)
        ]
        strands = next(item for item in requirements if item.name == "strands-agents")
        assert "1.47.0" in strands.specifier
        assert "1.56.0" not in strands.specifier


def test_pinned_backend_sdk_can_import_the_runtime_memory_integration():
    # This import failed before the runtime could open its HTTP listener on AWS.
    # No client/session is created and no credentials or network are used.
    from bedrock_agentcore.memory.integrations.strands.session_manager import (
        AgentCoreMemorySessionManager,
    )

    assert callable(AgentCoreMemorySessionManager)
