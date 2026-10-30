"""N10v4-bug1: every OpenCode exec on the Skill Lab worker died with "database is locked".

``agentcore_worker._work_root()`` preferred the Runtime's session-storage mount
(``/mnt/workspace``), and OpenCode's home — ``$XDG_DATA_HOME`` with its sqlite db — sits
beside the work_dir, so the db landed on a network filesystem where sqlite locking does not
work. The worker now keeps the OpenCode XDG dirs on LOCAL container disk per job (and drops
them afterwards) and places the work_dir on local disk too unless the job asks for the
persistent mount. Like ``test_skill_lab_worker_context``, the vendored tree is probed in a
subprocess with a fake mount path, a fake S3 and a fake CLI backend — no AWS, no OpenCode.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

VENDOR = Path(__file__).resolve().parents[2] / "vendor" / "skillopt"

_PROBE = """
import io, json, os, sys, tarfile, tempfile, types
sys.path.insert(0, sys.argv[1])
openai = types.ModuleType('openai'); openai.OpenAI = object; openai.AzureOpenAI = object
sys.modules.setdefault('openai', openai)
from skillopt.model import agentcore_worker as w, opencode_backend as oc

mount = sys.argv[2]            # a writable dir standing in for /mnt/workspace
persistent = sys.argv[3] == "1"
w.SESSION_STORAGE_ROOT = mount
w._model_api_key_for_job = lambda job_id: "key"

def tarball():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(w.TAR_ROOT); info.type = tarfile.DIRTYPE; info.mode = 0o755
        tar.addfile(info)
    return buf.getvalue()

class S3:
    def __init__(self): self.puts = {}
    def get_object(self, Bucket, Key):
        if Key.endswith("/in.tar.gz"): return {"Body": io.BytesIO(tarball())}
        raise Exception("NoSuchKey")
    def put_object(self, Bucket, Key, Body): self.puts[Key] = Body
w._s3_client = lambda: S3()
w._cancel_requested = lambda s3, bucket, prefix: False

seen = {}
def fake_backend(backend, **kw):
    seen["kw"] = {k: v for k, v in kw.items() if k in ("work_dir", "home")}
    seen["home_exists_during_exec"] = os.path.isdir(kw["home"])
    # the env the CLI child would get for this job: every XDG dir under the explicit home
    cfg_path = oc.write_opencode_config(kw["work_dir"], {"$schema": "x"}, home=kw["home"])
    env = oc.subprocess_env(cfg_path, base={"PATH": os.environ["PATH"]}, work_dir=kw["work_dir"],
                            home=kw["home"], base_url="https://bedrock.example")
    seen["cfg_path"] = cfg_path
    seen["env"] = {k: env[k] for k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
                                      "XDG_STATE_HOME", "TMPDIR")}
    # the default derivation (no explicit home) would have put the home beside the work_dir
    seen["default_home"] = oc.job_home(kw["work_dir"])
    return "answer", "raw"
w._run_backend = fake_backend

ack = w.handle_payload({"action": "exec", "backend": "opencode_exec", "bucket": "b",
                        "job_prefix": "skill-lab/exec-jobs/job77", "wait": True,
                        "persistent_workspace": persistent})
