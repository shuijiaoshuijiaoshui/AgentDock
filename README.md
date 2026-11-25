# AgentDock

企业级 A2A 商家经营智能体中台。架构：**一主（编排 Server，LangGraph StateGraph）+ 七子（域 A2A Server：商品/订单/履约/售后/财务/政策/客户）+ 人工坐席 console + 审计单写者 securityd**。

- 需求：[`PRD-AgentDock.md`](PRD-AgentDock.md)（v2.2）
- 技术方案：[`AgentDock-LangGraph-技术方案.md`](AgentDock-LangGraph-技术方案.md)
- 导览（纯静态，双击可看）：[总览](docs/tour.html) · [流程图](docs/flow.html) · [泳道图](docs/swimlane.html) · [旅程](docs/journey.html)

## 当前状态：M1+M2+M3+M4 全量交付 + toB 阶段一安全加固（六域真实现 + 串行链/裁决/Saga/JWS/OTel/会话摘要）

| 部署单元 | 端口 | M1 状态 |
|---|---|---|
| 编排 Server（intake→understand→route→advance→synthesize→deliver + handoff） | 8300 | ✅ 可运行（advance 并行扇出 + **M3 串行链**：分层调度、上游否决取消下游、Saga 逆序补偿、权威源裁决 arbitrate） |
| 商品与营销域 | 8301 | ✅ 真实现（4 skill + Mock 数据） |
| 售后域 | 8304 | ✅ 真实现（4 skill；资格判定经 PolicyClient 引用生效条款） |
| 政策域 | 8306 | ✅ 真实现（双形态：A2A 2 skill + `/internal/clauses` 只读 API；生效期硬过滤） |
| 订单域 | 8302 | ✅ 真实现（order.query / order.amend / order.cancel+restore；写操作幂等，改地址两阶段确认） |
| 履约域 | 8303 | ✅ 真实现（fulfill.track 物流轨迹 / fulfill.atp 可承诺时效） |
| 结算与财务域 | 8305 | ✅ 真实现（billing.statement / refund.status / refund.settle / invoice.issue；退款幂等 + chaos 注入） |
| 人工坐席工作台（`handoff.accept` + 工作台页） | 8307 | ✅ 可运行（转人工→接管→回流；右侧只读工具面板） |

## 人机协同演示（转人工）

```bash
uv run python scripts/dev_all.py
# 浏览器打开 http://127.0.0.1:8307/portal     —— 演示商家后台壳（侧边栏/弹窗两种客服形态）
# 浏览器打开 http://127.0.0.1:8307/customer   —— 客户侧会话页（登录/问候/卡片/转人工/历史）
# 浏览器打开 http://127.0.0.1:8307/            —— 坐席工作台（左会话/中聊天/右工具）
```

实时通道是 **WebSocket**（`/ws/{conversation_id}`）：人工接管后的
对话双向且持续，客户/坐席消息都走 WS，服务端按会话维护连接注册表并扇出到该会话全部连接
（接管/回流等状态变更同样实时推送）。鉴权改为**连接后首包认证**：浏览器 WS 不能带
Authorization 头，连接建立后第一条消息必须是 `{"type":"auth","token":"<JWT>"}`，服务端验签
并从 token claim 取 tenant/actor/role；REST 保留（建会话/接管回流/工具/审计），聊天消息的
写库与扇出逻辑在 REST 与 WS 间共用。多实例部署时，连接注册表换 Redis pub/sub。

### 安全基线（toB 阶段一）

- **JWT 身份**：`POST /api/auth/login`（tenant + username + password）签发
  `tenant_id / actor_id / role / scopes`；HTTP 统一 Bearer 验签，工具只认 token claim，
  不再接受 metadata 里自称的 scope，也不再有“未带 scope = 内部信任”。
- **租户隔离**：会话/消息/交接/审计/业务 Mock 表全部带 `tenant_id`，所有 SQL 强制过滤；
  跨租户查询返回 not_found。演示租户：`t_demo` 与 `t_other`。
- **演示账号**：`customer / agent / supervisor / finance / admin`，密码 `demo123`
  （第二租户：`customer2 / agent2 / supervisor2`）。客户页登录 `customer`，工作台登录
  `agent` 或 `supervisor`。
