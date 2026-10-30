"""Regression for the live strands-agents 1.56 release that broke generated runtimes.

The CN build's memory path is the self-hosted library (memory-runtime.md), not the SDK's
AgentCore Memory integration, so no import of that integration is pinned here (N8/C5).
"""

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

