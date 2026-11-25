"""政策文档解析/切分/自动关键词（纯函数，无网络/DB）。"""

import pytest

from domains.policy.chunking import (
    ChunkingError,
    auto_keywords,
    clause_chunks,
    parse_document,
)


def test_parse_markdown_two_clauses():
    text = """前言（应被忽略）

## CLS-006 设备安装服务
安装由认证工程师上门完成，偏远地区需额外预约。

## CLS-007 延保服务
延保服务需在购买后 30 日内申请，覆盖原厂保修期外 12 个月。
"""
    clauses = parse_document(text)
    assert [c["clause_id"] for c in clauses] == ["CLS-006", "CLS-007"]
    assert clauses[0]["title"] == "设备安装服务"
    assert "认证工程师" in clauses[0]["text"]
    assert clauses[0]["keywords"]
    assert clauses[0]["chunks"][0]["chunk_index"] == 0


def test_plain_heading_style():
    clauses = parse_document("CLS-008: 发票补开\n发票遗失可申请补开，需提供购买凭证。")
    assert clauses[0]["clause_id"] == "CLS-008"
    assert clauses[0]["title"] == "发票补开"


def test_duplicate_clause_rejected_with_line():
    with pytest.raises(ChunkingError) as exc:
        parse_document("## CLS-009 甲\n内容一\n## CLS-009 乙\n内容二")
    assert exc.value.code == "duplicate_clause"
    assert exc.value.line == 3


def test_missing_heading_rejected():
    with pytest.raises(ChunkingError) as exc:
        parse_document("这是一段没有条款标题的政策正文。")
    assert exc.value.code == "no_clause_heading"


def test_long_clause_split_keeps_clause_id():
    body = "\n\n".join(f"第{i}段：" + "保修条款内容" * 120 for i in range(4))
    text = f"## CLS-010 超长条款\n{body}"
    clauses = parse_document(text, max_chars=500)
    assert clauses[0]["clause_id"] == "CLS-010"
    assert len(clauses[0]["chunks"]) > 1
    assert all(c["content_hash"] for c in clauses[0]["chunks"])


def test_auto_keywords_deterministic_and_relevant():
    title = "异地商家合作与采购流程"
    text = "异地商家合作需提交资质，区域经理审核经销资格并签署采购协议。"
    a = auto_keywords(title, text)
    b = auto_keywords(title, text)
    assert a == b
    assert "合作" in a or "商家" in a


def test_clause_chunks_title_prefixed():
    chunks = clause_chunks("整机保修", "保修期 12 个月。", max_chars=100)
    assert chunks[0]["text"].startswith("整机保修")
