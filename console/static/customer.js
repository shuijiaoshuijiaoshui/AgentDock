/* B 端客户侧：登录 → 自动问候 → 消息/附件/卡片/确认 → 转人工 → 历史。
 *
 * 单一事实源：聊天区所有气泡都来自服务端消息（初始快照 / WS snapshot /
 * after_id 增量同步），本地不生成假消息，按 message.id 去重，因此
 * refreshHistory 与 WS snapshot 不会双渲染。
 */

import { ApiError, api, setCsrf, setSurface, uploadAttachment } from "./js/api.js";
import {
  cardNode, clear, el, emptyNode, loadingNode, messageNode, pendingNode,
  stateBadge,
} from "./js/components.js";
import { ChatSocket } from "./js/ws.js";

setSurface("customer");

const $ = (id) => document.getElementById(id);

const chatEl = $("chat");
const inputEl = $("input");
const sendBtn = $("send-btn");
const attachBtn = $("attach-btn");
const fileInput = $("file-input");
const previewsEl = $("attach-previews");
const errorBox = $("error-box");
const errorText = $("error-text");
const retryBtn = $("retry-btn");
const closedNotice = $("closed-notice");
const loginBox = $("login");
const loginError = $("login-error");
const sideList = $("side-list");
const historyDrawer = $("history-drawer");
const historyList = $("history-list");
const historyMore = $("history-more");

const QUICK_TEXTS = {
  orders: "我的订单",
  recon: "查一下我的对账单",
  invoice: "查一下开票进度",
  aftersale: "查询 SKU-A001 的售后资格",
};

const state = {
  me: null,
  conversation: null,
  conversationId: "",
  lastServerId: 0,       // 已渲染的最大 message.id（增量同步游标）
  rendered: new Set(),   // 已渲染的 message.id
  pendingFiles: [],      // [{file, previewUrl, attachmentId, uploading, error}]
  socket: null,
  sending: false,
  closed: false,
  historyBefore: "",
};

/* ---------------- 状态与错误 ---------------- */

function setStatus(text) {
  $("status").textContent = text;
}

function badgeState(conv) {
  const holder = $("state-badge");
  const badge = stateBadge(conv && conv.state, conv && conv.status);
  badge.id = "state-badge";
  holder.replaceWith(badge);
}

function applyConversation(conv) {
  if (!conv) return;
  state.conversation = conv;
  badgeState(conv);
  const closed = conv.state === "CLOSED" || conv.status === "closed";
  state.closed = closed;
  closedNotice.classList.toggle("hidden", !closed);
  $("handoff-btn").classList.toggle("hidden", closed);
  inputEl.disabled = closed;
  attachBtn.disabled = closed;
  for (const chip of document.querySelectorAll("[data-quick]")) {
    chip.disabled = closed;
  }
  updateSendState();
  if (closed) setStatus("会话已结束");
  else if (conv.state === "PENDING_HANDOFF") setStatus("已转人工 · 排队中");
  else if (conv.state === "HUMAN_ACTIVE") setStatus("人工客服服务中");
  else if (conv.state === "PENDING_REVIEW") setStatus("AI 服务中 · 待主管复核");
  else setStatus("AI 助手服务中");
}

function showError(message, onRetry) {
  errorText.textContent = message;
  retryBtn.classList.toggle("hidden", !onRetry);
  retryBtn.onclick = onRetry || null;
  errorBox.classList.remove("hidden");
}

function hideError() {
  errorBox.classList.add("hidden");
}

function handleAuthError(err) {
  if (err instanceof ApiError && err.status === 401) {
    showLogin();
    return true;
  }
  return false;
}

/* ---------------- 渲染（按 id 去重，唯一事实源） ---------------- */

function appendMessage(message) {
  if (!message || message.id === undefined || message.id === null) return;
  if (state.rendered.has(message.id)) return;
  state.rendered.add(message.id);
  if (typeof message.id === "number" && message.id > state.lastServerId) {
    state.lastServerId = message.id;
  }
  const empty = chatEl.querySelector(".empty");
  if (empty) empty.remove();
  chatEl.appendChild(messageNode(message));
  const meta = message.meta || {};
  for (const card of meta.cards || []) {
    chatEl.appendChild(cardNode(card, { onAction: handleCardAction }));
  }
  for (const pending of meta.pending_actions || []) {
    chatEl.appendChild(pendingNode(pending, { onConfirm: handleConfirm }));
  }
}

