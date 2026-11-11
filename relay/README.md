# 中继（relay）

一个 Cloudflare Worker + Durable Object，把看板服务端的请求排队转给本地桥。桥在 NAT 后面、不开端口，靠长轮询主动来取活；中继本身不持有桥的签名密钥，只验一把外层钥匙。协议见 [`../docs/protocol.md`](../docs/protocol.md)。

## 部署

```bash
cd relay
npx wrangler deploy
# 生成一把随机钥匙（64 位十六进制）并设为 secret：
openssl rand -hex 32 | npx wrangler secret put RELAY_KEY
```

部署后得到 `https://baw-relay.<你的子域名>.workers.dev`，这个地址和钥匙分别填进桥的 `bridge-relay.json` 与看板的服务端常量。免费额度足够个人使用（Durable Object 用 SQLite 类，`wrangler.toml` 里已带 v1 迁移）。

注意：Worker 名字（`wrangler.toml` 的 `name`）可以随便改，改完三处配置（桥、看板）跟着填新地址即可。
