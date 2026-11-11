# 看板（space）动作规格

看板是一个 Muse Space（全栈 Web 应用，六页签：余额 / 持仓 / 卖出单 / 记录 / 项目 / 待办）。Space 的源码由 Muse 按规格生成、没有可导出的源码包，这里给出完整的动作规格与行为约定——照着这份规格在 Muse 里描述，就能重建一个等价的看板。服务端常量（只放服务端，绝不下发前端）：

```
RELAY_URL    = https://baw-relay.<你的子域名>.workers.dev
RELAY_KEY    = <中继钥匙，64 位十六进制>
BRIDGE_SECRET= <桥签名密钥，64 位十六进制>
```

服务端调桥统一走 [`../docs/protocol.md`](../docs/protocol.md) 的 `/call`：外层头 `X-Relay-Key`，内层 HMAC 签名（密钥用**文本字节**），请求带正常浏览器 UA；网络错误或 504 重试 1 次。先以 20 秒短超时探活 `GET /health`，探活要能区分三种结果：通、没响应、签名没通过（401 不要误报成没响应）。

## 数据模型

- `wallets`：label（号名）、address、bnbBalance、bnbUsd、sessionStatus（CONNECTED / UNCONNECTED / UNKNOWN）、holdings[]（symbol、address、balance、price、value）
- `historyByProfile`：每号最近一次拉取的链上记录缓存 {rows, fetchedAt}（行结构见协议文档 `/history`）
- `sellOrders`：walletLabel、tokenSymbol、tokenAddress、percent、sellAmount、status（pending / executed / failed / cancelled）、txHash、failReason、备注
- `projects`、`todos`：项目与待办（名字、状态、备注、到期时间等），用户手工行受同步保护

## 动作

| 动作 | 行为 |
| --- | --- |
| `getstate` | 返回全部状态（含 lastSyncedAt） |
| `createsellorder` | 登记卖出单（pending）后**当场执行**：从该号持仓按代币解析合约地址，`POST /sell {id, profile, token, amount, to:"BNB"}`（100% 传 `"all"`，timeoutMs 120000，可重试 1 次）；按协议的结果映射立刻回写状态：sold→executed+txHash、failed→failed+原文原因、unknown→保持 pending 并备注「结果不明，正在查链上，不要重发」、桥联系不上→保持 pending 并备注「桥未响应」 |
| `updatesellorderstatus` | 外部回写卖出结果（兜底，只许 executed / failed） |
| `cancelsellorder` | 取消 pending 的单 |
| `requestsync` | 直接触发一次 `bridgeSync` 并把结果文案回给界面 |
| `quotesell` | 只报价不下单：`POST /quote {profile, token, amount}`（超时 240 秒），原样返回预览结果供卖出弹窗的报价卡展示；预览未通过时界面禁用确认按钮 |
| `gethistory` | `GET /history?profile=` 拉该号链上记录并缓存到 `historyByProfile`，返回 rows |
| `bridgesync`（定时） | 每 10 分钟纯脚本运行（Space 的定时动作，无 AI）：经桥并行拉 `/balances`、`/holdings`、`/sessions` 更新钱包、持仓与登录态、刷新 lastSyncedAt；bnbUsd 按 Binance 公开行情 BNBUSDT 折算，行情失败保留旧美元值；**任一调用失败不改任何数据，只记录失败** |
| `applysync` | 整表替换式同步（兜底，手工行受保护；字段必须给全，缺字段等于删数据） |
| 项目 / 待办 CRUD | add/update/delete/toggle，常规 |

## 界面约定

- 首页（余额页）按 lastSyncedAt 推导连接状态：20 分钟内显示「已接通」，超时显示未接通——不要写死状态。总览分三段：总资产 / 可见资产 / 隐藏·风险，并汇总登录态（X/N 在线、Y 已退出）；每个号带在线/已退出/未知徽标。
- 持仓页按合约聚合跨号持仓：搜索、隐藏小额开关（总价值 < $1）、全部/正常/风险分段、展开看各号分布；每行带「币安K线 / OKX K线」外链（按合约地址拼 web3 代币页，新标签打开）。
- 卖出弹窗带报价卡：先 `quotesell` 展示预计到账与预览输出，预览未通过时确认按钮禁用；没取报价也可照旧直卖。
- 所有失败文案用大白话展示桥回的原文原因，不吞不改。
