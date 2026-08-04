"""Caller-supplied pip requirements must name one immutable artifact (T10).

Unpinned entries mean a rebuild installs whatever the index serves that day, with
no spec change to show for it. Refused at schema validation so the console sees
the error before a build starts.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.schemas.agent import AgentSpec
from app.schemas.requirements import (
    REQUIRED_FORM,
    is_pinned,
    resolve_pins,
    unpinned,
)

PINNED = [
    "requests==2.32.3",
    "strands-agents==1.47.0",
    "strands-agents[otel]==1.47.0",
    "strands-agents[otel,openai]==1.47.0",
    "pydantic==2.13.4 ; python_version >= '3.12'",
    "some-pkg==1.0.0rc1",
    "some-pkg==1.0.0.post1",
    "some-pkg==1!2.0.0",
    "pkg @ https://example.com/pkg-1.0-py3-none-any.whl#sha256=" + "a" * 64,
    "pkg @ git+https://github.com/o/r@" + "0" * 40,
]

UNPINNED = [
    "requests",                       # bare name
    "requests>=2.32",
    "requests>2",
    "requests<3",
    "requests~=2.32.0",
    "requests!=2.31.0",
    "requests>=2.32,<3",
    "requests==2.32.*",               # a range wearing == clothing
    "strands-agents[otel]>=1.0,<2",
    "pkg @ git+https://github.com/o/r@main",   # a branch moves
    "pkg @ git+https://github.com/o/r@v1.2.3",  # so can a tag
    "pkg @ git+https://github.com/o/r",
    "pkg @ https://example.com/pkg-1.0.tar.gz",  # no content hash
    "",
]


class TestIsPinned:
    @pytest.mark.parametrize("entry", PINNED)
    def test_accepts(self, entry):
        assert is_pinned(entry) is True, entry

    @pytest.mark.parametrize("entry", UNPINNED)
    def test_rejects(self, entry):
        assert is_pinned(entry) is False, entry

    def test_unpinned_preserves_input_order(self):
        assert unpinned(["a==1", "b", "c==2", "d>=1"]) == ["b", "d>=1"]


def _spec(**overrides):
    base = {
        "name": "pinning-probe",
        "method": "zip_runtime",
        "system_prompt": "you are a test agent",
    }
    return AgentSpec(**{**base, **overrides})


class TestAgentSpecValidation:
    def test_a_pinned_spec_is_accepted(self):
        spec = _spec(requirements=["requests==2.32.3"])
        assert spec.requirements == ["requests==2.32.3"]

    def test_no_requirements_is_fine(self):
        assert _spec().requirements == []

    def test_an_unpinned_requirement_is_refused(self):
        with pytest.raises(ValueError) as excinfo:
            _spec(requirements=["requests>=2.32"])
        assert "requests>=2.32" in str(excinfo.value)

    def test_the_message_states_the_required_form(self):
        """An error that only says "invalid" leaves the user guessing."""
        with pytest.raises(ValueError) as excinfo:
            _spec(requirements=["requests"])
        message = str(excinfo.value)
        assert "name==version" in message
        assert "#sha256=" in REQUIRED_FORM

    def test_every_offender_is_named_not_just_the_first(self):
        with pytest.raises(ValueError) as excinfo:
            _spec(requirements=["ok==1.0.0", "loose-one", "loose-two>=2"])
        message = str(excinfo.value)
        assert "loose-one" in message
        assert "loose-two>=2" in message
        assert "ok==1.0.0" not in message


class TestPlatformListsAreNotSubjectToThis:
    """The platform's own lists keep ranges on purpose (pip is meant to intersect
    two specs for the same project — see MANTLE_EXTRA_REQUIREMENTS). Locking, not
    hand-pinning, is what makes the resolved set reproducible."""

    def test_base_requirements_stay_ranged(self):
        from app.templates.strands_agent import base_requirements

        assert unpinned(base_requirements()), (
            "base_requirements is pinned now — if that was deliberate, update "
            "app/schemas/requirements.py's module docstring and this test"
        )

    def test_a_spec_with_ranged_platform_deps_still_validates(self):
        # The validator must only see spec.requirements, never the merged list.
        assert _spec(requirements=[]).method == "zip_runtime"


class TestResolvePins:
    """`resolve_pins` turns platform-derived ranges into pins.

    Driven with a stub runner: the real path shells out to `uv pip compile`, which
    needs the network, and this suite is hermetic. The cross-platform resolve was
    verified against the real requirement set during design.

    Cases about pass-through, extras, and ordering pass `platform=[]` — those
    behaviours do not depend on the platform contribution. The cases that do are
    grouped at the bottom.
    """

    @staticmethod
    def _runner(resolved: str):
        def run(cmd, capture_output=True, text=True):
            out = cmd[cmd.index("-o") + 1]
            Path(out).write_text(resolved, encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        return run

    def test_already_pinned_entries_skip_the_resolver_entirely(self):
        def explode(*args, **kwargs):  # pragma: no cover - must not be called
            raise AssertionError("resolver invoked for an already-pinned set")

        assert resolve_pins(["a==1.0.0", "b==2.0.0"], [], runner=explode) == [
            "a==1.0.0",
            "b==2.0.0",
        ]

    def test_a_range_becomes_a_pin(self):
        out = resolve_pins(
            ["strands-agents >= 1.15.0"],
            [],
            runner=self._runner("strands-agents==1.50.2\n"),
        )
        assert out == ["strands-agents==1.50.2"]

    def test_extras_are_reattached(self):
        """`uv pip compile` drops extras from its output, so `botocore[crt]` would
        silently become plain `botocore` and install different code."""
        out = resolve_pins(
            ["botocore[crt] >= 1.35.0"],
            [],
            runner=self._runner("botocore==1.43.62\n"),
        )
        assert out == ["botocore[crt]==1.43.62"]

    def test_a_star_pin_is_resolved_too(self):
        out = resolve_pins(
            ["bedrock-agentcore==1.17.*"],
            [],
            runner=self._runner("bedrock-agentcore==1.17.0\n"),
        )
        assert out == ["bedrock-agentcore==1.17.0"]

    def test_input_order_is_preserved_and_pinned_entries_pass_through(self):
        out = resolve_pins(
            ["keep==1.0.0", "loose>=2", "also-keep==3.0.0"],
            [],
            runner=self._runner("loose==2.5.0\n"),
        )
        assert out == ["keep==1.0.0", "loose==2.5.0", "also-keep==3.0.0"]

    def test_name_normalisation_matches_the_resolver_output(self):
        out = resolve_pins(
            ["Strands_Agents >= 1.0"],
            [],
            runner=self._runner("strands-agents==1.50.2\n"),
        )
        assert out == ["strands-agents==1.50.2"]

    def test_resolver_failure_is_reported_not_swallowed(self):
        def failing(cmd, capture_output=True, text=True):
            return SimpleNamespace(returncode=1, stdout="", stderr="no matching version")

        with pytest.raises(ValueError, match="could not resolve requirements"):
            resolve_pins(["impossible>=99"], [], runner=failing)

    def test_an_entry_missing_from_the_output_is_an_error(self):
        """Silently dropping a requirement would ship an agent without a
        dependency it asked for."""
        with pytest.raises(ValueError, match="not present in the resolver output"):
            resolve_pins(
                ["wanted>=1"], [], runner=self._runner("something-else==1.0.0\n")
            )

    def test_the_resolve_matches_the_deploy_target_and_artifact_type(self):
        seen = {}

        def capture(cmd, capture_output=True, text=True):
            seen["cmd"] = cmd
            Path(cmd[cmd.index("-o") + 1]).write_text("x==1.0.0\n", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        resolve_pins(["x>=1"], [], runner=capture)
        assert "aarch64-manylinux2014" in seen["cmd"]
        assert "3.13" in seen["cmd"]
        assert "--only-binary=:all:" in seen["cmd"]

    def test_the_dependency_walk_is_not_disabled(self):
        """`--no-deps` would hide the transitive caps that make a pin lockable —
        it is what let `mcp==2.0.0` through. Its absence is the fix, so it is
        asserted rather than left to inspection."""
        seen = {}

        def capture(cmd, capture_output=True, text=True):
            seen["cmd"] = cmd
            Path(cmd[cmd.index("-o") + 1]).write_text("x==1.0.0\n", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        resolve_pins(["x>=1"], [], runner=capture)
        assert "--no-deps" not in seen["cmd"]

    def test_the_result_satisfies_the_validator(self):
        out = resolve_pins(["loose>=2"], [], runner=self._runner("loose==2.5.0\n"))
        assert unpinned(out) == []

    # --- the platform contribution -----------------------------------------

    @staticmethod
    def _capture_input(resolved: str):
        """Stub runner that also records the compile input it was handed."""
        seen: dict = {}

        def run(cmd, capture_output=True, text=True):
            seen["cmd"] = cmd
            src = cmd[cmd.index("compile") + 1]
            seen["input"] = Path(src).read_text(encoding="utf-8").splitlines()
            Path(cmd[cmd.index("-o") + 1]).write_text(resolved, encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        return run, seen

    def test_platform_entries_reach_the_compile_input(self):
        runner, seen = self._capture_input("mcp==1.29.0\n")
        resolve_pins(
            ["mcp>=1.19.0"],
            ["strands-agents[otel]>=1.0,<2", "bedrock-agentcore==1.17.*"],
            runner=runner,
        )
        assert "strands-agents[otel]>=1.0,<2" in seen["input"]
        assert "bedrock-agentcore==1.17.*" in seen["input"]
        assert "mcp>=1.19.0" in seen["input"]

    def test_a_platform_capped_transitive_is_not_over_pinned(self):
        """The 2026-08-04 failure: `mcp>=1.19.0` resolved alone gives 2.0.0, which
        no `strands-agents` release allows, so the package stage could never lock
        it. With the platform entries in the input the resolver returns 1.29.0."""
        runner, _ = self._capture_input(
            "strands-agents==1.50.2\nmcp==1.29.0\nhttpx==0.28.1\n"
        )
        out = resolve_pins(
            ["mcp>=1.19.0"], ["strands-agents[otel]>=1.0,<2"], runner=runner
        )
        assert out == ["mcp==1.29.0"]

    def test_the_transitive_closure_is_not_returned(self):
        """Dropping `--no-deps` makes the compile output the whole closure. A spec
        must still name only what the caller asked for — the closure belongs in the
        build's hashed lockfile."""
        runner, _ = self._capture_input(
            "strands-agents==1.50.2\nmcp==1.29.0\nhttpx==0.28.1\nopenai==2.53.0\n"
        )
        out = resolve_pins(
            ["mcp>=1.19.0"],
            ["strands-agents[otel]>=1.0,<2", "openai>=2,<3"],
            runner=runner,
        )
        assert out == ["mcp==1.29.0"]

    def test_platform_is_required_so_no_call_site_can_resolve_in_a_vacuum(self):
        with pytest.raises(TypeError):
            resolve_pins(["mcp>=1.19.0"])  # type: ignore[call-arg]