function renderAll(messages) {
  clear(chatEl);
  state.rendered.clear();
  state.lastServerId = 0;
  if (!messages || !messages.length) {
    chatEl.appendChild(emptyNode("还没有消息"));
  } else {
    for (const message of messages) appendMessage(message);
  }
  chatEl.scrollTop = chatEl.scrollHeight;
}

/** 增量同步：after_id 之后的服务端消息；与 WS 事件按 id 去重。 */
async function syncAfter() {
  if (!state.conversationId) return;
  const data = await api(
    `/api/conversations/${state.conversationId}/messages?after_id=${state.lastServerId}`,
  );
  for (const message of data) appendMessage(message);
  chatEl.scrollTop = chatEl.scrollHeight;
}

/* ---------------- 会话 ---------------- */

async function loadCurrentConversation() {
  setStatus("正在加载会话…");
  clear(chatEl);
  chatEl.appendChild(loadingNode());
  try {
    const data = await api("/api/customer/conversation");
    state.me = data.customer || {};
    state.conversationId = (data.conversation || {}).id || "";
    $("who").textContent = state.me.display_name
      ? `${state.me.display_name}（${state.me.customer_id}）`
      : (state.me.customer_id || "未登录");
    applyConversation(data.conversation);
    renderAll(data.messages || []);
    hideError();
    connectSocket();
    loadSideList();
  } catch (err) {
    if (handleAuthError(err)) return;
    clear(chatEl);
    chatEl.appendChild(emptyNode("会话加载失败"));
    chatEl.appendChild(retryRow("重试", loadCurrentConversation));
  }
}

function retryRow(label, handler) {
  const row = el("div", { className: "card-actions" });
  const btn = el("button", { className: "btn-ghost", text: label, attrs: { type: "button" } });
  btn.addEventListener("click", handler);
  row.appendChild(btn);
  return row;
}

async function openConversation(cid) {
  if (!cid || cid === state.conversationId) {
    historyDrawer.classList.add("hidden");
    return;
  }
  let conv = null;
  try {
    const list = await api("/api/conversations");
    conv = list.find((item) => item.id === cid) || null;
  } catch (err) {
    if (handleAuthError(err)) return;
    showError(`打开会话失败：${err.message}`);
    return;
  }
  if (!conv) {
    showError("会话不存在或无权访问");
    return;
  }
  state.conversationId = cid;
  state.lastServerId = 0;
  applyConversation(conv);
  try {
    const messages = await api(`/api/conversations/${cid}/messages`);
    renderAll(messages);
    hideError();
  } catch (err) {
    if (handleAuthError(err)) return;
    clear(chatEl);
    chatEl.appendChild(emptyNode("消息加载失败"));
    return;
  }
  connectSocket();
  historyDrawer.classList.add("hidden");
}

function connectSocket() {
  if (state.socket) state.socket.close();
  if (!state.conversationId) return;
  state.socket = new ChatSocket({
    conversationId: state.conversationId,
    surface: "customer",
    onSnapshot: (evt) => {
      if (evt.conversation) applyConversation(evt.conversation);
      renderAll(evt.messages || []);
    },
    onMessage: (message, conversation) => {
      if (conversation) applyConversation(conversation);
      appendMessage(message);
      chatEl.scrollTop = chatEl.scrollHeight;
    },
    onState: (conversation) => applyConversation(conversation),
    onStatus: (status) => {
      if (status === "disconnected" && !state.closed) {
        setStatus("连接断开，正在重连…");
      }
    },
  });
  state.socket.connect();
}

/* ---------------- 发送 ---------------- */

function updateSendState() {
  const uploading = state.pendingFiles.some((item) => item.uploading);
  sendBtn.disabled = state.closed || state.sending || uploading;
  sendBtn.textContent = state.sending ? "发送中…" : "发送";
}

