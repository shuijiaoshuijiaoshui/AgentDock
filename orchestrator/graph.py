"""一主：编排主图（技术方案 2.2 的 M1 子集）。

M1 链路：intake → understand（低置信 interrupt 澄清）→ route（纯查表）
       → dispatch（A2A 委托商品域；域追问 → ask_user interrupt 回填）→ synthesize → deliver
       旁支：nomatch（拒答）/ smalltalk（本地应答，不建 Task）
M3 接入：advance 拓扑调度器（Send 分层扇出）、arbitrate、saga。
"""

import json
import logging
import re
import uuid
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from shared.audit import audit_from_config
from shared.auth import auth_from_config, decode_action_token
from shared.config import get_settings
from shared.llm import get_small_llm
from shared.llm_guard import sanitize_for_llm, sanitize_text
from shared.pii import mask_pii
from shared.tenant import customer_id as auth_customer_id
from shared.tracing import new_trace_id

from orchestrator.prompts import (
    SYNTHESIZE_SYSTEM,
    SYNTHESIZE_VERSION,
    UNDERSTAND_SYSTEM,
    UNDERSTAND_VERSION,
)
from orchestrator.arbitrate import (
    arbitrate_fields,
    arbitrate_value,
    check_snapshot_drift,
    ready_domains,
    veto_reason,
)
from orchestrator.route_table import (
    LOW_CONFIDENCE_THRESHOLD,
    SAGA_CHAINS,
    SMALLTALK_REPLIES,
    domain_base_url,
    route_plan,
)
from orchestrator.state import OrchestratorState

logger = logging.getLogger(__name__)

_INTENT_SET = {
    "product_lookup", "product_search", "product_compare", "promo_evaluate",
    "subsidy_check",
    "aftersale_eligibility", "aftersale_reason", "ticket_create", "ticket_query",
    "policy_qa", "warranty_check",
    "order_query", "fulfill_track", "fulfill_atp",
    "order_amend", "order_price_amend", "refund_status", "refund_settle",
    "invoice_issue", "invoice_status", "cancel_refund",
    "customer_profile", "credit_query", "reconciliation_query", "complaint_list",
    "promo_arbitrate",
    "ad_analysis", "product_selection", "competitor_watch", "ad_plan_save",
    "hot_restock",
    "greet", "request_human", "unknown",
}
_SKU_RE = re.compile(r"SKU[-A-Za-z0-9]*", re.IGNORECASE)
# 域名中文名（局部交付标注用）
DOMAIN_NAMES = {
    "merchandising": "商品", "order": "订单", "fulfillment": "履约",
    "aftersale": "售后", "finance": "结算财务", "policy": "政策",
    "customer": "客户", "marketing": "营销分析",
    "humandesk": "人工坐席",
}
# 高风险词（PRD 7.12 触发条件 3：纠纷/投诉/资金类直接转人工）
_RISK_RE = re.compile(r"投诉|纠纷|举报|315|工商|曝光|律师函|法院|差评到底")


class Understanding(BaseModel):
    intent: str = Field(description="候选意图之一")
    slots: dict = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)
    top2: list[str] = Field(default_factory=list, description="置信前二的意图名")


# ---------------------------------------------------------------------------
# fake 模式的规则理解（不调真模型，跑通链路/测试用）
# ---------------------------------------------------------------------------

def rule_understand(text: str, history: dict | None = None) -> dict:
    """fake 理解入口：规则分支 + 历史槽位继承（代词指代上一轮对象）。"""
    u = _rule_understand_base(text)
    slots = u.get("slots") or {}
    for k, v in _history_carry_slots(text, history or {}).items():
        if k in slots and not slots.get(k):
            slots[k] = v
            u["slots"] = slots
    return u


