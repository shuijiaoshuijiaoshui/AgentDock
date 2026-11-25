"""AgentCard JWS 签发/验签（PRD 7.3.1：JWS + RFC 8785，验签通过才信任）。

实现走 a2a-sdk 1.2.3 官方 signing 模块：RFC 8785 JCS 规范化 + PyJWT JWS，
本模块只负责密钥解析与 callable 组装，不自己实现协议细节。

- 算法：ES256（EC P-256 非对称）。私钥只在发布方（各 Server），验签方持公钥。
- 密钥文件：settings.card_signing_key_file（默认 infra/keys/card_signing_key.pem，
  .gitignore 已挡）。dev/test 缺失时首次使用自动生成一份进程间共享的密钥文件
  （dev_all 并发拉起用 O_EXCL 锁防竞争）；ENV=prod 拒绝自动生成，必须预置。
- kid 轮换：签名用 settings.key_version 作 kid；验签按 Card protected header 的
  kid 取公钥——当前版本从私钥文件派生，历史版本经 CARD_VERIFY_KEYS_JSON
  （{"v1": "<PEM 内容或公钥文件路径>"}）并存；未知 kid 快速失败。
- 历史的 HMAC 共享密钥（CARD_SIGNING_KEY env）不用于 ES256：对称密钥意味着
  验签方也能伪造签名，与"私钥签名"语义冲突；文件里若被写成非 PEM 会明确报错。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from a2a.client.card_resolver import AgentCardResolutionError
from a2a.utils.signing import (
    InvalidSignaturesError,
    NoSignatureError,
    SignatureVerificationError,
)

from shared.config import get_settings
from shared.keys import KeyConfigurationError

# SDK 验签失败类型（delegate 捕获后审计并转 CardVerificationError）
SIGNATURE_ERRORS = (SignatureVerificationError,)


class CardVerificationError(RuntimeError):
    """AgentCard 验签失败——委托被拒绝（失败也是一种产物，见 PRD 7.8）。"""


# ---------------------------------------------------------------------------
# 密钥：生成 / 解析
# ---------------------------------------------------------------------------

def _generate_private_pem() -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _public_pem_from_private_pem(pem: bytes) -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = serialization.load_pem_private_key(pem, password=None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    return key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _key_path(path: Path | None = None) -> Path:
    return path or Path(get_settings().card_signing_key_file)


def ensure_card_key(*, force: bool = False, path: Path | None = None) -> Path:
    """确保 Card 签名私钥文件存在，返回其路径。

    dev/test：缺失则生成（O_EXCL 锁防止 dev_all 并发拉起时多个进程各写各的）；
    prod：缺失直接报错，密钥必须由运维预置（与 shared/keys.py 同一哲学）。
    """
    key_path = _key_path(path)
    settings = get_settings()
    if key_path.exists() and not force:
        return key_path
    if settings.env == "prod":
        raise KeyConfigurationError(
            f"生产环境必须预置 Card 签名密钥（{key_path}）；"
            "运行 scripts/gen_keys.py 生成后投放，禁止自动生成"
        )
    key_path.parent.mkdir(parents=True, exist_ok=True)
    lock = key_path.with_name(key_path.name + ".lock")
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # 另一个进程正在生成：等它写完（最多 10s）
        for _ in range(200):
            if key_path.exists():
                return key_path
            time.sleep(0.05)
        raise KeyConfigurationError(f"等待 Card 密钥生成超时：{key_path}") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"pid={os.getpid()}\n")
        if key_path.exists() and not force:
            return key_path  # 等锁期间已被先到的进程写好
        pem = _generate_private_pem()
        tmp = key_path.with_name(f"{key_path.name}.tmp{os.getpid()}")
        tmp.write_bytes(pem)
        os.replace(tmp, key_path)  # 同目录原子替换，读方要么看不到要么看到完整 PEM
        pub = key_path.with_name(f"{key_path.stem}_public.pem")
        tmp_pub = pub.with_name(f"{pub.name}.tmp{os.getpid()}")
        tmp_pub.write_bytes(_public_pem_from_private_pem(pem))
        os.replace(tmp_pub, pub)
        return key_path
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


def _private_pem(path: Path | None = None) -> bytes:
    key_path = _key_path(path)
    ensure_card_key(path=path)
    pem = key_path.read_bytes()
    if not pem.startswith(b"-----BEGIN"):
        raise KeyConfigurationError(
            f"{key_path} 不是 PEM 私钥（疑似历史 HMAC 共享密钥）；"
            "删除后运行 scripts/gen_keys.py 重新生成"
        )
    return pem


# ---------------------------------------------------------------------------
# 签发（发布方）/ 验签（消费方）
# ---------------------------------------------------------------------------

def sign_card(card, *, path: Path | None = None, kid: str | None = None):
    """给 AgentCard 附加 JWS 签名（RFC 8785 JCS 由 SDK 处理），返回带签名的 Card。"""
    from a2a.utils.signing import create_agent_card_signer

    settings = get_settings()
    signer = create_agent_card_signer(
        _private_pem(path),
        {"kid": kid or settings.key_version, "alg": "ES256", "typ": "JOSE"},
    )
    return signer(card)


def card_key_material(kid: str | None = None, jku: str | None = None) -> bytes:
    """按 kid 解析验签公钥（create_signature_verifier 的 key_provider 签名）。

    优先级：CARD_VERIFY_KEYS_JSON 映射（轮换并存）> 当前版本（从私钥派生公钥）。
    """
    settings = get_settings()
    if kid and settings.card_verify_keys_json:
        entry = json.loads(settings.card_verify_keys_json).get(kid)
        if entry is not None:
            p = Path(entry)
            return p.read_bytes() if p.exists() else entry.encode("utf-8")
    if not kid or kid == settings.key_version:
        return _public_pem_from_private_pem(_private_pem())
    raise CardVerificationError(
        f"未知 Card 签名 kid：{kid!r}（当前 {settings.key_version}）"
    )


def build_card_verifier():
    """返回 SDK 签名的 verifier callable：验签通过静默，失败抛 SignatureVerificationError。"""
    from a2a.utils.signing import create_signature_verifier

    return create_signature_verifier(card_key_material, ["ES256"])
