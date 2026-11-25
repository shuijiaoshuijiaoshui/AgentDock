/* WebSocket 封装：同源 Cookie 认证、指数退避重连、消息去重交给上层。 */

const BASE = (() => {
  const path = new URL(import.meta.url).pathname;
  const index = path.indexOf("/static/");
  return index > 0 ? path.slice(0, index) : "";
})();

export class ChatSocket {
  constructor({ conversationId, surface, onSnapshot, onMessage, onState, onStatus }) {
    this.conversationId = conversationId;
    this.surface = surface;
    this.onSnapshot = onSnapshot;
    this.onMessage = onMessage;
    this.onState = onState;
    this.onStatus = onStatus;
    this.ws = null;
    this.retry = 0;
    this.closedByUs = false;
  }

  connect() {
    if (!this.conversationId) return;
    this.closedByUs = false;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}${BASE}/ws/${this.conversationId}`
      + `?surface=${encodeURIComponent(this.surface)}`;
    this.ws = new WebSocket(url);
    this.ws.addEventListener("open", () => {
      this.retry = 0;
      if (this.onStatus) this.onStatus("connected");
    });
    this.ws.addEventListener("message", (event) => {
      let evt;
      try {
        evt = JSON.parse(event.data);
      } catch (_) {
        return;
      }
      if (evt.type === "snapshot" && this.onSnapshot) this.onSnapshot(evt);
      else if (evt.type === "message" && this.onMessage) this.onMessage(evt.message, evt.conversation);
      else if (evt.type === "state" && this.onState) this.onState(evt.conversation);
    });
    this.ws.addEventListener("close", () => {
      if (this.onStatus) this.onStatus("disconnected");
      if (this.closedByUs) return;
      if (this.retry < 6) {
        this.retry += 1;
        const delay = Math.min(8000, 500 * 2 ** this.retry);
        setTimeout(() => this.connect(), delay);
      }
    });
  }

  send(text) {
    if (this.ws && this.ws.readyState === 1) {
      this.ws.send(JSON.stringify({ text }));
      return true;
    }
    return false;
  }

  close() {
    this.closedByUs = true;
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }
}
