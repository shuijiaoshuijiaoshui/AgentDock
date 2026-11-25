"""售后域 AgentCard（skill 9-12）。"""

from shared.a2a_langgraph import build_agent_card


def build_card(url: str):  # noqa: ANN201
    return build_agent_card(
        name="售后 Agent",
        description="退换修资格与责任判定、维修工单创建与进度查询（AgentDock 售后域）",
        url=url,
        skills=[
            {
                "id": "aftersale.eligibility",
                "name": "资格判定",
                "description": "判定退换修资格（引用生效条款，含购买日期推算）",
                "tags": ["aftersale"],
            },
            {
                "id": "aftersale.reason",
                "name": "责任判定",
                "description": "质量/人为/物流责任判定（附运费承担条款）",
                "tags": ["aftersale"],
            },
            {
                "id": "ticket.create",
                "name": "创建维修工单",
                "description": "创建维修工单（写操作，补偿=作废工单）",
                "tags": ["aftersale"],
            },
            {
                "id": "ticket.query",
                "name": "工单进度查询",
                "description": "按工单号查询进度",
                "tags": ["aftersale"],
            },
        ],
    )
