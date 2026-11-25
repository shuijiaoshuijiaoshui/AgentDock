"""卡片按钮 action registry（2B）。

前端卡片按钮只允许传 registry 里的 action_id + 业务参数；
intent/slots 由服务端解析（复用 1B 签名 action token 机制），禁止客户端直传。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ActionSpec:
    action_id: str
    intent: str
    label: str
    slots_fields: tuple[str, ...] = field(default_factory=tuple)
    required_params: tuple[str, ...] = field(default_factory=tuple)


ACTION_REGISTRY: dict[str, ActionSpec] = {
    spec.action_id: spec
    for spec in (
        ActionSpec("order.detail", "order_query", "查看订单", ("order_no",), ("order_no",)),
        ActionSpec("order.track", "fulfill_track", "查物流", ("order_no",), ("order_no",)),
        ActionSpec("aftersale.eligibility", "aftersale_eligibility", "查售后资格",
                   ("sku", "order_no"), ("sku",)),
        ActionSpec("aftersale.apply", "ticket_create", "发起售后",
                   ("sku", "fault_type"), ("sku",)),
        ActionSpec("credit.view", "credit_query", "查授信", (), ()),
        ActionSpec("reconciliation.view", "reconciliation_query", "查对账",
                   ("period",), ()),
        ActionSpec("inventory.query", "fulfill_atp", "查库存", ("sku",), ("sku",)),
        ActionSpec("ticket.create", "ticket_create", "建工单",
                   ("sku", "fault_type"), ("sku",)),
    )
}


def resolve_action(action_id: str, params: dict) -> tuple[str, dict] | None:
    """白名单解析：返回 (intent, slots)；未注册/缺必填参数返回 None。"""
    spec = ACTION_REGISTRY.get(str(action_id or ""))
    if spec is None:
        return None
    params = params or {}
    slots: dict = {}
    for field_name in spec.slots_fields:
        value = params.get(field_name)
        if value not in (None, ""):
            slots[field_name] = value
    for required in spec.required_params:
        if required not in slots:
            return None
    return spec.intent, slots
