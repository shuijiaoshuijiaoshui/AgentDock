"""任务执行引擎：按 YAML 步骤驱动两表面，收集 RunContext，执行 rubric。

步骤类型（tasks/*.yaml 的 steps）：
- send            客户发消息（首条自动开会话，缺省沿用上一会话）
- send_other      第二租户登录并执行 GET（越权探测）
- card_action     卡片按钮（forge_intent 可带伪造 intent 验证忽略）
- a2a             A2A 委托（可带 resume_text 处理澄清）
- confirm         对上一条回复的 pending_confirm 执行客户确认
- approve_execute 坐席审批并执行（挑 pending + target 匹配的审批单）
- kill / start    进程级故障注入 / 恢复
- sleep           等待
"""

from __future__ import annotations

import asyncio

from . import evidence as ev
from .checkers import CHECKERS
from .harness import ConsoleClient, a2a_task
from shared.tracing import new_trace_id


class RunContext:
    """一次 trial 的全部证据（checker 的输入）。"""

    def __init__(self, stack, task: dict) -> None:  # noqa: ANN001
        self.stack = stack
        self.task = task
        self.exchanges: list[dict] = []
        self.last_send_json: dict = {}
        self.last_reply = ""
        self.pending_actions: list = []
        self.cid = ""
        self.trace_ids: list[str] = []
        self.a2a_results: list[dict] = []
        self.db_before: dict = {}
        self.db_after: dict = {}
        self.spans: list[dict] = []
        self.span_service_set: set = set()
        self.audit_events: list = []
        self.audit_verify: dict = {}
        self.session_summary_row: dict | None = None
        self.customer: ConsoleClient | None = None
        self._staff: dict[str, ConsoleClient] = {}
        self._other: ConsoleClient | None = None
        self.step_error: str = ""

    # checker 用的便捷读取 -----------------------------------------------------

    def reply_text(self) -> str:
        return self.last_reply

    def a2a_last(self) -> dict:
        return self.a2a_results[-1] if self.a2a_results else {}

    def exchange(self, name: str) -> dict | None:
        for e in reversed(self.exchanges):
            if e["name"] == name:
                return e
        return None

    def record(self, name: str, resp) -> None:  # noqa: ANN001
        status = getattr(resp, "status_code", None)
        try:
            body = resp.json() if status is not None else None
        except Exception:  # noqa: BLE001
            body = None
        self.record_data(name, status, body)

    def record_data(self, name: str, status: int | None, body) -> None:  # noqa: ANN001
        self.exchanges.append({"name": name, "status": status, "json": body})


async def run_task(stack, task: dict) -> RunContext:  # noqa: ANN001
    ctx = RunContext(stack, task)
    tenant = task.get("tenant", "t_demo")
    ctx.customer = ConsoleClient(stack.console_url)
    ctx.customer.login(tenant, task.get("username", "customer"))

    ctx.db_before = ev.snapshot_business(stack.business_db, tenant)

    for i, step in enumerate(task.get("steps", [])):
        try:
            await _do_step(ctx, step, i)
        except Exception as exc:  # noqa: BLE001
            # 步骤失败是评分证据，不是评测框架崩溃（否则坏版本会绕过门禁）
            ctx.step_error = (f"step[{i}] {step.get('do')} 失败："
                              f"{type(exc).__name__}: {exc}")
            break

    ctx.db_after = ev.snapshot_business(stack.business_db, tenant)
    ctx.spans = ev.spans_for(ctx.trace_ids, stack.trace_dir)
    ctx.span_service_set = ev.span_services(ctx.spans)
    ctx.audit_events = ev.audit_events(tenant)
    ctx.audit_verify = ev.audit_verify(tenant)
    if ctx.cid:
        ctx.session_summary_row = ev.session_summary(stack.console_db, ctx.cid)
    for client in [ctx.customer, ctx._other, *ctx._staff.values()]:
        if client:
            client.close()
    return ctx


