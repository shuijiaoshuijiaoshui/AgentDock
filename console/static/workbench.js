/* Phase 4 内部坐席工作台：队列 / 摘要 / 消息 / 360 / 工单 / 知识库 / 统计 / 审批。
 *
 * 约束：全部 DOM 用 textContent/createElement（禁止 HTML 字符串注入）；
 * 不使用浏览器持久存储；无 style 属性/赋值（CSP style-src 'self'）；
 * 同一模块复用 customer 的 api/ws/components。
 */

import { ApiError, api, setCsrf, setSurface } from "./js/api.js";
import {
  cardNode, clear, el, emptyNode, fieldRow, fmtTime, loadingNode, messageNode,
  stateBadge,
} from "./js/components.js";
import { ChatSocket } from "./js/ws.js";

setSurface("workbench");

const $ = (id) => document.getElementById(id);

const state = {
  me: null,
  role: "",
  canApprove: false,
  canPublish: false,
  tab: "pending",
  cursor: 0,
  nextCursor: 0,
  queue: [],
  cid: "",
  conv: null,
  socket: null,
  lastId: 0,
  rendered: new Set(),
  messages: new Map(),
  quick: [],
  agents: [],
  side: "360",
  polls: [],
  toastTimer: null,
  notesOpen: false,
  ticketDept: "warehouse",
  kbStatus: "all",
  assistTaskId: null,
};

/* ---------------- 通用 ---------------- */

function toast(message, kind = "") {
  const box = $("toast");
  box.textContent = message;
  box.className = `toast ${kind}`.trim();
  box.classList.remove("hidden");
  if (state.toastTimer) clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => box.classList.add("hidden"), 3200);
}

function errText(err) {
  if (err instanceof ApiError) {
    if (err.status === 401) return "登录已失效";
    if (err.status === 403) return "无权限执行该操作";
    return err.detail || err.message;
  }
  return String(err && err.message ? err.message : err);
}

function handleAuth(err) {
  if (err instanceof ApiError && err.status === 401) {
    showLogin("登录已失效，请重新登录");
    return true;
  }
  return false;
}

async function busy(button, fn) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "处理中…";
  try {
    await fn();
  } catch (err) {
    if (!handleAuth(err)) toast(errText(err), "error");
  } finally {
    button.textContent = label;
    button.disabled = false;
    // 操作后按钮可用性由会话状态决定（如接管后禁用接管、启用发送）
    if (state.conv) applyConversation(state.conv);
  }
}

function secondsText(seconds) {
  const s = Number(seconds || 0);
  if (s < 60) return `${s} 秒`;
  if (s < 3600) return `${Math.floor(s / 60)} 分钟`;
  return `${(s / 3600).toFixed(1)} 小时`;
}

function roleLabel(role) {
  return { agent: "坐席", supervisor: "主管", admin: "管理员", finance: "财务" }[role] || role;
}

/* ---------------- 登录 ---------------- */

function showLogin(message = "") {
  if (state.socket) {
    state.socket.close();
    state.socket = null;
  }
  stopPolls();
  $("login").classList.remove("hidden");
  $("who").textContent = "未登录";
  $("conn").textContent = "—";
  if (message) {
    $("login-error").textContent = message;
    $("login-error").classList.remove("hidden");
  }
}

function hideLogin() {
  $("login").classList.add("hidden");
  $("login-error").classList.add("hidden");
}

async function login() {
  const btn = $("login-btn");
  btn.disabled = true;
  btn.textContent = "登录中…";
  $("login-error").classList.add("hidden");
  try {
    const resp = await fetch("/api/auth/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tenant_id: $("tenant").value,
        username: $("username").value.trim(),
        password: $("password").value,
        surface: "workbench",
      }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      $("login-error").textContent = data.error === "invalid_credentials"
        ? "账号或密码错误" : `登录失败（${data.error || resp.status}）`;
      $("login-error").classList.remove("hidden");
      return;
    }
    setCsrf(data.csrf_token || "");
    hideLogin();
    await init();
  } catch (err) {
    $("login-error").textContent = `登录失败：${err.message}`;
    $("login-error").classList.remove("hidden");
  } finally {
    btn.disabled = false;
    btn.textContent = "登录";
  }
}

async function logout() {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch (_) { /* ignore */ }
  setCsrf("");
  state.me = null;
  state.cid = "";
  state.conv = null;
  stopPolls();
  showLogin();
}

async function init() {
  try {
    const me = await api("/api/auth/me");
    if (!["agent", "supervisor", "admin", "finance"].includes(me.role)) {
      showLogin("当前账号无工作台权限（需 agent / supervisor / admin / finance）");
      return;
    }
    state.me = me;
    state.role = me.role;
    state.canApprove = (me.scopes || []).includes("approval.write");
    state.canPublish = ["supervisor", "admin"].includes(me.role);
    setCsrf(me.csrf_token || "");
    hideLogin();
    $("who").textContent = `${me.display_name || me.user_id} · ${roleLabel(me.role)}`;
    applyRoleVisibility();
    await loadQuickReplies();
    await loadAgents();
    if (me.role === "finance") {
      switchSide("approvals");
      await loadApprovals();
    } else {
      await loadQueue(true);
      if (state.cid) await selectConversation(state.cid, { keep: true });
    }
    startPolls();
  } catch (err) {
    showLogin(errText(err));
  }
}

function applyRoleVisibility() {
  document.body.classList.toggle("role-finance", state.role === "finance");
  $("tab-stats").classList.toggle("hidden", !["supervisor", "admin"].includes(state.role));
  for (const tab of document.querySelectorAll(".side-tab")) {
    if (state.role === "finance") {
      tab.classList.toggle("hidden", tab.dataset.side !== "approvals");
    }
  }
}

function startPolls() {
  stopPolls();
  if (state.role !== "finance") {
    state.polls.push(setInterval(() => loadQueue(false).catch(() => {}), 5000));
  }
  state.polls.push(setInterval(() => {
    if (state.side === "approvals") loadApprovals().catch(() => {});
  }, 10000));
}

function stopPolls() {
  for (const id of state.polls) clearInterval(id);
  state.polls = [];
}

/* ---------------- 队列 ---------------- */

function queueItem(item) {
  const node = el("div", {
    className: `q-item q-${item.priority}` + (item.id === state.cid ? " active" : ""),
    attrs: { "data-conv-id": item.id, role: "button", tabindex: "0" },
  });
  const row1 = el("div", { className: "q-row1" });
  row1.appendChild(el("b", { text: item.customer_name || item.customer_id || "未登记客户" }));
  row1.appendChild(stateBadge(item.state, item.status));
  node.appendChild(row1);
  node.appendChild(el("div", {
    className: "q-meta",
    text: `${item.customer_id || "-"} · ${item.tier || "未分级"} · 优先级 ${item.priority}`
      + (item.assigned_agent_name ? ` · ${item.assigned_agent_name}` : ""),
  }));
  node.appendChild(el("div", { className: "q-last", text: item.last_message || "（无消息）" }));
  const foot = el("div", { className: "q-foot" });
  if (item.unread > 0) {
    foot.appendChild(el("span", { className: "q-unread", text: `${item.unread} 未读` }));
  }
  if (item.handoff_reason) {
    foot.appendChild(el("span", { text: item.handoff_reason.slice(0, 18) }));
  }
  const sla = el("span", {
    className: "q-sla" + (item.sla_breached ? " breached" : ""),
    text: item.sla_breached
      ? `SLA 超时 · ${secondsText(item.waiting_seconds)}`
      : `等待 ${secondsText(item.waiting_seconds)}`,
  });
  foot.appendChild(sla);
  node.appendChild(foot);
  node.addEventListener("click", () => selectConversation(item.id));
  node.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") selectConversation(item.id);
  });
  return node;
}

