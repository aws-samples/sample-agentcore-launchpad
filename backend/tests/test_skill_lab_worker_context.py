"""N10v3-bug2: the Skill Lab worker's background exec job must see the request's
Runtime-injected workload access token.

``BedrockAgentCoreApp`` stores the token (and the request headers) in per-request
``contextvars``; a bare ``threading.Thread`` starts with an EMPTY context, so the job
thread saw ``None`` and every rollout failed with "no workload access token on this
invocation". The vendored tree is never imported by the backend (see
``test_skill_lab_foundation``), so the probe runs as a subprocess on the backend
interpreter, which carries the pinned ``bedrock-agentcore`` SDK.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

VENDOR = Path(__file__).resolve().parents[2] / "vendor" / "skillopt"

_PROBE = """
import contextvars, json, sys, threading, types
sys.path.insert(0, sys.argv[1])
openai = types.ModuleType('openai'); openai.OpenAI = object; openai.AzureOpenAI = object
sys.modules.setdefault('openai', openai)

from bedrock_agentcore.runtime.context import BedrockAgentCoreContext
from skillopt.model import agentcore_worker

seen = {}
done = threading.Event()
hooks = []
resolved = []

def fake_resolve(**_):
    # N12-bug8 pattern: the exchange runs on the REQUEST thread, with the injected token
    resolved.append({"thread": threading.current_thread().name,
                     "token": BedrockAgentCoreContext.get_workload_access_token()})
    return "global-bedrock-key"

agentcore_worker.resolve_model_api_key = fake_resolve

def fake_job(payload):
    seen["token"] = BedrockAgentCoreContext.get_workload_access_token()
    seen["headers"] = BedrockAgentCoreContext.get_request_headers()
    seen["thread"] = threading.current_thread().name
    # a pool thread with an EMPTY context still gets the cached key, no second exchange
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        seen["pool_key"] = pool.submit(agentcore_worker._model_api_key_for_job, "job1").result()
    done.set()
    return {"ok": True}

agentcore_worker._run_exec_job = fake_job
agentcore_worker._TASK_HOOKS["add"] = lambda name: hooks.append(("add", name)) or 7
agentcore_worker._TASK_HOOKS["complete"] = lambda task_id: hooks.append(("complete", task_id))

def request():
    # What BedrockAgentCoreApp does for one invocation before calling the entrypoint.
    BedrockAgentCoreContext.set_workload_access_token("tok-from-request")
    BedrockAgentCoreContext.set_request_headers({"X-Amzn-Bedrock-AgentCore-Custom-Tr": "t1"})
    return agentcore_worker.handle_payload({
        "action": "exec", "backend": "opencode_exec", "bucket": "b", "job_prefix": "exec-jobs/job1",
    })

ack = contextvars.copy_context().run(request)
finished = done.wait(10)
for _ in range(100):
    if any(kind == "complete" for kind, _ in hooks):
        break
    threading.Event().wait(0.02)
print(json.dumps({"ack": ack, "finished": finished, "seen": seen, "hooks": hooks,
                  "resolved": resolved, "cached": agentcore_worker._MODEL_API_KEY,
                  "main_sees": BedrockAgentCoreContext.get_workload_access_token()}))
"""

_FAIL_PROBE = """
import contextvars, json, sys, types
sys.path.insert(0, sys.argv[1])
openai = types.ModuleType('openai'); openai.OpenAI = object; openai.AzureOpenAI = object
sys.modules.setdefault('openai', openai)
from bedrock_agentcore.runtime.context import BedrockAgentCoreContext
from skillopt.model import agentcore_worker

def refuse(**_):
    raise RuntimeError("no workload access token on this invocation")
agentcore_worker.resolve_model_api_key = refuse
steps = []
def fake_job(payload):
    try:
        agentcore_worker._model_api_key_for_job("job1")
    except RuntimeError as exc:
        steps.append(f"job raised: {exc}")
    return {"ok": True}
agentcore_worker._run_exec_job = fake_job
ack = agentcore_worker.handle_payload({"action": "exec", "backend": "opencode_exec",
    "bucket": "b", "job_prefix": "exec-jobs/job1", "wait": True})
print(json.dumps({"ack": ack, "steps": steps, "parked": list(agentcore_worker._MODEL_KEY_ERRORS),
                  "cached": agentcore_worker._MODEL_API_KEY}))
"""


def _run_probe(tmp_path: Path, source: str = _PROBE) -> dict:
    script = tmp_path / "probe.py"
    script.write_text(source)
    proc = subprocess.run(
        [sys.executable, str(script), str(VENDOR)],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"},
        check=True,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_background_exec_job_inherits_the_request_workload_token(tmp_path: Path):
    out = _run_probe(tmp_path)
    assert out["ack"] == {"ok": True, "accepted": True, "job_prefix": "exec-jobs/job1"}
    assert out["finished"] is True
    # the job ran off the request thread …
    assert out["seen"]["thread"] == "exec-job1"
    # … and still saw the token + headers the Runtime injected for this invocation
    assert out["seen"]["token"] == "tok-from-request"
    assert out["seen"]["headers"] == {"X-Amzn-Bedrock-AgentCore-Custom-Tr": "t1"}
    # the request context never leaked into the process-wide context
    assert out["main_sees"] is None


def test_the_key_is_exchanged_on_the_request_thread_and_cached_for_the_job(tmp_path: Path):
    """N12-bug8 pattern (B): ``_handle_exec`` resolves the key before the job leaves the
    request thread; the job's own threads read the process cache, never Identity."""
    out = _run_probe(tmp_path)
    assert out["resolved"] == [{"thread": "MainThread", "token": "tok-from-request"}]
    assert out["cached"] == {"key": "global-bedrock-key"}
    assert out["seen"]["pool_key"] == "global-bedrock-key"


def test_a_failed_request_thread_exchange_is_the_jobs_infrastructure_error(tmp_path: Path):
    """The exchange failure is parked for the job and raised at its key step (→ result.json
    / done.json with ``error_kind: infrastructure``), the ack itself still succeeds."""
    out = _run_probe(tmp_path, _FAIL_PROBE)
    assert out["ack"] == {"ok": True}
    assert out["steps"] == ["job raised: no workload access token on this invocation"]
    assert out["parked"] == [] and out["cached"] == {}  # consumed once, nothing poisoned


def test_background_exec_job_keeps_the_healthy_busy_task_tracking(tmp_path: Path):
    out = _run_probe(tmp_path)
    assert out["hooks"] == [["add", "exec:job1"], ["complete", 7]]
