"""能力模块分层测试（业务模块视图 ← → 域/工具映射的防漂移防线）。

对应 shared/modules.py：MODULES（11 个）+ SKILL_MODULE（35 个上卡 skill）。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest

from shared.modules import (
    MODULES,
    SKILL_MODULE,
    module_label,
    module_of,
    module_order,
    modules_of_side,
    primary_module,
    skills_of_module,
)

DOMAINS = ("merchandising", "order", "fulfillment", "aftersale",
           "finance", "policy", "customer", "marketing")


def _all_domain_skills() -> dict[str, str]:
    """{domain: tool_name} → 全量 skill 集合 {skill_id: domain}。"""
    import importlib

    out: dict[str, str] = {}
    count = 0
    for d in DOMAINS:
        mod = importlib.import_module(f"domains.{d}.tools")
        count += len(mod.TOOLS)
        for skill in mod.TOOL_TO_SKILL.values():
            assert skill not in out, f"duplicate skill {skill}"
            out[skill] = d
    assert count == 35, f"工具总数漂移：{count}"
    return out


def test_every_skill_maps_to_modules():
    """34/34 覆盖：每个域 skill 都登记了模块，且 SKILL_MODULE 无过期条目。"""
    skills = _all_domain_skills()
    missing = sorted(set(skills) - set(SKILL_MODULE))
    assert not missing, f"未登记模块的 skill：{missing}"
    stale = sorted(set(SKILL_MODULE) - set(skills))
    assert not stale, f"SKILL_MODULE 含不存在的 skill：{stale}"
    assert len(SKILL_MODULE) == 35


def test_module_ids_valid_and_planned_modules_have_no_skills():
    """模块 id 合法；planned 模块不得挂 skill（防止对外叙事漂移）。"""
    for skill, mods in SKILL_MODULE.items():
        assert mods, skill
        for m in mods:
            assert m in MODULES, f"{skill} 引用了未声明模块 {m}"
        assert primary_module(skill) in MODULES
    planned = {m for m, meta in MODULES.items() if meta["status"] == "planned"}
    for m in planned:
        assert skills_of_module(m) == [], f"planned 模块 {m} 不应挂 skill"
    # 侧别与展示序
    assert modules_of_side("consumer") and modules_of_side("merchant")
    order = module_order()
    assert order["assist.guide"] < order["assist.review"] \
        < order["merchant.insight"]


def test_domain_cards_carry_module_tags():
    """每个域 Card 的每个 skill 都带 module:<主模块> 标签（集中注入验证）。"""
    import importlib

    for d in DOMAINS:
        card_mod = importlib.import_module(f"domains.{d}.card")
        card = card_mod.build_card(f"http://127.0.0.1:9999")
        assert card.skills, d
        for sk in card.skills:
            tags = list(sk.tags)
            primary = primary_module(sk.id)
            assert primary, f"{d}/{sk.id} 未登记模块"
            assert f"module:{primary}" in tags, f"{d}/{sk.id} 缺标签：{tags}"
            # 主 + 次模块全部上卡
            for m in module_of(sk.id):
                assert f"module:{m}" in tags


def test_console_registry_has_module_fields_and_ordered():
    """工作台工具面板：每个入口带 module/label，且按模块声明序排序。"""
    from console.tools import registry
    from shared.auth import ALL_SCOPES, AuthContext

    auth = AuthContext(tenant_id="t_demo", actor_id="AG-001", role="agent",
                       scopes=frozenset(ALL_SCOPES))
    tools = registry(auth)
    assert len(tools) == 11, f"面板入口数漂移：{len(tools)}"
    order = module_order()
    last = -1
    for t in tools:
        assert t["module"] in MODULES, t
        assert t["module_label"] == module_label(t["module"])
        assert t["skill"] in SKILL_MODULE
        idx = order[t["module"]]
        assert idx >= last, f"面板未按模块序排序：{t['id']}"
        last = idx


def test_capability_matrix_doc_is_in_sync():
    """能力矩阵文档存在，且包含全部 11 个模块名（文档-代码轻校验）。"""
    doc = ROOT / "docs" / "能力矩阵.md"
    assert doc.exists(), "缺少 docs/能力矩阵.md"
    text = doc.read_text(encoding="utf-8")
    for m, meta in MODULES.items():
        assert meta["name"] in text, f"能力矩阵缺少模块「{meta['name']}」"
        assert m in text, f"能力矩阵缺少模块 id {m}"
    assert "35" in text
