"""LLM 工厂：智谱 GLM（OpenAI 兼容端点）。

真实模式用 langchain-openai 的 ChatOpenAI + bigmodel base_url（智谱官方推荐的
LangChain 接入方式，工具调用/结构化输出兼容性比 langchain_community.ChatZhipuAI 稳）。
fake 模式不构造 LLM，由各图节点自带规则分支（见 orchestrator/graph.py、domains/*/graph.py）。
"""

from functools import lru_cache

from langchain_openai import ChatOpenAI

from shared.config import Settings, get_settings


def _real_llm(model: str) -> ChatOpenAI:
    s = get_settings()
    return ChatOpenAI(
        model=model,
        api_key=s.zhipuai_api_key,
        base_url=s.llm_base_url,
        temperature=0.2,
        timeout=60,
        max_retries=1,
    )


@lru_cache
def get_small_llm() -> ChatOpenAI:
    """意图/槽位抽取等轻量任务（PRD 9.3：小模型降本）。"""
    return _real_llm(get_settings().llm_small_model)


@lru_cache
def get_main_llm() -> ChatOpenAI:
    """域内推理与答案合成。"""
    return _real_llm(get_settings().llm_main_model)


def llm_provider(settings: Settings | None = None) -> str:
    return (settings or get_settings()).llm_provider
