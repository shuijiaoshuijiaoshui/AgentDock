"""Embedding 提供方：local（fastembed/ONNX，生产）与 fake（测试确定性替身）。

fake 不是语义模型：它用字符 n-gram 哈希构造稳定向量，只用于无网络/CI
验证索引与融合链路；生产必须用 local。
"""

from __future__ import annotations

import hashlib
import math

from shared.config import get_settings


class EmbeddingUnavailable(RuntimeError):
    pass


_model = None


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return vec
    return [x / norm for x in vec]


def _fake_embed(text: str, dim: int) -> list[float]:
    """确定性字符 2-gram 哈希向量（非语义，测试/离线替身）。"""
    vec = [0.0] * dim
    body = (text or "").strip()
    if not body:
        body = "<empty>"
    grams = [body[i:i + 2] for i in range(max(1, len(body) - 1))]
    grams.append(body)
    for gram in grams:
        digest = hashlib.sha256(gram.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vec[idx] += sign
    return _normalize(vec)


def _local_embed(texts: list[str], model_name: str, cache_dir: str) -> list[list[float]]:
    global _model
    if _model is None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - 环境相关
            raise EmbeddingUnavailable(
                "缺少本地 embedding 依赖：请执行 `uv sync --extra embeddings`"
            ) from exc
        try:
            _model = TextEmbedding(model_name=model_name, cache_dir=cache_dir)
        except TypeError:  # 旧版 fastembed 无 cache_dir
            _model = TextEmbedding(model_name=model_name)
    return [list(map(float, vec)) for vec in _model.embed(texts)]


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    settings = get_settings()
    dim = int(settings.embedding_dim)
    if settings.embedding_provider == "fake":
        vectors = [_fake_embed(t, dim) for t in texts]
    else:
        try:
            vectors = _local_embed(
                texts, settings.embedding_model, settings.embedding_cache_dir)
        except EmbeddingUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - 模型加载/推理失败统一降级信号
            raise EmbeddingUnavailable(f"本地 embedding 失败：{exc}") from exc
    for vec in vectors:
        if len(vec) != dim:
            raise EmbeddingUnavailable(
                f"embedding 维度 {len(vec)} 与配置 embedding_dim={dim} 不一致；"
                "请对齐配置或重建索引")
    return [_normalize(v) for v in vectors]
