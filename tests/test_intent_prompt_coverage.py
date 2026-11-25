"""意图理解 prompt 菜单覆盖断言（交接手册 §12 已知漂移的回归防线）。

UNDERSTAND_SYSTEM 候选菜单必须覆盖路由表全部意图——真模型（LLM_PROVIDER=openai）
下缺菜单一类意图就不可靠（1.5 版曾缺 8 个）。
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.prompts import UNDERSTAND_SYSTEM, UNDERSTAND_VERSION  # noqa: E402
from orchestrator.route_table import ROUTE_TABLE  # noqa: E402


def test_understand_menu_covers_all_routed_intents():
    menu = set(re.findall(r"^\s*-\s*([a-z_]+)[：:]", UNDERSTAND_SYSTEM, re.M))
    missing = sorted(set(ROUTE_TABLE) - menu)
    assert not missing, f"UNDERSTAND_SYSTEM 菜单缺少意图：{missing}"


def test_understand_version_bumped_for_menu_change():
    # 菜单变更必须 bump 版本（PRD 7.4 灰度/回滚依据）
    assert UNDERSTAND_VERSION >= "understand@1.6"