async function sendMessage(rawText) {
  const text = String(rawText || "").trim();
  const pending = state.pendingFiles;
  const uploading = pending.some((item) => item.uploading);
  if (uploading) {
    showError("附件仍在上传，请稍候再发送");
    return;
  }
  const failed = pending.some((item) => !item.attachmentId);
  if (failed) {
    showError("有附件上传失败，请移除后重试");
    return;
  }
  const attachmentIds = pending.map((item) => item.attachmentId);
  if (!text && !attachmentIds.length) return;
  if (state.closed) {
    showError("会话已关闭，输入已禁用。");
    return;
  }
  hideError();
  state.sending = true;
  updateSendState();
  inputEl.value = "";
  // 用户消息由服务端先落库再广播/返回；这里不做本地假消息，避免双渲染。
  const sending = api("/api/customer/send", {
    method: "POST",
    body: {
      conversation_id: state.conversationId,
      text: text || "请查看附件",
      attachment_ids: attachmentIds,
    },
  });
  const early = setTimeout(() => {
    syncAfter().catch(() => {});
  }, 150);
  try {
    const data = await sending;
    clearTimeout(early);
    if (data.conversation_id && data.conversation_id !== state.conversationId) {
      state.conversationId = data.conversation_id;
      connectSocket();
    }
    clearPreviews();
    await syncAfter();
    try {
      const convs = await api("/api/conversations");
      const conv = convs.find((item) => item.id === state.conversationId);
      if (conv) applyConversation(conv);
    } catch (_) {
      /* 状态刷新非关键：WS 的 conversation 事件会补 */
    }
  } catch (err) {
    clearTimeout(early);
    if (handleAuthError(err)) return;
    showError(`发送失败：${err.message}`, () => sendMessage(text));
    inputEl.value = text;
  } finally {
    state.sending = false;
    updateSendState();
    inputEl.focus();
  }
}

/* ---------------- 卡片 / 确认 ---------------- */

