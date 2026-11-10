"""samples/itsm-cn synthetic ServiceNow service: token-based free-text search (N17-obs553).

The service module is loaded against a temp SQLite file; no host, Gateway or AWS involved.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[2] / "samples/itsm-cn/lab-assets/service.py"
TOOLS = SERVICE.with_name("tools.json")


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("ITSM_DB", str(tmp_path / "state.sqlite"))
    monkeypatch.setenv("ITSM_STAFF_ACCOUNTS", "itdesk01,itdesk11")
    monkeypatch.delenv("ITSM_STAFF_PUBLISH_TARGETS", raising=False)
    spec = importlib.util.spec_from_file_location("itsm_service_under_test", SERVICE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.reset()
    return module


def numbers(result):
    return sorted(t["number"] for t in result["tickets"])


def test_search_tokens_split_on_whitespace_and_both_comma_kinds(service):
    assert service.search_tokens("QDocs 权限") == ["qdocs", "权限"]
    assert service.search_tokens("period locked,季末,补录") == ["period", "locked", "季末", "补录"]
    assert service.search_tokens(" a，b；c;d\t e ") == ["a", "b", "c", "d", "e"]
    assert service.search_tokens("") == [] and service.search_tokens(None) == []


def test_qdocs_permission_query_finds_the_learners_access_request(service):
    # obs553 example 1: the whole string "QDocs 权限" is not a substring of "QDocs 编辑权限申请"
    result = service.execute("search_tickets", {"requester": "lab01", "query": "QDocs 权限"})
    assert numbers(result) == ["RITM0010001"] and result["count"] == 1
    # case-insensitive, one token suffices, the other learner's ticket stays invisible
    assert numbers(service.execute("search_tickets", {"requester": "lab02", "query": "qdocs"})) == [
        "RITM0010002"
    ]
    nothing = service.execute("search_tickets", {"requester": "lab01", "query": "无此内容"})
    assert numbers(nothing) == []


def test_period_locked_query_finds_the_resolved_archive_tickets_for_staff(service):
    # obs553 example 2: the desk account's fallback query after search_resolved_tickets failed
    result = service.execute(
        "search_tickets", {"requester": "itdesk11", "query": "period locked,季末,补录"}
    )
    assert numbers(result) == ["INC0099001", "INC0099002", "INC0099003"]
    # a learner never sees the archive, whatever the query
    assert numbers(service.execute(
        "search_tickets", {"requester": "lab01", "query": "period locked,季末,补录"})) == []


def test_state_and_requester_filters_are_unchanged(service):
    staff = "itdesk01"
    assert len(service.execute("search_tickets", {"requester": staff})["tickets"]) == 23
    assert len(service.execute("search_tickets", {"requester": "lab03"})["tickets"]) == 2
    held = service.execute("search_tickets", {"requester": staff, "state": "on_hold"})
    assert len(held["tickets"]) == 10 and all(t["state"] == "on_hold" for t in held["tickets"])
    both = service.execute(
        "search_tickets", {"requester": staff, "state": "resolved", "query": "季末 补录"}
    )
    assert numbers(both) == ["INC0099001", "INC0099002", "INC0099003"]
    none = service.execute("search_tickets", {"requester": staff, "state": "new", "query": "季末"})
    assert none["count"] == 0
    # the ticket number itself still matches (the old whole-JSON match allowed that)
    assert numbers(service.execute(
        "search_tickets", {"requester": "lab04", "query": "INC0020004"})) == ["INC0020004"]
    with pytest.raises(service.BusinessError, match="requester_required"):
        service.execute("search_tickets", {"query": "x"})


def test_search_resolved_tickets_uses_the_same_tokens(service):
    hit = service.execute(
        "search_resolved_tickets", {"requester": "itdesk01", "keywords": "period locked,季末,补录"}
    )
    assert numbers(hit) == ["INC0099001", "INC0099002", "INC0099003"]
    # only the resolution of INC0099002 names the (fictional) contact
    contact = service.execute(
        "search_resolved_tickets", {"requester": "itdesk01", "keywords": "张经理"}
    )
    assert numbers(contact) == ["INC0099002"]
    assert service.execute(
        "search_resolved_tickets", {"requester": "itdesk01", "keywords": " , "})["tickets"] == []
    with pytest.raises(service.BusinessError, match="servicedesk_required"):
        service.execute("search_resolved_tickets", {"requester": "lab01", "keywords": "季末"})


def test_tool_descriptions_tell_the_model_the_matching_rule():
    tools = {t["name"]: t for target in json.loads(TOOLS.read_text()).values() for t in target}
    for name in ("search_tickets", "search_resolved_tickets"):
        text = tools[name]["description"]
        assert "分词" in text and "任一" in text and "不区分大小写" in text, name
    assert "QDocs 权限" in tools["search_tickets"]["description"]
    assert tools["search_tickets"]["inputSchema"]["required"] == ["requester"]
