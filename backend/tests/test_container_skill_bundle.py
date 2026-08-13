"""bundle_skill_paths_into — spec.skills → .claude/skills/{name}/ (stub S3)."""

from pathlib import Path

from app.deployer.zip_runtime import bundle_skill_paths_into

from .conftest import ws_ctx


class StubS3:
    """Paginator + download_file stub over a {key: bytes} store."""

    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        store = self.objects

        class P:
            def paginate(self, Bucket, Prefix):
                contents = [
                    {"Key": k, "Size": len(v)}
                    for k, v in store.items()
                    if k.startswith(Prefix)
                ]
                yield {"Contents": contents}

        return P()

    def download_file(self, bucket, key, target):
        Path(target).write_bytes(self.objects[key])


def test_bundles_explicit_paths(tmp_path: Path):
    s3 = StubS3(
        {
            "skills/web-analyzer/SKILL.md": b"# web-analyzer",
            "skills/web-analyzer/scripts/run.py": b"print(1)",
            "agent-skills/ab12cd34/custom-notes/SKILL.md": b"# custom",
        }
    )
    logs: list[str] = []
    result = bundle_skill_paths_into(
        ["s3://bkt/skills/web-analyzer/", "s3://bkt/agent-skills/ab12cd34/custom-notes/"],
        tmp_path / ".claude",
        logs.append,
        ws_ctx(),
        s3_client=s3,
    )
    assert result["bundled"] == ["web-analyzer", "custom-notes"]
    assert result["files"] == 3
    assert (tmp_path / ".claude/skills/web-analyzer/SKILL.md").read_bytes() == b"# web-analyzer"
    assert (tmp_path / ".claude/skills/web-analyzer/scripts/run.py").exists()
    assert (tmp_path / ".claude/skills/custom-notes/SKILL.md").exists()


def test_empty_and_blank_paths_are_noops(tmp_path: Path):
    result = bundle_skill_paths_into([], tmp_path, lambda m: None, ws_ctx(), s3_client=None)
    assert result == {"bundled": [], "files": 0, "bytes": 0}
    # blank entries filtered before any S3 client is needed
    result = bundle_skill_paths_into(["", "  "], tmp_path, lambda m: None, ws_ctx(), s3_client=None)
    assert result == {"bundled": [], "files": 0, "bytes": 0}


def test_missing_prefix_skips_without_raising(tmp_path: Path):
    s3 = StubS3({"skills/other/SKILL.md": b"x"})
    logs: list[str] = []
    result = bundle_skill_paths_into(
        ["s3://bkt/skills/ghost/"], tmp_path / ".claude", logs.append, ws_ctx(),
        s3_client=s3,
    )
    assert result["bundled"] == []
    assert not (tmp_path / ".claude/skills/ghost").exists()  # empty dir cleaned up


def test_download_error_skips_that_skill_only(tmp_path: Path):
    class Boom(StubS3):
        def download_file(self, bucket, key, target):
            if "bad" in key:
                raise RuntimeError("s3 down")
            super().download_file(bucket, key, target)

    s3 = Boom(
        {
            "skills/bad/SKILL.md": b"x",
            "skills/good/SKILL.md": b"y",
        }
    )
    logs: list[str] = []
    result = bundle_skill_paths_into(
        ["s3://bkt/skills/bad/", "s3://bkt/skills/good/"],
        tmp_path / ".claude",
        logs.append,
        ws_ctx(),
        s3_client=s3,
    )
    assert result["bundled"] == ["good"]
    assert any("download failed" in m for m in logs)
    assert not (tmp_path / ".claude/skills/bad").exists()


def test_container_build_context_includes_skills(tmp_path: Path, monkeypatch):
    """_build_context wires assemble_build_context + skill bundling together."""
    from app.deployer import container as c
    from app.schemas.agent import AgentSpec

    spec = AgentSpec(
        name="sdk-skill-agent",
        method="container",
        system_prompt="hi",
        skills=["s3://bkt/skills/web-analyzer/"],
    )
    s3 = StubS3({"skills/web-analyzer/SKILL.md": b"# web-analyzer"})
    monkeypatch.setattr(
        c,
        "bundle_skill_paths_into",
        lambda paths, dest, log, workspace, **kw: bundle_skill_paths_into(
            paths, dest, log, workspace, s3_client=s3
        ),
    )

    class AgentRow:
        name = "sdk-skill-agent"

    ctx_dir = c._build_context(spec, AgentRow(), lambda m: None, ws_ctx())
    assert (ctx_dir / ".claude/skills/web-analyzer/SKILL.md").exists()
    assert not (ctx_dir / ".claude/agents").exists()  # no baked-in subagents anymore
    assert (ctx_dir / "main.py").exists()
