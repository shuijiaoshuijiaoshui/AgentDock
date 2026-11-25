"""集中配置：全部从 .env / 环境变量读取（见 .env.example）。"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# Mock 业务库（商品/促销/券/客户/条款/售后/工单）；相对运行目录
MOCK_DB_PATH = "infra/mockdata/mock.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ===== 业务库（域 + console 共用；生产换 Postgres）=====
    business_db_path: str = "infra/mockdata/mock.db"

    # ===== LLM（智谱 GLM，OpenAI 兼容端点）=====
    llm_provider: Literal["openai", "fake"] = "openai"
    zhipuai_api_key: str = ""
    llm_base_url: str = "https://open.bigmodel.cn/api/paas/v4/"
    llm_small_model: str = "glm-4.6"
    llm_main_model: str = "glm-4.6"

    # ===== 状态持久化 =====
    checkpointer: Literal["sqlite", "postgres"] = "sqlite"
    sqlite_db_path: str = ".data/checkpoints.sqlite"
    postgres_dsn: str = "postgresql://agentdock:agentdock@localhost:5433/agentdock"

    # ===== 服务与安全 =====
    env: Literal["dev", "test", "prod"] = "dev"
    dev_mode: bool = True
    # 兼容旧脚本/旧部署的字段；新代码不再把它当业务身份（仅保留给显式迁移脚本）
    internal_token: str = "dev-internal-token"

    # JWT / 密钥（生产必须覆盖默认值；见 shared/keys.py）
    auth_secret: str = "dev-jwt-secret-change-me"
    auth_issuer: str = "agentdock"
    auth_token_ttl_minutes: int = 480
    key_provider: Literal["env", "file", "kms"] = "env"
    key_version: str = "v1"
    card_signing_key: str = "dev-card-signing-key"
    card_signing_key_file: str = "infra/keys/card_signing_key.pem"
    # M4（PRD 7.3.1）：Card JWS 签发/验签（ES256）。签名/验签默认开；
    # CARD_VERIFY_KEYS_JSON = {"v1": "<PEM 内容或公钥文件路径>"} 支持新旧 kid 并存轮换。
    card_signing_enabled: bool = True
    card_verify_enabled: bool = True
    card_cache_ttl_seconds: int = 300
    card_verify_keys_json: str = ""

    # 共享安全存储（审计/审批；生产换 Postgres）
    shared_db_path: str = ".data/shared.db"

    # ===== securityd（审计/审批/确认单写者）=====
    # local：单进程直写 SQLite（只用于单测）；remote：所有业务服务经 HTTP 调 securityd。
    security_mode: Literal["local", "remote"] = "local"
    security_db_path: str = ".data/security.db"
    security_port: int = 8309
    security_url: str = ""
    audit_signing_key: str = "dev-audit-signing-key"
    # 同源反代/本地直连都可配置；逗号分隔。空 = 本机 console 端口。
    allowed_origins: str = ""
    cookie_path: str = "/"

    # PII / 限流
    pii_masking_enabled: bool = True
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = 120
    rate_limit_ws_per_minute: int = 60

    # ===== OTel 跨进程 trace（M4 / PRD 7.10）=====
    # 默认关：关闭时零导入零开销（"trace 可丢弃，审计不可"）。
    # 采样 = 头部采样（按 trace_id 确定性掷签）；异常全采需 collector 级尾采样（已知限制）。
    tracing_enabled: bool = False
    tracing_sample_ratio: float = 1.0
    trace_dir: str = ".data/traces"
    # 本进程服务名（dev_all/compose 注入 SERVICE_NAME；span 归档按它分文件）
    service_name: str = ""

    # 金额阈值与审批（单位：元）
    approval_refund_threshold: float = 500.0
    approval_price_change_threshold: float = 500.0
    approval_invoice_threshold: float = 1000.0
    approval_dual_threshold_multiplier: float = 5.0
    approval_ttl_minutes: int = 30

    # ===== 多轮历史注入（M4 / PRD 10.4：近 N 轮原文 + 更早滚动摘要）=====
    history_recent_turns: int = 6
    history_turn_max_chars: int = 300

    # ===== 政策检索（keyword 默认；hybrid 需 pgvector + embedding）=====
    policy_retriever: Literal["keyword", "hybrid"] = "keyword"
    policy_vector_dsn: str = ""            # 空 = 沿用 postgres_dsn
    embedding_provider: Literal["local", "fake"] = "local"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_dim: int = 512
    embedding_cache_dir: str = ".data/models"
    policy_vector_min_score: float = 0.35
    policy_chunk_max_chars: int = 1200

    orchestrator_port: int = 8300
    merchandising_port: int = 8301
    # 客户与账务域（资料/授信/对账/投诉）
    customer_port: int = 8308
    # 营销经营分析域（投流/选品/竞品/爆款，toB 商家服务端）
    marketing_port: int = 8310
    # 人工坐席工作台（A2A handoff.accept + 工作台页双形态，PRD 3.3 第 8 行 / 7.12）
    console_port: int = 8307

    # ===== 服务间地址（Docker/compose 用服务名直连；空 = 本机 127.0.0.1:端口）=====
    orchestrator_url: str = ""
    merchandising_url: str = ""
    customer_url: str = ""
    marketing_url: str = ""
    order_url: str = ""
    fulfillment_url: str = ""
    aftersale_url: str = ""
    finance_url: str = ""
    policy_url: str = ""
    console_url: str = ""       # humandesk（转人工落点）
    # 本服务对外广播地址（写进 AgentCard）。注意：a2a-sdk 客户端建 transport 跟随
    # Card 里声明的 URL 回连，而不是 create_from_url 传入的地址——Docker 里必须
    # 设为 http://<服务名>:<端口>，否则全栈互调会回打 127.0.0.1（已实测踩坑）。
    public_url: str = ""

    def domain_url(self, domain: str) -> str:
        """调用方视角的域地址：覆盖项优先，否则本机端口拼接。"""
        override = {
            "orchestrator": self.orchestrator_url,
            "merchandising": self.merchandising_url,
            "customer": self.customer_url,
            "marketing": self.marketing_url,
            "order": self.order_url,
            "fulfillment": self.fulfillment_url,
            "aftersale": self.aftersale_url,
            "finance": self.finance_url,
            "policy": self.policy_url,
            "humandesk": self.console_url,
        }.get(domain, "")
        if override:
            return override
        port = {
            "orchestrator": self.orchestrator_port,
            "merchandising": self.merchandising_port,
            "customer": self.customer_port,
            "marketing": self.marketing_port,
            "order": self.order_port,
            "fulfillment": self.fulfillment_port,
            "aftersale": self.aftersale_port,
            "finance": self.finance_port,
            "policy": self.policy_port,
            "humandesk": self.console_port,
        }.get(domain)
        return f"http://127.0.0.1:{port}" if port else ""

    def my_url(self, port: int) -> str:
        """本服务广播地址（AgentCard.url 用）：public_url 优先。"""
        return self.public_url or f"http://127.0.0.1:{port}"

    order_port: int = 8302
    fulfillment_port: int = 8303
    aftersale_port: int = 8304
    finance_port: int = 8305
    policy_port: int = 8306

    @property
    def llm_ready(self) -> bool:
        return bool(self.zhipuai_api_key) or self.llm_provider == "fake"


@lru_cache
def get_settings() -> Settings:
    return Settings()
