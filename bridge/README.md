# 卖出桥（bridge）

跑在本地 Mac 上的常驻程序（Python 3 标准库，零第三方依赖）：一头主动长轮询 Cloudflare 中继取活，一头调本地 `kit.py` 查余额、查持仓、卖币。只出不进，不开任何端口给外面（本地调试口 `127.0.0.1:18921` 除外）。

## 目录约定

桥不是独立软件，它住在你的币安钱包工作区里，依赖工作区现有的工具链：

```
<工作区根>/
├── .venv/bin/python        # 桥和 kit.py 共用的解释器
├── chain/
│   ├── kit.py              # 多号下单工具（见下「kit.py 接口约定」）
│   └── accounts.json       # {profile 名: 钱包地址}，只有公开地址
├── bin/bawp                # 币安 Agentic Wallet 的命令行包装（查持仓用）
└── bridge/
    ├── bridge.py           # 本文件
    ├── bridge-secret.txt   # 桥签名密钥（从 .example 复制改名后填真值）
    ├── bridge-relay.json   # 中继地址 + 中继钥匙（从 .example 复制改名后填真值）
    ├── install.command     # 双击：注册 launchd 常驻并启动
    ├── stop.command        # 双击：急停 + 取消常驻
    ├── outbox/             # balances.json / holdings.json / sessions.json 快照（每 10 分钟）
    └── results/            # 每张卖出单的结果落盘（幂等的依据）
```

## kit.py 接口约定

桥只通过两个子命令用 kit.py（你的实现只要满足这个约定即可）：

- `kit.py bal`：逐行输出每个号的 BNB 余额 JSON：`{"profile": "...", "bnb": 0.1, "st": "OK"}`
- `kit.py sell <profile> <token合约> <amount> --exec --slip 20`：真卖。要求自带两段保护：先试算（模拟），试算不过绝不广播；内部安全拦截（无限授权、改账户权限、超预算）一律拦下。输出一行 JSON，含 `st`（`OK` / `REVERTED` / `UNKNOWN` / `SKIP`）和成交时的 `tx`。

持仓走 `bin/bawp <profile> wallet balance --binanceChainId 56 --hideLowValue false --json`，返回 `{success, data: [{symbol, address, balance, price, value}]}`，零余额不返回。

## 运行

1. 建好 `bridge-secret.txt`、`bridge-relay.json`（真值只留本地，已在 `.gitignore` 里）。
2. 双击 `install.command`（launchd 标签 `com.baw.bridge`，跑在图形会话里——钱包登录态在图形会话的钥匙串里，普通 SSH 会话会报假掉线）。首次双击若被 macOS 拦，去「系统设置 → 隐私与安全性」拉到最下面点「仍要打开」。
3. 日志看 `bridge.log`；桥每 10 分钟写一次余额 + 持仓快照。
4. 更新桥：直接覆盖 `bridge.py`，它检测到文件变化会自己重跑新版，不用再双击。

## 安全规矩（代码里已写死）

- 卖出串行执行（全局锁），同一 id 只执行一次，重复请求回落盘旧结果。
- `STOP` 文件存在时 `/sell` 一律 423 拒绝。
- `st=UNKNOWN` 只如实回 `unknown_check_onchain`，绝不在桥内重试。
- 验签失败回 401；时间戳超出 ±120 秒回 401（防重放）。
- 配置和结果里都不落任何私钥、助记词——桥从头到尾接触不到。
