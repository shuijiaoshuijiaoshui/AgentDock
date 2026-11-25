"""LangGraph ↔ A2A 桥接层（技术方案 2.4 映射表的 M1 落地，六域共用）。

- LangGraphTaskExecutor：把 A2A Message 转成图输入；interrupt → input-required；
  终态 → Artifact(DataPart+TextPart)；同 taskId 回填 → Command(resume)。
- SqliteTaskStore / PostgresTaskStore：TaskStore 落库（taskId → Task 快照），
  重启后仍能复盘与续聊（AC-14）。
- build_a2a_app / build_checkpointer：a2a-sdk 1.2.x 的应用组装与状态持久化。
"""

import asyncio
import json
import logging
import sqlite3
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from google.protobuf.json_format import MessageToDict, ParseDict
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

import a2a.helpers as ah
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.tasks import TaskStore, TaskUpdater
from a2a.types import TaskState
from a2a.types.a2a_pb2 import (
    AgentCard,
    ListTasksRequest,
    ListTasksResponse,
    Task,
)

from shared.audit import audit_from_auth
from shared.auth import (
    AuthContext,
    AuthServerCallContextBuilder,
    auth_from_headers,
    reset_current_auth,
    set_current_auth,
)
from shared.config import get_settings
from shared.request_context import (
    begin_tool_events, reset_request_meta, reset_tool_events, set_request_meta,
)
from shared.tracing import start_server_span

logger = logging.getLogger(__name__)

ArtifactBuilder = Callable[[dict], tuple[dict, str]]
"""从图终态提取 (Artifact data dict, 人话摘要)。"""


def request_metadata(context: RequestContext) -> dict:
    """A2A 请求 metadata（pb2 Struct 或 dict）→ dict。"""
    m = context.metadata
    if m is None:
        return {}
    if hasattr(m, "DESCRIPTOR"):
        return MessageToDict(m, preserving_proto_field_name=True)
    return dict(m)


def _tenant_of(context: Any) -> str:
    """从 ServerCallContext / RequestContext / dict 提取 tenant（TaskStore 隔离用）。

    无租户一律返回空串，由调用方 fail-closed；不再回落 t_demo。
    """
    if context is None:
        return ""
    if isinstance(context, dict):
        return str(context.get("tenant") or context.get("tenant_id") or "")
    tenant = getattr(context, "tenant", "")
    if tenant:
        return str(tenant)
    call = getattr(context, "call_context", None)
    if call is not None and getattr(call, "tenant", ""):
        return str(call.tenant)
    state = getattr(context, "state", None)
    if isinstance(state, dict):
        auth = state.get("auth")
        if isinstance(auth, AuthContext):
            return auth.tenant_id
        if isinstance(auth, dict) and auth.get("tenant_id"):
            return str(auth["tenant_id"])
    return ""


def default_artifact_builder(state: dict) -> tuple[dict, str]:
    data = state.get("artifact") or {}
    summary = state.get("summary") or json.dumps(data, ensure_ascii=False)
    return data, summary


