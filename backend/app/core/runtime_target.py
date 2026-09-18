"""The Python platform the zip deploy paths resolve and install for.

One definition, three consumers that MUST agree: the `uv pip compile` resolve,
the `pip install` into the bundle (both in `app/deployer/`), and the
range→pin resolver (`app/schemas/requirements.py`). Resolving against one
platform and installing for another produces a lock that does not describe the
artifact.

The default is `manylinux_2_28` on aarch64. Evidence (measured 2026-09-18 from
inside a deployed AgentCore Runtime direct-code agent, PYTHON_3_13): the runtime
OS is Amazon Linux 2023.12.20260817 on aarch64 with glibc 2.34, so every
manylinux tag up to `manylinux_2_34` is loadable. The official docs recommend
`manylinux2014` (glibc 2.17), which is safe but strictly narrower — packages
that only publish `manylinux_2_26`/`2_28` aarch64 wheels (e.g. `google-re2`,
pulled in by `chromadb`) are unsolvable under it even though they run fine on
the runtime. `2_28` is what current manylinux images actually publish for;
`2_34` would buy nothing today and breaks the day the fleet moves to an older
glibc image, so the headroom stays unspent.

Configurable as `runtime_python_platform` (`LAUNCHPAD_RUNTIME_PYTHON_PLATFORM`),
value `manylinux_2_<minor>` or the legacy alias `manylinux2014`. `manylinux2014`
is the documented safe fallback if a future runtime image ever reports an older
glibc.
"""

import re

from app.core.config import get_settings

# AgentCore Runtime zips run on Python 3.13 regardless of the resolve platform.
TARGET_PYTHON = "3.13"

_PLATFORM_RE = re.compile(r"^manylinux(?:2014|_2_(?P<minor>\d+))$")

# The legacy aliases map onto PEP 600 glibc minors; pip expands neither
# direction on its own (measured: `--platform manylinux_2_28_aarch64` alone
# refuses a manylinux_2_17-only wheel), so the ladder below is built explicitly.
_MANYLINUX2014_MINOR = 17


def _glibc_minor(setting: str) -> int:
    match = _PLATFORM_RE.match(setting)
    if match is None:
        raise ValueError(
            f"runtime_python_platform {setting!r} is not a manylinux platform — "
            "use manylinux_2_<minor> (e.g. manylinux_2_28) or manylinux2014"
        )
    return int(match.group("minor") or _MANYLINUX2014_MINOR)


def uv_platform() -> str:
    """The single `--python-platform` value for `uv pip compile`.

    uv derives the whole compatible-tag set from one platform (a
    `aarch64-manylinux_2_28` resolve accepts `manylinux_2_17` wheels), so no
    ladder is needed on this side.
    """
    return f"aarch64-{get_settings().runtime_python_platform}"


def pip_platforms() -> list[str]:
    """Every `--platform` value the `pip install` step must pass.

    pip treats `--platform` tags as exact strings — it does NOT expand
    `manylinux_2_28_aarch64` down to older glibc tags the way it expands the
    *running* interpreter's platform. One flag would therefore reject the
    `manylinux_2_17`-only wheels most projects publish, undoing the resolve.
    """
    minor = _glibc_minor(get_settings().runtime_python_platform)
    ladder = [f"manylinux_2_{m}_aarch64" for m in range(minor, _MANYLINUX2014_MINOR - 1, -1)]
    # the pre-PEP600 alias many wheel filenames still use (manylinux2014 == 2_17)
    ladder.append("manylinux2014_aarch64")
    return ladder


def pip_platform_args() -> list[str]:
    """`--platform` argv fragments for a pip command line."""
    args: list[str] = []
    for platform in pip_platforms():
        args += ["--platform", platform]
    return args
