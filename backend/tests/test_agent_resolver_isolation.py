import subprocess
import zipfile
from pathlib import Path

import pytest

from app.deployer.zip_runtime import _compile_lock
from app.schemas.requirements import resolve_pins
from app.services.requirements_txt import preresolve


def _write_wheel(directory: Path, name: str, version: str, requires: str = "") -> None:
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    if requires:
        metadata += f"Requires-Dist: {requires}\n"
    dist_info = f"{name}-{version}.dist-info"
    with zipfile.ZipFile(directory / f"{name}-{version}-py3-none-any.whl", "w") as wheel:
        wheel.writestr(f"{dist_info}/METADATA", metadata)
        wheel.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        wheel.writestr(f"{dist_info}/RECORD", "")


@pytest.mark.parametrize("resolver", ["package", "pin", "preresolve"])
def test_agent_resolvers_ignore_host_dependency_overrides(tmp_path, monkeypatch, resolver):
    """Resolve real local wheel metadata despite a conflicting host Pillow override."""
    host = tmp_path / "host"
    host.mkdir()
    (host / "pyproject.toml").write_text(
        '[tool.uv]\noverride-dependencies = ["pillow>=13.0,<14"]\n', encoding="utf-8"
    )
    monkeypatch.chdir(host)
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    _write_wheel(wheels, "pillow", "12.3.0")
    _write_wheel(wheels, "pillow", "13.0.0")
    _write_wheel(wheels, "strands_agents_tools", "0.8.3", "pillow>=12.1.1,<13.0.0")
    locks = []

    def run_offline(command, **kwargs):
        result = subprocess.run(
            [*command, "--no-index", "--find-links", str(wheels)], **kwargs
        )
        if result.returncode == 0:
            locks.append(Path(command[command.index("-o") + 1]).read_text(encoding="utf-8"))
        return result

    requirements = ["strands-agents-tools>=0.8.3,<0.9"]
    if resolver == "package":
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        _compile_lock(requirements, build_dir, compile_runner=run_offline)
    elif resolver == "pin":
        assert resolve_pins(requirements, [], runner=run_offline) == [
            "strands-agents-tools==0.8.3"
        ]
    else:
        assert preresolve("\n".join(requirements), runner=run_offline) == {
            "status": "ok", "package_count": 2, "error": None,
        }

    assert len(locks) == 1
    assert "pillow==12.3.0" in locks[0]
    assert "pillow==13.0.0" not in locks[0]
