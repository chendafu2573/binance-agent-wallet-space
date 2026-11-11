# binance-agent-wallet-space

多号币安 Agentic Wallet 的看板 + 卖出系统：一块云端看板（余额、聚合持仓、卖出单、链上记录、项目、待办）、一个跑在本地 Mac 上的卖出桥、一个免费的 Cloudflare Worker 中继把两边连起来。看板上点卖出，桥在本地经 `kit.py` 真卖，结果当场回写；卖出前可以先让桥报价（卖不卖得动、预计到账多少，点之前就知道）；余额、持仓和每个号的登录态每 10 分钟自动经桥同步回看板，全程纯脚本、无 AI 跑腿。

Multi-profile Binance Agentic Wallet dashboard + local sell bridge + free Cloudflare Worker relay. Click "sell" in the dashboard, the bridge on your own Mac executes via `kit.py` and writes the result back immediately.

## 架构

```
┌─────────────┐   POST /call（签名）   ┌──────────────┐   长轮询 /poll   ┌─────────────┐
│  看板（Space）│ ───────────────────▶ │ Worker 中继    │ ◀─────────────── │  桥（本地 Mac）│
│  服务端动作   │ ◀─────────────────── │ Durable Object │ ───────────────▶ │  kit.py sell │
└─────────────┘        返回结果         └──────────────┘    POST /result    └─────────────┘
```

- **桥只出不进**：桥主动连中继取活，家里不开任何端口、不开 SSH、不需要公网 IP，也不需要自有域名（用免费的 `*.workers.dev`）。
- **两道锁**：中继只验外层钥匙（`X-Relay-Key`）；真正的请求签名是端到端的 HMAC-SHA256（带时间戳防重放），在桥上验，中继看不到签名密钥。
- **卖出幂等**：每张卖出单按 id 只执行一次，结果落盘；网络超时重试不会重复卖。`st=UNKNOWN`（结果不明）绝不重发，先查链上。
- **急停**：桥目录放一个 `STOP` 文件，卖出一律拒绝（读接口照常）。
- **私钥不出本地**：钱包登录态只在本地机器的钥匙串里；看板和中继都接触不到。桥只卖不转、不提、不买；最终下单仍过 `kit.py` 自身的安全拦截（无限授权、改账户权限、超额花费一律拦下）。

## 组件

| 目录 | 是什么 |
| --- | --- |
| `bridge/` | 卖出桥（Python 标准库，零依赖）：本地 HTTP 服务 + 中继长轮询客户端，launchd 常驻 |
| `relay/` | Cloudflare Worker 中继（Durable Object 做请求队列），`wrangler deploy` 一条命令上线 |
| `space/` | 看板（Muse Space）的动作规格说明：数据模型、卖出直调流程、每 10 分钟纯脚本同步 |
| `docs/protocol.md` | 中继协议与签名规范（语言无关，自己实现看板端也可以照这个对接） |

## 快速开始

1. **部署中继**：`cd relay && npx wrangler deploy`，再把随机生成的 64 位十六进制钥匙设进去：`npx wrangler secret put RELAY_KEY`。记下你的 `https://baw-relay.<子域名>.workers.dev`。
2. **生成桥签名密钥**：再随机生成一把 64 位十六进制（和中继钥匙不是同一把）。
3. **放桥**：把 `bridge/` 放进你的钱包工作区（布局见 `bridge/README.md`），按 `bridge-secret.txt.example`、`bridge-relay.json.example` 建好两个配置文件（真值切勿提交仓库），双击 `install.command`。
4. **建看板**：按 `space/README.md` 的动作规格在 Muse 里建 Space，服务端常量填中继地址、两把钥匙。
5. **验证**：看板点「同步」，27 个号（或你的号数）的余额和持仓进来；点一笔小额卖出，状态当场变「已卖出」带交易哈希。

## 安全须知

- 两把钥匙（中继钥匙、桥签名密钥）只放在三处：中继的 secret、桥的本地文件、看板服务端常量。绝不进前端代码、绝不进仓库、绝不打日志。
- Cloudflare 账号本身成了这套系统的一把钥匙：给账号开两步验证，别共用弱密码。
- 本系统只做「卖出到 BNB」一个动作。转账、提币、买入一律没有接口，不是没做按钮，是桥根本不实现。

## 免责声明

个人工具，按现状开源（MIT）。链上交易不可逆，先用小额验证再放量；作者不对使用后果负责。与 Binance 无隶属关系。

## License

MIT
