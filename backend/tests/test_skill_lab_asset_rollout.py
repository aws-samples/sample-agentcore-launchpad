from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

VENDOR = Path(__file__).parents[2] / "vendor" / "skillopt"


def test_vendored_loader_and_rollout_materialize_binary_assets(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    first = b"%PDF-1.7\x00binary"
    second = b"\x89PNG\r\n\x1a\nmore\x00bytes"
    files = {}
    for destination, name, data, media in (
        ("data/source.pdf", "source.pdf", first, "application/pdf"),
        ("images/chart.png", "chart.png", second, "image/png"),
    ):
        digest = hashlib.sha256(data).hexdigest()
        (assets / digest).write_bytes(data)
        files[destination] = {
            "asset": f"sha256:{digest}",
            "name": name,
            "media_type": media,
            "size": len(data),
        }
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps([{"id": "one", "question": "q", "rubric": "r", "files": files}]))
    script = tmp_path / "probe.py"
    script.write_text(
        """
import json, os, pathlib, sys, types
sys.path.insert(0, sys.argv[1])
openai = types.ModuleType('openai'); openai.OpenAI = object; openai.AzureOpenAI = object
sys.modules.setdefault('openai', openai)
from skillopt.envs.skilleval.dataloader import load_tasks
from skillopt.envs.skilleval import rollout
items = load_tasks(sys.argv[2], assets_dir=sys.argv[3])
def prepare_workspace(**kwargs):
    root = pathlib.Path(kwargs['work_dir']); root.mkdir(parents=True, exist_ok=True)
    for src, dst in kwargs.get('copy_files') or []:
        target = root / dst
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(pathlib.Path(src).read_bytes())
    (root / 'task.md').write_text(kwargs['task_text'])
rollout.prepare_workspace = prepare_workspace
rollout.build_manifest = lambda *_: {}
rollout.run_claude_code_exec = lambda **_: ('ok', {})
rollout.diff_manifests = lambda *_: []
result = rollout._rollout_one(items[0], '# skill', sys.argv[4], timeout=60, model='model')
print(json.dumps({'asset_files': items[0]['_asset_files'], 'work_dir': result['work_dir']}))
"""
    )
    work = tmp_path / "work"
    proc = subprocess.run(
        [sys.executable, str(script), str(VENDOR), str(tasks), str(assets), str(work)],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"},
        check=True,
    )
    result = json.loads(proc.stdout)
    root = Path(result["work_dir"])
    assert (root / "data/source.pdf").read_bytes() == first
    assert (root / "images/chart.png").read_bytes() == second
    persisted = json.loads(tasks.read_text())
    assert all("_asset_files" not in item for item in persisted)