async function loadQueue(reset, append = false) {
  if (state.role === "finance") return;
  if (reset || !append) {
    state.cursor = 0;
    state.nextCursor = 0;
  }
  const params = new URLSearchParams({
    tab: state.tab,
    sort: $("queue-sort").value,
    limit: "20",
  });
  const q = $("queue-search").value.trim();
  const priority = $("queue-priority").value;
  if (q) params.set("q", q);
  if (priority) params.set("priority", priority);
  if (append && state.cursor) params.set("cursor", String(state.cursor));
  let data;
  try {
    data = await api(`/api/workbench/queues?${params.toString()}`);
  } catch (err) {
    if (!handleAuth(err)) toast(errText(err), "error");
    return;
  }
  const list = $("queue-list");
  if (!append) {
    clear(list);
    state.queue = [];
  }
  if (!data.items.length && !append) {
    list.appendChild(emptyNode("该队列暂无会话"));
  }
  for (const item of data.items) {
    state.queue.push(item);
    list.appendChild(queueItem(item));
  }
  state.cursor = data.next_cursor || 0;
  $("queue-more").classList.toggle("hidden", !state.cursor);
  $("queue-total").textContent = `共 ${data.total} 条`;
  for (const [tab, count] of Object.entries(data.counts || {})) {
    const node = $(`count-${tab}`);
    if (node) node.textContent = String(count);
  }
}

/* ---------------- 选择会话 ---------------- */

async function selectConversation(cid, { keep = false } = {}) {
  if (!cid) return;
  if (!keep && state.cid === cid) return;
  state.cid = cid;
  state.lastId = 0;
  clear($("chat"));
  $("chat").appendChild(loadingNode("加载会话…"));
  clear($("summary-body"));
  $("summary-body").appendChild(loadingNode("生成摘要…"));
  clear($("pending-wrap"));
  $("pending-wrap").classList.add("hidden");
  clear($("trace-body"));
  $("trace-body").appendChild(loadingNode("加载 trace…"));
  markQueueActive();

  try {
    const convs = await api("/api/conversations");
    const conv = convs.find((item) => item.id === cid);
    if (conv) applyConversation(conv);
    const messages = await api(`/api/conversations/${cid}/messages`);
    renderAll(messages);
    if (messages.length) {
      try {
        await api(`/api/conversations/${cid}/read`, {
          method: "POST",
          body: { last_message_id: messages[messages.length - 1].id },
        });
      } catch (_) { /* 已读非关键 */ }
    }
    connectSocket(cid);
    await Promise.all([
      loadSummary(),
      loadPending(),
      loadTrace(),
      load360(),
    ]);
    if (state.notesOpen) await loadNotes();
    if (state.side === "tickets") await loadTickets();
    if (state.side === "knowledge") await loadKnowledge();
    await loadQueue(false);
  } catch (err) {
    if (handleAuth(err)) return;
    clear($("chat"));
    $("chat").appendChild(emptyNode(`会话加载失败：${errText(err)}`));
    toast(errText(err), "error");
  }
}

function markQueueActive() {
  for (const node of document.querySelectorAll(".q-item")) {
    node.classList.toggle("active", node.dataset.convId === state.cid);
  }
}

function applyConversation(conv) {
  if (!conv) return;
  state.conv = conv;
  const name = conv.customer_id || "未登记客户";
  $("chat-title").textContent = `会话 ${String(conv.id).slice(0, 8)} · ${name}`;
  const sub = {
    AI_ACTIVE: "AI 服务中", PENDING_HANDOFF: "待接管",
    HUMAN_ACTIVE: "人工服务中", PENDING_REVIEW: "待主管复核", CLOSED: "已结束",
  }[conv.state] || conv.state;
  $("chat-sub").textContent = `${sub} · mode=${conv.mode}`
    + (conv.assigned_agent_id ? ` · 坐席 ${conv.assigned_agent_id}` : "");
  const holder = $("chat-badge");
  const badge = stateBadge(conv.state, conv.status);
  badge.id = "chat-badge";
  holder.replaceWith(badge);

  const isClosed = conv.state === "CLOSED" || conv.status === "closed";
  const human = conv.mode === "human" && !isClosed;
  $("btn-takeover").disabled = conv.state !== "PENDING_HANDOFF";
  $("btn-return").disabled = !human;
  $("btn-close").disabled = isClosed;
  $("btn-review-approve").classList.toggle(
    "hidden", !(state.role === "supervisor" || state.role === "admin")
      || conv.state !== "PENDING_REVIEW");
  $("btn-review-reject").classList.toggle(
    "hidden", !(state.role === "supervisor" || state.role === "admin")
      || conv.state !== "PENDING_REVIEW");
  $("btn-transfer").disabled = isClosed || !state.agents.length;
  $("btn-send").disabled = !human;
  $("agent-input").disabled = !human;
  refreshRejectLinks();
}

/* ---------------- 消息 ---------------- */

function appendMessage(message) {
  if (!message || message.id === undefined || message.id === null) return;
  if (state.rendered.has(message.id)) return;
  state.rendered.add(message.id);
  if (typeof message.id === "number" && message.id > state.lastId) {
    state.lastId = message.id;
  }
  state.messages.set(message.id, message);
  const empty = $("chat").querySelector(".empty");
  if (empty) empty.remove();
  const node = messageNode(message);
  if (typeof message.id === "number") node.dataset.messageId = String(message.id);
  const human = state.conv && state.conv.mode === "human"
    && state.conv.state !== "CLOSED";
  if (message.role === "ai" && human) {
    const actions = el("div", { className: "msg-actions" });
    const reject = el("button", {
      className: "mini-link", text: "驳回并编辑", attrs: { type: "button" },
    });
    reject.addEventListener("click", () => openRejectEditor(node, message));
    actions.appendChild(reject);
    node.appendChild(actions);
  }
  $("chat").appendChild(node);
  const meta = message.meta || {};
  for (const card of meta.cards || []) {
    $("chat").appendChild(cardNode(card, {
      onAction: () => toast("卡片动作由客户在客户侧确认", ""),
    }));
  }
  $("chat").scrollTop = $("chat").scrollHeight;
}

function renderAll(messages) {
  clear($("chat"));
  state.rendered.clear();
  state.lastId = 0;
  if (!messages || !messages.length) {
    $("chat").appendChild(emptyNode("暂无消息"));
    return;
  }
  for (const message of messages) appendMessage(message);
}

async function syncAfter() {
  if (!state.cid) return;
  const data = await api(
    `/api/conversations/${state.cid}/messages?after_id=${state.lastId}`,
  );
  for (const message of data) appendMessage(message);
}

async function refreshRejectLinks() {
  // 接管/回流后，已渲染的 AI 气泡需要补上/去掉「驳回并编辑」入口。
  const human = state.conv && state.conv.mode === "human"
    && state.conv.state !== "CLOSED";
  for (const node of document.querySelectorAll("#chat .msg.ai")) {
    const has = node.querySelector(".mini-link");
    if (human && !has) {
      const actions = el("div", { className: "msg-actions" });
      const reject = el("button", {
        className: "mini-link", text: "驳回并编辑", attrs: { type: "button" },
      });
      const raw = node.dataset.messageId;
      reject.addEventListener("click", () => {
        const message = state.messages.get(Number(raw));
        if (message) openRejectEditor(node, message);
      });
      actions.appendChild(reject);
      node.appendChild(actions);
    } else if (!human && has) {
      has.closest(".msg-actions").remove();
    }
  }
}

