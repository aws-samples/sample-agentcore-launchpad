# Access log format

One request per line, five pipe-separated fields, no header:

    timestamp|method|path|status|latency

- `timestamp` — ISO-8601 UTC, e.g. `2026-06-14T09:12:33Z`
- `method` — HTTP verb
- `path` — request path, no query string
- `status` — HTTP status code; 5xx counts as an error for triage
- `latency` — integer milliseconds with `ms` suffix, e.g. `184ms`

Lines that do not parse (wrong field count, bad status/latency) are
counted as malformed and excluded from all statistics.
