"""CloudWatch Transaction Search = X-Ray segments destined to CW Logs (hub + workspace
bootstrap)."""

import time
from typing import Any

TRANSACTION_SEARCH_POLL_ATTEMPTS = 30
TRANSACTION_SEARCH_POLL_INTERVAL_S = 5


def _wait_transaction_search_active(xray: Any) -> dict[str, Any]:
    state: dict[str, Any] = {}
    for _ in range(TRANSACTION_SEARCH_POLL_ATTEMPTS):
        state = xray.get_trace_segment_destination()
        if state.get("Status") == "ACTIVE":
            break
        # Deliberate status polling interval; the loop has a fixed attempt cap.
        time.sleep(TRANSACTION_SEARCH_POLL_INTERVAL_S)  # nosemgrep: arbitrary-sleep
    return state


def ensure_transaction_search(xray: Any) -> dict[str, Any]:
    """CloudWatch Transaction Search = X-Ray segments destined to CW Logs."""
    state = xray.get_trace_segment_destination()
    if state.get("Destination") == "CloudWatchLogs":
        if state.get("Status") == "ACTIVE":
            return {"enabled": True, "changed": False, "status": state["Status"]}
        # N11-bug11 (live cn-northwest-1): a second bootstrap minutes after the
        # first found Destination=CloudWatchLogs, Status=PENDING and re-issued
        # the update, which X-Ray rejects with InvalidRequestException
        # "Updates are not allowed while the current status is PENDING". The
        # switch is already underway — just wait for it.
        state = _wait_transaction_search_active(xray)
        return {"enabled": state.get("Status") == "ACTIVE", "changed": False,
                "status": state.get("Status")}
    xray.update_trace_segment_destination(Destination="CloudWatchLogs")
    state = _wait_transaction_search_active(xray)
    return {"enabled": state.get("Status") == "ACTIVE", "changed": True,
            "status": state.get("Status")}
