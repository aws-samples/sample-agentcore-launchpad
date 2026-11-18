"""Read current batch findings without persisting generated evidence."""

import logging
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from app.evaluation import agentcore_eval as ac
from app.evaluation.models import EvalRun
from app.services.agentcore.client import data_client
from app.services.workspace import WorkspaceContext

logger = logging.getLogger(__name__)


def batch_findings(
    run: EvalRun, *, workspace: WorkspaceContext,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source": "batch", "state": "not_applicable", "aws_status": None,
        "findings": [], "diagnostics": [],
    }
    if run.mode != "evaluators" or not any(
        eid.rsplit("/", 1)[-1] == ac.FAILURE_ANALYSIS for eid in run.evaluators or []
    ):
        return base
    if not run.batch_eval_id:
        state = "unavailable" if run.status in ("failed", "stopped", "completed") else "pending"
        return {**base, "state": state}
    try:
        batch = ac.get_batch_evaluation(data_client(workspace), batch_id=run.batch_eval_id)
    except (BotoCoreError, ClientError) as exc:
        logger.warning("failed to read findings for run %s: %s", run.id, type(exc).__name__)
        return {**base, "state": "unavailable"}
    status = batch.get("status")
    patterns = batch.get("insightResults")
    diagnostics = [str(detail)[:500] for detail in (batch.get("errorDetails") or [])[:10]]
    if status == "COMPLETED" and isinstance(patterns, list):
        state = "ready"
    elif status in ("COMPLETED_WITH_ERRORS", "STOPPED") and isinstance(patterns, list):
        state = "partial"
    elif status in ("COMPLETED_WITH_ERRORS", "FAILED"):
        state = "error"
    elif status in ("COMPLETED", "STOPPED") or not status:
        state = "unavailable"
    else:
        state = "pending"
    return {
        **base, "state": state, "aws_status": status,
        "findings": patterns[:50] if isinstance(patterns, list) else [],
        "diagnostics": diagnostics,
    }
