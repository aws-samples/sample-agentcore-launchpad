#!/usr/bin/env python3
"""Parse a pipe-separated access log and print a JSON stats summary.

Usage: python3 parse_logs.py <logfile>

Line format (see references/log-format.md):
    timestamp|method|path|status|latency_ms
Malformed lines are skipped and counted, never fatal.
"""
import json
import math
import sys

TOOL = "parse_logs v1.2"


def parse(path):
    statuses, latencies = [], []
    endpoints = {}
    malformed = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) != 5 or not parts[4].endswith("ms"):
                malformed += 1
                continue
            try:
                status = int(parts[3])
                latency = int(parts[4][:-2])
            except ValueError:
                malformed += 1
                continue
            statuses.append(status)
            latencies.append(latency)
            endpoints[parts[2]] = endpoints.get(parts[2], 0) + 1

    total = len(statuses)
    errors = sum(1 for s in statuses if s >= 500)
    lat_sorted = sorted(latencies)
    p95 = lat_sorted[max(0, math.ceil(0.95 * total) - 1)] if total else 0
    top = min(endpoints, key=lambda e: (-endpoints[e], e)) if endpoints else ""
    return {
        "tool": TOOL,
        "total_requests": total,
        "error_count": errors,
        "error_rate_pct": round(100.0 * errors / total, 1) if total else 0.0,
        "avg_latency_ms": round(sum(latencies) / total, 1) if total else 0.0,
        "p95_latency_ms": p95,
        "top_endpoint": top,
        "skipped_malformed_lines": malformed,
    }


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: parse_logs.py <logfile>")
    print(json.dumps(parse(sys.argv[1]), indent=2))


if __name__ == "__main__":
    main()