function openRejectEditor(node, message) {
  if (node.querySelector(".reject-editor")) return;
  const editor = el("div", { className: "reject-editor" });
  const textarea = el("textarea", { attrs: { rows: "3" } });
  textarea.value = message.content || "";
  const reason = el("input", { attrs: { placeholder: "驳回原因（必填，进入反馈统计）" } });
  const row = el("div", { className: "row" });
  const send = el("button", { className: "btn-primary", text: "发送修改", attrs: { type: "button" } });
  const cancel = el("button", { className: "btn-ghost", text: "取消", attrs: { type: "button" } });
  cancel.addEventListener("click", () => editor.remove());
  send.addEventListener("click", async () => {
    const edited = textarea.value.trim();
    const why = reason.value.trim();
    if (!edited || !why) {
      toast("编辑后内容与驳回原因都不能为空", "error");
      return;
    }
    await busy(send, async () => {
      await api(`/api/conversations/${state.cid}/reject-answer`, {
        method: "POST",
        body: { message_id: message.id, edited_answer: edited, reason: why },
      });
      toast("已驳回并发送修改后的答复", "ok");
      editor.remove();
      await syncAfter();
      await loadQueue(false);
      await loadSummary();
      await loadTrace();
    });
  });
  row.appendChild(send);
  row.appendChild(cancel);
  editor.appendChild(textarea);
  editor.appendChild(reason);
  editor.appendChild(row);
  node.appendChild(editor);
  textarea.focus();
}

/* ---------------- WebSocket ---------------- */

function connectSocket(cid) {
  if (state.socket) state.socket.close();
  if (!cid || state.role === "finance") return;
  state.socket = new ChatSocket({
    conversationId: cid,
    surface: "workbench",
    onSnapshot: (evt) => {
      if (evt.conversation) applyConversation(evt.conversation);
      renderAll(evt.messages || []);
    },
    onMessage: (message, conversation) => {
      if (conversation) applyConversation(conversation);
      appendMessage(message);
      if (message && message.role === "ai") {
        loadPending().catch(() => {});
        loadSummary().catch(() => {});
      }
    },
    onState: (conversation) => {
      if (!conversation || conversation.id !== state.cid) return;
      applyConversation(conversation);
      loadQueue(false).catch(() => {});
    },
    onStatus: (status) => {
      $("conn").textContent = status === "connected" ? "WebSocket 已连接" : "WebSocket 重连中…";
    },
  });
  state.socket.connect();
}

/* ---------------- 会话操作 ---------------- */

function setText(nodeId, text) {
  $(nodeId).textContent = text;
}

async function refreshCurrent() {
  if (!state.cid) return;
  try {
    const convs = await api("/api/conversations");
    const conv = convs.find((item) => item.id === state.cid);
    if (conv) applyConversation(conv);
  } catch (_) { /* ignore */ }
  await Promise.all([loadSummary(), loadPending(), loadTrace(), loadQueue(false)]);
  if (state.side === "360") await load360();
}

$("btn-takeover").addEventListener("click", (event) => busy(event.currentTarget, async () => {
  await api(`/api/conversations/${state.cid}/takeover`, { method: "POST" });
  toast("已接管会话", "ok");
  await refreshCurrent();
}));

$("btn-return").addEventListener("click", (event) => busy(event.currentTarget, async () => {
  await api(`/api/conversations/${state.cid}/return`, { method: "POST" });
  toast("已交还 AI", "ok");
  await refreshCurrent();
}));

$("btn-close").addEventListener("click", (event) => busy(event.currentTarget, async () => {
  await api(`/api/conversations/${state.cid}/close`, { method: "POST" });
  toast("已关闭 / 转待复核", "ok");
  await refreshCurrent();
}));

for (const [id, decision] of [["btn-review-approve", "approve"],
  ["btn-review-reject", "reject"]]) {
  $(id).addEventListener("click", (event) => busy(event.currentTarget, async () => {
    await api(`/api/conversations/${state.cid}/review`, {
      method: "POST", body: { decision },
    });
    toast(decision === "approve" ? "复核通过，会话关闭" : "已驳回，回到人工", "ok");
    await refreshCurrent();
  }));
}

$("btn-transfer").addEventListener("click", (event) => busy(event.currentTarget, async () => {
  const to = $("transfer-agent").value;
  if (!to) {
    toast("请选择转交坐席", "error");
    return;
  }
  await api(`/api/conversations/${state.cid}/transfer`, {
    method: "POST", body: { to_agent_id: to },
  });
  toast("已转交", "ok");
  await refreshCurrent();
}));

$("btn-send").addEventListener("click", (event) => busy(event.currentTarget, async () => {
  const text = $("agent-input").value.trim();
  if (!text) return;
  await api(`/api/conversations/${state.cid}/reply`, {
    method: "POST", body: { text },
  });
  $("agent-input").value = "";
  await syncAfter();
  await loadQueue(false);
}));

$("agent-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("btn-send").click();
  }
});

/* ---------------- 摘要 / 待办 / trace ---------------- */

function summaryField(label, value) {
  return el("span", { className: "summary-field" }, [
    el("span", { className: "k", text: label }),
    document.createTextNode(value === null || value === undefined || value === "" ? "-" : String(value)),
  ]);
}

async function loadSummary(refresh = false) {
  if (!state.cid) return;
  const data = await api(
    `/api/conversations/${state.cid}/summary${refresh ? "?refresh=1" : ""}`,
  );
  const s = data.summary || {};
  const customer = s.customer || {};
  const body = $("summary-body");
  clear(body);
  body.appendChild(el("div", { className: "summary-line", text: s.summary_text || "" }));
  const grid = el("div", { className: "summary-grid" });
  grid.appendChild(summaryField("客户", `${customer.name || ""}（${customer.customer_id || "-"}）`));
  grid.appendChild(summaryField("等级/折扣", `${customer.dealer_level || customer.tier || "-"} / ${customer.discount ?? "-"}`));
  grid.appendChild(summaryField("授信剩余", customer.credit_remaining ?? "-"));
  grid.appendChild(summaryField("风险", customer.risk_level || "-"));
  grid.appendChild(summaryField("本次诉求", s.request || "-"));
  grid.appendChild(summaryField("AI 意图", s.intent || "-"));
  body.appendChild(grid);
  const suggest = el("ul", { className: "summary-suggest" });
  for (const item of s.suggested_actions || []) {
    suggest.appendChild(el("li", { text: item }));
  }
  body.appendChild(suggest);
  const stats = s.stats || {};
  body.appendChild(el("div", {
    className: "hint",
    text: `未读 ${stats.unread ?? 0} · 消息 ${stats.message_count ?? 0}`
      + ` · 等待 ${secondsText(stats.waiting_seconds || 0)}`,
  }));
  setText("summary-cached", data.cached ? "缓存" : "已生成");
}

$("summary-refresh").addEventListener("click", (event) => busy(event.currentTarget,
  async () => { await loadSummary(true); }));

function pendingCard(pending) {
  const card = el("div", { className: "pending-card" });
  card.appendChild(el("b", {
    text: pending.type === "approval" ? "待审批动作" : "待客户确认",
  }));
  const preview = pending.preview || {};
  for (const [key, value] of Object.entries(preview)) {
    if (value === null || value === undefined || value === "") continue;
    card.appendChild(summaryField(key, value));
  }
  const row = el("div", { className: "row" });
  if (pending.type === "confirm") {
    // 两阶段确认必须由客户本人在会话中完成；坐席只提醒，不代确认
    row.appendChild(el("span", {
      className: "warn-text", text: "需客户在客户会话中确认后才会执行",
    }));
    if (state.conv && state.conv.mode === "human") {
      const remind = el("button", {
        className: "btn-ghost", text: "提醒客户确认", attrs: { type: "button" },
      });
      remind.addEventListener("click", () => busy(remind, async () => {
        const preview = Object.entries(pending.preview || {})
          .filter(([, v]) => v !== null && v !== "" && v !== undefined)
          .map(([k, v]) => `${k}=${v}`).join("，");
        await api(`/api/conversations/${state.cid}/reply`, {
          method: "POST",
          body: { text: `提醒：您的待办（${preview || "待确认操作"}）需要您确认后才会执行。` },
        });
        toast("已提醒客户确认", "ok");
        await syncAfter();
      }));
      row.appendChild(remind);
    }
  } else if (pending.approval_id) {
    if (state.canApprove) {
      const btn = el("button", {
        className: "btn-ok", text: "执行审批动作", attrs: { type: "button" },
      });
      btn.addEventListener("click", () => busy(btn, async () => {
        await api(`/api/approvals/${pending.approval_id}/execute`, { method: "POST" });
        toast("审批动作已执行", "ok");
        await syncAfter();
        await loadPending();
        await loadSummary();
      }));
      row.appendChild(btn);
    } else {
      row.appendChild(el("span", {
        className: "warn-text",
        text: `待财务审批（${pending.approval_id.slice(0, 8)}）`,
      }));
    }
  }
  card.appendChild(row);
  return card;
}

