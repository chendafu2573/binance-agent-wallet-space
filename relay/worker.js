// baw-relay 中继：把看板服务端的签名请求经长轮询转给本地机器上的桥。
// 中继只验外层 RELAY_KEY，不接触桥的 HMAC 密钥（端到端签名在桥上验）。
// 协议：
//   POST /call   {method, path, body, headers, timeoutMs} -> {ok, status, body}
//   GET  /poll   桥长轮询取活，25 秒无活回 204（短挂起勤回收，防长连接被中间层静默掐断后活丢在死连接上）
//   POST /result {reqId, status, body} 桥回结果

export class Relay {
  constructor(state, env) {
    this.env = env;
    this.queue = [];        // 等桥来取的请求
    this.pollWaiters = [];  // 挂起的 poll：{resolve, timer}
    this.callWaiters = new Map(); // reqId -> {resolve}
    this.early = new Map(); // 先于 /call 注册到达的结果（兜底）
  }

  json(obj, status = 200) {
    return new Response(JSON.stringify(obj), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  }

  async fetch(req) {
    if (req.headers.get("X-Relay-Key") !== this.env.RELAY_KEY) {
      return this.json({ ok: false, error: "unauthorized" }, 401);
    }
    const url = new URL(req.url);
    if (url.pathname === "/call" && req.method === "POST") return this.onCall(req);
    if (url.pathname === "/poll" && req.method === "GET") return this.onPoll();
    if (url.pathname === "/result" && req.method === "POST") return this.onResult(req);
    return this.json({ ok: false, error: "not_found" }, 404);
  }

  wakePoll() {
    while (this.pollWaiters.length && this.queue.length) {
      const w = this.pollWaiters.shift();
      clearTimeout(w.timer);
      w.resolve(this.queue.shift());
    }
  }

  async onCall(req) {
    const body = await req.json();
    const reqId = crypto.randomUUID();
    const item = {
      reqId,
      method: body.method,
      path: body.path,
      body: body.body || "",
      headers: body.headers || {},
    };
    const timeoutMs = Math.min(body.timeoutMs || 120000, 280000);
    const resultPromise = new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.callWaiters.delete(reqId);
        const i = this.queue.indexOf(item);
        if (i >= 0) this.queue.splice(i, 1);
        resolve(null);
      }, timeoutMs);
      this.callWaiters.set(reqId, {
        resolve: (r) => { clearTimeout(timer); resolve(r); },
      });
    });
    this.queue.push(item);
    this.wakePoll();
    if (this.early.has(reqId)) {
      const r = this.early.get(reqId);
      this.early.delete(reqId);
      this.callWaiters.get(reqId).resolve(r);
    }
    const result = await resultPromise;
    if (result === null) {
      return this.json({ ok: false, error: "agent_timeout" }, 504);
    }
    return this.json({ ok: true, status: result.status, body: result.body });
  }

  async onPoll() {
    if (this.queue.length) return this.json(this.queue.shift());
    const item = await new Promise((resolve) => {
      const w = { resolve, timer: null };
      w.timer = setTimeout(() => {
        const i = this.pollWaiters.indexOf(w);
        if (i >= 0) this.pollWaiters.splice(i, 1);
        resolve(null);
      }, 25000);
      this.pollWaiters.push(w);
    });
    if (item === null) return new Response(null, { status: 204 });
    return this.json(item);
  }

  async onResult(req) {
    const r = await req.json();
    const w = this.callWaiters.get(r.reqId);
    if (w) {
      this.callWaiters.delete(r.reqId);
      w.resolve({ status: r.status, body: r.body });
    } else {
      this.early.set(r.reqId, { status: r.status, body: r.body });
    }
    return this.json({ ok: true });
  }
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (url.pathname === "/") return new Response("relay ok");
    const stub = env.RELAY.get(env.RELAY.idFromName("main"));
    return stub.fetch(req);
  },
};