def _rule_understand_base(text: str) -> dict:
    t = text.strip()
    if re.search(r"你好|您好|hi|hello|在吗", t, re.IGNORECASE):
        return {"intent": "greet", "slots": {}, "confidence": 0.95,
                "top2": ["greet", "unknown"]}
    if re.search(r"转人工|人工客服|找人工|真人客服|人工服务", t):
        return {"intent": "request_human", "slots": {}, "confidence": 0.95,
                "top2": ["request_human", "unknown"]}
    skus = [m.group(0).upper() for m in _SKU_RE.finditer(t)]
    # 跨域并行（warranty_check）：坏了/故障 + 保修 → 三域并行（PRD 5.3 场景 6）
    if any(w in t for w in ("坏了", "故障", "失灵", "不出声", "闪烁", "进水")) \
            and "保修" in t:
        return {"intent": "warranty_check",
                "slots": {"sku": skus[0] if skus else "",
                          "fault_type": next((w for w in ("进水", "失灵", "闪烁", "故障", "坏了") if w in t), "故障"),
                          "query": t},
                "confidence": 0.9, "top2": ["warranty_check", "aftersale_eligibility"]}
    tm = re.search(r"TK-\d+", t, re.IGNORECASE)
    if tm and not any(w in t for w in ("建", "报修", "申请")):
        return {"intent": "ticket_query", "slots": {"ticket_no": tm.group(0).upper()},
                "confidence": 0.9, "top2": ["ticket_query", "ticket_create"]}
    if "工单" in t and any(w in t for w in ("建", "报修", "申请", "创建")):
        return {"intent": "ticket_create",
                "slots": {"sku": skus[0] if skus else "",
                          "fault_type": next((w for w in ("进水", "失灵", "闪烁", "故障", "坏了") if w in t), "故障")},
                "confidence": 0.9, "top2": ["ticket_create", "aftersale_eligibility"]}
    # 营销经营分析（toB，商家服务端；先于改价/退款分支，避免"降价"等词误路由）
    if any(w in t for w in ("保存方案", "记录方案", "生成计划单", "落方案")):
        cm = re.search(r"CP-\d+", t, re.IGNORECASE)
        kind = ("ad_adjust" if any(w in t for w in ("投流", "投放", "广告"))
                else "restock" if any(w in t for w in ("爆款", "补货", "备货"))
                else "selection")
        return {"intent": "ad_plan_save",
                "slots": {"kind": kind,
                          "target": (cm.group(0).upper() if cm else
                                     (skus[0] if skus else "")),
                          "note": t},
                "confidence": 0.9, "top2": ["ad_plan_save", "ad_analysis"]}
    if any(w in t for w in ("爆款", "什么好卖", "啥好卖", "卖得最", "最卖得")):
        return {"intent": "hot_restock",
                "slots": {"query": t},
                "confidence": 0.9, "top2": ["hot_restock", "fulfill_atp"]}
    if any(w in t for w in ("投流", "投放", "广告", "ROI", "roi", "转化率")):
        return {"intent": "ad_analysis",
                "slots": {"query": t},
                "confidence": 0.9, "top2": ["ad_analysis", "competitor_watch"]}
    if any(w in t for w in ("竞品", "比价", "价格监控")):
        return {"intent": "competitor_watch",
                "slots": {"query": t, "sku": skus[0] if skus else ""},
                "confidence": 0.9, "top2": ["competitor_watch", "ad_analysis"]}
    if any(w in t for w in ("选品",)):
        return {"intent": "product_selection",
                "slots": {"query": t},
                "confidence": 0.9, "top2": ["product_selection", "hot_restock"]}
    if any(w in t for w in ("责任", "谁的", "谁承担")):
        return {"intent": "aftersale_reason",
                "slots": {"fault_type": next((w for w in ("进水", "失灵", "闪烁", "故障", "坏了") if w in t), "故障")},
                "confidence": 0.9, "top2": ["aftersale_reason", "aftersale_eligibility"]}
    if "取消订单" in t or ("取消" in t and ("退款" in t or "退钱" in t)):
        return {"intent": "cancel_refund",
                "slots": {"order_no": om.group(0).upper() if (om := re.search(r"SO-\d+", t, re.IGNORECASE)) else ""},
                "confidence": 0.9, "top2": ["cancel_refund", "order_query"]}
    om0 = re.search(r"SO-\d+", t, re.IGNORECASE)
    if any(w in t for w in ("改价", "调价", "降价", "改价格")):
        pm = re.search(r"(?:改价|调价|降价|改价格)[^\d]{0,8}(\d+(?:\.\d+)?)", t)
        if om0 and pm:
            return {"intent": "order_price_amend",
                    "slots": {"order_no": om0.group(0).upper(),
                              "new_price": float(pm.group(1))},
                    "confidence": 0.9,
                    "top2": ["order_price_amend", "order_amend"]}
    if any(w in t for w in ("改地址", "修改地址", "改收货", "换个地址")):
        om_amend = re.search(r"SO-\d+", t, re.IGNORECASE)
        am = re.search(r"(?:改到|改为|改成)\s*([^\s，。]+)", t)
        return {"intent": "order_amend",
                "slots": {"order_no": om_amend.group(0).upper() if om_amend else "",
                          "address": am.group(1) if am else ""},
                "confidence": 0.9, "top2": ["order_amend", "order_query"]}
    if any(w in t for w in ("额度", "授信", "账期", "信用")):
        return {"intent": "credit_query", "slots": {},
                "confidence": 0.9, "top2": ["credit_query", "customer_profile"]}
    if any(w in t for w in ("对账",)):
        pm = re.search(r"\d{4}-\d{2}", t)
        return {"intent": "reconciliation_query",
                "slots": {"period": pm.group(0) if pm else ""},
                "confidence": 0.9,
                "top2": ["reconciliation_query", "customer_profile"]}
    if any(w in t for w in ("资料", "等级", "折扣", "客户编码")):
        return {"intent": "customer_profile", "slots": {},
                "confidence": 0.9, "top2": ["customer_profile", "credit_query"]}
    if any(w in t for w in ("开票", "发票")) and any(
        w in t for w in ("进度", "状态", "记录", "到哪", "查询", "查一下")
    ):
        return {"intent": "invoice_status",
                "slots": {"order_no": om0.group(0).upper() if om0 else ""},
                "confidence": 0.9, "top2": ["invoice_status", "invoice_issue"]}
    if any(w in t for w in ("开票", "发票")):
        return {"intent": "invoice_issue",
                "slots": {"order_no": om0.group(0).upper() if om0 else "",
                          "title": "默认抬头"},
                "confidence": 0.9, "top2": ["invoice_issue", "refund_status"]}
    if ("退款" in t or "退钱" in t) and any(
        w in t for w in ("执行", "发起", "办理", "退钱", "并")
    ):
        return {"intent": "refund_settle",
                "slots": {"order_no": om0.group(0).upper() if om0 else ""},
                "confidence": 0.9, "top2": ["refund_settle", "refund_status"]}
    if "退款" in t or "退钱" in t:
        return {"intent": "refund_status",
                "slots": {"order_no": (om := re.search(r"SO-\d+", t, re.IGNORECASE)) and om.group(0).upper() or "",
                          "sku": skus[0] if skus else ""},
                "confidence": 0.9, "top2": ["refund_status", "aftersale_eligibility"]}
    om = re.search(r"SO-\d+", t, re.IGNORECASE)
    # 实付仲裁优先：含"实付/实际付"是财务口径问题，不是单纯商品估价
    if "实付" in t or ("实际" in t and ("付" in t or "价格" in t or "多少钱" in t)):
        return {"intent": "promo_arbitrate",
                "slots": {"order_no": om.group(0).upper() if om else ""},
                "confidence": 0.9, "top2": ["promo_arbitrate", "promo_evaluate"]}
    # 物流类优先于订单类：「发货了吗/到哪了/什么时候到」问的是履约节点，不是订单状态
    if any(w in t for w in ("物流", "履约", "到货", "发货", "配送", "快递", "运单",
                            "签收", "到哪", "什么时候到", "多久到", "几天到", "送到")):
        return {"intent": "fulfill_track",
                "slots": {"order_no": om.group(0).upper() if om else ""},
                "confidence": 0.9, "top2": ["fulfill_track", "order_query"]}
    if skus and any(w in t for w in ("有货", "现货", "库存", "能发", "可售", "补货")):
        return {"intent": "fulfill_atp", "slots": {"sku": skus[0]},
                "confidence": 0.9, "top2": ["fulfill_atp", "product_lookup"]}
    if any(w in t for w in ("订单", "下单", "我的单", "买过")) or om:
        return {"intent": "order_query",
                "slots": {"order_no": om.group(0).upper() if om else ""},
                "confidence": 0.9, "top2": ["order_query", "fulfill_track"]}
    cm = re.search(r"CLS-\d+", t, re.IGNORECASE)
    if any(w in t for w in ("政策", "条款", "规则", "规定")) or cm:
        return {"intent": "policy_qa",
                "slots": {"query": t, "clause_id": cm.group(0).upper() if cm else ""},
                "confidence": 0.9, "top2": ["policy_qa", "product_search"]}
    if any(w in t for w in ("保修", "退换", "退货资格", "能退", "能换", "资格")):
        return {"intent": "aftersale_eligibility",
                "slots": {"sku": skus[0] if skus else "",
                          "order_no": om.group(0).upper() if om else ""},
                "confidence": 0.9, "top2": ["aftersale_eligibility", "policy_qa"]}
    if ("对比" in t or "比较" in t) and len(skus) >= 2:
        return {"intent": "product_compare", "slots": {"skus": skus},
                "confidence": 0.9, "top2": ["product_compare", "product_lookup"]}
    if any(w in t for w in ("国补", "补贴", "以旧换新")):
        return {"intent": "subsidy_check",
                "slots": {"sku": skus[0] if skus else ""},
                "confidence": 0.9, "top2": ["subsidy_check", "promo_evaluate"]}
    if any(w in t for w in ("优惠", "到手", "多少钱", "价格")):
        return {"intent": "promo_evaluate", "slots": {"sku": skus[0] if skus else ""},
                "confidence": 0.9, "top2": ["promo_evaluate", "product_lookup"]}
    if skus:
        return {"intent": "product_lookup", "slots": {"sku": skus[0]},
                "confidence": 0.9, "top2": ["product_lookup", "promo_evaluate"]}
    if any(w in t for w in ("查", "找", "看看", "商品")):
        keywords = re.sub(r"帮我|查询|看看|找|一下|商品", "", t).strip() or "商品"
        return {"intent": "product_search", "slots": {"keywords": keywords},
                "confidence": 0.75, "top2": ["product_search", "product_lookup"]}
    return {"intent": "unknown", "slots": {}, "confidence": 0.3,
            "top2": ["product_search", "product_lookup"]}


# ---------------------------------------------------------------------------
# 多轮历史（M4 / PRD 10.4）：LLM 历史块 + fake 槽位继承 + 滚动摘要
# ---------------------------------------------------------------------------

# 指代词：当前句出现且缺关键 id 时，才从历史继承槽位（有显式 id 一律以当前句为准）
_HISTORY_PRONOUN_RE = re.compile(r"它|这个|那个|这款|这件|该商品|此商品|上述|刚才|前面")


def _history_block(history: dict) -> str:
    """历史 → 注入 LLM 的文本块（每轮再过一遍 mask_pii，双保险）。"""
    if not isinstance(history, dict):
        return ""
    from shared.pii import mask_pii

    parts = []
    if history.get("summary"):
        parts.append(f"【此前对话摘要】{mask_pii(str(history['summary']))[0]}")
    turns = history.get("recent_turns") or []
    if turns:
        role_names = {"customer": "客户", "ai": "客服", "agent": "坐席"}
        lines = [
            f"{role_names.get(str(t.get('role', '')), str(t.get('role', '')))}："
            f"{mask_pii(str(t.get('text', '')))[0]}"
            for t in turns
        ]
        parts.append("【最近对话】\n" + "\n".join(lines))
    return "\n\n".join(parts)


def _history_carry_slots(text: str, history: dict) -> dict:
    """fake 规则的槽位继承：当前句含指代词时，从历史按类型补最近一次的 id。

    扫描顺序从新到旧：最近窗口 → 掉窗轮次（carry_turns）→ 滚动摘要文本——
    指代对象可能已经掉出最近窗口（PRD 10.4：摘要承载更早历史的指代线索）。
    """
    if not _HISTORY_PRONOUN_RE.search(text):
        return {}
    texts = [str(t.get("text", ""))
             for t in reversed((history or {}).get("recent_turns") or [])]
    texts += [str(t.get("text", ""))
              for t in reversed((history or {}).get("carry_turns") or [])]
    summary = str((history or {}).get("summary") or "")
    if summary:
        texts.append(summary)
    found: dict[str, str] = {}
    for tt in texts:
        if len(found) == 3:
            break
        if "sku" not in found:
            m = _SKU_RE.search(tt)
            if m:
                found["sku"] = m.group(0).upper()
        if "order_no" not in found:
            om = re.search(r"SO-\d+", tt, re.IGNORECASE)
            if om:
                found["order_no"] = om.group(0).upper()
        if "ticket_no" not in found:
            tm = re.search(r"TK-\d+", tt, re.IGNORECASE)
            if tm:
                found["ticket_no"] = tm.group(0).upper()
    return found