async function loadPending() {
  if (!state.cid) return;
  let list = [];
  try {
    list = await api(`/api/conversations/${state.cid}/pending-actions`);
  } catch (_) {
    return;
  }
  const wrap = $("pending-wrap");
  clear(wrap);
  wrap.classList.toggle("hidden", !list.length);
  for (const pending of list) wrap.appendChild(pendingCard(pending));
}

const STAGE_LABELS = {
  intake: "intake", understand: "understand", route: "route",
  domain_call: "domain", tool: "tool", arbitrate: "arbitrate",
  synthesize: "synthesize", deliver: "deliver",
  saga_compensate: "saga", unknown: "unknown",
};

function traceRun(run) {
  const box = el("details", { className: "trace-run" });
  box.appendChild(el("summary", {
    text: `${run.intent || "unknown"} · ${String(run.trace_id || "").slice(0, 10)}`
      + ` · ${fmtTime(run.finished_at)} · ${(run.steps || []).length} 步`,
  }));
  box.appendChild(el("div", {
    className: "meta",
    text: `model=${run.model || "-"} · prompt=${run.prompt_version || "-"}`
      + ` · status=${run.status || "-"}`,
  }));
  for (const step of run.steps || []) {
    const row = el("div", { className: "trace-step" });
    row.appendChild(el("span", {
      className: "stage", text: STAGE_LABELS[step.stage] || step.stage,
    }));
    const detail = el("div", { className: "detail" });
    const head = el("div");
    head.appendChild(el("span", {
      className: step.status === "completed" ? "ok" : "err",
      text: step.status || "completed",
    }));
    head.appendChild(document.createTextNode(
      ` · ${step.domain || "-"} / ${step.tool || "-"}`
      + (step.latency_ms !== null && step.latency_ms !== undefined
        ? ` · ${step.latency_ms}ms` : ""),
    ));
    detail.appendChild(head);
    const summary = step.output_summary || step.input_summary || "";
    if (summary) detail.appendChild(el("div", { className: "meta", text: String(summary).slice(0, 300) }));
    if (step.error) detail.appendChild(el("div", { className: "meta err", text: step.error }));
    if (step.model || step.prompt_version) {
      detail.appendChild(el("div", {
        className: "meta",
        text: `model=${step.model || "-"} prompt=${step.prompt_version || "-"}`,
      }));
    }
    row.appendChild(detail);
    box.appendChild(row);
  }
  return box;
}

async function loadTrace() {
  if (!state.cid) return;
  const data = await api(`/api/conversations/${state.cid}/trace`);
  const body = $("trace-body");
  clear(body);
  const runs = data.runs || [];
  if (!runs.length) {
    body.appendChild(emptyNode("暂无 Agent 调用记录"));
    return;
  }
  for (const run of runs) body.appendChild(traceRun(run));
}

/* ---------------- 备注 ---------------- */

async function loadNotes() {
  if (!state.cid) return;
  const data = await api(`/api/conversations/${state.cid}/notes`);
  const list = $("note-list");
  clear(list);
  const notes = data.notes || [];
  if (!notes.length) {
    list.appendChild(emptyNode("暂无内部备注"));
    return;
  }
  for (const note of notes) {
    const item = el("div", { className: "note-item" });
    item.appendChild(el("div", { text: note.content }));
    item.appendChild(el("div", {
      className: "meta", text: `${note.author_id} · ${fmtTime(note.created_at)}`,
    }));
    list.appendChild(item);
  }
}

$("btn-note").addEventListener("click", async () => {
  state.notesOpen = !state.notesOpen;
  $("note-panel").classList.toggle("hidden", !state.notesOpen);
  if (state.notesOpen) await loadNotes().catch((err) => toast(errText(err), "error"));
});
$("note-close").addEventListener("click", () => {
  state.notesOpen = false;
  $("note-panel").classList.add("hidden");
});
$("note-add").addEventListener("click", (event) => busy(event.currentTarget, async () => {
  const content = $("note-input").value.trim();
  if (!content) return;
  await api(`/api/conversations/${state.cid}/notes`, {
    method: "POST", body: { content },
  });
  $("note-input").value = "";
  toast("备注已保存（客户不可见）", "ok");
  await loadNotes();
}));

/* ---------------- 快捷回复 ---------------- */

async function loadQuickReplies() {
  try {
    const data = await api("/api/quick-replies");
    state.quick = data.quick_replies || [];
  } catch (_) {
    state.quick = [];
  }
  const row = $("quick-row");
  clear(row);
  for (const reply of state.quick) {
    const chip = el("button", {
      className: "chip-btn", text: reply.title, attrs: { type: "button", title: reply.content },
    });
    chip.addEventListener("click", () => {
      const input = $("agent-input");
      input.value = input.value ? `${input.value}\n${reply.content}` : reply.content;
      input.focus();
    });
    row.appendChild(chip);
  }
}

/* ---------------- 转交坐席列表 ---------------- */

async function loadAgents() {
  try {
    const data = await api("/api/agents");
    state.agents = data.agents || [];
  } catch (_) {
    state.agents = [];
  }
  const select = $("transfer-agent");
  clear(select);
  select.appendChild(el("option", { text: "选择转交坐席", attrs: { value: "" } }));
  for (const agent of state.agents) {
    if (agent.user_id === (state.me && state.me.user_id)) continue;
    select.appendChild(el("option", {
      text: `${agent.display_name}（${roleLabel(agent.role)} · ${agent.load}）`,
      attrs: { value: agent.user_id },
    }));
  }
}

/* ---------------- 客户 360 ---------------- */

function kvGrid(pairs) {
  const grid = el("div", { className: "kv" });
  for (const [key, value] of pairs) {
    grid.appendChild(el("span", { className: "k", text: key }));
    grid.appendChild(el("span", {
      text: value === null || value === undefined || value === "" ? "-" : String(value),
    }));
  }
  return grid;
}

function miniTable(columns, rows) {
  const table = el("table", { className: "mini-table" });
  const thead = el("thead");
  const tr = el("tr");
  for (const col of columns) tr.appendChild(el("th", { text: col.label }));
  thead.appendChild(tr);
  table.appendChild(thead);
  const tbody = el("tbody");
  for (const row of rows) {
    const line = el("tr");
    for (const col of columns) {
      line.appendChild(el("td", {
        text: row[col.key] === null || row[col.key] === undefined ? "-" : String(row[col.key]),
      }));
    }
    tbody.appendChild(line);
  }
  table.appendChild(tbody);
  return table;
}

