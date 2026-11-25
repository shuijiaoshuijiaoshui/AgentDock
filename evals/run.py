"""AgentDock-Eval CLI。

用法：
    uv run python -m evals.run --trials 3 --provider fake
    uv run python -m evals.run --tasks E-S1,E-R1 --trials 1

流程：拉起评测栈（每套评测一次）→ 每 trial 重置业务库 → 逐任务执行步骤 →
三证据通道采集 → rubric 评分 → 三维聚合 + Pass@k/Pass^k → 门禁判定 → 报告。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

from .harness import EvalStack
from .runner import grade_task, run_task
from .score import aggregate, judge_gate, write_reports

ROOT = Path(__file__).resolve().parents[1]
TASKS_DIR = Path(__file__).resolve().parent / "tasks"
GATE_FILE = Path(__file__).resolve().parent / "gate.yaml"


def load_tasks(ids: list[str] | None, retriever: str = "keyword") -> list[dict]:
    tasks = []
    for f in sorted(TASKS_DIR.glob("*.yaml")):
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        data["_file"] = f.name
        tasks.append(data)
    if ids:
        want = {x.strip() for x in ids}
        tasks = [t for t in tasks if t["id"] in want]
        missing = want - {t["id"] for t in tasks}
        if missing:
            raise SystemExit(f"任务不存在：{sorted(missing)}")
        return tasks
    if retriever == "hybrid":
        # hybrid 默认只跑语义套件；核心任务仍可显式 --tasks 指定
        return [t for t in tasks if t.get("requires") == "hybrid"]
    return [t for t in tasks if t.get("requires") != "hybrid"]


async def amain(args: argparse.Namespace) -> int:
    tasks = load_tasks(
        [x for x in args.tasks.split(",") if x] if args.tasks else None,
        args.retriever)
    if not tasks:
        raise SystemExit(f"没有可运行的 {args.retriever} 任务")
    if args.retriever == "hybrid":
        from domains.policy import vector_index

        if not vector_index.ping():
            raise SystemExit(
                "hybrid 模式需要可达的 pgvector：请设置 POLICY_VECTOR_DSN，"
                "并先运行 scripts/index_policies.py --rebuild")
    gate = yaml.safe_load(GATE_FILE.read_text(encoding="utf-8"))
    trials_n = max(1, args.trials)
    print(f"[eval] 任务 {len(tasks)} 个 × {trials_n} trials，"
          f"provider={args.provider} retriever={args.retriever}")
    t0 = time.time()
    stack = EvalStack(provider=args.provider, retriever=args.retriever)
    results: list[dict] = []
    try:
        stack.start()
        for task in tasks:
            trials = []
            for k in range(1, trials_n + 1):
                stack.reseed()
                ctx = await run_task(stack, task)
                trials.append(grade_task(task, ctx))
                worst = trials[-1]["scores"]
                print(f"  [{task['id']}] trial {k}: {worst}")
            results.append(aggregate(task, trials))
    finally:
        stack.stop()
    verdict = judge_gate(results, gate)
    meta = {
        "timestamp": datetime.now().strftime("%Y%m%d-%H%M%S"),
        "provider": args.provider,
        "trials": trials_n,
        "suite": args.tasks or "core",
        "elapsed_sec": round(time.time() - t0, 1),
    }
    out_dir = Path(__file__).resolve().parent / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    mpath, jpath = write_reports(out_dir, meta, results, verdict)
    print(f"\n[eval] 用时 {meta['elapsed_sec']}s；报告：\n  {mpath}\n  {jpath}")
    print(f"[eval] 门禁：{'PASS' if verdict['pass'] else 'FAIL'} "
          f"completion 均分={verdict['completion_avg_all']} "
          f"安全违规={verdict['safety_violations_total']}")
    for f in verdict["failures"]:
        print(f"  - ❌ {f}")
    return 0 if verdict["pass"] else 1


def main() -> int:
    p = argparse.ArgumentParser(prog="evals.run")
    p.add_argument("--trials", type=int, default=3,
                   help="每个任务重复次数（Pass@k/Pass^k 用，默认 3）")
    p.add_argument("--provider", choices=("fake", "real"), default="fake",
                   help="模型口径：fake=规则模式（确定性回归）；real=真 LLM")
    p.add_argument("--retriever", choices=("keyword", "hybrid"),
                   default="keyword",
                   help="政策检索模式：keyword=默认；hybrid 需 pgvector + embedding")
    p.add_argument("--tasks", default="",
                   help="逗号分隔任务 id（默认全部 core 套件）")
    args = p.parse_args()
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\n[eval] 中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