def _deterministic_summary(old_summary: str, carry: list[dict]) -> str:
    lines = [old_summary] if old_summary else []
    for t in carry:
        tag = "客户问" if t.get("role") == "customer" else "客服答"
        lines.append(f"{tag}：{str(t.get('text', ''))[:120]}")
    return "；".join(lines)[:800]


def _maybe_session_summary(state: OrchestratorState, config: RunnableConfig) -> dict:
    """滚动摘要（PRD 10.4）：仅当存在"既不在摘要覆盖内、又掉出最近窗口"的轮次
    （carry_turns 非空）时增量生成；结果经 artifact 回写渠道层落库。

    fake 模式确定性拼接（可测不 flaky）；真实模式主模型一次调用，失败回落
    确定性拼接——摘要属于增强信息，绝不让它拖垮交付。
    """
    history = state.get("history") or {}
    carry = [t for t in (history.get("carry_turns") or []) if t.get("text")]
    if not carry:
        return {}
    old_summary = str(history.get("summary") or "")
    upto = max(int(t.get("id") or 0) for t in carry)
    summary = ""
    if get_settings().llm_provider != "fake":
        try:
            from shared.llm import get_main_llm

            payload = {"previous_summary": old_summary, "new_turns": [
                {"role": t.get("role"), "text": str(t.get("text", ""))[:200]}
                for t in carry
            ]}
            resp = get_main_llm().invoke(
                [
                    SystemMessage(content=(
                        "把客服对话的旧摘要与新轮次合并为一段不超过 200 字的滚动摘要；"
                        "保留商品/订单/工单编号与关键结论，只输出摘要正文。")),
                    HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
                ],
                config=config,
            )
            summary = str(resp.content)[:800]
        except Exception as exc:  # noqa: BLE001
            logger.warning("滚动摘要 LLM 调用失败，回落确定性拼接：%s", exc)
    if not summary:
        summary = _deterministic_summary(old_summary, carry)
    return {"summary": summary, "upto_message_id": upto}


def _clarify_options(top2) -> list[str]:  # noqa: ANN001
    """澄清选项白名单化：只保留合法且非 unknown 的标签，去重保序，最多两个。

    真模型可能把自造标签（"product_query"/中文名/大小写变体）填进 top2——
    这些不能展示给用户。过滤后不足两个时，understand 不再澄清，直接走拒答。
    """
    opts: list[str] = []
    for x in top2 or []:
        if isinstance(x, str) and x in _INTENT_SET and x != "unknown" and x not in opts:
            opts.append(x)
    return opts[:2]


def _classify(text: str, config: RunnableConfig | None,
              history: dict | None = None) -> dict:
    if get_settings().llm_provider == "fake":
        u = rule_understand(text, history)
    else:
        block = _history_block(history or {})
        content = f"{block}\n\n【当前用户输入】{text}" if block else text
        llm = get_small_llm().with_structured_output(Understanding)
        u = llm.invoke(
            [SystemMessage(content=UNDERSTAND_SYSTEM), HumanMessage(content=content)],
            config=config,
        )
        u = {"intent": u.intent, "slots": u.slots, "confidence": u.confidence,
             "top2": u.top2}
    if u["intent"] not in _INTENT_SET:
        u = {"intent": "unknown", "slots": {}, "confidence": 0.3,
             "top2": u.get("top2", [])[:2]}
    # 澄清选项只认白名单；不足两个 → understand 的条件不成立，直接拒答
    u["top2"] = _clarify_options(u.get("top2"))
    return u


# ---------------------------------------------------------------------------
# 节点
# ---------------------------------------------------------------------------

def intake(state: OrchestratorState, config: RunnableConfig) -> dict:
    """规范化 + 输入安全 + PII 脱敏 + 身份槽位（只从 token claim 取）。"""
    raw = state["messages"][-1].content if state.get("messages") else ""
    text = str(raw).strip()[:2000]
    s = get_settings()
    findings = []
    if s.pii_masking_enabled:
        text, findings = mask_pii(text)
    from shared.request_context import current_request_meta

    meta = {
        **(config.get("metadata") or {} if config else {}),
        **current_request_meta(),
    }
    auth = auth_from_config(config)
    if auth is None:
        raise RuntimeError("missing verified auth context")
    # 结构化 action 只认服务端签发的短时 action token（aud=orchestrator, typ=action）；
    # 未签名 metadata.action 一律忽略并审计（Phase 0 v2 §2：客户端只能传 action_id）。
    action_token = str(meta.get("action_token") or "")
    forced_intent = ""
    forced_slots: dict = {}
    if action_token:
        claims = decode_action_token(action_token)
        identity_ok = (
            claims is not None
            and str(claims.get("tenant_id", "")) == auth.tenant_id
            and str(claims.get("sub", "")) == auth.actor_id
        )
        conversation_ok = claims is not None and str(
            claims.get("conversation_id", "")
        ) == str(meta.get("conversation_id", ""))
        intent_ok = (
            claims is not None
            and str(claims.get("intent") or "") in _INTENT_SET
            and isinstance(claims.get("slots"), dict)
        )
        if claims is None or not identity_ok or not conversation_ok or not intent_ok:
            reason = (
                "invalid_token" if claims is None
                else "identity_mismatch" if not identity_ok
                else "conversation_mismatch" if not conversation_ok
                else "invalid_intent"
            )
            audit_from_config(
                config, "action.forced_intent_rejected", target="intake",
                params={"reason": reason},
                context_id=str(meta.get("context_id", "")),
                trace_id=str(meta.get("trace_id", "")),
            )
        else:
            forced_intent = str(claims["intent"])
            forced_slots = dict(claims["slots"])
            audit_from_config(
                config, "action.forced_intent_accepted", target=forced_intent,
                params={"action_id": str(claims.get("action_id", "")),
                        "conversation_id": str(claims.get("conversation_id", ""))},
                context_id=str(meta.get("context_id", "")),
                trace_id=str(meta.get("trace_id", "")),
            )
    elif meta.get("action"):
        audit_from_config(
            config, "action.forced_intent_rejected", target="intake",
            params={"reason": "unsigned_action_metadata"},
            context_id=str(meta.get("context_id", "")),
            trace_id=str(meta.get("trace_id", "")),
        )
    if findings:
        audit_from_config(
            config, "pii.mask", target="intake",
            params={"kinds": sorted({f.kind for f in findings}),
                    "count": len(findings)},
            result_summary="输入 PII 已脱敏（原文不入库/不进 LLM）",
            context_id=str(meta.get("context_id", "")),
            trace_id=str(meta.get("trace_id", "")),
        )
    return {
        "user_text": text,
        "trace_id": meta.get("trace_id") or new_trace_id(),
        "context_id": meta.get("context_id") or "",
        # customer 角色以 token.sub 为准；坐席/主管按会话对象（同租户）传 metadata
        "customer_id": auth_customer_id(config) or meta.get("customer_id") or "",
        "tenant_id": auth.tenant_id,
        "actor_id": auth.actor_id,
        "actor_role": auth.role,
        "scopes": sorted(auth.scopes),
        "pii_masked": len(findings),
        # 碰壁计数由渠道层（console）跨 Task 续传：本 Task 内触发即 +1
        "hit_wall_count": int(meta.get("hit_wall_count") or 0),
        # 多轮历史（M4 / PRD 10.4）：渠道层随 metadata 续传；进 LLM 前会再脱敏
        "history": meta.get("history") if isinstance(meta.get("history"), dict) else {},
        # 混沌注入（M3 Saga 演练）：e2e 经 metadata 传 chaos=finance_settle_fail
        "chaos": meta.get("chaos") or "",
        "risk_flag": bool(_RISK_RE.search(text)),
        "snapshot_id": uuid.uuid4().hex[:12],
        "clarify_count": 0,
        "prompt_version": UNDERSTAND_VERSION,
        "forced_intent": forced_intent,
        "forced_slots": forced_slots or {},
        "confirm_token": str(meta.get("confirm_token") or ""),
        "approval_id": str(meta.get("approval_id") or ""),
    }


