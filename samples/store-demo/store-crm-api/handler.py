"""无人机授权体验店 CRM demo API — MCP tools behind AgentCore Gateway (Lambda target).

Seven tools over four DynamoDB tables (customers / purchases / tickets / products).
Reads: find_customer, list_purchases, list_products, list_tickets, get_ticket.
Writes (tickets only): create_ticket, update_ticket. No payment, refund or order function.
The Gateway passes the tool arguments as the event and the qualified tool name
(``target___tool``) in the Lambda client context.
"""

import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key

PREFIX = os.environ.get("TABLE_PREFIX", "store-demo-")
AS_OF = date.fromisoformat(os.environ.get("AS_OF", "2026-10-08"))
_db = boto3.resource("dynamodb")
CUSTOMERS = _db.Table(PREFIX + "customers")
PURCHASES = _db.Table(PREFIX + "purchases")
TICKETS = _db.Table(PREFIX + "tickets")
PRODUCTS = _db.Table(PREFIX + "products")

CID_RE = re.compile(r"^C\d{4}$")
TID_RE = re.compile(r"^TK-\d{4,6}$")
STATUSES = ["待处理", "处理中", "待客户回复", "已解决", "已关闭"]
CATEGORIES = ["保修维修", "退换货", "物流", "使用咨询", "随心换", "其他"]
PRIORITIES = ["低", "中", "高"]
# The Gateway passes no caller identity to a Lambda target, so a model-supplied operator name
# would be unverifiable — writes are attributed to the assistant until identity is injected.
OPERATOR = "AI 助手"


def _plain(x):
    if isinstance(x, list):
        return [_plain(v) for v in x]
    if isinstance(x, dict):
        return {k: _plain(v) for k, v in x.items()}
    if isinstance(x, Decimal):
        return int(x) if x == x.to_integral_value() else float(x)
    return x


def _months_between(start: date, end: date) -> int:
    m = (end.year - start.year) * 12 + (end.month - start.month)
    return m - (1 if end.day < start.day else 0)


def _customer_id(args):
    raw = str(args.get("customer_id", "")).strip().upper()
    if not CID_RE.match(raw):
        return None, {"error": "invalid_customer_id", "customer_id": args.get("customer_id"),
                      "expected_format": "CNNNN，例如 C1001"}
    return raw, None


def _mask(name):
    return name[0] + "*" * (len(name) - 2) + name[-1] if len(name) > 2 else name[0] + "*"


def _get_customer(cid):
    item = CUSTOMERS.get_item(Key={"customer_id": cid}).get("Item")
    return _plain(item) if item else None


def find_customer(args):
    cid = str(args.get("customer_id", "") or "").strip().upper()
    phone4 = re.sub(r"\D", "", str(args.get("phone_last4", "") or ""))
    if cid:
        cid, err = _customer_id({"customer_id": cid})
        if err:
            return err
        found = [_get_customer(cid)] if _get_customer(cid) else []
    elif len(phone4) == 4:
        found = _plain(CUSTOMERS.scan(FilterExpression=Attr("phone").contains(phone4))["Items"])
        found = [c for c in found if c["phone"].endswith(phone4)]
    else:
        return {"error": "missing_lookup_key",
                "message": "需要 customer_id（CNNNN）或 phone_last4（手机号后 4 位）"}
    if not found:
        return {"error": "customer_not_found", "customer_id": cid or None,
                "phone_last4": phone4 or None}
    if len(found) > 1:  # disambiguate on the least data: no profile until one is confirmed
        found.sort(key=lambda c: c["customer_id"])
        return {"as_of": AS_OF.isoformat(), "ambiguous": True, "match_count": len(found),
                "candidates": [{"customer_id": c["customer_id"], "name_masked": _mask(c["name"]),
                                "phone": c["phone"], "city": c["city"]} for c in found],
                "message": "手机号后 4 位匹配到多名会员："
                           "请向客户核对姓名，确认后用 customer_id 查询"}
    out = []
    for c in found:
        since = date.fromisoformat(c["member_since"])
        out.append({**c, "tenure_months": _months_between(since, AS_OF)})
    return {"as_of": AS_OF.isoformat(), "customers": out}


def list_purchases(args):
    cid, err = _customer_id(args)
    if err:
        return err
    if _get_customer(cid) is None:
        return {"error": "customer_not_found", "customer_id": cid}
    rows = _plain(PURCHASES.query(KeyConditionExpression=Key("customer_id").eq(cid))["Items"])
    rows.sort(key=lambda r: (r["order_date"], r["order_id"]), reverse=True)
    drones = [r for r in rows if r["category"] == "drone"]
    last = drones[0] if drones else None
    summary = {
        "order_count": len(rows),
        "drone_purchase_count": len(drones),
        "owned_drone_models": sorted({r["drone_model"] for r in drones}),
        "last_drone_purchase_date": last["order_date"] if last else None,
        "last_drone_model": last["drone_model"] if last else None,
        "days_since_last_drone_purchase":
            (AS_OF - date.fromisoformat(last["order_date"])).days if last else None,
        "last_any_purchase_date": rows[0]["order_date"] if rows else None,
    }
    return {"as_of": AS_OF.isoformat(), "customer_id": cid, "summary": summary,
            "purchases": [{k: v for k, v in r.items() if k != "customer_id"} for r in rows]}


