"""Synthetic ITSM teaching service; private HTTP behind a Gateway Lambda target."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = Path(os.environ.get("ITSM_DB", str(ROOT / "state.sqlite")))


def _staff_accounts():
    names = os.environ.get("ITSM_STAFF_ACCOUNTS", "itdesk01").split(",")
    staff = frozenset(n.strip() for n in names if n.strip())
    if not staff:
        raise ValueError("ITSM_STAFF_ACCOUNTS must name at least one service-desk account")
    return staff


def _publish_targets():
    raw = os.environ.get("ITSM_STAFF_PUBLISH_TARGETS", "").strip()
    targets = json.loads(raw) if raw else {}
    if not isinstance(targets, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and v.startswith("s3://")
        for k, v in targets.items()
    ):
        raise ValueError("ITSM_STAFF_PUBLISH_TARGETS must be a JSON object username -> s3:// prefix")
    return {k: v.rstrip("/") + "/" for k, v in targets.items()}


# Service-desk accounts; every other requester is a learner who only sees their own tickets.
STAFF = _staff_accounts()
# Staff username -> s3:// prefix the Gateway Lambda publishes that account's articles to.
# An account without an entry publishes to the Lambda's shared knowledge-base source.
PUBLISH_TARGETS = _publish_targets()


def seed_state():
    tickets = {}
    for n in range(1, 11):
        who = f"lab{n:02}"
        for prefix, tail in (("RITM", 10000 + n), ("INC", 20000 + n)):
            tid = f"{prefix}{tail:07}"
            tickets[tid] = {
                "number": tid,
                "requester": who,
                "short_description": "QDocs 编辑权限申请"
                if prefix == "RITM"
                else "企业微信 CRM 同步失败",
                "state": "pending_approval" if prefix == "RITM" else "on_hold",
                "assignment_group": "质量系统支持组" if prefix == "RITM" else "CRM 支持组",
                "details": "已完成 QD-301，等待直属经理审批。"
                if prefix == "RITM"
                else "请补充企业微信版本、错误截图和发生时间。挂起超过 5 个工作日未补充信息将自动关闭。",
                "comments": [],
                "resolution": "",
            }
    for i, note in enumerate(
        [
            "已确认季末 period locked。地区经理在 CRM 电脑端开启 24 小时临时补录窗口，代表在窗口内补录并注明实际拜访日期，再由经理复核。不要修改设备时间。",
            "季末锁定后，先由地区经理在 CRM 电脑端开启临时补录窗口（24 小时），代表补录实际拜访日期，经理复核。联系人虚构张经理，手机号 13800000000，知识文章不得包含此信息。",
            "用户说重试后恢复，但原因不明，未验证任何处理方法。",
        ],
        1,
    ):
        tid = f"INC009900{i}"
        tickets[tid] = {
            "number": tid,
            "requester": f"archive{i:02}",
            "short_description": "季末 CRM period locked 补录",
            "state": "resolved",
            "assignment_group": "CRM 支持组",
            "details": "合成历史工单，仅用于实验。",
            "comments": [],
            "resolution": note,
            "verified": i != 3,
        }
    return {"tickets": tickets, "articles": {}, "next_incident": 30001, "next_article": 10001}


def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=15)
    c.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, data TEXT NOT NULL)")
    c.execute(
        "INSERT OR IGNORE INTO state VALUES (1, ?)", (json.dumps(seed_state(), ensure_ascii=False),)
    )
    c.commit()
    return c


def reset():
    with connect() as c:
        c.execute(
            "UPDATE state SET data=? WHERE id=1", (json.dumps(seed_state(), ensure_ascii=False),)
        )


class BusinessError(Exception):
    pass


def require_staff(requester):
    if requester not in STAFF:
        raise BusinessError("servicedesk_required")


def publish_target(requester):
    return PUBLISH_TARGETS.get(requester)


def owned_ticket(state, requester, number):
    ticket = state["tickets"].get(number)
    if not ticket or (requester not in STAFF and ticket["requester"] != requester):
        raise BusinessError("ticket_not_found_or_not_owned")
    return ticket


def visible_article(state, requester, article_id):
    """A draft belongs to its author; staff sharing the author's publish target may review it."""
    article = state["articles"].get(article_id)
    if not article:
        raise BusinessError("article_not_found")
    author = article.get("author")
    if author not in (None, requester) and publish_target(author) != publish_target(requester):
        raise BusinessError("article_not_owned")
    return article


