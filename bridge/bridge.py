#!/usr/bin/env python3
"""币安钱包桥 v4：本机服务 + Workers 中继客户端。

两条入口，同一套处理（process_request，含 HMAC 验签）：
- 本机 HTTP：127.0.0.1:18921（仅本机，调试用）
- 中继长轮询：桥主动连 Cloudflare Worker 中继取活（只出不进），
  配置在同目录 bridge-relay.json：{"relay_url", "relay_key"}

接口：GET /health、GET /balances、GET /holdings、GET /sessions、
GET /history?profile=X、POST /sell、POST /quote（只报价不执行）
/sell 幂等：同一 id 只执行一次，结果落盘 results/，重复请求直接回旧结果。

安全规矩（沿用 v1–v3）：
- bridge/STOP 存在时 /sell 一律拒绝（急停），读接口照常。
- st=UNKNOWN 绝不重发，只如实回 unknown_check_onchain。
- 卖出串行执行（全局锁）；kit.py 内部安全拦截照旧生效。
- 每 10 分钟写 outbox/balances.json + holdings.json 快照并刷新缓存。
- 桥文件被更新时自动重跑新版（os.execv）。
密钥：同目录 bridge-secret.txt（不进日志、不回传）。
"""
import concurrent.futures as cf
import hashlib
import re
import hmac
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BR)
OUTBOX = os.path.join(BR, "outbox")
RESULTS = os.path.join(BR, "results")
STOP = os.path.join(BR, "STOP")
LOG = os.path.join(BR, "bridge.log")
SECRET_FILE = os.path.join(BR, "bridge-secret.txt")
RELAY_FILE = os.path.join(BR, "bridge-relay.json")
PY = os.path.join(ROOT, ".venv", "bin", "python")
KIT = os.path.join(ROOT, "chain", "kit.py")
BAWP = os.path.join(ROOT, "bin", "bawp")
PORT = 18921
VERSION = "4.2"
BAL_EVERY = 600
CACHE_TTL = 600
MAX_SKEW = 120  # 签名时间戳容差（秒），防重放

try:
    PROFILES = list(json.load(open(os.path.join(ROOT, "chain", "accounts.json"))).keys())
except Exception:
    PROFILES = []
try:
    SECRET = open(SECRET_FILE).read().strip()
except Exception:
    SECRET = ""
try:
    RELAY = json.load(open(RELAY_FILE))
except Exception:
    RELAY = {}

_sell_lock = threading.Lock()
_cache = {"balances": (0, None), "holdings": (0, None), "sessions": (0, None)}
_hist_cache = {}  # profile -> (ts, rows)
HIST_TTL = 300
_cache_lock = threading.Lock()


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def run_kit(args, exec_=False, timeout=600):
    env = dict(os.environ)
    env["PROJ"] = "bridge"
    cmd = [PY, KIT] + args + (["--exec"] if exec_ else [])
    r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
    rows = []
    for ln in r.stdout.splitlines():
        ln = ln.strip()
        if ln.startswith("{"):
            try:
                rows.append(json.loads(ln))
            except Exception:
                pass
    return rows, (r.stdout + r.stderr)[-2000:]


def fetch_balances():
    rows, _ = run_kit(["bal"], timeout=300)
    return {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "rows": rows}


def wallet_holdings(profile):
    try:
        r = subprocess.run(
            [BAWP, profile, "wallet", "balance", "--binanceChainId", "56",
             "--hideLowValue", "false", "--json"],
            capture_output=True, text=True, timeout=90)
        d = json.loads(r.stdout)
        if not d.get("success"):
            return []
        out = []
        for t in d.get("data") or []:
            out.append({
                "symbol": t.get("symbol"),
                "address": t.get("address"),
                "balance": t.get("balance"),
                "price": t.get("price"),
                "value": t.get("value"),
            })
        return out
    except Exception:
        return []


def fetch_holdings():
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        got = dict(zip(PROFILES, ex.map(wallet_holdings, PROFILES)))
    return {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "wallets": got}


def wallet_status(profile):
    try:
        r = subprocess.run([BAWP, profile, "wallet", "status", "--json"],
                           capture_output=True, text=True, timeout=30)
        d = json.loads(r.stdout)
        raw = str(((d.get("data") or {}).get("status")
                   or ("CONNECTED" if d.get("success") else "UNKNOWN"))).upper()
        return {"status": raw if raw in ("CONNECTED", "UNCONNECTED") else "UNKNOWN"}
    except Exception as e:
        return {"status": "UNKNOWN", "error": str(e)[:120]}


def fetch_sessions():
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        got = dict(zip(PROFILES, ex.map(wallet_status, PROFILES)))
    return {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "wallets": got}


