"""坐席工作台右侧工具面板的注册表（只读，PRD 7.12 + FR-2）。

- 全部为 A2A 直调域 Server（绕过编排，权限按坐席 token 的 scope）；
- customer.query 已升格为 customer 域的 customer.search 工具（本文件只保留
  面板入口 id 兼容 UI），客户数据逻辑单份实现在域内；
- 每次调用写 console 审计 + 统一审计；域工具内部强制 tenant 过滤与脱敏。
"""

import json
import logging

from shared.auth import AuthContext
from shared.config import get_settings
from shared.modules import module_label, module_order, primary_module

logger = logging.getLogger(__name__)


def _a2a_tools() -> list[dict]:
    s = get_settings()
    return [
        {
            "id": "customer.query", "name": "查用户信息", "source": "a2a",
            "skill": "customer.search",
            "domain": "customer", "url": s.domain_url("customer"),
            "params": [{"name": "query", "label": "客户ID/姓名/手机尾号",
                        "placeholder": "CU-1001 或 王小明 或 1234"}],
            "render": lambda text: f"查询客户 {text} 的信息",
            "requires_customer": False,
        },
        {
            "id": "product.lookup", "name": "查商品详情", "source": "a2a",
            "skill": "product.lookup",
            "domain": "merchandising", "url": s.domain_url("merchandising"),
            "params": [{"name": "sku", "label": "SKU", "placeholder": "SKU-A001"}],
            "render": lambda text: f"查询商品 {text} 的详情",
            "requires_customer": False,
        },
        {
            "id": "order.query", "name": "查订单状态", "source": "a2a",
            "skill": "order.query",
            "domain": "order", "url": s.domain_url("order"),
            "params": [{"name": "order_no", "label": "订单号", "placeholder": "SO-2001"}],
            "render": lambda text: f"查询订单 {text} 的状态",
            "requires_customer": True,
        },
        {
            "id": "fulfill.track", "name": "查物流节点", "source": "a2a",
            "skill": "fulfill.track",
            "domain": "fulfillment", "url": s.domain_url("fulfillment"),
            "params": [{"name": "order_no", "label": "订单号", "placeholder": "SO-2001"}],
            "render": lambda text: f"查询订单 {text} 的履约节点",
            "requires_customer": True,
        },
        {
            "id": "fulfill.atp", "name": "查可售时效", "source": "a2a",
            "skill": "fulfill.atp",
            "domain": "fulfillment", "url": s.domain_url("fulfillment"),
            "params": [{"name": "sku", "label": "SKU", "placeholder": "SKU-A001"}],
            "render": lambda text: f"查询 {text} 的可承诺库存与时效",
            "requires_customer": False,
        },
        {
            "id": "ticket.query", "name": "查工单进度", "source": "a2a",
            "skill": "ticket.query",
            "domain": "aftersale", "url": s.domain_url("aftersale"),
            "params": [{"name": "ticket_no", "label": "工单号", "placeholder": "TK-3001"}],
            "render": lambda text: f"查询工单 {text} 的进度",
            "requires_customer": True,
        },
        {
            "id": "policy.clause", "name": "查政策条款", "source": "a2a",
            "skill": "policy.clause",
            "domain": "policy", "url": s.domain_url("policy"),
            "params": [{"name": "clause_id", "label": "条款号", "placeholder": "CLS-001"}],
            "render": lambda text: f"查询条款 {text} 的现行版本",
            "requires_customer": False,
        },
        # 营销经营分析（toB，marketing.read 坐席/主管可见）
        {
            "id": "marketing.ad_report", "name": "投流效果分析", "source": "a2a",
            "skill": "ad.report",
            "domain": "marketing", "url": s.domain_url("marketing"),
            "params": [{"name": "query", "label": "范围", "placeholder": "近14天 或 CP-01"}],
            "render": lambda text: f"分析投放效果：{text or '近 14 天'}",
            "requires_customer": False,
        },
        {
            "id": "marketing.hot_rank", "name": "爆款发现", "source": "a2a",
            "skill": "hot.rank",
            "domain": "marketing", "url": s.domain_url("marketing"),
            "params": [{"name": "query", "label": "范围", "placeholder": "近7天"}],
            "render": lambda text: f"查询爆款排行：{text or '近 7 天'}",
            "requires_customer": False,
        },
        {
            "id": "marketing.competitor_watch", "name": "竞品价格监控", "source": "a2a",
            "skill": "competitor.watch",
            "domain": "marketing", "url": s.domain_url("marketing"),
            "params": [{"name": "query", "label": "SKU（可空）", "placeholder": "SKU-A001"}],
            "render": lambda text: f"竞品价格监控：{text or '全部监控项'}",
            "requires_customer": False,
        },
        {
            "id": "marketing.selection_assist", "name": "选品助手", "source": "a2a",
            "skill": "selection.assist",
            "domain": "marketing", "url": s.domain_url("marketing"),
            "params": [{"name": "query", "label": "关键词（可空）", "placeholder": "显示器"}],
            "render": lambda text: f"选品分析：{text or '全量候选'}",
            "requires_customer": False,
        },
    ]