async function handleCardAction(action, button) {
  if (state.closed) {
    showError("会话已关闭，无法执行卡片操作。");
    return;
  }
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "处理中…";
  try {
    // 只发送 action_id + 展示参数；intent/slots 由服务端白名单映射。
    await api(`/api/conversations/${state.conversationId}/card-action`, {
      method: "POST",
      body: { action_id: action.action_id, params: action.params || {} },
    });
    await syncAfter();
  } catch (err) {
    if (handleAuthError(err)) return;
    showError(`操作失败：${err.message}`);
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

async function handleConfirm(pending, button) {
  button.disabled = true;
  button.textContent = "执行中…";
  try {
    await api(`/api/conversations/${state.conversationId}/actions`, {
      method: "POST",
      body: { action_id: pending.action_id },
    });
    await syncAfter();
    button.textContent = "已提交";
  } catch (err) {
    if (handleAuthError(err)) return;
    button.disabled = false;
    button.textContent = pending.type === "approval"
      ? "我已提交审批，继续" : "确认执行";
    showError(`确认失败：${err.message}`);
  }
}

/* ---------------- 附件 ---------------- */

function renderPreviews() {
  clear(previewsEl);
  if (!state.pendingFiles.length) {
    previewsEl.classList.add("hidden");
    updateSendState();
    return;
  }
  previewsEl.classList.remove("hidden");
  for (const item of state.pendingFiles) {
    const box = el("div", { className: "attach-preview" });
    if ((item.file.type || "").startsWith("image/") && item.previewUrl) {
      box.appendChild(el("img", {
        attrs: { src: item.previewUrl, alt: item.file.name },
      }));
    } else {
      box.appendChild(el("div", { className: "attach-name", text: "PDF" }));
    }
    box.appendChild(el("div", { className: "name", text: item.file.name }));
    if (item.uploading) {
      box.appendChild(el("div", { className: "name", text: "上传中…" }));
    } else if (item.error) {
      box.appendChild(el("div", { className: "name danger", text: "上传失败" }));
      const retry = el("button", {
        className: "rm", text: "↻", attrs: { type: "button", title: "重试" },
      });
      retry.addEventListener("click", () => uploadOne(item));
      box.appendChild(retry);
    } else {
      const remove = el("button", {
        className: "rm", text: "×", attrs: { type: "button", title: "移除" },
      });
      remove.addEventListener("click", () => {
        if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
        state.pendingFiles = state.pendingFiles.filter((entry) => entry !== item);
        renderPreviews();
      });
      box.appendChild(remove);
    }
    previewsEl.appendChild(box);
  }
  updateSendState();
}

function clearPreviews() {
  for (const item of state.pendingFiles) {
    if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
  }
  state.pendingFiles = [];
  renderPreviews();
}

async function uploadOne(item) {
  item.uploading = true;
  item.error = false;
  renderPreviews();
  try {
    const data = await uploadAttachment(item.file, state.conversationId);
    item.attachmentId = data.attachment_id;
  } catch (err) {
    if (handleAuthError(err)) return;
    item.error = true;
    showError(`附件上传失败：${err.message}`, () => uploadOne(item));
  } finally {
    item.uploading = false;
    renderPreviews();
  }
}

function onFilePicked(event) {
  const files = Array.from(event.target.files || []);
  fileInput.value = "";
  for (const file of files) {
    if (file.size > 10 * 1024 * 1024) {
      showError(`${file.name} 超过 10MB 上限`);
      continue;
    }
    const item = {
      file,
      previewUrl: (file.type || "").startsWith("image/")
        ? URL.createObjectURL(file) : "",
      attachmentId: "",
      uploading: false,
      error: false,
    };
    state.pendingFiles.push(item);
    uploadOne(item);
  }
  renderPreviews();
}

/* ---------------- 转人工 / 历史 ---------------- */

async function requestHandoff() {
  if (state.closed) return;
  const btn = $("handoff-btn");
  const original = btn.textContent;
  btn.textContent = "转接中…";
  try {
    const data = await api(
      `/api/customer/conversations/${state.conversationId}/handoff`,
      { method: "POST" },
    );
    await syncAfter();
    if (data.state) {
      applyConversation({ ...(state.conversation || {}), state: data.state,
        status: data.status });
    }
  } catch (err) {
    if (handleAuthError(err)) return;
    showError(`转人工失败：${err.message}`);
  } finally {
    btn.textContent = original;
  }
}

async function loadHistory(reset) {
  if (reset) {
    state.historyBefore = "";
    clear(historyList);
    historyList.appendChild(loadingNode());
  }
  const query = state.historyBefore
    ? `?limit=10&before=${encodeURIComponent(state.historyBefore)}`
    : "?limit=10";
  try {
    const data = await api(`/api/customer/history${query}`);
    if (reset) clear(historyList);
    const items = data.conversations || [];
    if (!items.length && reset) {
      historyList.appendChild(emptyNode("暂无历史会话"));
    }
    for (const conv of items) {
      historyList.appendChild(historyItem(conv));
    }
    state.historyBefore = data.next_before || "";
    historyMore.classList.toggle("hidden", !state.historyBefore);
  } catch (err) {
    if (handleAuthError(err)) return;
    if (reset) {
      clear(historyList);
      historyList.appendChild(emptyNode(`历史加载失败：${err.message}`));
    }
  }
}

function historyItem(conv) {
  const item = el("div", {
    className: "side-item" + (conv.id === state.conversationId ? " active" : ""),
    attrs: { "data-conv-id": conv.id, role: "button", tabindex: "0" },
  });
  const head = el("div");
  head.appendChild(el("b", { text: `会话 ${String(conv.id).slice(0, 8)}` }));
  head.appendChild(document.createTextNode(" "));
  head.appendChild(stateBadge(conv.state, conv.status));
  item.appendChild(head);
  item.appendChild(el("div", {
    className: "meta",
    text: `${conv.message_count} 条 · ${String(conv.updated_at || "").slice(0, 16).replace("T", " ")}`,
  }));
  item.appendChild(el("div", { className: "meta", text: conv.last_message || "" }));
  item.addEventListener("click", () => openConversation(conv.id));
  item.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") openConversation(conv.id);
  });
  return item;
}

