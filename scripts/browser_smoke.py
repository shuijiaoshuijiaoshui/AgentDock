"""工作台浏览器冒烟：真实鼠标/键盘事件（CDP），不用 evaluate 代点。

依赖：本机 Chrome/Edge（自动探测）+ 已启动的 dev_all 栈（console :8307）。
用法：
    uv run python scripts/browser_smoke.py
    uv run python scripts/browser_smoke.py --base http://127.0.0.1:8307
    uv run python scripts/browser_smoke.py --user agent   # 坐席视角（无统计 tab）

默认用 supervisor 跑全流程（队列 + 摘要 + 工单 + 知识 + 统计 + 审批可见）；
agent 的「只见自己接管」作用域由 tests/test_workbench_e2e.py 断言。

脚本通过 Chrome DevTools Protocol 的 Input.dispatchMouseEvent /
Input.insertText 发送真实输入事件；Runtime.evaluate 只用于读状态与量坐标。
覆盖：坐席登录 → 队列 → 选中会话 → 摘要 → 接管 → 回复 → 内部备注 →
驳回并编辑 → 工单创建 → trace 展开 → 知识库编辑 → 统计 → 交还 AI。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import httpx

CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def find_browser() -> str:
    for path in CANDIDATES:
        if Path(path).is_file():
            return path
    fallback = shutil.which("chrome") or shutil.which("msedge")
    if fallback:
        return fallback
    raise SystemExit("未找到 Chrome/Edge，无法运行浏览器冒烟")


def free_port() -> int:
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class CDP:
    """极简 CDP 客户端：同步 websocket（websockets.sync）。"""

    def __init__(self, ws_url: str) -> None:
        from websockets.sync.client import connect

        self.ws = connect(ws_url, open_timeout=20, max_size=20 * 1024 * 1024)
        self.next_id = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 20):
        self.next_id += 1
        msg_id = self.next_id
        self.ws.send(json.dumps({"id": msg_id, "method": method,
                                 "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = self.ws.recv(timeout=max(0.1, deadline - time.time()))
            data = json.loads(raw)
            if data.get("id") == msg_id:
                if "error" in data:
                    raise RuntimeError(f"{method} failed: {data['error']}")
                return data.get("result") or {}
            # 忽略事件（loadEventFired 等）
        raise TimeoutError(method)

    def eval(self, expression: str):
        result = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        })
        if result.get("exceptionDetails"):
            raise RuntimeError(result["exceptionDetails"])
        return result.get("result", {}).get("value")

    def wait_for(self, expression: str, timeout: float = 20) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.eval(expression):
                    return True
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.25)
        return False

    def click(self, selector: str, *, index: int = 0) -> None:
        box = self.eval(
            "(() => {"
            f" const list = document.querySelectorAll({json.dumps(selector)});"
            f" const el = list[{index}];"
            " if (!el) return null;"
            " el.scrollIntoView({block:'center', inline:'center'});"
            " const r = el.getBoundingClientRect();"
            " return {x: r.x + r.width/2, y: r.y + r.height/2,"
            "         w: r.width, h: r.height};"
            "})()"
        )
        if not box or box["w"] <= 0 or box["h"] <= 0:
            raise AssertionError(f"元素不可点击：{selector}[{index}]")
        x, y = round(box["x"]), round(box["y"])
        for kind in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", {
                "type": kind, "x": x, "y": y, "button": "left",
                "buttons": 1, "clickCount": 1,
            })

    def type(self, text: str) -> None:
        self.call("Input.insertText", {"text": text})

    def press_enter(self) -> None:
        for kind in ("rawKeyDown", "char", "keyUp"):
            params = {
                "type": kind, "key": "Enter", "code": "Enter",
                "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13,
            }
            if kind == "char":
                params["text"] = "\r"
            self.call("Input.dispatchKeyEvent", params)

    def select_all(self) -> None:
        self.call("Input.dispatchKeyEvent", {
            "type": "rawKeyDown", "key": "a", "code": "KeyA",
            "windowsVirtualKeyCode": 65, "modifiers": 2,
        })
        self.call("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": "a", "code": "KeyA",
            "windowsVirtualKeyCode": 65, "modifiers": 2,
        })

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


class Smoke:
    def __init__(self, base: str, user: str = "supervisor") -> None:
        self.base = base.rstrip("/")
        self.user = user
        self.proc: subprocess.Popen | None = None
        self.cdp: CDP | None = None
        self.profile = tempfile.mkdtemp(prefix="agentdock-smoke-")

    # ---------- 启动浏览器 ----------
    def start_browser(self) -> None:
        port = free_port()
        browser = find_browser()
        args = [
            browser, "--headless=new", f"--remote-debugging-port={port}",
            f"--user-data-dir={self.profile}", "--no-first-run",
            "--no-default-browser-check", "--disable-gpu",
            "--window-size=1400,900", f"{self.base}/",
        ]
        self.proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 30
        target = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/list", timeout=2,
                ) as resp:
                    pages = json.loads(resp.read().decode())
                target = next(
                    (p for p in pages if p.get("type") == "page"
                     and p.get("webSocketDebuggerUrl")), None,
                )
                if target:
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.4)
        if not target:
            raise SystemExit("浏览器调试端口未就绪")
        self.cdp = CDP(target["webSocketDebuggerUrl"])
        self.cdp.call("Page.enable")
        self.cdp.call("Runtime.enable")

    def stop_browser(self) -> None:
        if self.cdp:
            self.cdp.close()
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)

    # ---------- 断言 ----------
    def check(self, cond: bool, step: str) -> None:
        if not cond:
            raise AssertionError(step)
        print(f"[ok] {step}")

    # ---------- 场景 ----------
    def ensure_pending(self) -> None:
        """通过真实客户 API 造一个待接管会话（UI 只负责坐席侧）。"""
        with httpx.Client(base_url=self.base, trust_env=False, timeout=60) as c:
            login = c.post("/api/auth/login", json={
                "tenant_id": "t_demo", "username": "customer",
                "password": "demo123", "surface": "customer",
            })
            login.raise_for_status()
            csrf = login.json()["csrf_token"]
            sent = c.post("/api/customer/send",
                          headers={"X-CSRF-Token": csrf},
                          json={"text": "转人工"})
            sent.raise_for_status()
            cid = sent.json()["conversation_id"]
            c.post(f"/api/customer/conversations/{cid}/handoff",
                   headers={"X-CSRF-Token": csrf})
        print(f"[ok] 造数：待接管会话 {cid[:8]}")

    def login(self) -> None:
        cdp = self.cdp
        assert cdp
        cdp.wait_for("document.readyState === 'complete'")
        cdp.wait_for("!!document.getElementById('login-btn')", 15)
        # 演示账号默认是 agent；需要 supervisor 时用真实键盘改写账号框
        current = cdp.eval("document.getElementById('username').value")
        if current != self.user:
            cdp.click("#username")
            cdp.select_all()
            cdp.type(self.user)
        cdp.click("#login-btn")
        self.check(cdp.wait_for(
            "document.getElementById('login').classList.contains('hidden')",
            20), "坐席登录（真实点击）")
        role_word = {"agent": "坐席", "supervisor": "主管"}[self.user]
        self.check(cdp.wait_for(
            f"document.getElementById('who').textContent.includes('{role_word}')",
            10), f"身份显示为{role_word}")

    def select_queue_item(self) -> str:
        cdp = self.cdp
        assert cdp
        self.check(cdp.wait_for(
            "document.querySelectorAll('.q-item').length > 0", 20), "队列加载")
        conv_id = cdp.eval(
            "document.querySelector('.q-item').dataset.convId")
        cdp.click(".q-item")
        self.check(cdp.wait_for(
            "document.getElementById('chat-title').textContent.includes('会话')",
            20), "选中队列会话")
        self.check(cdp.wait_for(
            "document.getElementById('summary-body').innerText.length > 10",
            20), "会话摘要加载")
        self.check(cdp.wait_for(
            "document.getElementById('side-360').innerText.includes('客户资料')",
            20), "客户 360 加载")
        self.check(cdp.eval(
            "document.getElementById('side-360').innerText.includes('授信额度')"),
            "360 含授信/账期")
        return conv_id

    def takeover(self) -> None:
        cdp = self.cdp
        assert cdp
        cdp.click("#btn-takeover")
        self.check(cdp.wait_for(
            "document.getElementById('chat-badge').textContent.includes('人工')",
            20), "接管会话（状态变人工）")
        self.check(cdp.eval(
            "document.getElementById('agent-input').disabled === false"),
            "接管后回复框可用")

    def reply(self) -> None:
        cdp = self.cdp
        assert cdp
        cdp.click("#agent-input")
        cdp.type("浏览器冒烟：您好，我是坐席，正在为您处理。")
        cdp.click("#btn-send")
        self.check(cdp.wait_for(
            "document.getElementById('chat').innerText.includes('浏览器冒烟')",
            20), "坐席回复发送")
        with httpx.Client(base_url=self.base, trust_env=False, timeout=30) as c:
            login = c.post("/api/auth/login", json={
                "tenant_id": "t_demo", "username": "customer",
                "password": "demo123", "surface": "customer",
            })
            login.raise_for_status()
            headers = {"Authorization": f"Bearer {login.json()['token']}"}
            msgs = c.get("/api/customer/conversation", headers=headers).json()
            cid = msgs["conversation"]["id"]
            history = c.get(
                f"/api/conversations/{cid}/messages", headers=headers,
            ).json()
        self.check(any("浏览器冒烟" in (m.get("content") or "")
                       and m.get("role") == "agent" for m in history),
                   "客户侧收到坐席消息")

    def note(self) -> None:
        cdp = self.cdp
        assert cdp
        cdp.click("#btn-note")
        self.check(cdp.wait_for(
            "!document.getElementById('note-panel').classList.contains('hidden')",
            10), "打开内部备注")
        cdp.click("#note-input")
        cdp.type("浏览器冒烟备注：客户要求今日回复")
        cdp.click("#note-add")
        self.check(cdp.wait_for(
            "document.getElementById('note-list').innerText.includes('浏览器冒烟备注')",
            20), "内部备注保存")

    def reject_answer(self) -> None:
        cdp = self.cdp
        assert cdp
        self.check(cdp.wait_for(
            "document.querySelector('#chat .mini-link') !== null", 15),
            "AI 答复出现驳回入口")
        cdp.click("#chat .mini-link")
        self.check(cdp.wait_for(
            "document.querySelectorAll('.reject-editor').length === 1", 10),
            "打开驳回编辑器")
        cdp.click(".reject-editor textarea")
        cdp.select_all()
        cdp.type("浏览器冒烟：已为您转人工，请描述问题。")
        cdp.click(".reject-editor input")
        cdp.type("话术缺少坐席姓名")
        cdp.click(".reject-editor .row button")
        self.check(cdp.wait_for(
            "document.getElementById('chat').innerText.includes("
            "'已为您转人工，请描述问题')", 20), "驳回并发送修改答复")

    def ticket(self) -> None:
        cdp = self.cdp
        assert cdp
        cdp.click('.side-tab[data-side="tickets"]')
        self.check(cdp.wait_for(
            "!document.getElementById('side-tickets').classList.contains('hidden')",
            10), "打开工单面板")
        cdp.click("#side-tickets .side-form input", index=0)
        cdp.type("SKU-A001")
        cdp.click("#side-tickets .side-form input", index=1)
        cdp.type("按键失灵")
        cdp.click("#side-tickets .side-form textarea")
        cdp.type("浏览器冒烟：仓库检测")
        cdp.click("#side-tickets .side-form button")
        self.check(cdp.wait_for(
            "document.getElementById('side-tickets').innerText.includes('浏览器冒烟')"
            " || document.getElementById('toast').textContent.includes('工单已创建')",
            20), "创建工单")
        # 分派：每张工单卡片最后一个按钮是「分派」，默认 warehouse
        cdp.eval(
            "(() => {"
            " const rows = document.querySelectorAll('#side-tickets .side-item .row');"
            " if (!rows.length) return null;"
            " const btns = rows[0].querySelectorAll('button');"
            " btns[btns.length - 1].scrollIntoView({block:'center'});"
            " return null; })()"
        )
        cdp.click("#side-tickets .side-item .row button", index=1)
        self.check(cdp.wait_for(
            "document.getElementById('toast').textContent.includes('已分派')", 20),
            "工单分派到 warehouse")

    def trace(self) -> None:
        cdp = self.cdp
        assert cdp
        cdp.click("#trace-panel summary")
        cdp.wait_for("document.getElementById('trace-panel').open === true", 5)
        cdp.click(".trace-run summary")
        self.check(cdp.wait_for(
            "document.querySelectorAll('.trace-step').length > 0", 10),
            "展开 Agent trace 步骤")

    def knowledge(self) -> None:
        cdp = self.cdp
        assert cdp
        cdp.click('.side-tab[data-side="knowledge"]')
        self.check(cdp.wait_for(
            "document.querySelectorAll('#side-knowledge .side-item').length > 0",
            15), "知识库列表加载")
        cdp.click("#side-knowledge .side-item .row button")
        self.check(cdp.wait_for(
            "document.querySelectorAll('#side-knowledge .side-form input').length > 0",
            10), "打开知识编辑表单")
        cdp.click("#side-knowledge .side-form textarea")
        cdp.type("浏览器冒烟补充：退款以审批完成时间为起点。")
        cdp.click("#side-knowledge .side-form button")
        self.check(cdp.wait_for(
            "document.getElementById('toast').textContent.includes('已保存')", 20),
            "保存知识版本")


    def assist(self) -> None:
        """AI 查询：问主 Agent + 调工具面板（真实点击/输入）。"""
        cdp = self.cdp
        assert cdp
        cdp.click('.side-tab[data-side="assist"]')
        self.check(cdp.wait_for(
            "!!document.getElementById('assist-input')", 15), "打开 AI 查询面板")
        cdp.click("#assist-input")
        cdp.type("查询订单 SO-2001 的状态")
        cdp.click("#assist-send")
        self.check(cdp.wait_for(
            "document.getElementById('assist-list').innerText.includes('SO-2001')",
            30), "AI 查询返回订单信息")
        # 工具面板：第一个工具（customer.query）默认预填当前客户
        cdp.click("#tool-list .tool-card .row button")
        self.check(cdp.wait_for(
            "(() => { const n = document.querySelector('#tool-list .tool-result');"
            " return n && !n.classList.contains('hidden')"
            " && !n.textContent.includes('调用中'); })()", 30),
            "工具面板调用返回")
        self.check(cdp.eval(
            "document.querySelector('#tool-list .tool-result').textContent"
            ".includes('王小明')"), "工具结果含客户信息")

    def stats(self) -> None:
        cdp = self.cdp
        assert cdp
        visible = cdp.eval(
            "!document.getElementById('tab-stats').classList.contains('hidden')")
        if not visible:
            print("[skip] 当前角色无统计权限（预期：仅 supervisor/admin）")
            return
        cdp.click('.side-tab[data-side="stats"]')
        self.check(cdp.wait_for(
            "document.querySelectorAll('#side-stats .stat-card').length >= 8", 20),
            "统计看板加载")

    def return_to_ai(self) -> None:
        cdp = self.cdp
        assert cdp
        cdp.click("#btn-return")
        self.check(cdp.wait_for(
            "document.getElementById('chat-badge').textContent.includes('AI')", 20),
            "交还 AI（状态回 AI）")

    def run(self) -> None:
        self.ensure_pending()
        self.start_browser()
        try:
            self.login()
            conv_id = self.select_queue_item()
            self.takeover()
            self.reply()
            self.note()
            self.reject_answer()
            self.ticket()
            self.trace()
            self.assist()
            self.knowledge()
            self.stats()
            self.return_to_ai()
            print(f"\n浏览器冒烟全部通过 ✔（会话 {conv_id[:8]}）")
        finally:
            self.stop_browser()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8307")
    parser.add_argument("--user", default="supervisor",
                        choices=("supervisor", "agent"))
    args = parser.parse_args()
    try:
        Smoke(args.base, args.user).run()
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
