"""营销经营分析域 AgentCard（toB，商家服务端）。"""

from shared.a2a_langgraph import build_agent_card


def build_card(url: str):  # noqa: ANN201
    return build_agent_card(
        name="营销经营分析 Agent",
        description="投放效果与方案、选品、竞品价格监控、爆款发现（AgentDock 营销域）",
        url=url,
        skills=[
            {
                "id": "ad.report",
                "name": "投放效果分析",
                "description": "分计划花费/曝光/点击/转化/GMV/ROI/CVR 与确定性投放方案",
                "tags": ["marketing"],
            },
            {
                "id": "selection.assist",
                "name": "选品助手",
                "description": "销量增速×竞品价差打分的选品候选与理由",
                "tags": ["marketing"],
            },
            {
                "id": "competitor.watch",
                "name": "竞品价格监控",
                "description": "竞品最新价对比、价差率与降价提醒",
                "tags": ["marketing"],
            },
            {
                "id": "hot.rank",
                "name": "爆款发现",
                "description": "近 N 天销量环比增速 top 与 GMV",
                "tags": ["marketing"],
            },
            {
                "id": "plan.save",
                "name": "保存经营方案",
                "description": "把分析方案存为计划单（两阶段确认，实际执行需人工审批）",
                "tags": ["marketing"],
            },
        ],
    )
