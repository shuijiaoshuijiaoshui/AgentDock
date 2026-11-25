"""履约域 AgentCard（skill 7-8）。"""

from shared.a2a_langgraph import build_agent_card


def build_card(url: str):  # noqa: ANN201
    return build_agent_card(
        name="履约 Agent",
        description="物流轨迹查询与可承诺库存时效（AgentDock 履约域）",
        url=url,
        skills=[
            {
                "id": "fulfill.track",
                "name": "物流轨迹",
                "description": "按订单号查询承运商、运单号与完整物流节点",
                "tags": ["fulfillment"],
            },
            {
                "id": "fulfill.atp",
                "name": "可承诺时效",
                "description": "按 sku 查询可承诺库存与预计发货时效",
                "tags": ["fulfillment"],
            },
            {
                "id": "return.create",
                "name": "创建退库单",
                "description": "退货入库单创建（草稿，待仓库人工确认入库）；退款执行走财务人工审批",
                "tags": ["fulfillment"],
            },
        ],
    )
