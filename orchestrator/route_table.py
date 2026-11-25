"""域路由表（PRD 7.5 / AC-21）：纯配置 + 纯函数，禁止 LLM 参与路由。

AC-21：同一句话（同 intent+槽位）两次请求必须走同一条域链 → 路由是确定性查表。
AC-28：新域接入 = 加一行（M5 演练）。
M1 只放行商品域的三类意图 + smalltalk；其余意图统一 nomatch（拒答）。
"""

from orchestrator.state import Plan

# 意图 → 域与模式（7.5 路由表第一版；M3 起接串行依赖边与仲裁）
ROUTE_TABLE: dict[str, dict] = {
    # 商品与营销（M1）
    "product_lookup": {"domains": ["merchandising"], "mode": "single"},
    "product_search": {"domains": ["merchandising"], "mode": "single"},
    "product_compare": {"domains": ["merchandising"], "mode": "single"},
    "promo_evaluate": {"domains": ["merchandising"], "mode": "single"},
    # 国补资格（价格差异只来自活动/国补，无会员制度）
    "subsidy_check": {"domains": ["merchandising"], "mode": "single"},
    # 售后（M2）
    "aftersale_eligibility": {"domains": ["aftersale"], "mode": "single"},
    "aftersale_reason": {"domains": ["aftersale"], "mode": "single"},
    "ticket_create": {"domains": ["aftersale"], "mode": "single"},
    "ticket_query": {"domains": ["aftersale"], "mode": "single"},
    # 政策（M2）
    "policy_qa": {"domains": ["policy"], "mode": "single"},
    # 订单/履约（M2 真实现；order.amend 写操作走 order_query 之外的两阶段确认链）
    "order_query": {"domains": ["order"], "mode": "single"},
    "fulfill_track": {"domains": ["fulfillment"], "mode": "single"},
    "fulfill_atp": {"domains": ["fulfillment"], "mode": "single"},
    # 订单/履约/财务 串行链（M3）：列表即链序，上游完成才发下一跳（PRD 7.9）
    "order_amend": {"domains": ["fulfillment", "order"], "mode": "serial"},
    # 改价（高价值写操作）：订单域单域执行；金额超阈值走审批/双人复核
    "order_price_amend": {"domains": ["order"], "mode": "single"},
    "refund_status": {"domains": ["aftersale", "fulfillment", "finance"], "mode": "serial"},
    "invoice_issue": {"domains": ["finance"], "mode": "single"},
    "invoice_status": {"domains": ["finance"], "mode": "single"},
    "refund_settle": {"domains": ["finance"], "mode": "single"},
    # Saga 写链（PRD 7.7 行 2「取消订单 + 退款」）：①订单取消 → ②财务退款，
    # 财务失败 → 逆序补偿（恢复订单）；补偿注册见 SAGA_CHAINS
    "cancel_refund": {"domains": ["order", "finance"], "mode": "serial"},
    # 跨域仲裁（PRD 7.6 / 3.4）：订单(查 sku) → 商品(到手价估算) ∥ 财务(实付账单)；
    # 实付金额以财务为权威，营销估算仅参考——"营销到手价当成交价"是 3.4 典型错法
    "promo_arbitrate": {
        "domains": ["order", "merchandising", "finance"], "mode": "arbitrate",
        "deps": {"merchandising": ["order"]},
    },
    # 跨域并行（M2）：坏了+保修 → 售后资格 ∥ 政策条款 ∥ 商品档案（PRD 5.3 场景 6）
    "warranty_check": {
        "domains": ["aftersale", "policy", "merchandising"], "mode": "parallel",
    },
    # 客户与账务（ToB 阶段二）：资料/授信/对账/投诉
    "customer_profile": {"domains": ["customer"], "mode": "single"},
    "credit_query": {"domains": ["customer"], "mode": "single"},
    "reconciliation_query": {"domains": ["customer"], "mode": "single"},
    "complaint_list": {"domains": ["customer"], "mode": "single"},
    # 营销经营分析（toB marketing 域，商家服务端）：只读分析 + 方案落单（两阶段确认）
    "ad_analysis": {"domains": ["marketing"], "mode": "single"},
    "product_selection": {"domains": ["marketing"], "mode": "single"},
    "competitor_watch": {"domains": ["marketing"], "mode": "single"},
    "ad_plan_save": {"domains": ["marketing"], "mode": "single"},
    # 爆款→备货联动：marketing(爆款增速) → fulfillment(可承诺库存)，
    # 上游产物 top_sku 注入下游槽位（ARBITRATE_INJECTIONS，同 promo_arbitrate 模式）
    "hot_restock": {
        "domains": ["marketing", "fulfillment"], "mode": "arbitrate",
        "deps": {"fulfillment": ["marketing"]},
    },
    # 本地 / 人机协同
    "greet": {"domains": [], "mode": "smalltalk"},
    "request_human": {"domains": ["humandesk"], "mode": "handoff"},
}

# M3 全部意图已接入。后续扩展（M4+/按需）：分域评测阈值标定、
# 串行链深度上限校验（PRD 7.5 不破 3 跳）在 advance 已按 deps 天然约束。

# Saga 注册表（PRD 7.7 硬规则 1：每个写操作必须登记补偿动作，没有补偿的写
# 不允许出现在跨域编排里）。writes = 链上各域的正向写 skill；
# compensate = 补偿委托文本模板（{biz_key} 由编排层注入，补偿逆序执行）。
SAGA_CHAINS: dict[str, dict] = {
    "cancel_refund": {
        "writes": {
            "order": {"skill": "order.cancel", "biz_key_suffix": "cancel"},
            "finance": {"skill": "refund.settle", "biz_key_suffix": "refund"},
        },
        # 补偿 = 恢复订单（finance.settle 失败无副作用：INSERT 幂等，未落退款单）
        "compensations": [
            {"domain": "order", "skill": "order.restore", "scope": "order.write",
             "text": "恢复订单（Saga 补偿），补偿键：{biz_key}"},
        ],
    },
}

SMALLTALK_REPLIES = {
    "greet": "你好，我是商家经营助手。可以问我商品、订单、售后、结算、政策的问题，"
             "商家服务端还可以问投流效果、选品、竞品价格和爆款。",
}

LOW_CONFIDENCE_THRESHOLD = 0.6


def route_plan(intent: str, slots: dict, confidence: float) -> Plan | None:
    """(intent, 槽位齐全性) → Plan；未命中返回 None（nomatch）。纯函数，可单测。"""
    entry = ROUTE_TABLE.get(intent)
    if entry is None:
        return None
    if not entry["domains"]:
        return Plan(intent=intent, domains=[], mode="smalltalk")
    if confidence < LOW_CONFIDENCE_THRESHOLD:
        return None
    return Plan(intent=intent, domains=list(entry["domains"]), mode=entry["mode"],
                deps=entry.get("deps") or {})


def domain_base_url(domain: str) -> str:
    """调用方视角的域地址：覆盖项（*_url，Docker 用服务名）优先，否则本机端口拼接。"""
    from shared.config import get_settings

    return get_settings().domain_url(domain)
