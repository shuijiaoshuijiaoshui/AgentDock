"""评测体系防腐守护：跑通 2 个代表任务（fake ×1 trial）防 harness 腐化。

不追求覆盖率——全量 16 任务 × k trials 由 `uv run python -m evals.run` 承担；
这里只保证：栈能起、两表面能跑、证据三通道能采、门禁能出结论。
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.harness import EvalStack  # noqa: E402
from evals.runner import grade_task, run_task  # noqa: E402
from evals.run import load_tasks  # noqa: E402


def test_evals_smoke_two_tasks():
    tasks = load_tasks(["E-Q1", "E-R1"])
    assert len(tasks) == 2

    async def _run():
        stack = EvalStack(provider="fake")
        try:
            stack.start()
            out = []
            for t in tasks:
                stack.reseed()
                ctx = await run_task(stack, t)
                out.append(grade_task(t, ctx))
            return out
        finally:
            stack.stop()

    results = asyncio.run(_run())
    by_id = {r["task_id"]: r for r in results}
    # 单域查询链路绿
    assert by_id["E-Q1"]["scores"]["completion"] == 1.0
    # 资金 Saga 回滚链路绿（残留=0 属 robustness 维度）
    r1 = by_id["E-R1"]
    assert r1["scores"]["completion"] == 1.0
    assert r1["scores"]["robustness"] == 1.0
    # 证据通道贯通：审计事件与 span 均已采集
    for r in results:
        assert r["rubrics"], "rubric 未执行"