class LangGraphTaskExecutor(AgentExecutor):
    """把一个已编译的 LangGraph app 包成 A2A AgentExecutor。

    约定：图 state 里用 `artifact`（dict）与 `summary`（str）承载产物；
    澄清/授权用 `interrupt(payload)`，payload 会原样放进 status message 的 DataPart。
    """

    def __init__(
        self,
        graph: Any,
        *,
        domain: str,
        artifact_builder: ArtifactBuilder | None = None,
    ) -> None:
        self.domain = domain
        self.artifact_builder = artifact_builder or default_artifact_builder
        # 图在服务 lifespan 里编译（需要先进入 checkpointer 上下文），随后 bind 进来。
        self.graph = graph

    def bind(self, graph: Any) -> None:
        self.graph = graph

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id, context_id = context.task_id, context.context_id
        headers: dict = {}
        try:
            headers = (context.call_context.state or {}).get("headers") or {}
        except Exception:  # noqa: BLE001
            headers = {}
        try:
            auth: AuthContext | None = auth_from_headers(headers)
        except Exception:  # noqa: BLE001
            auth = None
        token_ctx = set_current_auth(auth)
        meta_ctx = None
        tool_ctx = begin_tool_events()
        updater: TaskUpdater | None = None
        span_cm = None
        span = None
        try:
            await event_queue.enqueue_event(
                ah.new_task(
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.TASK_STATE_SUBMITTED,
                )
            )
            req_meta = request_metadata(context)
            meta_ctx = set_request_meta(req_meta)
            trace_id = req_meta.get("trace_id", "")
            # M4（PRD 7.10）：服务端 span。父来自调用方 traceparent（无则用
            # trace_id 锚定新根，与审计/agent_runs 同号）；tracing 关闭时为 no-op。
            span_cm = start_server_span(
                f"a2a.execute {self.domain}",
                trace_id=str(trace_id),
                parent_traceparent=str(req_meta.get("traceparent", "")),
                attributes={"a2a.domain": self.domain,
                            "a2a.task_id": task_id,
                            "a2a.context_id": context_id},
            )
            span = span_cm.__enter__()
            # 审计上下文优先用调用方传入的会话 id（console conversation_id），
            # 避免同一轮同时出现 A2A contextId 与业务会话 id 两套事实。
            audit_context_id = str(req_meta.get("context_id") or context_id or "")
            audit_from_auth(
                auth, "task.submitted", target=self.domain,
                params={"task_id": task_id, "context_id": context_id},
                trace_id=str(trace_id), context_id=audit_context_id,
                task_id=str(task_id or ""),
            )
            updater = TaskUpdater(event_queue, task_id, context_id)
            if auth is None or not auth.tenant_id or not auth.actor_id:
                await updater.failed(
                    ah.new_text_message(
                        "unauthorized: verified tenant/actor required",
                        context_id=context_id, task_id=task_id,
                    )
                )
                return

            user_text = context.get_user_input() or ""
            current = context.current_task
            is_resume = bool(
                current is not None
                and current.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
            )

            # checkpointer thread 必须带租户前缀：task_id 在 A2A 层是全局的，
            # 但图状态不能跨租户混用。
            thread_id = f"{auth.tenant_id}:{task_id}"
            config = {
                "configurable": {"thread_id": thread_id},
                # 请求 metadata 全量透传（trace_id/context_id/hit_wall_count…）；
                # auth 是 token claim 的安全投影（不含原始 token）。
                "metadata": {
                    **req_meta,
                    "domain": self.domain,
                    "task_id": task_id,
                    "context_id": context_id,
                    "auth": auth.as_metadata(),
                },
            }
            graph_input: Any
            if is_resume:
                graph_input = Command(resume=user_text)
            else:
                graph_input = {"messages": [HumanMessage(content=user_text)]}

            await updater.start_work()
            audit_from_auth(
                auth, "task.working", target=self.domain,
                params={"task_id": task_id, "resume": is_resume},
                trace_id=str(trace_id), context_id=audit_context_id,
                task_id=str(task_id or ""),
            )
            async for _ in self.graph.astream(
                graph_input, config, stream_mode="updates"
            ):
                pass

            snap = await self.graph.aget_state(config)
            state = snap.values
            if span is not None and state.get("decision_log"):
                span.set_attribute(
                    "a2a.decision_log",
                    json.dumps(state["decision_log"], ensure_ascii=False)[:1500],
                )
            interrupts = getattr(snap, "tasks", []) and [
                i for t in snap.tasks for i in (t.interrupts or ())
            ]
            if interrupts:
                payload = dict(interrupts[0].value)
                audit_from_auth(
                    auth, "task.input_required", target=self.domain,
                    params={"task_id": task_id, "payload": payload},
                    trace_id=str(trace_id), context_id=audit_context_id,
                    task_id=str(task_id or ""),
                )
                await updater.requires_input(
                    ah.new_data_message(payload, context_id=context_id, task_id=task_id)
                )
                return

            data, summary = self.artifact_builder(state)
            await updater.add_artifact(
                [ah.new_data_part(data), ah.new_text_part(summary)],
                name=f"{self.domain}.result",
            )
            await updater.complete()
            audit_from_auth(
                auth, "task.completed", target=self.domain,
                params={"task_id": task_id},
                result_summary=summary[:200], trace_id=str(trace_id),
                context_id=audit_context_id, task_id=str(task_id or ""),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("domain=%s task=%s 执行失败", self.domain, task_id)
            if span is not None:
                span.record_exception(exc)
            audit_from_auth(
                auth, "task.failed", target=self.domain,
                params={"task_id": task_id}, result_summary="域内执行失败",
                context_id=audit_context_id, task_id=str(task_id or ""),
            )
            if updater is not None:
                await updater.failed(
                    ah.new_text_message(
                        "域内执行失败", context_id=context_id, task_id=task_id
                    )
                )
        finally:
            if span_cm is not None:
                span_cm.__exit__(None, None, None)
            if meta_ctx is not None:
                reset_request_meta(meta_ctx)
            reset_tool_events(tool_ctx)
            reset_current_auth(token_ctx)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        headers: dict = {}
        try:
            headers = (context.call_context.state or {}).get("headers") or {}
        except Exception:  # noqa: BLE001
            headers = {}
        try:
            auth = auth_from_headers(headers)
        except Exception:  # noqa: BLE001
            auth = None
        await event_queue.enqueue_event(
            ah.new_task(
                task_id=context.task_id,
                context_id=context.context_id,
                state=TaskState.TASK_STATE_SUBMITTED,
            )
        )
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()
        audit_from_auth(
            auth, "task.canceled", target=self.domain,
            params={"task_id": context.task_id},
            context_id=str(context.context_id or ""),
            task_id=str(context.task_id or ""),
        )


# ---------------------------------------------------------------------------
# TaskStore：Task 快照落库（sqlite / postgres），重启可复盘
# ---------------------------------------------------------------------------


class SqliteTaskStore(TaskStore):
    """基于 SQLite 的 TaskStore（单机开发态）。同步驱动 + 线程锁，调用量小，够用。"""

    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS a2a_tasks (
                    tenant_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    context_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (tenant_id, task_id)
                )
                """
            )
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(a2a_tasks)")}
            if "tenant_id" not in cols:
                self._conn.execute(
                    "ALTER TABLE a2a_tasks ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 't_demo'"
                )
                self._conn.execute(
                    "UPDATE a2a_tasks SET tenant_id='t_demo' "
                    "WHERE tenant_id IS NULL OR tenant_id=''"
                )
            # 旧表是 task_id 单主键：重建为 (tenant_id, task_id) 复合主键。
            pk_cols = [
                r[1] for r in self._conn.execute("PRAGMA table_info(a2a_tasks)")
                if r[5]
            ]
            if pk_cols and pk_cols != ["tenant_id", "task_id"]:
                self._conn.execute("ALTER TABLE a2a_tasks RENAME TO a2a_tasks_old")
                self._conn.execute(
                    """
                    CREATE TABLE a2a_tasks (
                        tenant_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        context_id TEXT NOT NULL,
                        state TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                        PRIMARY KEY (tenant_id, task_id)
                    )
                    """
                )
                self._conn.execute(
                    "INSERT INTO a2a_tasks(tenant_id, task_id, context_id, state, payload, updated_at) "
                    "SELECT tenant_id, task_id, context_id, state, payload, updated_at "
                    "FROM a2a_tasks_old"
                )
                self._conn.execute("DROP TABLE a2a_tasks_old")

    @staticmethod
    def _tenant(context: Any) -> str:
        tenant = _tenant_of(context)
        if not tenant:
            raise ValueError("missing verified tenant for task store")
        return tenant

    def _exec(self, sql: str, args: tuple) -> None:
        with self._lock, self._conn:
            self._conn.execute(sql, args)

    async def save(self, task: Task, context: Any) -> None:
        d = MessageToDict(task, preserving_proto_field_name=True)
        self._exec(
            "INSERT INTO a2a_tasks(tenant_id, task_id, context_id, state, payload) "
            "VALUES (?,?,?,?,?) ON CONFLICT(tenant_id, task_id) DO UPDATE SET "
            "state=excluded.state, payload=excluded.payload, "
            "updated_at=(datetime('now'))",
            (self._tenant(context), task.id, task.context_id,
             TaskState.Name(task.status.state), json.dumps(d)),
        )

    async def get(self, task_id: str, context: Any) -> Task | None:
        row = await asyncio.to_thread(self._get_row, task_id, self._tenant(context))
        if row is None:
            return None
        return ParseDict(json.loads(row), Task())

    def _get_row(self, task_id: str, tenant_id: str) -> str | None:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "SELECT payload FROM a2a_tasks WHERE task_id=? AND tenant_id=?",
                (task_id, tenant_id),
            )
            r = cur.fetchone()
        return r[0] if r else None

    async def delete(self, task_id: str, context: Any) -> None:
        self._exec(
            "DELETE FROM a2a_tasks WHERE task_id=? AND tenant_id=?",
            (task_id, self._tenant(context)),
        )

    async def list(  # noqa: A003
        self, params: ListTasksRequest, context: Any
    ) -> ListTasksResponse:
        def _rows() -> list[str]:
            with self._lock, self._conn:
                cur = self._conn.execute(
                    "SELECT payload FROM a2a_tasks WHERE context_id=? AND tenant_id=? "
                    "ORDER BY updated_at DESC LIMIT ?",
                    (params.context_id, self._tenant(context), params.page_size or 50),
                )
                return [r[0] for r in cur.fetchall()]

        rows = await asyncio.to_thread(_rows)
        tasks = [ParseDict(json.loads(r), Task()) for r in rows]
        return ListTasksResponse(tasks=tasks, total_size=len(tasks))


class PostgresTaskStore(TaskStore):
    """基于 Postgres 的 TaskStore（生产口径；M1 提供骨架实现）。"""

    def __init__(self, dsn: str) -> None:
        import time

        import psycopg

        self._dsn = dsn
        # 建连重试：容器编排下 postgres 可能晚于本服务就绪（compose 已加
        # depends_on healthy，这里再兜一层，抗重启竞态）
        last_err: Exception | None = None
        for _ in range(12):
            try:
                self._pool = psycopg.Connection.connect(dsn, autocommit=True)
                last_err = None
                break
            except psycopg.OperationalError as e:  # noqa: PERF203
                last_err = e
                time.sleep(2.5)
        if last_err is not None:
            raise RuntimeError(f"PostgresTaskStore 无法连接 postgres：{last_err}") from last_err
        with self._pool.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS a2a_tasks (
                    tenant_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    context_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    payload JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (tenant_id, task_id)
                )
                """
            )
            cur.execute(
                "ALTER TABLE a2a_tasks ADD COLUMN IF NOT EXISTS "
                "tenant_id TEXT NOT NULL DEFAULT 't_demo'"
            )
            # 旧库把 task_id 当单主键；升级为复合主键（旧数据均属单租户）。
            try:
                cur.execute(
                    "ALTER TABLE a2a_tasks DROP CONSTRAINT IF EXISTS a2a_tasks_pkey"
                )
                cur.execute(
                    "ALTER TABLE a2a_tasks ADD PRIMARY KEY (tenant_id, task_id)"
                )
            except Exception:  # noqa: BLE001
                # 已有复合主键/无法自动迁移时保持原状，启动日志由调用方观察。
                pass

    async def save(self, task: Task, context: Any) -> None:
        d = MessageToDict(task, preserving_proto_field_name=True)
        tenant = _tenant_of(context)
        if not tenant:
            raise ValueError("missing verified tenant for task store")

        def _write() -> None:
            with self._pool.cursor() as cur:
                cur.execute(
                    "INSERT INTO a2a_tasks(tenant_id, task_id, context_id, state, payload) "
                    "VALUES (%s,%s,%s,%s,%s) ON CONFLICT(tenant_id, task_id) DO UPDATE SET "
                    "state=EXCLUDED.state, payload=EXCLUDED.payload, updated_at=now()",
                    (tenant, task.id, task.context_id,
                     TaskState.Name(task.status.state), d),
                )

        await asyncio.to_thread(_write)

    async def get(self, task_id: str, context: Any) -> Task | None:
        def _read() -> dict | None:
            with self._pool.cursor() as cur:
                cur.execute(
                    "SELECT payload FROM a2a_tasks WHERE task_id=%s AND tenant_id=%s",
                    (task_id, _tenant_of(context)),
                )
                r = cur.fetchone()
            return r[0] if r else None

        d = await asyncio.to_thread(_read)
        return ParseDict(d, Task()) if d else None

    async def delete(self, task_id: str, context: Any) -> None:
        def _del() -> None:
            with self._pool.cursor() as cur:
                cur.execute(
                    "DELETE FROM a2a_tasks WHERE task_id=%s AND tenant_id=%s",
                    (task_id, _tenant_of(context)),
                )

        await asyncio.to_thread(_del)

    async def list(  # noqa: A003
        self, params: ListTasksRequest, context: Any
    ) -> ListTasksResponse:
        def _rows() -> list[dict]:
            with self._pool.cursor() as cur:
                cur.execute(
                    "SELECT payload FROM a2a_tasks WHERE context_id=%s AND tenant_id=%s "
                    "ORDER BY updated_at DESC LIMIT %s",
                    (params.context_id, _tenant_of(context), params.page_size or 50),
                )
                return [r[0] for r in cur.fetchall()]

        rows = await asyncio.to_thread(_rows)
        tasks = [ParseDict(r, Task()) for r in rows]
        return ListTasksResponse(tasks=tasks, total_size=len(tasks))


