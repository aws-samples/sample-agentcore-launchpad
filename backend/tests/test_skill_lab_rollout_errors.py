"""N10v3-bug3 (vendored side): a rollout that failed on the platform's infrastructure
(the remote worker could not run the CLI at all) must be an INVALID row — excluded from
scoring — not a scored ``artifact_failure`` zero; a genuine exec failure stays scored.

Runs the vendored tree as a subprocess on the backend interpreter (the backend never
imports ``skillopt``; see ``test_skill_lab_foundation``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

VENDOR = Path(__file__).resolve().parents[2] / "vendor" / "skillopt"

_ROLLOUT_PROBE = """
import json, os, sys, types
sys.path.insert(0, sys.argv[1])
openai = types.ModuleType('openai'); openai.OpenAI = object; openai.AzureOpenAI = object
sys.modules.setdefault('openai', openai)
os.environ["SKILLOPT_TARGET_BACKEND"] = "opencode_exec"

from skillopt.envs.skilleval import rollout
from skillopt.model.agentcore_runner import AgentCoreRunnerError

out = {}
def run(kind, exc):
    def fake_exec(**kwargs):
        raise exc
    rollout.run_opencode_exec = fake_exec
    rollout.get_target_backend = lambda: rollout.OPENCODE_BACKEND
    item = {"id": "t-" + kind, "question": "q", "rubric": "r"}
    out_root = os.path.join(sys.argv[2], kind)
    res = rollout._rollout_one(item, "# skill", out_root, timeout=5, model="m")
    out[kind] = {k: res.get(k) for k in ("score_valid", "error", "error_type")}

run("infra", AgentCoreRunnerError(
    "Remote opencode_exec infrastructure failure (job j1): no workload access token"))
run("exec", RuntimeError("Remote opencode_exec failed (job j2): opencode exited 1"))
print(json.dumps(out))
"""

_WORKER_PROBE = """
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

root = pathlib.Path(sys.argv[2])
source = root / "source"; source.mkdir(); (source / "task.md").write_text("q")
tar = agentcore_runner._tar_work_dir(str(source))
s3 = S3({"jobs/infra/in.tar.gz": tar, "jobs/exec/in.tar.gz": tar})
agentcore_worker._s3_client = lambda: s3
agentcore_worker._work_root = lambda: str(root / "worker")
agentcore_worker._apply_exec_config = lambda backend, cfg: None

def raise_key(**_):
    raise RuntimeError("no workload access token on this invocation")
agentcore_worker.resolve_model_api_key = raise_key
agentcore_worker._run_backend = lambda backend, **kw: ("ok", "raw")
r1 = agentcore_worker._run_exec_job(
    {"backend": "opencode_exec", "bucket": "b", "job_prefix": "jobs/infra", "wait": True})

agentcore_worker.resolve_model_api_key = lambda **_: "key"
def boom(backend, **kw):
    raise RuntimeError("opencode exited 1")
agentcore_worker._run_backend = boom
r2 = agentcore_worker._run_exec_job(
    {"backend": "opencode_exec", "bucket": "b", "job_prefix": "jobs/exec", "wait": True})

results = {k: json.loads(s3.objects[f"jobs/{k}/result.json"]) for k in ("infra", "exec")}
done = {k: json.loads(s3.objects[f"jobs/{k}/done.json"]) for k in ("infra", "exec")}
# the host side: how run_remote_exec surfaces each kind
raised = {}
for kind in ("infra", "exec"):
    try:
        agentcore_runner._raise_for_remote_error("opencode_exec", kind, results[kind])
    except Exception as exc:
        raised[kind] = type(exc).__name__
print(json.dumps({"results": results, "done": done, "raised": raised,
                  "harness_error": [r1["harness_error"], r2["harness_error"]]}))
"""


def _probe(tmp_path: Path, source: str) -> dict:
    script = tmp_path / "probe.py"
    script.write_text(source)
    work = tmp_path / "work"
    work.mkdir()
    proc = subprocess.run(
        [sys.executable, str(script), str(VENDOR), str(work)],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_an_infrastructure_exec_failure_is_an_invalid_row_not_a_zero(tmp_path: Path):
    out = _probe(tmp_path, _ROLLOUT_PROBE)
    infra, exec_ = out["infra"], out["exec"]
    assert infra["score_valid"] is False
    assert infra["error_type"] == "infrastructure"
    assert "no workload access token" in infra["error"]
    # a genuine exec failure keeps the vendored semantics: scored artifact failure
    assert exec_["score_valid"] is True
    assert exec_["error_type"] is None
    assert "opencode exited 1" in exec_["error"]


def test_the_worker_tags_the_error_kind_and_the_runner_raises_accordingly(tmp_path: Path):
    out = _probe(tmp_path, _WORKER_PROBE)
    assert out["harness_error"] == [True, True]
    assert out["results"]["infra"]["error_kind"] == "infrastructure"
    assert out["results"]["exec"]["error_kind"] == "exec"
    assert out["done"]["infra"]["error_kind"] == "infrastructure"
    assert out["done"]["infra"]["state"] == "FAILED"
    # host side: infrastructure → AgentCoreRunnerError (rollout marks the row invalid),
    # exec → plain RuntimeError (rollout scores it as an artifact failure, as before)
    assert out["raised"] == {"infra": "AgentCoreRunnerError", "exec": "RuntimeError"}
