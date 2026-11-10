"""Refresh the live Gateway targets' inline tool schemas from ``lab-assets/tools.json``.

    python3 refresh_targets.py            # dry run (default): per-target changed / unchanged
    python3 refresh_targets.py --apply    # update_gateway_target for the targets that differ

``deploy_tools.py`` creates a Gateway target once and skips it on every later run, so a change
to a tool description or input schema in ``tools.json`` never reached a live Gateway
(N17-obs543: the knowledge tools' descriptions now carry the exact
``launchpad_gw_servicenow-knowledge___<tool>`` call names). For every target the manifest
records (``gateway.targets``), this script reads the live target, compares its
``toolSchema.inlinePayload`` with the local list (canonical JSON, order of the tools kept) and,
with ``--apply``, calls ``update_gateway_target`` with the local payload — name, description,
Lambda ARN and credential provider are passed through unchanged — then waits for ``READY``.
Tool NAMES are expected to stay the same (Cedar rules name ``<target>___<tool>``); a renamed or
added / removed tool is reported as such and still applied, so read the dry run first. A target
in the manifest but missing from ``tools.json`` (or the reverse) is reported and skipped; a
target whose schema is not inline (S3) is reported as unsupported. ``workshop.py tools`` runs
the ``--apply`` form right after ``deploy_tools.py``. Every run writes
``<state_dir>/evidence/refresh-targets-<ts>.json``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

READY_ATTEMPTS = 60
READY_SLEEP = 3.0


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True,
                      help="read the live targets and print what would change (default)")
    mode.add_argument("--apply", action="store_true",
                      help="update the targets whose inline payload differs")
    args = p.parse_args(argv)
    if args.apply:
        args.dry_run = False
    return args


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def inline_payload(target: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The live target's inline tool list, or None when the schema is not inline."""
    config = ((target.get("targetConfiguration") or {}).get("mcp") or {}).get("lambda") or {}
    schema = config.get("toolSchema") or {}
    payload = schema.get("inlinePayload")
    return list(payload) if isinstance(payload, list) else None


def tool_diff(live: list[dict[str, Any]], local: list[dict[str, Any]]) -> list[str]:
    """One line per tool that differs: ``name: description`` / ``name: schema`` /
    ``name: added`` / ``name: removed``; empty when the two payloads are the same."""
    live_by = {t.get("name"): t for t in live}
    local_by = {t.get("name"): t for t in local}
    lines: list[str] = []
    for name, tool in local_by.items():
        old = live_by.get(name)
        if old is None:
            lines.append(f"{name}: added")
            continue
        parts = []
        if (old.get("description") or "") != (tool.get("description") or ""):
            parts.append("description")
        if canonical(old.get("inputSchema")) != canonical(tool.get("inputSchema")):
            parts.append("inputSchema")
        if canonical(old.get("outputSchema")) != canonical(tool.get("outputSchema")):
            parts.append("outputSchema")
        if parts:
            lines.append(f"{name}: {' + '.join(parts)}")
    lines.extend(f"{name}: removed" for name in live_by if name not in local_by)
    if not lines and canonical(live) != canonical(local):
        lines.append("(tool order)")
    return lines


def update_body(gid: str, live: dict[str, Any], local: list[dict[str, Any]]) -> dict[str, Any]:
    """The ``update_gateway_target`` request: everything the live target has, with the local
    inline payload in place of its tool list."""
    lambda_config = dict(live["targetConfiguration"]["mcp"]["lambda"])
    lambda_config["toolSchema"] = {"inlinePayload": local}
    body: dict[str, Any] = {
        "gatewayIdentifier": gid,
        "targetId": live["targetId"],
        "name": live["name"],
        "targetConfiguration": {"mcp": {"lambda": lambda_config}},
    }
    if live.get("description"):
        body["description"] = live["description"]
    if live.get("credentialProviderConfigurations"):
        body["credentialProviderConfigurations"] = live["credentialProviderConfigurations"]
    return body


def wait_ready(gw, gid: str, target_id: str, *, sleep=time.sleep,
               attempts: int = READY_ATTEMPTS) -> dict[str, Any]:
    for _ in range(attempts):
        r = gw.get_gateway_target(gatewayIdentifier=gid, targetId=target_id)
        if r.get("status") == "READY":
            return r
        if r.get("status") in ("FAILED", "DELETE_UNSUCCESSFUL"):
            raise RuntimeError(f"target {target_id} {r.get('status')}: {r.get('statusReasons')}")
        sleep(READY_SLEEP)
    raise RuntimeError(f"target {target_id} not READY after the update")


def refresh(gw, manifest: dict[str, Any], schemas: dict[str, list[dict[str, Any]]], *,
            apply: bool, log=print, sleep=time.sleep) -> list[dict[str, Any]]:
    """Compare every recorded target with ``schemas`` (the parsed ``tools.json``) and, with
    ``apply``, update the ones that differ. Returns one result row per target name."""
    gateway = manifest.get("gateway") or {}
    gid = gateway.get("id")
    recorded = gateway.get("targets") or {}
    if not gid or not recorded:
        raise SystemExit("no Gateway targets recorded in the manifest (run deploy_tools.py)")
    results: list[dict[str, Any]] = []
    for name, row in recorded.items():
        entry: dict[str, Any] = {"target": name, "id": row.get("id")}
        local = schemas.get(name)
        if local is None:
            entry["action"] = "SKIPPED (not in tools.json)"
            log(f"  [{name}] {entry['action']}")
            results.append(entry)
            continue
        live = gw.get_gateway_target(gatewayIdentifier=gid, targetId=row["id"])
        payload = inline_payload(live)
        if payload is None:
            entry["action"] = "UNSUPPORTED (tool schema is not inline)"
            log(f"  [{name}] {entry['action']}")
            results.append(entry)
            continue
        changes = tool_diff(payload, local)
        entry["changes"] = changes
        if not changes:
            entry["action"] = "UNCHANGED"
            log(f"  [{name}] UNCHANGED ({len(local)} tools)")
        elif apply:
            gw.update_gateway_target(**update_body(gid, live, local))
            after = wait_ready(gw, gid, row["id"], sleep=sleep)
            entry["action"] = "UPDATED"
            entry["updated_at"] = str(after.get("updatedAt") or "")
            log(f"  [{name}] UPDATED: " + "; ".join(changes))
        else:
            entry["action"] = "WOULD UPDATE"
            log(f"  [{name}] WOULD UPDATE: " + "; ".join(changes))
        results.append(entry)
    for name in schemas:
        if name not in recorded:
            entry = {"target": name, "id": None, "action": "NOT DEPLOYED (run deploy_tools.py)"}
            log(f"  [{name}] {entry['action']}")
            results.append(entry)
    return results


def main(argv=None) -> int:
    import lab

    args = parse_args(argv)
    lab.sync_assets()  # N17-obs558: tools.json follows the package, not the first install
    manifest = lab.load()
    schemas = json.loads((lab.ROOT / "lab-assets/tools.json").read_text())
    gw = lab.AWS.client("bedrock-agentcore-control")
    print("DRY RUN — nothing is written" if args.dry_run else "APPLY", flush=True)
    results = refresh(gw, manifest, schemas, apply=args.apply)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    lab.evidence(f"refresh-targets-{ts}.json",
                 {"mode": "apply" if args.apply else "dry-run", "results": results})
    pending = [r["target"] for r in results if r["action"] == "WOULD UPDATE"]
    if pending:
        print(f"{len(pending)} target(s) differ from tools.json: {', '.join(pending)}"
              " — rerun with --apply", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