def fetch_history(profile):
    now = time.time()
    with _cache_lock:
        ts, rows = _hist_cache.get(profile, (0, None))
        if rows is not None and now - ts < HIST_TTL:
            return {"profile": profile, "rows": rows, "cached": True}
    rows = []
    try:
        r = subprocess.run(
            [BAWP, profile, "wallet", "tx-history", "--binanceChainId", "56",
             "--size", "50", "--json"],
            capture_output=True, text=True, timeout=90)
        d = json.loads(r.stdout)
        txs = ((d.get("data") or {}).get("transactions") or []) if d.get("success") else []
        for tx in txs:
            hashes = tx.get("txHashList") or []
            h = hashes[0] if hashes else {}
            ins = h.get("instructions") or {}
            sends = ins.get("send") or []
            receives = ins.get("receive") or []
            send = sends[0] if sends else None
            receive = receives[0] if receives else None
            ttype = tx.get("txType") or "contract"
            if ttype == "transfer":
                kind = "发送" if send and not receive else "接收" if receive and not send else "交易"
            elif ttype == "contract_call":
                kind = "交易" if send and receive else "合约"
            else:
                kind = "合约"
            src = send or receive
            amount = ""
            symbol = ""
            if src:
                symbol = str((src.get("tokenInfo") or {}).get("symbol") or "")
                amount = f"{src.get('amount')} {symbol}".strip()
            rows.append({
                "time": tx.get("txTime"), "kind": kind, "txType": ttype,
                "symbol": symbol, "amount": amount,
                "status": tx.get("status") or h.get("status") or "unknown",
                "txHash": tx.get("txHash") or h.get("txHash") or "",
            })
    except Exception as e:
        log(f"history {profile} 失败：{e}")
    with _cache_lock:
        _hist_cache[profile] = (time.time(), rows)
    return {"profile": profile, "rows": rows, "cached": False}


def do_quote(order):
    """只报价不执行：走 kit.py sell 预览（不带 --exec），与真卖同一条路。"""
    prof = str(order.get("profile") or "")
    token = str(order.get("token") or "")
    amount = str(order.get("amount") or "all")
    if not prof or not token:
        return {"ok": False, "error": "bad_quote"}
    args = ["sell", prof, token, amount, "--to", "BNB", "--slip", "20"]
    rows, raw = run_kit(args, exec_=False, timeout=240)
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    est = None
    for m in re.finditer(r"([0-9]+\.[0-9]+|[0-9]+)\s*BNB", raw):
        est = m.group(1)
    return {"ok": True, "profile": prof, "token": token, "amount": amount,
            "previewOk": ("PREVIEW_OK" in raw),
            "estBnb": est, "sts": [r.get("st") for r in rows],
            "lines": lines[-12:]}


def cached(kind, fetcher):
    now = time.time()
    with _cache_lock:
        ts, val = _cache[kind]
        if val is not None and now - ts < CACHE_TTL:
            return val
    val = fetcher()
    with _cache_lock:
        _cache[kind] = (time.time(), val)
    return val


def write_snapshot(name, data):
    tmp = os.path.join(OUTBOX, name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(OUTBOX, name))


def snapshot_loop():
    while True:
        try:
            b = fetch_balances()
            h = fetch_holdings()
            ss = fetch_sessions()
            write_snapshot("balances.json", b)
            write_snapshot("holdings.json", h)
            write_snapshot("sessions.json", ss)
            with _cache_lock:
                _cache["balances"] = (time.time(), b)
                _cache["holdings"] = (time.time(), h)
                _cache["sessions"] = (time.time(), ss)
            log("快照已写（balances + holdings + sessions）")
        except Exception as e:
            log(f"快照失败：{e}")
        time.sleep(BAL_EVERY)


def result_path(oid):
    safe = "".join(c for c in str(oid) if c.isalnum() or c in "-_")[:64]
    return os.path.join(RESULTS, safe + ".json"), safe


