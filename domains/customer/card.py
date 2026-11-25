"""客户域 AgentCard（skill 17-20）。"""

from shared.a2a_langgraph import build_agent_card


def build_card(url: str):  # noqa: ANN201
    return build_agent_card(
        name="客户与账务 Agent",
        description="客户资料/等级折扣、授信额度与账期、对账单、投诉记录（AgentDock 客户域）",
        url=url,
        skills=[
            {
                "id": "customer.profile",
                "name": "客户资料",
                "description": "按客户查询编码/等级/专属折扣/客户经理等资料",
                "tags": ["customer"],
            },
            {
                "id": "credit.query",
                "name": "授信与账期",
                "description": "查询授信额度、已用/剩余额度、账期天数与到期日",
                "tags": ["customer"],
            },
            {
                "id": "reconciliation.query",
                "name": "对账查询",
                "description": "按周期查询对账单金额、状态与开票状态",
                "tags": ["customer"],
            },
            {
                "id": "complaint.list",
                "name": "投诉记录",
                "description": "查询客户历史投诉记录与处理状态",
                "tags": ["customer"],
            },
            {
                "id": "customer.search",
                "name": "客户检索",
                "description": "按客户ID/手机号/姓名检索同租户客户（staff-only，输出脱敏）",
                "tags": ["customer"],
            },
            {
                "id": "customer.overview",
                "name": "客户360",
                "description": "客户资料/订单/工单/投诉/售后/对账只读聚合视图",
                "tags": ["customer"],
            },
        ],
    )