async function load360() {
  if (!state.cid || state.role === "finance") return;
  let data;
  try {
    data = await api(`/api/conversations/${state.cid}/customer360`);
  } catch (err) {
    clear($("side-360"));
    $("side-360").appendChild(emptyNode(errText(err)));
    return;
  }
  const pane = $("side-360");
  clear(pane);
  const c = data.customer || {};
  const p = data.profile || {};
  pane.appendChild(el("h4", { text: "客户资料" }));
  pane.appendChild(kvGrid([
    ["客户", `${c.name || ""}（${c.customer_id || "-"}）`],
    ["等级", c.tier || p.dealer_level || "-"],
    ["折扣", p.discount ?? "-"],
    ["客户经理", p.account_manager || "-"],
    ["授信额度", p.credit_limit ?? "-"],
    ["已用/剩余", `${p.credit_used ?? "-"} / ${p.credit_remaining ?? "-"}`],
    ["账期/到期", `${p.credit_days ?? "-"} 天 / ${p.credit_due_date || "-"}`],
    ["风险", p.risk_level || "-"],
    ["电话", c.phone || "-"],
    ["城市", c.city || "-"],
  ]));
  pane.appendChild(el("h4", { text: "最近订单" }));
  pane.appendChild(miniTable([
    { key: "order_no", label: "订单" }, { key: "sku", label: "SKU" },
    { key: "amount", label: "金额" }, { key: "status", label: "状态" },
    { key: "created_at", label: "下单" },
  ], data.orders || []));
  pane.appendChild(el("h4", { text: "工单" }));
  pane.appendChild(miniTable([
    { key: "ticket_no", label: "工单" }, { key: "sku", label: "SKU" },
    { key: "fault_type", label: "故障" }, { key: "status", label: "状态" },
    { key: "department", label: "部门" },
  ], data.tickets || []));
  pane.appendChild(el("h4", { text: "投诉" }));
  pane.appendChild(miniTable([
    { key: "complaint_id", label: "编号" }, { key: "order_no", label: "订单" },
    { key: "type", label: "类型" }, { key: "status", label: "状态" },
    { key: "created_at", label: "时间" },
  ], data.complaints || []));
  pane.appendChild(el("h4", { text: "售后" }));
  pane.appendChild(miniTable([
    { key: "aftersale_id", label: "编号" }, { key: "order_no", label: "订单" },
    { key: "sku", label: "SKU" }, { key: "fault_type", label: "故障" },
    { key: "status", label: "状态" },
  ], data.aftersales || []));
  pane.appendChild(el("h4", { text: "对账" }));
  pane.appendChild(miniTable([
    { key: "period", label: "期间" }, { key: "amount", label: "金额" },
    { key: "status", label: "状态" }, { key: "invoice_status", label: "开票" },
    { key: "due_date", label: "到期" },
  ], data.reconciliations || []));
  pane.appendChild(el("h4", { text: "Agent 已查询数据" }));
  pane.appendChild(miniTable([
    { key: "stage", label: "阶段" }, { key: "domain", label: "域" },
    { key: "tool", label: "工具" }, { key: "status", label: "状态" },
    { key: "output_summary", label: "输出" },
  ], (data.agent_queries || []).map((q) => ({
    ...q, output_summary: String(q.output_summary || "").slice(0, 80),
  }))));
}

/* ---------------- 工单 ---------------- */

function ticketItem(ticket) {
  const item = el("div", { className: "side-item" });
  const line = el("div");
  line.appendChild(el("b", { text: ticket.ticket_no }));
  line.appendChild(document.createTextNode(
    ` · ${ticket.sku || "-"} · ${ticket.status || "-"} · ${ticket.department || "未分派"}`,
  ));
  item.appendChild(line);
  item.appendChild(el("div", {
    className: "meta",
    text: `${ticket.fault_type || "-"} · 优先级 ${ticket.priority || "-"}`
      + ` · 处理人 ${ticket.assignee_id || "-"}`,
  }));
  const row = el("div", { className: "row" });
  const status = el("select");
  for (const value of ["created", "received", "repairing", "resolved", "closed"]) {
    status.appendChild(el("option", { text: value, attrs: { value } }));
  }
  status.value = ticket.status || "created";
  const update = el("button", { className: "btn-ghost", text: "更新状态", attrs: { type: "button" } });
  update.addEventListener("click", () => busy(update, async () => {
    await api(`/api/tickets/${ticket.ticket_no}`, {
      method: "PATCH", body: { status: status.value },
    });
    toast("工单状态已更新", "ok");
    await loadTickets();
  }));
  const assign = el("button", { className: "btn-ghost", text: "分派", attrs: { type: "button" } });
  assign.addEventListener("click", () => busy(assign, async () => {
    const department = state.ticketDept || "warehouse";
    await api(`/api/tickets/${ticket.ticket_no}/assign`, {
      method: "POST", body: { department },
    });
    toast(`已分派到 ${department}`, "ok");
    await loadTickets();
  }));
  row.appendChild(status);
  row.appendChild(update);
  row.appendChild(assign);
  item.appendChild(row);
  return item;
}

async function loadTickets() {
  if (!state.cid || state.role === "finance") return;
  const pane = $("side-tickets");
  clear(pane);
  const customerId = (state.conv && state.conv.customer_id) || "";
  if (!customerId) {
    pane.appendChild(emptyNode("该会话未绑定客户"));
    return;
  }
  pane.appendChild(el("h4", { text: "新建工单" }));
  const form = el("div", { className: "side-form" });
  const sku = el("input", { attrs: { placeholder: "SKU，如 SKU-A001" } });
  const fault = el("input", { attrs: { placeholder: "故障类型" } });
  const priority = el("select");
  for (const value of ["normal", "high", "medium"]) {
    priority.appendChild(el("option", { text: value, attrs: { value } }));
  }
  const department = el("select");
  department.appendChild(el("option", { text: "不指定", attrs: { value: "" } }));
  department.appendChild(el("option", { text: "warehouse", attrs: { value: "warehouse" } }));
  department.appendChild(el("option", { text: "finance", attrs: { value: "finance" } }));
  const desc = el("textarea", { className: "full", attrs: { rows: "2", placeholder: "问题描述" } });
  const create = el("button", { className: "btn-primary", text: "创建工单", attrs: { type: "button" } });
  create.addEventListener("click", () => busy(create, async () => {
    if (!sku.value.trim()) {
      toast("请填写 SKU", "error");
      return;
    }
    await api("/api/tickets", {
      method: "POST",
      body: {
        customer_id: customerId, sku: sku.value.trim(),
        fault_type: fault.value.trim() || "其他",
        priority: priority.value, department: department.value,
        description: desc.value.trim(), conversation_id: state.cid,
      },
    });
    toast("工单已创建", "ok");
    sku.value = "";
    fault.value = "";
    desc.value = "";
    await loadTickets();
  }));
  form.appendChild(sku);
  form.appendChild(fault);
  form.appendChild(priority);
  form.appendChild(department);
  form.appendChild(desc);
  form.appendChild(create);
  pane.appendChild(form);
  const assignDept = el("select");
  assignDept.appendChild(el("option", { text: "分派到 warehouse", attrs: { value: "warehouse" } }));
  assignDept.appendChild(el("option", { text: "分派到 finance", attrs: { value: "finance" } }));
  assignDept.addEventListener("change", () => { state.ticketDept = assignDept.value; });
  state.ticketDept = "warehouse";
  pane.appendChild(el("h4", { text: "该客户工单" }));
  pane.appendChild(assignDept);
  const data = await api(`/api/tickets?customer_id=${encodeURIComponent(customerId)}`);
  const list = el("div", { className: "side-list" });
  for (const ticket of data.tickets || []) list.appendChild(ticketItem(ticket));
  if (!(data.tickets || []).length) list.appendChild(emptyNode("暂无工单"));
  pane.appendChild(list);
}

/* ---------------- 知识库 ---------------- */

