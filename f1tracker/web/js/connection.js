// WebSocket connection to the tracker server, with automatic reconnects.

export class Connection {
  constructor(url) {
    this.url = url;
    this.handlers = new Map();
    this.ws = null;
    this.retryDelay = 500;
  }

  on(type, handler) {
    if (!this.handlers.has(type)) this.handlers.set(type, []);
    this.handlers.get(type).push(handler);
    return this;
  }

  emit(type, payload) {
    for (const handler of this.handlers.get(type) ?? []) handler(payload);
  }

  connect() {
    const ws = (this.ws = new WebSocket(this.url));
    ws.onopen = () => {
      this.retryDelay = 500;
      this.emit("open");
    };
    ws.onmessage = (event) => {
      const message = JSON.parse(event.data);
      this.emit(message.type, message);
    };
    ws.onclose = () => {
      this.emit("close");
      setTimeout(() => this.connect(), this.retryDelay);
      this.retryDelay = Math.min(this.retryDelay * 2, 8000);
    };
  }

  send(cmd, payload = {}) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ cmd, ...payload }));
    }
  }
}
