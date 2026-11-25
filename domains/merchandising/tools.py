"""商品与营销域工具（技术方案第 6 章 18 skill 中的 1–4）。

工具 = skill：一个 @tool 对应 Card 里的一个 skill id。
权限前置（8.2）：工具内先过 ensure_scope——M1 scope 来自 run config metadata
（dev 内部信任默认放行），M4 换成 token claim 的精确 scope 校验 + 行级过滤 + 脱敏；
未过直接返回结构化 rejected，不进 LLM 上下文。
"""

import json
import logging
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from shared.auth import ensure_scope
from shared.authz import AuthzError, resolve_customer
from shared.db import business_db_path
from shared.tenant import tenant_id

logger = logging.getLogger(__name__)




def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(business_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _rejected(scope: str, config) -> str:  # noqa: ANN001
    return json.dumps(
        {"error": "rejected", "need_scope": scope}, ensure_ascii=False
    )


@tool
def product_lookup(sku: str, config: RunnableConfig) -> str:
    """查询单个商品的详情（skill: product.lookup）。传入 sku，如 SKU-A001。"""
    if not ensure_scope(config, "merchandising.read"):
        return _rejected("merchandising.read", config)
    tenant = tenant_id(config)
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM products WHERE tenant_id=? AND sku=?", (tenant, sku)
        ).fetchone()
    if row is None:
        return json.dumps({"error": "not_found", "sku": sku}, ensure_ascii=False)
    return json.dumps(dict(row), ensure_ascii=False)


@tool
def product_retrieve(keywords: str, config: RunnableConfig) -> str:
    """按关键词搜索商品候选（skill: product.retrieve）。keywords 为空格分隔的词。"""
    if not ensure_scope(config, "merchandising.read"):
        return _rejected("merchandising.read", config)
    tenant = tenant_id(config)
    terms = [t for t in keywords.split() if t]
    if not terms:
        return json.dumps([], ensure_ascii=False)
    sql = "SELECT * FROM products WHERE tenant_id=? AND (" + " OR ".join(
        ["name LIKE ? OR brand LIKE ? OR model LIKE ?"] * len(terms)
    ) + ")"
    args: list[Any] = [tenant]
    for t in terms:
        args += [f"%{t}%", f"%{t}%", f"%{t}%"]
    with _db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return json.dumps([dict(r) for r in rows], ensure_ascii=False)


@tool
def compare_matrix(skus: str, config: RunnableConfig) -> str:
    """对比多个商品，输出对比矩阵（skill: compare.matrix）。skus 为逗号分隔的 sku。"""
    if not ensure_scope(config, "merchandising.read"):
        return _rejected("merchandising.read", config)
    tenant = tenant_id(config)
    wanted = [s.strip() for s in skus.split(",") if s.strip()]
    if len(wanted) < 2:
        return json.dumps(
            {"error": "need_two_skus", "hint": "对比至少需要 2 个 sku"}, ensure_ascii=False
        )
    with _db() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                f"SELECT * FROM products WHERE tenant_id=? AND sku IN "
                f"({','.join('?' * len(wanted))})",
                [tenant, *wanted],
            ).fetchall()
        ]
    if len(rows) < 2:
        return json.dumps(
            {"error": "not_enough_products", "found": len(rows)}, ensure_ascii=False
        )
    keys = ["sku", "name", "brand", "price", "stock", "status"]
    return json.dumps({"matrix": [{k: r.get(k) for k in keys} for r in rows]}, ensure_ascii=False)


@tool
def promo_evaluate(sku: str, config: RunnableConfig, coupon_ids: str = "") -> str:
    """估算商品到手价（skill: promo.evaluate，结果标注“估算”）。coupon_ids 为逗号分隔，可空。"""
    if not ensure_scope(config, "merchandising.read"):
        return _rejected("merchandising.read", config)
    tenant = tenant_id(config)
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM products WHERE tenant_id=? AND sku=?", (tenant, sku)
        ).fetchone()
        if row is None:
            return json.dumps({"error": "not_found", "sku": sku}, ensure_ascii=False)
        price = float(row["price"])
        promo = conn.execute(
            "SELECT * FROM promos WHERE tenant_id=? AND sku=?", (tenant, sku)
        ).fetchone()
        coupons = conn.execute(
            "SELECT * FROM coupons WHERE tenant_id=?", (tenant,)
        ).fetchall()

        def apply(cur: float, kind: str, value: float) -> float:
            return round(cur * value, 2) if kind == "percent" else round(cur - value, 2)

        applied: list[str] = []
        final = price
        if promo:
            final = apply(final, promo["kind"], float(promo["value"]))
            applied.append(promo["rule"])
        used_coupons = [c for c in coupons if c["sku_scope"] in ("*", sku)]
        for cid in [c.strip() for c in coupon_ids.split(",") if c.strip()]:
            used_coupons = [c for c in used_coupons if c["coupon_id"] == cid] or used_coupons
        for c in used_coupons:
            final = apply(final, c["kind"], float(c["value"]))
            applied.append(c["rule"])
    return json.dumps(
        {
            "sku": sku,
            "list_price": price,
            "estimate_price": max(final, 0.0),
            "applied": applied,
            "estimate": True,  # 到手价标“估算”（技术方案第 6 章）
        },
        ensure_ascii=False,
    )


