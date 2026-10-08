"""Deterministic demo seed for the 无人机授权体验店 CRM (dev/prod demo only, no real people).

Four tables:
  customers  — 会员档案 + tenure（入会日期 member_since, tenure_months 由 handler 按 as_of 计算）
  purchases  — 购买记录（无人机整机 = category "drone"，其余为配件 / 服务）
  tickets    — 售后工单（含处理记录 notes）
  products   — 可推荐商品目录（电池、充电管家、ND 镜、桨叶、收纳包、随心换、新机型）

The named test customers (C1001–C1008) cover the recommendation rule edges:
老客户 (tenure ≥ 24 个月) AND 最近一次购机 ≥ 365 天前 → 可以推荐。
"""

import json
import random
from datetime import date, timedelta

AS_OF = date(2026, 10, 8)
rng = random.Random(20261008)

DRONES = {
    "DRN-M4P": ("Mavic 4 Pro 畅飞套装", "Mavic 4 Pro", 16888),
    "DRN-LX1": ("Lito X1 标准版", "Lito X1", 3999),
    "DRN-M3P": ("Mavic 3 Pro 畅飞套装", "Mavic 3 Pro", 15688),
    "DRN-A3S": ("Air 3S 畅飞套装", "Air 3S", 8788),
    "DRN-MN4P": ("Mini 4 Pro 带屏遥控器版", "Mini 4 Pro", 5788),
}

PRODUCTS = [
    # sku, name, category, compatible_models, price, stock, note
    ("ACC-M4P-BAT", "Mavic 4 Pro 智能飞行电池（BWXGP1-2788-7）", "电池", ["Mavic 4 Pro"], 1199, 46,
     "电池循环次数多、续航下降的老机主首选"),
    ("ACC-M4P-HUB", "Mavic 4 Pro 充电管家（可作移动电源）", "充电", ["Mavic 4 Pro"], 699, 20,
     "多电池按电量高低依次充电"),
    ("ACC-M4P-ND", "Mavic 4 Pro ND 镜套装（ND16/64/256）", "镜头配件", ["Mavic 4 Pro"], 899, 15, ""),
    ("ACC-M4P-PROP", "Mavic 4 Pro 低噪音桨叶（6030F，对）", "桨叶", ["Mavic 4 Pro"], 89, 120,
     "桨叶属易耗件，有缺口或变形需更换"),
    ("ACC-M4P-BAG", "Mavic 4 Pro 单肩包", "收纳", ["Mavic 4 Pro"], 499, 12, ""),
    ("SVC-M4P-CARE", "随心换 1 年版（Mavic 4 Pro）", "服务", ["Mavic 4 Pro"], 1399, 999,
     "需在激活后指定天数内购买，以官方规则为准"),
    ("ACC-LX1-BAT", "Lito X1 智能飞行电池", "电池", ["Lito X1"], 399, 60, ""),
    ("ACC-LX1-HUB", "Lito X1 双向充电管家", "充电", ["Lito X1"], 299, 25, ""),
    ("ACC-LX1-PROP", "Lito X1 桨叶（对）", "桨叶", ["Lito X1"], 39, 200, ""),
    ("SVC-LX1-CARE", "随心换 1 年版（Lito X1）", "服务", ["Lito X1"], 399, 999, ""),
    ("ACC-M3P-BAT", "Mavic 3 系列智能飞行电池", "电池", ["Mavic 3 Pro"], 1099, 8,
     "老机型电池，库存有限"),
    ("ACC-A3S-BAT", "Air 3 系列智能飞行电池", "电池", ["Air 3S"], 799, 30, ""),
    ("ACC-MN4P-BAT", "Mini 4 Pro 长续航智能飞行电池", "电池", ["Mini 4 Pro"], 549, 40, ""),
    ("DRN-M4P", "Mavic 4 Pro 畅飞套装（以旧换新可抵扣）", "整机升级", ["Mavic 3 Pro", "Air 3S", "Mini 4 Pro"],
     16888, 10, "老机型用户升级推荐；以旧换新抵扣金额以门店评估为准"),
    ("DRN-LX1", "Lito X1 标准版", "整机升级", ["Mini 4 Pro"], 3999, 18, "入门轻量机型"),
]

SURNAMES = list("王李张刘陈杨黄赵吴周徐孙马朱胡郭何林罗高")
GIVEN = ["伟", "芳", "娜", "敏", "静", "强", "磊", "洋", "艳", "勇", "军", "杰", "涛", "明", "超",
         "秀英", "建华", "海燕", "志强", "晓东", "雪梅", "子轩", "思远", "嘉怡"]
CITIES = ["深圳", "上海", "北京", "广州", "成都", "杭州"]


def d(s: str) -> str:
    return s


def days_ago(n: int) -> str:
    return (AS_OF - timedelta(days=n)).isoformat()


customers: list[dict] = []
purchases: list[dict] = []
tickets: list[dict] = []
_order_seq = [50000]
_ticket_seq = [8000]


