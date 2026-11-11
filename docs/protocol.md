# 中继协议与签名规范

看板服务端、Worker 中继、本地桥三方之间的协议。语言无关：任何实现了这份规范的看板端都能对接本仓库的桥和中继。

## 通道

中继是一个 Cloudflare Worker + 一个 Durable Object（单例，`idFromName("main")`），内存里维护三样东西：待取请求队列、挂起的 `/poll`、等结果的 `/call`。根路径 `GET /` 返回纯文本 `relay ok`（健康探活，不验钥匙）。

所有业务请求必须带 HTTP 头 `X-Relay-Key: <中继钥匙>`，中继只验这一层；验不过回 401。

### POST /call（看板端 → 中继）

```json
{
  "method": "POST",
  "path": "/sell",
  "body": "{\"id\":\"12\",\"profile\":\"wallet1\",\"token\":\"0x...\",\"amount\":\"all\",\"to\":\"BNB\"}",
  "headers": { "X-Bridge-Timestamp": "1794389051", "X-Bridge-Signature": "<hex>" },
  "timeoutMs": 120000
}
```

中继把 `{reqId, method, path, body, headers}` 排队，等桥取走并回结果后返回：

```json
{ "ok": true, "status": 200, "body": "<桥返回的 JSON 字符串>" }
```

超时（`timeoutMs`，上限 280000）回 `504 {"ok": false, "error": "agent_timeout"}`，同时把排队中的请求摘掉。

### GET /poll（桥 → 中继）

长轮询：有活立刻返回一条 `{reqId, method, path, body, headers}`；25 秒无活回 `204` 空响应，桥随即重连。短挂起是为了让中间层静默掐断的死连接尽快回收——活发到死连接上就丢了。

### POST /result（桥 → 中继）

```json
{ "reqId": "<uuid>", "status": 200, "body": "<处理结果的 JSON 字符串>" }
```

## 内层签名（端到端，桥验）

中继不感知签名。看板端对每个业务请求签：

```
ts  = 当前 Unix 秒（字符串）
msg = ts + "." + method + "." + path + "." + sha256hex(body)
sig = HMAC_SHA256(key = 桥签名密钥的文本字节, msg) 的 hex
```

放进 `headers` 的 `X-Bridge-Timestamp`、`X-Bridge-Signature` 随 `/call` 透传给桥。桥验签时要求：签名相等且 `|now - ts| <= 120` 秒（防重放）。注意密钥是**文本字节**，不要把十六进制字符串再解码成二进制——这是实际踩过的坑（解码派生会恒定 401）。

另注意：Cloudflare 会拦默认的程序化 User-Agent（如 `Python-urllib`）返回 403/1010，所有请求请带一个正常浏览器 UA。

## 桥的接口

| 接口 | 说明 |
| --- | --- |
| `GET /health` | `{ok, version, profiles, stopped}` |
| `GET /balances` | 各号 BNB 余额（带缓存，桥每 10 分钟刷新快照到 `outbox/balances.json`） |
| `GET /holdings` | 各号代币持仓（合约地址、数量、报价、美元值） |
| `GET /sessions` | 各号会话登录态 `{wallets: {profile: {status: CONNECTED / UNCONNECTED / UNKNOWN}}}`（带缓存，随快照刷新） |
| `GET /history?profile=<号名>` | 该号最近 50 条链上记录（时间 ISO 字符串、类型、资产与数量、状态、txHash；桥做轻归一化） |
| `POST /quote` | `{profile, token, amount}`：只报价不执行——走与真卖相同的预览路径，回 `{previewOk, estBnb, sts, lines}`，供看板在确认前展示「预计到账 / 卖不卖得动」 |
| `POST /sell` | `{id, profile, token, amount, to}`；`amount` 可为 `"all"`、百分比或数量 |

卖出结果的 `status` 语义（看板端按此回写卖出单）：

| status | 含义 | 看板动作 |
| --- | --- | --- |
| `sold` | 已成交，带 `exec[].tx` | 置「已卖出」+ 交易哈希 |
| `failed` | 明确失败（试算没过、被风控拦、无余额、流动性不足等），带原文原因 | 置「失败」+ 原因大白话展示 |
| `unknown_check_onchain` | 已发出但结果不明 | 保持「待执行」，备注勿重发，先查链上 |

## 重试规则

- 读接口（health / balances / holdings / sessions / history）和只报价的 `/quote`：网络错误或 504 可重试 1 次。
- 卖出：同一个 `id` 的 `/call` 可以重试——桥按 id 幂等，同 id 只执行一次，重复请求直接回第一次的结果落盘件。但「结果不明」（unknown）**绝不**换新 id 重发。
- 桥目录有 `STOP` 文件时 `/sell` 回 423，读接口不受影响。