def understand(state: OrchestratorState, config: RunnableConfig) -> dict:
    """LLM 结构化输出 intent+slots+confidence；低置信 interrupt() 澄清一次（6.1.1）。

    渠道层结构化 action 已给出 intent/slots 时直接采用（只跳过 NLU，不跳过授权）。
    """
    if state.get("forced_intent"):
        return {
            "intent": state["forced_intent"],
            "slots": state.get("forced_slots") or {},
            "confidence": 1.0,
        }
    u = _classify(state["user_text"], config, state.get("history"))
    # 澄清选项在使用点再过滤一次（纵深防御）：野标签不展示给用户；
    # 过滤后不足两个 → 条件不成立，直接落单走 route→nomatch 拒答。
    options = _clarify_options(u.get("top2"))
    if (
        u["confidence"] < LOW_CONFIDENCE_THRESHOLD
        and u["intent"] == "unknown"
        and state.get("clarify_count", 0) < 1
        and len(options) >= 2
        and not state.get("risk_flag")  # 高风险（投诉/纠纷类）不反问，直接转人工（PRD 7.12 触发 3）
    ):
        reply = interrupt({
            "type": "clarify",
            "question": f"您是想「{options[0]}」还是「{options[1]}」？",
            "options": options,
        })
        u2 = _classify(sanitize_text(str(reply)), config, state.get("history"))
        return {
            "intent": u2["intent"], "slots": u2["slots"],
            "confidence": u2["confidence"],
            "clarify_count": 1, "resume_reply": str(reply),
            "decision_log": [{
                "stage": "understand", "event": "clarify",
                "asked_top2": options, "reply": str(reply),
            }],
        }
        # 注：碰壁计数统一在 nomatch 节点 +1（澄清耗尽的轮次也会走 nomatch），
        # 此处不再累计，避免同一轮双计。
    return {"intent": u["intent"], "slots": u["slots"], "confidence": u["confidence"]}


def route(state: OrchestratorState, config: RunnableConfig) -> dict:
    """查路由表 → Plan（纯代码，AC-21）；并生成快照归属。"""
    plan = route_plan(state["intent"], state["slots"], state["confidence"])
    audit_from_config(
        config, "route.plan", target=state.get("intent", ""),
        params={"slots": state.get("slots") or {},
                "confidence": state.get("confidence", 0)},
        result_summary=json.dumps(plan, ensure_ascii=False, default=str),
        context_id=state.get("context_id", ""),
        trace_id=state.get("trace_id", ""),
    )
    log_entry = {
        "stage": "route", "intent": state.get("intent", ""),
        "domains": list((plan or {}).get("domains") or []),
        "mode": (plan or {}).get("mode", ""),
        "confidence": state.get("confidence", 0),
    }
    return {"plan": plan, "decision_log": [log_entry]}


def after_route(state: OrchestratorState) -> Literal["advance", "nomatch", "smalltalk", "handoff"]:
    """转人工三触发（PRD 7.12）：明确要求 / 碰壁≥2 / 高风险——优先于其他路由。"""
    if (
        state["intent"] == "request_human"
        or state.get("risk_flag")
        or state.get("hit_wall_count", 0) >= 2
    ):
        return "handoff"
    plan = state.get("plan")
    if plan is None:
        return "nomatch"
    if plan["mode"] == "smalltalk":
        return "smalltalk"
    return "advance"


def _delegation_text(intent: str, slots: dict, domain: str = "") -> str:
    """把意图+槽位翻成给域 Agent 的自然语言委托文本（域内 LLM/fake 都按自然语言解析）。

    并行链路（warranty_check）按域定制话术；域不认识时回落意图原文。
    """
    sku = slots.get("sku", "")
    if intent == "product_lookup":
        return f"查询商品 {sku} 的详情"
    if intent == "product_search":
        return f"搜索商品：{slots.get('keywords', '')}"
    if intent == "product_compare":
        skus = slots.get("skus") or []
        return "对比商品 " + ",".join(skus)
    if intent == "promo_evaluate":
        return f"估算商品 {sku} 的到手价"
    if intent == "subsidy_check":
        return f"查商品 {sku} 的国补资格与补贴价"
    if intent == "aftersale_eligibility":
        base = f"判定 {sku} 的保修资格"
        if slots.get("order_no"):
            base += f"，订单 {slots.get('order_no')}"
        return base
    if intent == "aftersale_reason":
        return f"判定责任：{slots.get('fault_type', '故障')}"
    if intent == "ticket_create":
        return f"为 {sku} 创建维修工单，故障：{slots.get('fault_type', '故障')}"
    if intent == "ticket_query":
        return f"查询工单 {slots.get('ticket_no', '')}"
    if intent == "policy_qa":
        return f"查询政策：{slots.get('query', slots.get('keywords', '政策'))}"
    if intent == "warranty_check":
        per_domain = {
            "aftersale": f"判定 {sku or '该商品'} 的保修资格",
            "policy": "查询保修政策条款",
            "merchandising": f"查询商品 {sku or ''} 的详情" if sku else "搜索商品",
        }
        return per_domain.get(domain, f"{slots.get('query', '')}")
    if intent == "order_query":
        order_no = slots.get("order_no")
        return f"查询订单 {order_no} 的状态" if order_no else "查询当前客户的订单状态"
    if intent == "fulfill_track":
        order_no = slots.get("order_no")
        return f"查询订单 {order_no} 的物流节点" if order_no else "查询当前客户订单的物流节点"
    if intent == "fulfill_atp":
        return f"查询 {slots.get('sku', '')} 的可承诺库存与发货时效"
    if intent == "invoice_issue":
        return f"为订单 {slots.get('order_no', '')} 开具发票，抬头 {slots.get('title', '默认抬头')}"
    if intent == "invoice_status":
        no = slots.get("order_no") or "当前客户"
        return f"查询 {no} 的开票记录与进度"
    if intent == "refund_settle":
        return f"为订单 {slots.get('order_no', '')} 执行退款"
    if intent == "customer_profile":
        return "查询当前客户的资料、等级与专属折扣"
    if intent == "credit_query":
        return "查询当前客户的授信额度、已用/剩余额度与账期"
    if intent == "reconciliation_query":
        period = slots.get("period") or ""
        base = "查询当前客户的对账单"
        return base + (f"（周期 {period}）" if period else "")
    if intent == "complaint_list":
        return "查询当前客户的投诉记录"
    if intent == "ad_analysis":
        return f"分析投放效果：{slots.get('query') or '近 14 天整体投放效果与方案'}"
    if intent == "product_selection":
        return f"选品分析：{slots.get('query') or '按销量增速与竞品价差给出选品候选'}"
    if intent == "competitor_watch":
        return (f"竞品价格监控：{slots.get('query') or '对比竞品最新价并给出降价提醒'}"
                + (f"（聚焦 {slots.get('sku')}）" if slots.get("sku") else ""))
    if intent == "ad_plan_save":
        return (f"保存经营方案：kind={slots.get('kind', 'general')}，"
                f"target={slots.get('target', '')}，备注：{slots.get('note', '')}")
    if intent == "hot_restock":
        per_domain = {
            "marketing": "查询近 7 天销量环比增速最高的爆款 SKU（top1，含增速与 GMV）",
            "fulfillment": (
                f"查询爆款 {slots.get('top_sku', '')} 的可承诺库存与发货时效，"
                "评估是否需要补货"),
        }
        return per_domain.get(domain, "爆款分析与备货评估")
    if intent == "order_amend":
        no = slots.get("order_no") or "当前客户订单"
        if slots.get("new_price") is not None:
            order_text = f"订单 {no} 改价，改价到 {slots.get('new_price')} 元"
        else:
            order_text = f"订单 {no} 改地址，改到 {slots.get('address', '')}"
        per_domain = {
            "fulfillment": f"查询订单 {no} 的物流状态（判断能否改地址）",
            "order": order_text,
        }
        return per_domain.get(domain, order_text)
    if intent == "order_price_amend":
        return f"订单 {slots.get('order_no', '')} 改价到 {slots.get('new_price', '')} 元"
    if intent == "refund_status":
        no = slots.get("order_no") or "当前客户订单"
        sku = slots.get("sku", "")
        per_domain = {
            "aftersale": f"判定 {sku or '该商品'} 的退货退款资格",
            "fulfillment": f"查询订单 {no} 的退货入库与物流状态",
            "finance": f"查询订单 {no} 的退款单与到账时间",
        }
        return per_domain.get(domain, f"订单 {no} 退款进度")
    if intent == "promo_arbitrate":
        no = slots.get("order_no") or "当前客户订单"
        per_domain = {
            "order": f"查询订单 {no} 的状态",
            "merchandising": f"估算商品 {slots.get('sku', '')} 的到手价",
            "finance": f"查询订单 {no} 的实付账单",
        }
        return per_domain.get(domain, f"订单 {no} 实付金额")
    if intent == "cancel_refund":
        no = slots.get("order_no") or ""
        per_domain = {
            "order": f"取消订单 {no}，原因：用户申请退款",
            "finance": f"为订单 {no} 执行退款",
        }
        return per_domain.get(domain, f"取消订单 {no} 并退款")
    return json.dumps({"intent": intent, "slots": slots}, ensure_ascii=False)