def order(cid: str, when: str, sku: str, qty: int = 1) -> str:
    _order_seq[0] += rng.randint(3, 17)
    oid = f"SO-{_order_seq[0]}"
    if sku in DRONES:
        name, model, price = DRONES[sku]
        cat = "drone"
    else:
        p = next(x for x in PRODUCTS if x[0] == sku)
        name, cat, price = p[1], p[2], p[4]
        model = p[3][0]
    purchases.append({"customer_id": cid, "order_id": oid, "order_date": when, "sku": sku,
                      "product_name": name, "category": cat, "drone_model": model,
                      "quantity": qty, "amount_cny": price * qty, "store": "深圳欢乐海岸授权体验店"})
    return oid


def ticket(cid: str, opened: str, category: str, subject: str, status: str, priority: str,
           order_id: str | None, notes: list[tuple[str, str, str]]) -> str:
    _ticket_seq[0] += rng.randint(1, 9)
    tid = f"TK-{_ticket_seq[0]}"
    tickets.append({
        "ticket_id": tid, "customer_id": cid, "category": category, "subject": subject,
        "status": status, "priority": priority, "related_order_id": order_id,
        "created_at": opened, "updated_at": notes[-1][0] if notes else opened,
        "notes": [{"at": a, "by": b, "text": t} for a, b, t in notes],
    })
    return tid


def customer(cid: str, name: str, phone: str, member_since: str, tier: str, city: str,
             consent: bool = True) -> None:
    customers.append({"customer_id": cid, "name": name, "phone": phone,
                      "member_since": member_since, "tier": tier, "city": city,
                      "marketing_consent": consent})


# ---- named test customers -------------------------------------------------------------
# C1001 老客户 + 最近购机 > 1 年 → 推荐（Mavic 3 Pro 老机主：电池 / 升级 Mavic 4 Pro）
customer("C1001", "陈志远", "138****1001", "2021-05-16", "金卡", "深圳")
order("C1001", "2021-05-16", "DRN-MN4P")
order("C1001", "2024-03-02", "DRN-M3P")
order("C1001", "2024-03-02", "ACC-M3P-BAT", 2)
ticket("C1001", "2025-11-20", "保修维修", "Mavic 3 Pro 云台抖动送修", "已关闭", "中", None,
       [("2025-11-20", "店员-小林", "客户反馈云台抖动，已寄送官方维修"),
        ("2025-12-03", "店员-小林", "维修完成，客户已取回，工单关闭")])

# C1002 老客户，但 2026-06 刚买了 Mavic 4 Pro（< 1 年）→ 不推荐
customer("C1002", "林晓雯", "139****1002", "2022-02-10", "金卡", "深圳")
order("C1002", "2022-02-10", "DRN-A3S")
order("C1002", "2026-06-18", "DRN-M4P")
order("C1002", "2026-06-18", "SVC-M4P-CARE")
ticket("C1002", "2026-09-28", "使用咨询", "Mavic 4 Pro 图传偶发卡顿", "处理中", "中", None,
       [("2026-09-28", "店员-阿杰", "客户反馈城区图传卡顿，已建议更新固件并避开强干扰环境"),
        ("2026-10-02", "店员-阿杰", "已约客户 10/10 到店检测")])

# C1003 新客户（tenure < 24 个月），购机 > 1 年 → 不推荐（不是老客户）
customer("C1003", "周子轩", "137****1003", "2025-04-03", "普通", "广州")
order("C1003", "2025-04-03", "DRN-MN4P")

# C1004 老客户，最近 1 年只买了配件（电池），最近一次 *购机* 在 2024-11 → 推荐
customer("C1004", "黄海燕", "136****1004", "2023-01-08", "银卡", "深圳")
order("C1004", "2023-01-08", "DRN-MN4P")
order("C1004", "2024-11-11", "DRN-A3S")
order("C1004", "2026-08-21", "ACC-A3S-BAT", 1)
ticket("C1004", "2026-08-25", "退换货", "Air 3 电池外壳划痕申请换货", "待客户回复", "低", None,
       [("2026-08-25", "店员-小林", "客户反馈新电池外壳划痕，请客户补拍照片"),
        ("2026-08-26", "店员-小林", "已发短信提醒客户补充照片，等待回复")])

# C1005 老客户，最近购机 2025-10-20（距 as_of 353 天，未满 1 年）→ 不推荐（边界）
customer("C1005", "吴建华", "135****1005", "2020-09-12", "钻石卡", "上海")
order("C1005", "2020-09-12", "DRN-MN4P")
order("C1005", "2025-10-20", "DRN-M4P")

