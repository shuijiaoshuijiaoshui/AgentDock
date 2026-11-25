"""附件存储：MIME 嗅探 / 大小限制 / 私有目录 / sha256 / 越权校验。

- 上传：multipart（前端/测试用 httpx files=），内容嗅探决定真实类型；
- 存储：`.data/uploads/{tenant}/{attachment_id}{ext}`，随机文件名，禁止静态挂载；
- 下载：按行鉴权后 FileResponse，Content-Type/Disposition 固定，路径不含用户输入。
"""

from __future__ import annotations

import hashlib
import re
import secrets
from pathlib import Path

from shared.auth import AuthContext

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10MB
UPLOAD_ROOT = Path(".data/uploads")

# magic bytes 嗅探（优先级即顺序）
_MAGIC: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"%PDF-", "application/pdf", ".pdf"),
)


def _sniff(data: bytes) -> tuple[str, str] | None:
    for magic, mime, ext in _MAGIC:
        if data.startswith(magic):
            return mime, ext
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None


def safe_filename(name: str) -> str:
    """展示用文件名：剥离路径与控制字符，限长 120。"""
    cleaned = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", Path(name or "").name)
    return cleaned[:120] or "attachment"


def save_attachment(
    *,
    tenant_id: str,
    uploader_id: str,
    filename: str,
    data: bytes,
    conversation_id: str = "",
) -> dict:
    """嗅探 + 校验 + 落盘 + 入库；返回附件行 dict。失败抛 ValueError。"""
    if not data:
        raise ValueError("empty_file")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("file_too_large")
    sniffed = _sniff(data)
    if sniffed is None:
        raise ValueError("unsupported_type")
    mime, ext = sniffed
    from console import db

    attachment_id = secrets.token_hex(16)
    storage_dir = UPLOAD_ROOT / tenant_id
    storage_dir.mkdir(parents=True, exist_ok=True)
    storage_path = storage_dir / f"{attachment_id}{ext}"
    storage_path.write_bytes(data)
    sha256 = hashlib.sha256(data).hexdigest()
    safe_name = safe_filename(filename)
    try:
        row = db.create_attachment(
            attachment_id=attachment_id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            uploader_id=uploader_id,
            filename=safe_name,
            mime=mime,
            size=len(data),
            sha256=sha256,
            storage_path=str(storage_path),
        )
    except Exception:  # noqa: BLE001
        storage_path.unlink(missing_ok=True)
        raise
    return row


def can_download(
    row: dict, auth: AuthContext, *, conversation_lookup
) -> bool:
    """下载鉴权：上传者本人 / 同租户 staff 对其可见会话 / 客户本人所属会话。"""
    if row["tenant_id"] != auth.tenant_id:
        return False
    if row["uploader_id"] == auth.actor_id:
        return True
    if auth.role == "customer":
        conv = (conversation_lookup(row["conversation_id"])
                if row["conversation_id"] else None)
        return bool(conv and conv.get("customer_id") == auth.actor_id)
    if auth.role in ("agent", "supervisor", "admin", "finance"):
        # staff：会话归属校验（_conv_for 语义），无会话的附件仅上传者可见
        if not row["conversation_id"]:
            return False
        return conversation_lookup(row["conversation_id"]) is not None
    return False
