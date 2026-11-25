"""政策文档解析与切分：Markdown/TXT → 条款级 chunk + 自动关键词。

约定（企业导入规范）：
- 一篇文档由若干条款组成，标题必须是 `## CLS-006 标题` 或 `CLS-006: 标题`；
- 一条款 = 一个权威单元（写入 policy_drafts 的最小单位）；
- 超长条款按空行分段，仍共享同一 clause_id（chunk_index 区分）；
- 关键词自动生成（标题词 + 中文 2-gram 词频），不再要求人工逐条调词。
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter

_HASH_HEADING = re.compile(
    r"^#{1,4}\s+(CLS-\d+)\s*[：:、.\-]?\s*(.*)$", re.IGNORECASE)
_PLAIN_HEADING = re.compile(
    r"^(CLS-\d+)\s*[：:、.]\s*(.*)$", re.IGNORECASE)
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")
_ASCII_WORD = re.compile(r"[A-Za-z][A-Za-z0-9\-]{1,}")
_STOP_GRAMS = {
    "的了", "了的", "一个", "我们", "你们", "他们", "这个", "那个", "以及",
    "并且", "或者", "但是", "因为", "所以", "如果", "可以", "需要", "进行",
    "相关", "以下", "以上", "其中", "对于", "关于", "按照", "根据",
}


class ChunkingError(ValueError):
    def __init__(self, code: str, detail: str, line: int = 0) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.line = line


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def auto_keywords(title: str, text: str, limit: int = 8) -> str:
    """确定性关键词：标题加权 + 正文中文 2-gram / 英文词频。"""
    scores: Counter[str] = Counter()
    for word in _ASCII_WORD.findall(title):
        scores[word.lower()] += 4
    for run in _CJK_RUN.findall(title):
        for i in range(len(run) - 1):
            gram = run[i:i + 2]
            if gram not in _STOP_GRAMS:
                scores[gram] += 3
    for word in _ASCII_WORD.findall(text):
        scores[word.lower()] += 1
    for run in _CJK_RUN.findall(text):
        for i in range(len(run) - 1):
            gram = run[i:i + 2]
            if gram not in _STOP_GRAMS:
                scores[gram] += 1
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return " ".join(term for term, _ in ordered[:limit])


def _split_long(text: str, max_chars: int) -> list[str]:
    """按空行分段；单段超长时硬切并保留 80 字重叠。"""
    if len(text) <= max_chars:
        return [text]
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paras or [text]:
        if len(para) > max_chars:
            if buf:
                chunks.append(buf.strip())
                buf = ""
            step = max(200, max_chars - 80)
            for start in range(0, len(para), step):
                piece = para[start:start + max_chars]
                if piece.strip():
                    chunks.append(piece.strip())
                if start + max_chars >= len(para):
                    break
            continue
        if not buf:
            buf = para
        elif len(buf) + 2 + len(para) <= max_chars:
            buf += "\n\n" + para
        else:
            chunks.append(buf.strip())
            buf = para
    if buf.strip():
        chunks.append(buf.strip())
    return chunks or [text[:max_chars]]


def clause_chunks(title: str, text: str, max_chars: int = 1200) -> list[dict]:
    """把一个条款正文切成 chunk（标题拼进 chunk，保留 clause 语义）。"""
    parts = _split_long(text, max_chars)
    chunks = []
    for i, part in enumerate(parts):
        payload = f"{title}\n{part}".strip()
        chunks.append({"chunk_index": i, "text": payload,
                       "content_hash": content_hash(payload)})
    return chunks


def parse_document(text: str, *, max_chars: int = 1200) -> list[dict]:
    """解析 Markdown/TXT 为条款列表；格式不合规抛 ChunkingError（带行号）。"""
    if not (text or "").strip():
        raise ChunkingError("empty_document", "文档为空")
    clauses: list[dict] = []
    current: dict | None = None
    seen: dict[str, int] = {}

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        body = "\n".join(current.pop("lines")).strip()
        if not body:
            raise ChunkingError(
                "empty_clause", f"{current['clause_id']} 正文为空",
                current["line"])
        current["text"] = body
        current["keywords"] = auto_keywords(current["title"], body)
        current["chunks"] = clause_chunks(current["title"], body, max_chars)
        clauses.append(current)
        current = None

    for lineno, line in enumerate((text or "").splitlines(), start=1):
        m = _HASH_HEADING.match(line) or _PLAIN_HEADING.match(line)
        if m:
            flush()
            clause_id = m.group(1).upper()
            title = (m.group(2) or "").strip()
            if not title:
                raise ChunkingError(
                    "missing_title", f"{clause_id} 缺少标题", lineno)
            if clause_id in seen:
                raise ChunkingError(
                    "duplicate_clause",
                    f"{clause_id} 重复出现（首次在第 {seen[clause_id]} 行）",
                    lineno)
            seen[clause_id] = lineno
            current = {"clause_id": clause_id, "title": title,
                       "line": lineno, "lines": []}
            continue
        if current is not None:
            current["lines"].append(line)

    flush()
    if not clauses:
        raise ChunkingError(
            "no_clause_heading",
            "未找到条款标题；请使用 `## CLS-006 标题` 或 `CLS-006: 标题`")
    return clauses
