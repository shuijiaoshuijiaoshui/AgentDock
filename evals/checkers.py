"""确定性 rubric 检查器库。

每个 checker：fn(ctx: RunContext, args: dict) -> (passed: bool, evidence: str)。
纯函数、不重试、不猜——检查的是任务执行后收集到的证据（回复/产物/审计/快照）。
"""

from __future__ import annotations

from . import evidence as ev


def _dig(obj, path: str):  # noqa: ANN001
    """'finance.0.amount' → obj["finance"][0]["amount"]；缺失返回 _MISS。"""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return _MISS
        elif isinstance(cur, dict):
            if part not in cur:
                return _MISS
            cur = cur[part]
        else:
            return _MISS
    return cur


class _Miss:
    def __repr__(self) -> str:  # pragma: no cover
        return "<missing>"


_MISS = _Miss()


# -- 回复文本 -----------------------------------------------------------------

def reply_contains(ctx, args: dict) -> tuple[bool, str]:  # noqa: ANN001
    text = ctx.reply_text()
    if args.get("all"):
        miss = [w for w in args["all"] if w not in text]
        return (not miss, f"回复缺少 {miss}" if miss else f"回复含全部关键词 {args['all']}")
    hits = [w for w in args.get("any", []) if w in text]
    return (bool(hits), f"命中 {hits}" if hits else f"回复未含任一关键词 {args.get('any')}")


def reply_not_contains(ctx, args: dict) -> tuple[bool, str]:  # noqa: ANN001
    text = ctx.reply_text()
    leak = [w for w in args.get("any", []) if w in text]
    return (not leak, f"回复泄露 {leak}" if leak else f"回复未泄露 {args.get('any')}")


# -- A2A 产物 / 状态 -----------------------------------------------------------

def state_completed(ctx, args: dict) -> tuple[bool, str]:  # noqa: ANN001
    ok = all(r.get("completed") for r in ctx.a2a_results) if ctx.a2a_results \
        else False
    states = [r.get("state") for r in ctx.a2a_results]
    return ok, f"A2A states={states}"


def artifacts_set(ctx, args: dict) -> tuple[bool, str]:  # noqa: ANN001
    arts = set(ctx.a2a_last()["artifacts"].keys()) if ctx.a2a_results else set()
    want = set(args.get("equals", []))
    ok = arts == want
    return ok, f"artifacts={sorted(arts)} 期望={sorted(want)}"


def artifact_field(ctx, args: dict) -> tuple[bool, str]:  # noqa: ANN001
    arts = ctx.a2a_last()["artifacts"] if ctx.a2a_results else {}
    got = _dig(arts, args["domain"] + "." + args["path"])
    want = args.get("equals")
    ok = got is not _MISS and got == want
    return ok, f"{args['domain']}.{args['path']}={got!r} 期望={want!r}"


def decision_log_has(ctx, args: dict) -> tuple[bool, str]:  # noqa: ANN001
    logs = ctx.a2a_last().get("decision_log", []) if ctx.a2a_results else []
    for e in logs:
        if all(e.get(k) == v for k, v in args.items() if k != "in"):
            return True, f"decision_log 命中 {args}: {e}"
    return False, f"decision_log 未命中 {args}；实际={logs}"


# -- 业务库快照 -----------------------------------------------------------------

def db_count(ctx, args: dict) -> tuple[bool, str]:  # noqa: ANN001
    n = ev.db_count(ctx.stack.business_db, args["table"], args.get("where"))
    want = args["equals"]
    return n == want, f"{args['table']} 行数={n} 期望={want} where={args.get('where')}"


def db_field(ctx, args: dict) -> tuple[bool, str]:  # noqa: ANN001
    got = ev.db_field(ctx.stack.business_db, args["table"], args["where"],
                      args["field"])
    want = args.get("equals")
    return got == want, (f"{args['table']}.{args['field']}={got!r} "
                         f"期望={want!r} where={args['where']}")


def refund_count(ctx, args: dict) -> tuple[bool, str]:  # noqa: ANN001
    n = ev.db_count(ctx.stack.business_db, "refunds",
                    {"tenant_id": ctx.task.get("tenant", "t_demo"),
                     **args.get("where", {})})
    want = args["equals"]
    return n == want, f"refunds 行数={n} 期望={want}"


# -- 待办 / 卡片 / HTTP ---------------------------------------------------------

def pending_actions_contain(ctx, args: dict) -> tuple[bool, str]:  # noqa: ANN001
    pas = ctx.pending_actions or []
    for pa in pas:
        if args.get("type") and pa.get("type") != args["type"]:
            continue
        # 客户侧待办不带 skill（内部字段）；只有带了才参与匹配
        if args.get("skill") and pa.get("skill") and \
                args["skill"] not in str(pa.get("skill")):
            continue
        return True, f"待办命中 {args}: {pa}"
    return False, f"待办未命中 {args}；实际={pas}"


def http_status_in(ctx, args: dict) -> tuple[bool, str]:  # noqa: ANN001
    ex = ctx.exchange(args["exchange"])
    if ex is None:
        return False, f"未找到交换 {args['exchange']}"
    ok = ex["status"] in args.get("values", [])
    return ok, f"{args['exchange']} status={ex['status']} 期望∈{args.get('values')}"


