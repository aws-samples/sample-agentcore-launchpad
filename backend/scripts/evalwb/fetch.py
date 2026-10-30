"""Read-only reads from the CN account: session records and the eval-run ledger row.

Records: one Logs Insights query per log group (the session's runtime group, then
``aws/spans``) with the production provider's own filter (``attributes.session.id``), so the
cache holds exactly the record set ``LaunchpadSpansProvider`` would read. Logs Insights
``StartQuery`` / ``GetQueryResults`` read; nothing is created.

Run: ``eval_runs`` lives in the host's SQLite ledger, which only the host can open. The
resolver sends ONE ``AWS-RunShellScript`` command over SSM that opens the ledger with
``mode=ro`` and prints the run, its agent's runtime id, the dataset scenarios, the recorded
results and the custom evaluator rows as one compressed JSON line — the same read-only
pattern the deploy-ops checks use. No HTTP call, no ledger write, no AWS write.

The boto3 clients are built here (not through ``app.services.aws_clients``): the workbench
is a developer tool that signs with this box's named profiles, never a workspace's role.
"""

from __future__ import annotations

import base64
import json
import time
import zlib
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from evalwb.cache import RUNTIME, SPANS, SessionRecords

CN_PROFILE = "cn-northwest-1"
CN_REGION = "cn-northwest-1"
HOST_STACKS = {"default": "launchpad-cn-ec2", "t1": "launchpad-cn-ec2-t1"}
RUNTIME_GROUP_PREFIX = "/aws/bedrock-agentcore/runtimes/"
QUERY_LIMIT = 10000
_MARK = "EVALWB-RUN "


def cn_session(profile: str = CN_PROFILE) -> Any:
    import boto3

    return boto3.Session(profile_name=profile, region_name=CN_REGION)


def runtime_log_group(resource_id: str, endpoint: str = "DEFAULT") -> str:
    return f"{RUNTIME_GROUP_PREFIX}{resource_id}-{endpoint}"


def _insights_ms(value: str) -> int:
    """Logs Insights ``@timestamp`` (``2026-10-23 14:13:25.784``, UTC) → epoch ms."""
    parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _run_query(logs: Any, groups: list[str], query: str, start: int, end: int,
               *, timeout_s: float = 120.0, sleep: Callable[[float], None] = time.sleep
               ) -> list[list[dict[str, str]]]:
    query_id = logs.start_query(logGroupNames=groups, startTime=start, endTime=end,
                                queryString=query)["queryId"]
    deadline = time.monotonic() + timeout_s
    while True:
        response = logs.get_query_results(queryId=query_id)
        status = response.get("status")
        if status == "Complete":
            return response.get("results", [])
        if status in ("Failed", "Cancelled", "Timeout"):
            raise RuntimeError(f"Logs Insights query {status} on {groups}")
        if time.monotonic() > deadline:
            raise RuntimeError(f"Logs Insights query still {status} after {timeout_s:g} s")
        sleep(1.0)


def session_query(session_ids: Iterable[str]) -> str:
    ids = sorted(set(session_ids))
    for sid in ids:  # same guard as the provider: never interpolate an unchecked id
        if "'" in sid or "\\" in sid:
            raise ValueError(f"invalid session id {sid!r}")
    match = (f"attributes.session.id = '{ids[0]}'" if len(ids) == 1
             else "attributes.session.id in [" + ", ".join(f"'{s}'" for s in ids) + "]")
    return (f"fields @timestamp, @message | filter {match} | sort @timestamp asc "
            f"| limit {QUERY_LIMIT}")


def rows_to_records(rows: list[list[dict[str, str]]], group: str) -> dict[str, list[dict]]:
    """Insights rows → ``{session_id: [{timestamp, group, record}]}``."""
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        fields = {f.get("field"): f.get("value") for f in row}
        try:
            record = json.loads(fields.get("@message") or "")
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        sid = (record.get("attributes") or {}).get("session.id")
        if not sid:
            continue
        out.setdefault(sid, []).append(
            {"timestamp": _insights_ms(fields["@timestamp"]), "group": group, "record": record})
    return out