def execute(tool, args, confirm=False):
    requester = args.get("requester")
    if not isinstance(requester, str) or not requester.strip():
        raise BusinessError("requester_required")
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        state = json.loads(c.execute("SELECT data FROM state WHERE id=1").fetchone()[0])
        if tool == "search_tickets":
            query = str(args.get("query", "")).lower()
            rows = [
                t
                for t in state["tickets"].values()
                if (requester in STAFF or t["requester"] == requester)
                and (not args.get("state") or args["state"] == t["state"])
                and (not query or query in json.dumps(t, ensure_ascii=False).lower())
            ]
            result = {"tickets": rows, "count": len(rows)}
        elif tool == "get_ticket":
            result = owned_ticket(state, requester, args["ticket_id"])
        elif tool == "create_incident":
            number = f"INC{state['next_incident']:07}"
            state["next_incident"] += 1
            result = {
                "number": number,
                "requester": requester,
                "state": "new",
                "short_description": args["title"],
                "details": args["description"],
                "assignment_group": "IT 服务台",
                "comments": [],
                "resolution": "",
            }
            state["tickets"][number] = result
        elif tool in ("add_comment", "escalate", "resolve_ticket"):
            result = owned_ticket(state, requester, args["ticket_id"])
            if tool == "add_comment":
                result["comments"].append({"by": requester, "text": args["comment"]})
                if result["state"] == "on_hold":
                    result["state"] = "in_progress"
            elif tool == "escalate":
                result["assignment_group"] = args["assignment_group"]
                result["state"] = "in_progress"
            else:
                require_staff(requester)
                result["resolution"] = args["resolution"]
                result["state"] = "resolved"
        elif tool == "search_resolved_tickets":
            require_staff(requester)
            keywords = [x.lower() for x in re.split(r"[,，;；]", args["keywords"]) if x.strip()]
            result = {
                "tickets": [
                    t
                    for t in state["tickets"].values()
                    if t["state"] == "resolved"
                    and any(
                        k.strip() in json.dumps(t, ensure_ascii=False).lower() for k in keywords
                    )
                ]
            }
        elif tool == "draft_article":
            require_staff(requester)
            source_ids = list(dict.fromkeys(args["source_ticket_ids"]))
            sources = [owned_ticket(state, requester, i) for i in source_ids]
            if len(sources) < 2 or any(
                t["state"] != "resolved" or not t.get("verified") for t in sources
            ):
                raise BusinessError("two_verified_resolved_sources_required")
            body = args["body"]
            if re.search(r"1[3-9]\d{9}|张经理", body):
                raise BusinessError("remove_personal_information")
            aid = f"KB{state['next_article']:07}"
            state["next_article"] += 1
            result = {
                "id": aid,
                "title": args["title"],
                "body": body,
                "source_ticket_ids": source_ids,
                "status": "draft",
                "author": requester,
                "reviewed_by": None,
            }
            state["articles"][aid] = result
        elif tool in ("get_article", "publish_article"):
            require_staff(requester)
            result = visible_article(state, requester, args["article_id"])
            if tool == "publish_article":
                if args.get("human_approved") is not True:
                    raise BusinessError("explicit_human_review_required")
                target = publish_target(requester)
                if confirm:
                    if target and not str(args["s3_uri"]).startswith(target):
                        raise BusinessError("publish_target_mismatch")
                    result["status"] = "published"
                    result["reviewed_by"] = requester
                    result["s3_uri"] = args["s3_uri"]
                else:
                    result = dict(result, publication_pending=True)
                    if target:
                        result["publish_target"] = target
        else:
            raise BusinessError("unknown_tool")
        c.execute("UPDATE state SET data=? WHERE id=1", (json.dumps(state, ensure_ascii=False),))
        return json.loads(json.dumps(result))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(json.dumps({"http": args[0] if args else "", "path": self.path}), flush=True)

    def answer(self, code, body):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self.answer(
                200,
                {
                    "status": "ok",
                    "scenario": os.environ.get("ITSM_SCENARIO", "itsm-workshop"),
                    "staff_accounts": sorted(STAFF),
                },
            )
        self.answer(404, {"error": "not_found"})

    def do_POST(self):
        expected = "Bearer " + (ROOT / "service-token").read_text().strip()
        if not hmac.compare_digest(self.headers.get("Authorization", ""), expected):
            return self.answer(401, {"error": "unauthorized"})
        if self.path not in ("/call", "/confirm-publish"):
            return self.answer(404, {"error": "not_found"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > 524288:
                return self.answer(413, {"error": "invalid_size"})
            body = json.loads(self.rfile.read(size))
            if self.path == "/confirm-publish" and body["tool"] != "publish_article":
                raise BusinessError("invalid_confirmation")
            result = execute(body["tool"], body["arguments"], self.path == "/confirm-publish")
            self.answer(200, {"ok": True, "result": result})
        except BusinessError as e:
            self.answer(200, {"ok": False, "error": str(e)})
        except (ValueError, TypeError, KeyError):
            self.answer(400, {"ok": False, "error": "invalid_arguments"})


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--reset", action="store_true")
    p.add_argument("--port", type=int, default=8128)
    a = p.parse_args()
    if a.reset:
        reset()
        print("Scenario seed restored.")
    else:
        connect().close()
        ThreadingHTTPServer(("0.0.0.0", a.port), Handler).serve_forever()
