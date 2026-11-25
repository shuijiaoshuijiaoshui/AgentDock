"""Artifact 统一结构（PRD 7.3.1 / 技术方案 2.4）。

每个域 Agent 的产物必须带权威域、数据时点与快照号——裁决器（M3）据此选权威值。
"""

from pydantic import BaseModel, Field


class ArtifactBase(BaseModel):
    authority_domain: str = Field(description="权威域（merchandising/order/…）")
    skill: str = Field(description="产出该产物的 skill id")
    as_of: str = Field(description="数据时点 ISO8601")
    snapshot_id: str = Field(description="本次会话快照号（编排层 route 生成）")
    data: dict = Field(default_factory=dict, description="业务数据载荷")
