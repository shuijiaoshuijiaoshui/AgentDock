"""密钥管理抽象（JWT / Card 签名 / 未来 KMS）。

设计取舍：
- 开发态用环境变量/本地文件，启动时明确告警；
- 生产态通过 `KEY_PROVIDER` 选择 KMS 实现，禁止回落到默认密钥；
- 业务代码只依赖 `get_signing_key()` / `get_card_signing_key()`，不直接读 env。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from pathlib import Path

from shared.config import get_settings

DEV_JWT_SECRET = "dev-jwt-secret-change-me"


class KeyConfigurationError(RuntimeError):
    """密钥配置错误（生产环境使用默认密钥、缺少 KMS 配置等）。"""


@dataclass(frozen=True)
class KeyRef:
    name: str
    version: str
    material: bytes


class KeyProvider:
    def get(self, name: str) -> KeyRef:  # pragma: no cover - interface
        raise NotImplementedError


class EnvKeyProvider(KeyProvider):
    """开发/测试：从环境变量读取；生产环境必须显式提供非默认值。"""

    def __init__(self) -> None:
        self.settings = get_settings()

    def get(self, name: str) -> KeyRef:
        if name == "jwt":
            raw = self.settings.auth_secret or DEV_JWT_SECRET
            if raw == DEV_JWT_SECRET and self.settings.env != "dev":
                raise KeyConfigurationError(
                    "生产环境禁止使用默认 AUTH_SECRET，请配置 KMS 或 AUTH_SECRET"
                )
            return KeyRef("jwt", self.settings.key_version, raw.encode("utf-8"))
        if name == "card":
            path = Path(self.settings.card_signing_key_file)
            if path.exists():
                return KeyRef("card", self.settings.key_version, path.read_bytes())
            raw = self.settings.card_signing_key or "dev-card-signing-key"
            if raw == "dev-card-signing-key" and self.settings.env != "dev":
                raise KeyConfigurationError(
                    "生产环境禁止使用默认 Card 签名密钥，请配置 KMS 或 CARD_SIGNING_KEY"
                )
            return KeyRef("card", self.settings.key_version, raw.encode("utf-8"))
        if name == "audit":
            raw = self.settings.audit_signing_key or "dev-audit-signing-key"
            if raw == "dev-audit-signing-key" and self.settings.env == "prod":
                raise KeyConfigurationError(
                    "生产环境禁止使用默认 AUDIT_SIGNING_KEY，请配置 KMS 或强随机值"
                )
            return KeyRef("audit", self.settings.key_version, raw.encode("utf-8"))
        raise KeyConfigurationError(f"未知密钥：{name}")


class KMSKeyProvider(KeyProvider):
    """云 KMS 适配点。

    不在仓库里绑定某一家云 SDK（避免把 boto3/阿里云 SDK 变成强依赖）。
    接入时实现 `get()`：从 KMS 取回明文密钥或直接暴露 sign/verify 接口。
    当前仅支持 `KEY_PROVIDER=env|file`；显式配置 `kms` 会快速失败，避免
    “看起来接了 KMS、实际还在用默认密钥”。
    """

    def __init__(self) -> None:
        raise KeyConfigurationError(
            "KEY_PROVIDER=kms 尚未接入具体云 SDK：请在部署环境实现 KMSKeyProvider，"
            "或使用 KEY_PROVIDER=env 并注入 AUTH_SECRET/CARD_SIGNING_KEY"
        )


def get_provider() -> KeyProvider:
    provider = os.environ.get("KEY_PROVIDER", get_settings().key_provider).lower()
    if provider == "kms":
        return KMSKeyProvider()
    return EnvKeyProvider()


def get_signing_key() -> KeyRef:
    return get_provider().get("jwt")


def get_card_signing_key() -> KeyRef:
    return get_provider().get("card")


def get_audit_signing_key() -> KeyRef:
    return get_provider().get("audit")


def sign_hmac(key: KeyRef, payload: bytes) -> str:
    """HMAC-SHA256 签名（Card/内部票据的最小实现；JWS 规范化可在此扩展）。"""
    digest = hmac.new(key.material, payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_hmac(key: KeyRef, payload: bytes, signature: str) -> bool:
    expected = sign_hmac(key, payload)
    return hmac.compare_digest(expected, signature)