def registry(auth: AuthContext | None = None) -> list[dict]:
    """给前端 /api/tools 的工具清单；按 token scope 过滤（前端隐藏不算权限）。"""
    if not auth or auth.role not in ("agent", "supervisor", "admin"):
        return []
    tools = []
    for t in _a2a_tools():
        if auth and not auth.has_scope(f"{t['domain']}.read"):
            continue
        skill = str(t.get("skill") or "")
        module = primary_module(skill) if skill else ""
        tools.append({
            "id": t["id"], "name": t["name"], "source": "a2a",
            "domain": t["domain"], "url": t["url"],
            "params": t["params"],
            "requires_customer": bool(t.get("requires_customer")),
            # 能力模块分层（产品视图）：工作台按 module 分组渲染
            "skill": skill,
            "module": module,
            "module_label": module_label(module) if module else "",
        })
    # 按模块声明序稳定排序（消费者侧在前、商家侧在后），组内保持声明顺序
    order = module_order()
    tools.sort(key=lambda t: order.get(t.get("module") or "", 999))
    return tools


async def invoke(
    tool_id: str, params: dict, conversation_id: str | None,
    auth: AuthContext | None = None,
) -> dict:
    """统一入口：返回 {state, text, data}。全部写审计（8.3）。"""
    from a2a.types import TaskState

    from console import db
    from shared.a2a_client import delegate
    from shared.audit import audit_from_auth

    tenant = auth.tenant_id if auth else "t_demo"
    actor = auth.actor_id if auth else "system"
    role = auth.role if auth else "system"
    if not auth or role not in ("agent", "supervisor", "admin"):
        return {"state": "TASK_STATE_REJECTED", "text": "无权限", "data": {}}

    tool = next((t for t in _a2a_tools() if t["id"] == tool_id), None)
    if tool is None:
        return {"state": "TASK_STATE_REJECTED", "text": "未知工具", "data": {}}
    if auth and not auth.has_scope(f"{tool['domain']}.read"):
        return {"state": "TASK_STATE_REJECTED", "text": "无权限", "data": {}}

    customer_id = str(params.get("customer_id") or "").strip()
    if not customer_id and conversation_id:
        conv = db.get_conversation(conversation_id, tenant_id=tenant)
        customer_id = str((conv or {}).get("customer_id") or "").strip()
    if tool.get("requires_customer") and not customer_id:
        return {
            "state": "TASK_STATE_REJECTED",
            "text": "缺少客户上下文（customer_id），坐席操作必须绑定会话客户",
            "data": {},
        }

    # 只取该工具声明的第一个参数；params 里还可能带 customer_id 上下文，
    # 不能盲取 next(iter(params.values()))，否则会把客户号当订单号/SKU 用。
    param_name = str(((tool.get("params") or [{}])[0]).get("name") or "")
    text = tool["render"](str(params.get(param_name, "")))
    try:
        result = await delegate(
            tool["url"], text,
            metadata={
                "trace_id": f"console-{conversation_id or 'adhoc'}",
                "context_id": conversation_id or "",
                "customer_id": customer_id,
            },
            auth=auth,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("工具 %s 调用失败：%s", tool_id, e)
        return {"state": "TASK_STATE_FAILED", "text": f"工具调用失败：{e}", "data": {}}
    state = TaskState.Name(result.state) if isinstance(result.state, int) else str(result.state)
    audit_from_auth(
        auth, "tool.console_dispatch", target=tool_id, params=params,
        result_summary=f"{state}: {result.text[:160]}",
        context_id=conversation_id or "",
    )
    return {
        "state": state,
        "text": result.text,
        "data": result.data_parts[0] if result.data_parts else {},
    }
