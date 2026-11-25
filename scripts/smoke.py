"""全链路冒烟：编排 Server → 域 Server → Artifact 业务字段。

前置：scripts/dev_all.py 已把服务拉起（或手动起 orchestrator:8300 + 各域）。
用法：uv run python scripts/smoke.py

1A 起所有调用都带验签身份；客户侧场景用显式 customer_id metadata，
并断言 Artifact 业务字段（不只断言 Task completed）。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from a2a.types import TaskState

from shared.a2a_client import delegate
from shared.auth import ALL_SCOPES, create_token, decode_token
from shared.config import get_settings
from shared.tracing import new_trace_id


def _merch(final: dict) -> dict:
    return final["artifacts"]["merchandising"]["data"]


def _order(final: dict) -> dict:
    return final["artifacts"]["order"]["data"]


CASES = [
    (
        "商品查询", "查询 SKU-A001 的商品详情", "CU-1001",
        lambda final: _merch(final)["name"] == "无线鼠标 Pro",
    ),
    (
        "商品对比", "对比 SKU-A001,SKU-A002", "CU-1001",
        lambda final: len(_merch(final)["matrix"]) == 2,
    ),
    (
        "到手价估算", "SKU-A002 的到手价是多少", "CU-1001",
        lambda final: _merch(final)["estimate"] is True,
    ),
    (
        "三域并行·保修判定", "SKU-A001 坏了，保修内还是保修外", "CU-1001",
        lambda final: (
            final["artifacts"]["aftersale"]["data"]["eligible"] is True
            and final["artifacts"]["policy"]["data"]["clauses"]
        ),
    ),
    (
        "政策咨询", "退货政策是什么", "CU-1001",
        lambda final: final["artifacts"]["policy"]["data"]["clauses"][0]["clause_id"]
        == "CLS-001",
    ),
    (
        "售后资格", "SKU-A002 还在保修期吗", "CU-1004",
        lambda final: final["artifacts"]["aftersale"]["data"]["eligible"] is False,
    ),
    (
        "跨域仲裁", "SO-2001 实付多少钱", "CU-1001",
        lambda final: (
            _order(final)["order_no"] == "SO-2001"
            and final["artifacts"]["finance"]["data"]["amount"] == 199.0
        ),
    ),
    (
        "nomatch 拒答", "帮我发一条朋友圈", "CU-1001",
        lambda final: "超出" in final.get("final_text", ""),
    ),
    (
        "smalltalk", "你好", "CU-1001",
        lambda final: bool(final.get("final_text")),
    ),
]


async def run_case(base: str, auth, name: str, text: str, customer_id: str,
                   check) -> tuple[bool, str]:  # noqa: ANN001
    r = await delegate(
        base, text,
        metadata={"customer_id": customer_id, "trace_id": new_trace_id()},
        timeout=60, auth=auth,
    )
    if r.state == TaskState.TASK_STATE_INPUT_REQUIRED and r.status_data:
        print(f"  澄清要求：{r.status_data.get('question', r.status_data)}")
        r = await delegate(
            base, "都不是",
            task_id=r.task_id, context_id=r.context_id,
            metadata={"customer_id": customer_id, "trace_id": new_trace_id()},
            timeout=60, auth=auth,
        )
    final = r.data_parts[0] if r.data_parts else {}
    state_ok = r.state == TaskState.TASK_STATE_COMPLETED
    try:
        biz_ok = bool(check(final))
    except Exception as exc:  # noqa: BLE001
        biz_ok = False
        print(f"  业务断言异常：{exc}")
    ok = state_ok and biz_ok
    print(
        f"[{name}] {'✔' if ok else '✘'} "
        f"state={TaskState.Name(r.state)} biz_field={'✔' if biz_ok else '✘'}"
    )
    if r.text:
        print(f"  回答：{r.text}")
    return ok, r.task_id


async def main() -> int:  # noqa: ANN201
    s = get_settings()
    base = f"http://127.0.0.1:{s.orchestrator_port}"
    token = create_token(
        tenant_id="t_demo", actor_id="smoke", role="service",
        scopes=ALL_SCOPES, token_type="service",
    )
    auth = decode_token(token)

    results: list[bool] = []
    for name, text, customer_id, check in CASES:
        ok, _ = await run_case(base, auth, name, text, customer_id, check)
        results.append(ok)

    # 澄清回填链路：模糊输入 → input-required → 回填商品查询
    print("\n--- 澄清回填链路 ---")
    r = await delegate(
        base, "帮我处理一下那个东西",
        metadata={"customer_id": "CU-1001", "trace_id": new_trace_id()},
        timeout=60, auth=auth,
    )
    if r.state == TaskState.TASK_STATE_INPUT_REQUIRED:
        print("  ✔ 任务暂停在 input-required，回填答复……")
        r2 = await delegate(
            base, "查询 SKU-A001 的商品详情",
            task_id=r.task_id, context_id=r.context_id,
            metadata={"customer_id": "CU-1001", "trace_id": new_trace_id()},
            timeout=60, auth=auth,
        )
        final = r2.data_parts[0] if r2.data_parts else {}
        try:
            ok = (
                r2.state == TaskState.TASK_STATE_COMPLETED
                and _merch(final)["name"] == "无线鼠标 Pro"
            )
        except Exception:  # noqa: BLE001
            ok = False
        print(f"  回填后 state={TaskState.Name(r2.state)} {'✔' if ok else '✘'}")
        if r2.text:
            print(f"  回答：{r2.text}")
        results.append(ok)
    else:
        print(f"  ✘ 未进入 input-required（state={TaskState.Name(r.state)}）")
        results.append(False)

    n_ok = sum(1 for r in results if r)
    print(f"\n冒烟结果：{n_ok}/{len(results)} 通过")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