- **PII 与限流**：入口与落库前脱敏手机号/身份证/银行卡/邮箱；HTTP/WS/登录接口滑动窗口限流
  （默认 120 次/分，登录 10 次/分；生产多实例换 Redis/网关）。
- **高价值动作审批**：**退款无论金额一律人工审批**（资金高风险，阈值只决定复核人数）；
  改价/开票超过阈值（默认 500/1000 元）返回 `pending_approval`；超过 5 倍阈值需
  **双人复核**（finance + supervisor/admin，且申请人不能自批）。审批 API：`GET /api/approvals?status=pending`、
  `POST /api/approvals/{id}/approve|reject`；审批通过后带 `approval_id` 重新调用工具。
- **统一审计**：`audit_events` 哈希链覆盖 `a2a.send`、`task.submitted/working/input-required/
  completed/failed/canceled`、`tool.call`、`arbitrate.*`、`deliver.*`、`approval.*`、
  `auth.login`、`rate_limit.block`；字段含 tenant/actor/role/action/target/trace/context/task/ip/ua。
  `GET /api/audit/events` 查询、`GET /api/audit/events/verify` 校验。
- **密钥管理**：`shared/keys.py` 统一取 JWT/Card 签名密钥；`ENV=prod` 时拒绝默认密钥，
  `KEY_PROVIDER=kms` 是显式适配点（未接具体云 SDK 会快速失败，避免“假 KMS”）。

### M4 收尾（phase5）

- **AgentCard JWS 签发/验签（PRD 7.3.1）**：全部服务经 `build_a2a_app` 统一给 Card 附加
  ES256（EC P-256）JWS 签名（a2a-sdk 官方 signing 模块，RFC 8785 JCS 规范化）；
  客户端 `delegate` 拉取 Card 后**验签通过才建 Task**，失败写审计 `card.verify_failed`
  并按局部交付降级；Card 缓存 TTL 300s；kid 轮换经 `CARD_VERIFY_KEYS_JSON`
  新旧公钥并存。密钥：`uv run python scripts/gen_keys.py`（dev 缺失自动生成，
  prod 必须预置）。
- **OTel 跨进程 trace（PRD 7.10）**：`TRACING_ENABLED=true` 时全链路 span 树——
  console 渠道根 span → 编排 → 域 → securityd，trace_id 与审计/agent_runs 同号
  （W3C Trace Context 经 metadata/出站头透传，delegate 是唯一跨进程出口）；
  span 按服务落 `TRACE_DIR/*.jsonl`（dev_all 默认开），`GET /api/traces/{trace_id}`
  重建以入口为根的调用树；头部采样按 trace_id
  确定性（`TRACING_SAMPLE_RATIO`），关闭时零导入零开销。
- **多轮历史注入（PRD 10.4）**：会话上下文 = 最近 6 轮原文 + 更早历史的滚动摘要
  （`session_summaries` 表，编排侧周期生成、渠道层落库、下一轮随 metadata 回注，
  注入前再过 mask_pii）；understand/synthesize 注入历史块（`understand@1.5` /
  `synthesize@1.1`），fake 模式支持指代继承（“它多少钱”→上一轮 SKU）；交接包
  携带渠道层真实近 6 轮。


- **浏览器会话（1C）**：登录同时下发 HttpOnly + SameSite=Lax 会话 Cookie 与 CSRF Cookie，
  customer/workbench 分名；写请求必须带 `X-CSRF-Token`。Bearer 仅保留给 API/测试客户端。
- **前端安全（1C/Phase 3）**：动态内容统一 `textContent`；脚本与样式全部外置；
  CSP `script-src 'self'; style-src 'self'`（无 `unsafe-inline`）；消息按 id 去重，
  WS 快照与增量同步是唯一事实源。customer 页 `frame-ancestors 'self'`
  （可被同源 `/portal` 内嵌），workbench `frame-ancestors 'none'`。
- **部署前缀（Phase 3）**：前端静态资源用相对路径，API/WS 从模块 URL 推断 `/static/`
  之前的前缀；`infra/nginx/agentdock-support.conf` 给出 `/support/* → console:8307`
  的同源反代示例（含 WS Upgrade、Cookie Path、附件大小）。
