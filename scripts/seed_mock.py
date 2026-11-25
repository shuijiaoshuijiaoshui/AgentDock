"""生成 Mock 数据（PRD 10.1）：SQLite，跨域可关联（预留 order_no 关联列）。

1A 起所有业务表使用 (tenant_id, business_id) 复合主键/唯一键；
t_demo 与 t_other 各自拥有独立的 SO-2001 / SKU-A001 / TK-3001 等业务键。
"""

import os
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.config import get_settings  # noqa: E402

DB = Path(os.environ.get("BUSINESS_DB_PATH", get_settings().business_db_path))

PRODUCTS = [
    # sku, name, brand, model, price, stock, status, lead_time_days
    ("SKU-A001", "无线鼠标 Pro", "Logi", "MX-750", 199.0, 320, "on_sale", 1),
    ("SKU-A002", "机械键盘 K870", "Keychron", "K8-Pro", 499.0, 150, "on_sale", 1),
    ("SKU-A003", "27寸显示器 X27", "Dell", "U2723", 2399.0, 80, "on_sale", 2),
    ("SKU-B001", "降噪耳机 Q35", "Sony", "WH-Q35", 1299.0, 0, "off_shelf", 7),
    ("SKU-B002", "USB-C 扩展坞", "Anker", "A836", 349.0, 210, "on_sale", 1),
]

PROMOS = [
    ("P-1001", "SKU-A001", "direct", 30.0, "直降 30 元"),
    ("P-1002", "SKU-A002", "percent", 0.9, "9 折"),
    ("P-1003", "SKU-A003", "direct", 200.0, "立减 200"),
    ("P-1004", "SKU-B002", "percent", 0.95, "95 折"),
]

COUPONS = [
    ("C-9001", "*", "direct", 20.0, "全场券：满 99 减 20"),
    ("C-9002", "SKU-A001", "percent", 0.95, "单品券：鼠标 95 折"),
]

POLICY_DOCS = [
    ("CLS-001", "v1", "七天无理由退货", "签收后 7 日内可无理由退货，运费由买家承担。",
     "2024-01-01", "2025-06-30", "退货 无理由 7天 七天"),
    ("CLS-001", "v2", "七天无理由退货", "签收后 7 日内可无理由退货，退货商品需保持完好；运费由买家承担，质量问题除外。",
     "2025-07-01", "", "退货 无理由 7天 七天 运费"),
    ("CLS-002", "v1", "整机保修", "自购买日起 12 个月内整机保修，人为损坏不在保修范围。",
     "2024-01-01", "", "保修 维修 质保 一年 12个月"),
    ("CLS-003", "v1", "运费承担规则", "质量问题产生的退换货运费由商家承担；无理由退换货运费由买家承担。",
     "2024-01-01", "", "运费 承担 质量"),
    ("CLS-004", "v1", "发票开具规则", "支持开具增值税专用发票，需购买方具备一般纳税人资质；开票金额以实际成交价为准。",
     "2024-01-01", "", "发票 专票 开票 资质"),
    ("CLS-005", "v1", "退款到账说明", "退款审核通过后 7-15 个工作日内原路退回；具体到账时间以支付渠道为准。",
     "2024-01-01", "", "退款 到账 工作日"),
]

SHIPMENTS = [
    ("SO-2001", "顺丰", "SF1234567890", "delivered", "2026-06-15 18:00:00", "2026-06-17"),
    ("SO-2002", "京东物流", "JD9876543210", "in_transit", "2026-10-21 08:30:00", "2026-10-28"),
    ("SO-2003", "中通", "ZT5566778899", "out_for_delivery", "2026-10-23 07:15:00", "2026-10-26"),
]

SHIPMENT_NODES = [
    ("SO-2001", "2026-06-15 18:00:00", "杭州仓", "已揽收", "collected"),
    ("SO-2001", "2026-06-16 03:20:00", "杭州转运中心", "运输中", "in_transit"),
    ("SO-2001", "2026-06-17 09:10:00", "杭州西湖营业点", "派送中", "out_for_delivery"),
    ("SO-2001", "2026-06-17 14:35:00", "杭州市西湖区", "已签收，签收人：本人", "delivered"),
    ("SO-2002", "2026-10-21 08:30:00", "上海仓", "已揽收", "collected"),
    ("SO-2002", "2026-10-21 19:45:00", "上海转运中心", "运输中", "in_transit"),
    ("SO-2002", "2026-10-24 06:50:00", "苏州转运中心", "运输中", "in_transit"),
    ("SO-2003", "2026-10-23 07:15:00", "广州仓", "已揽收", "collected"),
    ("SO-2003", "2026-10-23 21:30:00", "广州转运中心", "运输中", "in_transit"),
    ("SO-2003", "2026-10-26 08:05:00", "广州天河营业点", "派送中", "out_for_delivery"),
]

