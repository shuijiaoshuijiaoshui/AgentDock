"""能力模块层（业务视图 → 域/工具映射）。

设计边界（PRD 7.x 域划分的补充说明）：
- 域 = 数据所有权 + 故障边界（工程层，稳定，按数据源切，不随业务口径变化）；
- 模块 = 用户视角的业务能力（产品层，按经营动作切，可随业务重排）；
- 两层的唯一映射事实源就是本文件的 SKILL_MODULE，供 AgentCard 标签
  （module:<id>）、坐席工具面板分组、能力矩阵文档与对外视图共用。

状态口径（status，防止对外叙事漂移）：
- implemented：能力完整交付；
- partial：只覆盖模块的一部分（如评价模块仅有投诉记录查询）；
- planned：本期未实现（不得挂任何 skill，测试会断言）。
"""

from __future__ import annotations

# 模块声明序 = 工作台与文档的展示顺序（消费者侧在前、商家侧在后）
MODULES: dict[str, dict] = {
    # ---- 消费者侧（toC）----
    "assist.guide": {
        "name": "导购与商品发现",
        "side": "consumer",
        "status": "implemented",
        "desc": "自然语言商品检索、详情、对比与到手价估算",
    },
    "assist.recommend": {
        "name": "个性化推荐",
        "side": "consumer",
        "status": "planned",
        "desc": "行为画像 + 千人千面召回（本期未实现）",
    },
    "assist.service": {
        "name": "智能客服与售后",
        "side": "consumer",
        "status": "implemented",
        "desc": "退换修资格/责任判定、工单、政策条款与知识库、客户检索与 360",
    },
    "assist.trade": {
        "name": "交易与履约辅助",
        "side": "consumer",
        "status": "implemented",
        "desc": "查单、改址/改价、取消与补偿、物流轨迹、退库草稿、退款与开票",
    },
    "assist.review": {
        "name": "评价与情感分析",
        "side": "consumer",
        "status": "partial",
        "desc": "仅投诉记录查询；评价聚合与情感分析未实现",
    },
    # ---- 商家侧（toB）----
    "merchant.insight": {
        "name": "数据分析与经营决策",
        "side": "merchant",
        "status": "implemented",
        "desc": "投放/销量/对账数据诊断，异常与归因简化版",
    },
    "merchant.content": {
        "name": "营销与内容生成",
        "side": "merchant",
        "status": "planned",
        "desc": "文案/素材/详情页生成（本期未实现，仅有方案落单）",
    },
    "merchant.selection": {
        "name": "选品与商品运营",
        "side": "merchant",
        "status": "implemented",
        "desc": "销量增速×竞品价差选品打分、竞品价格监控、爆款排行",
    },
    "merchant.stock": {
        "name": "库存与履约",
        "side": "merchant",
        "status": "implemented",
        "desc": "可承诺库存与时效、退库单草稿、爆款→备货建议联动",
    },
    "merchant.growth": {
        "name": "投放与增长",
        "side": "merchant",
        "status": "implemented",
        "desc": "ROI 分档诊断（加投/观察/暂停）+ 可执行方案落单",
    },
    "merchant.supply": {
        "name": "供应链与预测",
        "side": "merchant",
        "status": "partial",
        "desc": "仅建议级备货联动（hot_restock 仲裁链）；需求预测/采购未实现",
    },
}

# skill id → 模块 id（首项为主模块，其余为次模块）。
# 覆盖 8 个域全部 35 个上卡 skill；planned 模块不挂任何 skill。
SKILL_MODULE: dict[str, tuple[str, ...]] = {
    # 导购与商品发现（4）
    "product.lookup": ("assist.guide",),
    "product.retrieve": ("assist.guide",),
    "compare.matrix": ("assist.guide",),
    "promo.evaluate": ("assist.guide",),
    "subsidy.check": ("assist.guide",),
    # 智能客服与售后（10）
    "aftersale.eligibility": ("assist.service",),
    "aftersale.reason": ("assist.service",),
    "ticket.create": ("assist.service",),
    "ticket.query": ("assist.service",),
    "policy.answer": ("assist.service",),
    "policy.clause": ("assist.service",),
    "knowledge.upsert": ("assist.service",),
    "customer.profile": ("assist.service",),
    "customer.search": ("assist.service",),
    "customer.overview": ("assist.service",),
    # 交易与履约辅助（11；return.create 兼库存模块）
    "order.query": ("assist.trade",),
    "order.amend": ("assist.trade",),
    "order.cancel": ("assist.trade",),
    "order.restore": ("assist.trade",),
    "fulfill.track": ("assist.trade",),
    "return.create": ("assist.trade", "merchant.stock"),
    "billing.statement": ("assist.trade",),
    "invoice.status": ("assist.trade",),
    "refund.status": ("assist.trade",),
    "refund.settle": ("assist.trade",),
    "invoice.issue": ("assist.trade",),
    # 评价与情感分析（1，partial）
    "complaint.list": ("assist.review",),
    # 数据分析与经营决策（2）
    "credit.query": ("merchant.insight",),
    "reconciliation.query": ("merchant.insight",),
    # 选品与商品运营（3；hot.rank 兼经营分析）
    "selection.assist": ("merchant.selection",),
    "competitor.watch": ("merchant.selection",),
    "hot.rank": ("merchant.selection", "merchant.insight"),
    # 库存与履约（1；fulfill.atp 兼交易履约）
    "fulfill.atp": ("merchant.stock", "assist.trade"),
    # 投放与增长（2；ad.report 兼经营分析）
    "ad.report": ("merchant.growth", "merchant.insight"),
    "plan.save": ("merchant.growth",),
}


def module_of(skill_id: str) -> tuple[str, ...]:
    """skill → 模块元组（未登记返回空元组，兼容 stub/内部 skill）。"""
    return SKILL_MODULE.get(skill_id, ())


def primary_module(skill_id: str) -> str:
    return SKILL_MODULE.get(skill_id, ("",))[0]


def module_label(module_id: str) -> str:
    """模块中文名；未知 id 原样返回（前端兜底分组）。"""
    return (MODULES.get(module_id) or {}).get("name", module_id)


def modules_of_side(side: str) -> list[str]:
    return [m for m, meta in MODULES.items() if meta["side"] == side]


def skills_of_module(module_id: str) -> list[str]:
    """模块覆盖的 skill（含次模块归属），按声明序遍历。"""
    return [s for s, mods in SKILL_MODULE.items() if module_id in mods]


def module_tags(skill_id: str) -> list[str]:
    """Card 标签形态：module:<id>（主 + 次）。"""
    return [f"module:{m}" for m in module_of(skill_id)]


def module_order() -> dict[str, int]:
    """模块展示序（声明序）：registry/前端分组共用。"""
    return {m: i for i, m in enumerate(MODULES)}