def exchange_json_path(ctx, args: dict) -> tuple[bool, str]:  # noqa: ANN001
    ex = ctx.exchange(args["exchange"])
    if ex is None:
        return False, f"未找到交换 {args['exchange']}"
    got = _dig(ex.get("json") or {}, args["path"])
    want = args.get("equals")
    ok = got is not _MISS and got == want
    return ok, f"{args['exchange']}.{args['path']}={got!r} 期望={want!r}"


def reply_json_contains(ctx, args: dict) -> tuple[bool, str]:  # noqa: ANN001
    """console send 回复 JSON 的任意字段含子串（reply/cards/pending_actions…）。"""
    text = str(ctx.last_send_json or "")
    hits = [w for w in args.get("any", []) if w in text]
    return (bool(hits), f"命中 {hits}" if hits else f"未含任一 {args.get('any')}")


def card_present(ctx, args: dict) -> tuple[bool, str]:  # noqa: ANN001
    cards = (ctx.last_send_json or {}).get("cards") or []
    want = args.get("type")
    ok = any(c.get("type") == want for c in cards)
    return ok, (f"卡片含 {want}" if ok else
                f"卡片无 {want}；实际={[c.get('type') for c in cards]}")


# -- 证据通道（每个任务的标准 rubric） -------------------------------------------

def trace_services_contain(ctx, args: dict) -> tuple[bool, str]:  # noqa: ANN001
    got = ctx.span_service_set
    want = set(args.get("services", []))
    ok = want <= got
    return ok, (f"span 服务={sorted(got)} 覆盖期望={sorted(want)}"
                if ok else f"span 服务={sorted(got)} 缺少={sorted(want - got)}")


def audit_chain_ok(ctx, args: dict) -> tuple[bool, str]:  # noqa: ANN001
    r = ctx.audit_verify
    ok = bool(r.get("ok"))
    detail = f"审计链 verify ok={ok} total={r.get('total')}"
    if not ok:
        detail += f" broken_at={r.get('broken_at')} reason={r.get('reason')}"
    return ok, detail


def trace_id_correlated(ctx, args: dict) -> tuple[bool, str]:  # noqa: ANN001
    """trace_id 三方贯通：审计事件里有它，span 里也有它。"""
    tids = {t for t in ctx.trace_ids if t}
    if not tids:
        return False, "本任务未采集到 trace_id"
    audit_tids = ev.audit_trace_ids(ctx.audit_events)
    span_tids = {s.get("trace_id", "") for s in ctx.spans}
    both = tids & audit_tids & span_tids
    return bool(both), (f"trace_id 贯通 {len(both)}/{len(tids)}"
                        + ("" if both else f"；审计={len(audit_tids)} span={len(span_tids)}"))


def session_summary_exists(ctx, args: dict) -> tuple[bool, str]:  # noqa: ANN001
    row = ctx.session_summary_row
    if row is None:
        return False, "session_summaries 无行"
    if args.get("contains") and args["contains"] not in str(row.get("summary", "")):
        return False, f"摘要缺少 {args['contains']}：{row.get('summary', '')[:120]}"
    return True, f"摘要已落库 upto={row.get('upto_message_id')}"


def degraded_flag(ctx, args: dict) -> tuple[bool, str]:  # noqa: ANN001
    arts = ctx.a2a_last()["artifacts"] if ctx.a2a_results else {}
    data = arts.get(args.get("domain", "aftersale"), {}).get("data", {})
    ok = data.get("degraded") is True
    return ok, f"degraded={data.get('degraded')}"


def artifact_field_contains(ctx, args: dict) -> tuple[bool, str]:  # noqa: ANN001
    arts = ctx.a2a_last()["artifacts"] if ctx.a2a_results else {}
    got = _dig(arts, args["domain"] + "." + args["path"])
    want = args.get("contains", "")
    ok = got is not _MISS and want in str(got)
    return ok, f"{args['domain']}.{args['path']}={got!r} 应含 {want!r}"


def clarify_happened(ctx, args: dict) -> tuple[bool, str]:  # noqa: ANN001
    first = ctx.a2a_results[0] if ctx.a2a_results else {}
    cr = first.get("clarify_result") or {}
    ok = cr.get("state") == "TASK_STATE_INPUT_REQUIRED"
    return ok, (f"首轮暂停于 input-required（payload keys="
                f"{list((cr.get('status_data') or {}).keys())}）"
                if ok else f"首轮未澄清：{cr.get('state')}")


CHECKERS = {
    "reply_contains": reply_contains,
    "reply_not_contains": reply_not_contains,
    "state_completed": state_completed,
    "artifacts_set": artifacts_set,
    "artifact_field": artifact_field,
    "artifact_field_contains": artifact_field_contains,
    "decision_log_has": decision_log_has,
    "db_count": db_count,
    "db_field": db_field,
    "refund_count": refund_count,
    "pending_actions_contain": pending_actions_contain,
    "http_status_in": http_status_in,
    "exchange_json_path": exchange_json_path,
    "reply_json_contains": reply_json_contains,
    "card_present": card_present,
    "trace_services_contain": trace_services_contain,
    "audit_chain_ok": audit_chain_ok,
    "trace_id_correlated": trace_id_correlated,
    "session_summary_exists": session_summary_exists,
    "degraded_flag": degraded_flag,
    "clarify_happened": clarify_happened,
}