TENANTS: dict[str, dict] = {
    "t_demo": {
        "customers": [
            ("CU-1001", "王小明", "金牌", "138****1234", "2023-05-12", "杭州", "SO-2001"),
            ("CU-1002", "李商户", "银牌", "159****5678", "2024-01-20", "上海", "SO-2002"),
            ("CU-1003", "张店铺", "普通", "186****9012", "2024-08-03", "广州", "SO-2003"),
            ("CU-1004", "陈卖家", "金牌", "135****3456", "2022-11-30", "深圳", "SO-2004"),
        ],
        "profiles": [
            ("CU-1001", "DEALER-1001", "金牌", 0.95, 50000, 12000, 30, "2026-11-15", 18,
             "high", "张经理", "low"),
            ("CU-1002", "DEALER-1002", "银牌", 0.97, 30000, 8000, 30, "2026-11-10", 13,
             "normal", "张经理", "low"),
            ("CU-1003", "DEALER-1003", "普通", 1.0, 10000, 0, 15, "2026-11-05", 8,
             "normal", "李经理", "medium"),
            ("CU-1004", "DEALER-1004", "金牌", 0.93, 80000, 15000, 45, "2026-12-01", 34,
             "high", "王经理", "low"),
        ],
        "orders": [
            ("SO-2001", "CU-1001", "SKU-A001", 1, 199.0, "signed",
             "2026-06-15 10:22:00", "2026-06-15 10:23:11", "alipay",
             "杭州市西湖区文三路 100 号", "王小明", "13800001234"),
            ("SO-2002", "CU-1002", "SKU-A002", 1, 499.0, "shipped",
             "2026-10-20 14:05:00", "2026-10-20 14:06:30", "wechat",
             "上海市浦东新区张江路 88 号", "李商户", "15900005678"),
            ("SO-2003", "CU-1003", "SKU-A003", 1, 2399.0, "in_transit",
             "2026-10-22 09:40:00", "2026-10-22 09:41:02", "alipay",
             "广州市天河区体育西路 12 号", "张店铺", "18600009012"),
            ("SO-2004", "CU-1004", "SKU-B002", 2, 698.0, "pending_payment",
             "2026-10-25 20:10:00", "", "",
             "深圳市南山区科技园 3 号", "陈卖家", "13500003456"),
            # 历史购买：用于“超保修期”判定（同客户同 SKU 的最新订单仍是它）
            ("SO-2005", "CU-1004", "SKU-A002", 1, 499.0, "signed",
             "2025-03-02 10:00:00", "2025-03-02 10:01:00", "alipay",
             "深圳市南山区科技园 3 号", "陈卖家", "13500003456"),
            # 高价值退款场景（>500，<2500，单审批人）
            ("SO-2006", "CU-1003", "SKU-A003", 1, 1200.0, "signed",
             "2026-10-10 10:00:00", "2026-10-10 10:01:00", "alipay",
             "广州市天河区体育西路 12 号", "张店铺", "18600009012"),
        ],
        "aftersales": [
            ("AS-1001", "CU-1001", "SKU-A001", "2026-06-15", "按键失灵", "in_warranty", "SO-2001"),
            ("AS-1002", "CU-1002", "SKU-A002", "2025-03-02", "进水", "out_of_warranty", "SO-2002"),
            ("AS-1003", "CU-1003", "SKU-A003", "2026-09-20", "屏幕闪烁", "in_warranty", "SO-2003"),
        ],
        "tickets": [
            ("TK-3001", "CU-1001", "SKU-A001", "按键失灵", "repairing", "SO-2001",
             "REP-001", "AG-001", "warehouse", "high", "agent",
             "2026-10-27 18:00:00"),
            ("TK-3002", "CU-1003", "SKU-A003", "屏幕闪烁", "received", "SO-2003",
             "REP-002", None, "warehouse", "normal", "agent",
             "2026-10-28 12:00:00"),
        ],
        "refunds": [("SO-2003", "seed:SO-2003:refund", 2199.0, "settled")],
        "reconciliations": [
            ("RC-2026-10-1001", "CU-1001", "2026-10", 12500.0, "matched", "issued",
             "2026-11-15"),
            ("RC-2026-10-1002", "CU-1002", "2026-10", 8300.0, "open", "not_issued",
             "2026-11-10"),
        ],
        "complaints": [
            ("CP-5001", "CU-1003", "SO-2003", "物流延迟", "物流超过承诺时效", "open",
             "2026-10-26 10:00:00"),
        ],
        "knowledge": [
            ("KB-1001", "v1", "保修起算口径", "保修期以订单支付时间起算；无订单时以售后记录购买日期为准。",
             "aftersale", "published", "2026-01-01", "", "保修 起算 订单", None, "SUP-001"),
            # 与权威条款 CLS-005（退款 7-15 工作日）冲突 → 检索时 policy_override=true
            ("KB-1002", "v1", "退款到账承诺", "大客户退款 3 个工作日内到账。",
             "finance", "published", "2026-01-01", "", "退款 到账 工作日", "CLS-005", "SUP-001"),
            # 运营知识：合作/入驻属线下流程，非权威条款；实际办理仍需人工
            ("KB-1003", "v1", "异地商家合作与采购流程",
             "异地商家合作流程：①提交营业执照与一般纳税人资质；②区域经理审核经销资格；"
             "③签署年度采购协议；④开通账期与专属报价。具体资质要求与区域政策以渠道经理确认为准。",
             "dealer", "published", "2026-01-01", "",
             "异地 商家 合作 入驻 加盟 采购 经销 流程 资质 购买", None, "SUP-001"),
        ],
    },
    "t_other": {
        "customers": [
            ("CU-2001", "赵经销", "金牌", "137****2222", "2023-08-01", "南京", "SO-2001"),
            ("CU-2002", "钱批发", "银牌", "158****3333", "2024-03-11", "苏州", "SO-2002"),
            ("CU-2003", "孙门店", "普通", "187****4444", "2024-09-09", "无锡", "SO-2003"),
            ("CU-2004", "周企业", "金牌", "136****5555", "2022-12-12", "常州", "SO-2004"),
        ],
        "profiles": [
            ("CU-2001", "DEALER-2001", "金牌", 0.94, 60000, 20000, 30, "2026-11-20", 23,
             "high", "赵经理", "low"),
            ("CU-2002", "DEALER-2002", "银牌", 0.96, 20000, 5000, 30, "2026-11-08", 11,
             "normal", "赵经理", "low"),
            ("CU-2003", "DEALER-2003", "普通", 1.0, 8000, 0, 15, "2026-11-01", 4,
             "normal", "钱经理", "medium"),
            ("CU-2004", "DEALER-2004", "金牌", 0.92, 100000, 30000, 45, "2026-12-05", 38,
             "high", "孙经理", "low"),
        ],
        "orders": [
            ("SO-2001", "CU-2001", "SKU-A001", 1, 299.0, "signed",
             "2026-07-01 09:00:00", "2026-07-01 09:01:00", "bank_transfer",
             "南京市鼓楼区中山路 1 号", "赵经销", "13700002222"),
            ("SO-2002", "CU-2002", "SKU-A002", 1, 599.0, "shipped",
             "2026-10-18 11:00:00", "2026-10-18 11:02:00", "alipay",
             "苏州市工业园区星湖街 2 号", "钱批发", "15800003333"),
            ("SO-2003", "CU-2003", "SKU-A003", 1, 2499.0, "in_transit",
             "2026-10-24 15:00:00", "2026-10-24 15:01:00", "wechat",
             "无锡市滨湖区太湖大道 3 号", "孙门店", "18700004444"),
            ("SO-2004", "CU-2004", "SKU-B002", 2, 798.0, "pending_payment",
             "2026-10-26 08:00:00", "", "",
             "常州市新北区龙城大道 4 号", "周企业", "13600005555"),
            ("SO-2005", "CU-2004", "SKU-A002", 1, 599.0, "signed",
             "2025-04-02 10:00:00", "2025-04-02 10:01:00", "wechat",
             "常州市新北区龙城大道 4 号", "周企业", "13600005555"),
            ("SO-2006", "CU-2003", "SKU-A003", 1, 1300.0, "signed",
             "2026-10-10 10:00:00", "2026-10-10 10:01:00", "wechat",
             "无锡市滨湖区太湖大道 3 号", "孙门店", "18700004444"),
        ],
        "aftersales": [
            ("AS-2001", "CU-2001", "SKU-A001", "2026-07-01", "按键失灵", "in_warranty", "SO-2001"),
            ("AS-2002", "CU-2002", "SKU-A002", "2025-04-02", "进水", "out_of_warranty", "SO-2002"),
            ("AS-2003", "CU-2003", "SKU-A003", "2026-09-25", "屏幕闪烁", "in_warranty", "SO-2003"),
        ],
        "tickets": [
            ("TK-3001", "CU-2001", "SKU-A001", "按键失灵", "repairing", "SO-2001",
             "REP-101", "AG-002", "warehouse", "high", "agent",
             "2026-10-27 18:00:00"),
            ("TK-3002", "CU-2003", "SKU-A003", "屏幕闪烁", "received", "SO-2003",
             "REP-102", None, "warehouse", "normal", "agent",
             "2026-10-28 12:00:00"),
        ],
        "refunds": [("SO-2003", "seed:SO-2003:refund", 2299.0, "settled")],
        "reconciliations": [
            ("RC-2026-10-2001", "CU-2001", "2026-10", 18500.0, "open", "not_issued",
             "2026-11-20"),
        ],
        "complaints": [
            ("CP-5001", "CU-2003", "SO-2003", "发票问题", "专票信息有误", "processing",
             "2026-10-26 11:00:00"),
        ],
        "knowledge": [
            ("KB-2001", "v1", "大客户开票口径", "大客户开票需先完成对账；开票金额以对账单为准。",
             "finance", "published", "2026-01-01", "", "开票 对账 大客户", None, "SUP-002"),
            ("KB-2003", "v1", "异地商家合作与采购流程",
             "异地商家合作流程：①提交营业执照与一般纳税人资质；②区域经理审核经销资格；"
             "③签署年度采购协议；④开通账期与专属报价。具体资质要求与区域政策以渠道经理确认为准。",
             "dealer", "published", "2026-01-01", "",
             "异地 商家 合作 入驻 加盟 采购 经销 流程 资质 购买", None, "SUP-002"),
        ],
    },
}


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS products;
        DROP TABLE IF EXISTS promos;
        DROP TABLE IF EXISTS coupons;
        DROP TABLE IF EXISTS customers;
        DROP TABLE IF EXISTS customer_profiles;
        DROP TABLE IF EXISTS policy_docs;
        DROP TABLE IF EXISTS policy_drafts;
        DROP TABLE IF EXISTS policy_documents;
        DROP TABLE IF EXISTS policy_index_outbox;
        DROP TABLE IF EXISTS knowledge_entries;
        DROP TABLE IF EXISTS aftersales;
        DROP TABLE IF EXISTS tickets;
        DROP TABLE IF EXISTS return_orders;
        DROP TABLE IF EXISTS orders;
        DROP TABLE IF EXISTS order_items;
        DROP TABLE IF EXISTS shipments;
        DROP TABLE IF EXISTS shipment_nodes;
        DROP TABLE IF EXISTS refunds;
        DROP TABLE IF EXISTS invoices;
        DROP TABLE IF EXISTS order_cancel_log;
        DROP TABLE IF EXISTS reconciliations;
        DROP TABLE IF EXISTS complaints;
        DROP TABLE IF EXISTS subsidies;
        DROP TABLE IF EXISTS ad_campaigns;
        DROP TABLE IF EXISTS sales_daily;
        DROP TABLE IF EXISTS competitor_prices;
        DROP TABLE IF EXISTS marketing_plans;

        CREATE TABLE products (
            tenant_id TEXT NOT NULL,
            sku TEXT NOT NULL, name TEXT, brand TEXT, model TEXT,
            price REAL, stock INTEGER, status TEXT,
            lead_time_days INTEGER NOT NULL DEFAULT 1,
            order_no TEXT,
            PRIMARY KEY (tenant_id, sku)
        );
        CREATE TABLE promos (
            tenant_id TEXT NOT NULL,
            promo_id TEXT NOT NULL, sku TEXT, kind TEXT, value REAL, rule TEXT,
            PRIMARY KEY (tenant_id, promo_id)
        );
        CREATE TABLE coupons (
            tenant_id TEXT NOT NULL,
            coupon_id TEXT NOT NULL, sku_scope TEXT, kind TEXT,
            value REAL, rule TEXT,
            PRIMARY KEY (tenant_id, coupon_id)
        );
        CREATE TABLE customers (
            tenant_id TEXT NOT NULL,
            customer_id TEXT NOT NULL, name TEXT, tier TEXT,
            phone TEXT, registered_at TEXT, city TEXT, order_no TEXT,
            PRIMARY KEY (tenant_id, customer_id)
        );
        CREATE TABLE customer_profiles (
            tenant_id TEXT NOT NULL,
            customer_id TEXT NOT NULL,
            customer_code TEXT, dealer_level TEXT, discount REAL,
            credit_limit REAL, credit_used REAL, credit_days INTEGER,
            credit_due_date TEXT, remaining_days INTEGER, priority TEXT,
            account_manager TEXT, risk_level TEXT, updated_at TEXT,
            PRIMARY KEY (tenant_id, customer_id)
        );
        CREATE TABLE policy_docs (
            tenant_id TEXT NOT NULL,
            clause_id TEXT NOT NULL, version TEXT NOT NULL,
            title TEXT, text TEXT,
            effective_from TEXT NOT NULL, effective_to TEXT, keywords TEXT,
            PRIMARY KEY (tenant_id, clause_id, version)
        );
        CREATE TABLE policy_drafts (
            tenant_id TEXT NOT NULL,
            draft_id TEXT NOT NULL,
            clause_id TEXT NOT NULL,
            base_version TEXT,
            title TEXT NOT NULL,
            text TEXT NOT NULL,
            keywords TEXT NOT NULL,
            effective_from TEXT NOT NULL,
            effective_to TEXT,
            status TEXT NOT NULL DEFAULT 'draft',
            author_id TEXT NOT NULL,
            submitted_by TEXT,
            reviewed_by TEXT,
            published_by TEXT,
            review_note TEXT,
            source_doc_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            published_at TEXT,
            PRIMARY KEY (tenant_id, draft_id)
        );
        CREATE INDEX IF NOT EXISTS idx_policy_drafts_clause
            ON policy_drafts(tenant_id, clause_id, status);
        CREATE TABLE policy_documents (
            tenant_id TEXT NOT NULL,
            doc_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            uploaded_by TEXT NOT NULL,
            clause_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, doc_id)
        );
        CREATE TABLE policy_index_outbox (
            tenant_id TEXT NOT NULL,
            clause_id TEXT NOT NULL,
            version TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, clause_id, version)
        );
        CREATE TABLE knowledge_entries (
            tenant_id TEXT NOT NULL,
            entry_id TEXT NOT NULL, version TEXT NOT NULL,
            title TEXT, content TEXT, scope TEXT, status TEXT,
            effective_from TEXT, effective_to TEXT, keywords TEXT,
            conflicts_with TEXT,
            updated_by TEXT, created_at TEXT,
            PRIMARY KEY (tenant_id, entry_id, version)
        );
        CREATE TABLE aftersales (
            tenant_id TEXT NOT NULL,
            aftersale_id TEXT NOT NULL,
            customer_id TEXT NOT NULL,
            sku TEXT, buy_date TEXT, fault_type TEXT, status TEXT, order_no TEXT,
            created_at TEXT,
            PRIMARY KEY (tenant_id, aftersale_id),
            UNIQUE (tenant_id, order_no, sku, fault_type)
        );
        CREATE TABLE tickets (
            tenant_id TEXT NOT NULL,
            ticket_no TEXT NOT NULL, customer_id TEXT, sku TEXT,
            fault_type TEXT, status TEXT, order_no TEXT, biz_key TEXT,
            assignee_id TEXT, department TEXT, priority TEXT, source TEXT,
            sla_due TEXT, description TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT,
            PRIMARY KEY (tenant_id, ticket_no)
        );
        CREATE UNIQUE INDEX idx_tickets_biz_key
            ON tickets(tenant_id, biz_key) WHERE biz_key IS NOT NULL;
        CREATE TABLE orders (
            tenant_id TEXT NOT NULL,
            order_no TEXT NOT NULL, customer_id TEXT, sku TEXT,
            qty INTEGER, amount REAL, status TEXT,
            created_at TEXT, paid_at TEXT, pay_channel TEXT, address TEXT,
            receiver TEXT, phone TEXT,
            PRIMARY KEY (tenant_id, order_no)
        );
        CREATE INDEX idx_orders_customer ON orders(tenant_id, customer_id);
        CREATE TABLE order_items (
            tenant_id TEXT NOT NULL,
            order_no TEXT NOT NULL, sku TEXT NOT NULL,
            qty INTEGER, unit_price REAL, amount REAL,
            PRIMARY KEY (tenant_id, order_no, sku)
        );
        CREATE TABLE shipments (
            tenant_id TEXT NOT NULL,
            order_no TEXT NOT NULL, carrier TEXT, tracking_no TEXT,
            status TEXT, shipped_at TEXT, estimated_delivery TEXT,
            PRIMARY KEY (tenant_id, order_no)
        );
        CREATE TABLE shipment_nodes (
            tenant_id TEXT NOT NULL,
            node_id TEXT NOT NULL, order_no TEXT, node_time TEXT,
            location TEXT, description TEXT, status TEXT,
            PRIMARY KEY (tenant_id, node_id),
            UNIQUE (tenant_id, order_no, node_time, description)
        );
        CREATE TABLE refunds (
            tenant_id TEXT NOT NULL,
            refund_id TEXT NOT NULL, order_no TEXT, biz_key TEXT,
            amount REAL, status TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (tenant_id, refund_id)
        );
        CREATE UNIQUE INDEX idx_refunds_biz_key
            ON refunds(tenant_id, biz_key) WHERE biz_key IS NOT NULL;
        CREATE UNIQUE INDEX idx_refunds_order_settled
            ON refunds(tenant_id, order_no) WHERE status = 'settled';
        CREATE TABLE return_orders (
            tenant_id TEXT NOT NULL,
            return_id TEXT NOT NULL, order_no TEXT, customer_id TEXT,
            reason TEXT, status TEXT NOT NULL DEFAULT 'pending_confirm',
            biz_key TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (tenant_id, return_id),
            UNIQUE (tenant_id, biz_key)
        );
        CREATE TABLE invoices (
            tenant_id TEXT NOT NULL,
            invoice_id TEXT NOT NULL, order_no TEXT, biz_key TEXT,
            title TEXT, status TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (tenant_id, invoice_id)
        );
        CREATE UNIQUE INDEX idx_invoices_biz_key
            ON invoices(tenant_id, biz_key) WHERE biz_key IS NOT NULL;
        CREATE UNIQUE INDEX idx_invoices_order_issued
            ON invoices(tenant_id, order_no) WHERE status = 'issued';
        CREATE TABLE order_cancel_log (
            tenant_id TEXT NOT NULL,
            log_id TEXT NOT NULL, order_no TEXT, biz_key TEXT,
            prev_status TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (tenant_id, log_id)
        );
        CREATE UNIQUE INDEX idx_cancel_log_biz_key
            ON order_cancel_log(tenant_id, biz_key) WHERE biz_key IS NOT NULL;
        CREATE TABLE reconciliations (
            tenant_id TEXT NOT NULL,
            reconciliation_id TEXT NOT NULL, customer_id TEXT,
            period TEXT, amount REAL, status TEXT, invoice_status TEXT,
            due_date TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (tenant_id, reconciliation_id),
            UNIQUE (tenant_id, customer_id, period)
        );
        CREATE TABLE complaints (
            tenant_id TEXT NOT NULL,
            complaint_id TEXT NOT NULL, customer_id TEXT, order_no TEXT,
            type TEXT, content TEXT, status TEXT, created_at TEXT,
            PRIMARY KEY (tenant_id, complaint_id)
        );

        -- 国补/活动补贴规则（商品域：价格差异的唯一来源之一）
        CREATE TABLE subsidies (
            tenant_id TEXT NOT NULL,
            subsidy_id TEXT NOT NULL,
            sku_scope TEXT NOT NULL,      -- '*' 或具体 SKU
            title TEXT,
            rate REAL,                     -- 补贴比例（0.15 = 15%）
            cap REAL,                      -- 单件封顶金额
            max_price REAL,                -- 参与门槛：商品单价不超过
            per_user_limit INTEGER NOT NULL DEFAULT 1,
            effective_from TEXT, effective_to TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            PRIMARY KEY (tenant_id, subsidy_id)
        );

        -- 营销经营分析（marketing 域）：投放/销量/竞品价格/方案单
        CREATE TABLE ad_campaigns (
            tenant_id TEXT NOT NULL,
            campaign_id TEXT NOT NULL, day TEXT NOT NULL,
            sku TEXT, channel TEXT, status TEXT,
            spend REAL, impressions INTEGER, clicks INTEGER,
            conversions INTEGER, gmv REAL,
            PRIMARY KEY (tenant_id, campaign_id, day)
        );
        CREATE TABLE sales_daily (
            tenant_id TEXT NOT NULL,
            sku TEXT NOT NULL, day TEXT NOT NULL,
            qty INTEGER, gmv REAL, views INTEGER,
            PRIMARY KEY (tenant_id, sku, day)
        );
        CREATE TABLE competitor_prices (
            tenant_id TEXT NOT NULL,
            comp_id TEXT NOT NULL, captured_at TEXT NOT NULL,
            sku TEXT, comp_sku TEXT, platform TEXT,
            comp_price REAL, our_price REAL,
            PRIMARY KEY (tenant_id, comp_id, captured_at)
        );
        CREATE TABLE marketing_plans (
            tenant_id TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            kind TEXT, target TEXT, note TEXT, status TEXT,
            biz_key TEXT, created_by TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (tenant_id, plan_id)
        );
        CREATE UNIQUE INDEX idx_marketing_plans_biz_key
            ON marketing_plans(tenant_id, biz_key) WHERE biz_key IS NOT NULL;
        """
    )


def _seed_tenant(conn: sqlite3.Connection, tenant: str, data: dict) -> None:
    conn.executemany(
        "INSERT INTO products(tenant_id, sku, name, brand, model, price, stock, "
        "status, lead_time_days) VALUES (?,?,?,?,?,?,?,?,?)",
        [(tenant, *p) for p in PRODUCTS],
    )
    conn.executemany(
        "INSERT INTO promos(tenant_id, promo_id, sku, kind, value, rule) "
        "VALUES (?,?,?,?,?,?)",
        [(tenant, *p) for p in PROMOS],
    )
    conn.executemany(
        "INSERT INTO coupons(tenant_id, coupon_id, sku_scope, kind, value, rule) "
        "VALUES (?,?,?,?,?,?)",
        [(tenant, *c) for c in COUPONS],
    )
    conn.executemany(
        "INSERT INTO customers(tenant_id, customer_id, name, tier, phone, "
        "registered_at, city, order_no) VALUES (?,?,?,?,?,?,?,?)",
        [(tenant, *c) for c in data["customers"]],
    )
    conn.executemany(
        "INSERT INTO customer_profiles(tenant_id, customer_id, customer_code, "
        "dealer_level, discount, credit_limit, credit_used, credit_days, "
        "credit_due_date, remaining_days, priority, account_manager, risk_level, "
        "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
        [(tenant, *p) for p in data["profiles"]],
    )
    conn.executemany(
        "INSERT INTO policy_docs(tenant_id, clause_id, version, title, text, "
        "effective_from, effective_to, keywords) VALUES (?,?,?,?,?,?,?,?)",
        [(tenant, *p) for p in POLICY_DOCS],
    )
    conn.executemany(
        "INSERT INTO knowledge_entries(tenant_id, entry_id, version, title, content, "
        "scope, status, effective_from, effective_to, keywords, conflicts_with, "
        "updated_by, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
        [(tenant, *k) for k in data["knowledge"]],
    )
    conn.executemany(
        "INSERT INTO aftersales(tenant_id, aftersale_id, customer_id, sku, buy_date, "
        "fault_type, status, order_no, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,datetime('now'))",
        [(tenant, *a) for a in data["aftersales"]],
    )
    conn.executemany(
        "INSERT INTO tickets(tenant_id, ticket_no, customer_id, sku, fault_type, "
        "status, order_no, biz_key, assignee_id, department, priority, source, "
        "sla_due) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(tenant, *t) for t in data["tickets"]],
    )
    conn.executemany(
        "INSERT INTO orders(tenant_id, order_no, customer_id, sku, qty, amount, "
        "status, created_at, paid_at, pay_channel, address, receiver, phone) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(tenant, *o) for o in data["orders"]],
    )
    conn.executemany(
        "INSERT INTO order_items(tenant_id, order_no, sku, qty, unit_price, amount) "
        "VALUES (?,?,?,?,?,?)",
        [(tenant, o[0], o[2], o[3], o[4] / max(int(o[3] or 1), 1), o[4])
         for o in data["orders"]],
    )
    conn.executemany(
        "INSERT INTO shipments(tenant_id, order_no, carrier, tracking_no, status, "
        "shipped_at, estimated_delivery) VALUES (?,?,?,?,?,?,?)",
        [(tenant, *s) for s in SHIPMENTS],
    )
    conn.executemany(
        "INSERT INTO shipment_nodes(tenant_id, node_id, order_no, node_time, location, "
        "description, status) VALUES (?,?,?,?,?,?,?)",
        [(tenant, f"{tenant}-N{idx:03d}", *n)
         for idx, n in enumerate(SHIPMENT_NODES, start=1)],
    )
    conn.executemany(
        "INSERT INTO refunds(tenant_id, refund_id, order_no, biz_key, amount, status) "
        "VALUES (?,?,?,?,?,?)",
        [(tenant, f"RF-{tenant}-{idx:03d}", *r)
         for idx, r in enumerate(data["refunds"], start=1)],
    )
    conn.executemany(
        "INSERT INTO reconciliations(tenant_id, reconciliation_id, customer_id, period, "
        "amount, status, invoice_status, due_date) VALUES (?,?,?,?,?,?,?,?)",
        [(tenant, *r) for r in data["reconciliations"]],
    )
    conn.executemany(
        "INSERT INTO complaints(tenant_id, complaint_id, customer_id, order_no, type, "
        "content, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
        [(tenant, *c) for c in data["complaints"]],
    )
    _seed_marketing(conn, tenant)
    _seed_subsidies(conn, tenant)


def _seed_subsidies(conn: sqlite3.Connection, tenant: str) -> None:
    """国补规则剧本：
    - SUB-01 数码国补（全 SKU，15% 封顶 2000，单价≤6000，每人限 1 件）
    - SUB-02 家电以旧换新（SKU-A003 专享，20% 封顶 2000，单价≤3000）→ 与 SUB-01 取更优
    - SUB-03 过期规则（SKU-A002）→ 必须被生效期过滤掉，绝不返回
    """
    conn.executemany(
        "INSERT INTO subsidies(tenant_id, subsidy_id, sku_scope, title, rate, "
        "cap, max_price, per_user_limit, effective_from, effective_to, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            (tenant, "SUB-01", "*", "数码产品国补", 0.15, 2000.0, 6000.0, 1,
             "2026-01-01", "", "active"),
            (tenant, "SUB-02", "SKU-A003", "家电以旧换新补贴", 0.20, 2000.0,
             3000.0, 1, "2026-01-01", "", "active"),
            (tenant, "SUB-03", "SKU-A002", "开年数码补贴（已结束）", 0.25,
             1000.0, 1000.0, 1, "2025-01-01", "2025-12-31", "active"),
        ],
    )


def _seed_marketing(conn: sqlite3.Connection, tenant: str) -> None:
    """营销分析域 mock：投放/销量/竞品价格（日期相对种子日，保证近 N 天窗口命中）。

    剧本：
    - CP-01 高 ROI（≈4.0 → 建议加投）、CP-02 边际（≈1.4 → 保持观察）、
      CP-03 亏损（≈0.87 → 暂停/换素材）；
    - SKU-A003 销量指数增长（爆款场景），SKU-B001 线性下滑，其余平稳；
    - MP-02 竞品近 7 天降价 ≈9.9%（触发降价提醒），其余平稳。
    """
    price = {p[0]: p[4] for p in PRODUCTS}
    today = date.today()

    def day(offset: int) -> str:
        return (today - timedelta(days=offset)).isoformat()

    campaigns = [
        # campaign_id, sku, channel, spend, impressions, clicks, conv, gmv
        ("CP-01", "SKU-A001", "直通车", 650, 5000, 150, 13, 2587),
        ("CP-02", "SKU-B002", "引力魔方", 1500, 9000, 270, 6, 2094),
        ("CP-03", "SKU-B001", "品销宝", 1500, 12000, 180, 1, 1299),
    ]
    ad_rows = []
    for offset in range(14):
        for cid, sku, ch, spend, impr, clicks, conv, gmv in campaigns:
            wobble = (offset % 3) - 1  # -1/0/1 轻微波动，保持确定性
            ad_rows.append((
                tenant, cid, day(offset), sku, ch, "enabled",
                spend + wobble * 30, impr + wobble * 100,
                max(clicks + wobble * 5, 1), max(conv + wobble, 0),
                gmv + wobble * 90,
            ))
    conn.executemany(
        "INSERT INTO ad_campaigns(tenant_id, campaign_id, day, sku, channel, "
        "status, spend, impressions, clicks, conversions, gmv) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ad_rows,
    )

    sales_rows = []
    for offset in range(30):
        i = 29 - offset  # 越新越大（指数增长曲线按自然日递增）
        plan = {
            "SKU-A001": 6 + (i % 2),
            "SKU-A002": 4,
            "SKU-A003": 1 + round(i * 0.25),
            "SKU-B001": max(9 - round(i * 0.15), 4),
            "SKU-B002": 7,
        }
        for sku, qty in plan.items():
            qty = max(qty, 0)
            sales_rows.append(
                (tenant, sku, day(offset), qty, round(qty * price[sku], 2),
                 qty * 40 + 60))
    conn.executemany(
        "INSERT INTO sales_daily(tenant_id, sku, day, qty, gmv, views) "
        "VALUES (?,?,?,?,?,?)",
        sales_rows,
    )

    comp_plans = [
        # comp_id, sku, comp_sku, platform, prices_by_offset(每隔2天快照)
        ("MP-01", "SKU-A001", "COMP-9001", "拼多多",
         {13: 219, 11: 219, 9: 219, 7: 219, 5: 219, 3: 219, 1: 219}),
        ("MP-02", "SKU-B001", "COMP-9002", "天猫",
         {13: 1499, 11: 1499, 9: 1499, 7: 1499, 5: 1499, 3: 1350, 1: 1350}),
        ("MP-03", "SKU-B002", "COMP-9003", "京东",
         {13: 339, 11: 339, 9: 339, 7: 339, 5: 339, 3: 339, 1: 339}),
        ("MP-04", "SKU-A003", "COMP-9004", "天猫",
         {13: 2599, 11: 2599, 9: 2599, 7: 2599, 5: 2599, 3: 2599, 1: 2599}),
        ("MP-05", "SKU-A002", "COMP-9005", "京东",
         {13: 509, 11: 509, 9: 509, 7: 509, 5: 509, 3: 509, 1: 509}),
    ]
    comp_rows = []
    for comp_id, sku, comp_sku, platform, prices in comp_plans:
        for offset, comp_price in prices.items():
            comp_rows.append((
                tenant, comp_id, day(offset), sku, comp_sku, platform,
                comp_price, price[sku],
            ))
    conn.executemany(
        "INSERT INTO competitor_prices(tenant_id, comp_id, captured_at, sku, "
        "comp_sku, platform, comp_price, our_price) VALUES (?,?,?,?,?,?,?,?)",
        comp_rows,
    )
    # marketing_plans：写入目标，种子不落数据


def seed() -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB)
    with conn:
        _create_schema(conn)
        for tenant, data in TENANTS.items():
            _seed_tenant(conn, tenant, data)
    total_orders = sum(len(d["orders"]) for d in TENANTS.values())
    total_tickets = sum(len(d["tickets"]) for d in TENANTS.values())
    print(
        f"mock 数据已生成：{DB}（{len(TENANTS)} 租户 / "
        f"{len(PRODUCTS) * len(TENANTS)} 商品 / {total_orders} 订单 / "
        f"{total_tickets} 工单 / {len(POLICY_DOCS) * len(TENANTS)} 条款）"
    )


if __name__ == "__main__":
    seed()
