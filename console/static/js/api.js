/* 共享 API 客户端：同源 Cookie + CSRF 双提交；统一错误对象。 */

export class ApiError extends Error {
  constructor(status, code, detail) {
    super(detail || code || `HTTP ${status}`);
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

let csrfToken = "";
let surface = "customer";

/** 部署前缀：直接挂在 / 时为空；反代到 /support/ 时为 /support。
 *  由模块自身 URL 推断（.../static/js/api.js），前端无需构建期配置。 */
export const BASE = (() => {
  const marker = "/static/";
  const index = new URL(import.meta.url).pathname.indexOf(marker);
  return index > 0 ? new URL(import.meta.url).pathname.slice(0, index) : "";
})();

export function withBase(path) {
  if (!path) return path;
  return path.startsWith("/") ? `${BASE}${path}` : path;
}

export function setSurface(name) {
  surface = name;
}

export function setCsrf(token) {
  csrfToken = token || "";
}

export function getCsrf() {
  return csrfToken;
}

export async function api(path, { method = "GET", body, headers = {}, raw = false } = {}) {
  const init = {
    method,
    credentials: "same-origin",
    headers: { "X-Surface": surface, ...headers },
  };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  if (method !== "GET" && method !== "HEAD" && csrfToken) {
    init.headers["X-CSRF-Token"] = csrfToken;
  }
  let resp;
  try {
    resp = await fetch(withBase(path), init);
  } catch (err) {
    throw new ApiError(0, "network_error", String(err));
  }
  if (resp.status === 401) {
    throw new ApiError(401, "unauthorized", "登录已失效");
  }
  if (!resp.ok) {
    let payload = {};
    try {
      payload = await resp.json();
    } catch (_) {
      payload = {};
    }
    throw new ApiError(
      resp.status,
      payload.error || "request_failed",
      payload.detail || payload.hint || "",
    );
  }
  if (raw) return resp;
  const text = await resp.text();
  return text ? JSON.parse(text) : {};
}

export async function uploadAttachment(file, conversationId) {
  const form = new FormData();
  form.append("file", file);
  if (conversationId) form.append("conversation_id", conversationId);
  const resp = await fetch(withBase("/api/attachments"), {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-Surface": surface, "X-CSRF-Token": csrfToken },
    body: form,
  });
  if (!resp.ok) {
    let payload = {};
    try {
      payload = await resp.json();
    } catch (_) {
      payload = {};
    }
    throw new ApiError(resp.status, payload.error || "upload_failed", payload.detail || "");
  }
  return resp.json();
}
