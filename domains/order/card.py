"""订单域 AgentCard（skill 5-6）。"""

from shared.a2a_langgraph import build_agent_card


def build_card(url: str):  # noqa: ANN201
    return build_agent_card(
        name="订单 Agent",
        description="订单状态查询与收货信息变更（AgentDock 订单域）",
        url=url,
        skills=[
            {
                "id": "order.query",
                "name": "订单状态查询",
                "description": "按订单号或客户 id 查询订单状态、金额与收货信息",
                "tags": ["order"],
            },
            {
                "id": "order.amend",
                "name": "订单信息变更",
                "description": "修改订单收货地址（写操作，两阶段确认 + 幂等）",
                "tags": ["order"],
            },
            {
                "id": "order.cancel",
                "name": "取消订单",
                "description": "取消订单（写操作，Saga 正向步骤，幂等；补偿=恢复订单）",
                "tags": ["order"],
            },
            {
                "id": "order.restore",
                "name": "恢复订单",
                "description": "Saga 补偿动作：恢复订单到取消前状态（幂等）",
                "tags": ["order"],
            },
        ],
    )