- **PII 不进 LLM（1C）**：合成与域内 ReAct 前按字段脱敏地址/手机/邮箱/证件/银行卡/客户 ID。
- **审计单写者（1B）**：securityd :8309 独占审计/审批/确认库；审计链带 HMAC 与
  per-tenant head，能发现内容篡改与链尾删除；业务服务只经 HTTP 调用。

客户页发「帮我转人工」（或同一会话两次需求未被满足、或提到投诉/纠纷）→
编排 `handoff` 节点生成结构化交接包，经 **A2A 委托**（新 Task、contextId 不变，PRD 7.12）
进入坐席队列 → 工作台「接管」→ 右侧工具面板查用户信息/商品/订单 → 回复实时到达客户页
→ 「交还 AI」后同一会话继续由 AI 接答。每次工具调用写审计（`GET /api/audit`），
审计表为哈希链（prev_hash 环环相扣，`GET /api/audit/verify` 全量校验，改/删任一条即断链定位）——
审计链可通过 `GET /api/audit/verify` 校验（PRD 8.3 / AC-8；工作台 UI 当前未放按钮）。

坐席工作台右栏**「AI 查询」tab 是主 Agent 的 toB 入口**：上半是问主 Agent
（如「SKU-A002 到手价」「我的授信额度」），走同一条 understand→route→dispatch 链路，
回答带「插入回复框」一键采纳；下半是工具面板（customer.query/product.lookup/order.query 等），
每张卡显示当前客户上下文并随请求带上 `customer_id`，未绑定会话的客户类工具自动禁用；
调用写审计——toC 客户问 AI 与 toB 坐席问 AI 双入口并存。

## 快速启动

```bash
uv sync                                    # 安装依赖（uv 会自动准备 Python 3.12）
cp .env.example .env                       # 填 ZHIPUAI_API_KEY（或先用 LLM_PROVIDER=fake）
uv run python scripts/gen_keys.py          # 生成 AgentCard JWS 签名密钥（dev 缺失时服务启动也会自动生成）
uv run python scripts/seed_mock.py         # 生成 Mock 数据（SQLite）
uv run python scripts/dev_all.py           # 一键拉起 <!-- STATS:dev_services -->11<!-- /STATS --> 个服务（默认开 trace，span 落 .data/traces/）
```

验证：

```bash
uv run python scripts/smoke.py             # 10 用例：商品×3 + 三域并行 + 政策/售后 + 跨域仲裁 + 澄清回填
uv run python scripts/ui_smoke.py          # 客户侧 UI 契约：登录/问候/卡片/附件/确认/转人工/历史/越权
uv run python scripts/browser_smoke.py     # 工作台浏览器冒烟（CDP 真实鼠标/键盘）：队列→接管→回复→备注→驳回→工单→trace→知识→统计→交还

串行链演示（M3，PRD 7.6/7.7/7.9）：对编排 8300 发——
`SKU-A003 的退款，SO-2003 退到哪了`  → 售后→履约→财务 三跳串行（金额只认财务）
`SKU-A002 的退款，SO-2002 退到哪了`  → 资格不通过 → 履约/财务被取消（上游否决）
`取消订单 SO-2001 给我退款`          → Saga 正向：订单取消→财务退款（写链，biz_key 幂等）
同句 + metadata chaos=finance_settle_fail → 财务故障 → 逆序补偿恢复订单（无悬挂）
`帮我把 SO-2001 改地址`              → 已签收被履约域否决，订单域不被调用
`SO-2001 实付多少钱`                → 订单→商品∥财务 分层 DAG；实付以财务为准，营销估算仅参考（7.6 仲裁）
uv run pytest                              # <!-- STATS:tests -->332<!-- /STATS --> 用例：路由确定性/工具合同/权威裁决/串行链否决/Saga 补偿/跨域仲裁 + 审计哈希链 + 转人工闭环

AgentDock-Eval（Claw-Eval 方法论：三证据通道 + 三维评分 + Pass^k 门禁）：
uv run python -m evals.run                 # 17 任务 × 3 trials：真 10 进程栈 + 确定性 rubric → 门禁 PASS/FAIL + 评测报告
uv run python -m evals.run --trials 1      # 快速回归；--provider real 接真模型测抖动；详见 docs/AgentDock-Eval指南.md

政策文档摄入 / 向量索引（可选，需 pgvector + 本地 embedding）：
uv pip install -r requirements-embeddings.txt
uv run python scripts/index_policies.py --rebuild --tenant t_demo   # 建索引
uv run python scripts/index_policies.py --drain                     # 发布后增量补索引
POLICY_RETRIEVER=hybrid uv run python -m evals.run --retriever hybrid  # 语义召回评测
```