def fetch_sessions(logs: Any, session_ids: list[str], log_group: str, start: datetime,
                   end: datetime, *, batch: int = 10,
                   log: Callable[[str], None] = print) -> list[SessionRecords]:
    """Every session's records from ``log_group`` and ``aws/spans`` in [start, end]."""
    window = (int(start.timestamp()), int(end.timestamp()))
    found: dict[str, list[dict[str, Any]]] = {sid: [] for sid in session_ids}
    for i in range(0, len(session_ids), batch):
        chunk = session_ids[i:i + batch]
        for groups, label in (([log_group], RUNTIME), ([SPANS], SPANS)):
            rows = _run_query(logs, groups, session_query(chunk), *window)
            if len(rows) >= QUERY_LIMIT:
                raise RuntimeError(f"{label}: {len(rows)} rows hit the query limit — "
                                   "use a smaller --batch")
            for sid, recs in rows_to_records(rows, label).items():
                if sid in found:
                    found[sid].extend(recs)
            log(f"  fetched {label:9s} sessions {i + 1}-{i + len(chunk)}: {len(rows)} records")
    fetched_at = datetime.now(UTC).isoformat(timespec="seconds")
    return [SessionRecords(
        session_id=sid, log_group=log_group, records=recs,
        source=f"fetched read-only from {log_group} + {SPANS}",
        meta={"fetched_at": fetched_at,
              "window": [start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")]},
    ) for sid, recs in found.items()]


def fetch_window(created_at: str | None, lookback_hours: int) -> tuple[datetime, datetime]:
    """The query window: a run's creation time −10 min … +26 h (the console's rule for the
    same records, ``observability._content_records``), else the last ``lookback_hours``."""
    now = datetime.now(UTC)
    if created_at:
        started = datetime.fromisoformat(created_at)
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        start = started - timedelta(minutes=10)
        return start, min(now, start + timedelta(hours=26))
    return now - timedelta(hours=lookback_hours), now


# ── the run ledger, read on the host ─────────────────────────────────────────

HOST_SCRIPT = r"""set -u; R=/home/ubuntu/launchpad
set -a; . /etc/launchpad/env 2>/dev/null; set +a
cd $R/backend && sudo -u ubuntu -H --preserve-env env PYTHONDONTWRITEBYTECODE=1 \
  $R/backend/.venv/bin/python -I - <<'PY'
import base64, json, sqlite3, subprocess, sys, zlib
RUN = "__RUN__"
db = sqlite3.connect("file:/home/ubuntu/launchpad/data/launchpad.db?mode=ro", uri=True)
db.row_factory = sqlite3.Row
def rows(sql, *args):
    return [dict(r) for r in db.execute(sql, args)]
def js(v):
    try:
        return json.loads(v) if isinstance(v, str) and (v[:1] in "[{" or v == "null") else v
    except ValueError:
        return v
run = rows("select id, workspace_id, agent_id, agent_name, log_source, dataset_id, "
           "dataset_version, mode, evaluators, status, session_ids, scenario_map, created_at "
           "from eval_runs where id=?", RUN)
out = {"run": None}
if run:
    run = {k: js(v) for k, v in run[0].items()}
    out["run"] = run
    agent = rows("select resource_id, name, method from agents where id=?", run["agent_id"])
    out["agent"] = agent[0] if agent else None
    if run.get("dataset_id"):
        ds = rows("select id, name, kind, items from eval_datasets where id=?", run["dataset_id"])
        out["dataset"] = {k: js(v) for k, v in ds[0].items()} if ds else None
    out["results"] = rows(
        "select session_id, trace_id, evaluator_id, level, score, label, error_type "
        "from eval_results where run_id=?", RUN)
    custom = [e for e in run.get("evaluators") or [] if not str(e).startswith("Builtin.")]
    out["evaluators"] = [{k: js(v) for k, v in r.items()} for e in custom for r in rows(
        "select id, name, definition, level, instructions, rating_scale, model_id, "
        "base_evaluator_id, lambda_arn, lambda_timeout_s from eval_evaluators where id=?", e)]
try:
    sys.path.insert(0, "/home/ubuntu/launchpad/backend")
    from app.core.config import get_settings
    s = get_settings()
    out["settings"] = {"eval_judge_model_id": s.eval_judge_model_id,
                       "observability_spans_source": s.observability_spans_source,
                       "model_region": s.model_region}
except Exception as e:
    out["settings_error"] = type(e).__name__
out["host_head"] = subprocess.run(["git", "-C", "/home/ubuntu/launchpad", "rev-parse", "HEAD"],
                                  capture_output=True, text=True).stdout.strip()
print("__MARK__" + base64.b64encode(zlib.compress(json.dumps(out, default=str).encode())).decode())
PY
"""


def host_instance(session: Any, host: str) -> str:
    stack = HOST_STACKS[host]
    resources = session.client("cloudformation").describe_stack_resources(StackName=stack)
    ids = [r["PhysicalResourceId"] for r in resources["StackResources"]
           if r["ResourceType"] == "AWS::EC2::Instance"]
    if len(ids) != 1:
        raise RuntimeError(f"stack {stack}: expected one EC2 instance, found {ids}")
    return ids[0]


def parse_host_output(stdout: str) -> dict[str, Any]:
    line = next((x for x in stdout.splitlines() if x.startswith(_MARK)), None)
    if line is None:
        raise RuntimeError("the host printed no run payload — see the SSM output: "
                           + stdout[-400:])
    try:
        return json.loads(zlib.decompress(base64.b64decode(line[len(_MARK):])))
    except (ValueError, zlib.error) as exc:  # SSM truncates stdout at 24 000 chars
        raise RuntimeError(f"run payload truncated or corrupt ({len(line)} chars)") from exc


def _ssm_payload(session: Any, host: str, script: str, comment: str, *,
                 instance_id: str | None, timeout_s: int,
                 sleep: Callable[[float], None]) -> dict[str, Any]:
    instance = instance_id or host_instance(session, host)
    ssm = session.client("ssm")
    command = ssm.send_command(
        InstanceIds=[instance], DocumentName="AWS-RunShellScript", Comment=comment,
        Parameters={"commands": [script], "executionTimeout": [str(timeout_s)]},
    )["Command"]["CommandId"]
    deadline = time.monotonic() + timeout_s + 30
    while True:
        sleep(2.0)
        try:
            inv = ssm.get_command_invocation(CommandId=command, InstanceId=instance)
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        if inv["Status"] in ("Success", "Failed", "Cancelled", "TimedOut"):
            break
        if time.monotonic() > deadline:
            raise RuntimeError(f"SSM command {command} still {inv['Status']}")
    if inv["Status"] != "Success":
        raise RuntimeError(f"SSM {inv['Status']}: {inv.get('StandardErrorContent', '')[-400:]}")
    payload = parse_host_output(inv.get("StandardOutputContent", ""))
    payload["host"], payload["instance_id"] = host, instance
    return payload


def resolve_run(session: Any, host: str, run_id: str, *, instance_id: str | None = None,
                timeout_s: int = 120, sleep: Callable[[float], None] = time.sleep
                ) -> dict[str, Any]:
    """``{run, agent, dataset?, results, evaluators, settings, host_head}`` of one run."""
    if not run_id.isalnum():
        raise ValueError(f"invalid run id {run_id!r}")
    script = HOST_SCRIPT.replace("__RUN__", run_id).replace("__MARK__", _MARK)
    payload = _ssm_payload(session, host, script, f"eval-wb read-only run resolve {run_id}",
                           instance_id=instance_id, timeout_s=timeout_s, sleep=sleep)
    if not payload.get("run"):
        raise RuntimeError(f"run {run_id} not found on host {host}")
    return payload


AGENTS_SCRIPT = HOST_SCRIPT.split("RUN = ")[0] + r"""IDS = __IDS__
db = sqlite3.connect("file:/home/ubuntu/launchpad/data/launchpad.db?mode=ro", uri=True)
rows = db.execute("select id, name, method, resource_id from agents where id in (%s)"
                  % ",".join("?" * len(IDS)), IDS).fetchall()
out = {"agents": {r[0]: {"name": r[1], "method": r[2], "resource_id": r[3]} for r in rows}}
print("__MARK__" + base64.b64encode(zlib.compress(json.dumps(out).encode())).decode())
PY
"""


def resolve_agents(session: Any, host: str, agent_ids: list[str], *,
                   instance_id: str | None = None, timeout_s: int = 120,
                   sleep: Callable[[float], None] = time.sleep) -> dict[str, dict[str, Any]]:
    """``{agent id: {name, method, resource_id}}`` from the host ledger (read-only)."""
    ids = sorted(set(agent_ids))
    if not all(i.isalnum() for i in ids):
        raise ValueError("invalid agent id")
    script = AGENTS_SCRIPT.replace("__IDS__", json.dumps(ids)).replace("__MARK__", _MARK)
    return _ssm_payload(session, host, script, "eval-wb read-only agent resolve",
                        instance_id=instance_id, timeout_s=timeout_s, sleep=sleep)["agents"]


def run_log_group(payload: dict[str, Any]) -> str:
    """The runtime group a run's sessions wrote to: a log-source run's own group, else the
    agent runtime's DEFAULT endpoint group (what the hub passes the provider)."""
    source = payload["run"].get("log_source")
    source = source if isinstance(source, dict) else {}
    groups = [g for g in source.get("log_group_names") or [] if g != SPANS]
    if groups:
        return groups[0]
    agent = payload.get("agent") or {}
    if not agent.get("resource_id"):
        raise RuntimeError("the run's agent has no runtime id — pass --log-group")
    return runtime_log_group(agent["resource_id"])
