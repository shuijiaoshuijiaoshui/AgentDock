"""仓库真实数字：测试数 / skill 数 / 服务数 / 文件与行数。

用法：
  python scripts/repo_stats.py           # 打印
  python scripts/repo_stats.py --check   # 核心指标硬校验（tests/skills/服务数）
  python scripts/repo_stats.py --write   # 用真实数字回写标记
非核心指标（py_loc/web_loc/tracked_files/py_files）仅在 --check 时警告，--write 自动刷新。
"""

from __future__ import annotations

import argparse
import ast
import importlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MARK_RE = re.compile(r"<!-- STATS:([a-z_]+) -->(.*?)<!-- /STATS -->", re.S)
MARK_FILES = (ROOT / "README.md", ROOT / "docs" / "STATS.md")
CHECK_STRICT = {"tests", "skills", "dev_services",
                "compose_services", "compose_app_services"}


def _pytest_count() -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT, capture_output=True, text=True, timeout=180,
    )
    out = proc.stdout + proc.stderr
    m = re.search(r"(\d+)\s+tests?\s+collected", out)
    if m:
        return int(m.group(1))
    return sum(1 for line in out.splitlines() if "::" in line)


def _skills() -> int:
    total = 0
    for card in sorted((ROOT / "domains").glob("*/card.py")):
        mod_name = ".".join(card.relative_to(ROOT).with_suffix("").parts)
        try:
            mod = importlib.import_module(mod_name)
            card_obj = mod.build_card("http://127.0.0.1:1")
            total += len(card_obj.skills)
        except Exception:  # noqa: BLE001
            continue
    return total


def _dev_services() -> int:
    tree = ast.parse((ROOT / "scripts" / "dev_all.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "SERVICES" for t in node.targets
        ):
            return len(node.value.elts)
    return 0


def _compose() -> tuple[int, int]:
    import yaml

    data = yaml.safe_load(
        (ROOT / "infra" / "docker-compose.yml").read_text(encoding="utf-8")
    )
    services = data.get("services") or {}
    infra = {"postgres"}
    return len(services), len(services) - len(infra & set(services))


def _loc() -> tuple[int, int, int, int, int]:
    skip = {".venv", ".data", ".git", "__pycache__", "node_modules"}
    py_files = [
        p for p in ROOT.rglob("*.py")
        if not any(part in skip for part in p.parts)
    ]
    web_files = [
        p for p in (ROOT / "console" / "static").rglob("*")
        if p.is_file() and p.suffix in {".html", ".js", ".css"}
    ]

    def _lines(p: Path) -> int:
        try:
            content = p.read_text(encoding="utf-8", errors="ignore")
            return content.count("\n") + (1 if content else 0)
        except OSError:
            return 0

    py_loc = sum(_lines(p) for p in py_files)
    web_loc = sum(_lines(p) for p in web_files)
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, timeout=60,
    ).stdout.splitlines()
    return len(py_files), py_loc, len(web_files), web_loc, len(tracked)


def collect() -> dict[str, int]:
    compose_total, compose_app = _compose()
    py_files, py_lines, web_files, web_lines, tracked = _loc()
    return {
        "tests": _pytest_count(),
        "skills": _skills(),
        "dev_services": _dev_services(),
        "compose_services": compose_total,
        "compose_app_services": compose_app,
        "py_files": py_files,
        "py_loc": py_lines,
        "web_files": web_files,
        "web_loc": web_lines,
        "tracked_files": tracked,
    }


def _render(stats: dict[str, int]) -> str:
    return "\n".join(f"- {k}: {v}" for k, v in stats.items())


def _apply(
    path: Path, stats: dict[str, int], write: bool,
) -> tuple[list[str], list[str]]:
    """返回 (硬失败列表, 警告列表)。非核心指标仅警告，--write 时一并刷新。"""
    if not path.exists():
        return [], []
    text = path.read_text(encoding="utf-8")
    problems: list[str] = []
    warnings: list[str] = []

    def repl(match: re.Match) -> str:
        key, expected = match.group(1), match.group(2).strip()
        actual = str(stats.get(key, ""))
        if expected != actual:
            entry = f"{path.name}:{key} 文档={expected} 实际={actual}"
            (problems if key in CHECK_STRICT else warnings).append(entry)
            return f"<!-- STATS:{key} -->{actual}<!-- /STATS -->"
        return match.group(0)

    new = MARK_RE.sub(repl, text)
    if write and new != text:
        path.write_text(new, encoding="utf-8")
    return problems, warnings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    stats = collect()
    print(_render(stats))
    if not (args.check or args.write):
        return 0
    problems: list[str] = []
    warnings: list[str] = []
    for path in MARK_FILES:
        hard, soft = _apply(path, stats, args.write)
        problems.extend(hard)
        warnings.extend(soft)
    if warnings:
        print("\n非核心指标（仅警告，--write 可刷新）：")
        for item in warnings:
            print(" ~", item)
    if problems:
        print("\n核心数字不一致：")
        for problem in problems:
            print(" -", problem)
        return 1 if args.check else 0
    print("\n文档数字与仓库一致 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