seen["home_exists_after"] = os.path.isdir(seen["kw"]["home"])
seen["ping"] = w.handle_payload({"action": "ping"})
print(json.dumps({"ack": ack, "seen": seen, "tmp": tempfile.gettempdir(), "mount": mount}))
"""


def _probe(tmp_path: Path, *, persistent: bool) -> dict:
    mount = tmp_path / "mnt-workspace"
    mount.mkdir()
    script = tmp_path / "probe.py"
    script.write_text(_PROBE)
    local_tmp = tmp_path / "local-tmp"
    local_tmp.mkdir()
    proc = subprocess.run(
        [sys.executable, str(script), str(VENDOR), str(mount), "1" if persistent else "0"],
        capture_output=True, text=True, check=True,
        # the container's local disk: an isolated tmp, so the assertions are path-exact
        env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1",
             "TMPDIR": str(local_tmp)},
    )
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    out["local_tmp"] = str(local_tmp)
    return out


def _under(path: str, root: str) -> bool:
    return os.path.realpath(path).startswith(os.path.realpath(root) + os.sep)


def test_opencode_home_and_work_dir_default_to_local_disk_not_the_session_mount(tmp_path):
    out = _probe(tmp_path, persistent=False)
    assert out["ack"]["ok"] is True and out["ack"]["harness_error"] is False
    seen = out["seen"]
    # the work_dir is on local disk although the mount exists and is writable
    assert _under(seen["kw"]["work_dir"], out["local_tmp"])
    assert not _under(seen["kw"]["work_dir"], out["mount"])
    # the OpenCode home is per job, on local disk, present while the CLI runs, gone after
    assert seen["kw"]["home"] == os.path.join(out["local_tmp"], "opencode-homes", "job77")
    assert seen["home_exists_during_exec"] is True and seen["home_exists_after"] is False
    # every XDG dir (sqlite db under XDG_DATA_HOME) + TMPDIR + opencode.json live in it
    for var, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                     ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state"), ("TMPDIR", "tmp")):
        assert seen["env"][var] == os.path.join(seen["kw"]["home"], sub), var
        assert not _under(seen["env"][var], out["mount"])
    assert seen["cfg_path"] == os.path.join(seen["kw"]["home"], "opencode.json")
    # ping tells the operator where things go now
    ping = seen["ping"]
    assert ping["session_storage"] is False and ping["session_storage_writable"] is True
    assert ping["work_root"] == out["local_tmp"]
    assert ping["opencode_home_root"] == os.path.join(out["local_tmp"], "opencode-homes")


def test_a_job_that_asks_for_the_persistent_mount_keeps_its_work_dir_there_but_not_the_db(
    tmp_path,
):
    out = _probe(tmp_path, persistent=True)
    seen = out["seen"]
    assert _under(seen["kw"]["work_dir"], out["mount"])
    # without the explicit home the db would have been beside the work_dir — on the mount
    assert _under(seen["default_home"], out["mount"])
    # ...the worker names a local home instead, so every XDG dir stays off the mount
    assert _under(seen["kw"]["home"], out["local_tmp"])
    assert all(_under(p, out["local_tmp"]) for p in seen["env"].values())
    assert seen["home_exists_after"] is False


def test_job_home_and_subprocess_env_honour_an_explicit_home(tmp_path):
    # the opencode_backend seam itself, host-side (no worker): explicit beats derived
    sys.path.insert(0, str(VENDOR))
    # A fake `openai` only for this import; it must NOT stay in sys.modules — a later test in
    # the same xdist worker importing the real client (strands.models.openai) would otherwise
    # see "'openai' is not a package" (gate failure 2026-10-14).
    added_fake_openai = "openai" not in sys.modules
    try:
        import types

        if added_fake_openai:
            openai = types.ModuleType("openai")
            openai.OpenAI = object  # type: ignore[attr-defined]
            openai.AzureOpenAI = object  # type: ignore[attr-defined]
            sys.modules["openai"] = openai
        from skillopt.model import opencode_backend as oc
    finally:
        sys.path.remove(str(VENDOR))
        if added_fake_openai:
            sys.modules.pop("openai", None)
    work = tmp_path / "pred" / "work"
    work.mkdir(parents=True)
    assert oc.job_home(str(work)) == str(tmp_path / "pred" / ".opencode-home")
    home = tmp_path / "elsewhere"
    assert oc.job_home(str(work), str(home)) == str(home)
    cfg = oc.write_opencode_config(str(work), {"$schema": "x"}, home=str(home))
    assert cfg == str(home / "opencode.json")
    env = oc.subprocess_env(cfg, base={"PATH": os.environ["PATH"]}, work_dir=str(work),
                            home=str(home), base_url="https://bedrock.example")
    assert env["XDG_DATA_HOME"] == str(home / "data") and env["TMPDIR"] == str(home / "tmp")
    assert not (tmp_path / "pred" / ".opencode-home").exists()
