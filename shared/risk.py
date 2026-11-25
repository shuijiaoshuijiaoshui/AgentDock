"""金额风险分级与审批策略（PRD 7.7 + 风险审计 R2/R3 的生产化补充）。

规则（可通过 Settings 调整）：
- 退款：**无论金额一律人工审批**（资金高风险，阈值只决定复核人数）；
- 改价 / 开票：金额超过阈值 → 需审批；
- 超过阈值的 5 倍 → 双人复核（职责分离）；
- 审批人不能是申请人本人；金融动作双人复核要求至少一名 finance + 一名 supervisor/admin；
- 工单创建保持两阶段确认（无需金额审批）。
"""

from __future__ import annotations

from dataclasses import dataclass

from shared.config import get_settings


@dataclass(frozen=True)
class RiskDecision:
    skill: str
    action: str
    tier: str
    requires_approval: bool
    required_approvals: int
    approver_roles: tuple[str, ...]
    dual_control: bool
    reason: str


def assess(skill: str, amount: float | None = None) -> RiskDecision:
    s = get_settings()
    if skill == "refund.settle":
        threshold = s.approval_refund_threshold
        action = "refund"
    elif skill == "invoice.issue":
        threshold = s.approval_invoice_threshold
        action = "invoice"
    elif skill == "order.amend":
        threshold = s.approval_price_change_threshold
        action = "price_change"
    elif skill == "ticket.create":
        return RiskDecision(
            skill=skill, action="ticket", tier="medium",
            requires_approval=False, required_approvals=0,
            approver_roles=(), dual_control=False,
            reason="工单创建走两阶段确认，不涉及资金；无需金额审批",
        )
    else:
        return RiskDecision(
            skill=skill, action=skill, tier="low",
            requires_approval=False, required_approvals=0,
            approver_roles=(), dual_control=False, reason="只读/低风险",
        )

    if amount is None:
        return RiskDecision(
            skill=skill, action=action, tier="high",
            requires_approval=True, required_approvals=1,
            approver_roles=("supervisor", "finance", "admin"),
            dual_control=False,
            reason="无法确定金额，按高风险处理，需人工确认",
        )

    # 退款是资金写操作：所有金额都必须人工最终判断，阈值只影响复核人数
    if action == "refund":
        required = 2 if amount > threshold * s.approval_dual_threshold_multiplier else 1
        return RiskDecision(
            skill=skill,
            action="refund",
            tier="high",
            requires_approval=True,
            required_approvals=required,
            approver_roles=("supervisor", "finance", "admin"),
            dual_control=required >= 2,
            reason=(
                f"退款属高风险资金动作，金额 {amount:.2f} 一律需人工审批"
                + ("（双人复核：finance + supervisor/admin）" if required >= 2
                   else "（1 人审批）")
            ),
        )

    if amount <= threshold:
        return RiskDecision(
            skill=skill, action=action, tier="medium" if action != "price_change" else "high",
            requires_approval=False, required_approvals=0,
            approver_roles=(), dual_control=False,
            reason=f"金额 {amount:.2f} ≤ 阈值 {threshold:.2f}",
        )

    required = 2 if amount > threshold * s.approval_dual_threshold_multiplier else 1
    return RiskDecision(
        skill=skill,
        action=action,
        tier="high",
        requires_approval=True,
        required_approvals=required,
        approver_roles=("supervisor", "finance", "admin"),
        dual_control=required >= 2,
        reason=(
            f"金额 {amount:.2f} > 阈值 {threshold:.2f}，需 {required} 人审批"
            + ("（双人复核：finance + supervisor/admin）" if required >= 2 else "")
        ),
    )
