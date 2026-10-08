无人机授权体验店 CRM 接口（测试环境）。由门店 IT 提供给客服助手使用；数据为 46 名模拟会员（不含真实个人信息，手机号已脱敏），统计基准日 as_of = 2026-10-08。数据存在 4 张表：会员档案（含 tenure）、购买记录、售后工单、可推荐商品目录。接口没有退款、下单、改价、发券功能。

工具 1 find_customer(customer_id? | phone_last4?)：按会员号 CNNNN 或手机号后 4 位查会员。返回 name、phone（脱敏）、member_since 入会日期、tenure_months 会员时长（月）、tier（普通/银卡/金卡/钻石卡）、city、marketing_consent（是否同意接收营销推荐）。手机号后 4 位匹配多人时不返回档案，只返回 ambiguous=true 和脱敏候选（customer_id、name_masked、phone、city），核对姓名后用 customer_id 再查。
工具 2 list_purchases(customer_id)：购买记录（新到旧）+ summary：owned_drone_models、last_drone_purchase_date（最近一次购买无人机整机，category=drone）、last_drone_model、days_since_last_drone_purchase（距 as_of 天数）、last_any_purchase_date（含配件/服务）。配件、电池、随心换 不算购机。
工具 3 list_products(compatible_model?, category?)：可推荐商品：sku、name、category（电池/充电/桨叶/镜头配件/收纳/服务/整机升级）、compatible_models、price_cny、stock、selling_point。
工具 4 list_tickets(customer_id, status?)：会员的售后工单列表；状态：待处理/处理中/待客户回复/已解决/已关闭。
工具 5 get_ticket(ticket_id)：工单详情 + 全部处理记录 notes。
工具 6 create_ticket(customer_id, category, subject, description, priority?, related_order_id?)：新建工单（写），初始 待处理；category：保修维修/退换货/物流/使用咨询/随心换/其他；priority 低/中/高；关联订单必须属于该会员。
工具 7 update_ticket(ticket_id, customer_id, note, status?)：追加处理记录（note 必填）并可改状态（写）；customer_id 必须是工单所属会员，否则返回 ticket_customer_mismatch；已关闭工单返回 ticket_closed，需新建。
写操作的处理记录统一记为「AI 助手」：Gateway 目前不向接口传递店员身份，接口没有处理人参数。
错误码：customer_not_found、invalid_customer_id、missing_lookup_key、ticket_not_found、invalid_ticket_id、invalid_status、invalid_category、invalid_priority、order_not_found、ticket_closed、ticket_customer_mismatch、missing_field。

IT 准备的验收测试会员（当前数据）：
- C1001 陈志远，金卡，入会 2021-05（tenure 64 个月）；最近购机 Mavic 3 Pro 2024-03-02（950 天前）；同意营销。
- C1002 林晓雯，金卡，tenure 55 个月；2026-06-18 刚买 Mavic 4 Pro + 随心换（112 天前）；有一张 处理中 工单（图传卡顿，已约 10/10 到店检测）。
- C1003 周子轩，普通，入会 2025-04（tenure 18 个月）；Mini 4 Pro 购于 2025-04-03（553 天前）。
- C1004 黄海燕，银卡，tenure 45 个月；最近购机 Air 3S 2024-11-11（696 天前），但 2026-08-21 买过一块 Air 3 电池；有一张 待客户回复 工单（电池外壳划痕换货，等客户补照片）。
- C1005 吴建华，钻石卡，tenure 72 个月；最近购机 Mavic 4 Pro 2025-10-20（353 天前）。
- C1006 马思远，金卡，tenure 78 个月；最近购机 Lito X1 2025-05-06（520 天前）；工单 TK-8020「Lito X1 电池无法充电」待处理、高优先级。
- C1007 赵雪梅，普通，tenure 51 个月；没有任何购买记录。
- C1008 孙晓东，银卡，tenure 58 个月；最近购机 Air 3S 2023-06-15（1211 天前）；marketing_consent = false（拒绝营销）。
IT 准备的评估夹具（当前数据）：
- 手机后 4 位重名：6688 匹配 C1041 王建国（金卡，深圳，tenure 85 个月，最近购机 Air 3S 2023-05-01）、C1042 王建军（银卡，深圳，tenure 22 个月，Lito X1 2024-12-01，工单 TK-8062 处理中）、C1043 李静（普通，上海，Mini 4 Pro 2025-12-20，292 天前）。三人推荐资格各不相同。
- 已关闭工单：TK-8004（C1001，Mavic 3 Pro 云台抖动送修，2025-12-03 已关闭），update_ticket 会返回 ticket_closed。
- 推荐门槛边界（均同意营销）：C1044 刘晓峰 入会 2024-10-08（正好 24 个月），最近购机 Air 3S 2025-10-08（正好 365 天）；C1045 郭雨欣 入会 2024-10-09（23 个月），购机 2025-10-08（365 天）；C1046 何俊杰 入会 2024-10-08（24 个月），最近购机 Mavic 4 Pro 2025-10-09（364 天）。
测试环境的工单写入会真实落表；IT 可随时重置为以上初始数据。
