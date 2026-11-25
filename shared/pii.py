"""PII 检测与脱敏（输入侧 + 审计/工具输出侧）。

原则：
- 入库、进 LLM、进审计之前脱敏；原文不落业务库；
- 只做“识别 + 遮盖”，不做“猜测补全”；
- 覆盖手机号、身份证、银行卡、邮箱四类高频 PII；生产可替换为 NER/规则库。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("id_card", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("bank_card", re.compile(r"(?<!\d)(?:\d[ -]?){16,19}(?!\d)")),
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
)


@dataclass(frozen=True)
class PiiFinding:
    kind: str
    masked: str


def _mask_id(value: str) -> str:
    clean = value.replace(" ", "").replace("-", "")
    if len(clean) <= 10:
        return "*" * len(clean)
    return f"{clean[:6]}{'*' * (len(clean) - 10)}{clean[-4:]}"


def _mask_phone(value: str) -> str:
    return f"{value[:3]}****{value[-4:]}" if len(value) >= 7 else "*" * len(value)


def _mask_email(value: str) -> str:
    name, _, domain = value.partition("@")
    if not domain:
        return "***"
    return f"{name[:1]}***@{domain}"


def mask_pii(text: str) -> tuple[str, list[PiiFinding]]:
    """返回 (脱敏文本, 命中列表)；命中列表只含类型与遮盖后的值。"""
    if not text:
        return text, []
    findings: list[PiiFinding] = []
    masked = text
    for kind, pattern in _PATTERNS:
        def _replace(match: re.Match[str], _kind: str = kind) -> str:
            raw = match.group(0)
            if _kind == "id_card":
                out = _mask_id(raw)
            elif _kind == "phone":
                out = _mask_phone(raw)
            elif _kind == "email":
                out = _mask_email(raw)
            else:
                out = _mask_id(raw)
            findings.append(PiiFinding(_kind, out))
            return out

        masked = pattern.sub(_replace, masked)
    return masked, findings


def mask_obj(value):
    """递归脱敏 dict/list/str（用于工具输出与审计参数）。"""
    if isinstance(value, str):
        return mask_pii(value)[0]
    if isinstance(value, dict):
        return {k: mask_obj(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_obj(v) for v in value]
    return value


def contains_pii(text: str) -> bool:
    return bool(mask_pii(text)[1])