def _send_payload(state: OrchestratorState, domain: str, resume: str = "",
                  pending: dict | None = None) -> dict:
    """构造 Send 给 domain_call 的输入（Send 输入是独立 state，不继承全图）。"""
    return {
        "domain": domain,
        "intent": state.get("intent", ""),
        "slots": state.get("slots") or {},
        "trace_id": state.get("trace_id", ""),
        "snapshot_id": state.get("snapshot_id", ""),
        "customer_id": state.get("customer_id", ""),
        "context_id": state.get("context_id", ""),
        "tenant_id": state.get("tenant_id", ""),
        "actor_id": state.get("actor_id", ""),
        "actor_role": state.get("actor_role", ""),
        "user_text": state.get("user_text", ""),
        "chaos": state.get("chaos", ""),
        "resume": resume,
        "pending": pending or {},
        "confirm_token": state.get("confirm_token", ""),
        "approval_id": state.get("approval_id", ""),
        "plan_mode": (state.get("plan") or {}).get("mode", ""),
        "plan_domains": list((state.get("plan") or {}).get("domains") or []),
    }


def advance(state: OrchestratorState, config: RunnableConfig):  # noqa: ANN201
    """拓扑调度器（技术方案 2.3 的 M2 版）：无入边域并发扇出，收齐即合成。

    M2 并行 = 单层（Plan.edges 恒空，全部域无入边）；M3 在此函数加分层与
    上游否决传播（propagate_cancellation）。输出经 Command(goto=[Send...])。
    """
    plan = state.get("plan")
    if not plan:
        from langgraph.types import Command

        return Command(goto="synthesize")

    from langgraph.types import Command, Send

    # 单域链路的澄清挂起：回填后按 pending_task 续跑（多域并行下 input-required
    # 视为该域局部交付——见 domain_call，避免多分支写 pending 冲突）
    pending = state.get("pending_task")
    if pending:
        resume = state.get("resume_reply") or state.get("user_text") or ""
        return Command(goto=[Send("domain_call", _send_payload(
            state, pending["domain"], resume=resume, pending=pending))])

    # ---- 串行链（M3）：列表即链序，上游完成才发下一跳（PRD 7.9）----
    if plan["mode"] == "serial":
        return _advance_serial(state, plan)

    # ---- 跨域仲裁（M3）：分层 DAG——deps 就绪才发，上游失败取消依赖域 ----
    if plan["mode"] == "arbitrate":
        return _advance_arbitrate(state, plan)

    done = set((state.get("artifacts") or {}).keys())
    remaining = [d for d in plan["domains"] if d not in done]
    if remaining:
        return Command(goto=[Send("domain_call", _send_payload(state, d))
                             for d in remaining])
    return Command(goto="synthesize")


# 仲裁链的跨域数据注入：上游产物 → 下游委托槽位（跨域边由编排层持有，PRD 7.7 注）
ARBITRATE_INJECTIONS: dict[tuple[str, str], str] = {
    # 商品域估算到手价需要 sku——从订单域产物取（order_query 返回 sku 字段）
    ("promo_arbitrate", "merchandising"): "sku",
    # 履约域查爆款备货需要 top_sku——从营销域 hot_rank 产物取（扁平字段）
    ("hot_restock", "fulfillment"): "top_sku",
}


def _advance_arbitrate(state: dict, plan: dict):
    """分层 DAG：无依赖域立即并发；有依赖域等上游完成并注入其产物。"""
    from langgraph.types import Command, Send

    domains = plan["domains"]
    deps = plan.get("deps") or {}
    artifacts = state.get("artifacts") or {}
    ready, cancelled = ready_domains(domains, deps, artifacts)

    updates: dict = {}
    if cancelled:
        updates["artifacts"] = {
            d: {"authority_domain": d,
                "data": {"error": "cancelled_by_upstream", "reason": reason}}
            for d, reason in cancelled
        }
        updates["decision_log"] = [
            {"stage": "advance", "event": "veto", "reason": reason,
             "cancelled": d} for d, reason in cancelled
        ]

    if ready:
        slots_base = state.get("slots") or {}
        sends = []
        for d in ready:
            slots = dict(slots_base)
            field = ARBITRATE_INJECTIONS.get((state.get("intent", ""), d))
            if field:
                for u in deps.get(d, []):
                    udata = (artifacts.get(u) or {}).get("data") or {}
                    if isinstance(udata, dict) and udata.get(field):
                        slots[field] = udata[field]
                        break
            payload = _send_payload(state, d)
            payload["slots"] = slots
            sends.append(Send("domain_call", payload))
        if updates:
            return Command(goto=[Send("domain_call", s) for s in sends],
                           update=updates)
        return Command(goto=sends)
    if updates:
        return Command(goto="synthesize", update=updates)
    return Command(goto="synthesize")


def _advance_serial(state: dict, plan: dict):
    """串行链调度：一跳一跳发；上游否决 → 取消全部下游（PRD 7.9 第 4 步）；
    Saga 链尾跳失败且正向写已落 → 转 saga_compensate（PRD 7.7）。"""
    from langgraph.types import Command, Send

    chain = plan["domains"]
    artifacts = state.get("artifacts") or {}
    done = {d for d in chain if d in artifacts}
    intent = state.get("intent", "")

    # Saga 补偿检查：全链走完且尾跳失败、链上有已成功的正向写 → 补偿
    saga = SAGA_CHAINS.get(intent)
    if saga and len(done) == len(chain):
        tail = (artifacts.get(chain[-1]) or {}).get("data") or {}
        if isinstance(tail, dict) and tail.get("error"):
            return Command(goto="saga_compensate")

    for i, d in enumerate(chain):
        if d in done:
            continue
        reason = veto_reason(intent, chain, artifacts)
        if reason:
            # 上游否决：链上剩余域全部标记取消（不调用），直接合成作答
            cancelled = {
                d2: {"authority_domain": d2,
                     "data": {"error": "cancelled_by_upstream", "reason": reason}}
                for d2 in chain[i:]
            }
            return Command(
                goto="synthesize",
                update={
                    "artifacts": cancelled,
                    "decision_log": [{"stage": "advance", "event": "veto",
                                      "reason": reason,
                                      "cancelled": list(chain[i:])}],
                },
            )
        return Command(goto=[Send("domain_call", _send_payload(state, d))])
    return Command(goto="synthesize")


