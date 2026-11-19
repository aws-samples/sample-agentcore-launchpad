"""AWS Agent Insights read contract and response projections."""

from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from app.core.errors import AppError, NotFoundError, aws_error_code

DEFAULT_STORE = "default-insight-store"
_PAGE_SIZE = 25
_DENIED = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}
_NOT_FOUND = {"ResourceNotFound", "ResourceNotFoundException", "NotFoundException"}
_UNSUPPORTED = {
    "UnknownOperationException", "NotImplementedException",
    "UnsupportedOperation", "UnsupportedOperationException",
}
_SUMMARY_FIELDS = (
    "agentInsightId", "applicationName", "type", "name", "description", "createdAt",
    "updatedAt", "lastSeenAt", "affectedSessionCount", "archiveStatus",
    "customerMetadataStatus",
)


def _read_operation(client: Any, name: str, **kwargs: Any) -> dict[str, Any]:
    operation = getattr(client, name, None)
    if operation is None:
        raise AppError("insight.unsupported", "Agent Insights API is unavailable.",
                       status_code=503)
    try:
        return operation(**kwargs)
    except ClientError as exc:
        code = aws_error_code(exc)
        if code in _NOT_FOUND and name == "get_agent_insight":
            raise NotFoundError(
                "insight.not_found", "The finding or its source is not available.",
            ) from None
        if code in _DENIED:
            raise AppError("insight.denied", "AWS denied access to Agent Insights.",
                           status_code=403) from None
        if code in _UNSUPPORTED or (code in _NOT_FOUND and name == "get_agent_insight_store"):
            raise AppError("insight.unsupported", "Agent Insights API is unavailable.",
                           status_code=503) from None
        raise AppError("insight.unavailable", "Agent Insights could not be read.",
                       status_code=503) from None
    except BotoCoreError:
        raise AppError("insight.unavailable", "Agent Insights could not be read.",
                       status_code=503) from None


def get_store_state(client: Any) -> str:
    try:
        store = _read_operation(
            client, "get_agent_insight_store",
            agentInsightStoreId=DEFAULT_STORE,
        )
    except AppError as exc:
        if exc.code.startswith("insight."):
            return exc.code.removeprefix("insight.")
        raise
    status = store.get("status")
    enablement = store.get("enablementStatus")
    if status == "ACTIVE" and enablement == "ENABLED":
        return "ready"
    if status == "UPDATING":
        return "updating"
    if status == "ERROR":
        return "error"
    if status == "INACTIVE" and enablement == "DISABLED":
        return "disabled"
    return "unavailable"


def _online_finding(item: dict[str, Any], service: str) -> bool:
    return (item.get("applicationName") == service
            and bool(item.get("agentInsightId"))
            and not item.get("batchEvaluationId")
            and item.get("archiveStatus") in (None, "ACTIVE"))


def _finding_out(item: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    out = {key: item[key] for key in _SUMMARY_FIELDS if key in item}
    if item.get("customerMetadataStatus") not in (None, "AVAILABLE"):
        return {key: value for key, value in out.items() if key not in ("name", "description")}
    if detail:
        out.update({key: item[key] for key in ("rootCauses", "sampleSessions") if key in item})
    return out


def list_active_findings(
    client: Any, application_name: str, *, token: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    if not application_name:
        raise ValueError("An Agent Insights list must specify an application name")
    params: dict[str, Any] = {
        "applicationName": application_name, "archiveStatus": "ACTIVE", "maxResults": _PAGE_SIZE,
    }
    if token:
        params["nextToken"] = token
    response = _read_operation(client, "list_agent_insights", **params)
    items = [_finding_out(item) for item in response.get("agentInsights", [])
             if _online_finding(item, application_name)]
    return items, response.get("nextToken")


def get_active_finding(client: Any, application_name: str, insight_id: str) -> dict[str, Any]:
    item = _read_operation(
        client, "get_agent_insight", applicationName=application_name, agentInsightId=insight_id,
    )
    if not _online_finding(item, application_name) or item.get("agentInsightId") != insight_id:
        raise NotFoundError("insight.not_found", "The finding or its source is not available.")
    return _finding_out(item, detail=True)