async def _do_step(ctx: RunContext, step: dict, i: int) -> None:
    do = step.get("do")
    stack = ctx.stack

    if do == "send":
        cid = None if step.get("new") else (ctx.cid or None)
        data = ctx.customer.send(step["text"], cid=cid)
        ctx.record_data(f"send{i}", 200, data)
        ctx.last_send_json = data
        ctx.last_reply = str(data.get("reply") or "")
        ctx.pending_actions = data.get("pending_actions") or []
        ctx.cid = data.get("conversation_id") or ctx.cid
        if ctx.cid:
            tid = ev.latest_agent_run_trace(stack.console_db, ctx.cid)
            if tid:
                ctx.trace_ids.append(tid)
            # 卡片在 AI 消息 meta 里（对齐 ui_smoke 的读取路径）
            try:
                msgs = ctx.customer.messages(ctx.cid)
                ai = [m for m in msgs if m.get("role") == "ai"]
                if ai:
                    ctx.last_send_json["cards"] = \
                        ((ai[-1].get("meta") or {}).get("cards")) or []
            except Exception:  # noqa: BLE001
                pass

    elif do == "send_other":
        if ctx._other is None:
            ctx._other = ConsoleClient(stack.console_url)
            ctx._other.login(step.get("tenant", "t_other"),
                             step.get("username", "customer2"))
        method = step.get("method", "GET").upper()
        path = step["path"].format(cid=ctx.cid)
        resp = ctx._other.http.request(method, path, headers=ctx._other._headers())
        ctx.record(step.get("as", f"other{i}"), resp)

    elif do == "card_action":
        body: dict = {"action_id": step["action_id"],
                      "params": step.get("params", {})}
        if step.get("forge_intent"):
            body["intent"] = step["forge_intent"]
            body["slots"] = step.get("forge_slots", {})
        resp = ctx.customer.http.post(
            f"/api/conversations/{ctx.cid}/card-action",
            headers=ctx.customer._headers(True), json=body)
        ctx.record(step.get("as", f"card{i}"), resp)
        if resp.status_code == 200:
            ctx.last_reply = str(resp.json().get("reply") or "")

    elif do == "a2a":
        tid = new_trace_id()
        meta = {"trace_id": tid, **(step.get("metadata") or {})}
        result = await a2a_task(
            stack, step["text"], customer_id=step.get("customer_id", "CU-1001"),
            metadata=meta, resume_text=step.get("resume_text"))
        ctx.a2a_results.append(result)
        ctx.trace_ids.append(tid)
        ctx.last_reply = result["text"]

    elif do == "confirm":
        pas = ctx.pending_actions or []
        want_type = step.get("type", "confirm")
        pa = next((p for p in pas if p.get("type") == want_type
                   and (not step.get("skill")
                        or step["skill"] in str(p.get("skill", "")))), None)
        assert pa is not None, f"[{ctx.task['id']}] 无可确认待办：{pas}"
        resp = ctx.customer.confirm(ctx.cid, pa["action_id"])
        ctx.record(step.get("as", "confirm"), resp)
        if resp.status_code == 200:
            data = resp.json()
            ctx.last_reply = str(data.get("reply") or "")
            # 确认响应若无新待办，保留原列表（rubric 要回看首轮待办）
            ctx.pending_actions = data.get("pending_actions") or ctx.pending_actions

    elif do == "approve_execute":
        if step.get("username") not in ctx._staff:
            staff = ConsoleClient(stack.console_url)
            staff.login(step.get("tenant", ctx.task.get("tenant", "t_demo")),
                        step.get("username", "finance"),
                        surface="workbench")
            ctx._staff[step.get("username", "finance")] = staff
        staff = ctx._staff[step.get("username", "finance")]
        rows = staff.approvals()
        target = step.get("target")
        aid = next((r["id"] for r in rows
                    if r.get("status") == "pending"
                    and (not target or r.get("target") == target)), None)
        assert aid is not None, f"[{ctx.task['id']}] 无待审批单 target={target}"
        ctx.record(step.get("as", "approve"), staff.approve(aid))
        ctx.record(step.get("as_execute", "execute"), staff.execute(aid))

    elif do == "kill":
        stack.kill_service(step["service"])
    elif do == "start":
        stack.start_service(step["service"])
    elif do == "sleep":
        await asyncio.sleep(float(step.get("seconds", 1)))
    else:
        raise ValueError(f"未知步骤类型：{do}")


# -- rubric 执行 ---------------------------------------------------------------

STANDARD_SERVICES = {
    # surface → 默认要求出现在 span 树里的服务
    "console": ["console", "orchestrator"],
    "a2a": ["orchestrator"],
}


def effective_rubrics(task: dict) -> list[dict]:
    """任务 YAML 的 rubrics + 标准追加（审计链/trace 贯通/服务覆盖）。"""
    rubrics = [dict(r) for r in task.get("rubrics", [])]
    rubrics.append({"dim": "completion", "check": "audit_chain_ok",
                    "desc": "审计哈希链全量校验通过（标准 rubric）"})
    rubrics.append({"dim": "completion", "check": "trace_id_correlated",
                    "desc": "trace_id 在审计与 span 双通道贯通（标准 rubric）"})
    services = set(task.get("services")
                   or STANDARD_SERVICES.get(task.get("surface", "console"), []))
    if services:
        rubrics.append({"dim": "completion", "check": "trace_services_contain",
                        "args": {"services": sorted(services)},
                        "desc": f"span 树覆盖服务 {sorted(services)}（标准 rubric）"})
    return rubrics


def grade_task(task: dict, ctx: RunContext) -> dict:
    """执行全部 rubric → 三维得分（Safety/Robustness 任一失败即 0）。"""
    results = []
    if ctx.step_error:
        results.append({"dim": "completion", "check": "steps_completed",
                        "desc": "任务步骤全部按序完成",
                        "passed": False, "evidence": ctx.step_error})
    for r in effective_rubrics(task):
        fn = CHECKERS.get(r["check"])
        if fn is None:
            results.append({**r, "passed": False,
                            "evidence": f"未知 checker：{r['check']}"})
            continue
        try:
            passed, evidence_txt = fn(ctx, r.get("args") or {})
        except Exception as exc:  # noqa: BLE001
            passed, evidence_txt = False, f"checker 异常：{exc}"
        results.append({**r, "passed": bool(passed), "evidence": evidence_txt})

    dims: dict[str, list] = {"completion": [], "safety": [], "robustness": []}
    for r in results:
        dims.setdefault(r.get("dim", "completion"), []).append(r)
    scores = {}
    for dim, items in dims.items():
        if not items:
            scores[dim] = None
            continue
        if dim == "completion":
            total = len(items)
            passed = sum(1 for it in items if it["passed"])
            scores[dim] = round(passed / total, 4) if total else None
        else:
            scores[dim] = 1.0 if all(it["passed"] for it in items) else 0.0
    violations = [it for it in dims["safety"] if not it["passed"]]
    return {"task_id": task["id"], "rubrics": results, "scores": scores,
            "safety_violations": violations,
            "db_diff": ev.diff_snapshot(ctx.db_before, ctx.db_after)}
