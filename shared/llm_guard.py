"""进入 LLM 前的字段级脱敏（PII allowlist/denylist）。

原则：本地/审计/工作台可保留原始数据；发给外部模型的请求只允许非敏感字段。
覆盖地址、收件人、手机号、邮箱、身份证、银行卡、客户 ID 等。
"""

from __future__ import annotations

import json
from typing import Any

from shared.pii import mask_pii

# 键名命中即脱敏（大小写不敏感）；address 类字段整段替换
SENSITIVE_KEYS = {
    "address", "old_address", "new_address", "shipping_address",
    "billing_address", "receiver", "receiver_name", "receiver_phone",
    "phone", "mobile", "tel", "contact", "email", "id_card", "id_number",
    "bank_card", "card_no", "customer_id", "customer_code",
}
REDACTED = "[REDACTED]"


def sanitize_text(text: str) -> str:
    """字符串：先尝试 JSON 递归脱敏，否则值级 PII 正则脱敏。"""
    raw = str(text or "")
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return mask_pii(raw)[0]
    return json.dumps(sanitize_for_llm(parsed), ensure_ascii=False, default=str)


def sanitize_for_llm(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).strip().lower() in SENSITIVE_KEYS:
                out[key] = REDACTED
            else:
                out[key] = sanitize_for_llm(item)
        return out
    if isinstance(value, (list, tuple)):
        return [sanitize_for_llm(v) for v in value]
    if isinstance(value, str):
        return mask_pii(value)[0]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return mask_pii(str(value))[0]


def sanitize_messages_for_llm(messages: list) -> list:
    """域内 ReAct 回灌 LLM 前，清洗 ToolMessage 等字符串内容。"""
    out = []
    for message in messages:
        content = getattr(message, "content", None)
        if isinstance(content, str):
            out.append(message.model_copy(update={"content": sanitize_text(content)}))
        else:
            out.append(message)
    return out


def assert_no_pii(payload: Any) -> None:
    """测试辅助：序列化后断言不含常见 PII 形态/敏感键。"""
    raw = json.dumps(payload, ensure_ascii=False, default=str)
    forbidden = ["13800001234", "13812345678", "杭州市西湖区", "CU-1001",
                 "a@b.com", "110101199001011234", "6222021234567890123"]
    for item in forbidden:
        if item in raw:
            raise AssertionError(f"LLM payload 含敏感数据：{item}")
