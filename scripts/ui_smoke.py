"""客户侧 UI 契约冒烟（与浏览器同源 Cookie + CSRF 路径一致）。

用法：
    uv run python scripts/ui_smoke.py
    uv run python scripts/ui_smoke.py --base http://127.0.0.1:8307

覆盖：登录 → 自动问候 → 消息/卡片 → 卡片按钮 → 未知 action 400 →
客户端伪造 intent/slots 被忽略 → 附件上传/下载 → 确认待办 → 转人工 →
历史分页 → 跨租户越权 403/404。退出码非 0 表示失败。
"""

from __future__ import annotations

import argparse
import base64
import sys
from urllib.parse import quote

import httpx

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQ"
    "DwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class Smoke:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.client = httpx.Client(base_url=self.base, trust_env=False, timeout=90)
        self.csrf = ""
        self.cid = ""

    def _headers(self, write: bool = False) -> dict:
        headers = {"X-Surface": "customer"}
        if write:
            headers["X-CSRF-Token"] = self.csrf
        return headers

    def _check(self, cond: bool, step: str) -> None:
        if not cond:
            raise SystemExit(f"[FAIL] {step}")
        print(f"[ok] {step}")

    def login(self, tenant: str = "t_demo", username: str = "customer") -> None:
        r = self.client.post("/api/auth/login", json={
            "tenant_id": tenant, "username": username,
            "password": "demo123", "surface": "customer",
        })
        self._check(r.status_code == 200, f"登录 {tenant}/{username}")
        self.csrf = r.json()["csrf_token"]

    def conversation(self) -> None:
        r = self.client.get("/api/customer/conversation")
        self._check(r.status_code == 200, "进入会话")
        data = r.json()
        self.cid = data["conversation"]["id"]
        name = data["customer"]["display_name"]
        greetings = [m["content"] for m in data["messages"] if m.get("role") == "ai"]
        self._check(any(name in text and "经营助手" in text for text in greetings),
                    "自动问候包含客户名")

    def send(self, text: str) -> dict:
        r = self.client.post("/api/customer/send", headers=self._headers(True),
                             json={"conversation_id": self.cid, "text": text})
        self._check(r.status_code == 200, f"发送：{text}")
        return r.json()

    def latest_ai_message(self, min_id: int = 0) -> dict:
        msgs = self.client.get(
            f"/api/conversations/{self.cid}/messages?after_id={min_id}",
        ).json()
        ai = [m for m in msgs if m.get("role") == "ai"]
        return ai[-1] if ai else {}

    def reset_conversations(self) -> None:
        """冒烟可重复：先关闭该客户所有未关闭会话，保证能生成新的自动问候。"""
        convs = self.client.get("/api/conversations").json()
        for conv in convs:
            if conv.get("state") == "CLOSED":
                continue
            r = self.client.post(
                f"/api/conversations/{conv['id']}/close",
                headers=self._headers(True),
            )
            self._check(r.status_code == 200, f"重置会话 {conv['id'][:8]}")

    def run(self) -> None:
        self.login()
        self.reset_conversations()
        self.conversation()

        sent = self.send("查询订单 SO-2001 的状态")
        self._check("SO-2001" in sent["reply"] and "tenant_id" not in sent["reply"],
                    "订单回复为人话摘要（无内部字段）")
        ai = self.latest_ai_message()
        cards = (ai.get("meta") or {}).get("cards") or []
        order = next((c for c in cards if c["type"] == "order"), None)
        self._check(order is not None, "订单卡片")
        self._check({"order.track", "aftersale.eligibility"}
                    <= {a["action_id"] for a in order["actions"]}, "卡片按钮白名单")

        acted = self.client.post(
            f"/api/conversations/{self.cid}/card-action", headers=self._headers(True),
            json={"action_id": "order.track", "params": {"order_no": "SO-2001"}},
        )
        self._check(acted.status_code == 200, "卡片按钮 order.track")
        self._check(any(w in acted.json()["reply"] for w in ("物流", "承运", "运单")),
                    "卡片按钮返回履约信息")

        # 客户端伪造 intent/slots：服务端只认 action_id 白名单映射
        forged = self.client.post(
            f"/api/conversations/{self.cid}/card-action", headers=self._headers(True),
            json={"action_id": "order.track", "params": {"order_no": "SO-2001"},
                  "intent": "refund_settle", "slots": {"order_no": "SO-2001"}},
        )
        self._check(forged.status_code == 200, "伪造 intent/slots 不报错（被忽略）")
        self._check("退款" not in forged.json()["reply"], "伪造 intent 未生效")

        unknown = self.client.post(
            f"/api/conversations/{self.cid}/card-action", headers=self._headers(True),
            json={"action_id": "evil.do_everything", "params": {}},
        )
        self._check(unknown.status_code == 400
                    and unknown.json()["error"] == "unknown_action",
                    "未注册 action_id → 400")

        up = self.client.post(
            "/api/attachments", headers=self._headers(True),
            files={"file": ("smoke.png", PNG, "image/png")},
            data={"conversation_id": self.cid},
        )
        self._check(up.status_code == 200, "附件上传")
        aid = up.json()["attachment_id"]
        dl = self.client.get(f"/api/attachments/{aid}")
        self._check(dl.status_code == 200 and dl.content == PNG, "附件鉴权下载")

        with_att = self.client.post(
            "/api/customer/send", headers=self._headers(True),
            json={"conversation_id": self.cid,
                  "text": "查询订单 SO-2001 的状态",
                  "attachment_ids": [aid]},
        )
        self._check(with_att.status_code == 200, "附件消息发送")
        msgs = self.client.get(
            f"/api/conversations/{self.cid}/messages?after_id=0").json()
        last_customer = [m for m in msgs if m.get("role") == "customer"][-1]
        attachments = (last_customer.get("meta") or {}).get("attachments") or []
        self._check(any(a.get("attachment_id") == aid for a in attachments),
                    "消息附件元数据")

        invoice = self.send("给订单 SO-2001 开票，抬头演示公司")
        pending = invoice.get("pending_actions") or []
        if pending:
            resumed = self.client.post(
                f"/api/conversations/{self.cid}/actions", headers=self._headers(True),
                json={"action_id": pending[0]["action_id"]},
            )
            self._check(resumed.status_code == 200 and resumed.json().get("ok"),
                        "确认待办 /actions 执行")
        else:
            self._check("发票" in invoice["reply"], "开票（幂等/已完成）")

        hand = self.client.post(
            f"/api/customer/conversations/{self.cid}/handoff",
            headers=self._headers(True),
        )
        self._check(hand.status_code == 200, "一键转人工")
        self._check(hand.json()["state"] == "PENDING_HANDOFF", "转人工状态机")

        page1 = self.client.get("/api/customer/history?limit=2").json()
        self._check(len(page1["conversations"]) == 2 and page1["next_before"],
                    "历史第一页")
        before = quote(page1["next_before"])
        page2 = self.client.get(f"/api/customer/history?limit=2&before={before}").json()
        ids1 = {c["id"] for c in page1["conversations"]}
        ids2 = {c["id"] for c in page2["conversations"]}
        self._check(not (ids1 & ids2), "历史翻页不重复")

        other = httpx.Client(base_url=self.base, trust_env=False, timeout=30)
        login2 = other.post("/api/auth/login", json={
            "tenant_id": "t_other", "username": "customer2",
            "password": "demo123", "surface": "customer",
        })
        self._check(login2.status_code == 200, "第二租户登录")
        peeking = other.get(f"/api/conversations/{self.cid}/messages")
        self._check(peeking.status_code in (403, 404), "跨租户读会话被拒")
        steal = other.get(f"/api/attachments/{aid}")
        self._check(steal.status_code in (403, 404), "跨租户下载附件被拒")
        other.close()

        # 收尾：关闭会话，方便下次冒烟重新生成问候
        closed = self.client.post(f"/api/conversations/{self.cid}/close",
                                  headers=self._headers(True))
        self._check(closed.status_code == 200, "关闭会话（收尾）")
        print("\nUI smoke 全部通过 ✔")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8307")
    args = parser.parse_args()
    try:
        Smoke(args.base).run()
    except httpx.HTTPError as exc:
        print(f"[FAIL] 网络错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
