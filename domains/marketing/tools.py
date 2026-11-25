"""营销经营分析域工具（toB，商家服务端）。

- ad.report：投放效果（花费/曝光/点击/转化/GMV/ROI/CVR）+ 确定性投放方案
- selection.assist：选品候选（销量增速 × 竞品价差空间打分，不含库存结论——
  库存权威在 fulfillment，备货联动走 hot_restock 编排）
- competitor.watch：竞品价格监控（价差率 + 近 7 天降价提醒）
- hot.rank：爆款发现（近 N 天销量环比增速 top）
- plan.save：把分析方案存为计划单（两阶段确认 + biz_key 幂等；
  真正执行动作仍需人工审批——金额/投放预算操作全部人工）

全部 staff-only：marketing scope 不授予 customer 角色；tenant 强制过滤。
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import date, timedelta

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from shared.audit import audit_from_config
from shared.auth import auth_from_config, ensure_scope
from shared.confirmations import consume_confirmation, create_confirmation
from shared.db import connect_business_db
from shared.tenant import tenant_id

# 方案阈值（确定性规则，可配置化留给后续迭代）
ROI_SCALE_UP = 2.0     # ROI ≥ 2 → 建议加投
ROI_KEEP = 1.0         # 1 ≤ ROI < 2 → 保持观察
PRICE_DROP_ALERT = 0.05  # 竞品降价 ≥ 5% → 提醒


def _rejected(scope: str) -> str:
    return json.dumps({"error": "rejected", "need_scope": scope}, ensure_ascii=False)


def _read_denied(config: dict | None, scope: str = "marketing.read") -> str | None:
    """scope 不足返回错误 JSON；tenant 缺失由 tenant_id() fail-closed 抛出。"""
    if not ensure_scope(config, scope):
        return _rejected(scope)
    return None


def _days_from(query: str, default: int = 14) -> int:
    m = re.search(r"(\d{1,3})\s*天", query or "")
    try:
        n = int(m.group(1)) if m else default
    except ValueError:
        n = default
    return max(1, min(n, 90))


def _roi(spend: float, gmv: float) -> float:
    return round(gmv / spend, 2) if spend > 0 else 0.0


def _plan_action(roi: float) -> str:
    if roi >= ROI_SCALE_UP:
        return "加投：ROI 达标，建议提高预算并扩量"
    if roi >= ROI_KEEP:
        return "保持观察：ROI 边际达标，暂不加投，优化素材后再看"
    return "暂停/换素材：ROI 低于 1，先止损再测新素材"


@tool
def ad_report(query: str, config: RunnableConfig) -> str:
    """投放效果分析（skill: ad.report）。query 可含"近 N 天"或计划号；
    输出分计划花费/曝光/点击/转化/GMV/ROI/CVR 与确定性投放方案。"""
    denied = _read_denied(config)
    if denied:
        return denied
    tenant = tenant_id(config)
    days = _days_from(query)
    today = date.today()
    start = (today - timedelta(days=days - 1)).isoformat()
    campaign_id = ""
    m = re.search(r"CP-\d+", (query or ""), re.IGNORECASE)
    if m:
        campaign_id = m.group(0).upper()
    sql = ("SELECT campaign_id, sku, channel, status, SUM(spend) AS spend, "
           "SUM(impressions) AS impressions, SUM(clicks) AS clicks, "
           "SUM(conversions) AS conversions, SUM(gmv) AS gmv "
           "FROM ad_campaigns WHERE tenant_id=? AND day>=?")
    args: list = [tenant, start]
    if campaign_id:
        sql += " AND campaign_id=?"
        args.append(campaign_id)
    sql += " GROUP BY campaign_id ORDER BY spend DESC"
    with connect_business_db() as conn:
        rows = conn.execute(sql, tuple(args)).fetchall()
    campaigns = []
    for r in rows:
        spend = float(r["spend"] or 0)
        gmv = float(r["gmv"] or 0)
        clicks = int(r["clicks"] or 0)
        conversions = int(r["conversions"] or 0)
        impressions = int(r["impressions"] or 0)
        roi = _roi(spend, gmv)
        campaigns.append({
            "campaign_id": r["campaign_id"], "sku": r["sku"],
            "channel": r["channel"], "status": r["status"],
            "spend": round(spend, 2), "gmv": round(gmv, 2),
            "impressions": impressions, "clicks": clicks,
            "conversions": conversions,
            "ctr": round(clicks / impressions, 4) if impressions else 0,
            "cvr": round(conversions / clicks, 4) if clicks else 0,
            "roi": roi,
            "suggestion": _plan_action(roi),
        })
    total_spend = sum(c["spend"] for c in campaigns)
    total_gmv = sum(c["gmv"] for c in campaigns)
    return json.dumps({
        "days": days, "campaigns": campaigns,
        "total": {"spend": round(total_spend, 2), "gmv": round(total_gmv, 2),
                  "roi": _roi(total_spend, total_gmv)},
        "plan": [
            {"campaign_id": c["campaign_id"], "action": c["suggestion"]}
            for c in campaigns
        ],
    }, ensure_ascii=False)


@tool
def product_selection(query: str, config: RunnableConfig) -> str:
    """选品助手（skill: selection.assist）：销量增速 × 竞品价差空间打分，
    输出候选排名与理由。query 可含商品名/品牌关键词过滤。"""
    denied = _read_denied(config)
    if denied:
        return denied
    tenant = tenant_id(config)
    keyword = (query or "").strip()
    today = date.today()
    win_now = (today - timedelta(days=6)).isoformat()
    win_prev_end = (today - timedelta(days=7)).isoformat()
    win_prev_start = (today - timedelta(days=13)).isoformat()
    with connect_business_db() as conn:
        sales = conn.execute(
            "SELECT sku, "
            "SUM(CASE WHEN day>=? THEN qty ELSE 0 END) AS qty_now, "
            "SUM(CASE WHEN day>=? AND day<=? THEN qty ELSE 0 END) AS qty_prev, "
            "SUM(CASE WHEN day>=? THEN gmv ELSE 0 END) AS gmv_now "
            "FROM sales_daily WHERE tenant_id=? GROUP BY sku",
            (win_now, win_prev_start, win_prev_end, win_now, tenant),
        ).fetchall()
        products = {
            r["sku"]: dict(r) for r in conn.execute(
                "SELECT sku, name, brand, price, stock FROM products "
                "WHERE tenant_id=?", (tenant,))
        }
        gaps: dict[str, float] = {}
        for r in conn.execute(
            "SELECT sku, comp_price, our_price FROM competitor_prices "
            "WHERE tenant_id=? AND captured_at=("
            "SELECT MAX(captured_at) FROM competitor_prices "
            "WHERE tenant_id=? AND sku=competitor_prices.sku)",
            (tenant, tenant),
        ).fetchall():
            ours = float(r["our_price"] or 0)
            comp = float(r["comp_price"] or 0)
            if ours > 0 and comp > 0:
                gaps[r["sku"]] = round((comp - ours) / ours, 4)
    candidates = []
    for s in sales:
        sku = s["sku"]
        prod = products.get(sku) or {}
        if keyword and keyword not in f"{prod.get('name', '')}{prod.get('brand', '')}":
            continue
        qty_now, qty_prev = int(s["qty_now"] or 0), int(s["qty_prev"] or 0)
        growth = round((qty_now - qty_prev) / qty_prev, 4) if qty_prev else (1.0 if qty_now else 0.0)
        gap = gaps.get(sku, 0.0)
        score = round(growth * 0.6 + gap * 0.4, 4)
        reasons = []
        reasons.append(f"近7天销量环比 {growth:+.0%}")
        if gap > 0:
            reasons.append(f"竞品高 {gap:.0%}，有提价/利润空间")
        elif gap < -0.05:
            reasons.append(f"竞品低 {-gap:.0%}，注意价格竞争")
        candidates.append({
            "sku": sku, "name": prod.get("name", ""), "price": prod.get("price"),
            "qty_now": qty_now, "growth": growth, "price_gap": gap,
            "score": score, "reason": "；".join(reasons),
        })
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return json.dumps({"candidates": candidates[:5]}, ensure_ascii=False)


@tool
def competitor_watch(query: str, config: RunnableConfig) -> str:
    """竞品价格监控（skill: competitor.watch）：最新价 vs 我方价、价差率，
    近 7 天降价≥5% 生成提醒。query 可含 sku 过滤。"""
    denied = _read_denied(config)
    if denied:
        return denied
    tenant = tenant_id(config)
    sku_filter = ""
    m = re.search(r"SKU-[A-Z0-9]+", (query or ""), re.IGNORECASE)
    if m:
        sku_filter = m.group(0).upper()
    today = date.today()
    week_ago = (today - timedelta(days=7)).isoformat()
    sql = ("SELECT sku, comp_sku, platform, comp_price, our_price, captured_at "
           "FROM competitor_prices WHERE tenant_id=? AND captured_at>=?")
    args: list = [tenant, week_ago]
    if sku_filter:
        sql += " AND sku=?"
        args.append(sku_filter)
    sql += " ORDER BY sku, captured_at"
    watchlist: dict[str, dict] = {}
    alerts = []
    with connect_business_db() as conn:
        rows = conn.execute(sql, tuple(args)).fetchall()
        product_price = {
            r["sku"]: float(r["price"] or 0) for r in conn.execute(
                "SELECT sku, price FROM products WHERE tenant_id=?", (tenant,))
        }
    for r in rows:
        key = f'{r["sku"]}:{r["comp_sku"]}'
        item = watchlist.setdefault(key, {
            "sku": r["sku"], "comp_sku": r["comp_sku"],
            "platform": r["platform"],
            "prices": [],
        })
        item["prices"].append((r["captured_at"], float(r["comp_price"] or 0)))
    for key, item in watchlist.items():
        prices = [p for _, p in item["prices"]]
        latest = item["prices"][-1]
        comp_price = latest[1]
        our_price = product_price.get(item["sku"], 0.0)
        gap = round((comp_price - our_price) / our_price, 4) if our_price else 0
        item.update({
            "comp_price": comp_price, "our_price": our_price,
            "gap_pct": gap, "captured_at": latest[0],
        })
        del item["prices"]
        first, last = prices[0], prices[-1]
        if first > 0 and (first - last) / first >= PRICE_DROP_ALERT:
            drop = round((first - last) / first, 4)
            item["dropped"] = True
            item["drop_pct"] = drop
            alerts.append({
                "sku": item["sku"], "comp_sku": item["comp_sku"],
                "platform": item["platform"],
                "drop_pct": drop,
                "alert": f"竞品 {item['comp_sku']}（{item['platform']}）近 7 天降价 "
                         f"{drop:.0%}，建议评估是否跟进或突出差异化",
            })
        else:
            item["dropped"] = False
    return json.dumps({
        "watchlist": list(watchlist.values()), "alerts": alerts,
    }, ensure_ascii=False)


@tool
def hot_rank(query: str, config: RunnableConfig) -> str:
    """爆款发现（skill: hot.rank）：近 N 天销量环比增速 top（默认 7 天，top 3）。
    query 可含"近 N 天"。库存/备货结论请走 hot_restock 联动履约域。"""
    denied = _read_denied(config)
    if denied:
        return denied
    tenant = tenant_id(config)
    days = _days_from(query, default=7)
    today = date.today()
    win_now_start = (today - timedelta(days=days - 1)).isoformat()
    prev_end = (today - timedelta(days=days)).isoformat()
    prev_start = (today - timedelta(days=2 * days - 1)).isoformat()
    with connect_business_db() as conn:
        sales = conn.execute(
            "SELECT sku, "
            "SUM(CASE WHEN day>=? THEN qty ELSE 0 END) AS qty_now, "
            "SUM(CASE WHEN day>=? AND day<=? THEN qty ELSE 0 END) AS qty_prev, "
            "SUM(CASE WHEN day>=? THEN gmv ELSE 0 END) AS gmv_now "
            "FROM sales_daily WHERE tenant_id=? GROUP BY sku",
            (win_now_start, prev_start, prev_end, win_now_start, tenant),
        ).fetchall()
        products = {
            r["sku"]: dict(r) for r in conn.execute(
                "SELECT sku, name, brand, price FROM products WHERE tenant_id=?",
                (tenant,))
        }
    top = []
    for s in sales:
        qty_now, qty_prev = int(s["qty_now"] or 0), int(s["qty_prev"] or 0)
        if qty_prev == 0:
            continue
        growth = round((qty_now - qty_prev) / qty_prev, 4)
        prod = products.get(s["sku"]) or {}
        top.append({
            "sku": s["sku"], "name": prod.get("name", ""),
            "qty_now": qty_now, "qty_prev": qty_prev, "growth": growth,
            "gmv_now": round(float(s["gmv_now"] or 0), 2),
        })
    top.sort(key=lambda c: c["growth"], reverse=True)
    top = top[:3]
    return json.dumps({
        "days": days, "top": top,
        # 扁平字段：供编排层 ARBITRATE_INJECTIONS 注入履约域（爆款→备货联动）
        "top_sku": top[0]["sku"] if top else "",
    }, ensure_ascii=False)


@tool
def plan_save(
    kind: str, target: str, config: RunnableConfig,
    note: str = "", confirm_token: str = "",
) -> str:
    """把分析方案存为计划单（skill: plan.save，写操作，两阶段确认）。

    第一次调用返回 pending_confirm + confirm_token（不落库）；
    携带 confirm_token 再次调用才真正写 marketing_plans（status=confirmed）。
    biz_key 幂等：同会话同 kind+target 重复保存只产生一张计划单。
    计划单只是决策记录——预算/投放等实际执行动作仍需人工操作审批。
    """
    if not ensure_scope(config, "marketing.write"):
        return _rejected("marketing.write")
    tenant = tenant_id(config)
    kind = (kind or "").strip() or "general"
    target = (target or "").strip()
    note = (note or "").strip()
    if not target:
        return json.dumps({"error": "need_target"}, ensure_ascii=False)
    auth = auth_from_config(config)
    meta = (config or {}).get("metadata") or {}
    context_id = str(meta.get("context_id") or meta.get("trace_id") or "adhoc")
    cust = str(meta.get("customer_id") or "")
    biz_key = f"{context_id}:{kind}:{target}:plan"
    action_target = f"{kind}:{target}"

    if not confirm_token:
        conf = create_confirmation(
            tenant_id=tenant, actor_id=auth.actor_id if auth else "system",
            customer_id=cust, action="marketing.plan", target=action_target,
            payload={"kind": kind, "target": target, "note": note},
            context_id=str(meta.get("context_id", "")),
            task_id=str(meta.get("task_id", "")),
        )
        audit_from_config(
            config, "tool.plan_save.pending", target=f"plan:{action_target}",
            params={"kind": kind, "target": target},
            result_summary="方案待确认（pending_confirm）",
            context_id=str(meta.get("context_id", "")),
            task_id=str(meta.get("task_id", "")),
        )
        return json.dumps({
            "status": "pending_confirm", "kind": kind, "target": target,
            "note": note, "confirm_token": conf.get("confirm_token", ""),
            "biz_key": biz_key,
            "hint": "方案待人工确认；确认后写入计划单，实际执行仍需人工审批",
        }, ensure_ascii=False)

    conf = consume_confirmation(
        confirm_token, tenant_id=tenant,
        actor_id=auth.actor_id if auth else "system", customer_id=cust,
        action="marketing.plan", target=action_target,
        payload={"kind": kind, "target": target, "note": note},
    )
    if not conf.get("ok"):
        return json.dumps(
            {"error": "invalid_confirm_token", "detail": conf.get("error", "")},
            ensure_ascii=False)
    with connect_business_db() as conn:
        existing = conn.execute(
            "SELECT * FROM marketing_plans WHERE tenant_id=? AND biz_key=?",
            (tenant, biz_key),
        ).fetchone()
        if existing is None:
            plan_id = f"PL-{secrets.token_hex(4).upper()}"
            conn.execute(
                "INSERT INTO marketing_plans(tenant_id, plan_id, kind, target, "
                "note, status, biz_key, created_by) VALUES (?,?,?,?,?,'confirmed',?,?)",
                (tenant, plan_id, kind, target, note, biz_key,
                 auth.actor_id if auth else "system"),
            )
            created = True
        else:
            plan_id = existing["plan_id"]
            created = False
    audit_from_config(
        config, "tool.plan_save", target=f"plan:{plan_id}",
        params={"kind": kind, "target": target, "status": "confirmed"},
        result_summary=(f"计划单 {plan_id} status=confirmed idempotent={not created}"),
        context_id=str(meta.get("context_id", "")),
        task_id=str(meta.get("task_id", "")),
    )
    return json.dumps({
        "plan_id": plan_id, "kind": kind, "target": target,
        "status": "confirmed", "biz_key": biz_key, "idempotent": not created,
        "hint": "计划单已生成；实际执行（改预算/改投放）需人工操作",
    }, ensure_ascii=False)


TOOLS = [ad_report, product_selection, competitor_watch, hot_rank, plan_save]
TOOL_TO_SKILL = {
    "ad_report": "ad.report",
    "product_selection": "selection.assist",
    "competitor_watch": "competitor.watch",
    "hot_rank": "hot.rank",
    "plan_save": "plan.save",
}
