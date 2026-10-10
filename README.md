# MEXC 公开行情采集（验收中）

只读取固定官方 `https://api.mexc.com`，无交易所认证、账户访问或订单。
官方新主机的依据：[集成指南](https://www.mexc.com/api-docs/futures/integration-guide)、[域名更新公告](https://www.mexc.com/en-GB/announcements/article/futures-api-access-domain-update-17827791532974)。旧 `contract.mexc.com` 已停止支持；本包不使用旧域名、备用域名、代理或区域选择。

先请求公开 `/api/v1/contract/ping`，再请求 `/api/v1/contract/detail/country?symbol=BTC_USDT`。
元数据必须明确为 BTC 基础币、USDT 计价和结算、`futureType=1` 永续、`state=0`、`preMarket=false`、`type=1`。
不根据交易 API 的 `apiAllowed` 字段推断公共行情访问权限。缺失分类字段即停止，尚未读 K 线。
默认 `--mode metadata` 只执行这两项预检，结果只称 `metadata_validated`，不会称 healthy，也不写行情快照。

工作流显式使用 `--mode full`：元数据通过后请求 `Min60`，间隔 2100ms 请求 `Min5`，最后再次获取服务器时间。
K 线范围采用官方秒级 `start/end`，每次不超过 2000 根；归一化为 721 根小时 K 线（720 根完成＋当前）和 300 根 5 分钟 K 线（299 根完成＋当前）。
数据只用官方文档定义的标准 `open/high/low/close/vol` 作为该平台参考行情，不替换为语义未说明的 `real*` 字段，也不宣称每根 open 是保证的实际第一笔成交价。
完成状态由 `开盘时间+周期<=服务器时间` 推导，不能伪造交易所原生确认字段。
UTC 周期对齐、连续性、当前 K 线、价格/成交量、OHLC、时钟 120 秒偏差全部严格检查；final ping 若已跨界使当前 K 线缺失则失败，不补读或重试。

任何 HTTP403/429、其他网络失败、非零业务码（含510）立即停止；不打印响应正文、上游消息或头，不切换域名/IP，不在同小时再次尝试。
只有所有校验通过才原子替换 `snapshots/latest.json`；失败保留旧文件，其旧生成时间不会更新。

快照根严格只有八字段：`schemaVersion/source/instId/generatedAt/clock/hour/five/contract`。
`source=mexc`，`instId=BTC_USDT`；clock 为 `success:true, code:0, data:服务器毫秒整数`。
hour/five 为 `success:true, code:0, data:{time,open,high,low,close,vol}`；time 为秒级整数，其他数组值为非指数十进制字符串。
contract 的 data 是单个对象，仅保留上述八项官方分类字段，以及上游存在时的正十进制字符串 `contractSize`。
没有用户本金、策略、密钥、账户、消息正文或其他私有数据。诊断日志只含固定 summary，`readyForProduction=false` 仅表示采集器诊断不能证明生产监控已通过验收。

沿用原包的 `claim_hour.py`、`wait_for_window.py` 和共享 concurrency 组 `public-okx-hourly-market`，组名保留以避免旧、新运行并发。
公开运行数据文件只允许 `snapshots/latest.json` 与 `snapshots/last_attempt.json`；后者只有 UTC 小时与真实尝试时间。
schedule 和 dispatch 均先刷新公开 main，以非强制提交/推送成功认领本小时，才允许任何 ping（包括元数据诊断）。
失败保留 claim；同小时已有 claim 跳过；不重置 marker。推送不确定、跨小时或 marker 不匹配均停止。
仅公开 main、标准 Ubuntu runner、contents-write，job 上限 5 分钟。token 仅发布步骤进程内存使用，不写日志/文件。

每小时第11分调度，schedule 只在当前 UTC 小时12:15至13:30窗口采集；dispatch 首次验收不等待，但同受小时 claim。
Cron/CDN/提交和生产消费会延迟；任何旧快照必须由独立消费者拒绝。
此候选未证明托管节点允许 MEXC 公共访问，也未证明自动 Cron、Cloudflare 消费、推送或免费 CPU 限制全部通过。
MEXC 与币安是不同参考行情来源，生产切换还需 MEXC 同源 ATR 起点与真实验收，不能继续用 OKX 历史种子。

46 项离线检查已通过（所有网络、时钟、等待均使用 mock）：

```sh
python3 -m unittest discover -s tests -v
```

授权后在未认领的 UTC 小时执行一次全量诊断的命令：

```sh
python3 collector.py --mode full --output snapshots/latest.json --required-attempt-marker snapshots/last_attempt.json
```

marker 必须先由既有工作流成功认领，不能手写或重置来允许重试。本命令自身不认领小时。
手动成功不能冒充自动 Cron 成功。Cloudflare 生产监控不会因发布公共采集器而自动更改。

官方字段依据：[服务器时间](https://www.mexc.com/api-docs/futures/market-endpoints/get-server-time)、[合约信息](https://www.mexc.com/api-docs/futures/market-endpoints/get-contract-info)、[K线](https://www.mexc.com/api-docs/futures/market-endpoints/get-candlestick-data)、[错误码](https://www.mexc.com/api-docs/futures/error-code)。