async function loadSideList() {
  clear(sideList);
  const conv = state.conversation;
  if (!conv) {
    sideList.appendChild(emptyNode("暂无会话"));
    return;
  }
  const item = el("div", { className: "side-item active" });
  const head = el("div");
  head.appendChild(el("b", { text: "当前会话" }));
  head.appendChild(document.createTextNode(" "));
  head.appendChild(stateBadge(conv.state, conv.status));
  item.appendChild(head);
  item.appendChild(el("div", {
    className: "meta", text: String(conv.created_at || "").slice(0, 16).replace("T", " "),
  }));
  item.addEventListener("click", () => historyDrawer.classList.add("hidden"));
  sideList.appendChild(item);
  const more = el("button", { className: "btn-ghost", text: "查看历史会话", attrs: { type: "button" } });
  more.addEventListener("click", () => {
    historyDrawer.classList.remove("hidden");
    loadHistory(true);
  });
  sideList.appendChild(more);
}

/* ---------------- 登录 ---------------- */

function showLogin() {
  if (state.socket) {
    state.socket.close();
    state.socket = null;
  }
  state.conversation = null;
  state.conversationId = "";
  loginBox.classList.remove("hidden");
  setStatus("请先登录");
  $("who").textContent = "未登录";
  clear(chatEl);
  chatEl.appendChild(emptyNode("登录后开始咨询"));
  closedNotice.classList.add("hidden");
}

function hideLogin() {
  loginBox.classList.add("hidden");
  loginError.classList.add("hidden");
  loginError.textContent = "";
}

async function login() {
  const btn = $("login-btn");
  btn.disabled = true;
  btn.textContent = "登录中…";
  loginError.classList.add("hidden");
  try {
    const resp = await fetch("/api/auth/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tenant_id: $("tenant").value,
        username: $("username").value.trim(),
        password: $("password").value,
        surface: "customer",
      }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      loginError.textContent = data.error === "invalid_credentials"
        ? "账号或密码错误" : `登录失败（${data.error || resp.status}）`;
      loginError.classList.remove("hidden");
      return;
    }
    setCsrf(data.csrf_token || "");
    hideLogin();
    await loadCurrentConversation();
  } catch (err) {
    loginError.textContent = `登录失败：${err.message}`;
    loginError.classList.remove("hidden");
  } finally {
    btn.disabled = false;
    btn.textContent = "登录";
  }
}

async function logout() {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch (_) {
    /* 退出失败也回到登录页，Cookie 过期即可 */
  }
  setCsrf("");
  showLogin();
}

async function init() {
  try {
    const me = await api("/api/auth/me");
    if (me.role !== "customer") {
      loginError.textContent = "当前账号不是客户角色，请用客户账号登录";
      loginError.classList.remove("hidden");
      showLogin();
      return;
    }
    setCsrf(me.csrf_token || "");
    hideLogin();
    await loadCurrentConversation();
  } catch (err) {
    showLogin();
  }
}

/* ---------------- 事件 ---------------- */

$("login-btn").addEventListener("click", login);
$("password").addEventListener("keydown", (event) => {
  if (event.key === "Enter") login();
});
$("logout-link").addEventListener("click", logout);
$("handoff-btn").addEventListener("click", requestHandoff);
$("history-btn").addEventListener("click", () => {
  historyDrawer.classList.remove("hidden");
  loadHistory(true);
});
$("history-close").addEventListener("click", () => historyDrawer.classList.add("hidden"));
historyMore.addEventListener("click", () => loadHistory(false));
$("side-toggle").addEventListener("click", () => $("side").classList.toggle("open"));

sendBtn.addEventListener("click", () => sendMessage(inputEl.value));
inputEl.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendMessage(inputEl.value);
  }
});
attachBtn.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", onFilePicked);
retryBtn.addEventListener("click", () => hideError());

for (const chip of document.querySelectorAll("[data-quick]")) {
  chip.addEventListener("click", () => sendMessage(QUICK_TEXTS[chip.dataset.quick] || ""));
}

init();
