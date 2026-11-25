dev 签名密钥目录：私钥不入库（.gitignore 已挡 *.pem/*.jwk）。
M1 阶段暂用 DEV_MODE 内部 token；JWS 签发/验签落地时，运行 scripts/gen_keys.py 生成。
