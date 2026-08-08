#!/usr/bin/env python3
"""E2E: Cedar policy enforcement at the gateway (real ALLOW + DENY).

Uses the platform policy-test endpoint so every evaluation lands in the
decision log the Governance UI renders.

Run:  cd backend && uv run python scripts/e2e_policy.py
"""

import argparse
import sys

from _e2e_client import e2e_client


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--base", default="http://localhost:8000")
    args = parser.parse_args()

    client = e2e_client(args.base, timeout=120)

    cases = [
        ("demo", "hr-database___get_employee", {"employee_id": "EMP-1024"}, "ALLOW"),
        ("demo", "hr-database___create_payout", {"employee_id": "EMP-1024", "amount": 42},
         "DENY"),
        ("admin", "hr-database___create_payout", {"employee_id": "EMP-1024", "amount": 42},
         "ALLOW"),
    ]
    failures = 0
    errors = 0
    for username, tool, arguments, expected in cases:
        res = client.post(
            "/api/governance/policy-test",
            json={"username": username, "tool": tool, "arguments": arguments},
        )
        res.raise_for_status()
        body = res.json()
        outcome = body["outcome"]
        # ERROR means no authorization decision was reached at all (bad demo
        # credentials, gateway unreachable, …). Counting it as a plain mismatch would
        # make an infrastructure outage look like a policy regression, so it is
        # reported separately.
        if outcome == "ERROR":
            errors += 1
            print(f"  ! {body['principal']:<22} {tool:<36} NOT EVALUATED")
            print(f"      {body['detail'][:160]}")
            continue
        ok = outcome == expected
        failures += 0 if ok else 1
        print(f"  {'✓' if ok else '✗'} {body['principal']:<22} {tool:<36} "
              f"{outcome:<6} (expected {expected})")
        if outcome == "DENY":
            print(f"      reason: {body['detail'][:120]}")

    log = client.get("/api/governance/decisions").json()["decisions"]
    print(f"\n  decision log entries: {len(log)} (latest: "
          f"{log[0]['principal']} {log[0]['tool']} {log[0]['outcome']})")

    if errors:
        print(
            f"E2E POLICY: FAIL ({errors} case(s) never evaluated — check the demo "
            "Cognito credentials in config/launchpad.yaml and the gateway URL)"
        )
        return 1
    if failures:
        print(f"E2E POLICY: FAIL ({failures} unexpected outcomes)")
        return 1
    print("E2E POLICY: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