def build_task_store():
    s = get_settings()
    if s.checkpointer == "postgres":
        return PostgresTaskStore(s.postgres_dsn)
    return SqliteTaskStore(s.sqlite_db_path)


# ---------------------------------------------------------------------------
# 组装：Card + executor + TaskStore + 路由 → Starlette 应用
# ---------------------------------------------------------------------------


def build_agent_card(
    *,
    name: str,
    description: str,
    url: str,
    skills: list[dict],
    streaming: bool = True,
) -> AgentCard:
    """skills: [{id, name, description, tags, examples?}]（Card 约定见技术方案 3.3）。

    能力模块标签（module:<id>）在此集中注入：8 个域卡共用一个构造点，
    skill.tags 仅信息性字段，不影响验签（签名在 build_a2a_app 构造后）。
    """
    from a2a.types import AgentCapabilities, AgentSkill
    from a2a.types.a2a_pb2 import AgentInterface
    from a2a.utils import TransportProtocol

    from shared.modules import module_tags

    return AgentCard(
        name=name,
        description=description,
        version="0.1.0",
        capabilities=AgentCapabilities(streaming=streaming),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain", "application/json"],
        skills=[
            AgentSkill(
                id=sk["id"],
                name=sk["name"],
                description=sk["description"],
                tags=[*sk.get("tags", []), *module_tags(sk["id"])],
            )
            for sk in skills
        ],
        supported_interfaces=[
            AgentInterface(
                url=url,
                protocol_binding=TransportProtocol.JSONRPC,
                protocol_version="1.0",
            )
        ],
    )


