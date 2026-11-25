"""check_tour.py — 导览站机械校验（零第三方依赖）。

四类检查（spec.md §三）：
1. 文件引用：页面 href/src 指向的相对路径必须存在
2. 符号引用：`xxx.py:NN [Symbol]` 形式的论断，文件必须存在、行号必须在范围内、
   符号名应出现在该文件中（缺失降为 WARN，防重构期误报）
3. 内部链接：页间相对链接可达；同页 #锚点 的 id 必须存在
4. 日期新鲜度：页脚"最后对照代码校验：YYYY-MM-DD"不得早于被描述源码的最新 mtime

用法：uv run python scripts/check_tour.py
ERROR（缺文件/行号越界/断链/过时）→ 退出码 1；WARN 只提示。
"""

import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
PAGES = ["tour.html", "journey.html", "flow.html", "swimlane.html",
         "prerequisites.html", "howto-modify.html",
         "rules-simulator.html", "modules/orchestrator.html",
         "modules/console.html", "modules/merchandising.html", "modules/shared.html"]

# 日期新鲜度扫描范围：被导览描述的源码（不含 docs/ 自身与虚拟环境）
SCAN_DIRS = ["orchestrator", "console", "domains", "shared", "scripts", "tests",
             "examples", "infra"]
SCAN_ROOT_FILES = ["README.md", "pyproject.toml", ".env.example"]

errors: list[str] = []
warns: list[str] = []


def strip_style(html: str) -> str:
    return re.sub(r"<style>.*?</style>", "", html, flags=re.S)


def find_source(relname: str) -> tuple[Path | None, bool]:
    """定位源码文件。带路径 → ROOT 相对路径直查；裸文件名 → 全库唯一匹配，
    多义返回 (None, True)（按 spec 降为 WARN）。排除 docs/.venv/.git/.data。"""
    if "/" in relname or "\\" in relname:
        p = (ROOT / relname).resolve()
        return (p, False) if p.exists() else (None, False)
    hits = [p for p in ROOT.rglob(Path(relname).name)
            if p.is_file() and not any(
                part in {".venv", ".git", ".data", "node_modules", "docs"} for part in p.parts)]
    if len(hits) == 1:
        return hits[0], False
    if len(hits) > 1:
        return None, True
    return None, False


def source_mtime() -> float:
    latest = 0.0
    for d in SCAN_DIRS:
        for p in (ROOT / d).rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts:
                latest = max(latest, p.stat().st_mtime)
    for f in SCAN_ROOT_FILES:
        p = ROOT / f
        if p.exists():
            latest = max(latest, p.stat().st_mtime)
    return latest


def main() -> int:
    src_mtime = source_mtime()
    newest_src = date.fromtimestamp(src_mtime)

    for page in PAGES:
        path = DOCS / page
        if not path.exists():
            errors.append(f"[文件] 页面缺失：docs/{page}")
            continue
        html = strip_style(path.read_text(encoding="utf-8"))
        base = path.parent

        # 1+3：href/src 相对引用 + 页间链接
        for m in re.finditer(r'(?:href|src)="([^"#]+)(#[^"]*)?"', html):
            target, frag = m.group(1), m.group(2)
            if target.startswith(("http://", "https://", "mailto:", "ws://", "wss:", "/")):
                continue
            resolved = (base / target).resolve()
            if not resolved.exists():
                errors.append(f"[文件] docs/{page} 引用不存在：{target}")
                continue
            if frag and resolved.suffix == ".html":
                frag_html = strip_style(resolved.read_text(encoding="utf-8"))
                if f'id="{frag[1:]}"' not in frag_html:
                    errors.append(f"[链接] docs/{page} → {target}{frag} 锚点不存在")

        # 3b：同页锚点
        for m in re.finditer(r'href="#([\w-]+)"', html):
            if f'id="{m.group(1)}"' not in html:
                errors.append(f"[链接] docs/{page} 同页锚点缺失：#{m.group(1)}")

        # 2：符号引用 `xxx.py:NN`（文件名可带多级路径）+ 紧随其后的符号名
        for m in re.finditer(r"((?:[\w.-]+/)*[\w-]+\.py):(\d+)(?:</b>)?\s*(?:·\s*)?([A-Za-z_]\w*)?", html):
            fname, lineno, sym = m.group(1), int(m.group(2)), m.group(3)
            if sym and m.group(0)[len(m.group(1)) + len(m.group(2)):].lstrip("</b> ·").startswith("."):
                sym = None  # 紧跟另一个 file.py 引用，不是符号
            src, ambiguous = find_source(fname)
            if src is None:
                if ambiguous:
                    warns.append(f"[歧义] docs/{page} → {fname} 全库命中多义，请改用相对路径")
                else:
                    errors.append(f"[文件] docs/{page} 引用源码不存在：{fname}")
                continue
            lines = src.read_text(encoding="utf-8").splitlines()
            if lineno > len(lines):
                errors.append(f"[行号] docs/{page} → {fname}:{lineno} 超出文件行数 {len(lines)}")
                continue
            if sym:
                tail = html[m.end(3):m.end(3) + 1] if m.group(3) else ""
                if tail in {"/", "."}:
                    sym = None  # 后随 / 或 . —— 是文件路径 token，不是符号
            if sym:
                body = "\n".join(lines[max(0, lineno - 25): min(len(lines), lineno + 5)])
                if sym not in body and sym not in src.read_text(encoding="utf-8"):
                    warns.append(f"[符号] docs/{page} → {fname}:{lineno} 附近未见符号 {sym}（可能重构，人工核对）")

        # 4：日期新鲜度
        dm = re.search(r"最后对照代码校验：(?:<b>)?(\d{4}-\d{2}-\d{2})", html)
        if not dm:
            errors.append(f"[日期] docs/{page} 页脚缺少「最后对照代码校验」戳")
        else:
            stamp = date.fromisoformat(dm.group(1))
            if stamp < newest_src:
                errors.append(f"[日期] docs/{page} 校验戳 {stamp} 早于源码最新改动 {newest_src}")

    for w in warns:
        print(f"WARN {w}")
    for e in errors:
        print(f"ERROR {e}")
    print(f"\n校验完成：{len(PAGES)} 页 · {len(errors)} error / {len(warns)} warn"
          f" · 源码最新改动 {newest_src}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
