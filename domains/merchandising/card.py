"""商品与营销域 AgentCard（技术方案 3.3 / 第 6 章 skill 1–4）。"""

from shared.a2a_langgraph import build_agent_card


def build_card(url: str):  # noqa: ANN201
    return build_agent_card(
        name="商品与营销 Agent",
        description="商品查询、对比与优惠估算（AgentDock 商品与营销域）",
        url=url,
        skills=[
            {
                "id": "product.retrieve",
                "name": "商品搜索",
                "description": "按关键词搜索商品候选列表",
                "tags": ["merchandising"],
            },
            {
                "id": "product.lookup",
                "name": "商品详情",
                "description": "按 sku 查询单个商品详情",
                "tags": ["merchandising"],
            },
            {
                "id": "compare.matrix",
                "name": "商品对比",
                "description": "多个 sku 输出对比矩阵（产物一次性，不流式）",
                "tags": ["merchandising"],
            },
            {
                "id": "promo.evaluate",
                "name": "到手价估算",
                "description": "结合促销与优惠券估算到手价（标注估算）",
                "tags": ["merchandising"],
            },
            {
                "id": "subsidy.check",
                "name": "国补资格",
                "description": "国补/以旧换新资格判定与补贴价估算（生效期+门槛+限购）",
                "tags": ["merchandising"],
            },
        ],
    )
