/* 共享 DOM 组件：只用 textContent / createElement，禁止 HTML 字符串注入。 */

import { withBase } from "./api.js";

export function el(tag, opts = {}, children = []) {
  const node = document.createElement(tag);
  if (opts.className) node.className = opts.className;
  if (opts.text !== undefined) node.textContent = String(opts.text);
  if (opts.attrs) {
    for (const [key, value] of Object.entries(opts.attrs)) {
      if (value !== null && value !== undefined) node.setAttribute(key, String(value));
    }
  }
  if (opts.on) {
    for (const [event, handler] of Object.entries(opts.on)) {
      node.addEventListener(event, handler);
    }
  }
  for (const child of children) {
    if (child) node.appendChild(child);
  }
  return node;
}

export function clear(node) {
  if (node) node.replaceChildren();
}

export function fmtTime(iso) {
  if (!iso) return "";
  const text = String(iso).replace("T", " ");
  return text.slice(0, 16);
}

const STATE_LABEL = {
  AI_ACTIVE: ["AI", "ai"],
  PENDING_HANDOFF: ["排队中", "queue"],
  HUMAN_ACTIVE: ["人工", "human"],
  PENDING_REVIEW: ["待复核", "review"],
  CLOSED: ["已结束", "closed"],
};

export function stateBadge(state, status) {
  let key = state;
  if (!key) {
    key = status === "with_human" ? "HUMAN_ACTIVE"
      : status === "pending_handoff" ? "PENDING_HANDOFF"
      : status === "closed" ? "CLOSED" : "AI_ACTIVE";
  }
  const [label, cls] = STATE_LABEL[key] || ["AI", "ai"];
  return el("span", { className: `badge ${cls}`, text: label });
}

export function messageNode(message, { onAttachment } = {}) {
  const role = message.role || "ai";
  const wrap = el("div", { className: `msg ${role}` });
  const meta = message.meta || {};
  if (role === "agent") {
    wrap.appendChild(el("b", { text: "【人工坐席】" }));
  }
  wrap.appendChild(document.createTextNode(message.content || ""));
  const attachments = meta.attachments || [];
  if (attachments.length) {
    const box = el("div", { className: "msg-attach" });
    for (const item of attachments) {
      const row = el("div", { className: "attach-row" });
      if ((item.mime || "").startsWith("image/")) {
        const img = el("img", {
          className: "thumb",
          attrs: { alt: item.filename || "附件", src: withBase(`/api/attachments/${item.attachment_id}`) },
        });
        row.appendChild(img);
      }
      const link = el("a", {
        text: item.filename || item.attachment_id,
        attrs: {
          href: withBase(`/api/attachments/${item.attachment_id}`),
          download: item.filename || "",
          target: "_blank",
          rel: "noopener",
        },
      });
      if (onAttachment) {
        link.addEventListener("click", (event) => onAttachment(event, item));
      }
      row.appendChild(link);
      row.appendChild(el("span", { text: ` · ${formatSize(item.size)}` }));
      box.appendChild(row);
    }
    wrap.appendChild(box);
  }
  if (message.created_at) {
    wrap.appendChild(el("span", { className: "msg-time", text: fmtTime(message.created_at) }));
  }
  return wrap;
}

export function formatSize(size) {
  const n = Number(size || 0);
  if (n > 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)}MB`;
  if (n > 1024) return `${Math.round(n / 1024)}KB`;
  return `${n}B`;
}

export function fieldRow(label, value) {
  const row = el("div", { className: "card-field" });
  row.appendChild(el("span", { className: "k", text: `${label}` }));
  row.appendChild(el("span", { text: value === null || value === undefined || value === "" ? "-" : String(value) }));
  return row;
}

const CARD_LABEL = {
  order: "订单",
  inventory: "库存",
  credit: "授信与账期",
  aftersale: "售后资格",
};

export function cardNode(card, { onAction } = {}) {
  const wrap = el("div", { className: "card", attrs: { "data-card-type": card.type || "" } });
  wrap.appendChild(el("div", {
    className: "card-title",
    text: `${CARD_LABEL[card.type] || "卡片"} · ${card.title || ""}`,
  }));
  const grid = el("div", { className: "card-grid" });
  for (const field of card.fields || []) {
    grid.appendChild(fieldRow(field.label, field.value));
  }
  wrap.appendChild(grid);
  if ((card.actions || []).length) {
    const actions = el("div", { className: "card-actions" });
    for (const action of card.actions) {
      const btn = el("button", {
        className: "chip-btn",
        text: action.label || action.action_id,
        attrs: { type: "button", "data-action-id": action.action_id || "" },
      });
      btn.addEventListener("click", () => {
        if (onAction) onAction(action, btn);
      });
      actions.appendChild(btn);
    }
    wrap.appendChild(actions);
  }
  return wrap;
}

export function pendingNode(pending, { onConfirm } = {}) {
  const isApproval = pending.type === "approval";
  const wrap = el("div", { className: "pending", attrs: { "data-action-id": pending.action_id } });
  wrap.appendChild(el("b", { text: isApproval ? "该操作需要内部审批" : "请确认以下操作" }));
  const preview = pending.preview || {};
  for (const [key, value] of Object.entries(preview)) {
    if (value === null || value === undefined || value === "") continue;
    wrap.appendChild(fieldRow(previewLabel(key), value));
  }
  if (pending.expires_at) {
    wrap.appendChild(el("div", {
      className: "attach-row",
      text: `有效期至 ${fmtTime(pending.expires_at)}`,
    }));
  }
  const row = el("div", { className: "row" });
  const btn = el("button", {
    className: "btn-primary",
    text: isApproval ? "我已提交审批，继续" : "确认执行",
    attrs: { type: "button" },
  });
  btn.addEventListener("click", () => {
    if (onConfirm) onConfirm(pending, btn);
  });
  row.appendChild(btn);
  wrap.appendChild(row);
  return wrap;
}

function previewLabel(key) {
  const map = {
    order_no: "订单号", title: "发票抬头", amount: "金额",
    sku: "SKU", fault_type: "故障", new_price: "新价格",
    new_address: "新地址", old_address: "原地址", customer_id: "客户",
  };
  return map[key] || key;
}

export function emptyNode(text) {
  return el("div", { className: "empty", text });
}

export function loadingNode(text = "加载中…") {
  return el("div", { className: "loading", text });
}

export function errorNode(text, onRetry) {
  const box = el("div", { className: "error-box" });
  box.appendChild(el("span", { text }));
  if (onRetry) {
    const btn = el("button", { className: "retry-btn", text: "重试", attrs: { type: "button" } });
    btn.addEventListener("click", onRetry);
    box.appendChild(btn);
  }
  return box;
}
