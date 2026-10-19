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
    assert_plain_ranges,
    is_pinned,
    is_plain_range,
    reresolve_inputs,
    resolve_pins,
    unpinned,
)

PINNED = [
    "requests==2.32.3",
    "strands-agents==1.47.0",
    "strands-agents[otel]==1.47.0",
    "strands-agents[otel,openai]==1.47.0",
    "pydantic==2.13.4 ; python_version >= '3.12'",
    'some-pkg==1.0 ; python_version >= "3.10"',
    'some-pkg==1.0 ; sys_platform != "win32"',
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

UNSAFE_REQUIREMENTS = [
    "a==1.0; m\n-i x",
    "a==1.0\r\n--extra-index-url x",
    'a==1.0; python_version > "3"\x85--index-url x',
    'a==1.0; python_version > "3"\u2028--index-url x',
    'a==1.0; python_version > "3"\u2029--index-url x',
    "a==1.0;-i x",
    "a==1.0; --extra-index-url x",
    "\na==1.0",
    "a==1.0\n",
    "a[\nextra]==1.0",
    "pkg @ https://example.com/pkg.whl#sha256=" + "a" * 64 + "; m\n-i x",
    "pkg @ git+https://github.com/o/r@" + "0" * 40 + ";-i x",
]


class TestIsPinned:
    @pytest.mark.parametrize("entry", PINNED)
    def test_accepts(self, entry):
        assert is_pinned(entry) is True, entry

    @pytest.mark.parametrize("entry", UNPINNED)
    def test_rejects(self, entry):
        assert is_pinned(entry) is False, entry

    @pytest.mark.parametrize("entry", UNSAFE_REQUIREMENTS)
    def test_rejects_requirements_file_injection(self, entry):
        assert not is_pinned(entry)
        with pytest.raises(ValueError):
            _spec(requirements=[entry])

    @pytest.mark.parametrize(
        "control", [chr(code) for code in range(32)] + ["\x7f", "\x85", "\u2028", "\u2029"]
    )
    def test_checks_control_characters_before_stripping_the_marker(self, control):
        assert not is_pinned(f'a==1.0; python_version > "3"{control}')

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

    @pytest.mark.parametrize("entry", UNSAFE_REQUIREMENTS)
    @pytest.mark.parametrize("source", ["entries", "platform"])
    def test_unsafe_input_is_refused_before_resolving(self, entry, source):
        def explode(*args, **kwargs):
            pytest.fail("unsafe requirements reached the resolver")

        entries, platform = ([entry], []) if source == "entries" else (["a>=1"], [entry])
        with pytest.raises(ValueError, match="single ASCII lines"):
            resolve_pins(entries, platform, runner=explode)

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
        assert "aarch64-manylinux_2_28" in seen["cmd"]  # runtime_target default
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


class TestReresolveInputs:
    """What a conversion twin's stale pins are re-resolved FROM (#300, PRD R2/AC4)."""

    def test_a_recorded_range_replaces_the_pin_of_the_same_project(self):
        assert reresolve_inputs(
            ["botocore[crt]==1.43.65", "mcp==2.1.1"],
            ["botocore[crt] >= 1.35.0", "mcp >= 1.19.0"],
        ) == ["botocore[crt] >= 1.35.0", "mcp >= 1.19.0"]

    def test_range_lookup_uses_the_pep503_name(self):
        """`_`/`-`/`.` and case all normalise — the pin's spelling comes from the
        resolver output, the range's from the Harness's pyproject.toml."""
        assert reresolve_inputs(
            ["strands-agents-tools==0.2.0", "Zope.Interface==7.0"],
            ["Strands_Agents_Tools >= 0.1", "zope-interface>=6"],
        ) == ["Strands_Agents_Tools >= 0.1", "zope-interface>=6"]

    def test_a_legacy_twin_relaxes_each_pin_to_its_name(self):
        """Twins converted before `requirement_ranges` existed have no ranges: the
        project name is the input, and extras survive — `botocore[crt]` must not
        quietly become `botocore`."""
        assert reresolve_inputs(["botocore[crt]==1.43.65", "mcp==2.1.1"], []) == [
            "botocore[crt]",
            "mcp",
        ]

    def test_the_legacy_relax_keeps_the_marker(self):
        assert reresolve_inputs(['tomli==2.0.1 ; python_version < "3.11"'], []) == [
            'tomli ; python_version < "3.11"'
        ]

    def test_url_and_vcs_pins_pass_through_unchanged(self):
        url = "pkg @ https://example.com/pkg-1.0-py3-none-any.whl#sha256=" + "a" * 64
        vcs = "other @ git+https://github.com/o/r@" + "0" * 40
        # even with a range recorded for the same name: a direct reference was chosen
        assert reresolve_inputs([url, vcs], ["pkg>=1", "other>=1"]) == [url, vcs]

    def test_order_is_kept_and_ranges_for_unpinned_projects_are_ignored(self):
        assert reresolve_inputs(
            ["b==2.0", "a==1.0", "c==3.0"], ["a>=1", "unrelated>=9"]
        ) == ["b", "a>=1", "c"]

    def test_legacy_inputs_resolve_back_to_pins(self):
        """End to end with `resolve_pins`: the relaxed names compile, and the
        extra is re-attached to the new pin."""
        def run(cmd, capture_output=True, text=True):
            out = cmd[cmd.index("-o") + 1]
            Path(out).write_text("botocore==1.43.114\nmcp==1.30.0\n", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        inputs = reresolve_inputs(["botocore[crt]==1.43.65", "mcp==2.1.1"], [])
        assert resolve_pins(inputs, ["boto3>=1.43.66,<2"], runner=run) == [
            "botocore[crt]==1.43.114",
            "mcp==1.30.0",
        ]


class TestRequirementRanges:
    """`AgentSpec.requirement_ranges` is compile input on a later rebuild, so it may
    hold plain index requirements only (#300, PRD R4/AC6)."""

    @pytest.mark.parametrize(
        "entry",
        [
            "botocore[crt] >= 1.35.0",
            "mcp >= 1.19.0",
            "aws-opentelemetry-distro",
            "requests>=2.32,<3",
            "strands-agents[otel,openai]~=1.47",
            "bedrock-agentcore==1.18.*",
            "pkg!=1.0.1",
            'tomli>=2 ; python_version < "3.11"',
            "foo>=1; platform_machine == 'aarch64' and sys_platform == 'linux'",
            'foo; sys_platform != "win32"',
            'foo; extra == "x"',
            'foo; (python_version >= "3.10")',
            "foo; '3.10' <= python_version",
        ],
    )
    def test_plain_ranges_are_accepted(self, entry):
        assert is_plain_range(entry), entry
        _spec(requirement_ranges=[entry])

    @pytest.mark.parametrize(
        "entry",
        [
            "https://evil.example/pkg-1.0-py3-none-any.whl",
            "pkg @ https://example.com/pkg-1.0.tar.gz",
            "pkg @ git+https://github.com/o/r@" + "0" * 40,
            "git+https://github.com/o/r",
            "-i https://evil.example/simple",
            "--extra-index-url https://evil.example/simple",
            "-r other.txt",
            "mcp>=1.19\n--index-url https://evil.example/simple",
            "mcp>=1.19\r",
            "mcp>=1.19\t",
            "mcp>=1.19 --no-index",
            "mcp>=1.19 # comment",
            "mcp>=1.19 \\",
            # C1 NEL / Unicode line separator: str.splitlines() breaks on both
            "mcp>=1.19\x85--index-url=https://evil.example/simple",
            "mcp >=1.19",
            "mcp\xa0>=1.19",
            # a marker opens with a variable, `(` or a quote — never an option
            "mcp>=1.19;-i https://evil.example/simple",
            "./local/path",
            "",
            "x" * 301,
        ],
    )
    def test_anything_else_is_refused(self, entry):
        assert not is_plain_range(entry), entry
        with pytest.raises(ValueError, match="requirement_ranges"):
            _spec(requirement_ranges=[entry])

    def test_none_and_empty_are_valid(self):
        assert _spec().requirement_ranges is None
        assert _spec(requirement_ranges=[]).requirement_ranges == []
        assert_plain_ranges(None)

    def test_the_entry_count_is_bounded(self):
        assert_plain_ranges([f"pkg{i}>=1" for i in range(100)])
        with pytest.raises(ValueError, match="at most 100"):
            _spec(requirement_ranges=[f"pkg{i}>=1" for i in range(101)])

    def test_every_offender_is_named(self):
        with pytest.raises(ValueError) as excinfo:
            _spec(requirement_ranges=["ok>=1", "-i https://x", "pkg @ https://y"])
        message = str(excinfo.value)
        assert "-i https://x" in message and "pkg @ https://y" in message
        assert "'ok>=1'" not in message