function knowledgeEditor(entry) {
  const form = el("div", { className: "side-form" });
  const title = el("input", { className: "full", attrs: { placeholder: "标题" } });
  title.value = entry.title || "";
  const content = el("textarea", { className: "full", attrs: { rows: "4", placeholder: "内容" } });
  content.value = entry.content || "";
  const scope = el("input", { attrs: { placeholder: "scope" } });
  scope.value = entry.scope || "general";
  const keywords = el("input", { attrs: { placeholder: "关键词" } });
  keywords.value = entry.keywords || "";
  const status = el("select");
  const options = state.canPublish
    ? ["draft", "submitted", "published", "archived"]
    : ["draft", "submitted"];
  for (const value of options) status.appendChild(el("option", { text: value, attrs: { value } }));
  status.value = entry.status && options.includes(entry.status) ? entry.status : "draft";
  const save = el("button", { className: "btn-primary", text: "保存版本", attrs: { type: "button" } });
  save.addEventListener("click", () => busy(save, async () => {
    if (!title.value.trim() || !content.value.trim()) {
      toast("标题与内容不能为空", "error");
      return;
    }
    const body = {
      title: title.value.trim(), content: content.value.trim(),
      scope: scope.value.trim() || "general",
      keywords: keywords.value.trim(), status: status.value,
    };
    if (entry.entry_id) body.entry_id = entry.entry_id;
    const result = await api("/api/knowledge", { method: "POST", body });
    toast(`已保存 ${result.entry_id}@${result.version}（${result.status}）`, "ok");
    const qInput = $("kb-q");
    await loadKnowledge(qInput ? qInput.value.trim() : "", state.kbStatus);
  }));
  form.appendChild(title);
  form.appendChild(content);
  form.appendChild(scope);
  form.appendChild(keywords);
  form.appendChild(status);
  form.appendChild(save);
  return form;
}

async function loadKnowledge(q = "", status = "all") {
  if (state.role === "finance") return;
  const pane = $("side-knowledge");
  clear(pane);
  const search = el("div", { className: "kb-search" });
  const input = el("input", { attrs: { placeholder: "搜索知识库", id: "kb-q" } });
  input.value = q;
  const select = el("select");
  for (const value of ["all", "draft", "submitted", "published", "archived"]) {
    select.appendChild(el("option", { text: value, attrs: { value } }));
  }
  select.value = status;
  state.kbStatus = status;
  const btn = el("button", { className: "btn-ghost", text: "搜索", attrs: { type: "button" } });
  btn.addEventListener("click", () => loadKnowledge(input.value.trim(), select.value));
  select.addEventListener("change", () => {
    state.kbStatus = select.value;
    loadKnowledge(input.value.trim(), select.value);
  });
  search.appendChild(input);
  search.appendChild(select);
  search.appendChild(btn);
  pane.appendChild(search);
  const data = await api(
    `/api/knowledge?status=${encodeURIComponent(status)}&q=${encodeURIComponent(q)}`,
  );
  const entries = data.entries || [];
  const list = el("div", { className: "side-list" });
  for (const entry of entries) {
    const item = el("div", { className: "side-item" });
    item.appendChild(el("b", { text: `${entry.title || entry.entry_id} @${entry.version}` }));
    item.appendChild(el("div", {
      className: "meta",
      text: `${entry.entry_id} · ${entry.status} · ${String(entry.content || "").slice(0, 60)}`,
    }));
    const row = el("div", { className: "row" });
    const edit = el("button", { className: "btn-ghost", text: "编辑", attrs: { type: "button" } });
    edit.addEventListener("click", () => {
      clear(pane);
      pane.appendChild(el("h4", { text: `编辑 ${entry.entry_id}` }));
      pane.appendChild(knowledgeEditor(entry));
      const back = el("button", { className: "btn-ghost", text: "返回列表", attrs: { type: "button" } });
      back.addEventListener("click", () => loadKnowledge(q));
      pane.appendChild(back);
    });
    const versions = el("button", { className: "btn-ghost", text: "版本", attrs: { type: "button" } });
    versions.addEventListener("click", () => busy(versions, async () => {
      const v = await api(`/api/knowledge/${entry.entry_id}/versions`);
      toast(`共 ${(v.versions || []).length} 个版本：`
        + (v.versions || []).map((x) => `${x.version}/${x.status}`).join("，"), "");
    }));
    row.appendChild(edit);
    row.appendChild(versions);
    item.appendChild(row);
    list.appendChild(item);
  }
  if (!entries.length) list.appendChild(emptyNode("暂无知识条目"));
  pane.appendChild(list);
}


/* ---------------- AI 查询（toB 主 Agent + 工具面板） ---------------- */

function assistMessageNode(item) {
  const node = el("div", { className: `assist-msg ${item.role}` });
  node.appendChild(el("div", { text: item.content || "" }));
  if (item.role === "ai" && item.content && item.content !== "思考中…") {
    const btn = el("button", {
      className: "insert-btn", text: "插入回复框", attrs: { type: "button" },
    });
    btn.addEventListener("click", () => {
      const input = $("agent-input");
      input.value = input.value ? `${input.value}\n${item.content}` : item.content;
      input.focus();
    });
    node.appendChild(btn);
  }
  return node;
}

function appendAssist(item) {
  const list = $("assist-list");
  if (!list) return null;
  const empty = list.querySelector(".empty");
  if (empty) empty.remove();
  const node = assistMessageNode(item);
  list.appendChild(node);
  list.scrollTop = list.scrollHeight;
  return node;
}

async function loadAssist() {
  const pane = $("side-assist");
  if (!pane) return;
  clear(pane);
  const hint = el("div", { className: "hint" });
  const boundCustomer = state.conv && state.conv.customer_id;
  hint.textContent = state.cid
    ? `当前客户：${boundCustomer || "-"}；可问订单、售后资格、授信、物流、商品等。`
    : "未选择会话：商品/库存类可直接问；订单/授信类请先在左侧选中会话。";
  pane.appendChild(el("h4", { text: "问主 Agent（toB）" }));
  pane.appendChild(hint);

  const list = el("div", { className: "assist-list", attrs: { id: "assist-list" } });
  pane.appendChild(list);
  try {
    const history = await api("/api/assist/history");
    if (!history.length) {
      list.appendChild(emptyNode("还没有提问记录"));
    } else {
      for (const item of history.slice(-20)) list.appendChild(assistMessageNode(item));
      list.scrollTop = list.scrollHeight;
    }
  } catch (err) {
    list.appendChild(emptyNode(errText(err)));
  }

  const inputRow = el("div", { className: "assist-input" });
  const input = el("input", {
    attrs: { id: "assist-input", placeholder: "如：SKU-A001 的售后资格 / 我的授信额度" },
  });
  const send = el("button", {
    className: "btn-primary", text: "发送", attrs: { type: "button", id: "assist-send" },
  });
  send.addEventListener("click", () => assistSend().catch((err) => {
    if (!handleAuth(err)) toast(errText(err), "error");
  }));
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      send.click();
    }
  });
  inputRow.appendChild(input);
  inputRow.appendChild(send);
  pane.appendChild(inputRow);

  pane.appendChild(el("h4", { text: "工具面板（只读 · 调用写审计）" }));
  const tools = el("div", { attrs: { id: "tool-list" } });
  pane.appendChild(tools);
  await loadTools(tools);
}

async function assistSend() {
  const input = $("assist-input");
  if (!input) return;
  const question = input.value.trim();
  if (!question) return;
  input.value = "";
  appendAssist({ role: "agent", content: question });
  const busyNode = appendAssist({ role: "ai", content: "思考中…" });
  const send = $("assist-send");
  if (send) send.disabled = true;
  try {
    const result = await api("/api/assist/ask", {
      method: "POST",
      body: {
        question,
        task_id: state.assistTaskId,
        conversation_id: state.cid || "",
      },
    });
    state.assistTaskId = result.task_id || null;
    if (busyNode) busyNode.remove();
    appendAssist({ role: "ai", content: result.reply || "（无回复）" });
  } catch (err) {
    if (busyNode) busyNode.remove();
    appendAssist({ role: "ai", content: `调用失败：${errText(err)}` });
    throw err;
  } finally {
    if (send) send.disabled = false;
  }
}


/* ---------------- 工具结果渲染（字段卡片，不直接糊 JSON） ---------------- */

