"""结算与财务域 AgentCard（skill 13-16）。"""

from shared.a2a_langgraph import build_agent_card


def build_card(url: str):  # noqa: ANN201
    return build_agent_card(
        name="结算与财务 Agent",
        description="实付账单、退款执行与状态、发票开具（AgentDock 结算与财务域）",
        url=url,
        skills=[
            {
                "id": "billing.statement",
                "name": "实付账单",
                "description": "按订单号查询实付金额与支付渠道（退款金额权威源）",
                "tags": ["finance"],
            },
            {
                "id": "refund.status",
                "name": "退款状态",
                "description": "按订单号查询退款单状态",
                "tags": ["finance"],
            },
            {
                "id": "refund.settle",
                "name": "执行退款",
                "description": "按账单实付金额执行退款（写操作，幂等 + Saga 补偿兜底）",
                "tags": ["finance"],
            },
            {
                "id": "invoice.issue",
                "name": "开具发票",
                "description": "开具电子发票（写操作，两阶段确认 + 幂等）",
                "tags": ["finance"],
            },
            {
                "id": "invoice.status",
                "name": "开票进度",
                "description": "按订单号查询开票进度（order_no 为空查客户全部发票）",
                "tags": ["finance"],
            },
        ],
    )
