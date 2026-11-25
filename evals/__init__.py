"""AgentDock-Eval：Claw-Eval 方法论的项目化落地。

三个证据通道（执行轨迹 / 审计日志 / 环境快照）→ 确定性 rubric →
Completion / Safety / Robustness 三维评分 → Pass@k / Pass^k → 上线门禁。

用法：
    uv run python -m evals.run --trials 3 --provider fake
详见 docs/AgentDock-Eval指南.md。
"""
