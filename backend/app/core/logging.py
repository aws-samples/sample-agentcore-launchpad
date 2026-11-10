"""Process logging for the backend (r7-i addendum 6).

Uvicorn configures only its own loggers (`uvicorn`, `uvicorn.error`, `uvicorn.access`);
the root logger — where every `launchpad.*` / `app.services.*` record ends up — had no
handler at all. Python's `lastResort` handler then printed WARNING+ records as the bare
message (no level, no logger name) and dropped INFO entirely, so on a host
`journalctl -u launchpad-backend | grep WARNING` or `grep opensearch_retry` found nothing
even while retries were being logged. `configure_logging()` gives the root logger one
stderr handler (stderr → journald under systemd, the terminal under `make dev`) with
`LOG_FORMAT`, once, and leaves anything that already owns the root logger alone
(`uvicorn --log-config`, an operator's `dictConfig`, pytest's capture). Uvicorn's access
lines keep their own format: `uvicorn.access` has its own handler and does not propagate.

Root at INFO would also let `httpx` / `httpcore` (the Bedrock model and judge calls) emit
one INFO line per request into the journal and bury the WARNINGs operators grep for, so
those two library loggers are pinned at WARNING (r7-j) — unless an operator already set
them explicitly. `launchpad.*` and `app.*` stay at INFO.

rev-r20 Nit 3 (N17-obs203 §8.1 hardening): `botocore` (and `boto3`, `urllib3`) are pinned
at INFO. At DEBUG botocore logs every request and response body — a `PutParameter` /
`GetParameter` of a `launchpad-*-api-key` SecureString included, value and all — so an
operator's root DEBUG (`configure_logging(logging.DEBUG)`, a `dictConfig` root at DEBUG)
must never reach those loggers by inheritance. An explicit operator level on them is left
alone, like the quiet pair.
"""

import logging
import sys

LOG_FORMAT = "%(levelname)s %(name)s: %(message)s"
HANDLER_NAME = "launchpad-root"
# chatty HTTP-client loggers whose INFO is one line per model/judge call
QUIET_LOGGERS = ("httpx", "httpcore")
QUIET_LEVEL = logging.WARNING
# AWS SDK / HTTP loggers whose DEBUG prints request and response bodies (SSM parameter values)
PINNED_LOGGERS = ("botocore", "boto3", "urllib3")
PINNED_LEVEL = logging.INFO


def configure_logging(level: int = logging.INFO) -> bool:
    """Attach the formatted stderr handler to the root logger if nobody has configured it
    yet. Returns True when this call installed it, False when the root logger already had
    handlers (including our own — the call is idempotent). Independently of who owns the
    root logger, `QUIET_LOGGERS` that nobody configured (level NOTSET) are raised to
    `QUIET_LEVEL` so their per-request INFO does not flood the journal, and `PINNED_LOGGERS`
    to `PINNED_LEVEL` so a root at DEBUG never makes botocore print parameter values."""
    for name in QUIET_LOGGERS:
        quiet = logging.getLogger(name)
        if quiet.level == logging.NOTSET:
            quiet.setLevel(QUIET_LEVEL)
    for name in PINNED_LOGGERS:
        pinned = logging.getLogger(name)
        if pinned.level == logging.NOTSET:
            pinned.setLevel(PINNED_LEVEL)
    root = logging.getLogger()
    if root.handlers:
        return False
    handler = logging.StreamHandler(sys.stderr)
    handler.set_name(HANDLER_NAME)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    return True