async def saga_compensate(state: dict, config: RunnableConfig) -> dict:
    """Saga 补偿（PRD 7.7）：链上正向写已落、尾跳失败 → 逆序执行补偿动作。

    硬规则：补偿幂等（biz_key 随委托透传）；补偿失败升级人工队列，
    禁止静默——悬挂状态比失败更危险。
    """
    from a2a.types import TaskState as TS

    from shared.a2a_client import delegate

    auth = auth_from_config(config)
    intent = state.get("intent", "")
    saga = SAGA_CHAINS.get(intent) or {}
    artifacts = state.get("artifacts") or {}
    results, log = [], []
    all_ok = True

    for comp in reversed(saga.get("compensations", [])):  # 逆序（硬规则 4）
        step = (saga.get("writes") or {}).get(comp["domain"]) or {}
        src = (artifacts.get(comp["domain"]) or {}).get("data") or {}
        biz_key = (src.get("biz_key")
                   or f"{state.get('context_id', '')}:"
                      f"{(state.get('slots') or {}).get('order_no', '')}:"
                      f"{step.get('biz_key_suffix', comp['domain'])}")
        text = comp["text"].format(biz_key=biz_key)
        meta = {
            "trace_id": state.get("trace_id", ""),
            "context_id": state.get("context_id", ""),
            "snapshot_id": state.get("snapshot_id", ""),
            "customer_id": state.get("customer_id", ""),
            "tenant_id": state.get("tenant_id", ""),
            "actor_id": state.get("actor_id", ""),
        }
        try:
            r = await delegate(
                domain_base_url(comp["domain"]), text, metadata=meta, auth=auth
            )
            # 业务回执校验：任务 completed 不代表补偿生效（如 biz_key 未命中）——
            # 工具回执带 verified=True / 无 error 才算补偿成功（PRD 7.7 硬规则 3）
            data = r.data_parts[0] if r.data_parts else {}
            payload = data.get("data") if isinstance(data, dict) else None
            ok = r.state == TS.TASK_STATE_COMPLETED and not (
                isinstance(payload, dict)
                and (payload.get("error") or payload.get("verified") is False))
        except Exception as e:  # noqa: BLE001
            logger.warning("Saga 补偿 %s 不可达：%s", comp["domain"], e)
            ok = False
        all_ok = all_ok and ok
        results.append({"domain": comp["domain"], "skill": comp["skill"],
                        "ok": ok, "biz_key": biz_key})
        log.append({"stage": "saga_compensate", "domain": comp["domain"],
                    "skill": comp["skill"], "ok": ok, "biz_key": biz_key})

    saga_result: dict = {"status": "rolled_back" if all_ok else "escalating",
                         "compensations": results}
    if not all_ok:
        # 补偿失败 → 升级人工（复用 handoff.accept，PRD 7.7 硬规则 3）
        package = {
            "context_id": state.get("context_id") or state.get("trace_id", ""),
            "trace_id": state.get("trace_id", ""),
            "customer_id": state.get("customer_id", ""),
            "reasons": [f"Saga 补偿失败，悬挂状态需人工处理（intent={intent}）"],
            "domain_artifacts": artifacts,
            "decision_log": log,
            "ai_draft": "",
            "user_text": state.get("user_text", ""),
            "history": [],
        }
        try:
            r = await delegate(
                domain_base_url("humandesk"),
                json.dumps(package, ensure_ascii=False),
                metadata={"trace_id": state.get("trace_id", ""),
                          "context_id": package["context_id"],
                          "tenant_id": state.get("tenant_id", ""),
                          "actor_id": state.get("actor_id", "")},
                auth=auth,
            )
            saga_result["status"] = "escalated"
            saga_result["escalation_task"] = r.task_id
        except Exception as e:  # noqa: BLE001
            saga_result["status"] = "escalation_failed"
            saga_result["escalation_error"] = str(e)
            logger.error("Saga 升级人工失败（禁止静默）：%s", e)
        log.append({"stage": "saga_compensate", "event": saga_result["status"]})

    return {"saga_result": saga_result, "decision_log": log}


async def domain_call(payload: dict, config: RunnableConfig) -> dict:
    """单域 A2A 委托（Send 并发实例，每域一个）。

    输出经 reducer 并回主图：artifacts 用 merge_dict（每分支只写自己的 key，
    并行安全）；decision_log 用 merge_list。
    """
    from a2a.types import TaskState

    from shared.a2a_client import delegate

    import time as _time

    started = _time.perf_counter()
    domain = payload["domain"]
    pending = payload.get("pending") or {}
    auth = auth_from_config(config)
    delegation = (payload.get("resume") if pending else None) or _delegation_text(
        payload.get("intent", ""), payload.get("slots") or {}, domain)
    meta = {
        "trace_id": payload.get("trace_id", ""),
        "snapshot_id": payload.get("snapshot_id", ""),
        "customer_id": payload.get("customer_id", ""),
        "context_id": payload.get("context_id", ""),
        "tenant_id": payload.get("tenant_id", ""),
        "actor_id": payload.get("actor_id", ""),
        "slots": json.dumps(payload.get("slots") or {}, ensure_ascii=False),
        "chaos": payload.get("chaos", ""),
        "confirm_token": payload.get("confirm_token", ""),
        "approval_id": payload.get("approval_id", ""),
    }
    try:
        if pending:
            result = await delegate(
                domain_base_url(domain),
                payload.get("resume") or payload.get("user_text", ""),
                task_id=pending.get("task_id"), context_id=pending.get("context_id"),
                metadata=meta, auth=auth,
            )
        else:
            result = await delegate(
                domain_base_url(domain),
                _delegation_text(payload.get("intent", ""), payload.get("slots") or {}, domain),
                metadata=meta, auth=auth,
            )
    except Exception as e:  # noqa: BLE001
        # 域不可达（连接拒绝/超时）：该域缺席=局部交付（PRD 7.3.6），
        # 不让单域故障拖垮整条链；M1 不重试，M3 在此接一级重试+二级降级
        logger.warning("域 %s 不可达：%s", domain, e)
        return {
            "artifacts": {domain: {"authority_domain": domain,
                                   "data": {"error": f"unreachable: {type(e).__name__}"}}},
            "decision_log": [{"stage": "domain_call", "domain": domain,
                              "state": "unreachable", "input_summary": delegation[:200],
                              "output_summary": f"unreachable: {e}",
                              "latency_ms": int((_time.perf_counter() - started) * 1000),
                              "error": str(e)}],
        }

    if result.state == TaskState.TASK_STATE_COMPLETED:
        artifact = result.data_parts[0] if result.data_parts else {}
        if isinstance(artifact, dict) and result.text:
            # 域侧 artifact_builder 的第二段 text part 已是人话摘要；
            # fake 合成复用它，避免把内部 JSON 原样回给客户。
            artifact = {**artifact, "summary": result.text}
        a_data = artifact.get("data") if isinstance(artifact, dict) else {}
        out = {
            "artifacts": {domain: artifact},
            "decision_log": [{
                "stage": "domain_call", "domain": domain,
                "task_id": result.task_id, "state": "completed",
                "skill": artifact.get("skill") if isinstance(artifact, dict) else "",
                "input_summary": delegation[:200],
                "output_summary": json.dumps(a_data, ensure_ascii=False)[:300],
                "latency_ms": int((_time.perf_counter() - started) * 1000),
                "error": str(a_data.get("error") or "") if isinstance(a_data, dict) else "",
            }],
        }
        if pending:
            # 清除挂起。pending 路径永远是单分支（见 advance），无并行写冲突
            out["pending_task"] = None
        return out
    if result.state == TaskState.TASK_STATE_INPUT_REQUIRED and pending:
        # 仅单域续跑链路（ask_user 回填路径）会再进 input-required：继续上抛
        return {
            "pending_task": {"domain": domain, "task_id": result.task_id,
                             "context_id": result.context_id},
            "paused_questions": [result.status_data or {"type": "domain_question"}],
            "decision_log": [{
                "stage": "domain_call", "domain": domain,
                "task_id": result.task_id, "state": "input_required",
                "input_summary": delegation[:200],
                "latency_ms": int((_time.perf_counter() - started) * 1000),
            }],
        }
    if result.state == TaskState.TASK_STATE_INPUT_REQUIRED:
        # 单域链路首次追问：写 pending_task，交 after_domain_call → ask_user 上抛；
        # 并行链路仍按局部交付处理（避免多分支同时写 pending）。
        if payload.get("plan_mode") == "single" and len(
            payload.get("plan_domains") or []
        ) == 1:
            return {
                "pending_task": {"domain": domain, "task_id": result.task_id,
                                 "context_id": result.context_id},
                "paused_questions": [result.status_data or {"type": "domain_question"}],
            }
        return {
            "artifacts": {domain: {
                "authority_domain": domain,
                "data": {"status": "input_required",
                         "question": str((result.status_data or {}).get("question", ""))},
            }},
            "decision_log": [{"stage": "domain_call", "domain": domain,
                              "state": "input_required(partial)"}],
        }
    # rejected / failed / canceled：不重试（PRD 7.3.6），该域缺席=局部交付
    return {
        "artifacts": {domain: {"authority_domain": domain,
                               "data": {"error": str(result.state)}}},
        "decision_log": [{"stage": "domain_call", "domain": domain,
                          "task_id": result.task_id, "state": str(result.state),
                          "input_summary": delegation[:200],
                          "latency_ms": int((_time.perf_counter() - started) * 1000),
                          "error": str(result.state)}],
    }