def test_vendored_loader_materializes_every_supported_format(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    xlsx = io.BytesIO()
    with zipfile.ZipFile(xlsx, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
    fixtures = {
        "data/book.xlsx": xlsx.getvalue(),
        "data/doc.pdf": b"%PDF-1.7\x00doc",
        "images/pixel.png": b"\x89PNG\r\n\x1a\nbytes",
        "images/photo.jpg": b"\xff\xd8\xffbytes",
        "images/photo.webp": b"RIFF\x04\x00\x00\x00WEBP",
        # Text formats travel the same path: the loader verifies digest+size and
        # never looks at media_type, so nothing about them is format-specific.
        "data/notes.md": "# 标题\n\n- item\n".encode(),
        "data/plain.txt": b"line one\r\nline two",
        "data/rows.csv": b"a,b\n1,2\n",
    }
    files = {}
    for destination, data in fixtures.items():
        digest = hashlib.sha256(data).hexdigest()
        (assets / digest).write_bytes(data)
        files[destination] = {
            "asset": f"sha256:{digest}",
            "name": Path(destination).name,
            "media_type": "application/octet-stream",
            "size": len(data),
        }
    tasks = tmp_path / "all.json"
    tasks.write_text(json.dumps([{"id": "all", "question": "q", "rubric": "r", "files": files}]))
    probe = tmp_path / "load.py"
    probe.write_text(
        """
import json, sys, types
sys.path.insert(0, sys.argv[1])
openai = types.ModuleType('openai'); openai.OpenAI = object; openai.AzureOpenAI = object
sys.modules.setdefault('openai', openai)
from skillopt.envs.skilleval.dataloader import load_tasks
print(json.dumps(load_tasks(sys.argv[2], assets_dir=sys.argv[3])[0]['_asset_files']))
"""
    )
    proc = subprocess.run(
        [sys.executable, str(probe), str(VENDOR), str(tasks), str(assets)],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"},
        check=True,
    )
    pairs = json.loads(proc.stdout)
    assert {destination for _, destination in pairs} == set(fixtures)
    assert all(Path(source).read_bytes() == fixtures[destination] for source, destination in pairs)


def test_vendored_loader_rejects_digest_mismatch(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    digest = hashlib.sha256(b"right").hexdigest()
    (assets / digest).write_bytes(b"wrong")
    tasks = tmp_path / "tasks.json"
    tasks.write_text(
        json.dumps(
            [
                {
                    "id": "one",
                    "question": "q",
                    "rubric": "r",
                    "files": {
                        "data/a.pdf": {
                            "asset": f"sha256:{digest}",
                            "name": "a.pdf",
                            "media_type": "application/pdf",
                            "size": 5,
                        }
                    },
                }
            ]
        )
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(VENDOR / "scripts/validate_tasks.py"),
            "--assets-dir",
            str(assets),
            str(tasks),
        ],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"},
        check=True,
    )
    result = json.loads(proc.stdout)["results"][0]
    assert result["ok"] is False
    assert "digest/size mismatch" in result["error"]


def test_agentcore_tar_worker_output_round_trip_preserves_binary_assets(tmp_path):
    """Exercise the production runner tar, worker extraction/flush, and output restore."""
    script = tmp_path / "transport_probe.py"
    script.write_text(
        """
import io, json, pathlib, sys, types
sys.path.insert(0, sys.argv[1])
openai = types.ModuleType('openai'); openai.OpenAI = object; openai.AzureOpenAI = object
sys.modules.setdefault('openai', openai)

from skillopt.model import agentcore_runner, agentcore_worker

class Body:
    def __init__(self, data): self.data = data
    def read(self): return self.data

class S3:
    def __init__(self, objects): self.objects = dict(objects)
    def get_object(self, Bucket, Key): return {"Body": Body(self.objects[Key])}
    def put_object(self, Bucket, Key, Body):
        self.objects[Key] = Body if isinstance(Body, bytes) else Body.encode()

source = pathlib.Path(sys.argv[2])
restored = pathlib.Path(sys.argv[3])
source.mkdir()
(source / "data").mkdir()
(source / "data" / "sheet.xlsx").write_bytes(b"PK\\x03\\x04\\x00sheet\\xff")
(source / "images").mkdir()
(source / "images" / "chart.png").write_bytes(b"\\x89PNG\\r\\n\\x1a\\nchart\\x00")
prefix = "jobs/roundtrip"
s3 = S3({f"{prefix}/in.tar.gz": agentcore_runner._tar_work_dir(str(source))})
agentcore_worker._s3_client = lambda: s3
agentcore_worker._work_root = lambda: sys.argv[4]
def run_backend(_backend, **kwargs):
    root = pathlib.Path(kwargs["work_dir"])
    sheet = root / "data" / "sheet.xlsx"
    sheet.write_bytes(sheet.read_bytes() + b"-worker")
    return "ok", "raw"
agentcore_worker._run_backend = run_backend
result = agentcore_worker._run_exec_job({
    "backend": "claude_code_exec", "bucket": "bucket", "job_prefix": prefix,
    "wait": True, "sync_interval": 0,
})
agentcore_runner._extract_tar_over_work_dir(s3.objects[result["out_key"]], str(restored))
print(json.dumps({"result": result}))
"""
    )
    source = tmp_path / "source"
    restored = tmp_path / "restored"
    restored.mkdir()
    worker_root = tmp_path / "worker"
    worker_root.mkdir()
    proc = subprocess.run(
        [sys.executable, str(script), str(VENDOR), str(source), str(restored), str(worker_root)],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"},
        check=True,
    )
    assert json.loads(proc.stdout)["result"]["harness_error"] is False
    assert (restored / "data/sheet.xlsx").read_bytes() == b"PK\x03\x04\x00sheet\xff-worker"
    assert (restored / "images/chart.png").read_bytes() == b"\x89PNG\r\n\x1a\nchart\x00"


def test_vendored_validator_reserves_runtime_roots_and_keeps_legacy_case_paths(tmp_path):
    legacy_files = {f"notes/{index:02d}.txt": str(index) for index in range(33)}
    legacy_files.update({"Case/Note.txt": "upper", "case/note.txt": "lower"})

    def validate(files):
        tasks = tmp_path / "tasks.json"
        tasks.write_text(
            json.dumps([{"id": "one", "question": "q", "rubric": "r", "files": files}])
        )
        proc = subprocess.run(
            [sys.executable, str(VENDOR / "scripts" / "validate_tasks.py"), str(tasks)],
            capture_output=True,
            text=True,
            env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"},
            check=True,
        )
        return json.loads(proc.stdout)["results"][0]

    assert validate(legacy_files)["ok"] is True
    for root in (".claude", ".codex", ".git", ".CLAUDE"):
        result = validate({f"{root}/input.txt": "legacy"})
        assert result["ok"] is False
        assert "collides with the evaluation runtime" in result["error"]
