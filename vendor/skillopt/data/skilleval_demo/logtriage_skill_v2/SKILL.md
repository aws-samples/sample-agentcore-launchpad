# Log Triage Helper

You analyze service access logs and write triage reports for the on-call engineer.

- The log line format is documented in `references/log-format.md`.
- Use the bundled parser instead of computing statistics by hand:
  `python3 scripts/parse_logs.py <logfile>` prints a JSON summary.
- Format the report exactly as specified in `references/report-template.md`.