def after_domain_call(state: OrchestratorState) -> Literal["ask_user", "advance"]:
    """单域链路域追问 → ask_user 上抛；否则回 advance 汇聚（并行下 pending 恒空）。"""
    return "ask_user" if state.get("pending_task") else "advance"


def ask_user(state: OrchestratorState) -> dict:
    """把域的追问原样上抛（interrupt payload → input-required），回填后重新 dispatch。"""
    payload = (state.get("paused_questions") or [{}])[-1]
    reply = interrupt(payload)
    return {"resume_reply": str(reply)}


def _arbitrate_answer(state: dict, artifacts: dict,
                      text_only: bool = True) -> tuple[str, list[dict]]:
    """promo_arbitrate 的合成口径：先裁决、后说话（PRD 7.6：LLM 不参与裁决）。

    实付金额以财务账单为权威；营销到手价估算只能作参考（estimate 标记）；
    财务无数据时按缺失处理，不得用估算补位（7.6 规则 2）。
    """
    slots = state.get("slots") or {}
    order_no = slots.get("order_no") or ""
    candidates = []
    m = (artifacts.get("merchandising") or {}).get("data") or {}
    if isinstance(m, dict) and "estimate_price" in m:
        candidates.append({"domain": "merchandising", "value": m["estimate_price"],
                           "estimate": True,
                           "applied": m.get("applied") or []})
    f = (artifacts.get("finance") or {}).get("data") or {}
    if isinstance(f, dict) and "amount" in f:
        candidates.append({"domain": "finance", "value": f["amount"],
                           "note": "实付账单"})
    arb = arbitrate_value("deal_amount", candidates)
    if arb["value"] is None:
        text = (f"订单 {order_no} 的实付金额暂不可查"
                f"（财务域无数据，按 PRD 7.6 不得用营销估算补位）。")
        if candidates:
            text += f"营销到手价估算 {candidates[0]['value']} 元仅供参考（估算标记）。"
    else:
        text = f"订单 {order_no} 实付金额以财务账单为准：{arb['value']} 元。"
        for c in arb["discarded"]:
            if c.get("estimate"):
                text += (f"营销域到手价估算 {c['value']} 元"
                         f"（{'；'.join(c.get('applied') or [])}，估算标记）"
                         f"仅为参考，不作为成交价。")
    log = [{"stage": "arbitrate_value", **arb}]
    return text, log


def _fake_artifact_parts(artifacts: dict) -> list[str]:
    """fake 模式的客户可见文案：优先域摘要，绝不回显原始 data。

    没有 summary 时只给通用答复（卡片已由控制台从 artifact 渲染），
    避免把 tenant_id/customer_id/tool_events 等内部字段发给客户。
    """
    parts: list[str] = []
    for domain, a in artifacts.items():
        data = a.get("data") if isinstance(a, dict) else None
        as_of = a.get("as_of", "") if isinstance(a, dict) else ""
        # 上游否决取消（PRD 7.9 第 4 步）：明确说"谁被取消、为什么"
        if isinstance(data, dict) and data.get("error") == "cancelled_by_upstream":
            parts.append(
                f"【{DOMAIN_NAMES.get(domain, domain)}已取消："
                f"{data.get('reason', '上游未通过')}】")
            continue
        # 局部交付标注（PRD 7.3.6 三级）：失败域/需补充域明确说出来，不编造
        if isinstance(data, dict) and data.get("error"):
            parts.append(f"【{DOMAIN_NAMES.get(domain, domain)}域暂不可用】")
            continue
        if isinstance(data, dict) and data.get("status") == "input_required":
            parts.append(
                f"【{DOMAIN_NAMES.get(domain, domain)}域需补充信息："
                f"{data.get('question', '')}】")
            continue
        summary = a.get("summary") if isinstance(a, dict) else ""
        if summary:
            parts.append(
                f"【{DOMAIN_NAMES.get(domain, domain)}】{summary}"
                f"（数据时点 {as_of}）"
            )
            continue
        parts.append(
            f"【{DOMAIN_NAMES.get(domain, domain)}】已完成查询，"
            f"请查看下方结果卡片（数据时点 {as_of}）。"
        )
    return parts


def synthesize(state: OrchestratorState, config: RunnableConfig) -> dict:
    """LLM 流式合成 + 来源绑定（FR-1.10 的 M1 非流式版）。

    合成前先过权威源裁决（PRD 7.6：LLM 只负责把裁决结论说人话），
    并做跨域快照时点检查（PRD 7.8：不得静默混用不同时点的数据）。
    """
    artifacts, arb_log = arbitrate_fields(state.get("artifacts") or {})
    drift_note = check_snapshot_drift(artifacts)
    if get_settings().llm_provider == "fake" and state.get("intent") == "promo_arbitrate":
        final_text, arb_log = _arbitrate_answer(state, artifacts)
        audit_from_config(
            config, "arbitrate.result", target=state.get("intent", ""),
            params={"decision_log": arb_log},
            result_summary=final_text[:200],
            context_id=state.get("context_id", ""),
            trace_id=state.get("trace_id", ""),
        )
        return {"final_text": final_text, "prompt_version": SYNTHESIZE_VERSION,
                "session_summary": _maybe_session_summary(state, config),
                "decision_log": arb_log}
    if get_settings().llm_provider == "fake":
        parts = _fake_artifact_parts(artifacts)
        final_text = "；".join(parts) if parts else "已完成您的请求。"
        if drift_note:
            final_text += f"（{drift_note}）"
    else:
        from langchain_core.messages import HumanMessage as HM

        from shared.llm import get_main_llm

        llm = get_main_llm()
        # 外部模型只拿脱敏后的 artifacts/user；地址/手机/邮箱/证件/客户 ID 不外发。
        user_payload = {
            "user": sanitize_text(state["user_text"]),
            "artifacts": sanitize_for_llm(artifacts),
        }
        # M4（PRD 10.4）：历史块供合成阶段衔接指代（"刚才那个商品"），已脱敏
        hist_block = _history_block(state.get("history") or {})
        if hist_block:
            user_payload["history"] = hist_block
        if state.get("intent") == "promo_arbitrate":
            _, arb_log = _arbitrate_answer(state, artifacts, text_only=False)
            user_payload["arbitration"] = [e for e in arb_log
                                           if e.get("stage") == "arbitrate_value"]
        resp = llm.invoke(
            [
                SystemMessage(content=SYNTHESIZE_SYSTEM),
                HM(content=json.dumps(user_payload, ensure_ascii=False)),
            ],
            config=config,
        )
        final_text = resp.content
        if drift_note:
            final_text += f"（{drift_note}）"
    saga = state.get("saga_result") or {}
    if saga.get("status") == "rolled_back":
        final_text += "（本次跨域变更已全部回滚，Saga 补偿成功，无残留状态。）"
    elif saga.get("status") == "escalated":
        final_text += "（⚠️ 部分变更未能自动回滚，已升级人工处理，请留意坐席接入。）"
    elif saga.get("status") == "escalation_failed":
        final_text += "（⚠️ 变更回滚失败且升级人工未成功，请立即联系管理员处理。）"
    s = get_settings()
    model = s.llm_main_model if s.llm_provider != "fake" else "fake"
    audit_from_config(
        config, "arbitrate.synthesize", target=state.get("intent", ""),
        params={"decision_log": arb_log, "domains": sorted(artifacts)},
        result_summary=final_text[:200],
        context_id=state.get("context_id", ""),
        trace_id=state.get("trace_id", ""),
    )
    syn_step = {
        "stage": "synthesize", "intent": state.get("intent", ""),
        "model": model, "prompt_version": SYNTHESIZE_VERSION,
        "output_summary": final_text[:200],
    }
    return {"final_text": final_text, "prompt_version": SYNTHESIZE_VERSION,
            "model": model,
            # M4（PRD 10.4）：滚动摘要随 artifact 回写渠道层（console 落库）
            "session_summary": _maybe_session_summary(state, config),
            "decision_log": arb_log + [syn_step]}