def do_sell(order):
    oid = str(order["id"])
    path, safe = result_path(oid)
    if os.path.exists(path):
        with open(path) as f:
            old = json.load(f)
        log(f"卖出单 {safe} 已有结果，拒绝重复执行，直接回旧结果")
        return old
    prof = str(order.get("profile") or "all")
    token = str(order["token"])
    amount = str(order.get("amount") or "all")
    to = str(order.get("to") or "BNB")
    args = ["sell", prof, token, amount, "--to", to, "--slip", "20"]
    log(f"收到卖出单 {safe}: {prof} 卖 {token} x {amount} -> {to}（直发，滑点 20%）")
    result = {"id": oid, "order": order, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    rows, raw = run_kit(args, exec_=True)
    result["exec"] = rows
    sts = [r.get("st") for r in rows]
    if rows and all(s == "OK" for s in sts):
        result["status"] = "sold"
    elif "UNKNOWN" in sts:
        result["status"] = "unknown_check_onchain"
    else:
        result["status"] = "failed"
        result["raw_tail"] = raw
    os.makedirs(RESULTS, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(result, f, ensure_ascii=False)
    os.replace(tmp, path)
    log(f"{safe} 执行完：{result['status']} {sts}")
    return result


def verify_request(method, path, body, ts, sig):
    """HMAC-SHA256：sig = HMAC(secret, ts + '.' + method + '.' + path + '.' + sha256(body))"""
    if not SECRET:
        return False
    try:
        skew = abs(time.time() - int(ts))
    except Exception:
        return False
    if skew > MAX_SKEW:
        return False
    msg = ".".join([str(ts), method, path, hashlib.sha256(body).hexdigest()])
    expect = hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expect, sig or "")


def process_request(method, path, body, headers):
    """统一处理入口。headers 为 dict（大小写不敏感地取签名头）。
    返回 (http_status, payload_obj)。"""
    def hget(name):
        for k, v in (headers or {}).items():
            if k.lower() == name.lower():
                return v
        return ""
    if not verify_request(method, path, body,
                          hget("X-Bridge-Timestamp"), hget("X-Bridge-Signature")):
        return 401, {"ok": False, "error": "unauthorized"}
    try:
        if path == "/health" and method == "GET":
            return 200, {"ok": True, "version": VERSION,
                         "profiles": len(PROFILES),
                         "stopped": os.path.exists(STOP)}
        if path == "/balances" and method == "GET":
            return 200, cached("balances", fetch_balances)
        if path == "/holdings" and method == "GET":
            return 200, cached("holdings", fetch_holdings)
        if path == "/sessions" and method == "GET":
            return 200, cached("sessions", fetch_sessions)
        if path == "/quote" and method == "POST":
            q = json.loads(body or b"{}")
            return 200, do_quote(q)
        if urllib.parse.urlsplit(path).path == "/history" and method == "GET":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            prof = (qs.get("profile") or [""])[0]
            if prof not in PROFILES:
                return 400, {"ok": False, "error": "bad_profile"}
            return 200, fetch_history(prof)
        if path == "/sell" and method == "POST":
            if os.path.exists(STOP):
                return 423, {"ok": False, "error": "bridge_stopped"}
            order = json.loads(body or b"{}")
            if not order.get("id") or not order.get("token"):
                return 400, {"ok": False, "error": "bad_order"}
            with _sell_lock:
                return 200, do_sell(order)
        return 404, {"ok": False, "error": "not_found"}
    except Exception as e:
        log(f"处理出错 {method} {path}：{e}")
        return 500, {"ok": False, "error": str(e)[:300]}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        code, obj = process_request(self.command, self.path, body,
                                    dict(self.headers.items()))
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _handle
    do_POST = _handle


def relay_call(method, path, payload=None, timeout=70):
    url = RELAY["relay_url"].rstrip("/") + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Relay-Key", RELAY["relay_key"])
    # Cloudflare 会拦默认的 Python-urllib UA（403/断连），必须带浏览器 UA
    req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return r.status, (json.loads(raw) if raw else None)


def relay_loop():
    log(f"中继客户端启动：{RELAY.get('relay_url')}")
    backoff = 2
    while True:
        try:
            status, item = relay_call("GET", "/poll", timeout=70)
            backoff = 2
            if status == 204 or not item:
                continue
            code, obj = process_request(
                item["method"], item["path"],
                (item.get("body") or "").encode(),
                item.get("headers") or {})
            relay_call("POST", "/result",
                       {"reqId": item["reqId"], "status": code,
                        "body": json.dumps(obj, ensure_ascii=False)},
                       timeout=30)
        except Exception as e:
            log(f"中继轮询出错（{e}），{backoff}s 后重连")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


def watch_self():
    mtime = os.path.getmtime(__file__)
    while True:
        time.sleep(2)
        try:
            if os.path.getmtime(__file__) != mtime:
                log("桥文件已更新，重跑新版")
                os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])
        except Exception:
            pass


def main():
    os.makedirs(OUTBOX, exist_ok=True)
    os.makedirs(RESULTS, exist_ok=True)
    if not SECRET:
        log("严重：bridge-secret.txt 缺失或为空，接口将全部拒绝（401）")
    threading.Thread(target=snapshot_loop, daemon=True).start()
    threading.Thread(target=watch_self, daemon=True).start()
    if RELAY.get("relay_url") and RELAY.get("relay_key"):
        threading.Thread(target=relay_loop, daemon=True).start()
    else:
        log("未配置中继（bridge-relay.json 缺失），只开本机 HTTP")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    log(f"桥 v4 已启动：127.0.0.1:{PORT}，{len(PROFILES)} 个号")
    srv.serve_forever()


if __name__ == "__main__":
    main()
