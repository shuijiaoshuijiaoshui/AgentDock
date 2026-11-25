"""生成 AgentCard JWS 签名密钥（ES256 / EC P-256，PRD 7.3.1）。

infra/keys/README.md 约定的引导脚本。dev/test 下首个服务启动也会按需自动生成
（shared/card_signing.ensure_card_key）；本脚本用于显式生成或轮换。

用法：
    uv run python scripts/gen_keys.py                # 缺失才生成
    uv run python scripts/gen_keys.py --force        # 覆盖重造（旧签名立即失效！）
    uv run python scripts/gen_keys.py --out 其他路径.pem

轮换（不使旧签名立即失效）：--force 换新钥后，把旧公钥登记进
CARD_VERIFY_KEYS_JSON = {"v1": "infra/keys/card_signing_key_public.pem"} 并
把 KEY_VERSION 升到新版本，旧 Card 在缓存 TTL 内仍可验。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.card_signing import ensure_card_key  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="生成 Card ES256 签名密钥对")
    ap.add_argument("--force", action="store_true",
                    help="覆盖已有密钥（会使已发布签名的旧 Card 立即验签失败）")
    ap.add_argument("--out", default="",
                    help="私钥输出路径（默认 settings.card_signing_key_file）")
    args = ap.parse_args()

    out = Path(args.out) if args.out else None
    path = ensure_card_key(force=args.force, path=out)
    pub = path.with_name(f"{path.stem}_public.pem")
    print(f"私钥（服务端签名用，勿入库）：{path}")
    print(f"公钥（验签方/轮换映射用）：{pub}")
    if not pub.exists():
        print("注意：公钥文件缺失，可从私钥重新导出。")


if __name__ == "__main__":
    main()
