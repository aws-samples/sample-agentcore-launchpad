"""Business invariants, using an isolated seed database; does not touch live data."""

import importlib.util
import json
import os
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]


def load_service(directory, staff=None, targets=None):
    """A fresh service module; the staff list and publish targets come from the environment."""
    for key in ("ITSM_STAFF_ACCOUNTS", "ITSM_STAFF_PUBLISH_TARGETS"):
        os.environ.pop(key, None)
    if staff is not None:
        os.environ["ITSM_STAFF_ACCOUNTS"] = ",".join(staff)
    if targets is not None:
        os.environ["ITSM_STAFF_PUBLISH_TARGETS"] = json.dumps(targets)
    os.environ["ITSM_DB"] = directory + "/test.sqlite"
    spec = importlib.util.spec_from_file_location("itsm", root / "lab-assets/service.py")
    service = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(service)
    service.reset()
    return service


def check_staff_boundary(service, staff, other):
    """The original invariants for one service-desk account ``staff``; ``other`` is not staff."""
    run = service.execute

    def refused(tool, args, reason):
        try:
            run(tool, args)
        except service.BusinessError as e:
            assert str(e) == reason, str(e)
        else:
            raise AssertionError("Expected refusal: " + reason)

    assert len(run("search_tickets", {"requester": "lab01"})["tickets"]) == 2
    assert len(run("search_tickets", {"requester": staff})["tickets"]) > 20
    refused(
        "get_ticket",
        {"requester": "lab01", "ticket_id": "INC0020002"},
        "ticket_not_found_or_not_owned",
    )
    refused(
        "add_comment",
        {"requester": "lab01", "ticket_id": "INC0020002", "comment": "test"},
        "ticket_not_found_or_not_owned",
    )
    assert (
        run("get_ticket", {"requester": staff, "ticket_id": "INC0020002"})["requester"] == "lab02"
    )
    updated = run(
        "add_comment", {"requester": "lab01", "ticket_id": "INC0020001", "comment": "企业微信4.0.8"}
    )
    assert updated["state"] == "in_progress"
    assert run("get_ticket", {"requester": "lab01", "ticket_id": "INC0020001"})["comments"]
    for who in ("lab01", other):
        refused(
            "resolve_ticket",
            {"requester": who, "ticket_id": "INC0020001", "resolution": "ok"},
            # ownership is checked before the staff gate: lab01 owns the ticket, ``other`` does not
            "servicedesk_required" if who == "lab01" else "ticket_not_found_or_not_owned",
        )
        refused(
            "publish_article",
            {"requester": who, "article_id": "KB0010001", "human_approved": True},
            "servicedesk_required",
        )
        refused("search_resolved_tickets", {"requester": who, "keywords": "CRM"}, "servicedesk_required")
    resolved = run(
        "resolve_ticket", {"requester": staff, "ticket_id": "INC0020001", "resolution": "已处理"}
    )
    assert resolved["state"] == "resolved"
    args = {
        "requester": staff,
        "title": "季末补录",
        "body": "地区经理在CRM电脑端开启24小时补录窗口。",
        "source_ticket_ids": ["INC0099001", "INC0099002"],
    }
    draft = run("draft_article", args)
    assert draft["author"] == staff and draft["reviewed_by"] is None
    refused(
        "draft_article",
        dict(args, source_ticket_ids=["INC0099001", "INC0099003"]),
        "two_verified_resolved_sources_required",
    )
    refused("draft_article", dict(args, body="联系电话13800000000"), "remove_personal_information")
    refused(
        "publish_article",
        {"requester": staff, "article_id": draft["id"], "human_approved": False},
        "explicit_human_review_required",
    )
    pending = run(
        "publish_article", {"requester": staff, "article_id": draft["id"], "human_approved": True}
    )
    assert pending["publication_pending"] and pending["status"] == "draft"
    target = service.publish_target(staff)
    assert pending.get("publish_target") == target
    if target:
        try:
            run(
                "publish_article",
                {
                    "requester": staff,
                    "article_id": draft["id"],
                    "human_approved": True,
                    "s3_uri": "s3://elsewhere/kb/x.md",
                },
                confirm=True,
            )
        except service.BusinessError as e:
            assert str(e) == "publish_target_mismatch", str(e)
        else:
            raise AssertionError("Expected refusal: publish_target_mismatch")
    published = run(
        "publish_article",
        {
            "requester": staff,
            "article_id": draft["id"],
            "human_approved": True,
            "s3_uri": (target or "s3://test/kb/") + draft["id"] + ".md",
        },
        confirm=True,
    )
    assert published["status"] == "published" and published["reviewed_by"] == staff
    service.reset()
    assert (
        run("get_ticket", {"requester": "lab01", "ticket_id": "INC0020001"})["state"] == "on_hold"
    )
    return draft


with tempfile.TemporaryDirectory() as d:
    # 1. No configuration: exactly the historical single account itdesk01.
    service = load_service(d)
    assert service.STAFF == frozenset({"itdesk01"}) and service.PUBLISH_TARGETS == {}
    check_staff_boundary(service, "itdesk01", other="itdesk03")

    # 2. Two configured accounts with distinct publish targets.
    targets = {"itdesk01": "s3://kb-bucket/shared", "itdesk03": "s3://kb-bucket/itdesk03/"}
    service = load_service(d, staff=["itdesk01", "itdesk03"], targets=targets)
    assert service.STAFF == frozenset({"itdesk01", "itdesk03"})
    assert service.publish_target("itdesk03") == "s3://kb-bucket/itdesk03/"
    assert service.publish_target("itdesk01") == "s3://kb-bucket/shared/"
    for staff in ("itdesk01", "itdesk03"):
        check_staff_boundary(service, staff, other="itdesk09")
    run = service.execute
    body = {"title": "季末补录", "body": "经理开启补录窗口。", "source_ticket_ids": ["INC0099001", "INC0099002"]}
    mine = run("draft_article", dict(body, requester="itdesk03"))
    try:
        run("get_article", {"requester": "itdesk01", "article_id": mine["id"]})
    except service.BusinessError as e:
        assert str(e) == "article_not_owned", str(e)
    else:
        raise AssertionError("A draft bound to another publish target must not be readable")
    assert run("get_article", {"requester": "itdesk03", "article_id": mine["id"]})["author"] == "itdesk03"

    # 3. Two accounts sharing one target review each other's drafts.
    service = load_service(
        d, staff=["itdesk01", "itdesk03"], targets={u: "s3://kb-bucket/shared" for u in ("itdesk01", "itdesk03")}
    )
    run = service.execute
    shared = run("draft_article", dict(body, requester="itdesk03"))
    assert run("get_article", {"requester": "itdesk01", "article_id": shared["id"]})["id"] == shared["id"]
    done = run(
        "publish_article",
        {"requester": "itdesk01", "article_id": shared["id"], "human_approved": True,
         "s3_uri": "s3://kb-bucket/shared/" + shared["id"] + ".md"},
        confirm=True,
    )
    assert done["author"] == "itdesk03" and done["reviewed_by"] == "itdesk01"
    print(
        "PASS: ownership, writes persist, state transition, staff boundary (default + configured), source corroboration, PII, review, publish target routing, draft ownership, publish commit, reset"
    )