# C1006 老客户（6 年），Lito X1 购于 2025-05（> 1 年）→ 推荐；有一张待处理工单
customer("C1006", "马思远", "133****1006", "2020-03-28", "金卡", "成都")
order("C1006", "2020-03-28", "DRN-MN4P")
order("C1006", "2025-05-06", "DRN-LX1")
ticket("C1006", "2026-10-06", "保修维修", "Lito X1 电池无法充电", "待处理", "高", None,
       [("2026-10-06", "系统", "客户在线提交：电池插上充电管家指示灯不亮")])

# C1007 老客户但没有任何购机记录（只办了会员）→ 无购机依据，不推荐
customer("C1007", "赵雪梅", "132****1007", "2022-07-01", "普通", "杭州")

# C1008 老客户，购机 > 1 年，但拒绝营销（marketing_consent=false）→ 不主动推荐
customer("C1008", "孙晓东", "131****1008", "2021-11-30", "银卡", "北京", consent=False)
order("C1008", "2023-06-15", "DRN-A3S")

# ---- filler customers -----------------------------------------------------------------
for i in range(9, 41):
    cid = f"C10{i:02d}"
    since = AS_OF - timedelta(days=rng.randint(60, 2400))
    customer(cid, rng.choice(SURNAMES) + rng.choice(GIVEN), f"1{rng.randint(30, 89)}****{1000 + i}",
             since.isoformat(), rng.choice(["普通", "普通", "银卡", "金卡"]), rng.choice(CITIES),
             consent=rng.random() > 0.15)
    t = since
    for _ in range(rng.randint(1, 3)):
        t = t + timedelta(days=rng.randint(0, 500))
        if t > AS_OF:
            break
        sku = rng.choice(list(DRONES))
        order(cid, t.isoformat(), sku)
        if rng.random() < 0.4:
            acc = [p[0] for p in PRODUCTS if DRONES[sku][1] in p[3] and p[0].startswith("ACC")]
            if acc:
                order(cid, (t + timedelta(days=rng.randint(0, 200))).isoformat()
                      if t + timedelta(days=200) < AS_OF else t.isoformat(), rng.choice(acc))
    if rng.random() < 0.35:
        st = rng.choice(["已关闭", "已解决", "处理中", "待处理"])
        opened = AS_OF - timedelta(days=rng.randint(1, 300))
        subj, cat = rng.choice([("遥控器无法对频", "使用咨询"), ("包裹物流延迟", "物流"),
                                ("桨叶断裂咨询更换", "使用咨询"), ("飞行器炸机申请随心换换新", "随心换"),
                                ("发票重开", "其他")])
        ticket(cid, opened.isoformat(), cat, subj, st, rng.choice(["低", "中", "高"]), None,
               [(opened.isoformat(), "系统", "客户提交工单")])

# ---- evaluation fixtures (appended AFTER the filler loop so its rng stream — and so every
# row above — stays byte-identical) ---------------------------------------------------------
# G20 同一手机后 4 位 6688 匹配 3 名会员，资格各不相同（选错人答案就错）
customer("C1041", "王建国", "139****6688", "2019-08-20", "金卡", "深圳")
order("C1041", "2019-08-20", "DRN-MN4P")
order("C1041", "2023-05-01", "DRN-A3S")
customer("C1042", "王建军", "186****6688", "2024-12-01", "银卡", "深圳")
order("C1042", "2024-12-01", "DRN-LX1")
ticket("C1042", "2026-09-30", "使用咨询", "Lito X1 遥控器对频失败", "处理中", "中", None,
       [("2026-09-30", "店员-阿杰", "客户反馈遥控器无法对频，已指导重新对频，待客户反馈")])
customer("C1043", "李静", "135****6688", "2022-03-15", "普通", "上海")
order("C1043", "2025-12-20", "DRN-MN4P")

# G23 推荐门槛边界：恰好 24 个月且恰好 365 天 → 达标；各差 1 天 → 不达标
customer("C1044", "刘晓峰", "158****1044", "2024-10-08", "银卡", "深圳")
order("C1044", "2024-10-08", "DRN-MN4P")
order("C1044", "2025-10-08", "DRN-A3S")
customer("C1045", "郭雨欣", "159****1045", "2024-10-09", "普通", "广州")  # 23 个月
order("C1045", "2025-10-08", "DRN-A3S")
customer("C1046", "何俊杰", "150****1046", "2024-10-08", "金卡", "杭州")
order("C1046", "2024-10-08", "DRN-MN4P")
order("C1046", "2025-10-09", "DRN-M4P")  # 364 天

purchases.sort(key=lambda p: (p["customer_id"], p["order_date"]))
seed = {
    "as_of": AS_OF.isoformat(),
    "customers": customers,
    "purchases": purchases,
    "tickets": tickets,
    "products": [{"sku": s, "name": n, "category": c, "compatible_models": m, "price_cny": p,
                  "stock": st, "selling_point": note} for s, n, c, m, p, st, note in PRODUCTS],
}
with open("seed.json", "w", encoding="utf-8") as f:
    json.dump(seed, f, ensure_ascii=False, indent=1)
print({k: len(v) for k, v in seed.items() if isinstance(v, list)})
