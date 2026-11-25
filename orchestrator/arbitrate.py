"""编排层纯函数：权威源裁决（PRD 7.6）+ 上游否决判定 + 快照漂移检查（7.8）。

全部是纯函数：输入 state/artifacts，输出判定结果——可单测、可回放，
禁止 LLM 参与（PRD 7.6：LLM 只在裁决完成后负责"怎么把结论说人话"）。
"""

from __future__ import annotations

from datetime import datetime


# ---------------------------------------------------------------------------
# 权威源矩阵（PRD 3.4）的字段级投影：字段名 → 唯一权威域。
# 「唯一写者原则」：非权威域的 Artifact 携带这些字段时，裁决器丢弃并记录。
# ---------------------------------------------------------------------------
FIELD_AUTHORITY: dict[str, str] = {
    # 财务权威（PRD 3.4：退款金额/到账时间以退款单为准，任何域不得推测）
    "refund_amount": "finance",
    "arrival_time": "finance",
    # 履约权威
    "atp": "fulfillment",
    "tracking_no": "fulfillment",
    # 订单权威（成交价）
    "deal_amount": "order",
    # 售后权威（资格结论）
    "eligible": "aftersale",
    # 注意：售后域的 clause 字段是"条款引用"（AC-31 要求 Artifact 引用
    # clause_id+版本+生效期），不是条款内容声明，不进裁决范围。
}

# 快照漂移阈值（PRD 7.8：max(as_of) - min(as_of) ≤ 5 分钟）
SNAPSHOT_DRIFT_LIMIT_SECONDS = 300

# 语义放行：(intent, domain, error) —— 该 error 不是故障而是业务状态信号，
# 含义恰好是"放行下一跳"。改地址链：履约查无运单 = 订单未发货 = 可改。
SEMANTIC_PASS: set[tuple[str, str, str]] = {
    ("order_amend", "fulfillment", "no_shipment"),
}


def arbitrate_fields(artifacts: dict) -> tuple[dict, list[dict]]:
    """按字段权威矩阵清洗各域产物（PRD 7.6：裁决是查表，不是投票）。

    返回 (清洗后的 artifacts, decision_log 条目)。非权威域携带权威字段时：
    丢弃该字段、保留其余数据，并把被丢弃的候选值写进 decision_log（对账/追责）。
    """
    cleaned: dict = {}
    log: list[dict] = []
    for domain, a in (artifacts or {}).items():
        if not isinstance(a, dict):
            cleaned[domain] = a
            continue
        data = a.get("data")
        if not isinstance(data, dict):
            cleaned[domain] = a
            continue
        dropped = {}
        for field, authority in FIELD_AUTHORITY.items():
            if field in data and domain != authority:
                dropped[field] = data.pop(field)
        if dropped:
            log.append({
                "stage": "arbitrate", "domain": domain,
                "event": "dropped_non_authority_fields",
                "authority": {f: FIELD_AUTHORITY[f] for f in dropped},
                "dropped": dropped,  # 被丢弃的候选值留档（7.6 对账要求）
            })
        cleaned[domain] = {**a, "data": data}
    if not log:
        log.append({"stage": "arbitrate", "event": "no_conflict"})
    return cleaned, log


def veto_reason(intent: str, chain: list[str], artifacts: dict) -> str | None:
    """串行链上游否决判定（PRD 7.9 第 4 步 / 7.8 部分交付第三档）。

    返回 None = 放行下一跳；返回字符串 = 否决原因（下游全部取消）。
    两类否决：业务否决（资格不通过/已发货）与系统失败（上游 error）。
    """
    for i, d in enumerate(chain):
        a = (artifacts or {}).get(d)
        data = a.get("data") if isinstance(a, dict) else None
        if not isinstance(data, dict):
            continue
        # 系统失败：上游任何 error 都否决下游（7.8：资格判定域失败 → 整体拒答）；
        # 语义放行除外（该 error 是"可继续"的业务状态信号）
        if (data.get("error") and i < len(chain) - 1
                and (intent, d, data["error"]) not in SEMANTIC_PASS):
            return f"上游 {d} 失败（{data['error']}）"
        if intent in ("refund_status", "cancel_refund") and d == "aftersale":
            if data.get("eligible") is False:
                return f"售后资格判定不通过（{data.get('conclusion', '')}）"
        if intent == "order_amend" and d == "fulfillment":
            # 已进入物流流程的订单不可改地址；无运单 = 未发货 = 放行
            if data.get("status") in ("collected", "in_transit",
                                      "out_for_delivery", "delivered"):
                return f"订单已发货（{data.get('status_name', data['status'])}），无法修改地址"
    return None