def build_a2a_app(
    card: AgentCard,
    executor: AgentExecutor,
    task_store: TaskStore | None = None,
):
    """a2a-sdk 1.2.x：handler + jsonrpc 路由 + card 路由 → Starlette 应用。

    auth 中间件在 build_server_app（含鉴权）里统一叠加。
    M4（PRD 7.3.1）：card_signing_enabled 时先给 Card 附加 JWS 签名再发布——
    全部服务都走本函数，一处签名全局生效。
    """
    if get_settings().card_signing_enabled:
        from shared.card_signing import sign_card

        card = sign_card(card)
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store or build_task_store(),
        agent_card=card,
    )
    routes = create_jsonrpc_routes(
        handler, rpc_url="/", context_builder=AuthServerCallContextBuilder()
    ) + create_agent_card_routes(card)

    import starlette.applications as sa

    return sa.Starlette(routes=routes)


def build_checkpointer_cm():
    """返回 async 上下文管理器，进入后得到 checkpointer（PostgresSaver / AsyncSqliteSaver）。

    用法：lifespan 里 `async with build_checkpointer_cm() as saver: await saver.setup(); ...`
    """
    s = get_settings()
    if s.checkpointer == "postgres":
        return AsyncPostgresSaver.from_conn_string(s.postgres_dsn)
    Path(s.sqlite_db_path).parent.mkdir(parents=True, exist_ok=True)
    return AsyncSqliteSaver.from_conn_string(s.sqlite_db_path)