@tool
def subsidy_check(sku: str, config: RunnableConfig, customer_id: str = "") -> str:
    """国补资格与补贴价（skill: subsidy.check）。

    确定性规则：生效期内 + 类目/SKU 范围命中 + 单价不超门槛 + 每人限购未超；
    多条规则命中取补贴金额更大者。结果是估算（实际以平台/政府资格校验与支付为准）。
    """
    if not ensure_scope(config, "merchandising.read"):
        return _rejected("merchandising.read", config)
    tenant = tenant_id(config)
    s = (sku or "").strip().upper()
    if not s:
        return json.dumps({"error": "need_sku"}, ensure_ascii=False)
    try:
        cust = resolve_customer(config, customer_id)
    except AuthzError as e:
        return e.as_json()
    today = date.today().isoformat()
    with _db() as conn:
        row = conn.execute(
            "SELECT sku, name, price FROM products WHERE tenant_id=? AND sku=?",
            (tenant, s),
        ).fetchone()
        if row is None:
            return json.dumps({"error": "not_found", "sku": s}, ensure_ascii=False)
        price = float(row["price"])
        rules = conn.execute(
            "SELECT * FROM subsidies WHERE tenant_id=? AND status='active' "
            "AND (sku_scope='*' OR sku_scope=?) "
            "AND (effective_from IS NULL OR effective_from='' OR effective_from<=?) "
            "AND (effective_to IS NULL OR effective_to='' OR effective_to>=?) "
            "ORDER BY subsidy_id",
            (tenant, s, today, today),
        ).fetchall()
        # 每人限购：按该客户在此 SKU 上的已购件数（取消单不计）
        bought = conn.execute(
            "SELECT COALESCE(SUM(qty),0) AS q FROM orders "
            "WHERE tenant_id=? AND customer_id=? AND sku=? AND status<>'cancelled'",
            (tenant, cust, s),
        ).fetchone()
    purchased = int(bought["q"] or 0)

    candidates = []
    for r in rules:
        max_price = float(r["max_price"] or 0)
        if max_price and price > max_price:
            continue
        amount = round(min(price * float(r["rate"] or 0), float(r["cap"] or 0)), 2)
        candidates.append({
            "subsidy_id": r["subsidy_id"], "title": r["title"],
            "rate": float(r["rate"] or 0), "cap": float(r["cap"] or 0),
            "max_price": max_price,
            "per_user_limit": int(r["per_user_limit"] or 1),
            "subsidy_amount": amount,
            "final_price": round(max(price - amount, 0.0), 2),
        })

    base = {
        "sku": s, "name": row["name"], "list_price": price,
        "purchased_qty": purchased, "estimate": True,
        "note": "补贴为估算，实际以平台/政府资格校验与支付结果为准",
    }
    if not candidates:
        return json.dumps({
            **base, "eligible": False,
            "reason": "当前无适用国补规则（商品单价可能超过补贴上限）",
        }, ensure_ascii=False)

    best = sorted(candidates, key=lambda c: (-c["subsidy_amount"], c["subsidy_id"]))[0]
    limit = best["per_user_limit"]
    if purchased >= limit:
        return json.dumps({
            **base, "eligible": False, "subsidy_id": best["subsidy_id"],
            "title": best["title"], "rate": best["rate"],
            "subsidy_amount": best["subsidy_amount"],
            "final_price": best["final_price"], "per_user_limit": limit,
            "reason": f"超出每人限购 {limit} 件（该客户已购 {purchased} 件）",
        }, ensure_ascii=False)
    return json.dumps({
        **base, "eligible": True, "subsidy_id": best["subsidy_id"],
        "title": best["title"], "rate": best["rate"],
        "subsidy_amount": best["subsidy_amount"],
        "final_price": best["final_price"], "per_user_limit": limit,
        "candidates": [c["subsidy_id"] for c in candidates],
    }, ensure_ascii=False)


TOOLS = [product_lookup, product_retrieve, compare_matrix, promo_evaluate,
         subsidy_check]
TOOL_TO_SKILL = {
    "product_lookup": "product.lookup",
    "product_retrieve": "product.retrieve",
    "compare_matrix": "compare.matrix",
    "promo_evaluate": "promo.evaluate",
    "subsidy_check": "subsidy.check",
}