def list_products(args):
    model = str(args.get("compatible_model", "") or "").strip()
    category = str(args.get("category", "") or "").strip()
    rows = _plain(PRODUCTS.scan()["Items"])
    if model:
        rows = [r for r in rows if any(model.lower() == m.lower() for m in r["compatible_models"])]
    if category:
        rows = [r for r in rows if r["category"] == category]
    rows.sort(key=lambda r: (r["category"], r["sku"]))
    return {"count": len(rows), "products": rows}


def _ticket(tid):
    item = TICKETS.get_item(Key={"ticket_id": tid}).get("Item")
    return _plain(item) if item else None


def list_tickets(args):
    cid, err = _customer_id(args)
    if err:
        return err
    status = str(args.get("status", "") or "").strip()
    if status and status not in STATUSES:
        return {"error": "invalid_status", "allowed": STATUSES}
    rows = _plain(TICKETS.scan(FilterExpression=Attr("customer_id").eq(cid))["Items"])
    if status:
        rows = [r for r in rows if r["status"] == status]
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return {"customer_id": cid, "count": len(rows),
            "tickets": [{k: r[k] for k in ("ticket_id", "category", "subject", "status",
                                           "priority", "created_at", "updated_at")}
                        for r in rows]}


def get_ticket(args):
    tid = str(args.get("ticket_id", "")).strip().upper()
    if not TID_RE.match(tid):
        return {"error": "invalid_ticket_id", "expected_format": "TK-NNNN"}
    t = _ticket(tid)
    return t if t else {"error": "ticket_not_found", "ticket_id": tid}


def _now():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")


def create_ticket(args):
    cid, err = _customer_id(args)
    if err:
        return err
    if _get_customer(cid) is None:
        return {"error": "customer_not_found", "customer_id": cid}
    category = str(args.get("category", "")).strip()
    subject = str(args.get("subject", "")).strip()
    description = str(args.get("description", "")).strip()
    priority = str(args.get("priority", "") or "中").strip()
    if category not in CATEGORIES:
        return {"error": "invalid_category", "allowed": CATEGORIES}
    if priority not in PRIORITIES:
        return {"error": "invalid_priority", "allowed": PRIORITIES}
    if not subject or not description:
        return {"error": "missing_field", "message": "subject 和 description 都必填"}
    order_id = str(args.get("related_order_id", "") or "").strip() or None
    if order_id:
        hit = PURCHASES.get_item(Key={"customer_id": cid, "order_id": order_id}).get("Item")
        if not hit:
            return {"error": "order_not_found", "related_order_id": order_id,
                    "message": "该订单不属于此客户或不存在"}
    seq = int(datetime.now().timestamp() * 1000) % 1_000_000
    tid = f"TK-{seq:06d}"
    now = _now()
    item = {"ticket_id": tid, "customer_id": cid, "category": category,
            "subject": subject[:120], "status": "待处理", "priority": priority,
            "related_order_id": order_id, "created_at": now, "updated_at": now,
            "notes": [{"at": now, "by": OPERATOR, "text": description[:2000]}]}
    TICKETS.put_item(Item=item, ConditionExpression="attribute_not_exists(ticket_id)")
    return {"created": True, "ticket": item}


def update_ticket(args):
    tid = str(args.get("ticket_id", "")).strip().upper()
    if not TID_RE.match(tid):
        return {"error": "invalid_ticket_id", "expected_format": "TK-NNNN"}
    cid, err = _customer_id(args)
    if err:
        return err
    t = _ticket(tid)
    if not t:
        return {"error": "ticket_not_found", "ticket_id": tid}
    if t["customer_id"] != cid:
        return {"error": "ticket_customer_mismatch", "ticket_id": tid, "customer_id": cid,
                "message": "该工单不属于此会员，请核对会员号和工单号"}
    note = str(args.get("note", "")).strip()
    status = str(args.get("status", "") or "").strip()
    if not note:
        return {"error": "missing_field", "message": "note 必填：每次更新都要写处理记录"}
    if status and status not in STATUSES:
        return {"error": "invalid_status", "allowed": STATUSES}
    if t["status"] == "已关闭":
        return {"error": "ticket_closed", "ticket_id": tid,
                "message": "已关闭的工单不能再修改，请新建工单"}
    now = _now()
    notes = t.get("notes", []) + [{"at": now, "by": OPERATOR, "text": note[:2000]}]
    new_status = status or t["status"]
    TICKETS.update_item(Key={"ticket_id": tid},
                        UpdateExpression="SET #s = :s, notes = :n, updated_at = :u",
                        ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={":s": new_status, ":n": notes, ":u": now})
    return {"updated": True, "ticket_id": tid, "previous_status": t["status"],
            "status": new_status, "notes": notes}


TOOLS = {f.__name__: f for f in (find_customer, list_purchases, list_products, list_tickets,
                                 get_ticket, create_ticket, update_ticket)}


def lambda_handler(event, context):
    name = ""
    try:
        name = context.client_context.custom["bedrockAgentCoreToolName"]
    except Exception:  # noqa: BLE001 — local invocation
        name = (event or {}).pop("__tool", "")
    tool = name.split("___", 1)[-1]
    fn = TOOLS.get(tool)
    if fn is None:
        return {"error": "unknown_tool", "tool": name}
    try:
        return json.loads(json.dumps(fn(event or {}), ensure_ascii=False, default=str))
    except Exception as e:  # noqa: BLE001
        return {"error": "internal_error", "message": str(e)[:300]}