## 接真实模型

`.env` 里改两行（默认 `LLM_PROVIDER=fake` 全链路不调真模型）：

```
LLM_PROVIDER=openai
ZHIPUAI_API_KEY=你的智谱API Key
```

模型名经 `LLM_SMALL_MODEL`（意图理解）/ `LLM_MAIN_MODEL`(域内推理与合成) 配置，
走智谱 OpenAI 兼容端点（`https://open.bigmodel.cn/api/paas/v4/`）。

生产口径（有 Docker 时，<!-- STATS:compose_app_services -->11<!-- /STATS --> 服务 + Postgres 一键起）：

```bash
docker compose -f infra/docker-compose.yml up -d --build   # 默认 LLM_PROVIDER=fake
docker compose -f infra/docker-compose.yml ps              # 等 <!-- STATS:compose_app_services -->11<!-- /STATS --> 服务 healthy/running
python scripts/smoke.py                                    # 宿主机对发布端口冒烟（9 用例）
# Postgres 模式验证（Task 落库 + 重启恢复 AC-14）：
docker exec -it $(docker ps -qf name=agentdock-postgres-1) psql -U agentdock   -c "SELECT task_id, state FROM a2a_tasks ORDER BY updated_at DESC LIMIT 5;"
docker restart $(docker ps -qf name=agentdock-orchestrator-1)   # 重启后同 thread 可恢复
# 接真实模型：.env 写 LLM_PROVIDER=openai + ZHIPUAI_API_KEY=... 再 up -d
```

## 仓库结构

```
agentdock/
├── orchestrator/   # 一主：LangGraph 主图 + 路由表 + a2a-sdk 包裹
├── domains/        # 六子：6 个域 Server（商品/订单/履约/售后/财务/政策）
├── shared/         # executor/TaskStore/JWT/审计/审批/PII/限流/keys（各域共用）
├── infra/          # docker-compose、Mock 数据、Card 签名密钥（dev）
├── tests/          # 路由确定性 / 工具合同 / 安全加固 / e2e smoke
└── scripts/        # seed_mock / dev_all / smoke
```

## 已知 TODO

- M1–M4 全部交付：六域真实现、串行链（上游否决）、arbitrate 纯函数裁决、Saga 补偿（chaos 演练）、promo_arbitrate 分层 DAG 仲裁；toB 阶段一安全加固；Card JWS 签发/验签、OTel 跨进程 trace、多轮历史注入（M4 收尾）。
- toB 阶段一已交付：JWT/租户/角色/scope、WS 首包认证、全 SQL tenant 过滤、PII/限流、金额阈值与双人复核、统一审计。
- trace 已知限制：头部采样按 trace_id 确定性，"异常全采"需 collector 级尾采样（未接）；导出仅 file exporter，OTLP/Jaeger 未接；多机部署需共享 `TRACE_DIR`（compose 未挂共享卷）；`agent-assist` 坐席助手会话的历史注入不在本阶段。
- Card JWS 已知限制：kid 轮换后旧签名 Card 在客户端缓存 TTL（300s）内仍被信任——轮换方必须把旧公钥保留在 `CARD_VERIFY_KEYS_JSON`。
- 安全存储当前为共享 SQLite（`SHARED_DB_PATH`）；生产多实例需换 Postgres + advisory lock/队列，限流换 Redis/网关。
- `KEY_PROVIDER=kms` 是适配点，尚未绑定具体云 SDK；生产须实现 `shared/keys.py` 的 KMSKeyProvider。
- `.archify` 泳道图仍是 v2.1「四域」口径，待按 v2.2 重绘。
- 多实例部署时 console 的 WS 扇出注册表换 Redis pub/sub。