const TOOL_FIELD_LABELS = {
  order_no: "订单号", status_name: "状态", status: "状态", amount: "金额",
  sku: "SKU", qty: "数量", created_at: "创建时间", updated_at: "更新时间",
  address: "收货地址", pay_channel: "支付渠道", paid_at: "支付时间",
  receiver: "收货人", phone: "电话", customer_id: "客户ID", name: "姓名",
  tier: "等级", city: "城市", registered_at: "注册时间", customer_code: "客户编码",
  brand: "品牌", model: "型号", price: "价格", stock: "库存",
  lead_time_days: "备货天数", available: "可售库存", ats_days: "可承诺天数",
  on_sale: "是否在售", hint: "说明", tracking_no: "运单号",
  shipped_at: "发货时间", estimated_delivery: "预计送达",
  ticket_no: "工单号", fault_type: "故障类型", department: "部门",
  priority: "优先级", assignee_id: "处理人", sla_due: "SLA 截止",
  description: "描述", clause_id: "条款号", version: "版本",
  title: "标题", text: "条款内容", effective_range: "生效范围",
  order_status: "订单状态",
};
const TOOL_VALUE_LABELS = {
  signed: "已签收", shipped: "已发货", pending_payment: "待支付",
  in_transit: "运输中", out_for_delivery: "派送中", delivered: "已签收",
  collected: "已揽收", created: "已创建", received: "已受理",
  repairing: "维修中", resolved: "已解决", closed: "已关闭",
  on_sale: "在售", off_sale: "下架", high: "高", medium: "中", normal: "普通",
  warehouse: "仓库", finance: "财务", alipay: "支付宝", wechat: "微信",
};
const TOOL_INTERNAL_KEYS = new Set([
  "tenant_id", "snapshot_id", "tool_events", "as_of", "authority_domain",
  "skill", "biz_key", "node_id", "nodes",
]);

function unwrapToolData(result) {
  let data = result.data;
  if (data && !Array.isArray(data) && typeof data === "object"
      && data.data !== undefined) {
    data = data.data;
  }
  return data;
}

function toolTitle(toolId, record) {
  const pick = (...keys) => {
    for (const key of keys) {
      if (record && record[key]) return record[key];
    }
    return "";
  };
  switch (toolId) {
    case "customer.query": return `客户 ${pick("name", "customer_id")}`.trim();
    case "product.lookup": return `商品 ${pick("name", "sku")}`.trim();
    case "order.query": return `订单 ${pick("order_no")}`.trim();
    case "fulfill.track": return `物流 ${pick("order_no", "tracking_no")}`.trim();
    case "fulfill.atp": return `库存 ${pick("sku")}`.trim();
    case "ticket.query": return `工单 ${pick("ticket_no")}`.trim();
    case "policy.clause": return `条款 ${pick("clause_id")} ${pick("version")}`.trim();
    default: return pick("name", "title", "order_no", "sku", "ticket_no") || "查询结果";
  }
}

function toolValue(key, value) {
  if (value === null || value === undefined || value === "") return "-";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (key === "amount" || key === "price") return `${value} 元`;
  if (key === "lead_time_days" || key === "ats_days") return `${value} 天`;
  const label = TOOL_VALUE_LABELS[String(value)];
  return label || String(value);
}

function renderToolRecord(container, toolId, record) {
  const box = el("div", { className: "tool-record" });
  box.appendChild(el("div", { className: "tool-record-title", text: toolTitle(toolId, record) }));
  const grid = el("div", { className: "tool-kv" });
  const keys = Object.keys(record || {});
  const preferred = [
    "order_no", "ticket_no", "customer_id", "name", "tier", "city",
    "status_name", "status", "amount", "price", "sku", "qty", "stock",
    "available", "ats_days", "tracking_no", "estimated_delivery",
    "created_at", "paid_at", "shipped_at", "address", "receiver", "phone",
    "fault_type", "department", "priority", "assignee_id", "sla_due",
    "title", "effective_range", "version", "clause_id", "text", "description",
  ];
  const ordered = preferred.filter((k) => keys.includes(k));
  for (const key of keys) {
    if (!ordered.includes(key)) ordered.push(key);
  }
  for (const key of ordered) {
    if (TOOL_INTERNAL_KEYS.has(key)) continue;
    if (key === "status" && record.status_name) continue;
    const value = record[key];
    if (value !== null && typeof value === "object") continue;
    grid.appendChild(el("span", { className: "k", text: TOOL_FIELD_LABELS[key] || key }));
    grid.appendChild(el("span", { className: "v", text: toolValue(key, value) }));
  }
  box.appendChild(grid);
  if (Array.isArray(record.nodes) && record.nodes.length) {
    const timeline = el("div", { className: "tool-nodes" });
    for (const node of record.nodes.slice(-6).reverse()) {
      timeline.appendChild(el("div", {
        className: "tool-node",
        text: `${node.node_time || ""} ${node.status_name || node.description || ""}`
          + (node.location ? ` · ${node.location}` : ""),
      }));
    }
    box.appendChild(el("div", { className: "tool-record-title", text: "物流节点" }));
    box.appendChild(timeline);
  }
  container.appendChild(box);
}

function renderToolResult(box, tool, result) {
  clear(box);
  if (result.state === "TASK_STATE_REJECTED"
      || result.state === "TASK_STATE_FAILED") {
    box.appendChild(el("div", {
      text: result.text || "调用被拒绝",
    }));
    return;
  }
  if (result.text) {
    box.appendChild(el("div", { className: "tool-summary", text: result.text }));
  }
  const data = unwrapToolData(result);
  const records = Array.isArray(data) ? data : (data && typeof data === "object" ? [data] : []);
  if (!records.length) {
    if (!result.text) box.appendChild(el("div", { text: "（无数据）" }));
    return;
  }
  for (const record of records.slice(0, 10)) {
    renderToolRecord(box, tool.id, record);
  }
  const raw = JSON.stringify(result, null, 2);
  const details = el("details", { className: "tool-raw" });
  details.appendChild(el("summary", { text: "查看原始数据" }));
  const pre = el("pre");
  pre.textContent = raw.length > 4000 ? `${raw.slice(0, 4000)}…` : raw;
  details.appendChild(pre);
  box.appendChild(details);
}

async function loadTools(container) {
  let tools = [];
  try {
    tools = await api("/api/tools");
  } catch (err) {
    container.appendChild(emptyNode(errText(err)));
    return;
  }
  clear(container);
  if (!tools.length) {
    container.appendChild(emptyNode("无可用工具"));
    return;
  }
  const customerId = (state.conv && state.conv.customer_id) || "";
  let currentGroup = null;
  for (const tool of tools) {
    // 能力模块分组（产品视图）：后端已按模块声明序排序，组头随分组切换插入
    const group = tool.module_label || "";
    if (group && group !== currentGroup) {
      currentGroup = group;
      container.appendChild(el("h4", { className: "tool-group", text: group }));
    }
    const card = el("div", { className: "tool-card" });
    const head = el("div", { className: "tool-head" });
    head.appendChild(el("b", { text: tool.name }));
    head.appendChild(el("span", {
      className: "src",
      text: `${tool.id} · ${tool.source === "console" ? "坐席专属" : `A2A ${tool.domain}`}`,
    }));
    card.appendChild(head);
    if (tool.requires_customer) {
      card.appendChild(el("div", {
        className: `tool-context${customerId ? "" : " missing"}`,
        text: customerId
          ? `客户上下文：${customerId}`
          : "未绑定客户：请先在左侧选中会话",
      }));
    }
    const params_declared = tool.params || [];
    const inputs = params_declared.map((param) => {
      const input = el("input", {
        attrs: { placeholder: param.placeholder || "参数值", "data-param": param.name || "" },
      });
      if (tool.requires_customer && customerId
          && ["query", "customer_id"].includes(param.name)) {
        // 常用工具直接用当前会话客户预填，避免坐席手动复制客户号
        // （仅限绑定客户的工具：营销分析等的 query 是分析范围，不能预填客户号）
        input.value = customerId;
      }
      card.appendChild(input);
      return input;
    });
    const firstParam = params_declared[0] || {};
    const row = el("div", { className: "row" });
    const invoke = el("button", { className: "btn-ghost", text: "调用", attrs: { type: "button" } });
    if (tool.requires_customer && !customerId) {
      invoke.disabled = true;
      invoke.title = "未绑定客户，请先选择会话";
    }
    const resultBox = el("div", { className: "tool-result hidden" });
    invoke.addEventListener("click", () => busy(invoke, async () => {
      // 按声明参数逐个收集（旧版只取第一个参数，多参数工具无法完整传参）
      const values = inputs.map((input) => input.value.trim());
      const missing = params_declared.some((param, i) => param.name && !values[i]);
      if (missing) {
        toast("请先填写参数", "error");
        return;
      }
      resultBox.classList.remove("hidden");
      resultBox.className = "tool-result";
      clear(resultBox);
      resultBox.textContent = "调用中…";
      const params = {};
      if (customerId) params.customer_id = customerId;
      params_declared.forEach((param, i) => {
        if (param.name) params[param.name] = values[i];
      });
      const body = { params, conversation_id: state.cid || "" };
      const result = await api(`/api/tools/${tool.id}/invoke`, {
        method: "POST", body,
      });
      resultBox.className = "tool-result";
      renderToolResult(resultBox, tool, result);
      if (result.state === "TASK_STATE_REJECTED"
          || result.state === "TASK_STATE_FAILED") {
        resultBox.classList.add("rejected");
      }
    }));
    row.appendChild(invoke);
    card.appendChild(row);
    card.appendChild(resultBox);
    container.appendChild(card);
  }
}