# 事实类型 → 权威域（PRD 7.6 裁决器签名的查表部分）
FACT_AUTHORITY: dict[str, str] = {
    "deal_amount": "finance",   # 实付金额以财务账单为准（3.4：营销到手价不得当成交价）
}


def arbitrate_value(fact_type: str, candidates: list[dict]) -> dict:
    """PRD 7.6 裁决器标准签名：输入 {fact_type, candidates[]}，输出 {value, authority, reason}。

    规则：取权威域值；权威域无数据时不得用非权威域补位（按缺失处理）；
    被丢弃的候选全部留档（对账/追责）。纯函数，可单测、可回放。
    """
    authority = FACT_AUTHORITY.get(fact_type)
    winner = next(
        (c for c in candidates if c.get("domain") == authority
         and c.get("value") is not None),
        None,
    )
    if winner is None:
        return {
            "fact_type": fact_type, "value": None, "authority": authority,
            "reason": f"权威域 {authority} 无数据，不得用非权威域补位（PRD 7.6）",
            "discarded": [],
        }
    return {
        "fact_type": fact_type,
        "value": winner["value"],
        "authority": authority,
        "reason": f"按权威源矩阵，{fact_type} 以 {authority} 域为准",
        "discarded": [c for c in candidates if c is not winner],
    }


def ready_domains(domains: list[str], deps: dict, artifacts: dict) -> tuple[list[str], list[tuple[str, str]]]:
    """分层 DAG 调度的纯函数部分：返回 (本波可发域名, 需取消的 (域名, 原因) 列表)。

    上游全部完成 → 就绪；任一上游失败（error 占位）→ 该域取消（不调用）。
    """
    ready, cancelled = [], []
    for d in domains:
        if d in (artifacts or {}):
            continue
        ups = deps.get(d, [])
        failed = [u for u in ups
                  if isinstance(((artifacts.get(u) or {}).get("data")) or None, dict)
                  and artifacts[u]["data"].get("error")]
        if failed:
            cancelled.append((d, f"上游 {failed[0]} 失败"))
        elif all(u in (artifacts or {}) for u in ups):
            ready.append(d)
    return ready, cancelled


def check_snapshot_drift(artifacts: dict) -> str | None:
    """跨域快照时点一致性（PRD 7.8：不得静默混用不同时点的数据）。

    返回 None = 时点一致；返回字符串 = 面向用户的标注文本。
    """
    stamps: list[tuple[str, str]] = []
    for domain, a in (artifacts or {}).items():
        if isinstance(a, dict) and a.get("as_of"):
            stamps.append((domain, str(a["as_of"])))
    if len(stamps) < 2:
        return None
    parsed: list[tuple[str, datetime]] = []
    for domain, raw in stamps:
        try:
            parsed.append((domain, datetime.fromisoformat(raw.replace("Z", "+00:00"))))
        except ValueError:
            return None  # 解析不了就不妄判（不误报）
    parsed.sort(key=lambda x: x[1])
    drift = (parsed[-1][1] - parsed[0][1]).total_seconds()
    if drift <= SNAPSHOT_DRIFT_LIMIT_SECONDS:
        return None
    lo, hi = parsed[0], parsed[-1]
    return (
        f"数据时点不一致（{lo[0]}域 {lo[1].strftime('%H:%M')} / "
        f"{hi[0]}域 {hi[1].strftime('%H:%M')}），结论可能已变化，请以最新数据为准。"
    )
