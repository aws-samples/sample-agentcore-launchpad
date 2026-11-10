"""N12-bug10: agent packages are assembled under the checkout's ``data/builds/<agent>``.

Live: ``zip_runtime`` built into ``/tmp/launchpad_build_<agent>``; a bootstrap run as root
(host-update.sh step 10 installing a preset) left that dir root-owned and the backend
service (``User=ubuntu``) failed its next repair with ``PermissionError: … deployment_
package.zip``. A checkout-local, git-ignored build dir is owned by whoever runs the backend
and survives /tmp cleaning. N17-obs151 moved the root and the per-agent path into
``deployer/build_dirs.py`` (with their retention); the methods still assemble in one place.
"""

from pathlib import Path

from app.core.config import DATA_DIR
from app.deployer import build_dirs, harness_local, zip_runtime


def test_build_dir_is_inside_the_checkouts_data_dir():
    assert build_dirs.DEFAULT_BUILD_ROOT == DATA_DIR / "builds"
    # tests redirect the live root (conftest `hermetic_build_root`); the per-agent path follows it
    assert build_dirs.BUILD_ROOT != build_dirs.DEFAULT_BUILD_ROOT
    assert zip_runtime.build_dir_for("my-agent") == build_dirs.BUILD_ROOT / "my-agent"
    # both methods assemble their package in the very same place
    assert harness_local.build_dir_for is zip_runtime.build_dir_for is build_dirs.build_dir_for


def test_no_shared_tmp_build_dir_literal_remains():
    for mod in (zip_runtime, harness_local, build_dirs):
        source = Path(mod.__file__).read_text(encoding="utf-8")
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        assert "/tmp/launchpad_build" not in code, mod.__name__