def deliver(state: OrchestratorState, config: RunnableConfig) -> dict:
    """交付前双校验（风险审计 R4：校验不通过必须阻断，不能只 warning）。

    规则：所有在场 Artifact 必须带 authority_domain；error 占位除外（那本身是
    降级标记）。任一产物缺权威标注 → 整体降级为拒答，不把无来源的数字给客户。
    """
    artifacts = state.get("artifacts") or {}
    missing = []
    for d, a in artifacts.items():
        if not isinstance(a, dict):
            missing.append(d)
            continue
        data = a.get("data")
        # data 可能是 dict 也可能是 list（如退款单列表、商品候选列表）
        if isinstance(data, dict) and data.get("error"):
            continue  # 降级占位，合成时已标注
        if not a.get("authority_domain"):
            missing.append(d)
    if missing:
        logger.error("产物缺少权威域标注，阻断交付：%s", missing)
        text = (
            "抱歉，本次查询结果的来源校验未通过，我不能直接给出结论。"
            "请换个问法，或回复「转人工」由人工客服为您核实。"
        )
        audit_from_config(
            config, "deliver.blocked", target=state.get("intent", ""),
            params={"missing": missing}, result_summary=text,
            context_id=state.get("context_id", ""),
            trace_id=state.get("trace_id", ""),
        )
        return {
            "final_text": text,
            "decision_log": [{
                "stage": "deliver", "event": "blocked",
                "reason": "missing_authority", "domains": missing,
            }],
        }
    audit_from_config(
        config, "deliver.answer", target=state.get("intent", ""),
        params={"domains": sorted(artifacts), "pii_masked": state.get("pii_masked", 0)},
        result_summary=state.get("final_text", "")[:300],
        context_id=state.get("context_id", ""),
        trace_id=state.get("trace_id", ""),
    )
    return {}


def nomatch(state: OrchestratorState) -> dict:
    return {
        "final_text": (
            "这个需求超出了我目前能处理的范围。当前开放：商品查询/对比/到手价、"
            "售后资格与责任判定、维修工单、政策条款、订单与物流查询（试运行）；"
            "商家服务端另开放投流分析、选品、竞品价格监控与爆款发现。"
            "您也可以随时回复「转人工」找人工客服。"
        ),
        "hit_wall_count": state.get("hit_wall_count", 0) + 1,
        "decision_log": [{"stage": "route", "event": "nomatch",
                          "intent": state.get("intent")}],
    }


def after_nomatch(state: OrchestratorState) -> Literal["handoff", "deliver"]:
    """本次拒答使碰壁累计达 2 → 当场转人工（PRD 7.12 触发条件 2），不再输出第二次拒答。"""
    return "handoff" if state.get("hit_wall_count", 0) >= 2 else "deliver"


async def handoff(state: OrchestratorState, config: RunnableConfig) -> dict:
    """转人工：结构化交接包 → A2A 委托人工坐席 Agent（handoff.accept）。

    交接包不是聊天记录（PRD 7.12(2)）：contextId、trace_id、域链 Artifact、
    决策日志、AI 草案与触发原因；坐席靠 contextId 拉历史。
    """
    from a2a.types import TaskState

    from shared.a2a_client import delegate

    reasons = []
    if state["intent"] == "request_human":
        reasons.append("用户明确要求转人工")
    if state.get("risk_flag"):
        reasons.append("高风险话术（纠纷/投诉类）")
    if state.get("hit_wall_count", 0) >= 2:
        reasons.append(f"反复碰壁（{state.get('hit_wall_count', 0)} 次）")
    # M4（PRD 10.4）：交接包历史优先用渠道层续传的真实近 N 轮——
    # state["messages"] 每 Task 只含当轮，旧写法实际只带 1 轮
    turns = (state.get("history") or {}).get("recent_turns") or []
    if turns:
        history = [str(t.get("text", "")) for t in turns if str(t.get("text", "")).strip()]
    else:
        history = [
            str(m.content) for m in state.get("messages", []) if str(m.content).strip()
        ][-6:]

    package = {
        "context_id": state.get("context_id") or state["trace_id"],
        "trace_id": state["trace_id"],
        "customer_id": state.get("customer_id") or "",
        "reasons": reasons or ["达到转人工条件"],
        "domain_artifacts": state.get("artifacts") or {},
        "decision_log": state.get("decision_log", []),
        "ai_draft": state.get("final_text") or "",
        "user_text": state["user_text"],
        "history": history,
    }
    meta = {
        "trace_id": state["trace_id"],
        "context_id": package["context_id"],
        "customer_id": package["customer_id"],
        "tenant_id": state.get("tenant_id", ""),
        "actor_id": state.get("actor_id", ""),
    }
    result = await delegate(
        domain_base_url("humandesk"),
        json.dumps(package, ensure_ascii=False),
        metadata=meta,
        auth=auth_from_config(config),
    )
    if result.state == TaskState.TASK_STATE_COMPLETED:
        final_text = "已为您转接人工客服，坐席正在接入，请稍候。"
    else:
        final_text = "转人工暂时排队失败，请稍后重试，或拨打服务热线 400-000-0000。"
    return {
        "handoff_done": result.state == TaskState.TASK_STATE_COMPLETED,
        "final_text": final_text,
        "decision_log": [{
            "stage": "handoff", "task_id": result.task_id,
            "state": str(result.state), "reasons": reasons,
        }],
    }


def smalltalk(state: OrchestratorState) -> dict:
    return {"final_text": SMALLTALK_REPLIES.get(state["intent"], "你好。")}


# ---------------------------------------------------------------------------
# 编译
# ---------------------------------------------------------------------------

def build_orchestrator_graph(checkpointer=None):  # noqa: ANN001
    g = StateGraph(OrchestratorState)
    g.add_node("intake", intake)
    g.add_node("understand", understand)
    g.add_node("route", route)
    g.add_node("advance", advance)
    g.add_node("domain_call", domain_call)
    g.add_node("saga_compensate", saga_compensate)
    g.add_node("ask_user", ask_user)
    g.add_node("handoff", handoff)
    g.add_node("synthesize", synthesize)
    g.add_node("deliver", deliver)
    g.add_node("nomatch", nomatch)
    g.add_node("smalltalk", smalltalk)

    g.add_edge(START, "intake")
    g.add_edge("intake", "understand")
    g.add_edge("understand", "route")
    g.add_conditional_edges(
        "route", after_route,
        {"advance": "advance", "nomatch": "nomatch", "smalltalk": "smalltalk",
         "handoff": "handoff"},
    )
    # domain_call（每域一实例，由 advance 经 Send 扇出）完成：有域追问 → ask_user
    # 上抛回填；否则回 advance 汇聚（还有未完成域则再扇出，收齐 → synthesize）
    g.add_edge("saga_compensate", "synthesize")
    g.add_conditional_edges(
        "domain_call", after_domain_call,
        {"ask_user": "ask_user", "advance": "advance"},
    )
    g.add_conditional_edges(
        "nomatch", after_nomatch,
        {"handoff": "handoff", "deliver": "deliver"},
    )
    # 域追问上抛 interrupt；回填后回 advance（按 pending_task 续跑该域）
    g.add_edge("ask_user", "advance")
    g.add_edge("handoff", "deliver")
    g.add_edge("synthesize", "deliver")
    g.add_edge("smalltalk", "deliver")
    g.add_edge("deliver", END)
    return g.compile(checkpointer=checkpointer)


def orchestrator_artifact_builder(state: dict) -> tuple[dict, str]:
    data = {
        "type": "final_answer",
        "intent": state.get("intent", ""),
        "final_text": state.get("final_text", ""),
        "artifacts": state.get("artifacts") or {},
        # 裁决/否决/Saga/步骤全程留痕（PRD 7.6 对账与追责要求）
        "decision_log": state.get("decision_log", []),
        "handoff": state.get("handoff_done", False),
        # 渠道层（console）据此跨 Task 续计碰壁
        "hit_wall_count": state.get("hit_wall_count", 0),
        # M4（PRD 10.4）：滚动摘要回写渠道层落库（空 dict = 本轮无需更新）
        "session_summary": state.get("session_summary") or {},
        "prompt_version": state.get("prompt_version", ""),
        "model": state.get("model", ""),
    }
    return data, state.get("final_text") or json.dumps(data, ensure_ascii=False)