/* ---------------- 统计 ---------------- */

async function loadStats() {
  const pane = $("side-stats");
  clear(pane);
  if (!["supervisor", "admin"].includes(state.role)) {
    pane.appendChild(emptyNode("统计仅主管/管理员可见"));
    return;
  }
  const data = await api("/api/stats/summary");
  const grid = el("div", { className: "stat-grid" });
  const cards = [
    ["会话总数", data.total_conversations],
    ["转人工率", `${(data.handoff_rate * 100).toFixed(1)}%`],
    ["AI 承接率", `${(data.ai_handled_rate * 100).toFixed(1)}%`],
    ["人工承接", data.human_handled_conversations],
    ["平均时长", `${Math.round(data.avg_closed_duration_seconds)}s`],
    ["驳回率", `${(data.rejection_rate * 100).toFixed(1)}%`],
    ["AI 消息", data.ai_messages],
    ["反馈数", data.feedback_count],
  ];
  for (const [key, value] of cards) {
    const card = el("div", { className: "stat-card" });
    card.appendChild(el("div", { className: "v", text: String(value) }));
    card.appendChild(el("div", { className: "k", text: key }));
    grid.appendChild(card);
  }
  pane.appendChild(grid);
  pane.appendChild(el("h4", { text: "诉求分布" }));
  const rows = Object.entries(data.intent_distribution || {})
    .sort((a, b) => b[1] - a[1])
    .map(([intent, count]) => ({ intent, count }));
  pane.appendChild(miniTable([
    { key: "intent", label: "意图" }, { key: "count", label: "次数" },
  ], rows));
}

/* ---------------- 审批 ---------------- */

async function loadApprovals() {
  const pane = $("side-approvals");
  clear(pane);
  let list = [];
  try {
    list = await api("/api/approvals");
  } catch (err) {
    pane.appendChild(emptyNode(errText(err)));
    return;
  }
  list = (list || []).filter((a) => ["pending", "approved"].includes(a.status)).slice(0, 20);
  if (!list.length) {
    pane.appendChild(emptyNode("暂无待审批"));
    return;
  }
  for (const approval of list) {
    const card = el("div", { className: "approval-card" });
    card.appendChild(el("b", { text: approval.action }));
    card.appendChild(el("span", {
      className: "meta", text: ` · ${approval.status}`,
    }));
    card.appendChild(el("div", {
      text: `对象 ${approval.target || "-"} · 金额 ${approval.amount ?? "-"} 元`,
    }));
    card.appendChild(el("div", {
      className: "meta", text: approval.reason || "",
    }));
    card.appendChild(el("div", {
      className: "meta",
      text: `申请人 ${approval.requested_by || "-"} · 已批 `
        + `${(approval.approvals || []).length}/${approval.required_approvals || 1}`,
    }));
    if (state.canApprove) {
      const row = el("div", { className: "row" });
      if (approval.status === "pending") {
        const reason = el("input", { attrs: { placeholder: "驳回原因（驳回时必填）" } });
        const ok = el("button", { className: "btn-ok", text: "批准", attrs: { type: "button" } });
        ok.addEventListener("click", () => busy(ok, async () => {
          const result = await api(`/api/approvals/${approval.id}/approve`, { method: "POST" });
          toast(result.status === "approved" ? "已满足审批人数" : "已记录，等待其他复核人", "ok");
          await loadApprovals();
        }));
        const no = el("button", { className: "btn-danger", text: "驳回", attrs: { type: "button" } });
        no.addEventListener("click", () => busy(no, async () => {
          if (!reason.value.trim()) {
            toast("请填写驳回原因", "error");
            return;
          }
          await api(`/api/approvals/${approval.id}/reject`, {
            method: "POST", body: { reason: reason.value.trim() },
          });
          toast("已驳回", "ok");
          await loadApprovals();
        }));
        row.appendChild(reason);
        row.appendChild(ok);
        row.appendChild(no);
      } else {
        const exec = el("button", { className: "btn-ok", text: "执行", attrs: { type: "button" } });
        exec.addEventListener("click", () => busy(exec, async () => {
          await api(`/api/approvals/${approval.id}/execute`, { method: "POST" });
          toast("审批动作已执行", "ok");
          await loadApprovals();
          if (state.cid) await refreshCurrent();
        }));
        row.appendChild(exec);
      }
      card.appendChild(row);
    } else {
      card.appendChild(el("div", {
        className: "meta", text: "只读：需财务/主管审批",
      }));
    }
    pane.appendChild(card);
  }
}

/* ---------------- 右栏 tab ---------------- */

async function switchSide(side) {
  state.side = side;
  for (const tab of document.querySelectorAll(".side-tab")) {
    tab.classList.toggle("active", tab.dataset.side === side);
  }
  for (const pane of document.querySelectorAll(".side-pane")) {
    pane.classList.toggle("hidden", pane.id !== `side-${side}`);
  }
  try {
    if (side === "360" && state.cid) await load360();
    else if (side === "tickets" && state.cid) await loadTickets();
    else if (side === "knowledge") await loadKnowledge(
      $("kb-q") ? $("kb-q").value : "", state.kbStatus);
    else if (side === "assist") await loadAssist();
    else if (side === "stats") await loadStats();
    else if (side === "approvals") await loadApprovals();
  } catch (err) {
    if (!handleAuth(err)) toast(errText(err), "error");
  }
}

for (const tab of document.querySelectorAll(".side-tab")) {
  tab.addEventListener("click", () => switchSide(tab.dataset.side));
}

/* ---------------- 队列 tab / 过滤 ---------------- */

for (const tab of document.querySelectorAll("#queue-tabs .tab")) {
  tab.addEventListener("click", () => {
    state.tab = tab.dataset.tab;
    for (const other of document.querySelectorAll("#queue-tabs .tab")) {
      other.classList.toggle("active", other === tab);
    }
    loadQueue(true).catch((err) => toast(errText(err), "error"));
  });
}

let searchTimer = null;
$("queue-search").addEventListener("input", () => {
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadQueue(true).catch(() => {}), 350);
});
$("queue-priority").addEventListener("change", () => loadQueue(true).catch(() => {}));
$("queue-sort").addEventListener("change", () => loadQueue(true).catch(() => {}));
$("queue-more").addEventListener("click", () => loadQueue(false, true).catch(() => {}));

/* ---------------- 登录事件 ---------------- */

$("login-btn").addEventListener("click", login);
$("password").addEventListener("keydown", (event) => {
  if (event.key === "Enter") login();
});
$("logout-link").addEventListener("click", logout);

init();
